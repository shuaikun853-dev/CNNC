"""Explicit remote harvest recovery operations against existing exports and tasks."""

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



def run_local_remote_harvest_recovery(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    remote_job_id: int,
    remote_identity: str,
    adapter: CnncLegacyZhaoshangAdapter,
) -> dict[str, Any]:
    """Backfill one manual remote job from an existing validated export only."""
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    expected_identity = str(remote_identity or "").strip()
    if not expected_identity:
        raise RecoveryRejected("exact remote identity is required")

    store = DailyAuditStore(path)
    run = store.get_run(business_date)
    if run is None:
        raise RecoveryRejected("remote recovery requires the existing daily run")
    process_lock = ProcessLock(lock_path)
    token = _acquire_recovery_lease(
        store,
        process_lock,
        business_date,
        f"recover-remote-{time.time_ns()}",
        300.0,
    )
    try:
        job = store.get_remote_job(int(remote_job_id))
        if job is None or int(job["run_id"]) != token.run_id:
            raise RecoveryRejected(f"remote job id does not exist in this run: {remote_job_id}")
        if job["state"] != "MANUAL_REQUIRED":
            raise RecoveryRejected(
                f"remote job id {remote_job_id} is not MANUAL_REQUIRED: {job['state']}"
            )
        if not int(job["post_attempted"]):
            raise RecoveryRejected("manual remote recovery requires durable POST evidence")
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
            raise RecoveryRejected("local recovery requires every existing durable file")
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
        outcome = adapter.harvest_existing(prepared)
        if outcome.status is not HarvestStatus.EXPORTED or not outcome.reused_existing:
            raise RecoveryRejected(outcome.diagnostic or "existing export is invalid")

        result_map = dict(outcome.result_map or {})
        result_by_identity = dict(outcome.result_by_identity or {})
        no_result_identities = set(outcome.no_result_identities)
        manual_identities = dict(outcome.manual_identities or {})
        crawls = store.crawl_work_for_job(int(remote_job_id))
        if not crawls:
            raise RecoveryRejected("manual remote job has no linked crawl identities")
        snapshot_id = run.get("frozen_snapshot_id")
        if snapshot_id is None:
            raise RecoveryRejected("remote recovery requires the frozen snapshot")
        frozen_sources = {
            str(item["row"].get("vCommodityId") or "").strip()
            for item in store.ordered_snapshot_items(int(snapshot_id))
        }
        validate_linked_crawls_against_remote_payload(
            operation="remote harvest recovery",
            requests=prepared.requests,
            crawls=crawls,
            frozen_sources=frozen_sources,
            remote_job_id=int(remote_job_id),
            run_id=token.run_id,
        )
        sku_count_by_url: dict[str, set[str]] = {}
        for request in prepared.requests:
            sku_count_by_url.setdefault(request.url.strip(), set()).add(request.sku_id)
        crawl_results: dict[int, Mapping[str, Any]] = {}
        for crawl in crawls:
            if crawl["state"] in {"NO_RESULT", "TERMINAL_FAILED"}:
                raise RecoveryRejected("terminal crawl evidence cannot be overwritten")
            sku_id = str(crawl["sku_id"] or "").strip()
            exact_url = str(crawl["exact_url"] or "").strip()
            value = result_by_identity.get((sku_id, exact_url))
            if value is None and len(sku_count_by_url.get(exact_url, set())) == 1:
                value = result_map.get(exact_url)
            if value is None:
                raise RecoveryRejected(
                    f"existing export has no exact result for crawl_work id {crawl['id']}"
                )
            crawl_results[int(crawl["id"])] = value

        # Every local validation finishes before the first business-state
        # mutation. This transition can never dispatch a remote operation.
        store.requeue_manual_harvest_job(
            token,
            int(remote_job_id),
            remote_identity=expected_identity,
        )
        claim = store.claim_harvest(
            token,
            int(remote_job_id),
            max_attempts=int(job["harvest_attempts"]) + 1,
            max_elapsed_seconds=max(float(job["harvest_elapsed_seconds"]) + 60.0, 60.0),
        )
        if claim is None:
            raise RecoveryRejected("manual remote job could not enter harvestable state")
        for crawl_id, result in crawl_results.items():
            store.complete_crawl_work(token, crawl_id, result)
        store.reconcile_remote_job(
            token,
            int(remote_job_id),
            remote_identity=expected_identity,
            state="COMPLETED",
            export_path=str(prepared.result_path),
            result=result_map,
        )
        store.record_harvest(
            token,
            int(remote_job_id),
            elapsed_seconds=0,
            max_attempts=int(job["harvest_attempts"]) + 1,
            max_elapsed_seconds=max(float(job["harvest_elapsed_seconds"]) + 60.0, 60.0),
        )
        return {
            "outcome": "REMOTE_HARVEST_RECOVERY_COMPLETED",
            "business_date": business_date,
            "remote_job_id": int(remote_job_id),
            "remote_identity": expected_identity,
            "crawl_completed": len(crawl_results),
            "reused_existing": True,
            "remote_calls": 0,
            **dict(outcome.validation_summary or {}),
        }
    finally:
        store.release_lease(token)
        process_lock.release()


def run_partial_exact_harvest_recovery(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    remote_job_id: int,
    remote_identity: str,
    adapter: CnncLegacyZhaoshangAdapter,
) -> dict[str, Any]:
    """Persist only deterministic identities from one existing partial export."""
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    expected_identity = str(remote_identity or "").strip()
    identity_parts = expected_identity.split("|")
    if len(identity_parts) != 3 or not all(identity_parts):
        raise RecoveryRejected("exact remote identity is required")

    store = DailyAuditStore(path)
    run = store.get_run(business_date)
    if run is None:
        raise RecoveryRejected("partial harvest requires the existing daily run")
    process_lock = ProcessLock(lock_path)
    token = _acquire_recovery_lease(
        store,
        process_lock,
        business_date,
        f"partial-exact-harvest-{time.time_ns()}",
        300.0,
    )
    try:
        job = store.get_remote_job(int(remote_job_id))
        if job is None or int(job["run_id"]) != token.run_id:
            raise RecoveryRejected(
                f"remote job id does not exist in this run: {remote_job_id}"
            )
        if job["state"] not in {"MANUAL_REQUIRED", "COMPLETED"}:
            raise RecoveryRejected(
                f"remote job id {remote_job_id} is not manual/completed: {job['state']}"
            )
        if not int(job["post_attempted"]):
            raise RecoveryRejected("partial harvest requires durable POST evidence")
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
            raise RecoveryRejected("partial harvest requires every existing durable file")
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
        if adapter.exact_export_remote_identity(prepared) != expected_identity:
            raise RecoveryRejected("exact EXPORTED evidence is required")
        try:
            classification = adapter.classify_existing_export_partial(prepared)
        except ValueError as exc:
            raise RecoveryRejected(str(exc)) from exc

        request_by_backend: dict[tuple[str, str, str, str], CrawlRequest] = {}
        result_key_to_backend: dict[tuple[str, str], tuple[str, str, str, str]] = {}
        for request in prepared.requests:
            backend_key = (
                request.mall_key,
                request.sku_id,
                request.url.strip(),
                request.backend_version,
            )
            if backend_key in request_by_backend:
                raise RecoveryRejected("immutable remote payload has duplicate backend identities")
            result_key = (request.sku_id, request.url.strip())
            if result_key in result_key_to_backend:
                raise RecoveryRejected("remote payload result identity is backend-ambiguous")
            request_by_backend[backend_key] = request
            result_key_to_backend[result_key] = backend_key

        crawls = store.crawl_work_for_job(int(remote_job_id))
        if not crawls:
            raise RecoveryRejected("partial harvest job has no linked crawl identities")
        crawl_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        full_identities: set[tuple[str, str, str, str, str]] = set()
        for crawl in crawls:
            source_id = str(crawl.get("source_id") or "").strip()
            backend_key = (
                str(crawl["mall_key"]),
                str(crawl["sku_id"]),
                str(crawl["exact_url"]).strip(),
                str(crawl["backend_version"]),
            )
            full_identity = (
                backend_key[0],
                source_id,
                backend_key[1],
                backend_key[2],
                backend_key[3],
            )
            if not source_id or full_identity in full_identities:
                raise RecoveryRejected("linked full durable crawl identities are invalid")
            full_identities.add(full_identity)
            if backend_key not in request_by_backend:
                raise RecoveryRejected("linked crawl identity is outside the immutable payload")
            crawl_groups.setdefault(backend_key, []).append(crawl)
        if set(crawl_groups) != set(request_by_backend):
            raise RecoveryRejected("immutable payload and linked crawl identities are incomplete")

        # All global evidence and the complete classification are validated
        # before the first mutation. Row writes are intentionally independent
        # so an interrupted recovery can resume without rewriting terminals.
        written_success = 0
        written_no_result = 0
        written_manual = 0
        skipped_terminal = 0
        exact_result_by_url: dict[str, Mapping[str, Any]] = {}
        for backend_key, grouped_crawls in crawl_groups.items():
            result_key = (backend_key[1], backend_key[2])
            for crawl in grouped_crawls:
                crawl_id = int(crawl["id"])
                current_state = str(crawl["state"])
                if current_state in {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"}:
                    skipped_terminal += 1
                    continue
                if result_key in classification.exact_matches:
                    result = classification.exact_matches[result_key]
                    store.complete_partial_exact_crawl_work(
                        token,
                        crawl_id,
                        remote_job_id=int(remote_job_id),
                        remote_identity=expected_identity,
                        result=result,
                    )
                    exact_result_by_url[backend_key[2]] = result
                    written_success += current_state != "SUCCESS"
                elif result_key in classification.exact_absent:
                    store.complete_partial_exact_no_result(
                        token,
                        crawl_id,
                        remote_job_id=int(remote_job_id),
                        remote_identity=expected_identity,
                        result_path=str(prepared.result_path),
                    )
                    written_no_result += current_state != "NO_RESULT"
                else:
                    store.mark_partial_exact_crawl_manual(
                        token,
                        crawl_id,
                        remote_job_id=int(remote_job_id),
                        remote_identity=expected_identity,
                        error=classification.ambiguous.get(
                            result_key, "partial export identity is ambiguous"
                        ),
                    )
                    written_manual += current_state != "MANUAL_REQUIRED"

        final_job = store.finish_partial_exact_harvest_job(
            token,
            int(remote_job_id),
            remote_identity=expected_identity,
            export_path=str(prepared.result_path),
            result=exact_result_by_url,
        )
        final_crawls = store.crawl_work_for_job(int(remote_job_id))
        states = Counter(str(crawl["state"]) for crawl in final_crawls)
        manual_ids = [
            int(crawl["id"])
            for crawl in final_crawls
            if crawl["state"] not in {"SUCCESS", "NO_RESULT"}
        ]
        result = {
            "outcome": (
                "PARTIAL_EXACT_HARVEST_COMPLETED"
                if final_job["state"] == "COMPLETED"
                else "PARTIAL_EXACT_HARVEST_MANUAL"
            ),
            "business_date": business_date,
            "remote_job_id": int(remote_job_id),
            "remote_identity": expected_identity,
            "expected": len(prepared.requests),
            "crawl_success": states.get("SUCCESS", 0),
            "crawl_no_result": states.get("NO_RESULT", 0),
            "crawl_manual": len(manual_ids),
            "written_success": written_success,
            "written_no_result": written_no_result,
            "written_manual": written_manual,
            "skipped_terminal": skipped_terminal,
            "manual_crawl_work_ids": manual_ids,
            "remote_state": str(final_job["state"]),
            "remote_calls": 0,
            "add_calls": 0,
            **dict(classification.validation_summary),
        }
        return result
    finally:
        store.release_lease(token)
        process_lock.release()


def run_existing_remote_harvest(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    remote_job_id: int,
    remote_identity: str,
    adapter: CnncLegacyZhaoshangAdapter,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Wait/export and backfill one exact existing remote job without create."""
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    expected_identity = str(remote_identity or "").strip()
    identity_parts = expected_identity.split("|")
    if len(identity_parts) != 3 or not all(identity_parts):
        raise RecoveryRejected("exact remote identity is required")
    if timeout_seconds <= 0:
        raise RecoveryRejected("harvest timeout must be positive")

    store = DailyAuditStore(path)
    run = store.get_run(business_date)
    if run is None or run.get("frozen_snapshot_id") is None:
        raise RecoveryRejected("existing harvest requires the frozen daily run")
    frozen_sources = {
        str(item["row"].get("vCommodityId") or item["row"].get("documentId") or "").strip()
        for item in store.ordered_snapshot_items(int(run["frozen_snapshot_id"]))
    }
    process_lock = ProcessLock(lock_path)
    token = _acquire_recovery_lease(
        store,
        process_lock,
        business_date,
        f"harvest-existing-{time.time_ns()}",
        max(300.0, timeout_seconds + 60.0),
    )
    try:
        job = store.get_remote_job(int(remote_job_id))
        if job is None or int(job["run_id"]) != token.run_id:
            raise RecoveryRejected(
                f"remote job id does not exist in this run: {remote_job_id}"
            )
        if job["state"] not in {"SUBMITTED", "RUNNING"}:
            raise RecoveryRejected(
                f"remote job id {remote_job_id} is not harvestable: {job['state']}"
            )
        if not int(job["post_attempted"]):
            raise RecoveryRejected("existing harvest requires durable POST evidence")
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
            raise RecoveryRejected("existing harvest requires every durable local path")
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
            operation="existing harvest",
            requests=prepared.requests,
            crawls=crawls,
            frozen_sources=frozen_sources,
            remote_job_id=int(remote_job_id),
            run_id=token.run_id,
        )
        if any(crawl["state"] in {"NO_RESULT", "TERMINAL_FAILED"} for crawl in crawls):
            raise RecoveryRejected("existing harvest cannot overwrite terminal crawl evidence")

        max_attempts = int(job["harvest_attempts"]) + 1
        max_elapsed_seconds = (
            float(job["harvest_elapsed_seconds"]) + timeout_seconds + 60.0
        )
        claim = store.claim_harvest(
            token,
            int(remote_job_id),
            max_attempts=max_attempts,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        if claim is None:
            raise RecoveryRejected("existing remote job could not enter harvest")
        started = time.monotonic()
        outcome = adapter.harvest(prepared, timeout_seconds=timeout_seconds)
        elapsed_seconds = max(0.0, time.monotonic() - started)
        if outcome.status is HarvestStatus.RUNNING:
            store.reconcile_remote_job(
                token,
                int(remote_job_id),
                remote_identity=expected_identity,
                state="RUNNING",
            )
            store.advance_crawl_work_for_job(token, int(remote_job_id), "RUNNING")
            store.record_harvest(
                token,
                int(remote_job_id),
                elapsed_seconds=elapsed_seconds,
                max_attempts=max_attempts,
                max_elapsed_seconds=max_elapsed_seconds,
                error=outcome.diagnostic,
            )
            return {
                "outcome": "REMOTE_EXISTING_HARVEST_RUNNING",
                "business_date": business_date,
                "remote_job_id": int(remote_job_id),
                "remote_identity": expected_identity,
                "expected": len(prepared.requests),
                "remote_calls": 1,
                "add_calls": 0,
            }
        if outcome.status is not HarvestStatus.EXPORTED:
            diagnostic = outcome.diagnostic or "legacy wait-export-only requires manual review"
            store.mark_remote_manual(token, int(remote_job_id), diagnostic)
            store.record_harvest(
                token,
                int(remote_job_id),
                elapsed_seconds=elapsed_seconds,
                max_attempts=max_attempts,
                max_elapsed_seconds=max_elapsed_seconds,
                error=diagnostic,
            )
            return {
                "outcome": "REMOTE_EXISTING_HARVEST_MANUAL",
                "business_date": business_date,
                "remote_job_id": int(remote_job_id),
                "remote_identity": expected_identity,
                "expected": len(prepared.requests),
                "error": diagnostic,
                "remote_calls": 1,
                "add_calls": 0,
            }
        if adapter.exact_export_remote_identity(prepared) != expected_identity:
            diagnostic = "exact EXPORTED evidence does not match durable remote identity"
            store.mark_remote_manual(token, int(remote_job_id), diagnostic)
            store.record_harvest(
                token,
                int(remote_job_id),
                elapsed_seconds=elapsed_seconds,
                max_attempts=max_attempts,
                max_elapsed_seconds=max_elapsed_seconds,
                error=diagnostic,
            )
            return {
                "outcome": "REMOTE_EXISTING_HARVEST_MANUAL",
                "business_date": business_date,
                "remote_job_id": int(remote_job_id),
                "remote_identity": expected_identity,
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

        for crawl_id, result in matched.items():
            store.complete_crawl_work(token, crawl_id, result)
        for crawl_id in no_result_ids:
            store.complete_partial_exact_no_result(
                token,
                crawl_id,
                remote_job_id=int(remote_job_id),
                remote_identity=expected_identity,
                result_path=str(prepared.result_path),
                reason="provenance-valid remote request has no result row",
            )
        remote_state = "EXPORTED" if manual else "COMPLETED"
        store.reconcile_remote_job(
            token,
            int(remote_job_id),
            remote_identity=expected_identity,
            state=remote_state,
            export_path=str(prepared.result_path),
            result=result_map,
        )
        if manual:
            store.mark_remote_manual(
                token,
                int(remote_job_id),
                "harvest has unresolved provenance mappings",
            )
            for crawl_id, error in manual:
                store.mark_partial_exact_crawl_manual(
                    token,
                    crawl_id,
                    remote_job_id=int(remote_job_id),
                    remote_identity=expected_identity,
                    error=error,
                )
        store.record_harvest(
            token,
            int(remote_job_id),
            elapsed_seconds=elapsed_seconds,
            max_attempts=max_attempts,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        statuses = [str(value.get("page_price_status") or "") for value in matched.values()]
        return {
            "outcome": (
                "REMOTE_EXISTING_HARVEST_MANUAL"
                if manual
                else "REMOTE_EXISTING_HARVEST_COMPLETED"
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
