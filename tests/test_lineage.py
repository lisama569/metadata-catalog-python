"""血缘写入与影响范围查询测试。"""

from __future__ import annotations

import logging

import pytest

from metadata_catalog.errors import AssetNotFoundError
from metadata_catalog.models import (
    AssetSpec,
    AssetType,
    LineageEdge,
    RegisterRequest,
)

logger = logging.getLogger("metadata_catalog.tests")


def register(catalog, *names: str) -> None:
    for n in names:
        catalog.register(
            RegisterRequest(spec=AssetSpec(name=n, asset_type=AssetType.TABLE))
        )


def test_edge_accepted_and_duplicate_is_flagged(catalog):
    register(catalog, "a", "b")
    logger.info("用例输入: a->b 首次写入，随后重复写同一对")
    first = catalog.add_lineage(LineageEdge("a", "b"))
    second = catalog.add_lineage(LineageEdge("a", "b"))
    logger.info("判定依据: first=%s second=%s/%s",
                first.code, second.code, second.reason)
    assert first.accepted and first.code == "accepted"
    assert not second.accepted and second.code == "duplicate_edge"
    # 重复边只存一份
    assert len(catalog.store.list_edges()) == 1


def test_edge_to_missing_asset_is_flagged(catalog):
    register(catalog, "a")
    logger.info("用例输入: a->ghost 与 ghost->a")
    r1 = catalog.add_lineage(LineageEdge("a", "ghost"))
    r2 = catalog.add_lineage(LineageEdge("ghost", "a"))
    logger.info("判定依据: r1=%s r2=%s", r1.code, r2.code)
    assert r1.code == "asset_not_found"
    assert r2.code == "asset_not_found"
    assert catalog.store.list_edges() == []


def test_self_loop_is_rejected(catalog):
    register(catalog, "a")
    logger.info("用例输入: a->a 自环")
    r = catalog.add_lineage(LineageEdge("a", "a"))
    logger.info("判定依据: code=%s reason=%s", r.code, r.reason)
    assert r.code == "lineage_cycle"


def test_indirect_cycle_is_detected(catalog):
    register(catalog, "a", "b", "d", "e")
    for u, d in [("a", "b"), ("b", "d"), ("d", "e")]:
        assert catalog.add_lineage(LineageEdge(u, d)).accepted
    logger.info("用例输入: 已有 a->b->d->e，再写 e->a")
    r = catalog.add_lineage(LineageEdge("e", "a"))
    logger.info("判定依据: code=%s reason=%s", r.code, r.reason)
    assert r.code == "lineage_cycle"
    # 拒绝的边不得落库
    assert not catalog.store.has_edge("e", "a")
    assert len(catalog.store.list_edges()) == 3


def test_batch_validates_sequentially_and_flags_each(catalog):
    register(catalog, "a", "b", "d")
    assert catalog.add_lineage(LineageEdge("a", "b")).accepted
    logger.info("用例输入: 批量 [b->d, d->a(成环), a->b(重复), a->ghost(缺失)]")
    results = catalog.add_lineage_batch(
        [
            LineageEdge("b", "d"),
            LineageEdge("d", "a"),
            LineageEdge("a", "b"),
            LineageEdge("a", "ghost"),
        ]
    )
    codes = [r.code for r in results]
    logger.info("判定依据: codes=%s", codes)
    assert codes == [
        "accepted",
        "lineage_cycle",
        "duplicate_edge",
        "asset_not_found",
    ]
    # 第一条被接受；其余拒绝不影响已接受的边
    assert catalog.store.has_edge("b", "d")


def test_impact_direct_and_transitive(catalog):
    register(catalog, "a", "b", "d", "e", "f")
    # a->b->d->e 主链；a->f 旁路；b->f
    for u, d in [("a", "b"), ("b", "d"), ("d", "e"), ("a", "f"), ("b", "f")]:
        catalog.add_lineage(LineageEdge(u, d))
    logger.info("用例输入: 查询 b 的上下游影响范围")
    impact = catalog.impact("b")
    logger.info(
        "判定依据: up_direct=%s down_direct=%s up_trans=%s down_trans=%s",
        impact.upstream_direct,
        impact.downstream_direct,
        impact.upstream_transitive,
        impact.downstream_transitive,
    )
    assert impact.upstream_direct == ["a"]
    assert impact.downstream_direct == ["d", "f"]
    assert impact.upstream_transitive == ["a"]
    assert impact.downstream_transitive == ["d", "e", "f"]


def test_impact_on_missing_asset_raises(catalog):
    logger.info("用例输入: 查询不存在资产 ghost 的影响范围")
    with pytest.raises(AssetNotFoundError) as exc:
        catalog.impact("ghost")
    logger.info("判定依据: code=%s", exc.value.code)
    assert exc.value.code == "asset_not_found"
