# STEP_1 Remote Stall Recovery — Review Fix Round 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four third-round findings on `fix/step1-remote-running-stall-recovery-20260825` (reviewed HEAD f03ae388): progress-based execution-state evidence, real HTTP deadline enforcement, attempted-probe cap, and the post-probe budget gate for fresh restarts.

**Architecture:** Finding A replaces the unverified `执行中` status-string gate with a tri-state classifier (`RemoteExecutionState`) that reuses the external tool's own `parse_task_progress_desc` field set and `is_progress_finished` business definition via the existing `canonical_progress_signature`. Finding B wraps the recovery probe/restart sessions in the existing `_BoundedReadOnlySession` so every real requests call is clamped to the live deadline — a CNNC-only change, no external tool modification. Finding C counts probe *attempts* (not successes) against the per-tick cap. Finding D re-checks the budget before dispatching a *fresh* restart only; already-sent resume phases (DISABLE_SENT/ENABLE_SENT) remain reconciliation-first safety work.

**Tech Stack:** Python 3.14, pytest, cmd.exe (`--basetemp=_run\basetemp -p no:cacheprovider`, `PYTHONIOENCODING=utf-8`).

**Spec:** third-round review findings text + `docs/superpowers/specs/2026-08-25-step1-remote-running-stall-recovery-design.md`.

## Global Constraints

- BASE_SHA `6efdc79c5bb10605f05f2931066a833f986952e5` (must not move); ROUND2_HEAD `f03ae388cdbf09567070dfeab04603bc5aeabf3d`; append commits 12-15, never rewrite the first 11.
- No rebase / merge / cherry-pick / reset / squash; no push to prod/main; no force push; no PR merge.
- Forbidden zones: 科力普, Scheduler, STEP_2/3, Detail, Source Stability, Final Verifier, Priority race, force-finalize ≤10, mro.qx.com, Model, price rules, recovery-exhausted final disable.
- External tool `E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py` is read-only this round; if a fix cannot be done CNNC-side, report `BLOCKED BY EXTERNAL TOOL CONTRACT` instead of faking closure.
- Closed findings must not regress: externally-disabled 0 writes; completed/unknown-state 0 writes; RESTARTED without confirmed evidence no burn; RECOVERY_EXHAUSTED full proof; SUBMIT_UNKNOWN unchanged; plain count=0 never NO_RESULT.
- Evidence anchors (external tool source): `is_progress_finished` (auto_crawl_task.py:680-703) = `total>0 AND pending==0 AND running==0 AND waiting_db==0 AND finished+failed>=total`; terminal status marker `"本次任务执行完毕"` (line 1048); `parse_task_progress_desc` returns `None` per missing label.

---

### Task 1 (Commit 12): Derive live activity from progress evidence — Finding A

**Files:**
- Modify: `app/services/remote_running_recovery.py` (classifier replaces `is_remote_task_still_running`; `_observation_is_no_price` gate)
- Modify: `app/services/legacy_zhaoshang_adapter.py` (write-time gate: status + progress + classify)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: `class RemoteExecutionState(str, Enum)` with `ACTIVE="REMOTE_ACTIVE"`, `TERMINAL="REMOTE_TERMINAL"`, `UNKNOWN="REMOTE_UNKNOWN"`; `TERMINAL_STATUS_MARKERS = ("本次任务执行完毕",)`; `classify_remote_execution_state(progress: Mapping|None, task_status_rsp_desc: object = "") -> RemoteExecutionState`.
- Removes: `is_remote_task_still_running`, `RUNNING_STATUS_RUNNING_MARKERS` (both call sites migrate).
- Semantics: terminal status marker → TERMINAL (wins over progress); progress via `canonical_progress_signature` (six ints, total>0) else UNKNOWN; any negative field or `finished+failed > total` → UNKNOWN; `pending>0 or running>0 or waiting_db>0` → ACTIVE; `finished+failed >= total` → TERMINAL; else UNKNOWN.

- [ ] RED unit cases 1-6 (ACTIVE with empty status; TERMINAL all-finished; marker wins over running; missing field UNKNOWN; total=0 UNKNOWN; contradiction UNKNOWN).
- [ ] RED evidence case 7: Job-53 pattern with `task_status_rsp_desc=""` and active progress → `stalled=True` (current code returns False — proves the blocker).
- [ ] RED adapter write-gate: probe ACTIVE but write-time progress terminal → NO_LONGER_RUNNING 0 writes; malformed/unknown progress → NO_LONGER_RUNNING 0 writes; probe ACTIVE + write-time still ACTIVE → restart proceeds even with unrecognized status string.
- [ ] Update inverted round-2 tests: `without_status` stall-evidence test flips to the round-3 contract; unknown/empty status decline tests become proceeds-with-active-progress or decline-with-unknown-progress.
- [ ] GREEN: classifier + observation gate + adapter revalidation (query_task_status AND query_task_progress at write time).
- [ ] Commit 12: `fix(remote): derive live activity from progress evidence`

### Task 2 (Commit 13): Enforce recovery deadlines on actual HTTP requests — Finding B

**Files:**
- Modify: `app/services/legacy_zhaoshang_adapter.py` (`_production_probe_running_task`, `_production_restart_running_task` wrap session in `_BoundedReadOnlySession(session, timeout_seconds, monotonic=self._monotonic, deadline=deadline)`)
- Test: `tests/test_remote_running_recovery.py` (fakes record actual timeouts)

**Interfaces:**
- Consumes: existing `_BoundedReadOnlySession` (adapter.py:370-408) — clamps `session.get/post` timeout kwarg to live remaining, raises `TimeoutError` before sending when exhausted.

- [ ] RED: probe `timeout_seconds=8` → every `session.get/post` actual timeout ≤ 8 (current code sends the tool's hard-coded 60 → fails).
- [ ] RED: restart `timeout_seconds=10` with a clock advancing 5s per request → disable timeout ≤ 5·n-remaining; enable timeout ≤ remaining after disable; lookup/status/progress calls all clamped.
- [ ] RED: deadline exhausted → next request not sent (TimeoutError, no fake record).
- [ ] GREEN: wrap both production paths' sessions; adapter's own exportTask post also flows through the wrapper.
- [ ] Commit 13: `fix(remote): enforce recovery deadlines on actual http requests`

### Task 3 (Commit 14): Cap attempted recovery probes per tick — Finding C

**Files:**
- Modify: `app/services/remote_running_recovery.py` (`run_running_recovery_pass` counters + cap on attempts)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: counters key `"recovery_probe_attempts"` (incremented before every `probe_remote` call); `"recovery_probes"` keeps its successful-observation semantics; the per-tick cap checks attempts.

- [ ] RED: 10 candidates, probe returns None → callback called exactly 4 times (current code calls 10).
- [ ] RED: 2 fail + 2 succeed + more → total callback calls == 4; successful observations == 2.
- [ ] RED: mixed success/exception ordering stays ≤ 4; failed probe writes no observation; failed probe leaves the job RUNNING (fail closed to ordinary harvest, never MANUAL_REQUIRED).
- [ ] GREEN: attempt counter + cap; commit `fix(remote): cap attempted recovery probes per tick`.

### Task 4 (Commit 15): Recheck budget before fresh restart — Finding D

**Files:**
- Modify: `app/services/remote_running_recovery.py` (`run_running_recovery_pass` stalled branch)
- Test: `tests/test_remote_running_recovery.py`

**Interfaces:**
- Produces: post-probe gate — a *fresh* restart (evidence restart phase not in `{DISABLE_SENT, ENABLE_SENT}`) requires an extra `can_start()`; resume-phase reconciliation is exempt (already-sent write safety work, SUBMIT_UNKNOWN reconcile-first parity).

- [ ] RED: can_start True at loop entry, probe flips it False, stalled=True → restart callback 0 calls, attempts 0, no DISABLE_SENT written; observation IS saved.
- [ ] RED: next pass with budget → fresh restart dispatched, attempts 1.
- [ ] RED: resume DISABLE_SENT with budget consumed after probe → restart callback still called with `"DISABLE_SENT"` (reconcile not deferred), no fresh disable.
- [ ] RED: resume ENABLE_SENT likewise called with `"ENABLE_SENT"`, reconcile-only.
- [ ] GREEN: gate + exemption; commit `fix(remote): recheck budget before fresh restart`.

---

### Task 5: Verification round

- [ ] External tool preflight re-run (SHA256 + signature compatibility, read-only).
- [ ] Focused regression: the 11 mandated files, fresh, 0 failed.
- [ ] Full suite: `--collect-only -q` then `tests -q` with basetemp flags; 0 failed.
- [ ] superpowers:requesting-code-review (12 mandated focus points); fix Critical/Important.
- [ ] superpowers:verification-before-completion; `git diff --check`; scope audit `f03ae388..HEAD`; prod SHA recheck; push; LOCAL==REMOTE; PR #1 stays OPEN; final report.
