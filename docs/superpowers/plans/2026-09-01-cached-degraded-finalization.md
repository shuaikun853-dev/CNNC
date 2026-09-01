# Cached Degraded Finalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or per-task dispatch) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the 20:35 safety closeout (and only it) to freeze the day's last-known-good authoritative snapshot from SQLite and materialize the final workbook when the authoritative source is provably unavailable, behind ten hard readiness gates.

**Architecture:** Strict finalization stays byte-for-byte identical. A new `incremental_audit_cached_finalize` module evaluates cached readiness purely from durable SQLite (reusing `source_stability_seconds`, `list_snapshots`, `list_crawl_work`, `list_remote_jobs`), freezes via the existing `store.freeze_snapshot`, and materializes by reusing `build_frozen_audit_artifact` + `LegacyWorkbookMaterializer` (no second Excel builder). Provenance (`finalization_mode=CACHED_DEGRADED`, reason, snapshot id, observed_at, stability) goes into `daily_runs.metrics_json` — no schema migration. Typed adapter exceptions (`SnapshotAuthExpiredError` etc.) feed `_safe_error`'s whitelist and closeout classification; only a fixed whitelist of source-unavailable error names may trigger the cached fallback.

**Tech Stack:** Python 3.14 stdlib + pytest + openpyxl; PowerShell 5.1-compatible scheduled runner script.

**Spec:** User task prompt "STEP_1 Cached Degraded Finalization" (2026-09-01) — 17 sections; the authoritative contract. Key code facts (worktree `E:\.A中核运维\cnnc-ops-agent-wt-cdf`, BASE 89aa9b0):

- `materialize_frozen_daily_run` — `app/workflows/incremental_price_audit_finalize.py:862-903` (acquire lock→lease→`build_frozen_audit_artifact`→readiness gate→`prepare_finalization`→`publisher.materialize`→`mark_finalized`→`update_daily_metrics`). Its internals are what cached mode re-orchestrates under one lock/lease.
- `build_frozen_audit_artifact` — same file L375-676; `FinalizeReadiness.ready` (L64-79) is the durable Gate 6-9 terminal check (detail/model pending+terminal, crawl pending/terminal/unplanned/invalid_price, remote pending/manual/invalid).
- `store.freeze_snapshot(token, snapshot_id)` — `app/services/daily_audit_store.py:2716-2726` (validates snapshot∈run, FrozenSnapshotConflict on different id, sets state=FINALIZING + frozen_snapshot_id; idempotent on same id).
- `source_stability_seconds(store, business_date, snapshot)` — `app/workflows/incremental_price_audit.py:279-312`.
- `_safe_error` — same file L255-270; `TickSummary` L232-238.
- `run_closeout` + `CloseoutConfig` + `_closeout_failure_class` — `app/services/incremental_audit_closeout.py:59-82` (name lists), L86-106 (classifier), L118-144 (config), L155-326 (loop; failure branch L253-274).
- Adapter raise sites — `app/services/cnnc_saas_snapshot_adapter.py`: L193 (rows), L203/207/209 (count), L232 (decode), L299 (payload not object), L303 (rspCode != 0000); `_is_retryable_response` L213-224; `SnapshotFetchExhausted` L69; retry loop L260-324.
- CLI — `scripts/incremental_price_audit.py`: subcommand loop L87-116 (`if command == "closeout":` block is where closeout-only flags live), `main()` plumbing L512-537, JSON output L573-599.
- Runtime — `app/services/incremental_audit_runtime.py`: `run_runtime_command` L1845+ (CloseoutConfig built L1919-1924), closeout branch L2017-2044, TickConfig composition L2011-2015.
- Runner ps1 — `scripts/run_incremental_price_audit_scheduled.ps1`: routing L45-92 (20:35 is the 3rd OR clause of the closeout branch), `Get-PhaseArguments` L101-115, `Invoke-Phase` L184-226, Preview JSON L117-143.
- Force-finalize precedent for provenance-in-metrics: `materialize_forced_frozen_daily_run` finalize.py L1020-1152 (`update_daily_metrics` with `force_finalize*` keys).
- Test harnesses: `tests/test_closeout_transient_recovery.py` `_Harness` L78-127 (direct `run_closeout` with `CloseoutConfig`); `tests/test_closeout_before_1830.py` `CloseoutHarness` L270-392 (runtime-level `run_runtime_command("closeout", dependencies=..., materialize=stub)`); `tests/test_incremental_price_audit_finalize.py` `_seed` L40-148 (full store seeding incl. `freeze_snapshot`+stages); `tests/test_cnnc_saas_snapshot_adapter.py` `_Opener` L15-84; ps1 preview tests `tests/test_incremental_audit_operations.py` L908-1018.

## Global Constraints

- BASE_SHA = `89aa9b080f0fb42d9fc3765cab80c115230e0104`; dev branch `fix/cached-degraded-finalization-20260901`; worktree `E:\.A中核运维\cnnc-ops-agent-wt-cdf`. NO merge/deploy/production-branch changes; never touch `runtime/` production SQLite.
- Strict-path behavior must be unchanged when `allow_cached_finalize` is false (default).
- NEVER `except Exception → cached finalize`. Fallback whitelist (exact string names, matched as `error.split("(",1)[0]`): `SnapshotAuthExpiredError`, `SnapshotFetchExhausted`, `TimeoutError`, `ConnectionError`, `URLError`.
- No SQLite schema migration; provenance in `metrics_json` (and optionally manifest sidecar payload keys — free-form dict).
- No secrets in logs/errors: typed error strings carry only `side` + `rsp_code`/reason enum, never rspDesc text, tokens, cookies, or response bodies.
- Cached/historical path must never call discover/Detail/Model/submit/priority/harvest — structurally (no deps parameter) and by regression assertion (callback counters == 0).
- Windows: run all commands via cmd.exe-safe forms; validate the ps1 with `C:\Users\13013\.zcode\skills\windows-shell-verified\scripts\Test-PowerShellFile.ps1` before executing it in tests. pytest runs use `--basetemp=.pytest_tmp`.
- Commit split: (1) `fix(audit): add typed snapshot source diagnostics` (2) `feat(audit): add guarded cached degraded finalization` (3) `test(audit): cover cached closeout recovery` (tests may be folded per-task into 1/2 where natural).

---

### Task 1: Typed snapshot source diagnostics (adapter + `_safe_error`)

**Files:**
- Modify: `app/services/cnnc_saas_snapshot_adapter.py`
- Modify: `app/workflows/incremental_price_audit.py` (`_safe_error` only)
- Test: `tests/test_cnnc_saas_snapshot_adapter.py` (append), `tests/test_incremental_price_audit.py` (append small `_safe_error` block)

**Interfaces (produced for later tasks):**
- `class SnapshotSourceError(RuntimeError)` with attrs `side: str`, `rsp_code: str`; `__str__` → `f"{type(self).__name__}(side={self.side},code={self.rsp_code})"`.
- `class SnapshotAuthExpiredError(SnapshotSourceError)`; `class SnapshotResponseCodeError(SnapshotSourceError)`.
- `class SnapshotContractError(RuntimeError)` with attrs `side: str | None`, `reason: str` (one of `ROWS_NOT_OBJECTS`, `COUNT_INVALID`, `PAYLOAD_NOT_OBJECT`, `NOT_JSON_OBJECT`); `__str__` → `f"SnapshotContractError(side={self.side},reason={self.reason})"`.
- Auth-code vocabulary (module constant): `SNAPSHOT_AUTH_EXPIRED_RSP_CODES = frozenset({"401", "3333"})` and `SNAPSHOT_AUTH_EXPIRED_DESC_MARKERS = frozenset({"登录已过期", "请重新登录", "unauthorized"})` — a response is auth-expired when its rspCode is in the code set OR rspDesc contains a marker (desc is used for classification only, never persisted).
- `_safe_error(exc)` now returns `str(exc)` verbatim for `SnapshotSourceError`/`SnapshotContractError` instances (fixed vocabulary, safe).

- [ ] **Step 1: failing tests** — in `tests/test_cnnc_saas_snapshot_adapter.py` add:

```python
def test_auth_expired_rsp_code_raises_typed_error():
    opener = _Opener([
        _Response(200, {"rspCode": "3333", "rspDesc": "登录已过期,请重新登录", "success": False, "data": ""}),
        _Response(200, {"rspCode": "3333", "rspDesc": "登录已过期,请重新登录", "success": False, "data": ""}),
    ])
    adapter = _adapter(opener)
    with pytest.raises(SnapshotAuthExpiredError) as excinfo:
        adapter.fetch()
    assert excinfo.value.side == "saas"
    assert excinfo.value.rsp_code == "3333"
    assert str(excinfo.value) == "SnapshotAuthExpiredError(side=saas,code=3333)"

def test_non_auth_bad_rsp_code_raises_response_code_error():
    opener = _Opener([_Response(200, {"rspCode": "5001", "rspDesc": "boom", "success": False})] * 2)
    with pytest.raises(SnapshotResponseCodeError) as excinfo:
        _adapter(_Opener([_Response(200, {"rspCode": "5001", "rspDesc": "x", "success": False})] * 2)).fetch()
    assert excinfo.value.rsp_code == "5001"

def test_rows_contract_violation_raises_contract_error():
    # rspCode 0000 but rows is a string
    opener = _Opener([_Response(200, {"rspCode": "0000", "count": 1, "rows": "nope"})] * 2)
    with pytest.raises(SnapshotContractError) as excinfo:
        _adapter(opener).fetch()
    assert excinfo.value.reason == "ROWS_NOT_OBJECTS"
```

(Reuse the file's existing `_Opener`/`_Response`/`_adapter` helpers; check their exact constructor signatures at tests L15-84 first and adapt. Note `_is_retryable_exception` must NOT retry these typed errors — add `with pytest.raises(SnapshotAuthExpiredError)` reaching the caller on first attempt: assert `len(opener.requests) == 1`.)

In `tests/test_incremental_price_audit.py` append:

```python
def test_safe_error_keeps_typed_snapshot_source_errors():
    from app.services.cnnc_saas_snapshot_adapter import SnapshotAuthExpiredError
    assert _safe_error(SnapshotAuthExpiredError("customer", "3333")) == "SnapshotAuthExpiredError(side=customer,code=3333)"
```

- [ ] **Step 2: run, verify fail** — `python -m pytest tests/test_cnnc_saas_snapshot_adapter.py -k "auth_expired or response_code or contract" tests/test_incremental_price_audit.py -k safe_error_typed -x --basetemp=.pytest_tmp`
- [ ] **Step 3: implement** — add the four exception classes + constants after `SnapshotConsistencyDrift` (adapter L105-110 area). In `_request_page`: replace L299-303 raise block with: if auth-expired (rspCode in set or marker in `str(payload.get("rspDesc") or "")`) → `raise SnapshotAuthExpiredError(side, str(payload.get("rspCode") or "")) from None`; elif retryable-response (existing check) → `_RetryableSnapshotResponse`; else → `SnapshotResponseCodeError(side, str(payload.get("rspCode") or ""))`. Wrap the `self._decode_response(response)` call in the non-auth_manager branch: `except RuntimeError → raise SnapshotContractError(side, "NOT_JSON_OBJECT") from exc`. Change `_rows_from_response` L193 → `SnapshotContractError(side, "ROWS_NOT_OBJECTS")`; `_count_from_response` L203/207/209 → `SnapshotContractError(side, "COUNT_INVALID")`. Ensure `_is_retryable_exception` returns False for these (it already only whitelists URLError/TimeoutError/OSError/JSONDecodeError/_RetryableSnapshotResponse — no change needed; verify by test).
  In `_safe_error` (workflow L255): first clause `if isinstance(exc, (SnapshotSourceError, SnapshotContractError)): return str(exc)` with import from `app.services.cnnc_saas_snapshot_adapter`.
- [ ] **Step 4: run adapter + workflow targeted tests green.**
- [ ] **Step 5: commit** — `fix(audit): add typed snapshot source diagnostics`

---

### Task 2: Cached finalization core (evaluator + runner)

**Files:**
- Create: `app/services/incremental_audit_cached_finalize.py`
- Test: `tests/test_incremental_audit_cached_finalize.py` (new)

**Interfaces:**
- Consumes: `DailyAuditStore` (`get_run/list_snapshots/ordered_snapshot_items/get_snapshot/acquire_lease/release_lease/freeze_snapshot/prepare_finalization/mark_finalized/update_daily_metrics/list_stage_work/list_crawl_work/list_remote_jobs`), `ProcessLock`/`LeaseUnavailable`, `source_stability_seconds` (workflow L279), `build_frozen_audit_artifact`/`FinalizeBlocked`/`FinalizePublication` (finalize.py), `LegacyWorkbookMaterializer`.
- Produces:

```python
CACHED_FINALIZATION_MODE = "CACHED_DEGRADED"
STRICT_FINALIZATION_MODE = "STRICT"

@dataclass(frozen=True)
class CachedFinalizePlan:      # pure-read evaluation result
    snapshot_id: int; row_count: int; ordered_hash: str
    observed_at: str; stability_seconds: float; tail_run_length: int

class CachedFinalizeBlocked(RuntimeError):
    def __init__(self, reason_code: str, detail: Mapping[str, Any]): ...

@dataclass(frozen=True)
class CachedFinalizeOutcome:
    outcome: str                        # "CACHED_FINALIZED" | "ALREADY_FINALIZED"
    publication: FinalizePublication | None
    cached_snapshot_id: int | None
    last_source_observed_at: str | None
    source_stability_seconds: float

def evaluate_cached_finalization_readiness(
    store, token, *, cutoff_at: datetime | None, now: datetime,
    source_stability_threshold_seconds: float,
) -> CachedFinalizePlan:   # raises CachedFinalizeBlocked(reason_code, detail)

def run_cached_finalization(
    store, process_lock, business_date: str, output_path: Path | str, *,
    cutoff_at: datetime | None, now: datetime,
    source_stability_threshold_seconds: float,
    degraded_reason_code: str, degraded_source_side: str,
    materializer: LegacyWorkbookMaterializer | None = None,
) -> CachedFinalizeOutcome:  # raises CachedFinalizeBlocked
```

Reason-code enum (fixed): `BEFORE_CUTOFF`, `NO_RUN`, `NO_SNAPSHOTS`, `SINGLE_OBSERVATION`, `TAIL_CONTENT_DIFFERS`, `STABILITY_BELOW_THRESHOLD`, `MEMBERS_INCOMPLETE`, `SAAS_COUNT_MISMATCH`, `STAGE_PENDING`, `CRAWL_PENDING`, `REMOTE_PENDING_OR_MANUAL`, `NOT_FROZEN_CONFLICT`.

Gate mapping (all evaluated pre-freeze, pure reads): Gate10 `now >= cutoff_at` (BEFORE_CUTOFT typo guard — exact string `BEFORE_CUTOFF`); Gate1 snapshots from `store.list_snapshots(business_date)`; Gate4 candidate = last element (by observed_at,id order); Gate2 walk tail backwards while `(row_count, ordered_hash)` equals candidate → `tail_run_length >= 2` else SINGLE_OBSERVATION (0/1 snapshot) or TAIL_CONTENT_DIFFERS; Gate3 `source_stability_seconds(store, business_date, candidate) + 1e-9 >= threshold` else STABILITY_BELOW_THRESHOLD (detail includes computed stability); Gate5 `len(store.ordered_snapshot_items(id)) == row_count` and `evidence_json` parses to dict with `saas_count == row_count` else MEMBERS_INCOMPLETE / SAAS_COUNT_MISMATCH; Gate6 `list_stage_work` per stage: states `{"PENDING","CLAIMED","RETRYABLE_FAILED"}` → pending, `TERMINAL_FAILED` → terminal → STAGE_PENDING (detail counts by stage+state); Gate7 `list_crawl_work(token, states={"PENDING","PRIORITY_PENDING","SUBMITTED","RUNNING","EXPORTED","RETRYABLE_FAILED","SUBMIT_UNKNOWN"})` non-empty → CRAWL_PENDING; Gate8 `list_remote_jobs(token, states={"SUBMIT_INTENT","SUBMIT_UNKNOWN","PRIORITY_PENDING","SUBMITTED","RUNNING","EXPORTED","MANUAL_REQUIRED"})` non-empty → REMOTE_PENDING_OR_MANUAL.

Runner sequence (single lock + single lease, mirroring `materialize_frozen_daily_run` L862-903 but with cached freeze first):
1. `run = store.get_run(business_date)`; if FINALIZED → return `CachedFinalizeOutcome("ALREADY_FINALIZED", None, run.frozen_snapshot_id, None, 0.0)`.
2. `process_lock.acquire()` (LeaseUnavailable → `CachedFinalizeBlocked("LOCK_HELD", {...})`); `token = store.acquire_lease(business_date, f"cached-finalize-{os.getpid()}", ttl_seconds=600, allow_unexpired_takeover=True, process_lock=process_lock)`.
3. Re-read run (FINALIZED → already path). `plan = evaluate_cached_finalization_readiness(...)` (raises Blocked → finally release).
4. Existing `run["frozen_snapshot_id"]`: if not None and != plan.snapshot_id → Blocked(NOT_FROZEN_CONFLICT); if None → `store.freeze_snapshot(token, plan.snapshot_id)`.
5. `built = build_frozen_audit_artifact(store, token, output_identity=f"daily-final-{business_date.replace('-','')}")`; if `not built.readiness.ready or built.artifact is None` → Blocked("READINESS_BLOCKED", {"readiness": asdict(built.readiness)}).
6. `store.prepare_finalization(token, str(resolved_output))`; `result = (materializer or LegacyWorkbookMaterializer()).materialize(built.artifact, resolved_output)`; `store.mark_finalized(token, str(resolved_output), result.manifest_sha256)`.
7. `store.update_daily_metrics(token, {"finalized_rows": result.row_count, "finalization_mode": CACHED_FINALIZATION_MODE, "cached_snapshot_id": plan.snapshot_id, "cached_last_source_observed_at": plan.observed_at, "cached_source_stability_seconds": plan.stability_seconds, "degraded_reason_code": degraded_reason_code, "degraded_source_side": degraded_source_side, "cached_finalized_at": datetime.now(UTC).isoformat(timespec="seconds"), "frozen_snapshot_id": plan.snapshot_id})`.
8. Return outcome. `finally: release lease, release lock`.

- [ ] **Step 1: write failing tests** — new file `tests/test_incremental_audit_cached_finalize.py`. Model the seeding on `tests/test_incremental_price_audit_finalize.py::_seed` (L40-148): build a full-good day (3 snapshots same hash at 04:00/05:00/07:00Z, 9 items, DETAIL/MODEL SUCCESS, crawl SUCCESS + remote COMPLETED via `prepare_remote_intent`+`reconcile_remote_job`), param `cutoff_at=18:00-local equivalent`, then per Test A-J mutate one thing:

```python
def _blocked_reason(excinfo): return excinfo.value.reason_code
# A: only one snapshot -> SINGLE_OBSERVATION
# B: last two hashes differ (ingest changed rows) -> TAIL_CONTENT_DIFFERS
# C: two identical snapshots 300s apart, threshold 600 -> STABILITY_BELOW_THRESHOLD
# D: one MODEL row back to PENDING -> STAGE_PENDING
# E: one MODEL row TERMINAL_FAILED -> STAGE_PENDING
# F: one crawl row state PENDING -> CRAWL_PENDING
# G: remote job SUBMIT_UNKNOWN -> REMOTE_PENDING_OR_MANUAL
# H: remote job MANUAL_REQUIRED -> REMOTE_PENDING_OR_MANUAL
# I: delete one snapshot_members row -> MEMBERS_INCOMPLETE
# J: now < cutoff -> BEFORE_CUTOFF
# happy path: run_cached_finalization -> FINALIZED, output exists,
#   metrics finalization_mode == "CACHED_DEGRADED", frozen_snapshot_id == last snapshot id,
#   materializer stub (reuse LegacyWorkbookMaterializer(writer=fake_writer) pattern from
#   tests/test_incremental_audit_materializer.py::_artifact/write) invoked exactly once;
#   idempotent second call -> outcome ALREADY_FINALIZED, no second workbook write;
#   already-strict-frozen same snapshot id -> proceeds to materialize (same-id freeze is idempotent).
```

- [ ] **Step 2: run to failure** (`python -m pytest tests/test_incremental_audit_cached_finalize.py -x --basetemp=.pytest_tmp` — ModuleNotFoundError).
- [ ] **Step 3: implement module** exactly per Interfaces; docstring states the ten gates and the no-discover guarantee (the module takes no TickDependencies and performs no network I/O — make that explicit in the docstring).
- [ ] **Step 4: green.**
- [ ] **Step 5: commit** — fold into commit (2) `feat(audit): add guarded cached degraded finalization`.

---

### Task 3: Closeout integration (classification + guarded fallback)

**Files:**
- Modify: `app/services/incremental_audit_closeout.py`
- Test: `tests/test_closeout_transient_recovery.py` (append)

**Interfaces:**
- Consumes: Task 1 typed names; Task 2 `run_cached_finalization`/`CachedFinalizeBlocked`.
- Produces: `CloseoutConfig.allow_cached_finalize: bool = False` (frozen dataclass field; add to `__post_init__` nothing — bool needs no validation); module constant `_ELIGIBLE_CACHED_SOURCE_UNAVAILABLE_NAMES = frozenset({"SnapshotAuthExpiredError", "SnapshotFetchExhausted", "TimeoutError", "ConnectionError", "URLError"})`; helper `_is_eligible_source_unavailable(summary) -> bool` (same leading-type-name extraction as L101); `run_closeout` gains keyword-only `cached_finalize: Callable[..., CachedFinalizeOutcome] = run_cached_finalization` (injectable for tests).

Loop change (inside the `summary.outcome in {"FAILED", "DISCOVERY_INCOMPLETE"}` branch, L253-274): before each `return merged(...)` that exits with FAILED (the permanent return, the unknown-exceeded return, and the deadline return), if `closeout.allow_cached_finalize` and `_is_eligible_source_unavailable(summary)` and `wall_deadline - monotonic() >= MIN_MATERIALIZE_SECONDS`: try `outcome = cached_finalize(store, process_lock, config.business_date, Path(output_path), cutoff_at=config.cutoff_utc, now=deps.wall_clock(), source_stability_threshold_seconds=config.source_stability_seconds, degraded_reason_code=_cached_reason_code(summary), degraded_source_side=_cached_source_side(summary))` → on success `trace.append("cached_finalized")` and `return merged("CACHED_FINALIZED"), outcome.publication, None, None`; on `CachedFinalizeBlocked as exc` → `trace.append(f"cached_blocked:{exc.reason_code}")` and fall through to the original return. `_cached_reason_code`: `"SOURCE_AUTH_UNAVAILABLE"` when auth-expired name else `"SOURCE_UNAVAILABLE"`; `_cached_source_side`: parse `side=...` from `summary.error` when present else `"unknown"`.

`_PERMANENT_CLOSEOUT_FAILURE_NAMES` += `{"SnapshotAuthExpiredError", "SnapshotResponseCodeError", "SnapshotContractError"}` (auth fails fast in strict passes; they are deterministic until a human replaces the curl).

- [ ] **Step 1: failing tests** (use `_Harness` pattern; deps.discover raises the given exception):

```python
def test_ordinary_closeout_never_cache_finalizes_on_auth_expired():   # Test K
    # allow_cached_finalize absent (default False); discover raises SnapshotAuthExpiredError("customer","3333")
    # -> final summary outcome == "FAILED"; materialize stub never called; no frozen_snapshot_id
def test_safety_closeout_generic_runtime_error_stays_strict():        # Test L
    # allow_cached_finalize=True; discover raises RuntimeError("boom") -> FAILED, no cached attempt (trace lacks "cached_finalized")
def test_safety_closeout_auth_expired_caches_final():                 # Test M
    # allow_cached_finalize=True; discover raises SnapshotAuthExpiredError("customer","3333"); store pre-seeded
    # full-good day (3 identical snapshots) -> outcome "CACHED_FINALIZED", publication not None,
    # run frozen_snapshot_id == last snapshot, metrics finalization_mode == CACHED_DEGRADED,
    # discover called >= 1 (strict attempt first), detail/model callbacks never invoked
def test_strict_recovery_prefers_strict_finalization():               # Test N
    # allow_cached_finalize=True; discover succeeds (2 identical observations) -> normal STRICT path:
    # outcome COMPLETED, frozen via tick, metrics have NO finalization_mode key,
    # cached_finalize stub (injected sentinel) never called
```

- [ ] **Step 2: red → Step 3: implement → Step 4: green.**
- [ ] **Step 5: commit** (2).

---

### Task 4: CLI flag + `cached-finalize` subcommand + runtime plumbing + status fields

**Files:**
- Modify: `scripts/incremental_price_audit.py`
- Modify: `app/services/incremental_audit_runtime.py`
- Test: `tests/test_closeout_before_1830.py` (append runtime-level), `tests/test_incremental_price_audit.py` (CLI parse test)

**Interfaces:**
- `run_runtime_command(..., allow_cached_finalize: bool = False, ...)` → threaded into `CloseoutConfig(allow_cached_finalize=...)`; accepted `command == "cached-finalize"`.
- Runtime `cached-finalize` command: does NOT build TickDependencies / does NOT discover — constructs paths/store/lock and calls `run_cached_finalization` directly with `cutoff_at` from the run row (`store.get_run(...)["cutoff_at"]`, parse ISO), `now=wall clock`, threshold from `--source-stability-seconds` (add this arg to the subcommand, default 600); maps `CachedFinalizeBlocked` → JSON `{"outcome": "CACHED_FINALIZE_BLOCKED", "reason_code": ...}` exit 2; success → `{"outcome": "CACHED_FINALIZED", "cached_snapshot_id": ..., "publication": path}` exit 0; ALREADY_FINALIZED → outcome `ALREADY_FINALIZED` exit 0.
- CLI: `--allow-cached-finalize` (store_true) inside the `if command == "closeout":` block (L98-116); new `cached-finalize` subcommand beside `force-finalize` (L280) using `_add_common_arguments` + `--output` + `--source-stability-seconds`.
- Status: in the runtime status collection (locate `collect_runtime_status`), the `final` section gains `finalization_mode`, `last_source_observed_at`, `degraded_reason_code`, `degraded_source_side` read from `daily_runs.metrics_json` (missing → `None`/absent keys). Strict days simply have no `finalization_mode` metric — emit `"STRICT"` only when run FINALIZED and metric absent? NO: emit the metric value only; strict finalize writes `finalization_mode=STRICT` at mark time is out of scope — keep status faithful to metrics (null means strict/unknown). Machine-readable contract: `final.finalization_mode ∈ {None, "CACHED_DEGRADED"}` this round.

- [ ] **Step 1: failing tests** — CLI parse (`build_parser().parse_args(["closeout","--allow-cached-finalize"])` → attr True); runtime-level: extend `CloseoutHarness.run()` with `allow_cached=False` kwarg → passes through to closeout config (assert via a stub cached sentinel); `cached-finalize` runtime command on a seeded full-good frozen-less day → FINALIZED without any discover (deps not constructed — assert by monkeypatching `build_default_dependencies` to raise if called).
- [ ] **Step 2-4: red/implement/green.**
- [ ] **Step 5: commit** (2).

---

### Task 5: Scheduler 20:35 routing

**Files:**
- Modify: `scripts/run_incremental_price_audit_scheduled.ps1`
- Test: `tests/test_incremental_audit_operations.py` (append to the routing table test at L908-1018)

- [ ] **Step 1: failing test** — add cases to the existing parametrized expectations: `("2026-09-01T20:35:00", ["closeout"])` asserts `"--allow-cached-finalize" in plan["Commands"][0]["Arguments"]`; `("2026-09-01T18:00:00", ...)` and `("2026-09-01T20:00:00", ...)` assert it is NOT present. Follow the existing `_run_powershell_script` + `-Preview -At` JSON assertions.
- [ ] **Step 2: red.** **Step 3: implement** — in the closeout `elseif` branch compute `$allowCachedFinalize = ($hour -eq 20 -and $minute -eq 35)` (still inside weekday guard); change `Get-PhaseArguments([string]$Phase, [int]$PhaseBudgetSeconds)` → add third param `[bool]$AllowCachedFinalize = $false`; append `if ($Phase -eq "closeout" -and $AllowCachedFinalize) { $arguments += "--allow-cached-finalize" }` before the invocation/mode lines; update both call sites (`$Preview` block L117-143 and `Invoke-Phase` L184-226 — thread the bool through `Invoke-Phase` the same way). Also surface `"AllowCachedFinalize": $allowCachedFinalize` in the Preview JSON for debuggability. **Step 4: validate ps1** with `powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\13013\.zcode\skills\windows-shell-verified\scripts\Test-PowerShellFile.ps1 -Path <script>` (0 parse errors) then run the tests green.
- [ ] **Step 5: commit** (2).

---

### Task 6: 2026-08-31 end-to-end regression fixture

**Files:**
- Test: `tests/test_incremental_audit_cached_finalize.py` (append) — or a sibling `tests/test_cached_finalization_20260831_regression.py` if cleaner

- [ ] **Step 1: write the regression test** reconstructing the incident at runtime-command level (modeled on `CloseoutHarness`, `tests/test_closeout_before_1830.py` L270-392): business_date `2026-08-31`; snapshots at 04:00:08Z/05:00:05Z/07:00:05Z (hash `81194f86…` style — any fixed identical hash) with 9 items; DETAIL/MODEL SUCCESS ×9; crawl SUCCESS ×9 bound to remote jobs COMPLETED ×4; `cutoff_at` 10:00Z; deps.discover raises `SnapshotAuthExpiredError("customer","3333")` with a call counter; wall clock at 20:35 local; closeout `allow_cached_finalize=True`.
  Assertions: (a) `outcome == "CACHED_FINALIZED"`, workbook exists at final path; (b) `frozen_snapshot_id == 3`; (c) metrics `finalization_mode == "CACHED_DEGRADED"`, `degraded_reason_code == "SOURCE_AUTH_UNAVAILABLE"`, `degraded_source_side == "customer"`, `cached_last_source_observed_at == "2026-08-31T07:00:05+00:00"`; (d) **detail/model/plan/submit/submit_gate/harvest callback counters all == 0** (deps closures count invocations); discover counter >= 1 (strict attempted first). (e) Control: same fixture with `allow_cached_finalize=False` → outcome FAILED, no workbook, frozen_snapshot_id None — proving old behavior (NO FINAL) is preserved for ordinary closeouts.
- [ ] **Step 2: red → implement gaps (should be none if Tasks 2-5 hold; fix integration bugs here) → green.**
- [ ] **Step 3: commit** (3) `test(audit): cover cached closeout recovery`.

---

### Task 7: Full validation + push

- [ ] Targeted suites: `tests/test_cnnc_saas_snapshot_adapter.py tests/test_closeout_transient_recovery.py tests/test_closeout_before_1830.py tests/test_incremental_price_audit_finalize.py tests/test_incremental_audit_cached_finalize.py tests/test_incremental_audit_operations.py tests/test_incremental_price_audit_force_finalize.py tests/test_daily_audit_store.py`
- [ ] Full suite: `python -m pytest --basetemp=.pytest_tmp -q` — record collected/passed/failed/skipped/duration. Baseline: run the same full suite on `89aa9b0` (e.g. `git -C <main repo> stash-free worktree or use the production checkout E:\.A中核运维\cnnc-ops-agent`) and diff failing test names; zero new failures allowed.
- [ ] Self-review checklist (spec §REVIEW) — 13 items, each answered in final report.
- [ ] `git push origin fix/cached-degraded-finalization-20260901` — NO merge, NO deploy.
