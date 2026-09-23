"""本地元数据目录与血缘服务。

模块划分:
- errors: 可区分的领域异常类型
- models: 领域模型
- store: 本地持久化存储(模拟元数据仓库)
- schema: 结构登记 / 幂等 / 兼容性演进
- lineage: 上下游血缘写入、校验与影响范围查询
- ownership: 标签与负责人的乐观并发更新
- service: 门面服务, 组合各子域并保证事务一致性
- api: FastAPI HTTP 层
"""

__all__ = [
    "errors",
    "models",
    "store",
    "schema",
    "lineage",
    "ownership",
    "service",
    "api",
]
