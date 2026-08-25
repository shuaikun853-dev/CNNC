# STEP_1 Remote Stall Recovery — Review Fix (Round 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four independent-review findings (2 BLOCKER, 2 IMPORTANT) on branch `fix/step1-remote-running-stall-recovery-20260825` (reviewed HEAD 72c2ad6) without touching production semantics outside the recovery feature.

**Architecture:** All four fixes stay inside the round-1 recovery surface: `legacy_zhaoshang_adapter.py` (write-path gates), `remote_running_recovery.py` (pass bounds + evidence strictness), `daily_audit_store.py` (transition validation), `incremental_price_audit.py` (phase order + budget wiring), `incremental_audit_runtime.py` (bridge timeout plumbing), `incremental_price_audit_finalize.py` (validator strictness).

**Tech Stack:** Python 3.14, pytest, SQLite durable store, cmd.exe on Windows (use `--basetemp` under `_run\`).

**Spec:** `docs/superpowers/specs/2026-08-25-step1-remote-running-stall-recovery-design.md` (round 1) + this round's review findings text.

## Global Constraints

- BASE_SHA `6efdc79c5bb10605f05f2931066a833f986952e5`; REVIEW_BASE_HEAD `72c2ad6c6be2b0b339d941e49de6e9d76d121d22`; production branch must not move.
- No rebase / merge / squash / cherry-pick; append commits 7-10 on top of 72c2ad6.
- Forbidden zones untouched: 科力普 crawler, STEP_2/3, Detail, Model, source stability, force-finalize ≤10, Scheduler, SUBMIT_UNKNOWN semantics, strict NO_RESULT resolver.
- Every finding: RED first (watch it fail on 72c2ad6 code), then minimal GREEN.
- All remote interactions in tests use fakes; no real writes, no production SQLite writes.
- Status vocabulary is evidence-anchored: `本次任务执行完毕` = terminal (external tool auto_crawl_task.py:1048, production job 54); `执行中` = the platform's executing label (tool progress parser); everything else/empty fails closed as NOT proven-running.

---

### Task 1 (Commit 7): Fresh recovery must not enable an externally disabled task — Finding 1

**Files:**
- Modify: `app/services/legacy_zhaoshang_adapter.py` (`_production_restart_running_task`, `RestartStatus`)
- Modify: `app/services/remote_running_recovery.py` (`_apply_restart` decline handling)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: `RestartStatus.EXTERNALLY_DISABLED`; adapter returns it with zero writes when `resume_phase is None` and the exact task is observed disabled.
- Produces: `_apply_restart` maps `EXTERNALLY_DISABLED` → `mark_remote_manual` (fail closed, no attempt burn, no `ENABLE_SENT`).

- [ ] RED: `test_adapter_fresh_restart_of_disabled_task_issues_no_write` — fresh restart, identity ok, task enabled=False → `disable_calls == 0`, `enable_calls == 0`, outcome status `EXTERNALLY_DISABLED`, `observed_enabled is False`.
- [ ] RED: pass-level `test_fresh_externally_disabled_outcome_fails_closed_to_manual` — restart callback returns `EXTERNALLY_DISABLED` → job becomes `MANUAL_REQUIRED`, `recovery_attempts == 0`.
- [ ] Verify both fail on current code (adapter currently proceeds to enable).
- [ ] GREEN: add enum value; in fresh path, when `enabled_now` is False return `EXTERNALLY_DISABLED` before any write; `_apply_restart` treats it like `IDENTITY_CONFLICT` (manual) and resets the write-ahead `DISABLE_SENT` phase to a declined marker.
- [ ] Resume regression: `DISABLE_SENT`/`DISABLE_CONFIRMED` resume with observed disabled still proceeds to enable (existing tests cover; add explicit assertion).
- [ ] Commit 7: `fix(remote): refuse fresh restart of externally disabled jobs`

### Task 2 (Commit 8): Revalidate still-RUNNING before any fresh remote write — Finding 2

**Files:**
- Modify: `app/services/legacy_zhaoshang_adapter.py` (status gate in fresh restart path; restart deadline checks between writes)
- Modify: `app/services/remote_running_recovery.py` (`is_remote_task_still_running`, `_observation_is_no_price`)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: `is_remote_task_still_running(task_status_rsp_desc: str) -> bool` — True only for the confirmed running marker; empty/terminal/unknown → False.
- Produces: `RestartStatus.NO_LONGER_RUNNING` — zero writes when fresh restart re-check finds the exact task not proven still-running; `_apply_restart` leaves the job RUNNING/harvestable with a declined phase marker.

- [ ] RED adapter: probe RUNNING → restart write-time status `本次任务执行完毕` → 0 disable / 0 enable, status `NO_LONGER_RUNNING`.
- [ ] RED adapter: unknown status string → 0 writes, `NO_LONGER_RUNNING`.
- [ ] RED adapter: status `执行中` → exact restart still works (control).
- [ ] RED evidence: observations with terminal/empty `task_status_rsp_desc` never satisfy `stalled` (evaluate_running_stall).
- [ ] RED resume: `ENABLE_SENT` reconciliation with remote already enabled still returns RESTARTED reconcile-only (new gate must not fire on resume).
- [ ] GREEN: fresh path re-queries `query_task_status` after identity lookup and before the first disable; gate via `is_remote_task_still_running`; `_observation_is_no_price` requires the observation's status to pass the same predicate.
- [ ] Commit 8: `fix(remote): revalidate live running state before restart`

### Task 3 (Commit 9): Bound stall recovery by the tick budget — Finding 3

**Files:**
- Modify: `app/services/remote_running_recovery.py` (`RECOVERY_MAX_PROBE_CANDIDATES_PER_TICK`, `RECOVERY_CALLBACK_TIMEOUT_SECONDS`, pass cap)
- Modify: `app/workflows/incremental_price_audit.py` (move `running_recovery` phase after `post_harvest`; timeout-bearing callback types)
- Modify: `app/services/incremental_audit_runtime.py` (bridge closures accept `timeout_seconds`)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: `run_running_recovery_pass(..., max_probe_candidates: int = 4)` — stops probing after the cap; remaining candidates wait for the next tick.
- Produces: `TickDependencies.remote_recovery_probe: (job, timeout_seconds) -> probe|None` and `remote_recovery_restart: (job, resume_phase, timeout_seconds) -> outcome`; the tick binds `budget.callback_timeout(RECOVERY_CALLBACK_TIMEOUT_SECONDS)` at call time.

- [ ] RED: tight remaining budget → no new probe this tick.
- [ ] RED: probe callback receives `timeout <= budget.remaining()` (capture arg).
- [ ] RED: restart callback timeout likewise bounded.
- [ ] RED: candidate cap — more candidates than the cap → bounded probe count, others untouched.
- [ ] RED: behavior — RUNNING recovery candidates present, Full Tick still executes discovery (discover callback invoked).
- [ ] GREEN: move phase after post_harvest; wire timeouts; add cap + constant.
- [ ] Existing closeout/harvest-only/drain-only regressions stay green.
- [ ] Commit 9: `fix(remote): bound stall recovery by tick budget`

### Task 4 (Commit 10): Require confirmed restart evidence before exhaustion — Finding 4

**Files:**
- Modify: `app/services/remote_running_recovery.py` (`_apply_restart` RESTARTED verification + crash-window guard)
- Modify: `app/services/daily_audit_store.py` (`mark_remote_recovery_exhausted` strict checks)
- Modify: `app/workflows/incremental_price_audit_finalize.py` (`_valid_recovery_exhausted_evidence` confirmed flags + `recovery_at`)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: shared predicate semantics — legal restart proof = `recovery_attempts == 1` AND `phase == ENABLE_CONFIRMED` AND `disable_evidence.confirmed_disabled is True` AND `enable_evidence.confirmed_enabled is True` AND non-empty valid `recovery_at` AND exact non-empty `remote_identity`.

- [ ] RED: `RESTARTED` outcome without disable_evidence / with `confirmed_disabled=False` / with `confirmed_enabled=False` → attempt not burned (stays 0), job not RECOVERY_EXHAUSTED-eligible.
- [ ] RED: finalize validator — remote job evidence with `confirmed_disabled=False` or `confirmed_enabled=False` → `remote_invalid` blocker (not accepted).
- [ ] RED: store `mark_remote_recovery_exhausted` with incomplete evidence → raises `RemoteIntentConflict`.
- [ ] GREEN: verify at all three layers; unconfirmed `RESTARTED` fails closed to `ENABLE_UNCONFIRMED`-style reconcile/manual handling; complete evidence flows unchanged.
- [ ] Commit 10: `fix(remote): require confirmed restart evidence before exhaustion`

---

### Task 5: External tool read-only compatibility preflight

- [ ] Script under `_run/`: file exists, SHA256, module import, presence + `inspect.signature` compatibility of `check_existing_task / get_task_identity / query_task_status / query_task_progress / parse_task_progress_desc / disable_task / enable_task` against adapter call sites. No remote calls.

### Task 6: Regression + full suite + review + push

- [ ] Focused suites fresh (`test_remote_running_recovery.py` + the 11 remote/finalize/runtime files that exist).
- [ ] `python -m pytest --collect-only -q` + `python -m pytest tests -q` (with `--basetemp`), 0 failed.
- [ ] superpowers:requesting-code-review on the four findings; fix Critical/Important.
- [ ] superpowers:verification-before-completion; diff scope audit; prod SHA recheck; push same branch; verify LOCAL==REMOTE; final report.
