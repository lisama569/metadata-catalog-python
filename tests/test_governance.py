"""标签/负责人更新、乐观并发控制与并发写入一致性测试。"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from metadata_catalog.errors import (
    AssetNotFoundError,
    ValidationError,
    VersionConflictError,
)
from metadata_catalog.models import (
    AssetSpec,
    AssetType,
    FieldSpec,
    FieldType,
    GovernanceUpdate,
    LineageEdge,
    RegisterRequest,
)

logger = logging.getLogger("metadata_catalog.tests")


def _asset(catalog, name: str = "t1") -> None:
    catalog.register(
        RegisterRequest(
            spec=AssetSpec(
                name=name,
                asset_type=AssetType.TABLE,
                fields=[FieldSpec("id", FieldType.LONG, False)],
            )
        )
    )


def test_set_owner_and_tags_bumps_version(catalog):
    _asset(catalog)
    logger.info("用例输入: gv=0 设置 owner=alice，add_tags={pii,finance}")
    out = catalog.update_governance(
        GovernanceUpdate(
            asset="t1",
            expected_governance_version=0,
            owner="alice",
            add_tags=frozenset({"pii", "finance"}),
        )
    )
    logger.info("判定依据: changed=%s gv=%s tags=%s", out.changed, out.governance_version, out.tags)
    assert out.changed and out.governance_version == 1
    assert out.owner == "alice" and out.tags == ["finance", "pii"]


def test_concurrent_governance_only_one_wins(catalog):
    _asset(catalog)
    n = 16
    logger.info("用例输入: %d 个线程同时基于 gv=0 写不同 owner", n)
    outcomes: list[str] = []
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            catalog.update_governance(
                GovernanceUpdate(
                    asset="t1",
                    expected_governance_version=0,
                    owner=f"user{i}",
                )
            )
            outcomes.append("ok")
        except VersionConflictError:
            outcomes.append("conflict")

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))
    logger.info("判定依据: ok=%d conflict=%d",
                outcomes.count("ok"), outcomes.count("conflict"))
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == n - 1
    snap = catalog.get_asset("t1")
    # 版本只能被推进一次，不允许丢更新把多个 owner 写花
    assert snap.governance_version == 1
    assert snap.owner in {f"user{i}" for i in range(n)}


def test_stale_version_raises_distinguishable_conflict(catalog):
    _asset(catalog)
    catalog.update_governance(
        GovernanceUpdate(asset="t1", expected_governance_version=0, owner="a")
    )
    logger.info("用例输入: 实际 gv=1，携带过期 gv=0 写入")
    with pytest.raises(VersionConflictError) as exc:
        catalog.update_governance(
            GovernanceUpdate(asset="t1", expected_governance_version=0, owner="b")
        )
    logger.info("判定依据: code=%s expected=%s actual=%s",
                exc.value.code, exc.value.expected_version, exc.value.actual_version)
    assert exc.value.code == "version_conflict"
    assert (exc.value.expected_version, exc.value.actual_version) == (0, 1)
    # 过期写入不得覆盖
    assert catalog.get_asset("t1").owner == "a"


def test_retry_with_fresh_version_succeeds(catalog):
    """模拟调用方冲突后读取新版本并重试，最终一致。"""
    _asset(catalog)
    catalog.update_governance(
        GovernanceUpdate(asset="t1", expected_governance_version=0, owner="a")
    )
    logger.info("用例输入: 先冲突，再以最新 gv 重试 add_tags={x}")
    with pytest.raises(VersionConflictError):
        catalog.update_governance(
            GovernanceUpdate(
                asset="t1", expected_governance_version=0,
                add_tags=frozenset({"x"}),
            )
        )
    fresh = catalog.get_asset("t1").governance_version
    out = catalog.update_governance(
        GovernanceUpdate(
            asset="t1", expected_governance_version=fresh,
            add_tags=frozenset({"x"}),
        )
    )
    logger.info("判定依据: gv=%s tags=%s", out.governance_version, out.tags)
    assert out.governance_version == 2 and out.tags == ["x"]


def test_tag_add_remove_and_replace(catalog):
    _asset(catalog)
    catalog.update_governance(
        GovernanceUpdate(asset="t1", expected_governance_version=0,
                         add_tags=frozenset({"a", "b"}))
    )
    logger.info("用例输入: gv=1 增量 remove a / add c")
    out = catalog.update_governance(
        GovernanceUpdate(asset="t1", expected_governance_version=1,
                         add_tags=frozenset({"c"}), remove_tags=frozenset({"a"}))
    )
    logger.info("判定依据: tags=%s", out.tags)
    assert out.tags == ["b", "c"]
    logger.info("用例输入: gv=2 replace_tags + add={z}")
    out = catalog.update_governance(
        GovernanceUpdate(asset="t1", expected_governance_version=2,
                         add_tags=frozenset({"z"}), replace_tags=True)
    )
    assert out.tags == ["z"]
    logger.info("判定依据: replace 后 tags=%s", out.tags)


def test_idempotent_governance_write_keeps_version(catalog):
    _asset(catalog)
    catalog.update_governance(
        GovernanceUpdate(asset="t1", expected_governance_version=0,
                         owner="alice", add_tags=frozenset({"a"}))
    )
    logger.info("用例输入: 相同 owner/tags 重复提交")
    out = catalog.update_governance(
        GovernanceUpdate(asset="t1", expected_governance_version=1,
                         owner="alice", add_tags=frozenset({"a"}))
    )
    logger.info("判定依据: changed=%s gv=%s", out.changed, out.governance_version)
    assert not out.changed and out.governance_version == 1


def test_governance_validation_errors(catalog):
    _asset(catalog)
    logger.info("用例输入: 空 owner / 同增同删同一标签 / 不存在资产")
    with pytest.raises(ValidationError):
        catalog.update_governance(
            GovernanceUpdate(asset="t1",
                             expected_governance_version=0, owner="   ")
        )
    with pytest.raises(ValidationError):
        catalog.update_governance(
            GovernanceUpdate(asset="t1", expected_governance_version=0,
                             add_tags=frozenset({"x"}),
                             remove_tags=frozenset({"x"}))
        )
    with pytest.raises(AssetNotFoundError):
        catalog.update_governance(
            GovernanceUpdate(asset="ghost", expected_governance_version=0)
        )


def test_concurrent_same_schema_register_is_idempotent(catalog):
    """并发用完全相同的结构登记同一资产，只允许一个 created，其余幂等。"""
    n = 12
    logger.info("用例输入: %d 线程并发登记同一结构", n)
    actions: list[str] = []
    barrier = threading.Barrier(n)

    def worker() -> None:
        barrier.wait()
        spec = AssetSpec(
            name="dup",
            asset_type=AssetType.TABLE,
            fields=[FieldSpec("id", FieldType.LONG, False)],
        )
        actions.append(catalog.register(RegisterRequest(spec=spec)).action)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(worker) for _ in range(n)]
        for fut in futures:
            fut.result()
    logger.info("判定依据: created=%d idempotent=%d",
                actions.count("created"), actions.count("re_registered_idempotent"))
    assert actions.count("created") == 1
    assert actions.count("re_registered_idempotent") == n - 1
    assert catalog.get_asset("dup").schema_version == 1


def test_concurrent_lineage_duplicate_edge(catalog):
    """并发写同一条血缘边：只允许一个 accepted，其余 duplicate_edge。"""
    for asset_name in ("a", "b"):
        catalog.register(
            RegisterRequest(
                spec=AssetSpec(name=asset_name, asset_type=AssetType.TABLE)
            )
        )
    thread_count = 12
    logger.info("用例输入: %d 线程并发写 a->b", thread_count)
    codes: list[str] = []
    barrier = threading.Barrier(thread_count)
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        r = catalog.add_lineage(LineageEdge("a", "b"))
        with lock:
            codes.append(r.code)

    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        futures = [pool.submit(worker) for _ in range(thread_count)]
        for fut in futures:
            fut.result()  # 暴露工作线程内的异常
    logger.info("判定依据: accepted=%d duplicate=%d",
                codes.count("accepted"), codes.count("duplicate_edge"))
    assert codes.count("accepted") == 1
    assert codes.count("duplicate_edge") == thread_count - 1
    assert len(catalog.store.list_edges()) == 1


def test_concurrent_schema_evolution_only_one_wins(catalog):
    """同进程内 N 个线程基于同一 sv 并发演进：恰好 1 成功，其余可区分冲突。"""
    _asset(catalog)
    n = 16
    logger.info("用例输入: %d 线程同时基于 sv=1 加不同字段", n)
    results: list[tuple[str, int]] = []
    barrier = threading.Barrier(n)
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        try:
            out = catalog.register(
                RegisterRequest(
                    spec=AssetSpec(
                        name="t1",
                        asset_type=AssetType.TABLE,
                        fields=[
                            FieldSpec("id", FieldType.LONG, False),
                            FieldSpec(f"col{i}", FieldType.STRING),
                        ],
                    ),
                    expected_schema_version=1,
                )
            )
            with lock:
                results.append((out.action, out.schema_version))
        except VersionConflictError as exc:
            with lock:
                results.append(("conflict", exc.actual_version))

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))
    evolved = [r for r in results if r[0] == "evolved"]
    conflicts = [r for r in results if r[0] == "conflict"]
    logger.info("判定依据: evolved=%d conflict=%d", len(evolved), len(conflicts))
    assert len(evolved) == 1
    assert len(conflicts) == n - 1
    assert {r[1] for r in conflicts} == {2}
    snap = catalog.get_asset("t1")
    assert snap.schema_version == 2
    assert len(snap.fields) == 2  # 无字段拼花


def test_returned_snapshots_are_independent_from_store(catalog):
    """调用方拿到的快照可自由修改，不会污染仓库内状态。"""
    _asset(catalog)
    leaked = catalog.get_asset("t1")
    leaked.schema_version = 99
    leaked.tags.add("ghost")
    leaked.fields["ghost"] = object()  # type: ignore[assignment]
    fresh = catalog.get_asset("t1")
    assert fresh.schema_version == 1
    assert fresh.tags == set()
    assert set(fresh.fields) == {"id"}
