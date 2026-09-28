"""本地磁盘持久化仓库（跨进程可用）。

以"每个资产一个数据文件、每条血缘边一个数据文件"的方式把仓库落到本地目录：

* 写入采用"临时文件 + fsync + 原子 rename + 目录 fsync"，进程在写入中途
  被杀掉或报错后，磁盘上要么是写入前的完整文件、要么是写入后的完整文件，
  不会留下只写了一半的残缺结构；
* 仓库级互斥通过 ``fcntl.flock`` 实现（读共享/写独占），多个进程打开同一
  仓库目录时，"检查 + 写入"仍然在同一跨进程临界区内完成；
* 进程内的事务模式与重入深度是**每线程**状态（见 :mod:`metadata_catalog.locking`），
  flock 描述符则是进程共享资源，配合进程内读写锁做引用计数；
* 读取维护进程内缓存，只在目录发生变化时才重新扫描并读取被改动的文件，
  仓库涨到上万量级时常规查询不会每次都全量读盘。

文件布局::

    <repo>/
      FORMAT                      # 仓库格式版本标记
      lock                        # 跨进程 flock 锁文件
      assets/<name>.json          # 每个资产的完整快照（原子替换）
      edges/<sha1>.json           # 每条血缘边一份（原子替换）

所有数据文件都是 JSON，带显式的 ``format_version``，仅支持同一大版本互操作。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import quote

from .errors import (
    StorageCorruptionError,
    StorageError,
    UnsupportedStorageFormatError,
)
from .locking import EXCLUSIVE, NONE, SHARED, TransactionGate
from .models import (
    AssetSnapshot,
    AssetType,
    FieldRecord,
    FieldSpec,
    FieldType,
    LineageEdge,
)

#: 当前仓库磁盘格式版本。仅同大版本（FORMAT_VERSION=1）的数据文件可读写。
FORMAT_VERSION = 1

#: 资产名直接编码的文件名长度上限（字节，不含前缀与扩展名）；超过则走哈希分支。
_NAME_DIRECT_LIMIT = 180

_ASSET_PREFIX = "N"  # 文件名形如 N<percent-encoded name>.json
_ASSET_HASH_PREFIX = "X"  # 文件名形如 X<sha256 hex>.json
_EDGE_PREFIX = "E"  # 文件名形如 E<sha1 hex>.json
_TMP_PREFIX = ".tmp."

#: 测试用故障注入：写入临时文件之后、原子 rename 之前触发。
#: "kill"  -> os._exit 模拟进程在写入中途被杀；
#: "raise" -> 抛 OSError 模拟写入中途报错。
_CRASH_ENV = "METADATA_CATALOG_CRASH_AFTER_WRITE"


def asset_filename(name: str) -> str:
    """资产名 -> assets/ 下的文件名（不含目录）。

    短名做可逆的 percent-encoding（``N...``）；超长名退化为内容定址的
    哈希名（``X...``），避免超过文件系统 255 字节的单文件名限制。
    两个前缀互不相交，因此两种分支之间不可能撞名。
    """
    encoded = quote(name, safe="")
    if len(encoded.encode("utf-8")) <= _NAME_DIRECT_LIMIT:
        return f"{_ASSET_PREFIX}{encoded}.json"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"{_ASSET_HASH_PREFIX}{digest}.json"


def edge_filename(edge: LineageEdge) -> str:
    """血缘边 -> edges/ 下的文件名（不含目录）。

    边的身份是三元组 ``(upstream, downstream, edge_type)`` 的哈希，
    哈希定址天然避免重复边产生多份文件。
    """
    raw = "\0".join([edge.upstream, edge.downstream, edge.edge_type]).encode(
        "utf-8"
    )
    return f"{_EDGE_PREFIX}{hashlib.sha1(raw).hexdigest()}.json"


def _check_format_version(obj: object, *, where: str) -> None:
    if not isinstance(obj, dict) or "kind" not in obj:
        raise StorageCorruptionError(f"数据文件内容不是合法记录: {where}")
    version = obj.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise StorageCorruptionError(f"数据文件缺少 format_version: {where}")
    if version != FORMAT_VERSION:
        raise UnsupportedStorageFormatError(
            f"数据文件格式版本 {version} 不受支持（当前仅支持 "
            f"v{FORMAT_VERSION}）: {where}",
            version=version,
            supported=FORMAT_VERSION,
        )


def serialize_asset(snapshot: AssetSnapshot) -> bytes:
    """资产快照 -> 可落盘的 JSON 字节（UTF-8、键顺序固定）。"""
    payload = {
        "format_version": FORMAT_VERSION,
        "kind": "asset",
        "name": snapshot.name,
        "asset_type": snapshot.asset_type.value,
        "description": snapshot.description,
        "schema_version": snapshot.schema_version,
        "governance_version": snapshot.governance_version,
        "owner": snapshot.owner,
        "tags": sorted(snapshot.tags),
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
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def deserialize_asset(raw: bytes, *, where: str = "<bytes>") -> AssetSnapshot:
    """JSON 字节 -> 资产快照；任何解析/结构异常都归类为数据损坏。"""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageCorruptionError(f"资产数据文件无法解析: {where}") from exc
    _check_format_version(obj, where=where)
    if obj.get("kind") != "asset":
        raise StorageCorruptionError(f"数据文件 kind 不是 asset: {where}")
    try:
        fields: dict[str, FieldRecord] = {}
        for item in obj["fields"]:
            spec = FieldSpec(
                name=item["name"],
                field_type=FieldType(item["field_type"]),
                nullable=bool(item["nullable"]),
                description=item["description"],
            )
            since = int(item["since_version"])
            if spec.name in fields:
                raise StorageCorruptionError(
                    f"资产数据文件字段重复: {spec.name} ({where})"
                )
            fields[spec.name] = FieldRecord(spec=spec, since_version=since)
        snapshot = AssetSnapshot(
            name=str(obj["name"]),
            asset_type=AssetType(obj["asset_type"]),
            description=str(obj["description"]),
            schema_version=int(obj["schema_version"]),
            fields=fields,
            owner=obj["owner"],
            tags=set(obj["tags"]),
            governance_version=int(obj["governance_version"]),
            created_at=float(obj["created_at"]),
            updated_at=float(obj["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageCorruptionError(f"资产数据文件内容不完整: {where}") from exc
    # 文件名与内容的身份一致性检查：防止外部错放/误改名造成张冠李戴。
    if where.endswith(".json") and os.path.basename(where) != asset_filename(
        snapshot.name
    ):
        raise StorageCorruptionError(
            f"资产数据文件名与内容中的资产名不一致: {where}"
        )
    return snapshot


def serialize_edge(edge: LineageEdge) -> bytes:
    """血缘边 -> 可落盘的 JSON 字节。"""
    payload = {
        "format_version": FORMAT_VERSION,
        "kind": "lineage_edge",
        "upstream": edge.upstream,
        "downstream": edge.downstream,
        "edge_type": edge.edge_type,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def deserialize_edge(raw: bytes, *, where: str = "<bytes>") -> LineageEdge:
    """JSON 字节 -> 血缘边；任何解析/结构异常都归类为数据损坏。"""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageCorruptionError(f"血缘数据文件无法解析: {where}") from exc
    _check_format_version(obj, where=where)
    if obj.get("kind") != "lineage_edge":
        raise StorageCorruptionError(
            f"数据文件 kind 不是 lineage_edge: {where}"
        )
    try:
        edge = LineageEdge(
            upstream=str(obj["upstream"]),
            downstream=str(obj["downstream"]),
            edge_type=str(obj["edge_type"]),
        )
    except (KeyError, TypeError) as exc:
        raise StorageCorruptionError(f"血缘数据文件内容不完整: {where}") from exc
    if where.endswith(".json") and os.path.basename(where) != edge_filename(edge):
        raise StorageCorruptionError(
            f"血缘数据文件名与边内容不一致: {where}"
        )
    return edge


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: str, data: bytes, *, fsync_dir: bool = True) -> None:
    """临时文件 + fsync + 原子 rename 地写入 ``path``。

    保证磁盘上的目标文件永远是某一份**完整内容**：rename 之前目标保持旧版本
    （或不存在），rename 之后目标即新版本。失败时清理临时文件，绝不残留半成品
    在正式文件名上。可选的目录 fsync 保证 rename 在断电后也已落盘。
    """
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=_TMP_PREFIX, dir=directory)
    try:
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            # fdopen 失败时 fd 未被接管，需要手动关闭。
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        crash = os.environ.get(_CRASH_ENV)
        if crash:
            if crash == "kill":
                os._exit(42)
            raise OSError(f"注入的写入失败（rename 之前）: {path}")

        os.replace(tmp_path, path)
        if fsync_dir:
            _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


class PersistentStore:
    """落盘在本地目录、支持多进程共享的元数据仓库。

    线程/进程模型：

    * 进程内用 :class:`~metadata_catalog.locking.TransactionGate` 实现
      每线程可重入的多读单写（事务模式与嵌套深度按线程隔离）；
    * 跨进程用 ``fcntl.flock`` 在同一 ``lock`` 文件上加共享/排他锁；
      flock 与打开文件描述关联、整个进程共享一把，因此配合进程内锁做
      引用计数：第一个读者加共享锁、最后一个读者释放；写者由进程内
      写锁保证是"唯一持锁线程"，直接加排他锁；
    * 每次进入**最外层**临界区都在持 flock 的状态下把磁盘变化增量同步进
      进程内缓存，因此一个进程提交的写入，另一个进程在下一次临界区一定
      能读到。

    读操作必须在 :meth:`read_transaction`（或 :meth:`transaction`）内调用；
    写操作必须在 :meth:`transaction`（排他）内调用。这与服务层"检查 +
    写入同一临界区"的用法一致。
    """

    def __init__(self, repo_dir: str | os.PathLike[str]) -> None:
        self.repo_dir = os.path.abspath(os.fspath(repo_dir))
        self.assets_dir = os.path.join(self.repo_dir, "assets")
        self.edges_dir = os.path.join(self.repo_dir, "edges")
        self.lock_path = os.path.join(self.repo_dir, "lock")
        self.format_path = os.path.join(self.repo_dir, "FORMAT")

        try:
            os.makedirs(self.assets_dir, exist_ok=True)
            os.makedirs(self.edges_dir, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"无法创建仓库目录: {self.repo_dir}") from exc

        # flock 锁文件描述符（不参与读写，只承载锁）。
        try:
            self._lock_fd = os.open(
                self.lock_path, os.O_RDWR | os.O_CREAT, 0o644
            )
        except OSError as exc:
            raise StorageError(f"无法创建仓库锁文件: {self.lock_path}") from exc

        self._closed = False

        # flock 引用计数：fd 是进程共享资源，多个读线程在同一进程内只加
        # 一次 LOCK_SH；写者由进程内写锁保证独占唯一。
        self._flock_guard = threading.Lock()
        self._flock_shared_count = 0

        self._gate = TransactionGate(
            on_outer_enter=self._on_outer_enter,
            on_outer_exit=self._on_outer_exit,
        )

        # 进程内缓存：正式文件名 -> (stat 签名, 反序列化对象)
        self._asset_cache: dict[str, tuple[tuple, AssetSnapshot]] = {}
        self._edge_cache: dict[str, tuple[tuple, LineageEdge]] = {}
        # 各数据目录最近一次同步时的签名（mtime_ns, ctime_ns）。
        # 配合 _dir_synced 判断是否做过首次同步：从未同步、或签名变化
        # （含目录曾消失又出现）时都必须重新扫描。
        self._dir_sigs: dict[str, tuple | None] = {}
        self._dir_synced: set[str] = set()
        # 构造期（FORMAT 初始化/临时文件清扫）持锁但不装载数据，避免仓库
        # 一打开就因单个损坏文件而无法启动；首个业务临界区才首次同步。
        self._refresh_enabled = False

        # 仓库首次初始化与残留临时文件清扫都必须在跨进程排他区内完成：
        # 否则可能与其他进程的首次初始化竞争，或把另一个存活进程正在写的
        # 临时文件误删掉。这里只初始化 FORMAT、清扫临时文件，不装载数据
        # （_refresh_enabled=False），首个业务事务进入时才做首次全量同步。
        init_failed = False
        try:
            with self._gate.transaction():
                self._init_format_file()
                self._sweep_temp_files()
        except BaseException:
            init_failed = True
            raise
        finally:
            self._refresh_enabled = True
            if init_failed:
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass

    # ---- 仓库初始化 ----
    def _init_format_file(self) -> None:
        """初始化/校验 FORMAT 标记。调用时已持跨进程排他锁。"""
        prefix = "metadata-catalog-repo format="
        if os.path.exists(self.format_path):
            try:
                with open(self.format_path, encoding="utf-8") as fh:
                    first = fh.readline().strip()
            except OSError as exc:
                raise StorageError(
                    f"FORMAT 标记文件无法读取: {self.format_path}"
                ) from exc
            if not first.startswith(prefix):
                raise StorageCorruptionError(
                    f"FORMAT 标记文件内容无法识别: {self.format_path}"
                )
            try:
                version = int(first[len(prefix):])
            except ValueError as exc:
                raise StorageCorruptionError(
                    f"FORMAT 标记文件中的版本号非法: {self.format_path}"
                ) from exc
            if version != FORMAT_VERSION:
                raise UnsupportedStorageFormatError(
                    f"仓库格式版本 {version} 不受支持（当前仅支持 "
                    f"v{FORMAT_VERSION}）: {self.repo_dir}",
                    version=version,
                    supported=FORMAT_VERSION,
                )
            return
        marker = (
            f"metadata-catalog-repo format={FORMAT_VERSION}\n"
            "# 本目录是元数据目录的本地持久化仓库；请勿手工改写数据文件。\n"
        ).encode("utf-8")
        try:
            atomic_write(self.format_path, marker, fsync_dir=True)
        except OSError as exc:
            raise StorageError(
                f"仓库 FORMAT 标记初始化失败: {self.format_path}"
            ) from exc

    def _sweep_temp_files(self) -> None:
        """清理上一次写入中途崩溃可能留下的临时文件。

        仅在构造仓库且持有跨进程排他锁时调用：此时其他打开同一仓库的
        进程无法处在写入中途，删除动作不会误伤存活写入。临时文件从未参与
        正式文件名，清理它们不影响任何已生效的状态。
        """
        for directory in (self.assets_dir, self.edges_dir):
            try:
                names = os.listdir(directory)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StorageError(f"仓库目录无法读取: {directory}") from exc
            for name in names:
                if name.startswith(_TMP_PREFIX):
                    try:
                        os.unlink(os.path.join(directory, name))
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        raise StorageError(
                            f"残留临时文件无法清理: {os.path.join(directory, name)}"
                        ) from exc

    # ---- 跨进程临界区（flock 引用计数） ----
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """排他临界区：进程内其他线程、其他进程都被挡在外面。可重入。"""
        if self._closed:
            raise StorageError("仓库已关闭，不能再开启事务")
        with self._gate.transaction():
            yield

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        """共享临界区：允许多个读者并发，但排斥任何写者。可重入。"""
        if self._closed:
            raise StorageError("仓库已关闭，不能再开启事务")
        with self._gate.read_transaction():
            yield

    def _on_outer_enter(self, mode: int) -> None:
        """拿到进程内锁后、事务生效前：挂 flock 并增量同步缓存。

        flock 与打开文件描述关联、由整个进程共享，它本身不是按线程计数
        的可重入锁：同一 fd 上若一个线程已持有 LOCK_SH，另一个线程再发
        LOCK_SH 会被内核当作"锁型转换竞争"而可能阻塞。因此所有 flock
        获取/释放都必须在 ``_flock_guard`` 内串行，保证每种锁型在本进程
        内只被真正获取一次，配合计数在用完时释放：

        * 共享：计数 0->1 的线程负责 LOCK_SH，其余线程只增计数
          （此时该线程已在进程内读锁保护下，且其他读者与其共享同一把
          跨进程共享锁，语义正确）；
        * 排他：进程内写锁已保证本进程没有任何读者/写者，直接 LOCK_EX。

        这同时消除了"线程 A 持 SH 阻塞在 EX 上、线程 B 又在等 A"这类
        同进程自锁。
        """
        with self._flock_guard:
            if mode == EXCLUSIVE:
                # 进程内写锁已保证此刻本进程没有其他读者/写者。
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            else:
                if self._flock_shared_count == 0:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_SH)
                self._flock_shared_count += 1
        try:
            if self._refresh_enabled:
                self._refresh_locked()
        except BaseException:
            with self._flock_guard:
                if mode == EXCLUSIVE:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                else:
                    self._flock_shared_count -= 1
                    if self._flock_shared_count == 0:
                        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            raise

    def _on_outer_exit(self, mode: int) -> None:
        """最外层事务释放时：解 flock（计数归零才真正解锁）。"""
        with self._flock_guard:
            if mode == EXCLUSIVE:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            else:
                self._flock_shared_count -= 1
                if self._flock_shared_count == 0:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    # ---- 磁盘 -> 缓存的增量同步（调用时已持 flock 与进程内锁） ----
    @staticmethod
    def _file_sig(path: str) -> tuple | None:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    @staticmethod
    def _dir_sig(path: str) -> tuple | None:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_ctime_ns)

    def _refresh_locked(self) -> None:
        self._refresh_dir(self.assets_dir, self._asset_cache, deserialize_asset)
        self._refresh_dir(self.edges_dir, self._edge_cache, deserialize_edge)

    def _refresh_dir(self, directory, cache, deserializer) -> None:
        current = self._dir_sig(directory)
        if (
            directory in self._dir_synced
            and current is not None
            and current == self._dir_sigs.get(directory)
        ):
            # 已做过首次同步且目录签名未变 -> 目录项没有增删改，跳过扫描。
            return
        # 先在新字典里完成全部读盘与反序列化，再整体替换缓存内容：
        # 即便与同进程其他读者的边界处理有疏漏，也不会让任何线程迭代到
        # 一张正在被增量改写的表。
        new_cache = dict(cache)
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            cache.clear()
            self._dir_sigs[directory] = current
            self._dir_synced.add(directory)
            return
        except OSError as exc:
            raise StorageError(f"仓库目录无法读取: {directory}") from exc
        live: set[str] = set()
        for name in names:
            if not name.endswith(".json"):
                continue
            live.add(name)
            path = os.path.join(directory, name)
            sig = self._file_sig(path)
            if sig is None:
                continue
            cached = new_cache.get(name)
            if cached is not None and cached[0] == sig:
                continue
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                raise StorageError(f"数据文件无法读取: {path}") from exc
            new_cache[name] = (sig, deserializer(raw, where=path))
        for gone in list(new_cache):
            if gone not in live:
                new_cache.pop(gone, None)
        cache.clear()
        cache.update(new_cache)
        self._dir_sigs[directory] = current
        self._dir_synced.add(directory)

    def _put_asset_locked(self, snapshot: AssetSnapshot) -> None:
        fname = asset_filename(snapshot.name)
        path = os.path.join(self.assets_dir, fname)
        try:
            atomic_write(path, serialize_asset(snapshot))
        except OSError as exc:
            # 磁盘故障归类为可区分的存储错误；注意此处尚未触碰缓存，
            # 旧快照在进程内与磁盘上都保持完整。
            if isinstance(exc, StorageError):
                raise
            raise StorageError(
                f"资产快照写入失败，仓库保持写入前状态: {path}"
            ) from exc
        sig = self._file_sig(path)
        assert sig is not None
        # 存独立副本：缓存对象不与调用方共享可变引用。
        self._asset_cache[fname] = (sig, snapshot.copy())
        self._dir_sigs[self.assets_dir] = self._dir_sig(self.assets_dir)

    # ---- 资产 ----
    def get_asset(self, name: str) -> AssetSnapshot | None:
        with self._read_scope():
            entry = self._asset_cache.get(asset_filename(name))
            if entry is not None and entry[1].name == name:
                return entry[1].copy()
            return None

    def put_asset(self, snapshot: AssetSnapshot) -> None:
        with self._write_scope():
            self._put_asset_locked(snapshot)

    def list_assets(self) -> list[AssetSnapshot]:
        with self._read_scope():
            return [entry[1].copy() for entry in self._asset_cache.values()]

    # ---- 血缘 ----
    def _find_edge(
        self, upstream: str, downstream: str
    ) -> tuple[str, LineageEdge] | None:
        wanted = (upstream, downstream)
        for fname, (_sig, edge) in list(self._edge_cache.items()):
            if (edge.upstream, edge.downstream) == wanted:
                return fname, edge
        return None

    def has_edge(self, upstream: str, downstream: str) -> bool:
        with self._read_scope():
            return self._find_edge(upstream, downstream) is not None

    def add_edge(self, edge: LineageEdge) -> None:
        with self._write_scope():
            fname = edge_filename(edge)
            path = os.path.join(self.edges_dir, fname)
            try:
                atomic_write(path, serialize_edge(edge))
            except OSError as exc:
                if isinstance(exc, StorageError):
                    raise
                raise StorageError(
                    f"血缘边写入失败，仓库保持写入前状态: {path}"
                ) from exc
            sig = self._file_sig(path)
            assert sig is not None
            self._edge_cache[fname] = (sig, edge)
            self._dir_sigs[self.edges_dir] = self._dir_sig(self.edges_dir)

    def remove_edge(self, upstream: str, downstream: str) -> None:
        with self._write_scope():
            found = self._find_edge(upstream, downstream)
            if found is None:
                return
            fname, _edge = found
            path = os.path.join(self.edges_dir, fname)
            try:
                os.unlink(path)
                _fsync_dir(self.edges_dir)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise StorageError(f"血缘边删除失败: {path}") from exc
            self._edge_cache.pop(fname, None)
            self._dir_sigs[self.edges_dir] = self._dir_sig(self.edges_dir)

    def list_edges(self) -> list[LineageEdge]:
        with self._read_scope():
            return [entry[1] for entry in self._edge_cache.values()]

    # ---- 维护 ----
    def reset(self) -> None:
        """清空仓库中全部资产与血缘边（仅供测试/维护使用）。"""
        with self._write_scope():
            for directory, cache in (
                (self.assets_dir, self._asset_cache),
                (self.edges_dir, self._edge_cache),
            ):
                try:
                    names = os.listdir(directory)
                except FileNotFoundError:
                    names = []
                except OSError as exc:
                    raise StorageError(f"仓库目录无法读取: {directory}") from exc
                for name in names:
                    if name.endswith(".json") or name.startswith(_TMP_PREFIX):
                        try:
                            os.unlink(os.path.join(directory, name))
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            raise StorageError(
                                f"仓库文件删除失败: {os.path.join(directory, name)}"
                            ) from exc
                try:
                    _fsync_dir(directory)
                except OSError as exc:
                    raise StorageError(f"仓库目录 fsync 失败: {directory}") from exc
                cache.clear()
                self._dir_sigs[directory] = self._dir_sig(directory)

    def close(self) -> None:
        """释放锁文件描述符。可重入；关闭后不能再开启事务。

        先取一次进程内排他事务，等待其他线程的在途事务结束，避免在它们
        仍持有 flock 时关闭描述符。
        """
        if self._closed:
            return
        with self._gate.transaction():
            self._closed = True
        try:
            os.close(self._lock_fd)
        except OSError:
            pass

    def __enter__(self) -> "PersistentStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 内部约束 ----
    @contextmanager
    def _read_scope(self) -> Iterator[None]:
        """已在任何临界区内则沿用；否则临时开一个共享临界区。"""
        if self._gate.held_mode() != NONE:
            yield
            return
        with self.read_transaction():
            yield

    @contextmanager
    def _write_scope(self) -> Iterator[None]:
        """已在排他临界区内则沿用；在共享临界区内属于实现错误；

        否则临时开一个排他临界区。这样公开方法可独立调用，而服务层显式
        事务内的"检查 + 多步写入"仍然共用同一个跨进程临界区。
        """
        mode = self._gate.held_mode()
        if mode == EXCLUSIVE:
            yield
            return
        if mode == SHARED:
            raise RuntimeError("写操作不能发生在读事务内")
        with self.transaction():
            yield
