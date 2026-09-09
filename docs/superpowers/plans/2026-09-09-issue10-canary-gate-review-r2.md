# Issue #10 Canary Gate Review Round 2 — Live Budget Target Set + Two-Pass Group Probe

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除两个 Blocker：live canary 在 preflight 后状态漂移时仍硬保证 max_urls/max_jobs，且任何超限/漂移必须发生在 candidate write / submit / prioritize 之前；preflight→live TOCTOU 由 stable target set（LiveCanaryBudget 语义）消除。

**Architecture:** 只读 preflight 的 scoped plan 额外产出 `canary_target_urls`（(mall, exact_url) 身份集合）；新 `CanaryLiveBudget` 携带该集合进 live；Phase 3 `_dispatch_mall_group` canary 分支改为 two-pass（Pass A 零写 probe+分类+budget 漂移检查+超限检查 → Pass B 才执行原有 reuse/terminal/create）；Phase 4 在任何 prioritize 之前整体校验 confirm+boost 的 job payload URL 全 ⊆ budget（任一漂移 → 整个 Phase 4 零调用），并在 confirm/boost 函数内保留 per-call 第二层。非 canary 模式逐字节不变。

**Tech Stack:** Python 3.14（`E:\Python314\python.exe`）、pytest、GitHub Actions。

**Spec:** 用户提供的 Round-2 规格（Blocker 1/2 + Case A-E + 保持清单）。PRE_FIX_HEAD=b573ace；基线对照 `965e864`；worktree `E:\.A中核运维\cnnc-ops-agent-worktrees\issue-10-zhaoshang-reference-prewarm`。

## Global Constraints

- 非	canary（无 --only-mall）模式行为与 summary 键集合逐字节不变；现有全部测试（含 Round-1 22 用例中不涉新契约的部分）保持语义。
- canary 语义基线不回退：RO preflight 先于 RW 构造器、显式三路径、路径策略、Phase1/2 越界零写、Staples/Tmall 零写、Phase3 wrong-mall submit guard、Phase4 三证 mall guard、weight_type=-1 证明、SUBMIT_UNKNOWN 不重发、plan-only 零写、老 schema 无效 preflight 不迁移。
- 预算身份 = (mall, exact_url)，不是调用次数：fresh create + 同任务同轮 priority confirm 仍算 1 个 interaction（Case E 必须保持合法）。
- 漂移/超限的 raise/阻断必须发生在任何 candidate `_record`、`ensure_crawl_work`、remote job create、submit、prioritize 之前。
- 本轮零真实动作（无真实 plan-only/remote/priority/cache 写/merge/deploy/push——push 由主 Agent 执行）。
- Shell=cmd.exe；pytest `E:\Python314\python.exe ... --basetemp=.pytest_tmp`；commit `fix(issue10-canary): ...` / `test(issue10-canary): ...` / `docs(issue10-canary): ...`。

---

### Task 1: plan-only 产出 canary_target_urls + CanaryLiveBudget 模型

**Files:**
- Modify: `app/services/customer_prewarm_dispatch.py`
- Test: `tests/test_canary_scope_gate.py`

**Interfaces (Produces):**
- `_plan_only_report`（scope 模式）新键 `summary["canary_target_urls"] = sorted([[mall, url], ...])`（JSON 安全）；同时把 `remote_interaction_urls` 改为 `len(target_set)`（身份口径，替代 reused_planned+new_urls_planned+pending_priority 的加法——对现有用例数值无影响，单 URL 仍=1）。
- `class CanaryLiveBudget`：
```python
class CanaryLiveBudget:
    """Live canary 硬预算：preflight 批准的 (mall, exact_url) 目标集合。

    成员检查即容量检查——集合在构造时已验证 |set| <= max_urls，live 中任何
    不在集合内的 interaction 都是 CANARY_LIVE_SCOPE_DRIFT，fail closed。
    """

    def __init__(self, only_mall: str, max_urls: int, allowed_urls) -> None:
        pairs = {(str(m).strip(), str(u).strip()) for m, u in allowed_urls}
        if len(pairs) > int(max_urls):
            raise CanaryScopeViolation(
                f"canary target set has {len(pairs)} urls, exceeds max {max_urls}"
            )
        self.only_mall = only_mall
        self.max_urls = int(max_urls)
        self._allowed = frozenset(pairs)

    @classmethod
    def from_plan(cls, plan: Mapping[str, Any], canary_scope: CanaryScope) -> "CanaryLiveBudget":
        pairs = [tuple(item) for item in plan.get("canary_target_urls") or []]
        return cls(canary_scope.only_mall, canary_scope.max_urls, pairs)

    def check(self, mall: Any, exact_url: Any) -> tuple[str, str]:
        key = (str(mall or "").strip(), str(exact_url or "").strip())
        if key not in self._allowed:
            raise CanaryScopeViolation(
                f"CANARY_LIVE_SCOPE_DRIFT: live interaction {key!r} is not in "
                "the read-only preflight target set"
            )
        return key
```
- `run_prewarm_dispatch_pass(..., canary_budget: CanaryLiveBudget | None = None)`：live + canary_scope + budget 为 None → `raise CanaryScopeViolation("live canary requires a preflight budget")`（plan-only 忽略 budget 参数）。

**收集规则（_plan_only_report 内，scope 分支）：**
- miss 循环：unfinished_bound 命中（reuse 计数处）→ add (mall_key, lookup_url)；create_groups append 处 → add 同 pair；terminal 两个分支不加。
- 第三桶（PRIORITY_PENDING 候选）：`pending_priority_planned += 1` 处 → add (_candidate_mall(candidate, mappings), candidate["lookup_url"])。

- [ ] Step 1: 失败测试：①scoped plan-only 输出 canary_target_urls 且 == [[“xhgj”, url]]；②remote_interaction_urls==len(canary_target_urls)；③CanaryLiveBudget.from_plan 构造 + check 成员/非成员（非成员 raise，消息含 CANARY_LIVE_SCOPE_DRIFT）；④构造超限集合 raise；⑤live+scope+budget=None → CanaryScopeViolation。
- [ ] Step 2: 跑红→实现→跑绿；回归 Round-1 22 用例（服务级 live 用例会因新契约红——本 Task 先只加新测试不删旧，旧 live 用例的修复放 Task 3 统一接线后）。
- [ ] Step 3: Commit `fix(issue10-canary): preflight target set + CanaryLiveBudget`

---

### Task 2: Blocker 1 — `_dispatch_mall_group` two-pass + Phase 4 整体预算前置

**Files:**
- Modify: `app/services/customer_prewarm_dispatch.py`
- Test: `tests/test_canary_scope_gate.py`（Case A/B/D/E）

**精确改动：**

1. `_dispatch_mall_group(..., canary_scope=None, canary_budget=None)`——保留首行 mall 断言；在现有 per-entry 循环**之前**插入 canary Pass A（零写）：
```python
precomputed_probes: dict[str, Any] = {}
if canary_scope is not None:
    live_interactions = 0
    for entry in entries:
        lookup_url = str(entry["routing"].lookup_url)
        probe = find_remote_work_for_url(paths.state_path, mall_key, lookup_url)
        precomputed_probes[lookup_url] = probe
        if canary_budget is not None:
            canary_budget.check(mall_key, lookup_url)  # drift -> raise, zero-write
        unfinished = (probe or {}).get("unfinished_bound") is not None
        terminal = bool((probe or {}).get("terminal_success") or (probe or {}).get("terminal_failed"))
        if unfinished or not terminal:
            live_interactions += 1
    if live_interactions > canary_scope.max_urls:
        raise CanaryScopeViolation(
            f"mall {mall_key}: live remote interactions {live_interactions} exceed "
            f"canary max urls {canary_scope.max_urls} (CANARY_LIVE_URL_LIMIT_EXCEEDED)"
        )
```
   现有 per-entry 循环内的 probe 调用改为：`probe = precomputed_probes.get(lookup_url) if canary_scope is not None else find_remote_work_for_url(paths.state_path, mall_key, lookup_url)`（非 canary 语义不变）。保留既有 `reused_here + len(batch_entries)` 后置断言（第二层，Pass A 后不可达但保留）。
2. Phase 4 整体前置（`_run_leased_pass`，`counters["jobs_reused"] = ...` 之后、4a confirm 循环之前）：
```python
phase4_budget_blocked = False
if canary_scope is not None and canary_budget is not None:
    pending_pairs: set[tuple[str, str]] = set()
    for item in [*confirm_items, *reuse_items]:
        job_row = store.get_remote_job(int(item["job_id"]))
        for mall_value, url_value in _job_interaction_pairs(job_row or {}):
            pending_pairs.add((str(mall_value), str(url_value)))
    try:
        for pair in sorted(pending_pairs):
            canary_budget.check(*pair)
    except CanaryScopeViolation as violation:
        phase4_budget_blocked = True
        phase_errors.append(f"phase 4 pre-priority budget guard: {violation}")
```
   4a/4b 两个循环整体包在 `if not phase4_budget_blocked:` 下（blocked 时零 prioritize 调用，Case D 整体失败语义）。
3. 新助手：
```python
def _job_interaction_pairs(job_row: Mapping[str, Any]) -> list[tuple[Any, Any]]:
    """job payload_json 里的 (mall_key, url) 对（canary 预算身份，durable 证据）。"""
    payload = job_row.get("payload_json") or "{}"
    data = json.loads(payload) if isinstance(payload, str) else dict(payload)
    return [(row.get("mall_key"), row.get("url")) for row in data.get("requests", [])]
```
4. `_confirm_pending_priority_job(..., canary_budget=None)` 与 `_boost_reused_job(..., canary_budget=None)`：在既有三证 mall gate 之后、`renew_lease`/`prioritize` 之前加 per-call 第二层：
```python
if canary_budget is not None:
    for mall_value, url_value in _job_interaction_pairs(job_row):
        canary_budget.check(mall_value, url_value)
```
   （fresh create 的同轮 confirm：payload URL=batch entries=预算成员 → 通过，URL 身份不双计 ✓。）
5. `_run_leased_pass(..., canary_budget=None)` 全链透传（Phase 3 调用点、同轮 confirm 调用点、4a/4b 调用点）。

**测试（新增）：**
- Case A（reuse-only 超限）：state db 放两个 xhgj URL 的 unfinished remote work，store 放 2 个 PENDING_PREWARM xhgj 候选，直接调 `_dispatch_mall_group(canary_scope=CanaryScope("xhgj", max_urls=1))` → pytest.raises(CanaryScopeViolation)（消息含 CANARY_LIVE_URL_LIMIT_EXCEEDED），两候选行 before==after（逐字段含 last_checked_at 不变），submit_calls=0、prioritize_calls=0；断言不是因 `not batch_entries` 提前 return（raise 必须发生）。
- Case B（mixed 超限）：URL A unfinished（reuse）+ URL B 全新（create），max=1，直接调 `_dispatch_mall_group` → raise 发生在任何 `_record` 之前：A/B 候选行不变、state db crawl_work/remote_jobs 行数不变、submit=0、prioritize=0。
- Case D（两个 pending priority 整体失败）：state db 两个 xhgj PRIORITY_PENDING job（identity 三段合法、payload 含各自 URL），store 两个 REMOTE_DISPATCHED xhgj 候选分别绑定；`CanaryLiveBudget` 只含 A 的 URL；service 级完整 live pass（fake adapter）→ adapter.prioritize_calls == 0（A 也不许先改）、phase_errors 含 phase 4 pre-priority budget guard/CANARY_LIVE_SCOPE_DRIFT、两候选行不变。
- Case E（fresh create + 同轮 confirm 仍=1）：复用 Round-1 Case 6 场景升级为带 budget 的 service 级 live（budget 由同句柄 plan-only 构造）→ remote_interaction_urls==1、jobs_created<=1、priority_confirmed>=1、canary 通过。
- 直接级：`_confirm_pending_priority_job`/`_boost_reused_job` 带 budget 且 job payload URL 越界 → prioritize_calls=0 + CANARY_LIVE_SCOPE_DRIFT phase error。

- [ ] Step 1: 写 Case A/B/D/E + 直调级失败测试 → 跑红
- [ ] Step 2: 实现 → 跑绿
- [ ] Step 3: Commit `fix(issue10-canary): two-pass group probe + phase-4 whole-set pre-priority budget guard`

---

### Task 3: CLI 接线 + Case C（drift）+ 既有用例迁移到 budget 契约

**Files:**
- Modify: `scripts/customer_prewarm_dispatch.py`、`tests/test_canary_scope_gate.py`

**精确改动：**

1. CLI canary live：preflight PASS 后
```python
canary_budget = CanaryLiveBudget.from_plan(preflight, canary_scope)
summary = run_prewarm_dispatch_pass(..., plan_only=False, canary_scope=canary_scope, canary_budget=canary_budget)
```
   携带键清单追加 `"canary_target_urls"`；live 结束后检查漂移硬失败：
```python
if any("CanaryScopeViolation" in e or "CANARY_LIVE_SCOPE_DRIFT" in e for e in summary.get("phase_errors", [])):
    <打印 marker 行> ; _write_json_output(...); return 2
```
2. 既有 service 级 live canary 用例（Round-1 的 Case 2/3/4/5/6/16 及 17b 不涉及）迁移到新契约：加测试助手
```python
def _live_budget(store, cache, scope, **kw):
    plan = run_prewarm_dispatch_pass(store, cache, ..., plan_only=True, canary_scope=scope, **kw)
    return CanaryLiveBudget.from_plan(plan, scope)
```
   live 调用统一传 `canary_budget=_live_budget(...)`。断言本身不变（数值语义不变）。
3. Case C（preflight/live drift，CLI 级）：tmp 沙箱 store 只放 1 个 xhgj 候选；monkeypatch `CustomerPrewarmStore.__init__` 为 wrapper（调用原构造后向 store 插入第二个 xhgj PENDING_PREWARM 候选——只发生在 RW 阶段，即 preflight 之后）；monkeypatch `dispatch_service.CnncLegacyZhaoshangAdapter` 为 fake（记录 submit/prioritize 调用）；跑 CLI live canary → exit 2 + CANARY_LIVE_SCOPE_DRIFT marker；fake submit_calls==0、prioritize_calls==0；两候选 cache_status 均仍 PENDING_PREWARM（业务零变更）；沙箱文件允许出现 remote_job_id 列（migration 豁免，断言其存在不构成失败），但不得出现 dispatch.lock/任务目录/input.xlsx。

- [ ] Step 1: 迁移既有用例 + 写 Case C 失败测试 → 跑红
- [ ] Step 2: 实现 CLI 接线 → 全部跑绿（22+新增全部）
- [ ] Step 3: 回归：`tests\test_canary_scope_gate.py tests\test_customer_prewarm_dispatch.py tests\test_customer_prewarm_dispatch_cli.py tests\test_customer_prewarm_store.py -q` 全绿
- [ ] Step 4: Commit `fix(issue10-canary): CLI live budget wiring + drift hard-fail`

---

### Task 4: CI + 门禁矩阵 + 全量基线

- [ ] Step 1: 定向：`tests\test_canary_scope_gate.py` 全绿（含 Case A-E）
- [ ] Step 2: 矩阵：#10 全部 8 文件 + #9（discovery/store/scanner：test_customer_prewarm_discovery/store/scan_cli）+ Shared Crawl Cache（test_shared_crawl_cache.py）+ legacy adapter/progress/priority/submission/phases/resume/harvest（test_legacy_zhaoshang_adapter.py、test_legacy_adapter_progress_stop.py、test_zhaoshang_priority_boost.py、test_zhaoshang_task_phases.py、test_zhaoshang_resume_modes.py、test_batch_zhaoshang_adapter.py、test_zhaoshang_history_backfill.py）+ STEP1（test_incremental_price_audit_finalize.py、test_closeout_before_1830.py、test_closeout_transient_recovery.py）——文件名先 `dir /b tests | rg` 核对存在性再跑。
- [ ] Step 3: 静态：`git diff --check b573ace..HEAD` + py_compile 双文件。
- [ ] Step 4: 全量 + 干净 965e864 基线对照（临时 worktree 用后删），三清单输出。
- [ ] Step 5: `.github/workflows/critical-safety-tests.yml` 无需改（test_canary_scope_gate.py 已在清单）；若本轮有新文件才动。Commit（如有）`ci(issue10-canary): ...`。

## Self-Review

- Blocker 1：Pass A 零写（probe+分类+budget.check+计数）→ 超限 raise 于任何 `_record`/`ensure_crawl_work`/submit 之前；reuse-only 组不再有 `not batch_entries` 逃生口（Case A）✓。
- Blocker 2：Phase 4 整体前置集合校验（任一漂移 → 4a/4b 全跳过，prioritize=0，Case D）+ per-call 第二层 ✓。
- TOCTOU：预算=preflight 时刻的 (mall,url) 身份集合；live 新增 URL/job 一律 CANARY_LIVE_SCOPE_DRIFT（Case C 端到端 + Case D）✓。
- 不双计：预算与 remote_interaction_urls 均按 URL 身份；fresh create+同轮 confirm=1（Case E）✓。
- 保持清单：全部既有 guard 未删未弱化（two-pass 只在 canary 分支；非 canary 循环原样；后置断言保留）✓。
