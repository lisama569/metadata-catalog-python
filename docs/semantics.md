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

同一仓库无论处于**同进程多线程**、**同机多进程**还是**真实 HTTP 并发**，
遵守完全相同的一套结论。锁与快照规则：

- 读之间可以同时进行（共享读锁）；写与一切操作互斥（排他写锁）。
  服务层的"检查 + 写入"始终在**同一个排他临界区**内完成，无 TOCTOU 窗口。
- 每个线程的事务模式与重入深度是**线程本地状态**，一个线程绝不会因为
  另一个线程持锁而"免锁"进入临界区；跨进程互斥在同一 `lock` 文件上以
  `flock` 完成，并对进程内多读者做引用计数（同一进程只真正加一次
  共享锁，最后一个读者释放）。
- 任何写入都先在一份**全新的快照对象**上形成完整下一状态，落盘成功后
  才替换仓库内对象（写时复制）；写失败时旧快照原封不动。读出来的快照
  是独立副本，调用方无法通过持有的引用污染仓库。
- 并发用相同结构登记同一资产：恰好 1 个 `created`，其余全部
  `re_registered_idempotent`，最终 `schema_version=1`；重复的幂等治理
  提交也不推进版本。
- 携带 `expected_schema_version` / `expected_governance_version` 时，
  若与当前版本不一致，返回 `409 version_conflict`（含
  `expected_version` / `actual_version`），**不写入**。并发下 N 个基于
  同一旧版本的写：恰好 1 个成功，其余全部冲突，最终版本只 +1，字段
  内容不会拼花。调用方重新读取最新版本后重试即可收敛。
- 读者永远只可能读到"某一份完整快照"：结构版本、字段集合、标签负责人
  与 `governance_version` 始终相互对得上，不会观察到版本已推进但内容
  仍旧/残缺的中间态。

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
  （下次打开仓库时在跨进程排他锁内自动清理，临时文件从不以正式名出现）；
- rename 之后崩溃：正式文件已是写入后的完整版本。

磁盘上永远不会出现"只写了一半"的资产结构，也不会出现"版本号已推进但字段内容
还是旧的/残缺的"这类版本与内容对不上的状态。单次写操作具备故障原子性：
重启后读到的要么是写入前、要么是写入后的完整状态。

**同进程可见性（与重启后一致）**：写入路径采用写时复制——领域层先在一份
全新的快照对象上构造完整下一状态，存储层**先完成原子落盘，再把缓存条目
替换为新快照**。所以当落盘在 rename 前抛错（磁盘写失败、中途报错）时：

1. 仓库的进程内缓存里仍然是写入前那份完整旧快照，本进程随后的读读到的
   就是旧状态，失败的结构/版本/标签/血缘**当场不可见**；
2. 紧接着的一次**无关更新**（别的资产演进、或同一资产的治理写）落盘时
   写出的仍是"旧结构 + 它自己的新治理"，不会把失败结构顺带带出——
   失败快照从来没有进入过缓存，没有任何对象可"夹带"；
3. 进程重启/换进程重开后结论相同（磁盘上本就只有旧文件）。

存储层把落盘阶段的 IO 故障归类为 `storage_error`（它同时是 `OSError`
子类）：调用方既能按存储错误码处理，也能用 `except OSError` 捕获；
HTTP 层映射为带 `code` 的 500。注意它与 `version_conflict`/
`incompatible_schema` 的区别——后两者是"请求被裁决为无效、从未尝试
落盘"，前者是"尝试落盘但没成功、仓库保持写入前状态"；两种情况下仓库
都不存在半成品。

> 说明：这是**单操作级**的原子性（不是跨多资产的事务日志）。批量血缘接口中
> 先通过的边会独立生效，被拒的边不生效，这与第 3 节既有语义一致。

### 5.3 多进程并发下的保证

并发控制分两层，缺一不可：

1. **进程内**（`locking.py`）：一把写者优先的可重入读写锁。事务模式
   （无/共享/排他）与重入深度保存在**线程本地**——这是同进程结论正确的
   前提：持锁状态不能放在共享字段上，否则一个线程会误读另一个线程的
   状态而免锁穿过临界区。
2. **跨进程**：`fcntl.flock` 在同一 `lock` 文件上提供共享/排他锁
   （POSIX；Linux/macOS 等）。flock 与打开文件描述关联、由整个进程
   共享而非按线程计数，因此所有 flock 获取/释放都在一把进程内互斥量
   内串行并配合引用计数：同一进程第一个读者加 `LOCK_SH`、最后一个读者
   `LOCK_UN`；写者由进程内写锁保证是唯一持锁线程，直接 `LOCK_EX`。

服务层的"检查 + 写入"始终在同一个跨进程排他临界区内完成，因此多进程下
与单进程结论**逐条一致**：

- 多个进程并发用相同结构首次登记同一资产：恰好 1 个 `created`，其余
  `re_registered_idempotent`，最终 `schema_version=1`，**不会重复累加版本**；
- 多个进程基于同一 `expected_schema_version`/`expected_governance_version`
  并发改同一资产：**恰好 1 个成功，其余全部返回可区分的 `version_conflict`**
  （携带 `expected_version` / `actual_version`），最终版本只 +1，不丢更新、
  不出现字段拼花；
- 多进程并发写同一条血缘边：恰好 1 个 `accepted`，其余 `duplicate_edge`，
  磁盘上只保留一份；成环检查同样在排他临界区内，跨进程也不会产生环或重复残留；
- 任何携带期望版本的写入，只要版本对不上就一定是冲突，**任何情况下都不会被
  静默当成成功**（HTTP `409`，异常含 expected/actual）。

**读不阻塞读**：查询走共享锁，多进程/多线程的读彼此完全并行；只有写需要排他
锁。每次进入最外层临界区时先在锁内增量同步磁盘变化（见 5.4），所以一个进程
提交后，另一个进程（以及同进程的其他线程）在下一次临界区一定能读到。读者在
写者推进版本的前/中/后三个时刻读到的，都只会是结构版本与字段内容相互对得上
的完整快照。

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
  `storage_corruption`；写入/读取/清理等磁盘操作中途的 IO 故障抛
  `storage_error`（同时是 `OSError` 子类）。这三类都属于**存储故障**
  （HTTP 500，各自 code 可区分），与业务拒绝（400/404/409/422）严格
  区分，调用方可按 `code` 分别处理；
- 任何未被上述分类覆盖的意外异常，HTTP 层还有一道
  `internal_error`(500) 的最后防线，保证调用方拿到的永远是结构化结论，
  而不是没有 `code` 的裸服务端错误；
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
| `tests/test_governance.py` | owner/标签增删替换、幂等无变化、乐观锁冲突、冲突后重试、**多线程并发**治理写、**多线程并发结构演进只一个成功**、并发同结构登记幂等、并发血缘重复边、读出快照与仓库内部状态相互独立 |
| `tests/test_api.py` | HTTP 状态码与错误 `code` 映射、批量接口、404/409/422、重启后 HTTP 读取完整、损坏文件 `500 storage_corruption`、**写盘失败 `500 storage_error` 且失败写不生效**、坏请求体 `400 validation_error` |
| `tests/test_concurrency.py` | **持久化仓库上的同进程真实线程**：持续并发读+突发并发写（快照自洽、读零错误）、同版本结构竞争、重复操作不重复推进版本、**失败写入后同进程继续读/无关写/重启均不可见**、存储故障归类 |
| `tests/test_http_concurrency.py` | **真实 uvicorn + httpx 的 TCP 并发**：并发读+治理写（读不超时/无错误，写恰一成功其余 409）、并发登记/血缘、过期结构版本并发写全部 409 |
| `tests/test_persistence.py` | **磁盘重启完整恢复**、被拒/失败写入不生效、"从未登记 vs 登记后失败"可区分、rename 前抛错/被 kill 不留脏数据与临时文件、FORMAT 版本兼容检查 |
| `tests/test_multiprocess.py` | **真实多个 OS 进程**（spawn）打开同一仓库：并发治理只一个成功其余冲突、并发登记只一次 created、并发血缘只一份边、并发演进只一次生效且不拼花、**多读者进程与写者进程并发期间快照始终自洽**、他进程写入对已开仓库可见 |
| `tests/test_mp_failure.py` | **子进程在 rename 前被 kill**：随后的无关写与第三个进程重启都看不到失败结构、锁可继续使用；多进程基于同一过期 sv 并发演进恰一成功 |
| `tests/test_scale.py` | 10 000 资产 + 5 000 边下暖缓存点查/传递遍历/单笔写入耗时护栏、冷启动装载耗时 |

并发与持久化用例的关键做法：

- 多线程用例使用 `Barrier`/统一起跑事件同时发起写入，断言"成功数恰为
  1 / 冲突或重复数为 N-1 / 最终版本只推进一次"；混合用例额外断言每个
  读者拿到的快照"结构版本 ↔ 字段集合 ↔ 治理版本"始终自洽；
- HTTP 用例在随机端口起真实 `uvicorn.Server`（非 TestClient 内联调用），
  由多个 httpx 客户端线程发真实请求，覆盖网络服务形态；
- 多进程用例使用 `multiprocessing` 的 **spawn** 上下文拉起独立解释器进程，
  每个进程独立构造 `PersistentStore`（独立 flock 客户端与独立缓存），这才是
  "多个服务实例操作同一本地仓库"的真实场景；
- 中断用例通过故障注入点（环境变量 `METADATA_CATALOG_CRASH_AFTER_WRITE`）在
  "临时文件写完、rename 之前"分别制造抛错与 `os._exit`（模拟被 kill），
  再在同进程继续读、做无关写、并重新打开断言状态完整且无残留；
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
7. **真实 HTTP 并发读写（可直接 shell 复现）**：起好服务、登记 `hot` 后，
   一个终端持续读、另一个终端并发发起 N 个同版本治理写：

   ```bash
   # 终端 A：持续读，期间不应出现非 200，gv 只会在 0/1 之间跳变一次
   while true; do curl -s localhost:8000/assets/hot \
     | python -c 'import sys,json;b=json.load(sys.stdin);print(b["governance_version"], b["schema_version"], len(b["fields"]))'; done

   # 终端 B：16 个基于 gv=0 的并发写，恰好一个 200，其余 409
   seq 0 15 | xargs -P16 -I{} curl -s -o /dev/null -w '%{http_code}\n' \
     -X PUT localhost:8000/assets/hot/governance \
     -H 'content-type: application/json' \
     -d '{"expected_governance_version":0,"owner":"u{}"}' | sort | uniq -c
   # 期望输出：1 个 200，15 个 409
   ```

8. **失败写入同进程/重启均不可见（故障注入复现）**：

   ```bash
   export METADATA_CATALOG_REPO=./demo_crash
   uv run python - <<'PY'
   import os
   from metadata_catalog.api import create_app
   from metadata_catalog.persistence import PersistentStore
   from metadata_catalog.catalog import MetadataCatalog
   from metadata_catalog.models import *

   repo = os.environ["METADATA_CATALOG_REPO"]
   c = MetadataCatalog(PersistentStore(repo))
   c.register(RegisterRequest(spec=AssetSpec(name="t1", asset_type=AssetType.TABLE,
       fields=[FieldSpec("id", FieldType.LONG, False)])))
   c.register(RegisterRequest(spec=AssetSpec(name="t2", asset_type=AssetType.TABLE,
       fields=[FieldSpec("id", FieldType.LONG, False)])))
   c.store.close()

   # 制造一次 rename 前失败
   os.environ["METADATA_CATALOG_CRASH_AFTER_WRITE"] = "raise"
   c = MetadataCatalog(PersistentStore(repo))
   try:
       c.register(RegisterRequest(spec=AssetSpec(name="t1", asset_type=AssetType.TABLE,
           fields=[FieldSpec("id", FieldType.LONG, False),
                   FieldSpec("name", FieldType.STRING)]),
           expected_schema_version=1))
   except OSError as e:
       print("写入失败被明确抛出:", type(e).__name__, getattr(e, "code", None))
   del os.environ["METADATA_CATALOG_CRASH_AFTER_WRITE"]

   # 同进程继续读：仍是写入前
   s = c.get_asset("t1")
   assert s.schema_version == 1 and set(s.fields) == {"id"}
   # 无关写：不会带出失败结构
   c.update_governance(GovernanceUpdate(asset="t1",
       expected_governance_version=0, owner="alice"))
   assert c.get_asset("t1").schema_version == 1
   c.store.close()

   # 重开（等价重启）：结论一致，且无 .tmp 残留
   c2 = MetadataCatalog(PersistentStore(repo))
   s = c2.get_asset("t1")
   assert s.schema_version == 1 and s.owner == "alice"
   print("重启后 t1:", s.schema_version, s.owner, sorted(s.fields))
   c2.store.close()
   PY
   ```

   模拟进程在写入中途被 kill 时，把注入值改为 `kill`（子进程会以退出码
   42 退出）；随后重新打开仓库，状态同样完整停留在写入前。这套故障注入
   的自动化版本见 `tests/test_persistence.py` 与 `tests/test_mp_failure.py`。
