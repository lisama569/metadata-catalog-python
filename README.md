# 本地元数据目录与血缘服务

本地可运行的元数据目录（资产 / 字段结构 / 标签 / 负责人）与上下游血缘服务，
用本地 JSON 文件存储模拟元数据仓库。重点保证：**登记幂等、结构兼容演进、
血缘成环/重复边/缺失端点可区分处理、并发写入不丢更新**，且每个判定都可解释。

- 语义与规则细节见 [`docs/design.md`](docs/design.md)。

## 快速开始

```bash
uv sync
uv run fastapi dev main.py        # 启动服务 http://127.0.0.1:8000
uv run pytest -q                  # 运行测试
```

## 示例

```bash
# 1) 登记资产
curl -s -X POST localhost:8000/assets -H 'content-type: application/json' -d '{
  "asset_id": "orders",
  "fields": [{"name":"id","data_type":"int"},{"name":"amt","data_type":"decimal"}]
}'

# 2) 兼容演进: 重命名 amt -> amount(显式声明, uid 保持稳定)
curl -s -X POST localhost:8000/assets -H 'content-type: application/json' -d '{
  "asset_id": "orders",
  "fields": [{"name":"id","data_type":"int"},{"name":"amount","data_type":"decimal"}],
  "renames": [{"from_name":"amt","to_name":"amount"}]
}'

# 3) 不兼容: 改类型会被 422 拒绝(SCHEMA_INCOMPATIBLE / FIELD_TYPE_CHANGED)
curl -s -X POST localhost:8000/assets -H 'content-type: application/json' -d '{
  "asset_id": "orders",
  "fields": [{"name":"id","data_type":"bigint"},{"name":"amount","data_type":"decimal"}]
}'

# 4) 血缘
curl -s -X POST localhost:8000/lineage/edges -H 'content-type: application/json' \
  -d '{"upstream":"orders","downstream":"orders_dwd"}'
curl -s 'localhost:8000/assets/orders/impact?direction=downstream'

# 5) 标签 / 负责人(乐观锁)
curl -s -X PUT localhost:8000/assets/orders/ownership -H 'content-type: application/json' \
  -d '{"tags":["pii"],"owners":["alice"],"expected_meta_version":0}'
```

## 代码结构

```
metadata_catalog/
  errors.py      # 可区分的领域异常与错误码
  models.py      # 领域模型 / 版本号
  store.py       # JSON 文件存储 + 线程/文件锁 + 事务
  schema.py      # 登记、幂等、结构兼容性演进
  lineage.py     # 血缘写入校验、成环检测、影响范围 BFS
  ownership.py   # 标签/负责人乐观并发更新
  service.py     # 事务门面
  api.py         # FastAPI HTTP 层
  tests/         # 53 个测试(含多线程/多进程并发)
```
