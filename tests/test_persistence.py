"""本地持久化语义测试：重启保留、失败/中断不留脏数据。

用真实的磁盘目录（``tmp_path``）与真实的进程重开验证：

* 资产快照（含当前结构、结构版本、字段 since_version、标签、负责人、
  治理版本）与血缘边在进程关闭、重新打开后完整可读；
* "从未登记"与"登记过但写入被拒绝/失败"严格可区分；
* 写入中途抛错后，仓库停留在写入前的完整状态，结构与版本始终对得上。

多进程真实并发见 ``tests/test_multiprocess.py``。
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from metadata_catalog.errors import (
    IncompatibleSchemaError,
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
from metadata_catalog.catalog import MetadataCatalog

logger = logging.getLogger("metadata_catalog.tests")


def _open(repo) -> MetadataCatalog:
    return MetadataCatalog(PersistentStore(repo))


def _table(name: str, *fields) -> AssetSpec:
    return AssetSpec(
        name=name,
        asset_type=AssetType.TABLE,
        fields=list(fields)
        or [FieldSpec("id", FieldType.LONG, nullable=False)],
    )


def test_full_state_survives_restart(tmp_path):
    repo = tmp_path / "repo"
    c1 = _open(repo)
    logger.info("用例输入: 登记 t1/t2，演进 t1，写治理与血缘")
    c1.register(RegisterRequest(spec=_table(
        "t1", FieldSpec("id", FieldType.LONG, False))))
    c1.register(RegisterRequest(spec=_table(
        "t2", FieldSpec("id", FieldType.LONG, False))))
    c1.register(
        RegisterRequest(
            spec=_table(
                "t1",
                FieldSpec("id", FieldType.LONG, False),
                FieldSpec("name", FieldType.STRING),
            ),
            expected_schema_version=1,
        )
    )
    c1.update_governance(
        GovernanceUpdate(
            asset="t1",
            expected_governance_version=0,
            owner="alice",
            add_tags=frozenset({"pii", "finance"}),
        )
    )
    r1 = c1.add_lineage(LineageEdge("t1", "t2"))
    assert r1.accepted
    c1.store.close()

    logger.info("判定依据: 重新打开同一目录（等价于服务重启/另一进程读取）")
    c2 = _open(repo)
    snap = c2.get_asset("t1")
    assert snap.schema_version == 2
    assert snap.governance_version == 1
    assert snap.owner == "alice"
    assert snap.tags == {"finance", "pii"}
    assert snap.fields["id"].since_version == 1
    assert snap.fields["name"].since_version == 2
    t2 = c2.get_asset("t2")
    assert t2.schema_version == 1 and t2.owner is None and t2.tags == set()
    assert {a.name for a in c2.list_assets()} == {"t1", "t2"}

    imp = c2.impact("t1")
    assert imp.downstream_direct == ["t2"]
    assert imp.downstream_transitive == ["t2"]
    assert c2.impact("t2").upstream_direct == ["t1"]
    assert len(c2.store.list_edges()) == 1
    c2.store.close()


def test_rejected_write_not_visible_after_restart(tmp_path):
    """被拒写入（不兼容/版本冲突）在重启后必须仍然像"没发生过"。"""
    repo = tmp_path / "repo"
    c1 = _open(repo)
    c1.register(RegisterRequest(spec=_table(
        "t1", FieldSpec("id", FieldType.LONG, False))))

    logger.info("用例输入: 不兼容的类型变更 long->string")
    with pytest.raises(IncompatibleSchemaError):
        c1.register(RegisterRequest(spec=_table(
            "t1", FieldSpec("id", FieldType.STRING, False))))

    logger.info("用例输入: 携带过期 schema_version=1 演进")
    c1.register(RegisterRequest(spec=_table(
        "t1",
        FieldSpec("id", FieldType.LONG, False),
        FieldSpec("name", FieldType.STRING),
    )))  # sv -> 2
    with pytest.raises(VersionConflictError):
        c1.register(
            RegisterRequest(
                spec=_table(
                    "t1",
                    FieldSpec("id", FieldType.LONG, False),
                    FieldSpec("name", FieldType.STRING),
                    FieldSpec("age", FieldType.INTEGER),
                ),
                expected_schema_version=1,
            )
        )
    c1.store.close()

    c2 = _open(repo)
    snap = c2.get_asset("t1")
    logger.info("判定依据: 重启后 schema_version=%s fields=%s",
                snap.schema_version, sorted(snap.fields))
    assert snap.schema_version == 2
    assert snap.fields["id"].spec.field_type is FieldType.LONG
    assert set(snap.fields) == {"id", "name"}
    c2.store.close()


def test_never_registered_vs_registered_then_failed(tmp_path):
    """从没登记的资产，与登记过但后续失败，必须能被明确区分。"""
    repo = tmp_path / "repo"
    c1 = _open(repo)
    c1.register(RegisterRequest(spec=_table("t1")))
    c1.store.close()

    c2 = _open(repo)
    # 从未登记：磁盘上没有这个资产的数据文件
    assert c2.store.get_asset("ghost") is None
    assert not (repo / "assets").glob("*.json") == []
    ghost_files = list((repo / "assets").glob("*.json"))
    # 登记过的 t1 一定在；ghost 一定没有对应文件
    assert len(ghost_files) == 1
    # 对 t1 的一次失败写入不改变"已登记"事实
    with pytest.raises(IncompatibleSchemaError):
        c2.register(RegisterRequest(spec=_table(
            "t1", FieldSpec("id", FieldType.STRING))))
    assert c2.get_asset("t1") is not None
    c2.store.close()

    c3 = _open(repo)
    assert c3.store.get_asset("ghost") is None  # 依然：从未登记
    assert c3.get_asset("t1") is not None       # 依然：登记过，失败未抹除
    c3.store.close()


def test_failed_write_keeps_complete_previous_state(tmp_path, monkeypatch):
    """atomic_write 在 rename 之前抛错：旧文件必须原封不动且完整。"""
    repo = tmp_path / "repo"
    c1 = _open(repo)
    c1.register(RegisterRequest(spec=_table(
        "t1", FieldSpec("id", FieldType.LONG, False))))
    c1.store.close()

    monkeypatch.setenv("METADATA_CATALOG_CRASH_AFTER_WRITE", "raise")
    c2 = _open(repo)
    with pytest.raises(OSError):
        c2.register(RegisterRequest(spec=_table(
            "t1",
            FieldSpec("id", FieldType.LONG, False),
            FieldSpec("name", FieldType.STRING),
        )))
    monkeypatch.delenv("METADATA_CATALOG_CRASH_AFTER_WRITE")

    # 同进程内：缓存可能仍保留旧对象（未受污染）；磁盘上必须是旧的完整版本。
    c2.store.close()

    logger.info("判定依据: 失败后重新打开，结构与版本停留在写入前")
    c3 = _open(repo)
    snap = c3.get_asset("t1")
    assert snap.schema_version == 1
    assert set(snap.fields) == {"id"}
    # 数据文件仍是可解析的完整 JSON，且没有残留临时文件
    files = list((repo / "assets").iterdir())
    assert [f for f in files if f.name.startswith(".tmp.")] == []
    data = json.loads((repo / "assets" / files[0].name).read_text("utf-8"))
    assert data["schema_version"] == 1
    c3.store.close()


def test_rejected_lineage_and_governance_leave_nothing(tmp_path):
    """血缘/治理的拒绝结论也不得产生任何落盘痕迹。"""
    repo = tmp_path / "repo"
    c1 = _open(repo)
    c1.register(RegisterRequest(spec=_table("a")))
    c1.register(RegisterRequest(spec=_table("b")))
    assert c1.add_lineage(LineageEdge("a", "b")).accepted
    dup = c1.add_lineage(LineageEdge("a", "b"))
    assert not dup.accepted and dup.code == "duplicate_edge"
    missing = c1.add_lineage(LineageEdge("a", "ghost"))
    assert not missing.accepted and missing.code == "asset_not_found"
    with pytest.raises(VersionConflictError):
        c1.update_governance(
            GovernanceUpdate(asset="a", expected_governance_version=9, owner="x")
        )
    c1.store.close()

    c2 = _open(repo)
    assert len(c2.store.list_edges()) == 1
    assert c2.get_asset("a").governance_version == 0
    assert c2.get_asset("a").owner is None
    c2.store.close()


def test_process_killed_mid_write_recovers_previous_state(tmp_path):
    """子进程在"临时文件写完、rename 之前"被 SIGKILL 式杀掉（os._exit）。

    重新打开后必须读到写入前那份完整状态：版本仍为 1、结构不变、
    没有残缺文件、没有残留临时文件，且锁已由操作系统自动释放。
    """
    import subprocess
    import sys

    repo = tmp_path / "repo"
    c1 = _open(repo)
    c1.register(RegisterRequest(spec=_table(
        "t1", FieldSpec("id", FieldType.LONG, False))))
    c1.store.close()

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
    ) % str(repo)
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    logger.info("判定依据: 子进程退出码=%s", proc.returncode)
    assert proc.returncode == 42, proc.stderr

    c2 = _open(repo)
    snap = c2.get_asset("t1")
    assert snap.schema_version == 1
    assert set(snap.fields) == {"id"}
    leftovers = [
        p.name
        for p in (repo / "assets").iterdir()
        if p.name.startswith(".tmp.")
    ]
    assert leftovers == []
    # 锁仍可用：新进程能正常写
    out = c2.register(
        RegisterRequest(
            spec=AssetSpec(
                name="t1",
                asset_type=AssetType.TABLE,
                fields=[
                    FieldSpec("id", FieldType.LONG, False),
                    FieldSpec("name", FieldType.STRING),
                ],
            ),
            expected_schema_version=1,
        )
    )
    assert out.action == "evolved" and out.schema_version == 2
    c2.store.close()


def test_format_file_compatibility_is_checked(tmp_path):
    """FORMAT 标记版本不受支持 / 被破坏时，给出可区分的存储错误且不静默初始化。"""
    from metadata_catalog.errors import (
        StorageCorruptionError,
        UnsupportedStorageFormatError,
    )
    from metadata_catalog.persistence import FORMAT_VERSION

    repo = tmp_path / "repo"
    s = PersistentStore(repo)
    s.close()
    marker = repo / "FORMAT"

    marker.write_text("metadata-catalog-repo format=99\n", encoding="utf-8")
    with pytest.raises(UnsupportedStorageFormatError) as exc:
        PersistentStore(repo)
    assert exc.value.format_version == 99
    assert exc.value.supported_version == FORMAT_VERSION

    marker.write_text("garbage\n", encoding="utf-8")
    with pytest.raises(StorageCorruptionError):
        PersistentStore(repo)


def test_failed_write_then_unrelated_write_then_restart(tmp_path, monkeypatch):
    """失败的结构演进不能被随后一次无关更新带出，重启后同样不可见。

    这是"失败资产结构在之后一次无关更新中被带出，重启后像已生效"的
    专项回归：失败写后先做一次无关治理写（会再次原子落盘），随后在
    *同一进程* 与 *重新打开* 两个视角都断言结构停留在写入前，而治理
    信息是新值——结构版本、字段、标签、负责人彼此对得上。
    """
    repo = tmp_path / "repo"
    c1 = _open(repo)
    c1.register(RegisterRequest(spec=_table(
        "t1", FieldSpec("id", FieldType.LONG, False))))

    monkeypatch.setenv("METADATA_CATALOG_CRASH_AFTER_WRITE", "raise")
    from metadata_catalog.errors import StorageError

    with pytest.raises(StorageError):
        c1.register(RegisterRequest(spec=_table(
            "t1",
            FieldSpec("id", FieldType.LONG, False),
            FieldSpec("name", FieldType.STRING),
        ), expected_schema_version=1))
    monkeypatch.delenv("METADATA_CATALOG_CRASH_AFTER_WRITE")

    # 同进程立即读：失败结构不可见
    s0 = c1.get_asset("t1")
    assert s0.schema_version == 1 and set(s0.fields) == {"id"}

    # 一次与结构无关的治理写（触发又一次原子落盘）
    gov = c1.update_governance(
        GovernanceUpdate(
            asset="t1", expected_governance_version=0,
            owner="alice", add_tags=frozenset({"pii"}),
        )
    )
    assert gov.changed and gov.governance_version == 1

    # 同进程：结构仍是旧的、治理是新的，二者不得串台
    s1 = c1.get_asset("t1")
    assert s1.schema_version == 1 and set(s1.fields) == {"id"}
    assert s1.governance_version == 1
    assert s1.owner == "alice" and s1.tags == {"pii"}
    c1.store.close()

    # 重启后：失败的结构演进依然完全不可见
    c2 = _open(repo)
    s2 = c2.get_asset("t1")
    logger.info("判定依据: 重启后 sv=%s fields=%s gv=%s owner=%s tags=%s",
                s2.schema_version, sorted(s2.fields),
                s2.governance_version, s2.owner, sorted(s2.tags))
    assert s2.schema_version == 1
    assert set(s2.fields) == {"id"}
    assert s2.fields["id"].spec.field_type is FieldType.LONG
    assert s2.governance_version == 1
    assert s2.owner == "alice" and s2.tags == {"pii"}
    # 临时文件无残留
    assert not list((repo / "assets").glob(".tmp.*"))
    c2.store.close()


def test_failed_edge_write_not_visible(tmp_path, monkeypatch):
    """血缘边文件写入失败时：该边在同进程和重启后都不可见，其余边不受影响。"""
    repo = tmp_path / "repo"
    c1 = _open(repo)
    c1.register(RegisterRequest(spec=_table("a")))
    c1.register(RegisterRequest(spec=_table("b")))
    c1.register(RegisterRequest(spec=_table("c")))
    assert c1.add_lineage(LineageEdge("a", "b")).accepted

    monkeypatch.setenv("METADATA_CATALOG_CRASH_AFTER_WRITE", "raise")
    with pytest.raises(OSError):
        c1.add_lineage(LineageEdge("b", "c"))
    monkeypatch.delenv("METADATA_CATALOG_CRASH_AFTER_WRITE")

    # 同进程：失败边不可见，旧边仍在
    assert len(c1.store.list_edges()) == 1
    assert not c1.store.has_edge("b", "c")
    assert c1.store.has_edge("a", "b")
    # 之后正常补写成功（锁/缓存未被失败写破坏）
    assert c1.add_lineage(LineageEdge("b", "c")).accepted
    c1.store.close()

    c2 = _open(repo)
    assert len(c2.store.list_edges()) == 2
    assert c2.store.has_edge("b", "c")
    assert not list((repo / "edges").glob(".tmp.*"))
    c2.store.close()
