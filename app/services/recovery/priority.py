"""Explicit remote priority recovery operations for existing jobs."""

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



# Precisely classified REMOTE_NOT_MATERIALIZED evidence plus the bare legacy
# type name persisted before classification existed. The bare name is admitted
# only because the zero-rows recovery re-derives the classification from a live
# probe of the exact task before any durable state transition.
ZERO_ROWS_PRIORITY_RECOVERY_DIAGNOSTICS = frozenset(
    REMOTE_NOT_MATERIALIZED_DIAGNOSTICS | {REMOTE_NOT_MATERIALIZED_LEGACY_DIAGNOSTIC}
)


def run_existing_remote_priority_recovery(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    remote_job_id: int,
    remote_identity: str,
    expected_current_state: str = "PRIORITY_PENDING",
    expected_result_state: str | None = None,
    expected_error_text: str | None = None,
    adapter: CnncLegacyZhaoshangAdapter,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Raise one exact existing remote job to verified highest priority without create."""
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    expected_identity = str(remote_identity or "").strip()
    identity_parts = expected_identity.split("|")
    if len(identity_parts) != 3 or not all(identity_parts):
        raise RecoveryRejected("exact remote identity is required")
    if timeout_seconds <= 0:
        raise RecoveryRejected("priority recovery timeout must be positive")
    expected_current_state = str(expected_current_state or "").strip()
    if expected_current_state not in {"PRIORITY_PENDING", "MANUAL_REQUIRED"}:
        raise RecoveryRejected("priority recovery expected state is invalid")
    if expected_current_state == "MANUAL_REQUIRED" and (
        expected_result_state != "PRIORITY_PENDING"
        or not is_retryable_priority_error_diagnostic(expected_error_text)
    ):
        raise RecoveryRejected(
            "manual priority recovery requires exact retryable priority evidence"
        )

    store = DailyAuditStore(path)
    run = store.get_run(business_date)
    if run is None or run.get("frozen_snapshot_id") is None:
        raise RecoveryRejected("priority recovery requires the frozen daily run")
    frozen_sources = {
        str(item["row"].get("vCommodityId") or item["row"].get("documentId") or "").strip()
        for item in store.ordered_snapshot_items(int(run["frozen_snapshot_id"]))
    }
    process_lock = ProcessLock(lock_path)
    token = _acquire_recovery_lease(
        store,
        process_lock,
        business_date,
        f"recover-priority-{time.time_ns()}",
        max(300.0, timeout_seconds + 60.0),
    )
    try:
        job = store.get_remote_job(int(remote_job_id))
        if job is None or int(job["run_id"]) != token.run_id:
            raise RecoveryRejected(
                f"remote job id does not exist in this run: {remote_job_id}"
            )
        if job["state"] != expected_current_state:
            raise RecoveryRejected(
                f"remote job id {remote_job_id} state changed: {job['state']}"
            )
        if not int(job["post_attempted"]):
            raise RecoveryRejected("priority recovery requires durable POST evidence")
        if str(job.get("remote_identity") or "").strip() != expected_identity:
            raise RecoveryRejected("remote identity does not exactly match durable state")
        if expected_current_state == "MANUAL_REQUIRED":
            try:
                prior_result = json.loads(str(job.get("result_json") or ""))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RecoveryRejected(
                    "manual priority recovery result evidence is unreadable"
                ) from exc
            if (
                not isinstance(prior_result, Mapping)
                or prior_result.get("state") != expected_result_state
            ):
                raise RecoveryRejected(
                    "manual priority recovery result state changed"
                )
            current_error = str(job.get("error_text") or "").strip()
            if (
                current_error != str(expected_error_text or "").strip()
                or not is_retryable_priority_error_diagnostic(current_error)
            ):
                raise RecoveryRejected(
                    "manual priority recovery error evidence changed"
                )

        try:
            payload = json.loads(str(job.get("payload_json") or ""))
            requests = tuple(CrawlRequest(**dict(row)) for row in payload["requests"])
            batch_state = Path(str(payload["batch_state"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RecoveryRejected("durable legacy payload cannot be reconstructed") from exc
        input_path = Path(str(job.get("input_path") or ""))
        result_path = Path(str(job.get("result_path") or ""))
        if not batch_state.is_file() or not input_path.is_file() or not result_path.parent.is_dir():
            raise RecoveryRejected("priority recovery requires every durable local path")
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

        crawls = store.crawl_work_for_job(int(remote_job_id))
        validate_linked_crawls_against_remote_payload(
            operation="priority recovery",
            requests=prepared.requests,
            crawls=crawls,
            frozen_sources=frozen_sources,
            remote_job_id=int(remote_job_id),
            run_id=token.run_id,
        )

        priority = adapter.prioritize(
            prepared,
            task_id=identity_parts[1],
            task_number=identity_parts[2],
            timeout_seconds=timeout_seconds,
            retry_unconfirmed=True,
        )
        if priority.status not in {
            PriorityStatus.UPDATED,
            PriorityStatus.ALREADY_HIGHEST,
        } or priority.weight_type != -1:
            raise RecoveryRejected(
                priority.diagnostic or "highest priority was not exactly confirmed"
            )

        if expected_current_state == "MANUAL_REQUIRED":
            store.requeue_manual_priority_job(
                token,
                int(remote_job_id),
                remote_identity=expected_identity,
            )

        store.reconcile_remote_job(
            token,
            int(remote_job_id),
            remote_identity=expected_identity,
            state="SUBMITTED",
            result={
                "priority_status": priority.status.value,
                "weight_type": -1,
            },
        )
        store.advance_crawl_work_for_job(token, int(remote_job_id), "SUBMITTED")
        expected = len(prepared.requests)
        return {
            "outcome": "REMOTE_PRIORITY_RECOVERY_COMPLETED",
            "business_date": business_date,
            "remote_job_id": int(remote_job_id),
            "remote_identity": expected_identity,
            "expected": expected,
            "matched": expected,
            "weight_type_minus_one": expected,
            "weight_type_zero": 0,
            "weight_type_other": 0,
            "priority_calls": 1,
            "add_calls": 0,
        }
    finally:
        store.release_lease(token)
        process_lock.release()


def run_existing_remote_priority_zero_rows_recovery(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    remote_job_id: int,
    remote_identity: str,
    expected_error_text: str,
    adapter: CnncLegacyZhaoshangAdapter,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Requeue one MANUAL zero-rows remote job after the remote re-materialized it.

    This never re-creates the Zhaoshang task. It re-checks the exact durable
    identity (mall|taskId|taskNumber) against ``s_task_info`` and only when the
    live probe proves the full expected subtask set -- and every other probe
    outcome stays fail-closed -- requeues MANUAL_REQUIRED to the normal
    priority/harvest path.
    """
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    expected_identity = str(remote_identity or "").strip()
    identity_parts = expected_identity.split("|")
    if len(identity_parts) != 3 or not all(identity_parts):
        raise RecoveryRejected("exact remote identity is required")
    if timeout_seconds <= 0:
        raise RecoveryRejected("zero-rows priority recovery timeout must be positive")
    accepted_error = str(expected_error_text or "").strip()
    if accepted_error not in ZERO_ROWS_PRIORITY_RECOVERY_DIAGNOSTICS:
        raise RecoveryRejected(
            "zero-rows priority recovery requires an exact REMOTE_NOT_MATERIALIZED error"
        )

    store = DailyAuditStore(path)
    run = store.get_run(business_date)
    if run is None or run.get("frozen_snapshot_id") is None:
        raise RecoveryRejected("zero-rows priority recovery requires the frozen daily run")
    frozen_sources = {
        str(item["row"].get("vCommodityId") or item["row"].get("documentId") or "").strip()
        for item in store.ordered_snapshot_items(int(run["frozen_snapshot_id"]))
    }
    process_lock = ProcessLock(lock_path)
    token = _acquire_recovery_lease(
        store,
        process_lock,
        business_date,
        f"recover-priority-zero-rows-{time.time_ns()}",
        max(300.0, timeout_seconds + 60.0),
    )
    try:
        job = store.get_remote_job(int(remote_job_id))
        if job is None or int(job["run_id"]) != token.run_id:
            raise RecoveryRejected(
                f"remote job id does not exist in this run: {remote_job_id}"
            )
        if job["state"] != "MANUAL_REQUIRED":
            raise RecoveryRejected(
                f"remote job id {remote_job_id} state changed: {job['state']}"
            )
        if not int(job["post_attempted"]):
            raise RecoveryRejected("zero-rows priority recovery requires durable POST evidence")
        if str(job.get("remote_identity") or "").strip() != expected_identity:
            raise RecoveryRejected("remote identity does not exactly match durable state")
        current_error = str(job.get("error_text") or "").strip()
        if current_error != accepted_error:
            raise RecoveryRejected("zero-rows priority recovery error evidence changed")

        try:
            payload = json.loads(str(job.get("payload_json") or ""))
            requests = tuple(CrawlRequest(**dict(row)) for row in payload["requests"])
            batch_state = Path(str(payload["batch_state"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RecoveryRejected("durable legacy payload cannot be reconstructed") from exc
        input_path = Path(str(job.get("input_path") or ""))
        result_path = Path(str(job.get("result_path") or ""))
        if not batch_state.is_file() or not input_path.is_file() or not result_path.parent.is_dir():
            raise RecoveryRejected("zero-rows priority recovery requires every durable local path")
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

        crawls = store.crawl_work_for_job(int(remote_job_id))
        validate_linked_crawls_against_remote_payload(
            operation="zero-rows priority recovery",
            requests=prepared.requests,
            crawls=crawls,
            frozen_sources=frozen_sources,
            remote_job_id=int(remote_job_id),
            run_id=token.run_id,
        )

        # Live probe of the exact already-created task, never a new /add. The
        # historical bare "PriorityUpdateZeroRowsError" evidence cannot by
        # itself distinguish zero-rows from verification failures, so the probe
        # re-derives the classification before any durable transition.
        priority = adapter.prioritize(
            prepared,
            task_id=identity_parts[1],
            task_number=identity_parts[2],
            timeout_seconds=timeout_seconds,
            retry_unconfirmed=True,
        )
        if priority.status not in {
            PriorityStatus.UPDATED,
            PriorityStatus.ALREADY_HIGHEST,
        } or priority.weight_type != -1:
            probe_diagnostic = str(priority.diagnostic or "")
            if probe_diagnostic in REMOTE_NOT_MATERIALIZED_DIAGNOSTICS:
                raise RecoveryRejected(
                    "remote subtask rows are still not materialized; retry after remote recovery"
                )
            raise RecoveryRejected(
                probe_diagnostic or "highest priority was not exactly confirmed"
            )

        store.requeue_manual_priority_job(
            token,
            int(remote_job_id),
            remote_identity=expected_identity,
        )
        store.reconcile_remote_job(
            token,
            int(remote_job_id),
            remote_identity=expected_identity,
            state="SUBMITTED",
            result={
                "priority_status": priority.status.value,
                "weight_type": -1,
            },
        )
        store.advance_crawl_work_for_job(token, int(remote_job_id), "SUBMITTED")
        expected = len(prepared.requests)
        return {
            "outcome": "REMOTE_PRIORITY_ZERO_ROWS_RECOVERY_COMPLETED",
            "business_date": business_date,
            "remote_job_id": int(remote_job_id),
            "remote_identity": expected_identity,
            "expected": expected,
            "matched": expected,
            "weight_type_minus_one": expected,
            "weight_type_zero": 0,
            "weight_type_other": 0,
            "priority_calls": 1,
            "add_calls": 0,
        }
    finally:
        store.release_lease(token)
        process_lock.release()
