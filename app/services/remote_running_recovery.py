"""Durable stall detection for long-RUNNING zhaoshang remote jobs.

A RUNNING job is only "stalled" when durable evidence proves the remote task
has made no business progress for a meaningful window and its export endpoint
keeps answering an explicit no-price response.  The classifier never uses
``remote_jobs.updated_at`` as progress (every harvest rewrites it) and never
treats a missing/legacy evidence document as proof of anything: lack of
evidence means "not yet proven stalled", so old durable rows keep harvesting
normally after the deployment.

All thresholds are derived from the 2026-08-25 production contrast:
91/93 completed jobs finish within 30 minutes / 3 attempts; job 20 completed
after 130 minutes with visible progress movement; job 53 only recovered after
a manual exact disable->enable once 168 minutes / 16 attempts had passed with
a frozen progress signature and a persistent ``count=0`` export response.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

from app.services.daily_audit_store import DailyAuditStore, LeaseToken


PROGRESS_FIELDS: tuple[str, ...] = (
    "total",
    "pending",
    "running",
    "failed",
    "waiting_db",
    "finished",
)
RUNNING_STALL_EXPORT_NO_PRICE_RSP_DESC = "未检索到商品价格信息"
# Candidate gating: only RUNNING jobs that have been harvested at least twice
# and are at least 30 minutes old spend read-only probe calls.
RUNNING_PROBE_MIN_HARVEST_ATTEMPTS = 2
RUNNING_PROBE_MIN_WALL_SECONDS = 1800.0
# Stall proof (all must hold): attempts, wall-clock age, a frozen progress
# signature observed at least 4 times across at least 60 minutes, and an
# explicit zero-count no-price export answer on every one of them.
RUNNING_STALL_MIN_HARVEST_ATTEMPTS = 8
RUNNING_STALL_MIN_WALL_SECONDS = 5400.0
RUNNING_STALL_MIN_SAME_SIGNATURE_OBSERVATIONS = 4
RUNNING_STALL_MIN_STATIONARY_SPAN_SECONDS = 3600.0
# Exhausted proof: after the one allowed restart, at least two fresh
# observations (30 minutes after recovery) still show a frozen signature and
# a zero-count no-price export.
RECOVERY_EXHAUSTED_MIN_POST_OBSERVATIONS = 2
RECOVERY_EXHAUSTED_MIN_POST_SPAN_SECONDS = 1800.0
# A disable/enable write whose outcome is unknown must be reconciled against
# the real remote state before any decision; after this window a still
# contradictory remote state fails closed into manual review.
RECOVERY_WRITE_CONFIRM_WINDOW_SECONDS = 900.0

RECOVERY_EXHAUSTED_STATE = "RECOVERY_EXHAUSTED"


def _text(value: object) -> str:
    return str(value or "").strip()


def _parse_timestamp(value: object) -> datetime | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def canonical_progress_signature(progress: Mapping[str, Any]) -> tuple[int, ...] | None:
    """Canonicalize the six business progress counters, or reject the row.

    ``None`` means the progress payload does not carry every field with an
    integer value; such an observation never counts as stationary evidence.
    """
    values: list[int] = []
    for field in PROGRESS_FIELDS:
        raw = progress.get(field) if isinstance(progress, Mapping) else None
        if raw is None or isinstance(raw, bool):
            return None
        try:
            values.append(int(raw))
        except (TypeError, ValueError):
            return None
    if values[0] <= 0:
        return None
    return tuple(values)


def decode_recovery_evidence(value: object) -> dict[str, Any]:
    """Decode the durable recovery document; legacy/absent rows return a fresh one."""
    raw = _text(value)
    if not raw:
        return {"version": 1, "observations": [], "restart": None}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"version": 1, "observations": [], "restart": None}
    if not isinstance(decoded, dict):
        return {"version": 1, "observations": [], "restart": None}
    observations = decoded.get("observations")
    if not isinstance(observations, list):
        observations = []
    restart = decoded.get("restart")
    if not isinstance(restart, dict):
        restart = None
    return {
        "version": 1,
        "observations": [row for row in observations if isinstance(row, dict)],
        "restart": restart,
    }


def encode_recovery_evidence(evidence: Mapping[str, Any]) -> str:
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def observation_from_probe(probe: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """Build one durable progress observation from a read-only probe result."""
    progress = probe.get("progress") if isinstance(probe.get("progress"), Mapping) else {}
    signature = canonical_progress_signature(progress)
    export_had_result = bool(probe.get("export_had_result"))
    export_count = probe.get("export_count")
    try:
        export_count = int(export_count) if export_count is not None and not isinstance(export_count, bool) else None
    except (TypeError, ValueError):
        export_count = None
    return {
        "observed_at": now.astimezone(UTC).isoformat(timespec="seconds"),
        "progress": {field: progress.get(field) for field in PROGRESS_FIELDS},
        "signature": list(signature) if signature is not None else None,
        "export_had_result": export_had_result,
        "export_count": export_count,
        "export_rsp_desc": _text(probe.get("export_rsp_desc")),
        "task_status_rsp_desc": _text(probe.get("task_status_rsp_desc")),
    }


def _observation_is_no_price(row: Mapping[str, Any]) -> bool:
    if row.get("signature") is None:
        return False
    if bool(row.get("export_had_result")):
        return False
    count = row.get("export_count")
    if not isinstance(count, int) or isinstance(count, bool) or count != 0:
        return False
    return _text(row.get("export_rsp_desc")) == RUNNING_STALL_EXPORT_NO_PRICE_RSP_DESC


def _stationary_tail(observations: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return the longest contiguous tail of observations sharing one signature."""
    if not observations:
        return []
    tail: list[Mapping[str, Any]] = []
    reference = observations[-1].get("signature")
    for row in reversed(observations):
        if row.get("signature") is None or row.get("signature") != reference:
            break
        tail.append(row)
    tail.reverse()
    return tail if reference is not None else []


def _span_seconds(rows: list[Mapping[str, Any]]) -> float:
    if len(rows) < 2:
        return 0.0
    start = _parse_timestamp(rows[0].get("observed_at"))
    end = _parse_timestamp(rows[-1].get("observed_at"))
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _identity_is_valid(remote_identity: object) -> bool:
    parts = _text(remote_identity).split("|")
    return len(parts) == 3 and all(part.strip() for part in parts)


def evaluate_running_stall(job: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """Classify one durable RUNNING job against the fail-closed stall rules.

    The returned decision is derived only from durable evidence plus the
    supplied clock.  ``probe_candidate`` gates read-only probe spending;
    ``stalled`` authorizes the single automatic restart; ``exhausted_ready``
    authorizes the terminal local give-up state.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("running stall evaluation clock must be timezone-aware")
    state = _text(job.get("state"))
    identity_valid = _identity_is_valid(job.get("remote_identity"))
    created_at = _parse_timestamp(job.get("created_at"))
    wall_seconds = max(0.0, (now - created_at).total_seconds()) if created_at is not None else 0.0
    try:
        attempts = int(job.get("harvest_attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    try:
        recovery_attempts = int(job.get("recovery_attempts") or 0)
    except (TypeError, ValueError):
        recovery_attempts = 0
    evidence = decode_recovery_evidence(job.get("recovery_evidence_json"))

    decision: dict[str, Any] = {
        "probe_candidate": False,
        "stalled": False,
        "exhausted_ready": False,
        "reason": "",
    }
    if state != "RUNNING" or not identity_valid:
        decision["reason"] = f"state={state or 'missing'};identity_valid={identity_valid}"
        return decision
    decision["probe_candidate"] = (
        attempts >= RUNNING_PROBE_MIN_HARVEST_ATTEMPTS
        and wall_seconds >= RUNNING_PROBE_MIN_WALL_SECONDS
    )

    observations = list(evidence["observations"])
    tail = _stationary_tail(observations)
    tail_no_price = all(_observation_is_no_price(row) for row in tail) if tail else False
    tail_span = _span_seconds(tail)

    restart = evidence["restart"]
    if recovery_attempts == 0:
        stalled = (
            attempts >= RUNNING_STALL_MIN_HARVEST_ATTEMPTS
            and wall_seconds >= RUNNING_STALL_MIN_WALL_SECONDS
            and len(tail) >= RUNNING_STALL_MIN_SAME_SIGNATURE_OBSERVATIONS
            and tail_span >= RUNNING_STALL_MIN_STATIONARY_SPAN_SECONDS
            and tail_no_price
        )
        decision["stalled"] = stalled
        decision["reason"] = (
            f"stationary_observations={len(tail)};span_seconds={tail_span:.0f};"
            f"attempts={attempts};wall_seconds={wall_seconds:.0f};no_price={tail_no_price}"
        )
        return decision

    # recovery_attempts >= 1: the single restart already happened; evaluate the
    # post-recovery tail only for the terminal give-up decision.
    decision["reason"] = f"recovery_attempts={recovery_attempts}"
    if recovery_attempts != 1 or not isinstance(restart, Mapping):
        return decision
    if _text(restart.get("phase")) != "ENABLE_CONFIRMED":
        return decision
    recovery_at = _parse_timestamp(restart.get("recovery_at"))
    if recovery_at is None:
        return decision
    post = [
        row
        for row in observations
        if (_parse_timestamp(row.get("observed_at")) or recovery_at) > recovery_at
    ]
    post_tail = _stationary_tail(post)
    post_no_price = all(_observation_is_no_price(row) for row in post_tail) if post_tail else False
    decision["exhausted_ready"] = (
        (now - recovery_at).total_seconds() >= RECOVERY_EXHAUSTED_MIN_POST_SPAN_SECONDS
        and len(post_tail) >= RECOVERY_EXHAUSTED_MIN_POST_OBSERVATIONS
        and post_no_price
    )
    return decision


def run_running_recovery_pass(
    store: DailyAuditStore,
    token: LeaseToken,
    *,
    probe_remote: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    restart_remote: Callable[[Mapping[str, Any], str | None], Mapping[str, Any]] | None = None,
    now: datetime,
    can_start: Callable[[], bool],
) -> dict[str, int]:
    """One bounded recovery sweep over the durable RUNNING jobs.

    Read-only probes append durable progress observations for every candidate.
    A job whose evidence proves a stall may consume its single automatic
    restart when (and only when) a restart callback is wired in; with no
    callback the pass only records evidence and leaves the job RUNNING.
    """
    counters = {
        "recovery_probes": 0,
        "recovery_restarts": 0,
        "recovery_exhausted": 0,
        "recovery_reconciled": 0,
    }
    for job in store.list_remote_jobs(token):
        if not can_start():
            break
        decision = evaluate_running_stall(job, now=now)
        if not decision["probe_candidate"]:
            continue
        probe = probe_remote(job)
        if not isinstance(probe, Mapping):
            continue
        counters["recovery_probes"] += 1
        evidence = decode_recovery_evidence(job.get("recovery_evidence_json"))
        evidence["observations"].append(observation_from_probe(probe, now=now))
        job = store.update_remote_recovery_evidence(token, int(job["id"]), evidence)
        decision = evaluate_running_stall(job, now=now)
        if decision["stalled"] and restart_remote is not None:
            outcome = restart_remote(job, None)
            counters["recovery_restarts"] += int(bool(outcome.get("restarted")))
            counters["recovery_reconciled"] += int(bool(outcome.get("reconciled")))
    return counters
