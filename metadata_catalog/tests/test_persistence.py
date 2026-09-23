"""持久化与跨进程并发测试。"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from metadata_catalog.errors import SchemaIncompatibleError
from metadata_catalog.models import (
    AssetRegistration,
    FieldDef,
    OwnershipUpdate,
    RegistrationOutcome,
)
from metadata_catalog.service import MetadataCatalogService
from metadata_catalog.store import LocalStore


def test_state_survives_reload(tmp_path: Path):
    path = tmp_path / "c.json"
    svc1 = MetadataCatalogService(LocalStore(path))
    svc1.register_asset(
        AssetRegistration(asset_id="a", fields=[FieldDef(name="id", data_type="int")])
    )
    # 重新打开同一文件: 数据完整恢复
    svc2 = MetadataCatalogService(LocalStore(path))
    asset = svc2.get_asset("a")
    assert asset.fields[0].name == "id"
    assert asset.schema_version == 1


def test_failed_transaction_does_not_persist(tmp_path: Path):
    store = LocalStore(tmp_path / "c.json")
    svc = MetadataCatalogService(store)
    svc.register_asset(
        AssetRegistration(asset_id="a", fields=[FieldDef(name="id", data_type="int")])
    )
    with pytest.raises(SchemaIncompatibleError):
        svc.register_asset(
            AssetRegistration(asset_id="a", fields=[FieldDef(name="id", data_type="bigint")])
        )
    # 用全新 store 实例从磁盘重读, 确认被拒绝的演进没有落盘
    reloaded = MetadataCatalogService(LocalStore(tmp_path / "c.json")).get_asset("a")
    assert reloaded.fields[0].data_type == "int"
    assert reloaded.schema_version == 1


def test_lineage_roundtrip(tmp_path: Path):
    path = tmp_path / "c.json"
    svc = MetadataCatalogService(LocalStore(path))
    svc.register_asset(AssetRegistration(asset_id="a"))
    svc.register_asset(AssetRegistration(asset_id="b"))
    svc.add_lineage(upstream="a", downstream="b")
    svc2 = MetadataCatalogService(LocalStore(path))
    rep = svc2.impact(asset_id="a")
    assert rep.direct == ["b"]
    # 重复边在重载后仍被判为重复
    dup = svc2.add_lineage(upstream="a", downstream="b")
    assert dup.status.value == "DUPLICATE_EDGE"


def _concurrent_register_worker(path: str, idx: int) -> None:
    """每个进程新建一个全新的 store 实例(模拟各自独立的服务进程)。"""
    svc = MetadataCatalogService(LocalStore(path))
    svc.register_asset(AssetRegistration(asset_id=f"p{idx}", fields=[]))


def test_multiprocess_writes_are_not_lost(tmp_path: Path):
    path = str(tmp_path / "c.json")
    # 先建一个初始资产
    MetadataCatalogService(LocalStore(path)).register_asset(
        AssetRegistration(asset_id="seed")
    )
    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_concurrent_register_worker, args=(path, i))
        for i in range(8)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    assert all(p.exitcode == 0 for p in procs)

    assets = MetadataCatalogService(LocalStore(path)).list_assets()
    ids = {a.asset_id for a in assets}
    assert ids == {"seed", *(f"p{i}" for i in range(8))}


def _ownership_worker(path: str, tag: str, q) -> None:
    svc = MetadataCatalogService(LocalStore(path))
    try:
        res = svc.update_ownership(
            OwnershipUpdate(asset_id="a", tags={tag}, expected_meta_version=0)
        )
        q.put(("ok", res.meta_version))
    except Exception as exc:  # noqa: BLE001
        q.put((type(exc).__name__, 0))


def test_multiprocess_ownership_cas(tmp_path: Path):
    path = str(tmp_path / "c.json")
    MetadataCatalogService(LocalStore(path)).register_asset(
        AssetRegistration(asset_id="a")
    )
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=_ownership_worker, args=(path, f"tag{i}", q))
        for i in range(4)
    ]
    for p in procs:
        p.start()
    outcomes = [q.get() for _ in procs]
    for p in procs:
        p.join()

    oks = [o for o in outcomes if o[0] == "ok"]
    conflicts = [o for o in outcomes if o[0] == "VersionConflictError"]
    assert len(oks) == 1 and len(conflicts) == 3
    assert MetadataCatalogService(LocalStore(path)).get_asset("a").meta_version == 1
