"""多进程下的失败写入不可见性与最终一致性。

场景：

1. 子进程在 rename 前被 ``os._exit`` 杀掉（模拟被 kill）；父进程随后做
   一次"与失败资产无关"的写入，再让第三个进程重新打开仓库（等价重启）：
   失败的结构/版本在任何一方都不可见，结构版本、字段、治理、血缘互相对得上；
2. 两个进程基于同一过期结构版本并发演进：恰好一个成功，另一个冲突，
   磁盘最终只有一份完整的下一版本（无拼花、无重复版本推进）。
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import subprocess
import sys

import pytest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.models import (
    AssetSpec,
    AssetType,
    FieldSpec,
    FieldType,
    RegisterRequest,
)
from metadata_catalog.persistence import PersistentStore

import _mp_workers as workers

logger = logging.getLogger("metadata_catalog.tests")

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="flock 持久化仓库的多进程测试依赖 POSIX"
)


def _ctx():
    return mp.get_context("spawn")


def _table(name, *fields):
    return AssetSpec(
        name=name,
        asset_type=AssetType.TABLE,
        fields=list(fields)
        or [FieldSpec("id", FieldType.LONG, nullable=False)],
    )


def test_killed_write_invisible_after_unrelated_write_and_restart(tmp_path):
    repo = str(tmp_path / "repo")
    cat = MetadataCatalog(PersistentStore(repo))
    cat.register(RegisterRequest(spec=_table(
        "t1", FieldSpec("id", FieldType.LONG, False))))
    cat.register(RegisterRequest(spec=_table(
        "other", FieldSpec("id", FieldType.LONG, False))))
    cat.store.close()

    # 子进程：rename 前被 kill
    script = (
        "import os; os.environ['METADATA_CATALOG_CRASH_AFTER_WRITE']='kill';\n"
        "from metadata_catalog.persistence import PersistentStore;\n"
        "from metadata_catalog.catalog import MetadataCatalog;\n"
        "from metadata_catalog.models import *;\n"
        "c = MetadataCatalog(PersistentStore(%r));\n"
        "c.register(RegisterRequest(spec=AssetSpec(name='t1', "
        "asset_type=AssetType.TABLE, fields=["
        "FieldSpec('id', FieldType.LONG, False), "
        "FieldSpec('name', FieldType.STRING)]), "
        "expected_schema_version=1));\n"
    ) % repo
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    logger.info("判定依据: 被 kill 子进程退出码=%s", proc.returncode)
    assert proc.returncode == 42, proc.stderr

    # 父进程（与被杀进程无关的另一个已开实例）：先确认失败不可见，
    # 再做一次无关资产的写入——该写入绝不能把 t1 的失败状态顺带落盘。
    watcher = MetadataCatalog(PersistentStore(repo))
    t1 = watcher.get_asset("t1")
    assert t1.schema_version == 1 and set(t1.fields) == {"id"}
    watcher.register(RegisterRequest(spec=_table(
        "other",
        FieldSpec("id", FieldType.LONG, False),
        FieldSpec("note", FieldType.STRING))))
    t1 = watcher.get_asset("t1")
    assert t1.schema_version == 1 and set(t1.fields) == {"id"}
    assert watcher.get_asset("other").schema_version == 2
    watcher.store.close()

    # 第三个进程：完全重启后最终状态依旧完整自洽
    restarted = MetadataCatalog(PersistentStore(repo))
    t1 = restarted.get_asset("t1")
    assert t1.schema_version == 1 and set(t1.fields) == {"id"}
    other = restarted.get_asset("other")
    assert other.schema_version == 2 and set(other.fields) == {"id", "note"}
    leftovers = [
        n for n in os.listdir(os.path.join(repo, "assets"))
        if n.startswith(".tmp.")
    ]
    assert leftovers == []
    # 锁已被 OS 释放，新进程可继续写 t1
    out = restarted.register(RegisterRequest(spec=_table(
        "t1",
        FieldSpec("id", FieldType.LONG, False),
        FieldSpec("name", FieldType.STRING)),
        expected_schema_version=1))
    assert out.action == "evolved" and out.schema_version == 2
    restarted.store.close()
    logger.info("判定依据: kill 后无关写与重启均未带出失败状态，锁可继续使用")


def test_mp_stale_schema_concurrent_one_wins(tmp_path):
    """多进程携带同一过期 sv 并发演进：1 成功，其余 409 式冲突且结果可区分。"""
    repo = str(tmp_path / "repo")
    cat = MetadataCatalog(PersistentStore(repo))
    cat.register(RegisterRequest(spec=_table("hot")))
    cat.store.close()

    n = 8
    ctx = _ctx()
    barrier = ctx.Barrier(n)
    with ctx.Pool(
        processes=n,
        initializer=workers.init_worker,
        initargs=(barrier,),
    ) as pool:
        results = pool.map(workers.evolve_worker, [(repo, i) for i in range(n)])
    evolved = [r for r in results if r[0] == "evolved"]
    conflicts = [r for r in results if r[0] == "conflict"]
    logger.info("判定依据: evolved=%d conflict=%d results=%s",
                len(evolved), len(conflicts), results)
    assert len(evolved) == 1
    assert len(conflicts) == n - 1
    assert {r[1] for r in conflicts} == {2}

    final = MetadataCatalog(PersistentStore(repo))
    snap = final.get_asset("hot")
    assert snap.schema_version == 2
    added = set(snap.fields) - {"id"}
    assert len(added) == 1 and next(iter(added)).startswith("col")
    final.store.close()
