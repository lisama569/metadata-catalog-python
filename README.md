# 本地元数据目录与血缘服务

一套可在本地直接运行的**元数据目录（Metadata Catalog）与数据血缘（Lineage）服务**。
数据落在本地磁盘目录，服务重启后数据仍在、多个进程可同时打开同一仓库，聚焦三件事：

1. 数据资产及其字段结构的**登记、幂等重复上报与结构演进**；
2. 上下游**血缘关系写入、成环检测与直接/传递影响范围查询**；
3. 标签与负责人的**乐观并发更新**，并发或过期版本写入不丢更新、不错乱。

> 默认使用**本地持久化仓库**（目录 `.metadata_catalog_repo/`，可用环境变量
> `METADATA_CATALOG_REPO` 覆盖）：重启不丢数据、多进程共享。需要进程内临时
> 仓库时可显式注入 `LocalStore()`。持久化与并发的完整语义见
> [`docs/semantics.md`](docs/semantics.md) 第 5 节。

## 快速开始

```bash
uv sync

# 启动 HTTP 服务（FastAPI + uvicorn）；数据默认落在 ./.metadata_catalog_repo
uv run fastapi run main.py --host 127.0.0.1 --port 8000
# 指定仓库目录：METADATA_CATALOG_REPO=/path/to/repo uv run python main.py
```

启动后访问交互式文档：<http://127.0.0.1:8000/docs>

停止后重新启动（或在另一个终端用同一仓库目录再启动一个实例），之前登记的
资产、结构版本、标签负责人、血缘关系都还在；多实例携带相同版本并发写同一资产
时，恰好一个成功，其余得到可区分的 `409 version_conflict`。

## 运行测试（验证方法）

```bash
uv run python -m pytest -s
```

- `-s` 可看到每个用例打印的**输入**与**判定依据**日志（`register 输入 ...` / `register 判定 ...` 等）。
- 测试覆盖：重复上报幂等、结构不兼容（类型变更/收紧非空/重命名改类型/资产种类变更）、
  血缘成环/重复边/缺失资产/传递影响、标签与负责人的乐观锁；
  **同进程真实线程并发**（持续并发读+突发并发写、同版本结构/治理竞争、
  并发登记与重复血缘去重）、**真实 HTTP 并发**（uvicorn + 多 httpx 客户端，
  读不超时、写恰一成功）、**失败写入回滚**（rename 前抛错后同进程读、
  无关写、重启都不可见）；以及**真实多进程并发与最终一致**（spawn 多进程、
  被杀写入不留脏数据）、重启完整恢复、上万规模读写耗时护栏（详见 docs 第 6 节）。
- 并发与故障专项用例可用
  `uv run python -m pytest tests/test_concurrency.py tests/test_http_concurrency.py tests/test_mp_failure.py tests/test_multiprocess.py tests/test_persistence.py`
  只跑这部分。

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
| 400 | `validation_error` | 输入不合法（空名、重复字段、rename 声明矛盾、请求体字段非法等） |
| 404 | `asset_not_found` | 引用的资产不存在 |
| 409 | `version_conflict` | 携带过期版本写入（结构或治理），冲突可区分（含 `expected_version` / `actual_version`） |
| 422 | `incompatible_schema` | 结构演进不兼容，被拒绝且**不写入** |
| 500 | `storage_error` | 本地仓库写入/读取中途的 IO 故障（如磁盘写失败）。失败写**不生效**，仓库停留在写入前完整状态；它同时是 `OSError` 子类，调用方两种 except 都能接住 |
| 500 | `storage_corruption` / `unsupported_storage_format` | 本地仓库数据文件损坏 / 格式版本不受支持（存储故障，区别于业务裁决） |
| 500 | `internal_error` | 理论上不应出现的未归类服务端异常的最后防线，响应仍带 `code`（不会是裸 500） |

业务裁决（400/404/409/422）与存储故障（500）严格分离：冲突和失败是两类
不同结果——**冲突**意味着调用方基于了过期版本，读新版本后重试即可收敛；
**失败**意味着这一次提交根本没有生效，仓库里只能看到写入前的完整状态，
不存在"半更新"需要回滚或修补。

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
  errors.py       # 可区分的领域错误（code + 详情，如 reasons / expected / actual；
                  #   StorageError 同时是 OSError 子类，含 storage_error/
                  #   storage_corruption / unsupported_storage_format）
  models.py       # 资产/字段/血缘/治理等数据模型（快照写时复制 copy/with_updates）
  locking.py      # 每线程可重入的多读单写事务门（写者优先），供两种仓库共用
  store.py        # 线程安全的进程内内存仓库（读共享/写排他，读出的快照为副本）
  persistence.py  # 本地磁盘持久化仓库：原子写 + flock 跨进程锁（引用计数）+ 增量缓存
  catalog.py      # 领域服务：登记演进、血缘校验、治理乐观锁（全部业务规则在此，
                  #   任何写入都"先构造完整新快照、落盘成功后才生效"）
  api.py          # FastAPI 适配层与错误码映射（默认打开本地持久化仓库，
                  #   含未归类异常 -> internal_error 的最后防线）
main.py           # 服务入口
tests/            # 登记/血缘/治理/HTTP + 并发/失败回滚/真实多进程/规模 测试
docs/             # 语义、持久化与并发保证的详细说明
```

仓库目录下：`FORMAT`（格式版本）、`lock`（跨进程锁）、`assets/*.json`
（每个资产一份完整快照）、`edges/*.json`（每条血缘边一份）。数据文件为内部
格式，请勿手工改写。

详细语义见 [`docs/semantics.md`](docs/semantics.md)。
