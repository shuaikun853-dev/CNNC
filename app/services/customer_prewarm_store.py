"""Cross-day bypass store for customer prewarm discovery state (Issue #9).

This store is deliberately a standalone database file
(``customer_prewarm.sqlite3``): discovery state must survive across business
days, so it can never live inside STEP1's per-day ``state.sqlite3``.  The
schema is pure-addition and idempotent (``CREATE TABLE IF NOT EXISTS`` plus
``PRAGMA table_info``/``ALTER TABLE`` migrations for pre-existing databases);
initialization never touches any other database or sibling file.

Connection handling mirrors ``app/services/shared_crawl_cache.py``:
connections are never cached, ``PRAGMA foreign_keys=ON`` / ``busy_timeout=5000``
/ WAL, writes run in short ``BEGIN IMMEDIATE`` transactions, reads use
short-lived connections.

Detail failures are transient by construction (detail.lock busy, network
errors, auth failures): a document always stays PENDING and is deferred by an
exponential backoff instead of ever becoming terminal.  ``FAILED`` remains a
reserved ``detail_state`` value for provably-deterministic cases only; no
current failure is classified deterministic.

Candidate identity is strictly ``(mall, original_url)``: the same
``original_url`` under a different mall is a separate candidate and an
existing candidate is never mutated across malls (cache hits stay append-only
and mall-attributed).

Phase 1 scope: durable discovery state only (documents, link candidates,
document-link association, idempotent sync ledger).  Mall routing and Shared
Crawl Cache lookups are later phases and are deliberately not wired here.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prewarm_documents (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL UNIQUE,
    business_date TEXT NOT NULL,
    four_category_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'event',
    detail_state TEXT NOT NULL DEFAULT 'PENDING',
    detail_attempts INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
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
    remote_job_id INTEGER,
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
CREATE TABLE IF NOT EXISTS prewarm_recovery_runs (
    id INTEGER PRIMARY KEY,
    business_date TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    documents_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS prewarm_daily_observations (
    business_date TEXT NOT NULL,
    candidate_id INTEGER NOT NULL,
    document_id TEXT NOT NULL,
    original_url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(business_date, candidate_id, document_id)
);
CREATE TABLE IF NOT EXISTS prewarm_remote_job_links (
    business_date TEXT NOT NULL,
    candidate_id INTEGER NOT NULL,
    remote_job_id INTEGER NOT NULL,
    producer TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(business_date, candidate_id, remote_job_id)
);
CREATE TABLE IF NOT EXISTS prewarm_lifecycle_cursors (
    name TEXT PRIMARY KEY,
    cursor_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

CACHE_STATUS_PENDING_LOOKUP = "PENDING_LOOKUP"
CACHE_STATUS_PENDING_PREWARM = "PENDING_PREWARM"
CACHE_STATUS_CACHE_HIT = "CACHE_HIT"
CACHE_STATUS_STAPLES_PENDING = "STAPLES_PENDING"
CACHE_STATUS_MANUAL_SKIP = "MANUAL_SKIP"
CACHE_STATUS_ROUTING_BLOCKED = "ROUTING_BLOCKED"
CACHE_STATUS_REMOTE_DISPATCHED = "REMOTE_DISPATCHED"
CACHE_STATUS_REMOTE_REUSED = "REMOTE_REUSED"

_ALLOWED_CACHE_STATUS = {
    CACHE_STATUS_PENDING_LOOKUP,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_STAPLES_PENDING,
    CACHE_STATUS_MANUAL_SKIP,
    CACHE_STATUS_ROUTING_BLOCKED,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_REMOTE_REUSED,
}

# 命中是终态:历史 append-only,CACHE_HIT 永不回退或改道。
# STAPLES_PENDING 是史泰博本地预爬路由态(Issue #11):PENDING_PREWARM 先进入
# 该态等待本地缓存/本地爬取,终态仍是 CACHE_HIT,永不改道招商局。
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    CACHE_STATUS_PENDING_LOOKUP: {CACHE_STATUS_PENDING_PREWARM, CACHE_STATUS_CACHE_HIT},
    CACHE_STATUS_PENDING_PREWARM: {
        CACHE_STATUS_CACHE_HIT,
        CACHE_STATUS_STAPLES_PENDING,
        CACHE_STATUS_MANUAL_SKIP,
        CACHE_STATUS_ROUTING_BLOCKED,
        CACHE_STATUS_REMOTE_DISPATCHED,
        CACHE_STATUS_REMOTE_REUSED,
    },
    # 路由被阻塞时可重新分类(映射补全后),也允许事后命中缓存或补挂远端结果。
    CACHE_STATUS_ROUTING_BLOCKED: {
        CACHE_STATUS_ROUTING_BLOCKED,
        CACHE_STATUS_CACHE_HIT,
        CACHE_STATUS_STAPLES_PENDING,
        CACHE_STATUS_MANUAL_SKIP,
        CACHE_STATUS_REMOTE_DISPATCHED,
        CACHE_STATUS_REMOTE_REUSED,
    },
    # #11 可能稍后补缓存;STAPLES_PENDING 只能自愈为命中或保持原状。
    CACHE_STATUS_STAPLES_PENDING: {CACHE_STATUS_STAPLES_PENDING, CACHE_STATUS_CACHE_HIT},
    # 人工处理是终态。
    CACHE_STATUS_MANUAL_SKIP: set(),
    CACHE_STATUS_REMOTE_DISPATCHED: {CACHE_STATUS_REMOTE_DISPATCHED, CACHE_STATUS_CACHE_HIT},
    CACHE_STATUS_REMOTE_REUSED: {CACHE_STATUS_REMOTE_REUSED, CACHE_STATUS_CACHE_HIT},
    CACHE_STATUS_CACHE_HIT: set(),
}

# remote_job_id 仅允许记录在远端复用/新派发的候选上,保留任务溯源。
_REMOTE_JOB_STATUSES = frozenset(
    {CACHE_STATUS_REMOTE_DISPATCHED, CACHE_STATUS_REMOTE_REUSED}
)

# 瞬时 detail 失败的指数退避:60s, 120s, 240s, ... 封顶 1 小时。
_DETAIL_RETRY_BACKOFF_CAP_SECONDS = 3600


def _detail_retry_backoff_seconds(consecutive_failures: int) -> int:
    if consecutive_failures <= 0:
        return 0
    return min(60 * 2 ** (consecutive_failures - 1), _DETAIL_RETRY_BACKOFF_CAP_SECONDS)


_READ_ONLY_GUARD_MESSAGE = "customer prewarm store opened read-only"


class CustomerPrewarmStore:
    """Standalone SQLite store for customer prewarm discovery state.

    ``read_only=True`` (or :meth:`open_read_only`) opens an EXISTING store for
    diagnostics / plan-only reports: no directory creation, no schema
    initialization, no WAL pragma — a missing store file raises
    :class:`FileNotFoundError` instead of being silently created, and every
    write API raises :class:`RuntimeError`.
    """

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self._read_only = bool(read_only)
        if self._read_only:
            if self.path != Path(":memory:") and not self.path.is_file():
                raise FileNotFoundError(
                    f"prewarm store not found (read-only): {self.path}"
                )
            return
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def open_read_only(cls, path: str | Path) -> "CustomerPrewarmStore":
        """Open an existing store read-only; never create or migrate anything."""
        return cls(path, read_only=True)

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

    def _connect_ro(self) -> sqlite3.Connection:
        # Mirrors customer_prewarm_discovery._open_state_db_readonly: the
        # mode=ro URI never creates files; no journal_mode pragma on ro
        # connections (busy_timeout alone is safe).
        connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _assert_writable(self) -> None:
        if self._read_only:
            raise RuntimeError(_READ_ONLY_GUARD_MESSAGE)

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
        connection = self._connect_ro() if self._read_only else self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._transaction(immediate=True) as connection:
            connection.executescript(_SCHEMA)
            # Pure-addition migration for pre-existing (Round-1) databases,
            # mirroring daily_audit_store._initialize: check PRAGMA
            # table_info, then ALTER TABLE; idempotent on every open.
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(prewarm_documents)")
            }
            if "consecutive_failures" not in columns:
                connection.execute(
                    "ALTER TABLE prewarm_documents "
                    "ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0"
                )
            if "next_retry_at" not in columns:
                connection.execute(
                    "ALTER TABLE prewarm_documents ADD COLUMN next_retry_at TEXT"
                )
            candidate_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(prewarm_candidates)")
            }
            if "remote_job_id" not in candidate_columns:
                connection.execute(
                    "ALTER TABLE prewarm_candidates ADD COLUMN remote_job_id INTEGER"
                )
            # Issue #13 Round 2 bootstrap: derive per-day observations from the
            # existing document-day evidence (prewarm_links × documents'
            # business_date).  INSERT OR IGNORE keeps this idempotent on every
            # open — existing observation rows (their refreshed last_seen_at)
            # are never overwritten.  New business days come ONLY from the two
            # explicit write paths (upsert_link's inline observation and
            # upsert_document's per-link derivation); this bootstrap covers
            # legacy rows only.
            connection.execute(
                """INSERT OR IGNORE INTO prewarm_daily_observations
                   (business_date, candidate_id, document_id, original_url,
                    first_seen_at, last_seen_at)
                   SELECT d.business_date, l.candidate_id, l.document_id,
                          l.original_url, l.first_seen_at, l.last_seen_at
                   FROM prewarm_links l
                   JOIN prewarm_documents d ON d.document_id = l.document_id"""
            )

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
        self._assert_writable()
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
                    # category 变化(含空→非空)→ 重置 PENDING 并清零 attempts 与退避。
                    connection.execute(
                        """UPDATE prewarm_documents
                           SET last_seen_at=?, source=?, four_category_id=?,
                               detail_state='PENDING', detail_attempts=0,
                               consecutive_failures=0, next_retry_at=NULL, last_error=NULL
                           WHERE document_id=?""",
                        (now, new_source, category_value, document_value),
                    )
                else:
                    connection.execute(
                        "UPDATE prewarm_documents SET last_seen_at=?, source=? WHERE document_id=?",
                        (now, new_source, document_value),
                    )
            # Issue #13 Round 3 (D2): 文档在业务日 D 被重见 ⇒ 其既有引用在
            # D 也被观测。为该文档的全部既有 links 派生当日 observation
            # (INSERT OR IGNORE,PK 幂等),覆盖 DONE 文档次日仅走 intake、
            # 不再重跑 detail 的重复引用场景。
            # Issue #13 Round 4 (Slice G2) 不变量:observations 是看板的权威
            # 逐日索引 —— 派生写与文档写同处一个 BEGIN IMMEDIATE 事务且绝不
            # 吞错:observation 失败让整个 upsert(含文档 UPDATE/INSERT)一起
            # 回滚,绝不出现"文档已持久化而当日 observation 缺失"的半成功。
            connection.execute(
                """INSERT OR IGNORE INTO prewarm_daily_observations
                   (business_date, candidate_id, document_id, original_url,
                    first_seen_at, last_seen_at)
                   SELECT ?, l.candidate_id, l.document_id, l.original_url, ?, ?
                   FROM prewarm_links l WHERE l.document_id=?""",
                (business_date_value, now, now, document_value),
            )
            refreshed = connection.execute(
                "SELECT * FROM prewarm_documents WHERE document_id=?", (document_value,)
            ).fetchone()
            return dict(refreshed) if refreshed is not None else {}

    def pending_documents(self, limit: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """PENDING docs with a category whose backoff window has passed.

        Oldest id first.  ``now`` is injectable for tests and normalized to
        the same UTC microsecond ISO format the store writes, so the
        ``next_retry_at`` comparison is exact.
        """
        if limit <= 0:
            return []
        now_value = (
            now if now is not None else datetime.now(UTC)
        ).astimezone(UTC).isoformat(timespec="microseconds")
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT * FROM prewarm_documents
                   WHERE detail_state='PENDING' AND four_category_id != ''
                     AND (next_retry_at IS NULL OR next_retry_at <= ?)
                   ORDER BY id LIMIT ?""",
                (now_value, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_document_detail_success(self, document_id: str) -> None:
        """Mark the detail as DONE regardless of the attempt counter."""
        self._assert_writable()
        document_value = self._require_text(document_id, "document_id")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE prewarm_documents
                   SET detail_state='DONE', consecutive_failures=0, next_retry_at=NULL,
                       last_error=NULL, last_checked_at=?
                   WHERE document_id=?""",
                (self._utcnow(), document_value),
            )

    def record_document_detail_failure(
        self, document_id: str, error: str, *, now: datetime | None = None
    ) -> None:
        """Record one transient detail failure; the document always stays PENDING.

        FAILED remains a reserved value for provably-deterministic cases only;
        no current failure is classified deterministic (detail.lock busy,
        network errors and auth failures are all transient by construction).
        The document is deferred by an exponential backoff:
        ``next_retry_at = now + min(60 * 2**(consecutive_failures - 1), 3600)``
        seconds.  ``now`` is injectable for tests.
        """
        self._assert_writable()
        document_value = self._require_text(document_id, "document_id")
        retry_base = (now if now is not None else datetime.now(UTC)).astimezone(UTC)
        now_text = self._utcnow()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT consecutive_failures FROM prewarm_documents WHERE document_id=?",
                (document_value,),
            ).fetchone()
            consecutive = (int(row["consecutive_failures"]) if row is not None else 0) + 1
            next_retry_at = retry_base + timedelta(
                seconds=_detail_retry_backoff_seconds(consecutive)
            )
            connection.execute(
                """UPDATE prewarm_documents
                   SET detail_attempts=detail_attempts+1,
                       consecutive_failures=?,
                       next_retry_at=?,
                       last_error=?,
                       last_checked_at=?
                   WHERE document_id=?""",
                (consecutive, next_retry_at.isoformat(), str(error), now_text, document_value),
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

        ``business_date`` has TWO uses (Issue #13 Round 3): it is stamped as
        the first-seen date when a NEW candidate row is created, AND it dates
        the daily observation recorded for this link — so callers pass the
        business date they are scanning and a link re-observed on a later day
        is observed under that later day.  It defaults to the current UTC
        date.  ``lookup_url`` defaults to ``original_url``;
        ``routing_url=None`` means no rewrite is known.
        """
        self._assert_writable()
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
            # Rationale: candidate identity is strictly (mall, original_url).
            # A mapping-table change that routes the same original_url to a
            # different mall creates a NEW candidate; an existing candidate is
            # NEVER re-attributed across malls (mutation would silently rewrite
            # history and could attach another mall's links to a CACHE_HIT
            # row).  Only last_seen_at is bumped on re-sight of the same
            # identity; lookup outcome fields move exclusively through
            # record_candidate_result.
            candidate = connection.execute(
                "SELECT * FROM prewarm_candidates WHERE mall=? AND original_url=?",
                (mall_value, original_value),
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
            # Issue #13 Round 2: the same write transaction also records the
            # document-day observation for this link.
            # Issue #13 Round 4 (Slice G2) invariant: observations are the
            # dashboard's authoritative day index — this inline write is
            # ATOMIC with the link/candidate write (same BEGIN IMMEDIATE
            # transaction, never swallowed): an observation failure raises
            # and rolls back the WHOLE upsert, so a persisted link can never
            # exist without its day observation (no half-success).
            self._insert_daily_observation(
                connection,
                business_date=business_date_value,
                candidate_id=candidate_id,
                document_id=document_value,
                original_url=original_value,
                now_text=now,
            )
            return {"link": dict(link), "candidate": dict(candidate)}

    def record_candidate_result(
        self,
        candidate_id: int,
        *,
        cache_status: str,
        match_level: str | None = None,
        resolved_identity_id: int | None = None,
        error: str | None = None,
        remote_job_id: int | None = None,
    ) -> dict[str, Any]:
        """Apply one cache-status transition (see ``_ALLOWED_TRANSITIONS``).

        ``remote_job_id`` is only accepted for the remote statuses
        (``REMOTE_DISPATCHED`` / ``REMOTE_REUSED``) and keeps the remote-task
        provenance on the candidate row; when omitted the stored value is left
        untouched so later re-calls never erase prior evidence.  The same holds
        for ``error`` on the same-status idempotent path: a non-NULL error
        refreshes ``last_error`` (diagnostic notes such as "priority pending
        confirmation" stay visible), a NULL error never erases prior evidence.
        """
        self._assert_writable()
        if cache_status not in _ALLOWED_CACHE_STATUS:
            raise ValueError(
                f"cache_status must be one of {sorted(_ALLOWED_CACHE_STATUS)}, got {cache_status!r}"
            )
        remote_job_value = None if remote_job_id is None else int(remote_job_id)
        if remote_job_value is not None and cache_status not in _REMOTE_JOB_STATUSES:
            raise ValueError(
                f"remote_job_id is only allowed for cache_status in "
                f"{sorted(_REMOTE_JOB_STATUSES)}, got {cache_status!r}"
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
                # 同状态幂等:仅刷新 last_checked_at(以及新提供的 remote_job_id /
                # 非空 error 诊断),其余字段不动。
                if remote_job_value is not None:
                    connection.execute(
                        """UPDATE prewarm_candidates
                           SET remote_job_id=?, last_error=COALESCE(?, last_error),
                               last_checked_at=? WHERE id=?""",
                        (remote_job_value, error_value, now, int(candidate_id)),
                    )
                else:
                    connection.execute(
                        """UPDATE prewarm_candidates
                           SET last_error=COALESCE(?, last_error), last_checked_at=? WHERE id=?""",
                        (error_value, now, int(candidate_id)),
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
            elif remote_job_value is not None:
                connection.execute(
                    """UPDATE prewarm_candidates
                       SET cache_status=?, match_level=?, resolved_identity_id=?,
                           remote_job_id=?, last_error=?, last_checked_at=?
                       WHERE id=?""",
                    (
                        cache_status,
                        match_value,
                        resolved_identity_id,
                        remote_job_value,
                        error_value,
                        now,
                        int(candidate_id),
                    ),
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

    def candidate_by_id(self, candidate_id: int) -> dict[str, Any] | None:
        """One candidate row by primary key, or ``None`` when it does not exist."""
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM prewarm_candidates WHERE id=?", (int(candidate_id),)
            ).fetchone()
        return dict(row) if row is not None else None

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
        """Candidates eligible for routing/re-routing in the prewarm phase.

        ROUTING_BLOCKED is intentionally retried: the store transition matrix
        permits reclassification after a mall mapping is added or restored.
        Truly unmapped URLs remain ROUTING_BLOCKED after the next pass.
        """
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT * FROM prewarm_candidates
                   WHERE cache_status IN ('PENDING_PREWARM', 'ROUTING_BLOCKED')
                   ORDER BY id"""
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

    # ---- daily observations / remote job links (Issue #13 Round 2) -------------

    @staticmethod
    def _insert_daily_observation(
        connection: sqlite3.Connection,
        *,
        business_date: str,
        candidate_id: int,
        document_id: str,
        original_url: str,
        now_text: str,
    ) -> None:
        """Upsert one document-day observation on an open connection.

        PK hit refreshes ``last_seen_at`` only (first_seen_at is immutable
        evidence).  Shared by :meth:`record_daily_observation` (own
        transaction) and :meth:`upsert_link` (same transaction as the link
        write — atomic with it since Round 4 Slice G2, never swallowed).
        """
        row = connection.execute(
            """SELECT first_seen_at FROM prewarm_daily_observations
               WHERE business_date=? AND candidate_id=? AND document_id=?""",
            (business_date, int(candidate_id), document_id),
        ).fetchone()
        if row is None:
            connection.execute(
                """INSERT INTO prewarm_daily_observations
                   (business_date, candidate_id, document_id, original_url,
                    first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    business_date,
                    int(candidate_id),
                    document_id,
                    original_url,
                    now_text,
                    now_text,
                ),
            )
        else:
            connection.execute(
                """UPDATE prewarm_daily_observations
                   SET last_seen_at=?, original_url=?
                   WHERE business_date=? AND candidate_id=? AND document_id=?""",
                (
                    now_text,
                    original_url,
                    business_date,
                    int(candidate_id),
                    document_id,
                ),
            )

    def record_daily_observation(
        self,
        business_date: str,
        candidate_id: int,
        document_id: str,
        original_url: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Upsert one (business_date, candidate, document) observation.

        The PK hit path refreshes ``last_seen_at`` so a document/url seen
        twice on the same business day stays one row.  ``now`` is injectable
        for tests and normalized to the store's UTC microsecond ISO format.
        """
        self._assert_writable()
        business_date_value = self._require_text(business_date, "business_date")
        document_value = self._require_text(document_id, "document_id")
        original_value = self._require_text(original_url, "original_url")
        now_text = (
            (now if now is not None else datetime.now(UTC))
            .astimezone(UTC)
            .isoformat(timespec="microseconds")
        )
        with self._transaction(immediate=True) as connection:
            self._insert_daily_observation(
                connection,
                business_date=business_date_value,
                candidate_id=int(candidate_id),
                document_id=document_value,
                original_url=original_value,
                now_text=now_text,
            )

    def record_remote_job_link(
        self,
        business_date: str,
        candidate_id: int,
        remote_job_id: int,
        producer: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Upsert one candidate↔remote-job linkage (PK hit refreshes
        ``recorded_at``; the first producer attribution is kept)."""
        self._assert_writable()
        business_date_value = self._require_text(business_date, "business_date")
        producer_value = self._require_text(producer, "producer")
        now_text = (
            (now if now is not None else datetime.now(UTC))
            .astimezone(UTC)
            .isoformat(timespec="microseconds")
        )
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT recorded_at FROM prewarm_remote_job_links
                   WHERE business_date=? AND candidate_id=? AND remote_job_id=?""",
                (business_date_value, int(candidate_id), int(remote_job_id)),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO prewarm_remote_job_links
                       (business_date, candidate_id, remote_job_id, producer, recorded_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        business_date_value,
                        int(candidate_id),
                        int(remote_job_id),
                        producer_value,
                        now_text,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE prewarm_remote_job_links
                       SET recorded_at=?
                       WHERE business_date=? AND candidate_id=? AND remote_job_id=?""",
                    (
                        now_text,
                        business_date_value,
                        int(candidate_id),
                        int(remote_job_id),
                    ),
                )

    def bind_redispatched_remote_job(
        self,
        business_date: str,
        candidate_id: int,
        expected_owning_day: str,
        expected_remote_job_id: int,
        remote_job_id: int,
        producer: str,
        *,
        error: str | None = None,
    ) -> bool:
        """Atomically replace one current binding and its latest linkage."""
        return self.bind_redispatched_remote_job_batch(
            business_date,
            [(candidate_id, expected_owning_day, expected_remote_job_id)],
            remote_job_id,
            producer,
            error=error,
        )

    def bind_redispatched_remote_job_batch(
        self,
        business_date: str,
        bindings: Sequence[tuple[int, str, int]],
        remote_job_id: int,
        producer: str,
        *,
        error: str | None = None,
    ) -> bool:
        """All-or-none post-send binding for one immutable remote request group."""
        self._assert_writable()
        day = self._require_text(business_date, "business_date")
        producer_value = self._require_text(producer, "producer")
        now = self._utcnow()
        if not bindings or len({int(item[0]) for item in bindings}) != len(bindings):
            return False
        with self._transaction(immediate=True) as connection:
            for candidate_id, expected_owning_day, expected_remote_job_id in bindings:
                expected_day = self._require_text(expected_owning_day, "expected_owning_day")
                candidate = connection.execute(
                    "SELECT cache_status, remote_job_id FROM prewarm_candidates WHERE id=?",
                    (int(candidate_id),),
                ).fetchone()
                latest = connection.execute(
                    """SELECT business_date, remote_job_id, producer FROM prewarm_remote_job_links
                       WHERE candidate_id=? ORDER BY recorded_at DESC, rowid DESC LIMIT 1""",
                    (int(candidate_id),),
                ).fetchone()
                if (
                    candidate is None
                    or str(candidate["cache_status"]) not in _REMOTE_JOB_STATUSES
                    or int(candidate["remote_job_id"] or 0) != int(expected_remote_job_id)
                    or latest is None
                    or str(latest["business_date"]) != expected_day
                    or int(latest["remote_job_id"]) != int(expected_remote_job_id)
                    or str(latest["producer"]) != producer_value
                ):
                    return False
                existing = connection.execute(
                    """SELECT producer FROM prewarm_remote_job_links
                       WHERE business_date=? AND candidate_id=? AND remote_job_id=?""",
                    (day, int(candidate_id), int(remote_job_id)),
                ).fetchone()
                if existing is not None and str(existing["producer"]) != producer_value:
                    return False
            for candidate_id, _expected_day, _expected_job_id in bindings:
                connection.execute(
                    """INSERT INTO prewarm_remote_job_links
                       (business_date, candidate_id, remote_job_id, producer, recorded_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(business_date, candidate_id, remote_job_id)
                       DO UPDATE SET recorded_at=excluded.recorded_at""",
                    (day, int(candidate_id), int(remote_job_id), producer_value, now),
                )
                connection.execute(
                    """UPDATE prewarm_candidates
                       SET remote_job_id=?, last_error=COALESCE(?, last_error), last_checked_at=?
                       WHERE id=?""",
                    (int(remote_job_id), self._optional_text(error), now, int(candidate_id)),
                )
            return True

    def candidate_ids_for_business_date(self, business_date: str) -> set[int]:
        """Distinct observed candidate ids of one business day (missing table →
        empty set; never a fallback to candidate first-seen)."""
        business_date_value = self._require_text(business_date, "business_date")
        try:
            with self._reader() as connection:
                rows = connection.execute(
                    "SELECT DISTINCT candidate_id FROM prewarm_daily_observations WHERE business_date=?",
                    (business_date_value,),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            # Issue #13 Round 2 前的旧库没有 observation 表:投影语义保持
            # 一致,按"该日无观测"处理,其余 OperationalError 照常上抛。
            return set()
        return {int(row["candidate_id"]) for row in rows}

    def remote_job_link_for_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        """Latest linkage of one candidate (max recorded_at, tie-break max rowid)."""
        with self._reader() as connection:
            row = connection.execute(
                """SELECT * FROM prewarm_remote_job_links
                   WHERE candidate_id=?
                   ORDER BY recorded_at DESC, rowid DESC
                   LIMIT 1""",
                (int(candidate_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _connection_has_current_remote_job_link(
        connection: sqlite3.Connection,
        *,
        business_date: str,
        remote_job_id: int,
        producer: str,
    ) -> bool:
        rows = connection.execute(
            """SELECT id FROM prewarm_candidates
               WHERE cache_status IN ('REMOTE_DISPATCHED', 'REMOTE_REUSED')
                 AND remote_job_id=?
               ORDER BY id""",
            (int(remote_job_id),),
        ).fetchall()
        for row in rows:
            link = connection.execute(
                """SELECT business_date, remote_job_id, producer
                   FROM prewarm_remote_job_links
                   WHERE candidate_id=?
                   ORDER BY recorded_at DESC, rowid DESC
                   LIMIT 1""",
                (int(row["id"]),),
            ).fetchone()
            if (
                link is not None
                and str(link["business_date"]) == business_date
                and int(link["remote_job_id"]) == int(remote_job_id)
                and str(link["producer"]) == producer
            ):
                return True
        return False

    def has_current_remote_job_link(
        self, *, business_date: str, remote_job_id: int, producer: str
    ) -> bool:
        """Read the latest active candidate linkage without changing durable state."""
        with self._reader() as connection:
            return self._connection_has_current_remote_job_link(
                connection,
                business_date=business_date,
                remote_job_id=remote_job_id,
                producer=producer,
            )

    @contextmanager
    def fence_current_remote_job_link(
        self,
        *,
        business_date: str,
        remote_job_id: int,
        producer: str,
    ) -> Iterator[bool]:
        """Fence the latest candidate linkage through an external state commit.

        ``BEGIN IMMEDIATE`` serializes every candidate/link writer while the
        caller performs the short historical-DB transition inside this
        context.  Remote I/O must happen before entering the fence.
        """
        self._assert_writable()
        business_date_value = self._require_text(business_date, "business_date")
        producer_value = self._require_text(producer, "producer")
        with self._transaction(immediate=True) as connection:
            yield self._connection_has_current_remote_job_link(
                connection,
                business_date=business_date_value,
                remote_job_id=int(remote_job_id),
                producer=producer_value,
            )

    def lifecycle_cursor(self, name: str) -> str | None:
        name_value = self._require_text(name, "name")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT cursor_value FROM prewarm_lifecycle_cursors WHERE name=?",
                (name_value,),
            ).fetchone()
        return str(row["cursor_value"]) if row is not None else None

    def set_lifecycle_cursor(self, name: str, cursor_value: str) -> None:
        self._assert_writable()
        name_value = self._require_text(name, "name")
        cursor_value_text = self._require_text(cursor_value, "cursor_value")
        now_text = datetime.now(UTC).isoformat(timespec="microseconds")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO prewarm_lifecycle_cursors
                   (name, cursor_value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     cursor_value=excluded.cursor_value,
                     updated_at=excluded.updated_at""",
                (name_value, cursor_value_text, now_text),
            )

    def remote_job_producer(self, business_date: str, remote_job_id: int) -> str | None:
        """Producer of any linkage row for (business_date, remote_job_id)."""
        business_date_value = self._require_text(business_date, "business_date")
        with self._reader() as connection:
            row = connection.execute(
                """SELECT producer FROM prewarm_remote_job_links
                   WHERE business_date=? AND remote_job_id=?
                   LIMIT 1""",
                (business_date_value, int(remote_job_id)),
            ).fetchone()
        return str(row["producer"]) if row is not None else None

    def remote_job_link_count(self, business_date: str, candidate_id: int) -> int:
        """Rows in ``prewarm_remote_job_links`` for one (business_date, candidate).

        The redispatch daily budget basis: each remote job a candidate was
        bound to on a day adds exactly one row, so the count is the number of
        remote dispatch attempts that candidate consumed that day.
        """
        business_date_value = self._require_text(business_date, "business_date")
        with self._reader() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS n FROM prewarm_remote_job_links
                   WHERE business_date=? AND candidate_id=?""",
                (business_date_value, int(candidate_id)),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

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
        self._assert_writable()
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
        self._assert_writable()
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

    def ledger_completed_entries(self, source: str) -> list[dict[str, Any]]:
        """Completed ledger entries (cache_result_id set) of one source, id order."""
        source_value = self._require_text(source, "source")
        with self._reader() as connection:
            rows = connection.execute(
                """SELECT * FROM prewarm_sync_ledger
                   WHERE source=? AND cache_result_id IS NOT NULL
                   ORDER BY id""",
                (source_value,),
            ).fetchall()
        return [dict(row) for row in rows]

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
        self._assert_writable()
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

    # ---- scanner recovery runs (Issue #9 Round 3 Finding A) -----------------------

    def last_recovery_run_at(self) -> str | None:
        """``ran_at`` of the most recent scanner recovery run (None if never run)."""
        with self._reader() as connection:
            row = connection.execute(
                "SELECT MAX(ran_at) AS ran_at FROM prewarm_recovery_runs"
            ).fetchone()
        value = row["ran_at"] if row is not None else None
        return value or None

    def record_recovery_run(self, business_date: str, documents_seen: int) -> None:
        """Append one scanner recovery run row (throttle bookkeeping, Issue #9)."""
        self._assert_writable()
        business_date_value = self._require_text(business_date, "business_date")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO prewarm_recovery_runs
                   (business_date, ran_at, documents_seen)
                   VALUES (?, ?, ?)""",
                (business_date_value, self._utcnow(), int(documents_seen)),
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
