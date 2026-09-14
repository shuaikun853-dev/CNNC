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
is always returned (HTTP-200 degraded shape at the API layer).

``payload_json`` and every other opaque blob column are NEVER projected —
each row leaves this module through an explicit field whitelist.

KPI units (single authority for the UI):

- ``reference_link_count`` / ``unique_url_count``: ``prewarm_links`` rows /
  distinct ``original_url`` among them (two documents sharing one URL count
  twice / once).
- ``cache_hit_candidate_count``: candidates with ``cache_status='CACHE_HIT'``.
- ``historical_cache_hit_count``: CACHE_HIT candidates whose CURRENT cache
  row ``succeeded_at`` is strictly before the business day's 00:00
  Asia/Shanghai (i.e. the hit predates the day being viewed).
- ``daily_internet_hit_count`` / ``prewarm_new_success_count`` /
  ``manual_recrawl_hit_count``: CACHE_HIT candidates by the CURRENT cache
  row's source class (see :func:`classify_crawl_source`).
- ``zhaoshang_running_candidate_count`` / ``..._job_count``: REMOTE_*
  candidates whose bound ``remote_jobs.state`` is in the running set
  {SUBMIT_INTENT, PRIORITY_PENDING, SUBMITTED, RUNNING, SUBMIT_UNKNOWN};
  the job count deduplicates by ``remote_job_id``.
- ``staples_running_count``: candidates that are STAPLES_PENDING OR own a
  staples sidecar job in {PENDING, RETRY_WAIT}, counted once each (union,
  no overlap).
- ``failure_count``: bucket sum of ROUTING_BLOCKED + MANUAL_SKIP candidates
  + staples jobs FAILED + REMOTE_* candidates whose job state is FAILED
  (per the design formula; a candidate can contribute to two buckets).
- ``remote_job_count``: distinct remote jobs bound to REMOTE_* candidates.
- ``remote_url_count``: candidate rows bound to non-terminal remote jobs
  (terminal = {COMPLETED, FAILED}; unknown state never counts).

Candidate current cache row resolution: ``resolved_identity_id`` → the
``crawl_results`` row with ``identity_id=? AND is_current=1``; candidates
without a resolved identity fall back to the newest current row with the
same ``original_url`` (mall spelling is not required to match — the cache
row may have been recorded under the routed mall key).
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
from app.services.incremental_audit_runtime import DEFAULT_RUNTIME_ROOT
from app.services.staples_prewarm import (
    STAPLES_JOB_STATE_FAILED,
    STAPLES_JOB_STATE_PENDING,
    STAPLES_JOB_STATE_RETRY_WAIT,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")

_PAGE_SIZE_DEFAULT = 50
_PAGE_SIZE_MAX = 200

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

# Single source-class mapping (Issue #13): the UI consumes only these classes.
_SOURCE_CLASS_BY_SOURCE = {
    "daily-internet": "daily-internet",
    "staples-local": "staples-local-prewarm",
    "manual-recrawl-staples": "manual-recrawl",
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


def classify_crawl_source(source: Any) -> str:
    """Map one cache-row ``source`` to its UI source class; else ``"other"``."""
    return _SOURCE_CLASS_BY_SOURCE.get(str(source or "").strip(), "other")


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


def _close_all(connections: Mapping[str, sqlite3.Connection]) -> None:
    for connection in connections.values():
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


def _load_candidates(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT id, mall, original_url, cache_status, match_level,
                  resolved_identity_id, remote_job_id
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


def _load_current_cache_rows(
    connection: sqlite3.Connection,
) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    columns = ", ".join(_CACHE_RESULT_COLUMNS)
    rows = connection.execute(
        f"SELECT {columns} FROM crawl_results WHERE is_current=1 ORDER BY id"
    ).fetchall()
    by_identity: dict[int, dict[str, Any]] = {}
    by_url: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        # Later id wins, mirroring the cache's newest-current election.
        by_identity[int(item["identity_id"])] = item
        by_url[str(item["original_url"])] = item
    return by_identity, by_url


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
    rows = connection.execute(
        """SELECT id AS result_id, source, title, price, real_sku_id, sku_status,
                  succeeded_at, is_current
           FROM crawl_results WHERE identity_id=? ORDER BY succeeded_at, id""",
        (int(identity_id),),
    ).fetchall()
    return [
        {
            "result_id": int(row["result_id"]),
            "source_class": classify_crawl_source(row["source"]),
            "title": row["title"],
            "price": row["price"],
            "real_sku_id": row["real_sku_id"],
            "sku_status": row["sku_status"],
            "succeeded_at": row["succeeded_at"],
            "is_current": bool(row["is_current"]),
        }
        for row in rows
    ]


def _candidate_view(paths: SimpleNamespace) -> dict[str, Any]:
    """Open all three stores read-only and load the raw rows (best-effort each)."""
    connections, available, reasons = _open_stores(paths)
    view: dict[str, Any] = {
        "connections": connections,
        "available": available,
        "reasons": reasons,
        "candidates": [],
        "documents": {},
        "staples_jobs": {},
        "cache_by_identity": {},
        "cache_by_url": {},
        "remote_jobs": {},
    }
    prewarm = connections.get("prewarm_store")
    if prewarm is not None:
        candidates = _scan(prewarm, "prewarm_store", available, reasons, _load_candidates)
        view["candidates"] = candidates or []
        documents = _scan(prewarm, "prewarm_store", available, reasons, _load_document_ids)
        view["documents"] = documents or {}
        view["staples_jobs"] = _load_staples_jobs_best_effort(prewarm, reasons)
    cache = connections.get("shared_cache")
    if cache is not None:
        loaded = _scan(cache, "shared_cache", available, reasons, _load_current_cache_rows)
        if loaded is not None:
            view["cache_by_identity"], view["cache_by_url"] = loaded
    state = connections.get("state_db")
    if state is not None:
        job_ids = [
            int(candidate["remote_job_id"])
            for candidate in view["candidates"]
            if candidate.get("remote_job_id") is not None
        ]
        loaded = _scan(
            state, "state_db", available, reasons,
            lambda connection: _load_remote_jobs(connection, job_ids),
        )
        view["remote_jobs"] = loaded or {}
    return view


def _current_cache_row(candidate: Mapping[str, Any], view: Mapping[str, Any]) -> dict[str, Any] | None:
    """resolved_identity_id → current row; else newest current row by original_url."""
    identity_id = candidate.get("resolved_identity_id")
    if identity_id is not None:
        row = view["cache_by_identity"].get(int(identity_id))
        if row is not None:
            return row
    return view["cache_by_url"].get(str(candidate.get("original_url") or ""))


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
    ``degraded_reasons`` (never an exception).
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
        kpi["status_distribution"] = {}

        candidates: list[dict[str, Any]] = view["candidates"]
        kpi["reference_link_count"] = sum(
            len(document_ids) for document_ids in view["documents"].values()
        )
        kpi["unique_url_count"] = len({str(c["original_url"]) for c in candidates})
        kpi["candidate_count"] = len(candidates)
        cutoff = _business_day_cutoff_utc(business_date)

        status_counts: dict[str, int] = {}
        running_job_ids: set[int] = set()
        bound_job_ids: set[int] = set()
        for candidate in candidates:
            status = str(candidate["cache_status"])
            status_counts[status] = status_counts.get(status, 0) + 1
            candidate_id = int(candidate["id"])
            current = _current_cache_row(candidate, view)

            if status == CACHE_STATUS_CACHE_HIT:
                kpi["cache_hit_candidate_count"] += 1
                if current is not None:
                    source_class = classify_crawl_source(current["source"])
                    if source_class == "daily-internet":
                        kpi["daily_internet_hit_count"] += 1
                    elif source_class == "staples-local-prewarm":
                        kpi["prewarm_new_success_count"] += 1
                    elif source_class == "manual-recrawl":
                        kpi["manual_recrawl_hit_count"] += 1
                    sku_status = str(current["sku_status"] or "")
                    if sku_status == SKU_STATUS_URL_PARSED_UNVERIFIED:
                        kpi["url_parsed_unverified_count"] += 1
                    elif sku_status == SKU_STATUS_UNRESOLVED:
                        kpi["unresolved_sku_count"] += 1
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
                job_id = candidate.get("remote_job_id")
                if job_id is not None:
                    bound_job_ids.add(int(job_id))
                    job = view["remote_jobs"].get(int(job_id)) or {}
                    state = job.get("state")
                    if state in _REMOTE_RUNNING_JOB_STATES:
                        kpi["zhaoshang_running_candidate_count"] += 1
                        running_job_ids.add(int(job_id))
                    if state in _REMOTE_TERMINAL_JOB_STATES:
                        if state == "FAILED":
                            kpi["failure_count"] += 1
                    else:
                        # Non-terminal job: its candidate URLs are still in flight.
                        kpi["remote_url_count"] += 1

        kpi["status_distribution"] = dict(sorted(status_counts.items()))
        kpi["zhaoshang_running_job_count"] = len(running_job_ids)
        kpi["remote_job_count"] = len(bound_job_ids)

        degraded, reasons = _degraded(view)
        return {
            "business_date": business_date,
            "generated_at": _generated_at(),
            "available": dict(view["available"]),
            "degraded": degraded,
            "degraded_reasons": reasons,
            "kpi": kpi,
        }
    finally:
        _close_all(view["connections"])


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
        if current is None or classify_crawl_source(current["source"]) != str(filters["source"]):
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
    source_class = classify_crawl_source(current["source"]) if current is not None else None
    sku_status = str(current["sku_status"] or "") if current is not None else None
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

    ``page_size`` is hard-clamped to 200 here (the API layer additionally
    validates 1..200 and answers 400 otherwise); ``page`` below 1 degrades to 1.
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
            remote_job = (
                view["remote_jobs"].get(int(candidate["remote_job_id"]))
                if candidate.get("remote_job_id") is not None
                else {}
            ) or {}
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
        _close_all(view["connections"])


# ---- link detail --------------------------------------------------------------------


def get_prewarm_link_detail(
    business_date: str, candidate_id: int, *, runtime_root: str | Path | None = None
) -> dict[str, Any] | None:
    """Full whitelist projection of one candidate, or ``None`` if it cannot be proven.

    Includes the current cache entry (URL chain, SKU evidence), the append-only
    history (oldest → newest, exactly one ``is_current``), and the remote /
    staples sidecar job blocks.  ``payload_json`` and remote input/output file
    paths are never projected.
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
        remote_job = (
            view["remote_jobs"].get(int(candidate["remote_job_id"]))
            if candidate.get("remote_job_id") is not None
            else {}
        ) or {}

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
                "source_class": classify_crawl_source(current["source"]),
                "succeeded_at": current["succeeded_at"],
            }

        degraded, reasons = _degraded(view)
        return {
            "business_date": business_date,
            "generated_at": _generated_at(),
            "candidate_id": wanted,
            "mall": str(candidate["mall"] or ""),
            "original_url": str(candidate["original_url"] or ""),
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
        _close_all(view["connections"])
