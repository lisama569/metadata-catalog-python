"""同一进程内的真实并发与失败回滚测试（持久化仓库 + 线程）。

与 test_governance.py 里既有的线程用例相比，这里聚焦修复的两类问题：

1. 真实"并发读 + 并发写"混合压力：读之间并行且绝不报错/超时，读到的
   每个快照都结构自洽（版本与字段内容对得上）；写之间严格串行，结论
   是"恰好一个成功 + 其余可区分冲突"，重复操作不重复推进版本；
2. 一次写入在 rename 前失败后：同进程后续读到的仍是写入前完整状态，
   随后一次"无关更新"不会把失败的版本/字段/治理信息带出来；重开仓库
   （等价重启）后同样不可见。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import (
    StorageError,
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
from metadata_catalog.persistence import PersistentStore

logger = logging.getLogger("metadata_catalog.tests")


@pytest.fixture
def repo_path(tmp_path):
    return str(tmp_path / "repo")


@pytest.fixture
def pcat(repo_path):
    catalog = MetadataCatalog(PersistentStore(repo_path))
    yield catalog
    catalog.store.close()


def _table(name, *fields):
    return AssetSpec(
        name=name,
        asset_type=AssetType.TABLE,
        fields=list(fields)
        or [FieldSpec("id", FieldType.LONG, nullable=False)],
    )


def _seed(catalog, *names):
    for nm in names:
        catalog.register(RegisterRequest(spec=_table(nm)))


def test_mixed_concurrent_reads_and_writes_are_coherent(pcat):
    """持续并发读 hot + 突发并发写（治理/演进/无关资产/血缘）。

    任何一次读都必须返回 200 式的有效快照：治理版本、结构版本、字段
    集合三者永远对得上；写竞争恰好一个成功。
    """
    _seed(pcat, "hot", "a", "b")
    n = 24
    read_errors: list[str] = []
    stop = threading.Event()
    start = threading.Event()

    def reader():
        count = 0
        start.wait()
        while not stop.is_set():
            snap = pcat.get_asset("hot")
            # 不变量：字段集合只可能是 {id} 或 {id,name}；版本随之取 1/2；
            # gv 只能 0/1。读到中间拼花状态即为失败。
            names = set(snap.fields)
            if names == {"id"}:
                if snap.schema_version != 1:
                    read_errors.append(f"sv={snap.schema_version} 但字段={names}")
                    return count
            elif names == {"id", "name"}:
                if snap.schema_version != 2:
                    read_errors.append(f"sv={snap.schema_version} 但字段={names}")
                    return count
            else:
                read_errors.append(f"非法字段集合 {names}")
                return count
            if snap.governance_version not in (0, 1):
                read_errors.append(f"非法 gv={snap.governance_version}")
                return count
            # impact 遍历（含血缘图）也不能抛错
            pcat.impact("a")
            count += 1
        return count

    def gov_writer(i):
        start.wait()
        try:
            pcat.update_governance(GovernanceUpdate(
                asset="hot", expected_governance_version=0, owner=f"u{i}"))
            return "ok"
        except VersionConflictError:
            return "conflict"

    def unrelated_writer(i):
        start.wait()
        pcat.register(RegisterRequest(spec=_table(f"load{i}")))
        pcat.add_lineage(LineageEdge(f"load{i}", "a"))
        return "loaded"

    with ThreadPoolExecutor(max_workers=n + 8) as pool:
        read_futs = [pool.submit(reader) for _ in range(12)]
        gov_futs = [pool.submit(gov_writer, i) for i in range(n)]
        indep_futs = [pool.submit(unrelated_writer, i) for i in range(8)]
        start.set()
        gov_results = [f.result() for f in gov_futs]
        indep = [f.result() for f in indep_futs]
        stop.set()
        total_reads = sum(f.result() for f in read_futs)

    assert not read_errors, read_errors[:1]
    assert gov_results.count("ok") == 1
    assert gov_results.count("conflict") == n - 1
    assert indep.count("loaded") == 8
    snap = pcat.get_asset("hot")
    assert snap.governance_version == 1
    logger.info("判定依据: %d 次并发读全部自洽，治理写 1 成功 %d 冲突，无关写 8 成功",
                total_reads, n - 1)
    assert total_reads > 0
    # 血缘：8 条 loadN -> a
    imp = pcat.impact("a")
    assert len(imp.upstream_direct) == 8


def test_concurrent_writes_same_version_only_one_wins(pcat):
    """同版本并发改结构 / 登记 / 更新标签负责人，其余可区分冲突。"""
    _seed(pcat, "hot")
    n = 24
    start = threading.Event()

    def evolve(i):
        start.wait()
        try:
            out = pcat.register(RegisterRequest(
                spec=_table("hot",
                            FieldSpec("id", FieldType.LONG, False),
                            FieldSpec(f"col{i}", FieldType.STRING)),
                expected_schema_version=1))
            return (out.action, out.schema_version)
        except VersionConflictError as exc:
            return ("conflict", exc.actual_version)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(evolve, i) for i in range(n)]
        start.set()
        results = [f.result() for f in futs]
    evolved = [r for r in results if r[0] == "evolved"]
    conflicts = [r for r in results if r[0] == "conflict"]
    assert len(evolved) == 1 and len(conflicts) == n - 1
    assert {r[1] for r in conflicts} == {2}
    snap = pcat.get_asset("hot")
    assert snap.schema_version == 2
    assert len(snap.fields) == 2  # 只留下获胜者的字段，无拼花
    logger.info("判定依据: 演进 1 成功，%d 冲突 actual=2，字段无拼花", n - 1)


def test_repeated_concurrent_operations_do_not_bump_version(pcat):
    """重复（幂等）操作在高并发下也不能重复推进任何版本。"""
    _seed(pcat, "dup", "a", "b")
    n = 24
    start = threading.Event()

    def same_register(_i):
        start.wait()
        return pcat.register(RegisterRequest(spec=_table("dup"))).action

    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(same_register, i) for i in range(n)]
        start.set()
        actions = [f.result() for f in futs]
    assert actions.count("re_registered_idempotent") == n
    assert pcat.get_asset("dup").schema_version == 1

    # 先成功写一次治理（gv 0 -> 1），再并发提交完全相同的幂等治理请求
    pcat.update_governance(GovernanceUpdate(
        asset="dup", expected_governance_version=0,
        owner="alice", add_tags=frozenset({"x"})))
    start = threading.Event()

    def same_gov(_i):
        start.wait()
        return pcat.update_governance(GovernanceUpdate(
            asset="dup", expected_governance_version=1,
            owner="alice", add_tags=frozenset({"x"}))).governance_version

    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(same_gov, i) for i in range(n)]
        start.set()
        gvs = [f.result() for f in futs]
    assert set(gvs) == {1}
    snap = pcat.get_asset("dup")
    assert snap.governance_version == 1 and snap.tags == {"x"}
    logger.info("判定依据: %d 次重复登记/重复治理均幂等，版本未重复推进", n)


def test_failed_write_invisible_to_later_reads_and_unrelated_writes(
    pcat, monkeypatch
):
    """rename 前失败：同进程后续读、随后一次无关写、再重开仓库都看不到它。"""
    _seed(pcat, "t1", "t2")
    monkeypatch.setenv("METADATA_CATALOG_CRASH_AFTER_WRITE", "raise")
    with pytest.raises(OSError):
        pcat.register(RegisterRequest(
            spec=_table("t1",
                        FieldSpec("id", FieldType.LONG, False),
                        FieldSpec("name", FieldType.STRING)),
            expected_schema_version=1))
    monkeypatch.delenv("METADATA_CATALOG_CRASH_AFTER_WRITE")

    # 1) 同进程后续读：写入前完整状态
    snap = pcat.get_asset("t1")
    assert snap.schema_version == 1 and set(snap.fields) == {"id"}
    assert snap.owner is None and snap.governance_version == 0

    # 2) 随后一次"无关更新"（另一个资产的结构演进）成功，
    #    且不会把 t1 的失败结构带出
    pcat.register(RegisterRequest(spec=_table(
        "t2",
        FieldSpec("id", FieldType.LONG, False),
        FieldSpec("note", FieldType.STRING))))
    t1 = pcat.get_asset("t1")
    assert t1.schema_version == 1 and set(t1.fields) == {"id"}
    t2 = pcat.get_asset("t2")
    assert t2.schema_version == 2 and set(t2.fields) == {"id", "note"}

    # 3) 对 t1 的一次无关治理写成功；落盘内容仍是旧结构 + 新治理
    out = pcat.update_governance(GovernanceUpdate(
        asset="t1", expected_governance_version=0, owner="alice"))
    assert out.governance_version == 1
    t1 = pcat.get_asset("t1")
    assert t1.schema_version == 1 and set(t1.fields) == {"id"}
    assert t1.owner == "alice" and t1.governance_version == 1
    logger.info("判定依据: 失败后同进程读/无关写均未带出失败结构")


def test_failed_write_still_invisible_after_reopen(tmp_path, monkeypatch):
    """失败后重开仓库（等价重启）：状态完整停留在写入前，无临时残留。"""
    repo = str(tmp_path / "repo")
    cat = MetadataCatalog(PersistentStore(repo))
    cat.register(RegisterRequest(spec=_table(
        "t1", FieldSpec("id", FieldType.LONG, False))))
    cat.store.close()

    monkeypatch.setenv("METADATA_CATALOG_CRASH_AFTER_WRITE", "raise")
    cat2 = MetadataCatalog(PersistentStore(repo))
    with pytest.raises(OSError):
        cat2.register(RegisterRequest(spec=_table(
            "t1",
            FieldSpec("id", FieldType.LONG, False),
            FieldSpec("name", FieldType.STRING)),
            expected_schema_version=1))
    cat2.store.close()
    monkeypatch.delenv("METADATA_CATALOG_CRASH_AFTER_WRITE")

    cat3 = MetadataCatalog(PersistentStore(repo))
    snap = cat3.get_asset("t1")
    assert snap.schema_version == 1 and set(snap.fields) == {"id"}
    import os

    leftovers = [
        n for n in os.listdir(os.path.join(repo, "assets"))
        if n.startswith(".tmp.")
    ]
    assert leftovers == []
    # 失败之后的下一次正常写必须能成功（锁与文件都处于完整状态）
    out = cat3.register(RegisterRequest(spec=_table(
        "t1",
        FieldSpec("id", FieldType.LONG, False),
        FieldSpec("name", FieldType.STRING)),
        expected_schema_version=1))
    assert out.action == "evolved" and out.schema_version == 2
    cat3.store.close()
    logger.info("判定依据: 重启后失败写不可见、无临时残留、后续写成功")


def test_storage_failure_is_classified_not_bare_exception(pcat, monkeypatch):
    """磁盘写故障经存储层归类为 StorageError（OSError 子类，带 code）。"""
    _seed(pcat, "t1")
    monkeypatch.setenv("METADATA_CATALOG_CRASH_AFTER_WRITE", "raise")
    with pytest.raises(StorageError) as exc:
        pcat.register(RegisterRequest(spec=_table(
            "t1",
            FieldSpec("id", FieldType.LONG, False),
            FieldSpec("name", FieldType.STRING))))
    assert isinstance(exc.value, OSError)
    assert exc.value.code == "storage_error"
    # 故障后仓库仍可用（去掉注入即可继续）
    monkeypatch.delenv("METADATA_CATALOG_CRASH_AFTER_WRITE")
    assert pcat.get_asset("t1").schema_version == 1


def test_failed_lineage_write_leaves_no_edge_and_is_retryable(
    pcat, repo_path, monkeypatch
):
    """血缘边落盘失败：边在同进程/重启后都不存在，故障消除后重试可成功。"""
    _seed(pcat, "a", "b")
    monkeypatch.setenv("METADATA_CATALOG_CRASH_AFTER_WRITE", "raise")
    with pytest.raises(StorageError):
        pcat.add_lineage(LineageEdge("a", "b"))
    monkeypatch.delenv("METADATA_CATALOG_CRASH_AFTER_WRITE")
    # 失败的边没有进入缓存（has_edge=False，impact 里也看不到）
    assert not pcat.store.has_edge("a", "b")
    assert pcat.impact("a").downstream_direct == []
    # 重试成功，且只留下一条边
    r = pcat.add_lineage(LineageEdge("a", "b"))
    assert r.accepted and r.code == "accepted"
    assert len(pcat.store.list_edges()) == 1
    pcat.store.close()

    reopened = MetadataCatalog(PersistentStore(repo_path))
    assert len(reopened.store.list_edges()) == 1
    reopened.store.close()
    logger.info("判定依据: 血缘写失败后同进程/重启均无残留边，重试只落一条")
