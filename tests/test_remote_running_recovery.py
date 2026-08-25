"""Stall detection and recovery contracts for RUNNING zhaoshang remote jobs.

All remote interactions use fake callbacks; no test touches the production
Zhaoshang platform or the production SQLite state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.remote_running_recovery import (
    evaluate_running_stall,
)
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
)


BUSINESS_DATE = "2026-08-25"
NOW = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)
NO_PRICE_RSP = "未检索到商品价格信息"
PROGRESS_STALLED = {"total": 10, "pending": 4, "running": 3, "failed": 0, "waiting_db": 1, "finished": 2}


def _signature(progress: dict) -> list[int]:
    return [
        int(progress["total"]),
        int(progress["pending"]),
        int(progress["running"]),
        int(progress["failed"]),
        int(progress["waiting_db"]),
        int(progress["finished"]),
    ]


def _observations(count: int, *, progress: dict | None = None, end: datetime = NOW, span_minutes: int = 70, export_count: int = 0) -> dict:
    """Build an evidence document whose last ``count`` observations share a signature."""
    base_progress = progress if progress is not None else dict(PROGRESS_STALLED)
    step = timedelta(minutes=span_minutes) / max(1, count - 1) if count > 1 else timedelta(0)
    rows = []
    for index in range(count):
        moment = end - step * (count - 1 - index)
        rows.append(
            {
                "observed_at": moment.isoformat(timespec="seconds"),
                "progress": dict(base_progress),
                "signature": _signature(base_progress),
                "export_count": export_count,
                "export_rsp_desc": NO_PRICE_RSP,
                "export_had_result": False,
                "task_status_rsp_desc": "执行中",
            }
        )
    return {"version": 1, "observations": rows, "restart": None}


def _job(
    *,
    state: str = "RUNNING",
    attempts: int = 15,
    age_minutes: int = 180,
    remote_identity: str | None = "lxwl|1269504|RW-20260825150224",
    mall_key: str = "lxwl",
    evidence: dict | None = None,
    recovery_attempts: int = 0,
    created_at: datetime | None = None,
) -> dict:
    created = created_at or (NOW - timedelta(minutes=age_minutes))
    row: dict = {
        "id": 53,
        "run_id": 1,
        "task_name": "cnnc_current_opt_领先未来商城_61284593895349",
        "mall_key": mall_key,
        "state": state,
        "remote_identity": remote_identity,
        "harvest_attempts": attempts,
        "harvest_elapsed_seconds": 300.0,
        "recovery_attempts": recovery_attempts,
        "recovery_evidence_json": json.dumps(evidence, ensure_ascii=False) if evidence is not None else None,
        "created_at": created.isoformat(timespec="seconds"),
        "updated_at": NOW.isoformat(timespec="seconds"),
        "post_attempted": 1,
    }
    return row


# ---------------------------------------------------------------------------
# A. Stall detector (task-book cases 1-9)
# ---------------------------------------------------------------------------


def test_case1_job53_pattern_is_stalled() -> None:
    job = _job(evidence=_observations(4))
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["probe_candidate"] is True
    assert decision["stalled"] is True
    assert decision["exhausted_ready"] is False


def test_case2_job88_pattern_is_not_stalled_and_not_even_probe_candidate() -> None:
    job = _job(attempts=6, age_minutes=25, evidence=_observations(2, span_minutes=20))
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["probe_candidate"] is False
    assert decision["stalled"] is False


def test_case3_long_running_with_growing_progress_is_not_stalled() -> None:
    observations = []
    for index in range(4):
        progress = dict(PROGRESS_STALLED)
        progress["finished"] = 2 + index
        observations.append(
            {
                "observed_at": (NOW - timedelta(minutes=45 * (3 - index))).isoformat(timespec="seconds"),
                "progress": progress,
                "signature": _signature(progress),
                "export_count": 0,
                "export_rsp_desc": NO_PRICE_RSP,
                "export_had_result": False,
                "task_status_rsp_desc": "执行中",
            }
        )
    job = _job(attempts=10, age_minutes=140, evidence={"version": 1, "observations": observations, "restart": None})
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False


def test_case4_stationary_but_too_young_is_not_stalled() -> None:
    job = _job(attempts=3, age_minutes=45, evidence=_observations(3, span_minutes=40))
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False


def test_case5_zero_export_count_with_changing_progress_is_not_stalled() -> None:
    observations = []
    for index in range(4):
        progress = dict(PROGRESS_STALLED)
        progress["pending"] = 4 - index
        progress["finished"] = 2 + index
        observations.append(
            {
                "observed_at": (NOW - timedelta(minutes=20 * (3 - index))).isoformat(timespec="seconds"),
                "progress": progress,
                "signature": _signature(progress),
                "export_count": 0,
                "export_rsp_desc": NO_PRICE_RSP,
                "export_had_result": False,
                "task_status_rsp_desc": "执行中",
            }
        )
    job = _job(attempts=9, age_minutes=95, evidence={"version": 1, "observations": observations, "restart": None})
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False


def test_case6_submit_unknown_is_never_stalled() -> None:
    job = _job(state="SUBMIT_UNKNOWN", evidence=_observations(4))
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False
    assert decision["probe_candidate"] is False


def test_case7_manual_required_is_never_stalled() -> None:
    job = _job(state="MANUAL_REQUIRED", evidence=_observations(4))
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False
    assert decision["probe_candidate"] is False


def test_case8_already_recovered_job_never_restarts_again() -> None:
    evidence = _observations(4)
    evidence["restart"] = {
        "phase": "ENABLE_CONFIRMED",
        "phase_at": (NOW - timedelta(minutes=20)).isoformat(timespec="seconds"),
        "recovery_attempts": 1,
        "recovery_at": (NOW - timedelta(minutes=20)).isoformat(timespec="seconds"),
        "pre_restart_signature": _signature(PROGRESS_STALLED),
    }
    job = _job(recovery_attempts=1, evidence=evidence)
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False


def test_case9_legacy_job_without_recovery_evidence_is_not_stalled() -> None:
    job = _job(evidence=None)
    job["recovery_evidence_json"] = None
    decision = evaluate_running_stall(job, now=NOW)
    # A legacy row may start collecting probe evidence after the deployment,
    # but the absence of evidence must never be read as a proven stall.
    assert decision["stalled"] is False
    assert decision["exhausted_ready"] is False


def test_stationary_observations_with_export_result_are_not_no_price_evidence() -> None:
    evidence = _observations(4, export_count=0)
    for row in evidence["observations"]:
        row["export_had_result"] = True
    job = _job(evidence=evidence)
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False


def test_stationary_span_under_one_hour_is_not_stalled() -> None:
    job = _job(evidence=_observations(4, span_minutes=45))
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False


# ---------------------------------------------------------------------------
# Tick integration: probe recording only (no remote writes in commit 1)
# ---------------------------------------------------------------------------


def _tick(store: DailyAuditStore, deps: TickDependencies, *, finalize: bool = False):
    config = TickConfig(
        business_date=BUSINESS_DATE,
        runtime_budget_seconds=720.0,
        heavy_work_reserve_seconds=120.0,
    )
    deps.wall_clock = lambda: NOW
    lock = ProcessLock(store.path.with_name("tick.lock"))
    return run_incremental_tick(store, lock, config, deps, finalize=finalize)


def _seed_running_job(store: DailyAuditStore, *, attempts: int = 2, age_minutes: int = 35) -> int:
    token = store.acquire_lease(BUSINESS_DATE, "seed", ttl_seconds=300)
    store.ensure_run(BUSINESS_DATE)
    job = store.prepare_remote_intent(
        token, "logical-1", "cnnc_current_opt_领先未来商城_61284593895349", "payload-hash", mall_key="lxwl"
    )
    store.claim_remote_submission(token, int(job["id"]))
    store.reconcile_remote_job(
        token, int(job["id"]), remote_identity="lxwl|1269504|RW-20260825150224", state="RUNNING"
    )
    with store._transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE remote_jobs SET harvest_attempts=?, created_at=?, updated_at=? WHERE id=?",
            (
                attempts,
                (NOW - timedelta(minutes=age_minutes)).isoformat(timespec="seconds"),
                (NOW - timedelta(minutes=age_minutes)).isoformat(timespec="seconds"),
                int(job["id"]),
            ),
        )
    store.release_lease(token)
    return int(job["id"])


def _discover_noop(timeout: float) -> DiscoveredState:
    return DiscoveredState(observed_at=NOW, headers=(), rows=())


def test_tick_persists_probe_observation_for_candidate_running_job(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_id = _seed_running_job(store)
    calls: list[int] = []

    def probe(job: dict) -> dict:
        calls.append(int(job["id"]))
        return {
            "progress": dict(PROGRESS_STALLED),
            "export_count": 0,
            "export_rsp_desc": NO_PRICE_RSP,
            "export_had_result": False,
            "task_status_rsp_desc": "执行中",
        }

    deps = TickDependencies(
        discover=_discover_noop,
        remote_recovery_probe=probe,
    )
    summary = _tick(store, deps)
    assert calls == [job_id]
    refreshed = store.get_remote_job(job_id)
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert len(evidence["observations"]) == 1
    assert evidence["observations"][0]["export_count"] == 0
    assert refreshed["state"] == "RUNNING"
    assert summary.outcome == "WAITING_REMOTE"


def test_tick_without_probe_callback_is_a_noop_for_recovery(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_id = _seed_running_job(store)
    deps = TickDependencies(discover=_discover_noop)
    _tick(store, deps)
    refreshed = store.get_remote_job(job_id)
    assert refreshed["recovery_evidence_json"] is None


def test_stalled_job_without_restart_callback_stays_running(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_id = _seed_running_job(store)
    token = store.acquire_lease(BUSINESS_DATE, "seed2", ttl_seconds=300)
    evidence = _observations(3)  # the pass will append the 4th one
    store.update_remote_recovery_evidence(token, job_id, evidence)
    store.release_lease(token)
    # Make the job satisfy the remaining stall gates: attempts and wall-clock.
    with store._transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE remote_jobs SET harvest_attempts=15, created_at=? WHERE id=?",
            ((NOW - timedelta(minutes=180)).isoformat(timespec="seconds"), job_id),
        )

    def probe(job: dict) -> dict:
        return {
            "progress": dict(PROGRESS_STALLED),
            "export_count": 0,
            "export_rsp_desc": NO_PRICE_RSP,
            "export_had_result": False,
            "task_status_rsp_desc": "执行中",
        }

    deps = TickDependencies(discover=_discover_noop, remote_recovery_probe=probe)
    _tick(store, deps)
    refreshed = store.get_remote_job(job_id)
    assert refreshed["state"] == "RUNNING"
    assert int(refreshed["recovery_attempts"] or 0) == 0
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert len(evidence["observations"]) == 4
