"""门面服务: 组合存储与各子域, 对外暴露统一入口。

所有写操作都包裹在单个 store 事务中, 保证“校验 + 生效 + 落盘”原子完成,
因此并发登记 / 并发血缘写入 / 并发元数据更新不会出现半截状态或丢更新。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from . import lineage as lineage_domain
from . import ownership as ownership_domain
from . import schema as schema_domain
from .errors import AssetNotFoundError
from .models import (
    Asset,
    AssetRegistration,
    ImpactReport,
    LineageWriteResult,
    OwnershipResult,
    OwnershipUpdate,
    RegistrationResult,
)
from .store import CatalogData, LocalStore


class MetadataCatalogService:
    def __init__(self, store: LocalStore, *, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock

    # ---- 资产 / 结构 ---------------------------------------------------
    def register_asset(self, req: AssetRegistration) -> RegistrationResult:
        now = self._clock()
        return self._store.transaction(
            lambda data: schema_domain.register_asset(data, req, now=now)
        )

    def get_asset(self, asset_id: str) -> Asset:
        def _get(data: CatalogData) -> Asset:
            asset = data.assets.get(asset_id)
            if asset is None:
                raise AssetNotFoundError(asset_id, context="读取资产")
            return asset

        return self._store.transaction(_get)

    def list_assets(self) -> list[Asset]:
        return self._store.transaction(
            lambda data: [data.assets[k] for k in sorted(data.assets)]
        )

    # ---- 血缘 ----------------------------------------------------------
    def add_lineage(self, *, upstream: str, downstream: str) -> LineageWriteResult:
        return self._store.transaction(
            lambda data: lineage_domain.add_edge(
                data, upstream=upstream, downstream=downstream
            )
        )

    def impact(
        self, *, asset_id: str, direction: str = "downstream"
    ) -> ImpactReport:
        return self._store.transaction(
            lambda data: lineage_domain.impact(
                data, asset_id=asset_id, direction=direction
            )
        )

    # ---- 标签 / 负责人 -------------------------------------------------
    def update_ownership(self, update: OwnershipUpdate) -> OwnershipResult:
        now = self._clock()
        return self._store.transaction(
            lambda data: ownership_domain.update_ownership(data, update, now=now)
        )
