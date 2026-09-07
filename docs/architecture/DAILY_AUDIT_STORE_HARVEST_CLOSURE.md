# DailyAuditStore Harvest Claim Closure 契约

状态：稳定边界（2026-09-07 建立，branch `refactor/daily-audit-store-boundary-20260907`）。
对象：`app/services/daily_audit_store.py` 中 `close_harvest_*` 方法族。

## 能力归属

"关闭一个已认领的收割（harvest claim）"是 Store 拥有的业务不变量，不是调用方协议。
recovery 通道（`app/services/recovery/harvest.py`、`app/services/recovery/wait_export.py`）
不再自行编排底层方法序列，必须通过三个关闭方法表达结果：

- `close_harvest_still_running` —— 有界等待只证明远端仍在执行：
  reconcile→RUNNING、推进非终态 crawl→RUNNING、按调用方限额结算。
- `close_harvest_manual` —— 人工复核结局：job 终态 MANUAL_REQUIRED + 诊断，crawl 不动。
- `close_harvest_exported` —— 导出收割：matched→`complete_crawl_work`、
  no_result→`complete_partial_exact_no_result`（固定 reason
  "provenance-valid remote request has no result row"）、reconcile(EXPORTED|COMPLETED)、
  manual crawl→`mark_remote_manual("harvest has unresolved provenance mappings")`
  +`mark_partial_exact_crawl_manual`、最后 `record_harvest`。

## Store 拥有的不变量（调用方不得假设也不得复刻）

1. 写序固定：matched → no_result → reconcile → manual 标记 → record_harvest。
2. COMPLETED 仅当零 manual crawl；否则 EXPORTED，随后 MANUAL_REQUIRED。
3. reconcile 必须先于 record_harvest —— 终态证据赢过重试上限
   （`record_harvest` 对 EXPORTED/COMPLETED 永不翻 MANUAL_REQUIRED）。
4. 终态 crawl 证据（SUCCESS/NO_RESULT/TERMINAL_FAILED）不可改写；远端 identity 不可变。
5. 每次写独立事务（与抽取前一致）；中途异常会留下部分写入，靠幂等重放恢复，
   不是回滚（特征化测试 `test_wrong_identity_rejected_after_crawl_writes_leaves_partial_state`
   如实冻结该语义）。

已知冻结怪癖：EXPORTED-with-manual 分支结尾的 `record_harvest` 不带 error，
会把 job 级诊断改写为 NULL（最终 MANUAL_REQUIRED + error_text NULL）。这是当前生产行为，
如需改变须独立任务 + 特征化测试同步更新。

## 调用方边界

- 需要"关闭收割声明"时用 `close_harvest_*`；只有新的、非收割语义的 job 推进
  （如 submit 结果、partial-exact 恢复流）才允许直接用 `reconcile_remote_job` /
  `advance_crawl_work_for_job` 等底层方法。
- 收割限额（max_attempts / max_elapsed_seconds / attempt headroom）仍是调用方策略，
  作为参数传入；Store 不拥有 headroom 规则。
- `app/workflows/incremental_price_audit.py::_harvest_batch` 的批量收割是
  evidence 驱动的不同协议（PRIORITY_PENDING 分支、按证据回退 identity），暂不迁移；
  迁移前不得假设它与本契约同构。

## 测试面

`tests/test_harvest_claim_closure_store.py`（14 用例）是本接缝的唯一权威测试面：
正常路径、终态保护、identity 不可变、SUPERSEDED 排除、幂等重放、跨 run 租约围栏、
限额交互。改 `close_harvest_*` 行为前先改这些测试（特征化优先）。
