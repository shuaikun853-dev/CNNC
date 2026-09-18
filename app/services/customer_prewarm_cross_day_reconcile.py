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
from contextlib import contextmanager
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
_CROSS_DAY_RECONCILE_CURSOR = "cross-day-reconcile"
_CROSS_DAY_HARVEST_CURSOR = "cross-day-harvest"
CROSS_DAY_HARVEST_PER_PASS = 3
CROSS_DAY_HARVEST_PER_JOB_TIMEOUT_SECONDS = 90.0

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

_HARVEST_ELIGIBLE_JOB_SQL = """
SELECT j.state, j.remote_identity, j.mall_key
FROM remote_jobs j
JOIN daily_runs r ON r.id = j.run_id
WHERE j.id = ? AND r.business_date = ?
  AND TRIM(COALESCE(j.remote_identity, '')) <> ''
  AND (
      j.state IN ('SUBMITTED', 'RUNNING', 'EXPORTED')
      OR (
          j.state IN ('COMPLETED', 'MANUAL_REQUIRED', 'TERMINAL_FAILED')
          AND EXISTS (
              SELECT 1 FROM crawl_work c
              WHERE c.remote_job_id = j.id AND c.state = 'SUCCESS'
                AND c.sku_id LIKE 'zhpq-%'
          )
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


def _rotate_after_cursor(
    keys: list[tuple[str, int]], cursor_value: str | None
) -> list[tuple[str, int]]:
    if not keys or not cursor_value:
        return keys
    try:
        cursor_day, cursor_job_id = cursor_value.rsplit("|", 1)
        cursor = (cursor_day, int(cursor_job_id))
    except (TypeError, ValueError):
        return keys
    split_at = next((index for index, key in enumerate(keys) if key > cursor), 0)
    return keys[split_at:] + keys[:split_at]


def _cursor_value(key: tuple[str, int]) -> str:
    return f"{key[0]}|{key[1]}"


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


def plan_cross_day_harvest_jobs(
    prewarm_store: CustomerPrewarmStore,
    *,
    current_business_date: str,
    state_runtime_root: str | Path,
) -> list[tuple[str, int]]:
    """Read-only historical harvest/cache targets from current candidate links."""
    root = Path(state_runtime_root)
    planned: list[tuple[str, int]] = []
    for owning_day, job_id in _linked_cross_day_job_keys(
        prewarm_store, current_business_date
    ):
        paths = runtime_paths(owning_day, root)
        if paths.run_dir.resolve().parent != root.resolve():
            continue
        connection = _open_state_db_readonly(paths.state_path)
        if connection is None:
            continue
        try:
            row = connection.execute(
                _HARVEST_ELIGIBLE_JOB_SQL, (job_id, owning_day)
            ).fetchone()
        finally:
            connection.close()
        if row is not None:
            planned.append((owning_day, job_id))
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
    from app.services.customer_prewarm_lifecycle import _mark_reconcile_manual

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
        keys = _rotate_after_cursor(
            keys,
            prewarm_store.lifecycle_cursor(_CROSS_DAY_RECONCILE_CURSOR),
        )
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
                try:
                    prewarm_store.set_lifecycle_cursor(
                        _CROSS_DAY_RECONCILE_CURSOR,
                        _cursor_value(key),
                    )
                except Exception as exc:  # noqa: BLE001 - job can still run safely
                    phase_errors.append(
                        "cross-day reconcile cursor: "
                        f"{type(exc).__name__}: {exc}"
                    )
                job_deadline = min(
                    deadline,
                    monotonic_value() + float(per_job_timeout_seconds),
                )
                if job_deadline - monotonic_value() < SUBMIT_MIN_SECONDS:
                    summary["cross_day_reconcile_deferred"] += 1
                    phase_errors.append(
                        f"cross-day job {owning_day}/{job_id}: per-job budget "
                        "exhausted; deferred"
                    )
                    continue
                prepared = _reconstruct_prepared_job(get_adapter(), owning_day, job)

                def linkage_still_current() -> bool:
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

                @contextmanager
                def historical_link_commit_fence():
                    with prewarm_store.fence_current_remote_job_link(
                        business_date=owning_day,
                        remote_job_id=job_id,
                        producer=PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
                    ) as owned:
                        if not owned:
                            summary["cross_day_reconcile_deferred"] += 1
                            phase_errors.append(
                                f"cross-day job {owning_day}/{job_id}: candidate "
                                "linkage changed before historical commit; result discarded"
                            )
                        yield owned

                if str(job.get("state") or "") == "PRIORITY_PENDING":
                    if not linkage_still_current():
                        continue
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
                        result_commit_fence=historical_link_commit_fence,
                        pre_remote_guard=linkage_still_current,
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
                    with historical_link_commit_fence() as commit_allowed:
                        if not commit_allowed:
                            continue
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
                        result_commit_fence=historical_link_commit_fence,
                        pre_remote_guard=linkage_still_current,
                    )
                elif status == "MANUAL" or status == "FOUND":
                    diagnostic = (
                        str(getattr(outcome, "diagnostic", "") or "")
                        if status == "MANUAL"
                        else "incomplete FOUND identity"
                    )
                    with historical_link_commit_fence() as commit_allowed:
                        if not commit_allowed:
                            continue
                        _mark_reconcile_manual(store, token, job_id, diagnostic)
                    phase_errors.append(
                        f"cross-day job {owning_day}/{job_id}: manual identity conflict; "
                        "no auto redispatch"
                    )
                elif status == "NOT_FOUND" and not str(
                    getattr(outcome, "diagnostic", "") or ""
                ).strip():
                    with historical_link_commit_fence() as commit_allowed:
                        if not commit_allowed:
                            continue
                        new_attempts = store.bump_remote_job_recovery_attempt(token, job_id)
                    summary["cross_day_reconcile_not_found"] += 1
                    if new_attempts >= effective_dead_threshold:
                        summary["cross_day_reconcile_dead"] += 1
                        phase_errors.append(
                            f"cross-day job {owning_day}/{job_id}: judged dead after "
                            f"{new_attempts} attempts; next dispatch owns redispatch"
                        )
                else:
                    phase_errors.append(
                        f"cross-day job {owning_day}/{job_id}: inconclusive remote lookup; deferred"
                    )
            finally:
                store.release_lease(token)
        except Exception as exc:  # noqa: BLE001 - one historical job cannot abort the pass
            phase_errors.append(
                f"cross-day job {owning_day}/{job_id}: {type(exc).__name__}: {exc}"
            )
        finally:
            lock.release()


def run_cross_day_harvest_phase(
    *,
    prewarm_store: CustomerPrewarmStore,
    shared_cache: Any,
    current_business_date: str,
    state_runtime_root: str | Path,
    get_adapter: Callable[[], Any],
    deadline: float,
    monotonic_value: Callable[[], float],
    summary: dict[str, Any],
    max_jobs: int = CROSS_DAY_HARVEST_PER_PASS,
    per_job_timeout_seconds: float = CROSS_DAY_HARVEST_PER_JOB_TIMEOUT_SECONDS,
) -> None:
    """Harvest linked historical work without nesting its owning-day lock.

    The existing recovery primitive takes its own ProcessLock and durable
    lease. Completed successes remain discoverable until cache sync succeeds,
    so a crash after harvest but before mirroring cannot strand the result.
    """
    from app.services import customer_prewarm_lifecycle as lifecycle
    from app.services.customer_prewarm_discovery import sync_daily_internet_results

    phase_errors = summary["phase_errors"]
    for name in (
        "cross_day_harvest_planned",
        "cross_day_harvest_attempted",
        "cross_day_harvested",
        "cross_day_harvest_cache_synced",
        "cross_day_harvest_deferred",
        "cross_day_harvest_lock_busy",
    ):
        summary.setdefault(name, 0)
    try:
        planned = plan_cross_day_harvest_jobs(
            prewarm_store,
            current_business_date=current_business_date,
            state_runtime_root=state_runtime_root,
        )
        keys = _rotate_after_cursor(
            planned, prewarm_store.lifecycle_cursor(_CROSS_DAY_HARVEST_CURSOR)
        )
    except Exception as exc:  # noqa: BLE001 - independent lifecycle phase
        phase_errors.append(f"cross-day harvest discovery: {type(exc).__name__}: {exc}")
        return
    summary["cross_day_harvest_planned"] = len(planned)
    root = Path(state_runtime_root)
    attempted = 0
    for owning_day, job_id in keys:
        if attempted >= min(int(max_jobs), CROSS_DAY_HARVEST_PER_PASS):
            summary["cross_day_harvest_deferred"] += 1
            continue
        remaining = deadline - monotonic_value()
        if remaining <= 0:
            summary["cross_day_harvest_deferred"] += 1
            continue
        paths = runtime_paths(owning_day, root)
        if paths.run_dir.resolve().parent != root.resolve() or not paths.state_path.is_file():
            summary["cross_day_harvest_deferred"] += 1
            phase_errors.append(f"cross-day harvest {owning_day}/{job_id}: historical DB unavailable")
            continue
        if (owning_day, job_id) not in _linked_cross_day_job_keys(
            prewarm_store, current_business_date
        ):
            summary["cross_day_harvest_deferred"] += 1
            continue
        connection = _open_state_db_readonly(paths.state_path)
        if connection is None:
            summary["cross_day_harvest_deferred"] += 1
            continue
        try:
            row = connection.execute(
                _HARVEST_ELIGIBLE_JOB_SQL, (job_id, owning_day)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            summary["cross_day_harvest_deferred"] += 1
            continue
        identity = str(row["remote_identity"] or "").strip()
        parts = identity.split("|")
        if len(parts) != 3 or not all(parts) or parts[0] != str(row["mall_key"] or ""):
            summary["cross_day_harvest_deferred"] += 1
            phase_errors.append(f"cross-day harvest {owning_day}/{job_id}: malformed identity")
            continue
        attempted += 1
        summary["cross_day_harvest_attempted"] += 1
        try:
            prewarm_store.set_lifecycle_cursor(
                _CROSS_DAY_HARVEST_CURSOR, _cursor_value((owning_day, job_id))
            )
        except Exception as exc:  # noqa: BLE001 - failure cannot block safe harvest
            phase_errors.append(f"cross-day harvest cursor: {type(exc).__name__}: {exc}")
        if str(row["state"]) not in {"COMPLETED", "MANUAL_REQUIRED", "TERMINAL_FAILED"}:
            remaining = deadline - monotonic_value()
            if remaining <= 0:
                summary["cross_day_harvest_deferred"] += 1
                continue
            try:
                result = lifecycle.harvest_existing_customer_prewarm_job(
                    state_path=paths.state_path,
                    lock_path=paths.lock_path,
                    business_date=owning_day,
                    remote_job_id=job_id,
                    remote_identity=identity,
                    adapter=get_adapter(),
                    timeout_seconds=min(float(per_job_timeout_seconds), remaining),
                )
            except LeaseUnavailable:
                summary["cross_day_harvest_lock_busy"] += 1
                continue
            except Exception as exc:  # noqa: BLE001 - isolate each historical job
                phase_errors.append(
                    f"cross-day harvest {owning_day}/{job_id}: {type(exc).__name__}: {exc}"
                )
                continue
            outcome = str(result.get("outcome") or "")
            if outcome == "REMOTE_EXISTING_HARVEST_RUNNING":
                continue
            if outcome == "REMOTE_EXISTING_HARVEST_COMPLETED":
                summary["cross_day_harvested"] += 1
            elif outcome != "REMOTE_EXISTING_HARVEST_MANUAL":
                phase_errors.append(
                    f"cross-day harvest {owning_day}/{job_id}: {outcome or 'unknown outcome'}"
                )
                continue
        if deadline - monotonic_value() <= 0:
            summary["cross_day_harvest_deferred"] += 1
            continue
        if (owning_day, job_id) not in _linked_cross_day_job_keys(
            prewarm_store, current_business_date
        ):
            summary["cross_day_harvest_deferred"] += 1
            continue
        try:
            synced = sync_daily_internet_results(
                prewarm_store,
                shared_cache,
                state_db_path=paths.state_path,
                business_date=owning_day,
                remote_job_id=job_id,
            )
            summary["cross_day_harvest_cache_synced"] += int(synced["ok_synced"])
            if synced["errors"] or synced["transient_failures"]:
                phase_errors.append(
                    f"cross-day harvest {owning_day}/{job_id}: cache sync incomplete"
                )
        except Exception as exc:  # noqa: BLE001 - completed row stays discoverable
            phase_errors.append(
                f"cross-day harvest {owning_day}/{job_id}: cache sync {type(exc).__name__}: {exc}"
            )
