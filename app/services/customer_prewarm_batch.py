"""Thin customer-prewarm facade over the shared Zhaoshang Batch lifecycle."""

from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.services.batch_sql_result_adapter import BatchSqlResultAdapter
from app.services.batch_zhaoshang_adapter import (
    CONTRACT_VERSION,
    CnncBatchZhaoshangAdapter,
)
from app.services.incremental_audit_runtime import (
    PROJECT_DATA_ROOT,
    _batch_sql_runtime_components,
    batch_zhaoshang_bridge,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
)
from app.services.daily_audit_store import (
    DailyAuditStore,
    LeaseToken,
    ProcessLock,
)
from app.services.batch_zhaoshang_priority import (
    BatchMaterializationTarget,
    BatchPriorityAttempt,
    connect_batch_priority_from_env_file,
    ensure_batch_priority,
)
from app.services.remote_diagnostics import sanitize_remote_diagnostic

PURPOSE_CUSTOMER_PREWARM = "customer-prewarm"
BATCH_BACKEND_VERSION = "batch-url-v1"


def is_customer_prewarm_batch_payload(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("contract_version") == CONTRACT_VERSION
        and value.get("purpose") == PURPOSE_CUSTOMER_PREWARM
    )


def is_customer_prewarm_batch_job(job: Mapping[str, Any]) -> bool:
    import json

    raw = job.get("payload_json") or "{}"
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return False
    return is_customer_prewarm_batch_payload(payload)


@dataclass(frozen=True)
class CustomerPrewarmBatchFacade:
    propose: Callable[[list[Mapping[str, Any]], float], list[Any]]
    finalize: Callable[[Any, tuple[Any, ...], float], Any]
    submit: Callable[[Mapping[str, Any], list[Mapping[str, Any]], float], Mapping[str, Any]]
    harvest: Callable[[list[Mapping[str, Any]], float], Mapping[int, Mapping[str, Any]]]


def apply_customer_prewarm_batch_evidence(
    store: DailyAuditStore,
    token: LeaseToken,
    job: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> str:
    """Persist one shared Batch bridge result without weakening its evidence."""

    job_id = int(job["id"])
    if evidence.get("manual_error"):
        store.mark_remote_manual(token, job_id, str(evidence["manual_error"]))
        return "MANUAL_REQUIRED"

    crawl_results = evidence.get("crawl_results")
    manual_crawls: list[tuple[int, str]] = []
    if isinstance(crawl_results, Mapping):
        for raw_crawl_id, crawl_result in crawl_results.items():
            crawl_id = int(raw_crawl_id)
            if isinstance(crawl_result, Mapping) and crawl_result.get("manual"):
                manual_crawls.append(
                    (crawl_id, str(crawl_result.get("error") or "ambiguous batch result"))
                )
            elif isinstance(crawl_result, Mapping) and crawl_result.get("no_result"):
                store.complete_partial_exact_no_result(
                    token,
                    crawl_id,
                    remote_job_id=job_id,
                    remote_identity=str(evidence.get("remote_identity") or ""),
                    result_path=str(job.get("result_path") or ""),
                    reason=str(crawl_result.get("reason") or "no exact batch result"),
                )
            elif isinstance(crawl_result, Mapping) and crawl_result.get("terminal"):
                store.fail_crawl_work(
                    token,
                    crawl_id,
                    str(crawl_result.get("error") or "terminal crawler result"),
                    retryable=False,
                )
            else:
                store.complete_crawl_work(token, crawl_id, crawl_result)

    state = str(evidence.get("state") or job.get("state") or "RUNNING")
    identity = str(evidence.get("remote_identity") or job.get("remote_identity") or "")
    original_state = str(job.get("state") or "")
    atomic = False
    if original_state == "SUBMIT_UNKNOWN" and state == "PRIORITY_PENDING":
        store.reconcile_batch_remote_job_and_crawl_work(
            token,
            job_id,
            remote_identity=identity,
            result=evidence.get("result"),
        )
        atomic = True
    elif original_state == "PRIORITY_PENDING" and state == "SUBMITTED":
        store.confirm_batch_remote_job_priority_and_crawl_work(
            token,
            job_id,
            remote_identity=identity,
            result=evidence.get("result"),
        )
        atomic = True
    elif identity:
        store.reconcile_remote_job(
            token,
            job_id,
            remote_identity=identity,
            state=state,
            result=evidence.get("result"),
        )

    if manual_crawls:
        store.mark_remote_manual(token, job_id, "harvest has unresolved provenance mappings")
        for crawl_id, error in manual_crawls:
            store.mark_partial_exact_crawl_manual(
                token,
                crawl_id,
                remote_job_id=job_id,
                remote_identity=identity,
                error=error,
            )
        return "MANUAL_REQUIRED"
    if not atomic and state != "PRIORITY_PENDING":
        store.advance_crawl_work_for_job(token, job_id, state)
    return state


def run_customer_prewarm_batch_job_step(
    store: DailyAuditStore,
    token: LeaseToken,
    facade: CustomerPrewarmBatchFacade,
    job_id: int,
    *,
    timeout_seconds: float,
    pre_remote_guard: Callable[[], bool] | None = None,
    result_commit_fence: Callable[[], Any] | None = None,
) -> tuple[str, Mapping[str, Any]]:
    job = store.get_remote_job(int(job_id))
    if job is None or not is_customer_prewarm_batch_job(job):
        raise ValueError("exact customer-prewarm Batch job is unavailable")
    original_state = str(job.get("state") or "")
    phase = "priority" if original_state == "PRIORITY_PENDING" else "harvest"
    claim = store.claim_harvest(
        token,
        int(job_id),
        max_attempts=120,
        max_elapsed_seconds=21600.0,
    )
    if claim is None:
        current = store.get_remote_job(int(job_id)) or job
        return str(current.get("state") or "MANUAL_REQUIRED"), {
            "manual_error": str(current.get("error_text") or "Batch retry limit reached")
        }
    if pre_remote_guard is not None and not pre_remote_guard():
        store.release_harvest_claim(token, int(job_id), phase=phase)
        return original_state, {"commit_discarded": True}
    enriched = {**dict(claim), "crawl_work": store.crawl_work_for_job(int(job_id))}
    started = time.monotonic()
    try:
        results = facade.harvest([enriched], max(0.001, timeout_seconds))
    except Exception as exc:
        store.record_harvest(
            token,
            int(job_id),
            elapsed_seconds=max(0.0, time.monotonic() - started),
            max_attempts=120,
            max_elapsed_seconds=21600.0,
            error=f"{type(exc).__name__}: {exc}",
            phase=phase,
        )
        raise
    evidence = results.get(int(job_id)) or results.get(str(int(job_id)))
    if not isinstance(evidence, Mapping):
        store.record_harvest(
            token,
            int(job_id),
            elapsed_seconds=max(0.0, time.monotonic() - started),
            max_attempts=120,
            max_elapsed_seconds=21600.0,
            error="customer-prewarm Batch harvest returned no exact evidence",
            phase=phase,
        )
        raise RuntimeError("customer-prewarm Batch harvest returned no exact evidence")
    if str(evidence.get("reconcile_status") or "") == "NOT_FOUND":
        state = original_state
    else:
        commit_fence = (
            result_commit_fence()
            if result_commit_fence is not None
            else nullcontext(True)
        )
        with commit_fence as commit_allowed:
            if not commit_allowed:
                store.record_harvest(
                    token,
                    int(job_id),
                    elapsed_seconds=max(0.0, time.monotonic() - started),
                    max_attempts=120,
                    max_elapsed_seconds=21600.0,
                    error="candidate linkage changed before Batch state commit",
                    phase=phase,
                )
                return original_state, {**dict(evidence), "commit_discarded": True}
            state = apply_customer_prewarm_batch_evidence(store, token, claim, evidence)
    store.record_harvest(
        token,
        int(job_id),
        elapsed_seconds=max(0.0, time.monotonic() - started),
        max_attempts=120,
        max_elapsed_seconds=21600.0,
        error=(
            str(evidence.get("retryable_error"))
            if evidence.get("retryable_error")
            else None
        ),
        phase=phase,
        phase_succeeded=(
            (original_state == "SUBMIT_UNKNOWN" and state == "PRIORITY_PENDING")
            or (original_state == "PRIORITY_PENDING" and state == "SUBMITTED")
        ),
    )
    return state, evidence


def harvest_existing_customer_prewarm_batch_job(
    *,
    state_path: str | Path,
    lock_path: str | Path,
    business_date: str,
    remote_job_id: int,
    facade: CustomerPrewarmBatchFacade,
    timeout_seconds: float,
    pre_remote_guard: Callable[[], bool] | None = None,
    result_commit_fence: Callable[[], Any] | None = None,
    reconcile_dead_threshold: int | None = None,
) -> dict[str, Any]:
    """Harvest one exact durable Batch job without any create capability."""

    lock = ProcessLock(lock_path)
    lock.acquire()
    store = DailyAuditStore(state_path)
    token: LeaseToken | None = None
    try:
        token = store.acquire_lease(
            business_date,
            "customer_prewarm_batch_harvest",
            max(60.0, float(timeout_seconds) + 30.0),
            allow_unexpired_takeover=True,
            process_lock=lock,
        )
        job = store.get_remote_job(int(remote_job_id))
        if job is None or int(job.get("run_id") or 0) != token.run_id:
            raise ValueError("exact customer-prewarm Batch job is unavailable")
        if not is_customer_prewarm_batch_job(job):
            raise ValueError("durable job is not customer-prewarm Batch")
        threshold = reconcile_dead_threshold
        if threshold is None:
            from app.services.customer_prewarm_lifecycle import RECONCILE_DEAD_THRESHOLD

            threshold = RECONCILE_DEAD_THRESHOLD
        threshold = max(1, int(threshold))

        def response(outcome: str, *, remote_calls: int) -> dict[str, Any]:
            return {
                "outcome": outcome,
                "business_date": business_date,
                "remote_job_id": int(remote_job_id),
                "remote_identity": str(job.get("remote_identity") or ""),
                "remote_calls": int(remote_calls),
                "add_calls": 0,
            }

        if pre_remote_guard is not None and not pre_remote_guard():
            return response("REMOTE_EXISTING_HARVEST_DEFERRED", remote_calls=0)
        state = str(job.get("state") or "")
        if state == "SUBMIT_INTENT" and int(job.get("post_attempted") or 0) == 1:
            store.mark_submit_unknown(
                token,
                int(remote_job_id),
                "manual cross-day recovery after interrupted Batch POST claim",
            )
            job = store.get_remote_job(int(remote_job_id)) or job
            state = str(job.get("state") or "")
        if state == "SUBMIT_UNKNOWN" and int(job.get("recovery_attempts") or 0) >= threshold:
            return response("REMOTE_EXISTING_RECONCILE_DEAD", remote_calls=0)
        phase = "priority" if state == "PRIORITY_PENDING" else "harvest"
        claim = store.claim_harvest(
            token,
            int(remote_job_id),
            max_attempts=120,
            max_elapsed_seconds=21600.0,
        )
        if claim is None:
            raise ValueError("customer-prewarm Batch job could not enter harvest")
        if pre_remote_guard is not None and not pre_remote_guard():
            store.release_harvest_claim(token, int(remote_job_id), phase=phase)
            return response("REMOTE_EXISTING_HARVEST_DEFERRED", remote_calls=0)
        started = time.monotonic()
        try:
            evidence_map = facade.harvest(
                [{**dict(claim), "crawl_work": store.crawl_work_for_job(int(remote_job_id))}],
                max(0.001, float(timeout_seconds)),
            )
        except Exception as exc:
            store.record_harvest(
                token,
                int(remote_job_id),
                elapsed_seconds=max(0.0, time.monotonic() - started),
                max_attempts=120,
                max_elapsed_seconds=21600.0,
                error=f"{type(exc).__name__}: {exc}",
                phase=phase,
            )
            raise
        evidence = evidence_map.get(int(remote_job_id)) or evidence_map.get(
            str(int(remote_job_id))
        )
        if not isinstance(evidence, Mapping):
            store.record_harvest(
                token,
                int(remote_job_id),
                elapsed_seconds=max(0.0, time.monotonic() - started),
                max_attempts=120,
                max_elapsed_seconds=21600.0,
                error="customer-prewarm Batch harvest returned no exact evidence",
                phase=phase,
            )
            raise RuntimeError("customer-prewarm Batch harvest returned no exact evidence")
        commit_fence = (
            result_commit_fence()
            if result_commit_fence is not None
            else nullcontext(True)
        )
        reconcile_not_found = str(evidence.get("reconcile_status") or "") == "NOT_FOUND"
        reconcile_outcome: str | None = None
        with commit_fence as commit_allowed:
            if not commit_allowed:
                store.record_harvest(
                    token,
                    int(remote_job_id),
                    elapsed_seconds=max(0.0, time.monotonic() - started),
                    max_attempts=120,
                    max_elapsed_seconds=21600.0,
                    error="candidate linkage changed before manual Batch commit",
                    phase=phase,
                )
                return response("REMOTE_EXISTING_HARVEST_DEFERRED", remote_calls=1)
            if reconcile_not_found:
                diagnostic = sanitize_remote_diagnostic(
                    evidence.get("reconcile_diagnostic") or ""
                )
                if diagnostic:
                    store.record_submit_reconcile_diagnostic(
                        token,
                        int(remote_job_id),
                        status="NOT_FOUND",
                        diagnostic=diagnostic,
                    )
                    reconcile_outcome = "REMOTE_EXISTING_RECONCILE_INCONCLUSIVE"
                else:
                    new_attempts = store.bump_remote_job_recovery_attempt(
                        token,
                        int(remote_job_id),
                        diagnostic="exact Batch remote task not found",
                    )
                    reconcile_outcome = (
                        "REMOTE_EXISTING_RECONCILE_DEAD"
                        if new_attempts >= threshold
                        else "REMOTE_EXISTING_RECONCILE_NOT_FOUND"
                    )
                new_state = state
            else:
                new_state = apply_customer_prewarm_batch_evidence(
                    store, token, claim, evidence
                )
        retryable = (
            str(evidence.get("retryable_error"))
            if evidence.get("retryable_error")
            else None
        )
        store.record_harvest(
            token,
            int(remote_job_id),
            elapsed_seconds=max(0.0, time.monotonic() - started),
            max_attempts=120,
            max_elapsed_seconds=21600.0,
            error=retryable,
            phase=phase,
            phase_succeeded=(
                (state == "SUBMIT_UNKNOWN" and new_state == "PRIORITY_PENDING")
                or (state == "PRIORITY_PENDING" and new_state == "SUBMITTED")
            ),
        )
        if reconcile_outcome is not None:
            result = response(reconcile_outcome, remote_calls=1)
            result["remote_identity"] = str(
                evidence.get("remote_identity") or job.get("remote_identity") or ""
            )
            return result
        if new_state == "COMPLETED":
            outcome = "REMOTE_EXISTING_HARVEST_COMPLETED"
        elif new_state == "MANUAL_REQUIRED":
            outcome = "REMOTE_EXISTING_HARVEST_MANUAL"
        else:
            outcome = "REMOTE_EXISTING_HARVEST_RUNNING"
        result = response(outcome, remote_calls=1)
        result["remote_identity"] = str(
            evidence.get("remote_identity") or job.get("remote_identity") or ""
        )
        return result
    finally:
        if token is not None:
            store.release_lease(token)
        lock.release()


def build_customer_prewarm_batch_facade(
    *,
    paths: Any,
    business_date: str,
    mappings: Sequence[Any],
    write_enabled: bool,
    priority_enabled: bool | None = None,
    adapter: CnncBatchZhaoshangAdapter | None = None,
    priority_runner: Callable[[Any, float], BatchPriorityAttempt] | None = None,
    result_adapter: BatchSqlResultAdapter | None = None,
    materialization_probe_runner: Callable[[Any, float], Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> CustomerPrewarmBatchFacade:
    """Build the shared Batch bridge with prewarm-specific request resolution."""

    batch_adapter = adapter or CnncBatchZhaoshangAdapter(write_enabled=write_enabled)
    legacy_adapter = CnncLegacyZhaoshangAdapter(mappings=mappings)

    effective_priority_enabled = (
        write_enabled if priority_enabled is None else bool(priority_enabled)
    )
    if priority_runner is None and effective_priority_enabled:
        def priority_runner(prepared_job: Any, timeout_seconds: float) -> BatchPriorityAttempt:
            return ensure_batch_priority(
                lambda remaining: connect_batch_priority_from_env_file(
                    PROJECT_DATA_ROOT / "ZHAOSHANG_DB.env",
                    timeout_seconds=remaining,
                ),
                BatchMaterializationTarget(
                    execute_numbers=prepared_job.execute_numbers,
                    expected_mall=prepared_job.mall_key,
                ),
                timeout_seconds=timeout_seconds,
            )

    if result_adapter is None or materialization_probe_runner is None:
        default_result_adapter, default_probe = _batch_sql_runtime_components(
            monotonic=monotonic
        )
        result_adapter = result_adapter or default_result_adapter
        materialization_probe_runner = materialization_probe_runner or default_probe

    def request_resolver(row: Mapping[str, Any]) -> CrawlRequest | None:
        request = row.get("request")
        return request if isinstance(request, CrawlRequest) else None

    def source_id_resolver(row: Mapping[str, Any]) -> str:
        return str(row.get("source_id") or "").strip()

    callbacks = batch_zhaoshang_bridge(
        batch_adapter,
        legacy_adapter,
        paths=paths,
        business_date=business_date,
        priority_runner=priority_runner,
        result_adapter=result_adapter,
        materialization_probe_runner=materialization_probe_runner,
        request_resolver=request_resolver,
        source_id_resolver=source_id_resolver,
        purpose=PURPOSE_CUSTOMER_PREWARM,
        monotonic=monotonic,
    )
    return CustomerPrewarmBatchFacade(*callbacks)
