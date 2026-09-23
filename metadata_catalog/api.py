"""FastAPI HTTP 层: 仅做协议适配, 业务规则全部在 service 层。

领域异常 -> HTTP 状态码 / 错误码映射
------------------------------------
AssetNotFoundError             404 ASSET_NOT_FOUND
AssetAlreadyExistsError        409 ASSET_ALREADY_EXISTS
SchemaIncompatibleError        422 SCHEMA_INCOMPATIBLE (details.reason 细分)
VersionConflictError           409 VERSION_CONFLICT
LineageEndpointMissingError    422 LINEAGE_ENDPOINT_MISSING
LineageCycleError              422 LINEAGE_CYCLE (details.cycle 给出环路径)
InvalidArgumentError           400 INVALID_ARGUMENT
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import (
    AssetAlreadyExistsError,
    AssetNotFoundError,
    CatalogError,
    ErrorCode,
    InvalidArgumentError,
    LineageCycleError,
    LineageEndpointMissingError,
    SchemaIncompatibleError,
    VersionConflictError,
)
from .models import AssetRegistration, OwnershipUpdate
from .service import MetadataCatalogService
from .store import LocalStore

logger = logging.getLogger("metadata_catalog.api")

_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.ASSET_NOT_FOUND: 404,
    ErrorCode.ASSET_ALREADY_EXISTS: 409,
    ErrorCode.SCHEMA_INCOMPATIBLE: 422,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.LINEAGE_ENDPOINT_MISSING: 422,
    ErrorCode.LINEAGE_CYCLE: 422,
    ErrorCode.INVALID_ARGUMENT: 400,
}


def create_app(
    store: LocalStore | None = None,
    *,
    service: MetadataCatalogService | None = None,
) -> FastAPI:
    """构造应用。测试可注入内存/临时目录的 store 或直接注入 service。"""
    app = FastAPI(title="本地元数据目录与血缘服务", version="0.1.0")

    if service is None:
        service = MetadataCatalogService(store or LocalStore())
    app.state.service = service

    @app.exception_handler(CatalogError)
    async def _catalog_error_handler(request: Request, exc: CatalogError) -> JSONResponse:
        logger.warning("请求被拒绝 path=%s %s", request.url.path, exc.to_dict())
        status = _STATUS_MAP.get(exc.error_code, 400)
        return JSONResponse(status_code=status, content=exc.to_dict())

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"service": "metadata-catalog", "status": "ok"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---- 资产 / 结构 ---------------------------------------------------
    @app.post("/assets")
    def register_asset(req: AssetRegistration) -> dict[str, Any]:
        result = service.register_asset(req)
        return result.model_dump(mode="json")

    @app.get("/assets")
    def list_assets() -> dict[str, Any]:
        assets = service.list_assets()
        return {"assets": [a.model_dump(mode="json") for a in assets]}

    @app.get("/assets/{asset_id}")
    def get_asset(asset_id: str) -> dict[str, Any]:
        return service.get_asset(asset_id).model_dump(mode="json")

    @app.put("/assets/{asset_id}/ownership")
    def update_ownership(asset_id: str, update: OwnershipUpdate) -> dict[str, Any]:
        # 路径优先, 防止 body 与路径不一致产生歧义。
        payload = update.model_copy(update={"asset_id": asset_id})
        return service.update_ownership(payload).model_dump(mode="json")

    # ---- 血缘 ----------------------------------------------------------
    @app.post("/lineage/edges")
    def add_edge(body: dict[str, Any]) -> dict[str, Any]:
        upstream = str(body.get("upstream", "")).strip()
        downstream = str(body.get("downstream", "")).strip()
        result = service.add_lineage(upstream=upstream, downstream=downstream)
        return result.model_dump(mode="json")

    @app.get("/assets/{asset_id}/impact")
    def impact(asset_id: str, direction: str = "downstream") -> dict[str, Any]:
        return service.impact(
            asset_id=asset_id, direction=direction
        ).model_dump(mode="json")

    return app
