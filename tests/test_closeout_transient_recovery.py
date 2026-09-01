"""Closeout must spend its full remaining budget on transient failures.

Retryable failures (snapshot fetch exhaustion, timeouts, connection errors,
discovery incompleteness, pagination drift) retry with a growing backoff
until the long-operation deadline instead of exiting after two passes.
Permanent failures (configuration, durable-state and identity conflicts)
still fail fast, and unknown failures keep the historical bounded stop.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.error import URLError

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_cached_finalize import run_cached_finalization
from app.services.incremental_audit_closeout import (
    CloseoutConfig,
    _closeout_failure_class,
    _failed_pass_backoff_seconds,
    run_closeout,
)
from app.services.cnnc_saas_snapshot_adapter import (
    SnapshotAuthExpiredError,
    SnapshotConsistencyDrift,
    SnapshotFetchExhausted,
)
from app.services.incremental_audit_materializer import LegacyWorkbookMaterializer
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
    TickSummary,
)
from app.services.incremental_audit_runtime import RuntimeConfigurationError
from tests.test_incremental_audit_cached_finalize import _seed_full_good_day, _writer


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
        self.detail_calls: list[str] = []
        self.model_calls: list[int] = []
        self.deps = TickDependencies(
            discover=discover,
            detail=self._detail,
            model_batch=self._model_batch,
            harvest=lambda _jobs, _timeout: {},
            wall_clock=lambda: datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
            monotonic=self.clock.monotonic,
        )

    def _detail(self, row_value, _timeout):
        self.detail_calls.append(str(row_value))
        return {"customerPrice": "10"}

    def _model_batch(self, batch, _timeout):
        self.model_calls.append(len(batch))
        return {index: {"match_result": "SAME"} for index, _row in enumerate(batch)}

    def run(self, config_overrides=None, cached_finalize=None, output_path=None, **closeout_overrides):
        config = TickConfig(
            business_date=BUSINESS_DATE,
            runtime_budget_seconds=120,
            heavy_work_reserve_seconds=5,
            lease_ttl_seconds=90,
            cutoff_at=CUTOFF,
            source_stability_seconds=0.0,
        )
        if config_overrides:
            config = replace(config, **config_overrides)
        values = {
            "wall_budget_seconds": 250,
            "source_observation_interval_seconds": 10,
            "remote_sweep_interval_seconds": 10,
            "final_materialize_reserve_seconds": 30,
        }
        values.update(closeout_overrides)
        closeout = CloseoutConfig(**values)

        def materialize(store, lock, business_date, output_path):
            self.materialize_calls.append(business_date)
            return object()

        closeout_kwargs: dict[str, Any] = {}
        if cached_finalize is not None:
            closeout_kwargs["cached_finalize"] = cached_finalize
        return run_closeout(
            self.store,
            self.lock,
            config,
            self.deps,
            closeout,
            output_path=output_path if output_path is not None else Path("final.xlsx"),
            materialize=materialize,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
            **closeout_kwargs,
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
    assert harness.clock.sleeps[:4] == [30.0, 60.0, 120.0, 10.0]
    # The final wait is clamped to the deadline instead of sleeping 300s
    # past it: the process exits on its own clock, never past the wall
    # budget, without relying on a scheduler hard kill.
    assert harness.clock.now == 1000 + 220


def test_backoff_never_sleeps_past_the_long_op_deadline(tmp_path):
    """A 300s backoff with 10s left must sleep 10s, not 300s.

    wall budget 977s with a 60s reserve puts the long-operation deadline at
    917s.  With failed_pass_retry_seconds=1 and 1s observation/sweep pacing
    the backoff schedule is exactly 1/2/4/300..., so the seventh failed pass
    lands at t=907 with a scheduled 300s backoff and exactly 10s of budget
    left.
    """
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise SnapshotFetchExhausted("saas", 1, 3, 5.0, TimeoutError("still down"))

    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run(
        wall_budget_seconds=977,
        source_observation_interval_seconds=1,
        remote_sweep_interval_seconds=1,
        final_materialize_reserve_seconds=60,
        failed_pass_retry_seconds=1.0,
    )

    assert summary.outcome == "CLOSEOUT_DEADLINE"
    assert len(calls) == 7
    assert harness.clock.sleeps == [1.0, 2.0, 4.0, 300.0, 300.0, 300.0, 10.0]
    # Never slept into the final materialization reserve, never needed an
    # external kill: the loop exited exactly at its own long-op deadline.
    assert harness.clock.now == 1000 + 917
    assert harness.clock.now <= 1000 + 977 - 60


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
        # Deterministic typed snapshot-source diagnostics fail fast in strict
        # passes; only the whitelisted subset may unlock the cached fallback.
        "SnapshotAuthExpiredError",
        "SnapshotResponseCodeError",
        "SnapshotContractError",
        "SnapshotAuthExpiredError(side=customer,code=3333)",
        "SnapshotContractError(side=saas,reason=ROWS_NOT_OBJECTS)",
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


# ---------------------------------------------------------------------------
# Guarded cached degraded finalization: only the 20:35 safety closeout, only
# on a whitelisted source-unavailable error, only with budget left.
# ---------------------------------------------------------------------------


def _never_cached_finalize(*_args, **_kwargs):
    raise AssertionError("cached finalization must not run for this closeout")


def test_ordinary_closeout_never_cache_finalizes_on_auth_expired(tmp_path):
    """Test K: without allow_cached_finalize the day must still end FAILED."""
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise SnapshotAuthExpiredError("customer", "3333")

    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run(
        cached_finalize=_never_cached_finalize
    )

    assert calls == [0], "auth expiry is deterministic and must fail fast"
    assert summary.outcome == "FAILED"
    assert publication is None
    assert "cached_finalized" not in summary.phase_trace
    assert harness.store.get_run(BUSINESS_DATE)["frozen_snapshot_id"] is None


def test_safety_closeout_generic_runtime_error_stays_strict(tmp_path):
    """Test L: a non-whitelisted error never unlocks the cached fallback."""
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise RuntimeError("boom")

    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run(
        allow_cached_finalize=True,
        cached_finalize=_never_cached_finalize,
    )

    assert len(calls) == 2, "unknown failures keep the historical bounded stop"
    assert summary.outcome == "FAILED"
    assert publication is None
    assert "cached_finalized" not in summary.phase_trace
    assert harness.store.get_run(BUSINESS_DATE)["frozen_snapshot_id"] is None


def test_safety_closeout_auth_expired_caches_final(tmp_path):
    """Test M: the 2026-08-31 shape recovers through the cached fallback."""
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise SnapshotAuthExpiredError("customer", "3333")

    store, snapshots = _seed_full_good_day(tmp_path / "state.sqlite3", BUSINESS_DATE)
    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run(
        allow_cached_finalize=True,
        config_overrides={"source_stability_seconds": 600.0},
        cached_finalize=partial(
            run_cached_finalization,
            materializer=LegacyWorkbookMaterializer(writer=_writer),
        ),
        output_path=tmp_path / "final.xlsx",
    )

    assert len(calls) >= 1, "the strict pass is always attempted first"
    assert summary.outcome == "CACHED_FINALIZED"
    assert publication is not None
    assert "cached_finalized" in summary.phase_trace
    # The cached path is structurally discovery-free: no Detail/Model calls.
    assert harness.detail_calls == []
    assert harness.model_calls == []
    run = store.get_run(BUSINESS_DATE)
    assert run["state"] == "FINALIZED"
    assert int(run["frozen_snapshot_id"]) == int(snapshots[-1]["id"])
    metrics = json.loads(run["metrics_json"])
    assert metrics["finalization_mode"] == "CACHED_DEGRADED"
    assert metrics["degraded_reason_code"] == "SOURCE_AUTH_UNAVAILABLE"
    assert metrics["degraded_source_side"] == "customer"
    assert metrics["cached_snapshot_id"] == int(snapshots[-1]["id"])


def test_safety_closeout_cached_blocked_keeps_failed_outcome(tmp_path):
    """A blocked cached attempt must not rewrite the strict failure outcome."""
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise SnapshotAuthExpiredError("customer", "3333")

    def blocked_cached_finalize(*_args, **_kwargs):
        from app.services.incremental_audit_cached_finalize import CachedFinalizeBlocked

        raise CachedFinalizeBlocked("STABILITY_BELOW_THRESHOLD", {"computed": 0.0})

    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run(
        allow_cached_finalize=True,
        cached_finalize=blocked_cached_finalize,
    )

    assert calls == [0]
    assert summary.outcome == "FAILED"
    assert publication is None
    assert "cached_blocked:STABILITY_BELOW_THRESHOLD" in summary.phase_trace
    assert "cached_finalized" not in summary.phase_trace


def test_strict_recovery_prefers_strict_finalization(tmp_path):
    """Test N: a recoverable day finalizes STRICT even with the flag enabled."""
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        return _observation([_row("v1", "s1")])

    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run(
        allow_cached_finalize=True,
        cached_finalize=_never_cached_finalize,
    )

    assert len(calls) == 1
    assert summary.outcome == "COMPLETED"
    assert publication is not None
    assert summary.frozen_snapshot_id is not None
    assert harness.materialize_calls == [BUSINESS_DATE]
    run = harness.store.get_run(BUSINESS_DATE)
    assert int(run["frozen_snapshot_id"]) == summary.frozen_snapshot_id
    metrics = json.loads(run["metrics_json"] or "{}")
    assert "finalization_mode" not in metrics


def test_safety_closeout_retryable_source_outage_caches_at_deadline(tmp_path):
    """A retryable source outage exhausts the budget into the top-of-loop
    deadline instead of the failure branch, so the whitelisted fallback is
    re-offered there: the day still leaves CACHED_DEGRADED, with provenance
    SOURCE_UNAVAILABLE/unknown (SnapshotFetchExhausted carries no side=)."""
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise SnapshotFetchExhausted("saas", 1, 3, 1.0, TimeoutError("x"))

    store, snapshots = _seed_full_good_day(tmp_path / "state.sqlite3", BUSINESS_DATE)
    harness = _Harness(tmp_path, discover)
    # Budget 130s with a 120s reserve puts the long-op deadline at t=10; the
    # 5s failed-pass backoff plus observation pacing walk the clock there, so
    # the loop exits through the top-of-loop deadline with the FAILED pass as
    # `last` — and exactly the 120s materialization reserve left (>= 60s).
    summary, publication, _blocked, _error = harness.run(
        allow_cached_finalize=True,
        config_overrides={"source_stability_seconds": 600.0},
        cached_finalize=partial(
            run_cached_finalization,
            materializer=LegacyWorkbookMaterializer(writer=_writer),
        ),
        output_path=tmp_path / "final.xlsx",
        wall_budget_seconds=130,
        final_materialize_reserve_seconds=120,
        failed_pass_retry_seconds=5.0,
    )

    assert calls == [0], "the retryable pass sleeps to the deadline without a second pass"
    assert summary.outcome == "CACHED_FINALIZED"
    assert "stop:deadline" in summary.phase_trace
    assert "cached_finalized" in summary.phase_trace
    assert publication is not None
    assert harness.detail_calls == []
    assert harness.model_calls == []
    run = store.get_run(BUSINESS_DATE)
    assert run["state"] == "FINALIZED"
    assert int(run["frozen_snapshot_id"]) == int(snapshots[-1]["id"])
    metrics = json.loads(run["metrics_json"])
    assert metrics["finalization_mode"] == "CACHED_DEGRADED"
    assert metrics["degraded_reason_code"] == "SOURCE_UNAVAILABLE"
    assert metrics["degraded_source_side"] == "unknown"
    assert metrics["cached_snapshot_id"] == int(snapshots[-1]["id"])


def test_safety_closeout_waiting_deadline_exhaustion_never_caches(tmp_path):
    """A deadline reached without a failed pass keeps the strict semantics.

    The source never stabilizes (stability window 600s, single-observation
    stability 0), so the passes end in a waiting outcome, never in
    FAILED/DISCOVERY_INCOMPLETE — the deadline must return CLOSEOUT_DEADLINE
    without any cached attempt."""
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        return _observation([_row("v1", "s1")])

    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run(
        allow_cached_finalize=True,
        config_overrides={"source_stability_seconds": 600.0},
        cached_finalize=_never_cached_finalize,
        wall_budget_seconds=400,
        final_materialize_reserve_seconds=120,
    )

    assert len(calls) >= 2, "observations keep running until the deadline"
    assert summary.outcome == "CLOSEOUT_DEADLINE"
    assert publication is None
    assert not any("FAILED" in entry or "DISCOVERY_INCOMPLETE" in entry for entry in summary.phase_trace)
    assert "cached_finalized" not in summary.phase_trace
    assert "cached_blocked" not in summary.phase_trace
    run = harness.store.get_run(BUSINESS_DATE)
    assert run["frozen_snapshot_id"] is None
    assert run["state"] != "FINALIZED"


# ---------------------------------------------------------------------------
# Whitelist tightening: generic transport failure names cannot prove the
# outage happened during discovery, so they keep strict retryable semantics
# and must never unlock the guarded cached degraded finalization.  The
# adapter already wraps transport-level retry exhaustion into the typed
# SnapshotFetchExhausted (still eligible), and SnapshotConsistencyDrift
# means the source was actively changing — never a state to degrade from.
# ---------------------------------------------------------------------------


def _assert_transport_outage_never_caches(tmp_path: Path, discover, store) -> None:
    """Shared assertions: strict retryable walk, no cached attempt, no freeze.

    The config mirrors the SnapshotFetchExhausted deadline fallback test so
    the cached fallback IS offered at the deadline; only the tightened
    whitelist refusal can explain the strict outcome.
    """
    harness = _Harness(tmp_path, discover)
    summary, publication, _blocked, _error = harness.run(
        allow_cached_finalize=True,
        config_overrides={"source_stability_seconds": 600.0},
        cached_finalize=_never_cached_finalize,
        output_path=tmp_path / "final.xlsx",
        wall_budget_seconds=130,
        final_materialize_reserve_seconds=120,
        failed_pass_retry_seconds=5.0,
    )

    assert summary.outcome == "CLOSEOUT_DEADLINE", (
        "a generic transport outage keeps strict retryable semantics: the "
        "backoff walk reaches the long-operation deadline and stops"
    )
    assert "pass1:FAILED" in summary.phase_trace
    assert "stop:deadline" in summary.phase_trace
    assert "cached_finalized" not in summary.phase_trace
    assert "cached_blocked" not in summary.phase_trace
    assert publication is None
    assert not (tmp_path / "final.xlsx").exists()
    run = store.get_run(BUSINESS_DATE)
    assert run["frozen_snapshot_id"] is None
    assert run["state"] != "FINALIZED"


def test_safety_closeout_timeout_error_never_caches(tmp_path):
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise TimeoutError("x")

    store, _snapshots = _seed_full_good_day(tmp_path / "state.sqlite3", BUSINESS_DATE)
    _assert_transport_outage_never_caches(tmp_path, discover, store)
    assert calls == [0], "retryable passes sleep to the deadline without a second pass"


def test_safety_closeout_connection_error_never_caches(tmp_path):
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise ConnectionError("x")

    store, _snapshots = _seed_full_good_day(tmp_path / "state.sqlite3", BUSINESS_DATE)
    _assert_transport_outage_never_caches(tmp_path, discover, store)
    assert calls == [0]


def test_safety_closeout_url_error_never_caches(tmp_path):
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise URLError("x")

    store, _snapshots = _seed_full_good_day(tmp_path / "state.sqlite3", BUSINESS_DATE)
    _assert_transport_outage_never_caches(tmp_path, discover, store)
    assert calls == [0]


def test_safety_closeout_consistency_drift_never_caches(tmp_path):
    """A mid-pagination source change is the opposite of a stable snapshot."""
    calls: list[int] = []

    def discover(_timeout):
        calls.append(len(calls))
        raise SnapshotConsistencyDrift("saas", "count changed during pagination")

    store, _snapshots = _seed_full_good_day(tmp_path / "state.sqlite3", BUSINESS_DATE)
    _assert_transport_outage_never_caches(tmp_path, discover, store)
    assert calls == [0]
