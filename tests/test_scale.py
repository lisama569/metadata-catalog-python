"""仓库规模到上万量级时的查询/写入性能护栏（本地持久化仓库）。

这些用例不绑定绝对硬件指标，只保证"落盘 + 缓存"方案在规模放大后不会
退化成每次读全量扫盘：

* 暖缓存下的单点查询与血缘遍历保持在毫秒级；
* 冷启动（新实例第一次读）也必须能在可接受时间内装载上万资产；
* 单笔写入在大规模仓库上仍然便宜（不重写全库）。

10k 资产 / 5k 边的种子数据只构造一次（module 级 fixture），用例按文件
顺序执行；最后一个用例才做写入。
"""

from __future__ import annotations

import logging
import time

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
from metadata_catalog.persistence import PersistentStore

logger = logging.getLogger("metadata_catalog.tests")

N_ASSETS = 10_000
N_CHAIN = 5_000  # a0 -> a1 -> ... -> a{N-1}，用于传递影响范围


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("scale") / "repo")
    catalog = MetadataCatalog(PersistentStore(path))
    fields = [
        FieldSpec("id", FieldType.LONG, nullable=False),
        FieldSpec("name", FieldType.STRING),
    ]
    t0 = time.perf_counter()
    with catalog.store.transaction():
        for i in range(N_ASSETS):
            catalog.register(
                RegisterRequest(
                    spec=AssetSpec(
                        name=f"asset{i}", asset_type=AssetType.TABLE, fields=fields
                    )
                )
            )
    t_assets = time.perf_counter() - t0
    t0 = time.perf_counter()
    with catalog.store.transaction():
        for i in range(N_CHAIN - 1):
            catalog.add_lineage(LineageEdge(f"asset{i}", f"asset{i + 1}"))
    t_edges = time.perf_counter() - t0
    logger.info(
        "种子数据: %d 资产写入 %.2fs，%d 边写入 %.2fs",
        N_ASSETS, t_assets, N_CHAIN - 1, t_edges,
    )
    catalog.store.close()
    return path


def test_warm_point_reads_and_traversal_are_fast(repo):
    catalog = MetadataCatalog(PersistentStore(repo))
    catalog.list_assets()  # 暖缓存：先全量装载一次

    t0 = time.perf_counter()
    for i in range(0, N_ASSETS, 100):
        snap = catalog.get_asset(f"asset{i}")
        assert snap.schema_version == 1 and len(snap.fields) == 2
    point_elapsed = time.perf_counter() - t0
    logger.info("规模 %d: 100 次暖缓存单点查询耗时 %.3fs", N_ASSETS, point_elapsed)
    assert point_elapsed < 0.2

    t0 = time.perf_counter()
    impact = catalog.impact("asset0")
    traversal = time.perf_counter() - t0
    assert len(impact.downstream_transitive) == N_CHAIN - 1
    assert impact.downstream_direct == ["asset1"]
    logger.info(
        "规模 %d: 长度 %d 链的传递影响遍历耗时 %.3fs",
        N_ASSETS, N_CHAIN, traversal,
    )
    assert traversal < 1.0
    catalog.store.close()


def test_cold_reopen_loads_in_acceptable_time(repo):
    t0 = time.perf_counter()
    catalog = MetadataCatalog(PersistentStore(repo))
    assets = catalog.list_assets()  # 冷启动：必须读完全部资产文件
    cold_assets = time.perf_counter() - t0
    assert len(assets) == N_ASSETS

    t0 = time.perf_counter()
    edges = catalog.store.list_edges()
    cold_edges = time.perf_counter() - t0
    assert len(edges) == N_CHAIN - 1
    logger.info(
        "冷启动装载: %d 资产 %.3fs，%d 边 %.3fs",
        len(assets), cold_assets, len(edges), cold_edges,
    )
    assert cold_assets < 10.0
    catalog.store.close()


def test_single_write_after_scale_still_cheap(repo):
    catalog = MetadataCatalog(PersistentStore(repo))
    catalog.list_assets()  # 暖缓存

    t0 = time.perf_counter()
    out = catalog.register(
        RegisterRequest(
            spec=AssetSpec(
                name="asset0",
                asset_type=AssetType.TABLE,
                fields=[
                    FieldSpec("id", FieldType.LONG, nullable=False),
                    FieldSpec("name", FieldType.STRING),
                    FieldSpec("note", FieldType.STRING),
                ],
            ),
            expected_schema_version=1,
        )
    )
    elapsed = time.perf_counter() - t0
    assert out.action == "evolved" and out.schema_version == 2
    logger.info("规模 %d: 单笔演进写入耗时 %.3fs", N_ASSETS, elapsed)
    assert elapsed < 1.0

    t0 = time.perf_counter()
    gov = catalog.update_governance(
        GovernanceUpdate(
            asset="asset9999", expected_governance_version=0, owner="bob"
        )
    )
    gov_elapsed = time.perf_counter() - t0
    assert gov.governance_version == 1
    logger.info("规模 %d: 单笔治理写入耗时 %.3fs", N_ASSETS, gov_elapsed)
    assert gov_elapsed < 1.0
    catalog.store.close()
