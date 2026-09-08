"""Evidence-based legacy zhaoshang history backfill (Issue #10 Review Round 2, Finding 2).

The old ``--backfill-zhaoshang-history`` merely re-ran the daily-internet sync
over ALL crawl ``SUCCESS`` rows.  This module replaces that fake with the REAL
evidence chain: a row is recorded into the shared cache ONLY when BOTH sides
agree —

1. the remote job is durably ``EXPORTED``/``COMPLETED`` with an export path,
   and :meth:`CnncLegacyZhaoshangAdapter.harvest_existing` re-validates the
   export workbook through the full provenance gate (批次号 == task_name,
   商城标识 == mall_key, execution provenance, ``(sku, url)`` multiset
   equality vs the immutable job payload) — zero remote calls; and
2. the workbook's validated ``(sku_id, exact_url)`` identity is bound to a
   ``SUCCESS`` crawl row of that very job whose ``result_json`` reports
   ``page_price_status == 'OK'`` (the per-row semantics the daily sync uses).

Everything else mirrors ``sync_daily_internet_results`` byte for byte: the
producer event key format (so the same real export success dedupes across
ingest sources), the ledger claim/complete flow with crash-recovery pre-pass,
the honest SKU encoding (usable → ``UPSTREAM_PROVIDED``, temporary codes →
``UNRESOLVED`` with the original value preserved as ``payload
["request_sku_id"]``), and the transient/deterministic failure split.

Unbound SUCCESS crawl rows (``remote_job_id IS NULL``) are structurally
invisible to this backfill: without a validated export workbook there is no
cross-proof, so they are never recorded.  The backfill NEVER creates remote
tasks, never prioritizes, never reconciles, never crawls, and never guesses
SKU aliases — ``record_success``'s chain-alias learning from submitted /
returned URLs is the only alias source, and only proven URLs are passed.
Reconstruction or validation failures are isolated per job into
``jobs_partial`` (a documented outcome, never a crash, never a fallback to
reading the workbook without the gate).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.services.customer_prewarm_discovery import (
    _open_state_db_readonly,
    is_temporary_request_code,
)
from app.services.customer_prewarm_dispatch import _reconstruct_prepared_job
from app.services.crawl_url_identity import (
    SKU_STATUS_UNRESOLVED,
    SKU_STATUS_UPSTREAM_PROVIDED,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    HarvestStatus,
)

BACKFILL_SOURCE = "zhaoshang-history"

# A completed ledger entry under ANY ingest source proves the same producer
# event is already in the cache: never append the same evidence twice just
# because the mirroring source label differs.
_CROSS_SOURCE_LEDGER_KEYS = ("daily-internet", "zhaoshang-history", "backfill-history")

_BACKFILL_COUNTER_KEYS = (
    "jobs_examined",
    "jobs_validated",
    "rows_examined",
    "rows_recorded",
    "already_ingested_cross_source",
    "skipped_no_workbook_match",
    "skipped_price_not_ok",
    "skipped_invalid",
    "errored_before",
    "transient_failures",
    "deterministic_failures",
)


def _empty_date_summary() -> dict[str, Any]:
    detail: dict[str, Any] = {"state_db": "ok", "jobs_partial": []}
    for key in _BACKFILL_COUNTER_KEYS:
        detail[key] = 0
    return detail


def _recover_stale_claims(
    prewarm_store: Any, shared_cache: Any, *, stale_claim_seconds: int, now_value: datetime
) -> tuple[int, int, list[str]]:
    """Mirror sync's crash-recovery pre-pass for ``zhaoshang-history`` claims."""
    recovered = 0
    dropped = 0
    errors: list[str] = []
    cutoff = (now_value - timedelta(seconds=stale_claim_seconds)).isoformat()
    for claim in prewarm_store.ledger_pending_claims(BACKFILL_SOURCE, older_than=cutoff):
        key = str(claim["source_event_key"])
        try:
            existing = shared_cache.find_id_by_source_event_key(BACKFILL_SOURCE, key)
        except Exception as exc:  # noqa: BLE001 - probe failure keeps the claim
            errors.append(f"crash probe {key}: {exc}")
            continue
        try:
            if existing is not None:
                prewarm_store.ledger_complete(BACKFILL_SOURCE, key, cache_result_id=int(existing))
                recovered += 1
            else:
                # Single-instance backfill: dropping lets the row re-record.
                prewarm_store.ledger_drop_claim(BACKFILL_SOURCE, key)
                dropped += 1
        except Exception as exc:  # noqa: BLE001 - recovery failure keeps scanning
            errors.append(f"crash recovery {key}: {exc}")
    return recovered, dropped, errors


def _record_row(
    prewarm_store: Any,
    shared_cache: Any,
    *,
    business_date: str,
    task_name: str,
    job_id: int,
    crawl_row: Mapping[str, Any],
    result_by_identity: Mapping[tuple[str, str], Mapping[str, Any]],
    detail: dict[str, Any],
) -> None:
    """Cross-prove and record ONE bound SUCCESS crawl row (mirrors the sync)."""
    result_json_raw = str(crawl_row["result_json"])
    crawl_id = int(crawl_row["id"])
    task_label = task_name or f"crawl-{crawl_id}"
    event_key = (
        f"{business_date}:{task_label}:{crawl_id}:"
        f"{hashlib.sha256(result_json_raw.encode('utf-8')).hexdigest()[:12]}"
    )
    try:
        result = json.loads(result_json_raw)
    except (TypeError, ValueError) as exc:
        # 内容损坏是确定性失败:记账,永不重试。
        prewarm_store.ledger_claim(BACKFILL_SOURCE, event_key)
        prewarm_store.ledger_complete(BACKFILL_SOURCE, event_key, error=f"invalid result_json: {exc}")
        detail["deterministic_failures"] += 1
        return
    if not isinstance(result, Mapping) or result.get("page_price_status") != "OK":
        detail["skipped_price_not_ok"] += 1
        return

    mall_key = str(crawl_row["mall_key"] or "").strip()
    exact_url = str(crawl_row["exact_url"] or "").strip()
    if not mall_key or not exact_url:
        prewarm_store.ledger_claim(BACKFILL_SOURCE, event_key)
        prewarm_store.ledger_complete(
            BACKFILL_SOURCE,
            event_key,
            error=f"invalid crawl row: crawl_id={crawl_id} "
            f"mall_key={mall_key!r} exact_url={exact_url!r}",
        )
        detail["skipped_invalid"] += 1
        return

    workbook_row = result_by_identity.get((str(crawl_row["sku_id"] or "").strip(), exact_url))
    if workbook_row is None:
        # No validated workbook row for this identity: no cross-proof, no record.
        detail["skipped_no_workbook_match"] += 1
        return
    if str(workbook_row.get("page_price_status") or "") != "OK":
        detail["skipped_price_not_ok"] += 1
        return

    # Cross-source dedupe BEFORE claiming: a completed ledger entry under any
    # ingest source proves the cache already holds this producer event.
    own_ledger = None
    for source in _CROSS_SOURCE_LEDGER_KEYS:
        ledger = prewarm_store.ledger_get(source, event_key)
        if source == BACKFILL_SOURCE:
            own_ledger = ledger
        if ledger is not None and ledger.get("cache_result_id") is not None:
            detail["already_ingested_cross_source"] += 1
            return
    if own_ledger is not None and own_ledger.get("error") is not None:
        detail["errored_before"] += 1
        return
    prewarm_store.ledger_claim(BACKFILL_SOURCE, event_key)

    # succeeded_at = crawl_work.updated_at(真实成功时序);无效时间戳降级为
    # None(cache 取当前时刻)并把原文保留进 payload。
    updated_at_raw = crawl_row["updated_at"]
    succeeded_at: str | None = None
    try:
        parsed_updated_at = datetime.fromisoformat(str(updated_at_raw)) if updated_at_raw else None
    except ValueError:
        parsed_updated_at = None
    if parsed_updated_at is not None and parsed_updated_at.tzinfo is not None:
        succeeded_at = str(updated_at_raw)

    payload: dict[str, Any] = {
        "source_event_key": event_key,
        "business_date": business_date,
        "crawl_work_id": crawl_id,
        "remote_job_id": job_id,
        "result": result,
    }
    if succeeded_at is None:
        payload["raw_updated_at"] = updated_at_raw

    # The row's sku_id is upstream (SaaS/request-side) evidence re-proven by
    # the validated workbook identity, never independently re-confirmed page
    # evidence: usable values are honestly encoded UPSTREAM_PROVIDED while
    # temporary codes (zhpq-*, ROW<n>, task/crawl-id echo) degrade to
    # UNRESOLVED.  Either way the original value is preserved verbatim as
    # payload["request_sku_id"] - a valid price result is never lost because
    # of a placeholder rejection.
    sku_value = str(crawl_row["sku_id"] or "").strip()
    crawl_label = task_name or None
    if sku_value and not is_temporary_request_code(sku_value, crawl_id=crawl_label):
        real_sku_id: str | None = sku_value
        sku_status = SKU_STATUS_UPSTREAM_PROVIDED
    else:
        real_sku_id = None
        sku_status = SKU_STATUS_UNRESOLVED
    payload["request_sku_id"] = sku_value
    try:
        row_out = shared_cache.record_success(
            mall=mall_key,
            submitted_url=exact_url,
            crawl_id=crawl_label,
            real_sku_id=real_sku_id,
            sku_status=sku_status,
            price=workbook_row.get("page_price_value"),
            source=BACKFILL_SOURCE,
            succeeded_at=succeeded_at,
            payload=payload,
        )
    except sqlite3.OperationalError:
        # 瞬时失败(库锁/繁忙):不记 ledger,下轮重试。
        prewarm_store.ledger_drop_claim(BACKFILL_SOURCE, event_key)
        detail["transient_failures"] += 1
        return
    except Exception as exc:  # noqa: BLE001 - ValueError/AliasConflict: deterministic
        prewarm_store.ledger_complete(BACKFILL_SOURCE, event_key, error=str(exc))
        detail["deterministic_failures"] += 1
        return
    prewarm_store.ledger_complete(BACKFILL_SOURCE, event_key, cache_result_id=int(row_out["id"]))
    detail["rows_recorded"] += 1


def backfill_zhaoshang_history(
    prewarm_store: Any,
    shared_cache: Any,
    *,
    runtime_root: str | Path,
    business_dates: Sequence[str],
    adapter: Any | None = None,
    stale_claim_seconds: int = 600,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Backfill legacy zhaoshang export successes from past per-day state DBs.

    Only rows with the REAL evidence chain are recorded: a durably exported /
    completed remote job whose export workbook passes
    :meth:`CnncLegacyZhaoshangAdapter.harvest_existing` (full provenance gate,
    zero remote calls) AND whose validated ``(sku, url)`` identity is bound to
    a ``SUCCESS`` crawl row of the same job with ``page_price_status == OK``.

    Idempotent and resumable via the same sync ledger format (the producer
    event key embeds business date, task name, crawl id and content hash);
    completed entries under any ingest source skip as
    ``already_ingested_cross_source``.  Read-only (``mode=ro``) on every
    historical state DB; reconstruction/validation failures are isolated per
    job into ``jobs_partial`` and never fall back to ungated workbook reads.
    The summary reports ``partial=True`` when any job failed evidence
    validation or any unexpected error was contained.
    """
    now_value = now if now is not None else datetime.now(UTC)
    summary: dict[str, Any] = {
        "dates_scanned": 0,
        "dates_missing": [],
        "jobs_partial": [],
        "errors": [],
        "per_date": {},
    }
    for key in _BACKFILL_COUNTER_KEYS:
        summary[key] = 0

    summary["recovered_claims"], summary["dropped_claims"], recovery_errors = (
        _recover_stale_claims(
            prewarm_store,
            shared_cache,
            stale_claim_seconds=stale_claim_seconds,
            now_value=now_value,
        )
    )
    summary["errors"].extend(recovery_errors)

    adapter_holder: list[Any] = []

    def get_adapter() -> Any:
        if not adapter_holder:
            adapter_holder.append(
                adapter if adapter is not None else CnncLegacyZhaoshangAdapter()
            )
        return adapter_holder[0]

    root = Path(runtime_root)
    for date in business_dates:
        business_date = str(date)
        detail = _empty_date_summary()
        summary["per_date"][business_date] = detail
        connection = _open_state_db_readonly(root / business_date / "state.sqlite3")
        if connection is None:
            detail["state_db"] = "missing"
            summary["dates_missing"].append(business_date)
            continue
        try:
            job_rows = connection.execute(
                """SELECT * FROM remote_jobs
                   WHERE state IN ('EXPORTED','COMPLETED')
                     AND export_path IS NOT NULL AND TRIM(export_path)<>''
                     AND payload_json IS NOT NULL
                   ORDER BY id"""
            ).fetchall()
            for job_row in job_rows:
                _backfill_job(
                    connection,
                    prewarm_store,
                    shared_cache,
                    get_adapter,
                    business_date=business_date,
                    job_row=dict(job_row),
                    detail=detail,
                )
        except Exception as exc:  # noqa: BLE001 - 单日失败不中断整体回填
            summary["errors"].append(f"{business_date}: {type(exc).__name__}: {exc}")
        finally:
            connection.close()
        if detail["state_db"] == "missing":
            continue
        detail["state_db"] = "ok"
        summary["dates_scanned"] += 1
        for key in _BACKFILL_COUNTER_KEYS:
            summary[key] += int(detail.get(key, 0))
        summary["jobs_partial"].extend(detail["jobs_partial"])
    summary["partial"] = bool(summary["jobs_partial"] or summary["errors"])
    return summary


def _backfill_job(
    connection: sqlite3.Connection,
    prewarm_store: Any,
    shared_cache: Any,
    get_adapter: Any,
    *,
    business_date: str,
    job_row: dict[str, Any],
    detail: dict[str, Any],
) -> None:
    """Validate ONE exported job and record its cross-proven crawl rows."""
    detail["jobs_examined"] += 1
    job_id = int(job_row["id"])
    task_name = str(job_row["task_name"] or "")
    try:
        prepared = _reconstruct_prepared_job(get_adapter(), business_date, job_row)
    except Exception as exc:  # noqa: BLE001 - fail-closed partial, never a fallback
        detail["jobs_partial"].append(
            {
                "job_id": job_id,
                "task_name": task_name,
                "reason": f"reconstruct failed: {type(exc).__name__}: {exc}",
            }
        )
        return
    outcome = get_adapter().harvest_existing(prepared)
    if outcome.status is not HarvestStatus.EXPORTED or not outcome.result_by_identity:
        reason = f"harvest status={outcome.status.value}"
        diagnostic = str(getattr(outcome, "diagnostic", "") or "")
        if diagnostic:
            reason = f"{reason}: {diagnostic}"
        detail["jobs_partial"].append(
            {"job_id": job_id, "task_name": task_name, "reason": reason}
        )
        return
    detail["jobs_validated"] += 1
    crawl_rows = connection.execute(
        """SELECT c.* FROM crawl_work c
           WHERE c.remote_job_id=? AND c.state='SUCCESS' AND c.result_json IS NOT NULL
           ORDER BY c.id""",
        (job_id,),
    ).fetchall()
    for crawl_row in crawl_rows:
        detail["rows_examined"] += 1
        _record_row(
            prewarm_store,
            shared_cache,
            business_date=business_date,
            task_name=task_name,
            job_id=job_id,
            crawl_row=dict(crawl_row),
            result_by_identity=outcome.result_by_identity,
            detail=detail,
        )
