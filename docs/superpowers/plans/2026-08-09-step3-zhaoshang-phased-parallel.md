# STEP 3 Zhaoshang Phased Parallel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one STEP_3 batch create Zhaoshang mall tasks sequentially, then wait for and export those tasks concurrently without repeating writes during resume.

**Architecture:** Keep the fixed STEP_3 entry point as the business orchestrator. Add a small importable phase runner for deterministic sequential-create and bounded concurrent wait/export behavior, and add an explicit read-only resume mode to the external single-mall tool.

**Tech Stack:** Python 3.14, `concurrent.futures`, `subprocess`, pytest, openpyxl.

## Global Constraints

- `/add`, enable, and priority adjustment remain sequential; an uncertain or failed create phase stops later creates and is never retried automatically.
- Concurrent workers may only query, wait, and export existing tasks.
- Existing result files skip both phases; existing tasks never repeat add, enable, or priority adjustment.
- Current batch `20260809_154847` must reuse its six recorded tasks and only create tasks for the remaining 51 URLs.

---

### Task 1: Safe external resume modes

**Files:**
- Modify: `E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py`
- Test: `tests/test_zhaoshang_resume_modes.py`

**Interfaces:**
- Produces: `run_auto_task(..., wait_export_only=False)` and CLI `--wait-export-only`.

- [ ] Write tests proving existing active tasks are not enabled or reprioritized in create-only mode, wait-export-only never creates, missing tasks fail closed, and conflicting flags fail before session creation.
- [ ] Run `python -m pytest tests\test_zhaoshang_resume_modes.py -q` and verify the new expectations fail.
- [ ] Add the mutually exclusive CLI flag and branch before every write operation.
- [ ] Re-run the focused tests and compile the external script.

### Task 2: Phased batch orchestration

**Files:**
- Create: `scripts/zhaoshang_task_phases.py`
- Modify: `scripts/process_monitor_no_push_zhaoshang.py`
- Test: `tests/test_zhaoshang_task_phases.py`
- Modify: `tests/test_monitor_stb_fallback.py`

**Interfaces:**
- Produces: `MallJob`, `run_create_phase(jobs, runner)`, and `run_wait_export_phase(jobs, runner, max_workers)`.
- Consumes: external modes `--create-only` and `--wait-export-only`.

- [ ] Write tests proving creates are sequential, a create failure stops later creates, existing exports are skipped, wait/export is bounded and actually concurrent, and one wait failure does not cancel the others.
- [ ] Run the new tests and verify they fail because the phase runner and CLI flag do not exist.
- [ ] Implement the minimal phase runner, add `--zs-wait-concurrency 6`, and refactor the fixed entry point to create all tasks before concurrent waits.
- [ ] Ensure `--zs-create-only` treats missing result files as intentional pending work rather than failures.
- [ ] Run focused third-step tests and `py_compile` for both scripts.

### Task 3: Runbook and acceptance criteria

**Files:**
- Modify: `docs/runbooks/STEP_3_NO_PUSH_PROCESSING.md`
- Modify: `TESTING.md`
- Modify: `tests/test_context_documents.py`

**Interfaces:**
- Documents the same two phases and `--zs-wait-concurrency` flag implemented in Task 2.

- [ ] Add a failing document-contract assertion for sequential writes plus concurrent read-only waits/exports.
- [ ] Update the runbook and testing commands, preserving the context-document size limits.
- [ ] Run `python -m pytest tests\test_context_documents.py -q` and the full focused STEP_3 suite.

### Task 4: Resume production batch

**Files:**
- Read and update: `runtime/模板以及推送失败数据/20260809_154847/BATCH_STATE.md`
- Generate only under: `runtime/模板以及推送失败数据/20260809_154847/`

**Interfaces:**
- Consumes the exact monitor workbook and authorization recorded in `BATCH_STATE.md`.

- [ ] Confirm the old parent/child processes are stopped and reread `BATCH_STATE.md` immediately before execution.
- [ ] Run the fixed entry point with the original timestamp, no STB or detail-reference fallback flags, and bounded wait concurrency.
- [ ] Verify five existing results are untouched, Colipu task `1227009` is only exported, and only the remaining malls receive new tasks.
- [ ] Reconcile task counts, output workbooks, summary JSON, logs, and missing-price rows.
- [ ] Update `BATCH_STATE.md` after production actions and record the next authorized action or completion state.
