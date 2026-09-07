"""Cross-day bypass store for customer prewarm discovery state (Issue #9).

This store is deliberately a standalone database file
(``customer_prewarm.sqlite3``): discovery state must survive across business
days, so it can never live inside STEP1's per-day ``state.sqlite3``.  The
schema is pure-addition and idempotent (``CREATE TABLE IF NOT EXISTS``);
initialization never touches any other database or sibling file.

Connection handling mirrors ``app/services/shared_crawl_cache.py``:
connections are never cached, ``PRAGMA foreign_keys=ON`` / ``busy_timeout=5000``
/ WAL, writes run in short ``BEGIN IMMEDIATE`` transactions, reads use
short-lived connections.

Phase 1 scope: durable discovery state only (documents, link candidates,
document-link association, idempotent sync ledger).  Mall routing and Shared
Crawl Cache lookups are later phases and are deliberately not wired here.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prewarm_documents (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL UNIQUE,
    business_date TEXT NOT NULL,
    four_category_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'event',
    detail_state TEXT NOT NULL DEFAULT 'PENDING',
    detail_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_checked_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prewarm_candidates (
    id INTEGER PRIMARY KEY,
    mall TEXT NOT NULL,
    original_url TEXT NOT NULL,
    lookup_url TEXT NOT NULL,
    routing_url TEXT,
    business_date TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'detail',
    cache_status TEXT NOT NULL DEFAULT 'PENDING_LOOKUP',
    match_level TEXT,
    resolved_identity_id INTEGER,
    last_error TEXT,
    last_checked_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(mall, original_url)
);
CREATE TABLE IF NOT EXISTS prewarm_links (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL,
    original_url TEXT NOT NULL,
    mall TEXT NOT NULL DEFAULT '',
    routing_url TEXT,
    candidate_id INTEGER NOT NULL REFERENCES prewarm_candidates(id),
    source TEXT NOT NULL DEFAULT 'detail',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(document_id, original_url)
);
CREATE TABLE IF NOT EXISTS prewarm_sync_ledger (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_event_key TEXT NOT NULL,
    cache_result_id INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source, source_event_key)
);
"""

CACHE_STATUS_PENDING_LOOKUP = "PENDING_LOOKUP"
CACHE_STATUS_PENDING_PREWARM = "PENDING_PREWARM"
CACHE_STATUS_CACHE_HIT = "CACHE_HIT"

_ALLOWED_CACHE_STATUS = {
    CACHE_STATUS_PENDING_LOOKUP,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_CACHE_HIT,
}

# 命中是终态:历史 append-only,CACHE_HIT 永不回退或改道。
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    CACHE_STATUS_PENDING_LOOKUP: {CACHE_STATUS_PENDING_PREWARM, CACHE_STATUS_CACHE_HIT},
    CACHE_STATUS_PENDING_PREWARM: {CACHE_STATUS_CACHE_HIT},
    CACHE_STATUS_CACHE_HIT: set(),
}


class CustomerPrewarmStore:
    """Standalone SQLite store for customer prewarm discovery state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    # Connections are intentionally never cached (same rationale as
    # SharedCrawlCache): ticks may be interrupted or cross threads.
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
            connection.executescript(_SCHEMA)

    # ---- shared helpers ------------------------------------------------------

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(UTC).isoformat(timespec="microseconds")

    @staticmethod
    def _require_text(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    # ---- public API ------------------------------------------------------------

    def table_names(self) -> set[str]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        return {row["name"] for row in rows}

    # ---- documents -------------------------------------------------------------

    def upsert_document(
        self,
        business_date: str,
        document_id: str,
        four_category_id: str = "",
        *,
        source: str = "event",
    ) -> dict[str, Any]:
        """Idempotent document upsert; category change resets the detail state."""
        business_date_value = self._require_text(business_date, "business_date")
        document_value = self._require_text(document_id, "document_id")
        category_value = self._optional_text(four_category_id) or ""
        source_value = self._optional_text(source) or "event"
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM prewarm_documents WHERE document_id=?", (document_value,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO prewarm_documents
                       (document_id, business_date, four_category_id, source,
                        detail_state, detail_attempts, last_error, last_checked_at,
                        first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, 'PENDING', 0, NULL, NULL, ?, ?)""",
                    (
                        document_value,
                        business_date_value,
                        category_value,
                        source_value,
                        now,
                        now,
                    ),
                )
            else:
                # business_date 是首见日,永不更新;一旦被 event 见过就保持 event。
                new_source = row["source"] if row["source"] == "event" else source_value
                if bool(category_value) and category_value != row["four_category_id"]:
                    # category 变化(含空→非空)→ 重置 PENDING 并清零 attempts。
                    connection.execute(
                        """UPDATE prewarm_documents
                           SET last_seen_at=?, source=?, four_category_id=?,
                               detail_state='PENDING', detail_attempts=0, last_error=NULL
                           WHERE document_id=?""",
                        (now, new_source, category_value, document_value),
                    )
                else:
                    connection.execute(
                        "UPDATE prewarm_documents SET last_seen_at=?, source=? WHERE document_id=?",
                        (now, new_source, document_value),
                    )
            refreshed = connection.execute(
                "SELECT * FROM prewarm_documents WHERE document_id=?", (document_value,)
            ).fetchone()
            return dict(refreshed) if refreshed is not None else {}

    def pending_documents(self, limit: int, *, max_attempts: int = 5) -> list[dict[str, Any]]:
        """PENDING docs with a category and attempts left, oldest id first."""
        if limit <= 0:
            return []
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT * FROM prewarm_documents
                   WHERE detail_state='PENDING' AND detail_attempts < ?
                     AND four_category_id != ''
                   ORDER BY id LIMIT ?""",
                (int(max_attempts), int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_document_detail_success(self, document_id: str) -> None:
        """Mark the detail as DONE regardless of the attempt counter."""
        document_value = self._require_text(document_id, "document_id")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE prewarm_documents
                   SET detail_state='DONE', last_error=NULL, last_checked_at=?
                   WHERE document_id=?""",
                (self._utcnow(), document_value),
            )

    def record_document_detail_failure(
        self, document_id: str, error: str, *, max_attempts: int = 5
    ) -> None:
        """Bump attempts; at ``max_attempts`` the doc becomes FAILED (terminal
        until its category changes)."""
        document_value = self._require_text(document_id, "document_id")
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE prewarm_documents
                   SET detail_attempts=detail_attempts+1,
                       last_error=?,
                       last_checked_at=?,
                       detail_state=CASE
                           WHEN detail_attempts+1 >= ? THEN 'FAILED'
                           ELSE detail_state END
                   WHERE document_id=?""",
                (str(error), now, int(max_attempts), document_value),
            )

    # ---- links / candidates ------------------------------------------------------

    def upsert_link(
        self,
        document_id: str,
        original_url: str,
        *,
        mall: str = "",
        lookup_url: str | None = None,
        routing_url: str | None = None,
        source: str = "detail",
        business_date: str | None = None,
    ) -> dict[str, Any]:
        """Idempotent link upsert returning ``{"link": ..., "candidate": ...}``.

        ``business_date`` is only used when a NEW candidate row is created;
        it defaults to the current UTC date (candidates carry the first-seen
        date, so callers usually pass the business date they are scanning).
        ``lookup_url`` defaults to ``original_url``; ``routing_url=None`` means
        no rewrite is known.
        """
        document_value = self._require_text(document_id, "document_id")
        original_value = self._require_text(original_url, "original_url")
        mall_value = self._optional_text(mall) or ""
        lookup_value = self._optional_text(lookup_url) or original_value
        routing_value = self._optional_text(routing_url)
        source_value = self._optional_text(source) or "detail"
        business_date_value = (
            self._require_text(business_date, "business_date")
            if business_date is not None
            else datetime.now(UTC).date().isoformat()
        )
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT * FROM prewarm_candidates WHERE mall=? AND original_url=?",
                (mall_value, original_value),
            ).fetchone()
            if candidate is None:
                # Rationale: candidate identity is (mall, original_url).  The
                # cross-mall same-original_url lookup below handles mapping-table
                # changes conservatively: reuse the existing candidate only when
                # it has never been a cache hit (routing may still change before
                # any hit); CACHE_HIT rows are left untouched (append-only).
                candidate = connection.execute(
                    "SELECT * FROM prewarm_candidates WHERE original_url=? ORDER BY id LIMIT 1",
                    (original_value,),
                ).fetchone()
                if candidate is None:
                    cursor = connection.execute(
                        """INSERT INTO prewarm_candidates
                           (mall, original_url, lookup_url, routing_url, business_date,
                            source, cache_status, match_level, resolved_identity_id,
                            last_error, last_checked_at, first_seen_at, last_seen_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'PENDING_LOOKUP', NULL, NULL, NULL, NULL, ?, ?)""",
                        (
                            mall_value,
                            original_value,
                            lookup_value,
                            routing_value,
                            business_date_value,
                            source_value,
                            now,
                            now,
                        ),
                    )
                    candidate = connection.execute(
                        "SELECT * FROM prewarm_candidates WHERE id=?", (int(cursor.lastrowid),)
                    ).fetchone()
                elif candidate["cache_status"] != CACHE_STATUS_CACHE_HIT:
                    connection.execute(
                        """UPDATE prewarm_candidates
                           SET mall=?, lookup_url=?, routing_url=?, last_seen_at=?
                           WHERE id=?""",
                        (mall_value, lookup_value, routing_value, now, int(candidate["id"])),
                    )
                    candidate = connection.execute(
                        "SELECT * FROM prewarm_candidates WHERE id=?", (int(candidate["id"]),)
                    ).fetchone()
                # else: CACHE_HIT candidate under another mall - leave untouched.
            else:
                connection.execute(
                    "UPDATE prewarm_candidates SET last_seen_at=? WHERE id=?",
                    (now, int(candidate["id"])),
                )
                candidate = connection.execute(
                    "SELECT * FROM prewarm_candidates WHERE id=?", (int(candidate["id"]),)
                ).fetchone()

            candidate_id = int(candidate["id"])
            link = connection.execute(
                "SELECT * FROM prewarm_links WHERE document_id=? AND original_url=?",
                (document_value, original_value),
            ).fetchone()
            if link is None:
                connection.execute(
                    """INSERT INTO prewarm_links
                       (document_id, original_url, mall, routing_url, candidate_id,
                        source, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        document_value,
                        original_value,
                        mall_value,
                        routing_value,
                        candidate_id,
                        source_value,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE prewarm_links
                       SET mall=?, routing_url=?, candidate_id=?, last_seen_at=?
                       WHERE id=?""",
                    (mall_value, routing_value, candidate_id, now, int(link["id"])),
                )
            link = connection.execute(
                "SELECT * FROM prewarm_links WHERE document_id=? AND original_url=?",
                (document_value, original_value),
            ).fetchone()
            return {"link": dict(link), "candidate": dict(candidate)}

    def record_candidate_result(
        self,
        candidate_id: int,
        *,
        cache_status: str,
        match_level: str | None = None,
        resolved_identity_id: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Apply one cache-status transition (see ``_ALLOWED_TRANSITIONS``)."""
        if cache_status not in _ALLOWED_CACHE_STATUS:
            raise ValueError(
                f"cache_status must be one of {sorted(_ALLOWED_CACHE_STATUS)}, got {cache_status!r}"
            )
        match_value = self._optional_text(match_level)
        error_value = self._optional_text(error)
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM prewarm_candidates WHERE id=?", (int(candidate_id),)
            ).fetchone()
            if row is None:
                raise ValueError(f"candidate {candidate_id} does not exist")
            current = row["cache_status"]
            if current == cache_status:
                # 同状态幂等:仅刷新 last_checked_at,其余字段不动。
                connection.execute(
                    "UPDATE prewarm_candidates SET last_checked_at=? WHERE id=?",
                    (now, int(candidate_id)),
                )
            elif current == CACHE_STATUS_CACHE_HIT:
                raise ValueError(
                    f"candidate {candidate_id} is CACHE_HIT; a hit is permanent and "
                    f"cannot move to {cache_status!r}"
                )
            elif cache_status not in _ALLOWED_TRANSITIONS[current]:
                raise ValueError(
                    f"candidate {candidate_id}: transition {current!r} -> "
                    f"{cache_status!r} is not allowed"
                )
            else:
                connection.execute(
                    """UPDATE prewarm_candidates
                       SET cache_status=?, match_level=?, resolved_identity_id=?,
                           last_error=?, last_checked_at=?
                       WHERE id=?""",
                    (
                        cache_status,
                        match_value,
                        resolved_identity_id,
                        error_value,
                        now,
                        int(candidate_id),
                    ),
                )
            refreshed = connection.execute(
                "SELECT * FROM prewarm_candidates WHERE id=?", (int(candidate_id),)
            ).fetchone()
            return dict(refreshed)

    def candidates(
        self, cache_status: str | None = None, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM prewarm_candidates"
        params: list[Any] = []
        if cache_status is not None:
            query += " WHERE cache_status=?"
            params.append(cache_status)
        query += " ORDER BY id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._reader() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_unique_cache_misses(self) -> list[dict[str, Any]]:
        """Stable seam for the follow-up prewarm phase: PENDING_PREWARM only."""
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM prewarm_candidates WHERE cache_status='PENDING_PREWARM' ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def links_for_document(self, document_id: str) -> list[dict[str, Any]]:
        document_value = self._require_text(document_id, "document_id")
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM prewarm_links WHERE document_id=? ORDER BY id",
                (document_value,),
            ).fetchall()
        return [dict(row) for row in rows]

    def links_for_candidate(self, candidate_id: int) -> list[dict[str, Any]]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM prewarm_links WHERE candidate_id=? ORDER BY id",
                (int(candidate_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- sync ledger -------------------------------------------------------------

    def ledger_get(self, source: str, source_event_key: str) -> dict[str, Any] | None:
        source_value = self._require_text(source, "source")
        key_value = self._require_text(source_event_key, "source_event_key")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM prewarm_sync_ledger WHERE source=? AND source_event_key=?",
                (source_value, key_value),
            ).fetchone()
        return dict(row) if row is not None else None

    def ledger_claim(self, source: str, source_event_key: str) -> dict[str, Any]:
        """Insert-if-absent idempotent claim (cache_result_id stays NULL)."""
        source_value = self._require_text(source, "source")
        key_value = self._require_text(source_event_key, "source_event_key")
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM prewarm_sync_ledger WHERE source=? AND source_event_key=?",
                (source_value, key_value),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO prewarm_sync_ledger
                       (source, source_event_key, cache_result_id, error, created_at)
                       VALUES (?, ?, NULL, NULL, ?)""",
                    (source_value, key_value, self._utcnow()),
                )
                row = connection.execute(
                    "SELECT * FROM prewarm_sync_ledger WHERE source=? AND source_event_key=?",
                    (source_value, key_value),
                ).fetchone()
            return dict(row)

    def ledger_complete(
        self,
        source: str,
        source_event_key: str,
        *,
        cache_result_id: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Finalize an existing claim; raise KeyError when it was never claimed."""
        source_value = self._require_text(source, "source")
        key_value = self._require_text(source_event_key, "source_event_key")
        error_value = self._optional_text(error)
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM prewarm_sync_ledger WHERE source=? AND source_event_key=?",
                (source_value, key_value),
            ).fetchone()
            if row is None:
                raise KeyError(f"no ledger claim for ({source_value!r}, {key_value!r})")
            connection.execute(
                "UPDATE prewarm_sync_ledger SET cache_result_id=?, error=? WHERE id=?",
                (cache_result_id, error_value, int(row["id"])),
            )
            refreshed = connection.execute(
                "SELECT * FROM prewarm_sync_ledger WHERE id=?", (int(row["id"]),)
            ).fetchone()
            return dict(refreshed)

    def ledger_pending_claims(self, source: str, *, older_than: str) -> list[dict[str, Any]]:
        """Un-finalized claims older than the cutoff (crash recovery probes)."""
        source_value = self._require_text(source, "source")
        if not isinstance(older_than, str) or not older_than.strip():
            raise ValueError("older_than must be a non-empty string")
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT * FROM prewarm_sync_ledger
                   WHERE source=? AND cache_result_id IS NULL AND error IS NULL
                     AND created_at < ?
                   ORDER BY created_at, id""",
                (source_value, older_than),
            ).fetchall()
        return [dict(row) for row in rows]

    def ledger_drop_claim(self, source: str, source_event_key: str) -> None:
        """Drop an un-finalized claim so crash recovery may re-record it."""
        source_value = self._require_text(source, "source")
        key_value = self._require_text(source_event_key, "source_event_key")
        with self._transaction(immediate=True) as connection:
            # 只有未 finalize 的 claim 可删;finalize 行属于 append-only 历史。
            connection.execute(
                """DELETE FROM prewarm_sync_ledger
                   WHERE source=? AND source_event_key=?
                     AND cache_result_id IS NULL AND error IS NULL""",
                (source_value, key_value),
            )

    # ---- stats ---------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._reader() as connection:
            documents = {
                row["detail_state"]: int(row["n"])
                for row in connection.execute(
                    "SELECT detail_state, COUNT(*) AS n FROM prewarm_documents GROUP BY detail_state"
                )
            }
            candidates = {
                row["cache_status"]: int(row["n"])
                for row in connection.execute(
                    "SELECT cache_status, COUNT(*) AS n FROM prewarm_candidates GROUP BY cache_status"
                )
            }
            links = int(connection.execute("SELECT COUNT(*) FROM prewarm_links").fetchone()[0])
            ledger_completed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM prewarm_sync_ledger WHERE cache_result_id IS NOT NULL"
                ).fetchone()[0]
            )
            ledger_errored = int(
                connection.execute(
                    """SELECT COUNT(*) FROM prewarm_sync_ledger
                       WHERE cache_result_id IS NULL AND error IS NOT NULL"""
                ).fetchone()[0]
            )
            ledger_pending = int(
                connection.execute(
                    """SELECT COUNT(*) FROM prewarm_sync_ledger
                       WHERE cache_result_id IS NULL AND error IS NULL"""
                ).fetchone()[0]
            )
            multi_document_candidates = int(
                connection.execute(
                    """SELECT COUNT(*) FROM (
                           SELECT candidate_id FROM prewarm_links
                           GROUP BY candidate_id HAVING COUNT(DISTINCT document_id) > 1
                       )"""
                ).fetchone()[0]
            )
        return {
            "documents": documents,
            "candidates": candidates,
            "links": links,
            "ledger": {
                "pending": ledger_pending,
                "completed": ledger_completed,
                "errored": ledger_errored,
            },
            "candidates_with_multiple_documents": multi_document_candidates,
        }
