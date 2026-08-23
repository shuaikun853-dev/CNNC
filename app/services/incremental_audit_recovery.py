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
ZERO_ROWS_PRIORITY_RECOVERY_DIAGNOSTICS = frozenset(
    REMOTE_NOT_MATERIALIZED_DIAGNOSTICS | {REMOTE_NOT_MATERIALIZED_LEGACY_DIAGNOSTIC}
)


_TIMEOUT_EXHAUSTION = re.compile(
    r"^ModelCallRetryExhaustedError\(attempts=(?P<attempts>[1-9][0-9]*),last_cause=TimeoutError\)$"
)


def _safe_error(exc: BaseException) -> str:
    attempts = getattr(exc, "attempts", None)
    last_cause = getattr(exc, "last_cause", None)
    if isinstance(attempts, int) and hasattr(exc, "elapsed_seconds"):
        cause = type(last_cause).__name__ if isinstance(last_cause, BaseException) else "deadline"
        return f"{type(exc).__name__}(attempts={attempts},last_cause={cause})"
    return type(exc).__name__


def requeue_model_transient_failures(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    expected_count: int,
    expected_attempts: int,
) -> dict[str, Any]:
    """Requeue only the exact bounded TimeoutError terminal MODEL set.

    Attempts, elapsed time, and the original error remain durable so the next
    normal tick consumes the remaining attempt budget instead of starting over.
    """
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    if expected_count <= 0 or expected_attempts <= 0:
        raise RecoveryRejected("expected count and attempts must be positive")

    store = DailyAuditStore(path)
    process_lock = ProcessLock(lock_path)
    token: LeaseToken | None = None
    process_lock.acquire()
    try:
        run = store.get_run(business_date)
        if run is None:
            raise RecoveryRejected("transient model requeue requires the existing daily run")
        terminals = store.list_stage_work_for_business_date(
            business_date, stage="MODEL", states={"TERMINAL_FAILED"}
        )
        if len(terminals) != expected_count:
            raise RecoveryRejected(
                f"expected exactly {expected_count} terminal MODEL rows, found {len(terminals)}"
            )
        for work in terminals:
            match = _TIMEOUT_EXHAUSTION.fullmatch(str(work.get("error_text") or ""))
            if (
                match is None
                or int(match.group("attempts")) != expected_attempts
                or int(work["attempts"]) != expected_attempts
                or int(work["retryable"]) != 0
                or work.get("result_json") is not None
            ):
                raise RecoveryRejected(
                    f"stage_work id {work['id']} does not exactly match the authorized timeout evidence"
                )

        token = store.acquire_lease(
            business_date,
            f"requeue-model-transient-{time.time_ns()}",
            300.0,
            process_lock=process_lock,
        )
        stage_work_ids = [int(work["id"]) for work in terminals]
        updated = store.requeue_terminal_model_work_batch_preserving_evidence(
            token, stage_work_ids
        )
        if len(updated) != expected_count:
            raise RecoveryRejected("transient model requeue did not update the exact expected set")
        return {
            "outcome": "MODEL_TRANSIENT_REQUEUED",
            "business_date": business_date,
            "expected_count": expected_count,
            "expected_attempts": expected_attempts,
            "error_type": "ModelCallRetryExhaustedError",
            "last_cause": "TimeoutError",
            "recovered": len(updated),
            "skipped": 0,
            "failed": 0,
            "stage_work_ids": stage_work_ids,
            "model_calls": 0,
            "remote_writes": 0,
        }
    finally:
        if token is not None:
            store.release_lease(token)
        process_lock.release()


def run_model_recovery(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    stage_work_ids: Sequence[int],
    model_batch: Callable[[Sequence[Mapping[str, Any]], float], Mapping[int, Any]],
    model_name: str,
    model_prompt_version: str,
    model_schema_version: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Requeue and execute only the specified frozen MODEL stages.

    This operation has no crawl planner, remote submitter, harvester, or
    finalizer dependency, so those phases are structurally unreachable.
    """
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    ids = [int(value) for value in stage_work_ids]
    if not ids or len(ids) != len(set(ids)):
        raise RecoveryRejected("stage-work ids must be a non-empty unique list")
    if timeout_seconds <= 0:
        raise RecoveryRejected("model recovery timeout must be positive")

    store = DailyAuditStore(path)
    run = store.get_run(business_date)
    if run is None or run.get("frozen_snapshot_id") is None:
        raise RecoveryRejected("model recovery requires the existing frozen daily run")
    frozen_items = store.ordered_snapshot_items(int(run["frozen_snapshot_id"]))
    frozen_item_ids = {int(item["id"]) for item in frozen_items}
    rows_by_item = {int(item["id"]): item["row"] for item in frozen_items}

    selected: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    for work_id in ids:
        work = store.get_stage_work(work_id)
        if work is None:
            raise RecoveryRejected(f"stage_work id does not exist: {work_id}")
        if work["stage"] != "MODEL":
            raise RecoveryRejected(f"stage_work id {work_id} is not MODEL")
        if work["state"] != "TERMINAL_FAILED":
            raise RecoveryRejected(
                f"stage_work id {work_id} is not TERMINAL_FAILED: {work['state']}"
            )
        item_id = int(work["audit_item_id"])
        if item_id not in frozen_item_ids:
            raise RecoveryRejected(f"stage_work id {work_id} is outside the frozen snapshot")
        row = rows_by_item[item_id]
        expected_fingerprint = model_fingerprint(
            row, model_name, model_prompt_version, model_schema_version
        )
        if work["fingerprint"] != expected_fingerprint:
            raise RecoveryRejected(f"stage_work id {work_id} has a stale model fingerprint")
        selected.append((work, row))

    process_lock = ProcessLock(lock_path)
    token = _acquire_recovery_lease(
        store,
        process_lock,
        business_date,
        f"recover-model-{time.time_ns()}",
        max(300.0, timeout_seconds + 60.0),
    )
    claimed: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    started = time.monotonic()
    try:
        # Validation above completes before the first mutation. The fenced
        # lease then makes every selected transition explicit and durable.
        for work, row in selected:
            reset = store.requeue_terminal_stage_work(token, int(work["id"]))
            current = store.claim_stage_work(
                token,
                int(reset["id"]),
                max_attempts=1,
                max_elapsed_seconds=timeout_seconds,
            )
            if current is None:
                raise RecoveryRejected(f"stage_work id {work['id']} could not be claimed")
            claimed.append((current, row))
        store.renew_lease(token, max(60.0, timeout_seconds + 30.0))
        try:
            results = model_batch([row for _work, row in claimed], timeout_seconds)
            if set(results) != set(range(len(claimed))):
                raise ValueError("model recovery must return exactly one result per selected stage")
        except Exception as exc:
            elapsed = max(0.0, time.monotonic() - started)
            for work, _row in claimed:
                store.fail_stage_work(
                    token,
                    int(work["id"]),
                    elapsed_seconds=elapsed,
                    error=_safe_error(exc),
                    retryable=False,
                    max_attempts=1,
                    max_elapsed_seconds=timeout_seconds,
                )
            raise
        for index, (work, _row) in enumerate(claimed):
            store.complete_recovered_model_work(token, int(work["id"]), results[index])
        return {
            "outcome": "MODEL_RECOVERY_PENDING_REVIEW",
            "business_date": business_date,
            "frozen_snapshot_id": int(run["frozen_snapshot_id"]),
            "stage_work_ids": ids,
            "recovered_results": [
                {"stage_work_id": int(work["id"]), "result": results[index]}
                for index, (work, _row) in enumerate(claimed)
            ],
            "model_calls": 1,
            "remote_writes": 0,
        }
    finally:
        store.release_lease(token)
        process_lock.release()


def release_model_recovery(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    stage_work_ids: Sequence[int],
) -> dict[str, Any]:
    """Release explicitly reviewed model results to normal crawl eligibility."""
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    ids = [int(value) for value in stage_work_ids]
    if not ids or len(ids) != len(set(ids)):
        raise RecoveryRejected("stage-work ids must be a non-empty unique list")
    store = DailyAuditStore(path)
    run = store.get_run(business_date)
    if run is None or run.get("frozen_snapshot_id") is None:
        raise RecoveryRejected("model recovery release requires the frozen daily run")
    frozen_item_ids = {
        int(item["id"])
        for item in store.ordered_snapshot_items(int(run["frozen_snapshot_id"]))
    }
    for work_id in ids:
        work = store.get_stage_work(work_id)
        if work is None:
            raise RecoveryRejected(f"stage_work id does not exist: {work_id}")
        if (
            work["stage"] != "MODEL"
            or work["state"] != "RECOVERED_PENDING_REVIEW"
            or work["result_json"] is None
            or int(work["audit_item_id"]) not in frozen_item_ids
        ):
            raise RecoveryRejected(
                f"stage_work id {work_id} is not a frozen recovered MODEL result pending review"
            )
    process_lock = ProcessLock(lock_path)
    token = _acquire_recovery_lease(
        store,
        process_lock,
        business_date,
        f"release-model-{time.time_ns()}",
        300.0,
    )
    try:
        store.release_recovered_model_work_batch(token, ids)
        return {
            "outcome": "MODEL_RECOVERY_RELEASED",
            "business_date": business_date,
            "frozen_snapshot_id": int(run["frozen_snapshot_id"]),
            "stage_work_ids": ids,
            "model_calls": 0,
            "remote_calls": 0,
        }
    finally:
        store.release_lease(token)
        process_lock.release()


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
