# 本地元数据目录与血缘服务

一套可在本地直接运行的**元数据目录（Metadata Catalog）与数据血缘（Lineage）服务**。
用进程内的线程安全存储模拟元数据仓库，聚焦三件事：

1. 数据资产及其字段结构的**登记、幂等重复上报与结构演进**；
2. 上下游**血缘关系写入、成环检测与直接/传递影响范围查询**；
3. 标签与负责人的**乐观并发更新**，并发或过期版本写入不丢更新、不错乱。

> 存储为内存模拟（进程重启即清空），不依赖外部数据库，便于本地验证与单测。

## 快速开始

```bash
uv sync

# 启动 HTTP 服务（FastAPI + uvicorn）
uv run fastapi run main.py --host 127.0.0.1 --port 8000
# 或：uv run python main.py
```

启动后访问交互式文档：<http://127.0.0.1:8000/docs>

## 运行测试（验证方法）

```bash
uv run python -m pytest -s
```

- `-s` 可看到每个用例打印的**输入**与**判定依据**日志（`register 输入 ...` / `register 判定 ...` 等）。
- 测试覆盖：重复上报幂等、结构不兼容（类型变更/收紧非空/重命名改类型/资产种类变更）、
  血缘成环/重复边/缺失资产/传递影响、标签与负责人的乐观锁与多线程并发写入。

## HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/assets/register` | 登记资产或上报新结构（新建/幂等/兼容演进/拒绝） |
| GET  | `/assets/{name}` | 查询资产快照（结构版本、治理版本、字段、标签、负责人） |
| GET  | `/assets` | 列出全部资产 |
| POST | `/lineage` | 写入单条血缘，返回结构化判定（accepted/code/reason） |
| POST | `/lineage/batch` | 批量顺序写入，逐条给出判定 |
| GET  | `/lineage/impact/{asset}` | 直接与传递上下游影响范围 |
| PUT  | `/assets/{name}/governance` | 更新负责人/标签（乐观版本） |

错误响应统一为：

```json
{ "code": "incompatible_schema", "message": "...", "reasons": ["..."] }
```

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `validation_error` | 输入不合法（空名、重复字段、rename 声明矛盾等） |
| 404 | `asset_not_found` | 引用的资产不存在 |
| 409 | `version_conflict` | 携带过期版本写入（结构或治理），冲突可区分 |
| 422 | `incompatible_schema` | 结构演进不兼容，被拒绝且**不写入** |

血缘写接口对"重复边/成环/缺失资产"返回 200 且在响应体中用
`accepted=false` + `code` **明确标记**（业务上可预期、需要逐条处理），而非服务端 5xx。

## 快速体验（curl）

```bash
curl -s -X POST localhost:8000/assets/register -H 'content-type: application/json' -d '{
  "name": "orders", "asset_type": "table",
  "fields": [
    {"name": "id", "field_type": "long", "nullable": false},
    {"name": "amount", "field_type": "double"}
  ]}'

# 完全相同的结构重复上报 -> re_registered_idempotent，版本不变
curl -s -X POST localhost:8000/assets/register -H 'content-type: application/json' -d '{
  "name": "orders", "asset_type": "table",
  "fields": [
    {"name": "id", "field_type": "long", "nullable": false},
    {"name": "amount", "field_type": "double"}
  ]}'
```

## 代码结构

```
metadata_catalog/
  errors.py    # 可区分的领域错误（code + 详情，如 reasons / expected / actual）
  models.py    # 资产/字段/血缘/治理等数据模型
  store.py     # 线程安全的本地内存仓库（RLock + 事务临界区）
  catalog.py   # 领域服务：登记演进、血缘校验、治理乐观锁（全部业务规则在此）
  api.py       # FastAPI 适配层与错误码映射
main.py        # 服务入口
tests/         # 登记/血缘/并发/HTTP 四组测试，打印输入与判定依据
docs/          # 语义与规则的详细说明
```

详细语义见 [`docs/semantics.md`](docs/semantics.md)。
