# Open-source readiness: safe offline demonstration

## Scope

The repository contains both reusable computation code and environment-specific operational integration. **The complete production application is not portable**, and publishing a repository is not proof that all code, data, or integrations are licensed for third-party use.

### Safe starting point

`examples/offline_price_audit.py` imports existing pure decision functions from `app/services/saas_audit_core.py`. It reads local JSON, evaluates synthetic prices, and prints an advisory-only JSON result. It does not connect to external services, invoke AI models, run SQL, modify files, approve records, or submit tasks.

Run from the repository root (Python 3.11+):

```bash
python -m pip install -r requirements.txt
python -m examples.offline_price_audit
python -m pytest tests/test_offline_price_audit.py -q
```

To supply different **non-sensitive** local data:

```bash
python -m examples.offline_price_audit --input examples/fixtures/sample_prices.json
```

`PASS` is a **demonstration classification**, never permission to approve, publish, push, or change production records. When evidence is incomplete, the demo returns `REVIEW_REQUIRED`.

## Release gates (not yet satisfied)

- [ ] Confirm repository and third-party code/data ownership and permission to publish.
- [ ] Review current files, **every branch, tags, pull requests, Actions artifacts, and full Git history** for sensitive information and client-specific details.
- [ ] Review GitHub secret-scanning alerts; rotate or revoke any exposed credential before history cleanup.
- [ ] Remove or replace client-specific URLs, IP addresses, IDs, screenshots, and operational instructions from a *separately authorized* community distribution.
- [ ] Choose an appropriate license **only after** confirming redistribution rights.
- [ ] Publish a synthetic API adapter and contract tests, then isolate environment-specific adapters behind interfaces.
- [ ] Run the full existing test suite and verify existing production deployment paths remain unchanged.
- [ ] Independently audit each release for confidential or personal data.

**Do not merge or deploy this demo as a claim that the full repository is safe to release.**
