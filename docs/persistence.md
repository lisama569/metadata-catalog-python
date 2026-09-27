# 本地持久化与多进程并发：语义与边界

本文档说明从"进程内内存模拟仓库"扩展为"**落地在本地磁盘、多进程共享**"后，
数据能恢复到什么程度、数据文件的兼容范围、多进程并发下哪些结论有保证、
哪些冲突/失败原因可区分，以及如何在本地验证。

> 持久化实现位于 `metadata_catalog/persistent_store.py` 的 `PersistentStore`，
> 它与 `LocalStore` 实现同一套存储接口，`MetadataCatalog` 的全部业务规则
> （登记/演进裁决、血缘成环、治理乐观锁）**完全复用、零改动**。

---

## 1. 怎么开启

```python
from metadata_catalog.catalog import MetadataCatalog
from metadata_catalog.persistent_store import PersistentStore

catalog = MetadataCatalog(PersistentStore("./data"))
```

HTTP 服务默认就把仓库放在当前目录的 `./data`：

```bash
uv run fastapi run main.py --host 127.0.0.1 --port 8000
# 指定其他目录：
METADATA_CATALOG_REPO=/path/to/repo uv run python main.py
```

多个服务进程/多个脚本只要指向**同一个目录**，就共享同一份数据。
不传目录时 `create_app()` 仍退回纯内存 `LocalStore`（旧行为不变）。

---

## 2. 数据文件布局与格式版本

仓库目录内容：

| 文件 | 作用 |
| --- | --- |
| `manifest.json` | **提交指针**。记录 `format_version` 与当前代际 `gen`，是"哪份状态已生效"的唯一权威 |
| `events-<gen>.log` | append-only 提交日志。每行一条完整 JSON（一个已提交事务的全部变更），以 `\n` 为提交边界 |
| `snapshots/snap-<gen>.json` | 压缩快照，含该代起点的全量资产/边与 `last_txn` |
| `lock` | 跨进程 `flock` 锁文件（EX 写 / SH 读），不承载业务数据 |

`format_version`（当前为 `1`）写在 manifest 与每个快照里。**磁盘布局发生
不兼容变更时会递增该版本号；旧版本文件会被明确拒绝加载并抛出
`StoreCorruptionError`，不会静默按错误格式解读。** 同版本内的向前兼容读取
遵循"缺字段取默认值"。数据文件是内部存储格式，不承诺跨大版本直接可读；
升级前请用当前版本先导出/迁移。

---

## 3. 重启后能恢复到什么程度（持久性语义）

### 3.1 恢复范围

进程退出后重新打开同一目录，下列数据**完整可读、结论不变**：

* 全部已登记资产及其**当前结构**；
* 每个字段的首次登记版本 `since_version`、资产的 `schema_version`；
* 标签集合、负责人、`governance_version`；
* 全部上下游血缘边与图方向，直接/传递影响范围结论一致。

"在另一个进程里打开同一目录"读到的是同一份数据（见第 4 节的跨进程可见性）。

### 3.2 提交原子性：要么写前、要么写后，没有半成品

一次写事务（一次登记 / 一次治理更新 / 一批血缘）的全部变更被序列化成
**一行 JSON**，通过"单次 `write` 追加 + `fsync` + 换行作为提交边界"提交：

* 读恢复只回放**以换行结尾的完整行**；
* 进程在 `write` 中途被杀，留下的是没有换行边界的"半行"，重启打开时
  会被裁掉（写者在排他锁内做），效果等同于该事务**从未发生**；
* 因此不可能出现"只写了一半的字段结构"或"版本号与字段内容对不上"。

### 3.3 "从未登记" 与 "登记过但写入被拒/失败" 严格可区分

* **从未登记**：`get_asset` 抛 `asset_not_found`，重启后依然是 not_found。
* **被业务拒绝的写入**（不兼容结构、成环、重复边、版本冲突、非法输入）：
  **不会向日志追加任何记录**。重启后资产仍保持拒绝前的结构与版本，
  绝不会"事后看起来生效了"。
* **写事务中途报错/被杀**：暂存的变更被丢弃，活动态从未被改动；
  磁盘上要么是写前状态、要么是写后状态。

### 3.4 快照压缩与崩溃恢复

事件日志超过阈值（默认 2000 条或约 8MiB）时，在排他锁内做一次压缩，
严格按以下顺序，崩溃发生在任意一步都能回到一份完整状态：

1. `snapshots/snap-<新代>.json` 走 临时文件 + fsync + rename 原子落盘；
2. 建立空的 `events-<新代>.log`；
3. **原子替换 `manifest.json` 推进代际指针**——这是唯一生效切换点；
4. 删除旧代日志/快照并 fsync 目录。

若崩溃发生在第 3 步之前，manifest 仍指旧代，新代临时/完整文件在打开时
被当作残留清理；发生在第 3 步之后，新代快照自包含、旧文件被清理。

### 3.5 持久性边界（如实说明）

* 提交以 `fsync` 落盘为界：已成功返回的写入在**操作系统崩溃/掉电**后仍可恢复；
* 存储介质本身损坏、用户手工删改数据文件不在保证范围内；文件被破坏到
  "越过最后提交边界仍能解析出半截但内容非法"时，会抛 `StoreCorruptionError`
  而不是静默返回错乱数据；
* 仓库目录不支持被网络文件系统（NFS 等）上语义不一致的多机共享，
  `flock` 与原子 rename 的保证针对**同一台机器的本地文件系统**。

---

## 4. 多进程并发下哪些结论有保证

跨进程互斥用 `fcntl.flock`：

* 写事务持 **排他锁（LOCK_EX）**，"追平日志尾部 → 业务检查 → 追加提交"
  在同一临界区内完成，消除跨进程 TOCTOU；
* 只读路径持 **共享锁（LOCK_SH）**，多个读者并发互不阻塞，仅与写者
  在短临界区互斥，不会出现大范围长时间互锁；
* 进程内另用一把可重入锁保护内存索引，支持同进程多线程嵌套使用。

### 4.1 有保证的结论

| 场景 | 保证 |
| --- | --- |
| 多进程并发首次登记同一资产 | 恰好 1 个 `created`，其余 `re_registered_idempotent`，最终 `schema_version=1`，**不重复加版本** |
| 多进程基于同一版本并发兼容演进 | 恰好 1 个成功推进版本；其余全部 `version_conflict`；冲突方重读再重试后，**每个字段都不丢**（最终一致） |
| 多进程基于同一治理版本更新 | 恰好 1 个成功，其余冲突；每次真实变化 `governance_version` 只 +1，重试后标签/负责人都保留 |
| 并发写同一对血缘边 | 仓库里只存一份；其余返回 `duplicate_edge`，不残留重复 |
| 并发写不同血缘边 | 全部存在；会成环的边被 `lineage_cycle` 拒绝，图始终是 DAG |
| 带过期版本写入 | **任何情况下**都是 `409 version_conflict`，绝不静默成功 |
| 一个进程提交后另一进程读 | 新打开或新事务的读者在持锁追平后能读到已提交数据 |

### 4.2 可区分的冲突/失败原因（机器可判定 `code`）

* `validation_error`（400）：非法输入；
* `incompatible_schema`（422，带 `reasons`）：结构演进被拒，**未写入**；
* `asset_not_found`（404）：引用从未登记的资产；
* `version_conflict`（409，带 `expected_version` / `actual_version`）：
  乐观锁过期，调用方应重读最新版本后自行重试；
* 血缘逐条结论 `accepted` / `duplicate_edge` / `lineage_cycle` /
  `asset_not_found` / `validation_error`（HTTP 200 + `accepted/code/reason`）；
* `StoreCorruptionError`：数据文件损坏或格式版本不兼容（打开期）。

### 4.3 隔离粒度（如实说明）

* 互斥粒度是**整仓的写事务**：同一时刻只有一个进程在提交，但临界区只覆盖
  "追平 + 检查 + 一次追加 + fsync"，常规读走内存索引/共享锁，因此在
  上万资产/边规模下读写都很快（见第 5 节实测用例）；
* 这是本地单仓仓库，不提供多机分布式事务、跨仓复制或远程访问；
* 血缘批量接口在一个写事务内顺序裁决，批次内前一条对后一条立即可见。

---

## 5. 本地怎么验证

```bash
uv run python -m pytest -s
```

本轮新增用例：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_persistence.py` | 重启后资产/结构版本/`since_version`/治理/血缘完整；从未登记 vs 被拒写入可区分且被拒不落日志；事务中途异常整体回滚；模拟进程被杀的日志半行被丢弃；批量血缘原子有序；压缩代际切换后一致；格式版本不兼容与已提交区损坏的明确报错 |
| `tests/test_multiprocess.py` | 用 `multiprocessing`（spawn）起**真实多进程** + `Barrier` 同发：并发首次登记单创建、并发演进无丢更新、并发治理单赢家且收敛、并发血缘去重、过期版本必冲突、提交后跨进程可见、读者/写者混合下始终读到自洽状态 |
| `tests/test_scale.py` | 1 万资产 + 1 万血缘边下的登记/逐条写/单资产查询/全量列举/万级传递影响/重启加载耗时阈值 |
| `tests/test_api.py`（新增用例） | HTTP 接入持久化目录、重建 app 后数据仍在、过期版本经 HTTP 为 409 |

上一轮的登记/血缘/治理/HTTP 用例（33 个）在 `PersistentStore` 与
`LocalStore` 同一套业务规则下继续通过，无回归。

### 手工验证

```bash
# 终端 A：起服务，登记并演进
uv run fastapi run main.py --port 8000
curl -s -X POST localhost:8000/assets/register -H 'content-type: application/json' \
  -d '{"name":"orders","asset_type":"table","fields":[{"name":"id","field_type":"long","nullable":false}]}'

# Ctrl-C 停掉，再启动（或另开终端 B 起第二个进程指向同一目录）
# GET /assets/orders 仍是之前的数据；两个进程并发改同一资产时，
# 只有携带正确版本的一方成功，其余收到 409。
```
