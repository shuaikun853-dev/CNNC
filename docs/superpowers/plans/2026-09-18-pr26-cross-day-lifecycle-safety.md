# PR 26 Cross-Day Lifecycle Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the historical harvest lifecycle, block ambiguous MANUAL recovery from automatic redispatch, and avoid priority writes for a binding already known stale.

**Architecture:** Keep candidate linkage in `CustomerPrewarmStore` as the discovery source and historical `(owning_day, job_id)` as the state identity. Run a separate historical harvest phase outside reconcile locks, using the existing recovery primitive. Classify ambiguous reconciliation durably without changing the single STEP1 create owner; retain post-call commit fencing and add a final pre-call ownership guard.

**Tech Stack:** Python 3.14, SQLite, pytest, Windows PowerShell, existing customer-prewarm and STEP1 stores.

**Spec:** User's PR #26 review request of 2026-09-18; `docs/superpowers/specs/2026-09-16-prewarm-lifecycle-closeout-design.md` is historical context.

## Global Constraints

- Base SHA `54e78c3ed5b5febc409ef535d3e59fb754244ae2`; pre-fix HEAD `8fe349608940552e3b93aab98e15af5a60c33439`.
- No real Zhaoshang calls, production DB writes, Scheduler changes, merge, deployment, or second create path.
- Plan-only uses read-only SQLite and creates no adapter, ProcessLock, lease, schema, or file.
- Every test uses `tmp_path` and fake adapters; test first, observe behavioral RED, implement, then GREEN.
- All temp and test output stays on E:; preserve other worktrees and dirty files.

---

### Task 1: Historical Harvest Liveness

**Files:** Modify `app/services/customer_prewarm_cross_day_reconcile.py`, `app/services/customer_prewarm_lifecycle.py`, `scripts/customer_prewarm_dispatch.py`, `tests/test_customer_prewarm_cross_day_reconcile.py`; optionally create `tests/test_customer_prewarm_cross_day_harvest.py` and register it in `.github/workflows/critical-safety-tests.yml`.

**Interfaces:** `plan_cross_day_harvest_jobs(prewarm_store, current_business_date, state_runtime_root) -> list[tuple[str,int]]`; `run_cross_day_harvest_phase(*, prewarm_store, current_business_date, state_runtime_root, get_adapter, deadline, monotonic_value, summary, max_jobs=3, per_job_timeout_seconds=90.0) -> None`.

- [ ] Add a D1 `SUBMITTED` + D2 dispatcher lifecycle regression asserting an existing historical task is harvested from D1, not D2, and its successful crawl can be mirrored into shared cache and absorbed as `CACHE_HIT`. Run it on `8fe3496` and record behavioral failure.
- [ ] Implement read-only discovery from active latest candidate links, exact `customer-prewarm-zhaoshang` producer, canonical earlier day, exact job ID, owning run, identity, state `SUBMITTED/RUNNING/EXPORTED`, and `zhpq-` crawl proof; dedupe by `(day,id)`.
- [ ] Execute outside today's and historical reconcile lock using `harvest_existing_customer_prewarm_job` with owning-day paths. Bound cap, per-job timeout, shared deadline, and persist a separate fairness cursor before each attempt. Never call submit/create.
- [ ] Wire CLI order: today dispatch, cross-day reconcile, cross-day harvest, today harvest. Update plan-only counts through read-only handles. Run focused tests and commit RED, then GREEN.

### Task 2: MANUAL Conflict Blocker

**Files:** Modify `app/services/customer_prewarm_cross_day_reconcile.py`, `app/services/customer_prewarm_lifecycle.py`, `app/services/daily_audit_store.py`, `tests/test_customer_prewarm_cross_day_reconcile.py`, `tests/test_customer_prewarm_lifecycle.py`.

**Interfaces:** Durable ambiguity classification must make `_job_is_dead` false for a remote identity/provenance conflict; `NOT_FOUND` recovery attempts retain their existing bounded behavior.

- [ ] Add adapter-realistic MANUAL fixtures and a threshold-pass test showing old code makes A3 consider the candidate redispatchable. Record RED and zero actual remote calls in the test.
- [ ] Add a durable blocker using existing state/evidence fields or the smallest compatible additive classification; ensure both cross-day A2 and today A2 respect it and A3 excludes it.
- [ ] Verify `NOT_FOUND` threshold behavior and historical ownership remain unchanged. Run focused tests; commit RED and GREEN separately.

### Task 3: Stale Pre-Priority Write

**Files:** Modify `app/services/customer_prewarm_dispatch.py`, `app/services/customer_prewarm_cross_day_reconcile.py`, `tests/test_customer_prewarm_cross_day_reconcile.py`.

**Interfaces:** Add `pre_remote_guard: Callable[[], bool] | None = None` to `_confirm_pending_priority_job`, executed after lease renewal and immediately before `adapter.prioritize`; cross-day passes a linkage check, same-day leaves it unset. Preserve `result_commit_fence`.

- [ ] Add a deterministic rebind-between-FOUND-and-prioritize test; record old `prioritize_calls == 1` RED against expected zero.
- [ ] Add minimal guard and call it for both same-pass FOUND and next-pass PRIORITY_PENDING. Keep remote I/O outside prewarm SQLite transactions.
- [ ] Add concurrent rebind completion check and run focused tests; commit RED and GREEN separately.

### Task 4: Safety Matrix, Documentation, Review, Delivery

**Files:** Modify `TESTING.md`, `docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md`, `docs/superpowers/specs/2026-09-16-prewarm-lifecycle-closeout-design.md`, `.github/workflows/critical-safety-tests.yml` when a separate safety file is added, and relevant tests.

- [ ] Verify lock-busy, missing DB, collision, plan-only SHA-256 zero-write, ordinary STEP1, Staples, fairness, and no-second-create regressions with fake adapters and E: temp paths.
- [ ] Run focused suites, documented customer-prewarm regression, exact local critical-safety list, and full suite. Reproduce every remaining full-suite failure at exact base SHA.
- [ ] Update docs and one current-HEAD PR test matrix; preserve historical design context rather than deleting it.
- [ ] Request independent Standards, Spec, and bug/race review against base SHA; resolve P0/P1 and repeat review.
- [ ] Verify clean diff and secret absence, commit only task files, push the existing PR branch, inspect GitHub Actions' actual failure reason, update PR body, and report precise results.
