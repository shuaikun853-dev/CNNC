# Parallel Context Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single global handoff card with per-batch state files so every step and multiple batches of the same step can run concurrently, while conversation thresholds only trigger reminders.

**Architecture:** `CURRENT_STATE.md` becomes a stable discovery and handoff protocol. Every production batch owns a `BATCH_STATE.md` inside its timestamped runtime directory, so parallel agents never update the same live-state document. Existing batch `price_audit_20260808_185049` is migrated without losing its write-action evidence.

**Tech Stack:** Markdown, Python 3.14, pytest, PowerShell, Git diff validation

## Global Constraints

- Do not call production interfaces, SQL, approval, rejection, pass, push, or crawler operations.
- Do not modify or remove unrelated user changes or runtime artifacts.
- Do not commit or push because the user has not explicitly authorized Git actions.
- A task owns one step and one batch, while any number of other step or same-step batches may run concurrently.
- Conversation length alone must never stop or reject requested work.

---

### Task 1: Encode the new context contract in tests

**Files:**
- Modify: `tests/test_context_documents.py`

**Interfaces:**
- Consumes: UTF-8 Markdown files through existing `_read(relative_path)`.
- Produces: Regression assertions for per-batch state, unrestricted batch concurrency, and reminder-only thresholds.

- [x] **Step 1: Replace the single-card assertions**

Assert that `CURRENT_STATE.md` contains `BATCH_STATE.md`, `runtime/**/BATCH_STATE.md`, and the full state enum `ACTIVE | WAITING_USER | BLOCKED | COMPLETE | ABANDONED`, and does not contain `## 当前交接卡`.

- [x] **Step 2: Add concurrency and reminder assertions**

Assert that `AGENTS.md` contains `同一步骤可以同时存在多个活跃批次`, `其他批次不得成为当前批次的阻塞条件`, and `上下文轮数本身不得作为暂停、拒绝或延后执行的理由`.

- [x] **Step 3: Require batch-local state in all runbooks**

For each file in `RUNBOOKS`, assert that it contains `BATCH_STATE.md` and does not instruct the Agent to update `CURRENT_STATE.md` with a live batch status.

- [x] **Step 4: Run the focused test and confirm the old documents fail**

Run: `python -m pytest tests/test_context_documents.py -q`

Expected: failure because the current documents still describe one global handoff card and forced handoff behavior.

### Task 2: Migrate the live batch and global protocol

**Files:**
- Create: `runtime/price_audit_20260808_185049/BATCH_STATE.md`
- Replace: `CURRENT_STATE.md`
- Modify: `AGENTS.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: The complete existing `CURRENT_STATE.md` handoff for batch `price_audit_20260808_185049`.
- Produces: One authoritative batch handoff file plus a stable global discovery protocol.

- [x] **Step 1: Copy the existing live handoff into the batch directory**

Preserve status `WAITING_USER`, all 14 task IDs, both confirmed `affected_rows` results, five missing-price rows, evidence paths, authorization limits, and the exact next action.

- [x] **Step 2: Replace `CURRENT_STATE.md` with the parallel protocol**

Document how to create a unique batch state, discover files using `runtime/**/BATCH_STATE.md`, resume a specifically identified batch, handle ambiguous “continue” requests, and ignore unrelated active batches as blockers. Include a reusable `BATCH_STATE.md` template and start/resume prompt templates.

- [x] **Step 3: Rewrite the AGENTS lifecycle rules**

Keep one task scoped to one step and batch, explicitly permit all steps and multiple same-step batches to run concurrently, allow `WAITING_USER` and `BLOCKED` batches to resume in their original task, and convert the 20/30-turn rules into non-blocking reminders.

- [x] **Step 4: Update the README overview**

Explain that active state is per-batch, other batches do not block new work, and the original task can resume after a pause.

### Task 3: Align runbooks and verification guidance

**Files:**
- Modify: `docs/runbooks/STEP_1_PRICE_AUDIT.md`
- Modify: `docs/runbooks/STEP_2_MONITOR_STATUS.md`
- Modify: `docs/runbooks/STEP_3_NO_PUSH_PROCESSING.md`
- Modify: `TESTING.md`

**Interfaces:**
- Consumes: The batch-state protocol defined in `CURRENT_STATE.md`.
- Produces: Step-specific creation, update, pause, and completion rules that never mutate a global live card.

- [x] **Step 1: Update first-step state instructions**

Create `<batch-directory>/BATCH_STATE.md` at batch start, update it after irreversible actions, set it to `WAITING_USER` or `BLOCKED` on real pauses, and set it to `COMPLETE` after final validation.

- [x] **Step 2: Update second-step state instructions**

Use the second-step timestamp directory's `BATCH_STATE.md`; record read-only progress and finish with `COMPLETE` without inspecting unrelated batches as a gate.

- [x] **Step 3: Update third-step state instructions**

Use the third-step output directory's `BATCH_STATE.md`; retain the exact monitor workbook path and authorization switches; allow the original task to resume after `WAITING_USER`.

- [x] **Step 4: Rewrite context verification in `TESTING.md`**

Replace the single-card and `CURRENT_STATE.md = ACTIVE / STEP_1` checks with per-batch state checks, concurrent discovery checks, reminder-only behavior, and validation of the migrated live batch.

### Task 4: Verify the completed migration

**Files:**
- Test: `tests/test_context_documents.py`
- Inspect: all files changed by Tasks 1-3

**Interfaces:**
- Consumes: Final Markdown and migrated batch state.
- Produces: Test, diff, and secret-scan evidence.

- [x] **Step 1: Run focused context tests**

Run: `python -m pytest tests/test_context_documents.py -q`

Expected: all tests pass.

- [x] **Step 2: Check formatting and changed-file scope**

Run: `git diff --check`

Run: `git diff -- AGENTS.md CURRENT_STATE.md README.md TESTING.md docs/runbooks tests/test_context_documents.py docs/superpowers`

Expected: no whitespace errors and no unrelated edits introduced by this implementation.

- [x] **Step 3: Scan changed documentation for credential-shaped values**

Use the existing patterns from `TESTING.md` against the changed documents and migrated `BATCH_STATE.md`.

Expected: no API keys, cookies, bearer tokens, passwords, database connection values, or login-state cURL content.

- [x] **Step 4: Reconcile migrated and concurrent evidence**

Confirm the batch state still contains 14 distinct task IDs, five missing-price items, two confirmed priority updates, and no background process. Preserve any newer authorization or completion evidence written by the batch's owning task after migration; never restore an older snapshot over concurrent progress.

- [x] **Step 5: Report without Git actions**

Summarize changed files, test results, migrated batch path, and the new non-blocking reminder behavior. Do not stage, commit, or push.
