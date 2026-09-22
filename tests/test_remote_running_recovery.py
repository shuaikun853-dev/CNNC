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
    run_running_recovery_pass,
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


def _tick(
    store: DailyAuditStore,
    deps: TickDependencies,
    *,
    finalize: bool = False,
    budget_seconds: float = 720.0,
    reserve_seconds: float = 120.0,
):
    config = TickConfig(
        business_date=BUSINESS_DATE,
        runtime_budget_seconds=budget_seconds,
        heavy_work_reserve_seconds=reserve_seconds,
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


def _seed_candidate_jobs(store: DailyAuditStore, count: int, *, attempts: int = 2, age_minutes: int = 35) -> list[int]:
    """Seed several distinct probe-candidate RUNNING jobs on one store."""
    token = store.acquire_lease(BUSINESS_DATE, "seed-many", ttl_seconds=300)
    store.ensure_run(BUSINESS_DATE)
    ids: list[int] = []
    for index in range(count):
        marker = 61284593895349 + index
        job = store.prepare_remote_intent(
            token,
            f"logical-{index}",
            f"cnnc_current_opt_领先未来商城_{marker}",
            f"payload-hash-{index}",
            mall_key="lxwl",
        )
        store.claim_remote_submission(token, int(job["id"]))
        store.reconcile_remote_job(
            token,
            int(job["id"]),
            remote_identity=f"lxwl|126950{index}|RW-2026082515022{index}",
            state="RUNNING",
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
        ids.append(int(job["id"]))
    store.release_lease(token)
    return ids


def _discover_noop(timeout: float) -> DiscoveredState:
    return DiscoveredState(observed_at=NOW, headers=(), rows=())


def test_tick_persists_probe_observation_for_candidate_running_job(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_id = _seed_running_job(store)
    calls: list[int] = []

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
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

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
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


# ---------------------------------------------------------------------------
# Review round 2, Finding 3: recovery must obey the tick budget.  Probes and
# restarts receive a TickBudget-derived timeout, the pass is capped per tick,
# and core discovery must never be starved by recovery probes.
# ---------------------------------------------------------------------------


def test_tick_skips_recovery_probe_when_budget_tight(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_id = _seed_running_job(store)
    calls: list[int] = []
    clock = {"now": 1000.0}

    def monotonic() -> float:
        return clock["now"]

    def discover(timeout: float) -> DiscoveredState:
        clock["now"] += 80.0  # core intake consumes most of the tick
        return DiscoveredState(observed_at=NOW, headers=(), rows=())

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
        calls.append(int(job["id"]))
        return _probe_payload()

    deps = TickDependencies(discover=discover, remote_recovery_probe=probe)
    deps.monotonic = monotonic
    summary = _tick(store, deps, budget_seconds=100.0, reserve_seconds=30.0)
    assert calls == []
    refreshed = store.get_remote_job(job_id)
    assert refreshed["recovery_evidence_json"] is None
    assert summary.outcome in {"BUDGET_EXHAUSTED", "WAITING_REMOTE"}


def test_tick_probe_callback_timeout_is_bounded_by_budget(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    _seed_running_job(store)
    captured: list[float] = []

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
        captured.append(float(timeout_seconds))
        return _probe_payload()

    deps = TickDependencies(discover=_discover_noop, remote_recovery_probe=probe)
    deps.monotonic = lambda: 0.0
    _tick(store, deps, budget_seconds=45.0, reserve_seconds=10.0)
    assert captured
    assert all(0.0 < value <= 45.0 for value in captured)
    assert max(captured) == 45.0  # min(configured 60s ceiling, remaining 45s)


def test_tick_restart_callback_timeout_is_bounded_by_budget(tmp_path) -> None:
    store, _job_id = _stalled_store(tmp_path)
    captured: list[float] = []

    def restart(job: dict, resume_phase: str | None, timeout_seconds: float | None = None) -> dict:
        captured.append(float(timeout_seconds))
        return {
            "status": "RESTARTED",
            "restarted": True,
            "disable_evidence": {"confirmed_disabled": True},
            "enable_evidence": {"confirmed_enabled": True},
        }

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
        return _probe_payload()

    deps = TickDependencies(
        discover=_discover_noop, remote_recovery_probe=probe, remote_recovery_restart=restart
    )
    deps.monotonic = lambda: 0.0
    _tick(store, deps, budget_seconds=50.0, reserve_seconds=10.0)
    assert captured
    assert all(0.0 < value <= 50.0 for value in captured)
    assert max(captured) == 50.0  # min(configured 60s ceiling, remaining 50s)


def test_recovery_pass_caps_probe_candidates_per_tick(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_ids = _seed_candidate_jobs(store, 6)
    calls: list[int] = []

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
        calls.append(int(job["id"]))
        parts = str(job["remote_identity"]).split("|")
        return _probe_payload(remote_task_id=parts[1], remote_task_number=parts[2])

    token = store.acquire_lease(BUSINESS_DATE, "cap", ttl_seconds=300)
    try:
        counters = run_running_recovery_pass(
            store, token, probe_remote=probe, restart_remote=None, now=NOW, can_start=lambda: True
        )
    finally:
        store.release_lease(token)
    assert len(calls) == 4
    assert counters["recovery_probes"] == 4
    probed = [store.get_remote_job(job_id) for job_id in job_ids if job_id in calls]
    unprobed = [store.get_remote_job(job_id) for job_id in job_ids if job_id not in calls]
    assert all(row["recovery_evidence_json"] for row in probed)
    assert all(row["recovery_evidence_json"] is None for row in unprobed)


def test_full_tick_runs_discovery_with_recovery_candidates_present(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    _seed_candidate_jobs(store, 4)
    discovered: list[float] = []
    clock = {"now": 1000.0}

    def monotonic() -> float:
        return clock["now"]

    def discover(timeout: float) -> DiscoveredState:
        discovered.append(timeout)
        return DiscoveredState(observed_at=NOW, headers=(), rows=())

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
        # Each probe consumes a large slice of the tick budget, as a probe
        # with several bounded network calls would in production.
        clock["now"] += 25.0
        parts = str(job["remote_identity"]).split("|")
        return _probe_payload(remote_task_id=parts[1], remote_task_number=parts[2])

    deps = TickDependencies(discover=discover, remote_recovery_probe=probe)
    deps.monotonic = monotonic
    summary = _tick(store, deps, budget_seconds=100.0, reserve_seconds=30.0)
    assert discovered, "core discovery must still run while recovery candidates exist"
    assert "running_recovery" in summary.phase_trace
    assert summary.phase_trace.index("discover") < summary.phase_trace.index("running_recovery")


# ---------------------------------------------------------------------------
# B. One-shot exact restart (task-book cases 7-13)
# ---------------------------------------------------------------------------


class _FakeRestart:
    def __init__(self, script: list[dict]) -> None:
        self._script = script
        self.calls: list[tuple[int, str | None]] = []

    def __call__(self, job: dict, resume_phase: str | None) -> dict:
        self.calls.append((int(job["id"]), resume_phase))
        step = self._script[min(len(self.calls) - 1, len(self._script) - 1)]
        return dict(step)


def _stalled_store(tmp_path, *, attempts: int = 15, age_minutes: int = 180, observations: int = 4):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_id = _seed_running_job(store, attempts=attempts, age_minutes=age_minutes)
    token = store.acquire_lease(BUSINESS_DATE, "seed-stall", ttl_seconds=300)
    store.update_remote_recovery_evidence(token, job_id, _observations(observations))
    store.release_lease(token)
    return store, job_id


def _probe_payload(**overrides) -> dict:
    payload = {
        "remote_task_id": "1269504",
        "remote_task_number": "RW-20260825150224",
        "remote_mall": "lxwl",
        "progress": dict(PROGRESS_STALLED),
        "export_count": 0,
        "export_rsp_desc": NO_PRICE_RSP,
        "export_had_result": False,
        "task_status_rsp_desc": "执行中",
    }
    payload.update(overrides)
    return payload


def _run_pass(store, probe: dict, restart, *, now: datetime = NOW) -> dict:
    token = store.acquire_lease(BUSINESS_DATE, "pass", ttl_seconds=300)
    try:
        return run_running_recovery_pass(
            store, token, probe_remote=lambda job: probe, restart_remote=restart, now=now, can_start=lambda: True
        )
    finally:
        store.release_lease(token)


def test_restart_executes_once_and_persists_recovery_attempt(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            {
                "status": "RESTARTED",
                "restarted": True,
                "disable_evidence": {"rspCode": "0000", "confirmed_disabled": True},
                "enable_evidence": {"rspCode": "0000", "confirmed_enabled": True},
            }
        ]
    )
    counters = _run_pass(store, _probe_payload(), restart)
    assert restart.calls == [(job_id, None)]
    assert counters["recovery_restarts"] == 1
    refreshed = store.get_remote_job(job_id)
    assert refreshed["state"] == "RUNNING"
    assert int(refreshed["recovery_attempts"]) == 1
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "ENABLE_CONFIRMED"
    assert evidence["restart"]["recovery_attempts"] == 1
    assert evidence["restart"]["recovery_at"]
    assert evidence["restart"]["disable_evidence"]["confirmed_disabled"] is True
    assert evidence["restart"]["enable_evidence"]["confirmed_enabled"] is True
    assert evidence["restart"]["recovery_reason"] == "REMOTE_RUNNING_STALLED"


def test_probe_identity_conflict_blocks_any_remote_write(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
    _run_pass(store, _probe_payload(remote_task_id="999999"), restart)
    assert restart.calls == []
    refreshed = store.get_remote_job(job_id)
    assert int(refreshed["recovery_attempts"] or 0) == 0


def test_probe_with_export_result_blocks_restart(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
    _run_pass(store, _probe_payload(export_had_result=True, export_count=None), restart)
    assert restart.calls == []
    refreshed = store.get_remote_job(job_id)
    assert refreshed["state"] == "RUNNING"


def test_disable_unknown_persists_sent_phase_and_resumes_without_resend(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            {"status": "DISABLE_UNKNOWN", "restarted": False, "observed_enabled": True},
            {
                "status": "RESTARTED",
                "restarted": True,
                "reconciled": True,
                "disable_evidence": {"confirmed_disabled": True, "reconciled": True},
                "enable_evidence": {"rspCode": "0000", "confirmed_enabled": True},
            },
        ]
    )
    _run_pass(store, _probe_payload(), restart, now=NOW)
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "DISABLE_SENT"
    assert int(store.get_remote_job(job_id)["recovery_attempts"] or 0) == 0
    # The durable phase already burned the one disable; the next pass must
    # resume by reconciliation, never issue a fresh disable.
    _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=2))
    assert restart.calls == [(job_id, None), (job_id, "DISABLE_SENT")]
    refreshed = store.get_remote_job(job_id)
    assert int(refreshed["recovery_attempts"]) == 1
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "ENABLE_CONFIRMED"


def test_enable_unknown_resumes_by_reconciliation_only(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            {"status": "ENABLE_UNKNOWN", "restarted": False, "observed_enabled": False},
            {
                "status": "RESTARTED",
                "restarted": True,
                "reconciled": True,
                "disable_evidence": {"confirmed_disabled": True},
                "enable_evidence": {"confirmed_enabled": True, "reconciled": True},
            },
        ]
    )
    _run_pass(store, _probe_payload(), restart, now=NOW)
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "ENABLE_SENT"
    _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=2))
    assert restart.calls == [(job_id, None), (job_id, "ENABLE_SENT")]
    assert int(store.get_remote_job(job_id)["recovery_attempts"]) == 1


_CONFIRMED_RESTART_STEP = {
    "status": "RESTARTED",
    "restarted": True,
    "disable_evidence": {"confirmed_disabled": True},
    "enable_evidence": {"confirmed_enabled": True},
}


def test_restart_outcome_survives_store_reopen(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    _run_pass(store, _probe_payload(), restart)
    reopened = DailyAuditStore(tmp_path / "state.sqlite3")
    refreshed = reopened.get_remote_job(job_id)
    assert int(refreshed["recovery_attempts"]) == 1
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "ENABLE_CONFIRMED"


def test_recovered_job_is_never_restarted_twice(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    _run_pass(store, _probe_payload(), restart)
    # The job stalls again with a fresh frozen signature after the restart.
    restart_again = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    _run_pass(store, _probe_payload(), restart_again, now=NOW + timedelta(minutes=5))
    assert restart_again.calls == []
    assert int(store.get_remote_job(job_id)["recovery_attempts"]) == 1


def test_identity_conflict_outcome_fails_closed_to_manual(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "IDENTITY_CONFLICT", "restarted": False, "diagnostic": "exact task reconciliation returned a different task name"}])
    _run_pass(store, _probe_payload(), restart)
    refreshed = store.get_remote_job(job_id)
    assert refreshed["state"] == "MANUAL_REQUIRED"
    assert "identity" in str(refreshed["error_text"]).lower()


def test_unconfirmed_write_beyond_window_fails_closed_to_manual(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "DISABLE_UNCONFIRMED", "restarted": False, "observed_enabled": True}])
    _run_pass(store, _probe_payload(), restart, now=NOW)
    assert store.get_remote_job(job_id)["state"] == "RUNNING"
    later = NOW + timedelta(minutes=20)
    _run_pass(store, _probe_payload(), restart, now=later)
    refreshed = store.get_remote_job(job_id)
    assert refreshed["state"] == "MANUAL_REQUIRED"
    assert "unconfirmed" in str(refreshed["error_text"]).lower()


def test_unconfirmed_write_inside_window_stays_pending(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "DISABLE_UNCONFIRMED", "restarted": False, "observed_enabled": True}])
    _run_pass(store, _probe_payload(), restart, now=NOW)
    _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=3))
    refreshed = store.get_remote_job(job_id)
    assert refreshed["state"] == "RUNNING"
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "DISABLE_SENT"


# ---------------------------------------------------------------------------
# Adapter production contracts: exact disable -> confirm -> enable -> confirm
# ---------------------------------------------------------------------------

TASK_ID = "1269504"
TASK_NUMBER = "RW-20260825150224"


class _FakeRestartResponse:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {}
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


_ACTIVE_PROGRESS_RSP = "总任务量：10条，待执行：4条，执行中：3条，失败：0条，待入库：1条，执行完成：2条"
_TERMINAL_PROGRESS_RSP = "总任务量：10条，待执行：0条，执行中：0条，失败：0条，待入库：0条，执行完成：10条"


class _RestartSession:
    """Stateful fake: updateState flips the exact task's enabled flag."""

    def __init__(
        self,
        task: dict,
        *,
        fail_disable: bool = False,
        fail_enable: bool = False,
        fail_lookup: bool = False,
        fail_status: bool = False,
        fail_progress: bool = False,
        fail_lookup_after_enable: bool = False,
        fail_lookup_after_disable: bool = False,
        status_rsp_desc: str = "执行中",
        progress_rsp_desc: str = _ACTIVE_PROGRESS_RSP,
        status_success: bool = True,
        status_payload: object = None,
        progress_success: bool = True,
        progress_payload: object = None,
    ) -> None:
        self.task = task
        self.fail_disable = fail_disable
        self.fail_enable = fail_enable
        self.fail_lookup = fail_lookup
        self.fail_status = fail_status
        self.fail_progress = fail_progress
        self.fail_lookup_after_enable = fail_lookup_after_enable
        self.fail_lookup_after_disable = fail_lookup_after_disable
        self.status_rsp_desc = status_rsp_desc
        self.progress_rsp_desc = progress_rsp_desc
        self.status_success = status_success
        self.status_payload = status_payload
        self.progress_success = progress_success
        self.progress_payload = progress_payload
        self.update_calls: list[dict] = []
        self.post_timeouts: list[float | None] = []
        self.get_timeouts: list[float | None] = []
        self.post_calls = 0
        self.get_calls = 0
        self._enable_posts_seen = 0
        self._disable_posts_seen = 0

    def post(self, url, json=None, timeout=None):
        self.post_calls += 1
        self.post_timeouts.append(timeout)
        if "updateState" in url:
            self.update_calls.append(dict(json))
            if json.get("state") == "0":
                self._disable_posts_seen += 1
                if self.fail_disable:
                    raise TimeoutError("disable response lost")
                self.task["enabled"] = False
                return _FakeRestartResponse({"rspCode": "0000", "success": True})
            if json.get("state") == "1":
                self._enable_posts_seen += 1
                if self.fail_enable:
                    raise TimeoutError("enable response lost")
                self.task["enabled"] = True
                return _FakeRestartResponse({"rspCode": "0000", "success": True})
        if "exportTask" in url:
            return _FakeRestartResponse(
                {"rspCode": "0000", "rspDesc": NO_PRICE_RSP, "count": 0, "data": None, "success": True}
            )
        if "queryTaskStatus" in url:
            if self.fail_status:
                raise TimeoutError("status response lost")
            if self.status_payload is not None:
                return _FakeRestartResponse(self.status_payload)
            return _FakeRestartResponse(
                {"rspDesc": self.status_rsp_desc, "success": self.status_success}
            )
        if "queryList" in url:
            if self.fail_lookup or (
                self.fail_lookup_after_enable and self._enable_posts_seen > 0
            ) or (
                self.fail_lookup_after_disable and self._disable_posts_seen > 0
            ):
                raise TimeoutError("lookup response lost")
            return _FakeRestartResponse({"data": [dict(self.task)]})
        return _FakeRestartResponse({"data": [dict(self.task)]})

    def get(self, url, params=None, timeout=None):
        self.get_calls += 1
        self.get_timeouts.append(timeout)
        if self.fail_progress:
            raise TimeoutError("progress response lost")
        if self.progress_payload is not None:
            return _FakeRestartResponse(self.progress_payload)
        return _FakeRestartResponse(
            {"rspDesc": self.progress_rsp_desc, "success": self.progress_success}
        )


def _restart_tool_module(task: dict, session: _RestartSession):
    class _Module:
        EXPORT_TASK_URL = "https://platform.invalid/exportTask"
        QUERY_PROGRESS_URL = "https://platform.invalid/queryTaskProgress"
        QUERY_TASK_STATUS_URL = "https://platform.invalid/queryTaskStatus"
        UPDATE_STATE_URL = "https://platform.invalid/updateState"
        QUERY_LIST_URL = "https://platform.invalid/queryList"

        @staticmethod
        def build_session():
            return session

        @staticmethod
        def priority_mall_code_for_key(mall_key):
            return "lxwl"

        @staticmethod
        def build_browser_task_identifiers(task_name, mall_key):
            return task_name, task_name

        @staticmethod
        def check_existing_task(session, task_name, mall_code):
            # Faithful to the real tool: HTTP through the session with its
            # own hard-coded timeout.
            payload = session.post(
                _Module.QUERY_LIST_URL, json={"taskName": task_name}, timeout=60
            ).json()
            return payload["data"][0]

        @staticmethod
        def get_task_identity(task, mall_code):
            return task["taskId"], task["taskNumber"], mall_code

        @staticmethod
        def query_task_status(session, task_id):
            return session.post(
                _Module.QUERY_TASK_STATUS_URL + f"?taskId={task_id}", timeout=60
            ).json()

        @staticmethod
        def query_task_progress(session, batch_number):
            return session.get(
                _Module.QUERY_PROGRESS_URL, params={"batchNumber": batch_number}, timeout=60
            ).json()

        @staticmethod
        def parse_task_progress_desc(rsp_desc):
            values = {
                "总任务量": "total",
                "待执行": "pending",
                "执行中": "running",
                "失败": "failed",
                "待入库": "waiting_db",
                "执行完成": "finished",
            }
            summary = {}
            for label, key in values.items():
                marker = f"{label}："
                if marker in rsp_desc:
                    head = rsp_desc.split(marker, 1)[1]
                    summary[key] = int(head.split("条", 1)[0])
            return summary

        @staticmethod
        def disable_task(session, task_id):
            return session.post(
                _Module.UPDATE_STATE_URL, json={"taskId": task_id, "state": "0"}, timeout=60
            ).json()

        @staticmethod
        def enable_task(session, task_id):
            return session.post(
                _Module.UPDATE_STATE_URL, json={"taskId": task_id, "state": "1"}, timeout=60
            ).json()

    return _Module


class _FakeAuthManager:
    def export_environment(
        self, system, refresh=False, timeout_seconds=None, deadline=None, monotonic=None
    ):
        return {"BLADE_AUTH": "bearer fake", "COOKIE": "x-access-token=fake; refreshToken=fake"}


def _prepared_job(tmp_path, monkeypatch, task: dict, session: _RestartSession, monotonic=None, auth=None):
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter

    run_dir = tmp_path / "zhaoshang"
    run_dir.mkdir(parents=True, exist_ok=True)
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.write_text("# batch\n", encoding="utf-8")
    clock_kwargs = {"monotonic": monotonic} if monotonic is not None else {}
    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[], db_env_path=None, auth_manager=auth or _FakeAuthManager(), **clock_kwargs
    )
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        [
            type(
                "Request",
                (),
                {
                    "mall_key": "lxwl",
                    "mall_source_id": "34",
                    "mall_name": "领先未来商城",
                    "sku_id": "sku-1",
                    "url": "https://www.66123123.com/item/1",
                    "backend_version": "legacy-v1",
                    "identity": ("lxwl", "34", "sku-1", "https://www.66123123.com/item/1", "legacy-v1"),
                },
            )()
        ],
        run_dir / "input.xlsx",
        run_dir / "result.xlsx",
        "cnnc_current_opt_领先未来商城_61284593895349",
        batch_state,
    )
    monkeypatch.setattr(
        type(adapter),
        "_load_module",
        classmethod(lambda cls, path, name: _restart_tool_module(task, session)),
    )
    return adapter, prepared


def _running_task_record(*, task_id: str = TASK_ID, task_number: str = TASK_NUMBER, enabled: bool = True) -> dict:
    return {
        "taskName": "cnnc_current_opt_领先未来商城_61284593895349",
        "mallLogo": "lxwl",
        "taskId": task_id,
        "taskNumber": task_number,
        "batchNumber": "cnnc_current_opt_领先未来商城_61284593895349",
        "enabled": enabled,
    }


def test_adapter_restart_full_protocol_confirms_both_writes(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "RESTARTED"
    assert outcome.disable_confirmed is True
    assert outcome.enable_confirmed is True
    disable_calls = [call for call in session.update_calls if call.get("state") == "0"]
    enable_calls = [call for call in session.update_calls if call.get("state") == "1"]
    assert len(disable_calls) == 1
    assert len(enable_calls) == 1
    assert disable_calls[0]["taskId"] == TASK_ID
    assert enable_calls[0]["taskId"] == TASK_ID


def test_adapter_restart_resume_disable_sent_never_disables_again(tmp_path, monkeypatch) -> None:
    task = _running_task_record(enabled=False)
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(
        prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0, resume_phase="DISABLE_SENT"
    )

    assert outcome.status.value == "RESTARTED"
    assert [call for call in session.update_calls if call.get("state") == "0"] == []
    assert len([call for call in session.update_calls if call.get("state") == "1"]) == 1


def test_adapter_restart_resume_enable_sent_never_writes(tmp_path, monkeypatch) -> None:
    task = _running_task_record(enabled=True)
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(
        prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0, resume_phase="ENABLE_SENT"
    )

    assert outcome.status.value == "RESTARTED"
    assert outcome.enable_confirmed is True
    assert session.update_calls == []


def test_adapter_restart_identity_conflict_issues_no_write(tmp_path, monkeypatch) -> None:
    task = _running_task_record(task_id="424242")
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "IDENTITY_CONFLICT"
    assert session.update_calls == []


def test_adapter_restart_disable_unknown_reports_observed_state(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_disable=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "DISABLE_UNKNOWN"
    assert outcome.observed_enabled is True
    assert [call for call in session.update_calls if call.get("state") == "1"] == []


# ---------------------------------------------------------------------------
# Review round 2, Finding 1: fresh recovery must never enable an externally
# disabled task.  CNNC holds no durable evidence that the observed disable
# came from this protocol, so the only legal answer is zero remote writes.
# ---------------------------------------------------------------------------


def test_adapter_fresh_restart_of_disabled_task_issues_no_write(tmp_path, monkeypatch) -> None:
    task = _running_task_record(enabled=False)
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "EXTERNALLY_DISABLED"
    assert outcome.observed_enabled is False
    assert session.update_calls == []
    assert task["enabled"] is False


def test_fresh_externally_disabled_outcome_fails_closed_to_manual(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [{"status": "EXTERNALLY_DISABLED", "restarted": False, "observed_enabled": False}]
    )
    _run_pass(store, _probe_payload(), restart)
    refreshed = store.get_remote_job(job_id)
    assert refreshed["state"] == "MANUAL_REQUIRED"
    assert int(refreshed["recovery_attempts"] or 0) == 0
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] != "ENABLE_SENT"


# ---------------------------------------------------------------------------
# Review round 2, Finding 2: the write path must revalidate that the exact
# task is STILL RUNNING right before the first fresh remote write, and only
# status-confirmed-running observations may count as stall evidence.  The
# status vocabulary is evidence-anchored: "本次任务执行完毕" is the terminal
# marker the external tool itself interprets (auto_crawl_task.py:1048) and
# "执行中" is the platform's executing label; anything else fails closed.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Review round 3, Finding A: live activity must be derived from the
# structured query_task_progress six-field evidence (the external tool's own
# parse_task_progress_desc / is_progress_finished semantics), NOT from an
# unverified query_task_status RUNNING string.  "执行中" is a progress
# label, never a confirmed status-endpoint value (all 24 real captures were
# empty).  Tri-state: ACTIVE / TERMINAL / UNKNOWN, fail closed on UNKNOWN.
# ---------------------------------------------------------------------------


def _live_progress(**overrides) -> dict:
    progress = {"total": 10, "pending": 0, "running": 3, "failed": 0, "waiting_db": 0, "finished": 7}
    progress.update(overrides)
    return progress


def test_case_a1_active_progress_with_empty_status_is_active() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    state = classify_remote_execution_state(_live_progress(), "")
    assert state is RemoteExecutionState.ACTIVE


def test_case_a2_all_finished_progress_is_terminal() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    state = classify_remote_execution_state(
        _live_progress(pending=0, running=0, waiting_db=0, finished=10), ""
    )
    assert state is RemoteExecutionState.TERMINAL


def test_case_a3_terminal_status_marker_wins_over_active_progress() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    state = classify_remote_execution_state(_live_progress(running=3), "本次任务执行完毕")
    assert state is RemoteExecutionState.TERMINAL


def test_case_a4_missing_progress_field_is_unknown() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    progress = _live_progress()
    del progress["running"]
    assert classify_remote_execution_state(progress, "") is RemoteExecutionState.UNKNOWN


def test_case_a5_zero_total_is_unknown() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    assert classify_remote_execution_state(
        _live_progress(total=0, finished=0), ""
    ) is RemoteExecutionState.UNKNOWN


def test_case_a6_contradictory_progress_is_unknown() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    assert classify_remote_execution_state(
        _live_progress(pending=0, running=0, waiting_db=0, finished=12), ""
    ) is RemoteExecutionState.UNKNOWN
    assert classify_remote_execution_state(
        _live_progress(pending=0, running=0, waiting_db=0, finished=9, failed=-1), ""
    ) is RemoteExecutionState.UNKNOWN


def test_case_a7_job53_pattern_with_empty_status_still_stalls() -> None:
    evidence = _observations(4)
    for row in evidence["observations"]:
        row["task_status_rsp_desc"] = ""
    job = _job(evidence=evidence)
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is True


def test_adapter_write_gate_declines_when_progress_turned_terminal(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, progress_rsp_desc=_TERMINAL_PROGRESS_RSP)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "NO_LONGER_RUNNING"
    assert session.update_calls == []


def test_adapter_write_gate_declines_on_unparseable_progress(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, progress_rsp_desc="进度数据异常")
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "NO_LONGER_RUNNING"
    assert session.update_calls == []


def test_adapter_fresh_restart_proceeds_with_active_progress_despite_unknown_status(
    tmp_path, monkeypatch
) -> None:
    task = _running_task_record()
    session = _RestartSession(task, status_rsp_desc="状态异常")
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "RESTARTED"
    assert len([call for call in session.update_calls if call.get("state") == "0"]) == 1
    assert len([call for call in session.update_calls if call.get("state") == "1"]) == 1


def test_adapter_fresh_restart_proceeds_with_empty_status(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, status_rsp_desc="")
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "RESTARTED"
    assert len(session.update_calls) == 2


def test_adapter_fresh_restart_declines_when_task_completed_between_probe_and_write(
    tmp_path, monkeypatch
) -> None:
    task = _running_task_record()
    session = _RestartSession(task, status_rsp_desc="本次任务执行完毕")
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "NO_LONGER_RUNNING"
    assert session.update_calls == []


def test_no_longer_running_outcome_leaves_job_harvestable_and_reset(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "NO_LONGER_RUNNING", "restarted": False}])
    _run_pass(store, _probe_payload(), restart)
    refreshed = store.get_remote_job(job_id)
    assert refreshed["state"] == "RUNNING"
    assert int(refreshed["recovery_attempts"] or 0) == 0
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] not in {"DISABLE_SENT", "ENABLE_SENT"}
    # The declined write-ahead phase must not wedge the protocol: once the
    # task proves stalled again a later pass may still restart it fresh.
    working = _FakeRestart(
        [
            {
                "status": "RESTARTED",
                "restarted": True,
                "disable_evidence": {"confirmed_disabled": True},
                "enable_evidence": {"confirmed_enabled": True},
            }
        ]
    )
    _run_pass(store, _probe_payload(), working, now=NOW + timedelta(minutes=95))
    assert working.calls == [(job_id, None)]
    assert int(store.get_remote_job(job_id)["recovery_attempts"]) == 1


def test_stationary_observations_with_terminal_status_are_not_stall_evidence() -> None:
    evidence = _observations(4)
    for row in evidence["observations"]:
        row["task_status_rsp_desc"] = "本次任务执行完毕"
    job = _job(evidence=evidence)
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False


# ---------------------------------------------------------------------------
# Review round 3, Finding B: the deadline must reach the REAL requests.  The
# external tool hard-codes timeout=60 inside its helpers, so the adapter must
# clamp every session.get/post to the live remaining budget — asserting on
# the timeouts the fake session actually received, not on what the adapter
# was merely handed.
# ---------------------------------------------------------------------------


class _SteppingClock:
    """Fake monotonic advancing by ``step`` on every call after the first."""

    def __init__(self, start: float = 1000.0, step: float = 0.0) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def _all_recorded_timeouts(session: _RestartSession) -> list[float]:
    return [
        float(timeout)
        for timeout in [*session.post_timeouts, *session.get_timeouts]
        if timeout is not None
    ]


# ---------------------------------------------------------------------------
# Review round 3, Finding C: the per-tick probe cap must count ATTEMPTS, not
# successful observations.  A platform outage making every probe fail (the
# bridge swallows exceptions into None) must not let one tick hammer every
# candidate — failed probes consume the allowance too.
# ---------------------------------------------------------------------------


def test_case_c1_all_failing_probes_stop_at_the_attempt_cap(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_ids = _seed_candidate_jobs(store, 10)
    calls: list[int] = []

    def probe(job: dict, timeout_seconds: float | None = None):
        calls.append(int(job["id"]))
        return None

    token = store.acquire_lease(BUSINESS_DATE, "capc1", ttl_seconds=300)
    try:
        counters = run_running_recovery_pass(
            store, token, probe_remote=probe, restart_remote=None, now=NOW, can_start=lambda: True
        )
    finally:
        store.release_lease(token)
    assert len(calls) == 4
    assert counters["recovery_probe_attempts"] == 4
    assert counters["recovery_probes"] == 0


def test_case_c2_failed_probes_share_the_allowance_with_successful_ones(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_ids = _seed_candidate_jobs(store, 10)
    calls: list[int] = []
    outcomes = [None, None, "ok", "ok"]

    def probe(job: dict, timeout_seconds: float | None = None):
        calls.append(int(job["id"]))
        index = min(len(calls) - 1, len(outcomes) - 1)
        if outcomes[index] is None:
            return None
        parts = str(job["remote_identity"]).split("|")
        return _probe_payload(remote_task_id=parts[1], remote_task_number=parts[2])

    token = store.acquire_lease(BUSINESS_DATE, "capc2", ttl_seconds=300)
    try:
        counters = run_running_recovery_pass(
            store, token, probe_remote=probe, restart_remote=None, now=NOW, can_start=lambda: True
        )
    finally:
        store.release_lease(token)
    assert len(calls) == 4
    assert counters["recovery_probes"] == 2


def test_case_c3_failed_probe_records_probe_health_and_stays_harvestable(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_ids = _seed_candidate_jobs(store, 1)

    def probe(job: dict, timeout_seconds: float | None = None):
        return None

    token = store.acquire_lease(BUSINESS_DATE, "capc3", ttl_seconds=300)
    try:
        run_running_recovery_pass(
            store, token, probe_remote=probe, restart_remote=None, now=NOW, can_start=lambda: True
        )
    finally:
        store.release_lease(token)
    refreshed = store.get_remote_job(job_ids[0])
    assert refreshed["state"] == "RUNNING"
    # Round 6: a failed probe durably records probe_health (no observation,
    # no restart-protocol phase) so a persistent probe fault cannot keep the
    # job RUNNING forever; the job itself stays harvestable.
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["observations"] == []
    assert evidence["restart"] is None
    assert evidence["probe_health"]["consecutive_failures"] == 1


# ---------------------------------------------------------------------------
# Review round 3, Finding D (Important 4): a fresh restart is deferrable tail
# work — after the probe spends budget, a stalled decision must re-check the
# budget before dispatching any remote write.  Since Round 4 the already-sent
# phases reconcile in the pre-probe branch above (which carries its own
# can_start gate) and never reach this fresh-write gate at all.
# ---------------------------------------------------------------------------


def _run_flagged_pass(store, probe, restart, *, can_start):
    token = store.acquire_lease(BUSINESS_DATE, "pass-d", ttl_seconds=300)
    try:
        return run_running_recovery_pass(
            store, token, probe_remote=probe, restart_remote=restart, now=NOW, can_start=can_start
        )
    finally:
        store.release_lease(token)


def test_case_d1_budget_consumed_after_probe_defers_fresh_restart(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    budget = {"ok": True}

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
        budget["ok"] = False  # the probe consumed the remaining headroom
        return _probe_payload()

    _run_flagged_pass(store, probe, restart, can_start=lambda: budget["ok"])
    refreshed = store.get_remote_job(job_id)
    assert restart.calls == []
    assert int(refreshed["recovery_attempts"] or 0) == 0
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"] is None
    # The expensive evidence IS saved; only the write is deferred.
    assert len(evidence["observations"]) == 5


def test_case_d2_next_tick_with_budget_dispatches_the_deferred_restart(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    budget = {"ok": True}

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
        budget["ok"] = False
        return _probe_payload()

    _run_flagged_pass(store, probe, restart, can_start=lambda: budget["ok"])

    def probe_again(job: dict, timeout_seconds: float | None = None) -> dict:
        return _probe_payload()

    _run_flagged_pass(store, probe_again, restart, can_start=lambda: True)
    assert restart.calls == [(job_id, None)]
    assert int(store.get_remote_job(job_id)["recovery_attempts"]) == 1


def test_case_d3_resume_disable_sent_reconciles_despite_consumed_budget(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "seed-d3", ttl_seconds=300)
    evidence = _observations(4)
    evidence["restart"] = {
        "phase": "DISABLE_SENT",
        "phase_at": NOW.isoformat(timespec="seconds"),
        "recovery_reason": "REMOTE_RUNNING_STALLED",
    }
    store.update_remote_recovery_evidence(token, job_id, evidence)
    store.release_lease(token)
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    budget = {"ok": True}

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
        budget["ok"] = False
        return _probe_payload()

    _run_flagged_pass(store, probe, restart, can_start=lambda: budget["ok"])
    # Already-sent write: reconciliation runs even though the fresh-write
    # gate would defer; the callback receives the resume phase, never a
    # second fresh dispatch.
    assert restart.calls == [(job_id, "DISABLE_SENT")]
    assert int(store.get_remote_job(job_id)["recovery_attempts"]) == 1


def test_case_d4_resume_enable_sent_reconciles_despite_consumed_budget(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "seed-d4", ttl_seconds=300)
    evidence = _observations(4)
    evidence["restart"] = {
        "phase": "ENABLE_SENT",
        "phase_at": NOW.isoformat(timespec="seconds"),
        "recovery_reason": "REMOTE_RUNNING_STALLED",
    }
    store.update_remote_recovery_evidence(token, job_id, evidence)
    store.release_lease(token)
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    budget = {"ok": True}

    def probe(job: dict, timeout_seconds: float | None = None) -> dict:
        budget["ok"] = False
        return _probe_payload()

    _run_flagged_pass(store, probe, restart, can_start=lambda: budget["ok"])
    assert restart.calls == [(job_id, "ENABLE_SENT")]
    assert int(store.get_remote_job(job_id)["recovery_attempts"]) == 1


def test_case_b1_probe_requests_never_exceed_deadline(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session, monotonic=_SteppingClock())

    adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=8.0)

    timeouts = _all_recorded_timeouts(session)
    assert timeouts, "the probe must have issued real requests"
    assert max(timeouts) <= 8.0


def test_case_b2_restart_requests_never_exceed_deadline(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session, monotonic=_SteppingClock())

    adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=10.0)

    timeouts = _all_recorded_timeouts(session)
    assert timeouts, "the restart must have issued real requests"
    assert max(timeouts) <= 10.0


def test_case_b3_timeouts_shrink_as_budget_is_consumed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(
        tmp_path, monkeypatch, task, session, monotonic=_SteppingClock(step=1.0)
    )

    adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=12.0)

    timeouts = _all_recorded_timeouts(session)
    assert timeouts
    assert max(timeouts) <= 12.0
    assert timeouts[-1] < timeouts[0]


def test_case_b4_exhausted_deadline_sends_no_further_request(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    clock = _SteppingClock(step=0.0)
    calls = {"count": 0}

    def monotonic() -> float:
        calls["count"] += 1
        if calls["count"] <= 2:
            return clock()
        return clock.value + 100.0  # every later check sees an exhausted deadline

    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session, monotonic=monotonic)
    # Round 5 contract: the exhausted pre-write deadline is a zero-write
    # outcome instead of an escaping TimeoutError.
    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=10.0)
    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.post_calls == 0
    assert session.get_calls == 0


def test_adapter_probe_running_task_reports_progress_and_export(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    probe = adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert probe.task_id == TASK_ID
    assert probe.task_number == TASK_NUMBER
    assert probe.progress["total"] == 10
    assert probe.progress["finished"] == 2
    assert probe.export_had_result is False
    assert probe.export_count == 0
    assert probe.export_rsp_desc == NO_PRICE_RSP


def test_default_dependencies_wire_probe_and_write_gated_restart(tmp_path, monkeypatch) -> None:
    from app.services import incremental_audit_runtime as runtime_module
    data = tmp_path / "fixture-data"
    data.mkdir()
    for name in ("latest_saas_sameAudit_queryAll.curl", "latest_cnnc_query.curl", "detail_request.curl"):
        (data / name).write_text("curl https://fixture.invalid/read-only", encoding="utf-8")
    monkeypatch.setattr(runtime_module, "PROJECT_DATA_ROOT", data)
    from app.services.incremental_audit_runtime import (
        build_default_dependencies,
        runtime_paths,
    )

    paths = runtime_paths(BUSINESS_DATE, tmp_path / "runtime")
    read_only = build_default_dependencies(
        zhaoshang_backend="legacy", paths=paths, business_date=BUSINESS_DATE
    )
    assert read_only.remote_recovery_probe is not None
    assert read_only.remote_recovery_restart is None

    write_enabled = build_default_dependencies(
        enable_zhaoshang=True,
        zhaoshang_backend="legacy",
        paths=paths,
        business_date=BUSINESS_DATE,
    )
    assert write_enabled.remote_recovery_probe is not None
    assert write_enabled.remote_recovery_restart is not None


# ---------------------------------------------------------------------------
# C. Recovery-exhausted terminal path (task-book cases 14-22)
# ---------------------------------------------------------------------------


def _recovered_store(tmp_path, *, recovery_age_minutes: int = 35, post_observations: int = 2, post_progress: dict | None = None):
    """Seed a job whose single restart already completed (ENABLE_CONFIRMED)."""
    store = DailyAuditStore(tmp_path / BUSINESS_DATE / "state.sqlite3")
    job_id = _seed_running_job(store, attempts=15, age_minutes=200)
    recovery_at = NOW - timedelta(minutes=recovery_age_minutes)
    observations = []
    step = timedelta(minutes=10)
    for index in range(post_observations):
        progress = post_progress if post_progress is not None else dict(PROGRESS_STALLED)
        moment = recovery_at + step * (index + 1)
        observations.append(
            {
                "observed_at": moment.isoformat(timespec="seconds"),
                "progress": dict(progress),
                "signature": _signature(progress),
                "export_count": 0,
                "export_rsp_desc": NO_PRICE_RSP,
                "export_had_result": False,
                "task_status_rsp_desc": "执行中",
            }
        )
    evidence = {
        "version": 1,
        "observations": observations,
        "restart": {
            "phase": "ENABLE_CONFIRMED",
            "phase_at": recovery_at.isoformat(timespec="seconds"),
            "recovery_attempts": 1,
            "recovery_at": recovery_at.isoformat(timespec="seconds"),
            "recovery_reason": "REMOTE_RUNNING_STALLED",
            "disable_evidence": {"confirmed_disabled": True, "response": {"rspCode": "0000"}},
            "enable_evidence": {"confirmed_enabled": True, "response": {"rspCode": "0000"}},
            "pre_restart_signature": _signature(PROGRESS_STALLED),
        },
    }
    token = store.acquire_lease(BUSINESS_DATE, "seed-recovered", ttl_seconds=300)
    store.update_remote_recovery_evidence(token, job_id, evidence)
    with store._transaction(immediate=True) as connection:
        connection.execute("UPDATE remote_jobs SET recovery_attempts=1 WHERE id=?", (job_id,))
    store.release_lease(token)
    return store, job_id


def _seed_linked_crawl(store: DailyAuditStore, job_id: int) -> int:
    token = store.acquire_lease(BUSINESS_DATE, "seed-crawl", ttl_seconds=300)
    crawl = store.ensure_crawl_work(
        token, "lxwl", "sku-1", "https://www.66123123.com/item/1", "legacy-v1", "fp-1", source_id="doc-1"
    )
    store.link_crawl_work_to_remote_job(token, int(crawl["id"]), job_id)
    store.release_lease(token)
    return int(crawl["id"])


def test_case15_stalled_after_restart_becomes_recovery_exhausted(tmp_path) -> None:
    store, job_id = _recovered_store(tmp_path)
    crawl_id = _seed_linked_crawl(store, job_id)
    counters = _run_pass(store, _probe_payload(), None)
    assert counters["recovery_exhausted"] == 1
    refreshed = store.get_remote_job(job_id)
    assert refreshed["state"] == "RECOVERY_EXHAUSTED"
    crawl = store.get_crawl_work(crawl_id)
    assert crawl["state"] == "NO_RESULT"
    evidence = json.loads(str(crawl["result_json"]))
    assert evidence["evidence_type"] == "REMOTE_STALL_RECOVERY_EXHAUSTED"
    assert evidence["page_price_value"] is None
    assert evidence["recovery_attempt"] == 1
    assert evidence["disable_confirmed"] is True
    assert evidence["enable_confirmed"] is True
    assert evidence["export_count"] == 0
    assert evidence["remote_identity"] == "lxwl|1269504|RW-20260825150224"


def test_case14_recovery_with_export_result_stays_running(tmp_path) -> None:
    store, job_id = _recovered_store(tmp_path)
    _run_pass(store, _probe_payload(export_had_result=True, export_count=None), None)
    assert store.get_remote_job(job_id)["state"] == "RUNNING"


def test_case23_slow_job_with_post_recovery_progress_not_exhausted(tmp_path) -> None:
    moving = dict(PROGRESS_STALLED)
    observations = []
    recovery_at = NOW - timedelta(minutes=60)
    for index in range(3):
        progress = dict(moving)
        progress["finished"] = 2 + index
        observations.append(
            {
                "observed_at": (recovery_at + timedelta(minutes=15 * (index + 1))).isoformat(timespec="seconds"),
                "progress": progress,
                "signature": _signature(progress),
                "export_count": 0,
                "export_rsp_desc": NO_PRICE_RSP,
                "export_had_result": False,
                "task_status_rsp_desc": "执行中",
            }
        )
    store, job_id = _recovered_store(tmp_path, recovery_age_minutes=60, post_observations=1)
    token = store.acquire_lease(BUSINESS_DATE, "fix-obs", ttl_seconds=300)
    store.update_remote_recovery_evidence(
        token, job_id, {"version": 1, "observations": observations, "restart": json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))["restart"]}
    )
    store.release_lease(token)
    _run_pass(store, _probe_payload(progress={**PROGRESS_STALLED, "finished": 5}), None)
    assert store.get_remote_job(job_id)["state"] == "RUNNING"


def test_case16_recovery_exhausted_never_enters_harvest_candidates(tmp_path) -> None:
    store, job_id = _recovered_store(tmp_path)
    crawl_id = _seed_linked_crawl(store, job_id)
    _run_pass(store, _probe_payload(), None)
    token = store.acquire_lease(BUSINESS_DATE, "claim", ttl_seconds=300)
    claim = store.claim_harvest(token, job_id, max_attempts=10, max_elapsed_seconds=100.0)
    store.release_lease(token)
    assert claim is None
    from app.workflows.incremental_price_audit import _HARVESTABLE_REMOTE_STATES

    assert "RECOVERY_EXHAUSTED" not in _HARVESTABLE_REMOTE_STATES
    assert store.get_crawl_work(crawl_id)["state"] == "NO_RESULT"


def test_case18_recovery_exhausted_lets_ordinary_final_converge(tmp_path) -> None:
    from app.workflows.incremental_price_audit_finalize import build_frozen_audit_artifact

    store, job_id = _recovered_store(tmp_path)
    _seed_linked_crawl(store, job_id)
    _run_pass(store, _probe_payload(), None)
    _freeze(store)

    token = store.acquire_lease(BUSINESS_DATE, "artifact", ttl_seconds=300)
    try:
        built = build_frozen_audit_artifact(
            store, token, output_identity="daily-final-test", tolerate_pending=False
        )
    finally:
        store.release_lease(token)
    assert built.readiness.remote_pending == 0
    assert built.readiness.remote_invalid == 0
    assert built.readiness.remote_manual == 0
    assert built.readiness.crawl_pending == 0
    assert built.readiness.crawl_no_result == 1


def test_case19_submit_unknown_still_blocks_ordinary_final(tmp_path) -> None:
    from app.workflows.incremental_price_audit_finalize import build_frozen_audit_artifact

    store, job_id = _recovered_store(tmp_path)
    crawl_id = _seed_linked_crawl(store, job_id)
    token = store.acquire_lease(BUSINESS_DATE, "unknown", ttl_seconds=300)
    store.mark_submit_unknown(token, job_id, "legacy create-only outcome unknown")
    store.fail_crawl_work(token, crawl_id, "submit unknown", retryable=True)
    store.release_lease(token)
    _freeze(store)
    token = store.acquire_lease(BUSINESS_DATE, "artifact2", ttl_seconds=300)
    try:
        built = build_frozen_audit_artifact(store, token, output_identity="daily-final-test")
    finally:
        store.release_lease(token)
    assert built.readiness.remote_submit_unknown >= 1
    assert built.artifact is None


def test_case20_manual_required_still_blocks_ordinary_final(tmp_path) -> None:
    from app.workflows.incremental_price_audit_finalize import build_frozen_audit_artifact

    store, job_id = _recovered_store(tmp_path)
    crawl_id = _seed_linked_crawl(store, job_id)
    token = store.acquire_lease(BUSINESS_DATE, "manual", ttl_seconds=300)
    store.mark_remote_manual(token, job_id, "legacy wait-export-only requires manual review")
    store.release_lease(token)
    _freeze(store)
    token = store.acquire_lease(BUSINESS_DATE, "artifact3", ttl_seconds=300)
    try:
        built = build_frozen_audit_artifact(store, token, output_identity="daily-final-test")
    finally:
        store.release_lease(token)
    assert built.readiness.remote_manual >= 1
    assert built.artifact is None


def test_case21_plain_running_still_blocks_ordinary_final(tmp_path) -> None:
    from app.workflows.incremental_price_audit_finalize import build_frozen_audit_artifact

    store, job_id = _recovered_store(tmp_path)
    _seed_linked_crawl(store, job_id)
    _freeze(store)
    token = store.acquire_lease(BUSINESS_DATE, "artifact4", ttl_seconds=300)
    try:
        built = build_frozen_audit_artifact(store, token, output_identity="daily-final-test")
    finally:
        store.release_lease(token)
    assert built.readiness.remote_pending >= 1
    assert built.artifact is None


def test_recovery_exhausted_without_full_evidence_is_invalid(tmp_path) -> None:
    from app.workflows.incremental_price_audit_finalize import build_frozen_audit_artifact

    store, job_id = _recovered_store(tmp_path)
    _seed_linked_crawl(store, job_id)
    _run_pass(store, _probe_payload(), None)
    # Corrupt the durable restart evidence: the terminal state must not be
    # accepted by finalize without its complete recovery proof.
    token = store.acquire_lease(BUSINESS_DATE, "corrupt", ttl_seconds=300)
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    evidence["restart"].pop("disable_evidence")
    store.update_remote_recovery_evidence(token, job_id, evidence)
    store.release_lease(token)
    _freeze(store)
    token = store.acquire_lease(BUSINESS_DATE, "artifact5", ttl_seconds=300)
    try:
        built = build_frozen_audit_artifact(store, token, output_identity="daily-final-test")
    finally:
        store.release_lease(token)
    assert built.readiness.remote_invalid >= 1


def _freeze(store: DailyAuditStore) -> None:
    row = {
        "vCommodityId": "doc-1",
        "documentId": "doc-1",
        "skuId": "sku-1",
        "extLink": "https://www.66123123.com/item/1",
        "fourCategoryId": "cat",
    }
    deps = TickDependencies(
        discover=lambda timeout: DiscoveredState(
            observed_at=NOW, headers=("vCommodityId", "skuId", "extLink"), rows=[row]
        ),
    )
    deps.wall_clock = lambda: NOW
    config = TickConfig(business_date=BUSINESS_DATE)
    lock = ProcessLock(store.path.with_name("freeze-tick.lock"))
    run_incremental_tick(store, lock, config, deps)
    token = store.acquire_lease(BUSINESS_DATE, "freeze2", ttl_seconds=300)
    snapshots = store.list_snapshots(BUSINESS_DATE)
    assert snapshots
    store.freeze_snapshot(token, int(snapshots[-1]["id"]))
    store.release_lease(token)


# ---------------------------------------------------------------------------
# Review round 2, Finding 4: a legal recovery needs CONFIRMED evidence at
# every layer.  A RESTARTED claim without confirmed disable+enable must not
# burn the attempt, the store must refuse RECOVERY_EXHAUSTED without the
# full proof, and finalize must reject unconfirmed evidence.
# ---------------------------------------------------------------------------


def test_restarted_without_disable_evidence_does_not_burn_attempt(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
    _run_pass(store, _probe_payload(), restart)
    refreshed = store.get_remote_job(job_id)
    assert int(refreshed["recovery_attempts"] or 0) == 0
    assert refreshed["state"] == "RUNNING"
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] != "ENABLE_CONFIRMED"


def test_restarted_with_unconfirmed_disable_does_not_burn_attempt(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            {
                "status": "RESTARTED",
                "restarted": True,
                "disable_evidence": {"confirmed_disabled": False},
                "enable_evidence": {"confirmed_enabled": True},
            }
        ]
    )
    _run_pass(store, _probe_payload(), restart)
    refreshed = store.get_remote_job(job_id)
    assert int(refreshed["recovery_attempts"] or 0) == 0
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] != "ENABLE_CONFIRMED"


def test_restarted_with_unconfirmed_enable_does_not_burn_attempt(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            {
                "status": "RESTARTED",
                "restarted": True,
                "disable_evidence": {"confirmed_disabled": True},
                "enable_evidence": {"confirmed_enabled": False},
            }
        ]
    )
    _run_pass(store, _probe_payload(), restart)
    refreshed = store.get_remote_job(job_id)
    assert int(refreshed["recovery_attempts"] or 0) == 0
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] != "ENABLE_CONFIRMED"


def test_unconfirmed_restarted_routes_to_enable_reconciliation_not_burn(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
    counters = _run_pass(store, _probe_payload(), restart)
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    # The enable direction may have been sent: reconcile it instead of
    # trusting an unevidenced RESTARTED claim, and never count the
    # unevidenced claim as a completed restart in the metrics.
    assert evidence["restart"]["phase"] == "ENABLE_SENT"
    assert counters["recovery_restarts"] == 0


def test_crash_window_guard_requires_confirmed_evidence(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "crash4", ttl_seconds=300)
    evidence = _observations(4)
    evidence["restart"] = {
        "phase": "ENABLE_CONFIRMED",
        "phase_at": NOW.isoformat(timespec="seconds"),
        "recovery_attempts": 1,
        "recovery_at": NOW.isoformat(timespec="seconds"),
        "recovery_reason": "REMOTE_RUNNING_STALLED",
        "disable_evidence": {},
        "enable_evidence": {"confirmed_enabled": False},
    }
    store.update_remote_recovery_evidence(token, job_id, evidence)
    store.release_lease(token)
    restart = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
    _run_pass(store, _probe_payload(), restart)
    refreshed = store.get_remote_job(job_id)
    assert restart.calls == []
    assert int(refreshed["recovery_attempts"] or 0) == 0
    assert refreshed["state"] == "MANUAL_REQUIRED"


def test_mark_recovery_exhausted_rejects_incomplete_evidence(tmp_path) -> None:
    from app.services.daily_audit_store import RemoteIntentConflict

    store, job_id = _stalled_store(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "ex4", ttl_seconds=300)
    evidence = _observations(4)
    evidence["restart"] = {
        "phase": "ENABLE_CONFIRMED",
        "recovery_attempts": 1,
        "recovery_at": NOW.isoformat(timespec="seconds"),
        "disable_evidence": {"response": {"rspCode": "0000"}},
        "enable_evidence": {"response": {"rspCode": "0000"}},
    }
    store.update_remote_recovery_evidence(token, job_id, evidence)
    with store._transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE remote_jobs SET recovery_attempts=1 WHERE id=?", (job_id,)
        )
    identity = str(store.get_remote_job(job_id)["remote_identity"])
    with pytest.raises(RemoteIntentConflict):
        store.mark_remote_recovery_exhausted(
            token, job_id, remote_identity=identity, evidence=evidence
        )
    store.release_lease(token)


def test_mark_recovery_exhausted_rejects_missing_recovery_at(tmp_path) -> None:
    from app.services.daily_audit_store import RemoteIntentConflict

    store, job_id = _stalled_store(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "ex4b", ttl_seconds=300)
    evidence = _observations(4)
    evidence["restart"] = {
        "phase": "ENABLE_CONFIRMED",
        "recovery_attempts": 1,
        "disable_evidence": {"confirmed_disabled": True},
        "enable_evidence": {"confirmed_enabled": True},
    }
    store.update_remote_recovery_evidence(token, job_id, evidence)
    with store._transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE remote_jobs SET recovery_attempts=1 WHERE id=?", (job_id,)
        )
    identity = str(store.get_remote_job(job_id)["remote_identity"])
    with pytest.raises(RemoteIntentConflict):
        store.mark_remote_recovery_exhausted(
            token, job_id, remote_identity=identity, evidence=evidence
        )
    store.release_lease(token)


def _finalize_remote_row(restart_doc: dict) -> dict:
    return {
        "post_attempted": 1,
        "remote_identity": "lxwl|1269504|RW-20260825150224",
        "recovery_attempts": 1,
        "recovery_evidence_json": json.dumps(
            {"version": 1, "observations": [], "restart": restart_doc}, ensure_ascii=False
        ),
    }


_COMPLETE_RESTART_DOC = {
    "phase": "ENABLE_CONFIRMED",
    "recovery_attempts": 1,
    "recovery_at": NOW.isoformat(timespec="seconds"),
    "disable_evidence": {"confirmed_disabled": True},
    "enable_evidence": {"confirmed_enabled": True},
}


def test_finalize_validator_accepts_complete_confirmed_evidence() -> None:
    from app.workflows.incremental_price_audit_finalize import _valid_recovery_exhausted_evidence

    assert _valid_recovery_exhausted_evidence(_finalize_remote_row(dict(_COMPLETE_RESTART_DOC))) is True


def test_finalize_validator_rejects_unconfirmed_disable_evidence() -> None:
    from app.workflows.incremental_price_audit_finalize import _valid_recovery_exhausted_evidence

    doc = dict(_COMPLETE_RESTART_DOC)
    doc["disable_evidence"] = {"confirmed_disabled": False}
    assert _valid_recovery_exhausted_evidence(_finalize_remote_row(doc)) is False


def test_finalize_validator_rejects_unconfirmed_enable_evidence() -> None:
    from app.workflows.incremental_price_audit_finalize import _valid_recovery_exhausted_evidence

    doc = dict(_COMPLETE_RESTART_DOC)
    doc["enable_evidence"] = {"response": {"rspCode": "0000"}}
    assert _valid_recovery_exhausted_evidence(_finalize_remote_row(doc)) is False


def test_finalize_validator_rejects_missing_recovery_at() -> None:
    from app.workflows.incremental_price_audit_finalize import _valid_recovery_exhausted_evidence

    doc = dict(_COMPLETE_RESTART_DOC)
    del doc["recovery_at"]
    assert _valid_recovery_exhausted_evidence(_finalize_remote_row(doc)) is False


def test_status_reports_recovery_exhausted_count(tmp_path) -> None:
    from app.services.incremental_audit_status import collect_runtime_status

    store, job_id = _recovered_store(tmp_path)
    _seed_linked_crawl(store, job_id)
    _run_pass(store, _probe_payload(), None)
    status = collect_runtime_status(
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path,
    )
    assert status["remote"]["recovery_exhausted"] == 1
    assert status["remote"]["running"] == 0
    assert status["crawl"]["no_result"] == 1


# ---------------------------------------------------------------------------
# Review findings: crash windows, transient classification, probe parity
# ---------------------------------------------------------------------------


def test_c1_crash_between_evidence_and_attempt_burn_never_restarts_twice(tmp_path) -> None:
    """Doc says ENABLE_CONFIRMED but the attempt column never burned."""
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    _run_pass(store, _probe_payload(), restart)
    # Simulate the crash window: the finished evidence document committed,
    # the recovery_attempts column write lost.
    token = store.acquire_lease(BUSINESS_DATE, "crash", ttl_seconds=300)
    with store._transaction(immediate=True) as connection:
        connection.execute("UPDATE remote_jobs SET recovery_attempts=0 WHERE id=?", (job_id,))
    store.release_lease(token)
    again = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    _run_pass(store, _probe_payload(), again, now=NOW + timedelta(minutes=5))
    assert again.calls == []
    refreshed = store.get_remote_job(job_id)
    assert int(refreshed["recovery_attempts"]) == 1
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    # The first restart's audit evidence must survive un-overwritten.
    assert evidence["restart"]["phase"] == "ENABLE_CONFIRMED"


def test_i1_restart_dispatch_exception_reconciles_via_phase_not_manual() -> None:
    """A transient adapter exception must not burn MANUAL_REQUIRED."""
    from app.services.remote_running_recovery import classify_restart_exception

    fresh = classify_restart_exception(TimeoutError("network blip mid-protocol"), None)
    assert fresh == "DISABLE_UNKNOWN"
    resumed = classify_restart_exception(
        ConnectionError("reset after enable"), "ENABLE_SENT"
    )
    assert resumed == "ENABLE_UNKNOWN"
    local = classify_restart_exception(ValueError("durable legacy paths are unavailable"), None)
    assert local == "IDENTITY_CONFLICT"
    ambiguous = classify_restart_exception(RuntimeError("精确任务不唯一: task_name=x"), None)
    assert ambiguous == "IDENTITY_CONFLICT"
    other_runtime = classify_restart_exception(RuntimeError("tool internal error"), None)
    assert other_runtime == "DISABLE_UNKNOWN"


def test_i1_bridge_restart_dispatch_failure_keeps_manual_for_unreconstructable_job(tmp_path) -> None:
    from app.services.incremental_audit_runtime import legacy_zhaoshang_bridge, runtime_paths
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter

    paths = runtime_paths(BUSINESS_DATE, tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    adapter = CnncLegacyZhaoshangAdapter(mappings=[], db_env_path=None)
    _planner, _finalizer, _submitter, _harvester, _probe, restart_cb = legacy_zhaoshang_bridge(
        adapter, paths=paths, business_date=BUSINESS_DATE
    )
    durable = {
        "id": 1,
        "logical_key": "missing",
        "task_name": "cnnc_current_opt_领先未来商城_61284593895349",
        "payload_hash": "hash",
        "payload_json": json.dumps({"requests": [], "batch_state": str(paths.run_dir / "BATCH_STATE.md")}),
        "input_path": None,
        "result_path": None,
        "remote_identity": "lxwl|1269504|RW-20260825150224",
    }

    outcome = restart_cb(durable, None, 60.0)

    assert outcome["status"] == "IDENTITY_CONFLICT"
    assert outcome["restarted"] is False


def test_i2_running_probe_rejects_rows_without_count(monkeypatch, tmp_path) -> None:
    task = _running_task_record()
    session = _RestartSession(task)

    class _RowsNoCountSession(_RestartSession):
        def post(self, url, json=None, timeout=None):
            if "exportTask" in url:
                return _FakeRestartResponse(
                    {"rspCode": "0000", "rspDesc": NO_PRICE_RSP, "count": None, "data": [{"url": "https://x/1"}], "success": True}
                )
            return super().post(url, json=json, timeout=timeout)

    rows_session = _RowsNoCountSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, rows_session)
    import pytest as _pytest

    with _pytest.raises(Exception):
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)


def test_i2_running_probe_treats_embedded_download_as_result(monkeypatch, tmp_path) -> None:
    task = _running_task_record()

    class _DownloadSession(_RestartSession):
        def post(self, url, json=None, timeout=None):
            if "exportTask" in url:
                return _FakeRestartResponse(
                    {"rspCode": "0000", "rspDesc": NO_PRICE_RSP, "count": 0, "data": {"url": "https://cdn.invalid/r.xlsx"}, "success": True}
                )
            return super().post(url, json=json, timeout=timeout)

    session = _DownloadSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    probe = adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert probe.export_had_result is True
    assert probe.export_count is None


def test_m1_recovery_exhausted_job_completes_leftover_pending_crawls(tmp_path) -> None:
    store, job_id = _recovered_store(tmp_path)
    crawl_id = _seed_linked_crawl(store, job_id)
    # Job already terminal but the crawl conversion was lost mid-crash.
    token = store.acquire_lease(BUSINESS_DATE, "pre-abandon", ttl_seconds=300)
    with store._transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE remote_jobs SET state='RECOVERY_EXHAUSTED' WHERE id=?", (job_id,)
        )
    store.release_lease(token)
    assert store.get_crawl_work(crawl_id)["state"] == "PENDING"

    _run_pass(store, _probe_payload(), None)

    assert store.get_crawl_work(crawl_id)["state"] == "NO_RESULT"


# ---------------------------------------------------------------------------
# Review round 4, BLOCKER A: structural and mathematical consistency of the
# six-field progress payload must be proven BEFORE the ACTIVE branch can
# accept the row.  A contradictory payload (finished + failed > total while
# pending/running/waiting_db are also positive) is UNKNOWN and can never
# become stall evidence, no matter how stationary it looks.
# ---------------------------------------------------------------------------


_ROUND4_CONTRADICTION = {
    "total": 10,
    "pending": 1,
    "running": 1,
    "failed": 1,
    "waiting_db": 0,
    "finished": 10,
}


def test_round4_a1_contradictory_active_looking_progress_is_unknown() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    state = classify_remote_execution_state(dict(_ROUND4_CONTRADICTION), "")
    assert state is RemoteExecutionState.UNKNOWN


def test_round4_a2_contradictory_progress_never_proves_stall() -> None:
    evidence = _observations(4, progress=dict(_ROUND4_CONTRADICTION))
    job = _job(evidence=evidence)
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False


def test_round4_a3_contradictory_stall_evidence_reaches_no_remote_write(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_id = _seed_running_job(store, attempts=15, age_minutes=180)
    token = store.acquire_lease(BUSINESS_DATE, "seed-a3", ttl_seconds=300)
    store.update_remote_recovery_evidence(
        token, job_id, _observations(4, progress=dict(_ROUND4_CONTRADICTION))
    )
    store.release_lease(token)
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])

    _run_pass(store, _probe_payload(progress=dict(_ROUND4_CONTRADICTION)), restart)

    assert restart.calls == []
    assert int(store.get_remote_job(job_id)["recovery_attempts"] or 0) == 0


def test_round4_a4_legal_active_progress_is_still_active() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    state = classify_remote_execution_state(
        {"total": 10, "pending": 1, "running": 2, "failed": 0, "waiting_db": 0, "finished": 7},
        "",
    )
    assert state is RemoteExecutionState.ACTIVE


def test_round4_a5_legal_terminal_progress_is_still_terminal() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    state = classify_remote_execution_state(
        {"total": 10, "pending": 0, "running": 0, "failed": 1, "waiting_db": 0, "finished": 9},
        "",
    )
    assert state is RemoteExecutionState.TERMINAL


# ---------------------------------------------------------------------------
# Review round 4, IMPORTANT D: the terminal marker is exactly one status
# value the external tool itself interprets (auto_crawl_task.py:1048,
# rspDesc == "本次任务执行完毕").  Substring containment widened the
# evidence: a failure phrase containing the marker must stay governed by
# the structured progress payload, and an unrecognized status with
# unprovable progress stays UNKNOWN.
# ---------------------------------------------------------------------------


def test_round4_d1_exact_marker_with_active_progress_is_terminal() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    state = classify_remote_execution_state(_live_progress(running=3), "本次任务执行完毕")
    assert state is RemoteExecutionState.TERMINAL


def test_round4_d2_whitespace_padded_marker_is_terminal() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    state = classify_remote_execution_state(_live_progress(running=3), "  本次任务执行完毕  ")
    assert state is RemoteExecutionState.TERMINAL


def test_round4_d3_failure_phrase_containing_marker_is_not_terminal() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    state = classify_remote_execution_state(_live_progress(running=3), "查询本次任务执行完毕状态失败")
    assert state is RemoteExecutionState.ACTIVE


def test_round4_d4_marker_suffixed_failure_phrase_with_unknown_progress_is_unknown() -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    state = classify_remote_execution_state({}, "本次任务执行完毕失败")
    assert state is RemoteExecutionState.UNKNOWN


# ---------------------------------------------------------------------------
# Review round 4, BLOCKER B: a durable restart phase that already dispatched
# a remote write (DISABLE_SENT / DISABLE_CONFIRMED / ENABLE_SENT) is an
# EXISTING recovery protocol.  The next tick must reconcile it against the
# live remote state WITHOUT re-proving the original stall: after the disable
# the progress endpoint can never answer ACTIVE evidence again, so gating
# reconciliation on decision.stalled wedges the durable phase forever (and
# the 900-second fail-closed window inside _apply_restart never fires).
# The adapter-side no-resend contracts stay proven by
# test_adapter_restart_resume_disable_sent_never_disables_again (B2) and
# test_adapter_restart_resume_enable_sent_never_writes.
# ---------------------------------------------------------------------------


_ROUND4_UNPROVABLE_PROGRESS = {
    "total": 0,
    "pending": 0,
    "running": 0,
    "failed": 0,
    "waiting_db": 0,
    "finished": 0,
}


def _unprovable_observations(count: int, *, end: datetime = NOW, span_minutes: int = 70) -> dict:
    """Post-disable observations: the progress answer carries no usable counters."""
    step = timedelta(minutes=span_minutes) / max(1, count - 1) if count > 1 else timedelta(0)
    rows = []
    for index in range(count):
        moment = end - step * (count - 1 - index)
        rows.append(
            {
                "observed_at": moment.isoformat(timespec="seconds"),
                "progress": dict(_ROUND4_UNPROVABLE_PROGRESS),
                "signature": None,
                "export_count": 0,
                "export_rsp_desc": NO_PRICE_RSP,
                "export_had_result": False,
                "task_status_rsp_desc": "",
            }
        )
    return {"version": 1, "observations": rows, "restart": None}


def _pending_phase_store(tmp_path, phase: str, *, phase_age_minutes: int = 0):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_id = _seed_running_job(store, attempts=15, age_minutes=180)
    token = store.acquire_lease(BUSINESS_DATE, f"seed-b-{phase}", ttl_seconds=300)
    evidence = _unprovable_observations(4)
    evidence["restart"] = {
        "phase": phase,
        "phase_at": (NOW - timedelta(minutes=phase_age_minutes)).isoformat(timespec="seconds"),
        "recovery_reason": "REMOTE_RUNNING_STALLED",
    }
    store.update_remote_recovery_evidence(token, job_id, evidence)
    store.release_lease(token)
    return store, job_id


def _unprovable_probe_payload() -> dict:
    return _probe_payload(progress=dict(_ROUND4_UNPROVABLE_PROGRESS), task_status_rsp_desc="")


def test_round4_b1_disable_sent_with_unknown_progress_still_reconciles(tmp_path) -> None:
    store, job_id = _pending_phase_store(tmp_path, "DISABLE_SENT")
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])

    _run_pass(store, _unprovable_probe_payload(), restart)

    assert restart.calls == [(job_id, "DISABLE_SENT")]
    assert int(store.get_remote_job(job_id)["recovery_attempts"]) == 1


def test_round4_b3_enable_sent_with_unknown_progress_reconciles_without_resend(tmp_path) -> None:
    store, job_id = _pending_phase_store(tmp_path, "ENABLE_SENT")
    restart = _FakeRestart(
        [
            {
                "status": "RESTARTED",
                "restarted": True,
                "reconciled": True,
                "disable_evidence": {"confirmed_disabled": True},
                "enable_evidence": {"confirmed_enabled": True, "reconciled": True},
            }
        ]
    )

    _run_pass(store, _unprovable_probe_payload(), restart)

    assert restart.calls == [(job_id, "ENABLE_SENT")]
    assert int(store.get_remote_job(job_id)["recovery_attempts"]) == 1
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "ENABLE_CONFIRMED"


def test_round4_b4_disable_confirmed_continues_legal_enable_phase(tmp_path) -> None:
    store, job_id = _pending_phase_store(tmp_path, "DISABLE_CONFIRMED")
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])

    _run_pass(store, _unprovable_probe_payload(), restart)

    assert restart.calls == [(job_id, "DISABLE_CONFIRMED")]
    assert int(store.get_remote_job(job_id)["recovery_attempts"]) == 1


def test_round4_b5_disable_sent_confirm_window_expiry_fails_closed(tmp_path) -> None:
    store, job_id = _pending_phase_store(tmp_path, "DISABLE_SENT", phase_age_minutes=20)
    restart = _FakeRestart([{"status": "DISABLE_UNCONFIRMED", "restarted": False, "observed_enabled": True}])

    _run_pass(store, _unprovable_probe_payload(), restart)

    assert restart.calls == [(job_id, "DISABLE_SENT")]
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"
    # The manual terminal keeps the stale DISABLE_SENT phase in its evidence
    # document; no later pass may re-dispatch the restart callback for a job
    # that already failed closed (round-4 review Critical 1).
    again = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    _run_pass(store, _unprovable_probe_payload(), again)
    assert again.calls == []
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"


def test_round4_b6_enable_sent_confirm_window_expiry_fails_closed(tmp_path) -> None:
    store, job_id = _pending_phase_store(tmp_path, "ENABLE_SENT", phase_age_minutes=20)
    restart = _FakeRestart([{"status": "ENABLE_UNCONFIRMED", "restarted": False, "observed_enabled": False}])

    _run_pass(store, _unprovable_probe_payload(), restart)

    assert restart.calls == [(job_id, "ENABLE_SENT")]
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"
    again = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    _run_pass(store, _unprovable_probe_payload(), again)
    assert again.calls == []
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"


def test_round4_b7_ordinary_non_stalled_job_without_pending_phase_is_untouched(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    _seed_running_job(store, attempts=2, age_minutes=35)
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])

    counters = _run_pass(store, _probe_payload(), restart)

    assert restart.calls == []
    assert counters["recovery_restarts"] == 0


def test_round4_b8_pending_reconciliation_never_burns_a_second_attempt(tmp_path) -> None:
    store, job_id = _pending_phase_store(tmp_path, "DISABLE_SENT")
    first = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])

    _run_pass(store, _unprovable_probe_payload(), first)

    assert first.calls == [(job_id, "DISABLE_SENT")]
    second = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    _run_pass(store, _unprovable_probe_payload(), second, now=NOW + timedelta(minutes=2))
    assert second.calls == []
    assert int(store.get_remote_job(job_id)["recovery_attempts"]) == 1


def test_round4_b9_pending_reconciliation_defers_without_minimal_budget(tmp_path) -> None:
    store, job_id = _pending_phase_store(tmp_path, "DISABLE_SENT")
    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])

    # Pins the loop-level §13 contract: without the minimal budget for even
    # one bounded callback, reconciliation waits for the next tick (the
    # reconcile branch's own can_start check is defense-in-depth behind the
    # loop-head gate).
    _run_flagged_pass(store, _unprovable_probe_payload(), restart, can_start=lambda: False)

    assert restart.calls == []
    assert store.get_remote_job(job_id)["state"] == "RUNNING"


# Round-4 review Critical 1: the reconcile branch must key on the RUNNING
# state exactly like the fresh stalled path did (evaluate_running_stall
# requires RUNNING).  A MANUAL_REQUIRED terminal keeps its stale pending
# phase (mark_remote_manual clears nothing), and a race can leave a stale
# phase on a COMPLETED job; neither may re-dispatch a callback, corrupt
# immutable COMPLETED evidence, or crash the whole sweep.


def test_round4_b10_manual_terminal_never_redispatches_pending_phase(tmp_path) -> None:
    store, job_id = _pending_phase_store(tmp_path, "DISABLE_SENT", phase_age_minutes=20)
    first = _FakeRestart([{"status": "DISABLE_UNCONFIRMED", "restarted": False, "observed_enabled": True}])

    _run_pass(store, _unprovable_probe_payload(), first)

    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"
    second = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    counters = _run_pass(store, _unprovable_probe_payload(), second)
    assert second.calls == []
    assert counters["recovery_restarts"] == 0
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"


def test_round4_b11_completed_job_with_stale_pending_phase_is_inert(tmp_path) -> None:
    store, job_id = _pending_phase_store(tmp_path, "DISABLE_SENT")
    token = store.acquire_lease(BUSINESS_DATE, "complete-b11", ttl_seconds=300)
    with store._transaction(immediate=True) as connection:
        connection.execute("UPDATE remote_jobs SET state='COMPLETED' WHERE id=?", (job_id,))
    store.release_lease(token)

    restart = _FakeRestart([dict(_CONFIRMED_RESTART_STEP)])
    counters = _run_pass(store, _unprovable_probe_payload(), restart)

    assert restart.calls == []
    assert counters["recovery_restarts"] == 0
    assert store.get_remote_job(job_id)["state"] == "COMPLETED"


# ---------------------------------------------------------------------------
# Review round 4, BLOCKER C (adapter side): the recovery probe/restart token
# refresh happens BEFORE the bounded session exists, so the deadline must
# bound the refresh HTTP itself through the real AuthManager network seam
# (a recording opener over a real AuthManager instance, not the
# _FakeAuthManager shortcut).  The refresh may only shrink — never extend —
# the callback budget, and a refresh that times out must leave zero remote
# writes.
# ---------------------------------------------------------------------------


class _FakeAuthHttpResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _RecordingAuthOpener:
    """Records the timeout of every token-refresh HTTP request."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self._response = _FakeAuthHttpResponse(
            {"data": {"accessToken": "zs-b", "refreshToken": "zs-r2"}}
        )

    def open(self, request, timeout=None):
        self.timeouts.append(float(timeout))
        return self._response


class _TimingOutAuthOpener(_RecordingAuthOpener):
    def open(self, request, timeout=None):
        self.timeouts.append(float(timeout))
        raise TimeoutError("auth refresh transport hung")


def _real_auth_manager(tmp_path, opener):
    from scripts.auth_manager import AuthManager

    state_path = tmp_path / "auth_state.json"
    state_path.write_text(
        json.dumps(
            {
                "saas": {"access_token": "saas-a", "refresh_token": "saas-r"},
                "zhaoshang": {"access_token": "zs-a", "refresh_token": "zs-r"},
            }
        ),
        encoding="utf-8",
    )
    return AuthManager(
        state_path=state_path,
        curl_sources={"saas": (), "zhaoshang": ()},
        opener=opener,
        logger=None,
    )


def test_round4_c3_exhausted_probe_deadline_sends_no_auth_http(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    opener = _RecordingAuthOpener()
    calls = {"count": 0}

    def monotonic() -> float:
        calls["count"] += 1
        if calls["count"] <= 1:
            return 1000.0
        return 1100.0  # every later check sees an exhausted deadline

    adapter, prepared = _prepared_job(
        tmp_path, monkeypatch, task, session, monotonic=monotonic, auth=_real_auth_manager(tmp_path, opener)
    )

    with pytest.raises(TimeoutError):
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=8.0)

    assert opener.timeouts == []
    assert session.post_calls == 0
    assert session.get_calls == 0


def test_round4_c5_probe_auth_refresh_shares_the_callback_deadline(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    opener = _RecordingAuthOpener()
    adapter, prepared = _prepared_job(
        tmp_path,
        monkeypatch,
        task,
        session,
        monotonic=_SteppingClock(step=0.5),
        auth=_real_auth_manager(tmp_path, opener),
    )

    adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=12.0)

    assert opener.timeouts, "the auth refresh must have issued its HTTP request"
    assert max(opener.timeouts) <= 12.0
    session_timeouts = _all_recorded_timeouts(session)
    assert session_timeouts
    # The refresh consumed part of the budget: every later business request
    # must clamp to the post-refresh remaining time, never a fresh 12 seconds.
    assert max(session_timeouts) <= 12.0 - 0.5


def test_round4_c6_restart_auth_refresh_shares_the_callback_deadline(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    opener = _RecordingAuthOpener()
    adapter, prepared = _prepared_job(
        tmp_path,
        monkeypatch,
        task,
        session,
        monotonic=_SteppingClock(step=0.5),
        auth=_real_auth_manager(tmp_path, opener),
    )

    adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert opener.timeouts, "the auth refresh must have issued its HTTP request"
    assert max(opener.timeouts) <= 20.0
    session_timeouts = _all_recorded_timeouts(session)
    assert session_timeouts
    # lookup + status + progress + disable + enable all share the one
    # deadline with the refresh; none may see a fresh full budget.
    assert max(session_timeouts) <= 20.0 - 0.5


def test_round4_c7_auth_refresh_timeout_dispatches_no_remote_write(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    opener = _TimingOutAuthOpener()
    adapter, prepared = _prepared_job(
        tmp_path, monkeypatch, task, session, auth=_real_auth_manager(tmp_path, opener)
    )

    # Round 5 contract: a pre-write auth failure is an explicit zero-write
    # outcome, not an exception escaping the adapter (which the bridge would
    # have to guess at).
    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=10.0)

    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert opener.timeouts, "the refresh HTTP was attempted and timed out"
    assert session.update_calls == []
    assert session.post_calls == 0
    assert session.get_calls == 0


# ---------------------------------------------------------------------------
# Round 5, BLOCKER A: a pre-write failure proves no remote write was
# attempted, so it must never be classified DISABLE_UNKNOWN (which would
# leave the write-ahead DISABLE_SENT durable, force reconcile-only, and
# waste the single automatic recovery attempt on a transient fault).
# ---------------------------------------------------------------------------


def test_round5_a1_pre_write_failure_keeps_fresh_retry_across_two_ticks(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            # Tick 1: the bridge translated a pre-write auth failure into an
            # explicit PRE_WRITE_FAILED outcome; no disable was attempted.
            {
                "status": "PRE_WRITE_FAILED",
                "restarted": False,
                "diagnostic": "restart raised AuthError before any write; nothing sent",
            },
            # Tick 2: the fault is gone; the same job must still be allowed
            # its single fresh disable->enable with resume_phase=None.
            {
                "status": "RESTARTED",
                "restarted": True,
                "disable_evidence": {"confirmed_disabled": True},
                "enable_evidence": {"confirmed_enabled": True},
            },
        ]
    )
    counters = _run_pass(store, _probe_payload(), restart, now=NOW)
    # Tick 1 durable state: no write left, no attempt burned, no pending
    # write-ahead phase that would force reconcile-only next tick.
    assert restart.calls == [(job_id, None)]
    assert counters["recovery_restarts"] == 0
    job = store.get_remote_job(job_id)
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert job["state"] == "RUNNING"
    assert int(job["recovery_attempts"]) == 0
    assert evidence["restart"]["phase"] == "PRE_WRITE_FAILED"
    assert evidence["restart"]["phase"] not in ("DISABLE_SENT", "ENABLE_SENT")
    assert evidence["restart"]["diagnostic"]

    counters = _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=2))
    assert restart.calls == [(job_id, None), (job_id, None)]
    assert counters["recovery_restarts"] == 1
    job = store.get_remote_job(job_id)
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert int(job["recovery_attempts"]) == 1
    assert evidence["restart"]["phase"] == "ENABLE_CONFIRMED"


def test_round5_a1_auth_refresh_failure_returns_pre_write_failed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(
        tmp_path, monkeypatch, task, session, auth=_real_auth_manager(tmp_path, _TimingOutAuthOpener())
    )

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=10.0)

    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []
    assert session.post_calls == 0
    assert session.get_calls == 0


def test_round5_a2_lookup_timeout_is_pre_write_failed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_lookup=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []


def test_round5_a3_status_timeout_is_pre_write_failed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_status=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []


def test_round5_a4_progress_timeout_is_pre_write_failed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_progress=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []


def test_round5_a5_deadline_exhausted_before_disable_is_pre_write_failed(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    calls = {"count": 0}

    def monotonic() -> float:
        calls["count"] += 1
        if calls["count"] <= 1:
            return 1000.0
        return 2000.0  # every later deadline check sees the budget exhausted

    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session, monotonic=monotonic)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=8.0)

    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []


def test_round5_a8_pre_write_failed_does_not_burn_or_count_a_restart(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            {
                "status": "PRE_WRITE_FAILED",
                "restarted": False,
                "diagnostic": "transient pre-write fault",
            }
        ]
    )
    counters = _run_pass(store, _probe_payload(), restart, now=NOW)
    assert counters["recovery_restarts"] == 0
    assert int(store.get_remote_job(job_id)["recovery_attempts"] or 0) == 0


def test_round5_post_disable_reconcile_timeout_stays_disable_unknown(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_disable=True, fail_lookup_after_disable=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert outcome.status.value == "DISABLE_UNKNOWN"
    assert len([c for c in session.update_calls if c.get("state") == "0"]) == 1


def test_round5_post_enable_confirm_timeout_is_enable_unknown(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, fail_lookup_after_enable=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert outcome.status.value == "ENABLE_UNKNOWN"
    assert len([c for c in session.update_calls if c.get("state") == "0"]) == 1
    assert len([c for c in session.update_calls if c.get("state") == "1"]) == 1


# ---------------------------------------------------------------------------
# Round 5, IMPORTANT B: the auth refresh must run on the SAME absolute
# callback deadline as the bounded session — the opener timeout is the live
# remainder, never a fresh copy of the initial budget.
# ---------------------------------------------------------------------------


def test_round5_b5_auth_deadline_and_session_share_one_budget(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    opener = _RecordingAuthOpener()
    clock = _SteppingClock(step=1.0)
    adapter, prepared = _prepared_job(
        tmp_path, monkeypatch, task, session, monotonic=clock, auth=_real_auth_manager(tmp_path, opener)
    )

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert outcome.status.value == "RESTARTED"
    assert opener.timeouts, "the auth refresh must have issued its HTTP request"
    # The refresh ran on the same absolute deadline: after two clock ticks
    # (deadline computation + pre-auth check) its opener timeout is the live
    # remainder (18 s), never a fresh 20 s.
    assert max(opener.timeouts) <= 18.0
    session_timeouts = _all_recorded_timeouts(session)
    assert session_timeouts
    # Every business request continues on the SAME deadline: the first one
    # sees at most the post-auth remainder (16 s), and later ones shrink.
    assert max(session_timeouts) <= 16.0


def test_round5_b6_lock_overrun_past_deadline_dispatches_no_write(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    clock = _SteppingClock(step=0.0)

    class _OverrunningAuthManager:
        """Simulates an auth lock wait that blocks past the callback deadline.

        The lock itself is not hard-cancelable (documented remaining risk);
        what must hold is that nothing after it — no auth HTTP benefit and
        no remote write — executes past the deadline.
        """

        def export_environment(
            self, system, refresh=False, timeout_seconds=None, deadline=None, monotonic=None
        ):
            clock.value = clock.value + 60.0
            return {
                "BLADE_AUTH": "bearer fake",
                "COOKIE": "x-access-token=fake; refreshToken=fake",
            }

    adapter, prepared = _prepared_job(
        tmp_path, monkeypatch, task, session, monotonic=clock, auth=_OverrunningAuthManager()
    )

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=10.0)

    assert outcome.status.value == "PRE_WRITE_FAILED"
    assert session.update_calls == []
    assert session.post_calls == 0
    assert session.get_calls == 0


# ---------------------------------------------------------------------------
# Round 5, IMPORTANT C: a business-failed status/progress response
# (success=false / non-success rspCode) must never donate evidence — its
# ACTIVE-looking counters cannot authorize stall proof or the fresh-restart
# write gate, and its terminal marker text cannot be trusted either.  The
# rule mirrors the external tool's own write-endpoint check
# (auto_crawl_task.py: success is not False AND rspCode in {None, "0000"}).
# ---------------------------------------------------------------------------


def test_round5_c0_business_response_truth_table() -> None:
    from app.services.remote_running_recovery import remote_business_response_ok

    assert remote_business_response_ok({"success": True, "rspCode": "0000", "rspDesc": "x"})
    assert remote_business_response_ok({"success": True})
    # The platform legitimately omits fields; absent stays admissible.
    assert remote_business_response_ok({"rspDesc": "总任务量：10条"})
    assert remote_business_response_ok({})

    assert not remote_business_response_ok(
        {"success": False, "rspCode": "8888", "rspDesc": "总任务量：10条，待执行：4条"}
    )
    assert not remote_business_response_ok({"success": False})
    assert not remote_business_response_ok({"success": "true"})
    assert not remote_business_response_ok({"rspCode": "8888"})
    assert not remote_business_response_ok("not-a-mapping")
    assert not remote_business_response_ok(["not", "a", "mapping"])
    assert not remote_business_response_ok(None)


def test_remote_business_response_ok_has_single_shared_implementation() -> None:
    import app.services.remote_running_recovery
    import app.services.zhaoshang_remote_task

    assert (
        app.services.remote_running_recovery.remote_business_response_ok
        is app.services.zhaoshang_remote_task.remote_business_response_ok
    )
    assert (
        app.services.remote_running_recovery.PROVEN_BUSINESS_SUCCESS_RSP_CODES
        is app.services.zhaoshang_remote_task.PROVEN_BUSINESS_SUCCESS_RSP_CODES
    )
    assert (
        app.services.zhaoshang_remote_task.remote_business_response_ok(
            {"success": True, "rspCode": "0000"}
        )
        is True
    )
    assert (
        app.services.zhaoshang_remote_task.remote_business_response_ok(
            {"success": True, "rspCode": "9999"}
        )
        is False
    )
    assert app.services.zhaoshang_remote_task.remote_business_response_ok({}) is True


def test_round5_c1_failed_progress_counters_are_not_stall_evidence(tmp_path, monkeypatch) -> None:
    from app.services.legacy_zhaoshang_adapter import LegacyProbeEvidenceError

    task = _running_task_record()
    session = _RestartSession(task, progress_success=False)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    # Round 7 BLOCKER A: business-failed progress with no trusted terminal
    # status and no actual export result is UNUSABLE evidence — the adapter
    # must refuse to return it as a healthy probe Mapping.
    with pytest.raises(LegacyProbeEvidenceError):
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    # A signature-less observation (legacy/injected shapes) still never
    # proves a stall, so no restart write may be dispatched for it.
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
    payload = _probe_payload(progress={}, export_rsp_desc="查询失败")
    _run_pass(store, payload, restart)
    assert restart.calls == []
    assert int(store.get_remote_job(job_id)["recovery_attempts"] or 0) == 0


def test_round5_c2_healthy_active_progress_still_active(tmp_path, monkeypatch) -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    task = _running_task_record()
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    probe = adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert probe.progress["total"] == 10
    assert (
        classify_remote_execution_state(probe.progress, probe.task_status_rsp_desc)
        is RemoteExecutionState.ACTIVE
    )


def test_round5_c3_failed_status_terminal_text_not_trusted(tmp_path, monkeypatch) -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    task = _running_task_record()
    session = _RestartSession(
        task,
        status_success=False,
        status_rsp_desc="本次任务执行完毕",
    )
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    probe = adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    # The marker inside a business-failed response is stripped: the healthy
    # progress alone decides, and it says ACTIVE.
    assert probe.task_status_rsp_desc == ""
    assert (
        classify_remote_execution_state(probe.progress, probe.task_status_rsp_desc)
        is RemoteExecutionState.ACTIVE
    )


def test_round5_c4_write_time_failed_progress_dispatches_no_write(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, progress_success=False)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    # The write gate re-queried progress and got a business failure: the
    # state is unprovable, so the zero-write decline fires.
    assert outcome.status.value == "NO_LONGER_RUNNING"
    assert session.update_calls == []


def test_round5_c5_non_mapping_payload_is_unknown(tmp_path, monkeypatch) -> None:
    from app.services.legacy_zhaoshang_adapter import LegacyProbeEvidenceError

    task = _running_task_record()
    session = _RestartSession(task, progress_payload=["not", "a", "mapping"])
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    # Round 7 BLOCKER A: a non-mapping progress payload with no terminal
    # status fallback and no export result is unusable evidence, surfaced as
    # the typed unusable-evidence failure instead of an empty probe Mapping.
    with pytest.raises(LegacyProbeEvidenceError):
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)


# ---------------------------------------------------------------------------
# Round 5 review follow-up: a pre-write failure is retryable, but a
# PERSISTENT pre-write fault (expired refreshToken, missing external tool)
# must eventually fail closed to manual review instead of retrying forever —
# the pre-Round-5 behavior escalated within the 900 s confirmation window.
# A consecutive-failure cap covers every permanent cause without guessing
# from the exception type.
# ---------------------------------------------------------------------------


def test_round5_persistent_pre_write_failures_fail_closed_to_manual(tmp_path) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES

    store, job_id = _stalled_store(tmp_path)
    failure = {"status": "PRE_WRITE_FAILED", "restarted": False, "diagnostic": "auth expired"}
    restart = _FakeRestart([failure] * RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES)

    for index in range(RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES):
        _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=3 * index))
        # Before the cap the job must stay fresh-eligible RUNNING.
        if index < RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES - 1:
            assert store.get_remote_job(job_id)["state"] == "RUNNING"

    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    assert int(job["recovery_attempts"] or 0) == 0
    # Every attempt stayed a fresh restart (resume_phase=None): the fault
    # was provably pre-write each time, so no side effect ever left.
    assert restart.calls == [(job_id, None)] * RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES


def test_round5_transient_pre_write_failures_reset_after_success(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            {"status": "PRE_WRITE_FAILED", "restarted": False, "diagnostic": "blip 1"},
            {"status": "PRE_WRITE_FAILED", "restarted": False, "diagnostic": "blip 2"},
            {
                "status": "RESTARTED",
                "restarted": True,
                "disable_evidence": {"confirmed_disabled": True},
                "enable_evidence": {"confirmed_enabled": True},
            },
        ]
    )

    _run_pass(store, _probe_payload(), restart, now=NOW)
    _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=2))
    job = store.get_remote_job(job_id)
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "PRE_WRITE_FAILED"
    assert evidence["restart"]["pre_write_failures"] == 2

    _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=4))
    job = store.get_remote_job(job_id)
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert job["state"] == "RUNNING"
    assert int(job["recovery_attempts"]) == 1
    assert evidence["restart"]["phase"] == "ENABLE_CONFIRMED"
    assert "pre_write_failures" not in evidence["restart"]


def test_round5_pre_write_failed_never_withdraws_a_real_pending_phase(tmp_path) -> None:
    # Defense-in-depth: a misbehaving injected runner returning
    # PRE_WRITE_FAILED while a genuine dispatched phase is durable must not
    # withdraw that write-ahead (which would authorize a second write).
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            {"status": "DISABLE_UNKNOWN", "restarted": False, "observed_enabled": True},
            {"status": "PRE_WRITE_FAILED", "restarted": False, "diagnostic": "injected lie"},
        ]
    )
    _run_pass(store, _probe_payload(), restart, now=NOW)
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "DISABLE_SENT"

    _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=2))
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "DISABLE_SENT"


# ---------------------------------------------------------------------------
# Review round 6, BLOCKER A: the 5th consecutive PRE_WRITE_FAILED must leave
# truthful terminal evidence.  The write-ahead DISABLE_SENT persisted before
# the dispatch is a pending remote write that never happened; keeping it
# durable on a MANUAL_REQUIRED job would let a later human requeue mistake
# it for this protocol's own disable and auto-enable an externally disabled
# task.
# ---------------------------------------------------------------------------


def _run_probe_pass(store, probe, restart, *, now: datetime = NOW) -> dict:
    """Like _run_pass but takes a callable probe (failure envelopes)."""
    token = store.acquire_lease(BUSINESS_DATE, "pass-probe", ttl_seconds=300)
    try:
        return run_running_recovery_pass(
            store, token, probe_remote=probe, restart_remote=restart, now=now, can_start=lambda: True
        )
    finally:
        store.release_lease(token)


class _NeverRestart:
    """Any call is a contract violation: paths that provably wrote nothing
    (pre-write failures, probe failures) must dispatch no remote write."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    def __call__(self, job: dict, resume_phase: str | None) -> dict:
        self.calls.append((int(job["id"]), resume_phase))
        raise AssertionError("no remote write may be dispatched on this path")


class _DangerAwareRestart:
    """Mimics the adapter resume hazard: a resumed DISABLE_SENT phase on an
    observed-disabled task is treated as "our own disable succeeded" and
    proceeds straight to the enable write."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []
        self.enable_attempts = 0

    def __call__(self, job: dict, resume_phase: str | None) -> dict:
        self.calls.append((int(job["id"]), resume_phase))
        if resume_phase in {"DISABLE_SENT", "DISABLE_CONFIRMED"}:
            self.enable_attempts += 1
            return {
                "status": "RESTARTED",
                "restarted": True,
                "disable_evidence": {"confirmed_disabled": True},
                "enable_evidence": {"confirmed_enabled": True},
            }
        return {"status": "EXTERNALLY_DISABLED", "restarted": False, "observed_enabled": False}


def test_round6_a1_fifth_pre_write_failure_keeps_truthful_terminal_evidence(tmp_path) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES

    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            {"status": "PRE_WRITE_FAILED", "restarted": False, "diagnostic": f"pre-write fault {index}"}
            for index in range(1, RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES + 1)
        ]
    )

    for index in range(RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES):
        _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=3 * index))
        job = store.get_remote_job(job_id)
        evidence = json.loads(str(job["recovery_evidence_json"]))
        if index < RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES - 1:
            assert job["state"] == "RUNNING"
            assert evidence["restart"]["phase"] == "PRE_WRITE_FAILED"
            assert evidence["restart"]["pre_write_failures"] == index + 1

    job = store.get_remote_job(job_id)
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert job["state"] == "MANUAL_REQUIRED"
    assert int(job["recovery_attempts"]) == 0
    assert evidence["restart"]["phase"] == "PRE_WRITE_FAILED"
    assert evidence["restart"]["phase"] not in ("DISABLE_SENT", "ENABLE_SENT")
    assert evidence["restart"]["pre_write_failures"] == RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES
    assert evidence["restart"]["failed_status"] == "PRE_WRITE_FAILED"
    assert evidence["restart"]["diagnostic"] == "pre-write fault 5"
    assert job["error_text"]
    assert "no remote write was dispatched" in str(job["error_text"])


def test_round6_a2_five_adapter_pre_write_failures_dispatch_zero_writes(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(
        tmp_path, monkeypatch, task, session, auth=_real_auth_manager(tmp_path, _TimingOutAuthOpener())
    )

    for _ in range(5):
        outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=10.0)
        assert outcome.status.value == "PRE_WRITE_FAILED"

    assert session.update_calls == []
    assert session.post_calls == 0
    assert session.get_calls == 0


def test_round6_a4_terminal_diagnostic_is_the_fifth_real_failure(tmp_path) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES

    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [
            {"status": "PRE_WRITE_FAILED", "restarted": False, "diagnostic": f"fault {index}"}
            for index in range(1, RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES + 1)
        ]
    )

    for index in range(RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES):
        _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=3 * index))

    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence["restart"]["diagnostic"] == "fault 5"
    assert evidence["restart"]["phase"] == "PRE_WRITE_FAILED"


def test_round6_requeue_after_pre_write_cap_never_resumes_stale_phase(tmp_path) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES

    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [{"status": "PRE_WRITE_FAILED", "restarted": False, "diagnostic": "auth expired"}]
    )
    for index in range(RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES):
        _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=3 * index))
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "PRE_WRITE_FAILED"

    # The project's real human requeue path: MANUAL_REQUIRED -> SUBMITTED,
    # then the ordinary harvest lifecycle returns the job to RUNNING.
    token = store.acquire_lease(BUSINESS_DATE, "requeue", ttl_seconds=300)
    store.requeue_manual_harvest_job(
        token, job_id, remote_identity="lxwl|1269504|RW-20260825150224"
    )
    store.reconcile_remote_job(
        token, job_id, remote_identity="lxwl|1269504|RW-20260825150224", state="RUNNING"
    )
    store.release_lease(token)
    assert store.get_remote_job(job_id)["state"] == "RUNNING"

    # The next pass must treat the job as a FRESH recovery: the truthful
    # PRE_WRITE_FAILED record is not a pending remote write phase.
    danger = _DangerAwareRestart()

    def probe(job: dict) -> dict:
        return _probe_payload()

    _run_probe_pass(store, probe, danger, now=NOW + timedelta(minutes=30))
    assert [resume_phase for _job_id, resume_phase in danger.calls] == [None]
    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] not in ("DISABLE_SENT", "ENABLE_SENT")


def test_round6_requeued_externally_disabled_task_is_never_auto_enabled(tmp_path) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES

    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart(
        [{"status": "PRE_WRITE_FAILED", "restarted": False, "diagnostic": "auth expired"}]
    )
    for index in range(RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES):
        _run_pass(store, _probe_payload(), restart, now=NOW + timedelta(minutes=3 * index))
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"

    def requeue_to_running() -> None:
        token = store.acquire_lease(BUSINESS_DATE, "requeue-x", ttl_seconds=300)
        store.requeue_manual_harvest_job(
            token, job_id, remote_identity="lxwl|1269504|RW-20260825150224"
        )
        store.reconcile_remote_job(
            token, job_id, remote_identity="lxwl|1269504|RW-20260825150224", state="RUNNING"
        )
        store.release_lease(token)

    def probe(job: dict) -> dict:
        return _probe_payload()

    danger = _DangerAwareRestart()
    # TWO full requeue cycles: after the first fresh EXTERNALLY_DISABLED the
    # job goes manual again; a second requeue must still never resume a
    # stale pending phase into the enable direction.
    for cycle in range(2):
        requeue_to_running()
        assert store.get_remote_job(job_id)["state"] == "RUNNING"
        _run_probe_pass(store, probe, danger, now=NOW + timedelta(minutes=30 * (cycle + 1)))
        assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"

    assert danger.enable_attempts == 0
    assert all(resume_phase is None for _job_id, resume_phase in danger.calls)
    job = store.get_remote_job(job_id)
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] not in ("DISABLE_SENT", "ENABLE_SENT")


# ---------------------------------------------------------------------------
# Review round 6, IMPORTANT B: persistent recovery-probe failures must be
# durably counted and eventually fail closed.  Probe health lives beside
# (never inside) restart.phase: a probe failure has not entered the restart
# protocol, and only the RUNNING-stall recovery probe stage feeds the
# counter (never harvest/submit/priority/discovery).
# ---------------------------------------------------------------------------


def test_round6_b1_first_probe_failure_is_durable_and_running(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)

    def failing_probe(job: dict):
        return None  # legacy injected-callback failure shape

    counters = _run_probe_pass(store, failing_probe, _NeverRestart())
    assert counters["recovery_probe_attempts"] == 1
    assert counters["recovery_probes"] == 0
    job = store.get_remote_job(job_id)
    assert job["state"] == "RUNNING"
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert evidence["probe_health"]["consecutive_failures"] == 1
    assert evidence["probe_health"]["last_failure_reason"]
    # A probe failure never enters the restart protocol.
    assert evidence["restart"] is None


def test_round6_b1b_bridge_failure_envelope_is_durable(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)

    def failing_probe(job: dict):
        return {"probe_failed": True, "diagnostic": "probe raised TimeoutError"}

    counters = _run_probe_pass(store, failing_probe, _NeverRestart())
    assert counters["recovery_probes"] == 0
    job = store.get_remote_job(job_id)
    assert job["state"] == "RUNNING"
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert evidence["probe_health"]["consecutive_failures"] == 1
    assert evidence["probe_health"]["last_failure_reason"] == "probe raised TimeoutError"
    assert evidence["restart"] is None


def test_round6_b2_four_probe_failures_still_running(tmp_path) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES

    store, job_id = _stalled_store(tmp_path)

    def failing_probe(job: dict):
        return {"probe_failed": True, "diagnostic": "probe raised AuthError"}

    for index in range(RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES - 1):
        _run_probe_pass(store, failing_probe, _NeverRestart(), now=NOW + timedelta(minutes=3 * index))
    job = store.get_remote_job(job_id)
    assert job["state"] == "RUNNING"
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert evidence["probe_health"]["consecutive_failures"] == RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES - 1


def test_round6_b3_fifth_probe_failure_fails_closed_to_manual(tmp_path) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES

    store, job_id = _stalled_store(tmp_path)
    restart = _NeverRestart()

    def failing_probe(job: dict):
        return {"probe_failed": True, "diagnostic": "probe raised AuthError"}

    for index in range(RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES):
        _run_probe_pass(store, failing_probe, restart, now=NOW + timedelta(minutes=3 * index))

    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert evidence["probe_health"]["consecutive_failures"] == RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES
    # Never fabricated into a pending remote-write phase: no restart was
    # ever attempted, let alone dispatched.
    assert evidence["restart"] is None
    assert restart.calls == []
    assert int(job["recovery_attempts"]) == 0
    assert "no restart was attempted" in str(job["error_text"])


def test_round6_legacy_evidence_without_probe_health_defaults_to_zero() -> None:
    from app.services.remote_running_recovery import decode_recovery_evidence

    decoded = decode_recovery_evidence(
        json.dumps({"version": 1, "observations": [], "restart": None})
    )
    assert decoded["probe_health"] is None


def test_round6_b4_probe_success_resets_failure_streak(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)

    def failing_probe(job: dict):
        return {"probe_failed": True, "diagnostic": "probe raised TimeoutError"}

    def probe(job: dict):
        return _probe_payload()

    _run_probe_pass(store, failing_probe, _NeverRestart(), now=NOW)
    _run_probe_pass(store, failing_probe, _NeverRestart(), now=NOW + timedelta(minutes=3))
    job = store.get_remote_job(job_id)
    assert json.loads(str(job["recovery_evidence_json"]))["probe_health"]["consecutive_failures"] == 2

    counters = _run_probe_pass(store, probe, None, now=NOW + timedelta(minutes=6))
    assert counters["recovery_probes"] == 1
    job = store.get_remote_job(job_id)
    evidence = json.loads(str(job["recovery_evidence_json"]))
    assert evidence.get("probe_health") is None
    assert len(evidence["observations"]) == 5  # 4 seeded + the fresh success


def test_round6_b5_probe_failure_count_is_consecutive_not_lifetime(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)

    def failing_probe(job: dict):
        return {"probe_failed": True, "diagnostic": "blip"}

    def probe(job: dict):
        return _probe_payload()

    _run_probe_pass(store, failing_probe, None, now=NOW)
    _run_probe_pass(store, failing_probe, None, now=NOW + timedelta(minutes=3))
    _run_probe_pass(store, probe, None, now=NOW + timedelta(minutes=6))  # reset
    _run_probe_pass(store, failing_probe, None, now=NOW + timedelta(minutes=9))

    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence["probe_health"]["consecutive_failures"] == 1
    assert evidence["restart"] is None


def test_round6_b7_probe_failure_cap_per_tick_is_four(tmp_path) -> None:
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    _seed_candidate_jobs(store, 10)
    calls: list[int] = []

    def failing_probe(job: dict):
        calls.append(int(job["id"]))
        return {"probe_failed": True, "diagnostic": "probe raised TimeoutError"}

    token = store.acquire_lease(BUSINESS_DATE, "b7", ttl_seconds=300)
    try:
        counters = run_running_recovery_pass(
            store, token, probe_remote=failing_probe, restart_remote=None, now=NOW, can_start=lambda: True
        )
    finally:
        store.release_lease(token)
    assert len(calls) == 4
    assert counters["recovery_probe_attempts"] == 4
    assert counters["recovery_probes"] == 0


def test_round6_b8_probe_failures_do_not_break_fair_progression(tmp_path) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES

    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_ids = _seed_candidate_jobs(store, 10)

    def failing_probe(job: dict):
        return {"probe_failed": True, "diagnostic": "probe raised TimeoutError"}

    # Five ticks: the first four candidates (id order) burn their whole
    # consecutive budget and go manual; later candidates stay untouched.
    for index in range(RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES):
        _run_probe_pass(store, failing_probe, None, now=NOW + timedelta(minutes=3 * index))
    states = {job_id: store.get_remote_job(job_id) for job_id in job_ids}
    assert all(states[job_id]["state"] == "MANUAL_REQUIRED" for job_id in job_ids[:4])
    assert all(states[job_id]["state"] == "RUNNING" for job_id in job_ids[4:])
    assert all(states[job_id]["recovery_evidence_json"] is None for job_id in job_ids[4:])

    # The next tick's four-probe allowance reaches the NEXT candidates.
    probed: list[int] = []

    def recording_probe(job: dict):
        probed.append(int(job["id"]))
        return {"probe_failed": True, "diagnostic": "probe raised TimeoutError"}

    _run_probe_pass(store, recording_probe, None, now=NOW + timedelta(minutes=30))
    assert probed == job_ids[4:8]


def test_round6_probe_failure_manual_blocks_ordinary_finalize(tmp_path) -> None:
    from app.workflows.incremental_price_audit_finalize import build_frozen_audit_artifact
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES

    store, job_id = _stalled_store(tmp_path)
    _seed_linked_crawl(store, job_id)

    def failing_probe(job: dict):
        return {"probe_failed": True, "diagnostic": "probe raised AuthError"}

    for index in range(RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES):
        _run_probe_pass(store, failing_probe, None, now=NOW + timedelta(minutes=3 * index))
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"

    _freeze(store)
    token = store.acquire_lease(BUSINESS_DATE, "b-final", ttl_seconds=300)
    try:
        built = build_frozen_audit_artifact(store, token, output_identity="daily-final-test")
    finally:
        store.release_lease(token)
    assert built.readiness.remote_manual >= 1
    assert built.artifact is None


def test_round6_recovery_probe_bridge_reports_failure_envelope(tmp_path) -> None:
    from app.services.incremental_audit_runtime import legacy_zhaoshang_bridge, runtime_paths
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter

    paths = runtime_paths(BUSINESS_DATE, tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    adapter = CnncLegacyZhaoshangAdapter(mappings=[], db_env_path=None)
    _planner, _finalizer, _submitter, _harvester, probe_cb, _restart_cb = legacy_zhaoshang_bridge(
        adapter, paths=paths, business_date=BUSINESS_DATE
    )
    durable = {
        "id": 1,
        "logical_key": "missing",
        "task_name": "cnnc_current_opt_领先未来商城_61284593895349",
        "payload_hash": "hash",
        "payload_json": json.dumps({"requests": [], "batch_state": str(paths.run_dir / "BATCH_STATE.md")}),
        "input_path": None,
        "result_path": None,
        "remote_identity": "lxwl|1269504|RW-20260825150224",
    }

    outcome = probe_cb(durable, 60.0)

    assert outcome["probe_failed"] is True
    assert str(outcome["diagnostic"]).startswith("probe raised ")


# ---------------------------------------------------------------------------
# Round 7, BLOCKER A: a transport-success Mapping is NOT a usable recovery
# probe.  The adapter owns the usability contract (the layer with the least
# information loss): a probe that completed with no reliable recovery
# evidence — business-failed/malformed/contradictory progress AND no trusted
# terminal status AND no actual export result — must surface as a typed
# unusable-evidence failure, never as a healthy Mapping that clears the
# durable probe-failure streak.  Reliable evidence (ACTIVE / TERMINAL
# progress, terminal status marker, export_had_result) is a SUCCESS probe
# even when the export answer is a legal no-price count=0.
# ---------------------------------------------------------------------------

_BRIDGE_REQUEST = {
    "mall_key": "lxwl",
    "mall_source_id": "34",
    "mall_name": "领先未来商城",
    "sku_id": "sku-1",
    "url": "https://www.66123123.com/item/1",
    "backend_version": "legacy-v1",
}


def _bridge_probe(tmp_path, monkeypatch, task, session, tool=None):
    """Full production chain for the recovery probe: adapter (fake tool) -> bridge.

    Returns ``(probe_cb, durable)`` where ``durable`` is a reconstructable
    durable job row, so the test exercises reconstruct -> probe_running_task
    -> envelope translation exactly like the tick does.  ``tool`` overrides
    the injected tool module (identity-conflict shapes).
    """
    from app.services.incremental_audit_runtime import legacy_zhaoshang_bridge, runtime_paths
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter

    paths = runtime_paths(BUSINESS_DATE, tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    batch_state = paths.run_dir / "BATCH_STATE.md"
    batch_state.write_text("# batch\n", encoding="utf-8")
    adapter = CnncLegacyZhaoshangAdapter(mappings=[], db_env_path=None, auth_manager=_FakeAuthManager())
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        [type("Request", (), dict(_BRIDGE_REQUEST, identity=tuple(_BRIDGE_REQUEST.values())))()],
        paths.run_dir / "input.xlsx",
        paths.run_dir / "result.xlsx",
        "cnnc_current_opt_领先未来商城_61284593895349",
        batch_state,
    )
    monkeypatch.setattr(
        type(adapter),
        "_load_module",
        classmethod(lambda cls, path, name: tool or _restart_tool_module(task, session)),
    )
    _planner, _finalizer, _submitter, _harvester, probe_cb, _restart_cb = legacy_zhaoshang_bridge(
        adapter, paths=paths, business_date=BUSINESS_DATE
    )
    durable = {
        "id": 1,
        "logical_key": prepared.logical_job_key,
        "task_name": "cnnc_current_opt_领先未来商城_61284593895349",
        "payload_hash": prepared.payload_sha256,
        "payload_json": json.dumps({"requests": [dict(_BRIDGE_REQUEST)], "batch_state": str(batch_state)}),
        "input_path": str(prepared.template_path),
        "result_path": str(prepared.result_path),
        "remote_identity": "lxwl|1269504|RW-20260825150224",
    }
    return probe_cb, durable


class _DownloadExportSession(_RestartSession):
    """exportTask answers with a real download URL instead of no-price."""

    def post(self, url, json=None, timeout=None):
        if "exportTask" in url:
            return _FakeRestartResponse(
                {"data": {"url": "https://files.invalid/export.xlsx"}, "success": True}
            )
        return super().post(url, json=json, timeout=timeout)


def test_round7_a1_business_failed_progress_chain_is_durable_failure(tmp_path, monkeypatch) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES

    task = _running_task_record()
    session = _RestartSession(
        task,
        progress_success=False,
        status_payload={"success": False, "rspCode": "8888", "rspDesc": "查询失败"},
    )
    probe_cb, durable = _bridge_probe(tmp_path, monkeypatch, task, session)

    outcome = probe_cb(durable, 60.0)
    # HTTP transport succeeded, but the probe carries no usable execution
    # evidence: business-failed progress + business-failed status + count=0
    # export.  The bridge must surface a typed unusable-evidence failure —
    # today it returns a healthy-looking Mapping.
    assert outcome["probe_failed"] is True
    assert outcome["failure_kind"] == "UNUSABLE_EVIDENCE"

    store, job_id = _stalled_store(tmp_path / "state")
    restart = _NeverRestart()
    for index in range(RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES):
        _run_probe_pass(store, lambda job: outcome, restart, now=NOW + timedelta(minutes=3 * index))
        if index < RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES - 1:
            job = store.get_remote_job(job_id)
            assert job["state"] == "RUNNING"
            evidence = json.loads(str(job["recovery_evidence_json"]))
            assert evidence["probe_health"]["consecutive_failures"] == index + 1
    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    assert restart.calls == []
    assert int(job["recovery_attempts"]) == 0


def test_round7_a2_failed_progress_plus_healthy_terminal_status_is_usable(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(
        task,
        progress_success=False,
        status_rsp_desc="本次任务执行完毕",
    )
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    probe = adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    # The trusted terminal marker alone is sufficient recovery evidence.
    assert probe.task_status_rsp_desc == "本次任务执行完毕"
    assert probe.progress.get("total") is None

    store, job_id = _stalled_store(tmp_path / "state")
    payload = {
        "remote_task_id": probe.task_id,
        "remote_task_number": probe.task_number,
        "remote_mall": probe.mall,
        "progress": dict(probe.progress),
        "export_count": probe.export_count,
        "export_rsp_desc": probe.export_rsp_desc,
        "export_had_result": probe.export_had_result,
        "task_status_rsp_desc": probe.task_status_rsp_desc,
    }
    _run_probe_pass(store, lambda job: payload, _NeverRestart())
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence.get("probe_health") is None
    assert len(evidence["observations"]) == 5


def test_round7_a3_failed_progress_plus_export_result_is_usable(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _DownloadExportSession(task, progress_success=False)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    probe = adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert probe.export_had_result is True
    assert probe.progress.get("total") is None

    store, job_id = _stalled_store(tmp_path / "state")
    payload = {
        "remote_task_id": probe.task_id,
        "remote_task_number": probe.task_number,
        "remote_mall": probe.mall,
        "progress": dict(probe.progress),
        "export_count": probe.export_count,
        "export_rsp_desc": probe.export_rsp_desc,
        "export_had_result": probe.export_had_result,
        "task_status_rsp_desc": probe.task_status_rsp_desc,
    }
    _run_probe_pass(store, lambda job: payload, _NeverRestart())
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence.get("probe_health") is None
    assert len(evidence["observations"]) == 5


def test_round7_a5_contradictory_progress_without_fallback_is_unusable(tmp_path, monkeypatch) -> None:
    from app.services.legacy_zhaoshang_adapter import LegacyProbeEvidenceError

    task = _running_task_record()
    session = _RestartSession(
        task,
        progress_rsp_desc="总任务量：10条，待执行：0条，执行中：0条，失败：5条，待入库：0条，执行完成：10条",
    )
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    with pytest.raises(LegacyProbeEvidenceError):
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)


def test_round7_a6_healthy_active_progress_is_usable(tmp_path, monkeypatch) -> None:
    from app.services.remote_running_recovery import RemoteExecutionState, classify_remote_execution_state

    task = _running_task_record()
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    probe = adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert (
        classify_remote_execution_state(probe.progress, probe.task_status_rsp_desc)
        is RemoteExecutionState.ACTIVE
    )


def test_round7_a7_healthy_terminal_progress_is_usable_success_probe(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, progress_rsp_desc=_TERMINAL_PROGRESS_RSP)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    probe = adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert probe.progress["finished"] == 10

    store, job_id = _stalled_store(tmp_path / "state")
    payload = {
        "remote_task_id": probe.task_id,
        "remote_task_number": probe.task_number,
        "remote_mall": probe.mall,
        "progress": dict(probe.progress),
        "export_count": probe.export_count,
        "export_rsp_desc": probe.export_rsp_desc,
        "export_had_result": probe.export_had_result,
        "task_status_rsp_desc": probe.task_status_rsp_desc,
    }
    _run_probe_pass(store, lambda job: payload, _NeverRestart())
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    # TERMINAL is success read evidence, never a probe failure.
    assert evidence.get("probe_health") is None
    assert len(evidence["observations"]) == 5


def test_round7_a9_probe_health_reset_by_actual_export_result(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _DownloadExportSession(task, progress_success=False)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    probe = adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)
    assert probe.export_had_result is True

    store, job_id = _stalled_store(tmp_path / "state")
    failing = lambda job: {"probe_failed": True, "diagnostic": "probe raised TimeoutError"}
    _run_probe_pass(store, failing, None, now=NOW)
    _run_probe_pass(store, failing, None, now=NOW + timedelta(minutes=3))
    _run_probe_pass(store, failing, None, now=NOW + timedelta(minutes=6))
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence["probe_health"]["consecutive_failures"] == 3

    payload = {
        "remote_task_id": probe.task_id,
        "remote_task_number": probe.task_number,
        "remote_mall": probe.mall,
        "progress": dict(probe.progress),
        "export_count": probe.export_count,
        "export_rsp_desc": probe.export_rsp_desc,
        "export_had_result": probe.export_had_result,
        "task_status_rsp_desc": probe.task_status_rsp_desc,
    }
    _run_probe_pass(store, lambda job: payload, None, now=NOW + timedelta(minutes=9))
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence.get("probe_health") is None


def test_round7_a10_failed_status_terminal_text_plus_active_progress_is_active(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(
        task,
        status_success=False,
        status_rsp_desc="本次任务执行完毕",
    )
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    probe = adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)

    assert probe.task_status_rsp_desc == ""
    assert probe.progress["total"] == 10

    store, job_id = _stalled_store(tmp_path / "state")
    payload = {
        "remote_task_id": probe.task_id,
        "remote_task_number": probe.task_number,
        "remote_mall": probe.mall,
        "progress": dict(probe.progress),
        "export_count": probe.export_count,
        "export_rsp_desc": probe.export_rsp_desc,
        "export_had_result": probe.export_had_result,
        "task_status_rsp_desc": probe.task_status_rsp_desc,
    }
    # No restart callback: this test is about probe usability, not the write
    # gate (a healthy ACTIVE no-price observation may legitimately authorize
    # the stall protocol on a stalled seed).
    _run_probe_pass(store, lambda job: payload, None)
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    # Healthy ACTIVE progress keeps the probe usable despite the failed status.
    assert evidence.get("probe_health") is None


def test_round7_a11_unusable_probe_is_a_failure_not_a_streak_reset(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(
        task,
        progress_success=False,
        status_payload={"success": False, "rspCode": "8888", "rspDesc": "查询失败"},
    )
    probe_cb, durable = _bridge_probe(tmp_path, monkeypatch, task, session)
    unusable = probe_cb(durable, 60.0)

    store, job_id = _stalled_store(tmp_path / "state")
    failing = lambda job: {"probe_failed": True, "diagnostic": "probe raised TimeoutError"}
    _run_probe_pass(store, failing, None, now=NOW)
    _run_probe_pass(store, failing, None, now=NOW + timedelta(minutes=3))
    _run_probe_pass(store, lambda job: unusable, None, now=NOW + timedelta(minutes=6))
    # The unusable probe must extend the streak to 3 — today the raw Mapping
    # clears it instead.
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence["probe_health"]["consecutive_failures"] == 3

    _run_probe_pass(store, lambda job: _probe_payload(), None, now=NOW + timedelta(minutes=9))
    evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))
    assert evidence.get("probe_health") is None


# ---------------------------------------------------------------------------
# Round 7, IMPORTANT B: a PROVEN exact remote identity conflict must fail
# closed on the FIRST occurrence — zero remote writes, immediate
# MANUAL_REQUIRED with truthful probe evidence — instead of masquerading as
# transient probe failure #1..#5.  Only proven conflicts qualify (live
# id/number mismatch vs durable identity, non-unique exact lookup, mall
# mismatch); malformed payloads and ordinary transport faults stay
# transient probe failures.
# ---------------------------------------------------------------------------


def _conflict_tool(task, session, *, lookup_error: str | None = None, identity_error: str | None = None):
    module = _restart_tool_module(task, session)

    if lookup_error is not None:
        def check_existing_task(session_, task_name, mall_code):
            raise RuntimeError(lookup_error)

        module.check_existing_task = staticmethod(check_existing_task)

    if identity_error is not None:
        def get_task_identity(task_, mall_code):
            raise RuntimeError(identity_error)

        module.get_task_identity = staticmethod(get_task_identity)

    return module


def test_round7_b1_live_task_id_mismatch_is_typed_conflict(tmp_path, monkeypatch) -> None:
    from app.services.legacy_zhaoshang_adapter import RemoteTaskIdentityConflictError

    task = _running_task_record(task_id="999999")
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    with pytest.raises(RemoteTaskIdentityConflictError):
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)


def test_round7_b2_live_task_number_mismatch_is_typed_conflict(tmp_path, monkeypatch) -> None:
    from app.services.legacy_zhaoshang_adapter import RemoteTaskIdentityConflictError

    task = _running_task_record(task_number="RW-20260825999999")
    session = _RestartSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    with pytest.raises(RemoteTaskIdentityConflictError):
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)


def test_round7_b3a_non_unique_exact_lookup_is_typed_conflict(tmp_path, monkeypatch) -> None:
    from app.services.legacy_zhaoshang_adapter import RemoteTaskIdentityConflictError

    task = _running_task_record()
    session = _RestartSession(task)
    tool = _conflict_tool(
        task, session, lookup_error="精确任务不唯一: task_name=cnnc_current_opt_领先未来商城_61284593895349, mall_code=lxwl, count=2"
    )
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)
    monkeypatch.setattr(
        type(adapter), "_load_module", classmethod(lambda cls, path, name: tool)
    )

    with pytest.raises(RemoteTaskIdentityConflictError):
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)


def test_round7_b3b_lookup_mall_mismatch_is_typed_conflict(tmp_path, monkeypatch) -> None:
    from app.services.legacy_zhaoshang_adapter import RemoteTaskIdentityConflictError

    task = _running_task_record()
    session = _RestartSession(task)
    tool = _conflict_tool(
        task, session, lookup_error="精确任务商城不匹配: task_name=cnnc_current_opt_领先未来商城_61284593895349, expected=lxwl, actual=['comix']"
    )
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)
    monkeypatch.setattr(
        type(adapter), "_load_module", classmethod(lambda cls, path, name: tool)
    )

    with pytest.raises(RemoteTaskIdentityConflictError):
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)


def test_round7_b3c_identity_mall_mismatch_is_typed_conflict(tmp_path, monkeypatch) -> None:
    from app.services.legacy_zhaoshang_adapter import RemoteTaskIdentityConflictError

    task = _running_task_record()
    session = _RestartSession(task)
    tool = _conflict_tool(
        task, session, identity_error="商城标识不一致: expected=lxwl, actual=comix"
    )
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)
    monkeypatch.setattr(
        type(adapter), "_load_module", classmethod(lambda cls, path, name: tool)
    )

    with pytest.raises(RemoteTaskIdentityConflictError):
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)


def test_round7_b4_malformed_task_payload_stays_transient(tmp_path, monkeypatch) -> None:
    from app.services.legacy_zhaoshang_adapter import RemoteTaskIdentityConflictError

    task = _running_task_record()
    session = _RestartSession(task)
    tool = _conflict_tool(
        task, session, identity_error="没有拿到 taskId/id，任务数据: {'taskName': 'cnnc_current_opt_领先未来商城_61284593895349'}"
    )
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)
    monkeypatch.setattr(
        type(adapter), "_load_module", classmethod(lambda cls, path, name: tool)
    )

    # A payload missing its identity fields is unprovable, NOT a proven
    # mismatch: it must stay an untyped transient failure.
    with pytest.raises(RuntimeError) as excinfo:
        adapter.probe_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=20.0)
    assert not isinstance(excinfo.value, RemoteTaskIdentityConflictError)


def test_round7_b5_identity_conflict_is_immediate_zero_write_manual(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task)
    tool = _conflict_tool(
        task, session, lookup_error="精确任务不唯一: task_name=cnnc_current_opt_领先未来商城_61284593895349, mall_code=lxwl, count=2"
    )
    probe_cb, durable = _bridge_probe(tmp_path, monkeypatch, task, session, tool=tool)

    outcome = probe_cb(durable, 60.0)
    assert outcome["probe_identity_conflict"] is True
    assert str(outcome["diagnostic"]).startswith("probe raised ")

    store, job_id = _stalled_store(tmp_path / "state")
    token = store.acquire_lease(BUSINESS_DATE, "seed-health", ttl_seconds=300)
    evidence = _observations(4)
    evidence["probe_health"] = {
        "consecutive_failures": 3,
        "last_failure_at": NOW.isoformat(timespec="seconds"),
        "last_failure_reason": "probe raised TimeoutError",
    }
    store.update_remote_recovery_evidence(token, job_id, evidence)
    store.release_lease(token)

    probe_calls: list[int] = []

    def probe(job: dict):
        probe_calls.append(int(job["id"]))
        return outcome

    restart = _NeverRestart()
    counters = _run_probe_pass(store, probe, restart)

    # First occurrence: immediate manual, one probe, zero remote writes.
    assert probe_calls == [job_id]
    assert counters["recovery_probes"] == 0
    assert counters["recovery_identity_manual"] == 1
    assert restart.calls == []
    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    assert session.update_calls == []
    # The durable remote identity is never re-bound.
    assert str(job["remote_identity"]) == "lxwl|1269504|RW-20260825150224"
    document = json.loads(str(job["recovery_evidence_json"]))
    health = document["probe_health"]
    assert health["identity_conflict"] is True
    assert health["terminal_reason"] == "REMOTE_IDENTITY_CONFLICT"
    # Historical transient failures are preserved, never faked into count=4.
    assert health["consecutive_failures"] == 3
    # A probe outcome never enters the restart protocol.
    assert document["restart"] is None
    assert "identity" in str(job["error_text"])


def test_round7_b5b_post_restart_identity_conflict_preserves_restart_proof(tmp_path) -> None:
    store, job_id = _recovered_store(tmp_path)
    restart_evidence = json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))["restart"]

    def probe(job: dict):
        return {
            "probe_identity_conflict": True,
            "diagnostic": "probe raised RemoteTaskIdentityConflictError",
        }

    _run_probe_pass(store, probe, _NeverRestart())

    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    document = json.loads(str(job["recovery_evidence_json"]))
    # The confirmed restart proof survives untouched.
    preserved = document["restart"]
    assert preserved["phase"] == "ENABLE_CONFIRMED"
    assert preserved["disable_evidence"]["confirmed_disabled"] is True
    assert preserved["enable_evidence"]["confirmed_enabled"] is True
    assert preserved["recovery_at"] == restart_evidence["recovery_at"]
    assert document["probe_health"]["identity_conflict"] is True
    assert int(job["recovery_attempts"]) == 1


# ---------------------------------------------------------------------------
# Round 7, MINOR C: the probe-failure cap error text must match the durable
# restart history.  A post-restart probe failure (recovery_attempts==1,
# ENABLE_CONFIRMED) must not claim "no restart was attempted".
# ---------------------------------------------------------------------------


def test_round7_c2_post_restart_probe_cap_error_is_truthful(tmp_path) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES

    store, job_id = _recovered_store(tmp_path)

    def failing_probe(job: dict):
        return {"probe_failed": True, "diagnostic": "probe raised AuthError"}

    for index in range(RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES):
        _run_probe_pass(store, failing_probe, _NeverRestart(), now=NOW + timedelta(minutes=3 * index))

    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    error_text = str(job["error_text"])
    assert "no restart was attempted" not in error_text
    assert "already confirmed" in error_text
    assert "post-restart" in error_text


def test_round7_c3_post_restart_probe_cap_preserves_restart_evidence(tmp_path) -> None:
    from app.services.remote_running_recovery import RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES

    store, job_id = _recovered_store(tmp_path)

    def failing_probe(job: dict):
        return {"probe_failed": True, "diagnostic": "probe raised AuthError"}

    for index in range(RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES):
        _run_probe_pass(store, failing_probe, _NeverRestart(), now=NOW + timedelta(minutes=3 * index))

    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    document = json.loads(str(job["recovery_evidence_json"]))
    restart = document["restart"]
    assert restart["phase"] == "ENABLE_CONFIRMED"
    assert restart["disable_evidence"]["confirmed_disabled"] is True
    assert restart["enable_evidence"]["confirmed_enabled"] is True
    assert restart["recovery_at"]
    assert document["probe_health"]["consecutive_failures"] == RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES
