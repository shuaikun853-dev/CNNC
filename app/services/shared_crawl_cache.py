"""Shared, cross-day SQLite crawl cache with a URL identity model (Issue #7).

This store is deliberately a standalone database file: STEP1's checkpoint
(``DailyAuditStore``) is one ``state.sqlite3`` per business day, while a crawl
cache must survive across days, so it can never live inside a per-day file.
The schema is pure-addition and idempotent (``CREATE TABLE IF NOT EXISTS``);
initialization never touches any other database.

Data-layer contract (no producer, scheduler, or business flow is wired here):

- History is append-only: every success inserts a new ``crawl_results`` row.
- Each identity has at most one ``is_current=1`` row, enforced by a partial
  unique index; a later-but-older success never steals current.
- SKU evidence is a three-state value (``CONFIRMED``,
  ``URL_PARSED_UNVERIFIED``, ``UNRESOLVED``) that is validated and stored
  verbatim; URL parsing never promotes a SKU to ``CONFIRMED``.
- Placeholder codes (``zhpq-*`` by default) and ``crawl_id`` echoes are
  rejected as ``real_sku_id`` even when the caller claims ``CONFIRMED``.
- A ``(mall, url)`` alias permanently belongs to one identity; a conflicting
  record raises :class:`AliasConflictError` and rolls the whole transaction
  back (fail-closed).

Connection handling mirrors ``app/services/daily_audit_store.py``: connections
are never cached, ``PRAGMA foreign_keys=ON`` / ``busy_timeout=5000`` / WAL,
writes run in ``BEGIN IMMEDIATE`` transactions.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from app.services.crawl_url_identity import (
    MALL_IDENTITY_PARSERS,
    PLACEHOLDER_PREFIXES_DEFAULT,
    SKU_STATUSES,
    SKU_STATUS_UNRESOLVED,
    is_placeholder_code,
)


class AliasConflictError(RuntimeError):
    """A (mall, url) already belongs to a different identity; fail-closed."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_identities (
    id INTEGER PRIMARY KEY,
    mall TEXT NOT NULL,
    product_identity TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_crawl_identities_mall_product
    ON crawl_identities(mall, product_identity) WHERE product_identity IS NOT NULL;

CREATE TABLE IF NOT EXISTS crawl_urls (
    id INTEGER PRIMARY KEY,
    mall TEXT NOT NULL,
    url TEXT NOT NULL,
    identity_id INTEGER NOT NULL REFERENCES crawl_identities(id),
    alias_kind TEXT NOT NULL,
    source_entry_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(mall, url)
);

CREATE TABLE IF NOT EXISTS crawl_results (
    id INTEGER PRIMARY KEY,
    identity_id INTEGER NOT NULL REFERENCES crawl_identities(id),
    mall TEXT NOT NULL,
    original_url TEXT NOT NULL,
    submitted_url TEXT NOT NULL,
    returned_url TEXT,
    canonical_url TEXT,
    crawl_id TEXT,
    real_sku_id TEXT,
    sku_status TEXT NOT NULL,
    title TEXT,
    price TEXT,
    comment_count INTEGER,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    succeeded_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_crawl_results_current
    ON crawl_results(identity_id) WHERE is_current=1;
CREATE INDEX IF NOT EXISTS ix_crawl_results_identity ON crawl_results(identity_id);

CREATE TABLE IF NOT EXISTS document_references (
    id INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL,
    mall TEXT NOT NULL,
    original_url TEXT NOT NULL,
    identity_id INTEGER NOT NULL REFERENCES crawl_identities(id),
    source TEXT NOT NULL DEFAULT '',
    source_entry_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(document_id, mall, original_url)
);
"""


class SharedCrawlCache:
    """Standalone SQLite store for cross-day crawl evidence and URL identity."""

    def __init__(
        self,
        path: str | Path,
        *,
        url_parsers: Mapping[str, Callable[[str], str | None]] | None = None,
        placeholder_prefixes: Sequence[str] = PLACEHOLDER_PREFIXES_DEFAULT,
    ) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._url_parsers: dict[str, Callable[[str], str | None]] = (
            dict(url_parsers) if url_parsers is not None else dict(MALL_IDENTITY_PARSERS)
        )
        self._placeholder_prefixes = tuple(placeholder_prefixes)
        self._initialize()

    # Connections are intentionally never cached (same rationale as
    # DailyAuditStore): ticks may be interrupted or cross threads.
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
        return datetime.now(UTC).isoformat(timespec="seconds")

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
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

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

    @staticmethod
    def _normalize_price(price: Any) -> str | None:
        if price is None:
            return None
        if isinstance(price, bool) or not isinstance(price, (str, int, float, Decimal)):
            raise ValueError("price must be a str, int, float, Decimal, or None")
        return str(price)

    @staticmethod
    def _result_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["is_current"] = bool(result.get("is_current"))
        try:
            result["payload"] = json.loads(result.get("payload_json") or "{}")
        except (TypeError, ValueError):
            result["payload"] = {}
        return result

    # ---- identity resolution ---------------------------------------------------

    def _parsed_identity(self, mall: str, candidates: Sequence[str | None]) -> str | None:
        """First parser hit over the ordered URL candidates, or None (conservative MISS)."""
        parser = self._url_parsers.get(mall)
        if parser is None:
            return None
        for url in candidates:
            if not url:
                continue
            try:
                parsed = parser(url)
            except Exception:  # noqa: BLE001 - conservative MISS on parser failure
                parsed = None
            if parsed is None:
                continue
            identity = str(parsed).strip()
            if identity:
                return identity
        return None

    def _resolve_or_create_identity(
        self,
        connection: sqlite3.Connection,
        mall: str,
        *,
        original: str | None,
        submitted: str | None,
        returned: str | None,
        canonical: str | None,
    ) -> int:
        product_identity = self._parsed_identity(
            mall, (returned, canonical, submitted, original)
        )
        if product_identity is not None:
            row = connection.execute(
                "SELECT id FROM crawl_identities WHERE mall=? AND product_identity=?",
                (mall, product_identity),
            ).fetchone()
            if row is not None:
                return int(row["id"])
            cursor = connection.execute(
                "INSERT INTO crawl_identities(mall, product_identity, created_at) VALUES (?, ?, ?)",
                (mall, product_identity, self._utcnow()),
            )
            return int(cursor.lastrowid)
        # Parser MISS: a repeat record of an already-registered *input* URL
        # belongs to that URL's identity so repeat successes append history
        # under one identity.  Redirect outputs (returned/canonical) never
        # drive adoption - an unknown input with a redirect onto a foreign
        # identity stays a fresh NULL identity and must fail closed in the
        # alias step.  The partial unique index deliberately allows many NULL
        # product_identity rows.
        for candidate in (original, submitted):
            if candidate is None:
                continue
            alias = connection.execute(
                "SELECT identity_id FROM crawl_urls WHERE mall=? AND url=?",
                (mall, candidate),
            ).fetchone()
            if alias is not None:
                return int(alias["identity_id"])
        cursor = connection.execute(
            "INSERT INTO crawl_identities(mall, product_identity, created_at) VALUES (?, NULL, ?)",
            (mall, self._utcnow()),
        )
        return int(cursor.lastrowid)

    def _resolve_identity_id(
        self, connection: sqlite3.Connection, mall: str, url: str
    ) -> tuple[int, str] | None:
        """Read-only identity resolution: Level 1/2 alias hit, then Level 3 parser."""
        row = connection.execute(
            "SELECT identity_id FROM crawl_urls WHERE mall=? AND url=?", (mall, url)
        ).fetchone()
        if row is not None:
            return int(row["identity_id"]), "EXACT"
        product_identity = self._parsed_identity(mall, [url])
        if product_identity is not None:
            identity = connection.execute(
                "SELECT id FROM crawl_identities WHERE mall=? AND product_identity=?",
                (mall, product_identity),
            ).fetchone()
            if identity is not None:
                return int(identity["id"]), "MALL_PARSER"
        return None

    # ---- document references ---------------------------------------------------

    def _upsert_document_reference(
        self,
        connection: sqlite3.Connection,
        *,
        document_id: str,
        mall: str,
        original_url: str,
        identity_id: int,
        source: str,
        source_entry_id: int | None,
        created_at: str,
    ) -> int:
        row = connection.execute(
            """SELECT id, identity_id FROM document_references
               WHERE document_id=? AND mall=? AND original_url=?""",
            (document_id, mall, original_url),
        ).fetchone()
        if row is not None:
            if int(row["identity_id"]) != identity_id:
                raise AliasConflictError(
                    f"document reference (document_id={document_id}, mall={mall}, "
                    f"original_url={original_url}) already belongs to identity "
                    f"{int(row['identity_id'])}, not {identity_id}"
                )
            return int(row["id"])
        cursor = connection.execute(
            """INSERT INTO document_references
               (document_id, mall, original_url, identity_id, source, source_entry_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (document_id, mall, original_url, identity_id, source, source_entry_id, created_at),
        )
        return int(cursor.lastrowid)

    # ---- public API ------------------------------------------------------------

    def table_names(self) -> set[str]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        return {row["name"] for row in rows}

    def record_success(
        self,
        mall: str,
        submitted_url: str,
        *,
        original_url: str | None = None,
        returned_url: str | None = None,
        canonical_url: str | None = None,
        crawl_id: str | None = None,
        real_sku_id: str | None = None,
        sku_status: str = SKU_STATUS_UNRESOLVED,
        title: str | None = None,
        price: str | int | float | Decimal | None = None,
        comment_count: int | None = None,
        source: str = "",
        succeeded_at: str | None = None,
        payload: Mapping[str, Any] | None = None,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one successful crawl as a new append-only evidence row."""
        mall_value = self._require_text(mall, "mall")
        submitted_value = self._require_text(submitted_url, "submitted_url")
        source_value = self._require_text(source, "source")
        returned_value = self._optional_text(returned_url)
        canonical_value = self._optional_text(canonical_url)
        original_value = self._optional_text(original_url) or submitted_value

        if sku_status not in SKU_STATUSES:
            raise ValueError(
                f"sku_status must be one of {sorted(SKU_STATUSES)}, got {sku_status!r}"
            )
        sku_value = self._optional_text(real_sku_id)
        if sku_status == SKU_STATUS_UNRESOLVED:
            if sku_value is not None:
                raise ValueError("real_sku_id must be empty when sku_status is UNRESOLVED")
        elif sku_value is None:
            raise ValueError(
                f"real_sku_id is required when sku_status is {sku_status}"
            )
        crawl_value = self._optional_text(crawl_id)
        if sku_value is not None:
            # Placeholder defence: even a claimed CONFIRMED SKU that smells like
            # a placeholder (or echoes the crawl id) must never reach storage.
            if is_placeholder_code(sku_value, self._placeholder_prefixes):
                raise ValueError(
                    f"real_sku_id {sku_value!r} matches a placeholder prefix; refusing to store"
                )
            if crawl_value is not None and sku_value == crawl_value:
                raise ValueError(
                    "real_sku_id equals crawl_id; refusing placeholder echo as SKU evidence"
                )

        if succeeded_at is None:
            succeeded_value = self._utcnow()
        else:
            succeeded_value = self._utc_timestamp(succeeded_at, field="succeeded_at")
        price_value = self._normalize_price(price)
        if comment_count is not None and (
            isinstance(comment_count, bool) or not isinstance(comment_count, int)
        ):
            raise ValueError("comment_count must be an integer or None")
        if payload is not None and not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping or None")
        payload_json = self._canonical_json(dict(payload or {}))
        title_value = self._optional_text(title)
        document_value = self._optional_text(document_id)
        now = self._utcnow()

        with self._transaction(immediate=True) as connection:
            identity_id = self._resolve_or_create_identity(
                connection,
                mall_value,
                original=original_value,
                submitted=submitted_value,
                returned=returned_value,
                canonical=canonical_value,
            )

            # Register every distinct URL of this success as an alias of the
            # identity; a URL owned by another identity fails the whole write.
            inserted_alias_ids: list[int] = []
            seen_urls: set[str] = set()
            for candidate in (original_value, submitted_value, returned_value, canonical_value):
                if candidate is None or candidate in seen_urls:
                    continue
                seen_urls.add(candidate)
                alias = connection.execute(
                    "SELECT id, identity_id FROM crawl_urls WHERE mall=? AND url=?",
                    (mall_value, candidate),
                ).fetchone()
                if alias is None:
                    cursor = connection.execute(
                        """INSERT INTO crawl_urls
                           (mall, url, identity_id, alias_kind, source_entry_id, created_at)
                           VALUES (?, ?, ?, 'CRAWL_CHAIN', NULL, ?)""",
                        (mall_value, candidate, identity_id, now),
                    )
                    inserted_alias_ids.append(int(cursor.lastrowid))
                elif int(alias["identity_id"]) != identity_id:
                    raise AliasConflictError(
                        f"(mall={mall_value}, url={candidate}) already belongs to identity "
                        f"{int(alias['identity_id'])}, not {identity_id}"
                    )

            current = connection.execute(
                "SELECT id, succeeded_at FROM crawl_results WHERE identity_id=? AND is_current=1",
                (identity_id,),
            ).fetchone()
            cursor = connection.execute(
                """INSERT INTO crawl_results
                   (identity_id, mall, original_url, submitted_url, returned_url, canonical_url,
                    crawl_id, real_sku_id, sku_status, title, price, comment_count, source,
                    payload_json, succeeded_at, created_at, is_current)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    identity_id,
                    mall_value,
                    original_value,
                    submitted_value,
                    returned_value,
                    canonical_value,
                    crawl_value,
                    sku_value,
                    sku_status,
                    title_value,
                    price_value,
                    comment_count,
                    source_value,
                    payload_json,
                    succeeded_value,
                    now,
                ),
            )
            new_id = int(cursor.lastrowid)

            # Current election: the newest (succeeded_at, id) wins; a late
            # arrival with an older succeeded_at never steals current.
            becomes_current = current is None or (succeeded_value, new_id) > (
                str(current["succeeded_at"]),
                int(current["id"]),
            )
            if current is not None and becomes_current:
                connection.execute(
                    "UPDATE crawl_results SET is_current=0 WHERE id=?", (int(current["id"]),)
                )
            if becomes_current:
                connection.execute(
                    "UPDATE crawl_results SET is_current=1 WHERE id=?", (new_id,)
                )

            if document_value is not None:
                self._upsert_document_reference(
                    connection,
                    document_id=document_value,
                    mall=mall_value,
                    original_url=original_value,
                    identity_id=identity_id,
                    source=source_value,
                    source_entry_id=new_id,
                    created_at=now,
                )

            for alias_id in inserted_alias_ids:
                connection.execute(
                    "UPDATE crawl_urls SET source_entry_id=? WHERE id=?", (new_id, alias_id)
                )

            row = connection.execute(
                "SELECT * FROM crawl_results WHERE id=?", (new_id,)
            ).fetchone()
            return self._result_row(row)  # type: ignore[return-value]

    def lookup_current(self, mall: str, url: str) -> dict[str, Any] | None:
        """Three-level read-only lookup: alias exact, then mall-parser identity."""
        mall_value = mall.strip() if isinstance(mall, str) else ""
        url_value = url.strip() if isinstance(url, str) else ""
        if not mall_value or not url_value:
            return None
        with self._reader() as connection:
            resolved = self._resolve_identity_id(connection, mall_value, url_value)
            if resolved is None:
                return None
            identity_id, match_level = resolved
            row = connection.execute(
                "SELECT * FROM crawl_results WHERE identity_id=? AND is_current=1",
                (identity_id,),
            ).fetchone()
            if row is None:
                return None
            history_count = connection.execute(
                "SELECT COUNT(*) FROM crawl_results WHERE identity_id=?", (identity_id,)
            ).fetchone()[0]
            return {
                "match_level": match_level,
                "identity_id": identity_id,
                "mall": mall_value,
                "entry": self._result_row(row),
                "history_count": int(history_count),
            }

    def history(self, mall: str, url: str) -> list[dict[str, Any]]:
        """All successful rows for the resolved identity, oldest first."""
        mall_value = mall.strip() if isinstance(mall, str) else ""
        url_value = url.strip() if isinstance(url, str) else ""
        if not mall_value or not url_value:
            return []
        with self._reader() as connection:
            resolved = self._resolve_identity_id(connection, mall_value, url_value)
            if resolved is None:
                return []
            rows = connection.execute(
                "SELECT * FROM crawl_results WHERE identity_id=? ORDER BY succeeded_at, id",
                (resolved[0],),
            ).fetchall()
        return [self._result_row(row) for row in rows]  # type: ignore[misc]

    def attach_document_reference(
        self,
        document_id: str,
        *,
        mall: str,
        url: str,
        source: str = "",
    ) -> dict[str, Any] | None:
        """Attach one document provenance row; unresolved URLs are not stored."""
        document_value = self._require_text(document_id, "document_id")
        mall_value = mall.strip() if isinstance(mall, str) else ""
        url_value = url.strip() if isinstance(url, str) else ""
        if not mall_value or not url_value:
            return None
        source_value = source.strip() if isinstance(source, str) else ""
        now = self._utcnow()
        with self._transaction(immediate=True) as connection:
            resolved = self._resolve_identity_id(connection, mall_value, url_value)
            if resolved is None:
                return None
            identity_id, _ = resolved
            reference_id = self._upsert_document_reference(
                connection,
                document_id=document_value,
                mall=mall_value,
                original_url=url_value,
                identity_id=identity_id,
                source=source_value,
                source_entry_id=None,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM document_references WHERE id=?", (reference_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def document_references(self, document_id: str) -> list[dict[str, Any]]:
        """All provenance rows of one document, ascending by id."""
        document_value = self._optional_text(document_id)
        if document_value is None:
            return []
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM document_references WHERE document_id=? ORDER BY id",
                (document_value,),
            ).fetchall()
        return [dict(row) for row in rows]
