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


DEFAULT_TENANT_ID = 188605
DEFAULT_POOL_ID = 222


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


class DailyAuditStore:
    """Small, per-operation SQLite store with fencing for one business day."""

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

    def ensure_run(
        self,
        business_date: str,
        tenant_id: int = DEFAULT_TENANT_ID,
        pool_id: int = DEFAULT_POOL_ID,
        cutoff_at: str | None = None,
    ) -> dict[str, Any]:
        self._validate_scope(tenant_id, pool_id)
        business_date = self._business_date(business_date)
        cutoff_at = self._utc_timestamp(cutoff_at, field="cutoff_at")
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO daily_runs(business_date, tenant_id, pool_id, cutoff_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(business_date, tenant_id, pool_id) DO NOTHING""",
                (business_date, tenant_id, pool_id, cutoff_at, now, now),
            )
            row = connection.execute(
                "SELECT * FROM daily_runs WHERE business_date=? AND tenant_id=? AND pool_id=?",
                (business_date, tenant_id, pool_id),
            ).fetchone()
            assert row is not None
            if cutoff_at is not None and row["cutoff_at"] is None:
                connection.execute("UPDATE daily_runs SET cutoff_at=?, updated_at=? WHERE id=?", (cutoff_at, now, row["id"]))
                row = connection.execute("SELECT * FROM daily_runs WHERE id=?", (row["id"],)).fetchone()
                assert row is not None
            if cutoff_at is not None and row["cutoff_at"] is not None and row["cutoff_at"] != cutoff_at:
                raise ValueError("cutoff_at conflicts with the existing daily run")
            return self._row(row)  # type: ignore[return-value]

    def get_run(self, business_date: str, tenant_id: int = DEFAULT_TENANT_ID, pool_id: int = DEFAULT_POOL_ID) -> dict[str, Any] | None:
        self._validate_scope(tenant_id, pool_id)
        business_date = self._business_date(business_date)
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM daily_runs WHERE business_date=? AND tenant_id=? AND pool_id=?", (business_date, tenant_id, pool_id)).fetchone())

    def acquire_lease(
        self,
        business_date: str,
        owner: str,
        ttl_seconds: float,
        tenant_id: int = DEFAULT_TENANT_ID,
        pool_id: int = DEFAULT_POOL_ID,
        *,
        allow_unexpired_takeover: bool = False,
        process_lock: ProcessLock | None = None,
    ) -> LeaseToken:
        self._validate_scope(tenant_id, pool_id)
        business_date = self._business_date(business_date)
        self.ensure_run(business_date, tenant_id, pool_id)
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            run = connection.execute("SELECT * FROM daily_runs WHERE business_date=? AND tenant_id=? AND pool_id=?", (business_date, tenant_id, pool_id)).fetchone()
            assert run is not None
            expires = run["lease_expires_at"]
            if run["lease_owner"] and expires and expires > now:
                if not allow_unexpired_takeover:
                    raise LeaseUnavailable(f"daily run {business_date} is leased by another tick")
                if process_lock is None or not process_lock.held:
                    raise LeaseUnavailable("unexpired lease takeover requires a held ProcessLock")
            generation = int(run["lease_generation"]) + 1
            connection.execute(
                """UPDATE daily_runs SET lease_owner=?, lease_generation=?, lease_expires_at=?, updated_at=? WHERE id=?""",
                (owner, generation, self._expiry(ttl_seconds), now, run["id"]),
            )
            return LeaseToken(int(run["id"]), business_date, owner, generation)

    def renew_lease(self, token: LeaseToken, ttl_seconds: float) -> LeaseToken:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            connection.execute(
                "UPDATE daily_runs SET lease_expires_at=?, updated_at=? WHERE id=? AND lease_owner=? AND lease_generation=?",
                (self._expiry(ttl_seconds), self._utcnow(), token.run_id, token.owner, token.generation),
            )
        return token

    def release_lease(self, token: LeaseToken) -> None:
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE daily_runs SET lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                   WHERE id=? AND lease_owner=? AND lease_generation=?""",
                (self._utcnow(), token.run_id, token.owner, token.generation),
            )

    def force_lease_expiry_for_test(self, business_date: str) -> None:
        """Test-only deterministic expiry hook; production ticks never call it."""
        with self._transaction(immediate=True) as connection:
            connection.execute("UPDATE daily_runs SET lease_expires_at=? WHERE business_date=?", ("1970-01-01T00:00:00+00:00", business_date))

    def _validate_lease(self, connection: sqlite3.Connection, token: LeaseToken) -> dict[str, Any]:
        run = connection.execute("SELECT * FROM daily_runs WHERE id=?", (token.run_id,)).fetchone()
        now = self._utcnow()
        if run is None or run["lease_owner"] != token.owner or int(run["lease_generation"]) != token.generation or not run["lease_expires_at"] or run["lease_expires_at"] <= now:
            raise StaleLease("lease owner, generation, or expiry no longer permits this transition")
        return self._row(run)  # type: ignore[return-value]

    @staticmethod
    def _source_fields(row: Mapping[str, Any]) -> tuple[str, str]:
        v = row.get("vCommodityId", row.get("documentId"))
        sku = row.get("skuId")
        if v is None or sku is None or not str(v).strip() or not str(sku).strip():
            raise ValueError("vCommodityId/documentId and skuId are required for automatic audit identity")
        return str(v).strip(), str(sku).strip()

    def ingest_snapshot(
        self,
        token: LeaseToken,
        rows: Sequence[Mapping[str, Any]],
        observed_at: str | None = None,
        *,
        source_fingerprints: Sequence[str] | None = None,
        customer_fingerprints: Sequence[str] | None = None,
        headers: Sequence[str] | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_fps = list(source_fingerprints) if source_fingerprints is not None else [self._fingerprint(row) for row in rows]
        customer_fps = list(customer_fingerprints) if customer_fingerprints is not None else ["" for _ in rows]
        if len(source_fps) != len(rows) or len(customer_fps) != len(rows):
            raise ValueError("source_fingerprints and customer_fingerprints must match snapshot row count")
        if any(not isinstance(value, str) or not value for value in source_fps):
            raise ValueError("source_fingerprints must be non-empty strings")
        ordered_hash = self._fingerprint(list(zip(source_fps, customer_fps, strict=True)))
        now = self._utcnow()
        explicit_observation = self._utc_timestamp(observed_at, field="observed_at")
        observation = explicit_observation or now
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            if explicit_observation is None:
                # Direct store callers without authoritative observation evidence
                # retain the original content-idempotent behavior. Production
                # discovery always supplies observed_at, so an unchanged list
                # observed after cutoff is still recorded as a new observation.
                existing = connection.execute(
                    "SELECT * FROM snapshots WHERE run_id=? AND ordered_hash=? ORDER BY observed_at DESC, id DESC LIMIT 1",
                    (token.run_id, ordered_hash),
                ).fetchone()
            else:
                existing = connection.execute(
                    "SELECT * FROM snapshots WHERE run_id=? AND ordered_hash=? AND observed_at=?",
                    (token.run_id, ordered_hash, observation),
                ).fetchone()
            if existing is not None:
                members = self._snapshot_members(connection, int(existing["id"]))
                current_members = [{**member, "change_kind": "UNCHANGED"} for member in members]
                return {
                    "snapshot": self._row(existing), "members": current_members, "member_count": len(members), "reused": True,
                    "status_counts": {"UNCHANGED": len(members)} if members else {},
                }
            run = connection.execute("SELECT frozen_snapshot_id FROM daily_runs WHERE id=?", (token.run_id,)).fetchone()
            if run is not None and run["frozen_snapshot_id"] is not None:
                raise FrozenSnapshotConflict("a frozen daily run cannot accept different snapshot membership")
            previous = connection.execute("SELECT id FROM snapshots WHERE run_id=? ORDER BY id DESC LIMIT 1", (token.run_id,)).fetchone()
            previous_fps: dict[tuple[str, str], set[str]] = {}
            if previous is not None:
                for item in connection.execute(
                    """SELECT a.v_commodity_id, a.sku_id, a.source_fingerprint, a.customer_fingerprint
                       FROM snapshot_members m JOIN audit_items a ON a.id=m.audit_item_id
                       WHERE m.snapshot_id=?""",
                    (previous["id"],),
                ):
                    previous_fps.setdefault((item["v_commodity_id"], item["sku_id"]), set()).add(
                        f"{item['source_fingerprint']}\x00{item['customer_fingerprint']}"
                    )
            cursor = connection.execute(
                """INSERT INTO snapshots(run_id, ordered_hash, observed_at, row_count, headers_json, evidence_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (token.run_id, ordered_hash, observation, len(rows), self._canonical_json(list(headers or [])), self._canonical_json(dict(evidence or {})), now),
            )
            snapshot_id = int(cursor.lastrowid)
            seen_occurrences: dict[tuple[str, str, str], int] = {}
            members: list[dict[str, Any]] = []
            for ordinal, (row, source_fp, customer_fp) in enumerate(zip(rows, source_fps, customer_fps, strict=True)):
                v, sku = self._source_fields(row)
                item = connection.execute(
                    "SELECT * FROM audit_items WHERE run_id=? AND v_commodity_id=? AND sku_id=? AND source_fingerprint=? AND customer_fingerprint=?",
                    (token.run_id, v, sku, source_fp, customer_fp),
                ).fetchone()
                if item is None:
                    cursor = connection.execute(
                        """INSERT INTO audit_items(run_id, v_commodity_id, sku_id, source_fingerprint, customer_fingerprint, row_json, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (token.run_id, v, sku, source_fp, customer_fp, self._canonical_json(row), now),
                    )
                    item_id = int(cursor.lastrowid)
                else:
                    item_id = int(item["id"])
                prior = previous_fps.get((v, sku))
                version = f"{source_fp}\x00{customer_fp}"
                kind = "NEW" if prior is None else ("UNCHANGED" if version in prior else "CHANGED")
                url = str(row.get("extLink") or "").strip()
                occurrence = (v, sku, url)
                duplicate_ordinal = seen_occurrences.get(occurrence, 0)
                seen_occurrences[occurrence] = duplicate_ordinal + 1
                connection.execute(
                    """INSERT INTO snapshot_members(snapshot_id, row_ordinal, audit_item_id, duplicate_ordinal, change_kind)
                       VALUES (?, ?, ?, ?, ?)""",
                    (snapshot_id, ordinal, item_id, duplicate_ordinal, kind),
                )
                members.append({"snapshot_id": snapshot_id, "row_ordinal": ordinal, "audit_item_id": item_id, "duplicate_ordinal": duplicate_ordinal, "change_kind": kind})
            snapshot = self._row(connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone())
            return {"snapshot": snapshot, "members": members, "member_count": len(members), "reused": False, "status_counts": self._count_kinds(members)}

    @staticmethod
    def _count_kinds(members: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for member in members:
            kind = str(member["change_kind"])
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    @staticmethod
    def _snapshot_members(connection: sqlite3.Connection, snapshot_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute("SELECT * FROM snapshot_members WHERE snapshot_id=? ORDER BY row_ordinal", (snapshot_id,))]

    def snapshot_members(self, snapshot_id: int) -> list[dict[str, Any]]:
        with self._reader() as connection:
            return self._snapshot_members(connection, snapshot_id)

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone())

    def list_snapshots(self, business_date: str) -> list[dict[str, Any]]:
        """Return that day's authoritative observations in chronological order."""
        business_date = self._business_date(business_date)
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT s.* FROM snapshots s
                   JOIN daily_runs r ON r.id=s.run_id
                   WHERE r.business_date=? AND r.tenant_id=? AND r.pool_id=?
                   ORDER BY s.observed_at, s.id""",
                (business_date, DEFAULT_TENANT_ID, DEFAULT_POOL_ID),
            ).fetchall()
        return [self._row(row) for row in rows]  # type: ignore[misc]

    def latest_snapshot(self, business_date: str) -> dict[str, Any] | None:
        run = self.get_run(business_date)
        if run is None:
            return None
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM snapshots WHERE run_id=? ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone())

    def first_snapshot_at_or_after(self, business_date: str, cutoff_at: str) -> dict[str, Any] | None:
        run = self.get_run(business_date)
        if run is None:
            return None
        cutoff = self._utc_timestamp(cutoff_at, field="cutoff_at")
        with self._reader() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM snapshots WHERE run_id=? AND observed_at>=? ORDER BY observed_at, id LIMIT 1",
                    (run["id"], cutoff),
                ).fetchone()
            )

    def ordered_snapshot_items(self, snapshot_id: int) -> list[dict[str, Any]]:
        """Return the preserved ordered multiset with the joined canonical row."""
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT m.row_ordinal, m.duplicate_ordinal, m.change_kind, a.*
                   FROM snapshot_members m JOIN audit_items a ON a.id=m.audit_item_id
                   WHERE m.snapshot_id=? ORDER BY m.row_ordinal""",
                (snapshot_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = self._row(row)
                assert item is not None
                item["row"] = json.loads(item["row_json"])
                result.append(item)
            return result

    def get_audit_item(self, audit_item_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM audit_items WHERE id=?", (audit_item_id,)).fetchone())

    def ensure_stage_work(self, token: LeaseToken, audit_item_id: int, stage: str, fingerprint: str) -> dict[str, Any]:
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            item = connection.execute("SELECT run_id FROM audit_items WHERE id=?", (audit_item_id,)).fetchone()
            if item is None or int(item["run_id"]) != token.run_id:
                raise ValueError("stage work must belong to the leased daily run")
            work = connection.execute("SELECT * FROM stage_work WHERE audit_item_id=? AND stage=?", (audit_item_id, stage)).fetchone()
            if work is None:
                cursor = connection.execute("INSERT INTO stage_work(audit_item_id, stage, fingerprint, updated_at) VALUES (?, ?, ?, ?)", (audit_item_id, stage, fingerprint, now))
                work = connection.execute("SELECT * FROM stage_work WHERE id=?", (cursor.lastrowid,)).fetchone()
            elif work["fingerprint"] != fingerprint:
                connection.execute(
                    """UPDATE stage_work SET fingerprint=?, state='PENDING', attempts=0, elapsed_seconds=0,
                       retryable=1, error_text=NULL, result_json=NULL, claimed_at=NULL, updated_at=? WHERE id=?""",
                    (fingerprint, now, work["id"]),
                )
                work = connection.execute("SELECT * FROM stage_work WHERE id=?", (work["id"],)).fetchone()
            return self._row(work)  # type: ignore[return-value]

    def reuse_successful_stage_work(self, token: LeaseToken, work_id: int) -> dict[str, Any] | None:
        """Copy same-run narrow-fingerprint success into a new item version."""
        with self._transaction(immediate=True) as connection:
            self._validate_stage_lease(connection, token, work_id)
            target = connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()
            assert target is not None
            if target["state"] == "SUCCESS":
                return self._row(target)
            if target["state"] == "RECOVERED_PENDING_REVIEW":
                return None
            source = connection.execute(
                """SELECT s.* FROM stage_work s JOIN audit_items a ON a.id=s.audit_item_id
                   WHERE a.run_id=? AND s.stage=? AND s.fingerprint=? AND s.state='SUCCESS' AND s.id<>?
                   ORDER BY s.id LIMIT 1""",
                (token.run_id, target["stage"], target["fingerprint"], work_id),
            ).fetchone()
            if source is None:
                return None
            connection.execute(
                """UPDATE stage_work SET state='SUCCESS', retryable=0, result_json=?, error_text=NULL,
                   claimed_at=NULL, updated_at=? WHERE id=?""",
                (source["result_json"], self._utcnow(), work_id),
            )
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())

    def get_stage_work(self, work_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())

    def list_stage_work(
        self, token: LeaseToken, *, stage: str | None = None, states: set[str] | None = None
    ) -> list[dict[str, Any]]:
        with self._transaction(immediate=False) as connection:
            self._validate_lease(connection, token)
            clauses = ["a.run_id=?"]
            params: list[Any] = [token.run_id]
            if stage is not None:
                clauses.append("s.stage=?")
                params.append(stage)
            if states:
                placeholders = ",".join("?" for _ in states)
                clauses.append(f"s.state IN ({placeholders})")
                params.extend(sorted(states))
            rows = connection.execute(
                f"SELECT s.* FROM stage_work s JOIN audit_items a ON a.id=s.audit_item_id WHERE {' AND '.join(clauses)} ORDER BY s.id",
                params,
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def list_stage_work_for_business_date(
        self,
        business_date: str,
        *,
        stage: str | None = None,
        states: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read stage evidence without acquiring or mutating the daily lease."""
        clauses = ["r.business_date=?"]
        params: list[Any] = [self._business_date(business_date)]
        if stage is not None:
            clauses.append("s.stage=?")
            params.append(stage)
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"s.state IN ({placeholders})")
            params.extend(sorted(states))
        with self._reader() as connection:
            rows = connection.execute(
                f"""SELECT s.* FROM stage_work s
                    JOIN audit_items a ON a.id=s.audit_item_id
                    JOIN daily_runs r ON r.id=a.run_id
                    WHERE {' AND '.join(clauses)} ORDER BY s.id""",
                params,
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def claim_stage_work(self, token: LeaseToken, work_id: int, *, max_attempts: int, max_elapsed_seconds: float) -> dict[str, Any] | None:
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            work = connection.execute("""SELECT s.*, a.run_id FROM stage_work s JOIN audit_items a ON a.id=s.audit_item_id WHERE s.id=?""", (work_id,)).fetchone()
            if work is None or int(work["run_id"]) != token.run_id:
                raise ValueError("stage work must belong to the leased daily run")
            if work["state"] not in {"PENDING", "RETRYABLE_FAILED"}:
                return None
            if int(work["attempts"]) >= max_attempts or float(work["elapsed_seconds"]) >= max_elapsed_seconds:
                connection.execute("UPDATE stage_work SET state='TERMINAL_FAILED', retryable=0, updated_at=? WHERE id=?", (now, work_id))
                return None
            connection.execute("UPDATE stage_work SET state='CLAIMED', attempts=attempts+1, claimed_at=?, updated_at=? WHERE id=?", (now, now, work_id))
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())

    def complete_stage_work(self, token: LeaseToken, work_id: int, result: Any) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_stage_lease(connection, token, work_id)
            work = connection.execute("SELECT state FROM stage_work WHERE id=?", (work_id,)).fetchone()
            if work is None or work["state"] != "CLAIMED":
                raise ValueError("only claimed stage work can be completed")
            connection.execute("UPDATE stage_work SET state='SUCCESS', retryable=0, result_json=?, error_text=NULL, claimed_at=NULL, updated_at=? WHERE id=?", (self._canonical_json(result), self._utcnow(), work_id))
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())  # type: ignore[return-value]

    def complete_recovered_model_work(
        self, token: LeaseToken, work_id: int, result: Any
    ) -> dict[str, Any]:
        """Persist recovered model evidence behind an explicit review hold."""
        with self._transaction(immediate=True) as connection:
            self._validate_stage_lease(connection, token, work_id)
            work = connection.execute(
                "SELECT * FROM stage_work WHERE id=?", (work_id,)
            ).fetchone()
            if work is None or work["stage"] != "MODEL" or work["state"] != "CLAIMED":
                raise ValueError("only claimed MODEL work can enter recovery review")
            connection.execute(
                """UPDATE stage_work
                   SET state='RECOVERED_PENDING_REVIEW', retryable=0, result_json=?,
                       error_text=NULL, claimed_at=NULL, updated_at=? WHERE id=?""",
                (self._canonical_json(result), self._utcnow(), work_id),
            )
            return self._row(
                connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()
            )  # type: ignore[return-value]

    def release_recovered_model_work_batch(
        self, token: LeaseToken, work_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Atomically release exact reviewed model results to normal workflow."""
        ids = [int(value) for value in work_ids]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("model recovery release ids must be non-empty and unique")
        placeholders = ",".join("?" for _ in ids)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            rows = connection.execute(
                f"""SELECT s.*, a.run_id FROM stage_work s
                    JOIN audit_items a ON a.id=s.audit_item_id
                    WHERE s.id IN ({placeholders}) ORDER BY s.id""",
                ids,
            ).fetchall()
            if len(rows) != len(ids):
                raise ValueError("every model recovery release id must exist")
            by_id = {int(row["id"]): row for row in rows}
            for work_id in ids:
                work = by_id[work_id]
                if (
                    int(work["run_id"]) != token.run_id
                    or work["stage"] != "MODEL"
                    or work["state"] != "RECOVERED_PENDING_REVIEW"
                    or work["result_json"] is None
                ):
                    raise ValueError(
                        f"stage_work id {work_id} is not a reviewed recoverable MODEL result"
                    )
            connection.execute(
                f"UPDATE stage_work SET state='SUCCESS', updated_at=? WHERE id IN ({placeholders})",
                (self._utcnow(), *ids),
            )
            return [
                self._row(
                    connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()
                )
                for work_id in ids
            ]  # type: ignore[list-item]

    def fail_stage_work(self, token: LeaseToken, work_id: int, *, elapsed_seconds: float, error: str, retryable: bool, max_attempts: int, max_elapsed_seconds: float) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_stage_lease(connection, token, work_id)
            work = connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone()
            assert work is not None
            if work["state"] != "CLAIMED":
                raise ValueError("only claimed stage work can be failed")
            total = float(work["elapsed_seconds"]) + max(0, elapsed_seconds)
            terminal = not retryable or int(work["attempts"]) >= max_attempts or total >= max_elapsed_seconds
            state = "TERMINAL_FAILED" if terminal else "RETRYABLE_FAILED"
            connection.execute("UPDATE stage_work SET state=?, elapsed_seconds=?, retryable=?, error_text=?, claimed_at=NULL, updated_at=? WHERE id=?", (state, total, 0 if terminal else 1, error, self._utcnow(), work_id))
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())  # type: ignore[return-value]

    def recover_claimed_stage_work(self, token: LeaseToken) -> int:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            result = connection.execute("""UPDATE stage_work SET state='RETRYABLE_FAILED', error_text=COALESCE(error_text, 'interrupted before completion'), claimed_at=NULL, updated_at=?
                WHERE state='CLAIMED' AND audit_item_id IN (SELECT id FROM audit_items WHERE run_id=?)""", (self._utcnow(), token.run_id))
            return result.rowcount

    def requeue_terminal_stage_work(self, token: LeaseToken, work_id: int) -> dict[str, Any]:
        """Explicit operator/engine action; never silently retries terminal evidence."""
        with self._transaction(immediate=True) as connection:
            self._validate_stage_lease(connection, token, work_id)
            work = connection.execute("SELECT state FROM stage_work WHERE id=?", (work_id,)).fetchone()
            if work is None or work["state"] != "TERMINAL_FAILED":
                raise ValueError("only terminal stage work can be explicitly requeued")
            connection.execute("UPDATE stage_work SET state='PENDING', attempts=0, elapsed_seconds=0, retryable=1, error_text=NULL, claimed_at=NULL, updated_at=? WHERE id=?", (self._utcnow(), work_id))
            return self._row(connection.execute("SELECT * FROM stage_work WHERE id=?", (work_id,)).fetchone())  # type: ignore[return-value]

    def requeue_terminal_model_work_batch_preserving_evidence(
        self, token: LeaseToken, work_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Atomically make an exact terminal MODEL set claimable without resetting its budget."""
        ids = [int(work_id) for work_id in work_ids]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("stage work ids must be a non-empty unique list")
        placeholders = ",".join("?" for _ in ids)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            rows = connection.execute(
                f"""SELECT s.*, a.run_id FROM stage_work s
                    JOIN audit_items a ON a.id=s.audit_item_id
                    WHERE s.id IN ({placeholders}) ORDER BY s.id""",
                ids,
            ).fetchall()
            if len(rows) != len(ids):
                raise ValueError("every selected stage work id must exist")
            if any(
                int(row["run_id"]) != token.run_id
                or row["stage"] != "MODEL"
                or row["state"] != "TERMINAL_FAILED"
                for row in rows
            ):
                raise ValueError("every selected stage must be terminal MODEL work in the leased run")
            result = connection.execute(
                f"""UPDATE stage_work
                    SET state='RETRYABLE_FAILED', retryable=1, claimed_at=NULL, updated_at=?
                    WHERE id IN ({placeholders}) AND state='TERMINAL_FAILED' AND stage='MODEL'""",
                [self._utcnow(), *ids],
            )
            if result.rowcount != len(ids):
                raise ValueError("terminal MODEL requeue did not update the exact selected set")
            updated = connection.execute(
                f"SELECT * FROM stage_work WHERE id IN ({placeholders}) ORDER BY id", ids
            ).fetchall()
            return [self._row(row) for row in updated]  # type: ignore[list-item]

    def _validate_stage_lease(self, connection: sqlite3.Connection, token: LeaseToken, work_id: int) -> None:
        self._validate_lease(connection, token)
        row = connection.execute("SELECT a.run_id FROM stage_work s JOIN audit_items a ON a.id=s.audit_item_id WHERE s.id=?", (work_id,)).fetchone()
        if row is None or int(row["run_id"]) != token.run_id:
            raise ValueError("stage work must belong to the leased daily run")

    def prepare_remote_intent(
        self,
        token: LeaseToken,
        logical_key: str,
        task_name: str,
        payload_hash: str,
        *,
        mall_key: str | None = None,
        input_path: str | None = None,
        result_path: str | None = None,
        payload_json: Any | None = None,
    ) -> dict[str, Any]:
        now = self._utcnow()
        encoded_payload = self._canonical_json(payload_json) if payload_json is not None else None
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = connection.execute("SELECT * FROM remote_jobs WHERE run_id=? AND logical_key=?", (token.run_id, logical_key)).fetchone()
            if job is not None:
                expected = {
                    "task_name": task_name,
                    "payload_hash": payload_hash,
                    "mall_key": mall_key,
                    "input_path": input_path,
                    "result_path": result_path,
                    "payload_json": encoded_payload,
                }
                if any(job[name] != value for name, value in expected.items()):
                    raise RemoteIntentConflict("logical remote job already has different immutable intent evidence")
                return self._row(job)  # type: ignore[return-value]
            try:
                cursor = connection.execute(
                    """INSERT INTO remote_jobs(run_id, logical_key, task_name, payload_hash, mall_key, input_path, result_path, payload_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (token.run_id, logical_key, task_name, payload_hash, mall_key, input_path, result_path, encoded_payload, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise RemoteIntentConflict("task name is already reserved by another logical remote job") from exc
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (cursor.lastrowid,)).fetchone())  # type: ignore[return-value]

    def get_remote_job(self, job_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())

    def list_remote_stall_evidence(self, run_id: int) -> list[dict[str, Any]]:
        """Lease-free read of pending/manual remote error evidence for the stall breaker."""
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT id, state, error_text, updated_at FROM remote_jobs
                   WHERE run_id=? AND state IN ('PRIORITY_PENDING', 'MANUAL_REQUIRED')
                     AND error_text IS NOT NULL
                   ORDER BY id""",
                (run_id,),
            ).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def list_remote_jobs(self, token: LeaseToken, *, states: set[str] | None = None) -> list[dict[str, Any]]:
        with self._transaction(immediate=False) as connection:
            self._validate_lease(connection, token)
            if states:
                placeholders = ",".join("?" for _ in states)
                rows = connection.execute(f"SELECT * FROM remote_jobs WHERE run_id=? AND state IN ({placeholders}) ORDER BY id", (token.run_id, *sorted(states))).fetchall()
            else:
                rows = connection.execute("SELECT * FROM remote_jobs WHERE run_id=? ORDER BY id", (token.run_id,)).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def claim_remote_submission(self, token: LeaseToken, job_id: int) -> dict[str, Any] | None:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if job["state"] != "SUBMIT_INTENT" or int(job["post_attempted"]):
                return None
            connection.execute("UPDATE remote_jobs SET post_attempted=1, updated_at=? WHERE id=?", (self._utcnow(), job_id))
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())

    def mark_submit_unknown(self, token: LeaseToken, job_id: int, error: str) -> dict[str, Any]:
        return self._set_job_state(token, job_id, "SUBMIT_UNKNOWN", error=error, require_post_attempted=True)

    def release_remote_submission_claim(self, token: LeaseToken, job_id: int, error: str) -> dict[str, Any]:
        """Retreat a burned claim only with proof the POST never started.

        Only an un-dispatched submit intent with no confirmed remote identity
        may return to claimable state; the immutable intent, bindings and
        uniqueness stay untouched and the durable diagnostic keeps the
        pre-send reason auditable.
        """
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "SUBMIT_INTENT":
                raise RemoteIntentConflict("pre-send release requires the un-dispatched submit intent state")
            if str(job["remote_identity"] or "").strip():
                raise RemoteIntentConflict("pre-send release requires no confirmed remote identity")
            if int(job["post_attempted"]):
                connection.execute(
                    "UPDATE remote_jobs SET post_attempted=0, error_text=?, updated_at=? WHERE id=?",
                    (error, self._utcnow(), job_id),
                )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())

    def mark_remote_manual(self, token: LeaseToken, job_id: int, error: str) -> dict[str, Any]:
        return self._set_job_state(token, job_id, "MANUAL_REQUIRED", error=error)

    def mark_remote_zero_write_manual(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        evidence: Mapping[str, Any],
        error: str,
    ) -> dict[str, Any]:
        """Terminalize a proven zero-write recovery outcome atomically.

        The truthful evidence document and the MANUAL_REQUIRED state must
        land in ONE transaction: writing evidence and state separately could
        crash between them and strand the write-ahead DISABLE_SENT phase (a
        pending remote write that never happened) on a terminal job — after
        a human requeue that fake pending phase would bypass the
        EXTERNALLY_DISABLED guard and could auto-enable a task a human
        deliberately disabled.  Legal only from RUNNING with an unburned
        attempt and the exact confirmed remote identity.
        """
        from app.services.remote_running_recovery import encode_recovery_evidence

        normalized_identity = str(remote_identity or "").strip()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "RUNNING":
                raise RemoteIntentConflict("zero-write manual requires the RUNNING state")
            if int(job["recovery_attempts"] or 0) != 0:
                raise RemoteIntentConflict("zero-write manual requires an unburned recovery attempt")
            if not normalized_identity or str(job["remote_identity"] or "").strip() != normalized_identity:
                raise RemoteIntentConflict("zero-write manual requires the exact confirmed remote identity")
            connection.execute(
                "UPDATE remote_jobs SET state='MANUAL_REQUIRED', recovery_evidence_json=?, error_text=?, updated_at=? WHERE id=?",
                (encode_recovery_evidence(evidence), str(error), self._utcnow(), job_id),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def requeue_manual_priority_job(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
    ) -> dict[str, Any]:
        """Explicitly requeue a confirmed remote task after a priority-only repair."""
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            remote_identity = str(remote_identity or "").strip()
            if (
                job["state"] != "MANUAL_REQUIRED"
                or not int(job["post_attempted"])
                or not remote_identity
                or str(job["remote_identity"] or "").strip() != remote_identity
            ):
                raise RemoteIntentConflict("manual priority requeue requires the exact confirmed remote identity")
            connection.execute(
                "UPDATE remote_jobs SET state='PRIORITY_PENDING', error_text=NULL, updated_at=? WHERE id=?",
                (self._utcnow(), job_id),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def requeue_manual_harvest_job(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
    ) -> dict[str, Any]:
        """Explicitly retry export mapping for one confirmed remote task."""
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            remote_identity = str(remote_identity or "").strip()
            if (
                job["state"] != "MANUAL_REQUIRED"
                or not int(job["post_attempted"])
                or not remote_identity
                or str(job["remote_identity"] or "").strip() != remote_identity
            ):
                raise RemoteIntentConflict(
                    "manual harvest requeue requires the exact confirmed remote identity"
                )
            connection.execute(
                "UPDATE remote_jobs SET state='SUBMITTED', error_text=NULL, updated_at=? WHERE id=?",
                (self._utcnow(), job_id),
            )
            return self._row(
                connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone()
            )  # type: ignore[return-value]

    def recover_ambiguous_remote_submissions(self, token: LeaseToken) -> int:
        """Fail closed after a crash between POST dispatch and durable outcome."""
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            result = connection.execute(
                """UPDATE remote_jobs SET state='SUBMIT_UNKNOWN', error_text=COALESCE(error_text, 'interrupted after POST dispatch'), updated_at=?
                   WHERE run_id=? AND state='SUBMIT_INTENT' AND post_attempted=1""",
                (self._utcnow(), token.run_id),
            )
            return result.rowcount

    def reconcile_remote_job(self, token: LeaseToken, job_id: int, *, remote_identity: str, state: str = "SUBMITTED", export_path: str | None = None, result: Any | None = None) -> dict[str, Any]:
        if state not in {"PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "COMPLETED"}:
            raise ValueError("remote reconciliation must use a known non-create state")
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if not int(job["post_attempted"]):
                raise RemoteIntentConflict("remote reconciliation requires durable post_attempted evidence")
            remote_identity = str(remote_identity or "").strip()
            if not remote_identity:
                raise RemoteIntentConflict("remote reconciliation requires an exact remote identity")
            existing_identity = str(job["remote_identity"] or "").strip()
            if existing_identity and existing_identity != remote_identity:
                raise RemoteIntentConflict("remote identity is immutable once recorded")
            legal = {
                "SUBMIT_INTENT": {"PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "COMPLETED"},
                "SUBMIT_UNKNOWN": {"PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "COMPLETED"},
                "PRIORITY_PENDING": {"PRIORITY_PENDING", "SUBMITTED"},
                "SUBMITTED": {"SUBMITTED", "RUNNING", "EXPORTED", "COMPLETED"},
                "RUNNING": {"RUNNING", "EXPORTED", "COMPLETED"},
                "EXPORTED": {"EXPORTED", "COMPLETED"},
                "COMPLETED": {"COMPLETED"},
            }
            if state not in legal.get(str(job["state"]), set()):
                raise RemoteIntentConflict(f"illegal remote state transition {job['state']} -> {state}")
            try:
                connection.execute("UPDATE remote_jobs SET state=?, remote_identity=?, export_path=COALESCE(?, export_path), result_json=COALESCE(?, result_json), error_text=NULL, updated_at=? WHERE id=?", (state, remote_identity, export_path, self._canonical_json(result) if result is not None else None, self._utcnow(), job_id))
            except sqlite3.IntegrityError as exc:
                raise RemoteIntentConflict(
                    "remote identity is already bound to another logical job in this daily run"
                ) from exc
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def claim_harvest(self, token: LeaseToken, job_id: int, *, max_attempts: int, max_elapsed_seconds: float) -> dict[str, Any] | None:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if job["state"] not in {"PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "SUBMIT_UNKNOWN"}:
                return None
            if int(job["harvest_attempts"]) >= max_attempts or float(job["harvest_elapsed_seconds"]) >= max_elapsed_seconds:
                connection.execute("UPDATE remote_jobs SET state='MANUAL_REQUIRED', error_text=COALESCE(error_text, 'harvest limit reached'), updated_at=? WHERE id=?", (self._utcnow(), job_id))
                return None
            connection.execute("UPDATE remote_jobs SET harvest_attempts=harvest_attempts+1, updated_at=? WHERE id=?", (self._utcnow(), job_id))
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())

    def record_harvest(self, token: LeaseToken, job_id: int, *, elapsed_seconds: float, max_attempts: int, max_elapsed_seconds: float, error: str | None = None) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            elapsed = float(job["harvest_elapsed_seconds"]) + max(0.0, elapsed_seconds)
            attempts = int(job["harvest_attempts"])
            # The last permitted harvest may return terminal evidence. That
            # evidence must win over the retry cap instead of being discarded.
            manual = str(job["state"]) not in {"EXPORTED", "COMPLETED"} and (
                attempts >= max_attempts or elapsed >= max_elapsed_seconds
            )
            state = "MANUAL_REQUIRED" if manual else str(job["state"])
            connection.execute("UPDATE remote_jobs SET state=?, harvest_elapsed_seconds=?, error_text=?, updated_at=? WHERE id=?", (state, elapsed, error, self._utcnow(), job_id))
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def update_remote_recovery_evidence(self, token: LeaseToken, job_id: int, evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the RUNNING-stall recovery evidence document for one job.

        The evidence document is the only authority for progress observations
        and restart phases; it never mutates ``result_json``/``error_text``
        semantics.  Any state may record probe evidence, but the update is
        fenced by the daily lease like every other durable write.
        """
        from app.services.remote_running_recovery import encode_recovery_evidence

        encoded = encode_recovery_evidence(evidence)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) == "COMPLETED":
                raise RemoteIntentConflict("completed remote job evidence is immutable")
            connection.execute(
                "UPDATE remote_jobs SET recovery_evidence_json=?, updated_at=? WHERE id=?",
                (encoded, self._utcnow(), job_id),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def mark_remote_recovery_attempted(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Burn the single automatic restart slot for one exact remote job.

        The transition is legal only from ``RUNNING`` with a matching exact
        remote identity and a previously unburned attempt, so a resumed
        process can never authorize a second disable->enable.
        """
        from app.services.remote_running_recovery import encode_recovery_evidence, decode_recovery_evidence

        normalized_identity = str(remote_identity or "").strip()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if str(job["state"]) != "RUNNING":
                raise RemoteIntentConflict("recovery attempt requires the RUNNING state")
            if not normalized_identity or str(job["remote_identity"] or "").strip() != normalized_identity:
                raise RemoteIntentConflict("recovery attempt requires the exact confirmed remote identity")
            if int(job["recovery_attempts"] or 0) != 0:
                raise RemoteIntentConflict("remote job already consumed its single recovery attempt")
            document = decode_recovery_evidence(job["recovery_evidence_json"])
            merged_restart = dict(document.get("restart") or {})
            merged_restart.update(
                {
                    "phase": "ENABLE_CONFIRMED",
                    "recovery_attempts": 1,
                }
            )
            document["restart"] = merged_restart
            connection.execute(
                "UPDATE remote_jobs SET recovery_attempts=1, recovery_evidence_json=?, updated_at=? WHERE id=?",
                (encode_recovery_evidence(document), self._utcnow(), job_id),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def mark_remote_recovery_exhausted(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Move one RUNNING job to the local RECOVERY_EXHAUSTED terminal state.

        This is a local business give-up, never a claim that the remote task
        finished: the durable restart evidence must prove the single exact
        disable->enable already ran and was confirmed before the day stops
        waiting on this job.
        """
        from app.services.remote_running_recovery import (
            decode_recovery_evidence,
            encode_recovery_evidence,
        )

        normalized_identity = str(remote_identity or "").strip()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            document = decode_recovery_evidence(job["recovery_evidence_json"])
            restart = document.get("restart") if isinstance(document.get("restart"), dict) else {}
            if str(job["state"]) != "RUNNING":
                raise RemoteIntentConflict("recovery exhaustion requires the RUNNING state")
            if not normalized_identity or str(job["remote_identity"] or "").strip() != normalized_identity:
                raise RemoteIntentConflict("recovery exhaustion requires the exact confirmed remote identity")
            if int(job["recovery_attempts"] or 0) != 1:
                raise RemoteIntentConflict("recovery exhaustion requires exactly one consumed recovery attempt")
            if str(restart.get("phase") or "") != "ENABLE_CONFIRMED":
                raise RemoteIntentConflict("recovery exhaustion requires confirmed disable and enable evidence")
            disable_evidence = restart.get("disable_evidence")
            enable_evidence = restart.get("enable_evidence")
            if not (
                isinstance(disable_evidence, dict)
                and disable_evidence.get("confirmed_disabled") is True
                and isinstance(enable_evidence, dict)
                and enable_evidence.get("confirmed_enabled") is True
            ):
                raise RemoteIntentConflict(
                    "recovery exhaustion requires disable_evidence.confirmed_disabled and "
                    "enable_evidence.confirmed_enabled to both be true"
                )
            recovery_at = str(restart.get("recovery_at") or "").strip()
            try:
                datetime.fromisoformat(recovery_at)
            except ValueError as exc:
                raise RemoteIntentConflict(
                    "recovery exhaustion requires a valid recovery timestamp"
                ) from exc
            merged = dict(evidence)
            merged_restart = dict(merged.get("restart") or {})
            merged_restart.setdefault("phase", "ENABLE_CONFIRMED")
            merged_restart.setdefault("recovery_attempts", 1)
            merged["restart"] = merged_restart
            connection.execute(
                "UPDATE remote_jobs SET state='RECOVERY_EXHAUSTED', recovery_evidence_json=?, error_text=?, updated_at=? WHERE id=?",
                (
                    encode_recovery_evidence(merged),
                    "remote stall recovery exhausted after one confirmed disable->enable restart",
                    self._utcnow(),
                    job_id,
                ),
            )
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    def complete_crawl_work_recovery_exhausted(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        remote_job_id: int,
        remote_identity: str,
        reason: str,
        restart_phase: str,
        recovery_attempt: int,
        recovery_at: str,
        disable_confirmed: bool,
        enable_confirmed: bool,
        last_progress_signature: object,
        export_count: object,
        export_rsp_desc: str,
    ) -> dict[str, Any]:
        """Persist the auditable no-price outcome for one abandoned crawl."""
        from app.services.remote_running_recovery import encode_recovery_evidence

        normalized_reason = str(reason or "").strip()
        normalized_identity = str(remote_identity or "").strip()
        if not normalized_reason or not normalized_identity:
            raise RemoteIntentConflict("recovery-exhausted crawl resolution requires exact evidence")
        if int(recovery_attempt) != 1 or str(restart_phase or "").strip() != "ENABLE_CONFIRMED":
            raise RemoteIntentConflict("recovery-exhausted crawl resolution requires the completed restart proof")
        if not disable_confirmed or not enable_confirmed:
            raise RemoteIntentConflict("recovery-exhausted crawl resolution requires confirmed disable and enable evidence")
        evidence = encode_recovery_evidence(
            {
                "evidence_type": "REMOTE_STALL_RECOVERY_EXHAUSTED",
                "no_result_reason": normalized_reason,
                "page_price_source": "招商局（无采集结果）",
                "page_price_status": "NO_RESULT",
                "page_price_value": None,
                "remote_identity": normalized_identity,
                "remote_state": "RECOVERY_EXHAUSTED",
                "recovery_attempt": 1,
                "recovery_at": str(recovery_at or ""),
                "disable_confirmed": True,
                "enable_confirmed": True,
                "post_recovery_harvest_checked": True,
                "last_progress_signature": last_progress_signature,
                "export_count": export_count,
                "export_rspDesc": str(export_rsp_desc or ""),
            }
        )
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                int(work["remote_job_id"] or 0) != int(remote_job_id)
                or str(job["remote_identity"] or "").strip() != normalized_identity
                or job["state"] != "RECOVERY_EXHAUSTED"
            ):
                raise RemoteIntentConflict(
                    "recovery-exhausted crawl resolution requires the exact abandoned remote job"
                )
            if work["state"] == "NO_RESULT":
                if work["result_json"] != evidence or work["error_text"] != normalized_reason:
                    raise RemoteIntentConflict("no-result crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] in {"SUCCESS", "TERMINAL_FAILED"}:
                raise RemoteIntentConflict("terminal crawl evidence is immutable")
            connection.execute(
                "UPDATE crawl_work SET state='NO_RESULT', result_json=?, error_text=?, updated_at=? WHERE id=?",
                (evidence, normalized_reason, self._utcnow(), crawl_work_id),
            )
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())  # type: ignore[return-value]

    def _set_job_state(self, token: LeaseToken, job_id: int, state: str, *, error: str | None = None, require_post_attempted: bool = False) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, job_id)
            if require_post_attempted and not int(job["post_attempted"]):
                raise ValueError("cannot record uncertain submit before durable post_attempted evidence")
            connection.execute("UPDATE remote_jobs SET state=?, error_text=?, updated_at=? WHERE id=?", (state, error, self._utcnow(), job_id))
            return self._row(connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]

    @staticmethod
    def _job_for_lease(connection: sqlite3.Connection, token: LeaseToken, job_id: int) -> sqlite3.Row:
        job = connection.execute("SELECT * FROM remote_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None or int(job["run_id"]) != token.run_id:
            raise ValueError("remote job must belong to the leased daily run")
        return job

    def ensure_crawl_work(
        self,
        token: LeaseToken,
        mall_key: str,
        sku_id: str,
        exact_url: str,
        backend_version: str,
        fingerprint: str,
        *,
        source_id: str = "",
    ) -> dict[str, Any]:
        normalized_url = exact_url.strip()
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            work = connection.execute("SELECT * FROM crawl_work WHERE run_id=? AND mall_key=? AND source_id=? AND sku_id=? AND exact_url=? AND backend_version=?", (token.run_id, mall_key, source_id, sku_id, normalized_url, backend_version)).fetchone()
            if work is None:
                cursor = connection.execute("INSERT INTO crawl_work(run_id, mall_key, source_id, sku_id, exact_url, backend_version, fingerprint, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (token.run_id, mall_key, source_id, sku_id, normalized_url, backend_version, fingerprint, now))
                work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (cursor.lastrowid,)).fetchone()
            elif work["fingerprint"] != fingerprint:
                if work["remote_job_id"] is None and work["state"] in {"PENDING", "RETRYABLE_FAILED"}:
                    connection.execute("UPDATE crawl_work SET fingerprint=?, state='PENDING', result_json=NULL, error_text=NULL, updated_at=? WHERE id=?", (fingerprint, now, work["id"]))
                work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (work["id"],)).fetchone()
            return self._row(work)  # type: ignore[return-value]

    def link_crawl_work_to_remote_job(self, token: LeaseToken, crawl_work_id: int, job_id: int) -> dict[str, Any]:
        return self.link_crawl_work_batch_to_remote_job(token, [crawl_work_id], job_id)[0]

    def link_crawl_work_batch_to_remote_job(
        self, token: LeaseToken, crawl_work_ids: Sequence[int], job_id: int,
    ) -> list[dict[str, Any]]:
        """Bind one immutable crawl intent atomically, or leave every row untouched."""
        ids = list(dict.fromkeys(int(work_id) for work_id in crawl_work_ids))
        if not ids:
            return []
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            self._job_for_lease(connection, token, job_id)
            placeholders = ",".join("?" for _ in ids)
            rows = connection.execute(f"SELECT * FROM crawl_work WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
            if len(rows) != len(ids) or any(int(row["run_id"]) != token.run_id for row in rows):
                raise ValueError("crawl work must belong to the leased daily run")
            for work in rows:
                if work["remote_job_id"] is not None and int(work["remote_job_id"]) != job_id:
                    raise RemoteIntentConflict("crawl work is already bound to another remote job")
                if work["remote_job_id"] is None and work["state"] in {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"}:
                    raise RemoteIntentConflict("terminal crawl work cannot be rebound")
            connection.execute(
                f"UPDATE crawl_work SET remote_job_id=?, updated_at=? WHERE id IN ({placeholders}) AND remote_job_id IS NULL",
                (job_id, self._utcnow(), *ids),
            )
            bound = connection.execute(f"SELECT * FROM crawl_work WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
            by_id = {int(row["id"]): self._row(row) for row in bound}
            return [by_id[work_id] for work_id in ids]  # type: ignore[list-item]

    def get_crawl_work(self, crawl_work_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())

    def reuse_successful_crawl_work(self, token: LeaseToken, crawl_work_id: int) -> dict[str, Any] | None:
        """Reuse or follow exact backend SKU/URL work for a new source document.

        ``source_id`` remains part of the durable business crawl identity, but
        the legacy backend request contains only mall/SKU/URL.  A new source
        relationship therefore follows an already submitted backend request
        instead of placing that same request into a second mixed-delta job.
        """
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            target = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            if target is None or int(target["run_id"]) != token.run_id:
                raise ValueError("crawl work must belong to the leased daily run")
            if target["state"] in {"SUCCESS", "NO_RESULT"}:
                return self._row(target)
            if target["remote_job_id"] is not None or target["state"] not in {"PENDING", "RETRYABLE_FAILED"}:
                return None
            source = connection.execute(
                """SELECT * FROM crawl_work WHERE run_id=? AND mall_key=? AND sku_id=? AND exact_url=?
                   AND backend_version=? AND state IN ('SUCCESS', 'NO_RESULT')
                   AND result_json IS NOT NULL AND id<>?
                   ORDER BY CASE state WHEN 'SUCCESS' THEN 0 ELSE 1 END, id LIMIT 1""",
                (token.run_id, target["mall_key"], target["sku_id"], target["exact_url"], target["backend_version"], crawl_work_id),
            ).fetchone()
            if source is None:
                source = connection.execute(
                    """SELECT * FROM crawl_work WHERE run_id=? AND mall_key=? AND sku_id=? AND exact_url=?
                       AND backend_version=? AND remote_job_id IS NOT NULL AND id<>?
                       AND state IN ('PENDING', 'PRIORITY_PENDING', 'SUBMITTED', 'RUNNING', 'EXPORTED', 'RETRYABLE_FAILED', 'SUBMIT_UNKNOWN')
                       ORDER BY id LIMIT 1""",
                    (
                        token.run_id,
                        target["mall_key"],
                        target["sku_id"],
                        target["exact_url"],
                        target["backend_version"],
                        crawl_work_id,
                    ),
                ).fetchone()
                if source is None:
                    source = connection.execute(
                        """SELECT * FROM crawl_work WHERE run_id=? AND mall_key=? AND sku_id=? AND exact_url=?
                           AND backend_version=? AND state='TERMINAL_FAILED' AND id<>?
                           ORDER BY id LIMIT 1""",
                        (
                            token.run_id,
                            target["mall_key"],
                            target["sku_id"],
                            target["exact_url"],
                            target["backend_version"],
                            crawl_work_id,
                        ),
                    ).fetchone()
                    if source is None:
                        return None
                    connection.execute(
                        """UPDATE crawl_work SET state='TERMINAL_FAILED', result_json=?, error_text=?, updated_at=?
                           WHERE id=? AND remote_job_id IS NULL""",
                        (
                            source["result_json"],
                            source["error_text"],
                            self._utcnow(),
                            crawl_work_id,
                        ),
                    )
                    return self._row(
                        connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
                    )
                connection.execute(
                    """UPDATE crawl_work SET state=?, remote_job_id=?, error_text=NULL, updated_at=?
                       WHERE id=? AND remote_job_id IS NULL""",
                    (
                        source["state"],
                        source["remote_job_id"],
                        self._utcnow(),
                        crawl_work_id,
                    ),
                )
                return self._row(
                    connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
                )
            connection.execute(
                """UPDATE crawl_work SET state=?, result_json=?, error_text=?, updated_at=? WHERE id=?""",
                (
                    source["state"],
                    source["result_json"],
                    source["error_text"] if source["state"] == "NO_RESULT" else None,
                    self._utcnow(),
                    crawl_work_id,
                ),
            )
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())  # type: ignore[return-value]

    def list_crawl_work(self, token: LeaseToken, *, states: set[str] | None = None) -> list[dict[str, Any]]:
        with self._transaction(immediate=False) as connection:
            self._validate_lease(connection, token)
            if states:
                placeholders = ",".join("?" for _ in states)
                rows = connection.execute(f"SELECT * FROM crawl_work WHERE run_id=? AND state IN ({placeholders}) ORDER BY id", (token.run_id, *sorted(states))).fetchall()
            else:
                rows = connection.execute("SELECT * FROM crawl_work WHERE run_id=? ORDER BY id", (token.run_id,)).fetchall()
            return [self._row(row) for row in rows]  # type: ignore[list-item]

    def complete_crawl_work(self, token: LeaseToken, crawl_work_id: int, result: Any) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            encoded = self._canonical_json(result)
            if work["state"] == "SUCCESS":
                if work["result_json"] != encoded:
                    raise RemoteIntentConflict("successful crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] in {"NO_RESULT", "TERMINAL_FAILED"}:
                raise RemoteIntentConflict("terminal crawl cannot become successful")
            connection.execute("UPDATE crawl_work SET state='SUCCESS', result_json=?, error_text=NULL, updated_at=? WHERE id=?", (encoded, self._utcnow(), crawl_work_id))
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())  # type: ignore[return-value]

    def complete_partial_exact_crawl_work(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        remote_job_id: int,
        remote_identity: str,
        result: Any,
    ) -> dict[str, Any]:
        """Persist one exact partial-export result bound to one confirmed job."""
        encoded = self._canonical_json(result)
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute(
                "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
            ).fetchone()
            assert work is not None
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                int(work["remote_job_id"] or 0) != int(remote_job_id)
                or str(job["remote_identity"] or "").strip()
                != str(remote_identity or "").strip()
                or not int(job["post_attempted"])
                or job["state"] not in {"MANUAL_REQUIRED", "COMPLETED"}
            ):
                raise RemoteIntentConflict(
                    "partial exact result requires the exact confirmed manual/completed job"
                )
            if work["state"] == "SUCCESS":
                if work["result_json"] != encoded:
                    raise RemoteIntentConflict("successful crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] in {"NO_RESULT", "TERMINAL_FAILED"}:
                raise RemoteIntentConflict("terminal crawl evidence is immutable")
            connection.execute(
                "UPDATE crawl_work SET state='SUCCESS', result_json=?, error_text=NULL, updated_at=? WHERE id=?",
                (encoded, self._utcnow(), crawl_work_id),
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
                ).fetchone()
            )  # type: ignore[return-value]

    def complete_partial_exact_no_result(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        remote_job_id: int,
        remote_identity: str,
        result_path: str,
        reason: str = "legacy export contains no matching durable identity row",
    ) -> dict[str, Any]:
        """Persist exact absence from a completed, provenance-valid export."""
        normalized_reason = str(reason or "").strip()
        normalized_path = str(result_path or "").strip()
        if not normalized_reason or not normalized_path:
            raise RemoteIntentConflict("partial no-result requires exact export evidence")
        evidence = self._canonical_json(
            {
                "evidence_type": "EXPORTED_NO_MATCHING_ROW",
                "no_result_reason": normalized_reason,
                "page_price_source": "招商局（无采集结果）",
                "page_price_status": "NO_RESULT",
                "page_price_value": None,
                "remote_identity": str(remote_identity or "").strip(),
                "remote_state": "COMPLETED",
                "result_path": normalized_path,
            }
        )
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute(
                "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
            ).fetchone()
            assert work is not None
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                int(work["remote_job_id"] or 0) != int(remote_job_id)
                or str(job["remote_identity"] or "").strip()
                != str(remote_identity or "").strip()
                or not int(job["post_attempted"])
                or job["state"] not in {
                    "PRIORITY_PENDING",
                    "SUBMITTED",
                    "RUNNING",
                    "EXPORTED",
                    "MANUAL_REQUIRED",
                    "COMPLETED",
                }
                or str(job["result_path"] or "").strip() != normalized_path
            ):
                raise RemoteIntentConflict(
                    "partial no-result requires the exact confirmed export job"
                )
            if work["state"] == "NO_RESULT":
                if work["result_json"] != evidence or work["error_text"] != normalized_reason:
                    raise RemoteIntentConflict("no-result crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] in {"SUCCESS", "TERMINAL_FAILED"}:
                raise RemoteIntentConflict("terminal crawl evidence is immutable")
            connection.execute(
                "UPDATE crawl_work SET state='NO_RESULT', result_json=?, error_text=?, updated_at=? WHERE id=?",
                (evidence, normalized_reason, self._utcnow(), crawl_work_id),
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
                ).fetchone()
            )  # type: ignore[return-value]

    def mark_partial_exact_crawl_manual(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        remote_job_id: int,
        remote_identity: str,
        error: str,
    ) -> dict[str, Any]:
        normalized_error = str(error or "").strip()
        if not normalized_error:
            raise RemoteIntentConflict("partial crawl manual state requires a reason")
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute(
                "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
            ).fetchone()
            assert work is not None
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                int(work["remote_job_id"] or 0) != int(remote_job_id)
                or str(job["remote_identity"] or "").strip()
                != str(remote_identity or "").strip()
                or job["state"] != "MANUAL_REQUIRED"
            ):
                raise RemoteIntentConflict("partial crawl manual state requires the exact manual job")
            if work["state"] in {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"}:
                return self._row(work)  # type: ignore[return-value]
            if work["state"] == "MANUAL_REQUIRED" and work["error_text"] == normalized_error:
                return self._row(work)  # type: ignore[return-value]
            connection.execute(
                "UPDATE crawl_work SET state='MANUAL_REQUIRED', result_json=NULL, error_text=?, updated_at=? WHERE id=?",
                (normalized_error, self._utcnow(), crawl_work_id),
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)
                ).fetchone()
            )  # type: ignore[return-value]

    def finish_partial_exact_harvest_job(
        self,
        token: LeaseToken,
        remote_job_id: int,
        *,
        remote_identity: str,
        export_path: str,
        result: Any,
    ) -> dict[str, Any]:
        """Complete the job only when every linked crawl is deterministically resolved."""
        normalized_identity = str(remote_identity or "").strip()
        normalized_path = str(export_path or "").strip()
        encoded_result = self._canonical_json(result)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                str(job["remote_identity"] or "").strip() != normalized_identity
                or not int(job["post_attempted"])
                or job["state"] not in {"MANUAL_REQUIRED", "COMPLETED"}
                or str(job["result_path"] or "").strip() != normalized_path
            ):
                raise RemoteIntentConflict(
                    "partial harvest finish requires the exact confirmed export job"
                )
            states = [
                str(row["state"])
                for row in connection.execute(
                    "SELECT state FROM crawl_work WHERE run_id=? AND remote_job_id=? ORDER BY id",
                    (token.run_id, int(remote_job_id)),
                ).fetchall()
            ]
            if not states:
                raise RemoteIntentConflict("partial harvest job has no linked crawl work")
            completed = all(state in {"SUCCESS", "NO_RESULT"} for state in states)
            state = "COMPLETED" if completed else "MANUAL_REQUIRED"
            error = None if completed else "partial exact harvest has unresolved crawl identities"
            if (
                str(job["state"]) == state
                and str(job["export_path"] or "") == normalized_path
                and str(job["result_json"] or "") == encoded_result
                and str(job["error_text"] or "") == str(error or "")
            ):
                return self._row(job)  # type: ignore[return-value]
            connection.execute(
                "UPDATE remote_jobs SET state=?, export_path=?, result_json=?, error_text=?, updated_at=? WHERE id=?",
                (state, normalized_path, encoded_result, error, self._utcnow(), int(remote_job_id)),
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM remote_jobs WHERE id=?", (int(remote_job_id),)
                ).fetchone()
            )  # type: ignore[return-value]

    def complete_crawl_work_without_result(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Record an explicit no-price business outcome from one exact completed export."""
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise RemoteIntentConflict("no-result resolution requires the persisted failure reason")
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            if work["state"] == "NO_RESULT":
                try:
                    evidence = json.loads(str(work["result_json"] or ""))
                except (TypeError, ValueError) as exc:
                    raise RemoteIntentConflict("no-result crawl evidence is immutable") from exc
                if (
                    str(work["error_text"] or "").strip() != normalized_reason
                    or not isinstance(evidence, dict)
                    or evidence.get("evidence_type") != "EXPORTED_NO_MATCHING_ROW"
                    or evidence.get("no_result_reason") != normalized_reason
                    or evidence.get("page_price_status") != "NO_RESULT"
                    or evidence.get("page_price_value") is not None
                ):
                    raise RemoteIntentConflict("no-result crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] != "TERMINAL_FAILED" or str(work["error_text"] or "").strip() != normalized_reason:
                raise RemoteIntentConflict("no-result resolution requires the exact terminal failure evidence")
            if work["remote_job_id"] is None:
                raise RemoteIntentConflict("no-result resolution requires completed export evidence")
            job = self._job_for_lease(connection, token, int(work["remote_job_id"]))
            remote_identity = str(job["remote_identity"] or "").strip()
            result_path = str(job["result_path"] or "").strip()
            if (
                job["state"] != "COMPLETED"
                or not int(job["post_attempted"])
                or not remote_identity
                or not result_path
                or job["result_json"] is None
            ):
                raise RemoteIntentConflict("no-result resolution requires completed export evidence")
            evidence = self._canonical_json(
                {
                    "evidence_type": "EXPORTED_NO_MATCHING_ROW",
                    "no_result_reason": normalized_reason,
                    "page_price_source": "招商局（无采集结果）",
                    "page_price_status": "NO_RESULT",
                    "page_price_value": None,
                    "remote_identity": remote_identity,
                    "remote_state": "COMPLETED",
                    "result_path": result_path,
                }
            )
            connection.execute(
                "UPDATE crawl_work SET state='NO_RESULT', result_json=?, updated_at=? WHERE id=?",
                (evidence, self._utcnow(), crawl_work_id),
            )
            return self._row(
                connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            )  # type: ignore[return-value]

    def complete_crawl_work_export_no_price(
        self,
        token: LeaseToken,
        crawl_work_id: int,
        *,
        remote_job_id: int,
        remote_identity: str,
        reason: str,
        export_rsp_desc: str,
        export_count: int,
    ) -> dict[str, Any]:
        """Persist terminal export-stage no-price evidence without a result workbook."""
        normalized_reason = str(reason or "").strip()
        normalized_rsp_desc = str(export_rsp_desc or "").strip()
        if not normalized_reason or not normalized_rsp_desc:
            raise RemoteIntentConflict("export no-price resolution requires exact export evidence")
        if int(export_count) != 0:
            raise RemoteIntentConflict("export no-price resolution requires a zero export count")
        evidence = self._canonical_json(
            {
                "evidence_type": "EXPORT_NO_PRICE_TERMINAL",
                "no_result_reason": normalized_reason,
                "page_price_source": "招商局（无采集结果）",
                "page_price_status": "NO_RESULT",
                "page_price_value": None,
                "remote_identity": str(remote_identity or "").strip(),
                "remote_state": "COMPLETED",
                "export_rspDesc": normalized_rsp_desc,
                "export_count": 0,
            }
        )
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            job = self._job_for_lease(connection, token, int(remote_job_id))
            if (
                int(work["remote_job_id"] or 0) != int(remote_job_id)
                or str(job["remote_identity"] or "").strip() != str(remote_identity or "").strip()
                or not int(job["post_attempted"])
                or job["state"] not in {"MANUAL_REQUIRED", "COMPLETED"}
            ):
                raise RemoteIntentConflict(
                    "export no-price resolution requires the exact confirmed remote job"
                )
            if work["state"] == "NO_RESULT":
                if work["result_json"] != evidence or work["error_text"] != normalized_reason:
                    raise RemoteIntentConflict("no-result crawl evidence is immutable")
                return self._row(work)  # type: ignore[return-value]
            if work["state"] in {"SUCCESS", "TERMINAL_FAILED", "MANUAL_REQUIRED"}:
                raise RemoteIntentConflict("terminal crawl evidence is immutable")
            if job["state"] != "MANUAL_REQUIRED":
                raise RemoteIntentConflict(
                    "export no-price crawl transition requires the manual remote job"
                )
            connection.execute(
                "UPDATE crawl_work SET state='NO_RESULT', result_json=?, error_text=?, updated_at=? WHERE id=?",
                (evidence, normalized_reason, self._utcnow(), crawl_work_id),
            )
            return self._row(
                connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            )  # type: ignore[return-value]

    def complete_manual_export_no_price_job(
        self,
        token: LeaseToken,
        job_id: int,
        *,
        remote_identity: str,
        result: Any,
    ) -> dict[str, Any]:
        """Complete one MANUAL wait-export job whose export stage proved terminal no-price."""
        normalized_identity = str(remote_identity or "").strip()
        if not normalized_identity:
            raise RemoteIntentConflict("export no-price completion requires an exact remote identity")
        encoded_result = self._canonical_json(result)
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            job = self._job_for_lease(connection, token, int(job_id))
            if (
                str(job["remote_identity"] or "").strip() != normalized_identity
                or not int(job["post_attempted"])
            ):
                raise RemoteIntentConflict(
                    "export no-price completion requires the exact confirmed remote job"
                )
            if job["state"] == "COMPLETED":
                if str(job["result_json"] or "") != encoded_result:
                    raise RemoteIntentConflict("completed remote job evidence is immutable")
                return self._row(job)  # type: ignore[return-value]
            if job["state"] != "MANUAL_REQUIRED":
                raise RemoteIntentConflict(
                    "export no-price completion requires the manual wait-export job"
                )
            connection.execute(
                "UPDATE remote_jobs SET state='COMPLETED', result_json=?, error_text=NULL, updated_at=? WHERE id=?",
                (encoded_result, self._utcnow(), int(job_id)),
            )
            return self._row(
                connection.execute("SELECT * FROM remote_jobs WHERE id=?", (int(job_id),)).fetchone()
            )  # type: ignore[return-value]

    def fail_crawl_work(self, token: LeaseToken, crawl_work_id: int, error: str, *, retryable: bool) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            self._validate_crawl_lease(connection, token, crawl_work_id)
            work = connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
            assert work is not None
            state = "RETRYABLE_FAILED" if retryable else "TERMINAL_FAILED"
            if work["state"] in {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"}:
                if work["state"] == state and str(work["error_text"] or "") == error:
                    return self._row(work)  # type: ignore[return-value]
                raise RemoteIntentConflict("terminal crawl evidence is immutable")
            connection.execute("UPDATE crawl_work SET state=?, error_text=?, updated_at=? WHERE id=?", (state, error, self._utcnow(), crawl_work_id))
            return self._row(connection.execute("SELECT * FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone())  # type: ignore[return-value]

    def advance_crawl_work_for_job(self, token: LeaseToken, job_id: int, state: str) -> int:
        with self._transaction(immediate=True) as connection:
            self._validate_lease(connection, token)
            self._job_for_lease(connection, token, job_id)
            result = connection.execute("UPDATE crawl_work SET state=?, updated_at=? WHERE run_id=? AND remote_job_id=? AND state NOT IN ('SUCCESS', 'NO_RESULT', 'TERMINAL_FAILED')", (state, self._utcnow(), token.run_id, job_id))
            return result.rowcount

    def _validate_crawl_lease(self, connection: sqlite3.Connection, token: LeaseToken, crawl_work_id: int) -> None:
        self._validate_lease(connection, token)
        row = connection.execute("SELECT run_id FROM crawl_work WHERE id=?", (crawl_work_id,)).fetchone()
        if row is None or int(row["run_id"]) != token.run_id:
            raise ValueError("crawl work must belong to the leased daily run")

    def crawl_work_for_job(self, job_id: int) -> list[dict[str, Any]]:
        with self._reader() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM crawl_work WHERE remote_job_id=? ORDER BY id", (job_id,))]

    def update_daily_metrics(self, token: LeaseToken, metrics: Mapping[str, Any]) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            run = self._validate_lease(connection, token)
            existing = json.loads(run["metrics_json"] or "{}")
            existing.update(metrics)
            connection.execute("UPDATE daily_runs SET metrics_json=?, updated_at=? WHERE id=?", (self._canonical_json(existing), self._utcnow(), token.run_id))
            return self._row(connection.execute("SELECT * FROM daily_runs WHERE id=?", (token.run_id,)).fetchone())  # type: ignore[return-value]

    def freeze_snapshot(self, token: LeaseToken, snapshot_id: int) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            run = self._validate_lease(connection, token)
            snapshot = connection.execute("SELECT id FROM snapshots WHERE id=? AND run_id=?", (snapshot_id, token.run_id)).fetchone()
            if snapshot is None:
                raise ValueError("frozen snapshot must belong to the leased daily run")
            frozen = run["frozen_snapshot_id"]
            if frozen is not None and int(frozen) != snapshot_id:
                raise FrozenSnapshotConflict("daily run already froze a different authoritative snapshot")
            connection.execute("UPDATE daily_runs SET state=CASE WHEN state='FINALIZED' THEN state ELSE 'FINALIZING' END, frozen_snapshot_id=?, updated_at=? WHERE id=?", (snapshot_id, self._utcnow(), token.run_id))
            return self._row(connection.execute("SELECT * FROM daily_runs WHERE id=?", (token.run_id,)).fetchone())  # type: ignore[return-value]

    def prepare_finalization(self, token: LeaseToken, output_path: str) -> dict[str, Any]:
        """Durably reserve the one formal output path before publishing it."""
        output_path = str(output_path or "").strip()
        if not output_path:
            raise ValueError("finalization output path must be non-empty")
        with self._transaction(immediate=True) as connection:
            run = self._validate_lease(connection, token)
            if run["frozen_snapshot_id"] is None:
                raise FrozenSnapshotConflict("finalization requires a frozen authoritative snapshot")
            prepared_path = str(run["final_output_path"] or "")
            if run["state"] == "FINALIZED":
                if prepared_path == output_path:
                    return self._row(run)  # type: ignore[return-value]
                raise FrozenSnapshotConflict("daily run was already finalized for another output path")
            if prepared_path and prepared_path != output_path:
                raise FrozenSnapshotConflict("daily run already prepared another final output path")
            if not prepared_path:
                connection.execute(
                    "UPDATE daily_runs SET state='FINALIZING', final_output_path=?, updated_at=? WHERE id=?",
                    (output_path, self._utcnow(), token.run_id),
                )
                run = connection.execute("SELECT * FROM daily_runs WHERE id=?", (token.run_id,)).fetchone()
                assert run is not None
            return self._row(run)  # type: ignore[return-value]

    def mark_finalized(self, token: LeaseToken, output_path: str, manifest_hash: str) -> dict[str, Any]:
        with self._transaction(immediate=True) as connection:
            run = self._validate_lease(connection, token)
            if run["frozen_snapshot_id"] is None:
                raise FrozenSnapshotConflict("finalization requires a frozen authoritative snapshot")
            if not run["final_output_path"]:
                raise FrozenSnapshotConflict("finalization requires a prepared output path")
            if run["final_output_path"] != output_path:
                raise FrozenSnapshotConflict("finalization output path differs from its prepared intent")
            if run["state"] == "FINALIZED":
                if run["final_manifest_hash"] == manifest_hash:
                    return run
                raise FrozenSnapshotConflict("daily run was already finalized with different output evidence")
            connection.execute("UPDATE daily_runs SET state='FINALIZED', final_manifest_hash=?, updated_at=? WHERE id=?", (manifest_hash, self._utcnow(), token.run_id))
            return self._row(connection.execute("SELECT * FROM daily_runs WHERE id=?", (token.run_id,)).fetchone())  # type: ignore[return-value]
