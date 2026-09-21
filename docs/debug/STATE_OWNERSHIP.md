# CNNC 状态归属说明

> 目的：当 Dashboard 说 A、`BATCH_STATE.md` 说 B、SQLite 又说 C 时，先回答“这个具体问题最终应该相信谁”。这里不重新定义状态，只记录当前代码已经实行的 ownership；没有单一答案的地方明确标为 `CURRENTLY AMBIGUOUS`。

系统全景见 [ARCHITECTURE.md](../../ARCHITECTURE.md)，按现象排查见 [BUG_ROUTING.md](BUG_ROUTING.md)。

## 最短结论

- 增量 STEP1 当日业务 checkpoint：信该业务日的 `state.sqlite3`。
- Prewarm 候选、链接和跨日 lifecycle：信 `customer_prewarm.sqlite3`，但远端任务自身状态还要回 owning-day `state.sqlite3`。
- 已爬取 URL 身份和缓存成功证据：信 `shared_crawl_cache.sqlite3`。
- 远端此刻进度：信 exact identity 下的招商局只读回读；它是外部观测，不会自动改写本地状态。
- `daily_status.json`、Dashboard、`BATCH_STATE.md` 和 `batch_state_snapshots` 都不是增量 STEP1 的业务 System of Record。

## 问题 -> 最终应该相信谁

| 问题 | 最终应该相信谁 | 其他信息是什么性质 |
|---|---|---|
| 某业务日 STEP1 处于 OPEN、FINALIZING 还是 FINALIZED？ | `runtime/incremental_price_audit/<业务日>/state.sqlite3` 的 `daily_runs`。 | `daily_status.json`、Dashboard 和 BATCH_STATE 都是派生显示或交接。 |
| 某个 Detail / Model / crawl 工作是否完成？ | 同一业务日 `state.sqlite3` 的 `stage_work` / `crawl_work`，并按 frozen 或 latest snapshot membership 解读。 | 日志是过程证据；最终 Excel 是发布结果，不是工作队列。 |
| CNNC 是否已经尝试远端 POST，能否安全重试？ | `state.sqlite3.remote_jobs` 的 immutable intent、`post_attempted`、payload hash、remote identity 和 durable evidence。 | HTTP 错误、stdout 和 BATCH_STATE 是辅助证据；`SUBMIT_UNKNOWN` 时不能靠它们授权重发。 |
| 招商局此刻完成了多少？ | exact remote identity 的只读 live progress。 | Dashboard live progress 是带缓存/限流的 projection；本地 SQLite 保存 CNNC 已确认并 checkpoint 的状态，两者需 reconcile。 |
| Priority 是否真正成功？ | 受控 Priority 工具的精确 task-id 集回读：`is_valid` 与 `weight_type=-1`；本次“已提优”还必须有 `affected_rows>0`。 | `task_status` 只是采集进度；SQLite 的 `PRIORITY_PENDING/UNCONFIRMED` 是本地安全状态，不替代远端 DB 精确回读。 |
| freeze 使用了哪一批输入？ | `state.sqlite3.daily_runs.frozen_snapshot_id` 指向的 `snapshots` + `snapshot_members`。 | 最新 SaaS 数据、Prewarm 后续变化和 Dashboard 不能改变已冻结 membership。 |
| 最终 Excel 是否是已发布的那一份？ | SQLite 的 `final_output_path/final_manifest_hash/final metrics` 与磁盘 workbook + manifest 的 hash/row-count 联合证据。 | “文件存在”或 Dashboard 显示 finalized 单独都不够。 |
| Prewarm 哪些文档/候选/链接待处理？ | `customer_prewarm.sqlite3` 的 `prewarm_documents`、`prewarm_candidates`、`prewarm_links` 和 daily observations。 | Prewarm Dashboard 是只读 projection；Shared Crawl Cache 只回答是否已有可复用结果。 |
| Prewarm 候选绑定了哪个历史远端任务？ | `customer_prewarm.sqlite3` 的 latest exact producer linkage，且必须与 owning-day `state.sqlite3.remote_jobs` 的 identity/payload 共同证明。 | 单独的裸 job id、当前日同号任务或 UI 文案都不能证明归属。 |
| 某 URL 是否已有可复用爬取结果？ | `shared_crawl_cache.sqlite3` 的 identity/alias ownership 和 current success row。 | `customer_prewarm.sqlite3.cache_status=CACHE_HIT` 是候选已吸收结果的状态；STEP3 输出是一次消费结果。 |
| STEP3 是否能把缓存当完整结果？ | Shared Crawl Cache 的 current row + `step3_prewarm_cache.py` 的消费 contract：mall/URL、CONFIRMED SKU、标题、正价格，京东/苏宁还需评论证据。 | Prewarm 的 CACHE_HIT 不能单独证明 STEP3 字段齐全。 |
| Scheduler 是否注册、何时跑过、返回什么？ | Windows Task Scheduler 的实际任务定义和 `Get-ScheduledTaskInfo`。 | 注册脚本代表期望配置；scheduler log 证明 runner 过程；SQLite 只证明业务命令实际 checkpoint。 |
| 当前生产代码身份是什么？ | 需要同时看 Dashboard startup SHA、生产 worktree local SHA 和 `origin/prod/closeout-20260818` remote SHA。 | GitHub PR 状态、某个开发 worktree 或单一 SHA 不能冒充部署身份。 |

## 各状态载体的性质

### 每日 `state.sqlite3`

**System of Record：增量 STEP1 某一业务日的 durable checkpoint。**

它保存 `daily_runs`、snapshots、audit items、stage work、remote jobs、crawl work、local fallback ledger 和受控替换/重建记录。写入由 ProcessLock + fenced lease + SQLite transaction 保护。排查时优先使用 read-only 打开方式，不手改数据库。

它不负责：跨日 Prewarm 候选总账、Shared Crawl Cache 历史、Windows Task Scheduler 实际配置、外部系统此刻状态。

### `customer_prewarm.sqlite3`

**System of Record：跨业务日的客户参考链接发现、候选路由、candidate linkage 和 lifecycle cursor。**

它回答“哪个文档发现了哪个链接”“候选目前属于哪个 prewarm 状态”“最新 exact producer linkage 指向哪里”。它不是远端 job row 的唯一来源，也不是最终页面证据缓存。

### `shared_crawl_cache.sqlite3`

**System of Record：跨日 crawl identity、URL alias ownership、append-only 成功历史和 current cache row。**

它回答“这个 mall + URL 当前有没有可信的爬取证据”。它不回答该候选是否仍属于今天的工作、不拥有远端 task lifecycle，也不能替代 STEP3 的字段完整性 contract。

### `daily_status.json`

**非权威 convenience snapshot。**

`write_daily_status()` 在有限命令结束后原子发布一次 `collect_runtime_status()` 结果。它可能比 SQLite 旧；Dashboard 的 14 天历史日历读取它，但当前业务日 summary 会重新从 SQLite 采集。冲突时不反向覆盖 SQLite。

### `BATCH_STATE.md`

**人和 AI 的 handoff / coordination 记录。**

在增量 STEP1 中，代码明确写着 “SQLite remains authoritative”；BATCH_STATE 是 best-effort compatibility alias。它可以记录最近动作、授权、输出和下一步，但不能推翻已经事务提交的业务状态。

对 legacy STEP1、STEP2、STEP3 和受控专项批次，BATCH_STATE 往往还是“已完成且不得重复”动作的主要本地交接证据，必须与日志、输出和外部回读一起判断。

### `batch_state_snapshots/BATCH_STATE_*.md`

**不可变的人类状态快照，不是业务数据库。**

每个增量命令在可能的情况下写一个唯一 snapshot；当 Windows reader 锁住 legacy `BATCH_STATE.md` 时，它仍可记录命令结果和 alias refresh 失败。它比 compatibility alias 更可靠地保留历史，但状态内容仍来自命令结果/SQLite checkpoint。

### Dashboard projection

**只读 projection。**

当前 STEP1 summary 从 SQLite 收集，Prewarm 页面从三库只读组装；14 天日历来自 `daily_status.json`；Scheduler 来自只读 PowerShell；远端 live progress 来自带 TTL/single-flight 的只读 probe。Dashboard 可以比某个便利文件更及时，也可能因 probe、缓存或部署 SHA 过旧而降级，不能成为写回 authority。

### Live remote progress

**外部系统在某一观测时刻的事实。**

它最适合回答“远端现在跑到哪里”，但不能回答“CNNC 已经安全 checkpoint 了什么”“这个 task 是否属于当前 immutable intent”。只有通过 exact identity/provenance 门禁后，workflow 才能把观测吸收到本地 SQLite。

## `CURRENTLY AMBIGUOUS`

### 1. Prewarm 跨两个 SQLite 的原子 ownership

`CURRENTLY AMBIGUOUS`：一个 prewarm 远端任务的 lifecycle 同时涉及 owning-day `state.sqlite3` 的 remote/crawl rows，以及 `customer_prewarm.sqlite3` 的 candidate/linkage/cursor。当前测试和代码用 exact identity、producer、payload、fencing 和恢复流程收敛它们，但两个数据库之间没有跨文件 ACID 事务。

**冲突时做法：** 不任选一个库覆盖另一个库。先找 owning day，验证 latest exact linkage + remote identity + immutable request group，再走现有 reconcile/harvest；无法证明就保持 MANUAL/fail-closed。

### 2. Legacy 三步流程的外部写与本地交接

`CURRENTLY AMBIGUOUS`：legacy STEP1 和 STEP3 的真实招商局动作、以及少量受控 SaaS 写，并没有全部进入每日 `state.sqlite3` 的同一事务模型。BATCH_STATE、日志、输出 manifest 和外部回读共同构成证据，但当前没有一个覆盖全部动作的单一本地 System of Record。

**冲突时做法：** 以外部 exact identity/readback 判断真实副作用，以 BATCH_STATE/日志判断本地已尝试动作；任何发送结果不确定的写都不得自动重试。

### 3. Dashboard 历史日历的新鲜度

`CURRENTLY AMBIGUOUS`：Dashboard 当前日核心 summary 重新读 SQLite，但 14 天 recent-days 分类只读各日 `daily_status.json`。如果某天 SQLite 后来发生了合法恢复而 convenience snapshot 没再次发布，历史小格可能滞后。

**冲突时做法：** 历史日历只用于导航；点进具体业务日后回到 SQLite 和 artifact 证据，不据此做生产写操作。

## 冲突处理口诀

1. 先问具体问题属于“本地 durable 决定”“外部此刻状态”“缓存证据”还是“显示”。
2. 找到对应 System of Record，再用 exact identity 串证据。
3. projection 可以提示异常，不能反向创造事实。
4. 跨库或发送结果不确定时 fail-closed，保留原始证据，不靠人工改库消除矛盾。
