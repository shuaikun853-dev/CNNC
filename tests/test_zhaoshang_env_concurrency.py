"""Concurrency regression tests for the zhaoshang process-env mutation lock.

P1-1: ``_temporary_environment`` mutates ``os.environ`` (process-global), so
two overlapping callers cross-contaminate auth or leave residue in the
long-lived uvicorn process.  The adapter serializes the mutation window with
``_ZHAOSHANG_ENV_LOCK``; session-based seams only lock the session-construction
window (``build_session`` copies BLADE_AUTH/COOKIE into the session headers and
never re-reads the environment) so their HTTP calls stay concurrent.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

import pytest

from app.services.legacy_zhaoshang_adapter import (
    _ZHAOSHANG_ENV_LOCK,
    _locked_session_construction,
    _locked_temporary_environment,
    _temporary_environment,
)
from app.services.remote_running_recovery import RemoteExecutionState
from app.services.zhaoshang_progress_probe import ZhaoshangProgressService

ENV_KEYS = ("BLADE_AUTH", "COOKIE")

# Structurally complete 18/18 terminal payload: finished + failed >= total and
# no pending/running/waiting_db work -> classify_remote_execution_state returns
# REMOTE_TERMINAL.
_TERMINAL_PROGRESS = {
    "total": 18,
    "pending": 0,
    "running": 0,
    "failed": 0,
    "waiting_db": 0,
    "finished": 18,
}


@contextmanager
def _isolate_env():
    """Snapshot / force-absent auth variables around one test, then restore."""
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    try:
        yield saved
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _auth_state() -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in ENV_KEYS}


def _env_snapshot() -> dict[str, str]:
    return dict(os.environ)


def _join(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "worker thread did not finish"


# ---------------------------------------------------------------------------
# 1. The bare primitive really is racy (control proof for the fix below).
# ---------------------------------------------------------------------------


def test_unlocked_primitive_shows_the_race():
    with _isolate_env() as original:
        entry_gate = threading.Barrier(2)
        inside_gate = threading.Barrier(2)
        observed: dict[str, str | None] = {}
        after: dict[str, str | None] = {}

        def worker(tag: str, value: str) -> None:
            entry_gate.wait(timeout=5)
            with _temporary_environment({"BLADE_AUTH": value, "COOKIE": value}):
                inside_gate.wait(timeout=5)
                observed[tag] = os.environ.get("BLADE_AUTH")
            after[tag] = os.environ.get("BLADE_AUTH")

        threads = [
            threading.Thread(target=worker, args=("A", "A")),
            threading.Thread(target=worker, args=("B", "B")),
        ]
        for thread in threads:
            thread.start()
        _join(threads)

        crossed = any(observed[tag] != tag for tag in ("A", "B"))
        residue = any(after[tag] != original["BLADE_AUTH"] for tag in ("A", "B"))
        assert crossed or residue, (
            "control expectation broken: the unlocked primitive no longer "
            "demonstrates the race it is supposed to justify guarding"
        )


# ---------------------------------------------------------------------------
# 2-6. The locked primitives serialize the process-env window.
# ---------------------------------------------------------------------------


def test_locked_environment_serializes_distinct_auth():
    with _isolate_env() as original:
        auth_a_set = threading.Event()
        observed: dict[str, dict[str, str | None]] = {}

        def worker_a() -> None:
            with _locked_temporary_environment({"BLADE_AUTH": "A", "COOKIE": "A"}):
                observed["A_inside"] = _auth_state()
                auth_a_set.set()
                # Overlapping-window simulation: B tries to enter while A is
                # still inside its mutation window.
                time.sleep(0.1)
                observed["A_still_inside"] = _auth_state()

        def worker_b() -> None:
            assert auth_a_set.wait(timeout=5)
            # Blocked on the lock while A is inside; only acquires it after A
            # restored, so what B sees at entry is the original, never A's auth.
            with _ZHAOSHANG_ENV_LOCK:
                observed["B_on_entry"] = _auth_state()
            with _locked_temporary_environment({"BLADE_AUTH": "B", "COOKIE": "B"}):
                observed["B_inside"] = _auth_state()

        threads = [
            threading.Thread(target=worker_a),
            threading.Thread(target=worker_b),
        ]
        for thread in threads:
            thread.start()
        _join(threads)

        a_auth = {"BLADE_AUTH": "A", "COOKIE": "A"}
        b_auth = {"BLADE_AUTH": "B", "COOKIE": "B"}
        assert observed["A_inside"] == a_auth
        assert observed["A_still_inside"] == a_auth
        assert observed["B_on_entry"] == original
        assert observed["B_inside"] == b_auth
        assert _auth_state() == original


def test_locked_session_construction_two_threads_distinct_auth():
    with _isolate_env() as original:
        auth_a_built = threading.Event()
        sniffs: dict[str, dict[str, str | None]] = {}

        def build_sniffing(event: threading.Event | None):
            def build() -> dict[str, str]:
                if event is not None:
                    event.set()
                return dict(os.environ)

            return build

        def worker(tag: str, value: str, event, wait_on: threading.Event | None) -> None:
            if wait_on is not None:
                assert wait_on.wait(timeout=5)
            with _locked_session_construction(
                {"BLADE_AUTH": value, "COOKIE": value}, build_sniffing(event)
            ) as raw:
                sniffs[tag] = {key: raw.get(key) for key in ENV_KEYS}

        threads = [
            threading.Thread(target=worker, args=("A", "tA", auth_a_built, None)),
            threading.Thread(target=worker, args=("B", "tB", None, auth_a_built)),
        ]
        for thread in threads:
            thread.start()
        _join(threads)

        assert sniffs["A"] == {"BLADE_AUTH": "tA", "COOKIE": "tA"}
        assert sniffs["B"] == {"BLADE_AUTH": "tB", "COOKIE": "tB"}
        assert _auth_state() == original


def test_construction_exception_restores_env():
    def boom() -> object:
        raise RuntimeError("build failed")

    with _isolate_env():
        os.environ["BLADE_AUTH"] = "orig"
        with pytest.raises(RuntimeError, match="build failed"):
            with _locked_session_construction({"BLADE_AUTH": "t", "COOKIE": "t"}, boom):
                raise AssertionError("the broken build must short-circuit the body")
        assert _auth_state() == {"BLADE_AUTH": "orig", "COOKIE": None}

    with _isolate_env():
        with pytest.raises(RuntimeError, match="build failed"):
            with _locked_session_construction({"BLADE_AUTH": "t", "COOKIE": "t"}, boom):
                raise AssertionError("the broken build must short-circuit the body")
        assert _auth_state() == {"BLADE_AUTH": None, "COOKIE": None}


def test_preexisting_env_restored():
    with _isolate_env():
        os.environ["BLADE_AUTH"] = "orig"
        with _locked_temporary_environment({"BLADE_AUTH": "temp", "COOKIE": "temp"}):
            assert _auth_state() == {"BLADE_AUTH": "temp", "COOKIE": "temp"}
        assert _auth_state() == {"BLADE_AUTH": "orig", "COOKIE": None}


def test_missing_env_stays_missing():
    with _isolate_env():
        with _locked_temporary_environment({"BLADE_AUTH": "temp", "COOKIE": "temp"}):
            assert _auth_state() == {"BLADE_AUTH": "temp", "COOKIE": "temp"}
        assert _auth_state() == {"BLADE_AUTH": None, "COOKIE": None}


def test_repeated_concurrent_rounds_no_residue():
    with _isolate_env() as original:
        rounds = 10
        round_gate = threading.Barrier(2)
        sniffs: dict[str, list[dict[str, str | None]]] = {"A": [], "B": []}

        def worker(tag: str, value: str) -> None:
            for _ in range(rounds):
                round_gate.wait(timeout=5)
                with _locked_temporary_environment({"BLADE_AUTH": value, "COOKIE": value}):
                    sniffs[tag].append(_auth_state())

        threads = [
            threading.Thread(target=worker, args=("A", "A")),
            threading.Thread(target=worker, args=("B", "B")),
        ]
        for thread in threads:
            thread.start()
        _join(threads)

        assert all(
            sniff == {"BLADE_AUTH": tag, "COOKIE": tag}
            for tag in ("A", "B")
            for sniff in sniffs[tag]
        )
        assert len(sniffs["A"]) == len(sniffs["B"]) == rounds
        assert _auth_state() == original


# ---------------------------------------------------------------------------
# 7. The dashboard progress query runs its HTTP fully outside the lock.
# ---------------------------------------------------------------------------


def test_progress_http_runs_outside_the_lock():
    with _isolate_env():
        rendezvous = threading.Barrier(2, timeout=10)
        lock_probes: list[str] = []
        probes_guard = threading.Lock()

        class _FakeAdapter:
            def query_task_progress_for_job(self, prepared, task_id, task_number, timeout):
                acquired = _ZHAOSHANG_ENV_LOCK.acquire(blocking=False)
                if acquired:
                    _ZHAOSHANG_ENV_LOCK.release()
                with probes_guard:
                    lock_probes.append("outside" if acquired else "INSIDE_LOCK")
                # Both dashboard queries must be in flight simultaneously; if
                # the network zone still held the env lock, one thread would
                # never reach this barrier and it would break after 10s.
                rendezvous.wait(timeout=10)
                return {
                    "progress": dict(_TERMINAL_PROGRESS),
                    "batch_number": "x",
                    "rsp_desc": "",
                }

        service = ZhaoshangProgressService(
            lambda: _FakeAdapter(),
            prepared_resolver=lambda job: object(),
            max_concurrency=2,
            background_refresh=False,
        )
        jobs = [
            {"id": "j1", "task_name": "t-one", "remote_identity": "mall|1269501|RW-1"},
            {"id": "j2", "task_name": "t-two", "remote_identity": "mall|1269502|RW-2"},
        ]
        results: dict[str, dict] = {}

        def refresh(job: dict) -> None:
            results[str(job["id"])] = service.refresh(job)

        threads = [threading.Thread(target=refresh, args=(job,)) for job in jobs]
        for thread in threads:
            thread.start()
        _join(threads)

        for job in jobs:
            status = results[job["id"]]
            assert status["available"] is True
            assert status["error_code"] is None
            assert status["classification"] == RemoteExecutionState.TERMINAL.value
        assert lock_probes == ["outside", "outside"]


# ---------------------------------------------------------------------------
# 8. The lock must be reentrant on the same thread (nested construction).
# ---------------------------------------------------------------------------


def test_construction_lock_reentrant_same_thread():
    with _isolate_env():
        with _locked_session_construction(
            {"BLADE_AUTH": "outer", "COOKIE": "outer"}, _env_snapshot
        ) as outer:
            with _locked_session_construction(
                {"BLADE_AUTH": "inner", "COOKIE": "inner"}, _env_snapshot
            ) as inner:
                assert {key: inner.get(key) for key in ENV_KEYS} == {
                    "BLADE_AUTH": "inner",
                    "COOKIE": "inner",
                }
            # Inner restore is precise: back to the outer window, not the base.
            assert {key: outer.get(key) for key in ENV_KEYS} == {
                "BLADE_AUTH": "outer",
                "COOKIE": "outer",
            }
            assert _auth_state() == {"BLADE_AUTH": "outer", "COOKIE": "outer"}
        assert _auth_state() == {"BLADE_AUTH": None, "COOKIE": None}
