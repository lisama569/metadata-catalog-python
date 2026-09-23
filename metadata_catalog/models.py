"""领域数据模型。

版本说明
--------
每个资产维护两个单调递增、互不相关的版本号:
- ``schema_version``: 字段结构每次被演进时 +1;
- ``meta_version``: 标签 / 负责人等元数据每次被更新时 +1。
两者都用于乐观并发控制: 调用方持有的版本过期时写入必须被拒绝。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .errors import IncompatibleReason  # noqa: F401  (类型再导出, 方便调用方 import)


class FieldDef(BaseModel):
    """一个字段的定义。

    ``uid`` 是字段在重命名过程中保持稳定的身份标识; ``name`` 是当前名称。
    首次登记时 uid 与 name 相同; 重命名只改 name, 从而可在演进中识别同一字段。
    """

    uid: str = ""
    name: str = Field(min_length=1)
    data_type: str
    nullable: bool = True
    description: str = ""


class RenameOp(BaseModel):
    """一次结构上报中声明的字段重命名: ``from_name`` -> ``to_name``。"""

    from_name: str
    to_name: str


class AssetRegistration(BaseModel):
    """登记 / 重复上报一个数据资产时的输入。"""

    asset_id: str = Field(min_length=1)
    name: str = ""
    asset_type: str = "table"
    fields: list[FieldDef] = Field(default_factory=list)
    renames: list[RenameOp] = Field(default_factory=list)
    # 调用方上次读到的结构版本; None 表示“仅当资产不存在时新建”。
    expected_schema_version: int | None = None


class Asset(BaseModel):
    """资产的完整持久化状态。"""

    asset_id: str
    name: str = ""
    asset_type: str = "table"
    fields: list[FieldDef] = Field(default_factory=list)
    tags: set[str] = Field(default_factory=set)
    owners: set[str] = Field(default_factory=set)
    schema_version: int = 0
    meta_version: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0


class RegistrationOutcome(str, Enum):
    """一次登记的判定结果(可解释)。"""

    CREATED = "CREATED"           # 首次登记
    IDEMPOTENT = "IDEMPOTENT"     # 重复上报且完全一致
    EVOLVED = "EVOLVED"           # 兼容演进(新增 / 删除 / 重命名)


class FieldChange(BaseModel):
    """结构演进中单个字段层面的变更说明。"""

    kind: str                     # added / removed / renamed / unchanged
    field_uid: str = ""
    from_name: str = ""
    to_name: str = ""
    data_type: str = ""


class RegistrationResult(BaseModel):
    asset: Asset
    outcome: RegistrationOutcome
    schema_version: int
    changes: list[FieldChange] = Field(default_factory=list)
    reason: str = ""              # 判定依据的人类可读说明


class LineageWriteStatus(str, Enum):
    """血缘写入的判定结果。"""

    CREATED = "CREATED"
    DUPLICATE_EDGE = "DUPLICATE_EDGE"   # 重复边: 幂等, 不重复计数


class LineageWriteResult(BaseModel):
    upstream: str
    downstream: str
    status: LineageWriteStatus
    version: int
    reason: str = ""


class ImpactReport(BaseModel):
    """任一资产的直接与传递影响范围。"""

    asset_id: str
    direction: str                 # downstream / upstream
    direct: list[str]
    transitive: list[str]         # 不含直接节点, 即严格的“传递层”
    reachable: list[str]          # direct ∪ transitive 的有序去重视图
    depth: dict[str, int] = Field(default_factory=dict)
    reason: str = ""


class OwnershipUpdate(BaseModel):
    """标签 / 负责人更新输入。

    tags/owners 给出“最终集合”; 置为 None 表示该部分不变。
    """

    asset_id: str = ""
    tags: set[str] | None = None
    owners: set[str] | None = None
    expected_meta_version: int | None = None


class OwnershipResult(BaseModel):
    asset: Asset
    meta_version: int
    changed: bool
    reason: str = ""
