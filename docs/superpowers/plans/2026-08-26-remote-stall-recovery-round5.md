# Remote Stall Recovery Round 5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three Round-5 review findings (BLOCKER A pre-write side-effect boundary, IMPORTANT B absolute auth deadline, IMPORTANT C business-response gate) as three minimal commits on `fix/step1-remote-running-stall-recovery-20260825` starting from `d97bf906`.

**Architecture:** The restart adapter owns the side-effect boundary via two write-started flags and converts every escape into an explicit outcome (new `PRE_WRITE_FAILED` status); the recovery pass withdraws the write-ahead `DISABLE_SENT` for zero-write proofs so the next tick retries fresh. AuthManager threads an absolute monotonic deadline through `export_environment → refresh → _request_refresh`, recomputing the live remainder immediately before the refresh HTTP. A business-response gate mirroring the external tool's own rule strips failed status/progress responses to unprovable before any parsing.

**Tech Stack:** Python 3.14, pytest, SQLite durable evidence, monkeypatched fake legacy tool modules.

**Spec:** `docs/superpowers/specs/2026-08-26-remote-stall-recovery-round5.md`

## Global Constraints

- Branch: `fix/step1-remote-running-stall-recovery-20260825`; base `d97bf9066047f3fd6330b62aba6e6131eeebab0c`; prod stays `6efdc79c5bb10605f05f2931066a833f986952e5`. No rebase/squash/amend/reset/cherry-pick/merge/deploy.
- RED first: every behavior change is preceded by a failing test observed on the pre-fix code.
- Locked designs must not regress (review §3 list: exact terminal marker, ACTIVE/TERMINAL/UNKNOWN semantics, pending-phase reconcile-first, probe cap 4, externally-disabled zero-write, strict NO_RESULT, SUBMIT_UNKNOWN).
- Production files allowed: `app/services/remote_running_recovery.py`, `app/services/legacy_zhaoshang_adapter.py`, `app/services/incremental_audit_runtime.py`, `scripts/auth_manager.py`, plus tests/docs.
- Windows cmd.exe shell; pytest with `PYTHONIOENCODING=utf-8 -p no:cacheprovider --basetemp=_run\basetemp-r5*`.
- External tool (`E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py`) is read-only reference.

---

### Task 1 (Commit 21): BLOCKER A — pre-write boundary

**Files:**
- Modify: `app/services/legacy_zhaoshang_adapter.py` (RestartStatus enum ~L222, `_production_restart_running_task` L1794-2026)
- Modify: `app/services/remote_running_recovery.py` (`_apply_restart` ~L507; `classify_restart_exception` docstring ~L126)
- Modify: `app/services/incremental_audit_runtime.py` (`recovery_restart` except-comment L1490-1500)
- Test: `tests/test_remote_running_recovery.py` (new round5 section; update `test_round4_c7`)

**Interfaces:**
- Produces: `RestartStatus.PRE_WRITE_FAILED`; adapter restart no longer raises on pre-write failures; `_apply_restart` maps outcome status `PRE_WRITE_FAILED` → durable phase `PRE_WRITE_FAILED` (zero-write, retryable, not in `PENDING_RESTART_PHASES`).

- [ ] **Step 1.1: RED — protocol two-tick (the review's headline scenario).** In `tests/test_remote_running_recovery.py`, after the round4 section, add:

```python
# ---------------------------------------------------------------------------
# Round 5, BLOCKER A: pre-write failures are zero-write and retryable.
# ---------------------------------------------------------------------------


def test_round5_a1_pre_write_failure_keeps_fresh_retry_across_two_ticks(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            # Tick 1: the bridge translated a pre-write auth failure into an
            # explicit PRE_WRITE_FAILED outcome (no disable was attempted).
            {
                "status": "PRE_WRITE_FAILED",
                "restarted": False,
                "diagnostic": "restart dispatch raised AuthError before any write; nothing sent",
            },
            # Tick 2: the fault is gone; the same job must still be allowed
            # its single fresh disable->enable with resume_phase=None.
            {
                "status": "RESTARTED",
                "restarted": True,
                "disable_evidence": {"confirmed_disabled": True},
                "enable_evidence": {"confirmed_enabled": True},
            },
        ]
    )
    counters = _run_pass(store, _probe_payload(), restart, now=NOW)
    # Tick 1 durable state: no write left, no attempt burned, no pending
    # write-ahead phase that would force reconcile-only next tick.
    assert restart.calls == [(job_id, None)]
    assert counters["recovery_restarts"] == 0
    job = store.get_remote_job(job_id)
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert job["state"] == "RUNNING"
    assert int(job["recovery_attempts"]) == 0
    assert evidence["restart"]["phase"] == "PRE_WRITE_FAILED"
    assert evidence["restart"]["phase"] not in ("DISABLE_SENT", "ENABLE_SENT")
    assert evidence["restart"]["diagnostic"]

    counters = _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=2))
    assert restart.calls == [(job_id, None), (job_id, None)]
    assert counters["recovery_restarts"] == 1
    job = store.get_remote_job(job_id)
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert int(job["recovery_attempts"]) == 1
    assert evidence["restart"]["phase"] == "ENABLE_CONFIRMED"
```

Run: `python -m pytest tests\test_remote_running_recovery.py::test_round5_a1_pre_write_failure_keeps_fresh_retry_across_two_ticks -q`
Expected RED: `evidence["restart"]["phase"]` is `"DISABLE_SENT"` (the trailing fall-through keeps the write-ahead phase).

- [ ] **Step 1.2: RED — adapter contract.** Add adapter-level tests using the existing `_prepared_job` harness (fake tool + `_RestartSession`):

```python
def test_round5_a1_auth_refresh_failure_returns_pre_write_failed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(
        tmp_path, monkeypatch, task, session, auth=_real_auth_manager(tmp_path, _TimingOutAuthOpener())
    )

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=10.0)

    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []
    assert session.post_calls == 0
    assert session.get_calls == 0
```

Update `test_round4_c7_auth_refresh_timeout_dispatches_no_remote_write` to the new contract (replace `with pytest.raises(AuthError):` + call with a direct call asserting `outcome.status.value == "PRE_WRITE_FAILED"`; drop the now-unused AuthError import if nothing else uses it).

Extend `_RestartSession` with optional failure switches used by the remaining RED tests (queryList timeout → lookup failure; queryTaskStatus timeout; queryTaskProgress timeout):

```python
class _RestartSession:
    def __init__(self, task, *, fail_disable=False, fail_enable=False,
                 fail_lookup=False, fail_status=False, fail_progress=False,
                 status_rsp_desc="执行中", progress_rsp_desc=_ACTIVE_PROGRESS_RSP):
        ...
        self.fail_lookup = fail_lookup
        self.fail_status = fail_status
        self.fail_progress = fail_progress
```
with `post()` raising `TimeoutError("lookup response lost")` for `queryList` when `fail_lookup`, for `queryTaskStatus` when `fail_status`; `get()` raising `TimeoutError("progress response lost")` when `fail_progress`.

```python
def test_round5_a2_lookup_timeout_is_pre_write_failed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_lookup=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)
    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)
    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []


def test_round5_a3_status_timeout_is_pre_write_failed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_status=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)
    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)
    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []


def test_round5_a4_progress_timeout_is_pre_write_failed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_progress=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)
    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)
    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []


def test_round5_a5_deadline_exhausted_before_disable_is_pre_write_failed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    calls = {"count": 0}

    def monotonic() -> float:
        calls["count"] += 1
        return 1000.0 if calls["count"] <= 1 else 2000.0  # deadline 1008 exhausted everywhere after setup

    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session, monotonic=monotonic)
    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=8.0)
    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []


def test_round5_post_disable_reconcile_timeout_stays_disable_unknown(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_disable=True, fail_lookup=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)
    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)
    assert outcome.status.value == "DISABLE_UNKNOWN"
    assert len([c for c in session.update_calls if c.get("state") == "0"]) == 1


def test_round5_post_enable_confirm_timeout_is_enable_unknown(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_lookup_after_enable=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)
    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)
    assert outcome.status.value == "ENABLE_UNKNOWN"
    assert len([c for c in session.update_calls if c.get("state") == "0"]) == 1
    assert len([c for c in session.update_calls if c.get("state") == "1"]) == 1
```

(`fail_lookup_after_enable` is a third switch: queryList succeeds until the first enable POST, then raises TimeoutError.)

- [ ] **Step 1.3: Run all new tests — verify RED** (protocol test fails with `DISABLE_SENT`; adapter tests fail with raised exceptions instead of outcomes).

- [ ] **Step 1.4: Implement.**

`app/services/legacy_zhaoshang_adapter.py`:
- Enum: add `PRE_WRITE_FAILED = "PRE_WRITE_FAILED"` to `RestartStatus`.
- `_production_restart_running_task`: wrap the whole body after the `resume_phase` validation in one `try:` with write-boundary flags, converting escapes:

```python
        disable_write_started = False
        enable_write_started = False
        try:
            ... existing body verbatim, with exactly two insertions:
            # immediately before `disable_response = tool.disable_task(...)`:
            disable_write_started = True
            # immediately before `enable_response = tool.enable_task(...)`:
            enable_write_started = True
        except Exception as exc:  # noqa: BLE001 - converted into an outcome
            return self._restart_escape_outcome(
                exc,
                resume_phase=resume_phase,
                disable_write_started=disable_write_started,
                enable_write_started=enable_write_started,
            )
```

- New module-level helper:

```python
def _pre_write_restart_failure_status(exc: BaseException) -> str:
    """Classify an exception raised before any remote write was attempted.

    Identity-shaped failures (the exact local↔remote identity cannot be
    reconstructed) stay the permanent IDENTITY_CONFLICT; every other
    pre-write failure — auth refresh, transport, deadline exhaustion,
    tooling — proves no remote write left this protocol.
    """
    from app.services.incremental_audit_runtime import RuntimeConfigurationError

    if isinstance(exc, (RuntimeConfigurationError, ValueError, KeyError, TypeError)):
        return "IDENTITY_CONFLICT"
    if isinstance(exc, RuntimeError):
        message = str(exc)
        if any(marker in message for marker in ("不唯一", "不一致", "没有拿到", "identity")):
            return "IDENTITY_CONFLICT"
    return "PRE_WRITE_FAILED"
```

- Adapter method:

```python
    def _restart_escape_outcome(
        self,
        exc: BaseException,
        *,
        resume_phase: str | None,
        disable_write_started: bool,
        enable_write_started: bool,
    ) -> LegacyRestartOutcome:
        """An exception may only escape as an outcome that names the boundary.

        The write-boundary flags are set immediately before the disable/enable
        calls, so this method always knows whether a side effect may have
        left: after the enable call the outcome is ENABLE_UNKNOWN, after the
        disable call DISABLE_UNKNOWN, and before both it is a provable
        zero-write failure (pre-write transient, or permanent identity
        conflict).  Resumed phases reconcile the already-durable direction.
        """
        diagnostic = f"restart raised {type(exc).__name__}: {exc}"
        if enable_write_started or resume_phase == "ENABLE_SENT":
            return LegacyRestartOutcome(
                RestartStatus.ENABLE_UNKNOWN,
                disable_confirmed=True,
                observed_enabled=None,
                diagnostic=diagnostic,
            )
        if disable_write_started or resume_phase in {"DISABLE_SENT", "DISABLE_CONFIRMED"}:
            return LegacyRestartOutcome(
                RestartStatus.DISABLE_UNKNOWN,
                observed_enabled=None,
                diagnostic=diagnostic,
            )
        from app.services.remote_running_recovery import _pre_write_restart_failure_status

        status = _pre_write_restart_failure_status(exc)
        return LegacyRestartOutcome(
            RestartStatus(status),
            observed_enabled=None,
            diagnostic=diagnostic,
        )
```

`app/services/remote_running_recovery.py`:
- Move/duplicate the identity classification into `_pre_write_restart_failure_status` as sketched (module-level, importing RuntimeConfigurationError lazily).
- `_apply_restart`, insert before the `DISABLE_UNKNOWN`/`ENABLE_UNKNOWN` branch:

```python
    if status == "PRE_WRITE_FAILED":
        # Zero-write proof: the adapter established that no remote write was
        # attempted (auth/lookup/status/progress/deadline all precede the
        # first write).  The write-ahead DISABLE_SENT must be withdrawn —
        # leaving it would force reconcile-only and waste the single
        # automatic recovery attempt on a transient fault.  The diagnostic
        # is kept durably; the attempt slot is NOT consumed; the job stays
        # RUNNING so the next pass may re-enter fresh recovery.
        evidence["restart"] = {
            "phase": "PRE_WRITE_FAILED",
            "phase_at": now.astimezone(UTC).isoformat(timespec="seconds"),
            "failed_status": status,
            "diagnostic": _text(outcome.get("diagnostic")) or status,
        }
        store.update_remote_recovery_evidence(token, job_id, evidence)
        return outcome
```

- Update `classify_restart_exception` docstring: production adapter escapes are converted at the source; this classifier is the last-resort for injected runners and unforeseen escapes, and unprovable fresh escapes stay reconcile-only (fail toward manual, never a second write).
- Update the `recovery_restart` bridge comment likewise.

- [ ] **Step 1.5: GREEN + focused regression** — `python -m pytest tests\test_remote_running_recovery.py tests\test_legacy_zhaoshang_adapter.py tests\test_incremental_audit_runtime.py tests\test_incremental_audit_recovery.py -q` → 0 failed. Also confirm A6/A7 reverse tests still green (`test_disable_unknown_persists_sent_phase_and_resumes_without_resend`, `test_enable_unknown_resumes_by_reconciliation_only`).

- [ ] **Step 1.6: Commit** `fix(remote): distinguish pre-write recovery failure from unknown disable`.

---

### Task 2 (Commit 22): IMPORTANT B — absolute auth deadline

**Files:**
- Modify: `scripts/auth_manager.py` (`_request_refresh` L316, `refresh` L373, `export_environment` L419; add `import time`)
- Modify: `app/services/legacy_zhaoshang_adapter.py` (both `export_environment(refresh=True, ...)` call sites pass `deadline=deadline, monotonic=self._monotonic`)
- Test: `tests/test_auth_manager.py` (round5 section), `tests/test_remote_running_recovery.py` (fake auth signature updates)

**Interfaces:**
- Produces: `AuthManager.export_environment(system, *, refresh, timeout_seconds=None, deadline=None, monotonic=None)`; `AuthManager.refresh(system, *, stale_access_token=None, timeout_seconds=None, deadline=None, monotonic=None)`; `_request_refresh(..., timeout_seconds=None, deadline=None, monotonic=None)`. `deadline` (absolute monotonic seconds) takes precedence over `timeout_seconds`; absent → unchanged behavior.

- [ ] **Step 2.1: RED — auth-level tests** in `tests/test_auth_manager.py` (reuse `_round4_manager` / `TimeoutRecordingOpener`):

```python
def test_round5_b1_deadline_recomputed_after_elapsed_pre_work(tmp_path):
    opener = TimeoutRecordingOpener(_rotated_response())
    manager = _round4_manager(tmp_path, opener)
    clock = _SteppedClock(start=1000.0, step=6.0)  # 6 s consumed before the HTTP
    environment = manager.export_environment(
        "zhaoshang", refresh=True, deadline=1008.0, monotonic=clock
    )
    assert opener.timeouts and max(opener.timeouts) <= 2.0
    assert environment["BLADE_AUTH"] == "bearer zs-b"


def test_round5_b2_exhausted_deadline_sends_zero_auth_http(tmp_path):
    opener = TimeoutRecordingOpener(_rotated_response())
    manager = _round4_manager(tmp_path, opener)
    with pytest.raises(TimeoutError):
        manager.export_environment("zhaoshang", refresh=True, deadline=1000.0, monotonic=lambda: 1005.0)
    assert opener.timeouts == []


def test_round5_b4_deadline_above_default_clamps_to_30(tmp_path):
    opener = TimeoutRecordingOpener(_rotated_response())
    manager = _round4_manager(tmp_path, opener)
    manager.export_environment("zhaoshang", refresh=True, deadline=1000.0, monotonic=lambda: 1000.0)
    assert opener.timeouts == [30.0]
```

(`_SteppedClock`: tiny local fake advancing 6.0 per call; B3 default-30 is already covered by `test_round4_c4` and re-asserted after the change.)

Run: expected RED — `TypeError: export_environment() got an unexpected keyword argument 'deadline'`.

- [ ] **Step 2.2: Implement** `scripts/auth_manager.py`:

```python
import time
```
`_request_refresh` signature adds `deadline: float | None = None, monotonic: Callable[[], float] | None = None`; timeout selection becomes:

```python
        clock = monotonic or time.monotonic
        if deadline is not None:
            # Absolute deadline: lock waits and pre-HTTP work consume the
            # same budget, so the HTTP only ever gets the live remainder.
            remaining = float(deadline) - clock()
            if not remaining > 0:
                raise TimeoutError("auth refresh deadline exhausted before HTTP")
            actual_timeout = min(30.0, remaining)
        elif timeout_seconds is None:
            actual_timeout = 30.0
        else:
            if not timeout_seconds > 0:
                raise ValueError("auth refresh timeout_seconds must be positive")
            actual_timeout = min(30.0, float(timeout_seconds))
```

`refresh` and `export_environment` gain the same two keyword params and pass them through. `Callable` is already imported from typing.

- [ ] **Step 2.3: GREEN (auth level) + adapter deadline threading.** Update the two adapter call sites to `auth_manager.export_environment("zhaoshang", refresh=True, timeout_seconds=remaining, deadline=deadline, monotonic=self._monotonic)`. Update `_FakeAuthManager` in `tests/test_remote_running_recovery.py` (and any other fake `export_environment` in tests, found via `findstr /s /n "def export_environment" tests`) to accept `deadline=None, monotonic=None`. Add adapter-level B5/B6:

```python
def test_round5_b5_auth_consumption_shrinks_session_deadline(tmp_path, monkeypatch) -> None:
    # A clock that burns 10 s inside the auth refresh: every later session
    # request must clamp to the remainder of the SAME deadline.
    ...build with _SteppingClock(step=?) covering probe path; assert max(session timeouts) <= budget - consumed
def test_round5_b6_lock_overrun_past_deadline_dispatches_no_write(tmp_path, monkeypatch) -> None:
    # Fake auth manager whose export_environment advances the adapter clock
    # beyond the deadline (simulated unbounded lock wait): restart returns
    # PRE_WRITE_FAILED with zero disable/enable.
```

- [ ] **Step 2.4: Focused regression** — auth + recovery + adapter files, 0 failed. Commit `fix(auth): carry absolute recovery deadline through refresh`.

---

### Task 3 (Commit 23): IMPORTANT C — business-response gate

**Files:**
- Modify: `app/services/remote_running_recovery.py` (new `remote_business_response_ok`)
- Modify: `app/services/legacy_zhaoshang_adapter.py` (probe: strip business-failed status/progress; write-time revalidation: same)
- Test: `tests/test_remote_running_recovery.py` (round5 C section)

**Interfaces:**
- Produces: `remote_business_response_ok(payload: object) -> bool` (Mapping; `success` present → must be `True`; `rspCode` present & non-empty → must be `"0000"`).

- [ ] **Step 3.1: RED** — probe-level and write-time tests (extend `_RestartSession` with `progress_payload`/`status_payload` overrides so responses can be business-failed):

```python
def test_round5_c1_failed_progress_counters_are_not_stall_evidence(...)  # probe: progress success=false ACTIVE-looking → signature None → stalled False → restart.calls == []
def test_round5_c2_healthy_active_progress_still_active(...)             # success=true rspCode=0000 → ACTIVE → restart dispatched
def test_round5_c3_failed_status_terminal_text_not_trusted(...)         # status success=false + marker + healthy ACTIVE progress → ACTIVE (restart proceeds)
def test_round5_c4_write_time_failed_progress_dispatches_no_write(...)  # stalled history + fresh progress success=false → zero update_calls
def test_round5_c5_non_mapping_payload_is_unknown(...)                  # progress GET returns JSON list → probe.progress == {} → no stall
```

Plus a direct unit block for `remote_business_response_ok` truth table.

- [ ] **Step 3.2: Implement** the helper in `remote_running_recovery.py` and strip at both adapter read paths (status desc → `""`, progress desc → `""` when `remote_business_response_ok` is False; non-Mapping handled by the same check). Export endpoint untouched.

- [ ] **Step 3.3: GREEN + focused regression + commit** `fix(remote): reject failed progress responses as recovery evidence`.

---

### Task 4: Full verification & ship

- [ ] Focused regression over the 13 review-listed test files (0 failed).
- [ ] `python -m pytest --collect-only -q` then `python -m pytest tests -q` (record collected/passed/skipped/failed/warnings/duration/exit code; must be 0 failed).
- [ ] `superpowers:requesting-code-review` independent review; fix Critical/Important before push.
- [ ] Git diff scope audit (`git diff d97bf906..HEAD --stat` matches the four production files + tests/docs), re-verify prod SHA, push branch, verify LOCAL_HEAD == REMOTE_HEAD and PR #1 still open.
