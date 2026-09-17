"""Bounded recovery of customer-prewarm jobs owned by earlier days.

Discovery starts from durable active customer candidates and their latest
``prewarm_remote_job_links`` row.  Historical state databases are opened
read-only while planning; execution support is added below by the lifecycle
runner.  Identity-less ``SUBMIT_UNKNOWN`` jobs recover identity, while
identity-bearing ``PRIORITY_PENDING`` jobs retry priority confirmation.  This
module never creates replacement remote work.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.services.customer_prewarm_discovery import _open_state_db_readonly
from app.services.customer_prewarm_store import (
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_REMOTE_REUSED,
    CustomerPrewarmStore,
)
from app.services.incremental_audit_runtime import runtime_paths
from app.services.daily_audit_store import DailyAuditStore, LeaseUnavailable, ProcessLock
from app.services.shared_crawl_cache import PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG

CROSS_DAY_RECONCILE_PER_PASS = 4
CROSS_DAY_RECONCILE_PER_JOB_TIMEOUT_SECONDS = 30.0

_ELIGIBLE_JOB_SQL = """
SELECT j.id
FROM remote_jobs j
WHERE j.id = ?
  AND (
      (
          j.state = 'SUBMIT_UNKNOWN'
          AND TRIM(COALESCE(j.remote_identity, '')) = ''
          AND COALESCE(j.recovery_attempts, 0) < ?
      )
      OR (
          j.state = 'PRIORITY_PENDING'
          AND TRIM(COALESCE(j.remote_identity, '')) <> ''
      )
  )
  AND EXISTS (
      SELECT 1 FROM crawl_work c
      WHERE c.remote_job_id = j.id AND c.sku_id LIKE 'zhpq-%'
  )
"""


def _resolve_dead_threshold(dead_threshold: int | None) -> int:
    if dead_threshold is not None:
        return int(dead_threshold)
    from app.services.customer_prewarm_lifecycle import RECONCILE_DEAD_THRESHOLD

    return RECONCILE_DEAD_THRESHOLD


def _linked_cross_day_job_keys(
    prewarm_store: CustomerPrewarmStore,
    current_business_date: str,
) -> list[tuple[str, int]]:
    """Return deduplicated current candidate bindings owned by earlier days."""
    keys: set[tuple[str, int]] = set()
    try:
        current_day = date.fromisoformat(current_business_date)
    except ValueError:
        return []
    if current_day.isoformat() != current_business_date:
        return []
    for status in (CACHE_STATUS_REMOTE_DISPATCHED, CACHE_STATUS_REMOTE_REUSED):
        for candidate in prewarm_store.candidates(status):
            candidate_job_id = candidate.get("remote_job_id")
            if candidate_job_id is None:
                continue
            link = prewarm_store.remote_job_link_for_candidate(int(candidate["id"]))
            if link is None:
                continue
            owning_day = str(link.get("business_date") or "")
            linked_job_id = int(link["remote_job_id"])
            try:
                historical_day = date.fromisoformat(owning_day)
            except ValueError:
                continue
            if (
                historical_day.isoformat() != owning_day
                or historical_day >= current_day
                or str(link.get("producer") or "")
                != PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
                or linked_job_id != int(candidate_job_id)
            ):
                continue
            keys.add((owning_day, linked_job_id))
    return sorted(keys)


def plan_cross_day_reconcile_jobs(
    prewarm_store: CustomerPrewarmStore,
    *,
    current_business_date: str,
    state_runtime_root: str | Path,
    dead_threshold: int | None = None,
) -> list[tuple[str, int]]:
    """Plan eligible identity recovery or priority confirmation read-only."""
    effective_dead_threshold = _resolve_dead_threshold(dead_threshold)
    planned: list[tuple[str, int]] = []
    root = Path(state_runtime_root)
    for owning_day, job_id in _linked_cross_day_job_keys(
        prewarm_store, current_business_date
    ):
        paths = runtime_paths(owning_day, root)
        if paths.run_dir.resolve().parent != root.resolve():
            continue
        connection = _open_state_db_readonly(
            paths.state_path
        )
        if connection is None:
            continue
        try:
            row = connection.execute(
                _ELIGIBLE_JOB_SQL, (int(job_id), effective_dead_threshold)
            ).fetchone()
        finally:
            connection.close()
        if row is not None:
            planned.append((owning_day, int(job_id)))
    return planned


def _eligible_locked_job(
    store: DailyAuditStore,
    job_id: int,
    dead_threshold: int,
) -> dict[str, Any] | None:
    """Revalidate a discovered job under its owning day's lock and lease."""
    job = store.get_remote_job(job_id)
    if job is None:
        return None
    state = str(job.get("state") or "")
    identity = str(job.get("remote_identity") or "").strip()
    if state == "SUBMIT_UNKNOWN":
        if identity or int(job.get("recovery_attempts") or 0) >= dead_threshold:
            return None
    elif state == "PRIORITY_PENDING":
        if not identity:
            return None
    else:
        return None
    if not any(
        str(crawl.get("sku_id") or "").startswith("zhpq-")
        for crawl in store.crawl_work_for_job(job_id)
    ):
        return None
    return job


def run_cross_day_reconcile_phase(
    *,
    prewarm_store: CustomerPrewarmStore,
    current_business_date: str,
    state_runtime_root: str | Path,
    get_adapter: Callable[[], Any],
    deadline: float,
    monotonic_value: Callable[[], float],
    summary: dict[str, Any],
    max_jobs: int = CROSS_DAY_RECONCILE_PER_PASS,
    per_job_timeout_seconds: float = CROSS_DAY_RECONCILE_PER_JOB_TIMEOUT_SECONDS,
    dead_threshold: int | None = None,
) -> None:
    """Recover each exact historical prewarm job under its own daily lease.

    The caller must have released today's dispatch lock before entry.  This
    phase never submits or redispatches; the existing today's redispatch phase
    owns dead jobs on the next scheduler pass.
    """
    from app.services.customer_prewarm_dispatch import (
        LEASE_TTL_SECONDS,
        SUBMIT_MIN_SECONDS,
        _confirm_pending_priority_job,
        _reconstruct_prepared_job,
        _status_value,
    )

    counters = (
        "cross_day_reconcile_planned",
        "cross_day_reconcile_attempted",
        "cross_day_reconciled_found",
        "cross_day_reconcile_not_found",
        "cross_day_reconcile_lock_busy",
        "cross_day_reconcile_deferred",
        "cross_day_reconcile_dead",
    )
    for name in counters:
        summary.setdefault(name, 0)
    phase_errors = summary["phase_errors"]
    effective_dead_threshold = _resolve_dead_threshold(dead_threshold)
    try:
        keys = _linked_cross_day_job_keys(prewarm_store, current_business_date)
        planned = set(
            plan_cross_day_reconcile_jobs(
                prewarm_store,
                current_business_date=current_business_date,
                state_runtime_root=state_runtime_root,
                dead_threshold=effective_dead_threshold,
            )
        )
    except Exception as exc:  # noqa: BLE001 - discovery failure is phase-isolated
        phase_errors.append(f"cross-day reconcile discovery: {type(exc).__name__}: {exc}")
        return
    summary["cross_day_reconcile_planned"] = len(planned)
    root = Path(state_runtime_root)
    attempted = 0
    for owning_day, job_id in keys:
        paths = runtime_paths(owning_day, root)
        if paths.run_dir.resolve().parent != root.resolve():
            summary["cross_day_reconcile_deferred"] += 1
            phase_errors.append(
                f"cross-day job {owning_day}/{job_id}: historical path invalid; deferred"
            )
            continue
        key = (owning_day, job_id)
        if not paths.state_path.is_file():
            summary["cross_day_reconcile_deferred"] += 1
            phase_errors.append(
                f"cross-day job {owning_day}/{job_id}: historical state DB missing; deferred"
            )
            continue
        if key not in planned:
            continue
        if attempted >= min(int(max_jobs), CROSS_DAY_RECONCILE_PER_PASS):
            summary["cross_day_reconcile_deferred"] += 1
            phase_errors.append(f"cross-day job {owning_day}/{job_id}: cap reached; deferred")
            continue
        remaining = deadline - monotonic_value()
        if remaining < SUBMIT_MIN_SECONDS:
            summary["cross_day_reconcile_deferred"] += 1
            phase_errors.append(
                f"cross-day job {owning_day}/{job_id}: budget deadline reached; deferred"
            )
            continue
        lock = ProcessLock(paths.lock_path)
        try:
            lock.acquire(
                owner={
                    "pid": os.getpid(),
                    "command": "customer_prewarm_cross_day_reconcile",
                    "business_date": owning_day,
                }
            )
        except LeaseUnavailable:
            summary["cross_day_reconcile_lock_busy"] += 1
            phase_errors.append(f"cross-day job {owning_day}/{job_id}: LOCK_BUSY; deferred")
            continue
        try:
            if not paths.state_path.is_file():
                summary["cross_day_reconcile_deferred"] += 1
                phase_errors.append(
                    f"cross-day job {owning_day}/{job_id}: historical state DB missing; deferred"
                )
                continue
            if key not in set(
                _linked_cross_day_job_keys(prewarm_store, current_business_date)
            ):
                summary["cross_day_reconcile_deferred"] += 1
                phase_errors.append(
                    f"cross-day job {owning_day}/{job_id}: candidate linkage changed; skipped"
                )
                continue
            store = DailyAuditStore(paths.state_path)
            if store.get_run(owning_day) is None:
                summary["cross_day_reconcile_deferred"] += 1
                phase_errors.append(f"cross-day job {owning_day}/{job_id}: daily run missing; deferred")
                continue
            try:
                token = store.acquire_lease(
                    owning_day,
                    "customer_prewarm_cross_day_reconcile",
                    LEASE_TTL_SECONDS,
                    allow_unexpired_takeover=True,
                    process_lock=lock,
                )
            except LeaseUnavailable:
                summary["cross_day_reconcile_lock_busy"] += 1
                phase_errors.append(f"cross-day job {owning_day}/{job_id}: LOCK_BUSY; deferred")
                continue
            try:
                job = _eligible_locked_job(store, job_id, effective_dead_threshold)
                if job is None or int(job["run_id"]) != token.run_id:
                    summary["cross_day_reconcile_deferred"] += 1
                    phase_errors.append(
                        f"cross-day job {owning_day}/{job_id}: eligibility changed; skipped"
                    )
                    continue
                remaining = deadline - monotonic_value()
                if remaining < SUBMIT_MIN_SECONDS:
                    summary["cross_day_reconcile_deferred"] += 1
                    phase_errors.append(
                        f"cross-day job {owning_day}/{job_id}: budget deadline reached; deferred"
                    )
                    continue
                attempted += 1
                summary["cross_day_reconcile_attempted"] += 1
                job_deadline = min(
                    deadline,
                    monotonic_value() + float(per_job_timeout_seconds),
                )
                prepared = _reconstruct_prepared_job(get_adapter(), owning_day, job)
                if str(job.get("state") or "") == "PRIORITY_PENDING":
                    if key not in set(
                        _linked_cross_day_job_keys(
                            prewarm_store, current_business_date
                        )
                    ):
                        summary["cross_day_reconcile_deferred"] += 1
                        phase_errors.append(
                            f"cross-day job {owning_day}/{job_id}: candidate linkage "
                            "changed before priority confirm; skipped"
                        )
                        continue

                    def priority_result_still_owned() -> bool:
                        if key in set(
                            _linked_cross_day_job_keys(
                                prewarm_store, current_business_date
                            )
                        ):
                            return True
                        summary["cross_day_reconcile_deferred"] += 1
                        phase_errors.append(
                            f"cross-day job {owning_day}/{job_id}: candidate linkage "
                            "changed during priority confirm; result discarded"
                        )
                        return False

                    _confirm_pending_priority_job(
                        prewarm_store=None,
                        store=store,
                        token=token,
                        get_adapter=get_adapter,
                        business_date=owning_day,
                        note_targets=[],
                        job_id=job_id,
                        deadline=job_deadline,
                        monotonic_value=monotonic_value,
                        summary=summary,
                        prepared=prepared,
                        result_commit_guard=priority_result_still_owned,
                    )
                    continue
                timeout = job_deadline - monotonic_value()
                if timeout <= 0:
                    summary["cross_day_reconcile_deferred"] += 1
                    phase_errors.append(
                        f"cross-day job {owning_day}/{job_id}: per-job budget exhausted; deferred"
                    )
                    continue
                outcome = get_adapter().reconcile_unknown(
                    prepared, timeout_seconds=timeout
                )
                if key not in set(
                    _linked_cross_day_job_keys(
                        prewarm_store, current_business_date
                    )
                ):
                    summary["cross_day_reconcile_deferred"] += 1
                    phase_errors.append(
                        f"cross-day job {owning_day}/{job_id}: candidate linkage "
                        "changed during remote lookup; result discarded"
                    )
                    continue
                status = _status_value(outcome.status)
                task_id = getattr(outcome, "task_id", None)
                task_number = getattr(outcome, "task_number", None)
                if status == "FOUND" and task_id and task_number:
                    mall_key = str(job.get("mall_key") or prepared.mall_key)
                    remote_identity = f"{mall_key}|{task_id}|{task_number}"
                    store.reconcile_remote_job_and_crawl_work(
                        token,
                        job_id,
                        remote_identity=remote_identity,
                        state="PRIORITY_PENDING",
                    )
                    summary["cross_day_reconciled_found"] += 1
                    _confirm_pending_priority_job(
                        prewarm_store=None,
                        store=store,
                        token=token,
                        get_adapter=get_adapter,
                        business_date=owning_day,
                        note_targets=[],
                        job_id=job_id,
                        deadline=job_deadline,
                        monotonic_value=monotonic_value,
                        summary=summary,
                        prepared=prepared,
                    )
                else:
                    if status == "FOUND":
                        phase_errors.append(
                            f"cross-day job {owning_day}/{job_id}: incomplete FOUND identity"
                        )
                    new_attempts = store.bump_remote_job_recovery_attempt(token, job_id)
                    summary["cross_day_reconcile_not_found"] += 1
                    if new_attempts >= effective_dead_threshold:
                        summary["cross_day_reconcile_dead"] += 1
                        phase_errors.append(
                            f"cross-day job {owning_day}/{job_id}: judged dead after "
                            f"{new_attempts} attempts; next dispatch owns redispatch"
                        )
            finally:
                store.release_lease(token)
        except Exception as exc:  # noqa: BLE001 - one historical job cannot abort the pass
            phase_errors.append(
                f"cross-day job {owning_day}/{job_id}: {type(exc).__name__}: {exc}"
            )
        finally:
            lock.release()
