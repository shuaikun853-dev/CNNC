"""Durable business-retry contracts for the DETAIL stage (plan §4, matrix B).

A retryable customer-price business failure (NOT_FOUND / DETAIL_REQUEST_FAILED
/ DETAIL_PARSE_FAILED) must enter the durable retry budget with its structured
error prefix intact, must go terminal once the attempt budget is exhausted, and
the exhausted rows must classify as MISSING / FAILED for the finalize gates.
Deterministic outcomes (FOUND, EXISTS, LOOKUP_FAILED, DETAIL_REQUEST_SKIPPED)
stay plain successes, and legacy invalid-success rows are requeued exactly
once by the next tick.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from app.services.customer_price_semantics import (
    DetailBusinessRetryRequired,
    DetailRequestError,
    encode_business_error,
    is_retryable_detail_http_status,
)
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_runtime import RuntimeConfigurationError
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
)
from app.workflows.incremental_price_audit_finalize import build_frozen_audit_artifact

DATE = "2026-08-11"


def _row(v="doc-1", sku="sku-1", **extra):
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": "",
        "fourCategoryId": "cat",
        "list_reference_price": "10",
        "queryStr": "title",
        "skuName": "title",
        **extra,
    }


def _config(**overrides):
    defaults = dict(
        business_date=DATE,
        runtime_budget_seconds=60,
        heavy_work_reserve_seconds=5,
        lease_ttl_seconds=30,
        detail_max_attempts=3,
    )
    defaults.update(overrides)
    return TickConfig(**defaults)


def _deps(rows, detail):
    def harvest(jobs, timeout):
        return {}

    def discover(timeout):
        return DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=rows,
            source_fingerprints=[f"source-{row['vCommodityId']}" for row in rows],
            customer_fingerprints=[f"customer-{row['fourCategoryId']}" for row in rows],
            evidence={"saas_count": len(rows), "customer_count": len(rows)},
        )

    def model(row, timeout):
        return {"match_result": "SAME", "confidence": 0.95, "model_match": "exact", "reason": "一致"}

    return TickDependencies(
        discover=discover,
        harvest=harvest,
        detail=detail,
        model=model,
        monotonic=lambda: 0.0,
        wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
    )


def _raising_detail(status: str):
    def detail(row, timeout):
        raise DetailBusinessRetryRequired({
            "customerPrice": None,
            "customerPriceStatus": status,
            "customerPriceSource": "",
            "customerPriceWarning": "详情接口未返回有效参考价" if status == "NOT_FOUND" else f"详情接口业务失败：{status}",
            "customerPriceEvidence": "",
        })

    return detail


def _detail_state(store, token):
    return store.list_stage_work(token, stage="DETAIL")[0]


def _freeze_with_model_success(store):
    """Freeze the latest snapshot and complete MODEL so finalize can judge rows."""
    token = store.acquire_lease(DATE, "freeze", 60)
    snapshot = store.latest_snapshot(DATE)
    store.freeze_snapshot(token, snapshot["id"])
    for item in store.ordered_snapshot_items(snapshot["id"]):
        model = store.ensure_stage_work(token, item["id"], "MODEL", f"model-{item['id']}")
        store.claim_stage_work(token, model["id"], max_attempts=1, max_elapsed_seconds=300)
        store.complete_stage_work(token, model["id"], {
            "match_result": "SAME", "confidence": 0.95, "model_match": "exact", "reason": "一致",
        })
    store.release_lease(token)


def test_not_found_retry_exhausts_to_terminal_and_finalize_sees_missing(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    lock = ProcessLock(tmp_path / "tick.lock")
    deps = _deps([_row()], _raising_detail("NOT_FOUND"))
    config = _config()

    first = run_incremental_tick(store, lock, config, deps)
    second = run_incremental_tick(store, lock, config, deps)
    third = run_incremental_tick(store, lock, config, deps)

    token = store.acquire_lease(DATE, "inspect", 30)
    work = _detail_state(store, token)
    store.release_lease(token)
    assert first.backlog["detail_terminal"] == 0
    assert second.backlog["detail_terminal"] == 0
    assert third.backlog["detail_terminal"] == 1
    assert work["state"] == "TERMINAL_FAILED"
    assert work["attempts"] == 3
    assert work["error_text"].startswith("customerPriceStatus=NOT_FOUND；")
    assert third.outcome == "MANUAL_REVIEW_REQUIRED"
    assert third.backlog["customer_price_missing"] == 1

    _freeze_with_model_success(store)
    token = store.acquire_lease(DATE, "finalize", 60)
    built = build_frozen_audit_artifact(store, token, output_identity="daily-final-20260811")
    store.release_lease(token)
    assert built.readiness.customer_price_missing == 1
    assert built.readiness.customer_price_failed == 0
    assert built.readiness.ready is False
    assert built.artifact is None


def test_request_failed_retry_exhausts_to_terminal_failed(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    lock = ProcessLock(tmp_path / "tick.lock")
    deps = _deps([_row()], _raising_detail("DETAIL_REQUEST_FAILED"))
    config = _config()

    for _ in range(3):
        summary = run_incremental_tick(store, lock, config, deps)

    token = store.acquire_lease(DATE, "inspect", 30)
    work = _detail_state(store, token)
    store.release_lease(token)
    assert work["state"] == "TERMINAL_FAILED"
    assert work["error_text"].startswith("customerPriceStatus=DETAIL_REQUEST_FAILED；")
    assert summary.outcome == "MANUAL_REVIEW_REQUIRED"
    assert summary.backlog["customer_price_failed"] == 1

    _freeze_with_model_success(store)
    token = store.acquire_lease(DATE, "finalize", 60)
    built = build_frozen_audit_artifact(store, token, output_identity="daily-final-20260811")
    store.release_lease(token)
    assert built.readiness.customer_price_failed == 1
    assert built.readiness.customer_price_missing == 0
    assert built.readiness.ready is False


def test_found_still_completes_success_without_retry(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    lock = ProcessLock(tmp_path / "tick.lock")
    calls = []

    def detail(row, timeout):
        calls.append(row["vCommodityId"])
        return {
            "customerPrice": "10",
            "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "",
            "customerPriceEvidence": "参考价1=10",
        }

    deps = _deps([_row()], detail)
    first = run_incremental_tick(store, lock, _config(), deps)
    second = run_incremental_tick(store, lock, _config(), deps)

    token = store.acquire_lease(DATE, "inspect", 30)
    work = _detail_state(store, token)
    store.release_lease(token)
    assert work["state"] == "SUCCESS"
    assert json.loads(work["result_json"])["customerPriceStatus"] == "FOUND"
    assert first.counters["detail_requests"] == 1
    assert second.counters["detail_requests"] == 0
    assert len(calls) == 1
    assert first.backlog["customer_price_missing"] == 0
    assert first.backlog["customer_price_failed"] == 0


def test_lookup_failed_is_deterministic_success_and_never_retried(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    lock = ProcessLock(tmp_path / "tick.lock")
    calls = []

    def detail(row, timeout):
        calls.append(row["vCommodityId"])
        return {
            "customerPrice": None,
            "customerPriceStatus": "LOOKUP_FAILED",
            "customerPriceSource": "",
            "customerPriceWarning": "customer export did not contain this documentId",
            "customerPriceEvidence": "",
        }

    deps = _deps([_row()], detail)
    first = run_incremental_tick(store, lock, _config(), deps)
    second = run_incremental_tick(store, lock, _config(), deps)

    token = store.acquire_lease(DATE, "inspect", 30)
    work = _detail_state(store, token)
    store.release_lease(token)
    assert work["state"] == "SUCCESS"
    assert second.counters["detail_requests"] == 0
    assert len(calls) == 1
    assert first.outcome != "MANUAL_REVIEW_REQUIRED"


def test_runtime_configuration_error_goes_terminal_on_first_attempt(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    lock = ProcessLock(tmp_path / "tick.lock")
    calls = []

    def detail(row, timeout):
        calls.append(row["vCommodityId"])
        raise RuntimeConfigurationError("detail template is broken")

    deps = _deps([_row()], detail)
    summary = run_incremental_tick(store, lock, _config(), deps)

    token = store.acquire_lease(DATE, "inspect", 30)
    work = _detail_state(store, token)
    store.release_lease(token)
    assert len(calls) == 1
    assert work["state"] == "TERMINAL_FAILED"
    assert work["attempts"] == 1
    assert summary.backlog["customer_price_failed"] == 1


def test_retryable_detail_failure_recovers_on_next_tick_and_process_restart(tmp_path):
    """Cross-tick durable replay: a fresh process/store object reclaims the row."""
    store = DailyAuditStore(tmp_path / "state.sqlite")
    lock = ProcessLock(tmp_path / "tick.lock")
    calls = []

    def flaky_detail(row, timeout):
        calls.append(row["vCommodityId"])
        if len(calls) == 1:
            raise DetailBusinessRetryRequired({
                "customerPrice": None,
                "customerPriceStatus": "NOT_FOUND",
                "customerPriceSource": "",
                "customerPriceWarning": "详情接口未返回有效参考价",
                "customerPriceEvidence": "",
            })
        return {
            "customerPrice": "10",
            "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "",
            "customerPriceEvidence": "参考价1=10",
        }

    deps = _deps([_row()], flaky_detail)
    first = run_incremental_tick(store, lock, _config(), deps)

    token = store.acquire_lease(DATE, "inspect", 30)
    work = _detail_state(store, token)
    store.release_lease(token)
    assert work["state"] == "RETRYABLE_FAILED"
    assert first.outcome == "PENDING_RETRY"

    restarted_store = DailyAuditStore(tmp_path / "state.sqlite")
    second = run_incremental_tick(restarted_store, ProcessLock(tmp_path / "tick2.lock"), _config(), deps)

    token = restarted_store.acquire_lease(DATE, "inspect-2", 30)
    work = _detail_state(restarted_store, token)
    store.release_lease(token)
    assert len(calls) == 2
    assert work["state"] == "SUCCESS"
    assert second.backlog["detail_terminal"] == 0


def test_legacy_invalid_success_is_requeued_once_and_then_succeeds(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    lock = ProcessLock(tmp_path / "tick.lock")
    bad = _row("doc-bad", "sku-bad")
    good = _row("doc-good", "sku-good")
    good_calls = {"count": 0}

    def detail(row, timeout):
        if row["vCommodityId"] == "doc-good":
            good_calls["count"] += 1
            if good_calls["count"] == 1:
                # First pass reproduces the legacy durable shape the old worker
                # checkpointed as SUCCESS; after the requeue the retry finds a
                # price like the recovered API would.
                return {
                    "customerPrice": None,
                    "customerPriceStatus": "NOT_FOUND",
                    "customerPriceSource": "",
                    "customerPriceWarning": "详情接口未返回有效参考价",
                    "customerPriceEvidence": "",
                }
        return {
            "customerPrice": "10",
            "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "",
            "customerPriceEvidence": "参考价1=10",
        }

    deps = _deps([bad, good], detail)
    run_incremental_tick(store, lock, _config(), deps)

    token = store.acquire_lease(DATE, "inspect-legacy", 30)
    legacy = next(
        work for work in store.list_stage_work(token, stage="DETAIL")
        if work["result_json"] and "NOT_FOUND" in work["result_json"]
    )
    store.release_lease(token)
    assert legacy["state"] == "SUCCESS"

    second = run_incremental_tick(store, lock, _config(), deps)
    assert second.counters["detail_business_requeued"] == 1

    # The requeued row was retried and completed within the same tick; the
    # healthy FOUND row was never touched.
    token = store.acquire_lease(DATE, "inspect", 30)
    works = store.list_stage_work(token, stage="DETAIL")
    assert len(works) == 2
    assert all(work["state"] == "SUCCESS" for work in works)
    assert all(
        json.loads(work["result_json"])["customerPriceStatus"] == "FOUND"
        for work in works
    )
    store.release_lease(token)

    # Idempotent: nothing invalid remains, so a later tick requeues nothing.
    third = run_incremental_tick(store, lock, _config(), deps)
    assert third.counters["detail_business_requeued"] == 0


def test_requeue_resets_budget_and_keeps_status_evidence_in_error_text(tmp_path):
    """Store-level contract: one requeue, budgets reset, evidence verbatim."""
    store = DailyAuditStore(tmp_path / "state.sqlite")
    lock = ProcessLock(tmp_path / "tick.lock")
    deps = _deps([_row()], lambda row, timeout: {
        "customerPrice": "10",
        "customerPriceStatus": "FOUND",
        "customerPriceSource": "DETAIL_REFERENCE_INFOS",
        "customerPriceWarning": "",
        "customerPriceEvidence": "参考价1=10",
    })
    run_incremental_tick(store, lock, _config(), deps)
    snapshot_id = int(store.latest_snapshot(DATE)["id"])

    token = store.acquire_lease(DATE, "corrupt", 30)
    work = _detail_state(store, token)
    # Fingerprint change gives the item a fresh PENDING row (mirroring a
    # legacy state file), then we write the exact invalid success the old
    # worker used to checkpoint.
    work = store.ensure_stage_work(
        token, int(work["audit_item_id"]), "DETAIL", str(work["fingerprint"]) + "-legacy"
    )
    store.claim_stage_work(token, work["id"], max_attempts=3, max_elapsed_seconds=300)
    store.complete_stage_work(token, work["id"], {
        "customerPrice": None,
        "customerPriceStatus": "NOT_FOUND",
        "customerPriceSource": "",
        "customerPriceWarning": "详情接口未返回有效参考价",
        "customerPriceEvidence": "",
    })
    assert store.requeue_invalid_business_success_details(token, snapshot_id) == 1
    requeued = store.get_stage_work(int(work["id"]))
    assert requeued["state"] == "PENDING"
    assert requeued["attempts"] == 0
    assert requeued["elapsed_seconds"] == 0
    assert requeued["retryable"] == 1
    assert requeued["result_json"] is None
    assert requeued["claimed_at"] is None
    assert requeued["error_text"] == (
        "requeued invalid business success: customerPriceStatus=NOT_FOUND"
    )
    # Second call finds nothing: the row is no longer an invalid success.
    assert store.requeue_invalid_business_success_details(token, snapshot_id) == 0
    # A healthy FOUND success is never requeued.
    store.claim_stage_work(token, work["id"], max_attempts=3, max_elapsed_seconds=300)
    store.complete_stage_work(token, work["id"], {
        "customerPrice": "10",
        "customerPriceStatus": "FOUND",
        "customerPriceSource": "DETAIL_REFERENCE_INFOS",
        "customerPriceWarning": "",
        "customerPriceEvidence": "参考价1=10",
    })
    assert store.requeue_invalid_business_success_details(token, snapshot_id) == 0
    store.release_lease(token)


# ---- typed transport-failure retry classification (round 2, P1-4) ----------


@pytest.mark.parametrize(
    ("http_status", "retryable"),
    [
        (None, True),   # transport/timeout/decode: no status, transient
        (408, True),
        (429, True),
        (500, True),
        (503, True),
        (403, True),    # CNNC gateway auth/WAF rotation window
        (412, True),
        (400, False),   # deterministic request errors: terminal on first attempt
        (404, False),
    ],
)
def test_typed_detail_request_error_follows_http_semantics(
    tmp_path, http_status, retryable
):
    store = DailyAuditStore(tmp_path / f"state-{http_status}.sqlite")
    lock = ProcessLock(tmp_path / f"tick-{http_status}.lock")
    calls = []

    def detail(row, timeout):
        calls.append(row["vCommodityId"])
        # Mirror the real worker: retryable comes from the HTTP status helper.
        raise DetailRequestError(
            http_status=http_status,
            message=f"HTTP {http_status}",
            retryable=is_retryable_detail_http_status(http_status),
        )

    deps = _deps([_row()], detail)
    first = run_incremental_tick(store, lock, _config(), deps)

    token = store.acquire_lease(DATE, "inspect", 30)
    work = _detail_state(store, token)
    store.release_lease(token)
    assert len(calls) == 1
    if retryable:
        assert work["state"] == "RETRYABLE_FAILED"
        assert work["attempts"] == 1
        assert first.outcome == "PENDING_RETRY"
        # The durable replay window reclaims the row on the next tick.
        run_incremental_tick(store, lock, _config(), deps)
        token = store.acquire_lease(DATE, "inspect-2", 30)
        work = _detail_state(store, token)
        store.release_lease(token)
        assert len(calls) == 2
        assert work["attempts"] == 2
    else:
        assert work["state"] == "TERMINAL_FAILED"
        assert work["attempts"] == 1
        assert first.outcome == "MANUAL_REVIEW_REQUIRED"


def test_timeout_and_connection_errors_stay_retryable(tmp_path):
    for exc_kind in ("timeout", "connection"):
        store = DailyAuditStore(tmp_path / f"state-{exc_kind}.sqlite")
        lock = ProcessLock(tmp_path / f"tick-{exc_kind}.lock")
        calls = []

        def detail(row, timeout, _exc_kind=exc_kind):
            calls.append(row["vCommodityId"])
            raise TimeoutError("deadline") if _exc_kind == "timeout" else ConnectionError("reset")

        deps = _deps([_row()], detail)
        summary = run_incremental_tick(store, lock, _config(), deps)

        token = store.acquire_lease(DATE, "inspect", 30)
        work = _detail_state(store, token)
        store.release_lease(token)
        assert len(calls) == 1, exc_kind
        assert work["state"] == "RETRYABLE_FAILED", exc_kind
        assert summary.outcome == "PENDING_RETRY", exc_kind


class _FakeAuthProvider:
    """duck-type CnncAuthProvider: worker only needs get/refresh/get_status."""

    def __init__(self):
        self.refresh_calls = 0

    def get_access_token(self):
        return "AT-1"

    def refresh(self, *, stale_access_token=None, deadline_monotonic=None, **_kwargs):
        self.refresh_calls += 1
        return "AT-2"

    def get_status(self):
        return {"status": "REFRESHED"}


def test_401_after_refresh_retries_durably_without_third_immediate(tmp_path, monkeypatch):
    """Real worker in the tick loop: claim 1 runs refresh + exactly one retry
    (two HTTP calls); still rejected -> durable RETRYABLE_FAILED.  The next
    tick claims again; there is never a third immediate call inside a claim."""
    import app.services.incremental_audit_runtime as runtime_module

    detail_calls: list[dict] = []

    def fake_call_detail_api(config, document_id, category_id, timeout):
        detail_calls.append({"access_token": dict(config.get("headers") or ()).get("access-token")})
        return {"ok": False, "http_status": 401, "payload": None, "error": "HTTP 401"}

    monkeypatch.setattr(runtime_module, "call_detail_api", fake_call_detail_api)
    provider = _FakeAuthProvider()
    worker = runtime_module._detail_worker(
        {"url": "https://detail.invalid", "headers": [("access-token", "PLACEHOLDER")]},
        request_interval_seconds=0,
        cnnc_provider=provider,
    )

    store = DailyAuditStore(tmp_path / "state.sqlite")
    lock = ProcessLock(tmp_path / "tick.lock")
    deps = _deps([_row()], worker)

    first = run_incremental_tick(store, lock, _config(), deps)

    token = store.acquire_lease(DATE, "inspect", 30)
    work = _detail_state(store, token)
    store.release_lease(token)
    # First claim: refresh once, one immediate retry, no third call.
    assert len(detail_calls) == 2
    assert detail_calls[0]["access_token"] == "AT-1"
    assert detail_calls[1]["access_token"] == "AT-2"
    assert provider.refresh_calls == 1
    assert work["state"] == "RETRYABLE_FAILED"
    assert work["attempts"] == 1
    assert first.outcome == "PENDING_RETRY"

    # Second tick: durable retry re-claims; again exactly two HTTP calls.
    run_incremental_tick(store, lock, _config(), deps)
    token = store.acquire_lease(DATE, "inspect-2", 30)
    work = _detail_state(store, token)
    store.release_lease(token)
    assert len(detail_calls) == 4
    assert work["attempts"] == 2
    assert work["state"] == "RETRYABLE_FAILED"
