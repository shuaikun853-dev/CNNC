# STEP_1 Remote Stall Recovery — Round 6 Review Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two Round-6 findings — (A) the 5th consecutive `PRE_WRITE_FAILED` leaves a fake durable `DISABLE_SENT` phase on the `MANUAL_REQUIRED` job, and (B) persistent recovery-probe failures have no durable cap so a permanently broken probe keeps the job RUNNING forever.

**Architecture:** One new small atomic store transition per finding (`mark_remote_zero_write_manual`, `record_remote_recovery_probe_failure`) so evidence + state land in one SQLite transaction; the recovery pass and the runtime probe bridge are minimally extended (probe failure envelope, durable `probe_health` counter kept strictly separate from `restart.phase`).

**Tech Stack:** Python 3.14, SQLite (via `DailyAuditStore`), pytest. No new dependencies.

**Spec:** Round-6 task brief (pasted 2026-08-26) + `docs/superpowers/specs/2026-08-25-step1-remote-running-stall-recovery-design.md`.

## Global Constraints

- BASE_SHA = `6efdc79c5bb10605f05f2931066a833f986952e5` (prod) must not move; dev branch `fix/step1-remote-running-stall-recovery-20260825` continues from ROUND5_HEAD `12e2aef4168e2f7455345bd3eae31b7afcb291b0`.
- No rebase/squash/amend/reset/cherry-pick/merge/deploy; append exactly two new commits (25, 26).
- Do NOT touch: Scheduler, 科力普 crawler, STEP_2/3, Detail, Source Stability, Final Verifier semantics, Priority race, mro.qx.com, Force Finalize `<=10`, Model, price algorithm, external tool sources, production SQLite, real zhaoshang tasks.
- Preserve all Round-5 guarantees (§5 of the brief): PRE_WRITE_FAILED vs DISABLE_UNKNOWN/ENABLE_UNKNOWN boundary, reconcile-only resumed phases, auth absolute deadline, business-response gate, exact terminal marker, attempt cap 4, fresh-restart budget gate, externally-disabled fresh task = 0 writes, RECOVERY_EXHAUSTED hard gate, count=0 ≠ NO_RESULT, SUBMIT_UNKNOWN no blind resend.
- Probe-failure counting is scoped to the RUNNING-stall recovery probe ONLY (never harvest/submit/priority/discovery/Detail/Model). Probe health lives in `recovery_evidence_json.probe_health`, never in `restart.phase`.
- New constants are independent: `RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES = 5` (engineering safety threshold, not production-calibrated — must be stated in the report).
- Ordinary Finalize must still block `MANUAL_REQUIRED` (probe-failure manual included); Force Finalize blocker semantics unchanged.
- Old evidence documents without `probe_health` must default to 0 consecutive failures (never treated as 5).
- TDD: every behavior change is RED first against `12e2aef` production code.

## Root Causes (verified in code at 12e2aef)

1. **BLOCKER A** — `app/services/remote_running_recovery.py:519-529` pre-writes `evidence["restart"] = {"phase": "DISABLE_SENT", ...}` durably (write-ahead). The 5th-failure branch (`:592-599`) calls `store.mark_remote_manual(...)` (which only sets `state`/`error_text`) and returns WITHOUT withdrawing the write-ahead. `mark_remote_manual` → `_set_job_state` (`daily_audit_store.py:1478-1485`) never touches `recovery_evidence_json`. Final durable state: `MANUAL_REQUIRED` + `recovery_attempts=0` + `restart.phase=DISABLE_SENT` although `disable_task` was never called. The same fake-phase survives in the fresh zero-write manual branch (`:632-640`, statuses `IDENTITY_CONFLICT`/`NOT_RUNNING`/`EXTERNALLY_DISABLED` with `resume_phase is None` — all proven zero-write outcomes).
   - Danger chain: human requeues the manual job (`requeue_manual_harvest_job` → `SUBMITTED`, ordinary harvest/reconcile → `RUNNING`) → the next pass sees `DISABLE_SENT ∈ PENDING_RESTART_PHASES` (`:811`) → reconciliation with `resume_phase="DISABLE_SENT"` → in the adapter (`legacy_zhaoshang_adapter.py:1977`) the `EXTERNALLY_DISABLED` guard only fires for `resume_phase is None`, and a resumed `DISABLE_SENT` on an observed-disabled task skips the disable write and proceeds straight to the **enable write** (`:2080-2088`) — the system auto-enables a task a human deliberately disabled.
2. **IMPORTANT B** — `incremental_audit_runtime.py:1444-1454`: `recovery_probe` catches every exception and returns `None`. `run_running_recovery_pass` (`remote_running_recovery.py:833-835`) treats a non-Mapping as a silent `continue`: no observation, no durable failure count; the per-tick attempt counter is in-memory only. A permanent probe fault (expired refreshToken, missing external tool, persistent network outage) → every tick probes, every tick fails, job stays RUNNING forever, never reaches `PRE_WRITE_FAILED` (that phase only exists in the restart protocol), never MANUAL_REQUIRED.

---

### Task 1: BLOCKER A — RED tests (A1–A4 + requeue safety)

**Files:**
- Modify: `tests/test_remote_running_recovery.py` (append a "Review round 6" section at the end)

**Interfaces:**
- Consumes: existing helpers `_stalled_store`, `_probe_payload`, `_FakeRestart`, `_run_pass`, `_run_probe_pass` (new helper added here), `_RestartSession`, `_prepared_job`, `_running_task_record`, `_real_auth_manager`, `_TimingOutAuthOpener`, constants `TASK_ID`/`TASK_NUMBER`, `RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES`.
- Produces: failing tests that pin the truthful-evidence contract.

- [x] **Step 1.1: add the `_run_probe_pass` helper and `_NeverRestart` stub**

```python
def _run_probe_pass(store, probe, restart, *, now: datetime = NOW) -> dict:
    """Like _run_pass but takes a callable probe (failure envelopes)."""
    token = store.acquire_lease(BUSINESS_DATE, "pass-probe", ttl_seconds=300)
    try:
        return run_running_recovery_pass(
            store, token, probe_remote=probe, restart_remote=restart, now=now, can_start=lambda: True
        )
    finally:
        store.release_lease(token)


class _NeverRestart:
    """Any call is a contract violation: probes that never proved a stall (or
    pre-write failures that provably wrote nothing) must dispatch no write."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    def __call__(self, job: dict, resume_phase: str | None) -> dict:
        self.calls.append((int(job["id"]), resume_phase))
        raise AssertionError("no remote write may be dispatched on this path")
```

- [x] **Step 1.2: A1 — 5th PRE_WRITE_FAILED must leave truthful terminal evidence** (`test_round6_a1_fifth_pre_write_failure_keeps_truthful_terminal_evidence`): 5 passes with per-index distinct diagnostics via `_FakeRestart`; after each pass `< 5` assert `state == "RUNNING"`, `phase == "PRE_WRITE_FAILED"`, `pre_write_failures == index + 1`; after the 5th assert `state == "MANUAL_REQUIRED"`, `recovery_attempts == 0`, `phase == "PRE_WRITE_FAILED"`, `phase not in ("DISABLE_SENT", "ENABLE_SENT")`, `pre_write_failures == 5`, `failed_status == "PRE_WRITE_FAILED"`.

- [x] **Step 1.3: A2 — five real adapter pre-write failures dispatch zero writes** (`test_round6_a2_five_adapter_pre_write_failures_dispatch_zero_writes`): mirror `test_round5_a1_auth_refresh_failure_returns_pre_write_failed` harness, loop 5× `adapter.restart_running_task(...)`, each `status.value == "PRE_WRITE_FAILED"`, assert `session.update_calls == []`, `session.post_calls == 0`, `session.get_calls == 0`.

- [x] **Step 1.4: A4 — terminal diagnostic is the 5th real failure** (`test_round6_a4_terminal_diagnostic_is_the_fifth_real_failure`): diagnostics `"fault 1".."fault 5"`; final `evidence["restart"]["diagnostic"] == "fault 5"`.

- [x] **Step 1.5: R1 — requeue after cap never resumes a stale phase** (`test_round6_requeue_after_pre_write_cap_never_resumes_stale_phase`): 5 failures → `MANUAL_REQUIRED` with `phase == "PRE_WRITE_FAILED"`; real requeue path `requeue_manual_harvest_job(token, job_id, remote_identity="lxwl|1269504|RW-20260825150224")` → `reconcile_remote_job(..., state="RUNNING")`; one more pass with a recording restart asserting `observed_phases == [None]` and final `MANUAL_REQUIRED` with `phase not in ("DISABLE_SENT", "ENABLE_SENT")`.

- [x] **Step 1.6: R2 (Blocker A final acceptance) — requeued externally-disabled task is never auto-enabled, across TWO requeue cycles** (`test_round6_requeued_externally_disabled_task_is_never_auto_enabled`): `_DangerAwareRestart` stub mimics the adapter resume hazard — a call with `resume_phase in {"DISABLE_SENT", "DISABLE_CONFIRMED"}` counts as an `enable_attempt` and returns a fake RESTARTED; a fresh call (`resume_phase is None`) returns `{"status": "EXTERNALLY_DISABLED", "restarted": False, "observed_enabled": False}`. Cycle: 5 pre-write failures → manual → requeue → RUNNING → pass (fresh EXTERNALLY_DISABLED → manual) → requeue AGAIN → RUNNING → pass. Assert `enable_attempts == 0`, every call had `resume_phase is None`, final state `MANUAL_REQUIRED`.

```python
class _DangerAwareRestart:
    """Mimics the adapter resume hazard: a resumed DISABLE_SENT phase on an
    observed-disabled task is treated as "our disable succeeded" and proceeds
    straight to the enable write."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []
        self.enable_attempts = 0

    def __call__(self, job: dict, resume_phase: str | None) -> dict:
        self.calls.append((int(job["id"]), resume_phase))
        if resume_phase in {"DISABLE_SENT", "DISABLE_CONFIRMED"}:
            self.enable_attempts += 1
            return {
                "status": "RESTARTED",
                "restarted": True,
                "disable_evidence": {"confirmed_disabled": True},
                "enable_evidence": {"confirmed_enabled": True},
            }
        return {"status": "EXTERNALLY_DISABLED", "restarted": False, "observed_enabled": False}
```

- [x] **Step 1.7: run the new tests, verify exact RED**

Run: `python -m pytest tests\test_remote_running_recovery.py -q -k round6 -p no:cacheprovider`
Expected FAIL: A1/A4/R1/R2 see actual `phase == "DISABLE_SENT"` (A2 already passes — it pins existing adapter zero-write behavior, kept as regression proof).

---

### Task 2: BLOCKER A — GREEN (atomic store transition + branch fixes)

**Files:**
- Modify: `app/services/daily_audit_store.py` (new method after `mark_remote_manual`, ~line 1148)
- Modify: `app/services/remote_running_recovery.py` (`_apply_restart` 5th-failure branch `:592-599` and fresh zero-write manual branch `:632-640`)

**Interfaces:**
- Produces: `DailyAuditStore.mark_remote_zero_write_manual(token, job_id, *, remote_identity: str, evidence: Mapping[str, Any], error: str) -> dict[str, Any]` — one immediate transaction: lease validated; job scoped to the run; state must be `RUNNING`; `recovery_attempts` must be 0; exact remote identity must match; writes the caller's final evidence document + `state='MANUAL_REQUIRED'` + `error_text`; raises `RemoteIntentConflict` on any precondition breach.

- [x] **Step 2.1: store method**

```python
def mark_remote_zero_write_manual(
    self,
    token: LeaseToken,
    job_id: int,
    *,
    remote_identity: str,
    evidence: Mapping[str, Any],
    error: str,
) -> dict[str, Any]:
    """Terminalize a proven zero-write recovery outcome atomically.

    The truthful evidence document and the MANUAL_REQUIRED state must land
    in ONE transaction: writing evidence and state separately could crash
    between them and strand the write-ahead DISABLE_SENT phase (a pending
    remote write that never happened) on a terminal job — after a human
    requeue that fake pending phase would bypass the EXTERNALLY_DISABLED
    guard and could auto-enable a task a human deliberately disabled.
    Legal only from RUNNING with an unburned attempt and the exact
    confirmed remote identity.
    """
    from app.services.remote_running_recovery import encode_recovery_evidence

    normalized_identity = str(remote_identity or "").strip()
    with self._transaction(immediate=True) as connection:
        self._validate_lease(connection, token)
        job = self._job_for_lease(connection, token, job_id)
        if str(job["state"]) != "RUNNING":
            raise RemoteIntentConflict("zero-write manual requires the RUNNING state")
        if int(job["recovery_attempts"] or 0) != 0:
            raise RemoteIntentConflict("zero-write manual requires an unburned recovery attempt")
        if not normalized_identity or str(job["remote_identity"] or "").strip() != normalized_identity:
            raise RemoteIntentConflict("zero-write manual requires the exact confirmed remote identity")
        connection.execute(
            "UPDATE remote_jobs SET state='MANUAL_REQUIRED', recovery_evidence_json=?, error_text=?, updated_at=? WHERE id=?",
            (encode_recovery_evidence(evidence), str(error), self._utcnow(), job_id),
        )
        return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]
```

- [x] **Step 2.2: `_apply_restart` 5th-failure branch** — replace the `mark_remote_manual` call with the truthful terminal record + atomic transition:

```python
        if consecutive >= RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES:
            # The write-ahead DISABLE_SENT persisted above must be withdrawn
            # in the SAME transaction that enters MANUAL_REQUIRED: leaving a
            # pending-remote-write phase that never happened on a terminal
            # job would (after a human requeue) bypass the EXTERNALLY_DISABLED
            # guard and could auto-enable a task a human disabled.
            evidence["restart"] = {
                "phase": "PRE_WRITE_FAILED",
                "phase_at": now.astimezone(UTC).isoformat(timespec="seconds"),
                "failed_status": status,
                "pre_write_failures": consecutive,
                "diagnostic": _text(outcome.get("diagnostic")) or status,
                "manual_reason": "CONSECUTIVE_PRE_WRITE_FAILURE_LIMIT",
            }
            store.mark_remote_zero_write_manual(
                token,
                job_id,
                remote_identity=_text(job.get("remote_identity")),
                evidence=evidence,
                error=(
                    f"remote stall recovery failed pre-write {consecutive} consecutive "
                    f"times; no remote write was dispatched; last failure: "
                    f"{_text(outcome.get('diagnostic')) or status}"
                ),
            )
            return outcome
```

- [x] **Step 2.3: `_apply_restart` fresh zero-write manual branch** — for `IDENTITY_CONFLICT` / `NOT_RUNNING` / `EXTERNALLY_DISABLED` with `resume_phase is None` (all proven zero-write), record the truthful terminal phase `{"phase": status, "phase_at": now, "failed_status": status, "diagnostic": ..., "manual_reason": "ZERO_WRITE_OUTCOME"}` via the same atomic method; resumed variants (`resume_phase is not None`) keep today's `mark_remote_manual` + real phase untouched.

- [x] **Step 2.4: GREEN run** — `python -m pytest tests\test_remote_running_recovery.py -q -p no:cacheprovider` → 0 failed (including all Round-5 regressions).

- [x] **Step 2.5: Commit 25**

```bash
git add app/services/daily_audit_store.py app/services/remote_running_recovery.py tests/test_remote_running_recovery.py
git commit -m "fix(remote): persist truthful evidence when pre-write retry cap trips"
```

---

### Task 3: IMPORTANT B — RED tests (B1–B8 + compat + finalize)

**Files:**
- Modify: `tests/test_remote_running_recovery.py` (extend the round-6 section)
- Modify: `tests/test_incremental_audit_runtime.py` (bridge envelope test)

**Interfaces:**
- Consumes: `RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES` (does not exist yet — RED), `_stalled_store`, `_seed_candidate_jobs`, `_run_probe_pass`, `_NeverRestart`, `_recovered_store`/`_stalled_store` + `_seed_linked_crawl` + `_freeze` + `build_frozen_audit_artifact`.

- [x] **Step 3.1: B1 — single probe failure (None and envelope shapes) is durable, job stays RUNNING, `restart` stays None** (`test_round6_b1_...` ×2). Legacy doc without `probe_health` must yield `consecutive_failures == 1` (compat default 0).
- [x] **Step 3.2: B2 — 4 failures → RUNNING, count 4** (`test_round6_b2_four_probe_failures_still_running`).
- [x] **Step 3.3: B3/B6 — 5th failure → MANUAL_REQUIRED, count 5, `recovery_attempts == 0`, `restart is None`, restart callback never called** (`test_round6_b3_fifth_probe_failure_fails_closed_to_manual`); also a decode-level test `test_round6_legacy_evidence_without_probe_health_defaults_to_zero`.
- [x] **Step 3.4: B4 — fail×2 then success resets the streak in the same evidence write as the observation append** (`test_round6_b4_probe_success_resets_failure_streak`): after success `evidence.get("probe_health") is None`, one observation appended, `counters["recovery_probes"] == 1`.
- [x] **Step 3.5: B5 — fail,fail,success,fail → count 1 (consecutive, not lifetime)** (`test_round6_b5_probe_failure_count_is_consecutive`).
- [x] **Step 3.6: B7 — 10 candidates, envelope failures, tick 1 probes exactly 4** (`test_round6_b7_probe_failure_cap_per_tick_is_four`).
- [x] **Step 3.7: B8 — fair progression across ticks** (`test_round6_b8_probe_failures_do_not_break_fair_progression`): 10 candidates all failing; 5 ticks → first 4 are `MANUAL_REQUIRED` with count 5, jobs 5–10 never probed (evidence None); tick 6 → next 4 candidates get their first attempts.
- [x] **Step 3.8: finalize block** (`test_round6_probe_failure_manual_blocks_ordinary_finalize`): `_stalled_store` + `_seed_linked_crawl`, 5 failing passes → `MANUAL_REQUIRED`, `_freeze`, `build_frozen_audit_artifact` → `readiness.remote_manual >= 1` and `artifact is None`.
- [x] **Step 3.9: bridge envelope** (`test_round6_recovery_probe_bridge_reports_failure_envelope`; placed next to the `test_i1_bridge_*` harness in `tests/test_remote_running_recovery.py` — the i1 bridge harness already lives there, so the runtime test file stays untouched): the bridge's `recovery_probe` on an unreconstructable job returns `{"probe_failed": True, "diagnostic": ...}` instead of `None`.
- [x] **Step 3.10: update `test_case_c3_failed_probe_writes_nothing_and_stays_harvestable` to the new contract** — a failed probe now durably records `probe_health` (no observation, job stays RUNNING and harvestable). This is the intended behavior change mandated by the brief (§14–§16); the old assertion `recovery_evidence_json is None` is superseded.
- [x] **Step 3.11: run, verify exact RED** — `python -m pytest tests\test_remote_running_recovery.py tests\test_incremental_audit_runtime.py -q -k "round6 or case_c3" -p no:cacheprovider`. Expected: B-suite RED (missing `probe_health` / missing constant / bridge returns None), c3 RED (no durable write today).

---

### Task 4: IMPORTANT B — GREEN (constant, decode, bridge envelope, pass recording, store method)

**Files:**
- Modify: `app/services/remote_running_recovery.py` (constant near `:140`, `decode_recovery_evidence` `:249-270`, probe branch of `run_running_recovery_pass` `:833-839`)
- Modify: `app/services/incremental_audit_runtime.py` (`recovery_probe` `:1444-1454`)
- Modify: `app/services/daily_audit_store.py` (new method `record_remote_recovery_probe_failure` next to `update_remote_recovery_evidence`)

**Interfaces:**
- Produces:
  - `RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES = 5` (independent of the pre-write constant).
  - `decode_recovery_evidence` now also returns `"probe_health": dict | None` (legacy/absent → None).
  - Probe failure envelope contract: a Mapping with `probe_failed is True` and a `diagnostic` string; any other Mapping is a successful probe.
  - `DailyAuditStore.record_remote_recovery_probe_failure(token, job_id, *, remote_identity: str, failed_at: str, failure_reason: str) -> dict[str, Any]`.

- [x] **Step 4.1: constant**

```python
# Durable cap for persistent recovery-PROBE faults (expired refreshToken,
# missing external tool, persistent transport outage): consecutive failures
# of the read-only probe itself fail closed to manual so a permanently
# broken probe cannot keep a job RUNNING forever.  Independent of the
# pre-write constant: probe health and the restart protocol are different
# stages.  5 is an engineering safety threshold, not production-calibrated.
RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES = 5
```

- [x] **Step 4.2: `decode_recovery_evidence` preserves `probe_health`** (dict or None) alongside version/observations/restart; legacy rows default to None.

- [x] **Step 4.3: bridge envelope** — `recovery_probe`'s `except Exception` returns `{"probe_failed": True, "diagnostic": f"probe raised {type(exc).__name__}"}` (type name only — no message content leaks into durable storage).

- [x] **Step 4.4: pass recording + success reset**

```python
        probe = probe_remote(job)
        probe_failed = isinstance(probe, Mapping) and probe.get("probe_failed") is True
        if probe_failed or not isinstance(probe, Mapping):
            # A probe that ran but produced no legal read-only evidence is a
            # retryable fault — but a PERSISTENT one must fail closed, or a
            # permanently broken probe (expired refreshToken, missing tool,
            # transport outage) leaves the job RUNNING forever.  Counting is
            # scoped to this recovery probe stage only; probe health lives
            # beside (never inside) restart.phase because a probe failure has
            # not entered the restart protocol.
            store.record_remote_recovery_probe_failure(
                token,
                int(job["id"]),
                remote_identity=_text(job.get("remote_identity")),
                failed_at=now.astimezone(UTC).isoformat(timespec="seconds"),
                failure_reason=(
                    _text(probe.get("diagnostic"))
                    if probe_failed
                    else "probe callback returned no evidence"
                ),
            )
            continue
        counters["recovery_probes"] += 1
        evidence = decode_recovery_evidence(job.get("recovery_evidence_json"))
        if evidence.get("probe_health") is not None:
            # A legal observation proves the probe path works: the streak
            # ends in the SAME evidence write that appends the observation
            # (one transaction — no crash gap can resurrect the old count).
            del evidence["probe_health"]
        evidence["observations"].append(observation_from_probe(probe, now=now))
```

- [x] **Step 4.5: store method** — one immediate transaction: lease validated; job scoped; state must be `RUNNING`; exact identity must match; recompute the increment from the STORE's durable document (decode → `probe_health.consecutive_failures + 1`, set `last_failure_at`/`last_failure_reason`); if the count reaches `RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES` also set `state='MANUAL_REQUIRED'` + `error_text="remote stall recovery probe failed N consecutive times; no restart was attempted; last failure: <reason>"` in the same transaction; else persist only the evidence; return the row.

- [x] **Step 4.6: GREEN run** — `python -m pytest tests\test_remote_running_recovery.py tests\test_incremental_audit_runtime.py -q -p no:cacheprovider` → 0 failed.

- [x] **Step 4.7: Commit 26**

```bash
git add app/services/remote_running_recovery.py app/services/incremental_audit_runtime.py app/services/daily_audit_store.py tests/test_remote_running_recovery.py tests/test_incremental_audit_runtime.py
git commit -m "fix(remote): bound persistent recovery probe failures"
```

---

### Task 5: Focused regression + full suite

- [x] **Step 5.1: focused** (brief §36): `test_remote_running_recovery.py test_auth_manager.py test_legacy_zhaoshang_adapter.py test_incremental_audit_runtime.py test_incremental_audit_recovery.py test_incremental_price_audit.py test_incremental_price_audit_finalize.py test_incremental_audit_operations.py test_remote_export_no_price_resolution.py test_incremental_price_audit_force_finalize.py test_closeout_before_1830.py test_harvest_only.py test_zhaoshang_resume_modes.py` → 0 failed.
- [x] **Step 5.2: symbol sweep** — grep tests touching `mark_remote_manual|requeue_manual_harvest_job|reconcile_remote_job|run_running_recovery_pass|probe_remote|recovery_probe|PRE_WRITE_FAILED|PENDING_RESTART_PHASES` and run them.
- [x] **Step 5.3: full suite** — `python -m pytest --collect-only -q` then `python -m pytest tests -q -p no:cacheprovider --basetemp=_run\basetemp-r6` with `PYTHONIOENCODING=utf-8`; record collected/passed/skipped/failed/warnings/duration/exit code; must be 0 failed.

### Task 6: Independent review, scope audit, push, report

- [x] **Step 6.1:** dispatch an independent code reviewer (superpowers:requesting-code-review) with the brief's 15 focus questions; fix Critical (must) and Important (before push).
- [x] **Step 6.2:** scope audit — `git status --short`, `git diff --check`, `git diff 12e2aef..HEAD --stat` (+ full diff review); expected files only: the three app files + two test files + this plan.
- [x] **Step 6.3:** prod SHA re-check — `git fetch origin && git rev-parse origin/prod/closeout-20260818` must still be `6efdc79c...`; STOP if moved.
- [x] **Step 6.4:** push — `git push origin fix/step1-remote-running-stall-recovery-20260825`.
- [x] **Step 6.5:** post-push verify — local HEAD == `git ls-remote` remote HEAD; PR #1 OPEN, NOT MERGED.
- [x] **Step 6.6:** final report in the brief's §44 format; stop (no merge/deploy/production writes).
