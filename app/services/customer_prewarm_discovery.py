"""Customer prewarm discovery: routing, detail scanning, and daily sync (Issue #9 Phase 2).

Phase 2 wires the Phase-1 durable discovery state
(:mod:`app.services.customer_prewarm_store`) to the cross-day Shared Crawl
Cache (:mod:`app.services.shared_crawl_cache`, Issue #7):

- :func:`route_url` maps one customer reference URL to a mall key using the
  in-repo zhaoshang mapping list, canonicalizing every exact ``jslink.com``
  host to ``b2b.nbdeli.com`` as a *lookup* transform:
  the original URL is never mutated, it is stored verbatim as
  ``original_url`` while ``routing_url``/``lookup_url`` carry the rewrite.
- :func:`process_document` reads one document's detail (read-only, injected
  fetcher), extracts reference links, routes them, and records CACHE_HIT or
  PENDING_PREWARM per link.  Nothing here creates crawls; misses stop at
  ``PENDING_PREWARM`` (the stable input of the follow-up prewarm phases).
- :func:`refresh_pending_candidates` re-checks pending candidates so newly
  synced daily results are absorbed the same day.
- :func:`sync_daily_internet_results` mirrors successful daily crawl rows
  (``crawl_work.state='SUCCESS'`` AND ``result_json.page_price_status=='OK'``,
  the authoritative per-row semantics of
  ``read_zhaoshang_result_xlsx_by_identity``) into the shared cache through an
  idempotent ledger with crash recovery.  When the validated export carries
  ``page_sku_id`` from ``商品最小品类单元id``, that independent page/crawler
  evidence is stored as ``CONFIRMED``.  Only when page SKU evidence is absent
  may a non-temporary request-side ``sku_id`` fall back to
  ``UPSTREAM_PROVIDED``; temporary codes (``zhpq-*``, STEP3 ``ROW<n>``,
  or crawl/task-id echoes) remain ``UNRESOLVED``.  The request-side value is
  always preserved as ``payload["request_sku_id"]``.  Title, price and
  comment evidence from the same validated export are mirrored at the same
  time.
- :func:`upgrade_upstream_sku_evidence` deterministically converts R3-era
  rows (``UNRESOLVED`` with a usable ``payload["request_sku_id"]``) into
  appended ``UPSTREAM_PROVIDED`` rows - append-only history preserved,
  current moved to the upgraded row, no re-crawl, idempotent via
  ``sku-upgrade:*`` ledger keys.
- :func:`recover_customer_documents` re-reads the customer side only for the
  scanner so documents the event sidecar never delivered still reach the
  store, throttled via the ``prewarm_recovery_runs`` table (Round 3 Finding
  A; run by :func:`run_prewarm_scan` as Phase B2).
- :func:`backfill_daily_history` reuses the same sync across past per-day
  state DBs to backfill daily-internet history (idempotent, resumable,
  read-only on every historical state DB).
- :func:`snapshot_cache_readonly` copies a shared cache file into a sandbox
  with SQLite ``mode=ro`` + the backup API (dry-run support; the source is
  never opened read-write).
- :func:`run_prewarm_scan` orchestrates phases A-D with a time budget and
  per-phase error isolation.

Every database failure is contained: a broken row, link, candidate, or whole
phase is counted and the scan continues.  Library code never prints.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from app.services.crawl_url_identity import (
    SKU_STATUS_CONFIRMED,
    SKU_STATUS_UNRESOLVED,
    SKU_STATUS_UPSTREAM_PROVIDED,
    is_placeholder_code,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    RECOVERY_FAILED_DOCUMENTS_SEEN,
    CustomerPrewarmStore,
)
from app.services.customer_reference_links import extract_reference_links
from app.services.daily_audit_store import LeaseUnavailable, ProcessLock
from app.services.saas_audit_core import extract_customer_price_from_detail
from app.services.shared_crawl_cache import (
    PRODUCER_DAILY_INTERNET,
    AliasConflictError,
    SharedCrawlCache,
)
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping
from app.services.zhaoshang_url_identity import canonicalize_new_zhaoshang_url

DETAIL_BUDGET_RESERVE_SECONDS = 5.0


@dataclass(frozen=True)
class RoutedUrl:
    """Routing outcome of one customer reference URL.

    ``mall`` is the mapped mall key ('' when unmapped); ``routing_url`` is the
    rewritten URL (``None`` when no rewrite applied); ``lookup_url`` is what
    the Shared Crawl Cache should be queried with.
    """

    mall: str
    lookup_url: str
    routing_url: str | None


def route_url(original_url: str, mappings: Sequence[ZhaoshangMallMapping]) -> RoutedUrl:
    """Route one customer reference URL to (mall, lookup_url, routing_url).

    The deterministic production canonicalization is applied first: an exact
    ``jslink.com`` host or any of its subdomains becomes ``b2b.nbdeli.com``.
    The original URL is NEVER mutated - it stays verbatim as ``original_url``;
    only the routing/lookup pair carries the rewrite.
    Unparseable/empty input never raises and routes to mall ``''``.
    """
    original = str(original_url or "").strip()
    routed = canonicalize_new_zhaoshang_url(original)
    host = ""
    try:
        host = (urlparse(routed).hostname or "").lower().strip(".")
    except ValueError:  # malformed URL (e.g. bad IPv6 literal) - conservative unmapped
        host = ""
    mall = ""
    if host:
        for mapping in mappings:
            for domain in mapping.domains:
                normalized = str(domain or "").strip().lower().strip(".")
                if not normalized:
                    continue
                if host == normalized or host.endswith("." + normalized):
                    mall = mapping.mall_key
                    break
            if mall:
                break
    return RoutedUrl(
        mall=mall,
        lookup_url=routed,
        routing_url=routed if routed != original else None,
    )


# ---- Phase B seam: intake -----------------------------------------------------


def intake_documents(
    store: CustomerPrewarmStore,
    business_date: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str = "event",
) -> int:
    """Upsert joined saas rows into the prewarm store; return distinct ids kept.

    Rows without a non-empty ``documentId`` are skipped.  Repeated ids inside
    one batch are deduplicated by counting distinct upserted ids.
    """
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        document_id = str(row.get("documentId") or "").strip()
        if not document_id:
            continue
        category = str(row.get("fourCategoryId") or "").strip()
        store.upsert_document(business_date, document_id, category, source=source)
        store.record_daily_document_evidence(
            business_date,
            document_id,
            four_category_id=category,
            four_category_name=row.get("fourCategoryName"),
            customer_title=row.get("commodityName"),
            source=source,
        )
        seen.add(document_id)
    return len(seen)


def prewarm_rows_from_customer_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Shape RAW customer query rows into the prewarm intake row shape.

    Takes the unfiltered customer-side rows (dicts carrying documentId /
    fourCategoryId / fourCategoryName / commodityName / referenceLink) and
    outputs the same 5-field shape as ``snapshot_discoverer``'s
    ``prewarm_rows``.  Duplicate documentIds are deduplicated FIRST-WINS (the
    scanner must never crash on odd pagination) and rows without a non-empty
    ``documentId`` are skipped; this helper never raises on row content.
    """
    shaped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        document_id = str(raw.get("documentId") or "").strip()
        if not document_id or document_id in seen:
            continue
        seen.add(document_id)
        shaped.append(
            {
                "documentId": document_id,
                "fourCategoryId": str(raw.get("fourCategoryId") or ""),
                "fourCategoryName": str(raw.get("fourCategoryName") or ""),
                "commodityName": str(raw.get("commodityName") or ""),
                "referenceLink": str(raw.get("referenceLink") or ""),
            }
        )
    return shaped


# ---- Phase B2: customer-side-only scanner recovery (Round 3 Finding A) -------------


def recover_customer_documents(
    prewarm_store: CustomerPrewarmStore,
    *,
    customer_fetch: Callable[[], Sequence[Mapping[str, Any]]],
    business_date: str,
    interval_seconds: float = 300.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Customer-side-only recovery intake for the scanner (Round 3 Finding A).

    When the STEP1 event sidecar intake failed for a whole day, the per-minute
    scanner would otherwise never see customer-only documents (they exist only
    in the authoritative customer query, not in the per-day state DB).  This
    recovery reads the customer side ONLY (no SaaS query) and upserts the
    rows as ``source="scanner-recovery"``.

    Throttled by the latest ``prewarm_recovery_runs`` outcome: when a
    previous run exists and ``(now - last_ran_at) < interval_seconds``, the
    function returns ``{"throttled": True, "documents_intaked": 0}`` without
    calling ``customer_fetch``.  ``interval_seconds=0`` disables throttling
    (run every pass).  ``customer_fetch`` exceptions still propagate to the
    phase guard, but the failed attempt is recorded with documents_seen=-1 so
    the same throttle interval applies before the next retry.
    """
    now_value = now if now is not None else datetime.now(UTC)
    last_ran_at = prewarm_store.last_recovery_run_at()
    if last_ran_at is not None and interval_seconds > 0:
        elapsed_seconds = (now_value - datetime.fromisoformat(last_ran_at)).total_seconds()
        if elapsed_seconds < interval_seconds:
            return {"throttled": True, "documents_intaked": 0}
    try:
        rows = customer_fetch()
    except Exception as exc:
        # A failed recovery still consumed the expensive customer/auth attempt.
        # Record it for the same throttle window so a one-minute scheduler does
        # not relaunch browser harvest every minute.  The named sentinel means
        # "attempted but customer rows were not obtained"; no customer
        # document is marked as seen or ingested.
        try:
            prewarm_store.record_recovery_run(
                business_date, RECOVERY_FAILED_DOCUMENTS_SEEN
            )
        except Exception as record_exc:  # preserve the authoritative fetch error
            exc.add_note(
                f"failed to persist recovery throttle marker: {type(record_exc).__name__}"
            )
        raise
    documents_seen = len(rows)
    intaked = intake_documents(
        prewarm_store,
        business_date,
        prewarm_rows_from_customer_rows(rows),
        source="scanner-recovery",
    )
    prewarm_store.record_recovery_run(business_date, documents_seen)
    return {"throttled": False, "documents_intaked": intaked, "documents_seen": documents_seen}


# ---- Phase C: detail scan -------------------------------------------------------


def process_document(
    store: CustomerPrewarmStore,
    shared_cache: SharedCrawlCache,
    document_row: Mapping[str, Any],
    *,
    mappings: Sequence[ZhaoshangMallMapping],
    detail_fetch: Callable[[str, str], Mapping[str, Any]],
    source: str = "detail",
    business_date: str | None = None,
) -> dict[str, Any]:
    """Discover reference links of one document and resolve them against the cache.

    ``detail_fetch`` receives ``(document_id, four_category_id)`` and returns
    ``{"ok": bool, "payload": mapping | None, "error": str | None}``; its
    exceptions are caught and counted as detail failures.  Per-link failures
    are isolated (collected, scan continues).  A detail failure never raises
    and never becomes terminal: the document stays PENDING and is deferred by
    the store's exponential backoff, so the next pass retries it.
    A document whose links hit per-link errors (e.g. sqlite busy) also stays
    retryable: it is recorded as a deferred failure instead of DONE, so the
    next pass re-processes it and the missing links are not lost.

    ``business_date`` (Issue #13 Round 3) is the SCAN PASS's business date:
    when provided it dates both the candidate first-seen of newly discovered
    links and the daily observation of every link processed here; when
    ``None`` the document's immutable first-seen ``business_date`` is used
    (legacy callers).
    """
    document_id = str(document_row["document_id"])
    four_category_id = str(document_row.get("four_category_id") or "")
    link_business_date = (
        str(business_date)
        if business_date is not None
        else str(document_row["business_date"])
    )
    try:
        response = detail_fetch(document_id, four_category_id)
    except Exception as exc:  # noqa: BLE001 - detail failure must never raise
        error = f"detail fetch exception: {exc}"
        store.record_document_detail_failure(document_id, error)
        return {"document_id": document_id, "detail_ok": False, "error": error}
    if not isinstance(response, Mapping) or not response.get("ok"):
        raw_error = response.get("error") if isinstance(response, Mapping) else None
        error = str(raw_error) if raw_error else "detail fetch reported failure"
        store.record_document_detail_failure(document_id, error)
        return {"document_id": document_id, "detail_ok": False, "error": error}

    detail_payload = response.get("payload")
    customer_price = extract_customer_price_from_detail(detail_payload)
    store.record_daily_document_evidence(
        link_business_date,
        document_id,
        four_category_id=four_category_id,
        customer_price=customer_price.get("customerPrice"),
        customer_price_status=customer_price.get("customerPriceStatus"),
        customer_price_source=customer_price.get("customerPriceSource"),
        customer_price_warning=customer_price.get("customerPriceWarning"),
        customer_price_evidence=customer_price.get("customerPriceEvidence"),
        source=source,
    )
    links = extract_reference_links(detail_payload)
    hits = 0
    misses = 0
    errors: list[str] = []
    warnings: list[str] = []
    try:
        for link in links:
            try:
                routed = route_url(link, mappings)
                upsert = store.upsert_link(
                    document_id,
                    link,
                    mall=routed.mall,
                    lookup_url=routed.lookup_url,
                    routing_url=routed.routing_url,
                    business_date=link_business_date,
                    source=source,
                )
                candidate_id = int(upsert["candidate"]["id"])
                if routed.mall == "":
                    try:
                        host = (urlparse(routed.lookup_url).hostname or "").lower().strip(".")
                    except ValueError:
                        host = ""
                    store.record_candidate_result(
                        candidate_id,
                        cache_status=CACHE_STATUS_PENDING_PREWARM,
                        error=f"unmapped mall: {host or 'unknown-host'}",
                    )
                    misses += 1
                    continue
                try:
                    hit = shared_cache.lookup_current(routed.mall, routed.lookup_url)
                except Exception as exc:  # noqa: BLE001 - retryable: stay PENDING_PREWARM
                    store.record_candidate_result(
                        candidate_id,
                        cache_status=CACHE_STATUS_PENDING_PREWARM,
                        error=f"lookup error: {exc}",
                    )
                    misses += 1
                    continue
                if hit is not None:
                    store.record_candidate_result(
                        candidate_id,
                        cache_status=CACHE_STATUS_CACHE_HIT,
                        match_level=hit.get("match_level"),
                        resolved_identity_id=int(hit["identity_id"]),
                    )
                    hits += 1
                    try:
                        # None (unresolved) is tolerated; only hard failures warn.
                        shared_cache.attach_document_reference(
                            document_id,
                            mall=routed.mall,
                            url=routed.lookup_url,
                            source=source,
                        )
                    except Exception as exc:  # noqa: BLE001 - best-effort provenance
                        warnings.append(f"attach failed for {link}: {exc}")
                else:
                    store.record_candidate_result(
                        candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM
                    )
                    misses += 1
            except Exception as exc:  # noqa: BLE001 - per-link isolation (Case 8)
                errors.append(f"link {link}: {exc}")
                continue
    finally:
        if errors:
            # Link-level errors must not mark the document DONE: DONE would hide
            # the not-yet-upserted links forever (they are only written by the
            # next detail pass). Record a deferred failure so the scan retries.
            store.record_document_detail_failure(
                document_id,
                "link processing errors: " + "; ".join(errors[:3]),
            )
        else:
            store.record_document_detail_success(document_id)
    return {
        "document_id": document_id,
        "detail_ok": True,
        "retried": bool(errors),
        "links": len(links),
        "hits": hits,
        "misses": misses,
        "errors": errors,
        "warnings": warnings,
    }


# ---- Phase D: candidate refresh ---------------------------------------------------


def refresh_pending_candidates(
    store: CustomerPrewarmStore,
    shared_cache: SharedCrawlCache,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    """Re-check PENDING_PREWARM candidates so newly synced rows are absorbed."""
    checked = 0
    hits = 0
    errors: list[str] = []
    for candidate in store.candidates(CACHE_STATUS_PENDING_PREWARM, limit=limit):
        mall = str(candidate.get("mall") or "").strip()
        if not mall:
            continue
        candidate_id = int(candidate["id"])
        try:
            hit = shared_cache.lookup_current(mall, str(candidate["lookup_url"]))
        except Exception as exc:  # noqa: BLE001 - per-candidate isolation
            errors.append(f"candidate {candidate_id}: {exc}")
            continue
        checked += 1
        if hit is None:
            continue
        try:
            store.record_candidate_result(
                candidate_id,
                cache_status=CACHE_STATUS_CACHE_HIT,
                match_level=hit.get("match_level"),
                resolved_identity_id=int(hit["identity_id"]),
            )
        except Exception as exc:  # noqa: BLE001 - per-candidate isolation
            errors.append(f"candidate {candidate_id}: {exc}")
            continue
        hits += 1
        for link in store.links_for_candidate(candidate_id):
            try:
                shared_cache.attach_document_reference(
                    str(link["document_id"]),
                    mall=mall,
                    url=str(candidate["lookup_url"]),
                    source=str(link.get("source") or ""),
                )
            except Exception as exc:  # noqa: BLE001 - best-effort provenance
                errors.append(
                    f"attach candidate {candidate_id} document {link['document_id']}: {exc}"
                )
    return {"checked": checked, "hits": hits, "errors": errors}


# ---- Phase A: daily internet sync ---------------------------------------------------


def _open_state_db_readonly(state_db_path: str | Path) -> sqlite3.Connection | None:
    path = Path(state_db_path)
    if not path.is_file():
        return None
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


# STEP3 的模板兜底形状:导出结果里出现 ROW<n> 说明上游没有给出真实 SKU。
_ROW_CODE_PATTERN = re.compile(r"^row\d+$", re.IGNORECASE)


def is_temporary_request_code(value: str, *, crawl_id: str | None = None) -> bool:
    """True when a request-side code is a temporary correlation code, never real SKU evidence.

    Covers #7 placeholder prefixes (zhpq-*), the STEP3 template fallback shape ROW<n>,
    and an echo of the crawl/remote task id."""
    stripped = str(value or "").strip()
    if not stripped:
        return False
    if is_placeholder_code(stripped):
        return True
    if _ROW_CODE_PATTERN.match(stripped):
        return True
    return crawl_id is not None and stripped == str(crawl_id).strip() and bool(str(crawl_id).strip())


def sync_daily_internet_results(
    prewarm_store: CustomerPrewarmStore,
    shared_cache: SharedCrawlCache,
    *,
    state_db_path: str | Path,
    business_date: str,
    source: str = "daily-internet",
    remote_job_id: int | None = None,
    stale_claim_seconds: int = 600,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Serialize every scanner/backfill/harvest mirror against one ledger."""
    if not Path(state_db_path).is_file():
        return _sync_daily_internet_results_unlocked(
            prewarm_store, shared_cache, state_db_path=state_db_path,
            business_date=business_date, source=source,
            remote_job_id=remote_job_id, stale_claim_seconds=stale_claim_seconds, now=now,
        )
    lock = ProcessLock(prewarm_store.path.with_name(f"{prewarm_store.path.name}.sync.lock"))
    try:
        lock.acquire()
    except LeaseUnavailable:
        return {
            "state_db": "deferred", "scanned": 0, "skipped_non_ok": 0,
            "skipped_invalid": 0, "already_synced": 0, "errored_before": 0,
            "ok_synced": 0, "transient_failures": 0, "deterministic_failures": 0,
            "recovered_claims": 0, "dropped_claims": 0,
            "errors": ["shared cache sync lock busy; retry next pass"],
        }
    try:
        return _sync_daily_internet_results_unlocked(
            prewarm_store, shared_cache, state_db_path=state_db_path,
            business_date=business_date, source=source,
            remote_job_id=remote_job_id, stale_claim_seconds=stale_claim_seconds, now=now,
        )
    finally:
        lock.release()


def _sync_daily_internet_results_unlocked(
    prewarm_store: CustomerPrewarmStore,
    shared_cache: SharedCrawlCache,
    *,
    state_db_path: str | Path,
    business_date: str,
    source: str = "daily-internet",
    remote_job_id: int | None = None,
    stale_claim_seconds: int = 600,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mirror successful daily crawl rows into the shared cache, idempotently.

    The function always records into whatever cache the caller passed in;
    safety comes from the caller composing sandbox paths (dry-run snapshots a
    read-only copy of the production cache and writes into the sandbox).

    Row-level success is ``crawl_work.state='SUCCESS'`` AND
    ``result_json.page_price_status=='OK'``.  Each row is keyed by a producer
    event key built from stable production identifiers (business date, remote
    task name, crawl_work id, content hash) and guarded by the prewarm sync
    ledger.  ``source_event_key`` is embedded verbatim in the cache payload so
    a crash between ``record_success`` and ``ledger_complete`` can be probed
    cross-DB via :meth:`SharedCrawlCache.find_id_by_source_event_key`.

    Deterministic failures (ValueError/AliasConflictError) are ledgered and
    never retried; transient failures (sqlite busy/locked) drop the claim so
    the next scan retries.  A missing state db returns ``state_db='missing'``
    without raising.

    SKU evidence note: the request-side ``crawl_work.sku_id`` remains
    upstream evidence only.  A validated export may additionally carry
    ``page_sku_id`` from ``商品最小品类单元id``; when present and non-temporary,
    that page/crawler evidence is stronger and is stored as ``CONFIRMED``.
    Without page evidence, a usable request SKU remains
    ``UPSTREAM_PROVIDED``; placeholders / ROW fallbacks / crawl-id echoes
    remain ``UNRESOLVED``.  The request value is preserved in
    ``payload["request_sku_id"]``, while validated title and comment evidence
    are mirrored alongside price.
    """
    now_value = now if now is not None else datetime.now(UTC)
    summary: dict[str, Any] = {
        "state_db": "ok",
        "scanned": 0,
        "skipped_non_ok": 0,
        "skipped_invalid": 0,
        "already_synced": 0,
        "errored_before": 0,
        "ok_synced": 0,
        "transient_failures": 0,
        "deterministic_failures": 0,
        "recovered_claims": 0,
        "dropped_claims": 0,
        "errors": [],
    }
    path = Path(state_db_path)
    if not path.is_file():
        summary["state_db"] = "missing"
        return summary

    # ---- crash-recovery pre-pass: link or drop stale unfinalized claims ----
    cutoff = (now_value - timedelta(seconds=stale_claim_seconds)).isoformat()
    for claim in prewarm_store.ledger_pending_claims(source, older_than=cutoff):
        key = str(claim["source_event_key"])
        try:
            existing = shared_cache.find_id_by_source_event_key(source, key)
        except Exception as exc:  # noqa: BLE001 - probe failure keeps the claim
            summary["errors"].append(f"crash probe {key}: {exc}")
            continue
        try:
            if existing is not None:
                prewarm_store.ledger_complete(source, key, cache_result_id=int(existing))
                summary["recovered_claims"] += 1
            else:
                # Scanner is single-instance: dropping lets the row re-record.
                prewarm_store.ledger_drop_claim(source, key)
                summary["dropped_claims"] += 1
        except Exception as exc:  # noqa: BLE001 - recovery failure keeps scanning
            summary["errors"].append(f"crash recovery {key}: {exc}")

    connection = _open_state_db_readonly(path)
    assert connection is not None  # guarded by the is_file() check above
    try:
        rows = connection.execute(
            """SELECT c.id AS crawl_id, c.mall_key, c.sku_id, c.exact_url, c.result_json,
                      c.updated_at, c.remote_job_id, r.task_name
               FROM crawl_work c LEFT JOIN remote_jobs r ON r.id=c.remote_job_id
               WHERE c.state='SUCCESS' AND c.result_json IS NOT NULL
                 AND (? IS NULL OR c.remote_job_id=?)
               ORDER BY c.id"""
            , (remote_job_id, remote_job_id)
        ).fetchall()
    finally:
        connection.close()

    for row in rows:
        summary["scanned"] += 1
        result_json_raw = str(row["result_json"])
        task_name = row["task_name"]
        task_label = task_name or f"crawl-{int(row['crawl_id'])}"
        event_key = (
            f"{business_date}:{task_label}:{int(row['crawl_id'])}:"
            f"{hashlib.sha256(result_json_raw.encode('utf-8')).hexdigest()[:12]}"
        )
        try:
            result = json.loads(result_json_raw)
        except (TypeError, ValueError) as exc:
            # 内容损坏是确定性失败:记账,永不重试。
            prewarm_store.ledger_claim(source, event_key)
            prewarm_store.ledger_complete(source, event_key, error=f"invalid result_json: {exc}")
            summary["deterministic_failures"] += 1
            continue
        if not isinstance(result, Mapping) or result.get("page_price_status") != "OK":
            summary["skipped_non_ok"] += 1
            continue

        mall_key = str(row["mall_key"] or "").strip()
        exact_url = str(row["exact_url"] or "").strip()
        if not mall_key or not exact_url:
            prewarm_store.ledger_claim(source, event_key)
            prewarm_store.ledger_complete(
                source,
                event_key,
                error=f"invalid crawl row: crawl_id={int(row['crawl_id'])} "
                f"mall_key={mall_key!r} exact_url={exact_url!r}",
            )
            summary["skipped_invalid"] += 1
            continue

        ledger = prewarm_store.ledger_get(source, event_key)
        if ledger is not None:
            if ledger.get("cache_result_id") is not None:
                summary["already_synced"] += 1
                continue
            if ledger.get("error") is not None:
                summary["errored_before"] += 1
                continue
        prewarm_store.ledger_claim(source, event_key)

        # succeeded_at = crawl_work.updated_at(真实成功时序);无效时间戳降级为
        # None(cache 取当前时刻)并把原文保留进 payload。
        updated_at_raw = row["updated_at"]
        succeeded_at: str | None = None
        try:
            parsed_updated_at = (
                datetime.fromisoformat(str(updated_at_raw)) if updated_at_raw else None
            )
        except ValueError:
            parsed_updated_at = None
        if (
            parsed_updated_at is not None
            and parsed_updated_at.tzinfo is not None
            and parsed_updated_at.utcoffset() is not None
        ):
            succeeded_at = str(updated_at_raw)
        payload: dict[str, Any] = {
            "source_event_key": event_key,
            "business_date": business_date,
            "crawl_work_id": int(row["crawl_id"]),
            "remote_job_id": row["remote_job_id"],
            "result": result,
        }
        if succeeded_at is None:
            payload["raw_updated_at"] = updated_at_raw

        # Prefer independent page/crawler SKU evidence from the validated
        # export.  The request-side sku_id remains provenance only and is used
        # as UPSTREAM_PROVIDED fallback when page_sku_id is absent.  Temporary
        # request codes never become real SKU evidence.  Price/title/comment
        # stay valid even when the request SKU itself is only a placeholder.
        sku_value = str(row["sku_id"] or "").strip()
        crawl_label = task_name or None
        page_sku = str(result.get("page_sku_id") or "").strip()
        if page_sku and not is_temporary_request_code(page_sku, crawl_id=crawl_label):
            real_sku_id: str | None = page_sku
            sku_status = SKU_STATUS_CONFIRMED
        elif sku_value and not is_temporary_request_code(sku_value, crawl_id=crawl_label):
            real_sku_id = sku_value
            sku_status = SKU_STATUS_UPSTREAM_PROVIDED
        else:
            real_sku_id = None
            sku_status = SKU_STATUS_UNRESOLVED
        payload["request_sku_id"] = sku_value
        # Issue #13 Round 2 provenance: a crawl row whose remote job is linked
        # in the prewarm store was produced by the customer-prewarm dispatcher;
        # everything else stays the plain daily-internet producer.
        producer = (
            prewarm_store.remote_job_producer(business_date, row["remote_job_id"])
            if row["remote_job_id"] is not None
            else None
        )
        effective_producer = producer or PRODUCER_DAILY_INTERNET
        try:
            row_out = shared_cache.record_success(
                mall=mall_key,
                submitted_url=exact_url,
                returned_url=result.get("returned_url"),
                crawl_id=crawl_label,
                real_sku_id=real_sku_id,
                sku_status=sku_status,
                title=result.get("page_title"),
                price=result.get("page_price_value"),
                comment_count=result.get("comment_count"),
                source=source,
                succeeded_at=succeeded_at,
                payload=payload,
                producer=effective_producer,
            )
        except sqlite3.OperationalError:
            # 瞬时失败(库锁/繁忙):不记 ledger,下轮重试。
            prewarm_store.ledger_drop_claim(source, event_key)
            summary["transient_failures"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 - ValueError/AliasConflict: deterministic
            prewarm_store.ledger_complete(source, event_key, error=str(exc))
            summary["deterministic_failures"] += 1
            continue
        prewarm_store.ledger_complete(source, event_key, cache_result_id=int(row_out["id"]))
        summary["ok_synced"] += 1
    return summary


# ---- R3 历史数据一次性确定性升级(免重爬) ---------------------------------------------

_UPGRADE_KEY_PREFIX = "sku-upgrade:"


def upgrade_upstream_sku_evidence(
    prewarm_store: CustomerPrewarmStore,
    shared_cache: SharedCrawlCache,
    *,
    source: str = "daily-internet",
) -> dict[str, Any]:
    """Deterministically upgrade R3-era SKU evidence, without any re-crawl.

    R3 conservatively recorded every daily-internet row as ``UNRESOLVED`` and
    preserved the upstream value as ``payload["request_sku_id"]``.  This
    one-shot seam converts those rows into appended ``UPSTREAM_PROVIDED``
    rows: append-only history is preserved, the identity's current row moves
    to the upgraded row (the upgrade reuses the original ``succeeded_at``,
    so the appended row wins current via the ``(succeeded_at, id)`` id
    tie-break), and nothing is re-crawled.

    Mechanics per completed sync-ledger entry of ``source``:

    - Read the cache row via :meth:`SharedCrawlCache.get_result`; skip
      non-``UNRESOLVED`` rows (already honest under the new encoding).
    - Skip when the payload has no usable ``request_sku_id``
      (``skipped_no_sku``) or the value is a temporary code - #7 placeholder
      prefix, the STEP3 ``ROW<n>`` template fallback shape, or a crawl-id
      echo (``skipped_temporary``).
    - Idempotency: each upgrade is keyed ``sku-upgrade:<event_key>`` in the
      sync ledger; an entry that already carries a ``cache_result_id`` is
      counted ``already_upgraded`` and skipped.  ``sku-upgrade:*`` marker
      entries themselves are never re-scanned, so re-runs stay bounded.
    - Deterministic failures (``ValueError``/``AliasConflictError``) are
      ledgered and counted ``failed``; transient sqlite contention drops the
      claim so the next run retries (``transient``).
    """
    summary: dict[str, Any] = {
        "scanned": 0,
        "upgraded": 0,
        "already_upgraded": 0,
        "skipped_no_sku": 0,
        "skipped_temporary": 0,
        "failed": 0,
        "transient": 0,
    }
    for ledger in prewarm_store.ledger_completed_entries(source):
        event_key = str(ledger["source_event_key"])
        if event_key.startswith(_UPGRADE_KEY_PREFIX):
            # 升级标记行不是 R3 候选:永不重复扫描,保证可无限重跑。
            continue
        summary["scanned"] += 1
        upgrade_key = f"{_UPGRADE_KEY_PREFIX}{event_key}"
        existing = prewarm_store.ledger_get(source, upgrade_key)
        if existing is not None and existing.get("cache_result_id") is not None:
            summary["already_upgraded"] += 1
            continue
        row = shared_cache.get_result(int(ledger["cache_result_id"]))
        if row is None or row.get("sku_status") != SKU_STATUS_UNRESOLVED:
            continue
        request_sku = str((row.get("payload") or {}).get("request_sku_id") or "").strip()
        if not request_sku:
            summary["skipped_no_sku"] += 1
            continue
        if is_temporary_request_code(request_sku, crawl_id=row.get("crawl_id")):
            summary["skipped_temporary"] += 1
            continue
        prewarm_store.ledger_claim(source, upgrade_key)
        try:
            upgraded = shared_cache.record_success(
                mall=row["mall"],
                submitted_url=row["submitted_url"],
                original_url=row["original_url"],
                returned_url=row["returned_url"],
                canonical_url=row["canonical_url"],
                crawl_id=row["crawl_id"],
                real_sku_id=request_sku,
                sku_status=SKU_STATUS_UPSTREAM_PROVIDED,
                title=row["title"],
                price=row["price"],
                comment_count=row["comment_count"],
                source=source,
                succeeded_at=row["succeeded_at"],
                payload={
                    **(row.get("payload") or {}),
                    "sku_evidence_upgraded_from_result_id": int(row["id"]),
                },
            )
        except sqlite3.OperationalError:
            # 瞬时失败(库锁/繁忙):丢 claim,下一轮重试。
            prewarm_store.ledger_drop_claim(source, upgrade_key)
            summary["transient"] += 1
            continue
        except (ValueError, AliasConflictError) as exc:
            # 确定性失败:记账,永不重试。
            prewarm_store.ledger_complete(source, upgrade_key, error=str(exc))
            summary["failed"] += 1
            continue
        prewarm_store.ledger_complete(
            source, upgrade_key, cache_result_id=int(upgraded["id"])
        )
        summary["upgraded"] += 1
    return summary


# ---- historical backfill (Finding 5) ------------------------------------------------

_SYNC_COUNTER_KEYS = (
    "scanned",
    "skipped_non_ok",
    "skipped_invalid",
    "already_synced",
    "errored_before",
    "ok_synced",
    "transient_failures",
    "deterministic_failures",
    "recovered_claims",
    "dropped_claims",
)


def backfill_daily_history(
    prewarm_store: CustomerPrewarmStore,
    shared_cache: SharedCrawlCache,
    *,
    runtime_root: str | Path,
    business_dates: Sequence[str],
    source: str = "daily-internet",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backfill historical daily-internet successes from past per-day state DBs.

    Reuses sync_daily_internet_results per business date; the sync ledger's
    source_event_key already embeds business_date, so backfilled events are
    globally unique, idempotent and resumable: an interrupted run can simply
    be re-run (completed keys skip, transient failures retry).  Read-only on
    every historical state DB; never creates remote tasks; no SKU-only merge
    (SharedCrawlCache URL-identity model only).

    The summary merges the per-sync counters (``per_date[date]`` keeps each
    date's own summary); one date failing with an unexpected exception is
    collected into ``errors`` and never aborts the remaining dates.
    """
    summary: dict[str, Any] = {
        "dates_scanned": 0,
        "dates_missing": [],
        "errors": [],
        "per_date": {},
    }
    for key in _SYNC_COUNTER_KEYS:
        summary[key] = 0
    root = Path(runtime_root)
    for date in business_dates:
        state_db = root / str(date) / "state.sqlite3"
        try:
            per_date = sync_daily_internet_results(
                prewarm_store,
                shared_cache,
                state_db_path=state_db,
                business_date=str(date),
                source=source,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - 单日失败不中断整体回填
            summary["errors"].append(f"{date}: {type(exc).__name__}: {exc}")
            continue
        summary["per_date"][str(date)] = per_date
        if per_date.get("state_db") == "missing":
            summary["dates_missing"].append(str(date))
            continue
        summary["dates_scanned"] += 1
        for key in _SYNC_COUNTER_KEYS:
            summary[key] += int(per_date.get(key, 0))
        summary["errors"].extend(f"{date}: {item}" for item in per_date.get("errors", []))
    return summary


# ---- dry-run cache snapshot (Finding 6) ---------------------------------------------


def snapshot_cache_readonly(
    source: str | Path,
    dest: str | Path,
    *,
    sandbox_root: str | Path | None = None,
) -> dict[str, object]:
    """Copy a Shared Crawl Cache file into the sandbox WITHOUT touching the source.

    Opens the source with SQLite URI mode=ro (no schema init, no WAL/DDL, no
    directory creation) and uses the sqlite backup API to write a consistent
    snapshot into dest.

    Fresh-destination semantics (Issue #9 Round 3): when ``sandbox_root`` is
    given AND ``dest`` resolves strictly inside it, any stale sandbox copy is
    removed BEFORE the copy — ``dest`` plus its SQLite ``-wal``/``-shm``
    sidecars — also in the missing-source case, so the sandbox cache is always
    a fresh snapshot of the source, never a leftover from a previous dry-run
    (whose production source may since have been deleted).  The returned dict
    gains ``"stale_sandbox_removed"`` for sandboxed destinations.  When
    ``sandbox_root`` is None OR dest resolves outside it, nothing is ever
    deleted and the behaviour/shape is unchanged — production paths can never
    be unlinked.

    Note: read-only access to a WAL-mode database may leave transient SQLite
    ``-shm``/``-wal`` sidecar files next to the source; the main database file
    itself is never written (read-only connections cannot checkpoint).
    """
    source_path = Path(source)
    dest_path = Path(dest)
    sandboxed = False
    if sandbox_root is not None:
        try:
            sandboxed = dest_path.resolve().is_relative_to(Path(sandbox_root).resolve())
        except (OSError, ValueError):  # pragma: no cover - unresolvable paths stay unsandboxed
            sandboxed = False
    removed_stale = False
    if sandboxed:
        for stale in (
            dest_path,
            dest_path.with_name(dest_path.name + "-wal"),
            dest_path.with_name(dest_path.name + "-shm"),
        ):
            try:
                stale.unlink()
                removed_stale = True
            except FileNotFoundError:
                continue
            except OSError:
                # Best-effort: a locked leftover must not fail the dry-run; the
                # backup copy below still replaces the database content.
                continue
    if not source_path.is_file():
        result: dict[str, object] = {"copied": False, "reason": "missing"}
        if sandboxed:
            result["stale_sandbox_removed"] = removed_stale
        return result
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest_path))
    try:
        src.backup(dst)
        rows = int(dst.execute("SELECT COUNT(*) FROM crawl_results").fetchone()[0])
    finally:
        dst.close()
        src.close()
    result = {"copied": True, "crawl_results": rows}
    if sandboxed:
        result["stale_sandbox_removed"] = removed_stale
    return result


# ---- orchestration ---------------------------------------------------------------


def _read_audit_item_rows(state_db_path: str | Path, business_date: str) -> list[Mapping[str, Any]]:
    connection = _open_state_db_readonly(state_db_path)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            """SELECT a.row_json FROM audit_items a
               JOIN daily_runs d ON d.id=a.run_id
               WHERE d.business_date=?""",
            (business_date,),
        ).fetchall()
    finally:
        connection.close()
    parsed: list[Mapping[str, Any]] = []
    for row in rows:
        try:
            value = json.loads(row["row_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping):
            parsed.append(value)
    return parsed


def run_prewarm_scan(
    *,
    prewarm_store: CustomerPrewarmStore,
    shared_cache: SharedCrawlCache,
    business_date: str,
    state_db_path: str | Path,
    mappings: Sequence[ZhaoshangMallMapping],
    detail_fetch: Callable[[str, str], Mapping[str, Any]],
    max_documents: int = 10,
    refresh_limit: int | None = None,
    monotonic: Callable[[], float] | None = None,
    budget_seconds: float = 50.0,
    customer_recovery_fetch: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    customer_recovery_interval_seconds: float = 300.0,
) -> dict[str, Any]:
    """One bounded scanner pass: daily sync, catch-up intake, recovery intake,
    detail scan, refresh.

    Phases run strictly A->B->B2->C->D so same-day syncs are visible to the
    detail scan and the refresh.  Every phase is individually guarded: a phase
    failure is recorded in ``phase_errors`` and later phases still run;
    per-document / per-candidate / per-row isolation lives inside the lower
    layers.

    Phase B2 (Round 3 Finding A) runs only when ``customer_recovery_fetch`` is
    wired: it re-reads the customer side only (no SaaS query) so documents the
    event sidecar never delivered still reach the store; the phase is
    throttled by ``customer_recovery_interval_seconds`` (0 = every pass) and
    its counters are ``customer_recovery_intaked`` /
    ``customer_recovery_throttled``.
    """
    clock = monotonic if monotonic is not None else time.monotonic
    deadline = clock() + budget_seconds
    summary: dict[str, Any] = {"phase_errors": []}

    # Phase A: daily internet sync
    try:
        summary.update(
            sync_daily_internet_results(
                prewarm_store,
                shared_cache,
                state_db_path=state_db_path,
                business_date=business_date,
            )
        )
    except Exception as exc:  # noqa: BLE001 - phase isolation
        summary["phase_errors"].append({"phase": "daily_sync", "error": str(exc)})

    # Phase B: catch-up intake from the same state db
    intake_count = 0
    try:
        intake_count = intake_documents(
            prewarm_store,
            business_date,
            _read_audit_item_rows(state_db_path, business_date),
            source="scanner",
        )
    except Exception as exc:  # noqa: BLE001 - phase isolation
        summary["phase_errors"].append({"phase": "catch_up_intake", "error": str(exc)})
    summary["intake_count"] = intake_count

    # Phase B2: customer-side-only recovery intake (Round 3 Finding A)
    summary["customer_recovery_intaked"] = 0
    summary["customer_recovery_throttled"] = 0
    try:
        if customer_recovery_fetch is not None:
            recovery = recover_customer_documents(
                prewarm_store,
                customer_fetch=customer_recovery_fetch,
                business_date=business_date,
                interval_seconds=customer_recovery_interval_seconds,
            )
            summary["customer_recovery_intaked"] = int(recovery.get("documents_intaked", 0))
            summary["customer_recovery_throttled"] = 1 if recovery.get("throttled") else 0
    except Exception as exc:  # noqa: BLE001 - phase isolation
        summary["phase_errors"].append({"phase": "customer_recovery", "error": str(exc)})

    # Phase C: detail scan, bounded by the remaining budget (keep a reserve)
    documents_processed = 0
    detail_failures = 0
    candidate_hits = 0
    candidate_misses = 0
    try:
        for document_row in prewarm_store.pending_documents(max_documents):
            if deadline - clock() <= DETAIL_BUDGET_RESERVE_SECONDS:
                break
            outcome = process_document(
                prewarm_store,
                shared_cache,
                document_row,
                mappings=mappings,
                detail_fetch=detail_fetch,
                # Issue #13 Round 3: the pass's business date (not the
                # document's immutable first-seen day) dates the candidate
                # first-seen of new links and the daily observation.
                business_date=business_date,
            )
            documents_processed += 1
            if outcome.get("detail_ok"):
                candidate_hits += int(outcome.get("hits", 0))
                candidate_misses += int(outcome.get("misses", 0))
            else:
                detail_failures += 1
    except Exception as exc:  # noqa: BLE001 - phase isolation
        summary["phase_errors"].append({"phase": "detail_scan", "error": str(exc)})
    summary["documents_processed"] = documents_processed
    summary["detail_failures"] = detail_failures
    summary["candidate_hits"] = candidate_hits
    summary["candidate_misses"] = candidate_misses

    # Phase D: refresh pending candidates (absorbs Phase A additions)
    try:
        refreshed = refresh_pending_candidates(prewarm_store, shared_cache, limit=refresh_limit)
        summary["refresh_checked"] = refreshed["checked"]
        summary["refresh_hits"] = refreshed["hits"]
    except Exception as exc:  # noqa: BLE001 - phase isolation
        summary["phase_errors"].append({"phase": "refresh_candidates", "error": str(exc)})

    summary["stats"] = prewarm_store.stats()
    return summary
