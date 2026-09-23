"""领域异常定义。

所有异常均携带可被调用方程序化区分的结构化信息, HTTP 层据此映射为
不同的状态码与错误码, 而不是统一返回模糊的 400。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """稳定的错误码, 调用方可据此区分失败原因。"""

    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    ASSET_ALREADY_EXISTS = "ASSET_ALREADY_EXISTS"
    SCHEMA_INCOMPATIBLE = "SCHEMA_INCOMPATIBLE"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    LINEAGE_ENDPOINT_MISSING = "LINEAGE_ENDPOINT_MISSING"
    LINEAGE_CYCLE = "LINEAGE_CYCLE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"


class CatalogError(Exception):
    """所有领域异常的基类。"""

    error_code: ErrorCode = ErrorCode.INVALID_ARGUMENT

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict = details or {}

    def to_dict(self) -> dict:
        return {
            "error_code": self.error_code.value,
            "message": self.message,
            "details": self.details,
        }


class AssetNotFoundError(CatalogError):
    """引用的资产不存在。"""

    error_code = ErrorCode.ASSET_NOT_FOUND

    def __init__(self, asset_id: str, *, context: str = "") -> None:
        super().__init__(
            f"资产不存在: {asset_id}" + (f" ({context})" if context else ""),
            details={"asset_id": asset_id, "context": context},
        )
        self.asset_id = asset_id


class AssetAlreadyExistsError(CatalogError):
    """以“仅新建”语义登记一个已存在的资产。"""

    error_code = ErrorCode.ASSET_ALREADY_EXISTS

    def __init__(self, asset_id: str) -> None:
        super().__init__(f"资产已存在: {asset_id}", details={"asset_id": asset_id})
        self.asset_id = asset_id


class IncompatibleReason(str, Enum):
    """结构不兼容的具体、可区分原因。"""

    FIELD_TYPE_CHANGED = "FIELD_TYPE_CHANGED"
    FIELD_REMOVED_WITHOUT_TOMBSTONE = "FIELD_REMOVED_WITHOUT_TOMBSTONE"
    FIELD_MISSING_RENAME = "FIELD_MISSING_RENAME"
    RENAME_TARGET_CONFLICT = "RENAME_TARGET_CONFLICT"
    RENAME_SOURCE_NOT_FOUND = "RENAME_SOURCE_NOT_FOUND"
    RENAME_TYPE_CHANGED = "RENAME_TYPE_CHANGED"
    DUPLICATE_FIELD_IN_PAYLOAD = "DUPLICATE_FIELD_IN_PAYLOAD"
    NAME_COLLISION = "NAME_COLLISION"


class SchemaIncompatibleError(CatalogError):
    """结构演进不兼容, 必须拒绝, 不得静默覆盖。"""

    error_code = ErrorCode.SCHEMA_INCOMPATIBLE

    def __init__(
        self,
        reason: IncompatibleReason,
        message: str,
        *,
        fields: list[str] | None = None,
    ) -> None:
        super().__init__(
            message,
            details={"reason": reason.value, "fields": fields or []},
        )
        self.reason = reason
        self.fields: list[str] = fields or []


class LineageEndpointMissingError(CatalogError):
    """血缘边指向尚不存在的资产。"""

    error_code = ErrorCode.LINEAGE_ENDPOINT_MISSING

    def __init__(self, *, upstream: str, downstream: str, missing: list[str]) -> None:
        super().__init__(
            f"血缘端点不存在: {missing}",
            details={
                "upstream": upstream,
                "downstream": downstream,
                "missing": missing,
            },
        )
        self.upstream = upstream
        self.downstream = downstream
        self.missing = missing


class LineageCycleError(CatalogError):
    """写入该边会导致血缘成环。"""

    error_code = ErrorCode.LINEAGE_CYCLE

    def __init__(self, *, upstream: str, downstream: str, cycle: list[str]) -> None:
        super().__init__(
            f"血缘成环: {' -> '.join(cycle)}",
            details={
                "upstream": upstream,
                "downstream": downstream,
                "cycle": cycle,
            },
        )
        self.upstream = upstream
        self.downstream = downstream
        self.cycle = cycle


class VersionConflictError(CatalogError):
    """携带过期版本的并发写入, 必须由调用方显式处理。"""

    error_code = ErrorCode.VERSION_CONFLICT

    def __init__(
        self,
        asset_id: str,
        *,
        expected: int | None,
        actual: int,
        scope: str,
    ) -> None:
        super().__init__(
            f"版本冲突: 资产 {asset_id} 期望 {scope} 版本 {expected}, 实际 {actual}",
            details={
                "asset_id": asset_id,
                "expected_version": expected,
                "actual_version": actual,
                "scope": scope,
            },
        )
        self.asset_id = asset_id
        self.expected_version = expected
        self.actual_version = actual
        self.scope = scope


class InvalidArgumentError(CatalogError):
    """明显非法的输入。"""

    error_code = ErrorCode.INVALID_ARGUMENT
