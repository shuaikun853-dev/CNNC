# priority_boost 提优异常 raise 站点审计（只读，2026-09-04）

范围：`zhaoshang_priority_boost_integration/zhaoshang_priority_boost.py`（基线 077a542）。
任务：列出全部 `PriorityUpdateZeroRowsError` / `PriorityRowsNotMaterializedError` /
`PriorityRowsIncompleteError` raise 站点，按
ROWS_NOT_MATERIALIZED / ROWS_INCOMPLETE / UPDATE_ROWCOUNT_MISMATCH /
LOCK_SET_DRIFT / POST_COMMIT_READBACK_FAILED / 其他 分类。只审计，不改代码。

## 异常层级（:46-56）

```
PriorityUpdateZeroRowsError(RuntimeError)            # :46 裸基类
├── PriorityRowsNotMaterializedError                 # :50 有界查找零行
└── PriorityRowsIncompleteError                      # :54 有界查找行数不足
```

`except PriorityUpdateZeroRowsError` 会同时捕获两类子类；历史上持久化的裸
类型名字符串（adapter `REMOTE_NOT_MATERIALIZED_LEGACY_DIAGNOSTIC`）因此无法
区分零行/校验失败——这正是 re-export/rebuild 活体证明 fallback 的背景。

## raise 站点清单

| 行号 | 异常类型 | 消息锚点 | 分类 | 判断依据 |
|---|---|---|---|---|
| :166 | `PriorityUpdateZeroRowsError`（裸基类） | `"no matching rows"` | **其他**（空集防御守卫，语义随调用点漂移） | 位于 `_validate_identity_rows`，行集为空时抛。三个调用点：:336 锁定集读取（窗口查到行后在锁定读中消失 → 语义近 LOCK_SET_DRIFT）、:406 事务内重读、:430 提交后读回（空 → 语义近 POST_COMMIT_READBACK_FAILED）。消息不带 adapter 锚定前缀（`no matching rows for mall/task_number/create_time window`），`remote_not_materialized_diagnostic` 判 None → 恒 fail-closed 手工，不会被误分类为零行未物化 |
| :315 | `PriorityRowsNotMaterializedError` | `"no matching rows for mall/task_number/create_time window"` | **ROWS_NOT_MATERIALIZED** | 有界重试查找循环（`max_select_retries=60`、`retry_sleep_seconds=2`，每次 `conn.rollback()` 刷新 REPEATABLE READ）走完后 matched=0，即调度端延迟物化未发生 |
| :319 | `PriorityRowsIncompleteError` | `"incomplete subtask rows: matched=N expected=M"` | **ROWS_INCOMPLETE** | matched>0 但 < `expected_subtask_count`，行集部分物化 |
| :342 | `PriorityUpdateZeroRowsError`（裸基类） | `"target set changed before priority lock: matched=… expected=…"` | **LOCK_SET_DRIFT** | 写事务内按主键 `SELECT … FOR UPDATE` 锁定的行数与窗口查找集不一致（:341 `len(before_rows) != expected_subtask_count`）：窗口查找与锁定读之间目标集被并发修改 |
| :394 | `PriorityUpdateZeroRowsError`（裸基类） | `"priority update affected unexpected row count: affected=… candidates=…"` | **UPDATE_ROWCOUNT_MISMATCH** | 分批 `UPDATE … SET weight_type='-1'` 的累计 rowcount ≠ 候选主键数（:393 `affected_rows != len(update_ids)`）：更新落点与预期主键集不符 |
| :418 | `PriorityUpdateZeroRowsError`（裸基类） | `"priority verification failed: exact target rows are not -1"` | **其他**（UPDATE 后事务内读回校验失败，commit 前） | 同事务内重读目标行 `weight_type` 仍非 -1（:413-420），commit(:421) 尚未发生；随后 :454 `except Exception` 统一 `conn.rollback()`，零持久化副作用 |
| :444 | `PriorityUpdateZeroRowsError`（裸基类） | `"post-commit priority verification failed: exact target rows are not -1"` | **POST_COMMIT_READBACK_FAILED** | :421 `conn.commit()` 之后用新读（`_id_readback_sql`）验证 `weight_type` 仍非 -1：提交后读回不符 |

## 交叉事实（与本次 hotfix 相关）

1. 只有 :315 / :319 的消息带 adapter 锚定前缀，`legacy_zhaoshang_adapter.remote_not_materialized_diagnostic`
   （adapter :144-159）分别归类为 `PriorityRowsNotMaterialized` / `PriorityRowsIncomplete`
   （`REMOTE_NOT_MATERIALIZED_DIAGNOSTICS`）。其余 5 个站点全部保持 fail-closed 手工路径。
2. 生产 job 30 的 `error_text="PriorityUpdateZeroRowsError"` 是史前"裸类型名"持久化
   （adapter :136-141 注释），无法区分以上 7 类中的任何一类；因此 2026-09-04 hotfix
   的放行通道不信任该字符串本身，只把它作为窄家族判定条件之一，并强制要求
   exact live progress probe（ACTIVE/TERMINAL）作为放行证据。
3. raise 站点按副作用时序分两类，**不能**一概而论"全部零/可回滚副作用"：
   - **PRE_COMMIT**（:166 / :315 / :319 / :342 / :394 / :418）：raise 发生在
     :421 `conn.commit()` 之前，最终经 :454-457 `except Exception →
     conn.rollback() → raise`，事务整体回滚，零持久化副作用，无半提交状态；
   - **POST_COMMIT**（:444，`"post-commit priority verification failed"`）：
     raise 发生在 :421 `conn.commit()` 之后（提交后用 `_id_readback_sql`
     读回校验失败）。此刻提优 UPDATE **已经提交落库**，:454-457 的
     `conn.rollback()` 只能结束当前（空）事务，**不能撤销已提交的提优写**。
     该路径留下的是"已提交但未通过提交后验证"的提优状态，不是零副作用路径，
     需要下游以持久化证据另行确认，不能按可回滚处理。
4. `PriorityScopeSafetyError`（:58）的 7 个 raise 站点（:146/:149/:170/:172/:176/:178/:181/:184/:303/:347/:412/:439/:450/:452）不属于本次审计的 ZeroRows 家族，未列入清单。
