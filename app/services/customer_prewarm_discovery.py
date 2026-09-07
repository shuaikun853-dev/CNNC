"""Customer prewarm discovery: routing, detail scanning, and daily sync (Issue #9 Phase 2).

Phase 2 wires the Phase-1 durable discovery state
(:mod:`app.services.customer_prewarm_store`) to the cross-day Shared Crawl
Cache (:mod:`app.services.shared_crawl_cache`, Issue #7):

- :func:`route_url` maps one customer reference URL to a mall key using the
  in-repo zhaoshang mapping list, applying the single deterministic production
  rewrite (``www.jslink.com`` -> ``b2b.nbdeli.com``) as a *lookup* transform:
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
  idempotent ledger with crash recovery.
- :func:`run_prewarm_scan` orchestrates phases A-D with a time budget and
  per-phase error isolation.

Every database failure is contained: a broken row, link, candidate, or whole
phase is counted and the scan continues.  Library code never prints.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from app.services.crawl_url_identity import SKU_STATUS_CONFIRMED, SKU_STATUS_UNRESOLVED
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    CustomerPrewarmStore,
)
from app.services.customer_reference_links import extract_reference_links
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

DETAIL_MAX_ATTEMPTS = 5
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

    The single deterministic production rewrite is applied first, verbatim
    from STEP3 ``scripts/process_monitor_no_push_zhaoshang.py:402``
    (``url.replace("www.jslink.com", "b2b.nbdeli.com")``): STEP3 applies it in
    its multi-link branch, Issue #9 applies it unconditionally as the one
    deterministic rule.  The original URL is NEVER mutated - it stays verbatim
    as ``original_url``; only the routing/lookup pair carries the rewrite.
    Unparseable/empty input never raises and routes to mall ``''``.
    """
    original = str(original_url or "").strip()
    routed = original.replace("www.jslink.com", "b2b.nbdeli.com")
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
        seen.add(document_id)
    return len(seen)


# ---- Phase C: detail scan -------------------------------------------------------


def process_document(
    store: CustomerPrewarmStore,
    shared_cache: SharedCrawlCache,
    document_row: Mapping[str, Any],
    *,
    mappings: Sequence[ZhaoshangMallMapping],
    detail_fetch: Callable[[str, str], Mapping[str, Any]],
    source: str = "detail",
) -> dict[str, Any]:
    """Discover reference links of one document and resolve them against the cache.

    ``detail_fetch`` receives ``(document_id, four_category_id)`` and returns
    ``{"ok": bool, "payload": mapping | None, "error": str | None}``; its
    exceptions are caught and counted as detail failures.  Per-link failures
    are isolated (collected, scan continues).  A detail failure never raises
    and bumps the bounded attempt counter (FAILED at ``max_attempts=5``).
    A document whose links hit per-link errors (e.g. sqlite busy) also stays
    retryable: it is recorded as a bounded failure instead of DONE, so the
    next pass re-processes it and the missing links are not lost.
    """
    document_id = str(document_row["document_id"])
    four_category_id = str(document_row.get("four_category_id") or "")
    business_date = str(document_row["business_date"])
    try:
        response = detail_fetch(document_id, four_category_id)
    except Exception as exc:  # noqa: BLE001 - detail failure must never raise
        error = f"detail fetch exception: {exc}"
        store.record_document_detail_failure(document_id, error, max_attempts=DETAIL_MAX_ATTEMPTS)
        return {"document_id": document_id, "detail_ok": False, "error": error}
    if not isinstance(response, Mapping) or not response.get("ok"):
        raw_error = response.get("error") if isinstance(response, Mapping) else None
        error = str(raw_error) if raw_error else "detail fetch reported failure"
        store.record_document_detail_failure(document_id, error, max_attempts=DETAIL_MAX_ATTEMPTS)
        return {"document_id": document_id, "detail_ok": False, "error": error}

    links = extract_reference_links(response.get("payload"))
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
                    business_date=business_date,
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
            # next detail pass). Record a bounded failure so the scan retries.
            store.record_document_detail_failure(
                document_id,
                "link processing errors: " + "; ".join(errors[:3]),
                max_attempts=DETAIL_MAX_ATTEMPTS,
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


def sync_daily_internet_results(
    prewarm_store: CustomerPrewarmStore,
    shared_cache: SharedCrawlCache,
    *,
    state_db_path: str | Path,
    business_date: str,
    source: str = "daily-internet",
    dry_run: bool = False,
    stale_claim_seconds: int = 600,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mirror successful daily crawl rows into the shared cache, idempotently.

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
    """
    now_value = now if now is not None else datetime.now(UTC)
    summary: dict[str, Any] = {
        "state_db": "ok",
        "scanned": 0,
        "skipped_non_ok": 0,
        "skipped_invalid": 0,
        "already_synced": 0,
        "errored_before": 0,
        "would_record": 0,
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
               ORDER BY c.id"""
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

        if dry_run:
            summary["would_record"] += 1
            prewarm_store.ledger_complete(source, event_key, error="dry_run")
            continue

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

        sku_value = str(row["sku_id"] or "").strip()
        try:
            row_out = shared_cache.record_success(
                mall=mall_key,
                submitted_url=exact_url,
                crawl_id=task_name or None,
                real_sku_id=sku_value or None,
                sku_status=SKU_STATUS_CONFIRMED if sku_value else SKU_STATUS_UNRESOLVED,
                price=result.get("page_price_value"),
                source=source,
                succeeded_at=succeeded_at,
                payload=payload,
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
    dry_run: bool = False,
    monotonic: Callable[[], float] | None = None,
    budget_seconds: float = 50.0,
) -> dict[str, Any]:
    """One bounded scanner pass: daily sync, catch-up intake, detail scan, refresh.

    Phases run strictly A->B->C->D so same-day syncs are visible to the detail
    scan and the refresh.  Every phase is individually guarded: a phase failure
    is recorded in ``phase_errors`` and later phases still run; per-document /
    per-candidate / per-row isolation lives inside the lower layers.
    """
    clock = monotonic if monotonic is not None else time.monotonic
    deadline = clock() + budget_seconds
    summary: dict[str, Any] = {"phase_errors": []}

    # Phase A: daily internet sync (dry_run passthrough)
    try:
        summary.update(
            sync_daily_internet_results(
                prewarm_store,
                shared_cache,
                state_db_path=state_db_path,
                business_date=business_date,
                dry_run=dry_run,
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

    # Phase C: detail scan, bounded by the remaining budget (keep a reserve)
    documents_processed = 0
    detail_failures = 0
    candidate_hits = 0
    candidate_misses = 0
    try:
        for document_row in prewarm_store.pending_documents(
            max_documents, max_attempts=DETAIL_MAX_ATTEMPTS
        ):
            if deadline - clock() <= DETAIL_BUDGET_RESERVE_SECONDS:
                break
            outcome = process_document(
                prewarm_store,
                shared_cache,
                document_row,
                mappings=mappings,
                detail_fetch=detail_fetch,
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
