"""Explicit wait-export recovery for manual legacy jobs."""

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
from app.services.recovery.common import (
    RecoveryRejected,
    _acquire_recovery_lease,
)
from app.services.recovery.validation import (
    validate_linked_crawls_against_remote_payload,
)



WAIT_EXPORT_MANUAL_DIAGNOSTIC = "legacy wait-export-only requires manual review"
# A capped wait that only proved the remote task is still executing strands the
# job with the RUNNING diagnostic; the wait-export recovery exists exactly to
# give such genuinely still-running remotes another bounded wait, so both
# diagnostics it can itself persist stay admissible entry points.
WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC = "legacy wait-export-only is still running"
WAIT_EXPORT_RECOVERY_ACCEPTED_ERRORS = frozenset(
    {WAIT_EXPORT_MANUAL_DIAGNOSTIC, WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC}
)


def run_remote_wait_export_recovery(
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
    """Harvest one MANUAL wait-export legacy job from its original remote task.

    The only remote call is the legacy wait-export-only subprocess against the
    durable task identity: no create, no enable, no priority change.  The
    caller must restate the exact persisted manual diagnostic so this channel
    can never generalize to other MANUAL causes, and a local result workbook
    must not already exist (that case belongs to recover-remote-harvest).
    """
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    expected_identity = str(remote_identity or "").strip()
    identity_parts = expected_identity.split("|")
    if len(identity_parts) != 3 or not all(identity_parts):
        raise RecoveryRejected("exact remote identity is required")
    if str(expected_error_text or "").strip() not in WAIT_EXPORT_RECOVERY_ACCEPTED_ERRORS:
        raise RecoveryRejected("wait-export recovery requires the exact legacy wait-export manual diagnostic")
    if timeout_seconds <= 0:
        raise RecoveryRejected("wait-export recovery timeout must be positive")

    store = DailyAuditStore(path)
    run = store.get_run(business_date)
    if run is None or run.get("frozen_snapshot_id") is None:
        raise RecoveryRejected("wait-export recovery requires the frozen daily run")
    frozen_sources = {
        str(item["row"].get("vCommodityId") or item["row"].get("documentId") or "").strip()
        for item in store.ordered_snapshot_items(int(run["frozen_snapshot_id"]))
    }
    process_lock = ProcessLock(lock_path)
    token = _acquire_recovery_lease(
        store,
        process_lock,
        business_date,
        f"recover-wait-export-{time.time_ns()}",
        max(300.0, timeout_seconds + 60.0),
    )
    try:
        job = store.get_remote_job(int(remote_job_id))
        if job is None or int(job["run_id"]) != token.run_id:
            raise RecoveryRejected(f"remote job id does not exist in this run: {remote_job_id}")
        if job["state"] != "MANUAL_REQUIRED":
            raise RecoveryRejected(f"remote job id {remote_job_id} is not MANUAL_REQUIRED: {job['state']}")
        if str(job.get("error_text") or "").strip() not in WAIT_EXPORT_RECOVERY_ACCEPTED_ERRORS:
            raise RecoveryRejected("remote job manual error is not the wait-export diagnostic")
        if not int(job["post_attempted"]):
            raise RecoveryRejected("wait-export recovery requires durable POST evidence")
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
        if not batch_state.is_file() or not input_path.is_file() or not result_path.parent.is_dir():
            raise RecoveryRejected("wait-export recovery requires every durable local path")
        if result_path.exists():
            raise RecoveryRejected("wait-export recovery must not overwrite an existing result workbook")
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
            operation="wait-export recovery",
            requests=prepared.requests,
            crawls=crawls,
            frozen_sources=frozen_sources,
            remote_job_id=int(remote_job_id),
            run_id=token.run_id,
        )
        if any(crawl["state"] in {"NO_RESULT", "TERMINAL_FAILED"} for crawl in crawls):
            raise RecoveryRejected("wait-export recovery cannot overwrite terminal crawl evidence")

        # Every local validation finishes before the first mutation. The
        # requeue returns the confirmed task to the harvestable state; it
        # never dispatches a remote operation. One extra attempt of headroom
        # keeps a genuinely still-running remote recoverable instead of
        # cap-flipping straight back to MANUAL_REQUIRED.
        store.requeue_manual_harvest_job(
            token,
            int(remote_job_id),
            remote_identity=expected_identity,
        )
        max_attempts = int(job["harvest_attempts"]) + 2
        max_elapsed_seconds = (
            float(job["harvest_elapsed_seconds"]) + timeout_seconds + 120.0
        )
        claim = store.claim_harvest(
            token,
            int(remote_job_id),
            max_attempts=max_attempts,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        if claim is None:
            raise RecoveryRejected("wait-export remote job could not enter harvest")
        started = time.monotonic()
        outcome = adapter.harvest(prepared, timeout_seconds=timeout_seconds)
        elapsed_seconds = max(0.0, time.monotonic() - started)
        if outcome.status is HarvestStatus.RUNNING:
            store.close_harvest_still_running(
                token,
                int(remote_job_id),
                remote_identity=expected_identity,
                diagnostic=outcome.diagnostic,
                elapsed_seconds=elapsed_seconds,
                max_attempts=max_attempts,
                max_elapsed_seconds=max_elapsed_seconds,
            )
            return {
                "outcome": "REMOTE_WAIT_EXPORT_RECOVERY_RUNNING",
                "business_date": business_date,
                "remote_job_id": int(remote_job_id),
                "remote_identity": expected_identity,
                "task_number": identity_parts[2],
                "expected": len(prepared.requests),
                "remote_calls": 1,
                "add_calls": 0,
            }
        if outcome.status is not HarvestStatus.EXPORTED:
            diagnostic = outcome.diagnostic or WAIT_EXPORT_MANUAL_DIAGNOSTIC
            store.close_harvest_manual(
                token,
                int(remote_job_id),
                diagnostic=diagnostic,
                elapsed_seconds=elapsed_seconds,
                max_attempts=max_attempts,
                max_elapsed_seconds=max_elapsed_seconds,
            )
            return {
                "outcome": "REMOTE_WAIT_EXPORT_RECOVERY_MANUAL",
                "business_date": business_date,
                "remote_job_id": int(remote_job_id),
                "remote_identity": expected_identity,
                "task_number": identity_parts[2],
                "expected": len(prepared.requests),
                "error": diagnostic,
                "remote_calls": 1,
                "add_calls": 0,
            }
        if adapter.exact_export_remote_identity(prepared) != expected_identity:
            diagnostic = "exact EXPORTED evidence does not match durable remote identity"
            store.close_harvest_manual(
                token,
                int(remote_job_id),
                diagnostic=diagnostic,
                elapsed_seconds=elapsed_seconds,
                max_attempts=max_attempts,
                max_elapsed_seconds=max_elapsed_seconds,
            )
            return {
                "outcome": "REMOTE_WAIT_EXPORT_RECOVERY_MANUAL",
                "business_date": business_date,
                "remote_job_id": int(remote_job_id),
                "remote_identity": expected_identity,
                "task_number": identity_parts[2],
                "expected": len(prepared.requests),
                "error": diagnostic,
                "remote_calls": 1,
                "add_calls": 0,
            }

        result_map = dict(outcome.result_map or {})
        result_by_identity = dict(outcome.result_by_identity or {})
        no_result_identities = set(outcome.no_result_identities)
        manual_identities = dict(outcome.manual_identities or {})
        sku_count_by_url: dict[str, set[str]] = {}
        for request in prepared.requests:
            sku_count_by_url.setdefault(request.url.strip(), set()).add(request.sku_id)
        matched: dict[int, Mapping[str, Any]] = {}
        no_result_ids: list[int] = []
        manual: list[tuple[int, str]] = []
        for crawl in crawls:
            sku_id = str(crawl["sku_id"] or "").strip()
            exact_url = str(crawl["exact_url"] or "").strip()
            value = result_by_identity.get((sku_id, exact_url))
            if value is None and len(sku_count_by_url.get(exact_url, set())) == 1:
                value = result_map.get(exact_url)
            identity = (sku_id, exact_url)
            if value is not None:
                matched[int(crawl["id"])] = value
            elif identity in no_result_identities:
                no_result_ids.append(int(crawl["id"]))
            else:
                manual.append(
                    (
                        int(crawl["id"]),
                        manual_identities.get(
                            identity,
                            "provenance-valid row lacks durable request-group mapping",
                        ),
                    )
                )

        store.close_harvest_exported(
            token,
            int(remote_job_id),
            remote_identity=expected_identity,
            matched=matched,
            no_result_crawl_ids=no_result_ids,
            manual_crawls=manual,
            export_path=str(prepared.result_path),
            result=result_map,
            elapsed_seconds=elapsed_seconds,
            max_attempts=max_attempts,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        statuses = [str(value.get("page_price_status") or "") for value in matched.values()]
        return {
            "outcome": (
                "REMOTE_WAIT_EXPORT_RECOVERY_MANUAL"
                if manual
                else "REMOTE_WAIT_EXPORT_RECOVERY_COMPLETED"
            ),
            "business_date": business_date,
            "remote_job_id": int(remote_job_id),
            "remote_identity": expected_identity,
            "task_number": identity_parts[2],
            "expected": len(prepared.requests),
            "downloaded": len(result_by_identity),
            "validated": len(matched),
            "positive_price": statuses.count("OK"),
            "real_zero": statuses.count("PRICE_IS_ZERO"),
            "price_invalid": len(matched) - statuses.count("OK") - statuses.count("PRICE_IS_ZERO"),
            "missing_crawl_work_ids": [crawl_id for crawl_id, _error in manual],
            "no_result": len(no_result_ids),
            "remote_calls": 1,
            "add_calls": 0,
        }
    finally:
        store.release_lease(token)
        process_lock.release()
