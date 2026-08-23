"""Closeout must spend its full remaining budget on transient failures.

Retryable failures (snapshot fetch exhaustion, timeouts, connection errors,
discovery incompleteness, pagination drift) retry with a growing backoff
until the long-operation deadline instead of exiting after two passes.
Permanent failures (configuration, durable-state and identity conflicts)
still fail fast, and unknown failures keep the historical bounded stop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_closeout import (
    CloseoutConfig,
    _closeout_failure_class,
    _failed_pass_backoff_seconds,
    run_closeout,
)
from app.services.cnnc_saas_snapshot_adapter import (
    SnapshotConsistencyDrift,
    SnapshotFetchExhausted,
)
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
    TickSummary,
)
from app.services.incremental_audit_runtime import RuntimeConfigurationError


BUSINESS_DATE = "2026-08-11"
CUTOFF = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)


class VirtualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


def _row(v: str, sku: str) -> dict[str, Any]:
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": "",
        "fourCategoryId": "cat",
        "list_reference_price": "10",
        "queryStr": f"title-{v}",
        "skuName": "title",
    }


def _observation(rows) -> DiscoveredState:
    return DiscoveredState(
        observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
        headers=["vCommodityId"],
        rows=list(rows),
        source_fingerprints=[f"source-{row['vCommodityId']}" for row in rows],
        customer_fingerprints=[f"customer-{row['vCommodityId']}" for row in rows],
        evidence={"saas_count": len(rows), "customer_count": len(rows)},
    )


class _Harness:
    def __init__(self, tmp_path: Path, discover) -> None:
        self.clock = VirtualClock()
        self.store = DailyAuditStore(tmp_path / "state.sqlite3")
        self.lock = ProcessLock(tmp_path / "closeout.lock")
        self.materialize_calls: list[str] = []
        self.deps = TickDependencies(
            discover=discover,
            detail=lambda _row_value, _timeout: {"customerPrice": "10"},
            model_batch=lambda batch, _timeout: {
                index: {"match_result": "SAME"} for index, _row in enumerate(batch)
            },
            harvest=lambda _jobs, _timeout: {},
            wall_clock=lambda: datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
            monotonic=self.clock.monotonic,
        )

    def run(self, **closeout_overrides):
        config = TickConfig(
            business_date=BUSINESS_DATE,
            runtime_budget_seconds=120,
            heavy_work_reserve_seconds=5,
            lease_ttl_seconds=90,
            cutoff_at=CUTOFF,
            source_stability_seconds=0.0,
        )
        closeout = CloseoutConfig(
            wall_budget_seconds=250,
            source_observation_interval_seconds=10,
            remote_sweep_interval_seconds=10,
            final_materialize_reserve_seconds=30,
            **closeout_overrides,
        )

        def materialize(store, lock, business_date, output_path):
            self.materialize_calls.append(business_date)
            return object()

        return run_closeout(
            self.store,
            self.lock,
            config,
            self.deps,
            closeout,
            output_path=Path("final.xlsx"),
            materialize=materialize,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )


def test_two_exhausted_passes_then_recovery_finishes_the_closeout(tmp_path):
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        if len(calls) <= 2:
            raise SnapshotFetchExhausted("saas", 1, 3, 5.0, TimeoutError("short outage"))
        return _observation([_row("v1", "s1")])

    harness = _Harness(tmp_path, discover)
    summary, publication, blocked, blocked_error = harness.run()

    assert len(calls) == 3
    assert summary.outcome != "FAILED"
    assert summary.outcome != "DISCOVERY_INCOMPLETE"
    assert publication is not None
    assert harness.materialize_calls == [BUSINESS_DATE]
    assert blocked is None


def test_discovery_incomplete_twice_then_recovery_finishes(tmp_path):
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        if len(calls) <= 2:
            return DiscoveredState(
                observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
                headers=[],
                rows=[],
                complete=False,
            )
        return _observation([_row("v1", "s1")])

    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run()

    assert len(calls) == 3
    assert summary.outcome not in {"FAILED", "DISCOVERY_INCOMPLETE"}
    assert publication is not None


def test_runtime_configuration_error_fails_fast_on_first_pass(tmp_path):
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise RuntimeConfigurationError("title-match model configuration is incomplete")

    harness = _Harness(tmp_path, discover)
    summary, publication, blocked, blocked_error = harness.run()

    assert len(calls) == 1, "a permanent failure must not consume a second pass"
    assert summary.outcome == "FAILED"
    assert publication is None


def test_retryable_failures_use_the_full_remaining_budget(tmp_path):
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise SnapshotFetchExhausted("saas", 1, 3, 5.0, TimeoutError("still down"))

    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run()

    # wall budget 250s, reserve 30s: passes run at t=0,30,90,210 and the next
    # would start past the long-operation deadline (220s).
    assert len(calls) == 4
    assert summary.outcome == "CLOSEOUT_DEADLINE"
    assert publication is None
    assert harness.clock.sleeps[:4] == [30.0, 60.0, 120.0, 300.0]


def test_unknown_failure_keeps_the_historical_bounded_stop(tmp_path):
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise ValueError("unexpected engine defect")

    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run()

    assert len(calls) == 2
    assert summary.outcome == "FAILED"
    assert publication is None


def test_failure_classifier_covers_the_documented_vocabulary():
    def _summary(outcome, error):
        return TickSummary(outcome, (), {}, {}, None, error)

    retryable = [
        "SnapshotFetchExhausted",
        "SnapshotConsistencyDrift",
        "TimeoutError",
        "ConnectionError",
        "URLError",
        "InterfaceError",
        "HTTPError(status=408)",
        "HTTPError(status=429)",
        "HTTPError(status=503)",
    ]
    permanent = [
        "RuntimeConfigurationError",
        "RemoteIntentConflict",
        "FrozenSnapshotConflict",
        "ArtifactConflictError",
        "MaterializationValidationError",
        "HTTPError(status=400)",
    ]
    for error in retryable:
        assert _closeout_failure_class(_summary("FAILED", error)) == "retryable", error
    for error in permanent:
        assert _closeout_failure_class(_summary("FAILED", error)) == "permanent", error
    assert _closeout_failure_class(_summary("DISCOVERY_INCOMPLETE", None)) == "retryable"
    assert _closeout_failure_class(_summary("FAILED", "ValueError")) == "unknown"
    assert _closeout_failure_class(_summary("FAILED", "OperationalError")) == "unknown"


def test_failed_pass_backoff_schedule_is_capped():
    closeout = CloseoutConfig(
        wall_budget_seconds=600,
        failed_pass_retry_seconds=30.0,
    )
    schedule = [
        _failed_pass_backoff_seconds(closeout, attempt) for attempt in range(1, 7)
    ]
    assert schedule == [30.0, 60.0, 120.0, 300.0, 300.0, 300.0]


def test_snapshot_consistency_drift_is_part_of_the_workflow_surface():
    # The typed drift error the snapshot adapter raises must be classified as
    # retryable by the closeout so pagination drift cannot end the day early.
    summary = TickSummary("FAILED", (), {}, {}, None, "SnapshotConsistencyDrift")
    assert _closeout_failure_class(summary) == "retryable"
    drift = SnapshotConsistencyDrift("saas", "count changed during pagination")
    assert drift.side == "saas"
