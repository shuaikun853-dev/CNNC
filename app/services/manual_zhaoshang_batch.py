"""Shared durable Batch runner for the two manual Zhaoshang entrypoints.

The business scripts only translate their rows to :class:`CrawlRequest` and
consume normalized results.  This module owns the Batch-first URL gate and
reuses the incremental workflow's intent-before-POST and harvest lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.services.batch_sql_result_adapter import ALLOWED_MALLS
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.legacy_zhaoshang_adapter import CrawlRequest
from app.services.zhaoshang_url_identity import (
    is_batch_submitted_url_supported,
    submitted_url_product_code,
)
from app.workflows.incremental_price_audit import (
    TickBudget,
    TickConfig,
    TickDependencies,
    _empty_counters,
    _harvest,
    _submit,
)


BATCH_BACKEND_VERSION = "batch-url-v1"
LEGACY_BACKEND_VERSION = "legacy-v1"


@dataclass(frozen=True)
class ManualBatchRequest:
    source_id: str
    request: CrawlRequest

    def __post_init__(self) -> None:
        if not str(self.source_id).strip():
            raise ValueError("manual Batch request requires a source_id")


@dataclass(frozen=True)
class BatchFirstResult:
    batch_results: Mapping[str, Mapping[str, Any]]
    legacy_result: Any
    batch_requests: tuple[ManualBatchRequest, ...]
    legacy_requests: tuple[ManualBatchRequest, ...]


@dataclass(frozen=True)
class Step3ManualRoute:
    items: tuple[ManualBatchRequest, ...]
    batch_items: tuple[ManualBatchRequest, ...]
    legacy_items: tuple[ManualBatchRequest, ...]
    zhaoshang_urls: tuple[str, ...]
    non_zhaoshang_urls: tuple[str, ...]
    source_urls: Mapping[str, str]


def authoritative_product_code(mall: str, url: str, candidate: object) -> str:
    """Reuse the submitted-URL identity rule only for its two scoped malls."""

    return str(submitted_url_product_code(mall, url) or candidate or "").strip()


def route_step3_manual_requests(
    urls: Sequence[str],
    *,
    adapter: Any,
    sku_resolver: Callable[[str], str],
    legacy_mall_resolver: Callable[[str], str | None],
    mall_names: Mapping[str, str],
) -> Step3ManualRoute:
    """Classify STEP3 URLs through the complete mapping before Batch routing."""

    items: list[ManualBatchRequest] = []
    zhaoshang_urls: list[str] = []
    non_zhaoshang_urls: list[str] = []
    source_urls: dict[str, str] = {}
    for index, original_url in enumerate(urls, start=1):
        candidate_code = str(sku_resolver(original_url) or "").strip()
        request = adapter.request_from_saas_row(
            {"skuId": candidate_code, "extLink": original_url},
            backend_version=LEGACY_BACKEND_VERSION,
        )
        mapping_failed = request is None
        if request is None:
            mall = str(legacy_mall_resolver(original_url) or "").strip()
            if not mall:
                non_zhaoshang_urls.append(original_url)
                continue
            request = CrawlRequest(
                mall_key=mall,
                mall_source_id="",
                mall_name=str(mall_names.get(mall) or mall),
                sku_id=candidate_code,
                url=original_url,
                backend_version=LEGACY_BACKEND_VERSION,
            )
        request = replace(
            request,
            sku_id=authoritative_product_code(
                request.mall_key, request.url, request.sku_id
            ),
        )
        source_id = "step3:{}:{}".format(
            request.mall_key,
            hashlib.sha256(
                "{}|{}|{}".format(
                    request.mall_key, request.sku_id, request.url
                ).encode("utf-8")
            ).hexdigest()[:20],
        )
        item = ManualBatchRequest(source_id, request)
        routed = item if mapping_failed else route_manual_request(item)
        if not routed.request.sku_id:
            routed = ManualBatchRequest(
                routed.source_id,
                replace(routed.request, sku_id="ROW{}".format(index)),
            )
        items.append(routed)
        zhaoshang_urls.append(original_url)
        source_urls[routed.source_id] = original_url
    routed_items = tuple(items)
    return Step3ManualRoute(
        items=routed_items,
        batch_items=tuple(
            item
            for item in routed_items
            if item.request.backend_version == BATCH_BACKEND_VERSION
        ),
        legacy_items=tuple(
            item
            for item in routed_items
            if item.request.backend_version == LEGACY_BACKEND_VERSION
        ),
        zhaoshang_urls=tuple(zhaoshang_urls),
        non_zhaoshang_urls=tuple(non_zhaoshang_urls),
        source_urls=source_urls,
    )


def route_manual_request(item: ManualBatchRequest) -> ManualBatchRequest:
    request = item.request
    use_batch = (
        request.mall_key in ALLOWED_MALLS
        and bool(str(request.sku_id).strip())
        and is_batch_submitted_url_supported(request.mall_key, request.url)
    )
    return ManualBatchRequest(
        item.source_id,
        replace(
            request,
            backend_version=(
                BATCH_BACKEND_VERSION if use_batch else LEGACY_BACKEND_VERSION
            ),
        ),
    )


def execute_batch_first(
    items: Sequence[ManualBatchRequest],
    *,
    batch_runner: Callable[
        [tuple[ManualBatchRequest, ...]], Mapping[str, Mapping[str, Any]]
    ],
    legacy_runner: Callable[[tuple[ManualBatchRequest, ...]], Any],
) -> BatchFirstResult:
    """Route supported new work to Batch and only safety fallbacks to legacy."""

    routed = tuple(route_manual_request(item) for item in items)
    batch = tuple(
        item for item in routed if item.request.backend_version == BATCH_BACKEND_VERSION
    )
    legacy = tuple(
        item for item in routed if item.request.backend_version == LEGACY_BACKEND_VERSION
    )
    batch_results = batch_runner(batch) if batch else {}
    legacy_result = legacy_runner(legacy) if legacy else None
    return BatchFirstResult(
        batch_results=batch_results,
        legacy_result=legacy_result,
        batch_requests=batch,
        legacy_requests=legacy,
    )


def normalized_result_from_crawl(
    request: CrawlRequest, result: Mapping[str, Any]
) -> dict[str, Any]:
    """Translate the canonical crawl result without inventing an export file."""

    error = str(result.get("error") or result.get("reason") or "").strip()
    if result.get("manual"):
        status = "IDENTITY_CONFLICT" if "IDENTITY_CONFLICT" in error else "INVALID_RESULT"
        price = None
    elif result.get("terminal"):
        status = "CRAWLER_FAILED"
        price = None
    elif result.get("no_result"):
        status = "NO_RESULT"
        price = None
    else:
        status = str(result.get("page_price_status") or "INVALID_RESULT")
        price = result.get("page_price_value")
    returned_url = str(result.get("returned_url") or request.url).strip()
    product_code = str(
        result.get("page_sku_id") or result.get("page_goods_no") or request.sku_id
    ).strip()
    comment = result.get("comment_count")
    return {
        "product_code": product_code,
        "page_price_value": price,
        "page_price_status": status,
        "page_price_source": "zhaoshang_batch",
        "price": price,
        "title": str(result.get("page_title") or "").strip(),
        "comment": "" if comment in (None, "") else str(comment),
        "channel": request.mall_key,
        "submitted_url": request.url,
        "returned_url": returned_url,
        "diagnostic": error,
    }


def batch_records_from_results(
    items: Sequence[ManualBatchRequest],
    results: Mapping[str, Mapping[str, Any]],
    *,
    title_is_invalid: Callable[[str], bool] | None = None,
    source_urls: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Map exact source-scoped Batch results back to submitted STEP3 URLs."""

    records: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    for item in items:
        source_url = str((source_urls or {}).get(item.source_id) or item.request.url)
        entry = results.get(item.source_id, {"state": "PENDING"})
        state = str(entry.get("state") or "PENDING")
        crawl_result = entry.get("result")
        if state == "SUCCESS" and isinstance(crawl_result, Mapping):
            normalized = normalized_result_from_crawl(item.request, crawl_result)
            title = normalized["title"]
            if title_is_invalid is not None and title_is_invalid(str(title)):
                title = ""
            records[source_url] = {
                "code": normalized["product_code"],
                "price": normalized["page_price_value"],
                "status": normalized["page_price_status"],
                "title": title,
                "comment": normalized["comment"],
                "channel": normalized["channel"],
                "submitted_url": source_url,
                "returned_url": normalized["returned_url"],
            }
            continue
        if state == "NO_RESULT":
            continue
        failures.append(
            {
                "source_id": item.source_id,
                "mall": item.request.mall_key,
                "url": source_url,
                "reason": str(entry.get("error") or state),
            }
        )
    return records, failures


def _production_callbacks(
    *,
    business_date: str,
    purpose: str,
    state_path: Path,
    db_env_path: Path | str | None = None,
) -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
    # Lazy import avoids a module cycle: incremental runtime imports the STEP1
    # script for its existing detail adapter.
    from app.services.batch_zhaoshang_adapter import CnncBatchZhaoshangAdapter
    from app.services.batch_zhaoshang_priority import (
        BatchMaterializationTarget,
        connect_batch_priority_from_env_file,
        ensure_batch_priority,
    )
    from app.services.incremental_audit_runtime import (
        PROJECT_DATA_ROOT,
        RuntimePaths,
        _batch_sql_runtime_components,
        batch_zhaoshang_bridge,
    )
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter

    run_dir = state_path.parent
    resolved_db_env_path = (
        Path(db_env_path)
        if db_env_path is not None
        else PROJECT_DATA_ROOT / "ZHAOSHANG_DB.env"
    )
    paths = RuntimePaths(
        business_date=business_date,
        run_dir=run_dir,
        state_path=state_path,
        lock_path=run_dir / "manual_zhaoshang_batch.lock",
        detail_lock_path=run_dir / "unused_detail.lock",
        final_output_path=run_dir / "unused_output.xlsx",
    )
    adapter = CnncBatchZhaoshangAdapter(write_enabled=True)
    result_adapter, probe_runner = _batch_sql_runtime_components(
        env_path=resolved_db_env_path
    )

    def priority_runner(prepared_job: Any, timeout_seconds: float) -> Any:
        return ensure_batch_priority(
            lambda remaining: connect_batch_priority_from_env_file(
                resolved_db_env_path,
                timeout_seconds=remaining,
            ),
            BatchMaterializationTarget(
                execute_numbers=prepared_job.execute_numbers,
                expected_mall=prepared_job.mall_key,
            ),
            timeout_seconds=timeout_seconds,
        )

    return batch_zhaoshang_bridge(
        adapter,
        CnncLegacyZhaoshangAdapter(),
        paths=paths,
        business_date=business_date,
        priority_runner=priority_runner,
        result_adapter=result_adapter,
        materialization_probe_runner=probe_runner,
        request_resolver=lambda row: row.get("request"),
        source_id_resolver=lambda row: str(row.get("source_id") or ""),
        purpose=purpose,
    )


def _decoded_result(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    try:
        value = json.loads(str(row.get("result_json") or ""))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def run_batch_zhaoshang_requests(
    items: Sequence[ManualBatchRequest],
    *,
    business_date: str,
    purpose: str,
    state_path: Path | str,
    db_env_path: Path | str | None = None,
    callbacks: tuple[
        Callable[..., Any], Callable[..., Any], Callable[..., Any], Callable[..., Any]
    ]
    | None = None,
    runtime_budget_seconds: float = 1200.0,
    max_harvest_passes: int = 120,
    poll_interval_seconds: float = 10.0,
    stop_after_priority: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Mapping[str, Any]]:
    """Run one durable Batch request set without ever blindly repeating POST."""

    normalized = tuple(items)
    if not normalized:
        return {}
    if any(item.request.backend_version != BATCH_BACKEND_VERSION for item in normalized):
        raise ValueError("run_batch_zhaoshang_requests accepts only routed Batch work")
    if runtime_budget_seconds <= 0 or max_harvest_passes < 0:
        raise ValueError("manual Batch runtime limits must be valid")

    target = Path(state_path).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = ProcessLock(target.with_suffix(target.suffix + ".lock"))
    lock.acquire(owner={"pid": os.getpid(), "purpose": purpose})
    store: DailyAuditStore | None = None
    token = None
    try:
        store = DailyAuditStore(target)
        external_timeout_seconds = min(60.0, runtime_budget_seconds)
        submit_headroom_seconds = max(
            300.0, len(normalized) * external_timeout_seconds
        )
        token = store.acquire_lease(
            business_date,
            f"manual-batch-{purpose}-{os.getpid()}",
            max(
                30.0,
                runtime_budget_seconds + submit_headroom_seconds + 30.0,
            ),
            allow_unexpired_takeover=True,
            process_lock=lock,
        )
        plan, finalize, submit, harvest = callbacks or _production_callbacks(
            business_date=business_date,
            purpose=purpose,
            state_path=target,
            db_env_path=db_env_path,
        )
        deps = TickDependencies(
            discover=lambda _timeout: None,  # never called by this runner
            plan_crawl=plan,
            finalize_crawl_plan=finalize,
            submit_crawl=submit,
            harvest=harvest,
            monotonic=monotonic,
        )
        config = TickConfig(
            business_date=business_date,
            runtime_budget_seconds=runtime_budget_seconds,
            heavy_work_reserve_seconds=0,
            external_timeout_seconds=external_timeout_seconds,
        )
        budget = TickBudget(monotonic(), runtime_budget_seconds, 0, monotonic)
        counters = _empty_counters()
        rows = [
            {
                "row": {
                    "request": item.request,
                    "source_id": item.source_id,
                    "vCommodityId": item.source_id,
                    "skuId": item.request.sku_id,
                    "extLink": item.request.url,
                    "__crawl_required": True,
                    "__crawl_mall_key": item.request.mall_key,
                    "__crawl_mall_source_id": item.request.mall_source_id,
                    "__crawl_backend_version": item.request.backend_version,
                }
            }
            for item in normalized
        ]
        counters["recovered_submit_unknown"] = (
            store.recover_ambiguous_remote_submissions(token)
        )
        _submit(store, token, rows, config, deps, budget, counters)
        for pass_index in range(max_harvest_passes):
            active = store.list_remote_jobs(
                token,
                states={
                    "SUBMIT_UNKNOWN",
                    "PRIORITY_PENDING",
                    "SUBMITTED",
                    "RUNNING",
                    "EXPORTED",
                },
            )
            if not active or budget.remaining() <= 0:
                break
            _harvest(store, token, deps, budget, counters, config)
            if stop_after_priority:
                priority_pending = store.list_remote_jobs(
                    token, states={"SUBMIT_UNKNOWN", "PRIORITY_PENDING"}
                )
                if not priority_pending:
                    break
            if pass_index + 1 < max_harvest_passes and poll_interval_seconds > 0:
                remaining = budget.remaining()
                if remaining <= 0:
                    break
                sleep(min(poll_interval_seconds, remaining))

        source_ids = {item.source_id for item in normalized}
        by_source: dict[str, Mapping[str, Any]] = {}
        for row in store.list_crawl_work(token):
            source_id = str(row.get("source_id") or "")
            if source_id not in source_ids:
                continue
            result = _decoded_result(row)
            remote_job = (
                store.get_remote_job(int(row["remote_job_id"]))
                if row.get("remote_job_id") is not None
                else None
            )
            remote_state = str((remote_job or {}).get("state") or "")
            entry: dict[str, Any] = {
                "state": str(row["state"]),
                "remote_state": remote_state,
                "priority_confirmed": remote_state
                not in {
                    "",
                    "SUBMIT_INTENT",
                    "SUBMIT_UNKNOWN",
                    "PRIORITY_PENDING",
                    "MANUAL_REQUIRED",
                },
            }
            if result is not None:
                entry["result"] = result
            if row.get("error_text"):
                entry["error"] = str(row["error_text"])
            by_source[source_id] = entry
        return by_source
    finally:
        try:
            if token is not None and store is not None:
                store.release_lease(token)
        finally:
            lock.release()
