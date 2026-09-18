# Local Fallback Production Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate `feat/incremental-local-fallback-8sites-20260917` at `ec37a2cfe1d7719af942176a34679a7e2c1ef56c` with production `54e78c3ed5b5febc409ef535d3e59fb754244ae2` so final output has one truthful result per exact customer URL and never appends a failed row for a URL already resolved by a valid local fallback.

**Architecture:** Preserve both histories with a non-fast-forward merge on an integration-only branch. Add one narrow page-price semantic for recognizing a valid local-fallback success, then consult that semantic for each `(mall, original_url)` candidate before the production failed-link projection is created. Keep job-level stale-state closure separate: it still requires every linked crawl to have a valid result, while failed-row suppression applies only to the exact locally resolved URL.

**Tech Stack:** Python 3.14, SQLite, pytest, openpyxl, Node.js dashboard tests, Git worktrees, GitHub pull request workflow.

**Spec:** User-provided task brief, “CNNC production-baseline integration + semantic conflict repair + independent verification”, dated 2026-09-17.

## Global Constraints

- Integration base is exactly `54e78c3ed5b5febc409ef535d3e59fb754244ae2`; the original feature branch and `ec37a2c` remain unmodified.
- Preserve production failed-customer-link projection and `remote_jobs.recovery_attempts` migration/recovery behavior.
- Preserve feature `local_fallback_work`, exact/final remote check, atomic claim, retry/race handling, late-remote immutability, 17:00-17:57 `PT3M` generation, and full STEP 1 authorization gate.
- Failed-link suppression is exact `(mall, original_url)` only and requires crawl `SUCCESS`, parseable `result_json`, `valid_page_price_result(result)`, and `page_price_source.startswith("本地兜底:")`.
- Never suppress sibling failed URLs because another URL in the same remote job succeeded.
- No real crawler requests, Zhaoshang task creation/priority writes, business writes, scheduler registration, production merge, or deployment.
- Keep C: read-only. Put TEMP, TMP, pytest basetemp, caches, logs, and reports on E:.
- Push and create a PR only after all verification gates pass and the remote production SHA still equals the integration base.

---

### Task 1: Record the auditable integration topology

**Files:**
- Modify: Git history only
- Verify: `app/services/daily_audit_store.py`
- Verify: `app/workflows/incremental_price_audit_finalize.py`

**Interfaces:**
- Consumes: production `54e78c3` and feature `ec37a2c`
- Produces: one non-fast-forward merge commit retaining both parents and all production/feature code

- [ ] **Step 1: Capture base evidence**

Run `git status --short --branch`, `git merge-base 54e78c3 ec37a2c`, `git diff --stat 307e41b..54e78c3`, and `git diff --stat 307e41b..ec37a2c` from the integration worktree.

- [ ] **Step 2: Merge without rewriting either lineage**

Run `git merge --no-ff --no-commit feat/incremental-local-fallback-8sites-20260917`, inspect every overlapping hunk, and confirm that automatic textual success is not treated as semantic success.

- [ ] **Step 3: Audit the merged schema and finalize paths**

Confirm `daily_audit_store.py` contains both production `recovery_attempts`/`bump_remote_job_recovery_attempt()` behavior and feature `local_fallback_work` schema/methods. Confirm finalize contains both failed-link appending and local stale-job closure.

- [ ] **Step 4: Commit the merge**

Commit with a message that names the production baseline and feature integration; record whether textual conflicts occurred.

### Task 2: Prove the failed-link semantic conflict RED

**Files:**
- Modify: `tests/test_finalize_failed_link_rows.py`
- Modify only if a finalize-level assertion belongs there: `tests/test_incremental_price_audit_finalize.py`

**Interfaces:**
- Consumes: merged production failed-link projection and feature local-fallback persistence
- Produces: regression tests at the public read-only `failed_customer_link_rows()` and artifact-build boundaries

- [ ] **Step 1: Add Case 1**

Create a stale/dead `SUBMIT_UNKNOWN` remote job for URL A, persist a valid `本地兜底:*` crawl success, and assert A is absent from `failed_customer_link_rows()`. Build the final artifact with failed-link inclusion and assert A appears once with its local price, never as `爬取失败`.

- [ ] **Step 2: Add Cases 2 and 4**

Attach A/B/C/D to one dead remote job: A and B valid local successes, C terminal/unresolved, D `NO_RESULT`. Assert only C/D remain failed. Also keep a focused A-local/B-`NO_RESULT` test to prove job-level `NO_RESULT` does not taint A.

- [ ] **Step 3: Add Case 3**

Complete local fallback for A, then deliver late remote terminal/failure evidence. Assert A's stored local success remains immutable and no failed row is projected.

- [ ] **Step 4: Run RED evidence before production-code edits**

Run only the four new tests with `E:\Python314\python.exe -m pytest ... -vv --basetemp=E:\_zcode_tmp\pytest-local-fallback-integration-red`. The expected failure is that locally resolved exact URLs are still returned by `failed_customer_link_rows()` and/or duplicated in the final artifact; test setup errors do not count as RED.

- [ ] **Step 5: Commit RED tests**

Commit the regression tests separately so the failing specification is auditable.

### Task 3: Implement exact-URL failed-row suppression

**Files:**
- Modify: `app/services/page_price_semantics.py`
- Modify: `app/services/customer_prewarm_lifecycle.py`
- Test: `tests/test_finalize_failed_link_rows.py`
- Test: `tests/test_incremental_price_audit_finalize.py`

**Interfaces:**
- Consumes: crawl mappings containing `state` and `result_json`
- Produces: `is_local_fallback_success(crawl: Mapping[str, Any]) -> bool` as the single validation rule used by job closure and candidate-level projection

- [ ] **Step 1: Reuse the shared semantic**

Use `is_local_fallback_success()` from `page_price_semantics.py`; extend it only if the regression tests expose a missing parse/validation case. Do not duplicate JSON, source-prefix, or price validation in lifecycle/finalize.

- [ ] **Step 2: Read exact candidate evidence**

Add a read-only query/helper in `customer_prewarm_lifecycle.py` that selects crawl evidence for the candidate's exact `(mall, original_url)` within the owning daily state database. A valid local success short-circuits failure projection for that candidate only.

- [ ] **Step 3: Preserve sibling failure classification**

Leave `_failed_link_reason()` precedence intact for candidates not locally resolved: redispatch exhausted, `NO_RESULT`, dead `SUBMIT_UNKNOWN`, terminal/dead, and `TERMINAL_FAILED` continue to project their production reasons.

- [ ] **Step 4: Run GREEN evidence**

Run the same four tests and basetemp used for RED. Require all four to pass, then run the complete changed test files.

- [ ] **Step 5: Commit the minimal fix**

Commit only the shared semantic/lifecycle fix and directly related test adjustments.

### Task 4: Verify migrations and document the combined semantics

**Files:**
- Modify: `tests/test_daily_audit_store.py`
- Modify: `docs/runbooks/STEP_1_INCREMENTAL_SCHEDULER.md`
- Modify if command coverage changes: `TESTING.md`

**Interfaces:**
- Consumes: SQLite database created before `recovery_attempts` and `local_fallback_work`
- Produces: repeatable initialization retaining old rows, both schema additions, and required indexes

- [ ] **Step 1: Add/extend old-database migration coverage**

Start from the oldest test schema supported by the store, open it twice, and assert `remote_jobs.recovery_attempts`, `local_fallback_work`, and `ux_local_fallback_active_url` exist without duplicate-ALTER errors or data loss.

- [ ] **Step 2: Run migration and lifecycle tests**

Run `tests/test_daily_audit_store.py`, `tests/test_customer_prewarm_lifecycle.py`, and the new failed-link tests with an E: basetemp.

- [ ] **Step 3: Document the final rule**

State that failed rows mean the exact URL remains unresolved; a valid local fallback suppresses only that exact URL, while stale-job closure remains all-linked-crawls-valid plus at least one local success.

- [ ] **Step 4: Commit tests and documentation**

Keep migration/doc changes separate from the production semantic fix where practical.

### Task 5: Run layered verification and compare the newest production baseline

**Files:**
- Create test logs only under `E:\_zcode_tmp\cnnc-local-fallback-prod-integration-20260917\`
- Do not modify baseline or crawler repositories

**Interfaces:**
- Consumes: detached pure production `54e78c3`, final integration HEAD, crawler `28870666`
- Produces: comparable pass/skip/fail counts, failed test names/stacks, isolated flake evidence, and `NEW_FAILURES`

- [ ] **Step 1: Cross-feature tests**

Run the seven named Python files from the brief plus scheduler frequency tests using E: TEMP/TMP and basetemp.

- [ ] **Step 2: Fresh CNNC relevant tests**

Collect and run tests matching incremental, fallback, finalize, prewarm, dashboard, scheduler, and daily-audit-store behavior. Save the exact collected set and result.

- [ ] **Step 3: Dashboard JavaScript and Critical Safety**

Run `node tests/js/run_dashboard_js_tests.js` and the repository-defined complete Critical Safety command. Require fresh integration-HEAD evidence.

- [ ] **Step 4: Full production baseline**

In a detached clean worktree at `54e78c3`, run `E:\Python314\python.exe -m pytest tests -q --basetemp=E:\_zcode_tmp\pytest-prod-54e78c3-full` and retain the complete log.

- [ ] **Step 5: Full integration suite**

Run the exact same command shape at integration HEAD with a separate E: basetemp. Compare failed node IDs, not only counts. Any integration-only failure blocks release.

- [ ] **Step 6: Diagnose every discrepancy**

For timing candidates, follow systematic debugging: isolated rerun on production, isolated rerun on integration, then classify from evidence. Do not label a failure flaky without both reruns.

- [ ] **Step 7: Re-run the crawler suite unchanged**

At crawler `28870666`, run its full 203-test command with E: TEMP/TMP and basetemp. Do not make crawler changes unless a contract failure is proven.

### Task 6: Independent review, final production gate, push, and PR

**Files:**
- Review all changes from `54e78c3` through integration HEAD
- Do not modify production or scheduler state

**Interfaces:**
- Consumes: final diff and all fresh verification evidence
- Produces: P0/P1/P2 review disposition and, only if every gate passes, a remote integration branch and PR targeting `prod/closeout-20260818`

- [ ] **Step 1: Request independent code review**

Give the reviewer the exact base/head SHAs and ask specifically about duplicate success+failed rows, exact identity, sibling failures, cross-day/replacement jobs, late remote evidence, fail-closed remote checks, schema upgrades, production writes, and authorization gates.

- [ ] **Step 2: Resolve review findings**

Fix every P0/P1 with a failing test first and rerun affected/full gates. List each P2 and explicitly decide whether it belongs in this scoped change.

- [ ] **Step 3: Run final verification from a clean tree**

Confirm intended files only, no credentials/runtime artifacts, all required evidence is fresh, and `P0=0`, `P1=0`, `NEW_FAILURES=0`.

- [ ] **Step 4: Re-fetch production and compare SHA**

Run `git fetch origin prod/closeout-20260818` and require `git rev-parse origin/prod/closeout-20260818` to equal `54e78c3ed5b5febc409ef535d3e59fb754244ae2`. If it moved, stop without push or PR.

- [ ] **Step 5: Push only the integration branch and open the PR**

Push `integration/incremental-local-fallback-prod-54e78c3-20260917`, create a PR to `prod/closeout-20260818`, record its URL, and do not merge it.
