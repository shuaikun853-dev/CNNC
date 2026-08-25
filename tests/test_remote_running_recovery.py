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


def test_restart_outcome_survives_store_reopen(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
    _run_pass(store, _probe_payload(), restart)
    reopened = DailyAuditStore(tmp_path / "state.sqlite3")
    refreshed = reopened.get_remote_job(job_id)
    assert int(refreshed["recovery_attempts"]) == 1
    evidence = json.loads(str(refreshed["recovery_evidence_json"]))
    assert evidence["restart"]["phase"] == "ENABLE_CONFIRMED"


def test_recovered_job_is_never_restarted_twice(tmp_path) -> None:
    store, job_id = _stalled_store(tmp_path)
    restart = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
    _run_pass(store, _probe_payload(), restart)
    # The job stalls again with a fresh frozen signature after the restart.
    restart_again = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
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


class _RestartSession:
    """Stateful fake: updateState flips the exact task's enabled flag."""

    def __init__(
        self,
        task: dict,
        *,
        fail_disable: bool = False,
        fail_enable: bool = False,
        status_rsp_desc: str = "执行中",
    ) -> None:
        self.task = task
        self.fail_disable = fail_disable
        self.fail_enable = fail_enable
        self.status_rsp_desc = status_rsp_desc
        self.update_calls: list[dict] = []

    def post(self, url, json=None, timeout=None):
        if "updateState" in url:
            self.update_calls.append(dict(json))
            if json.get("state") == "0":
                if self.fail_disable:
                    raise TimeoutError("disable response lost")
                self.task["enabled"] = False
                return _FakeRestartResponse({"rspCode": "0000", "success": True})
            if json.get("state") == "1":
                if self.fail_enable:
                    raise TimeoutError("enable response lost")
                self.task["enabled"] = True
                return _FakeRestartResponse({"rspCode": "0000", "success": True})
        if "exportTask" in url:
            return _FakeRestartResponse(
                {"rspCode": "0000", "rspDesc": NO_PRICE_RSP, "count": 0, "data": None, "success": True}
            )
        if "queryTaskStatus" in url:
            return _FakeRestartResponse({"rspDesc": self.status_rsp_desc, "success": True})
        return _FakeRestartResponse({"data": [dict(self.task)]})

    def get(self, url, params=None, timeout=None):
        return _FakeRestartResponse(
            {
                "rspDesc": "总任务量：10条，待执行：4条，执行中：3条，失败：0条，待入库：1条，执行完成：2条",
                "success": True,
            }
        )


def _restart_tool_module(task: dict, session: _RestartSession):
    class _Module:
        EXPORT_TASK_URL = "https://platform.invalid/exportTask"
        QUERY_PROGRESS_URL = "https://platform.invalid/queryTaskProgress"
        QUERY_TASK_STATUS_URL = "https://platform.invalid/queryTaskStatus"
        UPDATE_STATE_URL = "https://platform.invalid/updateState"

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
            return dict(session.task)

        @staticmethod
        def get_task_identity(task, mall_code):
            return task["taskId"], task["taskNumber"], mall_code

        @staticmethod
        def query_task_status(session, task_id):
            return {"rspDesc": session.status_rsp_desc, "success": True}

        @staticmethod
        def query_task_progress(session, batch_number):
            return session.get(
                _Module.QUERY_PROGRESS_URL, params={"batchNumber": batch_number}
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
    def export_environment(self, system, refresh=False):
        return {"BLADE_AUTH": "bearer fake", "COOKIE": "x-access-token=fake; refreshToken=fake"}


def _prepared_job(tmp_path, monkeypatch, task: dict, session: _RestartSession):
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter

    run_dir = tmp_path / "zhaoshang"
    run_dir.mkdir(parents=True, exist_ok=True)
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.write_text("# batch\n", encoding="utf-8")
    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[], db_env_path=None, auth_manager=_FakeAuthManager()
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


def test_adapter_fresh_restart_declines_when_task_completed_between_probe_and_write(
    tmp_path, monkeypatch
) -> None:
    task = _running_task_record()
    session = _RestartSession(task, status_rsp_desc="本次任务执行完毕")
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "NO_LONGER_RUNNING"
    assert session.update_calls == []


def test_adapter_fresh_restart_declines_on_unknown_remote_status(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, status_rsp_desc="状态异常")
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.restart_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status.value == "NO_LONGER_RUNNING"
    assert session.update_calls == []


def test_adapter_fresh_restart_declines_on_empty_remote_status(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _RestartSession(task, status_rsp_desc="")
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


def test_stationary_observations_without_status_are_not_stall_evidence() -> None:
    evidence = _observations(4)
    for row in evidence["observations"]:
        row["task_status_rsp_desc"] = ""
    job = _job(evidence=evidence)
    decision = evaluate_running_stall(job, now=NOW)
    assert decision["stalled"] is False


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


def test_default_dependencies_wire_probe_and_write_gated_restart(tmp_path) -> None:
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
    restart = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
    _run_pass(store, _probe_payload(), restart)
    # Simulate the crash window: the finished evidence document committed,
    # the recovery_attempts column write lost.
    token = store.acquire_lease(BUSINESS_DATE, "crash", ttl_seconds=300)
    with store._transaction(immediate=True) as connection:
        connection.execute("UPDATE remote_jobs SET recovery_attempts=0 WHERE id=?", (job_id,))
    store.release_lease(token)
    again = _FakeRestart([{"status": "RESTARTED", "restarted": True}])
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

    outcome = restart_cb(durable, None)

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
