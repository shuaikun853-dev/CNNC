"""Explicit, narrowly scoped recovery operations for one durable audit run."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.services.daily_audit_store import DailyAuditStore, LeaseToken, ProcessLock
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    ExportNoPriceProbe,
    HarvestStatus,
    PriorityStatus,
    REMOTE_NOT_MATERIALIZED_DIAGNOSTICS,
    REMOTE_NOT_MATERIALIZED_LEGACY_DIAGNOSTIC,
    is_retryable_priority_error_diagnostic,
)
from app.services.saas_audit_core import model_fingerprint
from app.services.recovery.wait_export import (
    WAIT_EXPORT_MANUAL_DIAGNOSTIC,
    WAIT_EXPORT_RECOVERY_ACCEPTED_ERRORS,
    WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC,
    run_remote_wait_export_recovery,
)
from app.services.recovery.priority import (
    ZERO_ROWS_PRIORITY_RECOVERY_DIAGNOSTICS,
    run_existing_remote_priority_recovery,
    run_existing_remote_priority_zero_rows_recovery,
)
from app.services.recovery.harvest import (
    run_existing_remote_harvest,
    run_local_remote_harvest_recovery,
    run_partial_exact_harvest_recovery,
)
from app.services.recovery.model import (
    requeue_model_transient_failures,
    release_model_recovery,
    run_model_recovery,
)
from app.services.recovery.validation import (
    validate_linked_crawls_against_remote_payload,
)
from app.services.recovery.common import (
    BackendIdentity,
    RecoveryRejected,
    _acquire_recovery_lease,
)


# Precisely classified REMOTE_NOT_MATERIALIZED evidence plus the bare legacy
# type name persisted before classification existed. The bare name is admitted
# only because the zero-rows recovery re-derives the classification from a live
# probe of the exact task before any durable state transition.
def resolve_exported_no_result(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    crawl_work_id: int,
    remote_job_id: int,
    remote_identity: str,
    adapter: CnncLegacyZhaoshangAdapter,
) -> dict[str, Any]:
    """Resolve one exact terminal crawl omitted from a completed legacy export."""
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    expected_identity = str(remote_identity or "").strip()
    if not expected_identity:
        raise RecoveryRejected("exact remote identity is required")

    store = DailyAuditStore(path)
    run = store.get_run(business_date)
    if run is None or run.get("frozen_snapshot_id") is None:
        raise RecoveryRejected("no-result recovery requires the frozen daily run")
    frozen_sources = {
        str(item["row"].get("vCommodityId") or item["row"].get("documentId") or "").strip()
        for item in store.ordered_snapshot_items(int(run["frozen_snapshot_id"]))
    }

    process_lock = ProcessLock(lock_path)
    token = _acquire_recovery_lease(
        store,
        process_lock,
        business_date,
        f"resolve-no-result-{time.time_ns()}",
        300.0,
    )
    try:
        target = store.get_crawl_work(int(crawl_work_id))
        if target is None or int(target["run_id"]) != token.run_id:
            raise RecoveryRejected(
                f"crawl_work id does not exist in this run: {crawl_work_id}"
            )
        if target["state"] != "TERMINAL_FAILED":
            raise RecoveryRejected(
                f"crawl_work id {crawl_work_id} is not TERMINAL_FAILED: {target['state']}"
            )
        if int(target.get("remote_job_id") or 0) != int(remote_job_id):
            raise RecoveryRejected("crawl work is not linked to the exact remote job")
        if str(target.get("source_id") or "").strip() not in frozen_sources:
            raise RecoveryRejected("crawl work is outside the frozen snapshot")

        job = store.get_remote_job(int(remote_job_id))
        if job is None or int(job["run_id"]) != token.run_id:
            raise RecoveryRejected(
                f"remote job id does not exist in this run: {remote_job_id}"
            )
        if (
            job["state"] != "COMPLETED"
            or not int(job["post_attempted"])
            or job.get("result_json") is None
        ):
            raise RecoveryRejected(
                "no-result recovery requires a completed exported remote job"
            )
        if str(job.get("remote_identity") or "").strip() != expected_identity:
            raise RecoveryRejected("remote identity does not exactly match durable state")

        try:
            payload = json.loads(str(job.get("payload_json") or ""))
            requests = tuple(CrawlRequest(**dict(row)) for row in payload["requests"])
            batch_state = Path(str(payload["batch_state"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RecoveryRejected("durable legacy payload cannot be reconstructed") from exc
        input_path = Path(str(job.get("input_path") or ""))
        result_path = Path(str(job.get("result_path") or ""))
        if not batch_state.is_file() or not input_path.is_file() or not result_path.is_file():
            raise RecoveryRejected("no-result recovery requires every existing durable file")
        prepared = adapter.prepare_job(
            business_date,
            requests,
            input_path,
            result_path,
            str(job["task_name"]),
            batch_state,
        )
        if (
            prepared.logical_job_key != str(job["logical_key"])
            or prepared.payload_sha256 != str(job["payload_hash"])
            or prepared.mall_key != str(job.get("mall_key") or "")
        ):
            raise RecoveryRejected("durable legacy job evidence is inconsistent")
        if adapter.exact_export_remote_identity(prepared) != expected_identity:
            raise RecoveryRejected(
                "exact EXPORTED evidence must match the durable remote identity"
            )

        crawls = store.crawl_work_for_job(int(remote_job_id))
        crawl_groups = validate_linked_crawls_against_remote_payload(
            operation="no-result recovery",
            requests=prepared.requests,
            crawls=crawls,
            frozen_sources=frozen_sources,
            remote_job_id=int(remote_job_id),
            run_id=token.run_id,
        )
        target_key = (
            str(target["mall_key"]),
            str(target["sku_id"]),
            str(target["exact_url"]).strip(),
            str(target["backend_version"]),
        )
        if int(crawl_work_id) not in {
            int(crawl["id"]) for crawl in crawl_groups.get(target_key, ())
        }:
            raise RecoveryRejected("target crawl identity is not unique in the remote job")
        other_states = {
            int(crawl["id"]): str(crawl["state"])
            for crawl in crawls
            if int(crawl["id"]) != int(crawl_work_id)
        }
        if any(state not in {"SUCCESS", "NO_RESULT"} for state in other_states.values()):
            raise RecoveryRejected(
                "all other exported crawl identities must already be terminal successes"
            )
        successful_keys = {
            identity
            for identity, grouped in crawl_groups.items()
            if any(crawl["state"] == "SUCCESS" for crawl in grouped)
        }
        successful_requests = tuple(
            request
            for request in prepared.requests
            if (
                request.mall_key,
                request.sku_id,
                request.url.strip(),
                request.backend_version,
            )
            in successful_keys
        )
        try:
            observed = adapter.validate_existing_export_exact_requests(
                prepared,
                successful_requests,
            )
        except Exception as exc:
            raise RecoveryRejected(
                "existing export does not uniquely prove the exact crawl omission"
            ) from exc
        if set(observed) != {
            (request.sku_id, request.url.strip()) for request in successful_requests
        }:
            raise RecoveryRejected(
                "existing export does not exactly preserve every successful sibling identity"
            )
        if (str(target["sku_id"]), str(target["exact_url"]).strip()) in observed:
            raise RecoveryRejected("existing export contains a real row for the target crawl")

        reason = str(target.get("error_text") or "").strip()
        if not reason:
            raise RecoveryRejected("terminal crawl has no persisted failure reason")
        resolved = store.complete_crawl_work_without_result(
            token,
            int(crawl_work_id),
            reason=reason,
        )
        return {
            "outcome": "CRAWL_NO_RESULT_RESOLVED",
            "business_date": business_date,
            "frozen_snapshot_id": int(run["frozen_snapshot_id"]),
            "crawl_work_id": int(crawl_work_id),
            "remote_job_id": int(remote_job_id),
            "remote_identity": expected_identity,
            "state": resolved["state"],
            "page_price_status": "NO_RESULT",
            "page_price_value": None,
            "remote_calls": 0,
        }
    finally:
        store.release_lease(token)
        process_lock.release()


# job with the RUNNING diagnostic; the wait-export recovery exists exactly to
# give such genuinely still-running remotes another bounded wait, so both
# diagnostics it can itself persist stay admissible entry points.
# The only export-stage evidence that may legally end a crawl without a
# workbook: the remote task finished and the export endpoint itself answered
# with the exact no-price response and a zero count.
EXPORT_NO_PRICE_RSP_DESC = "未检索到商品价格信息"
EXPORT_NO_PRICE_TASK_FINISHED_DESC = "本次任务执行完毕"
EXPORT_NO_PRICE_NO_RESULT_LIMIT = 10
EXPORT_NO_PRICE_CRAWL_REASON = "remote task finished and export endpoint reported no price rows"
WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC = "legacy wait-export-only did not produce an export"
EXPORT_NO_PRICE_ACCEPTED_ERRORS = frozenset(
    {WAIT_EXPORT_MANUAL_DIAGNOSTIC, WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC}
)


def run_remote_export_no_price_resolution(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    remote_job_id: int,
    remote_identity: str,
    expected_error_text: str,
    adapter: CnncLegacyZhaoshangAdapter,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Resolve one MANUAL wait-export job as terminal export-stage no-price.

    The only admissible shape is: the exact remote task already finished, the
    live export endpoint answered the exact no-price response with a zero
    count, and no local result workbook exists.  Every weaker shape -- still
    running, partial progress, failures, transient export errors, rows in the
    export, an existing workbook, or a threshold breach -- stays fail-closed.
    """
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    expected_identity = str(remote_identity or "").strip()
    identity_parts = expected_identity.split("|")
    if len(identity_parts) != 3 or not all(identity_parts):
        raise RecoveryRejected("exact remote identity is required")
    if str(expected_error_text or "").strip() not in EXPORT_NO_PRICE_ACCEPTED_ERRORS:
        raise RecoveryRejected(
            "export no-price resolution requires the exact legacy wait-export manual diagnostic"
        )
    if timeout_seconds <= 0:
        raise RecoveryRejected("export no-price resolution timeout must be positive")

    store = DailyAuditStore(path)
    run = store.get_run(business_date)
    if run is None or run.get("frozen_snapshot_id") is None:
        raise RecoveryRejected("export no-price resolution requires the frozen daily run")
    frozen_sources = {
        str(item["row"].get("vCommodityId") or item["row"].get("documentId") or "").strip()
        for item in store.ordered_snapshot_items(int(run["frozen_snapshot_id"]))
    }
    process_lock = ProcessLock(lock_path)
    token = _acquire_recovery_lease(
        store,
        process_lock,
        business_date,
        f"resolve-export-no-price-{time.time_ns()}",
        max(300.0, timeout_seconds + 60.0),
    )
    try:
        job = store.get_remote_job(int(remote_job_id))
        if job is None or int(job["run_id"]) != token.run_id:
            raise RecoveryRejected(f"remote job id does not exist in this run: {remote_job_id}")
        if str(job.get("remote_identity") or "").strip() != expected_identity:
            raise RecoveryRejected("remote identity does not exactly match durable state")
        if not int(job["post_attempted"]):
            raise RecoveryRejected("export no-price resolution requires durable POST evidence")

        try:
            payload = json.loads(str(job.get("payload_json") or ""))
            requests = tuple(CrawlRequest(**dict(row)) for row in payload["requests"])
            batch_state = Path(str(payload["batch_state"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RecoveryRejected("durable legacy payload cannot be reconstructed") from exc
        input_path = Path(str(job.get("input_path") or ""))
        result_path = Path(str(job.get("result_path") or ""))
        if not batch_state.is_file() or not input_path.is_file() or not result_path.parent.is_dir():
            raise RecoveryRejected("export no-price resolution requires every durable local path")
        if result_path.exists():
            raise RecoveryRejected(
                "a local result workbook already exists; use the harvest path instead"
            )
        prepared = adapter.prepare_job(
            business_date,
            requests,
            input_path,
            result_path,
            str(job["task_name"]),
            batch_state,
        )
        if (
            prepared.logical_job_key != str(job["logical_key"])
            or prepared.payload_sha256 != str(job["payload_hash"])
            or prepared.mall_key != str(job.get("mall_key") or "")
            or identity_parts[0] != prepared.mall_key
        ):
            raise RecoveryRejected("durable legacy job evidence is inconsistent")
        if adapter.exact_priority_remote_identity(prepared) != expected_identity:
            raise RecoveryRejected("exact accepted priority evidence is required")

        crawls = store.crawl_work_for_job(int(remote_job_id))
        validate_linked_crawls_against_remote_payload(
            operation="export no-price resolution",
            requests=prepared.requests,
            crawls=crawls,
            frozen_sources=frozen_sources,
            remote_job_id=int(remote_job_id),
            run_id=token.run_id,
        )
        if not crawls:
            raise RecoveryRejected("export no-price resolution requires linked crawl work")

        if job["state"] == "COMPLETED":
            try:
                completed_result = json.loads(str(job.get("result_json") or ""))
            except (TypeError, ValueError):
                completed_result = None
            already_resolved = (
                isinstance(completed_result, dict)
                and completed_result.get("evidence_type") == "EXPORT_NO_PRICE_TERMINAL"
                and all(crawl["state"] == "NO_RESULT" for crawl in crawls)
            )
            if already_resolved:
                no_result_after = len(store.list_crawl_work(token, states={"NO_RESULT"}))
                return {
                    "outcome": "REMOTE_EXPORT_NO_PRICE_ALREADY_RESOLVED",
                    "business_date": business_date,
                    "frozen_snapshot_id": int(run["frozen_snapshot_id"]),
                    "remote_job_id": int(remote_job_id),
                    "remote_identity": expected_identity,
                    "task_number": identity_parts[2],
                    "expected": len(prepared.requests),
                    "resolved": 0,
                    "resolved_crawl_work_ids": sorted(int(crawl["id"]) for crawl in crawls),
                    "no_result_before": no_result_after,
                    "no_result_after": no_result_after,
                    "no_result_limit": EXPORT_NO_PRICE_NO_RESULT_LIMIT,
                    "remote_calls": 0,
                    "add_calls": 0,
                }
            raise RecoveryRejected(
                f"remote job id {remote_job_id} is not MANUAL_REQUIRED: {job['state']}"
            )
        if job["state"] != "MANUAL_REQUIRED":
            raise RecoveryRejected(
                f"remote job id {remote_job_id} is not MANUAL_REQUIRED: {job['state']}"
            )
        if str(job.get("error_text") or "").strip() != str(expected_error_text or "").strip():
            raise RecoveryRejected("remote job manual error is not the wait-export diagnostic")
        terminal_crawls = [
            crawl for crawl in crawls
            if crawl["state"] in {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED", "MANUAL_REQUIRED"}
        ]
        if terminal_crawls:
            raise RecoveryRejected(
                "export no-price resolution cannot overwrite terminal crawl evidence"
            )

        no_result_before = len(store.list_crawl_work(token, states={"NO_RESULT"}))
        if no_result_before + len(crawls) > EXPORT_NO_PRICE_NO_RESULT_LIMIT:
            raise RecoveryRejected(
                "export no-price resolution would exceed the business no-result limit "
                f"({no_result_before}+{len(crawls)}>{EXPORT_NO_PRICE_NO_RESULT_LIMIT})"
            )

        # Live re-verification of the exact remote task; every historical
        # string is re-derived before any durable transition happens.
        try:
            probe = adapter.probe_export_no_price(
                prepared,
                identity_parts[1],
                identity_parts[2],
                timeout_seconds,
            )
        except RecoveryRejected:
            raise
        except Exception as exc:
            raise RecoveryRejected(
                f"export no-price probe did not confirm terminal evidence: {exc}"
            ) from exc
        if not isinstance(probe, ExportNoPriceProbe):
            raise RecoveryRejected("export no-price probe returned unexpected evidence")
        if EXPORT_NO_PRICE_TASK_FINISHED_DESC not in str(probe.task_status_rsp_desc):
            raise RecoveryRejected(
                "remote task status does not confirm the task finished"
            )
        progress = dict(probe.progress or {})
        expected_count = len(prepared.requests)
        progress_values = {
            key: progress.get(key)
            for key in ("total", "pending", "running", "failed", "waiting_db", "finished")
        }
        if (
            progress_values["total"] != expected_count
            or progress_values["finished"] != expected_count
            or progress_values["pending"] != 0
            or progress_values["running"] != 0
            or progress_values["failed"] != 0
            or progress_values["waiting_db"] != 0
        ):
            raise RecoveryRejected(
                f"remote task progress is not fully completed: {progress_values}"
            )
        if str(probe.export_rsp_desc) != EXPORT_NO_PRICE_RSP_DESC:
            raise RecoveryRejected(
                f"export response is not the exact no-price semantic: {probe.export_rsp_desc!r}"
            )
        if int(probe.export_count) != 0:
            raise RecoveryRejected(
                f"export response contains rows (count={probe.export_count}); use the harvest path"
            )

        resolved_crawl_ids: list[int] = []
        resolved_source_ids: list[str] = []
        for crawl in crawls:
            store.complete_crawl_work_export_no_price(
                token,
                int(crawl["id"]),
                remote_job_id=int(remote_job_id),
                remote_identity=expected_identity,
                reason=EXPORT_NO_PRICE_CRAWL_REASON,
                export_rsp_desc=str(probe.export_rsp_desc),
                export_count=int(probe.export_count),
            )
            resolved_crawl_ids.append(int(crawl["id"]))
            source_id = str(crawl.get("source_id") or "").strip()
            if source_id and source_id not in resolved_source_ids:
                resolved_source_ids.append(source_id)
        store.complete_manual_export_no_price_job(
            token,
            int(remote_job_id),
            remote_identity=expected_identity,
            result={
                "evidence_type": "EXPORT_NO_PRICE_TERMINAL",
                "remote_identity": expected_identity,
                "task_id": identity_parts[1],
                "task_number": identity_parts[2],
                "mall_key": prepared.mall_key,
                "payload_hash": str(job["payload_hash"]),
                "task_status_rspDesc": str(probe.task_status_rsp_desc),
                "progress": progress_values,
                "export_rspDesc": str(probe.export_rsp_desc),
                "export_count": int(probe.export_count),
                "resolved_crawl_work_ids": resolved_crawl_ids,
                "resolved_source_ids": resolved_source_ids,
                "no_result_before": no_result_before,
                "no_result_limit": EXPORT_NO_PRICE_NO_RESULT_LIMIT,
            },
        )
        final_crawls = store.crawl_work_for_job(int(remote_job_id))
        if not all(crawl["state"] == "NO_RESULT" for crawl in final_crawls):
            raise RecoveryRejected("resolved crawl work did not reach NO_RESULT")
        no_result_after = len(store.list_crawl_work(token, states={"NO_RESULT"}))
        if no_result_after != no_result_before + len(crawls):
            raise RecoveryRejected("no-result accounting did not advance exactly by the resolved crawls")
        return {
            "outcome": "REMOTE_EXPORT_NO_PRICE_RESOLVED",
            "business_date": business_date,
            "frozen_snapshot_id": int(run["frozen_snapshot_id"]),
            "remote_job_id": int(remote_job_id),
            "remote_identity": expected_identity,
            "task_number": identity_parts[2],
            "expected": expected_count,
            "resolved": len(resolved_crawl_ids),
            "resolved_crawl_work_ids": resolved_crawl_ids,
            "resolved_source_ids": resolved_source_ids,
            "no_result_before": no_result_before,
            "no_result_after": no_result_after,
            "no_result_limit": EXPORT_NO_PRICE_NO_RESULT_LIMIT,
            "task_status_rsp_desc": str(probe.task_status_rsp_desc),
            "progress": progress_values,
            "export_rsp_desc": str(probe.export_rsp_desc),
            "export_count": int(probe.export_count),
            "remote_calls": int(probe.remote_calls),
            "add_calls": 0,
        }
    finally:
        store.release_lease(token)
        process_lock.release()
