"""落盘的本地元数据仓库（支持重启恢复与多进程并发）。

本模块是 ``LocalStore`` 的持久化实现，接口与之一致，供 ``MetadataCatalog``
无缝替换。设计要点（详见 docs/persistence.md）：

* 数据文件位于仓库目录：``manifest.json``（代际指针）、``events-<gen>.log``
  （提交记录日志，append-only）、``snapshots/snap-<gen>.json``（压缩快照）。
* 每次提交的全部变更序列化为**一行** JSON 追加到事件日志并 ``fsync``：
  单条 write 小于 ``PIPE_BUF`` 时内核层面不交错，整行以换行符为提交边界，
  进程在中途被杀只会留下"没有换行的半行"，恢复时按整行回放，保证一个
  事务要么完整可见、要么完全不可见（原子提交）。
* 跨进程互斥使用 ``fcntl.flock``：写事务持排他锁（``LOCK_EX``），
  读路径持共享锁（``LOCK_SH``），锁只覆盖"重读日志尾部 + 判定 + 追加提交"
  这样的短临界区；常规读在内存索引上完成，不会长时间互相阻塞。
* 被业务拒绝的写入不会追加任何记录，因此磁盘上不可能出现"被拒却生效"。
* 日志膨胀后在排他锁内做快照压缩：先写临时文件、``fsync``、``rename``
  原子替换、再推进 manifest 代际，崩溃在任何一步都能回到上一个完整状态。
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import (
    AssetSnapshot,
    AssetType,
    FieldRecord,
    FieldSpec,
    FieldType,
    LineageEdge,
)

#: 磁盘文件格式版本。不兼容地改动 JSON 布局时必须递增，读取方据此拒绝加载。
FORMAT_VERSION = 1

#: 触发快照压缩的事件条数阈值。
COMPACT_ENTRY_THRESHOLD = 2000

#: 触发快照压缩的日志字节阈值（约 8MiB）。
COMPACT_BYTES_THRESHOLD = 8 * 1024 * 1024


# --------------------------------------------------------------------------
# JSON 编解码：内存模型 <-> 磁盘记录。磁盘上统一使用一行一条 JSON 的文本，
# JSON 本身保证转义后记录内不会出现裸换行符，因此 '\n' 可作为提交边界。
# --------------------------------------------------------------------------
def _asset_to_json(snapshot: AssetSnapshot) -> dict:
    return {
        "name": snapshot.name,
        "asset_type": snapshot.asset_type.value,
        "description": snapshot.description,
        "schema_version": snapshot.schema_version,
        "fields": [
            {
                "name": name,
                "field_type": rec.spec.field_type.value,
                "nullable": rec.spec.nullable,
                "description": rec.spec.description,
                "since_version": rec.since_version,
            }
            for name, rec in sorted(snapshot.fields.items())
        ],
        "owner": snapshot.owner,
        "tags": sorted(snapshot.tags),
        "governance_version": snapshot.governance_version,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
    }


def _asset_from_json(data: dict) -> AssetSnapshot:
    return AssetSnapshot(
        name=data["name"],
        asset_type=AssetType(data["asset_type"]),
        description=data.get("description", ""),
        schema_version=data["schema_version"],
        fields={
            f["name"]: FieldRecord(
                spec=FieldSpec(
                    name=f["name"],
                    field_type=FieldType(f["field_type"]),
                    nullable=f.get("nullable", True),
                    description=f.get("description", ""),
                ),
                since_version=f.get("since_version", 1),
            )
            for f in data.get("fields", [])
        },
        owner=data.get("owner"),
        tags=set(data.get("tags", [])),
        governance_version=data.get("governance_version", 0),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _edge_to_json(edge: LineageEdge) -> dict:
    return {
        "upstream": edge.upstream,
        "downstream": edge.downstream,
        "edge_type": edge.edge_type,
    }


def _edge_from_json(data: dict) -> LineageEdge:
    return LineageEdge(
        upstream=data["upstream"],
        downstream=data["downstream"],
        edge_type=data.get("edge_type", "derived"),
    )


def _ops_to_record(txn: int, ops: list[tuple]) -> dict:
    """把一个已提交事务的全部变更编码为单条日志记录。

    op 形如 ``("put_asset", snapshot)`` / ``("remove_asset", name)`` /
    ``("add_edge", edge)`` / ``("remove_edge", (u, d))``。
    """
    encoded = []
    for op, payload in ops:
        if op == "put_asset":
            encoded.append({"op": "put_asset", "asset": _asset_to_json(payload)})
        elif op == "remove_asset":
            encoded.append({"op": "remove_asset", "name": payload})
        elif op == "add_edge":
            encoded.append({"op": "add_edge", "edge": _edge_to_json(payload)})
        elif op == "remove_edge":
            encoded.append(
                {"op": "remove_edge", "upstream": payload[0], "downstream": payload[1]}
            )
        else:  # pragma: no cover - 内部一致性
            raise StoreCorruptionError(f"未知的提交操作类型: {op}")
    return {"txn": txn, "ops": encoded}


def _record_to_ops(record: dict) -> tuple[int, list[tuple]]:
    """解析单条日志记录为 ``(事务号, 变更列表)``。"""
    ops: list[tuple] = []
    for entry in record["ops"]:
        op = entry["op"]
        if op == "put_asset":
            ops.append((op, _asset_from_json(entry["asset"])))
        elif op == "remove_asset":
            ops.append((op, entry["name"]))
        elif op == "add_edge":
            ops.append((op, _edge_from_json(entry["edge"])))
        elif op == "remove_edge":
            ops.append((op, (entry["upstream"], entry["downstream"])))
        else:
            raise StoreCorruptionError(f"日志中存在未知操作: {op}")
    return int(record["txn"]), ops


def _dumps_line(record: dict) -> bytes:
    """记录序列化为单行 JSON 字节（含换行边界）。"""
    return (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _loads_line(line: str) -> dict:
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise StoreCorruptionError(f"日志记录不是合法 JSON: {exc}") from exc


class StoreCorruptionError(Exception):
    """仓库数据文件损坏或格式版本不兼容。"""


class PersistentStore:
    """落盘的本地元数据仓库，接口与 :class:`LocalStore` 一致。

    :param repo_dir: 仓库目录；不存在会自动初始化。同一目录可被多个进程同时
        打开，进程间通过 ``flock`` 互斥。
    """

    def __init__(self, repo_dir: str | os.PathLike) -> None:
        self._root = Path(repo_dir)
        self._snap_dir = self._root / "snapshots"
        self._manifest_path = self._root / "manifest.json"
        self._lock_path = self._root / "lock"

        self._root.mkdir(parents=True, exist_ok=True)
        self._snap_dir.mkdir(exist_ok=True)

        # 进程内可重入锁：同一进程多线程访问本实例时保护内存态。
        self._rlock = threading.RLock()
        # 每线程的事务/读锁状态，避免嵌套时重复 flock。
        self._tl = threading.local()

        # flock 关联的是"打开文件描述项"而非进程，故 EX/SH 各持一个 fd。
        self._ex_fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        self._sh_fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        self._closed = False

        # 内存态（_rlock 保护）。
        self._gen: int = 0
        self._txn: int = 0
        self._assets: dict[str, AssetSnapshot] = {}
        self._edges: dict[tuple[str, str], LineageEdge] = {}
        self._forward: dict[str, set[str]] = {}
        self._event_bytes: int = 0
        self._event_entries: int = 0

        self._open_existing()

    # ------------------------------------------------------------------
    # 跨进程 flock（可重入；事务内的读不再重复申请 SH 锁）
    # ------------------------------------------------------------------
    def _lock_ex(self) -> None:
        depth = getattr(self._tl, "ex_depth", 0)
        if depth == 0:
            fcntl.flock(self._ex_fd, fcntl.LOCK_EX)
        self._tl.ex_depth = depth + 1

    def _unlock_ex(self) -> None:
        depth = self._tl.ex_depth - 1
        self._tl.ex_depth = depth
        if depth == 0:
            fcntl.flock(self._ex_fd, fcntl.LOCK_UN)

    def _lock_sh(self) -> None:
        # 当前线程已持排他锁时，共享访问天然被覆盖。
        if getattr(self._tl, "ex_depth", 0) > 0:
            self._tl.sh_nested = getattr(self._tl, "sh_nested", 0) + 1
            return
        depth = getattr(self._tl, "sh_depth", 0)
        if depth == 0:
            fcntl.flock(self._sh_fd, fcntl.LOCK_SH)
        self._tl.sh_depth = depth + 1

    def _unlock_sh(self) -> None:
        if getattr(self._tl, "sh_nested", 0) > 0:
            self._tl.sh_nested -= 1
            return
        depth = self._tl.sh_depth - 1
        self._tl.sh_depth = depth
        if depth == 0:
            fcntl.flock(self._sh_fd, fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # 文件布局工具
    # ------------------------------------------------------------------
    def _events_path(self, gen: int) -> Path:
        return self._root / f"events-{gen}.log"

    def _snap_path(self, gen: int) -> Path:
        return self._snap_dir / f"snap-{gen}.json"

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _atomic_write(self, path: Path, data: bytes) -> None:
        """临时文件 + fsync + rename + 目录 fsync 的原子落盘。"""
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        self._fsync_dir(path.parent)

    def _write_manifest(self, gen: int) -> None:
        body = json.dumps(
            {"format_version": FORMAT_VERSION, "gen": gen},
            separators=(",", ":"),
        ).encode("utf-8")
        self._atomic_write(self._manifest_path, body)

    def _read_manifest(self) -> int:
        try:
            raw = self._manifest_path.read_bytes()
        except FileNotFoundError:
            return 0
        manifest = json.loads(raw)
        version = manifest.get("format_version")
        if version != FORMAT_VERSION:
            raise StoreCorruptionError(
                f"仓库格式版本不兼容: 文件版本={version}, "
                f"当前软件支持的版本={FORMAT_VERSION}"
            )
        return int(manifest["gen"])

    def _trim_torn_tail(self, gen: int) -> None:
        """裁掉当前代日志末尾没有换行边界的半行（崩溃中未提交的写入）。"""
        path = self._events_path(gen)
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return
        try:
            size = os.fstat(fd).st_size
            if size == 0:
                return
            last_nl = -1
            offset = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                nl = chunk.rfind(b"\n")
                if nl >= 0:
                    last_nl = offset + nl
                offset += len(chunk)
            keep = last_nl + 1  # 保留到最后一个换行（含）；无换行则为 0
            if keep < size:
                os.ftruncate(fd, keep)
                os.fsync(fd)
        finally:
            os.close(fd)

    def _load_gen(self, gen: int) -> None:
        """从快照 + 事件日志完整加载 ``gen`` 代到内存态。"""
        assets: dict[str, AssetSnapshot] = {}
        edges: dict[tuple[str, str], LineageEdge] = {}
        forward: dict[str, set[str]] = {}
        last_txn = 0

        snap_path = self._snap_path(gen)
        if snap_path.exists():
            snap = json.loads(snap_path.read_bytes())
            if snap.get("format_version") != FORMAT_VERSION:
                raise StoreCorruptionError(
                    f"快照 {snap_path} 格式版本不兼容: "
                    f"{snap.get('format_version')}"
                )
            if int(snap["gen"]) != gen:
                raise StoreCorruptionError(
                    f"快照代际与清单不一致: snap={snap['gen']} manifest={gen}"
                )
            for item in snap.get("assets", []):
                snapshot = _asset_from_json(item)
                assets[snapshot.name] = snapshot
            for item in snap.get("edges", []):
                edge = _edge_from_json(item)
                edges[edge.key()] = edge
                forward.setdefault(edge.upstream, set()).add(edge.downstream)
            last_txn = int(snap.get("last_txn", 0))

        entries = 0
        event_bytes = 0
        path = self._events_path(gen)
        if path.exists():
            raw = path.read_bytes()
            event_bytes = len(raw)
            for line in raw.splitlines():
                if not line.strip():
                    continue
                record = _loads_line(line.decode("utf-8"))
                txn, ops = _record_to_ops(record)
                if txn != last_txn + 1:
                    raise StoreCorruptionError(
                        f"事件日志事务号不连续: 期望 {last_txn + 1}, 实际 {txn}"
                    )
                last_txn = txn
                entries += 1
                self._apply_ops(assets, edges, ops, forward)

        self._gen = gen
        self._txn = last_txn
        self._assets = assets
        self._edges = edges
        self._forward = forward
        self._event_entries = entries
        self._event_bytes = event_bytes

    @staticmethod
    def _apply_ops(
        assets: dict[str, AssetSnapshot],
        edges: dict[tuple[str, str], LineageEdge],
        ops: list[tuple],
        forward: dict[str, set[str]] | None = None,
    ) -> None:
        for op, payload in ops:
            if op == "put_asset":
                assets[payload.name] = payload
            elif op == "remove_asset":
                assets.pop(payload, None)
            elif op == "add_edge":
                edges[payload.key()] = payload
                if forward is not None:
                    forward.setdefault(payload.upstream, set()).add(payload.downstream)
            elif op == "remove_edge":
                existed = edges.pop(payload, None)
                if existed is not None and forward is not None:
                    forward.get(payload[0], set()).discard(payload[1])

    def _remove_stray_files(self, gen: int) -> None:
        """清理崩溃的初始化/压缩遗留：临时文件、旧代日志与快照。"""
        for path in self._root.glob("*.tmp.*"):
            path.unlink(missing_ok=True)
        for path in self._snap_dir.glob("*.tmp.*"):
            path.unlink(missing_ok=True)
        for path in self._root.glob("events-*.log"):
            try:
                g = int(path.stem.split("-")[1])
            except (IndexError, ValueError):
                continue
            if g != gen:
                path.unlink(missing_ok=True)
        for path in self._snap_dir.glob("snap-*.json"):
            try:
                g = int(path.stem.split("-")[1])
            except (IndexError, ValueError):
                continue
            if g != gen:
                path.unlink(missing_ok=True)

    def _open_existing(self) -> None:
        """跨进程排他地完成首次加载（或初始化新仓库）。"""
        with self._rlock:
            self._lock_ex()
            try:
                gen = self._read_manifest()
                if gen == 0:
                    # 清单是唯一的"已提交"指针；它缺失说明没有任何已提交数据，
                    # 其余文件只能是崩溃残留，清理后初始化第 1 代。
                    self._remove_stray_files(0)
                    self._atomic_write(self._events_path(1), b"")
                    self._write_manifest(1)
                    gen = 1
                self._trim_torn_tail(gen)
                self._load_gen(gen)
                self._remove_stray_files(gen)
            finally:
                self._unlock_ex()

    # ------------------------------------------------------------------
    # 内存态追平磁盘（调用时已持 SH/EX 跨进程锁）
    # ------------------------------------------------------------------
    def _catch_up(self, *, for_write: bool) -> None:
        """让内存态追上磁盘上最后一个**已提交**事务。

        读端只应用到最后一个换行边界为止（半行 = 未提交，不可见）；
        写端额外负责把上一个崩溃进程遗留的半行裁掉。
        """
        gen = self._read_manifest()
        if gen != self._gen:
            # 发生过压缩代际切换：整载新一代。
            self._load_gen(gen)
            return
        path = self._events_path(gen)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            self._load_gen(gen)
            return
        if size < self._event_bytes:
            # 同代日志不应变短；出现即说明文件被动过，整载自证。
            self._load_gen(gen)
            return
        if size == self._event_bytes:
            return
        with open(path, "rb") as f:
            f.seek(self._event_bytes)
            tail = f.read()
        nl = tail.rfind(b"\n")
        committed = tail[: nl + 1] if nl >= 0 else b""
        if for_write and len(committed) < len(tail):
            # EX 锁内裁掉崩溃残留的未提交半行。
            self._trim_torn_tail(gen)
        last_txn = self._txn
        for raw in committed.splitlines():
            if not raw.strip():
                continue
            record = _loads_line(raw.decode("utf-8"))
            txn, ops = _record_to_ops(record)
            if txn != last_txn + 1:
                raise StoreCorruptionError(
                    f"事件日志事务号不连续: 期望 {last_txn + 1}, 实际 {txn}"
                )
            last_txn = txn
            self._apply_ops(self._assets, self._edges, ops, self._forward)
            self._event_entries += 1
        self._txn = last_txn
        self._event_bytes += len(committed)

    def _maybe_compact(self) -> None:
        """日志超过阈值时在排他锁内做快照压缩（调用时已持 EX 锁）。

        步骤严格按"先建新一代文件、再原子切换清单指针、最后删旧文件"排序，
        崩溃发生在任何一步，恢复后都指向一份完整状态：

        1. 写 ``snapshots/snap-<新代>.json``（tmp + fsync + rename）；
        2. 写空的 ``events-<新代>.log``；
        3. 原子推进 ``manifest.json`` 的代际指针——这是唯一切换点；
        4. 删除旧代日志与快照并 fsync 目录。
        """
        if (
            self._event_entries < COMPACT_ENTRY_THRESHOLD
            and self._event_bytes < COMPACT_BYTES_THRESHOLD
        ):
            return
        new_gen = self._gen + 1
        snapshot = {
            "format_version": FORMAT_VERSION,
            "gen": new_gen,
            "last_txn": self._txn,
            "assets": [
                _asset_to_json(a)
                for a in sorted(self._assets.values(), key=lambda x: x.name)
            ],
            "edges": [
                _edge_to_json(e)
                for e in sorted(self._edges.values(), key=lambda x: x.key())
            ],
        }
        self._atomic_write(
            self._snap_path(new_gen),
            json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
        )
        self._atomic_write(self._events_path(new_gen), b"")
        # 指针切换：此后新读者只能看到新一代。
        self._write_manifest(new_gen)
        old_events = self._events_path(self._gen)
        old_snap = self._snap_path(self._gen)
        old_events.unlink(missing_ok=True)
        old_snap.unlink(missing_ok=True)
        self._fsync_dir(self._root)
        self._fsync_dir(self._snap_dir)

        self._gen = new_gen
        self._event_entries = 0
        self._event_bytes = 0

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        """跨进程共享读临界区：进入时追平到最后一个已提交事务。

        多个读事务可并发（``LOCK_SH``），且不会阻塞彼此；仅与写事务
        （``LOCK_EX``）互斥，互斥窗口只覆盖短临界区，避免大范围长阻塞。
        """
        with self._rlock:
            self._lock_sh()
            try:
                self._catch_up(for_write=False)
                yield
            finally:
                self._unlock_sh()

    def _in_txn(self) -> bool:
        return getattr(self._tl, "txn_depth", 0) > 0

    # ---- 与 LocalStore 一致的接口 ----
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """进入跨进程排他的写事务。

        临界区内完成"追平磁盘 -> 检查 -> 写入暂存"；正常退出时把本事务的
        全部变更序列化为**一条**记录追加落盘（单次 write + fsync），因此
        事务具有原子性：要么整条可见，要么完全不可见。异常退出时暂存被
        丢弃，内存活动态从未被直接修改（事务内读到的是深拷贝工作副本）。
        支持嵌套：内层事务并入外层，仅最外层提交一次。
        """
        with self._rlock:
            depth = getattr(self._tl, "txn_depth", 0)
            if depth == 0:
                self._lock_ex()
                self._catch_up(for_write=True)
                self._tl.txn_depth = 1
                self._tl.asset_writes = {}
                self._tl.edge_adds = {}
                self._tl.edge_removes = set()
                committed = False
                try:
                    yield
                    self._commit_pending()
                    committed = True
                finally:
                    self._tl.txn_depth = 0
                    self._tl.asset_writes = None
                    self._tl.edge_adds = None
                    self._tl.edge_removes = None
                    if not committed:
                        # 异常路径：丢弃暂存。活动态从未被改动。
                        pass
                    self._unlock_ex()
            else:
                self._tl.txn_depth = depth + 1
                try:
                    yield
                finally:
                    self._tl.txn_depth = depth

    def _commit_pending(self) -> None:
        """把暂存的变更原子追加到事件日志并更新内存态（已持 EX 锁）。"""
        asset_writes: dict[str, AssetSnapshot] = self._tl.asset_writes
        edge_adds: dict[tuple[str, str], LineageEdge] = self._tl.edge_adds
        edge_removes: set[tuple[str, str]] = self._tl.edge_removes

        ops: list[tuple] = [
            ("put_asset", snapshot) for snapshot in asset_writes.values()
        ]
        ops.extend(("add_edge", edge) for edge in edge_adds.values())
        ops.extend(("remove_edge", key) for key in sorted(edge_removes))
        if not ops:
            return

        next_txn = self._txn + 1
        line = _dumps_line(_ops_to_record(next_txn, ops))
        path = self._events_path(self._gen)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            written = os.write(fd, line)
            if written != len(line):
                raise OSError("事件日志短写，提交失败")
            os.fsync(fd)
        finally:
            os.close(fd)

        self._txn = next_txn
        # 工作副本转正为活动态（与 LocalStore 把同一对象放入 dict 的语义一致）。
        self._apply_ops(self._assets, self._edges, ops, self._forward)
        self._event_entries += 1
        self._event_bytes += len(line)
        self._maybe_compact()

    # ---- 资产 ----
    def get_asset(self, name: str) -> AssetSnapshot | None:
        if self._in_txn():
            writes: dict[str, AssetSnapshot] = self._tl.asset_writes
            if name in writes:
                # 本事务已改过：返回同一份工作副本，保证"自己的写入可见"。
                return writes[name]
            live = self._assets.get(name)
            if live is None:
                return None
            # 只读检出返回深拷贝，但**不**暂存：业务层仅在真正修改后才会
            # 调用 put_asset，避免血缘校验等只读路径产生冗余落盘。
            return copy.deepcopy(live)

        with self._rlock:
            self._lock_sh()
            try:
                self._catch_up(for_write=False)
                snapshot = self._assets.get(name)
                return copy.deepcopy(snapshot) if snapshot is not None else None
            finally:
                self._unlock_sh()

    def put_asset(self, snapshot: AssetSnapshot) -> None:
        if self._in_txn():
            self._tl.asset_writes[snapshot.name] = snapshot
            return
        with self.transaction():
            self._tl.asset_writes[snapshot.name] = snapshot

    def list_assets(self) -> list[AssetSnapshot]:
        if self._in_txn():
            merged = dict(self._assets)
            merged.update(self._tl.asset_writes)
            # 返回深拷贝，避免调用方拿到活动态对象的别名在事务外/异常路径改动它。
            return [copy.deepcopy(a) for a in merged.values()]
        with self._rlock:
            self._lock_sh()
            try:
                self._catch_up(for_write=False)
                return [copy.deepcopy(a) for a in self._assets.values()]
            finally:
                self._unlock_sh()

    # ---- 血缘 ----
    def has_edge(self, upstream: str, downstream: str) -> bool:
        key = (upstream, downstream)
        if self._in_txn():
            if key in self._tl.edge_removes:
                return False
            if key in self._tl.edge_adds:
                return True
            return key in self._edges
        with self._rlock:
            self._lock_sh()
            try:
                self._catch_up(for_write=False)
                return key in self._edges
            finally:
                self._unlock_sh()

    def add_edge(self, edge: LineageEdge) -> None:
        key = edge.key()
        if self._in_txn():
            self._tl.edge_removes.discard(key)
            self._tl.edge_adds[key] = edge
            return
        with self.transaction():
            self._tl.edge_removes.discard(key)
            self._tl.edge_adds[key] = edge

    def remove_edge(self, upstream: str, downstream: str) -> None:
        key = (upstream, downstream)
        if self._in_txn():
            self._tl.edge_adds.pop(key, None)
            self._tl.edge_removes.add(key)
            return
        with self.transaction():
            self._tl.edge_adds.pop(key, None)
            self._tl.edge_removes.add(key)

    def list_edges(self) -> list[LineageEdge]:
        if self._in_txn():
            merged = dict(self._edges)
            merged.update(self._tl.edge_adds)
            for key in self._tl.edge_removes:
                merged.pop(key, None)
            return list(merged.values())
        with self._rlock:
            self._lock_sh()
            try:
                self._catch_up(for_write=False)
                return list(self._edges.values())
            finally:
                self._unlock_sh()

    def downstream_of(self, upstream: str) -> list[str]:
        """该节点当前的直接下游（增量正向索引，O(度)）。

        写事务内合并本事务待提交的增删边，使批次内先前通过的边对后续
        成环判定可见；活动索引只在提交时才更新，中止则不留痕。
        """
        if self._in_txn():
            neighbors = set(self._forward.get(upstream, set()))
            for (u, d) in self._tl.edge_adds:
                if u == upstream:
                    neighbors.add(d)
            for (u, d) in self._tl.edge_removes:
                if u == upstream:
                    neighbors.discard(d)
            return sorted(neighbors)
        with self._rlock:
            self._lock_sh()
            try:
                self._catch_up(for_write=False)
                return sorted(self._forward.get(upstream, set()))
            finally:
                self._unlock_sh()

    # ---- 调试/测试辅助 ----
    def reset(self) -> None:
        """清空仓库并回到第 1 代空状态（测试与本地重建使用）。"""
        with self._rlock:
            self._lock_ex()
            try:
                for path in self._root.iterdir():
                    if path.name == "lock" or path.name == "snapshots":
                        continue
                    path.unlink()
                for path in self._snap_dir.glob("*"):
                    path.unlink()
                self._atomic_write(self._events_path(1), b"")
                self._write_manifest(1)
                self._load_gen(1)
            finally:
                self._unlock_ex()

    def close(self) -> None:
        with self._rlock:
            if self._closed:
                return
            os.close(self._ex_fd)
            os.close(self._sh_fd)
            self._closed = True
