# STEP_1 Remote RUNNING Stall Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 只有能证明真正 stalled 的 exact remote job，才自动执行一次、且仅一次禁用→启用恢复；恢复后仍失败则进入明确的 RECOVERY_EXHAUSTED 本地终态并停止 harvest，让当天 ordinary final 收敛。

**Architecture:** 新增 `app/services/remote_running_recovery.py`（判定+协议+终态），`DailyAuditStore` 增两列与三个受控转移方法，`CnncLegacyZhaoshangAdapter` 增 `probe_running_task`/`restart_running_task`（复用外部工具既有接口，不修改外部工具），`run_incremental_tick` 挂一个受 budget 门控的 recovery pass，finalize/status 做 RECOVERY_EXHAUSTED 兼容。

**Tech Stack:** Python 3.14, pytest, SQLite（WAL）, 依赖注入（无新第三方依赖）。

**Spec:** `docs/superpowers/specs/2026-08-25-step1-remote-running-stall-recovery-design.md`

## Global Constraints

- BASE_SHA=6efdc79c5bb10605f05f2931066a833f986952e5；开发分支 fix/step1-remote-running-stall-recovery-20260825。
- 严禁修改：科力普/colipu crawler、multi_mall_crawler、mall mapping、Scheduler 脚本、STEP_2、STEP_3、force-finalize ≤10 策略、外部招商局工具、生产 SQLite（测试用 tmp_path）。
- 普通 RUNNING + count=0 绝不直接 NO_RESULT；strict NO_RESULT resolver（EXPORT_NO_PRICE_TERMINAL / EXPORTED_NO_MATCHING_ROW）语义不变。
- 不伪造远端 COMPLETED；RECOVERY_EXHAUSTED 是本地放弃语义。
- 所有远端写（disable/enable）必须精确身份四要素一致（task_name、mall、task_id、task_number），且最多一次。
- TDD：每个行为先 RED 再 GREEN。测试一律 fake/mocked adapter，不触生产任务。
- 提交前 `git status --short`、`git diff --check`；三个独立 commit。

---

### Task 1 (COMMIT 1): Stall detection — durable evidence + 纯逻辑判定

**Files:**
- Modify: `app/services/daily_audit_store.py`（schema migration + `update_remote_recovery_evidence`）
- Create: `app/services/remote_running_recovery.py`
- Modify: `app/workflows/incremental_price_audit.py`（TickDependencies 两个可选回调 + run_incremental_tick 挂 pass，本 commit 只 probe 记录不重启）
- Test: `tests/test_remote_running_recovery.py`

**Interfaces (Produces):**
- `remote_running_recovery.PROGRESS_FIELDS = ("total","pending","running","failed","waiting_db","finished")`
- `canonical_progress_signature(progress: Mapping) -> tuple[int, ...] | None`
- `evaluate_running_stall(job: Mapping, *, now: datetime) -> dict`（keys: `probe_candidate`, `stalled`, `exhausted_ready`, `reason`）
- `run_running_recovery_pass(store, token, *, probe_remote, restart_remote=None, now, budget_can_start_heavy) -> dict`（counters）
- `DailyAuditStore.update_remote_recovery_evidence(token, job_id, evidence: Mapping) -> dict`
- TickDependencies 新字段：`remote_recovery_probe`, `remote_recovery_restart`（默认 None）

Steps:

- [ ] **1.1 RED**：写 `tests/test_remote_running_recovery.py`，覆盖任务书 CASE 1-9（Job53 stalled / Job88 not / progress 增长 not / 刚开始 not / count=0 但 progress 变 not / SUBMIT_UNKNOWN not / MANUAL_REQUIRED not / recovery_attempted 不再 restart / 旧 job 缺 evidence 不误判）。纯函数测试直接构造 job dict（`recovery_evidence_json` 为 JSON 字符串或 None）。
- [ ] **1.2 RED 验证**：`python -m pytest tests\test_remote_running_recovery.py -q` → 全部 FAIL（模块不存在）。
- [ ] **1.3 GREEN**：实现 `remote_running_recovery.py` 的 signature/evaluate 与 evidence JSON 读写（`decode_recovery_evidence`, `append_progress_observation`）。实现 store migration（两列）与 `update_remote_recovery_evidence`。运行测试至绿。
- [ ] **1.4 RED→GREEN（挂点）**：测试「tick 在 harvest 后对 probe_candidate 调 probe 并持久化 observation；probe 回调缺失时 pass 为 no-op」。实现 TickDependencies 字段 + `run_incremental_tick` 中 `trace.append("running_recovery")` 段（受 `budget.can_start_heavy()` 门控；仅当 job stalled 且 `restart_remote` 可用才调 restart——本 commit restart_remote 恒 None，断言零写）。
- [ ] **1.5 回归**：`python -m pytest tests\test_incremental_price_audit.py tests\test_daily_audit_store.py -q` 全绿。
- [ ] **1.6 Commit**：`feat(remote): detect stalled running zhaoshang jobs`

### Task 2 (COMMIT 2): One-shot exact restart

**Files:**
- Modify: `app/services/legacy_zhaoshang_adapter.py`（`probe_running_task`, `restart_running_task` + `RunningTaskProbe`/`LegacyRestartOutcome` dataclass + `RestartStatus` enum）
- Modify: `app/services/remote_running_recovery.py`（`_execute_restart` phase 状态机 + reconcile 续行）
- Modify: `app/services/daily_audit_store.py`（`mark_remote_recovery_attempted`）
- Modify: `app/services/incremental_audit_runtime.py`（legacy bridge 组装 probe/restart 闭包；`build_default_dependencies` 在 enable_zhaoshang 时接 restart）
- Test: `tests/test_remote_running_recovery.py`（B 组用例 7-13）

**Interfaces (Produces):**
- `CnncLegacyZhaoshangAdapter.probe_running_task(job, task_id, task_number, timeout_seconds) -> RunningTaskProbe`（exact lookup + identity 校验 + status/progress/export；export 有结果文件时 `export_had_result=True`）
- `CnncLegacyZhaoshangAdapter.restart_running_task(job, task_id, task_number, timeout_seconds, *, resume_phase=None) -> LegacyRestartOutcome`（status ∈ {RESTARTED, IDENTITY_CONFLICT, NOT_RUNNING, DISABLE_UNKNOWN, DISABLE_UNCONFIRMED, ENABLE_UNKNOWN, ENABLE_UNCONFIRMED, ALREADY_DISABLED}）
- `DailyAuditStore.mark_remote_recovery_attempted(token, job_id, *, remote_identity, evidence) -> dict`

Steps:

- [ ] **2.1 RED**：B 组测试（fake probe/restart 回调）：stalled+exact identity→disable 一次→confirm→enable 一次→confirm→recovery_attempts=1 durable；模糊/冲突 identity→零远端写；disable unknown→先 reconcile 不盲重发；enable unknown→同；restart 完成后 durable=1；新进程（重开 store）仍读到 1；recovery_attempts==1 永不第二次 restart。
- [ ] **2.2 RED 验证**：运行→FAIL。
- [ ] **2.3 GREEN**：adapter 两个方法（生产路径复用外部工具函数：`check_existing_task`/`get_task_identity`/`disable_task`/`enable_task`/`query_task_status`/`query_task_progress`/`exportTask`，构造与 `_production_export_no_price_probe` 相同的 session 环境；`session_environment` 两键齐全校验）；recovery 模块 phase 状态机（写前持久化 DISABLE_SENT/ENABLE_SENT；unknown→下次 pass reconcile；超 15min 确认窗仍矛盾→MANUAL_REQUIRED fail-closed）；store `mark_remote_recovery_attempted`；runtime bridge 组装。至绿。
- [ ] **2.4 回归**：`python -m pytest tests\test_legacy_zhaoshang_adapter.py tests\test_incremental_audit_runtime.py tests\test_incremental_audit_operations.py -q` 全绿。
- [ ] **2.5 Commit**：`fix(remote): restart stalled zhaoshang job once`

### Task 3 (COMMIT 3): RECOVERY_EXHAUSTED terminal path

**Files:**
- Modify: `app/services/daily_audit_store.py`（`mark_remote_recovery_exhausted`, `complete_crawl_work_recovery_exhausted`）
- Modify: `app/services/remote_running_recovery.py`（exhausted_ready 判定 + 收口动作）
- Modify: `app/workflows/incremental_price_audit_finalize.py`（remote 分类 + `_valid_no_result` 第三 evidence_type）
- Modify: `app/services/incremental_audit_status.py`（`remote.recovery_exhausted` 计数）
- Test: `tests/test_remote_running_recovery.py`（C/D 组用例 14-26）

Steps:

- [ ] **3.1 RED**：C/D 组测试：restart 后下一 harvest 拿到价格→SUCCESS 不 EXHAUSTED；restart 后仍 stalled+no-price→RECOVERY_EXHAUSTED；EXHAUSTED 不进 harvest 候选；linked crawls→NO_RESULT 带完整 evidence 不造价格；EXHAUSTED→ordinary final 收敛；SUBMIT_UNKNOWN 仍 block；MANUAL_REQUIRED 仍 block；普通 RUNNING 仍 block；普通 count=0 strict NO_RESULT 不变；慢任务不因时长被 exhaust；已有 NO_RESULT resolver 测试全过；force-finalize 测试全过；FINALIZED immutable 测试全过。
- [ ] **3.2 RED 验证**：运行→FAIL。
- [ ] **3.3 GREEN**：store 两个方法（严格前置校验+terminal immutable）；recovery 收口；finalize 分类（evidence 完整才接受，否则 remote_invalid）+`_valid_no_result` 新分支（校验 remote_state/recovery_attempt/export_count）；status 新增 additive 字段。至绿。
- [ ] **3.4 回归+全量**：`python -m pytest -q` 全绿。
- [ ] **3.5 Commit**：`fix(remote): stop harvesting after failed stall recovery`

### Task 4: Verification + review + 发布

- [ ] `superpowers:verification-before-completion`：fresh `python -m pytest -q` 记录 exit code/passed/skipped/duration。
- [ ] `superpowers:requesting-code-review`：独立 reviewer 按 12 项重点审查；Critical/Important 必须解决。
- [ ] `git fetch origin` 复核 `origin/prod/closeout-20260818` 未移动；移动则停止并标记。
- [ ] `git push -u origin fix/step1-remote-running-stall-recovery-20260825`；`ls-remote` 校验 LOCAL_HEAD==REMOTE_HEAD。严禁 push prod/main、严禁 merge。
- [ ] 更新 runbook/TESTING 中受影响段落（STEP_1_INCREMENTAL_SCHEDULER 的状态枚举说明 + TESTING 增量测试命令行加新测试文件）。
