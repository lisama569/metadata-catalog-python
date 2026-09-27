"""本地持久化与崩溃恢复测试。

覆盖：
* 重启（新实例）后资产/结构版本/字段历史/治理信息/血缘完整可读；
* "从未登记"与"登记后写入被拒"在重启后仍可区分，被拒不落脏数据；
* 写事务中途异常 -> 原子回滚，磁盘上不存在残缺结构或版本错位；
* 日志尾部半行（进程被杀）-> 恢复时裁掉，只保留完整已提交事务；
* 快照压缩与代际切换后数据一致；
* 仓库格式版本不兼容时明确报错。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.errors import (
    AssetNotFoundError,
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
from metadata_catalog.persistent_store import (
    FORMAT_VERSION,
    PersistentStore,
    StoreCorruptionError,
)


def _open(repo_dir) -> MetadataCatalog:
    return MetadataCatalog(PersistentStore(str(repo_dir)))


def _orders_spec(*extra: FieldSpec) -> AssetSpec:
    fields = [
        FieldSpec("id", FieldType.LONG, nullable=False),
        FieldSpec("amount", FieldType.DOUBLE),
    ]
    return AssetSpec(
        "orders", AssetType.TABLE, fields + list(extra), description="订单表"
    )


# --------------------------------------------------------------------------
# 1. 重启恢复
# --------------------------------------------------------------------------
def test_full_state_survives_reopen(tmp_path):
    """登记/演进/治理/血缘在新实例里必须完整一致地读到。"""
    cat = _open(tmp_path)
    cat.register(RegisterRequest(_orders_spec()))
    cat.register(RegisterRequest(_users_spec()))
    cat.register(
        RegisterRequest(
            _orders_spec(FieldSpec("note", FieldType.STRING)),
            expected_schema_version=1,
        )
    )
    cat.add_lineage(LineageEdge("users", "orders"))
    cat.update_governance(
        GovernanceUpdate(
            "orders", 0, owner="alice", add_tags=frozenset({"pii", "finance"})
        )
    )
    cat.store.close()

    cat2 = _open(tmp_path)
    snap = cat2.get_asset("orders")
    assert snap.schema_version == 2
    assert snap.governance_version == 1
    assert snap.owner == "alice"
    assert snap.tags == {"pii", "finance"}
    assert set(snap.fields) == {"id", "amount", "note"}
    # 字段首次登记版本不能错位：老字段 since=1，新字段 since=2。
    assert snap.fields["id"].since_version == 1
    assert snap.fields["note"].since_version == 2
    assert snap.fields["id"].spec.field_type is FieldType.LONG
    assert snap.fields["id"].spec.nullable is False
    assert snap.description == "订单表"

    users = cat2.get_asset("users")
    assert users.schema_version == 1
    impact = cat2.impact("orders")
    assert impact.upstream_direct == ["users"]
    assert impact.downstream_direct == []
    assert cat2.list_assets().__len__() == 2
    cat2.store.close()


def _users_spec() -> AssetSpec:
    return AssetSpec(
        "users", AssetType.TABLE, [FieldSpec("uid", FieldType.LONG, nullable=False)]
    )


# --------------------------------------------------------------------------
# 2. 从未登记 vs 登记后被拒
# --------------------------------------------------------------------------
def test_never_registered_distinct_from_rejected_write(tmp_path):
    cat = _open(tmp_path)
    cat.register(RegisterRequest(_orders_spec()))
    # 不兼容写入必须被拒。
    with pytest.raises(IncompatibleSchemaError):
        cat.register(
            RegisterRequest(
                AssetSpec(
                    "orders",
                    AssetType.TABLE,
                    [FieldSpec("id", FieldType.STRING, nullable=False)],
                ),
                expected_schema_version=1,
            )
        )
    # 过期版本写入必须冲突，不算成功（结构本身不变，仅版本号过期）。
    with pytest.raises(VersionConflictError):
        cat.register(
            RegisterRequest(_orders_spec(), expected_schema_version=0)
        )
    # 一个从未登记的资产。
    with pytest.raises(AssetNotFoundError):
        cat.get_asset("ghost")
    cat.store.close()

    cat2 = _open(tmp_path)
    # 重启后：被拒资产仍是原结构、原版本，而不是"按拒绝内容生效"。
    snap = cat2.get_asset("orders")
    assert snap.schema_version == 1
    assert set(snap.fields) == {"id", "amount"}
    assert snap.fields["id"].spec.field_type is FieldType.LONG
    # "从未登记"依然明确是 not_found，而不是变成别的状态。
    with pytest.raises(AssetNotFoundError):
        cat2.get_asset("ghost")
    cat2.store.close()

    # 被拒写入不得在日志里留下任何记录（仅 1 条 created 事务）。
    log = (tmp_path / "events-1.log").read_text()
    txns = [json.loads(line)["txn"] for line in log.splitlines() if line.strip()]
    assert txns == [1]


# --------------------------------------------------------------------------
# 3. 写事务中途异常 -> 原子性
# --------------------------------------------------------------------------
def test_aborted_transaction_leaves_no_partial_state(tmp_path):
    store = PersistentStore(str(tmp_path))
    cat = MetadataCatalog(store)
    cat.register(RegisterRequest(_orders_spec()))

    # 直接在存储层事务里制造"先暂存资产 A，再追加血缘，随后异常"。
    asset_a = cat.get_asset("orders")
    from metadata_catalog.models import AssetSnapshot

    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction():
            asset_a.schema_version = 99
            asset_a.fields = {}
            store.put_asset(asset_a)
            store.add_edge(LineageEdge("orders", "orders"))
            raise RuntimeError("boom")

    # 内存态立即保持写入前完整状态。
    snap = cat.get_asset("orders")
    assert snap.schema_version == 1
    assert set(snap.fields) == {"id", "amount"}
    assert not store.has_edge("orders", "orders")
    store.close()

    # 重启后同样完整：残缺结构没有落盘，版本与内容对得上。
    cat2 = _open(tmp_path)
    snap2 = cat2.get_asset("orders")
    assert snap2.schema_version == 1
    assert set(snap2.fields) == {"id", "amount"}
    assert not cat2.store.has_edge("orders", "orders")
    cat2.store.close()


def test_torn_tail_after_kill_is_discarded(tmp_path):
    """模拟进程在 write 中途被杀：日志末尾是没有换行边界的半行。"""
    cat = _open(tmp_path)
    cat.register(RegisterRequest(_orders_spec()))
    cat.store.close()

    log = tmp_path / "events-1.log"
    committed = log.read_bytes()
    # 追加一条"写了一半"的记录（无结尾换行，内容也是残缺 JSON）。
    with open(log, "ab") as f:
        f.write(b'{"txn": 2, "ops": [{"op": "put_asset", "asset": {"name": "orders')
    cat2 = _open(tmp_path)
    snap = cat2.get_asset("orders")
    assert snap.schema_version == 1
    with pytest.raises(AssetNotFoundError):
        cat2.get_asset("half")
    # 半行应已被裁掉，文件只剩完整记录。
    assert log.read_bytes() == committed
    cat2.store.close()


def test_batch_lineage_is_atomic_and_ordered(tmp_path):
    cat = _open(tmp_path)
    for n in ("a", "b", "c"):
        cat.register(RegisterRequest(AssetSpec(n, AssetType.TABLE, [])))
    results = cat.add_lineage_batch(
        [
            LineageEdge("a", "b"),
            LineageEdge("b", "c"),
            LineageEdge("c", "a"),  # 成环，拒绝
            LineageEdge("a", "b"),  # 批次内重复，拒绝
        ]
    )
    codes = [r.code for r in results]
    assert codes == ["accepted", "accepted", "lineage_cycle", "duplicate_edge"]
    cat.store.close()

    cat2 = _open(tmp_path)
    assert cat2.add_lineage(LineageEdge("a", "b")).code == "duplicate_edge"
    assert cat2.add_lineage(LineageEdge("c", "a")).code == "lineage_cycle"
    cat2.store.close()


# --------------------------------------------------------------------------
# 4. 压缩 / 代际 / 格式版本
# --------------------------------------------------------------------------
def test_compaction_switch_and_recover(tmp_path, monkeypatch):
    import metadata_catalog.persistent_store as ps

    monkeypatch.setattr(ps, "COMPACT_ENTRY_THRESHOLD", 5)
    monkeypatch.setattr(ps, "COMPACT_BYTES_THRESHOLD", 1)

    store = PersistentStore(str(tmp_path))
    cat = MetadataCatalog(store)
    for i in range(12):
        cat.register(
            RegisterRequest(
                AssetSpec(
                    f"t{i}",
                    AssetType.TABLE,
                    [FieldSpec("id", FieldType.LONG)],
                    description=f"d{i}",
                )
            )
        )
    assert store._gen >= 2
    # 旧代文件应已被清理，只保留当前代。
    gens_events = sorted(
        int(p.stem.split("-")[1]) for p in tmp_path.glob("events-*.log")
    )
    gens_snaps = sorted(
        int(p.stem.split("-")[1]) for p in tmp_path.glob("snapshots/snap-*.json")
    )
    assert gens_events == [store._gen]
    assert gens_snaps == [store._gen]

    # 压缩后继续写入（新一代事件日志）并血缘。
    cat.register(
        RegisterRequest(
            AssetSpec(
                "t0",
                AssetType.TABLE,
                [FieldSpec("id", FieldType.LONG), FieldSpec("z", FieldType.STRING)],
                description="d0",
            ),
            expected_schema_version=1,
        )
    )
    cat.add_lineage(LineageEdge("t0", "t1"))
    store.close()

    cat2 = _open(tmp_path)
    assert len(cat2.list_assets()) == 12
    t0 = cat2.get_asset("t0")
    assert t0.schema_version == 2
    assert t0.fields["z"].since_version == 2
    assert t0.description == "d0"
    assert cat2.impact("t1").upstream_direct == ["t0"]
    cat2.store.close()


def test_incompatible_format_version_rejected(tmp_path):
    PersistentStore(str(tmp_path)).close()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"format_version": FORMAT_VERSION + 1, "gen": 1}))
    with pytest.raises(StoreCorruptionError):
        PersistentStore(str(tmp_path))


def test_corrupt_event_line_in_committed_region_rejected(tmp_path):
    cat = _open(tmp_path)
    cat.register(RegisterRequest(_orders_spec()))
    cat.store.close()
    log = tmp_path / "events-1.log"
    raw = log.read_bytes()
    # 把已提交区域中间破坏成非法 JSON（有完整换行边界，因此不会被当半行裁掉）。
    log.write_bytes(raw[:-1].rsplit(b"{", 1)[0] + b"not-json\n")
    with pytest.raises(StoreCorruptionError):
        _open(tmp_path)
