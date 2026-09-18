# STEP1 Closeout Evidence Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the dashboard, force-closeout gate, finalized metrics, and final workbook use the same day-scoped failed-link projection without discarding persisted customer evidence.

**Architecture:** Add a pure projection contract to `page_price_semantics.py`. The prewarm store will persist additive per-business-day document evidence; lifecycle failure rows will be emitted at `(business_date, documentId, mall, original_url)` granularity. Finalize and read-only status will both feed their frozen rows and day-scoped failures through the same projection, while finalized status prefers immutable finalize metrics (with a workbook fallback for older finalized runs).

**Tech Stack:** Python 3.14, SQLite, Pydantic models, openpyxl, pytest.

**Spec:** User-supplied `pasted-text.txt` for the 2026-09-17 STEP1 closeout/final-workbook/dashboard evidence consistency incident.

## Global Constraints

- Base is current `origin/prod/closeout-20260818` SHA `54e78c3ed5b5febc409ef535d3e59fb754244ae2`.
- Production runtime under `E:\.A中核运维\cnnc-ops-agent\runtime` is read-only.
- No network calls, remote task changes, SaaS writes, scheduler changes, production final regeneration, or destructive SQLite migration.
- Keep audit-model summary totals separate from the operational missing-page-price count.
- Preserve the existing stale-count check and make its actual count the full final projection.
- All new persistence is additive and old code can ignore it.
- Tests use temporary SQLite/workbooks only.

---

### Task 1: RED projection and finalizer contracts

**Files:**
- Modify: `tests/test_finalize_failed_link_rows.py`
- Modify: `tests/test_incremental_price_audit_force_finalize.py`

**Interfaces:**
- Consumes: existing failed-link fixture and frozen daily-run builders.
- Produces: failing tests for complete missing counts, retained customer evidence, document/day isolation, frozen-row overlay dedupe, two-document fanout, force limit rejection, and manifest coverage.

- [ ] Add literal fixtures for 7 frozen missing plus 68 unique day-scoped failures and assert the artifact/workbook missing count is 75.
- [ ] Add a 7 plus 14 force-finalize case and assert `MISSING_PRICE_LIMIT_EXCEEDED` with actual 21.
- [ ] Add failed-link cases for retained `queryStr`/customer price, yesterday-document exclusion, yesterday-candidate exclusion, exact frozen identity overlay, and two same-day documents sharing one URL.
- [ ] Run the selected tests and record the expected failures against production HEAD.

### Task 2: RED additive evidence store and lifecycle contracts

**Files:**
- Modify: `tests/test_customer_prewarm_store.py`
- Modify: `tests/test_customer_prewarm_discovery.py`
- Modify: `tests/test_finalize_failed_link_rows.py`

**Interfaces:**
- Consumes: `CustomerPrewarmStore.upsert_document`, `intake_documents`, `process_document`.
- Produces: day-scoped document evidence available to lifecycle/finalize without network access.

- [ ] Add an old-schema migration fixture and assert candidates/links survive while the new evidence table becomes usable.
- [ ] Assert intake stores the day's title/category evidence and detail success stores extracted customer-price evidence for that same business day.
- [ ] Assert lifecycle failure rows join only that day's observations/evidence and emit one row per document identity.
- [ ] Run the tests and record the expected schema/API failures.

### Task 3: Implement the shared projection and additive persistence

**Files:**
- Modify: `app/services/page_price_semantics.py`
- Modify: `app/services/customer_prewarm_store.py`
- Modify: `app/services/customer_prewarm_discovery.py`
- Modify: `app/services/customer_prewarm_lifecycle.py`
- Modify: `app/workflows/incremental_price_audit_finalize.py`

**Interfaces:**
- Produces: `FinalPagePriceProjection`, strict business-row identity, day-scoped document evidence readers/writers, per-document failed rows, frozen overlay instructions, and deduped append instructions.
- Consumes: persisted daily observations plus customer/detail evidence; never mutable all-history links as final evidence.

- [ ] Add the evidence table and idempotent additive migration.
- [ ] Persist intake title/category and detail customer-price evidence without changing the price algorithm.
- [ ] Replace all-history `links_for_candidate` projection with business-date observations joined to day evidence.
- [ ] Apply failures to exact frozen identities first; append only unmatched identities and preserve aligned enriched/audit records.
- [ ] Recompute readiness page-price fields from the shared final projection while leaving audit summary totals unchanged.
- [ ] Run Task 1 and Task 2 tests to GREEN.

### Task 4: Unify live status, force-closeout, and finalized evidence

**Files:**
- Modify: `app/services/incremental_audit_status.py`
- Modify: `app/workflows/incremental_price_audit_finalize.py`
- Modify: `tests/test_incremental_audit_status.py`
- Modify: `tests/test_incremental_price_audit_force_finalize.py`

**Interfaces:**
- Consumes: shared `FinalPagePriceProjection` and finalize metrics.
- Produces: `missing_page_price_rows`, `frozen_missing_page_price_rows`, and `failed_customer_link_rows` from one projection; immutable finalized counts.

- [ ] Add a live-status test whose frozen count is 7 and unmatched failures are 68; assert 75 plus the two diagnostic components.
- [ ] Add a finalized-status test, mutate the prewarm fixture after finalize, and assert the persisted count does not change.
- [ ] Build live status from the same pure projection used by finalize.
- [ ] Persist authoritative final projection counts in both normal and forced finalization.
- [ ] For older finalized runs without the new metrics, derive the immutable count from the recorded final workbook rather than mutable prewarm state.
- [ ] Run status, dashboard, and force-finalize tests to GREEN.

### Task 5: Regression, read-only replay, and branch delivery

**Files:**
- Modify only if behavior changed: `TESTING.md`, `docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md`.

**Interfaces:**
- Consumes: completed implementation and copied read-only production fixtures.
- Produces: verified commit on `fix/step1-closeout-evidence-consistency-20260917`, pushed without merge/deploy.

- [ ] Run targeted failed-link, store, lifecycle, status, force-finalize, finalizer, dashboard, and JS tests.
- [ ] Run the TESTING.md STEP1 incremental regression set and then the full pytest suite if feasible.
- [ ] Copy production databases/final inputs to `E:\_zcode_tmp`, replay without network or production writes, and compare old dashboard/final evidence to the new projection.
- [ ] Run `git diff --check`, inspect `git status --short`, and scan the diff for secrets and unrelated files.
- [ ] Commit only task files with `fix(closeout): unify failed-link evidence and missing-price projection`.
- [ ] Push the branch; do not merge or deploy.
