"""元数据目录的数据模型（类型定义层）。

模型全部使用 ``dataclass`` 表达，存储层与服务层基于这些类型工作。
时间统一使用时区感知的 UTC 时间戳。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum


class FieldType(str, Enum):
    """字段物理/逻辑类型。类型之间默认不兼容，不允许隐式变更。"""

    STRING = "string"
    INTEGER = "integer"
    LONG = "long"
    DOUBLE = "double"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    BINARY = "binary"


class AssetType(str, Enum):
    """资产种类。"""

    TABLE = "table"
    TOPIC = "topic"
    DASHBOARD = "dashboard"
    PIPELINE = "pipeline"


@dataclass(frozen=True)
class FieldSpec:
    """单个字段的结构声明。"""

    name: str
    field_type: FieldType
    nullable: bool = True
    description: str = ""


@dataclass(frozen=True)
class AssetSpec:
    """资产登记时提交的结构声明。"""

    name: str
    asset_type: AssetType
    fields: list[FieldSpec] = field(default_factory=list)
    description: str = ""


@dataclass(frozen=True)
class RegisterRequest:
    """登记/演进请求。

    :param renames: 显式声明的重命名映射 ``{旧字段名: 新字段名}``，
        服务据此把"删除+新增"识别为重命名。
    :param expected_schema_version: 可选的乐观版本；调用方基于某个已见
        结构版本登记时传入，服务发现版本过期则拒绝。
    """

    spec: AssetSpec
    renames: dict[str, str] = field(default_factory=dict)
    expected_schema_version: int | None = None


@dataclass(frozen=True)
class FieldRecord:
    """已登记字段在仓库中的记录（携带首次登记版本）。

    不可变：结构演进时整体构造新记录，绝不就地改写。
    """

    spec: FieldSpec
    since_version: int = 1


@dataclass(frozen=True)
class AssetSnapshot:
    """资产在仓库中的当前完整快照。

    不可变：任何更新都通过 :func:`dataclasses.replace` 生成并整体发布新
    快照。这样"写入中途失败"不会把半成品留在仓库缓存里，也避免不同线程
    同时看到一个正在被改写的对象。``tags``/``fields`` 均为新构造的容器，
    发布后不再被任何一方修改。
    """

    name: str
    asset_type: AssetType
    description: str
    schema_version: int
    fields: dict[str, FieldRecord]
    owner: str | None
    tags: set[str]
    governance_version: int
    created_at: float
    updated_at: float

    def field_specs(self) -> dict[str, FieldSpec]:
        return {name: record.spec for name, record in self.fields.items()}


@dataclass(frozen=True)
class LineageEdge:
    """血缘边：``upstream`` 产出数据流入 ``downstream``。"""

    upstream: str
    downstream: str
    edge_type: str = "derived"

    def key(self) -> tuple[str, str]:
        return self.upstream, self.downstream


@dataclass(frozen=True)
class ImpactResult:
    """某资产的直接与传递影响范围。"""

    asset: str
    upstream_direct: list[str]
    downstream_direct: list[str]
    upstream_transitive: list[str]
    downstream_transitive: list[str]


@dataclass(frozen=True)
class EdgeCheckResult:
    """单条血缘边的校验结论（供批量校验/登记使用）。"""

    edge: LineageEdge
    accepted: bool
    code: str
    reason: str


@dataclass(frozen=True)
class GovernanceUpdate:
    """标签/负责人更新请求（携带期望的治理版本做乐观锁）。"""

    asset: str
    expected_governance_version: int
    owner: str | None = None
    add_tags: frozenset[str] = frozenset()
    remove_tags: frozenset[str] = frozenset()
    replace_tags: bool = False


@dataclass(frozen=True)
class RegisterOutcome:
    """登记操作的结论，明确区分"新建/幂等重复/演进"。"""

    asset: str
    action: str  # created | re_registered_idempotent | evolved
    schema_version: int
    changed_fields: dict[str, str]  # 字段名 -> added|removed|renamed_from:xxx|type_changed...
    reason: str


@dataclass(frozen=True)
class GovernanceOutcome:
    """治理信息更新结论。"""

    asset: str
    governance_version: int
    owner: str | None
    tags: list[str]
    changed: bool
    reason: str


def now_ts() -> float:
    """当前 UTC 时间戳（秒）。"""
    return time.time()
