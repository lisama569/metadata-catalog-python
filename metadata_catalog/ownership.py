"""标签与负责人的乐观并发更新。

语义
----
- ``tags`` / ``owners`` 为集合, 给出即表示“最终集合”(整体替换该维度);
  置为 None 表示该维度本次不变。
- 必须携带 ``expected_meta_version``(调用方上次读到的 meta_version):
  与当前值不一致即抛 VersionConflictError, 拒绝写入, 由调用方重试/放弃,
  从而在并发修改下不丢更新。
- 资产不存在抛 AssetNotFoundError。
- 集合内容相同则 changed=False 且不提升版本。
"""

from __future__ import annotations

import logging

from .errors import AssetNotFoundError, VersionConflictError
from .models import Asset, OwnershipResult, OwnershipUpdate

logger = logging.getLogger("metadata_catalog.ownership")


def update_ownership(data, update: OwnershipUpdate, *, now: float) -> OwnershipResult:
    logger.info(
        "update_ownership 输入 asset_id=%s tags=%s owners=%s expected_meta_version=%s",
        update.asset_id,
        sorted(update.tags) if update.tags is not None else None,
        sorted(update.owners) if update.owners is not None else None,
        update.expected_meta_version,
    )

    asset = data.assets.get(update.asset_id)
    if asset is None:
        logger.warning("更新被拒绝 asset_id=%s 判定=ASSET_NOT_FOUND", update.asset_id)
        raise AssetNotFoundError(update.asset_id, context="更新标签/负责人")

    if update.expected_meta_version is None:
        logger.warning(
            "更新被拒绝 asset_id=%s 判定=VERSION_CONFLICT 依据=缺少 expected_meta_version, 当前=%s",
            update.asset_id, asset.meta_version,
        )
        raise VersionConflictError(
            update.asset_id,
            expected=None,
            actual=asset.meta_version,
            scope="meta",
        )
    if update.expected_meta_version != asset.meta_version:
        logger.warning(
            "更新被拒绝 asset_id=%s 判定=VERSION_CONFLICT 依据=版本冲突 期望=%s 实际=%s",
            update.asset_id, update.expected_meta_version, asset.meta_version,
        )
        raise VersionConflictError(
            update.asset_id,
            expected=update.expected_meta_version,
            actual=asset.meta_version,
            scope="meta",
        )

    new_tags = (
        set(update.tags) if update.tags is not None else set(asset.tags)
    )
    new_owners = (
        set(update.owners) if update.owners is not None else set(asset.owners)
    )

    changed = new_tags != asset.tags or new_owners != asset.owners
    if not changed:
        logger.info(
            "判定 无变化 asset_id=%s meta_version=%s",
            update.asset_id,
            asset.meta_version,
        )
        return OwnershipResult(
            asset=asset,
            meta_version=asset.meta_version,
            changed=False,
            reason="标签与负责人集合均无变化, 幂等返回, 不提升版本",
        )

    evolved: Asset = asset.model_copy(
        update={
            "tags": new_tags,
            "owners": new_owners,
            "meta_version": asset.meta_version + 1,
            "updated_at": now,
        }
    )
    data.assets[update.asset_id] = evolved
    logger.info(
        "判定 已更新 asset_id=%s meta_version %s -> %s tags=%s owners=%s",
        update.asset_id,
        asset.meta_version,
        evolved.meta_version,
        sorted(new_tags),
        sorted(new_owners),
    )
    return OwnershipResult(
        asset=evolved,
        meta_version=evolved.meta_version,
        changed=True,
        reason=(
            f"校验期望版本 {update.expected_meta_version} 与当前一致后写入, "
            f"meta_version {asset.meta_version} -> {evolved.meta_version}"
        ),
    )
