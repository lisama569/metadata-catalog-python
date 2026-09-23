"""血缘写入、校验与影响范围测试。"""

from __future__ import annotations

import pytest

from metadata_catalog.errors import (
    AssetNotFoundError,
    InvalidArgumentError,
    LineageCycleError,
    LineageEndpointMissingError,
)
from metadata_catalog.models import LineageWriteStatus

from .conftest import assert_log_contains


def _make_assets(service, *ids: str) -> None:
    for aid in ids:
        service.register_asset(_reg(aid))


def _reg(aid):
    from metadata_catalog.models import AssetRegistration
    return AssetRegistration(asset_id=aid, fields=[])


# ---- 写入 / 重复边 ------------------------------------------------------
def test_create_edge(service, caplog):
    _make_assets(service, "a", "b")
    res = service.add_lineage(upstream="a", downstream="b")
    assert res.status is LineageWriteStatus.CREATED
    assert res.version == 1
    assert_log_contains(caplog, "add_edge 输入", "判定 CREATED")


def test_duplicate_edge_is_idempotent(service, caplog):
    _make_assets(service, "a", "b")
    first = service.add_lineage(upstream="a", downstream="b")
    second = service.add_lineage(upstream="a", downstream="b")
    assert first.status is LineageWriteStatus.CREATED
    assert second.status is LineageWriteStatus.DUPLICATE_EDGE
    assert second.version == first.version  # 不提升版本
    # 邻接表里仍然只有一条
    rep = service.impact(asset_id="a")
    assert rep.direct == ["b"]
    assert_log_contains(caplog, "DUPLICATE_EDGE", "幂等")


# ---- 成环 / 端点缺失 ----------------------------------------------------
def test_cycle_rejected_with_path(service, caplog):
    _make_assets(service, "a", "b", "c")
    service.add_lineage(upstream="a", downstream="b")
    service.add_lineage(upstream="b", downstream="c")
    with pytest.raises(LineageCycleError) as exc:
        service.add_lineage(upstream="c", downstream="a")
    # 环路径首尾闭合且包含所有节点
    assert exc.value.cycle[0] == exc.value.cycle[-1] == "c"
    assert set(exc.value.cycle) == {"a", "b", "c"}
    # 被拒绝的边没有落库
    assert service.impact(asset_id="c").direct == []
    assert_log_contains(caplog, "血缘成环")


def test_self_loop_rejected(service):
    _make_assets(service, "a")
    with pytest.raises(LineageCycleError):
        service.add_lineage(upstream="a", downstream="a")


def test_longer_cycle_rejected(service):
    _make_assets(service, *[f"n{i}" for i in range(5)])
    for i in range(4):
        service.add_lineage(upstream=f"n{i}", downstream=f"n{i + 1}")
    with pytest.raises(LineageCycleError) as exc:
        service.add_lineage(upstream="n4", downstream="n0")
    assert exc.value.cycle[0] == "n4" and exc.value.cycle[-1] == "n4"


def test_missing_downstream_rejected(service, caplog):
    _make_assets(service, "a")
    with pytest.raises(LineageEndpointMissingError) as exc:
        service.add_lineage(upstream="a", downstream="ghost")
    assert exc.value.missing == ["ghost"]
    assert_log_contains(caplog, "add_edge 输入")


def test_missing_upstream_rejected(service):
    _make_assets(service, "b")
    with pytest.raises(LineageEndpointMissingError) as exc:
        service.add_lineage(upstream="ghost", downstream="b")
    assert exc.value.missing == ["ghost"]


def test_rejected_edge_does_not_partially_persist(service):
    _make_assets(service, "a", "b")
    with pytest.raises(LineageCycleError):
        service.add_lineage(upstream="a", downstream="a")
    assert service.impact(asset_id="a").reachable == []


# ---- 影响范围 -----------------------------------------------------------
def test_direct_and_transitive_downstream(service):
    _make_assets(service, "a", "b", "c", "d")
    service.add_lineage(upstream="a", downstream="b")
    service.add_lineage(upstream="b", downstream="c")
    service.add_lineage(upstream="b", downstream="d")
    rep = service.impact(asset_id="a", direction="downstream")
    assert rep.direct == ["b"]
    assert rep.transitive == ["c", "d"]
    assert rep.reachable == ["b", "c", "d"]
    assert rep.depth == {"b": 1, "c": 2, "d": 2}


def test_direct_and_transitive_upstream(service):
    _make_assets(service, "a", "b", "c")
    service.add_lineage(upstream="a", downstream="b")
    service.add_lineage(upstream="b", downstream="c")
    rep = service.impact(asset_id="c", direction="upstream")
    assert rep.direct == ["b"]
    assert rep.transitive == ["a"]


def test_diamond_impact_dedup(service):
    # a -> b,d; b,d -> c: c 经两条路径可达, 但只应出现一次
    _make_assets(service, "a", "b", "c", "d")
    service.add_lineage(upstream="a", downstream="b")
    service.add_lineage(upstream="a", downstream="d")
    service.add_lineage(upstream="b", downstream="c")
    service.add_lineage(upstream="d", downstream="c")
    rep = service.impact(asset_id="a")
    assert rep.reachable.count("c") == 1
    assert rep.depth == {"b": 1, "d": 1, "c": 2}


def test_impact_unknown_asset(service):
    with pytest.raises(AssetNotFoundError):
        service.impact(asset_id="nope")


def test_impact_bad_direction(service):
    _make_assets(service, "a")
    with pytest.raises(InvalidArgumentError):
        service.impact(asset_id="a", direction="sideways")


# ---- 并发写入 -----------------------------------------------------------
def test_concurrent_edge_writes_are_consistent(service):
    import threading

    _make_assets(service, "root", *[f"n{i}" for i in range(20)])

    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            service.add_lineage(upstream="root", downstream=f"n{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    rep = service.impact(asset_id="root")
    expected = {f"n{i}" for i in range(20)}
    assert set(rep.direct) == expected
    assert len(rep.direct) == 20  # 无丢边、无重复
    assert rep.transitive == []


def test_concurrent_duplicate_and_cycle(service):
    import threading

    _make_assets(service, "a", "b")
    outcomes: list[str] = []

    def add_ab() -> None:
        res = service.add_lineage(upstream="a", downstream="b")
        outcomes.append(res.status.value)

    def add_ba() -> None:
        try:
            service.add_lineage(upstream="b", downstream="a")
        except LineageCycleError:
            outcomes.append("LINEAGE_CYCLE")

    t1 = threading.Thread(target=add_ab)
    t2 = threading.Thread(target=add_ba)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 恰好一条 CREATED; 另一个要么 DUPLICATE_EDGE(同向重复) 要么 CYCLE,
    # 但绝不会两条方向都被接受。
    assert outcomes.count("CREATED") == 1
    assert set(outcomes) <= {"CREATED", "DUPLICATE_EDGE", "LINEAGE_CYCLE"}
    assert "LINEAGE_CYCLE" in outcomes or outcomes.count("DUPLICATE_EDGE") == 1
