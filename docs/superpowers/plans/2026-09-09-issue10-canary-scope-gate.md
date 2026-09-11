# Issue #10 Canary 硬安全门禁（--only-mall 全链 scope）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `customer_prewarm_dispatch` 增加 `--only-mall`/`--canary-max-urls` 硬 canary scope：只读 preflight 先行、四阶段全约束、双层防御断言、显式路径守卫、沙箱迁移策略；普通模式行为逐字节不变。

**Architecture:** `CanaryScope` frozen dataclass 作为唯一策略对象贯穿 CLI 与 service；`_plan_only_report` 与 live `_run_leased_pass` 共用同一 scope predicate（同参 plan-only = 有效预演）；CLI 在任何 RW 构造器之前执行同源只读 preflight；Phase 3/4 各有独立第二层防御（CanaryScopeViolation / CANARY_PRIORITY_SCOPE_VIOLATION）。

**Tech Stack:** Python 3.14（`E:\Python314\python.exe`）、pytest、GitHub Actions。

**Spec:** 用户提供的 22 节规格。基线：分支 `feat/issue-10-zhaoshang-reference-prewarm`@8de3bf9（worktree `E:\.A中核运维\cnnc-ops-agent-worktrees\issue-10-zhaoshang-reference-prewarm`，已核验干净）；生产 a1baf01 未动；全量对照基线 `965e864`。

## Global Constraints

- 普通模式（无 `--only-mall`）：plan-only 与 live dispatcher 行为**逐字节不变**（summary 键集合也不变）；现有 8 个 `test_customer_prewarm_*` 测试文件零改动全绿。
- canary 模式下除 `only_mall` 外：candidate 写=0、cache 写=0、remote 写=0、task create=0、priority call=0（Phase 1/2 先于一切写过滤）。
- 本轮零真实动作：不跑真实 plan-only、不碰沙箱/生产数据文件（测试全用 tmp_path fixture）、不 merge/deploy/注册计划任务、不 push（主 Agent 验收后 push）。
- 不改 Phase 1-4 现有业务语义（reuse/create/submit/priority proof/SUBMIT_UNKNOWN/absorb）；scope 是外层约束，不是业务变更。
- `remote_identity` 格式 = `"{mall_key}|{task_id}|{task_number}"`（首段=商城）；Phase 4 scope 三证 = `remote_jobs.mall_key` 列 + identity 首段 + `prepared.mall_key`。
- store 迁移时机：`CustomerPrewarmStore(path)` RW 构造器执行 PRAGMA/ALTER（`prewarm_candidates.remote_job_id`）；`open_read_only` 不迁移。preflight 必须全部走 `open_read_only`。
- Shell=cmd.exe；pytest 用 `E:\Python314\python.exe ... --basetemp=.pytest_tmp`；commit 信息 `feat(issue10-canary): ...` / `test(issue10-canary): ...` / `ci(issue10-canary): ...`。

---

### Task 1: service 层 CanaryScope + plan-only scope 化 + preflight 校验器

**Files:**
- Modify: `app/services/customer_prewarm_dispatch.py`
- Test: `tests/test_canary_scope_gate.py`（新建，本 Task 先建文件放 Case 1/10/17 单元部分）

**Interfaces (Produces):**
- `class CanaryScopeViolation(RuntimeError)`
- `@dataclass(frozen=True) class CanaryScope: only_mall: str; max_urls: int = 1; max_jobs: int = 1`，方法 `mall_in_scope(mall) -> bool`（strip 后严格相等，空 mall 恒 False）
- 结果码常量：`CANARY_MALL_NOT_MAPPED / CANARY_TARGET_EMPTY / CANARY_URL_LIMIT_EXCEEDED / CANARY_JOB_LIMIT_EXCEEDED / CANARY_PREFLIGHT_FAILED / CANARY_PRIORITY_SCOPE_VIOLATION`（值=同名字符串）
- `validate_canary_preflight(plan: dict, canary_scope: CanaryScope, mappings: Sequence[ZhaoshangMallMapping]) -> str | None`（PASS→None）
- `_plan_only_report(..., canary_scope: CanaryScope | None = None)`：scope 存在时 summary 增键（普通模式键集合不变）：`canary_mode=True, only_mall, canary_max_urls, candidates_seen_total, candidates_in_scope, skipped_by_canary_filter, remote_interaction_urls, other_remote_tasks_created=0, other_priority_changed=0, priority_updates_performed=0`，并设 `summary["canary_preflight"] = "PASS" | <CODE>`
- `run_prewarm_dispatch_pass(..., canary_scope: CanaryScope | None = None)`

**精确改动：**

1. imports 加 `from dataclasses import dataclass`；常量区加 CanaryScope/CanaryScopeViolation/六码。
2. `_empty_summary(business_date, plan_only, canary_scope=None)`：scope 非 None 时追加上述 canary 键（普通模式不加任何键）。
3. `_plan_only_report` 顶部签名加 `canary_scope=None`；summary 构造传入。
4. misses 主循环：`routing = classify_reference_url(...)` 之后、现有 outcome if-chain 之前插入：
```python
if canary_scope is not None:
    summary["candidates_seen_total"] += 1
    if routing.outcome != OUTCOME_ZHAOSHANG or not canary_scope.mall_in_scope(routing.mall_key):
        summary["skipped_by_canary_filter"] += 1
        continue  # BEFORE cache recheck — mirrors live zero-write skip
    summary["candidates_in_scope"] += 1
```
（staples/tmall/unmapped 全部算 out-of-scope，连 cache recheck 都不做。）
5. 第三桶（PRIORITY_PENDING 扫描）双循环内、`job_id is None` 检查前插入：
```python
if canary_scope is not None:
    summary["candidates_seen_total"] += 1
    if not canary_scope.mall_in_scope(_candidate_mall(candidate, mappings)):
        continue  # no job read, no priority planning
    summary["candidates_in_scope"] += 1
```
6. 汇总段（`planned_jobs = ...` 之后）：
```python
if canary_scope is not None:
    summary["remote_interaction_urls"] = (
        summary["existing_remote_reused_planned"]
        + summary["new_remote_urls_planned"]
        + pending_priority_planned
    )
    summary["canary_preflight"] = validate_canary_preflight(summary, canary_scope, mappings) or "PASS"
```
7. `validate_canary_preflight` 实现（顺序即优先级）：
```python
def validate_canary_preflight(plan, canary_scope, mappings):
    if not any(m.mall_key == canary_scope.only_mall for m in mappings):
        return CANARY_MALL_NOT_MAPPED
    if int(plan.get("candidates_in_scope", 0) or 0) < 1:
        return CANARY_TARGET_EMPTY
    if int(plan.get("remote_interaction_urls", 0) or 0) > canary_scope.max_urls:
        return CANARY_URL_LIMIT_EXCEEDED
    if int(plan.get("new_remote_jobs_planned", 0) or 0) > canary_scope.max_jobs:
        return CANARY_JOB_LIMIT_EXCEEDED
    if plan.get("phase_errors"):
        return CANARY_PREFLIGHT_FAILED
    return None
```
8. `run_prewarm_dispatch_pass` 签名加 `canary_scope=None`；plan_only 分支透传；live 分支在 `_empty_summary(...)` 传 scope，且入口处加防御：
```python
if canary_scope is not None and not plan_only and max_jobs_per_pass > canary_scope.max_jobs:
    raise CanaryScopeViolation(
        f"--max-jobs-per-pass {max_jobs_per_pass} exceeds canary cap {canary_scope.max_jobs}"
    )
```
（`_run_leased_pass` 透传 scope，Task 2 接线。）

- [ ] Step 1: 新建 `tests/test_canary_scope_gate.py`，先写失败测试：Case 1 骨架（构造多商城 fake store：xhgj×1 + comix(qx 空mall)×1 + stbchina×1 + tmall×1，plan-only scope=xhgj → candidates_in_scope=1、skipped_by_canary_filter=3、create 组只有 xhgj、canary_preflight=PASS）+ Case 10（`validate_canary_preflight({"new_remote_jobs_planned": 2, "candidates_in_scope": 1, "remote_interaction_urls": 1, "phase_errors": []}, scope, mappings含xhgj)` → CANARY_JOB_LIMIT_EXCEEDED）+ Case 17a（普通 plan-only summary 无 `canary_mode` 键）。fixture 风格必须先读 `tests/test_customer_prewarm_dispatch.py` 并复用其 fake store/cache/mappings 构造方式。
- [ ] Step 2: 跑红 → 实现 → 跑绿（`E:\Python314\python.exe -m pytest tests\test_canary_scope_gate.py -q --basetemp=.pytest_tmp`）
- [ ] Step 3: 回归现有：`E:\Python314\python.exe -m pytest tests\test_customer_prewarm_dispatch.py tests\test_customer_prewarm_dispatch_cli.py -q --basetemp=.pytest_tmp` 全绿
- [ ] Step 4: Commit `feat(issue10-canary): CanaryScope policy + scoped plan-only preflight validator`

---

### Task 2: live 四阶段 scope 约束 + 双层防御

**Files:**
- Modify: `app/services/customer_prewarm_dispatch.py`
- Test: `tests/test_canary_scope_gate.py`（追加 Case 2-6/11/12/16）

**精确改动：**

1. `_run_leased_pass(..., canary_scope=None)`：
   - Phase 1 双循环体首部（`mall = _candidate_mall(...)` 之后、`if not mall` 之前）：
```python
if canary_scope is not None:
    counters["candidates_seen_total"] += 1
    if not canary_scope.mall_in_scope(mall):
        counters["skipped_by_canary_filter"] += 1
        continue  # NO cache lookup, NO record, NO reuse/confirm lists
    counters["candidates_in_scope"] += 1
```
   - Phase 2 misses 循环：`routing = classify_reference_url(...)` 后、outcome if-chain 前插入与 plan-only 完全相同的 scope 块（抽公共函数 `_scope_gate(summary, routing, canary_scope) -> bool` 返回是否 in-scope 并累计计数，两处调用同一函数，满足 §12 same predicate）。
   - Phase 3 组循环调用 `_dispatch_mall_group(..., canary_scope=canary_scope)`；组循环后 canary 结算：
```python
if canary_scope is not None:
    summary["priority_updates_performed"] 已在调用点累计
    summary["remote_jobs_created"] = counters["jobs_created"]
    summary["remote_interaction_urls"] = (
        counters["existing_remote_reused"] + counters["new_remote_urls"]
        + summary["priority_updates_performed"] - counters["priority_confirmed"] - counters["priority_boosted"] + <confirm-only 次数>
    )
```
   —— 若该式复杂化，简化为：Phase 3 内联计数 `remote_side_effects_total`（每次 reuse wire +1、每次 create group +len(batch_entries)、每次 confirm/boost 调用 +1），结尾直接 `summary["remote_interaction_urls"] = remote_side_effects_total`。采用此法。
   - `other_remote_tasks_created`：Phase 3 组循环中 `if canary_scope is not None and mall_key != canary_scope.only_mall: counters["other_remote_tasks_created"] += <本组返回值>`。
2. `_dispatch_mall_group(..., canary_scope=None)`：
   - 函数体第一行（任何 probe/写之前）：
```python
if canary_scope is not None and mapping.mall_key != canary_scope.only_mall:
    raise CanaryScopeViolation(
        f"canary scope {canary_scope.only_mall!r} violated by mall group {mapping.mall_key!r}"
    )
```
   - 组内本地计数 `reused_here = 0`（unfinished 分支 +1）；`if not batch_entries: return 0` 之后、max-jobs 检查之前：
```python
if canary_scope is not None and reused_here + len(batch_entries) > canary_scope.max_urls:
    raise CanaryScopeViolation(
        f"mall {mall_key}: group remote side effects {reused_here + len(batch_entries)} "
        f"exceed canary max urls {canary_scope.max_urls}"
    )
```
   - Phase 4 两个调用点透传 scope；`_confirm_pending_priority_job(..., canary_scope=None)`、`_boost_reused_job(..., canary_scope=None)`：在现有 identity 三段校验块之后、`store.renew_lease(...)`/`prioritize` 之前插入：
```python
if canary_scope is not None and not (
    str(job_row.get("mall_key") or "") == canary_scope.only_mall
    and parts[0] == canary_scope.only_mall
    and prepared.mall_key == canary_scope.only_mall
):
    summary["phase_errors"].append(
        f"priority {'confirm' if confirm else 'boost'} job {job_id}: "
        f"{CANARY_PRIORITY_SCOPE_VIOLATION} (job mall evidence does not prove "
        f"{canary_scope.only_mall!r})"
    )
    return False / return
```
   （confirm 返回 False；boost 直接 return；两处 prioritize call=0。）
   - 每次 `adapter.prioritize(...)` 实际调用点（confirm/boost 各一）之后：`if canary_scope is not None: summary["priority_updates_performed"] += 1`。
   - 同 pass 内新建任务 submit 成功后同轮 confirm（`_dispatch_mall_group` 尾部调用）也透传 `canary_scope=canary_scope`。

**测试 Case（追加）：**
- Case 2：fake live（复用现有 dispatch 测试的 fake state db + fake adapter），多商城候选，scope=xhgj 跑完整 pass → 其它 mall 候选行（status/last_checked_at/remote_job_id）前后逐字段一致（直接 dump store 表对比）。
- Case 3：某 comix 候选 cache 里有 current 命中 + scope=xhgj live → 该候选不被 absorb（仍 REMOTE_DISPATCHED/原状态）。
- Case 4/5：stbchina / detail.tmall 候选 scope pass 后仍 PENDING_PREWARM（非 STAPLES_PENDING/MANUAL_SKIP）。
- Case 6：xhgj 1 URL happy path（fake adapter prioritize 返回 UPDATED+weight_type=-1）→ jobs_created=1、remote_interaction_urls=1、other_remote_tasks_created=0、other_priority_changed=0、priority_confirmed=1。
- Case 11：直接调 `_dispatch_mall_group(mapping=comix, entries=[...], canary_scope=CanaryScope("xhgj"))` → pytest.raises(CanaryScopeViolation)，fake adapter.submit 调用数=0。
- Case 12：state db 放一个 comix job（identity `comix|tid|tnum`，mall_key=comix，状态 SUBMITTED），store 放 comix REMOTE_REUSED 候选绑它，scope=xhgj 跑 pass → adapter.prioritize 调用数=0（Phase 1 已跳过该候选，根本不进列表——再加一层：直接调 `_boost_reused_job(canary_scope=CanaryScope("xhgj"))` 该 job → prioritize=0 + phase_errors 含 CANARY_PRIORITY_SCOPE_VIOLATION）。
- Case 16：#9 老 schema store（无 remote_job_id 列）+ 合法 prewarm 数据 → service 层 RW live（scope=xhgj，fake adapter）→ 该 store 文件出现 remote_job_id 列（仅沙箱 store 迁移），state db 无任何 prewarm 表。
- Case 17b：普通 live（无 scope）完整跑一遍现有 fake 场景 → summary 无 canary 键、行为与改动前一致（现有测试文件全绿即证）。

- [ ] Step 1: 写 Case 2-6/11/12/16 失败测试 → 跑红
- [ ] Step 2: 实现 → 跑绿
- [ ] Step 3: 回归 `tests\test_customer_prewarm_dispatch.py tests\test_customer_prewarm_dispatch_cli.py tests\test_customer_prewarm_store.py -q` 全绿
- [ ] Step 4: Commit `feat(issue10-canary): four-phase scope enforcement with defensive assertions`

---

### Task 3: CLI canary 参数 + 只读 preflight 顺序 + 显式路径守卫

**Files:**
- Modify: `scripts/customer_prewarm_dispatch.py`
- Test: `tests/test_canary_scope_gate.py`（追加 Case 7/8/9/14/15 + 参数冲突组）

**精确改动：**

1. `build_parser` 增：
```python
parser.add_argument("--only-mall", default=None, metavar="MALL",
    help="canary mode: hard-scope the ENTIRE pass (all four phases) to one "
         "mall; requires explicit --prewarm-store-path/--shared-cache-path/"
         "--state-runtime-root and --max-jobs-per-pass 1")
parser.add_argument("--canary-max-urls", type=int, default=1, metavar="N",
    help="canary: max remote-interaction URLs (reuse + create + priority "
         "confirm) allowed by preflight; default 1")
```
2. `main()` 校验（放在现有 budget/max-jobs 校验后）：
```python
if args.only_mall:
    if args.canary_max_urls < 1:
        print("ERROR --canary-max-urls must be at least 1"); return 2
    if args.max_jobs_per_pass > 1:
        print("ERROR --max-jobs-per-pass must be 1 with --only-mall"); return 2
    if args.backfill_zhaoshang_history is not None:
        print("ERROR --only-mall is mutually exclusive with --backfill-zhaoshang-history"); return 2
    if args.prewarm_store_path is None or args.shared_cache_path is None or args.state_runtime_root is None:
        print("ERROR canary mode requires explicit --prewarm-store-path, "
              "--shared-cache-path and --state-runtime-root "
              "(CANARY_EXPLICIT_PATHS_REQUIRED)"); return 2
```
3. `_run_dispatch` 内，在解析三个路径后（canary 时三者必显式）加路径策略检查：
```python
canary_scope = (
    CanaryScope(only_mall=args.only_mall.strip(), max_urls=args.canary_max_urls)
    if args.only_mall else None
)
if canary_scope is not None:
    state_root = Path(args.state_runtime_root).resolve()
    for label, p in (("prewarm-store", prewarm_store_path), ("shared-cache", shared_cache_path)):
        if p == state_root or state_root in p.parents or p.name == "state.sqlite3":
            print(json.dumps({"outcome": "CANARY_PATH_POLICY_VIOLATION",
                "path": str(p), "state_runtime_root": str(state_root), "role": label},
                ensure_ascii=False, sort_keys=True))
            return 2
```
4. `_run_dispatch` plan-only 分支：`run_prewarm_dispatch_pass(..., canary_scope=canary_scope)`；调用后：
```python
if canary_scope is not None and summary.get("canary_preflight", "PASS") != "PASS":
    print(f"canary preflight FAILED outcome={summary['canary_preflight']}")
    if args.json_output is not None: _write_json_output(args.json_output, summary)
    return 2
```
5. `_run_dispatch` live 分支改为三分支：普通 live 原样；canary live 新增（关键顺序）：
```python
elif canary_scope is not None:
    # READ-ONLY preflight BEFORE any RW store/lock/migration/artifact
    try:
        ro_prewarm = CustomerPrewarmStore.open_read_only(prewarm_store_path)
    except FileNotFoundError:
        return _print_missing_store_diagnosis(business_date, "PREWARM_STORE_MISSING", prewarm_store_path, args)  # canary: 改 return 2
    try:
        ro_cache = SharedCrawlCache.open_read_only(shared_cache_path)
    except FileNotFoundError:
        return _print_missing_store_diagnosis(... )  # 同上 return 2
    had_remote_job_column = _store_has_remote_job_column(prewarm_store_path)
    preflight = run_prewarm_dispatch_pass(
        ro_prewarm, ro_cache, business_date=business_date, runtime_root=runtime_root,
        state_runtime_root=state_runtime_root, mappings=mappings,
        budget_seconds=args.budget_seconds, max_jobs_per_pass=args.max_jobs_per_pass,
        plan_only=True, canary_scope=canary_scope)
    if preflight.get("canary_preflight", "PASS") != "PASS":
        print(f"customer_prewarm_dispatch canary preflight BLOCKED outcome={preflight['canary_preflight']}")
        if args.json_output is not None: _write_json_output(args.json_output, preflight)
        return 2
    # preflight PASS -> RW live
    rw_prewarm = CustomerPrewarmStore(prewarm_store_path)
    rw_cache = SharedCrawlCache(shared_cache_path)
    summary = run_prewarm_dispatch_pass(rw_prewarm, rw_cache, ..., plan_only=False, canary_scope=canary_scope)
    for key in ("existing_remote_reused_planned", "new_remote_urls_planned",
                "new_remote_jobs_planned", "priority_updates_planned",
                "remote_interaction_urls", "candidates_seen_total",
                "candidates_in_scope", "skipped_by_canary_filter"):
        summary[key] = preflight.get(key, summary.get(key, 0))
    summary["sandbox_schema_migrated"] = (
        not had_remote_job_column and _store_has_remote_job_column(prewarm_store_path))
```
   `_store_has_remote_job_column(path)`：`sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)` → `PRAGMA table_info(prewarm_candidates)` → 列名集合含 `remote_job_id`（文件不存在→False）。
6. stdout 增 canary 行（scope 时）：
```python
print(f"canary only_mall={..} candidates_in_scope={..} skipped_by_canary_filter={..} "
      f"remote_interaction_urls={..} other_remote_tasks_created={..} other_priority_changed={..} "
      f"sandbox_schema_migrated={..}")
```
7. `_print_missing_store_diagnosis` 保持 plan-only exit 0 原语义；canary live 路径的 missing store 用独立小函数 return 2（不复用 exit 0 语义）。

**测试 Case（追加）：**
- Case 7：老 schema store + xhgj×2 URL 候选 + `--canary-max-urls 1` live → exit 2、outcome=CANARY_URL_LIMIT_EXCEEDED；monkeypatch `CustomerPrewarmStore.__init__`/`SharedCrawlCache.__init__`/`app.services.daily_audit_store.ProcessLock` 为 AssertionError 桩证明 RW 构造器/锁零调用；store 文件 SHA256 前后一致（Case 15 一起：仍无 remote_job_id 列）。
- Case 8：`--only-mall not-exist` live → CANARY_MALL_NOT_MAPPED、exit 2、零 RW（同上桩）。
- Case 9：mappings 不含目标 mall（用最小 fake mapping workbook/直接注入）→ CANARY_MALL_NOT_MAPPED。与 8 二选一实现路径（8 走 CLI 真实 mapping 加载，9 走 validate 单元级），两者都要有。
- Case 14：canary live 缺 `--shared-cache-path`（其它齐全）→ exit 2、输出含 CANARY_EXPLICIT_PATHS_REQUIRED、零 RW。
- 参数冲突组：`--only-mall x --max-jobs-per-pass 8` → exit 2；`--only-mall x --canary-max-urls 0` → exit 2；`--only-mall x --backfill-zhaoshang-history 3` → exit 2。
- 路径策略组：`--prewarm-store-path <state_root>/customer_prewarm.sqlite3`（state_root=--state-runtime-root）→ exit 2 CANARY_PATH_POLICY_VIOLATION。
- Case 13：同参 plan-only 与 preflight 数值一致——CLI 层：同一 fixture 先跑 `--plan-only --only-mall xhgj`，再对 live canary 用 monkeypatch 的 fake adapter 跑通，断言两者 summary 的 candidates_in_scope/remote_interaction_urls/new_remote_urls_planned 相等（live 的这些键来自 preflight 携带）。
- CLI 正常模式回归：现有 `tests/test_customer_prewarm_dispatch_cli.py` 零改动全绿。

- [ ] Step 1: 写 Case 7/8/9/13/14/15 + 参数/路径冲突组失败测试 → 跑红
- [ ] Step 2: 实现 → 跑绿
- [ ] Step 3: 回归 `tests\test_customer_prewarm_dispatch_cli.py tests\test_customer_prewarm_scan_cli.py -q` 全绿
- [ ] Step 4: Commit `feat(issue10-canary): CLI canary args, read-only preflight ordering, explicit path guard`

---

### Task 4: CI + 门禁矩阵

**Files:**
- Modify: `.github/workflows/critical-safety-tests.yml`

- [ ] Step 1: `tests/test_canary_scope_gate.py` 加入 Run critical safety tests 清单（置顶区块），同步更正注释中的文件计数与说明（canary 安全门禁：other-mall 零写 / URL 超限零写 / 错 mall submit+priority tripwire / 显式路径 / 老schema无迁移）。
- [ ] Step 2: 定向门禁：
```bash
E:\Python314\python.exe -m pytest tests\test_canary_scope_gate.py tests\test_customer_prewarm_dispatch.py tests\test_customer_prewarm_dispatch_cli.py tests\test_customer_prewarm_store.py tests\test_customer_prewarm_discovery.py tests\test_customer_prewarm_step1_isolation.py tests\test_customer_prewarm_daily_sync.py tests\test_customer_prewarm_customer_recovery.py -q --basetemp=.pytest_tmp
```
- [ ] Step 3: #7/#9/#10 + 招商局 + STEP1 矩阵：
```bash
E:\Python314\python.exe -m pytest tests\test_shared_crawl_cache.py tests\test_customer_reference_routing.py tests\test_legacy_zhaoshang_adapter.py tests\test_legacy_adapter_progress_stop.py tests\test_zhaoshang_priority_boost.py tests\test_zhaoshang_task_phases.py tests\test_zhaoshang_resume_modes.py tests\test_zhaoshang_result_adapter.py tests\test_process_monitor_zs_result_mapping.py tests\test_incremental_price_audit.py tests\test_incremental_audit_runtime.py tests\test_incremental_price_audit_finalize.py tests\test_closeout_before_1830.py tests\test_closeout_transient_recovery.py -q --basetemp=.pytest_tmp --timeout=600 --timeout-method=thread
```
（文件名以 tests/ 目录实际存在为准，缺名先 `dir /b tests | rg` 核对再替换等价物。）
- [ ] Step 4: 静态：`git diff --check` + `E:\Python314\python.exe -m py_compile scripts\customer_prewarm_dispatch.py app\services\customer_prewarm_dispatch.py`
- [ ] Step 5: 全量：`E:\Python314\python.exe -m pytest tests -q --basetemp=.pytest_tmp --timeout=600 --timeout-method=thread`；失败与干净 `965e864` worktree 同命令对照，输出 BRANCH_FAILURES / BASELINE_FAILURES / NEW_REAL_REGRESSIONS 三清单（基线临时 worktree 用后即删，勿动其他 worktree）。
- [ ] Step 6: Commit `ci(issue10-canary): add canary scope gate tests to critical safety suite`

## Self-Review

- 规格 §1-§22 逐节映射：§1 CanaryScope✓(Task1)；§2 CLI+cap 冲突 fail-closed✓(Task3)；§3 remote_interaction_urls 口径=reuse+create+confirm✓(Task1/2)；§4 preflight 顺序（RW 构造前）✓(Task3)；§5 六 fail-closed 码✓(Task1 validator+Task3 CLI)；§6 Phase1 scope（_candidate_mall 既有统一 resolver）✓(Task2)；§7 Phase2 纯路由先过滤零写✓；§8 staples/tmall=OUT_OF_SCOPE 不改状态✓(Case4/5)；§9 Phase3 双防御（mall_key 断言+组内 URL 上限断言）✓；§10 Phase4 三证一致（mall_key列+identity首段+prepared.mall_key）✓；§11 summary 字段全清单✓；§12 plan/live 共用 predicate（_scope_gate 函数两处同源）✓；§13 不写死 xhgj/1✓；§14 显式路径+不指向 state root✓；§15 沙箱迁移披露 sandbox_schema_migrated✓；§16 invalid preflight 连 schema 都不改（open_read_only 无 ALTER + Case7/15 文件哈希证）✓；§17 普通模式不变（键集合断言+现有测试零改）✓；§18 17 Cases✓；§19 CI✓；§20 门禁矩阵+基线 965e864✓；§21 零真实动作✓；§22 报告由主 Agent 产出。
- 命名一致性：CanaryScope.only_mall/max_urls/max_jobs；summary 键与规格 §11 逐字对齐；validate_canary_preflight 返回 None|CODE。
