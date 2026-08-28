"""PHASE E: RUNNING-stall recovery probe outage fault classification.

F-P2-8 = TESTED_AND_DESIGNED_NOT_IMPLEMENTED.

This file pins the CURRENT system's actual walk for every fault class that
can strike the read-only RUNNING-stall recovery probe.  The system does NOT
distinguish job-specific from shared-infrastructure faults on the counted
path — every fault except a PROVEN exact-identity conflict lands in the same
per-job consecutive probe-failure counter and fails closed to MANUAL_REQUIRED
after 5 consecutive occurrences (``RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES``).
That is a deliberate design trade-off (dc90078: prefer manual review over a
silently spinning probe); this round ONLY tests it, never changes it.

Measured fault table (production chain: adapter exception -> bridge envelope
in ``legacy_zhaoshang_bridge.recovery_probe`` -> durable walk in
``run_running_recovery_pass`` -> ``DailyAuditStore.record_remote_recovery_probe_failure``):

- auth outage (``RefreshTokenExpiredError``, scripts/auth_manager.py)
  -> generic ``except Exception`` envelope ``{"probe_failed": True}``
     (app/services/incremental_audit_runtime.py, recovery_probe)
  -> per-job consecutive counter; 5 consecutive -> MANUAL_REQUIRED.
- DB unreachable (``pymysql.err.OperationalError(2003, ...)``)
  -> same generic envelope -> same counted path.
- remote transport broken (``TimeoutError`` / ``ConnectionError`` /
  ``ConnectionResetError``) -> same generic envelope -> same counted path.
- malformed single-task response (``ValueError`` / ``KeyError`` from parsing)
  -> same generic envelope -> same counted path (NOT distinguished from a
  shared outage today — that non-distinction is exactly what F-P2-8 tracks).
- exact identity conflict (``RemoteTaskIdentityConflictError``,
  app/services/legacy_zhaoshang_adapter.py:267)
  -> typed ``{"probe_identity_conflict": True}`` envelope
     (incremental_audit_runtime.py recovery_probe except-clause)
  -> IMMEDIATE MANUAL_REQUIRED on the FIRST occurrence, zero remote writes,
     and it does NOT consume or feed the consecutive probe-failure counter
     (remote_running_recovery.py run_running_recovery_pass identity branch).

All durable-state tests use a real ``DailyAuditStore`` on tmp_path and fake
probe callbacks; no test touches the production platform.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pymysql.err import OperationalError

from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_runtime import legacy_zhaoshang_bridge, runtime_paths
from app.services.legacy_zhaoshang_adapter import RemoteTaskIdentityConflictError
from app.services.remote_running_recovery import (
    RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES,
    run_running_recovery_pass,
)
from scripts.auth_manager import RefreshTokenExpiredError


BUSINESS_DATE = "2026-08-25"
NOW = datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC)
PROGRESS_ACTIVE = {"total": 10, "pending": 4, "running": 3, "failed": 0, "waiting_db": 1, "finished": 2}

_BRIDGE_REQUEST = {
    "mall_key": "lxwl",
    "mall_source_id": "34",
    "mall_name": "领先未来商城",
    "sku_id": "sku-1",
    "url": "https://www.66123123.com/item/1",
    "backend_version": "legacy-v1",
}


# ---------------------------------------------------------------------------
# Shared test scaffolding (self-contained; this file must not depend on the
# fakes inside tests/test_remote_running_recovery.py, which a parallel task
# is editing).
# ---------------------------------------------------------------------------


def _seed_candidate_jobs(store: DailyAuditStore, count: int) -> list[int]:
    """Seed ``count`` distinct probe-candidate RUNNING jobs on one store."""
    token = store.acquire_lease(BUSINESS_DATE, "seed", ttl_seconds=300)
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
                    2,
                    (NOW - timedelta(minutes=35)).isoformat(timespec="seconds"),
                    (NOW - timedelta(minutes=35)).isoformat(timespec="seconds"),
                    int(job["id"]),
                ),
            )
        ids.append(int(job["id"]))
    store.release_lease(token)
    return ids


def _run_pass(store: DailyAuditStore, probe, restart=None, *, now: datetime = NOW) -> dict:
    token = store.acquire_lease(BUSINESS_DATE, "pass", ttl_seconds=300)
    try:
        return run_running_recovery_pass(
            store, token, probe_remote=probe, restart_remote=restart, now=now, can_start=lambda: True
        )
    finally:
        store.release_lease(token)


def _evidence_of(store: DailyAuditStore, job_id: int) -> dict:
    return json.loads(str(store.get_remote_job(job_id)["recovery_evidence_json"]))


class _RaisingProbeAdapter:
    """Duck-typed legacy adapter whose read-only RUNNING probe raises ``exc``.

    Only the two methods the bridge's recovery probe touches are needed:
    ``prepare_job`` (crash reconstruction) and ``probe_running_task``.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def prepare_job(self, business_date, requests, input_path, result_path, task_name, batch_state):
        return SimpleNamespace(
            business_date=business_date,
            mall_key="lxwl",
            mall_source_id="34",
            mall_name="领先未来商城",
            logical_job_key="logical-1",
            payload_sha256="payload-hash",
            template_path=input_path,
            result_path=result_path,
            batch_state_path=batch_state,
            requests=requests,
            task_name=task_name,
        )

    def probe_running_task(self, _prepared_job, _task_id, _task_number, timeout_seconds=60.0):
        raise self._exc


def _bridge_probe_with(tmp_path, exc: BaseException):
    """Build the production bridge with a probe that raises ``exc``.

    Returns ``(probe_cb, durable_row)`` where ``durable_row`` is a
    reconstructable durable job row, so the call covers
    reconstruct -> identity parts -> probe_running_task -> envelope
    translation exactly like the tick does.
    """
    paths = runtime_paths(BUSINESS_DATE, tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    batch_state = paths.run_dir / "BATCH_STATE.md"
    batch_state.write_text("# batch\n", encoding="utf-8")
    input_path = paths.run_dir / "input.xlsx"
    input_path.write_bytes(b"template")
    result_path = paths.run_dir / "result.xlsx"
    adapter = _RaisingProbeAdapter(exc)
    _planner, _finalizer, _submitter, _harvester, probe_cb, _restart_cb = legacy_zhaoshang_bridge(
        adapter, paths=paths, business_date=BUSINESS_DATE
    )
    durable = {
        "id": 1,
        "logical_key": "logical-1",
        "task_name": "cnnc_current_opt_领先未来商城_61284593895349",
        "payload_hash": "payload-hash",
        "payload_json": json.dumps(
            {"requests": [dict(_BRIDGE_REQUEST)], "batch_state": str(batch_state)}
        ),
        "input_path": str(input_path),
        "result_path": str(result_path),
        "remote_identity": "lxwl|1269504|RW-20260825150224",
    }
    return probe_cb, durable


def _shared_outage_exceptions():
    """Shared-infrastructure fault candidates (would hit MANY jobs at once)."""
    return [
        pytest.param(RefreshTokenExpiredError("zhaoshang refreshToken 已过期"), id="auth-refresh-expired"),
        pytest.param(
            OperationalError(2003, "Can't connect to MySQL server on '127.0.0.1' (10061)"),
            id="db-unreachable-2003",
        ),
        pytest.param(TimeoutError("probe timed out"), id="transport-timeout"),
        pytest.param(ConnectionError("connection dropped"), id="transport-connection-error"),
        pytest.param(ConnectionResetError("connection reset by peer"), id="transport-connection-reset"),
    ]


def _malformed_response_exceptions():
    """Job-specific malformed-response fault candidates (parse stage)."""
    return [
        pytest.param(ValueError("malformed task payload: expected object"), id="malformed-valueerror"),
        pytest.param(KeyError("data"), id="malformed-keyerror"),
    ]


def _legal_probe(job: dict) -> dict:
    parts = str(job["remote_identity"]).split("|")
    return {
        "remote_mall": parts[0],
        "remote_task_id": parts[1],
        "remote_task_number": parts[2],
        "progress": dict(PROGRESS_ACTIVE),
        "export_count": 0,
        "export_rsp_desc": "未检索到商品价格信息",
        "export_had_result": False,
        "task_status_rsp_desc": "",
    }


# ---------------------------------------------------------------------------
# A. Envelope translation through the production bridge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", _shared_outage_exceptions())
def test_shared_infrastructure_faults_translate_to_counted_probe_failures(tmp_path, exc):
    """Every shared-outage exception becomes the SAME retryable envelope.

    Auth expiry, DB unreachability, and transport breaks are not special-cased
    anywhere in the probe chain: they all land in the generic
    ``except Exception`` of the bridge and surface as ``probe_failed``.
    """
    probe_cb, durable = _bridge_probe_with(tmp_path, exc)

    outcome = probe_cb(durable, 60.0)

    assert outcome["probe_failed"] is True
    assert outcome.get("probe_identity_conflict") is not True
    assert type(exc).__name__ in str(outcome["diagnostic"])


@pytest.mark.parametrize("exc", _malformed_response_exceptions())
def test_malformed_responses_are_not_distinguished_from_shared_outages(tmp_path, exc):
    """Job-specific parse faults share the generic counted path (F-P2-8 pin).

    A malformed single-task response (ValueError/KeyError while parsing) is
    currently indistinguishable from a shared-infrastructure outage: the same
    ``probe_failed`` envelope, the same per-job counter.
    """
    probe_cb, durable = _bridge_probe_with(tmp_path, exc)

    outcome = probe_cb(durable, 60.0)

    assert outcome["probe_failed"] is True
    assert outcome.get("probe_identity_conflict") is not True
    assert type(exc).__name__ in str(outcome["diagnostic"])


def test_identity_conflict_translates_to_the_immediate_manual_signal(tmp_path):
    """A PROVEN exact-identity conflict gets its own typed envelope."""
    probe_cb, durable = _bridge_probe_with(
        tmp_path, RemoteTaskIdentityConflictError("live task id mismatch")
    )

    outcome = probe_cb(durable, 60.0)

    assert outcome["probe_identity_conflict"] is True
    assert outcome.get("probe_failed") is not True


# ---------------------------------------------------------------------------
# B. Durable walk: one failure — still RUNNING, counted per job
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", _shared_outage_exceptions() + _malformed_response_exceptions())
def test_single_probe_failure_keeps_job_running_and_counts_exactly_one(tmp_path, exc):
    """After ONE outage/probe failure the job is still RUNNING (never MANUAL)."""
    probe_cb, durable = _bridge_probe_with(tmp_path, exc)
    outcome = probe_cb(durable, 60.0)

    store = DailyAuditStore(tmp_path / "state.sqlite3")
    (job_id,) = _seed_candidate_jobs(store, 1)

    counters = _run_pass(store, lambda _job: outcome)

    job = store.get_remote_job(job_id)
    assert job["state"] == "RUNNING"
    assert job["error_text"] is None
    evidence = _evidence_of(store, job_id)
    assert evidence["probe_health"]["consecutive_failures"] == 1
    assert evidence["probe_health"]["last_failure_reason"]
    assert counters["recovery_probes"] == 0
    assert counters["recovery_identity_manual"] == 0


# ---------------------------------------------------------------------------
# C. Durable walk: five consecutive failures fail closed to MANUAL_REQUIRED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(TimeoutError("probe timed out"), id="shared-transport-timeout"),
        pytest.param(ValueError("malformed task payload"), id="malformed-response"),
    ],
)
def test_fifth_consecutive_failure_fails_closed_to_manual(tmp_path, exc):
    """4 failures stay RUNNING; the 5th consecutive failure enters MANUAL."""
    probe_cb, durable = _bridge_probe_with(tmp_path, exc)
    outcome = probe_cb(durable, 60.0)

    store = DailyAuditStore(tmp_path / "state.sqlite3")
    (job_id,) = _seed_candidate_jobs(store, 1)

    for index in range(RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES):
        _run_pass(store, lambda _job: outcome, now=NOW + timedelta(minutes=3 * index))
        job = store.get_remote_job(job_id)
        if index < RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES - 1:
            assert job["state"] == "RUNNING"
            assert _evidence_of(store, job_id)["probe_health"]["consecutive_failures"] == index + 1

    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    health = _evidence_of(store, job_id)["probe_health"]
    assert health["consecutive_failures"] == RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES
    assert "5 consecutive" in str(job["error_text"])


def test_same_shared_fault_counts_independently_on_two_jobs(tmp_path):
    """F-P2-8 mechanism: one shared outage accumulates PER JOB, not globally.

    The same auth outage strikes two different RUNNING candidates in the same
    pass; each job keeps its own consecutive counter and both independently
    reach MANUAL_REQUIRED after five consecutive failures.
    """
    exc = RefreshTokenExpiredError("zhaoshang refreshToken 已过期")
    probe_cb, durable = _bridge_probe_with(tmp_path, exc)
    outcome = probe_cb(durable, 60.0)

    store = DailyAuditStore(tmp_path / "state.sqlite3")
    job_ids = _seed_candidate_jobs(store, 2)

    # One pass: the SAME shared fault hits both jobs.
    _run_pass(store, lambda _job: outcome)
    for job_id in job_ids:
        job = store.get_remote_job(job_id)
        assert job["state"] == "RUNNING"
        assert _evidence_of(store, job_id)["probe_health"]["consecutive_failures"] == 1

    for index in range(1, RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES):
        _run_pass(store, lambda _job: outcome, now=NOW + timedelta(minutes=3 * index))

    for job_id in job_ids:
        job = store.get_remote_job(job_id)
        assert job["state"] == "MANUAL_REQUIRED"
        health = _evidence_of(store, job_id)["probe_health"]
        assert health["consecutive_failures"] == RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES
        assert "5 consecutive" in str(job["error_text"])


# ---------------------------------------------------------------------------
# D. Identity conflict: immediate MANUAL, zero writes, no counter consumption
# ---------------------------------------------------------------------------


def test_identity_conflict_is_immediate_manual_and_skips_the_probe_counter(tmp_path):
    """First occurrence -> MANUAL_REQUIRED with zero writes and no counting."""
    probe_cb, durable = _bridge_probe_with(
        tmp_path, RemoteTaskIdentityConflictError("live task id mismatch")
    )
    outcome = probe_cb(durable, 60.0)

    store = DailyAuditStore(tmp_path / "state.sqlite3")
    (job_id,) = _seed_candidate_jobs(store, 1)

    counters = _run_pass(store, lambda _job: outcome)

    job = store.get_remote_job(job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    assert "identity conflicts" in str(job["error_text"])
    health = _evidence_of(store, job_id)["probe_health"]
    assert health["identity_conflict"] is True
    assert health["terminal_reason"] == "REMOTE_IDENTITY_CONFLICT"
    # The conflict does NOT consume the consecutive outage counter: a fresh
    # job has no historical count, so none is fabricated.
    assert "consecutive_failures" not in health
    assert counters["recovery_identity_manual"] == 1
    assert counters["recovery_probes"] == 0
    assert int(job["recovery_attempts"]) == 0


def test_identity_conflict_on_one_job_leaves_the_other_job_untouched(tmp_path):
    """The conflict path is per-job: a sibling candidate keeps probing fine."""
    probe_cb, durable = _bridge_probe_with(
        tmp_path, RemoteTaskIdentityConflictError("live task id mismatch")
    )
    outcome = probe_cb(durable, 60.0)

    store = DailyAuditStore(tmp_path / "state.sqlite3")
    first, second = _seed_candidate_jobs(store, 2)

    def probe(job: dict) -> dict:
        if int(job["id"]) == first:
            return outcome
        return _legal_probe(job)

    _run_pass(store, probe)

    conflicted = store.get_remote_job(first)
    assert conflicted["state"] == "MANUAL_REQUIRED"
    sibling = store.get_remote_job(second)
    assert sibling["state"] == "RUNNING"
    sibling_evidence = _evidence_of(store, second)
    assert sibling_evidence.get("probe_health") is None
    assert len(sibling_evidence["observations"]) == 1
