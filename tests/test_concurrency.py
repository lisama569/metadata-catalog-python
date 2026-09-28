"""同一进程内的真实并发测试（线程 + 持久化仓库）。

与 ``test_governance.py`` 中基于内存仓库的用例互补，这里全部使用真实磁盘
上的 :class:`PersistentStore`（与 HTTP 服务同一条代码路径），用 ``Barrier``
让多个线程同时发起请求，验证：

* 真实并发读 + 写：读彼此并行、绝不因缓存刷新抛未分类异常，读者只可能
  读到写入前或写入后的完整快照；
* 同版本并发演进 / 并发登记 / 并发治理：恰好一个成功，其余是可区分的
  version_conflict / 幂等结论，版本不重复推进、字段不拼花；
* 磁盘写入失败后：同进程后续读仍得到写入前的完整状态，随后一次无关写
  不会把失败内容带出。
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from metadata_catalog.errors import StorageError, VersionConflictError
from metadata_catalog.models import (
    AssetSpec,
    AssetType,
    FieldSpec,
    FieldType,
    GovernanceUpdate,
    LineageEdge,
    RegisterRequest,
)
from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.persistence import PersistentStore

logger = logging.getLogger("metadata_catalog.tests")


@pytest.fixture
def catalog(tmp_path):
    c = MetadataCatalog(PersistentStore(tmp_path / "repo"))
    yield c
    c.store.close()


def _table(name, *fields):
    return AssetSpec(
        name=name,
        asset_type=AssetType.TABLE,
        fields=list(fields)
        or [FieldSpec("id", FieldType.LONG, nullable=False)],
    )


def _seed(catalog, *names):
    for n in names:
        catalog.register(RegisterRequest(spec=_table(n)))


def test_concurrent_reads_and_writes_never_error(catalog):
    """持续并发读 + 写：所有调用必须成功或可区分冲突，无未分类异常；
    读者看到的结构版本与字段集合始终自洽。"""
    _seed(catalog, "hot")
    n_readers, n_writers = 8, 6
    start = Barrier(n_readers + n_writers)
    stop = Event()
    errors: list[Exception] = []

    def reader():
        start.wait()
        try:
            while not stop.is_set():
                snap = catalog.get_asset("hot")
                # 每个读者还走 list 路径，覆盖缓存遍历
                catalog.list_assets()
                # 版本与字段必须对得上：每个生效写恰好增加一个字段
                assert len(snap.fields) == snap.schema_version
        except Exception as exc:  # noqa: BLE001 - 并发下任何异常都不允许
            errors.append(exc)

    def writer(i):
        start.wait()
        # 基于读到的最新结构做"纯追加"演进；冲突是合法结论，不记录为错误。
        snap = catalog.get_asset("hot")
        fields = [
            FieldSpec(r.spec.name, r.spec.field_type, r.spec.nullable,
                      r.spec.description)
            for r in snap.fields.values()
        ]
        fields.append(FieldSpec(f"c{i}", FieldType.STRING))
        try:
            catalog.register(
                RegisterRequest(
                    spec=AssetSpec(name="hot", asset_type=AssetType.TABLE,
                                   fields=fields),
                    expected_schema_version=snap.schema_version,
                )
            )
        except VersionConflictError:
            pass

    with ThreadPoolExecutor(max_workers=n_readers + n_writers) as pool:
        readers = [pool.submit(reader) for _ in range(n_readers)]
        writers = [pool.submit(writer, i) for i in range(n_writers)]
        for f in writers:
            f.result()
        stop.set()
        for f in readers:
            f.result()

    assert errors == []
    snap = catalog.get_asset("hot")
    logger.info("判定依据: 最终 sv=%s 字段=%s", snap.schema_version,
                sorted(snap.fields))
    # 至少一个写成功；字段数与版本号始终匹配（无版本/内容对不上）
    assert snap.schema_version >= 2
    assert len(snap.fields) == snap.schema_version


def test_concurrent_evolution_same_version_one_wins(catalog):
    """N 个线程基于同一 schema_version 给同一资产加不同字段：
    恰好一个 evolved，其余全部 version_conflict 且 actual_version 可区分；
    最终只 +1 一次、字段不拼花。"""
    _seed(catalog, "hot")
    n = 16
    start = Barrier(n)
    outcomes: list[tuple[str, int]] = []

    def worker(i):
        start.wait()
        try:
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
            outcomes.append((out.action, out.schema_version))
        except VersionConflictError as exc:
            outcomes.append(("conflict", exc.actual_version))

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    evolved = [o for o in outcomes if o[0] == "evolved"]
    conflicts = [o for o in outcomes if o[0] == "conflict"]
    logger.info("判定依据: evolved=%d conflict=%d outcomes=%s",
                len(evolved), len(conflicts), outcomes)
    assert len(evolved) == 1
    assert len(conflicts) == n - 1
    # 冲突方都明确看到新版本 2，而不是模糊失败
    assert {v for _a, v in conflicts} == {2}
    snap = catalog.get_asset("hot")
    assert snap.schema_version == 2
    added = set(snap.fields) - {"id"}
    assert len(added) == 1
    # 失败的各方重试时必须先拿到 2，不能再凭旧版本成功
    for i in range(n):
        with pytest.raises(VersionConflictError):
            catalog.register(
                RegisterRequest(
                    spec=_table(
                        "hot",
                        FieldSpec("id", FieldType.LONG, False),
                        FieldSpec(f"retry{i}", FieldType.STRING),
                    ),
                    expected_schema_version=1,
                )
            )
    assert catalog.get_asset("hot").schema_version == 2


def test_concurrent_register_new_asset_idempotent(catalog):
    """并发首次登记同一资产：恰好一个 created，其余幂等，版本恒为 1。"""
    n = 16
    start = Barrier(n)
    actions: list[str] = []

    def worker():
        start.wait()
        out = catalog.register(RegisterRequest(spec=_table("dup")))
        actions.append(out.action)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda _: worker(), range(n)))

    assert actions.count("created") == 1
    assert actions.count("re_registered_idempotent") == n - 1
    assert catalog.get_asset("dup").schema_version == 1


def test_concurrent_governance_same_version_one_wins(catalog):
    """并发基于同一治理版本改负责人：一个成功，其余可区分冲突，gv 只 +1。"""
    _seed(catalog, "hot")
    n = 16
    start = Barrier(n)
    results: list[tuple[str, int]] = []

    def worker(i):
        start.wait()
        try:
            out = catalog.update_governance(
                GovernanceUpdate(
                    asset="hot",
                    expected_governance_version=0,
                    owner=f"user{i}",
                )
            )
            results.append(("ok", out.governance_version))
        except VersionConflictError as exc:
            results.append(("conflict", exc.actual_version))

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    oks = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "conflict"]
    assert len(oks) == 1 and oks[0][1] == 1
    assert len(conflicts) == n - 1
    assert {v for _a, v in conflicts} == {1}
    snap = catalog.get_asset("hot")
    assert snap.governance_version == 1
    assert snap.owner in {f"user{i}" for i in range(n)}
    # 标签/负责人/结构版本不能相互错位：结构版本未动
    assert snap.schema_version == 1


def test_concurrent_duplicate_lineage_single_edge(catalog):
    """并发写同一条血缘边：只一个 accepted，其余 duplicate_edge，只存一份。"""
    _seed(catalog, "a", "b")
    n = 16
    start = Barrier(n)
    codes: list[str] = []

    def worker():
        start.wait()
        codes.append(catalog.add_lineage(LineageEdge("a", "b")).code)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(lambda _: worker(), range(n)))

    assert codes.count("accepted") == 1
    assert codes.count("duplicate_edge") == n - 1
    assert len(catalog.store.list_edges()) == 1


def test_failed_write_invisible_to_later_reads_in_same_process(
    catalog, monkeypatch
):
    """磁盘写失败后：同进程后续读仍是写入前完整状态；异常是可分类的
    StorageError（同时也是 OSError），不是未分类服务端异常。"""
    _seed(catalog, "hot")

    monkeypatch.setenv("METADATA_CATALOG_CRASH_AFTER_WRITE", "raise")
    with pytest.raises(OSError) as excinfo:
        catalog.register(
            RegisterRequest(
                spec=_table(
                    "hot",
                    FieldSpec("id", FieldType.LONG, False),
                    FieldSpec("name", FieldType.STRING),
                ),
                expected_schema_version=1,
            )
        )
    assert isinstance(excinfo.value, StorageError)
    assert excinfo.value.code == "storage_error"
    monkeypatch.delenv("METADATA_CATALOG_CRASH_AFTER_WRITE")

    snap = catalog.get_asset("hot")
    logger.info("判定依据: 失败后同进程读 sv=%s fields=%s",
                snap.schema_version, sorted(snap.fields))
    assert snap.schema_version == 1
    assert set(snap.fields) == {"id"}
    # 结构/治理版本仍自洽
    assert snap.governance_version == 0 and snap.owner is None

    # 一次无关的治理写成功后，失败的结构演进仍不得被"顺带带出"
    out = catalog.update_governance(
        GovernanceUpdate(asset="hot", expected_governance_version=0,
                         owner="alice", add_tags=frozenset({"pii"}))
    )
    assert out.changed and out.governance_version == 1
    snap2 = catalog.get_asset("hot")
    assert snap2.schema_version == 1
    assert set(snap2.fields) == {"id"}
    assert snap2.owner == "alice" and snap2.tags == {"pii"}


def test_failed_governance_write_keeps_previous_governance(
    catalog, monkeypatch
):
    """治理信息写入磁盘失败：负责人/标签/治理版本在同进程内也保持旧值。"""
    _seed(catalog, "hot")
    catalog.update_governance(
        GovernanceUpdate(asset="hot", expected_governance_version=0,
                         owner="alice")
    )
    monkeypatch.setenv("METADATA_CATALOG_CRASH_AFTER_WRITE", "raise")
    with pytest.raises(StorageError):
        catalog.update_governance(
            GovernanceUpdate(asset="hot", expected_governance_version=1,
                             owner="bob", add_tags=frozenset({"x"}))
        )
    monkeypatch.delenv("METADATA_CATALOG_CRASH_AFTER_WRITE")

    snap = catalog.get_asset("hot")
    assert snap.governance_version == 1
    assert snap.owner == "alice" and snap.tags == set()
    # 失败后携带失败时使用的版本重试仍冲突（版本未被推进）
    with pytest.raises(VersionConflictError) as exc:
        catalog.update_governance(
            GovernanceUpdate(asset="hot", expected_governance_version=0,
                             owner="carol")
        )
    assert exc.value.actual_version == 1
