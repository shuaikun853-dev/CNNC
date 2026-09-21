# CNNC Bug 排查说明书

> 本说明书是**优先导航，不是真理**。它帮助新 AI 少走弯路，但不把现象硬编码成唯一根因。路由中的证据都正常却仍解释不了故障时，允许扩大搜索范围，并回到当前代码、Git 历史、runbook 和测试重新建立假设。

## 先按这个顺序

1. 先确认业务日、批次目录、用户看到的原始现象和当前正式 SHA；不要先改代码。
2. 在下表按“用户看到的现象”选第一检查区域。
3. 先读权威状态和原始证据，再读 projection、日志和代码。
4. 用 exact identity 把同一个文档、URL、远端任务或 snapshot 串起来；不要只凭名称相似。
5. 证伪旧实现后再修根因；测试优先覆盖 module 的稳定 interface 和可观察行为。
6. 表内路线不能解释时，扩大搜索范围；不要为了迎合说明书而篡改事实。

状态冲突时先看 [状态归属说明](STATE_OWNERSHIP.md)，系统全景见 [ARCHITECTURE.md](../../ARCHITECTURE.md)。所有生产红线仍以 [AGENTS.md](../../AGENTS.md) 为准。

## 按现象路由

| 用户看到的现象 | 第一检查区域 | 先看什么证据 | 如果正常再查哪里 | 不要直接做什么 |
|---|---|---|---|---|
| **招商局任务已经完成，但 CNNC 没有及时回收** | STEP1 Remote Execution / Recovery。先在业务日 `state.sqlite3` 对齐 `remote_jobs`、`crawl_work`、`remote_identity` 和任务生产者。 | 远端只读 live progress 的六计数；本地 job state、harvest 次数、`result_path/export_path`、错误和 recovery evidence；scheduler log 是否实际跑过 harvest。若是 customer-prewarm，再核对 `customer_prewarm.sqlite3` 的 exact producer/link/payload linkage。 | 普通 STEP1 查 `app/services/recovery/harvest.py` 与 workflow `_harvest()`；prewarm 查 `customer_prewarm_lifecycle.py`、`customer_prewarm_cross_day_reconcile.py`；Dashboard 单独查 progress projection。 | 不要新建替代任务、盲目 export、直接改 SQLite，或因为远端已完成就跳过 identity/provenance 门禁。 |
| **出现 `SUBMIT_UNKNOWN`** | 招商局提交对账 module；`state.sqlite3.remote_jobs` 是本地 durable 状态入口。 | `post_attempted`、immutable payload hash、task name、remote identity、脱敏 submit diagnostic、最近 reconcile evidence；再按 exact identity 做只读远端查找。 | `legacy_zhaoshang_adapter.py` / `batch_zhaoshang_adapter.py` 的 `reconcile_unknown`，以及 workflow 的 unknown recovery；prewarm 还要查 owning-day linkage。 | **绝对不要再次 POST `/add`**，不要根据超时猜“失败”，不要用模糊任务名接管。 |
| **任务一直显示 RUNNING** | RUNNING stall / recovery。先分清“远端仍有进度”与“本地投影没更新”。 | live progress 的 completed/pending/running/failed/waiting_db、最近变化时间；`recovery_evidence_json`、probe 失败次数、restart phase、writer/scheduler 最近运行。 | `remote_running_recovery.py`、`remote_stall_breaker.py`、`zhaoshang_progress_probe.py`，以及 scheduler log 中的有限 tick/harvest。 | 不要只凭持续时间判死；不要重复 disable/enable、手动清状态或再建一份任务。既有自动恢复只允许严格门禁下的一次精确 restart。 |
| **Priority 没有确认成功 / 一直 PRIORITY_PENDING** | Priority 受控工具与 remote job durable evidence。 | 精确 task_name -> taskNumber + mall；add payload 有效子任务数；task id 集、`is_valid`、写前/写后 `weight_type=-1` 回读；`affected_rows`、`PRIORITY_UNCONFIRMED` reason/retryable。`task_status` 只作进度观察。 | `zhaoshang_priority_boost_integration/`、`legacy_zhaoshang_adapter.py`、prewarm lifecycle 的 priority recovery；最近对应测试是 `test_zhaoshang_priority_boost.py` 和 priority recovery contract。 | 不要手写宽 SQL、把 taskNumber 当唯一身份、用 task_status 过滤范围，或把 0 行更新报告成“已提优”。 |
| **Dashboard 显示和真实状态不一致** | Dashboard projection。先把“当前本地业务状态”“远端此刻进度”“历史日历摘要”分开。 | 当前业务日直接读 `state.sqlite3`；核对 `daily_status.json` 生成时间/指纹；远端行看 live progress 是否只是临时观测；检查 Dashboard startup/local/remote SHA。 | `dashboard_projection.py`、`prewarm_dashboard_projection.py`、`zhaoshang_progress_probe.py`、`production_identity.py`；若只是 14 天日历，注意它读的是 `daily_status.json`。 | 不要先改前端文案掩盖冲突，不要把 Dashboard 当 System of Record，不要让只读 probe 结果直接覆盖 SQLite。 |
| **Scheduler 没有正常执行** | Windows Task Scheduler + scheduled runner，不先归因业务 workflow。 | `Get-ScheduledTask` / `Get-ScheduledTaskInfo` 的存在、状态、LastRunTime、NextRunTime、LastTaskResult；`runtime/incremental_price_audit/scheduler_logs/<日期>.log` 的 RUN/START/END/STOP；writer lock 和 SQLite 是否出现对应 command checkpoint。 | `run_incremental_price_audit_scheduled.ps1` 的时段路由/预算，`register_incremental_price_audit_tasks.ps1` 的实际注册参数，`production_identity.py` / `production_readiness.py` 的只读查询。 | 不要因为当日 DB 缺失就断言 Scheduler 未触发；不要直接重注册、删除任务、杀进程或删除 lock。先证明是触发、runner、Python 还是业务门禁哪一层失败。 |
| **Auth / Token 失效，出现 401、`rspCode=3333` 或登录提示** | 对应认证 provider：中核看 `cnnc_auth_core.py` 与 `data/cnnc_auth_state.json`；SaaS/招商局看 `auth_manager.py` 与 `data/auth_state.json`。 | 只看脱敏 status、last_status/last_error/last refresh/verify；区分 HTTP 401、业务认证失败、HTTP 412 WAF、SSO_LOGIN_REQUIRED、deadline/cooldown。 | 发起请求的 snapshot/detail/adapter module；代理绕过、JSON 转义、请求头大小写；Dashboard auth projection。 | 不要打印 Token/Cookie，不要手拆 cURL，不要把 412 当 Token 失效循环刷新，不要对任何写请求做发送后自动重试。修改认证文件前遵守 `AGENTS.md` 授权规则。 |
| **最终 Excel 缺数据、行数或 Dashboard 计数不一致** | Freeze / Finalize。先验证“冻结输入 -> finalization intent -> workbook/manifest -> SQLite commit”整条证据链。 | `daily_runs.state/frozen_snapshot_id/final_output_path/final_manifest_hash/metrics_json`；snapshot row_count；最终 metrics；manifest hash、workbook SHA、实际 `record_results` 行数和缺价行数。 | `incremental_price_audit_finalize.py`、`incremental_audit_materializer.py`、`incremental_audit_status.py` 和 `test_finalize_failed_link_rows.py`；再查 prewarm failed-link 投影是否在冻结前进入。 | 不要从后来变化的 prewarm 数据重算已 finalized 指标，不要覆盖正式 Excel，不要只看文件存在就认为一致。 |
| **Prewarm 跨天任务没有正确吸收 / 旧任务一直留着** | Prewarm cross-day lifecycle。先确定 owning day，而不是只看当前日期或裸 job id。 | `customer_prewarm.sqlite3` 的 candidate、latest linkage、producer、lifecycle cursor/reconcile intent；owning-day `state.sqlite3.remote_jobs/crawl_work`；shared cache 是否已出现 exact `(mall, original_url)` SUCCESS。 | `customer_prewarm_cross_day_reconcile.py`、`customer_prewarm_lifecycle.py`、dispatcher 的 lifecycle reserve 和 owning-day lock；测试看 cross-day reconcile/production-shape。 | 不要把同号 job 跨天错绑，不要用裸 remote id 提优，不要在 MANUAL/不确定 NOT_FOUND 时自动 redispatch，也不要为“吸收”再 create。 |
| **本地 fallback 没触发** | Incremental Local Fallback。先验证它是否真的符合 eligible contract。 | 每日 SQLite 的 exact `crawl_work` + `local_fallback_work`：商城、完整 URL、远端终态、`last_remote_check_at`、eligible reason、attempt count、claim；当前时段、最终远端检查、crawler revision/cleanliness、剩余 tick budget。 | `incremental_local_fallback.py`、workflow 的 final remote check、scheduler 是否运行的是完整 tick/closeout（白天 harvest-only 不执行本地 fallback）。 | 不要手跑 child 绕过 claim，不要重置尝试次数，不要把不支持站点塞进 allowlist，不要让本地失败覆盖 remote `NO_RESULT/TERMINAL_FAILED` 证据。 |
| **STEP3 cache 命中异常、价格/标题/SKU/评论没有正确填充** | STEP3 cache consumer + Shared Crawl Cache。先按 mall + 完整 URL 查 current cache row。 | `sku_status=CONFIRMED`、合法真实 SKU、非空 title、正价格、channel/mall 一致；京东/苏宁还要 `comment_count` 或可信 `comment_count_raw`。再看 STEP3 批次 `prewarm_cache` summary 和输出清单。 | `step3_prewarm_cache.py`、`shared_crawl_cache.py`、`zhaoshang_result_adapter.py`、`process_monitor_no_push_zhaoshang.py` 及 `test_step3_prewarm_cache.py`。 | 不要用 `UPSTREAM_PROVIDED` 冒充实采 SKU，不要用客户标题补商城标题，不要让缺评论的京东/苏宁行进入完整 zhImport，也不要从 STEP3 反写缓存。 |
| **`BATCH_STATE.md` / `batch_state_snapshots` / `daily_status.json` 和 SQLite 不一致** | 先按状态归属分层，不把文件互相覆盖。对增量 STEP1，业务状态先读每日 SQLite。 | SQLite 最新 run/job/work/finalize 事务；最新 immutable `batch_state_snapshots/BATCH_STATE_*.md` 的 event 和 alias refresh；`BATCH_STATE.md` 是否因 Windows reader 更新失败；`daily_status.json` 是否比 SQLite 旧。 | `_record_batch_result()`、`write_daily_status()`、`collect_runtime_status()`；如果是 legacy STEP1/STEP2/STEP3，再查该批次日志与外部回读，因为这些流程没有同一套 daily SQLite。 | 不要用较新的文件 mtime 反推业务状态，不要为“对齐”回写 SQLite，不要删除 lock 或历史 snapshot；先明确是哪种流程和哪个 System of Record。 |

## 三个容易混淆的判断

### 1. 远端完成，不等于本地已安全完成

远端 live progress 可以证明招商局此刻报告已结束，但本地还需要 exact identity、导出、结果校验和 SQLite checkpoint。排查“未回收”时，两边都要看；不能让任一边单独替代另一边。

### 2. 页面新，不等于状态新

Dashboard 页面可能刚刷新，但某些区块来自便利快照或有 TTL 的只读 probe。当前业务状态仍要回到对应 SQLite；历史日历只是一层摘要。

### 3. `BATCH_STATE.md` 很重要，但不是增量 STEP1 业务数据库

它用于人和 AI 交接、记录不可重复动作及最近命令结果。增量 STEP1 明确以 SQLite 为权威；Windows 文件锁导致 alias 旧时，应看 immutable batch-state snapshot 和 SQLite，而不是反转已经 checkpoint 的命令。

## 证据来源

本路由由当前正式代码、`STEP_1_INCREMENTAL_SCHEDULER.md`、`STEP_1_DASHBOARD.md`、三步 runbook、`CNNC_BUG_HOTSPOT_AUDIT_20260823.md`、近期已合并修复 PR #26、#29-#35 及对应回归测试交叉确认。8 月 hotspot audit 只作为历史线索；当前 ownership 和入口以本次正式 SHA 的代码为准。
