# CNNC SSO Autofill Hardening — Design / Review Contract

- Date: 2026-09-10
- Base branch: `prod/closeout-20260818`
- Base SHA: `a1baf01a72620e4260e465a02b4e251f7d2c74ce`
- Review branch: `review/sso-autofill-20260910`
- Scope: CNNC authentication browser and Dashboard recovery UX only
- Status: review contract; implementation must be isolated from unrelated dirty-worktree changes

## 1. Goal

When the controlled CNNC SSO login browser is opened after an explicit human re-login action, automatically fill the configured username and password so the human only needs to complete the site's required interactive verification (for example slider / SMS / MFA).

The feature must **not** submit the login form, bypass verification, solve a captcha, impersonate a human interaction, or remove the requirement for explicit human completion of authentication.

## 2. Existing behaviour that must remain unchanged

The implementation must preserve the existing `scripts/cnnc_auth_browser.py login` contract unless this document explicitly changes it:

- one-time temporary Chrome profile
- CDP cleanup and process cleanup
- TGC remains the success signal for completed human login
- only the SSO session-cookie whitelist is exported to `data/cnnc_auth_storage_state.json`
- storage-state / TGC / harvest / OC→AT semantics are unchanged
- login timeout and existing exit-code meanings remain unchanged
- no full Cookie, token, username, or password is printed to stdout/stderr/logs

## 3. Credential handling

The current global rule in `AGENTS.md` forbids persisting real usernames and passwords. Therefore an implementation that stores plaintext credentials in `data/cnnc_login_credentials.json` or any other repo/runtime file is **not acceptable**.

Accepted designs:

1. **Ephemeral source**: credentials are supplied to the login process through non-persisted process environment / injected runtime dependency and never written to disk; or
2. **OS-protected source**: Windows Credential Manager / DPAPI-backed storage scoped to the current Windows user. If this option is chosen, `AGENTS.md` must first be updated with a narrow explicit exception defining that protected store as the only permitted persistent login-secret location. Repo files, `data/*.json`, `.env`, logs and test fixtures still must never contain the real password.

Credential absence, disabled configuration, read failure, or malformed configuration must fall back to the original manual-login behaviour without preventing the browser from opening.

A credential rotation / clear path must exist if an OS-protected persistent source is used.

## 4. Trusted-origin gate

Autofill must be denied by default.

Before reading or filling any candidate input in a page/frame:

- the frame URL must be parseable
- scheme must be `https`
- origin must exactly match an explicit CNNC SSO/login allowlist derived from an observed real login flow
- `about:blank`, `data:`, third-party origins and unknown subdomains must never receive credentials

The allowlist must be a small constant or configuration owned by the backend module; it must not be supplied by the page or browser DOM.

If the real login form is inside an iframe, record only the redacted frame URL/origin during the human E2E and add the minimum exact origin required. Do not broaden to suffix matching merely to make a test pass.

## 5. Field-selection and fill safety

Autofill may act only on visible, enabled, editable inputs and must preserve human input.

Required rules:

- password target requires a visible editable `type=password` field on a trusted origin
- username target uses conservative observed attributes/labels from the real page; generic fallback must exclude captcha/SMS/OTP/code/search/verification fields
- only fill a field whose current value is empty
- immediately before fill, re-resolve/re-check trusted origin, element identity and empty value
- never overwrite a value entered by the human or browser password manager
- never click submit/login/next, press Enter, trigger a verification action, or interact with captcha/slider/SMS/MFA controls
- repeated polling must be idempotent: once a credential has been successfully filled, later ticks must not keep rewriting it
- errors in the autofill helper are bounded WARN-only and must not abort the existing login wait loop
- logs may identify safe field metadata/origin but never credential values

## 6. Module / seam design

Do not add another large responsibility cluster directly to `cnnc_auth_browser.py`.

Preferred deep module:

`scripts/cnnc_login_autofill.py`

Expose a small interface that lets `cmd_login()` perform one bounded autofill tick or create a small controller. Hide credential-source handling, trusted-origin checks, candidate selection, idempotence and fill decisions behind that interface.

`cnnc_auth_browser.py` remains the owner of Chrome/CDP lifecycle, TGC polling, storage-state export and exit codes.

Tests should exercise the autofill module through its declared interface rather than locking onto many private helper functions.

## 7. Feedback loops and tests

Follow `diagnosing-bugs` + `tdd`: establish red-capable behaviour checks before changing the implementation.

Required automated seams:

1. **Credential-source seam**
   - absent/disabled/error -> manual fallback
   - no real secret in fixtures/log output
   - OS-protected source, if used, is tested through a fake adapter and has a clear/rotation path
2. **Autofill module seam**
   - trusted username + password fields are filled once
   - human pre-filled values are preserved
   - unknown/third-party frame is never filled
   - captcha/SMS/OTP/code fields are never selected
   - DOM replacement / signature drift causes skip, not blind fill
   - repeated ticks are idempotent
   - autofill exception does not terminate login wait
3. **Existing login regression seam**
   - TGC detection, storage-state export, timeout and cleanup behaviour still pass existing tests
4. **Dashboard recovery UX**
   - when `login_required=true`, the CNNC auth `<details>` section opens automatically and the re-login control is visible

Run at minimum the new autofill tests plus `tests/test_cnnc_auth_browser_harvest.py`, relevant CNNC auth provider/recovery tests, Dashboard JS tests, and any context-document tests touched by rule/document changes.

## 8. Human E2E gate

Before merge/deploy, run one controlled human login E2E on the real CNNC SSO page with secrets redacted from evidence.

Acceptance sequence:

1. user explicitly triggers re-login
2. browser opens normally
3. username/password are auto-filled on trusted CNNC origin only
4. no submit/next/captcha/MFA action occurs automatically
5. human completes the required verification
6. TGC is observed
7. storage state is exported using the existing whitelist
8. login-and-recover completes provider refresh/verify/publish
9. Dashboard reports the recovered auth state

Capture only timestamps, status names, trusted origins and redacted field metadata. Do not capture credential values or authenticated request headers.

## 9. Incident follow-up

The 2026-09-10 incident established that SaaS snapshot transport failure can occur before the customer/CNNC auth path, leaving the persisted auth state looking healthy even when it is stale.

Do not turn `get_status()` into an uncontrolled network probe. Prefer explicit stale-state projection based on existing `last_verify_at` / refresh evidence, and keep any active `--ensure` operation user-triggered or otherwise separately designed and reviewed.

At minimum, the Dashboard must not hide a known `SSO_LOGIN_REQUIRED` recovery control inside a closed `<details>` block.

## 10. Git / review gate

The implementation branch must contain only this feature/fix and directly required tests/docs. Unrelated pre-existing local modifications must not be committed.

Formal review fixed point:

```text
a1baf01a72620e4260e465a02b4e251f7d2c74ce
```

Before review:

```powershell
git rev-parse HEAD
git diff --check a1baf01a72620e4260e465a02b4e251f7d2c74ce...HEAD
git log a1baf01a72620e4260e465a02b4e251f7d2c74ce..HEAD --oneline
git diff --name-status a1baf01a72620e4260e465a02b4e251f7d2c74ce...HEAD
```

Final merge gate is a Matt Pocock `code-review` two-axis review:

- **Standards**: CNNC `AGENTS.md`, `TESTING.md`, relevant runbook/ADR plus Fowler smell baseline
- **Spec**: this document

No production merge/deploy is implied by passing review; final merge/deploy remains a separate user decision.
