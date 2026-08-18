# SaaS And Zhaoshang Token Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep existing cURL files as immutable bootstrap templates while automatically rotating, persisting, and injecting separate SaaS and Zhaoshang credentials.

**Architecture:** Add a focused `scripts/auth_manager.py` module that owns bootstrap, refresh, atomic persistence, per-system locking, structured cookie/header injection, and read-only 401 retry. Existing business scripts keep their URLs, payloads, and decision logic; they call the authentication module only at the request boundary or before launching the existing Zhaoshang helper.

**Tech Stack:** Python standard library (`urllib`, `http.cookies`, `json`, `threading`, `pathlib`), pytest.

## Global Constraints

- Never modify any `.curl` file.
- Once `data/auth_state.json` exists, it is the only runtime credential source; do not fall back to cURL after any state or refresh failure.
- Persist both returned tokens atomically and never log full credentials.
- Keep SaaS and Zhaoshang state and refresh locks separate.
- Automatically retry at most once, and only for read-only HTTP requests that return an explicit HTTP 401 or an unambiguous authentication-failure payload.
- Never automatically retry Zhaoshang `/add`, SaaS approve/reject, submit, push, or any other non-idempotent request.
- Do not alter business URLs, request bodies, query parameters, task creation logic, or approval logic.
- Real verification may call only the two refresh endpoints and safe read-only endpoints.

---

### Task 1: Authentication State And Rotation

**Files:**
- Create: `scripts/auth_manager.py`
- Create: `tests/test_auth_manager.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `AuthManager`, `AuthTokens`, `AuthError`, `RefreshTokenExpiredError`, `inject_auth`, and read-only request helpers.
- Consumes: `scripts.curl_utils.load_curl_command` and `internal_direct_opener`.

- [ ] Write failing tests for cURL-only bootstrap, state-only reuse, missing/corrupt state fail-closed behavior, atomic two-token persistence, rotation, redacted errors, separate locks, and structured Cookie/Blade-Auth injection.
- [ ] Run `python -m pytest tests/test_auth_manager.py -q` and confirm failures are caused by the missing module.
- [ ] Implement the minimum authentication module using a temporary sibling file plus `Path.replace`, and add an explicit `data/auth_state.json` ignore rule.
- [ ] Run `python -m pytest tests/test_auth_manager.py -q` and confirm the state/refresh tests pass.

### Task 2: Safe Request Retry Boundary

**Files:**
- Modify: `scripts/auth_manager.py`
- Modify: `tests/test_auth_manager.py`

**Interfaces:**
- Produces: a request path that rebuilds authentication headers, refreshes once on explicit authentication failure, and rejects retry for non-read-only operations.

- [ ] Write failing tests proving a read-only request retries exactly once after HTTP 401, a second 401 stops, and non-idempotent requests are never retried.
- [ ] Run the focused tests and confirm the expected failures.
- [ ] Implement the one-retry guard and authentication-failure classifier without broad error matching.
- [ ] Run the focused tests and confirm they pass.

### Task 3: Existing Entry-Point Integration

**Files:**
- Modify: `scripts/export_current_monitor_status.py`
- Modify: `scripts/approve_saas_audit_excel.py`
- Modify: `scripts/reject_saas_audit_excel.py`
- Modify: `scripts/audit_saas_excel_file.py`
- Modify: `scripts/process_monitor_no_push_zhaoshang.py`
- Modify: corresponding existing tests.

**Interfaces:**
- Consumes: `AuthManager.prepare_curl`, `AuthManager.export_environment`, and the safe read-only retry helper.

- [ ] Add failing integration tests proving SaaS status/query uses refreshed structured credentials, approve/reject request construction changes authentication only and does not retry writes, and Zhaoshang subprocesses receive refreshed environment values before launch.
- [ ] Run each focused test file and confirm failures reflect missing integration.
- [ ] Wire authentication immediately after cURL parsing and before request creation/subprocess launch, preserving all business data.
- [ ] Run the focused first-, second-, and third-step tests.

### Task 4: Documentation And Verification

**Files:**
- Modify: `TESTING.md`
- Modify: the affected runbook sections only if their runtime authentication instructions changed.

- [ ] Document the ignored state path, bootstrap rule, safe retry boundary, and offline/real verification commands without credentials.
- [ ] Run syntax compilation and the repository test commands selected by `TESTING.md`.
- [ ] Verify the `.curl` hashes are unchanged before any network activity.
- [ ] Perform the minimum real SaaS and Zhaoshang refresh sequence, save rotated state, test whether each immediately previous refresh token remains valid without printing it, and stop if strict single-use rotation could disrupt the shared browser session.
- [ ] Call only safe read-only SaaS and Zhaoshang queries using state-backed credentials; do not call `/add`, approve, reject, submit, or push.
- [ ] Re-run `.curl` hashes, sensitive-diff checks, and focused tests, then report exact evidence and any remaining browser-session risk.
