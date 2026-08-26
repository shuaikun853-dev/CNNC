# STEP_1 Remote Stall Recovery Review Fix Round 7 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three Round 6 review findings — BLOCKER A (a transport-success Mapping is wrongly equated to a usable recovery probe), IMPORTANT B (a proven exact remote identity conflict takes 5 transient failures instead of immediate manual), MINOR C (the probe-failure cap error text claims "no restart was attempted" even after a confirmed restart).

**Architecture:** The usability contract moves to the adapter (`legacy_zhaoshang_adapter.py`), the layer with the least information loss: `_production_probe_running_task` raises a typed `LegacyProbeEvidenceError` when a probe completes with no usable execution evidence (no ACTIVE/TERMINAL progress, no trusted terminal status, no actual export result), and a typed `RemoteTaskIdentityConflictError` for proven identity conflicts. The runtime bridge translates the typed exceptions into distinct envelopes (`probe_failed`+`UNUSABLE_EVIDENCE` vs `probe_identity_conflict`); the recovery pass counts the former durably and fail-closes the latter immediately through a new atomic store transition. The store's probe-failure cap gains truthful error text derived from the durable restart history.

**Tech Stack:** Python 3.14, pytest, SQLite via `DailyAuditStore`, existing fake-tool test harness in `tests/test_remote_running_recovery.py`.

**Spec:** Round 7 review directive (pasted task text); production history `origin/prod/closeout-20260818` @ `6efdc79c5bb10605f05f2931066a833f986952e5`; dev branch `fix/step1-remote-running-stall-recovery-20260825` @ Round 6 HEAD `77a73a44522e75a5f45c3cfd795643c6c955e553`.

## Global Constraints

- Append-only on the dev branch: no rebase / squash / amend / reset / cherry-pick to prod / new branch / force push / merge PR #1 / deploy / production SQLite / real zhaoshang remote tasks / Job 53 / Job 88.
- Round 6 closed findings (§3 of the directive, items 1-22) must not regress; do not touch Scheduler, AuthManager, Force Finalize, finalize algorithm, external tool, ordinary harvest semantics, strict NO_RESULT, SUBMIT_UNKNOWN.
- Probe usability applies ONLY to the RUNNING-stall recovery probe — never ordinary harvest, never `probe_export_no_price`, never the restart protocol's own gates.
- Terminal execution state and `export_had_result=True` are SUCCESS probe evidence (reset the streak); only unprovable evidence is a failure.
- Legal Job53 shape (healthy ACTIVE progress + stationary signature + count=0 + `未检索到商品价格信息`) stays a legal stall observation, NOT a probe failure.
- Message bodies never enter durable storage; only exception TYPE names are recorded (existing convention).
- No message-grep in the runtime bridge; the adapter wraps the unmodifiable external tool's documented identity-failure markers at its own boundary.
- Tests must run with `set PYTHONIOENCODING=utf-8` and `-p no:cacheprovider --basetemp=_run\basetemp-r7` on Windows cmd.

## Root Causes (systematic-debugging Phase 1 findings)

1. **BLOCKER A** — `legacy_zhaoshang_adapter.py:_production_probe_running_task` tail returns `RunningTaskProbe(progress={}, task_status_rsp_desc="", export_had_result=False, export_count=0)` when the business gate stripped both status and progress and the export endpoint answered no-result. The bridge (`incremental_audit_runtime.py:recovery_probe`) converts any non-exception return into a plain Mapping; `remote_running_recovery.py:run_running_recovery_pass` treats any Mapping without `probe_failed` as a success probe — it clears `probe_health` and appends a `signature=None` observation that can never prove a stall. A permanent business failure therefore never reaches the durable cap.
2. **IMPORTANT B** — the adapter raises generic `LegacyZhaoshangAdapterError("live task identity does not match the durable remote identity")` for id/number mismatch (`legacy_zhaoshang_adapter.py:1662-1665`); the tool-level identity failures (`精确任务不唯一` from `_one_exact_task`, `精确任务商城不匹配` from `_one_exact_task`, `商城标识不一致` from `get_task_identity`) escape as bare `RuntimeError`. The bridge catches `Exception` and returns the generic `probe_failed` envelope, so a proven conflict counts as transient failure #1..#5.
3. **MINOR C** — `daily_audit_store.py:record_remote_recovery_probe_failure` writes a fixed `"no restart was attempted"` headline once the cap trips, without reading `recovery_attempts` / `restart.phase`; post-restart probe failures (recovery_attempts==1, ENABLE_CONFIRMED) are misdescribed.

External tool semantics verified against `E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py`:
- `check_existing_task` → `_one_exact_task` raises `RuntimeError("精确任务不唯一: ...")` (multiple exact matches) or `RuntimeError("精确任务商城不匹配: ...")` (name matched, mall differs); `extract_records_from_query_result` never raises (returns `[]`), so those two are the ONLY RuntimeError escapes of `check_existing_task`.
- `get_task_identity` raises `RuntimeError("商城标识不一致: ...")` for mall mismatch and `RuntimeError("没有拿到 ...")` for malformed task payloads (missing taskId/taskNumber/mallLogo). Only the mall mismatch is a proven conflict; `没有拿到` stays a transient probe failure (unprovable, not a mismatch).
- `_one_exact_task` returning `None` (no name match) surfaces in the adapter as `"running task probe found no exact existing task"` — NOT an identity conflict (stays transient).

## File Structure

- Modify `app/services/legacy_zhaoshang_adapter.py` — typed exceptions + probe identity/evidence contract.
- Modify `app/services/incremental_audit_runtime.py` — `recovery_probe` bridge typed-envelope translation.
- Modify `app/services/remote_running_recovery.py` — identity-conflict immediate manual branch in `run_running_recovery_pass`.
- Modify `app/services/daily_audit_store.py` — `mark_remote_recovery_probe_identity_manual` + truthful cap error text.
- Modify `tests/test_remote_running_recovery.py` — Round 7 RED/GREEN tests + evolve two Round 5 tests to the new contract.

Three commits, mirroring the directive's suggested structure:
- Commit 28 `fix(remote): count unusable running probes as durable failures` (BLOCKER A + bridge envelope + tests)
- Commit 29 `fix(remote): fail closed immediately on probe identity conflict` (IMPORTANT B + store transition + tests)
- Commit 30 `fix(remote): report post-restart probe failure truthfully` (MINOR C + tests)

---

### Task 1 (Commit 28): BLOCKER A — probe usability contract at the adapter

**Files:**
- Modify: `app/services/legacy_zhaoshang_adapter.py` (exception block near line 235; `_production_probe_running_task` tail near line 1777)
- Modify: `app/services/incremental_audit_runtime.py` (`recovery_probe` near line 1444)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: `class LegacyProbeEvidenceError(LegacyZhaoshangAdapterError)`; adapter probe raises it when `export_had_result` is False and `classify_remote_execution_state(parsed_progress, status_desc)` is not ACTIVE/TERMINAL.
- Produces: bridge envelope `{"probe_failed": True, "failure_kind": "UNUSABLE_EVIDENCE", "diagnostic": "probe raised LegacyProbeEvidenceError"}`.
- Keeps: the three `export_had_result=True` early returns (workbook / download-url dict / embedded download string) untouched.

- [ ] **Step 1: RED tests A1-A10** (run first; all must fail against 77a73a4 except A6/A7/A8 which already pass — those prove the success semantics and must STAY green)

New tests in `tests/test_remote_running_recovery.py` under a `# Round 7, BLOCKER A` banner, using the existing `_RestartSession` / `_prepared_job` harness and `_run_probe_pass` / `_stalled_store` helpers:

```python
def test_round7_a1_business_failed_progress_five_times_goes_manual(tmp_path, monkeypatch) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES
    from app.services.incremental_audit_runtime import legacy_zhaoshang_bridge, runtime_paths
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter

    task = _running_task_record()
    session = _RestartSession(task, progress_success=False, status_payload={"success": False, "rspCode": "8888", "rspDesc": "boom"})
    paths = runtime_paths(BUSINESS_DATE, tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    adapter = CnncLegacyZhaoshangAdapter(mappings=[], db_env_path=None)
    monkeypatch.setattr(type(adapter), "_load_module",
        classmethod(lambda cls, path, name: _restart_tool_module(task, session)))
    _planner, _finalizer, _submitter, _harvester, probe_cb, _restart = legacy_zhaoshang_bridge(
        adapter, paths=paths, business_date=BUSINESS_DATE)
    durable = _durable_bridge_job(paths)  # helper (see Step 1b)

    outcome = probe_cb(durable, 60.0)
    assert outcome["probe_failed"] is True          # RED today: returns a plain Mapping

    store, job_id = _stalled_store(tmp_path)
    restart = _NeverRestart()
    for index in range(RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES):
        _run_probe_pass(store, lambda job: outcome, restart, now=NOW + timedelta(minutes=3 * index))
        if index < RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES - 1:
            assert store.get_remote_job(job_id)["state"] == "RUNNING"
    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    assert restart.calls == [] and int(job["recovery_attempts"]) == 0
```

- `A2`: progress business-failed + healthy terminal status (`status_rsp_desc="本次任务执行完毕"`, `status_success=True`) → adapter probe RETURNS normally (no raise) and the pass does NOT count a failure (`probe_health` stays absent; observation appended).
- `A3`: progress business-failed + `export_had_result=True` (fake exportTask returns workbook headers / `data.url` download dict) → probe returns, no failure count.
- `A4`: malformed progress (`progress_payload=["not","a","mapping"]`) + non-terminal status + count=0 → probe raises `LegacyProbeEvidenceError`; pass counts failure 1.
- `A5`: contradictory progress (`progress_rsp_desc` parsing to `finished+failed>total`) + no terminal + count=0 → raises; durable failure.
- `A6` (already green, keep): healthy ACTIVE progress → success observation, no failure count.
- `A7` (already green, keep): healthy TERMINAL progress (`_TERMINAL_PROGRESS_RSP`) → success probe, `probe_health` untouched/absent, no failure count.
- `A8` (already green, keep): `probe_health=3` then healthy ACTIVE → reset to absent + observation appended.
- `A9`: `probe_health=3` then actual export result (download URL) → reset.
- `A10`: status business-failed with terminal text (`status_success=False, status_rsp_desc="本次任务执行完毕"`) + healthy ACTIVE progress → probe returns ACTIVE, no failure count (mirror of round5_c3, now asserting no failure count too).
- **A11 (the §35 sequence)**: failure, failure, unusable Mapping (via bridge envelope), healthy ACTIVE → `probe_health` counts 1, 2, 3, then absent. RED today because the unusable Mapping is currently counted as a success (resets at step 3).
- **Evolve `test_round5_c1` / `test_round5_c5`**: they assert the OLD contract (probe returns with empty progress). Change to `pytest.raises(LegacyProbeEvidenceError)` while keeping their no-restart-dispatch halves.

**Step 1b: helper** — `_durable_bridge_job(paths)` builds the durable job Mapping the bridge's `recovery_probe` needs (same shape as `test_round6_recovery_probe_bridge_reports_failure_envelope`, but with REAL prepared-job paths so `reconstruct` succeeds): write `BATCH_STATE.md`, `input.xlsx`, `result.xlsx` through `adapter.prepare_job` like `_prepared_job` does, then return the durable dict with matching `logical_key`/`payload_hash`/`payload_json`/`input_path`/`result_path`/`remote_identity`. (Reuse `_prepared_job`'s body: make it optionally return `(adapter, prepared)` plus build the durable dict from `prepared.logical_job_key`, `prepared.payload_sha256`, and the real paths.)

- [ ] **Step 2: run the new tests, confirm RED** — `A1, A4, A5, A9*, A10*, A11` fail (`*` = the new assertion half fails); A2/A3 fail at the adapter level until the tail check lands (they currently pass as "returns normally" but the pass-level no-failure-count assertion may already pass — the RED discriminator is A1/A11). Record old failure output.
- [ ] **Step 3: implement the adapter contract**

```python
# exceptions (after LegacyPreSendFailure, ~line 251)
class LegacyProbeEvidenceError(LegacyZhaoshangAdapterError):
    """A read-only probe completed without any usable execution evidence."""

class RemoteTaskIdentityConflictError(LegacyZhaoshangAdapterError):
    """The live exact task provably conflicts with the durable remote identity."""
```

`_production_probe_running_task` tail — after computing `export_count`, before the final `return RunningTaskProbe(...)`:

```python
from app.services.remote_running_recovery import (
    RemoteExecutionState, classify_remote_execution_state,
)
execution_state = classify_remote_execution_state(parsed_progress, status_desc)
if execution_state not in (RemoteExecutionState.ACTIVE, RemoteExecutionState.TERMINAL):
    raise LegacyProbeEvidenceError(
        "running task probe returned no usable execution evidence "
        f"(execution_state={execution_state.value})"
    )
```

(The three `export_had_result=True` early returns stay above this check.)

- [ ] **Step 4: implement the bridge envelope** — in `recovery_probe`, insert BEFORE the generic `except Exception`:

```python
except LegacyProbeEvidenceError:
    return {
        "probe_failed": True,
        "failure_kind": "UNUSABLE_EVIDENCE",
        "diagnostic": "probe raised LegacyProbeEvidenceError",
    }
```

(import `LegacyProbeEvidenceError` from the adapter module next to the existing adapter imports.)

- [ ] **Step 5: run Task 1 tests** — GREEN; also run `python -m pytest tests\test_remote_running_recovery.py -q` fully green.
- [ ] **Step 6: commit** — `fix(remote): count unusable running probes as durable failures`

---

### Task 2 (Commit 29): IMPORTANT B — typed identity conflict, immediate zero-write manual

**Files:**
- Modify: `app/services/legacy_zhaoshang_adapter.py` (`_production_probe_running_task` identity branch, line ~1657-1665)
- Modify: `app/services/incremental_audit_runtime.py` (`recovery_probe` first except branch)
- Modify: `app/services/remote_running_recovery.py` (`run_running_recovery_pass` after `probe = probe_remote(job)`)
- Modify: `app/services/daily_audit_store.py` (new `mark_remote_recovery_probe_identity_manual` after `mark_remote_zero_write_manual`)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: `RemoteTaskIdentityConflictError(LegacyZhaoshangAdapterError)` raised for: live id/number mismatch vs durable; tool `check_existing_task` RuntimeError escapes (不唯一 / 商城不匹配); tool `get_task_identity` mall mismatch (商城标识不一致 marker). `没有拿到...` malformed payloads re-raise unchanged (transient).
- Produces: bridge envelope `{"probe_identity_conflict": True, "diagnostic": "probe raised RemoteTaskIdentityConflictError"}`.
- Produces: store `mark_remote_recovery_probe_identity_manual(token, job_id, *, remote_identity, evidence, error) -> dict` — preconditions: valid lease, state RUNNING, exact remote identity (NO recovery_attempts precondition: post-restart conflicts are legal); single transaction writes evidence + MANUAL_REQUIRED + error_text.
- Produces: recovery-pass evidence shape `"probe_health": {"identity_conflict": True, "terminal_reason": "REMOTE_IDENTITY_CONFLICT", "observed_at": <iso>, "diagnostic": <type-level text>, "consecutive_failures": <historical int if present>}`; `restart` document untouched; new counter `recovery_identity_manual`.

- [ ] **Step 1: RED tests B1-B6**

Adapter-level (fake tool raises the REAL tool messages; extend `_restart_tool_module` fakes in-test via subclasses or session/task tweaks):
- `B1`: task record with different `taskId` than durable → `pytest.raises(RemoteTaskIdentityConflictError)`.
- `B2`: different `taskNumber` → same.
- `B3a`: `check_existing_task` fake raising `RuntimeError("精确任务不唯一: task_name=..., mall_code=lxwl, count=2")` → conflict.
- `B3b`: `check_existing_task` fake raising `RuntimeError("精确任务商城不匹配: ...")` → conflict.
- `B3c`: `get_task_identity` fake raising `RuntimeError("商城标识不一致: expected=lxwl, actual=comix")` → conflict.
- `B4`: `get_task_identity` raising `RuntimeError("没有拿到 taskId/id...")` → NOT a conflict: plain RuntimeError escapes (`pytest.raises(RuntimeError)` minus conflict type), and at pass level counts `probe_health.consecutive_failures == 1` with job RUNNING. Also ordinary `TimeoutError` from lookup → count 1 (already covered by round6_b1b shape; assert job RUNNING, no identity manual).

Pass-level (envelope `{"probe_identity_conflict": True, ...}`):
- `B5` (bridge→pass e2e, §33 chain): durable job seeded RUNNING with `probe_health.consecutive_failures=3`; one pass with a probe callback built from the REAL adapter (fake tool raising 不唯一) through `legacy_zhaoshang_bridge` → state MANUAL_REQUIRED after ONE probe call; `probe_calls == 1`; `restart.calls == []`; `session.update_calls == []`; durable `remote_identity` unchanged; evidence `probe_health.identity_conflict is True`, `terminal_reason == "REMOTE_IDENTITY_CONFLICT"`, `consecutive_failures == 3` (preserved, NOT 4); `restart is None`.
- `B5b` (post-restart): seed `_recovered_store`-style job (recovery_attempts=1, restart ENABLE_CONFIRMED with confirmed disable/enable evidence), probe identity conflict → MANUAL_REQUIRED while `restart.phase == "ENABLE_CONFIRMED"`, `disable_evidence.confirmed_disabled is True`, `enable_evidence.confirmed_enabled is True`, `recovery_at` intact.
- `B6`: after identity manual, `remote_identity` column unchanged (assert equality with seed).

- [ ] **Step 2: run, confirm RED** — B1/B2/B3a-c/B5/B5b fail (currently generic exception / 5-count path).
- [ ] **Step 3: adapter** — wrap the probe's lookup/identity block:

```python
try:
    task = tool.check_existing_task(session, resolved_task_name, mall_code)
except RuntimeError as exc:
    # check_existing_task's only RuntimeError escapes are the tool's own
    # exact-identity failures (auto_crawl_task.py _one_exact_task: 精确任务不唯一 /
    # 精确任务商城不匹配) — extract_records_from_query_result never raises.
    raise RemoteTaskIdentityConflictError(str(exc)) from exc
remote_calls = 1
if task is None:
    raise LegacyZhaoshangAdapterError("running task probe found no exact existing task")
try:
    live_task_id, live_task_number, mall_logo = tool.get_task_identity(task, mall_code)
except RuntimeError as exc:
    if "商城标识不一致" in str(exc):
        raise RemoteTaskIdentityConflictError(str(exc)) from exc
    raise
if str(live_task_id) != str(task_id) or str(live_task_number) != str(task_number):
    raise RemoteTaskIdentityConflictError(
        "live task identity does not match the durable remote identity"
    )
```

- [ ] **Step 4: bridge** — first except branch:

```python
except RemoteTaskIdentityConflictError:
    return {
        "probe_identity_conflict": True,
        "diagnostic": "probe raised RemoteTaskIdentityConflictError",
    }
```

- [ ] **Step 5: store** — new atomic transition (mirrors `mark_remote_zero_write_manual`, minus the unburned-attempt precondition):

```python
def mark_remote_recovery_probe_identity_manual(
    self, token: LeaseToken, job_id: int, *, remote_identity: str,
    evidence: Mapping[str, Any], error: str,
) -> dict[str, Any]:
    """Terminalize a probe-proven remote identity conflict atomically.

    One transaction: truthful probe evidence + MANUAL_REQUIRED + error_text.
    Legal from RUNNING with the exact confirmed remote identity regardless of
    recovery_attempts — a post-restart probe may also prove the conflict, and
    the caller's evidence document must preserve the confirmed restart proof
    untouched.  Zero remote writes by construction (read-only probe outcome).
    """
```

- [ ] **Step 6: recovery pass** — after `probe = probe_remote(job)`:

```python
identity_conflict = isinstance(probe, Mapping) and probe.get("probe_identity_conflict") is True
if identity_conflict:
    document = decode_recovery_evidence(job.get("recovery_evidence_json"))
    historical = None
    previous = document.get("probe_health")
    if isinstance(previous, Mapping):
        try:
            historical = int(previous.get("consecutive_failures"))
        except (TypeError, ValueError):
            historical = None
    conflict: dict[str, Any] = {
        "identity_conflict": True,
        "terminal_reason": "REMOTE_IDENTITY_CONFLICT",
        "observed_at": now.astimezone(UTC).isoformat(timespec="seconds"),
        "diagnostic": _text(probe.get("diagnostic")) or "probe identity conflict",
    }
    if historical is not None:
        conflict["consecutive_failures"] = historical
    document["probe_health"] = conflict
    store.mark_remote_recovery_probe_identity_manual(
        token, int(job["id"]),
        remote_identity=_text(job.get("remote_identity")),
        evidence=document,
        error=(
            "remote stall recovery probe found the live exact task identity "
            "conflicts with the durable remote identity; manual review required"
        ),
    )
    counters["recovery_identity_manual"] = counters.get("recovery_identity_manual", 0) + 1
    continue
```

- [ ] **Step 7: run Task 2 tests** — GREEN; whole recovery test file green.
- [ ] **Step 8: commit** — `fix(remote): fail closed immediately on probe identity conflict`

---

### Task 3 (Commit 30): MINOR C — truthful probe-cap error text

**Files:**
- Modify: `app/services/daily_audit_store.py` (`record_remote_recovery_probe_failure` cap branch)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: cap error text selected from the store's own durable row — `recovery_attempts==0` → `"...no restart was attempted; last failure: ..."`; `recovery_attempts==1 and restart.phase=="ENABLE_CONFIRMED"` → `"...the single restart was already confirmed; post-restart recovery probe failed N consecutive times; last failure: ..."`; anything else (recovery_attempts>1 or missing/phase-mismatched restart evidence) → fail closed MANUAL_REQUIRED with text naming the inconsistency (`recovery_attempts=N; restart phase=...`).

- [ ] **Step 1: RED tests C1-C3**
  - `C1` (already round6_b3; keep): recovery_attempts=0, 5 failures → error contains "no restart was attempted".
  - `C2`: seed recovered store (recovery_attempts=1, ENABLE_CONFIRMED), 5 probe failures → MANUAL_REQUIRED and error does NOT contain "no restart was attempted"; contains "already confirmed" and "post-restart".
  - `C3`: same seed → evidence still has `restart.phase == "ENABLE_CONFIRMED"`, confirmed disable/enable evidence and `recovery_at` intact (nothing deleted).
- [ ] **Step 2: run, confirm C2 RED** (today it emits the wrong headline).
- [ ] **Step 3: implement** — inside the cap branch, read `job["recovery_attempts"]` and `document["restart"]`:

```python
recovery_attempts = int(job["recovery_attempts"] or 0)
restart_phase = _text((document.get("restart") or {}).get("phase"))
if recovery_attempts == 0:
    headline = (f"remote stall recovery probe failed {consecutive} consecutive "
                f"times; no restart was attempted")
elif recovery_attempts == 1 and restart_phase == "ENABLE_CONFIRMED":
    headline = (f"remote stall recovery probe failed {consecutive} consecutive "
                f"times; the single restart was already confirmed; post-restart "
                f"recovery probes did not produce usable evidence")
else:
    headline = (f"remote stall recovery probe failed {consecutive} consecutive "
                f"times; inconsistent restart evidence (recovery_attempts="
                f"{recovery_attempts}, restart phase={restart_phase or 'missing'})")
error_text = f"{headline}; last failure: {reason}"
```

(All three branches still enter MANUAL_REQUIRED — fail closed per existing invariant.)
- [ ] **Step 4: GREEN + commit** — `fix(remote): report post-restart probe failure truthfully`

---

### Task 4: Focused regression + full suite + review + push

- [ ] Fresh baseline BEFORE Task 1 (already recorded at 77a73a4): `python -m pytest tests\test_remote_running_recovery.py tests\test_legacy_zhaoshang_adapter.py tests\test_incremental_audit_runtime.py tests\test_incremental_audit_recovery.py -q` → 0 failed.
- [ ] Focused after all tasks: the 12-file list from directive §39 (`test_auth_manager.py`, `test_incremental_price_audit.py`, `test_incremental_price_audit_finalize.py`, `test_incremental_audit_operations.py`, `test_remote_export_no_price_resolution.py`, `test_incremental_price_audit_force_finalize.py`, `test_closeout_before_1830.py`, `test_harvest_only.py`, `test_zhaoshang_resume_modes.py` included) → 0 failed.
- [ ] Symbol search over the changed symbols (RunningTaskProbe, recovery_probe, probe_failed, probe_health, remote_business_response_ok, classify_remote_execution_state, LegacyZhaoshangAdapterError, identity, mark_remote_manual) → all related tests green.
- [ ] `python -m pytest --collect-only -q` then `python -m pytest tests -q` with `PYTHONIOENCODING=utf-8`, `-p no:cacheprovider`, `--basetemp=_run\basetemp-r7` → record collected/passed/skipped/failed/warnings/duration/exit code; MUST be 0 failed.
- [ ] `superpowers:requesting-code-review` dispatch with the §42 checklist; fix Critical/Important before push.
- [ ] Scope audit: `git status --short`, `git diff --check`, `git diff 77a73a4..HEAD --stat` — only the four production files + one test file + plan doc.
- [ ] Prod SHA gate: `git fetch origin` + `git rev-parse origin/prod/closeout-20260818` == `6efdc79c5bb10605f05f2931066a833f986952e5`.
- [ ] Push `fix/step1-remote-running-stall-recovery-20260825`; verify LOCAL_HEAD == REMOTE_HEAD, PR #1 OPEN / NOT MERGED. Stop.
