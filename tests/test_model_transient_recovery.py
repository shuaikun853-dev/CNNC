"""Durable recovery contracts for transient MODEL failures.

A short provider outage (transport error or temporarily invalid response
contract) must stay RETRYABLE_FAILED across durable attempts instead of
terminating after one replay, and retries must be spaced by a durable
backoff so a busy scheduler cannot burn the whole budget in minutes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib import error

import pytest

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.title_match_model_adapter import (
    TITLE_MATCH_PROMPT_VERSION,
    TITLE_MATCH_SCHEMA_VERSION,
    ModelCallRetryExhaustedError,
    ModelResponseContractError,
)
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
)


@dataclass
class Clock:
    value: float = 0.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass
class WallClock:
    """Real-now based clock so durable ``updated_at`` backoff lines up."""

    offset_seconds: float = 0.0

    def now(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.offset_seconds)

    def advance(self, seconds: float) -> None:
        self.offset_seconds += seconds


def _row(v="doc-1", sku="sku-1"):
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": " https://shop.example/item ",
        "fourCategoryId": "cat",
        "list_reference_price": "10",
        "queryStr": "title",
        "skuName": "title",
        "mall_key": "mall-a",
    }


def _deps(wall: WallClock, rows):
    def discover(_timeout):
        return DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=rows,
            source_fingerprints=[f"source-{row['vCommodityId']}" for row in rows],
            customer_fingerprints=[f"customer-{row['vCommodityId']}" for row in rows],
            evidence={"saas_count": len(rows), "customer_count": len(rows)},
        )

    def detail(_row, _timeout):
        return {"customerPrice": "10"}

    return TickDependencies(
        discover=discover,
        detail=detail,
        monotonic=lambda: 0.0,
        wall_clock=wall.now,
    )


def _config():
    # Production defaults only: the whole point of these contracts is that the
    # default durable budget keeps transient failures recoverable.
    return TickConfig(business_date="2026-08-11")


def _model_state(store: DailyAuditStore) -> dict:
    token = store.acquire_lease("2026-08-11", "inspect", 30)
    try:
        return dict(store.list_stage_work(token, stage="MODEL")[0])
    finally:
        store.release_lease(token)


def _run(store, tmp_path, deps):
    return run_incremental_tick(
        store, ProcessLock(tmp_path / f"tick-{time.time_ns()}.lock"), _config(), deps
    )


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("model timeout"),
        error.HTTPError("https://model.invalid", 429, "rate limited", {}, None),
        error.HTTPError("https://model.invalid", 503, "unavailable", {}, None),
        ModelResponseContractError("model response is not valid JSON"),
        ModelCallRetryExhaustedError(
            "batch model call", 3, 120.0, ModelResponseContractError("missing results")
        ),
        ModelCallRetryExhaustedError(
            "batch model call", 3, 240.0, TimeoutError("model timeout")
        ),
    ],
    ids=("timeout", "http-429", "http-503", "contract", "exhausted-contract", "exhausted-timeout"),
)
def test_transient_model_failure_two_durable_attempts_stay_retryable(tmp_path, failure):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    wall = WallClock()
    calls = 0
    deps = _deps(wall, [_row()])

    def model_batch(_batch, _timeout):
        nonlocal calls
        calls += 1
        raise failure

    deps.model_batch = model_batch

    _run(store, tmp_path, deps)
    wall.advance(301)  # past the largest backoff step
    _run(store, tmp_path, deps)

    model = _model_state(store)
    assert model["state"] == "RETRYABLE_FAILED"
    assert model["attempts"] == 2
    assert calls == 2


def test_transient_model_failure_recovers_on_later_tick(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    wall = WallClock()
    calls = 0
    deps = _deps(wall, [_row()])

    def model_batch(_batch, _timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("short outage")
        return {0: {"match_result": "SAME"}}

    deps.model_batch = model_batch

    first = _run(store, tmp_path, deps)
    assert first.outcome == "PENDING_RETRY"
    wall.advance(61)
    second = _run(store, tmp_path, deps)

    model = _model_state(store)
    assert model["state"] == "SUCCESS"
    assert model["attempts"] == 2
    assert calls == 2
    assert second.outcome == "COMPLETED"


@pytest.mark.parametrize(
    "failure",
    [
        error.HTTPError("https://model.invalid", 400, "bad request", {}, None),
        RuntimeError("deterministic configuration error"),
    ],
    ids=("http-400", "runtime-configuration"),
)
def test_permanent_model_failure_stays_terminal(tmp_path, failure):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    wall = WallClock()
    calls = 0
    deps = _deps(wall, [_row()])

    def model_batch(_batch, _timeout):
        nonlocal calls
        calls += 1
        raise failure

    deps.model_batch = model_batch

    first = _run(store, tmp_path, deps)
    wall.advance(301)
    second = _run(store, tmp_path, deps)
    model = _model_state(store)

    assert first.outcome == "MANUAL_REVIEW_REQUIRED"
    assert second.outcome == "MANUAL_REVIEW_REQUIRED"
    assert model["state"] == "TERMINAL_FAILED"
    assert model["attempts"] == 1
    assert calls == 1


def test_backoff_window_blocks_reclaim_until_due(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    wall = WallClock()
    calls = 0
    deps = _deps(wall, [_row()])

    def model_batch(_batch, _timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("short outage")
        return {0: {"match_result": "SAME"}}

    deps.model_batch = model_batch

    _run(store, tmp_path, deps)
    assert _model_state(store)["attempts"] == 1

    wall.advance(30)  # inside the first 60s backoff window
    _run(store, tmp_path, deps)
    assert calls == 1, "backoff window must block a durable re-claim"
    assert _model_state(store)["attempts"] == 1

    wall.advance(31)  # 61s after the failure: the first window has expired
    _run(store, tmp_path, deps)
    assert calls == 2
    assert _model_state(store)["state"] == "SUCCESS"


def test_backoff_and_attempts_survive_process_restart(tmp_path):
    state_path = tmp_path / "state.sqlite"
    store = DailyAuditStore(state_path)
    wall = WallClock()
    calls = 0
    deps = _deps(wall, [_row()])

    def model_batch(_batch, _timeout):
        nonlocal calls
        calls += 1
        raise TimeoutError("short outage")

    deps.model_batch = model_batch
    _run(store, tmp_path, deps)
    store.close() if hasattr(store, "close") else None

    # A brand-new process reopens the same durable state.
    reopened = DailyAuditStore(state_path)
    wall.advance(30)
    _run(reopened, tmp_path, deps)
    assert calls == 1
    assert _model_state(reopened)["attempts"] == 1

    wall.advance(31)
    _run(reopened, tmp_path, deps)
    assert calls == 2
    assert _model_state(reopened)["attempts"] == 2


def test_model_contract_and_fingerprint_semantics_are_unchanged():
    # The business prompt, result schema and successful parse outputs must not
    # change while the retry classification is widened.  The prompt pin moved
    # on 2026-08-28 (SAME hard-rule double-insurance tightening) and again in
    # review round 1 (P2-1: conditional key-field phrasing) — exactly the
    # content-addressed cache-invalidation this assertion guards.
    assert TITLE_MATCH_PROMPT_VERSION == "sha256:023ffa5c2130f900383a215fbe4ab7ae2f7c3fd3119657a1ba25bbd8c039853e"
    assert TITLE_MATCH_SCHEMA_VERSION == "sha256:d596ebe5295054569aa9e0d9cb18af5c6f65d46d2f0d3e1df76e03b097da6e62"

    import app.services.title_match_model_adapter as adapter

    parsed = adapter._parse_model_response(
        json.dumps({"match_result": "SAME", "confidence": 0.9, "model_match": "exact"}),
        "sku-1",
    )
    assert parsed.match_result == "SAME"
    assert parsed.confidence == 0.9
    assert parsed.model_match == "exact"

    batched = adapter._parse_batch_model_response(
        json.dumps({"results": [{"index": 0, "match_result": "NOT_SAME"}]}), 1
    )
    assert batched[0].match_result == "NOT_SAME"


@pytest.mark.parametrize(
    ("raw", "sku"),
    [
        ("not-json", "sku-1"),
        (json.dumps({"choices": [{"message": {"content": "also not json"}}]}), "sku-1"),
        (json.dumps({"choices": []}), "sku-1"),
    ],
    ids=("raw-body", "choices-content", "empty-choices"),
)
def test_single_parse_contract_violations_raise_typed_error(raw, sku):
    import app.services.title_match_model_adapter as adapter

    with pytest.raises(ModelResponseContractError):
        adapter._parse_model_response(raw, sku)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps({"unexpected": True}),
        json.dumps({"choices": [{"message": {"content": "{\"rows\": []}"}}]}),
    ],
    ids=("raw-body", "missing-results", "choices-nested-missing-results"),
)
def test_batch_parse_contract_violations_raise_typed_error(raw):
    import app.services.title_match_model_adapter as adapter

    with pytest.raises(ModelResponseContractError):
        adapter._parse_batch_model_response(raw, 1)


def test_batch_missing_indexes_surface_as_typed_contract_error(monkeypatch):
    import app.services.title_match_model_adapter as adapter

    monkeypatch.setenv("TITLE_MATCH_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TITLE_MATCH_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("TITLE_MATCH_MODEL_NAME", "test-model")
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS", "5")
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        adapter,
        "_post_model_payload",
        lambda _payload, timeout_seconds=None: json.dumps({"results": []}),
    )

    with pytest.raises(ModelCallRetryExhaustedError) as caught:
        adapter.call_title_match_model_batch(
            [adapter.TitleMatchModelRequest(query_str="q", sku_name="s")]
        )

    assert isinstance(caught.value.last_cause, ModelResponseContractError)


def test_adapter_internal_retry_treats_contract_error_as_retryable():
    import app.services.title_match_model_adapter as adapter

    assert adapter._retryable_model_failure(ModelResponseContractError("missing results"))
