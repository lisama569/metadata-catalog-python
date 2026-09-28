"""FastAPI HTTP 适配层。

把 JSON 请求转换为领域模型，领域错误映射为结构化的 HTTP 响应，
错误响应统一带 ``code`` 字段，调用方可据此区分失败原因。

血缘写接口对单条/批量都返回 200 + 每条边的判定结论（accepted/code/reason），
因为"重复边/成环/资产不存在"属于需要明确标记、可预期处理的业务结论，
而不是服务端异常。
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .catalog import MetadataCatalog
from .errors import (
    AssetNotFoundError,
    CatalogError,
    IncompatibleSchemaError,
    StorageError,
    ValidationError,
    VersionConflictError,
)
from .models import (
    AssetSpec,
    AssetType,
    FieldSpec,
    FieldType,
    GovernanceUpdate,
    LineageEdge,
    RegisterRequest,
)
from .persistence import PersistentStore
from .store import LocalStore

#: 服务默认使用的本地仓库目录；可用环境变量覆盖。
DEFAULT_REPO_DIR = os.environ.get("METADATA_CATALOG_REPO", ".metadata_catalog_repo")

logger = logging.getLogger("metadata_catalog.api")

_HTTP_STATUS = {
    ValidationError.code: 400,
    IncompatibleSchemaError.code: 422,
    AssetNotFoundError.code: 404,
    VersionConflictError.code: 409,
    # StorageError 子类统一在异常处理器中映射为 500。
}


# ---------- 请求/响应 schema ----------
class FieldIn(BaseModel):
    name: str
    field_type: FieldType
    nullable: bool = True
    description: str = ""


class RegisterIn(BaseModel):
    name: str
    asset_type: AssetType
    fields: list[FieldIn] = Field(default_factory=list)
    description: str = ""
    renames: dict[str, str] = Field(default_factory=dict)
    expected_schema_version: int | None = None


class LineageEdgeIn(BaseModel):
    upstream: str
    downstream: str
    edge_type: str = "derived"


class GovernanceIn(BaseModel):
    expected_governance_version: int
    owner: str | None = None
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    replace_tags: bool = False


def _snapshot_dict(s) -> dict:
    return {
        "name": s.name,
        "asset_type": s.asset_type.value,
        "description": s.description,
        "schema_version": s.schema_version,
        "governance_version": s.governance_version,
        "owner": s.owner,
        "tags": sorted(s.tags),
        "fields": [
            {
                "name": name,
                "field_type": rec.spec.field_type.value,
                "nullable": rec.spec.nullable,
                "description": rec.spec.description,
                "since_version": rec.since_version,
            }
            for name, rec in sorted(s.fields.items())
        ],
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def create_app(
    catalog: MetadataCatalog | None = None,
    *,
    store=None,
    repo_dir: str | os.PathLike[str] | None = None,
) -> FastAPI:
    """构造 FastAPI 应用。

    存储解析顺序：

    1. 显式传入的 ``catalog``（测试最常用）；
    2. 显式传入的 ``store``（如 ``LocalStore()`` 或 ``PersistentStore(path)``）；
    3. 默认在本地 ``repo_dir``（环境变量 ``METADATA_CATALOG_REPO``，
       默认 ``.metadata_catalog_repo``）上打开持久化仓库——服务重启后
       数据仍在，多个进程可同时打开同一目录。
    """
    if catalog is None:
        if store is None:
            store = PersistentStore(repo_dir or DEFAULT_REPO_DIR)
        catalog = MetadataCatalog(store)
    app = FastAPI(
        title="本地元数据目录与血缘服务",
        version="0.1.0",
    )

    @app.exception_handler(CatalogError)
    async def _catalog_error_handler(_request, exc: CatalogError):
        body: dict = {"code": exc.code, "message": exc.message}
        if isinstance(exc, IncompatibleSchemaError):
            body["reasons"] = exc.reasons
        if isinstance(exc, VersionConflictError):
            body["expected_version"] = exc.expected_version
            body["actual_version"] = exc.actual_version
        # StorageError 及其子类（storage_corruption / unsupported_storage_format）
        # 都视为仓库侧故障，统一 500，与业务裁决类错误明确区分。
        if isinstance(exc, StorageError):
            status = 500
        else:
            status = _HTTP_STATUS.get(exc.code, 400)
        return JSONResponse(status_code=status, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ):
        # 请求体/查询参数不合法：归类为 validation_error(400)，与领域层的
        # ValidationError 同码，调用方按 code 即可区分。
        return JSONResponse(
            status_code=400,
            content={
                "code": "validation_error",
                "message": "请求参数不合法",
                "reasons": [
                    ".".join(str(p) for p in err.get("loc", []))
                    + ": "
                    + str(err.get("msg", ""))
                    for err in exc.errors()
                ],
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected_error_handler(_request: Request, exc: Exception):
        # 最后防线：任何未被归类的异常也必须是带 code 的结构化 500，
        # 而不是没有业务含义的裸服务端错误。细节进日志，不回显给调用方。
        logger.exception("未归类的服务端异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "服务端发生未归类内部错误，请查看服务日志",
            },
        )

    @app.get("/")
    def read_root():
        return {"service": "metadata-catalog", "status": "ok"}

    @app.post("/assets/register")
    def register_assets(payload: RegisterIn):
        spec = AssetSpec(
            name=payload.name,
            asset_type=payload.asset_type,
            description=payload.description,
            fields=[
                FieldSpec(
                    name=f.name,
                    field_type=f.field_type,
                    nullable=f.nullable,
                    description=f.description,
                )
                for f in payload.fields
            ],
        )
        outcome = catalog.register(
            RegisterRequest(
                spec=spec,
                renames=payload.renames,
                expected_schema_version=payload.expected_schema_version,
            )
        )
        return {
            "asset": outcome.asset,
            "action": outcome.action,
            "schema_version": outcome.schema_version,
            "changed_fields": outcome.changed_fields,
            "reason": outcome.reason,
        }

    @app.get("/assets/{name}")
    def get_asset(name: str):
        return _snapshot_dict(catalog.get_asset(name))

    @app.get("/assets")
    def list_assets():
        return {"assets": [_snapshot_dict(s) for s in catalog.list_assets()]}

    @app.post("/lineage")
    def add_lineage(payload: LineageEdgeIn):
        result = catalog.add_lineage(
            LineageEdge(
                upstream=payload.upstream,
                downstream=payload.downstream,
                edge_type=payload.edge_type,
            )
        )
        return {
            "edge": {
                "upstream": result.edge.upstream,
                "downstream": result.edge.downstream,
            },
            "accepted": result.accepted,
            "code": result.code,
            "reason": result.reason,
        }

    @app.post("/lineage/batch")
    def add_lineage_batch(payload: list[LineageEdgeIn]):
        results = catalog.add_lineage_batch(
            [
                LineageEdge(
                    upstream=p.upstream,
                    downstream=p.downstream,
                    edge_type=p.edge_type,
                )
                for p in payload
            ]
        )
        return {
            "results": [
                {
                    "edge": {
                        "upstream": r.edge.upstream,
                        "downstream": r.edge.downstream,
                    },
                    "accepted": r.accepted,
                    "code": r.code,
                    "reason": r.reason,
                }
                for r in results
            ]
        }

    @app.get("/lineage/impact/{asset}")
    def get_impact(asset: str):
        r = catalog.impact(asset)
        return {
            "asset": r.asset,
            "upstream_direct": r.upstream_direct,
            "downstream_direct": r.downstream_direct,
            "upstream_transitive": r.upstream_transitive,
            "downstream_transitive": r.downstream_transitive,
        }

    @app.put("/assets/{name}/governance")
    def update_governance(name: str, payload: GovernanceIn):
        outcome = catalog.update_governance(
            GovernanceUpdate(
                asset=name,
                expected_governance_version=payload.expected_governance_version,
                owner=payload.owner,
                add_tags=frozenset(payload.add_tags),
                remove_tags=frozenset(payload.remove_tags),
                replace_tags=payload.replace_tags,
            )
        )
        return {
            "asset": outcome.asset,
            "governance_version": outcome.governance_version,
            "owner": outcome.owner,
            "tags": outcome.tags,
            "changed": outcome.changed,
            "reason": outcome.reason,
        }

    return app
