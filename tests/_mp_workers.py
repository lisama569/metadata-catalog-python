"""``tests/test_multiprocess.py`` 的子进程工作函数。

必须是顶层可 pickle 的函数（multiprocessing 默认在 spawn 子进程里重新
import 模块），每个子进程独立打开同一个仓库目录，模拟"多个服务实例/
多个进程同时操作同一个本地仓库"。
"""

from __future__ import annotations

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import VersionConflictError
from metadata_catalog.models import (
    AssetSpec,
    AssetType,
    FieldSpec,
    FieldType,
    GovernanceUpdate,
    LineageEdge,
    RegisterRequest,
)
from metadata_catalog.persistence import PersistentStore

# 由 Pool initializer 注入：所有子进程共享的同步屏障。
BARRIER = None


def init_worker(barrier) -> None:
    global BARRIER
    BARRIER = barrier


def _table(name: str, *fields) -> AssetSpec:
    return AssetSpec(name=name, asset_type=AssetType.TABLE, fields=list(fields))


def governance_worker(args) -> tuple[str, int | None, str | None]:
    """N 个进程都基于 gv=0 抢写不同 owner；返回 (ok|conflict, gv, owner)。"""
    repo, i = args
    catalog = MetadataCatalog(PersistentStore(repo))
    try:
        BARRIER.wait()
        out = catalog.update_governance(
            GovernanceUpdate(
                asset="hot", expected_governance_version=0, owner=f"user{i}"
            )
        )
        return ("ok", out.governance_version, out.owner)
    except VersionConflictError as exc:
        return ("conflict", exc.actual_version, None)
    finally:
        catalog.store.close()


def register_worker(args) -> tuple[str, int]:
    """N 个进程并发用相同结构首次登记同一资产。"""
    repo, i = args
    catalog = MetadataCatalog(PersistentStore(repo))
    try:
        BARRIER.wait()
        out = catalog.register(
            RegisterRequest(
                spec=_table(
                    "dup", FieldSpec("id", FieldType.LONG, nullable=False)
                )
            )
        )
        return (out.action, out.schema_version)
    finally:
        catalog.store.close()


def lineage_worker(args) -> tuple[str, str]:
    """N 个进程并发写同一条边 a->b。返回 (accepted?, code)。"""
    repo, i = args
    catalog = MetadataCatalog(PersistentStore(repo))
    try:
        BARRIER.wait()
        r = catalog.add_lineage(LineageEdge("a", "b"))
        return ("accepted" if r.accepted else "rejected", r.code)
    finally:
        catalog.store.close()


def evolve_worker(args) -> tuple[str, int | None]:
    """N 个进程并发基于 sv=1 给同一资产加不同字段，恰好一个成功。"""
    repo, i = args
    catalog = MetadataCatalog(PersistentStore(repo))
    try:
        BARRIER.wait()
        out = catalog.register(
            RegisterRequest(
                spec=_table(
                    "hot",
                    FieldSpec("id", FieldType.LONG, False),
                    FieldSpec(f"col{i}", FieldType.STRING),
                ),
                expected_schema_version=1,
            )
        )
        return (out.action, out.schema_version)
    except VersionConflictError as exc:
        return ("conflict", exc.actual_version)
    finally:
        catalog.store.close()


def mixed_worker(args) -> tuple[int, int, int, int]:
    """每个进程内再起多个线程，混合并发读 + 追加式演进同一资产。

    返回 (读次数, 读异常数, 写成功数, 写冲突数)。用于同时压测进程内
    读写锁/缓存与跨进程 flock：读不允许出现任何异常，写要么成功要么
    可区分冲突；最终 sv == 1 + 成功写数，字段数与版本一致。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    repo, idx = args
    catalog = MetadataCatalog(PersistentStore(repo))
    n_threads = 4
    stats = {"reads": 0, "read_err": 0, "w_ok": 0, "w_conf": 0}
    lock = threading.Lock()
    local_barrier = threading.Barrier(n_threads)

    # 跨进程屏障每进程恰好放行一次：在工作线程启动前等待所有进程就绪，
    # 随后进程内 n_threads 个线程在 local_barrier 上同时起步。
    BARRIER.wait()

    def job(t):
        local_barrier.wait()
        rounds = 10
        for k in range(rounds):
            if (t + k) % 3 == 0:
                # 写：基于最新快照追加自己的字段
                snap = catalog.get_asset("hot")
                fields = [
                    FieldSpec(r.spec.name, r.spec.field_type, r.spec.nullable)
                    for r in snap.fields.values()
                ]
                fields.append(
                    FieldSpec(f"p{idx}t{t}k{k}", FieldType.STRING)
                )
                try:
                    catalog.register(
                        RegisterRequest(
                            spec=AssetSpec(
                                name="hot",
                                asset_type=AssetType.TABLE,
                                fields=fields,
                            ),
                            expected_schema_version=snap.schema_version,
                        )
                    )
                    with lock:
                        stats["w_ok"] += 1
                except VersionConflictError:
                    with lock:
                        stats["w_conf"] += 1
            else:
                # 读：只允许看到自洽快照
                try:
                    snap = catalog.get_asset("hot")
                    catalog.list_assets()
                    assert len(snap.fields) == snap.schema_version
                    with lock:
                        stats["reads"] += 1
                except Exception:
                    with lock:
                        stats["read_err"] += 1

    try:
        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            list(pool.map(job, range(n_threads)))
    finally:
        catalog.store.close()
    return (stats["reads"], stats["read_err"], stats["w_ok"], stats["w_conf"])
