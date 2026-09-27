"""元数据目录领域错误类型。

所有错误都携带 ``code``（机器可判定的拒绝原因）与人类可读的 ``message``，
调用方可据此区分不同的失败原因，而无需解析错误文本。
"""

from __future__ import annotations


class CatalogError(Exception):
    """所有元数据目录错误的基类。"""

    code: str = "catalog_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def __str__(self) -> str:  # pragma: no cover - 简单格式化
        return f"[{self.code}] {self.message}"


class ValidationError(CatalogError):
    """输入不满足基本约束（空名、字段缺失、类型非法等）。"""

    code = "validation_error"


class IncompatibleSchemaError(CatalogError):
    """结构演进不兼容（如字段类型变化、重命名冲突）。

    :param reasons: 逐条不兼容原因描述，便于调用方定位。
    """

    code = "incompatible_schema"

    def __init__(self, message: str, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons: list[str] = reasons or []


class AssetNotFoundError(CatalogError):
    """引用了不存在的资产。"""

    code = "asset_not_found"


class LineageCycleError(CatalogError):
    """新增血缘边会导致成环。"""

    code = "lineage_cycle"

    def __init__(self, message: str, cycle: list[str] | None = None) -> None:
        super().__init__(message)
        self.cycle: list[str] = cycle or []


class DuplicateEdgeError(CatalogError):
    """血缘边重复（同一对上下游已存在）。"""

    code = "duplicate_edge"


class VersionConflictError(CatalogError):
    """携带过期版本的并发写入，乐观锁冲突。"""

    code = "version_conflict"

    def __init__(self, message: str, *, expected: int, actual: int) -> None:
        super().__init__(message)
        self.expected_version = expected
        self.actual_version = actual


class StorageError(CatalogError):
    """本地持久化仓库自身的故障（IO 错误、数据文件损坏、格式不受支持等）。

    与"业务上被拒绝"（``IncompatibleSchemaError`` / ``VersionConflictError``）
    严格区分：存储故障意味着仓库状态可能不可用，而不是一次有据可依的裁决。
    """

    code = "storage_error"


class StorageCorruptionError(StorageError):
    """仓库中的数据文件无法解析，磁盘内容已损坏。"""

    code = "storage_corruption"


class UnsupportedStorageFormatError(StorageError):
    """数据文件来自更新/更旧的仓库格式版本，当前进程无法安全读写。"""

    code = "unsupported_storage_format"

    def __init__(self, message: str, *, version: int, supported: int) -> None:
        super().__init__(message)
        self.format_version = version
        self.supported_version = supported
