"""Read-only projection for the customer prewarm dashboard (Issue #13).

Deep read-model module: the dashboard API layer stays thin and this module
owns every query, whitelist and KPI unit semantics.  Zero writes, zero
network, zero FastAPI imports — all three databases (the customer prewarm
store, the shared crawl cache and STEP1's per-day ``state.sqlite3``) are
opened through sqlite ``mode=ro`` URIs, so a GET can never create or modify
a file (a missing store is reported, never created).

Degradation contract: a missing store file or a ``sqlite3.OperationalError``
(locked / corrupt / unreadable) never raises.  The affected store's
contribution is zero-filled, ``available.<store>`` flips to ``False`` and a
human-readable note is appended to ``degraded_reasons``; the payload itself
is always returned (HTTP-200 degraded shape at the API layer).  Pure data
absence — e.g. a legacy store whose Round-2 observation table does not exist
yet, or a candidate whose mall cannot be proven — is NOT a degradation: it
yields an empty contribution with no note.

``payload_json`` and every other opaque blob column are NEVER projected —
each row leaves this module through an explicit field whitelist.

Day scope (Review Round 2, B2): summary, list AND detail consistently see
only the candidates of the viewed business day — the observed candidate set
of ``prewarm_daily_observations(business_date)``.  There is NO fallback to
the candidate's first-seen date for scoping.  The summary payload reports
the transparent unit as ``scope`` = ``{"business_date", ...,
"candidate_count_in_scope"}``.

Remote job resolution (B2): per candidate the linkage row
(``prewarm_remote_job_links``, latest ``recorded_at``) pins the job to ITS
business date, and the job row is read from that date's
``runtime_root/<date>/state.sqlite3``; a legacy candidate without linkage
falls back to the candidate's OWN first-seen date.  Job ids collide across
dates, so jobs are keyed by ``(business_date, job_id)`` — the selected
date's DB is NEVER queried by bare integer id.

Cache current-row resolution (Review Round 2, B1): identity is ``mall + URL``
(parent #6).  ``resolved_identity_id`` → the ``crawl_results`` row with
``identity_id=? AND is_current=1`` (the mall is inherent).  Candidates
without a resolved identity fall back through the Shared Crawl Cache's OWN
identity contract — ``SharedCrawlCache.lookup_current(mall, url)`` — for at
most the ``(mall, url)`` pairs with ``url`` in ``(lookup_url, original_url)``
and a PROVEN mall: the candidate's non-empty mall, else the mall derived
from ``classify_reference_url`` (staples URL → ``stbchina``, zhaoshang URL →
the mapped mall key), else NO row at all.  URL-only matching and SKU
matching are forbidden: a row whose mall differs from the proven mall can
never be attached.

KPI units (single authority for the UI):

- ``reference_link_count`` / ``unique_url_count``: ``prewarm_links`` rows of
  the day scope / distinct ``original_url`` among them.
- ``cache_hit_candidate_count``: day-scope candidates with
  ``cache_status='CACHE_HIT'``.
- ``historical_cache_hit_count``: CACHE_HIT candidates whose CURRENT cache
  row ``succeeded_at`` is strictly before the business day's 00:00
  Asia/Shanghai (i.e. the hit predates the day being viewed).
- ``daily_internet_hit_count`` / ``prewarm_new_success_count`` /
  ``manual_recrawl_hit_count``: CACHE_HIT candidates by the CURRENT cache
  row's class from :func:`classify_crawl_producer` — the single
  classification authority (``daily-internet``; ``staples-local-prewarm`` +
  ``customer-prewarm-zhaoshang``; ``manual-recrawl``).  Legacy rows with a
  NULL ``producer`` classify by ``source`` until the URL is re-crawled
  (documented limitation for already-absorbed rows).
- ``zhaoshang_running_candidate_count`` / ``..._job_count``: REMOTE_*
  candidates whose resolved ``remote_jobs.state`` is in the running set
  {SUBMIT_INTENT, PRIORITY_PENDING, SUBMITTED, RUNNING, SUBMIT_UNKNOWN};
  the job count deduplicates by ``(business_date, remote_job_id)``.
- ``staples_running_count``: candidates that are STAPLES_PENDING OR own a
  staples sidecar job in {PENDING, RETRY_WAIT}, counted once each (union,
  no overlap).
- ``failure_count``: bucket sum of ROUTING_BLOCKED + MANUAL_SKIP candidates
  + staples jobs FAILED + REMOTE_* candidates whose job state is FAILED
  (per the design formula; a candidate can contribute to two buckets).
- ``url_parsed_unverified_count`` / ``unresolved_sku_count``: candidates whose
  CURRENT cache row carries that ``sku_status`` — the same unit as the
  ``unverified_sku`` / ``unresolved_sku`` filters (any candidate status with a
  current cache row, not only CACHE_HIT ones).
- ``remote_job_count``: distinct ``(business_date, job_id)`` refs bound to
  REMOTE_* candidates.
- ``remote_url_count``: candidate rows bound to non-terminal remote jobs
  (terminal = {COMPLETED, FAILED}; unknown state never counts).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from datetime import time as dt_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from app.services.crawl_url_identity import (
    SKU_STATUS_CONFIRMED,
    SKU_STATUS_UNRESOLVED,
    SKU_STATUS_UPSTREAM_PROVIDED,
    SKU_STATUS_URL_PARSED_UNVERIFIED,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_MANUAL_SKIP,
    CACHE_STATUS_PENDING_LOOKUP,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_REMOTE_REUSED,
    CACHE_STATUS_ROUTING_BLOCKED,
    CACHE_STATUS_STAPLES_PENDING,
)
from app.services.customer_reference_routing import (
    OUTCOME_STAPLES,
    OUTCOME_ZHAOSHANG,
    classify_reference_url,
)
from app.services.incremental_audit_runtime import DEFAULT_RUNTIME_ROOT
from app.services.shared_crawl_cache import (
    PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
    PRODUCER_DAILY_INTERNET,
    PRODUCER_MANUAL_RECRAWL_STAPLES,
    PRODUCER_MANUAL_RECRAWL_ZHAOSHANG,
    PRODUCER_STAPLES_LOCAL,
    SharedCrawlCache,
)
from app.services.staples_prewarm import (
    STAPLES_JOB_STATE_FAILED,
    STAPLES_JOB_STATE_PENDING,
    STAPLES_JOB_STATE_RETRY_WAIT,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")

_PAGE_SIZE_DEFAULT = 50
_PAGE_SIZE_MAX = 200
# Public alias: the API layer validates ``1..PAGE_SIZE_MAX`` against this SAME
# constant (single source; the underscore name stays module-internal).
PAGE_SIZE_MAX = _PAGE_SIZE_MAX

# Cache statuses of candidates that own a remote (招商局) job.
_REMOTE_CANDIDATE_STATUSES = frozenset(
    {CACHE_STATUS_REMOTE_DISPATCHED, CACHE_STATUS_REMOTE_REUSED}
)
# remote_jobs states that still need work (production-observed set; any state
# outside this set is treated as non-running per the confirmed state list).
_REMOTE_RUNNING_JOB_STATES = frozenset(
    {"SUBMIT_INTENT", "PRIORITY_PENDING", "SUBMITTED", "RUNNING", "SUBMIT_UNKNOWN"}
)
# remote_jobs states that will never progress further on their own.
_REMOTE_TERMINAL_JOB_STATES = frozenset({"COMPLETED", "FAILED"})
_FAILURE_CANDIDATE_STATUSES = frozenset({CACHE_STATUS_ROUTING_BLOCKED, CACHE_STATUS_MANUAL_SKIP})
_STAPLES_RUNNING_JOB_STATES = frozenset({STAPLES_JOB_STATE_PENDING, STAPLES_JOB_STATE_RETRY_WAIT})

# ---- source/producer classification (single authority, B3) ----------------------
#
# ``classify_crawl_producer(source, producer)`` is THE one public authority
# (the former ``classify_crawl_source`` was folded into it and removed):
# a non-NULL producer wins and maps directly; a NULL producer classifies by
# source — the documented legacy behaviour for rows absorbed before the
# producer column existed (they keep their class until re-crawled).

# Class values: the exact set the source filter and the UI select accept.
CLASS_DAILY_INTERNET = "daily-internet"
CLASS_CUSTOMER_PREWARM_ZHAOSHANG = "customer-prewarm-zhaoshang"
CLASS_STAPLES_LOCAL_PREWARM = "staples-local-prewarm"
CLASS_MANUAL_RECRAWL = "manual-recrawl"
CLASS_OTHER = "other"

# producer value → class (direct attribution wins over source).
_CLASS_BY_PRODUCER = {
    PRODUCER_DAILY_INTERNET: CLASS_DAILY_INTERNET,
    PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG: CLASS_CUSTOMER_PREWARM_ZHAOSHANG,
    PRODUCER_STAPLES_LOCAL: CLASS_STAPLES_LOCAL_PREWARM,
    PRODUCER_MANUAL_RECRAWL_STAPLES: CLASS_MANUAL_RECRAWL,
    PRODUCER_MANUAL_RECRAWL_ZHAOSHANG: CLASS_MANUAL_RECRAWL,
}
# Legacy NULL-producer rows: source value → class.
_CLASS_BY_SOURCE = {
    "daily-internet": CLASS_DAILY_INTERNET,
    "staples-local": CLASS_STAPLES_LOCAL_PREWARM,
    "manual-recrawl-staples": CLASS_MANUAL_RECRAWL,
}

# sku_status → (label, requires_review).  Unknown values degrade to the raw
# string and keep the review flag (fail-visible, never silently trusted).
_SKU_LABELS = {
    SKU_STATUS_CONFIRMED: ("已确认", False),
    SKU_STATUS_UPSTREAM_PROVIDED: ("上游提供", False),
    SKU_STATUS_URL_PARSED_UNVERIFIED: ("URL解析·未验证", True),
    SKU_STATUS_UNRESOLVED: ("未解析", True),
}

_CACHE_STATUS_LABELS = {
    CACHE_STATUS_PENDING_LOOKUP: "待查询",
    CACHE_STATUS_PENDING_PREWARM: "待预爬",
    CACHE_STATUS_STAPLES_PENDING: "史泰博预爬中",
    CACHE_STATUS_REMOTE_DISPATCHED: "招商局已派发",
    CACHE_STATUS_REMOTE_REUSED: "招商局复用中",
    CACHE_STATUS_CACHE_HIT: "缓存命中",
    CACHE_STATUS_ROUTING_BLOCKED: "路由受阻",
    CACHE_STATUS_MANUAL_SKIP: "人工跳过",
}

# Whitelisted crawl_results columns (NEVER payload_json).
_CACHE_RESULT_COLUMNS = (
    "id",
    "identity_id",
    "mall",
    "original_url",
    "submitted_url",
    "returned_url",
    "canonical_url",
    "crawl_id",
    "real_sku_id",
    "sku_status",
    "title",
    "price",
    "comment_count",
    "source",
    "producer",
    "succeeded_at",
)

# Whitelisted list_prewarm_links row fields.
_LINK_ROW_KEYS = (
    "candidate_id",
    "document_ids",
    "document_count",
    "mall",
    "original_url",
    "cache_status",
    "cache_status_label",
    "match_level",
    "source_class",
    "producer",
    "title",
    "price",
    "crawl_id",
    "real_sku_id",
    "sku_status",
    "sku_status_label",
    "sku_requires_review",
    "last_success_at",
    "remote_job_state",
    "staples_job_state",
)

_AVAILABLE_KEYS = ("prewarm_store", "shared_cache", "state_db")


# ---- pure helpers ------------------------------------------------------------


def classify_crawl_producer(source: Any, producer: Any) -> str:
    """Classify one cache row into its UI source class (single authority).

    A non-NULL ``producer`` wins and maps directly onto one of the five
    class values (unknown producers → ``"other"``); a NULL/empty producer
    falls back to the legacy ``source`` mapping.  The former
    ``classify_crawl_source`` is folded into this function and removed.
    """
    producer_value = str(producer or "").strip()
    if producer_value:
        return _CLASS_BY_PRODUCER.get(producer_value, CLASS_OTHER)
    return _CLASS_BY_SOURCE.get(str(source or "").strip(), CLASS_OTHER)


def sku_status_label(sku_status: Any) -> str:
    """Human label for a ``sku_status`` value; unknown values pass through."""
    value = str(sku_status or "")
    return _SKU_LABELS[value][0] if value in _SKU_LABELS else value


def sku_requires_review(sku_status: Any) -> bool:
    """True for URL-parsed/unresolved (and any unknown) SKU evidence."""
    value = str(sku_status or "")
    if value in _SKU_LABELS:
        return _SKU_LABELS[value][1]
    return True


def cache_status_label(cache_status: Any) -> str:
    """Human label for a candidate ``cache_status``; unknown values pass through."""
    value = str(cache_status or "")
    return _CACHE_STATUS_LABELS.get(value, value)


def prewarm_paths(business_date: str, runtime_root: str | Path | None = None) -> SimpleNamespace:
    """The three read-only inputs of one prewarm dashboard view (no IO)."""
    root = Path(runtime_root) if runtime_root is not None else Path(DEFAULT_RUNTIME_ROOT)
    return SimpleNamespace(
        business_date=str(business_date),
        prewarm_db=root / "customer_prewarm.sqlite3",
        cache_db=root / "shared_crawl_cache.sqlite3",
        state_db=root / str(business_date) / "state.sqlite3",
    )


def _generated_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _business_day_cutoff_utc(business_date: str) -> datetime | None:
    """The business day's 00:00 Asia/Shanghai as a UTC instant (None if invalid)."""
    try:
        day = date.fromisoformat(str(business_date or ""))
    except ValueError:
        return None
    return datetime.combine(day, dt_time.min, tzinfo=_SHANGHAI).astimezone(UTC)


# ---- read-only plumbing --------------------------------------------------------


def _connect_ro(path: Path) -> sqlite3.Connection:
    """mode=ro URI connection: never creates files, never writes (seam for tests)."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _open_stores(paths: SimpleNamespace) -> tuple[dict[str, sqlite3.Connection], dict[str, bool], list[str]]:
    connections: dict[str, sqlite3.Connection] = {}
    available = {key: False for key in _AVAILABLE_KEYS}
    reasons: list[str] = []
    for key, path in (
        ("prewarm_store", paths.prewarm_db),
        ("shared_cache", paths.cache_db),
        ("state_db", paths.state_db),
    ):
        if not path.is_file():
            reasons.append(f"{key} missing: {path.name}")
            continue
        try:
            connections[key] = _connect_ro(path)
        except sqlite3.Error as exc:
            reasons.append(f"{key} unavailable: {exc}")
            continue
        available[key] = True
    return connections, available, reasons


def _close_all(view: Mapping[str, Any]) -> None:
    """Close every connection this view opened (selected-day + per-day job DBs).

    ``view["cache_ro"]`` (the SharedCrawlCache read-only facade used by the B1
    fallback) keeps no persistent connection — its readers open per call — so
    there is nothing to close there.
    """
    for connections in (view.get("connections") or {}, view.get("day_connections") or {}):
        for connection in connections.values():
            if connection is None:
                continue
            try:
                connection.close()
            except sqlite3.Error:
                pass


def _scan(
    connection: sqlite3.Connection,
    key: str,
    available: dict[str, bool],
    reasons: list[str],
    loader: Callable[[sqlite3.Connection], Any],
) -> Any:
    """Run one store query; an OperationalError degrades that store instead of raising."""
    try:
        return loader(connection)
    except sqlite3.Error as exc:
        available[key] = False
        reasons.append(f"{key} read failed: {exc}")
        return None


def _execute_producer_aware(
    connection: sqlite3.Connection, sql_with: str, sql_without: str, params: list[Any] | None = None
) -> list[sqlite3.Row]:
    """Run a whitelist SELECT that names the ``producer`` column.

    A cache DB never re-opened RW after the Round-2 migration lacks the
    column; those legacy files degrade to NULL-producer projection instead of
    losing the whole shared-cache contribution.  Any other error propagates.
    """
    try:
        return connection.execute(sql_with, params or []).fetchall()
    except sqlite3.OperationalError as exc:
        if "producer" not in str(exc).lower():
            raise
        return connection.execute(sql_without, params or []).fetchall()


def _load_candidates(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, mall, original_url, lookup_url, business_date, cache_status,
                  match_level, resolved_identity_id, remote_job_id, routing_url
           FROM prewarm_candidates ORDER BY id"""
    ).fetchall()
    return [dict(row) for row in rows]


def _load_document_ids(connection: sqlite3.Connection) -> dict[int, list[str]]:
    rows = connection.execute(
        "SELECT candidate_id, document_id FROM prewarm_links ORDER BY id"
    ).fetchall()
    documents: dict[int, list[str]] = {}
    for row in rows:
        documents.setdefault(int(row["candidate_id"]), []).append(str(row["document_id"]))
    return documents


def _load_staples_jobs(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    rows = connection.execute(
        "SELECT candidate_id, state, attempt_count, updated_at FROM staples_prewarm_jobs"
    ).fetchall()
    return {int(row["candidate_id"]): dict(row) for row in rows}


def _load_staples_jobs_best_effort(
    connection: sqlite3.Connection, reasons: list[str]
) -> dict[int, dict[str, Any]]:
    """Sidecar table may predate Issue #11; its absence is a note, never an error."""
    try:
        return _load_staples_jobs(connection)
    except sqlite3.Error as exc:
        reasons.append(f"staples sidecar jobs unavailable: {exc}")
        return {}


def _load_current_cache_rows(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """Current cache rows by identity (newest current election wins per identity)."""
    columns = ", ".join(_CACHE_RESULT_COLUMNS)
    legacy_columns = ", ".join(name for name in _CACHE_RESULT_COLUMNS if name != "producer")
    rows = _execute_producer_aware(
        connection,
        f"SELECT {columns} FROM crawl_results WHERE is_current=1 ORDER BY id",
        f"SELECT {legacy_columns} FROM crawl_results WHERE is_current=1 ORDER BY id",
    )
    by_identity: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item.setdefault("producer", None)
        # Later id wins, mirroring the cache's newest-current election.
        by_identity[int(item["identity_id"])] = item
    return by_identity


def _load_remote_jobs(
    connection: sqlite3.Connection, job_ids: list[int]
) -> dict[int, dict[str, Any]]:
    if not job_ids:
        return {}
    placeholders = ",".join("?" * len(job_ids))
    rows = connection.execute(
        f"""SELECT id, state, task_name, remote_identity, updated_at
            FROM remote_jobs WHERE id IN ({placeholders})""",
        [int(job_id) for job_id in job_ids],
    ).fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def _load_history_by_identity(connection: sqlite3.Connection, identity_id: int) -> list[dict[str, Any]]:
    columns = (
        "id AS result_id, source, producer, title, price, real_sku_id, sku_status, "
        "succeeded_at, is_current"
    )
    legacy_columns = columns.replace("producer, ", "")
    rows = _execute_producer_aware(
        connection,
        f"SELECT {columns} FROM crawl_results WHERE identity_id=? ORDER BY succeeded_at, id",
        f"SELECT {legacy_columns} FROM crawl_results WHERE identity_id=? ORDER BY succeeded_at, id",
        [int(identity_id)],
    )
    return [
        {
            "result_id": int(row["result_id"]),
            "source_class": classify_crawl_producer(row["source"], row["producer"])
            if "producer" in row.keys()
            else classify_crawl_producer(row["source"], None),
            "title": row["title"],
            "price": row["price"],
            "real_sku_id": row["real_sku_id"],
            "sku_status": row["sku_status"],
            "succeeded_at": row["succeeded_at"],
            "is_current": bool(row["is_current"]),
        }
        for row in rows
    ]


# ---- day scope (B2) --------------------------------------------------------------


def _load_day_candidate_ids(view: dict[str, Any], connection: sqlite3.Connection) -> set[int]:
    """Candidate ids observed on the viewed business day (the ONLY scope).

    A legacy store whose observations table does not exist yet yields an
    empty scope WITHOUT a degraded note (data absence, not a read failure);
    any other read error degrades the prewarm store honestly.
    """
    try:
        rows = connection.execute(
            "SELECT DISTINCT candidate_id FROM prewarm_daily_observations WHERE business_date=?",
            (view["business_date"],),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return set()
        view["available"]["prewarm_store"] = False
        view["reasons"].append(f"prewarm_store read failed: {exc}")
        return set()
    return {int(row["candidate_id"]) for row in rows}


def _load_remote_job_links(
    view: dict[str, Any], connection: sqlite3.Connection, candidate_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """candidate_id → latest linkage row (max ``recorded_at``, tie-break max
    ``rowid`` — the same ordering the store's own
    ``remote_job_link_for_candidate`` uses).

    Missing linkage table (legacy store) → no linkage at all, without a
    degraded note — the per-candidate legacy fallback then applies.
    """
    if not candidate_ids:
        return {}
    placeholders = ",".join("?" * len(candidate_ids))
    try:
        rows = connection.execute(
            f"""SELECT candidate_id, business_date, remote_job_id
                FROM prewarm_remote_job_links
                WHERE candidate_id IN ({placeholders})
                ORDER BY recorded_at, rowid""",
            [int(candidate_id) for candidate_id in candidate_ids],
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {}
        view["available"]["prewarm_store"] = False
        view["reasons"].append(f"prewarm_store read failed: {exc}")
        return {}
    latest: dict[int, dict[str, Any]] = {}
    for row in rows:  # ordered → the last row per candidate wins
        latest[int(row["candidate_id"])] = {
            "business_date": str(row["business_date"]),
            "remote_job_id": int(row["remote_job_id"]),
        }
    return latest


def _day_state_connection(view: dict[str, Any], job_date: str) -> sqlite3.Connection | None:
    """One mode=ro connection per distinct job date, cached in the view.

    The selected date's DB is the already-open ``state_db`` connection; other
    dates are opened lazily exactly once.  A missing/unreadable day DB is a
    degraded note (its jobs zero-fill), never an exception.
    """
    if job_date == view["business_date"]:
        return view["connections"].get("state_db")
    if job_date in view["day_connections"]:
        return view["day_connections"][job_date]
    path = view["runtime_root"] / job_date / "state.sqlite3"
    if not path.is_file():
        view["day_connections"][job_date] = None
        view["reasons"].append(f"state_db[{job_date}] missing: state.sqlite3")
        return None
    try:
        connection = _connect_ro(path)
    except sqlite3.Error as exc:
        view["day_connections"][job_date] = None
        view["reasons"].append(f"state_db[{job_date}] unavailable: {exc}")
        return None
    view["day_connections"][job_date] = connection
    return connection


def _resolve_job_refs(view: dict[str, Any]) -> None:
    """Pin every scoped REMOTE candidate to ``(job_date, job_id)`` and load
    the referenced job rows per distinct date (job ids collide across dates,
    so the map is keyed by the pair — never by the bare integer id)."""
    links = view.get("job_links") or {}
    for candidate in view["candidates"]:
        candidate_id = int(candidate["id"])
        link = links.get(candidate_id)
        if link is not None:
            job_date = link["business_date"]
            job_id: Any = link["remote_job_id"]
        else:
            # Legacy pre-linkage row: the candidate's OWN first-seen date.
            job_date = str(candidate.get("business_date") or "")
            job_id = candidate.get("remote_job_id")
        if job_id is None or not job_date:
            continue
        try:
            date.fromisoformat(job_date)
        except ValueError:
            continue  # malformed evidence can never widen into a path
        view["job_refs"][candidate_id] = (job_date, int(job_id))

    by_date: dict[str, list[int]] = {}
    for job_date, job_id in view["job_refs"].values():
        by_date.setdefault(job_date, []).append(job_id)
    for job_date in sorted(by_date):
        connection = _day_state_connection(view, job_date)
        if connection is None:
            continue
        try:
            loaded = _load_remote_jobs(connection, by_date[job_date])
        except sqlite3.Error as exc:
            view["reasons"].append(f"state_db[{job_date}] read failed: {exc}")
            continue
        for job_id, row in loaded.items():
            view["remote_jobs"][(job_date, job_id)] = row


def _remote_job_for(view: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """The resolved remote job row of one candidate ({} when unresolvable)."""
    ref = view["job_refs"].get(int(candidate["id"]))
    if ref is None:
        return {}
    return view["remote_jobs"].get(ref) or {}


def _candidate_view(paths: SimpleNamespace) -> dict[str, Any]:
    """Open the stores read-only and load the raw rows (best-effort each),
    already scoped to the viewed business day."""
    connections, available, reasons = _open_stores(paths)
    view: dict[str, Any] = {
        "business_date": paths.business_date,
        "runtime_root": paths.prewarm_db.parent,
        "connections": connections,
        "day_connections": {},  # job_date -> ro connection (None = known missing)
        "available": available,
        "reasons": reasons,
        "candidates": [],
        "documents": {},
        "staples_jobs": {},
        "cache_by_identity": {},
        "cache_ro": None,
        "job_links": {},
        "job_refs": {},  # candidate_id -> (job_date, job_id)
        "remote_jobs": {},  # (job_date, job_id) -> row
    }
    prewarm = connections.get("prewarm_store")
    if prewarm is not None:
        candidates = _scan(prewarm, "prewarm_store", available, reasons, _load_candidates)
        documents = _scan(prewarm, "prewarm_store", available, reasons, _load_document_ids)
        view["documents"] = documents or {}
        view["staples_jobs"] = _load_staples_jobs_best_effort(prewarm, reasons)
        day_ids = _load_day_candidate_ids(view, prewarm)
        # Single scope cut: summary / list / detail all iterate these rows only.
        view["candidates"] = [
            candidate for candidate in candidates or [] if int(candidate["id"]) in day_ids
        ]
        view["job_links"] = _load_remote_job_links(
            view, prewarm, [int(candidate["id"]) for candidate in view["candidates"]]
        )
    cache = connections.get("shared_cache")
    if cache is not None:
        loaded = _scan(cache, "shared_cache", available, reasons, _load_current_cache_rows)
        if loaded is not None:
            view["cache_by_identity"] = loaded
        view["cache_ro"] = _open_cache_ro(paths.cache_db, reasons)
    _resolve_job_refs(view)
    return view


def _open_cache_ro(path: Path, reasons: list[str]) -> SharedCrawlCache | None:
    """The B1 fallback's read-only facade (opened once; never creates files)."""
    try:
        return SharedCrawlCache.open_read_only(path)
    except (OSError, sqlite3.Error) as exc:
        reasons.append(f"shared_cache unavailable: {exc}")
        return None


# ---- B1: mall-proven cache fallback ------------------------------------------------


def _reference_mappings(view: dict[str, Any]) -> list[Any] | None:
    """Zhaoshang mapping table, loaded lazily once per view (None = unloadable)."""
    if "_mappings" in view:
        return view["_mappings"]
    try:
        from scripts.zhaoshang_mall_mapping import load_zhaoshang_mall_mappings

        mappings: list[Any] | None = list(load_zhaoshang_mall_mappings())
    except Exception:  # noqa: BLE001 - mapping evidence is best-effort only
        mappings = None
    view["_mappings"] = mappings
    return mappings


def _proven_fallback_mall(view: dict[str, Any], candidate: Mapping[str, Any]) -> str | None:
    """The only mall a URL-only fallback may ever match against.

    ``candidate.mall`` when non-empty; else the mall proven by classifying the
    candidate's URL (original first, then the lookup rewrite): staples →
    ``stbchina``, zhaoshang → the mapped mall key; anything else → NO
    fallback (``None``).
    """
    mall = str(candidate.get("mall") or "").strip()
    if mall:
        return mall
    url = str(candidate.get("original_url") or "").strip() or str(
        candidate.get("lookup_url") or ""
    ).strip()
    if not url:
        return None
    mappings = _reference_mappings(view)
    if mappings is None:
        return None
    routing = classify_reference_url(url, mappings)
    if routing.outcome == OUTCOME_STAPLES:
        return "stbchina"
    if routing.outcome == OUTCOME_ZHAOSHANG:
        return str(routing.mall_key or "").strip() or None
    return None


def _cache_row_by_proven_mall(
    candidate: Mapping[str, Any], view: dict[str, Any]
) -> dict[str, Any] | None:
    """Delegating fallback: the cache's own identity contract (mall + URL).

    ``lookup_current`` is mall-scoped by construction (exact alias → mall
    parser → identity), so a row from ANOTHER mall can never be attached.
    SKU evidence is never consulted.  At most the ``(mall, lookup_url)`` and
    ``(mall, original_url)`` pairs are tried; the first hit wins.
    """
    cache_ro = view.get("cache_ro")
    if cache_ro is None:
        return None
    mall_value = _proven_fallback_mall(view, candidate)
    if not mall_value:
        return None
    for url in (candidate.get("lookup_url"), candidate.get("original_url")):
        url_value = str(url or "").strip()
        if not url_value:
            continue
        try:
            hit = cache_ro.lookup_current(mall_value, url_value)
        except sqlite3.Error:
            return None
        entry = (hit or {}).get("entry")
        if entry is not None:
            # Whitelist the entry (its raw dict still carries payload_json).
            row = {key: entry.get(key) for key in _CACHE_RESULT_COLUMNS}
            row.setdefault("producer", None)
            return row
    return None


def _current_cache_row(
    candidate: Mapping[str, Any], view: Mapping[str, Any]
) -> dict[str, Any] | None:
    """resolved_identity_id → current row; else the mall-PROVEN fallback.

    Identity = mall + URL (parent #6); a URL alone — let alone a SKU — never
    matches across malls.
    """
    identity_id = candidate.get("resolved_identity_id")
    if identity_id is not None:
        row = view["cache_by_identity"].get(int(identity_id))
        if row is not None:
            return row
    return _cache_row_by_proven_mall(candidate, view)


def _degraded(view: Mapping[str, Any]) -> tuple[bool, list[str]]:
    reasons = list(view["reasons"])
    return bool(reasons), reasons


# ---- summary --------------------------------------------------------------------


def build_prewarm_dashboard_summary(
    business_date: str, *, runtime_root: str | Path | None = None
) -> dict[str, Any]:
    """KPI summary of the prewarm read model for one business date.

    Always returns the full payload shape; stores that are missing or fail
    to read are zero-filled and reported through ``available`` /
    ``degraded_reasons`` (never an exception).  ``scope`` carries the
    transparent unit of every KPI: the candidates observed on THIS day.
    """
    paths = prewarm_paths(business_date, runtime_root)
    view = _candidate_view(paths)
    try:
        kpi: dict[str, Any] = {key: 0 for key in (
            "reference_link_count",
            "unique_url_count",
            "candidate_count",
            "cache_hit_candidate_count",
            "historical_cache_hit_count",
            "daily_internet_hit_count",
            "prewarm_new_success_count",
            "manual_recrawl_hit_count",
            "zhaoshang_running_candidate_count",
            "zhaoshang_running_job_count",
            "staples_running_count",
            "failure_count",
            "url_parsed_unverified_count",
            "unresolved_sku_count",
            "remote_job_count",
            "remote_url_count",
        )}
        kpi["status_distribution"] = []

        candidates: list[dict[str, Any]] = view["candidates"]
        kpi["reference_link_count"] = sum(
            len(view["documents"].get(int(candidate["id"])) or []) for candidate in candidates
        )
        kpi["unique_url_count"] = len({str(c["original_url"]) for c in candidates})
        kpi["candidate_count"] = len(candidates)
        cutoff = _business_day_cutoff_utc(business_date)

        status_counts: dict[str, int] = {}
        running_job_refs: set[tuple[str, int]] = set()
        bound_job_refs: set[tuple[str, int]] = set()
        for candidate in candidates:
            status = str(candidate["cache_status"])
            status_counts[status] = status_counts.get(status, 0) + 1
            candidate_id = int(candidate["id"])
            current = _current_cache_row(candidate, view)

            # SKU-review KPIs use the SAME unit as the unverified_sku /
            # unresolved_sku filters: ANY candidate whose current cache row
            # carries the sku_status counts (not only CACHE_HIT ones).
            if current is not None:
                sku_status = str(current["sku_status"] or "")
                if sku_status == SKU_STATUS_URL_PARSED_UNVERIFIED:
                    kpi["url_parsed_unverified_count"] += 1
                elif sku_status == SKU_STATUS_UNRESOLVED:
                    kpi["unresolved_sku_count"] += 1

            if status == CACHE_STATUS_CACHE_HIT:
                kpi["cache_hit_candidate_count"] += 1
                if current is not None:
                    source_class = classify_crawl_producer(
                        current["source"], current.get("producer")
                    )
                    if source_class == CLASS_DAILY_INTERNET:
                        kpi["daily_internet_hit_count"] += 1
                    elif source_class in (CLASS_STAPLES_LOCAL_PREWARM, CLASS_CUSTOMER_PREWARM_ZHAOSHANG):
                        kpi["prewarm_new_success_count"] += 1
                    elif source_class == CLASS_MANUAL_RECRAWL:
                        kpi["manual_recrawl_hit_count"] += 1
                    if cutoff is not None:
                        succeeded_at = _parse_utc(current["succeeded_at"])
                        if succeeded_at is not None and succeeded_at < cutoff:
                            kpi["historical_cache_hit_count"] += 1

            staples_job = view["staples_jobs"].get(candidate_id) or {}
            if status == CACHE_STATUS_STAPLES_PENDING or (
                staples_job.get("state") in _STAPLES_RUNNING_JOB_STATES
            ):
                kpi["staples_running_count"] += 1

            if status in _FAILURE_CANDIDATE_STATUSES:
                kpi["failure_count"] += 1
            if staples_job.get("state") == STAPLES_JOB_STATE_FAILED:
                kpi["failure_count"] += 1

            if status in _REMOTE_CANDIDATE_STATUSES:
                ref = view["job_refs"].get(candidate_id)
                if ref is not None:
                    bound_job_refs.add(ref)
                    job = view["remote_jobs"].get(ref) or {}
                    state = job.get("state")
                    if state in _REMOTE_RUNNING_JOB_STATES:
                        kpi["zhaoshang_running_candidate_count"] += 1
                        running_job_refs.add(ref)
                    if state in _REMOTE_TERMINAL_JOB_STATES:
                        if state == "FAILED":
                            kpi["failure_count"] += 1
                    else:
                        # Non-terminal job: its candidate URLs are still in flight.
                        kpi["remote_url_count"] += 1

        # List shape (single source of labels via cache_status_label; unknown
        # statuses degrade to the raw string), sorted by status for stability.
        kpi["status_distribution"] = [
            {"status": status, "label": cache_status_label(status), "count": count}
            for status, count in sorted(status_counts.items())
        ]
        kpi["zhaoshang_running_job_count"] = len(running_job_refs)
        kpi["remote_job_count"] = len(bound_job_refs)

        degraded, reasons = _degraded(view)
        return {
            "business_date": business_date,
            "generated_at": _generated_at(),
            "scope": {
                "business_date": business_date,
                "candidate_count_in_scope": len(candidates),
            },
            "available": dict(view["available"]),
            "degraded": degraded,
            "degraded_reasons": reasons,
            "kpi": kpi,
        }
    finally:
        _close_all(view)


# ---- link list --------------------------------------------------------------------


def _normalize_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only truthy, string-keyed filters (flag filters are truthy-only)."""
    if not filters:
        return {}
    if not isinstance(filters, Mapping):
        return {}
    return {str(key): value for key, value in filters.items() if value}


def _link_row_matches(
    candidate: Mapping[str, Any],
    current: Mapping[str, Any] | None,
    staples_job: Mapping[str, Any],
    remote_job: Mapping[str, Any],
    filters: Mapping[str, Any],
) -> bool:
    """Server-side filter evaluation (units identical to the KPI definitions)."""
    if "mall" in filters:
        needle = str(filters["mall"]).strip().lower()
        haystacks = [str(candidate.get("mall") or "")]
        if current is not None:
            haystacks.append(str(current.get("mall") or ""))
        if not any(needle in value.lower() for value in haystacks):
            return False
    status = str(candidate["cache_status"])
    if "status" in filters and status != str(filters["status"]):
        return False
    if "source" in filters:
        # The filter value is one of the 5 classification values and matches
        # the SAME authority as the KPIs (producer wins over source).
        if current is None or classify_crawl_producer(
            current["source"], current.get("producer")
        ) != str(filters["source"]):
            return False
    if "missing_title" in filters:
        # Targets crawled rows whose current cache row carries no title; a
        # candidate without any cache row has not been crawled yet and never
        # matches this filter.
        if current is None or str(current["title"] or "").strip():
            return False
    if "unverified_sku" in filters:
        if current is None or str(current["sku_status"] or "") != SKU_STATUS_URL_PARSED_UNVERIFIED:
            return False
    if "unresolved_sku" in filters:
        if current is None or str(current["sku_status"] or "") != SKU_STATUS_UNRESOLVED:
            return False
    if "failure" in filters:
        failed = status in _FAILURE_CANDIDATE_STATUSES
        failed = failed or staples_job.get("state") == STAPLES_JOB_STATE_FAILED
        failed = failed or remote_job.get("state") == "FAILED"
        if not failed:
            return False
    if "cache_miss" in filters and status == CACHE_STATUS_CACHE_HIT:
        return False
    return True


def _build_link_row(
    candidate: Mapping[str, Any],
    document_ids: list[str],
    current: Mapping[str, Any] | None,
    staples_job: Mapping[str, Any],
    remote_job: Mapping[str, Any],
) -> dict[str, Any]:
    if current is not None:
        source_class = classify_crawl_producer(current["source"], current.get("producer"))
        producer = current.get("producer") or None
        sku_status = str(current["sku_status"] or "")
    else:
        source_class = None
        producer = None
        sku_status = None
    return {
        "candidate_id": int(candidate["id"]),
        "document_ids": list(document_ids),
        "document_count": len(document_ids),
        "mall": str(candidate["mall"] or ""),
        "original_url": str(candidate["original_url"] or ""),
        "cache_status": str(candidate["cache_status"]),
        "cache_status_label": cache_status_label(candidate["cache_status"]),
        "match_level": candidate["match_level"],
        "source_class": source_class,
        "producer": producer,
        "title": current["title"] if current is not None else None,
        "price": current["price"] if current is not None else None,
        "crawl_id": current["crawl_id"] if current is not None else None,
        "real_sku_id": current["real_sku_id"] if current is not None else None,
        "sku_status": sku_status,
        "sku_status_label": sku_status_label(sku_status) if sku_status else None,
        # No cache row yet = nothing to review (the row itself is pending).
        "sku_requires_review": sku_requires_review(sku_status) if sku_status else False,
        "last_success_at": current["succeeded_at"] if current is not None else None,
        "remote_job_state": remote_job.get("state"),
        "staples_job_state": staples_job.get("state"),
    }


def list_prewarm_links(
    business_date: str,
    *,
    page: int = 1,
    page_size: int = _PAGE_SIZE_DEFAULT,
    filters: Mapping[str, Any] | None = None,
    runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    """One server-side filtered page of prewarm link rows (whitelist fields only).

    Rows are scoped to the viewed day's observed candidates.  ``page_size``
    is hard-clamped to 200 here (the API layer additionally validates 1..200
    and answers 400 otherwise); ``page`` below 1 degrades to 1.
    """
    try:
        page_value = int(page)
    except (TypeError, ValueError):
        page_value = 1
    page_value = max(1, page_value)
    try:
        size_value = int(page_size)
    except (TypeError, ValueError):
        size_value = _PAGE_SIZE_DEFAULT
    size_value = min(max(size_value, 1), _PAGE_SIZE_MAX)

    paths = prewarm_paths(business_date, runtime_root)
    view = _candidate_view(paths)
    try:
        applied = _normalize_filters(filters)
        rows: list[dict[str, Any]] = []
        for candidate in view["candidates"]:
            candidate_id = int(candidate["id"])
            current = _current_cache_row(candidate, view)
            staples_job = view["staples_jobs"].get(candidate_id) or {}
            remote_job = _remote_job_for(view, candidate)
            if not _link_row_matches(candidate, current, staples_job, remote_job, applied):
                continue
            rows.append(
                _build_link_row(
                    candidate,
                    view["documents"].get(candidate_id) or [],
                    current,
                    staples_job,
                    remote_job,
                )
            )
        total = len(rows)
        total_pages = (total + size_value - 1) // size_value if total else 0
        start = (page_value - 1) * size_value
        degraded, reasons = _degraded(view)
        return {
            "business_date": business_date,
            "generated_at": _generated_at(),
            "links": rows[start : start + size_value],
            "pagination": {
                "page": page_value,
                "page_size": size_value,
                "total": total,
                "total_pages": total_pages,
            },
            "filters_applied": applied,
            "available": dict(view["available"]),
            "degraded": degraded,
            "degraded_reasons": reasons,
        }
    finally:
        _close_all(view)


# ---- link detail --------------------------------------------------------------------


def get_prewarm_link_detail(
    business_date: str, candidate_id: int, *, runtime_root: str | Path | None = None
) -> dict[str, Any] | None:
    """Full whitelist projection of one candidate, or ``None`` if it cannot be proven.

    The candidate must belong to the viewed day's observed set — an
    out-of-scope (or unknown) id answers ``None`` (HTTP-404 at the API layer).
    Includes the current cache entry (URL chain, SKU evidence), the
    append-only history (oldest → newest, exactly one ``is_current``), and
    the remote / staples sidecar job blocks.  ``payload_json`` and remote
    input/output file paths are never projected.
    """
    paths = prewarm_paths(business_date, runtime_root)
    view = _candidate_view(paths)
    try:
        wanted = int(candidate_id)
        candidate = next(
            (item for item in view["candidates"] if int(item["id"]) == wanted), None
        )
        if candidate is None:
            return None
        current = _current_cache_row(candidate, view)
        staples_job = view["staples_jobs"].get(wanted) or {}
        remote_job = _remote_job_for(view, candidate)

        history: list[dict[str, Any]] = []
        cache = view["connections"].get("shared_cache")
        identity_id = candidate.get("resolved_identity_id")
        if identity_id is None and current is not None:
            identity_id = current.get("identity_id")
        if cache is not None and identity_id is not None:
            history = _scan(
                cache, "shared_cache", view["available"], view["reasons"],
                lambda connection: _load_history_by_identity(connection, int(identity_id)),
            ) or []

        current_payload: dict[str, Any] | None = None
        if current is not None:
            current_payload = {
                "identity_id": int(current["identity_id"]),
                "match_level": candidate["match_level"],
                "mall": str(current["mall"] or ""),
                "original_url": current["original_url"],
                "submitted_url": current["submitted_url"],
                "returned_url": current["returned_url"],
                "canonical_url": current["canonical_url"],
                "crawl_id": current["crawl_id"],
                "real_sku_id": current["real_sku_id"],
                "sku_status": current["sku_status"],
                "sku_status_label": sku_status_label(current["sku_status"]),
                "sku_requires_review": sku_requires_review(current["sku_status"]),
                "title": current["title"],
                "price": current["price"],
                "comment_count": current["comment_count"],
                "source": current["source"],
                "producer": current.get("producer") or None,
                "source_class": classify_crawl_producer(
                    current["source"], current.get("producer")
                ),
                "succeeded_at": current["succeeded_at"],
            }

        # Server-authoritative route hint for the drawer's recrawl panel: the
        # SAME classifier the manual-recrawl runner guards with.  The mappings
        # loader is imported lazily inside this function (missing-config
        # robustness) and any failure degrades the route to None — the UI then
        # falls back to its host heuristic.
        route: str | None = None
        try:
            from scripts.zhaoshang_mall_mapping import load_zhaoshang_mall_mappings

            routing = classify_reference_url(
                str(candidate["original_url"] or ""),
                list(load_zhaoshang_mall_mappings()),
            )
            if routing.outcome == OUTCOME_STAPLES:
                route = "staples-local"
            elif routing.outcome == OUTCOME_ZHAOSHANG:
                route = "zhaoshang"
        except Exception:  # noqa: BLE001 - routing evidence is best-effort only
            route = None

        degraded, reasons = _degraded(view)
        return {
            "business_date": business_date,
            "generated_at": _generated_at(),
            "candidate_id": wanted,
            "mall": str(candidate["mall"] or ""),
            "original_url": str(candidate["original_url"] or ""),
            "routing_url": candidate.get("routing_url"),
            "route": route,
            "cache_status": str(candidate["cache_status"]),
            "cache_status_label": cache_status_label(candidate["cache_status"]),
            "match_level": candidate["match_level"],
            "resolved_identity_id": candidate["resolved_identity_id"],
            "document_ids": list(view["documents"].get(wanted) or []),
            "current": current_payload,
            "history": history,
            "remote_job": {
                "state": remote_job.get("state"),
                "task_name": remote_job.get("task_name"),
                "remote_identity": remote_job.get("remote_identity"),
                "updated_at": remote_job.get("updated_at"),
            }
            if remote_job
            else None,
            "staples_job": {
                "state": staples_job.get("state"),
                "attempt_count": staples_job.get("attempt_count"),
                "updated_at": staples_job.get("updated_at"),
            }
            if staples_job
            else None,
            "available": dict(view["available"]),
            "degraded": degraded,
            "degraded_reasons": reasons,
        }
    finally:
        _close_all(view)
