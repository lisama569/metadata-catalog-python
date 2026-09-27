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
