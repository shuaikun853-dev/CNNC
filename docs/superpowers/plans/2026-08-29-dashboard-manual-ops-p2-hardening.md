# Dashboard Manual Ops 生产后 P2 安全加固 — 实施计划

- 生产基线: `68832442fba9e2bf51e84618f45f7920bcdcb6cb` (`origin/prod/closeout-20260818`)
- 维护分支: `fix/dashboard-manual-ops-p2-hardening-20260829` (独立 worktree, 禁 merge/deploy)
- 性质: 小范围维护型收口; 不动模型匹配 / parallel / decoupling / Scheduler / SaaS / finalizer 主逻辑 / UI 大改 / 招商局建任务主流程

## P2-A: PRE_WRITE_FAILED 过度 fail-closed

### 审计结论 (代码证据)

`RestartStatus.PRE_WRITE_FAILED` 在 stop 路径上只有一个来源:
`_production_stop_running_task` 的 escape 转换器 `_restart_escape_outcome`
(`legacy_zhaoshang_adapter.py:2073`)。它仅在 `writes["disable"] is False` 且
`resume_phase is None` 时委托 `_pre_write_restart_failure_status` 产生
PRE_WRITE_FAILED; `writes["disable"]` 只在 `tool.disable_task()` 调用前一行
(`legacy_zhaoshang_adapter.py:2554`) 置 True, 且该调用的异常在原地被捕获并转成
DISABLE_UNKNOWN (re-observe) 或 escape 成 DISABLE_UNKNOWN。因此:

1. PRE_WRITE_FAILED ⟺ disable POST 从未开始 (zero remote write, 由代码结构证明)。
2. 不存在"已发出却标为 PRE_WRITE_FAILED"的生产路径 (注入 seam `_stop_runner` /
   `stop_callable` 仅测试可达; 生产 CLI/API 全部走默认 adapter)。
3. timeout-after-send / connection-reset-after-send / 未知响应 / 进程崩溃 → 全部
   DISABLE_UNKNOWN 或 durable STOP_SENT, 仍 reconcile-only。

### 现状问题

`dashboard_manual_ops._run_manual_rebuild_once` 的 stop 分支没有处理
PRE_WRITE_FAILED: 它落入 catch-all `else` → `stop_unconfirmed`, durable phase
留在 STOP_SENT。任务仍在运行 → 下一次点击走 STOP_SENT resume →
OLD_TASK_STOP_UNCONFIRMED (拒绝第二次 stop) → 永久死结。
Scheduler 侧 recovery (`remote_running_recovery.py:589-645`) 早已对等价场景
撤回 write-ahead DISABLE_SENT 并允许 fresh 重试; Dashboard 缺同构语义。

### 修复设计

在 stop 状态分派链中新增 `RestartStatus.PRE_WRITE_FAILED` 分支 (在
DISABLE_UNKNOWN 分支之后、catch-all 之前):

- `persist_action_phase("INTENT", error_text=f"stop pre-write failure: {diagnostic}")`
  —— 撤回 write-ahead STOP_SENT, 回到明确 zero-write 可重试状态 INTENT。
  resume-from-INTENT 路径已存在 (`dashboard_manual_ops.py:1090`), 会重新执行
  progress precheck → STOP_SENT → stop (fresh stop, 同一 action_id, 同一
  deterministic replacement name)。
- 返回 `REBUILD_REJECTED` + 新 error_code `STOP_PRE_WRITE_FAILED`, reason 说明
  zero-write 已证明、同一 action 可安全重试。
- DISABLE_UNKNOWN / DISABLE_UNCONFIRMED / 超时 / 异常 escape / crash 保持
  STOP_SENT + reconcile-only, 不变。

## P2-B: TASK_NAME_CONFLICT 死结

### 审计结论

`supersede_remote_job_to_replacement` 在事务内发现 replacement task_name 已被
其它 `remote_jobs` 行占用时抛 `RemoteIntentConflict("replacement task name is
already reserved")` (`daily_audit_store.py:1291-1296`), 冲突时零 durable 写入。
当前 handler (`dashboard_manual_ops.py:1291-1302`) 直接返回
TASK_NAME_CONFLICT 而 durable phase 停在 STOP_CONFIRMED / TERMINAL_PROOF
(active phase)。后续: 同 action_id 重试 → 同 deterministic name → 再冲突;
新 action_id → `create_rebuild_action` 的 active guard 拒绝
(REBUILD_ACTION_ACTIVE) → 永久死结 (需人工改库)。

supersede 只会在 stop 协议完成之后被调用 (phase ∈ STOP_CONFIRMED /
TERMINAL_PROOF), 此时旧任务的 stop 已被证实, 释放 action ownership 后新
action 的 precheck 只会走 EXTERNALLY_DISABLED (zero-write) 或 TERMINAL 路径,
不存在第二次 disable 的可能。

### 修复设计

- 在 `persist_action_phase` 闭包里维护 `current_phase` 局部变量 (每次 persist
  同步更新), 作为冲突时的 durable phase 事实来源。
- TASK_NAME_CONFLICT 分支: 若 `current_phase ∈ {INTENT, STOP_CONFIRMED,
  TERMINAL_PROOF}` (zero-write 或 stop-已证实, 无 ambiguous 远端写), 先
  `persist_action_phase("FAILED", error_text=...)` 再返回 (payload 增加
  `action_failed: True`); 否则 (STOP_SENT / CREATE_SENT 等歧义 phase, 防御性)
  保持现状 fail-closed 不释放。
- FAILED 不在 active guard 内 → 下一次新 action_id 生成新 deterministic name,
  正常重建; `already has a replacement` 分支 (ALREADY_REPLACED) 与其它
  RemoteIntentConflict 分支不变。

## P2-C: 历史业务日 action durable lookup

### 审计结论

`GET /dashboard/api/actions/{action_id}` 的 durable fallback 只查
`runtime_paths(_today()).run_dir` (`dashboard_api.py:373-374`)。跨业务日
(午夜后 / Dashboard 重启后) 查询昨日 action → 404。

### 修复设计

- `DashboardActionRunner` 新增 `durable_status_recent(action_id, *, today=None,
  lookback_days=7)`: 以 Shanghai 业务日 today 为锚, 依次在
  `self._runtime_root/<date>` 的 `dashboard_actions.jsonl` 里做现有
  `durable_status` 查找 (今天→今天-1→…→今天-(N-1)), 命中即返回。
- 端点改为调用 `durable_status_recent`; 日期序列全部服务端生成
  (`business_date_in_shanghai` + timedelta), 客户端无法注入任何路径;
  action_id 只用于 JSONL 记录等值匹配, 不接触文件系统。
- `durable_status` 单目录方法保留; `/api/actions` 列表端点维持今日范围 (现场
  控制台语义, 不扩)。

## 测试计划 (新增)

`tests/test_rebuild_crash_recovery.py` (复用 `_CrashSeed`):

- P2-A-1 stop 返回 PRE_WRITE_FAILED → error_code STOP_PRE_WRITE_FAILED,
  durable phase 回 INTENT, error_text 带 diagnostic。
- P2-A-2 同一 action 重试 (新点击) → 从 INTENT resume, 再次 fresh stop,
  最终 DISPATCHED; stop_calls==2, 同一 replacement name。
- P2-A-3 DISABLE_UNKNOWN(observed=True) → STOP_SENT + 重点击拒绝第二次 stop
  (回归保护)。
- P2-A-4 stop_callable 抛 TimeoutError → phase 留 STOP_SENT, 重点击
  reconcile-only 拒绝。
- P2-B-1 预埋同名 remote_jobs 行 → 第一次 rebuild TASK_NAME_CONFLICT 且
  durable phase FAILED (action_failed=True)。
- P2-B-2 冲突后新 action_id → EXTERNALLY_DISABLED (zero-write) → 新 name
  DISPATCHED; stop_disable_writes 不变。
- P2-B-3 同 action_id 幂等: FAILED 后同 id 重试 → 走 fresh protocol 不再
  REBUILD_ACTION_ACTIVE。
- P2-B-4 防御门: monkeypatch 抑制 STOP_CONFIRMED persist (维持 STOP_SENT) +
  supersede 抛冲突 → 不释放, durable 仍 STOP_SENT。
- P2-B-5 已有 replacement → ALREADY_REPLACED 不变。

`tests/test_dashboard_actions_api.py` / `tests/test_dashboard_action_runner.py`:

- P2-C-1 今日 action durable 查询 200 (现有语义保持)。
- P2-C-2 昨日业务日 run_dir 的 action → 200。
- P2-C-3 新进程 (无 handle) + 昨日记录 → 200 (重启可查)。
- P2-C-4 不存在 id → 404。
- P2-C-5 窗口外 (lookback+1 天前) → 404 (bounded 证明)。
- P2-C-6 action_id 含 `..`/路径样式 → 404 且不触碰窗口外目录 (无 traversal)。

## 回归

定向: manual rebuild / rebuild crash recovery / dashboard actions API /
action runner / manual re-export / progress probe / finalize superseded。
Production regression: crawl/model decoupling, model parallel,
planning_pending, title hard rules, scheduler/operations。最后 full pytest。
