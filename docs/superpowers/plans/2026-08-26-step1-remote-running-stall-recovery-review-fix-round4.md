# STEP_1 Remote Stall Recovery Review Fix Round 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four REQUEST CHANGES findings from the Round 3 independent review (BLOCKER A, BLOCKER B, BLOCKER C, IMPORTANT D) on top of `6851e6bdc6780ab00dcdd08d4db03b587f387cb1`, appending three minimal commits to `fix/step1-remote-running-stall-recovery-20260825`.

**Architecture:** All four findings are fail-closed evidence-boundary fixes inside the existing remote-stall-recovery state machine. A and D tighten the `classify_remote_execution_state` classifier (contradiction check before ACTIVE; exact terminal marker). B reorders `run_running_recovery_pass` so durable pending side-effect phases reconcile before (and independently of) fresh stall detection. C threads an optional `timeout_seconds` through `AuthManager.export_environment → refresh → _request_refresh` so the recovery probe/restart token refresh is bounded by the live callback deadline.

**Tech Stack:** Python 3.14, pytest, stdlib `urllib`. No new dependencies.

**Spec:** Round 4 directive (2026-08-26 task brief, sections 0–37). Production authority: `origin/prod/closeout-20260818` = `6efdc79c5bb10605f05f2931066a833f986952e5`.

## Global Constraints

- Append-only on `fix/step1-remote-running-stall-recovery-20260825` from `6851e6bd…`: no rebase, squash, amend, reset, cherry-pick to prod, new branch, merge, deploy.
- Production branch must still be `6efdc79c5bb10605f05f2931066a833f986952e5` at push time; otherwise STOP.
- RED-first for every fix; any new test that initially passes must trigger re-investigation, not code churn.
- Only these production files may change: `app/services/remote_running_recovery.py`, `app/services/legacy_zhaoshang_adapter.py`, `scripts/auth_manager.py`, plus `tests/test_remote_running_recovery.py` and `tests/test_auth_manager.py`.
- Do not regress the 11 already-reviewed invariants listed in directive §3 (externally-disabled zero-write, fresh-write revalidation, progress-derived liveness, probe attempt cap, bounded read-only session, post-probe budget gate, RESTARTED confirmation gates, RECOVERY_EXHAUSTED proof, count=0 no NO_RESULT, SUBMIT_UNKNOWN reconcile-only).
- Windows: commands run through cmd.exe; pytest with `PYTHONIOENCODING=utf-8 -p no:cacheprovider --basetemp=_run\basetemp…`.
- Commit order: Commit 16 = BLOCKER A + IMPORTANT D (+ tests), Commit 17 = BLOCKER B (+ tests), Commit 18 = BLOCKER C (+ tests).

## Verified Root Causes (Phase 1 evidence)

1. **A** — `app/services/remote_running_recovery.py:76-79`: `pending > 0 or running > 0 or waiting_db > 0 → ACTIVE` runs before `finished + failed > total → UNKNOWN`. With `total=10, pending=1, running=1, waiting_db=0, finished=10, failed=1` the contradictory row (11 > 10) is classified ACTIVE, becomes stationary-tail evidence and can authorize a remote restart. The external tool's `is_progress_finished` (auto_crawl_task.py:680-703) checks liveness before the sum, but it is only a "finished?" predicate — the fail-closed classifier must reject contradictions first. No other deterministic contradiction has production evidence, so only `finished + failed > total` is added (do NOT invent sum-of-states constraints).
2. **D** — `remote_running_recovery.py:68`: `any(marker in description …)` is substring matching. `"查询本次任务执行完毕状态失败"` contains `"本次任务执行完毕"` and would be classified TERMINAL. Evidence only supports the exact status value (auto_crawl_task.py:1048). Fix: `description in TERMINAL_STATUS_MARKERS` on the `_text()`-trimmed value (tuple membership = exact match).
3. **B** — `remote_running_recovery.py:702`: `_apply_restart` is reachable only under `decision["stalled"]`. Once a disable was sent, the remote task is disabled, progress stops classifying ACTIVE, `_observation_is_no_price` returns False, `stalled` collapses to False, and the durable phase wedges forever in DISABLE_SENT/ENABLE_SENT — the 900 s fail-closed window lives inside `_apply_restart` (DISABLE/ENABLE_UNCONFIRMED branch) and is equally unreachable. Additionally `_apply_restart`'s resume set (`remote_running_recovery.py:443-447`) omits `DISABLE_CONFIRMED`, which the adapter legitimately accepts (`legacy_zhaoshang_adapter.py:1797,1923`).
4. **C** — `scripts/auth_manager.py:322`: `self.opener.open(request, timeout=30)` is hard-coded. `export_environment("zhaoshang", refresh=True)` → `refresh()` → `_request_refresh()` performs exactly ONE network request on this chain (no retry loop; `probe_previous_refresh_token`/`request_json` are not in it), invoked in `_production_probe_running_task` (`legacy_zhaoshang_adapter.py:1618-1620`) and `_production_restart_running_task` (`:1803-1805`) BEFORE the `_BoundedReadOnlySession` exists, so a 30 s token refresh can blow an 8 s remaining deadline.

---

### Task 1 (Commit 16): Contradiction-before-ACTIVE + exact terminal marker

**Files:**
- Modify: `app/services/remote_running_recovery.py:57-82` (`classify_remote_execution_state`)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: unchanged public signature `classify_remote_execution_state(progress, task_status_rsp_desc="") -> RemoteExecutionState`.

- [x] **Step 1: RED tests** — append to `tests/test_remote_running_recovery.py` (round-4 section):

```python
# Round 4, BLOCKER A: structural/mathematical consistency must be proven
# BEFORE the ACTIVE branch can accept the row.  A contradictory progress
# payload is UNKNOWN regardless of pending/running/waiting_db.

_ROUND4_CONTRADICTION = {
    "total": 10, "pending": 1, "running": 1, "failed": 1,
    "waiting_db": 0, "finished": 10,
}

def test_round4_a1_contradictory_active_looking_progress_is_unknown() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state
    assert classify_remote_execution_state(dict(_ROUND4_CONTRADICTION), "") is RemoteExecutionState.UNKNOWN

def test_round4_a2_contradictory_progress_never_proves_stall(tmp_path) -> None:
    evidence = _observations(4, progress=dict(_ROUND4_CONTRADICTION))
    job = _job(evidence=evidence)
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False

def test_round4_a3_contradictory_progress_reaches_no_remote_write(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path, observations=3)
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    _run_pass(store, _probe_payload(progress=dict(_ROUND4_CONTRADICTION)), restart)
    assert restart.calls == []
    assert int(store.get_remote_job(job_id)["recovery_attempts"] or 0) == 0

def test_round4_a4_legal_active_progress_is_still_active() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state
    assert classify_remote_execution_state(
        {"total": 10, "pending": 1, "running": 2, "failed": 0, "waiting_db": 0, "finished": 7}, ""
    ) is RemoteExecutionState.ACTIVE

def test_round4_a5_legal_terminal_progress_is_still_terminal() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state
    assert classify_remote_execution_state(
        {"total": 10, "pending": 0, "running": 0, "failed": 1, "waiting_db": 0, "finished": 9}, ""
    ) is RemoteExecutionState.TERMINAL
```

(A3 works because the pass appends the contradictory observation, `_observation_is_no_price` rejects it, the stationary tail breaks, `stalled` stays False, and the fresh write gate declines; the assertion is the observable contract: zero restart dispatch, zero attempt burn.)

- [x] **Step 2: RED run** — `pytest tests\test_remote_running_recovery.py -q -k round4_a` → A1 FAILS (`is REMOTE_ACTIVE`), A2/A3 verify the downstream wedge, A4/A5 PASS (guards against over-tightening).

- [x] **Step 3: IMPORTANT D RED tests** (same commit — same classifier):

```python
# Round 4, IMPORTANT D: the terminal marker is one exact status value
# (auto_crawl_task.py:1048); substring containment widened the evidence.

def test_round4_d1_exact_marker_with_active_progress_is_terminal() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state
    assert classify_remote_execution_state(
        _live_progress(running=3), "本次任务执行完毕"
    ) is RemoteExecutionState.TERMINAL

def test_round4_d2_whitespace_padded_marker_is_terminal() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state
    assert classify_remote_execution_state(
        _live_progress(running=3), "  本次任务执行完毕  "
    ) is RemoteExecutionState.TERMINAL

def test_round4_d3_failure_phrase_containing_marker_is_not_terminal() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state
    assert classify_remote_execution_state(
        _live_progress(running=3), "查询本次任务执行完毕状态失败"
    ) is RemoteExecutionState.ACTIVE

def test_round4_d4_marker_suffixed_failure_phrase_with_unknown_progress_is_unknown() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state
    assert classify_remote_execution_state(
        {}, "本次任务执行完毕失败"
    ) is RemoteExecutionState.UNKNOWN
```

- [x] **Step 4: RED run** — `-k round4_d` → D3 FAILS (currently TERMINAL), D1/D2/D4 PASS already (D2 passes because `_text()` trims before the substring check; it guards the exact-match change).

- [x] **Step 5: minimal implementation** — reorder + exact match in `classify_remote_execution_state`:

```python
    description = _text(task_status_rsp_desc)
    if description in TERMINAL_STATUS_MARKERS:
        return RemoteExecutionState.TERMINAL
    signature = canonical_progress_signature(progress if isinstance(progress, Mapping) else {})
    if signature is None:
        return RemoteExecutionState.UNKNOWN
    total, pending, running, failed, waiting_db, finished = signature
    if any(value < 0 for value in signature):
        return RemoteExecutionState.UNKNOWN
    if finished + failed > total:
        return RemoteExecutionState.UNKNOWN
    if pending > 0 or running > 0 or waiting_db > 0:
        return RemoteExecutionState.ACTIVE
    if finished + failed >= total:
        return RemoteExecutionState.TERMINAL
    return RemoteExecutionState.UNKNOWN
```

(Update the module docstring comment block near `TERMINAL_STATUS_MARKERS` to record exact-match + contradiction-first.)

- [x] **Step 6: GREEN + full file** — `pytest tests\test_remote_running_recovery.py -q` all green.

- [x] **Step 7: Commit 16** — `fix(remote): reject contradictory progress before activity`

---

### Task 2 (Commit 17): reconcile pending restart phases before the stalled gate

**Files:**
- Modify: `app/services/remote_running_recovery.py` — `run_running_recovery_pass` loop head, `_apply_restart` resume set
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: `PENDING_RESTART_PHASES = ("DISABLE_SENT", "DISABLE_CONFIRMED", "ENABLE_SENT")` module constant; `run_running_recovery_pass` control order becomes RECOVERY_EXHAUSTED sweep → pending-phase reconciliation → fresh probe/stall workflow.

- [x] **Step 1: RED tests**:

```python
# Round 4, BLOCKER B: a durable phase that already dispatched a remote
# write is an EXISTING recovery protocol; the next tick reconciles it and
# never re-demands a fresh stalled proof (post-disable progress can never
# be ACTIVE evidence again).

def _pending_phase_store(tmp_path, phase: str, *, progress: dict | None = None, phase_age_minutes: int = 0):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_id = _seed_running_job(store, attempts=15, age_minutes=180)
    token = store.acquire_lease(BUSINESS_DATE, f"seed-{phase}", ttl_seconds=300)
    evidence = _observations(4, progress=progress) if progress is not None else _observations(4)
    evidence["restart"] = {
        "phase": phase,
        "phase_at": (NOW - timedelta(minutes=phase_age_minutes)).isoformat(timespec="seconds"),
        "recovery_reason": "REMOTE_RUNNING_STALLED",
    }
    store.update_remote_recovery_evidence(token, job_id, evidence)
    store.release_lease(token)
    return store, job_id

def _unknown_progress_payload(**overrides) -> dict:
    payload = _probe_payload(progress={"total": 0}, task_status_rsp_desc="")
    payload.update(overrides)
    return payload
```

- B1: seed DISABLE_SENT, observations progress `{}`-shaped UNKNOWN (use `_unknown_progress_payload` as the probe return and seed evidence whose tail rows classify UNKNOWN): assert `restart.calls == [(job_id, "DISABLE_SENT")]` — currently the callback is never invoked → RED.
- B2 (adapter level, already green — keep): `test_adapter_restart_resume_disable_sent_never_disables_again`.
- B3: seed ENABLE_SENT + UNKNOWN probe: assert one call `resume_phase="ENABLE_SENT"` and `session`/adapter never re-enables → run-pass asserts calls and that outcome is reconciled (use `_CONFIRMED_RESTART_STEP` script; assert `recovery_attempts == 1` exactly once).
- B4: seed DISABLE_CONFIRMED + non-ACTIVE progress: assert `restart.calls == [(job_id, "DISABLE_CONFIRMED")]`.
- B5: seed DISABLE_SENT with `phase_age_minutes=20` (>900 s) and script `[{"status": "DISABLE_UNCONFIRMED", "restarted": False, "observed_enabled": True}]`: assert `state == "MANUAL_REQUIRED"`.
- B6: same with ENABLE_SENT + `ENABLE_UNCONFIRMED` → MANUAL_REQUIRED.
- B7 (guard): no pending phase + not stalled → `restart.calls == []` (reuse `test_recovered_job_is_never_restarted_twice` semantics with a fresh non-stalled job).
- B8: seed DISABLE_SENT, script confirmed RESTARTED twice across two passes: assert `recovery_attempts == 1` after both, second pass dispatches nothing (phase becomes ENABLE_CONFIRMED).

- [x] **Step 2: RED run** — B1/B3/B4/B5/B6 FAIL (no callback, wedge persists).

- [x] **Step 3: minimal implementation** — in `run_running_recovery_pass`, directly after the RECOVERY_EXHAUSTED sweep block and before `evaluate_running_stall`:

```python
        document = decode_recovery_evidence(job.get("recovery_evidence_json"))
        pending_restart = document.get("restart")
        if (
            restart_remote is not None
            and isinstance(pending_restart, Mapping)
            and _text(pending_restart.get("phase")) in PENDING_RESTART_PHASES
        ):
            # An already-dispatched remote write is an existing recovery
            # protocol: reconcile the durable phase against the live remote
            # state without re-proving the original stall (post-disable
            # progress can never be ACTIVE evidence again).  The adapter
            # re-verifies exact task/mall identity on every lookup and never
            # repeats the already-sent write direction.
            if not can_start():
                # Reconciliation is one bounded callback; without the minimal
                # budget for it the next tick retries.  Fresh restarts below
                # stay deferrable exactly as before.
                continue
            outcome = _apply_restart(store, token, job, document, restart_remote, now=now)
            counters["recovery_restarts"] += int(bool(outcome.get("restarted")))
            counters["recovery_reconciled"] += int(bool(outcome.get("reconciled")))
            continue
```

plus `PENDING_RESTART_PHASES: tuple[str, ...] = ("DISABLE_SENT", "DISABLE_CONFIRMED", "ENABLE_SENT")` near `RECOVERY_WRITE_CONFIRM_WINDOW_SECONDS`, and in `_apply_restart` extend the resume set:

```python
    if isinstance(restart, Mapping) and _text(restart.get("phase")) in PENDING_RESTART_PHASES:
        resume_phase = _text(restart.get("phase"))
```

The existing in-loop `is_already_sent` budget-bypass for the fresh path is subsumed: pending phases now `continue` before the probe, so the old `{"DISABLE_SENT","ENABLE_SENT"}` check inside the stalled branch can be simplified to `not can_start()` (fresh restarts only). Keep behavior identical for fresh jobs.

- [x] **Step 4: GREEN + file** — all `tests\test_remote_running_recovery.py` green (d3/d4 keep passing: the probe is skipped for pending phases so the callback still receives the resume phase).

- [x] **Step 5: Commit 17** — `fix(remote): reconcile pending restart phases before stall gating`

---

### Task 3 (Commit 18): bound the recovery token refresh by the callback deadline

**Files:**
- Modify: `scripts/auth_manager.py:316-417` (`_request_refresh`, `refresh`, `export_environment`)
- Modify: `app/services/legacy_zhaoshang_adapter.py` — `_production_probe_running_task`, `_production_restart_running_task`
- Test: `tests/test_auth_manager.py`, `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces (all backward-compatible, keyword-only, default `None` = today's 30 s):
  - `_request_refresh(self, system, refresh_token, *, timeout_seconds: float | None = None)`
  - `refresh(self, system, *, stale_access_token=None, timeout_seconds: float | None = None)`
  - `export_environment(self, system="zhaoshang", *, refresh=True, timeout_seconds: float | None = None)`
  - semantics: `None → 30.0`; else `actual = min(30.0, float(timeout_seconds))` and require `timeout_seconds > 0` (ValueError otherwise).

- [x] **Step 1: RED tests in `tests/test_auth_manager.py`** — real `AuthManager` with a recording fake opener + tmp state file (NO `_FakeAuthManager`):

```python
class _RecordingOpener:  # counts calls, records timeout, returns rotated tokens
    ...
class _FakeRefreshResponse:  # context manager: read() -> JSON bytes
    ...
```

- C4 (compat): `export_environment(refresh=True)` → opener saw exactly `timeout == 30`.
- C1: `export_environment(refresh=True, timeout_seconds=8)` → opener timeout `<= 8` (currently RED: 30).
- C2: `timeout_seconds=3.5` → `<= 3.5`.
- C3: `timeout_seconds<=0` (or exhausted remaining) → `ValueError`/no opener call (opener.calls == 0).
- Clamp-up guard: `timeout_seconds=60` → opener sees 30 (min(30, 60)).

- [x] **Step 2: RED run** — `pytest tests\test_auth_manager.py -q -k round4` → C1/C2 FAIL with observed 30.

- [x] **Step 3: AuthManager minimal implementation** (thread the parameter; do not touch rotation/lock/cookie semantics).

- [x] **Step 4: adapter RED tests in `tests/test_remote_running_recovery.py`** — extend `_prepared_job` with an optional real-AuthManager seam (fake opener + state file; `_load_module` still monkeypatched):

- C5 probe: `_SteppingClock(step=…)` so refresh consumes budget; assert opener timeout `<= remaining-at-refresh` AND every later session post/get timeout `<= remaining-after-refresh` (never a fresh full budget).
- C6 restart: same sharing across lookup/status/progress/disable/enable (reuse `_RestartSession` recorded timeouts + opener record).
- C7: opener raises TimeoutError (wrapped into `AuthError` by `_request_refresh`) → `pytest.raises` on `restart_running_task`; `session.update_calls == []` (zero disable/enable).

- [x] **Step 5: adapter minimal implementation** — in both recovery production paths, compute `remaining = deadline - self._monotonic()` BEFORE the refresh, `raise TimeoutError` when `<= 0`, and call `export_environment("zhaoshang", refresh=True, timeout_seconds=remaining)`; keep the existing post-refresh remaining check.

- [x] **Step 6: GREEN + both files** — `tests\test_auth_manager.py` and `tests\test_remote_running_recovery.py` fully green.

- [x] **Step 7: Commit 18** — `fix(auth): bound recovery token refresh by callback deadline`

---

### Task 4: verification, review, push

- [ ] Focused regression (13 files: the directive's 11 + `test_auth_manager.py`; directive list verified to exist).
- [ ] Full suite: `pytest --collect-only -q` then `pytest tests -q` with `PYTHONIOENCODING=utf-8 -p no:cacheprovider --basetemp=_run\basetemp-r4-full`; record collected/passed/skipped/failed/warnings/duration/exit code; require 0 failed.
- [ ] Independent code review via superpowers:requesting-code-review against the 15 review points (directive §30); fix Critical (must) and Important (before push).
- [ ] verification-before-completion; `git status --short`, `git diff --check`, range diff/stat audit (no scheduler/科力普/STEP_2/3/formatting noise; pyc noise must NOT be committed — add only the five files).
- [ ] Re-verify `git rev-parse origin/prod/closeout-20260818` == `6efdc79…`; then push `fix/step1-remote-running-stall-recovery-20260825`; verify LOCAL_HEAD == REMOTE_HEAD, PR #1 OPEN/NOT MERGED; final report.
