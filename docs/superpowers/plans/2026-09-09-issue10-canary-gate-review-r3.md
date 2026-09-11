# Issue #10 Canary Gate Review Round 3 — Drift 终止后续阶段 + Live 零写 Revalidation Barrier

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** P0 修复：任何 live `CanaryScopeViolation` 后 Phase 4 及后续远端阶段零执行；新增 live 全局零写 revalidation barrier（锁后、Phase 1 前），drift/超限必须在任何 candidate business write / submit / prioritize 之前被发现。

**Architecture:** Fix 1 = `_run_leased_pass` Phase 3 组循环为 `CanaryScopeViolation` 加专门 except 分支（append 后 `return` 终止整个 leased pass）；Fix 2 = `_run_leased_pass` 入口（Phase 1 之前）调用**同一个** `_plan_only_report`（对当前 RW 句柄做只读计算，无任何写路径）重算 canary_target_urls，`CanaryLiveBudget.revalidate(current)` 做 `current ⊆ allowed` 子集校验（shrink 合法、增长即 CANARY_LIVE_SCOPE_DRIFT），失败则记 phase_error + outcome 标记 + return——零第二套判定规则。

**Spec:** 用户 Round-3 规格（P0 + Fix1/2 + Case F/G/H/I + 保持清单）。PRE_FIX_HEAD=d175f67；基线对照 965e864；worktree `E:\.A中核运维\cnnc-ops-agent-worktrees\issue-10-zhaoshang-reference-prewarm`。

## Global Constraints

- `_plan_only_report` 是纯读（miss 循环 cache hit 只计数不 `_record`；probe/`_read_remote_job_readonly` 均 mode=ro）——barrier 复用它即天然零写，禁止另写第二套 target 收集规则（避免 R2 式口径分叉）。
- 普通（非 canary）模式逐字节不变；Round-1/R2 全部 guard 保留未弱化。
- barrier 允许的前置动作（不构成违规）：preflight PASS 后的沙箱 additive schema migration、process/instance lock 获取；barrier 自身不得有任何 candidate/remote/cache 业务写。
- 目标减少=安全（{} ⊆ {A} 合法），目标新增=漂移。
- 本轮零真实动作；禁 push（主 Agent 验收后 push）。
- Shell=cmd.exe；pytest `E:\Python314\python.exe ... --basetemp=.pytest_tmp`；commit `fix(issue10-canary): ...` / `test(issue10-canary): ...`。

---

### Task 1: Fix 1 — CanaryScopeViolation 终止后续远端阶段

**Files:** Modify `app/services/customer_prewarm_dispatch.py`；Test `tests/test_canary_scope_gate.py`

**精确改动：** `_run_leased_pass` Phase 3 组循环的 except 拆分：
```python
        except CanaryScopeViolation as exc:
            # A canary scope breach in ANY group terminates the WHOLE pass:
            # no further groups and Phase 4 must never run afterwards
            # (canary fail-closed: drift → zero further remote interactions).
            phase_errors.append(f"mall {mall_key}: CanaryScopeViolation: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - per-group isolation
            phase_errors.append(f"mall {mall_key}: {type(exc).__name__}: {exc}")
            continue
```

**测试（先红）：** Fix1 直钉——service 级：store 放 1 个 REMOTE_DISPATCHED xhgj 候选（绑 PENDING 有 identity job，进 confirm_items）+ 1 个 PENDING_PREWARM xhgj 候选；monkeypatch `dispatch_service._dispatch_mall_group` 为 raise CanaryScopeViolation；budget 由真实 plan 构造（barrier 会通过）；fake adapter 记录调用。断言：adapter.prioritize_calls == 0、submit_calls == 0、两候选行 before==after、phase_errors 含 CanaryScopeViolation、（Task 2 完成前 barrier 不存在时也应通过——本测试钉的是 Phase 3→4 终止语义）。

- [ ] 失败测试 → 实现 → canary 全量绿
- [ ] Commit `fix(issue10-canary): canary scope violation terminates the whole live pass`

### Task 2: Fix 2 — live 零写 revalidation barrier + CanaryLiveBudget.revalidate

**Files:** Modify `app/services/customer_prewarm_dispatch.py`

**精确改动：**
1. `CanaryLiveBudget` 新方法：
```python
    def revalidate(self, current_urls) -> int:
        """Live 零写 revalidation：current ⊆ allowed（shrink 合法，增长即漂移）。

        返回 current 集合大小；任一 live actionable 目标不在 preflight 集合内
        时 raise CanaryScopeViolation（CANARY_LIVE_SCOPE_DRIFT），调用方必须
        在任何业务写之前 fail closed。
        """
        current = {(str(m).strip(), str(u).strip()) for m, u in current_urls}
        drift = current - self._allowed
        if drift:
            raise CanaryScopeViolation(
                f"CANARY_LIVE_SCOPE_DRIFT: live actionable targets not in the "
                f"preflight set: {sorted(drift)!r}"
            )
        return len(current)
```
2. `_run_leased_pass` 顶部（`phase_errors`/`counters` 取得后、Phase 1 注释之前）：
```python
    # ---- Phase 0 (canary): zero-write live revalidation barrier ----
    # Re-derive the CURRENT actionable target set through the SAME read-only
    # plan logic (_plan_only_report has no write path) and require it to be a
    # subset of the preflight-approved budget.  Anything that grew between
    # preflight and live is drift and must fail closed BEFORE Phase 1 absorbs
    # or mutates any candidate (target shrink is safe and allowed).
    if canary_scope is not None and canary_budget is not None:
        revalidation = _plan_only_report(
            prewarm_store,
            shared_cache,
            business_date=summary["business_date"],
            state_db_path=paths.state_path,
            mappings=mappings,
            max_jobs_per_pass=max_jobs_per_pass,
            canary_scope=canary_scope,
        )
        try:
            summary["canary_revalidated_targets"] = canary_budget.revalidate(
                revalidation.get("canary_target_urls") or []
            )
        except CanaryScopeViolation as violation:
            phase_errors.append(f"live revalidation barrier: {violation}")
            summary["outcome"] = "CANARY_LIVE_SCOPE_DRIFT"
            return
```
（`_run_leased_pass` 已持有全部所需参数；确认签名里有 `shared_cache`/`mappings`/`max_jobs_per_pass`/`paths`——有。）

- [ ] 失败测试（Case G/H 先红）→ 实现 → 绿
- [ ] Commit `fix(issue10-canary): zero-write live revalidation barrier before phase 1`

### Task 3: Case F/G/H/I 测试

**Files:** Test `tests/test_canary_scope_gate.py`

- **Case F（CLI 级，复用 Case C 注入模式）**：preflight 时 store 只有 A=REMOTE_DISPATCHED xhgj 候选（state db 有其 PRIORITY_PENDING 有 identity job）→ target={A}；RW 构造器 wrapper 注入 B=新 PENDING_PREWARM xhgj 候选。跑 CLI live canary → exit 2、stdout/json 含 CANARY_LIVE_SCOPE_DRIFT、fake adapter submit_calls==0 且 prioritize_calls==0（**证明 barrier 在 Phase 3/4 之前挡住，A 的 priority 未被触碰**）、state db job 仍 PRIORITY_PENDING、A 行不变、B 仍 PENDING_PREWARM。
- **Case G（service 级）**：A=PENDING_PREWARM xhgj create 目标（budget={A}）；构造 budget 后、调 live 前：向 shared cache 写入 A 的 current hit + 向 store 注入 B=新 PENDING_PREWARM xhgj 候选。live pass（带 budget）→ barrier 因 B 失败（outcome=CANARY_LIVE_SCOPE_DRIFT）：**A 未被写成 CACHE_HIT**（仍 PENDING_PREWARM，证明 drift 先于 Phase 1 absorb 的 `_record`）、B 不变、全部候选 business rows before==after、adapter 零调用。
- **Case H（service 级）**：budget={A}；live 前向 state db 写入 A 的 terminal_success 覆盖（crawl row SUCCESS 绑 job 或既有 seed helper 等价物）→ barrier：current={} ⊆ {A} → 通过、无 drift；Phase 3 terminal 分支正常记录（候选写允许）、零 submit/prioritize、phase_errors 无 drift。
- **Case I（回归）**：单 URL create→同轮 confirm 场景（既有 Case E/6 场景升级跑一遍带 barrier 的完整 live）→ remote_interaction_urls==1、jobs_created<=1、prioritize 仅该任务、canary_revalidated_targets==1、outcome=OK。

- [ ] 全部先红→绿；`tests\test_canary_scope_gate.py` 全绿（39+新增）
- [ ] Commit `test(issue10-canary): round-3 cases F/G/H/I`

### Task 4: 门禁矩阵 + 全量基线

- [ ] 定向：canary 全绿；#10 全部 8 文件；SharedCrawlCache、legacy adapter/progress/priority_boost/task_phases/resume_modes、STEP1 finalize/closeout（文件名先 `dir /b tests | rg` 核对）。
- [ ] `git diff --check d175f67..HEAD` + py_compile 双文件。
- [ ] 全量 + 干净 965e864 临时 worktree 基线对照（用后删），三清单；上轮已证伪的并行扰动失败独立复跑定性。
- [ ] 无 CI yml 改动（文件已在清单）。

## Self-Review

- P0：Phase 3 violation → `return`（无后续组、无 Phase 4）✓；barrier 在 Phase 1 之前（absorb/cache-hit `_record`、Phase 2 状态写、Phase 3、Phase 4 全部之前）✓。
- 同源规则：barrier 复用 `_plan_only_report`（同一 predicate/同一 target 收集）+ `CanaryLiveBudget.revalidate`（子集语义）✓ 无第二套判定。
- shrink 合法（Case H）；增长=drift（Case F/G）✓。
- 保持清单全部不动（two-pass/Phase4 整体守卫/三证/路径守卫/普通模式等仅作为回归验证对象）✓。
