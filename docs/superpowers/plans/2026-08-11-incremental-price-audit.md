# Incremental Price Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a recoverable finite tick and idempotent daily finalize around the existing CNNC/SaaS price-audit business behavior.

**Architecture:** Extract the canonical pure audit behavior behind the legacy façade, store only daily checkpoints and remote identities in SQLite, and run the same orchestration engine in tick and finalize modes. Keep the current full-run CLI and priority implementation unchanged.

**Tech Stack:** Python 3.11+, stdlib `sqlite3`, Pydantic, openpyxl, pytest, Windows Task Scheduler.

## Global Constraints

- Scope is fixed to `tenant_id=188605` and `commodity_pool_id=222`.
- Do not call production APIs or SQL during implementation or tests.
- Do not modify the existing Zhaoshang priority implementation.
- Keep the legacy full-run entry point operational as rollback.
- Detail concurrency remains exactly 1.
- No daemon or `while True` scheduler loop.
- No git commit unless the user explicitly requests it.
- Every behavior change follows red-green-refactor TDD.

---

### Task 1: Characterize And Extract The Canonical Core

**Files:**
- Create: `app/services/saas_audit_core.py`
- Create: `app/services/zhaoshang_result_adapter.py`
- Create: `tests/test_saas_audit_core.py`
- Modify: `scripts/audit_saas_excel_file.py`
- Modify: `scripts/export_excluded_malls.py`
- Modify: `tests/test_work_01_saas_approval_json_audit.py`

**Interfaces:**
- Produces normalization, JSON parsing, customer join, Detail parsing, record construction, identity/fingerprint, price decisions, and Zhaoshang result parsing.
- The legacy script re-exports existing function names so callers and monkeypatches remain compatible.

- [ ] Add characterization tests proving the legacy façade and core return identical values for concatenated JSON, Decimal prices, customer join, Detail parsing and price decisions.
- [ ] Add an AST test that exactly one top-level definition remains for `run_zhaoshang_tasks`, `read_zhaoshang_result_xlsx`, `apply_zhaoshang_results_to_rows`, and `VALUE_DISPLAY_NAMES`.
- [ ] Run the new tests and verify they fail because the core modules do not exist and duplicate definitions remain.
- [ ] Move only the pure functions listed in the design into `saas_audit_core.py`; re-export them from the legacy script.
- [ ] Move the effective late Zhaoshang reader into `zhaoshang_result_adapter.py`; keep task creation and priority in the legacy script.
- [ ] Delete only the shadowed early definitions and retain the currently effective behavior.
- [ ] Point `export_excluded_malls.py` directly at the core to remove its reverse import of the legacy CLI.
- [ ] Run canonical, Zhaoshang resume/priority, approve and reject tests.

### Task 2: Bound Model Attempts And Elapsed Time

**Files:**
- Create: `tests/test_title_match_model_retry.py`
- Modify: `app/services/title_match_model_adapter.py`
- Modify: `.env.example`

**Interfaces:**
- Model operations accept the existing call signatures.
- Policy defaults: 3 maximum attempts and 300 seconds maximum elapsed per operation.
- `TITLE_MATCH_MODEL_MAX_ATTEMPTS` and `TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS` override defaults; existing `TITLE_MATCH_MODEL_MAX_RETRIES` remains a compatibility alias.

- [ ] Add tests for default finite attempts, elapsed exhaustion, per-attempt timeout clamping, successful retry, and invalid environment fallback.
- [ ] Run the tests and verify the current unlimited default fails them.
- [ ] Implement a monotonic deadline and bounded sleep without converting failures into business `MANUAL_REVIEW` results.
- [ ] Verify single and batch model calls raise a retry-exhausted exception carrying attempt and elapsed evidence.
- [ ] Run workflow model tests.

### Task 3: Implement The Minimal Daily SQLite Store

**Files:**
- Create: `app/services/daily_audit_store.py`
- Create: `tests/test_daily_audit_store.py`

**Interfaces:**
- Produces `DailyAuditStore`, `ProcessLock`, `LeaseUnavailable`, `FrozenSnapshotConflict`, and typed row dictionaries for runs, snapshots, items and jobs.
- Store methods use explicit transactions and never expose a shared SQLite connection across threads.

- [ ] Add failing tests for schema creation, business-date isolation and idempotent `ensure_run`.
- [ ] Add failing tests for non-blocking OS lock plus fenced lease acquire/overlap/release/expiry and stale-owner write rejection.
- [ ] Add failing tests for ordered snapshot multiplicity, NEW/CHANGED/UNCHANGED diff and distinct-fingerprint duplicate rejection.
- [ ] Add failing tests for stage result reuse when its narrow fingerprint is unchanged and reset when it changes.
- [ ] Add failing tests for durable remote intent, stable task name/payload hash, many-to-many job items and unknown-submit fail-closed behavior.
- [ ] Add failing tests for idempotent freeze and conflicting freeze rejection.
- [ ] Implement only the seven tables and transactions described in the design.
- [ ] Run store tests and inspect the temporary database schema.

### Task 4: Implement The Finite Tick Engine

**Files:**
- Create: `app/workflows/incremental_price_audit.py`
- Create: `tests/test_incremental_price_audit.py`
- Modify: `app/workflows/work_01_saas_approval_json_audit.py`

**Interfaces:**
- Produces `TickConfig`, `TickBudget`, `TickDependencies`, `TickSummary`, and `run_incremental_tick(...)`.
- Extends `audit_saas_approval_records(...)` with an optional precomputed model-result mapping while preserving existing callers.

- [ ] Add a failing test for fixed order: harvest, discover, diff, Detail, model, submit, checkpoint.
- [ ] Add failing tests proving Detail concurrency is one and successful narrow fingerprints are reused.
- [ ] Add failing tests proving the wall-clock reserve stops claiming heavy work and persists remaining backlog.
- [ ] Add failing tests for retryable external failure, crash after stage success before next stage, and rerun continuation.
- [ ] Add a failing test that a second owner returns `ALREADY_RUNNING` without invoking dependencies.
- [ ] Implement the smallest orchestration engine satisfying those tests; no sleep loop.
- [ ] Run pipeline and existing workflow tests.

### Task 5: Connect The Legacy Zhaoshang Phases

**Files:**
- Create: `app/services/legacy_zhaoshang_adapter.py`
- Create: `tests/test_legacy_zhaoshang_adapter.py`
- Modify: `scripts/zhaoshang_task_phases.py` only if a pure compatibility hook is required.

**Interfaces:**
- Produces an Adapter that groups crawl identities by mall, writes immutable input templates, calls legacy `create-only` once, and later calls `wait-export-only` with the same task name.
- No priority code is copied or modified.

- [ ] Add failing tests for stable logical job id, deterministic payload hash, legacy `(mall, skuId, URL)` request identity and URL result fan-out.
- [ ] Add failing tests that `SUBMIT_INTENT` exists before the runner is called.
- [ ] Add failing tests for client timeout after POST: state becomes `SUBMIT_UNKNOWN`, exact reconciliation is attempted, and create is never called again.
- [ ] Add failing tests for existing valid export reuse and bounded harvest timeout classified as still running.
- [ ] Implement the Adapter around injected runners, then provide the production subprocess runner without invoking it in tests.
- [ ] Run resume, task phase and priority regression tests.

### Task 6: Implement Idempotent Finalize And Materialization

**Files:**
- Create: `scripts/incremental_price_audit.py`
- Create: `tests/test_incremental_price_audit_equivalence.py`
- Modify: `app/workflows/incremental_price_audit.py`
- Modify: `app/services/daily_audit_store.py`

**Interfaces:**
- CLI commands: `tick`, `finalize`, and `status`.
- `finalize` calls the tick engine, freezes the first successful post-cutoff authoritative snapshot, drains within budget, and atomically publishes one workbook.

- [ ] Add failing CLI help tests proving no network or write occurs during import/help.
- [ ] Add failing tests for pre-cutoff refusal, first post-cutoff freeze, frozen-member stability and late-row exclusion.
- [ ] Add failing tests for finalize budget exhaustion followed by successful resume.
- [ ] Add failing tests for temp workbook validation, atomic publish, manifest reuse and conflicting output protection.
- [ ] Build a fixture from historical row shapes and compare legacy one-shot output with 4-8 incremental snapshots plus finalize for row order, multiplicity, keys and business result columns.
- [ ] Implement the CLI and materializer by reusing canonical record construction and legacy workbook writer.
- [ ] Run the equivalence test twice to prove finalize idempotency.

### Task 7: Documentation, Metrics And Full Verification

**Files:**
- Modify: `docs/runbooks/STEP_1_PRICE_AUDIT.md`
- Modify: `TESTING.md`
- Modify: `.env.example`

**Interfaces:**
- Documents safe offline setup and the external scheduler commands without creating a Windows scheduled task automatically.

- [ ] Document `tick/finalize/status`, state paths, default budgets, recovery states and legacy rollback.
- [ ] Document metrics: `today_discovered`, `today_processed`, `delta_new`, `delta_changed`, `detail_requests`, `model_attempts`, `crawl_submitted`, `crawl_running`, `crawl_completed`, `crawl_exported`, `final_pending`, `finalized_rows`.
- [ ] Run context-document budgets and secret scans.
- [ ] Run all targeted tests, then `python -m pytest tests -q` with an isolated base temp.
- [ ] Run `py_compile`, `git diff --check`, inspect `git status` and review only this task's diff without reverting pre-existing changes.
