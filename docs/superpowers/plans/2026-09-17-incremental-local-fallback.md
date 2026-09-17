# Incremental Local Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable, link-scoped automatic local fallback to the incremental STEP_1 scheduler while preserving the full STEP_1 manual authorization gate.

**Architecture:** A focused `incremental_local_fallback.py` module owns time policy, final remote confirmation, atomic claims, pinned child execution, strict output validation, result provenance, and metrics. `DailyAuditStore` adds a durable one-row-per-crawl fallback ledger. The tick invokes the module only after a remote harvest; finalization counts unresolved crawl URLs rather than stale remote job labels.

**Tech Stack:** Python 3.12+, SQLite, openpyxl, PowerShell 5.1 scheduler scripts, pytest.

**Spec:** `C:\Users\13013\.codex\attachments\691b159c-0d4a-4651-9f54-f5b82cf8dae3\pasted-text.txt`

## Global Constraints

- Base SHA is `307e41b20feba502495917e1090b56d596685cb6`.
- No production directory, production SQLite, task registration, remote task creation, priority change, approval, rejection, or deployment.
- Automatic fallback is limited to current-day unresolved exact URLs in the eight-site allowlist.
- The pinned crawler must be clean and exactly at the new reviewed crawler commit.
- Retryable local failures get at most two attempts per URL per business date; risk/scope/manifest failures never retry.

---

### Task 1: Durable policy and atomic claim

**Files:**
- Create: `app/services/incremental_local_fallback.py`
- Modify: `app/services/daily_audit_store.py`
- Create: `tests/test_incremental_local_fallback.py`
- Modify: `tests/test_daily_audit_store.py`

**Interfaces:**
- Produces: policy evaluation for `DAYTIME_60M`, `PEAK_3M`, `AFTER_1800`; store methods to record remote checks and atomically claim one unresolved crawl.

- [ ] Write failing boundary and two-claim race tests, including crash/no-same-tick-reclaim behavior.
- [ ] Run focused tests and verify red.
- [ ] Add the fallback ledger migration and minimal policy/claim implementation.
- [ ] Run focused tests to green.
- [ ] Commit `feat(step1): add incremental local fallback policy`.

### Task 2: Pinned crawler execution and merge

**Files:**
- Modify: `app/services/incremental_local_fallback.py`
- Modify: `app/workflows/incremental_price_audit.py`
- Modify: `scripts/audit_saas_excel_file.py`
- Modify: `tests/test_incremental_local_fallback.py`
- Modify: `tests/test_incremental_price_audit.py`

**Interfaces:**
- Consumes: existing harvest callback and eight-site crawler CLI.
- Produces: strict child manifest execution and local result provenance merged into `crawl_work`.

- [ ] Write failing remote-race, partial-job, late-remote, output-validation, retry, and manual-gate regression tests.
- [ ] Run focused tests and verify red.
- [ ] Implement final remote check, exact atomic snapshot, allowlisted child env, strict output readback, and terminal merge.
- [ ] Update the shared pinned revision to the crawler HEAD and extend the full STEP_1 allowlist without changing its authorization flow.
- [ ] Run focused integration tests to green.
- [ ] Commit `feat(step1): integrate pinned multi-site crawler fallback`.

### Task 3: Finalization, dashboard, scheduler, and docs

**Files:**
- Modify: `app/workflows/incremental_price_audit_finalize.py`
- Modify: `app/services/incremental_audit_status.py`
- Modify: `app/services/dashboard_projection.py`
- Modify: `scripts/register_incremental_price_audit_tasks.ps1`
- Modify: `scripts/run_incremental_price_audit_scheduled.ps1`
- Modify: `docs/runbooks/STEP_1_PRICE_AUDIT.md`
- Modify: `docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md`
- Modify: `TESTING.md`
- Modify: scheduler/finalization/dashboard tests.

**Interfaces:**
- Produces: stale-remote nonblocking readiness when all linked crawls are resolved; fallback counts by state/reason/site; 17:00-17:57 `PT3M` full ticks.

- [ ] Write failing stale-remote, dashboard projection, and 16:59:59/17:00/17:03/17:57/18:00 scheduler tests.
- [ ] Run focused tests and verify red.
- [ ] Implement minimal readiness/projection/schedule changes and documentation.
- [ ] Parse both PowerShell files with the real parser and run scheduler simulations.
- [ ] Run focused, critical-safety, and full repository tests; compare the two known worktree-config baseline failures.
- [ ] Commit scheduler/tests/docs in natural reviewable commits, then push the feature branch without merge or deploy.
