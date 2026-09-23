"""标签 / 负责人更新与乐观并发控制测试。"""

from __future__ import annotations

import threading

import pytest

from metadata_catalog.errors import AssetNotFoundError, VersionConflictError
from metadata_catalog.models import AssetRegistration, OwnershipUpdate

from .conftest import assert_log_contains


def _create(service, asset_id="a"):
    service.register_asset(AssetRegistration(asset_id=asset_id, fields=[]))


def _up(asset_id="a", *, tags=None, owners=None, expected=None) -> OwnershipUpdate:
    return OwnershipUpdate(
        asset_id=asset_id, tags=tags, owners=owners, expected_meta_version=expected
    )


def test_initial_update_requires_version_zero(service, caplog):
    _create(service)
    res = service.update_ownership(_up(tags={"pii"}, owners={"alice"}, expected=0))
    assert res.changed is True
    assert res.meta_version == 1
    assert set(res.asset.tags) == {"pii"}
    assert_log_contains(caplog, "update_ownership 输入", "判定 已更新")


def test_noop_update_is_idempotent(service):
    _create(service)
    service.update_ownership(_up(tags={"pii"}, expected=0))  # v1
    res = service.update_ownership(_up(tags={"pii"}, expected=1))
    assert res.changed is False
    assert res.meta_version == 1


def test_stale_version_rejected_and_distinguishable(service, caplog):
    _create(service)
    service.update_ownership(_up(tags={"first"}, expected=0))  # v1
    with pytest.raises(VersionConflictError) as exc:
        # 第二个写者仍持有过期的 v0
        service.update_ownership(_up(tags={"second"}, expected=0))
    err = exc.value
    assert err.error_code.value == "VERSION_CONFLICT"
    assert err.expected_version == 0
    assert err.actual_version == 1
    assert err.scope == "meta"
    assert_log_contains(caplog, "版本冲突")


def test_missing_asset_rejected(service):
    with pytest.raises(AssetNotFoundError):
        service.update_ownership(_up(asset_id="ghost", tags={"x"}, expected=0))


def test_partial_update_preserves_other_dimension(service):
    _create(service)
    service.update_ownership(_up(tags={"pii"}, owners={"alice"}, expected=0))  # v1
    # 只改 owners, tags=None 表示该维度不变
    res = service.update_ownership(_up(owners={"alice", "bob"}, expected=1))
    assert res.meta_version == 2
    assert set(res.asset.tags) == {"pii"}
    assert set(res.asset.owners) == {"alice", "bob"}


def test_concurrent_updates_one_wins_no_lost_update(service):
    """两个写者都基于 v0 并发写: 一个成功, 另一个必须被识别为冲突而非覆盖。"""
    _create(service)
    results: dict[str, object] = {}

    def writer(name: str, tag: str) -> None:
        try:
            res = service.update_ownership(_up(tags={tag}, expected=0))
            results[name] = ("ok", res.meta_version)
        except VersionConflictError as exc:
            results[name] = ("conflict", exc.actual_version)

    t1 = threading.Thread(target=writer, args=("w1", "tag-1"))
    t2 = threading.Thread(target=writer, args=("w2", "tag-2"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    statuses = {v[0] for v in results.values()}
    assert statuses == {"ok", "conflict"}  # 一成一冲突, 而不是都成功导致丢更新

    final = service.get_asset("a")
    assert final.meta_version == 1  # 只有一次写入生效
    assert final.tags in ({"tag-1"}, {"tag-2"})


def test_retry_after_conflict_succeeds(service):
    """冲突方重新读取最新版本后重试, 应当成功(可恢复的并发控制)。"""
    _create(service)
    service.update_ownership(_up(tags={"first"}, expected=0))  # v1
    # 持 v0 的过期写失败
    with pytest.raises(VersionConflictError):
        service.update_ownership(_up(tags={"second"}, expected=0))
    # 重新读到 v1 后基于最新版本合并重试
    current = service.get_asset("a")
    merged_tags = current.tags | {"second"}
    res = service.update_ownership(_up(tags=merged_tags, expected=current.meta_version))
    assert res.changed is True
    assert res.meta_version == 2
    assert set(res.asset.tags) == {"first", "second"}
