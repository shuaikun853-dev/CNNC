# STEP_1 Remote Stall Recovery — Round 5 Spec (Root Cause)

Round 5 of the independent-review fix cycle on
`fix/step1-remote-running-stall-recovery-20260825` (base `d97bf906`, prod
`6efdc79c`). Scope is exactly three findings: BLOCKER A, IMPORTANT B,
IMPORTANT C.

## BLOCKER A — pre-write failures misclassified as DISABLE_UNKNOWN

### Root cause (verified in code)

1. `CnncLegacyZhaoshangAdapter._production_restart_running_task`
   (app/services/legacy_zhaoshang_adapter.py:1794) lets exceptions raised
   BEFORE the first remote write escape to the caller. Escape sites on the
   fresh (`resume_phase=None`) path: argument/phase validation, external-tool
   module load, `auth_manager.export_environment(refresh=True)` (AuthError
   wraps TimeoutError/URLError), session-env setup, the exact `lookup()`
   HTTP timeout, write-time `query_task_status`/`query_task_progress`
   timeouts, and every `deadline - monotonic() <= 0` check before the
   disable call.
2. The runtime bridge `recovery_restart()`
   (app/services/incremental_audit_runtime.py:1490) catches every escape with
   `classify_restart_exception(exc, resume_phase)`, which maps
   TimeoutError/ConnectionError/OSError and marker-less RuntimeError with
   `resume_phase=None` to `DISABLE_UNKNOWN`.
3. `_apply_restart()` had already durably written the write-ahead phase
   `DISABLE_SENT` before the adapter call, and on `DISABLE_UNKNOWN` it keeps
   it. Next tick `DISABLE_SENT` ∈ PENDING_RESTART_PHASES → reconcile-only →
   the disable was never actually sent → `DISABLE_UNCONFIRMED` → 900 s →
   `MANUAL_REQUIRED`. A single transient pre-write network fault wastes the
   only automatic recovery attempt.

Live evidence: `test_round4_c7_auth_refresh_timeout_dispatches_no_remote_write`
asserts `pytest.raises(AuthError)` — the escape is the bug.

### Fix design

- New `RestartStatus.PRE_WRITE_FAILED` (zero-write, retryable).
- The adapter owns the side-effect boundary with two write-boundary flags:
  `disable_write_started` is set immediately before `tool.disable_task(...)`,
  `enable_write_started` immediately before `tool.enable_task(...)`. One
  outer `except Exception` converts escapes into explicit outcomes:
  - enable started (or resume ENABLE_SENT) → `ENABLE_UNKNOWN`
  - disable started (or resume DISABLE_SENT/DISABLE_CONFIRMED) → `DISABLE_UNKNOWN`
  - otherwise → pre-write classification: identity-shaped
    (RuntimeConfigurationError/ValueError/KeyError/TypeError or RuntimeError
    with 不唯一/不一致/没有拿到/identity markers) → `IDENTITY_CONFLICT`;
    everything else → `PRE_WRITE_FAILED`.
  The production adapter then never lets an exception escape; internally
  handled disable/enable outcomes are unchanged.
- `_apply_restart()` gains a `PRE_WRITE_FAILED` branch that withdraws the
  write-ahead `DISABLE_SENT` into a durable zero-write
  `phase="PRE_WRITE_FAILED"` record (diagnostic kept, `recovery_attempts`
  untouched, job stays RUNNING). The phase is NOT in PENDING_RESTART_PHASES,
  so the next tick re-enters fresh recovery with `resume_phase=None`.
- `classify_restart_exception` stays as last-resort defense for injected
  runners / unforeseen escapes; unprovable fresh escapes remain
  reconcile-only (fail toward manual, never toward a second write).

## IMPORTANT B — auth refresh lock wait not covered by the deadline

### Root cause (verified in code)

`AuthManager.refresh()` (scripts/auth_manager.py:373) takes
`self._locked(system)` — a blocking `threading.RLock` plus a blocking
process file lock (`msvcrt.locking(LK_LOCK)` on Windows: ~10 retries 1 s
apart then OSError; `fcntl.flock(LOCK_EX)` on POSIX: unbounded) — BEFORE
`_request_refresh()` runs its HTTP. The `timeout_seconds` passed by the
recovery adapter is a static budget computed before those waits, so the HTTP
request can still receive the full initial remainder after the locks finally
release. `_save_tokens` takes a second nested `state` lock after the HTTP.

### Fix design (minimal, no lock-subsystem rewrite)

- New optional keyword args threading through
  `export_environment → refresh → _request_refresh`:
  `deadline: float | None` (absolute monotonic) and
  `monotonic: Callable[[], float] | None` (defaults `time.monotonic`).
- `_request_refresh` recomputes `remaining = deadline - clock()` immediately
  before `opener.open`; `remaining <= 0` → `TimeoutError` with ZERO auth
  HTTP; otherwise `timeout = min(30, remaining)`. A passed `deadline`
  takes precedence over `timeout_seconds`; all existing call sites
  (no deadline) keep the 30 s default exactly.
- The locks themselves stay blocking (documented Remaining Risk: AUTH LOCK
  WAIT IS NOT HARD-CANCELABLE). The guarantee instead: lock wait consumes
  the same absolute deadline (zero HTTP after exhaustion), and the adapter
  re-checks its callback deadline after `export_environment` returns, so no
  remote write is ever dispatched past the deadline.
- `_save_tokens` intentionally not deadline-gated: once the refresh HTTP
  succeeded, persisting rotated tokens is mandatory (losing them would
  invalidate the refresh chain); the adapter deadline check after return
  still prevents any remote write past the deadline.
- The probe/restart adapters now pass `deadline=deadline,
  monotonic=self._monotonic` alongside the existing `timeout_seconds`.

## IMPORTANT C — business-failed status/progress responses trusted as evidence

### Root cause (verified in code + external tool)

`_production_probe_running_task` and the write-time revalidation parse
`rspDesc` from `query_task_status`/`query_task_progress` responses without
checking `success`/`rspCode`. A
`{"success": false, "rspCode": "8888", "rspDesc": "总任务量：10条，待执行：4条…"}`
payload parses into ACTIVE-looking counters → `RemoteExecutionState.ACTIVE`
→ stall evidence / fresh-restart write authorization. A failed status
response carrying the terminal marker would be trusted as TERMINAL.

External-tool evidence (auto_crawl_task.py, read-only):
- write endpoints (L447/L604): business success ⇔ `success is not False`
  AND `rspCode ∈ {None, "0000"}`;
- `is_progress_finished` (L681) trusts progress counters only when
  `progress_json.get("success")` is truthy.

### Fix design

- New `remote_business_response_ok(payload)` in remote_running_recovery.py
  mirroring the tool's write-endpoint rule: payload must be a Mapping;
  if `success` is present it must be exactly `True`; if `rspCode` is present
  and non-empty it must be `"0000"` (PROVEN success set). Absent fields stay
  admissible (the platform legitimately omits rspCode).
- Both read paths strip business-failed components to their unprovable form
  before parsing: status → `""` (terminal marker cannot come from a failed
  response), progress → `""` (counters parse to all-None → UNKNOWN).
  Existing fail-closed machinery (signature rejection, UNKNOWN never
  authorizes writes, write-gate declines non-ACTIVE) then enforces the rest.
- Export `count=0` / no-price logic is untouched (out of scope by review).
