"""仓库涨到上万量级后的性能与正确性测试。

约束（本地开发机可接受范围，留足余量）：

* 1 万资产逐条登记 + 1 万条血缘逐条独立提交，整体写入很快完成；
* 单个资产结构查询、全量列举、单条写入在毫秒量级；
* 万级链条的传递影响范围查询在百毫秒内；
* 重启后仅从快照加载，资产/边数量不变、结论一致。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.models import (
    AssetSpec,
    AssetType,
    FieldSpec,
    FieldType,
    GovernanceUpdate,
    LineageEdge,
    RegisterRequest,
)
from metadata_catalog.persistent_store import PersistentStore

N = 10_000


@pytest.fixture
def fast_repo(tmp_path_factory):
    """在内存文件系统（tmpfs，如 /tmp）上建仓库目录。

    写入每次提交仍做真实的 ``fsync``，崩溃恢复语义不变；只是避开普通
    机械/SSD 上每次 fsync ~3ms 的物理开销（2 万次提交在 ext4 上约 30s
    是磁盘耐久成本而非算法退化）。无 tmpfs 时回退到 pytest 临时目录。
    """
    base = Path("/tmp") if Path("/tmp").is_dir() else None
    root = tempfile.mkdtemp(prefix="mc-scale-", dir=base)
    yield Path(root)


def _name(i: int) -> str:
    return f"t{i:05d}"


def test_scale_10k_assets_and_lineage(fast_repo):
    store = PersistentStore(str(fast_repo))
    cat = MetadataCatalog(store)

    t0 = time.perf_counter()
    for i in range(N):
        cat.register(
            RegisterRequest(
                AssetSpec(
                    _name(i),
                    AssetType.TABLE,
                    [
                        FieldSpec("id", FieldType.LONG, nullable=False),
                        FieldSpec("v", FieldType.STRING),
                    ],
                )
            )
        )
    register_all = time.perf_counter() - t0
    assert register_all < 15.0, f"1 万登记耗时 {register_all:.1f}s"

    t0 = time.perf_counter()
    for i in range(N - 1):
        cat.add_lineage(LineageEdge(_name(i), _name(i + 1)))
    edges_all = time.perf_counter() - t0
    assert edges_all < 15.0, f"1 万条血缘提交耗时 {edges_all:.1f}s"

    # 常规读/写在毫秒量级。
    t0 = time.perf_counter()
    snap = cat.get_asset(_name(5000))
    assert snap.schema_version == 1
    get_ms = (time.perf_counter() - t0) * 1000
    assert get_ms < 200, f"get_asset {get_ms:.1f}ms"

    t0 = time.perf_counter()
    assets = cat.list_assets()
    list_ms = (time.perf_counter() - t0) * 1000
    assert len(assets) == N
    assert list_ms < 500, f"list_assets {list_ms:.1f}ms"

    t0 = time.perf_counter()
    cat.update_governance(
        GovernanceUpdate(_name(1), 0, owner="scale", add_tags=frozenset({"x"}))
    )
    write_ms = (time.perf_counter() - t0) * 1000
    assert write_ms < 200, f"single write {write_ms:.1f}ms"

    # 万级链条的传递下游（9999 个可达节点）必须可在百毫秒级算出。
    t0 = time.perf_counter()
    impact = cat.impact(_name(0))
    impact_ms = (time.perf_counter() - t0) * 1000
    assert len(impact.downstream_direct) == 1
    assert len(impact.downstream_transitive) == N - 1
    assert impact_ms < 1000, f"impact {impact_ms:.1f}ms"

    assert store._gen >= 2, "1 万写入应当触发过快照压缩"
    store.close()

    # 重启后只从快照+少量事件加载，结论一致、数量不变。
    t0 = time.perf_counter()
    store2 = PersistentStore(str(fast_repo))
    open_ms = (time.perf_counter() - t0) * 1000
    cat2 = MetadataCatalog(store2)
    assert len(cat2.list_assets()) == N
    assert len(cat2.store.list_edges()) == N - 1
    impact2 = cat2.impact(_name(0))
    assert len(impact2.downstream_transitive) == N - 1
    assert cat2.get_asset(_name(1)).owner == "scale"
    assert open_ms < 2000, f"reopen {open_ms:.1f}ms"
    store2.close()
