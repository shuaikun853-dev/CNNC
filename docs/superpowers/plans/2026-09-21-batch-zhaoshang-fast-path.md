# Batch Zhaoshang Fast Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add crash-safe exact batch materialization probing and primary-key priority boosting before batch progress polling.

**Architecture:** Keep batch create/progress in the existing adapter, add a focused allowlisted SQL service, and orchestrate it through the existing durable `PRIORITY_PENDING` state. Persisted batch payloads remain the recovery authority, so no new POST is needed after crashes.

**Tech Stack:** Python 3.14, dataclasses/enums, PyMySQL-compatible DB protocol, SQLite durable job state, pytest.

**Spec:** `docs/superpowers/specs/2026-09-21-batch-zhaoshang-fast-path-design.md`

## Global Constraints

- Keep `zhaoshang_backend=legacy` as the default and do not change legacy behavior.
- Permit only allowlisted schemas and exact bounded `exec_num`/frozen `task_id` predicates.
- Never log credentials or run production writes during tests or validation.
- Preserve the unknown batch export contract and existing manual completion behavior.
- Do not merge, deploy, push, or run a production canary.

---

### Task 1: Exact Materialization Probe

**Files:**
- Create: `app/services/batch_zhaoshang_priority.py`
- Create: `tests/test_batch_zhaoshang_priority.py`

**Interfaces:**
- Produces: `BatchMaterializationTarget`, `BatchMaterializationStatus`, `BatchMaterializationProbe`, and `probe_batch_materialization(conn, target, ...)`.
- Consumes: PyMySQL-compatible connections/cursors and stable `execute_numbers` from `PreparedBatchJob`.

- [ ] **Step 1: Write failing probe tests**

Cover `0/N`, partial, exact, duplicate `exec_num`, duplicate `task_id`, unexpected mall, invalid identity, multi-schema conflict, bounded chunks, and timeout classification with a scripted fake connection.

- [ ] **Step 2: Run the probe tests and verify RED**

Run: `E:\Python314\python.exe -m pytest tests\test_batch_zhaoshang_priority.py -q`

Expected: collection/import failure because the service does not exist.

- [ ] **Step 3: Implement the minimal read-only probe**

Validate unique non-empty targets of size `1..100`, render schema names only from the fixed allowlist, parameterize every execution identity, query in bounded chunks, and return typed fail-closed results without dynamic discovery.

- [ ] **Step 4: Run the probe tests and verify GREEN**

Run the same test command; expected all probe cases pass.

### Task 2: Exact Primary-Key Priority Boost

**Files:**
- Modify: `app/services/batch_zhaoshang_priority.py`
- Modify: `tests/test_batch_zhaoshang_priority.py`

**Interfaces:**
- Produces: `BatchPriorityOutcome`, `BatchPriorityStatus`, `boost_batch_priority_by_execute_numbers(conn, target, probe, ...)`, and bounded `ensure_batch_priority(...)` orchestration.
- Consumes: an exact `MATERIALIZED` probe with frozen schema and task rows.

- [ ] **Step 1: Write failing priority tests**

Cover all-active updates, partial already-highest rows, completed rows, zero affected rows with successful readback, completion race, identity drift, invalid readback weight, unexpected affected-row count, and idempotent reruns.

- [ ] **Step 2: Run priority tests and verify RED**

Run the targeted priority test selection and confirm failures are caused by missing boost behavior.

- [ ] **Step 3: Implement lock, update, and readback**

Freeze exact primary keys, current-read them, update only `00/01 + is_valid=1 + weight<>-1`, commit only after in-transaction verification, then perform a post-commit exact readback. Roll back on every conflict and classify updated/already-highest/already-completed counts.

- [ ] **Step 4: Implement bounded polling and DB connection factory**

Use the caller's deadline, one-second sleeps capped by remaining time, connection read/write timeouts capped by the callback budget, and the existing ignored `ZHAOSHANG_DB.env` loader without printing values.

- [ ] **Step 5: Run all priority tests and verify GREEN**

Run: `E:\Python314\python.exe -m pytest tests\test_batch_zhaoshang_priority.py -q`

### Task 3: Durable Runtime Integration

**Files:**
- Modify: `app/services/incremental_audit_runtime.py`
- Modify: `tests/test_incremental_audit_runtime.py`
- Modify: `tests/test_incremental_audit_operations.py` only if a persisted workflow transition needs direct coverage.

**Interfaces:**
- Consumes: persisted `PreparedBatchJob.execute_numbers`, exact batch reconciliation, and a priority runner returning typed outcomes.
- Produces: `PRIORITY_PENDING -> SUBMITTED -> RUNNING` sequencing with priority evidence in `result_json`.

- [ ] **Step 1: Write failing bridge tests**

Prove a confirmed POST enters `PRIORITY_PENDING`, no progress query happens before priority confirmation, pending materialization stays retryable, conflicts become manual, and confirmed priority advances before progress.

- [ ] **Step 2: Write the crash-recovery RED test**

Persist `SUBMIT_INTENT`, burn the POST claim, simulate accepted POST followed by process loss, run ambiguous-submit recovery, reconcile the exact batch number, resume priority from persisted execute numbers, and assert the POST call count remains one.

- [ ] **Step 3: Run targeted runtime tests and verify RED**

Run: `E:\Python314\python.exe -m pytest tests\test_incremental_audit_runtime.py tests\test_incremental_audit_operations.py -k "batch or crash or legacy" -q`

- [ ] **Step 4: Integrate the priority runner minimally**

Return `PRIORITY_PENDING` from submit and unknown reconciliation, execute priority only for that state, persist sanitized pending/conflict/confirmation evidence, then leave existing progress and manual export behavior unchanged.

- [ ] **Step 5: Verify runtime and legacy GREEN**

Run the targeted runtime command plus `tests\test_legacy_zhaoshang_adapter.py` and `tests\test_zhaoshang_priority_boost.py`.

### Task 4: Documentation And Historical Read-Only Validation

**Files:**
- Modify: `TESTING.md`
- Modify: `docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md`

**Interfaces:**
- Documents: batch materialization/priority sequence, allowlist, retry state, no-export limitation, test command, and canary measurements.

- [ ] **Step 1: Add focused test/runbook guidance**

Document that batch is opt-in, exact execute-number priority runs before progress, legacy remains default, and production canary requires separate authorization.

- [ ] **Step 2: Run historical read-only validation**

Query the two supplied historical execute numbers through the new probe only. Record exact status/schema/count and perform no update.

- [ ] **Step 3: Validate documentation and secret scans**

Run `tests\test_context_documents.py`, `git diff --check`, and repository secret-pattern scans without displaying secret values.

### Task 5: Verification And Review

**Files:**
- Review all changed files and tests.

**Interfaces:**
- Produces: factual test evidence and a review finding list.

- [ ] **Step 1: Run focused suites separately**

Run and record duration/exit/count for the new targeted suite, batch adapter suite, runtime suite, and legacy regression suite.

- [ ] **Step 2: Run the complete test suite**

Run: `E:\Python314\python.exe -m pytest tests -q` with E-drive temp/cache roots and record all passes/failures/skips/duration.

- [ ] **Step 3: Perform requesting-code-review workflow**

Review spec compliance and code quality, fix confirmed issues through RED/GREEN tests, and rerun affected suites.

- [ ] **Step 4: Inspect the final diff**

Run `git status --short`, `git diff --stat`, `git diff --check`, and credential-pattern scans. Confirm no production write, canary, merge, deploy, or push occurred.
