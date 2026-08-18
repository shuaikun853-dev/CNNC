# Partial Exact Harvest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and execute inline. No subagents are requested for this production recovery.

**Goal:** Persist deterministic rows from a partial legacy export while containing ambiguity to only affected durable crawl identities.

**Architecture:** Add a pure partial-export classifier to the existing legacy adapter. Add narrowly scoped fenced store transitions and a local-only recovery command that validates all immutable remote evidence before applying per-crawl outcomes.

**Tech Stack:** Python 3, SQLite, openpyxl, pytest, argparse.

## Global Constraints

- Remote calls and `/add` calls must both remain zero.
- Do not lower provenance, mall, execution, priority, export, URL, or durable identity validation.
- Do not overwrite `SUCCESS` or `NO_RESULT`, and do not increment `harvest_attempts`.
- Do not change Model, Scheduler, P1-04, P1-05, or the 2026-08-13 golden run.
- Do not commit.

---

### Task 1: Partial Export Classifier

**Files:**
- Modify: `app/services/legacy_zhaoshang_adapter.py`
- Test: `tests/test_legacy_zhaoshang_adapter.py`

**Interfaces:**
- Produces: `classify_existing_export_partial(job) -> PartialExactHarvestOutcome`
- Outcome exposes exact matches, exact absences, ambiguities, and validation counts keyed by `(sku_id, exact_url)`.

- [ ] Add failing tests for partial exact/absent, price zero, sibling SKU isolation, foreign provenance filtering, and SKU candidate disagreement.
- [ ] Run the focused tests and confirm they fail because the classifier is absent.
- [ ] Implement provenance filtering, candidate equivalence classes, exact/canonical matching, and per-identity classification.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Idempotent Durable Transitions

**Files:**
- Modify: `app/services/daily_audit_store.py`
- Modify: `app/services/incremental_audit_status.py`
- Test: `tests/test_incremental_audit_recovery.py`

**Interfaces:**
- Produces fenced transitions for partial `SUCCESS`, `NO_RESULT`, crawl manual, and remote completion/manual reconciliation.

- [ ] Add failing tests proving terminal results are immutable and crawl manual is counted separately from pending.
- [ ] Run the focused tests and confirm the expected failures.
- [ ] Implement exact-job-bound, idempotent transitions with `EXPORTED_NO_MATCHING_ROW` evidence.
- [ ] Run the focused tests and confirm they pass.

### Task 3: Local Recovery Command

**Files:**
- Modify: `app/services/incremental_audit_recovery.py`
- Modify: `app/services/incremental_audit_runtime.py`
- Modify: `scripts/incremental_price_audit.py`
- Modify: `docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md`
- Modify: `TESTING.md`
- Test: `tests/test_incremental_audit_recovery.py`
- Test: `tests/test_incremental_audit_runtime.py`

**Interfaces:**
- Produces: `run_partial_exact_harvest_recovery(...)` and CLI `recover-partial-exact-harvest`.

- [ ] Add the eight required recovery tests, including rerun and injected mid-write crash.
- [ ] Run the focused tests and confirm RED.
- [ ] Implement pre-mutation global validation, per-crawl mapping, duplicate backend identity containment, idempotent writes, and safe event reporting.
- [ ] Run recovery, adapter, runtime, status, and operations suites.

### Task 4: Production Recovery And 13:00 Verification

**Files:**
- Update: `runtime/incremental_price_audit/2026-08-14/BATCH_STATE.md`
- Create: a new timestamped diagnostics directory under the same run.

- [ ] Reconfirm no active lease or wrapper before each local recovery.
- [ ] Reconfirm exact remote completion/priority/export evidence for jobs 1, 3, 4, 5, and 6 with only authorized read-only queries.
- [ ] Run the local command for jobs 3, 4, 5, 1, and eligible unique identities in job 6; assert `remote_calls=0` and `add_calls=0`.
- [ ] After 13:00, collect status and verify no duplicate task name, duplicate `/add`, orphan crawl, or submit unknown.
- [ ] Record exact production actions and unresolved crawl IDs in `BATCH_STATE.md`.

