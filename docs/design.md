# 元数据目录与血缘服务 — 语义与设计说明

本地可运行的元数据目录与血缘服务。使用**本地 JSON 文件存储**模拟元数据仓库，
在多线程 / 多进程并发与异常输入下，保证资产登记、结构演进与血缘查询的结论
一致、拒绝原因可区分、过程可解释。

---

## 1. 运行方式

```bash
# 安装依赖(含 pytest)
uv sync

# 启动 HTTP 服务(默认在工作目录生成 catalog_data.json)
uv run fastapi dev main.py
# 或
uv run uvicorn main:app --reload

# 运行测试
uv run pytest -q
```

服务默认数据文件为进程工作目录下的 `catalog_data.json`，另有同名校验锁文件
`catalog_data.json.lock`（用于跨进程互斥，可安全保留或删除）。

---

## 2. HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/assets` | 登记 / 重复上报 / 演进资产结构 |
| GET  | `/assets` | 列出全部资产 |
| GET  | `/assets/{id}` | 查询单个资产 |
| PUT  | `/assets/{id}/ownership` | 更新标签 / 负责人（乐观锁） |
| POST | `/lineage/edges` | 写入一条上下游血缘边 |
| GET  | `/assets/{id}/impact?direction=downstream\|upstream` | 直接 + 传递影响范围 |

### 错误码与 HTTP 状态码

所有业务错误返回统一结构：

```json
{ "error_code": "...", "message": "...", "details": { ... } }
```

| error_code | HTTP | 触发条件 |
|------------|------|----------|
| `ASSET_NOT_FOUND` | 404 | 引用了不存在的资产 |
| `ASSET_ALREADY_EXISTS` | 409 | 以仅新建语义登记已存在资产 |
| `SCHEMA_INCOMPATIBLE` | 422 | 结构演进不兼容，`details.reason` 进一步细分（见 §4） |
| `VERSION_CONFLICT` | 409 | 携带过期版本的并发写入，`details` 含 `expected_version` / `actual_version` / `scope` |
| `LINEAGE_ENDPOINT_MISSING` | 422 | 血缘端点不存在，`details.missing` 列出缺失资产 |
| `LINEAGE_CYCLE` | 422 | 血缘成环，`details.cycle` 给出闭环路径 |
| `INVALID_ARGUMENT` | 400 | 明显非法入参 |

调用方依据 `error_code`（结构问题再看 `details.reason`）即可程序化区分失败原因。

---

## 3. 登记与幂等语义

### 3.1 字段身份模型

每个字段有稳定身份 `uid` 与当前名称 `name`：

- 首次登记时若不提供 `uid`，系统以字段名作为 `uid`；
- **重命名只改 `name`，`uid` 不变**，因此系统能在多次上报间识别“同一个字段”，
  不会把重命名误当成“删除旧字段 + 新增字段”。

### 3.2 三种登记结果（`outcome`）

| outcome | 含义 | schema_version |
|---------|------|----------------|
| `CREATED` | 资产不存在，首次登记 | 置为 1 |
| `IDEMPOTENT` | 上报结构与已登记结构**逐位完全一致**（身份、顺序、类型、可空、描述） | 不变 |
| `EVOLVED` | 兼容的结构演进（新增 / 删除 / 重命名） | +1 |

- 重复上报**完全幂等**：不提升版本、不改 `updated_at`、返回空变更清单；
- 每次结果都带 `changes`（added / removed / renamed / unchanged）和人类可读的
  `reason`，说明判定依据；
- 任何不兼容变更都会在改动前抛出异常，**旧结构与版本保持原样，绝不静默覆盖**。

### 3.3 结构版本（CAS）

每个资产维护两个互相独立、单调递增的版本号：

- `schema_version`：字段结构每次演进时 +1；
- `meta_version`：标签 / 负责人每次变更时 +1。

`POST /assets` 的 `expected_schema_version`：

- 不传（`null`）：upsert 语义——重复上报幂等，变更走兼容性校验；
- 传整数：CAS 语义——必须等于当前 `schema_version`，否则返回 `VERSION_CONFLICT`。

---

## 4. 结构兼容规则

| 变更 | 是否兼容 | 判定 / 拒绝原因 |
|------|----------|-----------------|
| 新增字段 | ✅ 兼容 | 记录 `added`，版本 +1 |
| 删除字段（旧字段在新结构中消失） | ✅ 兼容 | 明确记录 `removed`，版本 +1（不静默保留） |
| 重命名字段（在 `renames` 显式声明） | ✅ 兼容 | 记录 `renamed`，`uid` 保持稳定，版本 +1 |
| 改变字段数据类型 | ❌ 拒绝 | `FIELD_TYPE_CHANGED` |
| 重命名同时改变类型 | ❌ 拒绝 | `RENAME_TYPE_CHANGED` |
| 字段消失 + 出现同类型新字段，但**未声明** rename | ❌ 拒绝 | `FIELD_MISSING_RENAME`（防止漏报重命名被静默当成 删+增） |
| rename 源字段不存在 | ❌ 拒绝 | `RENAME_SOURCE_NOT_FOUND` |
| rename 目标未出现在新结构 / 多个字段改到同名 | ❌ 拒绝 | `RENAME_TARGET_CONFLICT` |
| 入参字段列表内部重名 | ❌ 拒绝 | `DUPLICATE_FIELD_IN_PAYLOAD` |
| 新增字段的 `uid` 与已有字段冲突 | ❌ 拒绝 | `NAME_COLLISION` |
| 结构完全一致的重复上报 | — 幂等 | `IDEMPOTENT`，不产生新版本 |

> 设计取向：**重命名必须显式声明**。当系统观察到“一个字段消失、同类型新字段
> 出现”这种高度疑似漏报重命名的情形时，选择拒绝并要求调用方明确意图，而不是
> 猜测。删除是合法操作：若新旧字段类型不同，则视为普通的 删除 + 新增，正常演进。

---

## 5. 血缘语义与验证

### 5.1 边方向与存储

- 边 `upstream -> downstream`，表示“上游产出 / 影响下游”；
- 同时维护正向邻接表 `out_edges` 与反向邻接表 `in_edges`，支持双向查询；
- 每条边记录写入时的全局血缘版本 `edge_versions`，整个血缘图有单调 `version`。

### 5.2 写入判定（不静默接受，也不静默丢弃）

| 情形 | 处理 |
|------|------|
| 端点不存在 | 抛 `LINEAGE_ENDPOINT_MISSING`，`details.missing` 列出所有缺失端点 |
| 会成环（含自环） | 抛 `LINEAGE_CYCLE`，`details.cycle` 给出形如 `c -> b -> a -> c` 的闭环 |
| 重复边 | 返回 `DUPLICATE_EDGE`，幂等，不重复计数、不提升版本 |
| 正常 | 返回 `CREATED` |

成环判定依据：加入 `u -> v` 前，若从 `v` 沿已有边能到达 `u`，则形成闭环
`u -> v -> … -> u`（自环 `u -> u` 同理）。

### 5.3 影响范围查询

`impact` 用 BFS 沿对应邻接方向计算：

- `direct`：深度 1 的直接影响；
- `transitive`：深度 ≥ 2 的传递影响；
- `reachable`：两者去重合并后的稳定有序视图；
- `depth`：每个可达节点到起点的最短距离；
- 菱形依赖（一个节点经多条路径可达）只计一次。

---

## 6. 并发控制：为什么结论一致

1. **单事务原子性**：所有写操作在一个存储事务内完成
   “加锁 → 加载最新已提交快照 → 校验 → 修改 → 原子落盘 → 解锁”。
   校验与生效之间不会插入其它写入，因此不会出现“检查时不存在、写入时已存在”
   之类的竞态，异常回滚也不会留下半截状态。
2. **进程内互斥**：每实例一把不可重入 `threading.Lock`，覆盖整个临界区。
3. **进程间互斥**：对锁文件加 `fcntl.flock` 排他锁；多进程以磁盘文件为裁决点。
4. **原子落盘**：临时文件 + `os.fsync` + `os.replace`，崩溃时旧文件保持完整。
5. **乐观并发控制**：标签 / 负责人更新必须携带 `expected_meta_version`；
   过期写入返回 `VERSION_CONFLICT`，由调用方重读最新版本后合并重试，从机制上
   避免“后写覆盖先写”的丢更新。

---

## 7. 如何验证（测试）

```bash
uv run pytest -q                       # 全量
uv run pytest -k concurrent -q         # 仅并发相关
uv run pytest metadata_catalog/tests/test_schema.py -v
```

覆盖场景：

- **重复上报幂等**：完全一致的二次上报不升版本、不改对象；
- **结构不兼容**：改类型、漏报重命名、非法 rename、入参重名等，断言各自不同的
  `reason`，并断言被拒绝后旧结构 / 版本不变；
- **血缘**：重复边幂等、成环（含自环、长环）返回闭环路径、缺失端点、直接 /
  传递 / 菱形影响范围；
- **并发写入**：
  - 多线程并发加 20 条边，无丢边无重复；
  - 多线程并发“同向重复 + 反向成环”，绝不会两个方向都被接受；
  - 多线程 / 多进程并发更新标签，恰好一成一冲突，无丢更新；
  - 多进程（`spawn`，各自独立 store 实例）并发登记资产，全部落盘；
- **持久化**：重启重载数据完整、失败事务不落盘、血缘重载后仍识别重复边。

### 日志：输入与判定依据

`metadata_catalog.schema / lineage / ownership / api` 各日志器在每次操作时打印：

- **输入**：如 `register_asset 输入 asset_id=... fields=[...] renames=[...] expected_ver=...`；
- **判定依据**：如 `判定 IDEMPOTENT(重复上报,幂等) ...`、
  `登记被拒绝 ... 依据=字段 id 类型不可变更: int -> bigint`、
  `血缘写入被拒绝 ... 依据=血缘成环 path=[...]`。

测试通过 `conftest.assert_log_contains` 断言关键输入与判定确实出现在日志中，
满足“单测日志打印输入与判定依据”。
