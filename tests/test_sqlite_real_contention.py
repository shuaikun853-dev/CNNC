"""L2 REAL COMPONENT TEST — real sqlite3 connections, real BEGIN IMMEDIATE
locks, real BUSY timeouts; tmp_path only.

These cases pin the durable behavior of the audit store under genuine
single-writer contention: an independent connection (or a killed child
process) holds the real SQLite write lock while the production code paths
run against the same database file.  Nothing is mocked, no synthetic
OperationalError is raised, and no pragma is changed — the production
configuration (``journal_mode=wal``, ``busy_timeout=5000``, ``BEGIN
IMMEDIATE`` in ``DailyAuditStore._connect``/``_transaction``) is exactly the
object under test.  Every database lives under ``tmp_path``.

This complements the msvcrt process-lock and spawn overlap coverage in
test_daily_audit_store.py and test_incremental_audit_resilience.py, which
fence contention before it reaches SQLite and therefore never observe a real
SQLITE_BUSY outcome, a contended constructor, WAL concurrent readers, or a
writer killed mid-transaction.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from app.services.daily_audit_store import DailyAuditStore, LeaseToken, LeaseUnavailable
from app.services.incremental_audit_closeout import _closeout_failure_class
from app.workflows.incremental_price_audit import TickSummary, _safe_error


BUSINESS_DATE = "2026-08-11"
# The store's per-connection busy budget is 5000 ms.  A contended writer must
# spend roughly that budget before failing — never hang, never fail instantly.
MIN_BUSY_SECONDS = 4.5
MAX_BUSY_SECONDS = 30.0


def _db(tmp_path: Path) -> Path:
    return tmp_path / "daily-audit.sqlite3"


def _prepared_store(tmp_path: Path) -> DailyAuditStore:
    """A real seeded store: schema applied, one business run, probe table."""
    store = DailyAuditStore(_db(tmp_path))
    store.ensure_run(BUSINESS_DATE)
    _prepare_probe_table(_db(tmp_path))
    return store


def _prepare_probe_table(db_path: Path) -> None:
    # The probe table must exist committed before contention starts so later
    # readers can SELECT it while the external writer holds uncommitted rows.
    connection = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS contention_probe(id INTEGER PRIMARY KEY, marker TEXT)"
        )
    finally:
        connection.close()


@contextmanager
def _held_write_lock(db_path: Path, marker: str) -> Iterator[sqlite3.Connection]:
    """An independent real connection holding an open IMMEDIATE transaction."""
    connection = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO contention_probe(marker) VALUES (?)", (marker,))
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def _integrity(db_path: Path) -> str:
    connection = sqlite3.connect(str(db_path), timeout=5)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def _ro_connection(db_path: Path) -> sqlite3.Connection:
    # Same read-only URI form the dashboard projection uses in production.
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _failed_summary(exc: BaseException) -> TickSummary:
    """Build the summary a real tick returns when it dies with ``exc``."""
    return TickSummary(
        outcome="FAILED",
        phase_trace=(),
        counters={},
        backlog={},
        frozen_snapshot_id=None,
        error=_safe_error(exc),
    )


def test_real_busy_write_waits_then_raises_and_recovers(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "tick-a", ttl_seconds=120)

    with _held_write_lock(_db(tmp_path), marker="external-writer"):
        started = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError) as exc_info:
            store.update_daily_metrics(token, {"probe": 1})
        busy_elapsed = time.perf_counter() - started

    assert "locked" in str(exc_info.value)
    assert MIN_BUSY_SECONDS <= busy_elapsed <= MAX_BUSY_SECONDS

    # Real closeout classification: a tick failed by this exception persists
    # error="OperationalError" (via _safe_error), which _closeout_failure_class
    # maps to "unknown" — the bounded-stop bucket where 2 consecutive failures
    # stop the round until the next scheduled trigger.  This pins the current
    # semantics only; reclassifying SQLITE_BUSY is an independent decision
    # that this test must not drive.
    summary = _failed_summary(exc_info.value)
    assert summary.error == "OperationalError"
    assert _closeout_failure_class(summary) == "unknown"

    # The contended attempt committed nothing; once the external writer
    # releases the lock the same production write succeeds and is the only
    # visible change.
    started = time.perf_counter()
    recovered = store.update_daily_metrics(token, {"probe": 1})
    assert time.perf_counter() - started < 10
    assert json.loads(recovered["metrics_json"]) == {"probe": 1}


def test_second_lease_acquire_is_single_writer(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)

    with _held_write_lock(_db(tmp_path), marker="writer-a"):
        started = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError) as exc_info:
            store.acquire_lease(BUSINESS_DATE, "tick-b", ttl_seconds=60)
        busy_elapsed = time.perf_counter() - started

    assert "locked" in str(exc_info.value)
    assert MIN_BUSY_SECONDS <= busy_elapsed <= MAX_BUSY_SECONDS

    # With the write lock free, the waiting tick takes the lease.
    token = store.acquire_lease(BUSINESS_DATE, "tick-b", ttl_seconds=60)
    assert isinstance(token, LeaseToken)
    assert token.owner == "tick-b"

    # A third tick cannot take the same unexpired day lease: the store's own
    # single-writer rule (LeaseUnavailable), not SQLITE_BUSY, refuses it.
    with pytest.raises(LeaseUnavailable, match="leased by another tick"):
        store.acquire_lease(BUSINESS_DATE, "tick-c", ttl_seconds=60)


def test_wal_readers_proceed_during_held_write_lock(tmp_path: Path) -> None:
    store = _prepared_store(tmp_path)

    with _held_write_lock(_db(tmp_path), marker="uncommitted"):
        started = time.perf_counter()
        reader = _ro_connection(_db(tmp_path))
        try:
            runs = reader.execute("SELECT COUNT(*) FROM daily_runs").fetchone()[0]
            probes = reader.execute("SELECT COUNT(*) FROM contention_probe").fetchone()[0]
        finally:
            reader.close()
        ro_elapsed = time.perf_counter() - started

        assert runs == 1
        assert probes == 0  # the writer's uncommitted row stays invisible
        assert ro_elapsed < 5  # readers never spend the BUSY budget

        started = time.perf_counter()
        with store._reader() as connection:
            runs = connection.execute("SELECT COUNT(*) FROM daily_runs").fetchone()[0]
            probes = connection.execute("SELECT COUNT(*) FROM contention_probe").fetchone()[0]
        store_elapsed = time.perf_counter() - started

        assert (runs, probes) == (1, 0)
        assert store_elapsed < 5


def test_store_constructor_under_contention_is_bounded_and_db_stays_consistent(tmp_path: Path) -> None:
    _prepared_store(tmp_path)

    with _held_write_lock(_db(tmp_path), marker="external-writer"):
        started = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError) as exc_info:
            DailyAuditStore(_db(tmp_path))
        busy_elapsed = time.perf_counter() - started

    assert "locked" in str(exc_info.value)
    assert MIN_BUSY_SECONDS <= busy_elapsed <= MAX_BUSY_SECONDS

    # After release the same construction succeeds and leaves a consistent db.
    store = DailyAuditStore(_db(tmp_path))
    assert store.ensure_run(BUSINESS_DATE)["business_date"] == BUSINESS_DATE
    assert _integrity(_db(tmp_path)) == "ok"


_CHILD_SCRIPT = """
import sqlite3
import sys
import time
from pathlib import Path

db_path, ready_path = sys.argv[1], sys.argv[2]
connection = sqlite3.connect(db_path, timeout=5, isolation_level=None)
connection.execute("PRAGMA busy_timeout = 5000")
connection.execute("BEGIN IMMEDIATE")
connection.execute(
    "INSERT INTO contention_probe(marker) VALUES ('midtransaction-uncommitted')"
)
Path(ready_path).write_text("in-transaction", encoding="utf-8")
time.sleep(30)
"""


def test_process_killed_midtransaction_rolls_back_and_recovers(tmp_path: Path) -> None:
    _prepared_store(tmp_path)
    ready = tmp_path / "child-in-transaction.txt"

    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT, str(_db(tmp_path)), str(ready)]
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                raise AssertionError(
                    f"child exited before signaling its open transaction (exit={child.returncode})"
                )
            time.sleep(0.1)
        assert ready.exists(), "child never signaled an open IMMEDIATE transaction"
    finally:
        child.kill()
        child.wait(15)

    # The killed writer left the WAL dirty; the next connection recovers it,
    # the uncommitted marker row is gone, and the store is fully usable again.
    assert _integrity(_db(tmp_path)) == "ok"
    marker_connection = sqlite3.connect(str(_db(tmp_path)), timeout=5)
    try:
        uncommitted = marker_connection.execute(
            "SELECT COUNT(*) FROM contention_probe WHERE marker='midtransaction-uncommitted'"
        ).fetchone()[0]
    finally:
        marker_connection.close()
    assert uncommitted == 0

    store = DailyAuditStore(_db(tmp_path))
    token = store.acquire_lease(BUSINESS_DATE, "recovery-tick", ttl_seconds=60)
    recovered = store.update_daily_metrics(token, {"recovered": 1})
    assert json.loads(recovered["metrics_json"]) == {"recovered": 1}
