"""Durable circuit breaker for remote subtask-materialization stalls.

The breaker is never held in process memory. Every scheduler process
re-derives it from the remote_jobs evidence already persisted by the normal
priority/harvest path (state + error_text + updated_at), so a restart cannot
forget an ongoing remote stall, and no new SQLite schema is required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from app.services.legacy_zhaoshang_adapter import (
    REMOTE_NOT_MATERIALIZED_DIAGNOSTICS,
)


REMOTE_STALL_THRESHOLD = 3
REMOTE_STALL_WINDOW_SECONDS = 900.0
REMOTE_STALL_REASON = "PRIORITY_ROWS_NOT_MATERIALIZED"
_STALL_EVIDENCE_STATES = frozenset({"PRIORITY_PENDING", "MANUAL_REQUIRED"})


def _parse_updated_at(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def evaluate_remote_stall(
    evidence_rows: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    threshold: int = REMOTE_STALL_THRESHOLD,
    window_seconds: float = REMOTE_STALL_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Count distinct recent REMOTE_NOT_MATERIALIZED jobs as breaker evidence.

    Only strictly classified stall diagnostics count. InterfaceError and other
    retryable transport diagnostics, harvest/export manuals, and identity or
    UPDATE verification failures never open this breaker. Evidence ages out of
    the window naturally; a successful probe clears the job's error_text and
    drops it from the count, which is how the breaker recovers.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("remote stall evaluation clock must be timezone-aware")
    recent_stall_jobs = 0
    seen_job_ids: set[int] = set()
    for row in evidence_rows:
        diagnostic = str(row.get("error_text") or "").strip()
        if diagnostic not in REMOTE_NOT_MATERIALIZED_DIAGNOSTICS:
            continue
        if str(row.get("state") or "").strip() not in _STALL_EVIDENCE_STATES:
            continue
        try:
            job_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        if job_id in seen_job_ids:
            continue
        updated_at = _parse_updated_at(row.get("updated_at"))
        if updated_at is None:
            continue
        if (moment - updated_at).total_seconds() > window_seconds:
            continue
        seen_job_ids.add(job_id)
        recent_stall_jobs += 1
    stall_detected = recent_stall_jobs >= threshold
    return {
        "stall_detected": stall_detected,
        "stall_reason": REMOTE_STALL_REASON if stall_detected else None,
        "recent_stall_jobs": recent_stall_jobs,
        "threshold": threshold,
        "window_seconds": window_seconds,
        "submit_paused": stall_detected,
    }


def remote_submit_pause_reason(
    store: Any,
    business_date: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return the durable submit-pause reason for one business day, if stalled."""
    run = store.get_run(business_date)
    if run is None:
        return None
    health = evaluate_remote_stall(
        store.list_remote_stall_evidence(int(run["id"])),
        now=now,
    )
    if not health["submit_paused"]:
        return None
    return (
        "remote stall detected; do not create additional remote tasks; "
        f"recent_stall_jobs={health['recent_stall_jobs']} "
        f"window_seconds={health['window_seconds']}"
    )
