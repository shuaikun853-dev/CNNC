"""Build and atomically publish the formal workbook for one frozen daily run."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from app.services.daily_audit_store import (
    DailyAuditStore,
    LeaseToken,
    LeaseUnavailable,
    ProcessLock,
)
from app.services.incremental_audit_materializer import (
    FrozenAuditArtifact,
    LegacyWorkbookMaterializer,
    MaterializationResult,
)
from app.services.saas_audit_core import build_records, normalize_cell_str, parse_decimal
from app.services.title_match_model_adapter import TitleMatchModelResult
from app.workflows.work_01_saas_approval_json_audit import audit_saas_approval_records


@dataclass(frozen=True)
class FinalizeReadiness:
    frozen_rows: int = 0
    detail_pending: int = 0
    detail_terminal: int = 0
    model_pending: int = 0
    model_terminal: int = 0
    crawl_pending: int = 0
    crawl_terminal: int = 0
    crawl_unplanned: int = 0
    crawl_invalid_price: int = 0

    @property
    def ready(self) -> bool:
        return not any(
            (
                self.detail_pending,
                self.model_pending,
                self.model_terminal,
                self.crawl_pending,
                self.crawl_terminal,
                self.crawl_unplanned,
                self.crawl_invalid_price,
            )
        )


@dataclass(frozen=True)
class FrozenArtifactBuild:
    readiness: FinalizeReadiness
    artifact: FrozenAuditArtifact | None


@dataclass(frozen=True)
class FinalizePublication:
    readiness: FinalizeReadiness
    materialization: MaterializationResult | None


class FinalizeBlocked(RuntimeError):
    def __init__(self, message: str, readiness: FinalizeReadiness | None = None) -> None:
        self.readiness = readiness
        super().__init__(message)


class FinalizeAlreadyRunning(RuntimeError):
    pass


def _decode_json(value: object, expected: type, field: str) -> Any:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError) as exc:
        raise FinalizeBlocked(f"frozen {field} is not valid JSON") from exc
    if not isinstance(decoded, expected):
        raise FinalizeBlocked(f"frozen {field} has the wrong JSON shape")
    return decoded


def _stage_maps(store: DailyAuditStore, token: LeaseToken) -> tuple[dict[int, Mapping[str, Any]], dict[int, Mapping[str, Any]]]:
    details = {int(row["audit_item_id"]): row for row in store.list_stage_work(token, stage="DETAIL")}
    models = {int(row["audit_item_id"]): row for row in store.list_stage_work(token, stage="MODEL")}
    return details, models


def _model_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return tuple(
        normalize_cell_str(row.get(name))
        for name in ("queryStr", "skuName", "modelAuditResult", "isMatch")
    )  # type: ignore[return-value]


def _detail_failure(error_text: object) -> dict[str, Any]:
    return {
        "customerPrice": None,
        "customerPriceStatus": "DETAIL_REQUEST_FAILED",
        "customerPriceSource": "",
        "customerPriceWarning": f"增量详情阶段失败：{normalize_cell_str(error_text) or 'ExternalError'}",
        "customerPriceEvidence": "",
    }


def _crawl_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        normalize_cell_str(row.get("vCommodityId") or row.get("documentId")),
        normalize_cell_str(row.get("skuId")),
        normalize_cell_str(row.get("extLink")),
    )


def _valid_crawl_price(result: Mapping[str, Any]) -> bool:
    status = normalize_cell_str(result.get("page_price_status"))
    try:
        value = Decimal(str(result.get("page_price_value")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (status == "OK" and value > 0) or (status == "PRICE_IS_ZERO" and value == 0)


def build_frozen_audit_artifact(
    store: DailyAuditStore,
    token: LeaseToken,
    *,
    output_identity: str,
) -> FrozenArtifactBuild:
    run = store.get_run(token.business_date)
    if run is None or int(run["id"]) != token.run_id or run["frozen_snapshot_id"] is None:
        raise FinalizeBlocked("daily run has no frozen authoritative snapshot")
    snapshot = store.get_snapshot(int(run["frozen_snapshot_id"]))
    if snapshot is None:
        raise FinalizeBlocked("frozen snapshot metadata is missing")
    headers = _decode_json(snapshot.get("headers_json"), list, "headers")
    evidence = _decode_json(snapshot.get("evidence_json"), dict, "evidence")
    if not headers:
        raise FinalizeBlocked("frozen snapshot did not persist legacy headers")

    items = store.ordered_snapshot_items(int(snapshot["id"]))
    details, models = _stage_maps(store, token)
    crawls_by_key: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for crawl in store.list_crawl_work(token):
        key = (
            normalize_cell_str(crawl.get("source_id")),
            normalize_cell_str(crawl.get("sku_id")),
            normalize_cell_str(crawl.get("exact_url")),
        )
        crawls_by_key.setdefault(key, []).append(crawl)

    counts = {
        "frozen_rows": len(items),
        "detail_pending": 0,
        "detail_terminal": 0,
        "model_pending": 0,
        "model_terminal": 0,
        "crawl_pending": 0,
        "crawl_terminal": 0,
        "crawl_unplanned": 0,
        "crawl_invalid_price": 0,
    }
    rows: list[dict[str, Any]] = []
    enriched: list[dict[str, Any]] = []
    model_results: dict[tuple[str, str, str, str], TitleMatchModelResult] = {}

    for item in items:
        item_id = int(item["id"])
        row = dict(item["row"])
        row["__source_fingerprint"] = item["source_fingerprint"]
        detail = details.get(item_id)
        if detail is None or detail["state"] in {"PENDING", "CLAIMED", "RETRYABLE_FAILED"}:
            counts["detail_pending"] += 1
            info = {}
        elif detail["state"] == "TERMINAL_FAILED":
            counts["detail_terminal"] += 1
            info = _detail_failure(detail.get("error_text"))
        elif detail["state"] == "SUCCESS":
            info = _decode_json(detail.get("result_json"), dict, "detail result")
            if "customerPrice" in info:
                info = {**info, "customerPrice": parse_decimal(info.get("customerPrice"))}
        else:
            counts["detail_pending"] += 1
            info = {}
        enriched.append(info)

        model = models.get(item_id)
        if model is None or model["state"] in {"PENDING", "CLAIMED", "RETRYABLE_FAILED"}:
            counts["model_pending"] += 1
        elif model["state"] == "TERMINAL_FAILED":
            counts["model_terminal"] += 1
        elif model["state"] == "SUCCESS":
            parsed = TitleMatchModelResult.model_validate(
                _decode_json(model.get("result_json"), dict, "model result")
            )
            key = _model_key(row)
            previous = model_results.get(key)
            if previous is not None and previous != parsed:
                raise FinalizeBlocked("one model fingerprint produced conflicting persisted results")
            model_results[key] = parsed
        else:
            counts["model_pending"] += 1

        crawl_candidates = crawls_by_key.get(_crawl_key(row), [])
        successful = [crawl for crawl in crawl_candidates if crawl["state"] == "SUCCESS"]
        required = bool(row.get("__crawl_required", bool(normalize_cell_str(row.get("extLink")))))
        if successful:
            selected = max(successful, key=lambda value: int(value["id"]))
            crawl_result = _decode_json(selected.get("result_json"), dict, "crawl result")
            if _valid_crawl_price(crawl_result):
                row["__page_price_value"] = parse_decimal(crawl_result.get("page_price_value"))
                row["__page_price_status"] = crawl_result.get("page_price_status")
                row["__page_price_source"] = crawl_result.get("page_price_source")
            elif required:
                counts["crawl_invalid_price"] += 1
        elif required:
            if not crawl_candidates:
                counts["crawl_unplanned"] += 1
            elif any(crawl["state"] in {"PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "RETRYABLE_FAILED", "SUBMIT_UNKNOWN"} for crawl in crawl_candidates):
                counts["crawl_pending"] += 1
            else:
                counts["crawl_terminal"] += 1
        rows.append(row)

    readiness = FinalizeReadiness(**counts)
    if not readiness.ready:
        return FrozenArtifactBuild(readiness, None)

    records = build_records(rows, enriched)
    audit_response = audit_saas_approval_records(
        records,
        use_model=True,
        use_link_audit=True,
        use_crawl_result=False,
        precomputed_title_model_results=model_results,
    )
    artifact = FrozenAuditArtifact(
        business_date=token.business_date,
        tenant_id=str(run["tenant_id"]),
        commodity_pool_id=str(run["pool_id"]),
        output_identity=output_identity,
        frozen_snapshot_id=str(snapshot["id"]),
        frozen_snapshot_hash=str(snapshot["ordered_hash"]),
        frozen_snapshot_evidence={
            "observed_at": snapshot["observed_at"],
            "row_count": snapshot["row_count"],
            "source": evidence,
        },
        headers=headers,
        frozen_rows=rows,
        enriched_rows=enriched,
        audit_response=audit_response,
    )
    return FrozenArtifactBuild(readiness, artifact)


def materialize_frozen_daily_run(
    store: DailyAuditStore,
    process_lock: ProcessLock,
    business_date: str,
    output_path: Path | str,
    *,
    materializer: LegacyWorkbookMaterializer | None = None,
) -> FinalizePublication:
    try:
        process_lock.acquire()
    except LeaseUnavailable as exc:
        raise FinalizeAlreadyRunning("another tick/finalize owns the process lock") from exc
    token: LeaseToken | None = None
    try:
        run = store.get_run(business_date)
        if run is None or run["frozen_snapshot_id"] is None:
            raise FinalizeBlocked("daily run has no frozen authoritative snapshot")
        token = store.acquire_lease(
            business_date,
            f"finalize-{os.getpid()}",
            ttl_seconds=600,
            allow_unexpired_takeover=True,
            process_lock=process_lock,
        )
        built = build_frozen_audit_artifact(
            store,
            token,
            output_identity=f"daily-final-{business_date.replace('-', '')}",
        )
        if not built.readiness.ready or built.artifact is None:
            raise FinalizeBlocked("frozen snapshot still has mandatory pending or invalid work", built.readiness)
        publisher = materializer or LegacyWorkbookMaterializer()
        resolved_output = Path(output_path).resolve()
        store.prepare_finalization(token, str(resolved_output))
        result = publisher.materialize(built.artifact, resolved_output)
        store.mark_finalized(token, str(resolved_output), result.manifest_sha256)
        store.update_daily_metrics(token, {"finalized_rows": result.row_count})
        return FinalizePublication(built.readiness, result)
    finally:
        if token is not None:
            store.release_lease(token)
        process_lock.release()
