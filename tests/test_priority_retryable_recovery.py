"""Recovery window for adapter-confirmed retryable Priority failures.

A Priority failure the adapter explicitly marks ``retryable=True`` (transient
transport, rows not yet materialized) must keep the durable job in
PRIORITY_PENDING across scheduler probes using its own durable priority
budget, without consuming later crawler-result harvest capacity. Non-retryable
failures stay immediately manual.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickBudget,
    TickConfig,
    TickDependencies,
    _harvest,
    run_incremental_tick,
)
from datetime import UTC, datetime

BUSINESS_DATE = "2026-08-11"
IDENTITY = "jd|1242978|RW-20260818110643"


@dataclass
class Clock:
    value: float = 0.0

    def monotonic(self) -> float:
        return self.value


def _seed_priority_pending_job(store: DailyAuditStore) -> dict:
    store.ensure_run(BUSINESS_DATE)
    token = store.acquire_lease(BUSINESS_DATE, "seed-priority-pending", 60)
    job = store.prepare_remote_intent(token, "logical-1", "task-1", "payload-1")
    store.claim_remote_submission(token, int(job["id"]))
    job = store.reconcile_remote_job(
        token,
        int(job["id"]),
        remote_identity=IDENTITY,
        state="PRIORITY_PENDING",
        result={"state": "PRIORITY_PENDING", "remote_identity": IDENTITY},
    )
    store.release_lease(token)
    return job


def _probe_config() -> TickConfig:
    # Production defaults only: the recovery window must come from the
    # default durable budget, not a test-tuned one.
    return TickConfig(
        business_date=BUSINESS_DATE,
        runtime_budget_seconds=60,
        heavy_work_reserve_seconds=0,
        lease_ttl_seconds=60,
    )


def _sweep(store, token, deps, config, clock):
    _harvest(
        store,
        token,
        deps,
        TickBudget(clock.monotonic(), 60, 0, clock.monotonic),
        {"crawl_harvested": 0},
        config,
    )


def test_default_priority_budget_matches_the_harvest_budget():
    config = TickConfig(business_date=BUSINESS_DATE)
    assert config.priority_retry_max_attempts == config.harvest_max_attempts == 120
    assert (
        config.priority_retry_max_elapsed_seconds
        == config.harvest_max_elapsed_seconds
        == 21600.0
    )


def test_ten_retryable_priority_stalls_stay_pending(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    job = _seed_priority_pending_job(store)
    deps = TickDependencies(
        discover=None,  # type: ignore[arg-type]
        harvest=lambda _jobs, _timeout: {
            int(job["id"]): {
                "state": "PRIORITY_PENDING",
                "remote_identity": IDENTITY,
                "retryable_error": "InterfaceError",
                "elapsed_seconds": 0.01,
                "result": {"state": "PRIORITY_PENDING"},
            }
        },
        monotonic=clock.monotonic,
    )
    config = _probe_config()
    token = store.acquire_lease(BUSINESS_DATE, "ten-stalls", 600)
    try:
        for expected_attempts in range(1, 11):
            _sweep(store, token, deps, config, clock)
            pending = store.get_remote_job(int(job["id"]))
            assert pending["state"] == "PRIORITY_PENDING", (
                f"probe {expected_attempts} must stay recoverable"
            )
            assert pending["priority_attempts"] == expected_attempts
            assert pending["harvest_attempts"] == 0
            assert pending["remote_identity"] == IDENTITY
    finally:
        store.release_lease(token)


def test_eleventh_probe_recovers_without_any_remote_create(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    job = _seed_priority_pending_job(store)
    probes = 0

    def discover(_timeout):
        return DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=[],
            evidence={"saas_count": 0, "customer_count": 0},
        )

    def harvest(_jobs, _timeout):
        nonlocal probes
        probes += 1
        if probes <= 10:
            return {
                int(job["id"]): {
                    "state": "PRIORITY_PENDING",
                    "remote_identity": IDENTITY,
                    "retryable_error": "PriorityRowsNotMaterialized",
                    "elapsed_seconds": 0.01,
                    "result": {"state": "PRIORITY_PENDING"},
                }
            }
        return {
            int(job["id"]): {
                "state": "SUBMITTED",
                "remote_identity": IDENTITY,
                "elapsed_seconds": 0.01,
                "result": {"priority_status": "PRIORITY_UPDATED", "weight_type": -1},
            }
        }

    deps = TickDependencies(
        discover=discover,
        harvest=harvest,
        submit_crawl=lambda *_args: pytest.fail(
            "priority recovery must never dispatch a new remote create"
        ),
        monotonic=clock.monotonic,
    )
    config = _probe_config()

    for _ in range(10):
        run_incremental_tick(
            store, ProcessLock(tmp_path / f"tick-{time.time_ns()}.lock"), config, deps
        )
        assert store.get_remote_job(int(job["id"]))["state"] == "PRIORITY_PENDING"

    final = run_incremental_tick(
        store, ProcessLock(tmp_path / f"tick-{time.time_ns()}.lock"), config, deps
    )
    recovered = store.get_remote_job(int(job["id"]))

    assert probes == 11
    assert recovered["state"] == "SUBMITTED"
    assert recovered["remote_identity"] == IDENTITY
    assert recovered["priority_attempts"] == 11
    assert recovered["harvest_attempts"] == 0
    assert recovered["error_text"] is None
    assert final.outcome != "MANUAL_REVIEW_REQUIRED"


def test_non_retryable_priority_error_is_manual_immediately(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    job = _seed_priority_pending_job(store)
    deps = TickDependencies(
        discover=None,  # type: ignore[arg-type]
        harvest=lambda _jobs, _timeout: {
            int(job["id"]): {
                "manual_error": "OperationalError",
                "remote_identity": IDENTITY,
            }
        },
        monotonic=clock.monotonic,
    )
    config = _probe_config()
    token = store.acquire_lease(BUSINESS_DATE, "manual", 60)
    try:
        _sweep(store, token, deps, config, clock)
        failed = store.get_remote_job(int(job["id"]))
    finally:
        store.release_lease(token)

    assert failed["state"] == "MANUAL_REQUIRED"
    assert failed["priority_attempts"] == 1
    assert failed["harvest_attempts"] == 0
    assert failed["remote_identity"] == IDENTITY
