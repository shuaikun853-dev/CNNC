# 实施计划：预爬生命周期收口（A 治本三件套 + B 失败进最终表）

依据 spec：docs/superpowers/specs/2026-09-16-prewarm-lifecycle-closeout-design.md
分支：feat/prewarm-lifecycle-closeout-20260916（基线 prod/closeout-20260818@c1a90cb 之后）

## 任务拆解（TDD，RED→GREEN→REFACTOR）

### WS-A1+A2+A3：生命周期阶段

**新文件** `app/services/customer_prewarm_lifecycle.py`：

- `plan_lifecycle_actions(state_db_path, prewarm_store, business_date, mappings, *, reconcile_dead_threshold=4, redispatch_daily_cap=2) -> dict`
  纯只读（mode=ro）规划：返回 `{harvest_job_ids, reconcile_jobs:[{job_id, recovery_attempts}], redispatch_groups:{mall:[candidate...]}, redispatch_blocked:[{candidate_id, reason}]}`。
- `run_reconcile_phase(store, token, get_adapter, paths, *, deadline, monotonic_value, summary, dead_threshold=4) -> None`
  （pass 内、lease 已持）对当日 SUBMIT_UNKNOWN 无身份 zhpq job：`_reconstruct_prepared_job` → `adapter.reconcile_unknown` → FOUND 则 `store.reconcile_remote_job(identity, 'PRIORITY_PENDING')` + `advance_crawl_work_for_job` + `_confirm_pending_priority_job`；否则 `recovery_attempts`+1（DailyAuditStore 需有对应写方法则用，无则新增窄方法 `bump_remote_job_recovery_attempt`）；≥阈值记 phase note 判死。
- `run_redispatch_phase(store, token, prewarm_store, paths, mappings, get_adapter, *, business_date, deadline, monotonic_value, max_jobs_per_pass, summary) -> None`
  （pass 内）查死 job 候选（REMOTE_* + remote_job_id → job state ∈ {MANUAL_REQUIRED, TERMINAL_FAILED, SUBMIT_UNKNOWN 判死}；跨日 job 用 prewarm_remote_job_links 的 business_date 判归属，跨日死 job 同样可重排——新 job 建在当日）→ 按商城分组、按 cap 过滤 → 复用 `_dispatch_mall_group` 创建链（entries 形状同 Phase 2）。
- `run_harvest_phase(state_path, lock_path, business_date, *, deadline, monotonic_value, summary, per_job_timeout_seconds=90.0, max_jobs=3) -> None`
  （pass 外、锁已释放后由 CLI 调）逐 job 调 `harvest_existing_customer_prewarm_job`（原语自带锁）。
- 常量：`RECONCILE_DEAD_THRESHOLD=4`、`REDISPATCH_DAILY_CAP=2`、`HARVEST_PER_PASS=3`、`RECONCILE_PER_PASS=6`。

**修改** `app/services/customer_prewarm_dispatch.py`：

- `_plan_only_report` 追加 `lifecycle_planned`（plan_lifecycle_actions 结果摘要，零写）。
- `_run_leased_pass` Phase 4 后追加 reconcile + redispatch（canary_scope 非 None 时整体跳过，记 phase note `lifecycle skipped in canary mode`）。
- summary 新计数键：`reconciled_found`、`reconcile_not_found`、`redispatched_urls`、`redispatch_jobs`、`redispatch_blocked`、`phase5_errors`（并入 phase_errors 亦可，取实现简单者，但 canary 键集合不得变化）。

**修改** `scripts/customer_prewarm_dispatch.py`：

- 新参 `--no-lifecycle`（默认执行生命周期）；live 非 canary 路径在 `run_prewarm_dispatch_pass` 返回后、budget 允许时调 `run_harvest_phase`。
- 两行式输出追加 `reconciled=… redispatched=… harvested=…`。

**测试** `tests/test_customer_prewarm_lifecycle.py`（先写，先 RED）：

1. plan：活体 job 进 harvest 集；SUBMIT_UNKNOWN 进 reconcile 集；MANUAL_REQUIRED/判死 job 的候选进 redispatch 分组；重排超限（links≥2）进 blocked。
2. reconcile FOUND：身份写入+PRIORITY_PENDING+prioritize 确认调用（fake adapter 记录）。
3. reconcile NOT_FOUND×4 → recovery_attempts 递增、第 4 次后不再进 reconcile 集。
4. redispatch：ensure_crawl_work 幂等返回既有行、重绑新 job、linkage 记录、cap 生效。
5. harvest：fake 原语调用计数、budget 截断、RUNNING 在途不报错。
6. canary 模式跳过全部生命周期。
7. plan_only 报告含 lifecycle_planned 且零写（沿用现有零写断言风格）。

### WS-B：失败行进最终表

**修改** `app/workflows/incremental_price_audit_finalize.py`（落点以实际代码为准，可能是结果合并/工作簿组装处）：

- 新函数 `failed_customer_link_rows(business_date, runtime_root) -> list[dict]`：只读查 prewarm store + 当日 state DB，产出失败链接行（商城/链接/单据/失败原因/末次任务名/时间），口径=spec B。
- 在最终表行组装处按**既有行格式**追加这些行，结果列="爬取失败"（+原因）。

**测试** `tests/test_finalize_failed_link_rows.py`：失败行结构与成功行同列；无失败时零追加；既有行字段零改动（哈希对照）。

### 顺序与回归

1. WS-A 测试 RED → 实现 GREEN。
2. WS-B 同。
3. 回归：`python -m pytest tests/ -k "prewarm or dispatch or finalize" -q --basetemp=E:\_zcode_tmp\pytest_tmp_lc` + 相关全量；生产验证仅 --plan-only。
4. 提交拆分：A 一个 commit、B 一个 commit；推送 + PR（READY_FOR_REVIEW，merge 归 reviewer）。

### DO_NOT（实现期间红线）

- 不改 canary 键集合与既有计数键语义；不动主看板按钮谓词；不实现 ofs/ycw 脚本调用；不写 C 盘；不部署不 merge。
