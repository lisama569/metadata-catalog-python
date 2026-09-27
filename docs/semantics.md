# 元数据目录与血缘服务：语义说明

本文档约定登记/幂等语义、结构兼容规则、血缘语义及其验证方法。
结论必须**一致且可解释**：每个操作都返回机器可判定的 `action` / `code`
与人类可读的 `reason` / `reasons`，调用方无需解析错误文本即可区分失败原因。

---

## 1. 资产登记与幂等语义

### 1.1 版本模型

每个资产同时维护两个相互独立的单调递增版本：

| 版本 | 推进时机 | 并发控制方式 |
| --- | --- | --- |
| `schema_version`（初始 1） | 字段结构发生兼容演进 | `expected_schema_version` 乐观检查（可选） |
| `governance_version`（初始 0） | 负责人或标签发生实际变化 | `expected_governance_version` 乐观检查（必填） |

### 1.2 登记结论（`action` 三态）

对 `POST /assets/register`（或 `MetadataCatalog.register`）：

1. **`created`**：资产首次登记，`schema_version=1`，全部字段 `since_version=1`。
2. **`re_registered_idempotent`**：资产已存在，且上报结构与已登记结构**完全一致**
   （字段集合、各字段类型、`nullable` 均相同）。此时**不产生新版本**，
   `changed_fields` 为空；描述等非结构属性允许刷新。
3. **`evolved`**：结构发生了下文允许的兼容变更，`schema_version += 1`，
   `changed_fields` 给出逐字段变更说明：
   - `added`：新增字段；
   - `removed`：删除字段；
   - `renamed_from:<旧名>`：依据调用方显式 `renames` 声明识别的重命名；
   - `nullable_relaxed`：`nullable` 由 false 放宽为 true；
   - `description_updated`：仅字段描述变化。

重复上报的判定基于**结构内容**而非调用次数，因此无论上报多少次，
相同结构永远得到相同的 `schema_version`，满足幂等。

### 1.3 并发一致性

- 内存实现（`LocalStore`）中所有写操作在同一把可重入锁内完成"**检查 + 写入**"，无 TOCTOU 窗口；
  持久化实现（`PersistentStore`）中该临界区对应跨进程 `flock(LOCK_EX)`，
  结论在**多个独立进程**下同样成立。
- 并发用相同结构登记同一资产：恰好 1 个 `created`，其余全部 `re_registered_idempotent`，
  最终 `schema_version=1`。
- 携带 `expected_schema_version` 时，若与当前版本不一致，返回
  `409 version_conflict`（含 `expected_version` / `actual_version`），**不写入**。
  调用方应重新读取最新结构后重试。
- 持久化、重启恢复、多进程保证、数据文件兼容范围详见
  [`persistence.md`](persistence.md)。

### 1.4 输入校验（`400 validation_error`）

- 资产名、字段名不能为空；同一资产内字段名不得重复；
- 新资产不能携带 `renames`；
- `renames` 的源字段必须存在于旧结构、目标字段必须出现在新结构；
  多个源不能重命名到同一目标；重命名目标不能与一个"保留不动"的同名字段冲突。

---

## 2. 结构兼容规则

一次结构上报相对当前版本，按以下规则裁决。**任何不兼容变更都会整体拒绝，
仓库中的现有结构与版本保持不变，不做静默覆盖。**

### 2.1 兼容（允许演进，`schema_version` 推进）

| 变更 | 说明 |
| --- | --- |
| 新增字段 | 新字段以新版本登记（`since_version` 为新版本） |
| 删除字段 | 旧字段从结构中移除 |
| 字段重命名 | **必须**在请求中显式声明 `renames: {旧名: 新名}`，类型保持不变 |
| 放宽可空 | `nullable: false -> true`（对存量数据安全） |
| 修改字段描述 | 纯元数据变更 |

> 未声明 `renames` 时，"旧名消失 + 新名出现"按 **删除 + 新增** 处理，
> 新字段被视为全新字段（`since_version` 推进）。这保证系统不会猜测语义，
> 重命名必须由调用方显式负责。

### 2.2 不兼容（拒绝，`422 incompatible_schema`，`reasons` 逐条可区分）

| 场景 | reasons 中的判定依据 |
| --- | --- |
| 字段类型变化（如 `long -> string`） | `字段类型变更不兼容: <字段> <旧类型> -> <新类型>` |
| 收紧可空（`true -> false`） | `字段由可空收紧为非空不兼容: <字段>` |
| 重命名同时改类型 | `字段重命名同时变更类型: ...` |
| 重命名同时收紧非空 | `字段重命名同时收紧为非空: ...` |
| 资产种类变化（table -> topic 等） | `资产种类变更不兼容: ...` |
| 重命名目标与保留字段同名冲突 | `rename_target_conflicts_with_kept_field:<字段>` |

拒绝后再次查询资产，可观察到字段定义与 `schema_version` 均未变化。

---

## 3. 血缘语义

### 3.1 边的方向与定义

边 `upstream -> downstream` 表示：`upstream` 产出的数据流入 `downstream`。

- **上游（upstream）**：数据来源，沿边反向遍历；
- **下游（downstream）**：被影响对象，沿边正向遍历（用于变更影响分析）。

### 3.2 写入校验（单条与批量顺序一致）

每条边按固定顺序校验，得到唯一 `code`：

| 顺序 | code | 条件 | 处理 |
| --- | --- | --- | --- |
| 1 | `validation_error` | 上游或下游名为空 | 拒绝 |
| 2 | `lineage_cycle` | 自环 `a -> a` | 拒绝 |
| 3 | `asset_not_found` | 上游或下游资产未登记 | 拒绝（**不**自动建资产） |
| 4 | `duplicate_edge` | 同一对 `(upstream, downstream)` 已存在 | 拒绝并标记，幂等跳过，边只存一份 |
| 5 | `lineage_cycle` | 当前图中已存在 `downstream -> ... -> upstream` 路径，加入后成环 | 拒绝，仓库保持 DAG |
| 6 | `accepted` | 以上皆否 | 写入 |

**成环判定**：新增边 `u -> v` 成环，当且仅当加入前存在从 `v` 到 `u` 的路径。
拒绝响应的 `reason` 中给出找到的环路径，便于解释。

**批量写入**：在同一临界区内按输入顺序"校验 + 写入"，先前通过的边对后续判定
立即可见，因此批次内的重复/成环同样会被识别；被拒绝的边跳过但保留各自结论，
不影响批次中的其他边。

### 3.3 影响范围查询

`GET /lineage/impact/{asset}` 返回：

- `upstream_direct` / `downstream_direct`：直接上游/下游；
- `upstream_transitive` / `downstream_transitive`：传递可达集合（不含资产自身）。

所有列表排序输出，保证结论可复现；资产不存在时返回 `404 asset_not_found`。
遍历使用 visited 集合，即使存在历史脏数据也不会死循环。

---

## 4. 标签与负责人（治理信息）

`PUT /assets/{name}/governance`，请求必须携带 `expected_governance_version`：

- `owner`：省略（null）表示不改；空串非法；
- `add_tags` / `remove_tags`：增量增删；同一标签在一次请求中既增又删会被拒绝；
- `replace_tags=true`：用 `add_tags` 整体替换标签集合；
- 版本不匹配 -> `409 version_conflict`（`expected_version`/`actual_version` 可区分），不写入；
- 当新负责人/标签与现状完全一致时，判定为幂等无变化（`changed=false`），版本不推进；
- 真实变化才推进版本。

并发下 N 个基于同一旧版本的写入：恰好 1 个成功，其余全部收到 `version_conflict`，
最终版本只 +1，不会出现丢更新或结构错乱。冲突方读取新版本后重试即可收敛。

---

## 5. 验证方法

### 5.1 自动化测试

```bash
uv run python -m pytest -s
```

分组与覆盖点：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_registration.py` | 首次登记、重复幂等、新增/删除/重命名演进、类型变更/收紧非空/改资产种类等不兼容拒绝、过期结构版本冲突、非法输入 |
| `tests/test_lineage.py` | 接受/重复边/缺失资产/自环/间接成环、批量顺序语义、直接与传递影响范围 |
| `tests/test_governance.py` | owner/标签增删替换、幂等无变化、乐观锁冲突、冲突后重试、**多线程并发**治理写入、并发同结构登记幂等、并发血缘重复边 |
| `tests/test_api.py` | HTTP 状态码与错误 `code` 映射、批量接口、404/409/422 |
| `tests/test_persistence.py` | 重启恢复、被拒/中断不产生脏数据、半行裁剪、压缩代际切换、格式版本不兼容 |
| `tests/test_multiprocess.py` | **真实多进程**并发登记/演进/治理/血缘的最终一致性、过期版本必冲突、跨进程可见 |
| `tests/test_scale.py` | 上万资产与血缘规模下读写/影响范围/重启加载性能 |

持久化与多进程语义、恢复边界、数据文件兼容性见 [`persistence.md`](persistence.md)。

每个用例都通过日志打印**输入**与**判定依据**，例如：

```
INFO metadata_catalog | register 输入: asset=t1 type=table fields=['id:long'] renames={'name': 'full_name'} expected_v=1
INFO metadata_catalog | register 判定: incompatible asset=t1 reasons=['字段类型变更不兼容: id long -> string']
INFO metadata_catalog.tests | 用例输入: id 类型 long -> string
INFO metadata_catalog.tests | 判定依据: code=incompatible_schema reasons=['字段类型变更不兼容: ...']
```

并发用例使用 `Barrier` 让多线程同时发起写入，并用计数断言
"成功数恰为 1 / 冲突或重复数为 N-1 / 最终版本只推进一次"，
验证锁与乐观版本在竞争下的一致性。

### 5.2 手工验证要点

1. **幂等**：同一结构连续登记两次，第二次 `action=re_registered_idempotent` 且版本不变。
2. **不兼容不覆盖**：先登记 `id:long`，再上报 `id:string` 得到 422；
   随后 `GET /assets/<name>` 仍为 `long`，版本仍为 1。
3. **成环**：建立 `a->b->d`，再写 `d->a` 返回 `lineage_cycle`，环不出现在仓库中。
4. **并发冲突**：两个客户端都基于同一治理版本修改，只有一个 200，另一个 409，
   且响应中带有 `expected_version` 与 `actual_version`。
