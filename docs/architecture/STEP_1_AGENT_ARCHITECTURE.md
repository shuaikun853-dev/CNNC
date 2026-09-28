# CNNC STEP_1 Agent Architecture Contract

> 目的：给修 Bug、做优化、Code Review、生产排障的 AI Agent 提供一个**可检索、可约束、可做影响分析**的第一步架构契约。
>
> 本文描述当前生产主路径：**增量 STEP_1 + Windows Task Scheduler + SQLite durable checkpoint + 招商局远端任务 + 晚间 closeout/finalize**。legacy 全量入口只作为对照/rollback，不是本文主拓扑。

## 0. 权威顺序与适用范围

创建依据：`prod/closeout-20260818 @ 077a5428c3a9d02c2e57fed4d12ffac498346482`。

Agent 必须按以下顺序判断事实：

1. `AGENTS.md`：全局授权与生产红线。
2. `CURRENT_STATE.md` / 目标批次 `BATCH_STATE.md`：批次并发与实时状态。
3. `docs/runbooks/STEP_1_PRICE_AUDIT.md` 与 `docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md`：业务运行规则。
4. **当前代码**：实现事实。若代码与本文冲突，以当前代码 + 当前 runbook 为准，并把“架构文档已漂移”作为本次变更的一部分修正。
5. 本文和 `STEP_1_AGENT_ARCHITECTURE.json`：用于快速定位边界、依赖、不变量和影响范围。

固定业务 scope：

- `tenant_id = 188605`
- `commodity_pool_id = 222`
- 自动 Scheduler 默认周一至周五运行。

---

## 1. 一句话架构

STEP_1 是一个**有限执行、可恢复、SQLite checkpoint 驱动**的价格审计流水线：Scheduler 周期唤醒 CLI；每次命令获取单 writer 锁和 fenced lease 后，从 SaaS 发现权威快照；**先规划/提交招商局爬取，再处理 Detail 客户价和 Model 同款校验**；所有外部动作前后都把 durable state 写入 SQLite；白天用 tick/harvest-only 持续推进，18:00 后 closeout 重复调用同一 tick 引擎，满足 source stability 后 freeze snapshot，继续 drain 未完成工作，readiness 全部通过后原子 materialize `final.xlsx`。

---

## 2. 主拓扑

```text
Windows Task Scheduler
        |
        v
run_incremental_price_audit_scheduled.ps1
        |
        v
scripts/incremental_price_audit.py
        |
        v
incremental_audit_runtime.py
        |
        +-------------------- single writer ---------------------+
        |     ProcessLock + fenced SQLite lease + IgnoreNew      |
        +---------------------------------------------------------+
        |
        v
recover durable claims / reconcile ambiguous submit state
        |
        v
opening HARVEST existing remote jobs
        |
        v
DISCOVER authoritative SaaS + customer snapshot
        |
        v
persist snapshot / audit_items / snapshot_members
        |
        +---------------------- intentionally independent ----------------------+
        |                                                                     |
        v                                                                     v
SUBMIT PATH                                                           LOCAL STAGE PATH
plan crawl from discovered row                                        DETAIL customer price
  -> crawl_work                                                       MODEL title match
  -> immutable remote intent                                          retry/checkpoint
  -> Zhaoshang add
  -> exact task identity
  -> priority
  -> remote_jobs
        |                                                                     |
        +-------------------------------+-------------------------------------+
                                        v
                               optional post HARVEST
                                        |
                                        v
                              running recovery pass
                                        |
                                        v
                                  CHECKPOINT
                                        |
                       +----------------+----------------+
                       |                                 |
                  next tick/harvest                 evening closeout
                                                         |
                                      repeated same tick engine
                                                         |
                                      source stability >= 600s
                                      row_count + ordered_hash stable
                                                         |
                                                      FREEZE
                                                         |
                                      no more source discovery
                                      keep Detail/Model/Submit/Harvest
                                                         |
                                           FinalizeReadiness gate
                                                         |
                                                   materialize
                                                         |
                                      saas_audit_final_YYYYMMDD.xlsx
                                                         |
                                                   FINALIZED
```

### 最关键依赖约束

**招商局 crawl submit 不依赖 MODEL 结果，也不依赖 DETAIL 结果。**

当前 `app/workflows/incremental_price_audit.py` 明确把 `submit` 放在 `detail`、`model` 前面，原因是爬取请求只来自发现行本身的 `extLink / vendor mapping / __crawl_* policy`。因此：

```text
DISCOVER -> SUBMIT
DISCOVER -> DETAIL
DISCOVER -> MODEL

禁止恢复成：
DISCOVER -> DETAIL/MODEL -> SUBMIT
```

任何改动如果让 MODEL 状态成为“是否建招商局任务”的 eligibility gate，都属于**架构回归**。

---

## 3. 调度与命令面

### 3.1 Scheduler

入口：

- `scripts/run_incremental_price_audit_scheduled.ps1`
- Task name: `CNNC-PriceAudit-Incremental`
- Multiple instances: `IgnoreNew`

当前主要节奏由 `docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md` 定义：

- 白天整点：`tick`
- 白天非整点：`harvest-only`
- 17 点高峰：更高频 `tick`
- `17:55`：只 `harvest-only`
- `18:00-20:00`：`closeout`
- `20:35`：Safety Closeout

不要在架构代码中重新实现第二套常驻 daemon。Scheduler 只是唤醒**有限进程**。

### 3.2 CLI

主入口：`scripts/incremental_price_audit.py`

核心命令：

- `tick`
- `harvest-only`
- `drain-only`
- `closeout`
- `finalize`
- `status`
- `lock-status`
- `doctor`
- recovery / manual operator 子命令

CLI 只做参数与 runtime composition；业务状态机在 workflow/service 层。

---

## 4. Tick 执行顺序

主实现：`app/workflows/incremental_price_audit.py::run_incremental_tick`

当前阶段顺序：

1. `ProcessLock.acquire()`
2. `DailyAuditStore.ensure_run()`
3. fenced lease
4. `recover`
   - 恢复被中断的 stage claim
   - reconcile ambiguous remote submission
5. `harvest`
   - sweep 已存在 remote jobs
6. `discover`
   - 仅未 frozen 且本 pass 不 skip discovery 时执行
7. `submit`
   - crawl planning
   - durable `crawl_work`
   - immutable `remote_jobs` intent
   - remote create / reconcile
8. `detail`
9. `model`
10. `post_harvest`
   - 本轮有新 submit 且预算允许时
11. `running_recovery`
12. `checkpoint`
13. backlog/outcome 计算
14. metrics 持久化
15. release lease / ProcessLock

**不要为了“看起来更顺”把 submit 移到 model 后面。** 当前顺序本身就是一个吞吐优化和故障隔离设计。

---

## 5. 外部系统与职责

| 系统/组件 | 作用 | 是否写外部系统 | 关键实现 |
|---|---|---:|---|
| SaaS pending source | 发现当前待审数据、形成 authoritative snapshot | 否 | `cnnc_saas_snapshot_adapter.py` / runtime discover |
| CNNC customer/detail | 补客户价业务证据 | 否 | `customer_price_semantics.py` / runtime detail |
| Title Match Model | 标题同款判断 | 模型调用 | `title_match_model_adapter.py` / MODEL stage |
| 招商局 | 建任务、启用/提优、等待、导出页面价 | **是** | legacy/batch adapter + priority tools |
| SQLite | durable checkpoint / machine truth | 本地写 | `daily_audit_store.py` |
| Dashboard | 状态观测 + 受控人工命令 | 读 + 受控命令 | `dashboard_projection.py`, `dashboard_actions.py`, `dashboard_manual_ops.py` |
| Excel Materializer | frozen snapshot 最终物化 | 本地文件写 | `incremental_audit_materializer.py` / finalize workflow |

---

## 6. SQLite：machine truth

业务日路径：

```text
runtime/incremental_price_audit/<business_date>/
  state.sqlite3                 <- 唯一状态权威
  incremental_price_audit.lock <- OS writer lock
  BATCH_STATE.md                <- Agent/人类交接状态
  daily_status.json             <- 原子便利快照，不是权威
  saas_audit_final_YYYYMMDD.xlsx
```

`DailyAuditStore` 的核心流水线表：

| 表 | 责任 |
|---|---|
| `daily_runs` | 每个 business date 的总状态、freeze、final、metrics、lease |
| `snapshots` | 每次 authoritative observation，保存 `ordered_hash`、`row_count`、证据 |
| `audit_items` | durable 业务行身份 |
| `snapshot_members` | snapshot 顺序、成员和 `NEW/CHANGED/...` 关系 |
| `stage_work` | DETAIL / MODEL durable work、attempt、retry、result |
| `remote_jobs` | 招商局 immutable intent、remote identity、状态、harvest evidence |
| `crawl_work` | 每个商城/sku/url 的爬取工作及结果 |

Dashboard 手动 rebuild 协议还使用：

- `remote_job_replacements`
- `remote_job_rebuild_actions`

因此做 schema、状态统计、Dashboard、recovery 变更时，不能只假设“永远只有七张表”。

### 状态读取规则

- **SQLite 是 machine truth。**
- `daily_status.json` 是命令结束后的便利投影。
- `BATCH_STATE.md` 是 Agent 接管/生产动作防重复证据，不代替 SQLite 业务状态机。
- Dashboard projection 应从 durable state 构造，不允许页面自己发明第二套状态。

---

## 7. 并发、锁与 crash safety

三层防重叠：

1. Windows Task Scheduler `IgnoreNew`
2. `runtime_root + business_date` 的 `ProcessLock`
3. `DailyAuditStore` fenced DB lease

关键语义：

- 同一 business date 任意时刻最多一个 production writer。
- `tick` / `closeout` / `finalize` / `drain-only` / `harvest-only` 共用 writer lock。
- 外部操作前后都 checkpoint；下一个有限命令可以继续。
- remote create 使用 durable immutable intent。
- POST 结果不确定时进入 `SUBMIT_UNKNOWN` / reconciliation，**不得因为“可能失败”直接重发 create**。
- `FINALIZED` 后，tick/closeout/finalize 必须在发 SaaS/招商局/remote submit 请求前 NOOP。

修改这些区域必须同时评估：幂等、重入、重复建任务、重复导出、lease takeover、进程硬停后的恢复。

---

## 8. Closeout / Freeze / Finalize

### 8.1 Closeout 不是第二套状态机

实现：`app/services/incremental_audit_closeout.py`

closeout 只是反复调用 `run_incremental_tick(... finalize=True ...)`，并控制两个节奏：

- authoritative source observation：默认最短间隔 300 秒
- remote full sweep：默认最短间隔 600 秒

所有业务操作仍走原 tick + SQLite checkpoint + final materializer。

### 8.2 Freeze 条件

未 frozen 时，最新 authoritative snapshot 满足：

- 本次 discovery complete
- `snapshot.row_count == saas_count`
- `row_count + ordered_hash` 连续稳定至少 `source_stability_seconds`（生产默认 600 秒）

则 freeze 最新 snapshot。

`customer_count` 是独立观测指标，不是 freeze equality gate。

Frozen 后：

- 不再 discover 新 source
- 继续推进 DETAIL / MODEL / SUBMIT / PRIORITY / HARVEST / recovery
- final workbook 必须基于 frozen snapshot

### 8.3 FinalizeReadiness

实现：`app/workflows/incremental_price_audit_finalize.py`

普通 finalize 主要阻塞类别包括：

- `detail_pending`
- `model_pending`
- `model_terminal`
- `crawl_pending`
- `crawl_terminal`
- `crawl_unplanned`
- `crawl_invalid_price`
- `remote_pending`
- `remote_manual`
- `remote_invalid`
- 客户价 `missing / failed / invalid / absent_proven`

**不要只看一个 `final_pending` 数字判断根因。** 排障必须展开 readiness/backlog 的具体桶。

---

## 9. Dashboard 架构：Read Model 与 Command Plane 分开

过去把 Dashboard 简化成“纯只读”已经不准确。

### 9.1 Read Model

`app/services/dashboard_projection.py`

职责：

- 从 durable state / status 生成页面展示
- 不应自己实现另一套 STEP_1 状态机

### 9.2 Manual Command Plane

`app/services/dashboard_actions.py`

Dashboard POST action 只做：

```text
validate
  -> durable intent: dashboard_actions.jsonl
  -> spawn existing scripts/incremental_price_audit.py CLI
```

支持动作：

- `closeout`
- `re-export`
- `rebuild`

它**不实现第二套业务状态机**；锁、幂等、reconcile 应继续复用现有 CLI/service。

`app/services/dashboard_manual_ops.py` 的人工重新导出/重建必须针对 exact durable remote job：

- re-export：永不 create
- rebuild：使用 durable supersede/replacement 协议
- exact `remote_identity`
- immutable payload
- linked crawl evidence
- ProcessLock + durable lease
- fail closed

因此 Dashboard Bug 修复要先判断它属于：

```text
展示投影问题      -> dashboard_projection/status
人工动作编排问题  -> dashboard_actions
远端任务恢复问题  -> dashboard_manual_ops / recovery core
真正流水线问题    -> runtime/workflow/store/adapters
```

---

## 10. 不变量（Architecture Invariants）

以下规则在做 Bug 修复或优化时默认视为**不可破坏约束**；若业务确实要改变，必须显式说明并同步 runbook、架构文档和测试。

### INV-01  SQLite authority

`state.sqlite3` 是 STEP_1 machine truth；JSON/Markdown/Dashboard 都是投影或交接证据。

### INV-02  MODEL 不得阻塞 crawl eligibility

MODEL 结果不是生成/提交招商局 crawl 的前置条件。

### INV-03  Crawl 只由发现行决定

crawl planning 使用 discovered row 的商城、sku、URL 和 crawl policy；不能偷偷依赖 MODEL/DETAIL 结论。

### INV-04  Single writer

同 business date 生产写必须经过 ProcessLock + fenced lease；Scheduler IgnoreNew 是额外保护，不可替代内部锁。

### INV-05  Burn-before-send / reconcile unknown

remote POST 的 intent/attempt 证据必须先 durable；发送结果不确定不得自动创建第二个逻辑任务。

### INV-06  Frozen source immutability

freeze 后不得继续 source discovery 或替换 frozen snapshot；final 只能基于 frozen evidence。

### INV-07  FINALIZED idempotency

FINALIZED 后自动/手工 tick、closeout、finalize 必须 NOOP，不重写 final.xlsx，不再产生新 remote submit。

### INV-08  Closeout reuse

closeout 不得复制一套 DETAIL/MODEL/SUBMIT/HARVEST 业务实现；必须复用 finite tick engine 与 materializer。

### INV-09  Dashboard reuse

Dashboard operator actions 只能编排既有 CLI/service，不得在 HTTP handler 内复制业务状态机。

### INV-10  Exact remote identity

re-export/rebuild/priority/harvest 必须以 durable exact identity 和当前任务证据操作；不得用模糊任务名或 taskNumber 单条件猜测。

### INV-11  Customer price fail-closed

客户价缺失、失败、无效或确定缺失证据属于 finalize blocker；不得用列表价或 `0` 冒充有效客户价。

### INV-12  Scope fixed

任何诊断或优化不得放宽 `tenant_id=188605 / commodity_pool_id=222` 范围。

---

## 11. Bug 排查地图

| 症状 | 第一检查点 | durable state | 主要代码 |
|---|---|---|---|
| Dashboard 新增数量不动 | discover 是否产生新 snapshot | `snapshots`, `snapshot_members`, metrics | snapshot adapter / dashboard projection |
| SaaS 页面有数据但 STEP_1 没发现 | pagination/complete/evidence | `snapshots` | `cnnc_saas_snapshot_adapter.py`, runtime discover |
| 客户价没取到 | DETAIL stage + customer-price business state | `stage_work(stage=DETAIL)` | runtime, `customer_price_semantics.py` |
| 模型没跑/很慢 | MODEL pending/attempt/concurrency | `stage_work(stage=MODEL)` | workflow model batching / model adapter |
| 招商局没建任务 | submit pass 是否运行、crawl 是否可规划 | `crawl_work`, `remote_jobs` | `_submit`, crawl planner, adapter |
| 招商局建了但页面看不到 | remote identity / submit reconcile | `remote_jobs` | adapter / submit reconciliation |
| 招商局任务卡住 | remote state / priority / running recovery | `remote_jobs`, `crawl_work` | harvest / progress / recovery |
| 导出无结果 | exact job harvest evidence | `remote_jobs`, `crawl_work` | harvest / no-result recovery |
| 点重新导出无效 | dashboard action + exact job eligibility | `dashboard_actions.jsonl`, `remote_jobs` | `dashboard_actions.py`, `dashboard_manual_ops.py` |
| 点重建后重复任务 | rebuild protocol / replacement linkage | `remote_job_rebuild_actions`, `remote_job_replacements` | `dashboard_manual_ops.py` |
| 18 点后一直不 freeze | snapshot stability / row_count / saas_count | `snapshots`, `daily_runs` | closeout + `source_stability_seconds` |
| freeze 了但不出最终表 | readiness 具体 blocker | all state tables | finalize workflow |
| `final_pending` 看不懂 | 展开 backlog/readiness buckets | stage/crawl/remote | status + finalize workflow |
| 进程关窗口后任务停 | Scheduler Action / shared console / runner | scheduler log | scheduled PS1 + registration script |
| 多个 trigger 冲突 | IgnoreNew + ProcessLock + lease | lock/lease | scheduler + DailyAuditStore |

---

## 12. Agent 修改前的影响分析模板

任何涉及 STEP_1 的 Bug/优化，先回答下面问题，再改代码：

```text
[1] 变更节点
- Scheduler / CLI / Runtime / Discover / Submit / Detail / Model /
  Harvest / Recovery / Store / Closeout / Finalize / Dashboard ?

[2] 输入与输出
- 读哪些 durable 状态？
- 写哪些 durable 状态？
- 调哪些外部接口？

[3] 依赖是否改变
- 是否新增 MODEL -> SUBMIT 之类的业务 gate？
- 是否改变 freeze/readiness 语义？

[4] 幂等与故障恢复
- 在外部 POST 前 crash 会怎样？
- POST 后结果未知会怎样？
- 下一 tick 会继续还是重复？

[5] 并发
- 是否仍经过 ProcessLock + lease？
- 是否可能和 Scheduler / Dashboard action 重叠？

[6] Dashboard
- 改的是 read projection 还是 command plane？
- 有没有复制第二套状态机？

[7] 验证
- 需要哪些单测/回归测试？
- 如何证明没有重复建任务/重复提交/提前 finalize？

[8] 回滚
- 代码回滚后 SQLite 中已写入的新状态能否被旧代码理解？
```

---

## 13. 变更时必须同步的文件

当以下语义变化时，不能只改代码：

| 变化 | 必须同步 |
|---|---|
| Scheduler 时间/命令 | `STEP_1_INCREMENTAL_SCHEDULER.md` + 本文 + tests |
| Tick phase / 并发依赖 | 本文 + `STEP_1_AGENT_ARCHITECTURE.json` + tests |
| SQLite schema/state | 本文 + JSON + migration/tests |
| Freeze/Finalize gate | runbook + 本文 + JSON + tests |
| Dashboard action | 本文 + dashboard tests |
| 外部 write/retry 语义 | `AGENTS.md`/runbook（按影响范围）+ 本文 + tests |

---

## 14. 机器可读版本

同目录：

`docs/architecture/STEP_1_AGENT_ARCHITECTURE.json`

Agent 需要做拓扑分析、自动影响分析或检查不变量时优先读取 JSON；需要理解业务原因、排障方法和安全语义时读取本文。
