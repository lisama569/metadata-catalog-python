"""真实多进程并发访问同一个本地持久化仓库的测试。

用 ``multiprocessing`` 启动**独立进程**（非线程），父进程创建
``Barrier`` / ``Queue`` 传给子进程，使 N 个进程在同一时刻发起写入。
覆盖并发首次登记、基于同版本演进、治理乐观锁、血缘去重与冲突重试。

worker 均为模块顶层函数，spawn/fork 子进程通过 import 本模块取得。
"""

from __future__ import annotations

import multiprocessing as mp

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


def _catalog(repo: str) -> MetadataCatalog:
    return MetadataCatalog(PersistentStore(repo))


# --------------------------------------------------------------------------
# workers（顶层函数，接收共享 barrier/queue）
# --------------------------------------------------------------------------
def worker_concurrent_create(args) -> None:
    repo, i, barrier, q = args
    c = _catalog(repo)
    barrier.wait()
    out = c.register(
        RegisterRequest(
            AssetSpec(
                "shared",
                AssetType.TABLE,
                [FieldSpec("id", FieldType.LONG, nullable=False)],
            )
        )
    )
    c.store.close()
    q.put((i, out.action, out.schema_version))


def worker_concurrent_evolve(args) -> None:
    """每个进程基于同一已见版本 1 增加各自的字段；冲突后重读重试。"""
    repo, i, barrier, q = args
    c = _catalog(repo)
    barrier.wait()
    expected = 1
    field_name = f"f{i}"
    succeeded = False
    attempts = 0
    while not succeeded and attempts < 50:
        attempts += 1
        snap = c.get_asset("shared")
        fields = [
            FieldSpec(name, rec.spec.field_type, rec.spec.nullable)
            for name, rec in sorted(snap.fields.items())
        ]
        if field_name in {f.name for f in fields}:
            # 本进程的字段已在更早的某次成功写入（重试幂等），直接结束。
            succeeded = True
            break
        fields.append(FieldSpec(field_name, FieldType.STRING))
        try:
            out = c.register(
                RegisterRequest(
                    AssetSpec("shared", AssetType.TABLE, fields),
                    expected_schema_version=expected,
                )
            )
            succeeded = out.action == "evolved"
            expected = out.schema_version
        except Exception as exc:  # VersionConflictError
            expected = getattr(exc, "actual_version", expected)
    c.store.close()
    q.put((i, "ok" if succeeded else "give_up"))


def worker_concurrent_governance(args) -> None:
    """每个进程基于同一治理版本打各自标签；冲突后重读重试，标签不丢。"""
    repo, i, barrier, q = args
    c = _catalog(repo)
    barrier.wait()
    tag = f"tag{i}"
    owner = f"owner{i % 3}"
    expected = 0
    done = False
    attempts = 0
    while not done and attempts < 50:
        attempts += 1
        try:
            out = c.update_governance(
                GovernanceUpdate(
                    "shared",
                    expected,
                    owner=owner,
                    add_tags=frozenset({tag}),
                )
            )
            expected = out.governance_version
            done = True
        except Exception as exc:  # VersionConflictError
            expected = getattr(exc, "actual_version", expected)
    c.store.close()
    q.put((i, "ok" if done else "give_up", owner, tag))


def worker_concurrent_edge(args) -> None:
    repo, i, n, barrier, q = args
    c = _catalog(repo)
    barrier.wait()
    # 一半进程重复写同一条边 a->b；另一半各写一条不同边 shared->tN。
    if i % 2 == 0:
        r = c.add_lineage(LineageEdge("a", "b"))
        c.store.close()
        q.put((i, r.code))
        return
    target = f"t{i}"
    # 目标资产由父进程统一预登记，这里直接写不同边。
    r = c.add_lineage(LineageEdge("shared", target))
    c.store.close()
    q.put((i, r.code))


def worker_stale_version_must_conflict(args) -> None:
    """无论并发与否，带着过期版本写入必须拿到冲突，绝不静默成功。"""
    repo, i, barrier, q = args
    c = _catalog(repo)
    barrier.wait()
    conflict = False
    try:
        c.update_governance(
            GovernanceUpdate(
                "shared", expected_governance_version=0, owner=f"p{i}"
            )
        )
    except Exception:
        conflict = True
    c.store.close()
    q.put((i, "conflict" if conflict else "silently_accepted"))


# --------------------------------------------------------------------------
# 测试基础设施
# --------------------------------------------------------------------------
def _run_in_processes(target, args_factory, n: int) -> list:
    """在 n 个独立 spawn 进程里运行 target，收集 Queue 结果（按进程顺序）。"""
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(n)
    q: mp.Queue = ctx.Queue()
    procs = []
    for i in range(n):
        p = ctx.Process(target=target, args=(args_factory(i, barrier, q),))
        procs.append(p)
    for p in procs:
        p.start()
    results = []
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"子进程异常退出 exitcode={p.exitcode}"
    while not q.empty():
        results.append(q.get())
    results.sort(key=lambda x: x[0])
    return results


def _seed_assets(repo: str, names: list[str]) -> None:
    c = _catalog(repo)
    for name in names:
        c.register(
            RegisterRequest(AssetSpec(name, AssetType.TABLE, []))
        )
    c.store.close()


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------
def test_concurrent_first_register_single_create(tmp_path):
    repo = str(tmp_path)
    n = 8
    results = _run_in_processes(
        worker_concurrent_create,
        lambda i, b, q: (repo, i, b, q),
        n,
    )
    actions = [r[1] for r in results]
    versions = [r[2] for r in results]
    assert actions.count("created") == 1, actions
    assert actions.count("re_registered_idempotent") == n - 1, actions
    assert all(v == 1 for v in versions), versions

    c = _catalog(repo)
    snap = c.get_asset("shared")
    assert snap.schema_version == 1
    assert set(snap.fields) == {"id"}
    c.store.close()


def test_concurrent_evolve_no_lost_update(tmp_path):
    repo = str(tmp_path)
    n = 8
    # 预置 version=1 的 shared（含 id 字段）。
    c = _catalog(repo)
    c.register(
        RegisterRequest(
            AssetSpec(
                "shared",
                AssetType.TABLE,
                [FieldSpec("id", FieldType.LONG, nullable=False)],
            )
        )
    )
    c.store.close()

    results = _run_in_processes(
        worker_concurrent_evolve,
        lambda i, b, q: (repo, i, b, q),
        n,
    )
    assert all(r[1] == "ok" for r in results), results

    c = _catalog(repo)
    snap = c.get_asset("shared")
    # 每个进程的字段都必须在，无丢更新；版本恰好 = 1 + n。
    assert set(snap.fields) == {"id"} | {f"f{i}" for i in range(n)}
    assert snap.schema_version == 1 + n, snap.schema_version
    c.store.close()


def test_concurrent_governance_single_winner_and_converge(tmp_path):
    repo = str(tmp_path)
    n = 8
    c = _catalog(repo)
    c.register(RegisterRequest(AssetSpec("shared", AssetType.TABLE, [])))
    c.store.close()

    results = _run_in_processes(
        worker_concurrent_governance,
        lambda i, b, q: (repo, i, b, q),
        n,
    )
    assert all(r[1] == "ok" for r in results), results

    c = _catalog(repo)
    snap = c.get_asset("shared")
    # 每次真实变化推进一次，恰好 n 次。
    assert snap.governance_version == n, snap.governance_version
    assert snap.tags == {f"tag{i}" for i in range(n)}
    c.store.close()


def test_concurrent_lineage_dedup_and_all_distinct_present(tmp_path):
    repo = str(tmp_path)
    n = 8
    seeded = ["a", "b", "shared"] + [f"t{i}" for i in range(n) if i % 2 == 1]
    _seed_assets(repo, seeded)

    results = _run_in_processes(
        worker_concurrent_edge,
        lambda i, b, q: (repo, i, n, b, q),
        n,
    )
    codes = sorted(r[1] for r in results)
    # 4 个偶数进程写同一条 a->b：恰 1 accepted，3 duplicate。
    assert codes.count("accepted") == 1 + (n // 2), codes
    assert codes.count("duplicate_edge") == 3, codes

    c = _catalog(repo)
    edges = c.store.list_edges()
    keys = {(e.upstream, e.downstream) for e in edges}
    assert ("a", "b") in keys
    for i in range(n):
        if i % 2 == 1:
            assert ("shared", f"t{i}") in keys
    # 同一条边在仓库里只能有一份。
    assert len(keys) == len(edges)
    c.store.close()


def test_stale_version_always_conflict_under_concurrency(tmp_path):
    repo = str(tmp_path)
    n = 6
    c = _catalog(repo)
    c.register(RegisterRequest(AssetSpec("shared", AssetType.TABLE, [])))
    # 把治理版本预先推进到非 0，使所有子进程携带的 0 都过期。
    c.update_governance(GovernanceUpdate("shared", 0, owner="seed"))
    c.store.close()

    results = _run_in_processes(
        worker_stale_version_must_conflict,
        lambda i, b, q: (repo, i, b, q),
        n,
    )
    outcomes = [r[1] for r in results]
    assert all(o == "conflict" for o in outcomes), outcomes

    c = _catalog(repo)
    # 没有任何过期写入生效，负责人仍是 seed，版本仍为 1。
    snap = c.get_asset("shared")
    assert snap.owner == "seed"
    assert snap.governance_version == 1
    c.store.close()


def worker_writer_then_reader_visibility(args) -> None:
    """角色 0 写；其余进程等待后必须读到已提交数据（跨进程可见）。"""
    repo, role, barrier, q = args
    c = _catalog(repo)
    if role == 0:
        c.register(
            RegisterRequest(
                AssetSpec(
                    "vis",
                    AssetType.TABLE,
                    [FieldSpec("id", FieldType.LONG)],
                )
            )
        )
        c.update_governance(GovernanceUpdate("vis", 0, owner="writer"))
    barrier.wait()
    snap = c.get_asset("vis")
    c.store.close()
    q.put((role, snap.schema_version, snap.owner, sorted(snap.tags)))


def test_cross_process_visibility_after_commit(tmp_path):
    repo = str(tmp_path)
    n = 4
    results = _run_in_processes(
        worker_writer_then_reader_visibility,
        lambda i, b, q: (repo, i, b, q),
        n,
    )
    for role, sv, owner, _tags in results:
        assert sv == 1 and owner == "writer", (role, sv, owner)


def worker_reader_writer_mix(args) -> None:
    """写者持续产生新版本；读者反复列举+取快照，要求始终读到完整一致状态。"""
    repo, role, barrier, q = args
    c = _catalog(repo)
    barrier.wait()
    import time

    deadline = time.time() + 5.0
    if role % 2 == 0:
        # 写者：演进 shared 多次，每次基于读到的最新版本。
        evolved = 0
        k = 0
        while time.time() < deadline and k < 200:
            snap = c.get_asset("shared")
            names = sorted(snap.fields)
            new_field = f"w{role}_{k}"
            if new_field in snap.fields:
                break
            specs = [
                FieldSpec(n, c.get_asset("shared").fields[n].spec.field_type)
                for n in names
            ]
            specs.append(FieldSpec(new_field, FieldType.STRING))
            try:
                c.register(
                    RegisterRequest(
                        AssetSpec("shared", AssetType.TABLE, specs),
                        expected_schema_version=snap.schema_version,
                    )
                )
                evolved += 1
            except Exception:
                pass
            k += 1
        q.put((role, "writer", evolved))
    else:
        # 读者：任何时刻读到的字段集合必须与其 schema_version 自洽
        # （字段数 >= 初始 1，且不会读到半成品）。
        reads = 0
        ok = True
        while time.time() < deadline and reads < 500:
            snap = c.get_asset("shared")
            if "id" not in snap.fields:
                ok = False
                break
            # 列表查询与详情查询不应崩溃，且数量为正。
            if not c.list_assets():
                ok = False
                break
            reads += 1
        q.put((role, "reader", reads if ok else -1))


def test_concurrent_readers_and_writers_consistent(tmp_path):
    repo = str(tmp_path)
    c = _catalog(repo)
    c.register(
        RegisterRequest(
            AssetSpec(
                "shared",
                AssetType.TABLE,
                [FieldSpec("id", FieldType.LONG, nullable=False)],
            )
        )
    )
    c.store.close()

    n = 6  # 3 写者 + 3 读者
    results = _run_in_processes(
        worker_reader_writer_mix,
        lambda i, b, q: (repo, i, b, q),
        n,
    )
    for _i, kind, value in results:
        if kind == "reader":
            assert value > 0, f"读者读到不一致状态: {value}"
        else:
            assert value >= 0

    # 最终：所有成功演进都落在一个版本链上，无重复/丢失版本。
    final = _catalog(repo)
    snap = final.get_asset("shared")
    # 每成功一次演进 +1；字段数应与版本数自洽。
    assert len(snap.fields) == snap.schema_version
    assert snap.schema_version >= 2
    final.store.close()
