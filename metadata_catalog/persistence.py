"""本地磁盘持久化仓库（跨进程可用）。

以"每个资产一个数据文件、每条血缘边一个数据文件"的方式把仓库落到本地目录：

* 写入采用"临时文件 + fsync + 原子 rename + 目录 fsync"，进程在写入中途
  被杀掉或报错后，磁盘上要么是写入前的完整文件、要么是写入后的完整文件，
  不会留下只写了一半的残缺结构；
* 仓库级互斥通过 ``fcntl.flock`` 实现（读共享/写独占），多个进程打开同一
  仓库目录时，"检查 + 写入"仍然在同一跨进程临界区内完成；
* 进程内并发由三层保证：

  - 跨进程 flock 与事务持锁状态都是**线程本地**的：每个线程在首次进入
    临界区时独立 ``open`` 一次锁文件并持有自己的 fd（独立的"打开文件
    描述"，flock 语义彼此独立，与多个进程各持一把锁等价），最外层退出
    时解锁并关闭；同线程事务可重入，嵌套不重复加锁；
  - 进程内另有多读单写可重入锁，读者之间完全并发、写者排除一切；
  - 缓存读取只在一把短锁内瞬时取一份"不可变缓存引用"，读之间完全并发；
    缓存刷新由刷新闸门串行，并对扫描期间又变化的目录放弃发布，杜绝
    "遍历撞上字典增删"与"旧扫描覆盖新缓存（已提交资产短暂消失）"；
* 业务对象（``AssetSnapshot``/``FieldRecord``）不可变，每次更新都整体
  构造并发布新快照，磁盘写失败时已发布的旧快照不被触碰——失败写在同一
  进程后续读取与重启后都完全不可见，也不会被一次无关更新顺带带出。

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


def _as_storage_error(exc: OSError, where: str) -> StorageError:
    """把底层文件系统故障统一归类为 ``storage_error``。

    注入故障（``METADATA_CATALOG_CRASH_AFTER_WRITE=raise``）等裸 ``OSError``
    经此包装后仍可被 ``except OSError`` 捕获（``StorageError`` 同时继承
    ``OSError``），但带有领域错误码，HTTP 层能映射成结构化的 500，而不是
    未分类的服务端异常。已属于领域错误的（损坏/格式/注入错误语义）原样保留。
    """
    if isinstance(exc, StorageError):
        return exc
    return StorageError(f"仓库磁盘操作失败（{where}）: {exc}")


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


class _ReentrantRWLock:
    """进程内的可重入读写锁：多读单写，同线程读可重入、写可重入。

    用来保护同一进程内对 flock 锁文件描述符与缓存的并发访问；跨进程的
    多读单写由 ``fcntl.flock`` 在共享/独占两把"外锁"上提供，本锁只负责
    进程内线程间。不支持"读锁升级为写锁"（catalog 中也不存在这种用法）。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers: dict[int, int] = {}
        self._writer: int | None = None
        self._writer_depth = 0
        self._waiting_writers = 0

    def acquire_read(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._writer == tid:
                # 写持锁线程重入：写锁已包含读权限，无需额外登记。
                return
            if tid in self._readers:
                # 同线程读锁重入。
                self._readers[tid] += 1
                return
            # 写者优先：有写者在排队时，新到读者排在其后，防止读流饿死写者。
            while self._writer is not None or self._waiting_writers > 0:
                self._cond.wait()
            self._readers[tid] = 1

    def release_read(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._writer == tid:
                return
            depth = self._readers.get(tid, 0)
            if depth <= 1:
                self._readers.pop(tid, None)
            else:
                self._readers[tid] = depth - 1
            if not self._readers:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._writer == tid:
                self._writer_depth += 1
                return
            if tid in self._readers:
                # 读锁升级为写锁会自锁，直接拒绝（调用方属于实现错误）。
                raise RuntimeError("不支持在读事务内嵌套写事务")
            self._waiting_writers += 1
            try:
                while self._writer is not None or self._readers:
                    self._cond.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = tid
            self._writer_depth = 1

    def release_write(self) -> None:
        tid = threading.get_ident()
        with self._cond:
            if self._writer != tid:
                raise RuntimeError("release_write 由未持写锁的线程调用")
            self._writer_depth -= 1
            if self._writer_depth == 0:
                self._writer = None
                self._cond.notify_all()


class PersistentStore:
    """落盘在本地目录、支持多进程共享的元数据仓库。

    线程模型：

    * 进程内用 :class:`_ReentrantRWLock` 实现多读单写；
    * 跨进程用 ``fcntl.flock`` 在同一 ``lock`` 文件上加共享/排他锁；
    * 每次进入临界区都会在持锁状态下把磁盘变化增量同步进进程内缓存，
      因此一个进程提交的写入，另一个进程在下一次临界区一定能读到。

    读操作必须在 :meth:`read_transaction`（或 :meth:`transaction`）内调用；
    写操作必须在 :meth:`transaction`（排他）内调用。这与服务层"检查 +
    写入同一临界区"的用法一致。
    """

    _NONE = 0
    _SHARED = 1
    _EXCLUSIVE = 2

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
            raise _as_storage_error(exc, f"创建仓库目录 {self.repo_dir}") from exc

        self._rw = _ReentrantRWLock()
        # 事务持有状态是**线程本地**的：每个线程独立记录自己是否处于
        # 临界区、持有哪种锁、嵌套深度，以及自己**专属的 flock 文件描述符**。
        #
        # 为什么每个线程要各开一个 lock 文件 fd：fcntl.flock 的锁关联在
        # "打开文件描述（open file description）"上，同一进程若让多个线程
        # 共用一个 fd，那么一个线程的 flock(UN) 会连带影响同一 fd 上其他
        # 线程的锁，无法在"同进程多线程 + 跨进程"下表达独立的共享/排他持有。
        # 每线程独立 open 即得到独立的打开文件描述，flock 语义随之独立，
        # 与"多个进程各持一把锁"完全等价。
        self._local = threading.local()
        self._closed = False

        # 进程内缓存：正式文件名 -> (stat 签名, 反序列化对象)
        self._asset_cache: dict[str, tuple[tuple, AssetSnapshot]] = {}
        self._edge_cache: dict[str, tuple[tuple, LineageEdge]] = {}
        # 各数据目录最近一次同步时的签名（mtime_ns, ctime_ns）；未变即跳过扫描。
        self._dir_sigs: dict[str, tuple | None] = {
            self.assets_dir: None,
            self.edges_dir: None,
        }
        # 缓存刷新（"列目录 + 读改动文件 + 整体换缓存"）专用锁。
        # 关键点：刷新会在 *共享* 临界区内发生（读者也要同步他进程的写入），
        # 同一时刻可能有多个线程都持有共享锁；若不加这把锁，两个读者可能
        # 同时写缓存 dict，而第三个读者正在遍历它 ->
        # "dictionary changed size during iteration"。刷新串行化后，
        # 遍历期间只可能读到一个完整的旧/新缓存，读之间仍然并发。
        self._cache_lock = threading.Lock()
        # 缓存刷新闸门：把"列目录 + 读改动文件 + 发布"在进程内串行化，
        # 杜绝多个读者各自刷新导致的缓存回退。它与 _cache_lock 不同——
        # 普通读只在 _cache_lock 内瞬时取一份缓存引用，不被慢刷新阻塞。
        self._refresh_gate = threading.Lock()

        try:
            self._init_format_file()
            self._sweep_temp_files()
        except StorageError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise _as_storage_error(exc, "初始化仓库目录") from exc

    # ---- 仓库初始化 ----
    def _init_format_file(self) -> None:
        prefix = "metadata-catalog-repo format="
        for _ in range(2):
            try:
                with open(self.format_path, encoding="utf-8") as fh:
                    first = fh.readline().strip()
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
            except FileNotFoundError:
                # 与其他进程并发首次初始化时：谁先原子建成谁生效，后来者重读。
                marker = (
                    f"metadata-catalog-repo format={FORMAT_VERSION}\n"
                    "# 本目录是元数据目录的本地持久化仓库；请勿手工改写数据文件。\n"
                ).encode("utf-8")
                try:
                    atomic_write(self.format_path, marker, fsync_dir=True)
                except OSError:
                    pass
        # 极少数并发下两次都没读到，退回一次直接判定（存在则成功，否则报错）。
        if not os.path.exists(self.format_path):
            raise StorageError(
                f"仓库 FORMAT 标记初始化失败: {self.format_path}"
            )

    def _sweep_temp_files(self) -> None:
        """清理上一次写入中途崩溃可能留下的临时文件。

        临时文件从未参与正式文件名，因此清理它们不影响任何已生效的状态。
        残留只可能来自"上个进程的未完成写入"，在构造新实例时统一清理。
        """
        for directory in (self.assets_dir, self.edges_dir):
            try:
                names = os.listdir(directory)
            except FileNotFoundError:
                continue
            for name in names:
                if name.startswith(_TMP_PREFIX):
                    try:
                        os.unlink(os.path.join(directory, name))
                    except FileNotFoundError:
                        pass

    # ---- 跨进程临界区 ----
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """排他临界区：进程内其他线程、其他进程都被挡在外面。可重入。"""
        self._enter(self._EXCLUSIVE)
        try:
            yield
        finally:
            self._leave()

    @contextmanager
    def read_transaction(self) -> Iterator[None]:
        """共享临界区：允许多个读者并发，但排斥任何写者。可重入。"""
        self._enter(self._SHARED)
        try:
            yield
        finally:
            self._leave()

    # ---- 跨进程临界区 ----
    def _thread_state(self) -> tuple[int, int]:
        """返回当前线程的 (持锁模式, 嵌套深度)；未在事务中为 (NONE, 0)。"""
        return getattr(self._local, "mode", self._NONE), getattr(
            self._local, "depth", 0
        )

    def _open_lock_fd(self) -> int:
        """当前线程专属的 flock 文件描述符（独立的打开文件描述）。"""
        try:
            return os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise _as_storage_error(exc, f"打开锁文件 {self.lock_path}")

    @staticmethod
    def _flock(fd: int, flags: int, where: str) -> None:
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            raise _as_storage_error(exc, where) from exc

    def _enter(self, mode: int) -> None:
        if self._closed:
            raise StorageError("仓库已关闭，不能再开启事务")
        held, depth = self._thread_state()
        if held != self._NONE:
            if held == self._SHARED and mode == self._EXCLUSIVE:
                # 读锁升级为写锁会自锁，直接拒绝（调用方属于实现错误）。
                raise RuntimeError("不支持在读事务内嵌套写事务")
            # 同线程内同模式/写内嵌套：直接加深，不重复获取进程锁或 flock。
            self._local.mode = held
            self._local.depth = depth + 1
            return

        fd = self._open_lock_fd()
        if mode == self._EXCLUSIVE:
            self._rw.acquire_write()  # 进程内：此刻无其他线程持锁
        else:
            self._rw.acquire_read()  # 进程内：允许多个读者并存
        try:
            # 每线程独立 fd：这里阻塞等待的是*其他进程*与*本进程其他线程*
            # 的冲突锁；多读者各自的 SH 彼此兼容。
            self._flock(
                fd,
                fcntl.LOCK_EX if mode == self._EXCLUSIVE else fcntl.LOCK_SH,
                "获取排他锁" if mode == self._EXCLUSIVE else "获取共享锁",
            )
        except BaseException:
            if mode == self._EXCLUSIVE:
                self._rw.release_write()
            else:
                self._rw.release_read()
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        self._local.mode = mode
        self._local.depth = 1
        self._local.fd = fd
        try:
            self._refresh_locked()
        except BaseException:
            # 刷新失败：释放本线程的 flock/fd 与进程内锁，状态完全回滚。
            self._flock(fd, fcntl.LOCK_UN, "释放锁")
            try:
                os.close(fd)
            except OSError:
                pass
            self._local.mode = self._NONE
            self._local.depth = 0
            self._local.fd = None
            if mode == self._EXCLUSIVE:
                self._rw.release_write()
            else:
                self._rw.release_read()
            raise

    def _leave(self) -> None:
        held, depth = self._thread_state()
        if held == self._NONE or depth <= 0:
            raise RuntimeError("释放事务时当前线程并未持有事务")
        if depth > 1:
            self._local.depth = depth - 1
            return
        # 最外层退出：释放本线程专属的 flock 与 fd，再释放进程内锁。
        fd = self._local.fd
        self._local.mode = self._NONE
        self._local.depth = 0
        self._local.fd = None
        self._flock(fd, fcntl.LOCK_UN, "释放排他锁" if held == self._EXCLUSIVE else "释放共享锁")
        try:
            os.close(fd)
        except OSError:
            pass
        if held == self._EXCLUSIVE:
            self._rw.release_write()
        else:
            self._rw.release_read()

    # ---- 磁盘 -> 缓存的增量同步（调用时已持外锁） ----
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
        self._asset_cache = self._refresh_dir(
            self.assets_dir, self._asset_cache, deserialize_asset
        )
        self._edge_cache = self._refresh_dir(
            self.edges_dir, self._edge_cache, deserialize_edge
        )

    def _refresh_dir(self, directory, cache, deserializer) -> dict:
        """把一个数据目录的磁盘变化增量同步进缓存，返回当前应使用的缓存。

        多个读者线程可同时处于共享临界区，刷新必须防住两类问题：

        * **遍历时改 dict**：发布永远是"整份新 dict 引用替换"，旧 dict
          一旦发布便不再被修改；读者先在 ``_cache_lock`` 内瞬时取一份缓存
          引用再在锁外遍历，因此不与刷新争用，普通读也不会被慢扫描阻塞；
        * **缓存回退（旧扫描覆盖新缓存）**：所有刷新由 ``_refresh_gate``
          在进程内串行。拿到闸门后再判一次目录签名（等闸门期间可能已被
          别的线程刷新）；扫描结束后若目录签名又变了（扫描期间磁盘又有新
          提交），本次结果**不发布**，原样沿用当前缓存，把这次变化留给下
          一次进入临界区的刷新。这样绝不可能用"基于旧目录算出的缓存"覆盖
          掉更新的缓存——该缺陷曾让已提交资产在本进程短暂消失。
        """
        with self._cache_lock:
            published_sig = self._dir_sigs.get(directory)
        disk_sig = self._dir_sig(directory)
        if disk_sig is not None and disk_sig == published_sig:
            return cache  # 目录签名未变：无需扫描

        with self._refresh_gate:
            # double-check：等待闸门期间可能已由别的线程完成刷新。
            with self._cache_lock:
                published_sig = self._dir_sigs.get(directory)
            disk_sig = self._dir_sig(directory)
            if disk_sig is not None and disk_sig == published_sig:
                return (
                    self._asset_cache
                    if directory == self.assets_dir
                    else self._edge_cache
                )

            try:
                names = os.listdir(directory)
            except FileNotFoundError:
                with self._cache_lock:
                    self._dir_sigs[directory] = None
                    if directory == self.assets_dir:
                        self._asset_cache = {}
                    else:
                        self._edge_cache = {}
                    return (
                        self._asset_cache
                        if directory == self.assets_dir
                        else self._edge_cache
                    )

            with self._cache_lock:
                base_cache = (
                    self._asset_cache
                    if directory == self.assets_dir
                    else self._edge_cache
                )
            new_cache: dict = dict(base_cache)
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
                    raise _as_storage_error(exc, f"读取数据文件 {path}")
                new_cache[name] = (sig, deserializer(raw, where=path))
            for gone in [n for n in list(new_cache) if n not in live]:
                new_cache.pop(gone, None)

            # 扫描期间目录若又发生变化，放弃发布，避免覆盖更新的缓存。
            final_sig = self._dir_sig(directory)
            with self._cache_lock:
                if final_sig != disk_sig:
                    return (
                        self._asset_cache
                        if directory == self.assets_dir
                        else self._edge_cache
                    )
                self._dir_sigs[directory] = final_sig
                if directory == self.assets_dir:
                    self._asset_cache = new_cache
                else:
                    self._edge_cache = new_cache
                return new_cache

    def _publish_asset(self, fname: str, sig: tuple, snapshot: AssetSnapshot) -> None:
        """在缓存锁内整体替换资产缓存引用（旧 dict 冻结）。"""
        with self._cache_lock:
            new_cache = dict(self._asset_cache)
            new_cache[fname] = (sig, snapshot)
            self._asset_cache = new_cache
            self._dir_sigs[self.assets_dir] = self._dir_sig(self.assets_dir)

    def _put_asset_locked(self, snapshot: AssetSnapshot) -> None:
        fname = asset_filename(snapshot.name)
        path = os.path.join(self.assets_dir, fname)
        try:
            atomic_write(path, serialize_asset(snapshot))
            sig = self._file_sig(path)
        except OSError as exc:
            # 磁盘写入失败时绝不触碰缓存：已发布的旧快照继续对所有读可见，
            # 失败写在同进程内与重启后都不可见。
            raise _as_storage_error(exc, f"写入资产文件 {path}")
        assert sig is not None
        self._publish_asset(fname, sig, snapshot)

    def _snapshot_asset_cache(self) -> dict[str, tuple[tuple, AssetSnapshot]]:
        with self._cache_lock:
            return self._asset_cache

    # ---- 资产 ----
    def get_asset(self, name: str) -> AssetSnapshot | None:
        with self._read_scope():
            cache = self._snapshot_asset_cache()
            entry = cache.get(asset_filename(name))
            if entry is not None and entry[1].name == name:
                return entry[1]
            return None

    def put_asset(self, snapshot: AssetSnapshot) -> None:
        with self._write_scope():
            self._put_asset_locked(snapshot)

    def list_assets(self) -> list[AssetSnapshot]:
        with self._read_scope():
            # 先取某一版缓存引用（一份此后不再被改写的 dict），再在锁外遍历。
            cache = self._snapshot_asset_cache()
            return [entry[1] for entry in cache.values()]

    # ---- 血缘 ----
    def _find_edge(
        self,
        cache: dict[str, tuple[tuple, LineageEdge]],
        upstream: str,
        downstream: str,
    ) -> tuple[str, LineageEdge] | None:
        wanted = (upstream, downstream)
        for fname, (_sig, edge) in cache.items():
            if (edge.upstream, edge.downstream) == wanted:
                return fname, edge
        return None

    def _snapshot_edge_cache(self) -> dict[str, tuple[tuple, LineageEdge]]:
        with self._cache_lock:
            return self._edge_cache

    def has_edge(self, upstream: str, downstream: str) -> bool:
        with self._read_scope():
            cache = self._snapshot_edge_cache()
            return self._find_edge(cache, upstream, downstream) is not None

    def add_edge(self, edge: LineageEdge) -> None:
        with self._write_scope():
            fname = edge_filename(edge)
            path = os.path.join(self.edges_dir, fname)
            try:
                atomic_write(path, serialize_edge(edge))
                sig = self._file_sig(path)
            except OSError as exc:
                raise _as_storage_error(exc, f"写入血缘文件 {path}")
            assert sig is not None
            with self._cache_lock:
                new_cache = dict(self._edge_cache)
                new_cache[fname] = (sig, edge)
                self._edge_cache = new_cache
                self._dir_sigs[self.edges_dir] = self._dir_sig(self.edges_dir)

    def remove_edge(self, upstream: str, downstream: str) -> None:
        with self._write_scope():
            cache = self._snapshot_edge_cache()
            found = self._find_edge(cache, upstream, downstream)
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
                raise _as_storage_error(exc, f"删除血缘文件 {path}")
            with self._cache_lock:
                new_cache = {k: v for k, v in self._edge_cache.items() if k != fname}
                self._edge_cache = new_cache
                self._dir_sigs[self.edges_dir] = self._dir_sig(self.edges_dir)

    def list_edges(self) -> list[LineageEdge]:
        with self._read_scope():
            cache = self._snapshot_edge_cache()
            return [entry[1] for entry in cache.values()]

    # ---- 维护 ----
    def reset(self) -> None:
        """清空仓库中全部资产与血缘边（仅供测试/维护使用）。"""
        with self._write_scope():
            for directory in (self.assets_dir, self.edges_dir):
                try:
                    names = os.listdir(directory)
                except FileNotFoundError:
                    names = []
                for name in names:
                    if name.endswith(".json") or name.startswith(_TMP_PREFIX):
                        try:
                            os.unlink(os.path.join(directory, name))
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            raise _as_storage_error(
                                exc, f"清空数据文件 {os.path.join(directory, name)}"
                            )
                try:
                    _fsync_dir(directory)
                except OSError as exc:
                    raise _as_storage_error(exc, f"同步目录 {directory}")
                with self._cache_lock:
                    if directory == self.assets_dir:
                        self._asset_cache = {}
                    else:
                        self._edge_cache = {}
                    self._dir_sigs[directory] = self._dir_sig(directory)

    def close(self) -> None:
        """标记仓库关闭并释放**当前线程**可能仍持有的锁文件描述符。

        flock fd 是线程本地的，正常用法应先让各线程退出事务；这里仅兜底
        关闭调用 close 的线程自身的 fd（关闭即自动释放其 flock）。其他线程
        的 fd 随线程结束/进程退出由操作系统回收。可重入；关闭后不能再开启事务。
        """
        if self._closed:
            return
        self._closed = True
        fd = getattr(self._local, "fd", None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._local.fd = None
            self._local.mode = self._NONE
            self._local.depth = 0

    def __enter__(self) -> "PersistentStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 内部约束 ----
    @contextmanager
    def _read_scope(self) -> Iterator[None]:
        """已在任何临界区内则沿用当前线程已持有的锁；否则临时开共享临界区。"""
        held, _depth = self._thread_state()
        if held != self._NONE:
            yield
            return
        with self.read_transaction():
            yield

    @contextmanager
    def _write_scope(self) -> Iterator[None]:
        """当前线程已在排他临界区内则沿用；在读临界区内属于实现错误；
        否则临时开一个排他临界区。这样公开方法可独立调用，而服务层显式
        事务内的"检查 + 多步写入"仍然共用同一个跨进程临界区。
        """
        held, _depth = self._thread_state()
        if held == self._EXCLUSIVE:
            yield
            return
        if held == self._SHARED:
            raise RuntimeError("写操作不能发生在读事务内")
        with self.transaction():
            yield
