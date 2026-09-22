"""Minimal, fenced SQLite checkpoint store for incremental price-audit ticks.

This module deliberately does not know how to call SaaS, Detail, a model, or
Zhaoshang.  It only durably records the identity and state needed for a later
finite tick to resume safely.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import time_ns
from typing import Any, Iterator, Mapping, Sequence

from app.services.customer_price_semantics import CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES
from app.services.crawl_url_identity import is_placeholder_code
from app.services.page_price_semantics import valid_page_price_result


DEFAULT_TENANT_ID = 188605
DEFAULT_POOL_ID = 222

_REQUIRED_FINAL_METRICS = frozenset({
    "finalized_rows",
    "final_missing_page_price_rows",
    "final_frozen_missing_page_price_rows",
    "final_failed_customer_link_rows",
    "final_appended_failed_customer_link_rows",
    "final_page_price_required_rows",
})
_REQUIRED_FORCE_FINAL_METRICS = frozenset({
    "forced", "forced_incomplete_count", "forced_incomplete_item_count",
    "remote_incomplete_job_count", "blocker_count", "force_finalize",
    "force_finalize_at", "force_finalize_max_incomplete",
    "force_finalize_actual_incomplete", "force_finalize_accept_unresolved_crawl",
    "missing_page_price_rows", "page_price_required_rows",
    "force_finalize_reason", "frozen_snapshot_id", "frozen_row_count",
    "blocker_summary_at_force_finalize", "force_finalize_incomplete_tasks",
})
FINALIZATION_INTENT_KEY = "_finalization_intent"


class LeaseUnavailable(RuntimeError):
    """The OS lock or the fenced database lease is held by another tick."""


class StaleLease(RuntimeError):
    """A state transition was attempted with a superseded lease generation."""


class FrozenSnapshotConflict(RuntimeError):
    """A caller tried to replace frozen or finalized daily output evidence."""


class RemoteIntentConflict(RuntimeError):
    """A logical remote job was reused with a different immutable payload."""


@dataclass(frozen=True)
class LeaseToken:
    run_id: int
    business_date: str
    owner: str
    generation: int


class ProcessLock:
    """A non-blocking, one-byte advisory process lock.

    ``msvcrt`` is used on Windows.  ``fcntl`` is intentionally only a small
    portability fallback for local tests and non-Windows CI.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.metadata_path = self.path.with_name(f"{self.path.name}.owner.json")
        self._file: Any | None = None
        self._held = False
        self._depth = 0
        self._owns_metadata = False

    @property
    def held(self) -> bool:
        return self._held

    @property
    def identity(self) -> str:
        return str(self.path.resolve())

    def acquire(self, *, owner: Mapping[str, Any] | None = None) -> "ProcessLock":
        if self._held:
            self._depth += 1
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.read(1) == b"":
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - Windows is the production target.
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise LeaseUnavailable(f"process lock is already held: {self.path}") from exc
        self._file = handle
        self._held = True
        self._depth = 1
        try:
            # Metadata is never authoritative.  Clear crash leftovers while
            # the OS lock is held so a probe cannot attribute a new owner to
            # the previous process during the atomic metadata refresh.
            self.metadata_path.unlink(missing_ok=True)
            if owner is not None:
                self._write_owner(owner)
        except BaseException:
            self._depth = 1
            self.release()
            raise
        return self

    def _write_owner(self, owner: Mapping[str, Any]) -> None:
        payload = dict(owner)
        payload["lock_identity"] = self.identity
        temporary = self.metadata_path.with_name(
            f".{self.metadata_path.name}.{os.getpid()}.{time_ns()}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.metadata_path)
            self._owns_metadata = True
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def inspect(cls, path: str | Path) -> dict[str, Any]:
        """Probe an existing OS lock without creating or changing runtime state."""
        probe = cls(path)
        result: dict[str, Any] = {
            "active": False,
            "lock_identity": probe.identity,
        }
        if not probe.path.is_file():
            return result
        try:
            handle = probe.path.open("r+b")
        except OSError:
            return result
        try:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - Windows is the production target.
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return result
            except (BlockingIOError, OSError):
                result["active"] = True
        finally:
            handle.close()
        try:
            metadata = json.loads(probe.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            metadata = {}
        if isinstance(metadata, dict):
            for key in (
                "business_date",
                "pid",
                "command",
                "started_at",
                "invocation",
            ):
                if metadata.get(key) not in (None, ""):
                    result[key] = metadata[key]
        return result

    def release(self) -> None:
        if not self._held or self._file is None:
            return
        if self._depth > 1:
            self._depth -= 1
            return
        try:
            if self._owns_metadata:
                self.metadata_path.unlink(missing_ok=True)
                self._owns_metadata = False
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows is the production target.
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            self._held = False
            self._depth = 0

    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


class AuditStateKernel:
    """One SQLite transaction and fencing implementation for both aggregates."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    # Connections are intentionally never cached: a tick may be interrupted or
    # invoked from another thread, and SQLite connections must not be shared.
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        return connection

    @contextmanager
    def _transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_runs (
                    id INTEGER PRIMARY KEY,
                    business_date TEXT NOT NULL,
                    tenant_id INTEGER NOT NULL,
                    pool_id INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'OPEN',
                    cutoff_at TEXT,
                    frozen_snapshot_id INTEGER,
                    final_output_path TEXT,
                    final_manifest_hash TEXT,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    lease_owner TEXT,
                    lease_generation INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(business_date, tenant_id, pool_id),
                    FOREIGN KEY(frozen_snapshot_id) REFERENCES snapshots(id)
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES daily_runs(id) ON DELETE CASCADE,
                    ordered_hash TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    headers_json TEXT NOT NULL DEFAULT '[]',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, ordered_hash, observed_at)
                );
                CREATE TABLE IF NOT EXISTS audit_items (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES daily_runs(id) ON DELETE CASCADE,
                    v_commodity_id TEXT NOT NULL,
                    sku_id TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    customer_fingerprint TEXT NOT NULL,
                    row_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, v_commodity_id, sku_id, source_fingerprint, customer_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS snapshot_members (
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                    row_ordinal INTEGER NOT NULL,
                    audit_item_id INTEGER NOT NULL REFERENCES audit_items(id),
                    duplicate_ordinal INTEGER NOT NULL,
                    change_kind TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, row_ordinal)
                );
                CREATE TABLE IF NOT EXISTS stage_work (
                    id INTEGER PRIMARY KEY,
                    audit_item_id INTEGER NOT NULL REFERENCES audit_items(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    elapsed_seconds REAL NOT NULL DEFAULT 0,
                    retryable INTEGER NOT NULL DEFAULT 1,
                    error_text TEXT,
                    result_json TEXT,
                    claimed_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(audit_item_id, stage)
                );
                CREATE TABLE IF NOT EXISTS remote_jobs (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES daily_runs(id) ON DELETE CASCADE,
                    logical_key TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    mall_key TEXT,
                    input_path TEXT,
                    result_path TEXT,
                    payload_json TEXT,
                    state TEXT NOT NULL DEFAULT 'SUBMIT_INTENT',
                    post_attempted INTEGER NOT NULL DEFAULT 0,
                    remote_identity TEXT,
                    export_path TEXT,
                    result_json TEXT,
                    error_text TEXT,
                    harvest_attempts INTEGER NOT NULL DEFAULT 0,
                    harvest_elapsed_seconds REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, logical_key),
                    UNIQUE(run_id, task_name)
                );
                CREATE TABLE IF NOT EXISTS crawl_work (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES daily_runs(id) ON DELETE CASCADE,
                    mall_key TEXT NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '',
                    sku_id TEXT NOT NULL,
                    exact_url TEXT NOT NULL,
                    backend_version TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    remote_job_id INTEGER REFERENCES remote_jobs(id),
                    result_json TEXT,
                    error_text TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, mall_key, source_id, sku_id, exact_url, backend_version)
                );
                CREATE TABLE IF NOT EXISTS local_fallback_work (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES daily_runs(id) ON DELETE CASCADE,
                    crawl_work_id INTEGER NOT NULL REFERENCES crawl_work(id) ON DELETE CASCADE,
                    mall_key TEXT NOT NULL,
                    exact_url TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    eligible_reason TEXT,
                    last_remote_check_at TEXT,
                    claim_at TEXT,
                    claim_tick_id TEXT,
                    last_attempt_tick_id TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    completed_at TEXT,
                    crawler_revision TEXT,
                    result_json TEXT,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(crawl_work_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_local_fallback_active_url
                    ON local_fallback_work(run_id, exact_url)
                    WHERE state='CLAIMED';
                CREATE TABLE IF NOT EXISTS remote_job_replacements (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES daily_runs(id) ON DELETE CASCADE,
                    old_remote_job_id INTEGER NOT NULL REFERENCES remote_jobs(id),
                    new_remote_job_id INTEGER NOT NULL REFERENCES remote_jobs(id),
                    rebuild_action_id TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    old_batch_number TEXT NOT NULL DEFAULT '',
                    new_batch_number TEXT NOT NULL DEFAULT '',
                    stop_evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, old_remote_job_id),
                    UNIQUE(rebuild_action_id)
                );
                CREATE TABLE IF NOT EXISTS remote_job_rebuild_actions (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES daily_runs(id) ON DELETE CASCADE,
                    action_id TEXT NOT NULL,
                    old_remote_job_id INTEGER NOT NULL REFERENCES remote_jobs(id),
                    remote_identity TEXT NOT NULL,
                    replacement_task_name TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'INTENT',
                    stop_evidence_json TEXT NOT NULL DEFAULT '{}',
                    new_remote_job_id INTEGER,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(action_id)
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(remote_jobs)")}
            if "harvest_attempts" not in columns:
                connection.execute("ALTER TABLE remote_jobs ADD COLUMN harvest_attempts INTEGER NOT NULL DEFAULT 0")
            if "harvest_elapsed_seconds" not in columns:
                connection.execute("ALTER TABLE remote_jobs ADD COLUMN harvest_elapsed_seconds REAL NOT NULL DEFAULT 0")
            if "recovery_attempts" not in columns:
                connection.execute("ALTER TABLE remote_jobs ADD COLUMN recovery_attempts INTEGER NOT NULL DEFAULT 0")
            if "recovery_evidence_json" not in columns:
                connection.execute("ALTER TABLE remote_jobs ADD COLUMN recovery_evidence_json TEXT")
            collision = connection.execute(
                """SELECT run_id, TRIM(remote_identity) AS remote_identity, COUNT(*) AS collision_count
                   FROM remote_jobs
                   WHERE remote_identity IS NOT NULL AND TRIM(remote_identity) <> ''
                   GROUP BY run_id, TRIM(remote_identity)
                   HAVING COUNT(*) > 1
                   ORDER BY run_id, remote_identity
                   LIMIT 1"""
            ).fetchone()
            if collision is not None:
                raise RemoteIntentConflict(
                    "duplicate remote_identity blocks the daily-store schema migration "
                    f"(run_id={collision['run_id']}, count={collision['collision_count']})"
                )
            try:
                connection.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS ux_remote_jobs_run_remote_identity
                       ON remote_jobs(run_id, TRIM(remote_identity))
                       WHERE remote_identity IS NOT NULL AND TRIM(remote_identity) <> ''"""
                )
            except sqlite3.IntegrityError as exc:
                raise RemoteIntentConflict(
                    "duplicate remote_identity blocks the daily-store schema migration"
                ) from exc
        self._migrate_snapshot_observation_constraint()

    def _migrate_snapshot_observation_constraint(self) -> None:
        """Preserve repeated observations of unchanged content across cutoff.

        Early development builds keyed snapshots only by their content hash.
        Rebuild that one table in place so an existing checkpoint can record a
        later observation without changing any snapshot/member ids.
        """
        if self.path == Path(":memory:"):
            return
        connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='snapshots'"
            ).fetchone()
            normalized = "".join(str(row["sql"] or "").upper().split()) if row else ""
            legacy = (
                "UNIQUE(RUN_ID,ORDERED_HASH)" in normalized
                and "UNIQUE(RUN_ID,ORDERED_HASH,OBSERVED_AT)" not in normalized
            )
            if legacy:
                connection.execute(
                    """CREATE TABLE snapshots_observation_migration (
                        id INTEGER PRIMARY KEY,
                        run_id INTEGER NOT NULL REFERENCES daily_runs(id) ON DELETE CASCADE,
                        ordered_hash TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        row_count INTEGER NOT NULL,
                        headers_json TEXT NOT NULL DEFAULT '[]',
                        evidence_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        UNIQUE(run_id, ordered_hash, observed_at)
                    )"""
                )
                connection.execute(
                    """INSERT INTO snapshots_observation_migration
                        (id, run_id, ordered_hash, observed_at, row_count, headers_json, evidence_json, created_at)
                    SELECT id, run_id, ordered_hash, observed_at, row_count, headers_json, evidence_json, created_at
                    FROM snapshots"""
                )
                connection.execute("DROP TABLE snapshots")
                connection.execute("ALTER TABLE snapshots_observation_migration RENAME TO snapshots")
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("snapshot observation migration violated a foreign key")
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    @staticmethod
    def _business_date(value: str) -> str:
        try:
            return date.fromisoformat(value).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("business_date must use YYYY-MM-DD") from exc

    @staticmethod
    def _utc_timestamp(value: str | None, *, field: str) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an explicit ISO-8601 UTC timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must include an explicit UTC offset")
        return parsed.astimezone(UTC).isoformat(timespec="seconds")

    @staticmethod
    def _validate_scope(tenant_id: int, pool_id: int) -> None:
        if tenant_id != DEFAULT_TENANT_ID:
            raise ValueError(f"tenant_id is fixed to {DEFAULT_TENANT_ID}")
        if pool_id != DEFAULT_POOL_ID:
            raise ValueError(f"pool_id is fixed to {DEFAULT_POOL_ID}")

    @staticmethod
    def _expiry(seconds: float) -> str:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("lease TTL must be finite and positive")
        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def _fingerprint(cls, value: Any) -> str:
        return hashlib.sha256(cls._canonical_json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for name in ("post_attempted", "retryable"):
            if name in result:
                result[name] = bool(result[name])
        return result

    def table_names(self) -> set[str]:
        with self._reader() as connection:
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        return {row["name"] for row in rows}

    def _validate_lease(self, connection: sqlite3.Connection, token: LeaseToken) -> dict[str, Any]:
        run = connection.execute("SELECT * FROM daily_runs WHERE id=?", (token.run_id,)).fetchone()
        now = self._utcnow()
        if run is None or run["lease_owner"] != token.owner or int(run["lease_generation"]) != token.generation or not run["lease_expires_at"] or run["lease_expires_at"] <= now:
            raise StaleLease("lease owner, generation, or expiry no longer permits this transition")
        return self._row(run)  # type: ignore[return-value]
