"""Explicit MODEL stage recovery operations."""

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
from app.services.recovery.common import RecoveryRejected, _acquire_recovery_lease



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
