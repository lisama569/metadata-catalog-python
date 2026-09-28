"""元数据目录的数据模型（类型定义层）。

模型全部使用 ``dataclass`` 表达，存储层与服务层基于这些类型工作。
时间统一使用时区感知的 UTC 时间戳。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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


@dataclass
class FieldRecord:
    """已登记字段在仓库中的记录（携带首次登记版本）。"""

    spec: FieldSpec
    since_version: int = 1


@dataclass
class AssetSnapshot:
    """资产在仓库中的当前完整快照。"""

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

    def copy(self) -> "AssetSnapshot":
        """深拷贝快照（dict/set 都是可变容器，必须独立复制）。

        存储层在返回快照给调用方时统一走这里，保证调用方拿到的对象与
        仓库内部持有的对象互不共享可变状态。
        """
        return AssetSnapshot(
            name=self.name,
            asset_type=self.asset_type,
            description=self.description,
            schema_version=self.schema_version,
            fields=dict(self.fields),
            owner=self.owner,
            tags=set(self.tags),
            governance_version=self.governance_version,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def with_updates(self, **changes) -> "AssetSnapshot":
        """基于当前快照构造一份新快照（写时复制）。

        ``fields`` / ``tags`` 以外的字段按关键字覆盖；fields、tags 即便
        内容相同也复制成新容器。任何写入路径都必须通过本方法得到"下一
        状态"，不允许原地修改当前快照——这样写入失败（甚至尚未落盘）时，
        仓库内与读者持有的旧快照都不会被污染。
        """
        data = {
            "name": self.name,
            "asset_type": self.asset_type,
            "description": self.description,
            "schema_version": self.schema_version,
            "fields": dict(self.fields),
            "owner": self.owner,
            "tags": set(self.tags),
            "governance_version": self.governance_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        data.update(changes)
        return AssetSnapshot(**data)


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
