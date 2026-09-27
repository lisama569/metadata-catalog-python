"""真实多进程并发：多个 OS 进程打开同一个本地仓库目录。

这些用例不用线程模拟，而是用 ``multiprocessing`` 拉起独立进程，每个进程
独立构造 ``PersistentStore``（独立的 flock 客户端、独立缓存），通过
``Barrier`` 同时发起写入，验证跨进程的：

* 乐观锁结论（恰好一个成功，其余可区分的 version_conflict）；
* 登记幂等（只产生一次 created / 版本不重复累加）；
* 血缘边去重（只落一份，重复边明确标记）。
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os

import pytest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.models import (
    AssetSpec,
    AssetType,
    FieldSpec,
    FieldType,
    GovernanceUpdate,
    RegisterRequest,
)
from metadata_catalog.persistence import PersistentStore

import _mp_workers as workers

logger = logging.getLogger("metadata_catalog.tests")

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="flock 持久化仓库的多进程测试依赖 POSIX"
)


def _ctx():
    # spawn：子进程拿到的是全新解释器，最贴近"另起一个服务进程"。
    return mp.get_context("spawn")


@pytest.fixture
def seeded_repo(tmp_path):
    repo = str(tmp_path / "repo")
    catalog = MetadataCatalog(PersistentStore(repo))

    def table(name):
        return AssetSpec(
            name=name,
            asset_type=AssetType.TABLE,
            fields=[FieldSpec("id", FieldType.LONG, nullable=False)],
        )

    catalog.register(RegisterRequest(spec=table("hot")))
    catalog.register(RegisterRequest(spec=table("a")))
    catalog.register(RegisterRequest(spec=table("b")))
    catalog.store.close()
    return repo


def _run(worker, repo, n):
    ctx = _ctx()
    barrier = ctx.Barrier(n)
    with ctx.Pool(
        processes=n,
        initializer=workers.init_worker,
        initargs=(barrier,),
    ) as pool:
        results = pool.map(worker, [(repo, i) for i in range(n)])
    return results


def test_mp_governance_only_one_wins(seeded_repo):
    n = 8
    logger.info("用例输入: %d 个进程同时基于 gv=0 写不同 owner", n)
    results = _run(workers.governance_worker, seeded_repo, n)
    oks = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "conflict"]
    logger.info("判定依据: ok=%d conflict=%d results=%s",
                len(oks), len(conflicts), results)
    assert len(oks) == 1
    assert len(conflicts) == n - 1
    # 冲突方拿到的 actual_version 必须能明确区分（=1），不能静默成功
    assert {r[1] for r in conflicts} == {1}

    catalog = MetadataCatalog(PersistentStore(seeded_repo))
    snap = catalog.get_asset("hot")
    assert snap.governance_version == 1
    assert snap.owner == oks[0][2]
    assert snap.owner in {f"user{i}" for i in range(n)}
    catalog.store.close()


def test_mp_concurrent_register_idempotent(seeded_repo):
    n = 8
    logger.info("用例输入: %d 个进程并发首次登记同一资产 dup", n)
    results = _run(workers.register_worker, seeded_repo, n)
    created = [r for r in results if r[0] == "created"]
    idem = [r for r in results if r[0] == "re_registered_idempotent"]
    logger.info("判定依据: created=%d idempotent=%d", len(created), len(idem))
    assert len(created) == 1
    assert len(idem) == n - 1
    assert {v for _a, v in results} == {1}
    catalog = MetadataCatalog(PersistentStore(seeded_repo))
    assert catalog.get_asset("dup").schema_version == 1
    catalog.store.close()


def test_mp_concurrent_lineage_dedup(seeded_repo):
    n = 8
    logger.info("用例输入: %d 个进程并发写同一条边 a->b", n)
    results = _run(workers.lineage_worker, seeded_repo, n)
    accepted = [r for r in results if r[0] == "accepted"]
    dup = [r for r in results if r[1] == "duplicate_edge"]
    logger.info("判定依据: accepted=%d duplicate=%d", len(accepted), len(dup))
    assert len(accepted) == 1
    assert len(dup) == n - 1
    catalog = MetadataCatalog(PersistentStore(seeded_repo))
    assert len(catalog.store.list_edges()) == 1
    catalog.store.close()


def test_mp_evolve_only_one_wins(seeded_repo):
    n = 8
    logger.info("用例输入: %d 个进程同时基于 sv=1 加不同字段", n)
    results = _run(workers.evolve_worker, seeded_repo, n)
    evolved = [r for r in results if r[0] == "evolved"]
    conflicts = [r for r in results if r[0] == "conflict"]
    logger.info("判定依据: evolved=%d conflict=%d results=%s",
                len(evolved), len(conflicts), results)
    assert len(evolved) == 1
    assert len(conflicts) == n - 1
    assert {r[1] for r in conflicts} == {2}
    catalog = MetadataCatalog(PersistentStore(seeded_repo))
    snap = catalog.get_asset("hot")
    assert snap.schema_version == 2
    # 只新增了获胜者那一个字段，不能出现字段拼花
    added = set(snap.fields) - {"id"}
    assert len(added) == 1 and next(iter(added)).startswith("col")
    catalog.store.close()


def write_assets_from_other_process(repo: str) -> None:
    """另一个独立进程：登记资产并写血缘，验证已开仓库能增量看到。"""
    catalog = MetadataCatalog(PersistentStore(repo))
    catalog.register(
        RegisterRequest(
            spec=AssetSpec(
                name="remote",
                asset_type=AssetType.TABLE,
                fields=[FieldSpec("id", FieldType.LONG, nullable=False)],
            )
        )
    )
    catalog.register(
        RegisterRequest(
            spec=AssetSpec(
                name="remote2",
                asset_type=AssetType.TABLE,
                fields=[FieldSpec("id", FieldType.LONG, nullable=False)],
            )
        )
    )
    from metadata_catalog.models import LineageEdge

    catalog.add_lineage(LineageEdge("remote", "remote2"))
    catalog.store.close()


def test_writes_from_other_process_become_visible(seeded_repo):
    """父进程先暖好缓存，子进程再写入；父进程下一次临界区必须读到新数据。"""
    ctx = _ctx()
    watcher = MetadataCatalog(PersistentStore(seeded_repo))
    assert {a.name for a in watcher.list_assets()} == {"hot", "a", "b"}

    p = ctx.Process(target=write_assets_from_other_process, args=(seeded_repo,))
    p.start()
    p.join(timeout=30)
    assert p.exitcode == 0

    logger.info("判定依据: 他进程提交后，已开仓库的下一次读刷新缓存")
    names = {a.name for a in watcher.list_assets()}
    assert names == {"hot", "a", "b", "remote", "remote2"}
    assert watcher.get_asset("remote").schema_version == 1
    assert watcher.impact("remote").downstream_direct == ["remote2"]
    watcher.store.close()
