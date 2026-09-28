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

- 所有写操作在存储层同一把可重入锁内完成"**检查 + 写入**"，无 TOCTOU 窗口。
- 并发用相同结构登记同一资产：恰好 1 个 `created`，其余全部 `re_registered_idempotent`，
  最终 `schema_version=1`。
- 携带 `expected_schema_version` 时，若与当前版本不一致，返回
  `409 version_conflict`（含 `expected_version` / `actual_version`），**不写入**。
  调用方应重新读取最新结构后重试。

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

## 5. 本地持久化与多进程仓库

前述所有语义在使用**本地持久化仓库**（`PersistentStore(repo_dir)`）时保持
不变，并且把"只在一个进程内有效"扩展为"落到本地磁盘、多个进程共享"。

### 5.1 存储形态与恢复程度

仓库就是本地目录（默认 `.metadata_catalog_repo`，可用环境变量
`METADATA_CATALOG_REPO` 或 `create_app(repo_dir=...)` 覆盖）：

```
<repo>/
  FORMAT            # 仓库格式版本标记，内容首行 metadata-catalog-repo format=1
  lock              # 跨进程 flock 锁文件（无业务数据）
  assets/N<name>.json   # 每个资产一份完整快照
  edges/E<sha1>.json    # 每条血缘边一份
```

**重启/换进程后能完整恢复的内容**：资产当前结构（含每个字段的
`since_version`）、`schema_version`、标签与负责人、`governance_version`、
全部已登记血缘边。服务进程退出再拉起，或另开一个进程打开同一目录，读到的
是同一份完整数据。

**"从未登记"与"登记过但写入失败"严格可区分**：

- 资产从未登记：目录里没有它的数据文件，读取得到 `asset_not_found`/`None`；
- 登记过但某次写入被拒绝（422/409）或写入时报错：旧文件原封不动，读取仍得到
  登记时那份完整快照。被拒/失败的写入在重启后不会看起来像生效过。

### 5.2 崩溃与部分写入语义（原子性）

每次写入都是"**临时文件写全 + `fsync` + 原子 `rename` 覆盖 + 目录 `fsync`**"。
因此进程在写入中途被杀掉或报错时：

- rename 之前崩溃：正式文件保持写入前的完整版本，只可能留下一个临时文件
  （下次打开仓库时自动清理，临时文件从不以正式名出现）；
- rename 之后崩溃：正式文件已是写入后的完整版本。

磁盘上永远不会出现"只写了一半"的资产结构，也不会出现"版本号已推进但字段内容
还是旧的/残缺的"这类版本与内容对不上的状态。单次写操作具备故障原子性：
重启后读到的要么是写入前、要么是写入后的完整状态。

#### 失败写在"同一进程"内同样不可见（不仅是重启后）

仅有磁盘原子替换还不够：如果业务层在写盘**之前**就先改写了进程内缓存中的对象，
那么写盘失败时旧文件虽在磁盘上完好，但同一进程随后的读已经能看到"版本推进、
字段变化"的半成品；更隐蔽的是，之后一次**无关更新**（如改负责人）会把这份脏
对象再次整体落盘，使失败的结构在重启后"看起来已经生效"。本实现通过两点杜绝：

- `AssetSnapshot` / `FieldRecord` 是**不可变对象**：任何更新都先在全新容器上
  组装，再用 `dataclasses.replace` 生成一份**新快照**整体发布，从不就地改写
  已在缓存中的旧快照；
- 存储层"先原子写盘成功，再用一份全新缓存 dict 整体替换引用"。写盘抛错时
  缓存完全不被触碰。

因此一次失败/中断写满足：**失败结构在同一进程的后续读取中不可见，也不会被随后
一次无关更新带出，重启后同样不可见**；仓库只保留写入前或写入后的完整状态，
结构版本、字段内容、标签负责人、血缘始终相互对得上。失败期间进程持有的排他锁
随异常正常释放，后续写可立即继续。

> 说明：这是**单操作级**的原子性（不是跨多资产的事务日志）。批量血缘接口中
> 先通过的边会独立生效，被拒的边不生效，这与第 3 节既有语义一致。

### 5.3 多进程与多线程并发下的保证

同一套结论在三种场景下完全一致：**同一进程内多线程、跨多个 OS 进程、真实 HTTP
并发**。并发控制分三层，各自堵住一类问题：

1. **跨进程**：`fcntl.flock` 在同一 `lock` 文件上提供读共享 / 写独占，服务层的
   "检查 + 写入"始终在同一个跨进程排他临界区内完成（POSIX；Linux/macOS 等）。
2. **进程内：线程本地的事务状态与 flock fd**。每个线程独立记录自己持有的锁
   模式与嵌套深度，并在首次进入临界区时**各自 `open` 一次锁文件、持有自己的
   fd**。`flock` 锁关联在"打开文件描述"上，独立 fd 让同进程多个读线程对其他
   进程表现为多把并存的共享锁、写线程则是排他锁——与"多个进程各持一把锁"
   完全等价，也不会出现一个线程解锁连带释放其他线程锁的问题。同线程事务可
   重入，嵌套不重复加锁。进程内另用一把多读单写锁保证读者并发、写者独占。
3. **缓存读多写一**：普通读只在一把短锁内瞬时取一份"此后不再被修改的缓存 dict
   引用"再遍历，所以读之间完全并发、不会在遍历中撞上字典增删；缓存刷新由刷新
   闸门串行，并在扫描结束后若发现目录又变化则放弃本次发布，杜绝"旧扫描覆盖新
   缓存"导致的已提交资产短暂消失。

由此保证：

- **读之间可以同时进行**：多线程/多进程的读彼此并行，且不被慢刷新长时间阻塞；
  读者只可能读到写入前或写入后的某一份完整快照；
- **写之间不能互相穿透**：所有写在排他临界区内完成"检查 + 写入"，无 TOCTOU；
- 多个进程/线程并发用相同结构首次登记同一资产：恰好 1 个 `created`，其余
  `re_registered_idempotent`，最终 `schema_version=1`，**不会重复累加版本**；
- 多个调用方基于同一 `expected_schema_version`/`expected_governance_version`
  并发改同一资产：**恰好 1 个成功，其余全部返回可区分的 `version_conflict`**
  （携带 `expected_version` / `actual_version`），最终版本只 +1，不丢更新、
  不出现字段拼花；
- 并发写同一条血缘边：恰好 1 个 `accepted`，其余 `duplicate_edge`，磁盘上只
  保留一份；成环检查同样在排他临界区内，并发也不会产生环或重复残留；
- 任何携带过期版本的写入，只要版本对不上就一定是冲突，**任何情况下都不会被
  静默当成成功**（HTTP `409`，异常含 expected/actual）；
- 每个调用都得到**明确的成功或可区分的冲突/失败**，不会变成未分类的服务端异常。

**他进程写入的可见性**：每次进入临界区时先按目录签名增量同步磁盘变化（见 5.4），
所以一个进程/线程提交后，另一个进程/线程在下一次进入临界区时一定能读到。

### 5.3.1 冲突、业务拒绝与存储失败如何区分

三类否定结果有明确边界，调用方只需按 `code`（HTTP 状态码）分支处理：

| 类别 | code | HTTP | 含义与调用方处理 |
| --- | --- | --- | --- |
| 业务冲突 | `version_conflict` | 409 | 携带过期版本的并发写，带 `expected_version`/`actual_version`；读取最新版本后重试 |
| 业务拒绝 | `validation_error` / `incompatible_schema` / `asset_not_found` / `lineage_cycle` / `duplicate_edge` | 400 / 422 / 404 / 200(血缘结论体) | 输入不合法、演进不兼容、引用缺失、成环、重复边；仓库状态不变 |
| 存储失败 | `storage_error` / `storage_corruption` / `unsupported_storage_format` | 500 | 磁盘 IO、fsync/flock、数据文件损坏或格式不受支持等仓库侧故障 |

底层文件系统故障（`open`/`write`/`fsync`/`flock` 失败）都会被包装成
`storage_error`，它**同时是 `OSError` 子类**，因此既保留 `except OSError` 的
捕获习惯，又带机器可判定的 `code`。HTTP 层另有 `internal_error`(500) 兜底，
任何未预期异常也返回结构化错误体而不是裸 500 文本。注意：**业务冲突（409）绝不
是 5xx，存储失败（500）也绝不会被误报成成功或冲突。**

### 5.4 规模与性能

- 每个资产/每条边是独立小文件，单点写入只覆盖一个文件，不重写全库；
- 进程内维护按文件 stat 签名（inode、mtime_ns、大小）的缓存，并先用目录签名
  （mtime_ns/ctime_ns）判断目录是否变化：无变化时一次额外的 `stat` 即返回，
  不再全量扫描；目录变化时只重读新增/改动的文件；
- 因此上万资产/边规模下：暖缓存的单点查询为纯内存操作，传递影响范围是对
  缓存图的 DFS，单笔写入只动一个数据文件；冷启动需要一次全量装载（读完所有
  数据文件），之后即进入缓存路径。

具体护栏见 `tests/test_scale.py`（10 000 资产 + 5 000 边）。

### 5.5 数据文件兼容范围

- 仓库目录首行 `FORMAT` 与每个数据文件内的 `format_version` 共同标识格式；
  当前为 **format v1**，仅支持读写 v1 的仓库与数据文件；
- 打开仓库时若 `FORMAT` 版本更新或更旧、无法识别，抛
  `unsupported_storage_format`（含文件版本与当前支持版本）；
  数据文件 JSON 损坏、缺字段、文件名与内容身份不一致等，抛
  `storage_corruption`。这两类属于**存储故障**（HTTP 500），与业务拒绝
  （400/404/409/422）严格区分，调用方可按 `code` 分别处理；
- 数据文件是面向机器的内部格式，应视为不兼容边界：**请勿手工改写**；
  跨大版本升级前应备份整个仓库目录。

### 5.6 平台与使用约束

- 跨进程锁依赖 `fcntl.flock`，即在 POSIX 本地文件系统上提供保证；网络文件系统
  （部分 NFS 挂载）上的 flock 与目录 mtime 语义可能不一致，不在保证范围内；
- 同一台机器上多个进程打开同一目录是受支持的标准用法；目录本身不要放在会被
  外部程序并发改写数据文件的位置。

---

## 6. 验证方法

### 6.1 自动化测试

```bash
uv run python -m pytest -s
```

分组与覆盖点：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_registration.py` | 首次登记、重复幂等、新增/删除/重命名演进、类型变更/收紧非空/改资产种类等不兼容拒绝、过期结构版本冲突、非法输入 |
| `tests/test_lineage.py` | 接受/重复边/缺失资产/自环/间接成环、批量顺序语义、直接与传递影响范围 |
| `tests/test_governance.py` | owner/标签增删替换、幂等无变化、乐观锁冲突、冲突后重试、**多线程并发**治理写入、并发同结构登记幂等、并发血缘重复边 |
| `tests/test_api.py` | HTTP 状态码与错误 `code` 映射、批量接口、404/409/422，以及**重启后 HTTP 读取完整**、损坏文件返回 `500 storage_corruption` |
| `tests/test_persistence.py` | **磁盘重启完整恢复**、被拒/失败写入不生效、"从未登记 vs 登记后失败"可区分、rename 前抛错/被 kill 不留脏数据与临时文件、**失败写后同进程读+无关写再重启仍不可见**、失败边写不可见、FORMAT 版本兼容检查 |
| `tests/test_concurrency.py` | **同一进程多线程 + 真实 PersistentStore**：并发读+写零异常且快照自洽、同版本并发演进/治理恰一个成功其余可区分冲突、并发登记幂等、并发重复血缘只一份、失败写在同进程后续读与无关写中不可见 |
| `tests/test_http_concurrency.py` | **真实 uvicorn 进程 + 多线程 httpx**：并发读+写不超时无 5xx、同版本并发演进/治理恰一个 200 其余 409、并发登记与重复血缘的幂等去重 |
| `tests/test_multiprocess.py` | **真实多个 OS 进程**（spawn）打开同一仓库：并发治理只一个成功其余冲突、并发登记只一次 created、并发血缘只一份边、并发演进只一次生效且不拼花、**多进程×多线程混合读写最终一致（sv==1+成功写数、字段数==sv）**、他进程写入对已开仓库可见 |
| `tests/test_scale.py` | 10 000 资产 + 5 000 边下暖缓存点查/传递遍历/单笔写入耗时护栏、冷启动装载耗时 |

并发与持久化用例的关键做法：

- 多线程用例使用 `Barrier` 同时发起请求，断言"成功数恰为 1 / 冲突或重复数为
  N-1 / 最终版本只推进一次 / 读者看到的字段数恒等于结构版本"；
- 真实 HTTP 用例动态选端口拉起独立 `uvicorn` 子进程（默认 PersistentStore），
  用线程池同时发请求，验证不是 TestClient 同步包装下的"伪并发"；
- 多进程用例使用 `multiprocessing` 的 **spawn** 上下文拉起独立解释器进程，
  每个进程独立构造 `PersistentStore`（独立 flock 客户端与独立缓存），混合用例
  还在每个进程内再起线程，这才是"多个服务实例 + 每实例多 worker"的真实场景；
- 中断用例通过故障注入点（环境变量 `METADATA_CATALOG_CRASH_AFTER_WRITE`）在
  "临时文件写完、rename 之前"分别制造抛错（`raise`，归类为 `storage_error`）
  与 `os._exit`（模拟被 kill），再在同进程读、无关写、重新打开三个视角断言
  失败内容完全不可见且无临时文件残留；
- 规模用例只设耗时护栏并打印实测耗时，不绑定具体硬件数字。

### 6.2 手工验证要点

1. **重启保留**：`METADATA_CATALOG_REPO=./demo_repo uv run fastapi run main.py`，
   登记资产/写治理/写血缘后停止服务，用同一命令再起，`GET /assets/<name>`
   仍能看到结构版本、负责人、标签，`GET /lineage/impact/<name>` 仍能看到血缘。
2. **多进程**：再开一个终端用同一 `METADATA_CATALOG_REPO` 起第二个实例，两边
   都能读到对方写入；对同一资产携带相同版本并发更新，只有一个成功，另一个收到
   `409 version_conflict`。
3. **幂等**：同一结构连续登记两次，第二次 `action=re_registered_idempotent`
   且版本不变。
4. **不兼容不覆盖**：先登记 `id:long`，再上报 `id:string` 得到 422；
   随后 `GET /assets/<name>` 仍为 `long`，版本仍为 1（重启后同样成立）。
5. **成环**：建立 `a->b->d`，再写 `d->a` 返回 `lineage_cycle`，环不出现在仓库中。
6. **并发冲突**：两个客户端都基于同一治理版本修改，只有一个 200，另一个 409，
   且响应中带有 `expected_version` 与 `actual_version`。
7. **失败写不可见（同进程 + 重启）**：

   ```bash
   # 起服务后先登记 t1（id:long, sv=1），再带故障注入做一次演进：
   METADATA_CATALOG_CRASH_AFTER_WRITE=raise \
   METADATA_CATALOG_REPO=./demo_repo uv run python - <<'PY'
   from metadata_catalog.api import *  # 或直接用 catalog
   from metadata_catalog.persistence import PersistentStore
   from metadata_catalog.catalog import MetadataCatalog
   from metadata_catalog.models import *
   c = MetadataCatalog(PersistentStore("./demo_repo"))
   try:
       c.register(RegisterRequest(spec=AssetSpec(name="t1", asset_type=AssetType.TABLE,
           fields=[FieldSpec("id", FieldType.LONG, False), FieldSpec("name", FieldType.STRING)]),
           expected_schema_version=1))
   except Exception as e:
       print("归类为", getattr(e, "code", type(e).__name__))   # storage_error
   print("同进程读:", c.get_asset("t1").schema_version)        # 仍是 1，无 name
   c.update_governance(GovernanceUpdate(asset="t1", expected_governance_version=0, owner="alice"))
   c.store.close()
   PY
   # 重新打开：sv 仍为 1、字段仍只有 id，而 owner=alice（结构失败未被治理写带出）
   ```

   把 `raise` 换成 `kill`（`os._exit(42)`，模拟进程在 rename 前被杀）再重新打开，
   结论相同：sv=1、字段只有 id、无 `.tmp.*` 残留，锁由系统自动释放、可继续写。
8. **只跑并发/持久化专项**：

   ```bash
   uv run python -m pytest tests/test_concurrency.py tests/test_http_concurrency.py \
     tests/test_multiprocess.py tests/test_persistence.py -q
   ```
