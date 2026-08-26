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
from enum import Enum
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
# Remote execution-state evidence.  The external tool interprets exactly one
# status value itself -- the terminal marker "本次任务执行完毕"
# (auto_crawl_task.py:1048) -- and no RUNNING string for the status endpoint
# has ever been observed in production (all 24 real captures were empty;
# "执行中" is a progress-descriptor label, not a status answer).  The marker
# therefore matches the _text()-normalized status value EXACTLY; substring,
# prefix, or suffix containment would widen the evidence to failure phrases
# like "查询本次任务执行完毕状态失败".  Liveness is derived from the
# structured query_task_progress six-field payload, mirroring the tool's own
# ``is_progress_finished`` business definition — but fail-closed: structural
# completeness (all six integer fields, total > 0, none negative) and the one
# deterministic mathematical consistency the tool's counters guarantee
# (finished + failed <= total) are verified BEFORE the ACTIVE branch may
# accept the row, so a contradictory payload is UNKNOWN regardless of
# pending/running/waiting_db.  Work is ACTIVE while any of those three is
# positive; TERMINAL only when all three are zero and finished + failed >=
# total.  Anything unparseable, contradictory, or otherwise unprovable is
# UNKNOWN and fails closed.
TERMINAL_STATUS_MARKERS: tuple[str, ...] = ("本次任务执行完毕",)
# Business-response evidence gate (review round 5): the external tool's own
# write endpoints (auto_crawl_task.py enable/disable) accept a payload only
# when ``success`` is not explicitly False and ``rspCode`` is absent or the
# one proven success code "0000"; absent fields stay admissible because the
# platform legitimately omits them.
PROVEN_BUSINESS_SUCCESS_RSP_CODES: frozenset[str] = frozenset({"0000"})


def remote_business_response_ok(payload: object) -> bool:
    """Mirror the external tool's business-success rule for read evidence.

    Only a business-successful status/progress response may donate evidence
    (counters, terminal marker text); a ``success=false`` or non-``0000``
    ``rspCode`` payload is stripped to its unprovable form by the callers so
    it can never authorize stall proof or a fresh restart write.  Absent
    ``success``/``rspCode`` fields remain admissible.
    """
    if not isinstance(payload, Mapping):
        return False
    if "success" in payload and payload.get("success") is not True:
        return False
    rsp_code = _text(payload.get("rspCode"))
    if rsp_code and rsp_code not in PROVEN_BUSINESS_SUCCESS_RSP_CODES:
        return False
    return True


class RemoteExecutionState(str, Enum):
    ACTIVE = "REMOTE_ACTIVE"
    TERMINAL = "REMOTE_TERMINAL"
    UNKNOWN = "REMOTE_UNKNOWN"


def classify_remote_execution_state(
    progress: Mapping[str, Any] | None,
    task_status_rsp_desc: object = "",
) -> RemoteExecutionState:
    """Classify one exact remote task as ACTIVE / TERMINAL / UNKNOWN.

    The terminal status marker wins over any progress payload; otherwise the
    structured progress counters decide.  UNKNOWN never authorizes stall
    evidence and never authorizes a fresh remote write.
    """
    description = _text(task_status_rsp_desc)
    if description in TERMINAL_STATUS_MARKERS:
        return RemoteExecutionState.TERMINAL
    signature = canonical_progress_signature(progress if isinstance(progress, Mapping) else {})
    if signature is None:
        return RemoteExecutionState.UNKNOWN
    total, pending, running, failed, waiting_db, finished = signature
    if any(value < 0 for value in signature):
        return RemoteExecutionState.UNKNOWN
    if finished + failed > total:
        return RemoteExecutionState.UNKNOWN
    if pending > 0 or running > 0 or waiting_db > 0:
        return RemoteExecutionState.ACTIVE
    if finished + failed >= total:
        return RemoteExecutionState.TERMINAL
    return RemoteExecutionState.UNKNOWN
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
# A pre-write failure is retryable, but a PERSISTENT pre-write fault
# (expired refreshToken, missing external tool, persistent transport
# outage) must eventually fail closed to manual review like the
# pre-Round-5 confirmation window did — the cap counts consecutive
# zero-write failures without guessing from the exception type.
RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES = 5
# Durable cap for persistent recovery-PROBE faults (expired refreshToken,
# missing external tool, persistent transport outage): consecutive failures
# of the read-only probe itself fail closed to manual so a permanently
# broken probe cannot keep a job RUNNING forever.  Independent of the
# pre-write constant — probe health and the restart protocol are different
# stages, and only the RUNNING-stall recovery probe feeds this counter
# (never harvest/submit/priority/discovery).  5 is an engineering safety
# threshold, not a production-calibrated one.
RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES = 5
# Durable restart phases that already dispatched (or durably recorded) a
# remote side effect.  A job carrying one of them is an EXISTING recovery
# protocol: the next pass reconciles the phase against the live remote state
# without re-proving the original stall (post-disable progress can never be
# ACTIVE evidence again).  ENABLE_CONFIRMED is the completed proof, never a
# pending phase; DECLINED is a zero-write withdrawal.
PENDING_RESTART_PHASES: tuple[str, ...] = ("DISABLE_SENT", "DISABLE_CONFIRMED", "ENABLE_SENT")
# Tick-budget bounds: recovery is tail work.  One probe or restart callback
# may never outlive the tick's remaining budget (the configured 60s ceiling
# is intersected with the live remaining time by the tick), and one Full
# Tick probes at most this many candidates — anything beyond waits for the
# next tick so 17-18h Discovery/Detail/Model/Submit intake keeps priority.
RECOVERY_CALLBACK_TIMEOUT_SECONDS = 60.0
RECOVERY_MAX_PROBE_CANDIDATES_PER_TICK = 4


def classify_restart_exception(exc: BaseException, resume_phase: str | None) -> str:
    """Classify an exception escaping the restart adapter, never blanket-manual.

    Since Round 5 the production restart adapter converts every escape into
    an explicit outcome that names the side-effect boundary itself
    (``PRE_WRITE_FAILED`` / ``DISABLE_UNKNOWN`` / ``ENABLE_UNKNOWN`` /
    ``IDENTITY_CONFLICT``), so this classifier is a last-resort defense for
    injected test runners and unforeseen adapter escapes only.  An escape
    that cannot be proven pre-write stays reconcile-only toward the resumed
    direction — it may fail closed into manual review but can never
    authorize a second write.

    Transient transport failures keep the durable phase so the next pass
    reconciles the already-sent direction against the real remote state.  Only
    failures proving the exact local identity cannot be reconstructed are a
    permanent identity conflict; everything unknown stays reconcilable.
    """
    from app.services.incremental_audit_runtime import RuntimeConfigurationError

    if isinstance(exc, RuntimeConfigurationError):
        return "IDENTITY_CONFLICT"
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "IDENTITY_CONFLICT"
    if isinstance(exc, RuntimeError):
        # The legacy tool raises RuntimeError with these markers for permanent
        # exact-identity anomalies; any other RuntimeError stays reconcilable.
        message = str(exc)
        if any(marker in message for marker in ("不唯一", "不一致", "没有拿到", "identity")):
            return "IDENTITY_CONFLICT"
        return "ENABLE_UNKNOWN" if resume_phase == "ENABLE_SENT" else "DISABLE_UNKNOWN"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "ENABLE_UNKNOWN" if resume_phase == "ENABLE_SENT" else "DISABLE_UNKNOWN"
    return "IDENTITY_CONFLICT"


def _pre_write_restart_failure_status(exc: BaseException) -> str:
    """Classify an exception raised before any remote write was attempted.

    Identity-shaped failures (the exact local↔remote identity cannot be
    reconstructed) stay the permanent IDENTITY_CONFLICT; every other
    pre-write failure — auth refresh, transport, deadline exhaustion,
    tooling — provably sent no remote write and is retryable as a fresh
    attempt on the next pass.
    """
    from app.services.incremental_audit_runtime import RuntimeConfigurationError

    if isinstance(exc, (RuntimeConfigurationError, ValueError, KeyError, TypeError)):
        return "IDENTITY_CONFLICT"
    if isinstance(exc, RuntimeError):
        message = str(exc)
        if any(marker in message for marker in ("不唯一", "不一致", "没有拿到", "identity")):
            return "IDENTITY_CONFLICT"
    return "PRE_WRITE_FAILED"


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
    """Decode the durable recovery document; legacy/absent rows return a fresh one.

    ``probe_health`` (durable consecutive recovery-probe failures) is kept
    strictly separate from ``restart``: a probe failure has not entered the
    restart protocol.  Legacy documents predate the field and default to
    ``None`` — a missing field is zero consecutive failures, never a cap.
    """
    raw = _text(value)
    if not raw:
        return {"version": 1, "observations": [], "restart": None, "probe_health": None}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"version": 1, "observations": [], "restart": None, "probe_health": None}
    if not isinstance(decoded, dict):
        return {"version": 1, "observations": [], "restart": None, "probe_health": None}
    observations = decoded.get("observations")
    if not isinstance(observations, list):
        observations = []
    restart = decoded.get("restart")
    if not isinstance(restart, dict):
        restart = None
    probe_health = decoded.get("probe_health")
    if not isinstance(probe_health, dict):
        probe_health = None
    return {
        "version": 1,
        "observations": [row for row in observations if isinstance(row, dict)],
        "restart": restart,
        "probe_health": probe_health,
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
    if (
        classify_remote_execution_state(row.get("progress"), row.get("task_status_rsp_desc"))
        is not RemoteExecutionState.ACTIVE
    ):
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


def _probe_identity_matches(job: Mapping[str, Any], probe: Mapping[str, Any]) -> bool:
    """The read-only probe must re-confirm the exact durable remote identity.

    Identity part 0 is the durable mall key while the probe reports the
    platform mall code; the legacy tool's MALL_CONFIG guarantees
    identification == mallLogo == mall_key for every supported mall, so the
    comparison is exact under that invariant.
    """
    parts = _text(job.get("remote_identity")).split("|")
    if len(parts) != 3:
        return False
    return (
        _text(probe.get("remote_mall")) == parts[0]
        and _text(probe.get("remote_task_id")) == parts[1]
        and _text(probe.get("remote_task_number")) == parts[2]
    )


def _outcome_proves_confirmed_restart(outcome: Mapping[str, Any]) -> bool:
    """A RESTARTED callback claim is legal only with both confirmations."""
    disable_evidence = outcome.get("disable_evidence")
    enable_evidence = outcome.get("enable_evidence")
    return (
        isinstance(disable_evidence, Mapping)
        and disable_evidence.get("confirmed_disabled") is True
        and isinstance(enable_evidence, Mapping)
        and enable_evidence.get("confirmed_enabled") is True
    )


def _durable_restart_proof_is_complete(restart: Mapping[str, Any] | None) -> bool:
    """The durable one-shot proof: confirmed disable AND enable AND a timestamp."""
    if not isinstance(restart, Mapping):
        return False
    if _text(restart.get("phase")) != "ENABLE_CONFIRMED":
        return False
    if _parse_timestamp(restart.get("recovery_at")) is None:
        return False
    disable_evidence = restart.get("disable_evidence")
    enable_evidence = restart.get("enable_evidence")
    return (
        isinstance(disable_evidence, Mapping)
        and disable_evidence.get("confirmed_disabled") is True
        and isinstance(enable_evidence, Mapping)
        and enable_evidence.get("confirmed_enabled") is True
    )


def _apply_restart(
    store: DailyAuditStore,
    token: LeaseToken,
    job: Mapping[str, Any],
    evidence: dict[str, Any],
    restart_remote: Callable[[Mapping[str, Any], str | None], Mapping[str, Any]],
    *,
    now: datetime,
) -> Mapping[str, Any]:
    """Consume the one automatic disable->enable attempt under durable phases.

    The phase is persisted before any remote write so a crash can never
    dispatch a second disable or enable; resumed passes only reconcile the
    already-sent direction.  Unconfirmed writes fail closed into manual
    review once the confirmation window expires.
    """
    job_id = int(job["id"])
    restart = evidence.get("restart")
    # Crash-window guard: the finished evidence document may have committed
    # while the recovery_attempts column write was lost.  The document is the
    # write-ahead proof, so this state means the restart already happened and
    # only the slot burn must be repeated — never a second remote write.
    if (
        isinstance(restart, Mapping)
        and _text(restart.get("phase")) == "ENABLE_CONFIRMED"
        and int(job.get("recovery_attempts") or 0) == 0
    ):
        if _durable_restart_proof_is_complete(restart):
            store.mark_remote_recovery_attempted(
                token, job_id, remote_identity=_text(job.get("remote_identity")), evidence=restart
            )
            return {"status": "RESTARTED", "restarted": True, "reconciled": True, "diagnostic": "recovered crash window: attempt slot burned from durable ENABLE_CONFIRMED evidence"}
        # A doc claiming ENABLE_CONFIRMED without the complete confirmed proof
        # can neither authorize the burn nor a fresh restart: fail closed.
        store.mark_remote_manual(
            token,
            job_id,
            "recovery crash-window evidence lacks confirmed disable/enable proof; "
            "refusing to burn the attempt or re-dispatch writes",
        )
        return {"status": "MANUAL_REQUIRED", "restarted": False, "diagnostic": "incomplete durable ENABLE_CONFIRMED evidence"}
    resume_phase: str | None = None
    if isinstance(restart, Mapping) and _text(restart.get("phase")) in PENDING_RESTART_PHASES:
        resume_phase = _text(restart.get("phase"))
    else:
        # Captured before the write-ahead overwrite: the consecutive
        # pre-write failure count lives in the previous zero-write record.
        previous_restart = restart
        evidence["restart"] = {
            "phase": "DISABLE_SENT",
            "phase_at": now.astimezone(UTC).isoformat(timespec="seconds"),
            "recovery_attempts": 0,
            "recovery_reason": "REMOTE_RUNNING_STALLED",
        }
        store.update_remote_recovery_evidence(token, job_id, evidence)
    outcome = restart_remote(job, resume_phase)
    status = _text(outcome.get("status"))
    restart = evidence["restart"]
    phase_at = _parse_timestamp(restart.get("phase_at"))
    if status == "RESTARTED":
        if not _outcome_proves_confirmed_restart(outcome):
            # An unevidenced RESTARTED claim must not burn the single attempt.
            # The enable direction may still have been sent, so persist the
            # ENABLE_SENT phase and let the next pass reconcile the real
            # remote state (and fail closed to manual past the window).
            restart["phase"] = "ENABLE_SENT"
            restart["diagnostic"] = (
                "RESTARTED claim lacked confirmed disable/enable evidence; "
                "awaiting phase reconciliation"
            )
            store.update_remote_recovery_evidence(token, job_id, evidence)
            # Keep the metrics honest: an unevidenced claim is not a restart.
            return {**outcome, "restarted": False}
        observations = evidence.get("observations") or []
        last_signature = (
            observations[-1].get("signature")
            if isinstance(observations, list) and observations
            else None
        )
        finished = {
            "phase": "ENABLE_CONFIRMED",
            "phase_at": now.astimezone(UTC).isoformat(timespec="seconds"),
            "recovery_attempts": 1,
            "recovery_at": now.astimezone(UTC).isoformat(timespec="seconds"),
            "recovery_reason": "REMOTE_RUNNING_STALLED",
            "disable_evidence": outcome.get("disable_evidence"),
            "enable_evidence": outcome.get("enable_evidence"),
            "pre_restart_signature": last_signature,
        }
        evidence["restart"] = finished
        store.update_remote_recovery_evidence(token, job_id, evidence)
        store.mark_remote_recovery_attempted(
            token, job_id, remote_identity=_text(job.get("remote_identity")), evidence=finished
        )
        return outcome
    if status == "PRE_WRITE_FAILED" and resume_phase is None:
        # Zero-write proof: the adapter established that no remote write was
        # attempted (auth/lookup/status/progress/deadline all precede the
        # first write).  The write-ahead DISABLE_SENT must be withdrawn —
        # leaving it would force reconcile-only and waste the single
        # automatic recovery attempt on a transient fault.  The diagnostic
        # is kept durably; the attempt slot is NOT consumed; the job stays
        # RUNNING so the next pass may re-enter fresh recovery.  Only a
        # fresh protocol (resume_phase is None) may claim this proof: a
        # PRE_WRITE_FAILED answer while a dispatched phase is durable must
        # never withdraw that write-ahead.  A persistent fault fails closed
        # to manual after the consecutive-failure cap so a permanent
        # auth/tooling outage cannot retry silently forever.
        previous = (
            previous_restart if isinstance(previous_restart, Mapping) else {}
        )
        consecutive = 1
        if _text(previous.get("phase")) == "PRE_WRITE_FAILED":
            try:
                consecutive = int(previous.get("pre_write_failures") or 0) + 1
            except (TypeError, ValueError):
                consecutive = 1
        if consecutive >= RECOVERY_MAX_CONSECUTIVE_PRE_WRITE_FAILURES:
            # The write-ahead DISABLE_SENT persisted above must be withdrawn
            # in the SAME transaction that enters MANUAL_REQUIRED: leaving a
            # pending-remote-write phase that never happened on a terminal
            # job would (after a human requeue) bypass the EXTERNALLY_DISABLED
            # guard and could auto-enable a task a human disabled.
            evidence["restart"] = {
                "phase": "PRE_WRITE_FAILED",
                "phase_at": now.astimezone(UTC).isoformat(timespec="seconds"),
                "failed_status": status,
                "pre_write_failures": consecutive,
                "diagnostic": _text(outcome.get("diagnostic")) or status,
                "manual_reason": "CONSECUTIVE_PRE_WRITE_FAILURE_LIMIT",
            }
            store.mark_remote_zero_write_manual(
                token,
                job_id,
                remote_identity=_text(job.get("remote_identity")),
                evidence=evidence,
                error=(
                    f"remote stall recovery failed pre-write {consecutive} consecutive "
                    f"times; no remote write was dispatched; last failure: "
                    f"{_text(outcome.get('diagnostic')) or status}"
                ),
            )
            return outcome
        evidence["restart"] = {
            "phase": "PRE_WRITE_FAILED",
            "phase_at": now.astimezone(UTC).isoformat(timespec="seconds"),
            "failed_status": status,
            "pre_write_failures": consecutive,
            "diagnostic": _text(outcome.get("diagnostic")) or status,
        }
        store.update_remote_recovery_evidence(token, job_id, evidence)
        return outcome
    if status in {"DISABLE_UNKNOWN", "ENABLE_UNKNOWN"}:
        phase = "DISABLE_SENT" if status == "DISABLE_UNKNOWN" else "ENABLE_SENT"
        restart["phase"] = phase
        restart["diagnostic"] = _text(outcome.get("diagnostic")) or status
        restart["observed_enabled"] = outcome.get("observed_enabled")
        store.update_remote_recovery_evidence(token, job_id, evidence)
        return outcome
    if status in {"DISABLE_UNCONFIRMED", "ENABLE_UNCONFIRMED"}:
        window_expired = (
            phase_at is not None
            and (now - phase_at).total_seconds() >= RECOVERY_WRITE_CONFIRM_WINDOW_SECONDS
        )
        if window_expired:
            store.mark_remote_manual(
                token,
                job_id,
                f"remote stall recovery write unconfirmed: {status}; "
                f"resume_phase={resume_phase or 'DISABLE_SENT'}",
            )
        else:
            restart["diagnostic"] = f"{status}; awaiting reconciliation"
            store.update_remote_recovery_evidence(token, job_id, evidence)
        return outcome
    if status in {"IDENTITY_CONFLICT", "NOT_RUNNING", "EXTERNALLY_DISABLED"}:
        reason = (
            "remote stall recovery found the exact task externally disabled; "
            "no durable evidence proves the disable belongs to this protocol"
            if status == "EXTERNALLY_DISABLED"
            else f"remote stall recovery exact identity failure: {status}"
        )
        error = f"{reason}; {_text(outcome.get('diagnostic'))}"
        if resume_phase is None:
            # All three statuses are proven zero-write outcomes of a FRESH
            # protocol: the write-ahead DISABLE_SENT above never became a
            # real dispatch, so the same invariant as the pre-write cap
            # applies — no fake pending phase may survive the terminal
            # manual state (one transaction: evidence + state + error).
            # Resumed phases keep their real dispatched direction and the
            # plain manual transition.
            evidence["restart"] = {
                "phase": status,
                "phase_at": now.astimezone(UTC).isoformat(timespec="seconds"),
                "failed_status": status,
                "diagnostic": _text(outcome.get("diagnostic")) or status,
                "manual_reason": "ZERO_WRITE_OUTCOME",
            }
            store.mark_remote_zero_write_manual(
                token,
                job_id,
                remote_identity=_text(job.get("remote_identity")),
                evidence=evidence,
                error=error,
            )
        else:
            store.mark_remote_manual(token, job_id, error)
        return outcome
    if status == "NO_LONGER_RUNNING":
        # A zero-write decline: the adapter proved the exact task is not
        # still running, so no side effect ever left this protocol.  The
        # write-ahead phase must be withdrawn, or a later pass would resume
        # as if a disable had been sent.  The job stays RUNNING/harvestable;
        # ordinary harvest reconciles the real terminal state.
        evidence["restart"] = {
            "phase": "DECLINED",
            "phase_at": now.astimezone(UTC).isoformat(timespec="seconds"),
            "declined_status": status,
            "diagnostic": _text(outcome.get("diagnostic")),
        }
        store.update_remote_recovery_evidence(token, job_id, evidence)
        return outcome
    restart["diagnostic"] = _text(outcome.get("diagnostic")) or status
    store.update_remote_recovery_evidence(token, job_id, evidence)
    return outcome


RECOVERY_EXHAUSTED_NO_RESULT_REASON = (
    "remote stall recovery exhausted; one exact disable->enable restart did "
    "not restore the task"
)


def _abandon_recovery_exhausted(
    store: DailyAuditStore,
    token: LeaseToken,
    job: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    """Move one unrecoverable job to the local give-up terminal state.

    The remote task is NOT marked completed: the durable state records the
    business decision to stop waiting.  Every linked crawl becomes an
    auditable NO_RESULT carrying the full recovery evidence chain; no price
    is fabricated anywhere.
    """
    job_id = int(job["id"])
    restart = dict(evidence.get("restart") or {})
    disable_evidence = dict(restart.get("disable_evidence") or {})
    enable_evidence = dict(restart.get("enable_evidence") or {})
    observations = list(evidence.get("observations") or [])
    last_observation = observations[-1] if observations else {}
    store.mark_remote_recovery_exhausted(
        token,
        job_id,
        remote_identity=_text(job.get("remote_identity")),
        evidence=dict(evidence),
    )
    _complete_recovery_exhausted_crawls(
        store,
        token,
        job,
        restart_phase=_text(restart.get("phase")),
        recovery_at=_text(restart.get("recovery_at")),
        disable_confirmed=bool(disable_evidence.get("confirmed_disabled")),
        enable_confirmed=bool(enable_evidence.get("confirmed_enabled")),
        last_progress_signature=last_observation.get("signature"),
        export_count=last_observation.get("export_count"),
        export_rsp_desc=_text(last_observation.get("export_rsp_desc")),
    )


def _complete_recovery_exhausted_crawls(
    store: DailyAuditStore,
    token: LeaseToken,
    job: Mapping[str, Any],
    *,
    restart_phase: str,
    recovery_at: str,
    disable_confirmed: bool,
    enable_confirmed: bool,
    last_progress_signature: object,
    export_count: object,
    export_rsp_desc: str,
) -> int:
    converted = 0
    for crawl in store.crawl_work_for_job(int(job["id"])):
        if str(crawl["state"]) in {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"}:
            continue
        store.complete_crawl_work_recovery_exhausted(
            token,
            int(crawl["id"]),
            remote_job_id=int(job["id"]),
            remote_identity=_text(job.get("remote_identity")),
            reason=RECOVERY_EXHAUSTED_NO_RESULT_REASON,
            restart_phase=restart_phase,
            recovery_attempt=1,
            recovery_at=recovery_at,
            disable_confirmed=disable_confirmed,
            enable_confirmed=enable_confirmed,
            last_progress_signature=last_progress_signature,
            export_count=export_count,
            export_rsp_desc=export_rsp_desc,
        )
        converted += 1
    return converted


def run_running_recovery_pass(
    store: DailyAuditStore,
    token: LeaseToken,
    *,
    probe_remote: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    restart_remote: Callable[[Mapping[str, Any], str | None], Mapping[str, Any]] | None = None,
    now: datetime,
    can_start: Callable[[], bool],
    max_probe_candidates: int = RECOVERY_MAX_PROBE_CANDIDATES_PER_TICK,
) -> dict[str, int]:
    """One bounded recovery sweep over the durable RUNNING jobs.

    Jobs carrying a durable pending restart phase (a remote write already
    dispatched) reconcile that phase first, without re-proving the original
    stall.  Ordinary RUNNING jobs get read-only probes appending durable
    progress observations; a job whose evidence proves a stall may consume
    its single automatic restart when (and only when) a restart callback is
    wired in; with no callback the pass only records evidence and leaves the
    job RUNNING.  The sweep is bounded: ``can_start`` is re-checked before
    every candidate and at most ``max_probe_candidates`` probes run per pass
    so the rest of the tick's budget stays with core intake work.
    """
    counters = {
        "recovery_probes": 0,
        "recovery_probe_attempts": 0,
        "recovery_restarts": 0,
        "recovery_exhausted": 0,
        "recovery_reconciled": 0,
    }
    for job in store.list_remote_jobs(token):
        if not can_start():
            break
        if str(job.get("state")) == "RECOVERY_EXHAUSTED":
            # Crash-recovery sweep: a terminal give-up whose linked crawls were
            # not all converted must be finished here, or ordinary finalize
            # would wedge on those pending rows forever.
            document = decode_recovery_evidence(job.get("recovery_evidence_json"))
            restart = dict(document.get("restart") or {})
            observations = list(document.get("observations") or [])
            last_observation = observations[-1] if observations else {}
            _complete_recovery_exhausted_crawls(
                store,
                token,
                job,
                restart_phase=_text(restart.get("phase")),
                recovery_at=_text(restart.get("recovery_at")),
                disable_confirmed=bool((restart.get("disable_evidence") or {}).get("confirmed_disabled")),
                enable_confirmed=bool((restart.get("enable_evidence") or {}).get("confirmed_enabled")),
                last_progress_signature=last_observation.get("signature"),
                export_count=last_observation.get("export_count"),
                export_rsp_desc=_text(last_observation.get("export_rsp_desc")),
            )
            continue
        # Write-side-effect reconciliation first: a durable pending phase
        # means a remote disable/enable already left this protocol, so the
        # task's progress can never re-prove the original stall (a disabled
        # task answers no ACTIVE evidence).  Reconcile the phase directly —
        # the adapter re-verifies the exact task/mall identity on every
        # lookup and never repeats the already-sent write direction — and
        # only fall through to fresh stall detection when no side effect is
        # outstanding.  Like the fresh stalled path, reconciliation is legal
        # only for RUNNING jobs: a manual terminal keeps its stale phase and
        # a race can leave one on a COMPLETED job (whose evidence is
        # immutable), so a non-RUNNING state must never re-dispatch here.
        document = decode_recovery_evidence(job.get("recovery_evidence_json"))
        pending_restart = document.get("restart")
        if (
            restart_remote is not None
            and str(job.get("state")) == "RUNNING"
            and isinstance(pending_restart, Mapping)
            and _text(pending_restart.get("phase")) in PENDING_RESTART_PHASES
        ):
            if not can_start():
                # Reconciliation needs the minimal budget for one bounded
                # callback; without it the next tick retries the phase.
                # Defense-in-depth behind the loop-head gate: nothing between
                # the two checks consumes budget today, but the guard keeps
                # the branch self-contained if that ever changes.
                continue
            outcome = _apply_restart(store, token, job, document, restart_remote, now=now)
            counters["recovery_restarts"] += int(bool(outcome.get("restarted")))
            counters["recovery_reconciled"] += int(bool(outcome.get("reconciled")))
            continue
        decision = evaluate_running_stall(job, now=now)
        if not decision["probe_candidate"]:
            continue
        if counters["recovery_probe_attempts"] >= max_probe_candidates:
            # Budget-shaped cap on ATTEMPTS: a probe allowance consumed by
            # failed probes (platform outage) must protect the remaining
            # candidates exactly like successful ones would.
            continue
        counters["recovery_probe_attempts"] += 1
        probe = probe_remote(job)
        probe_failed = isinstance(probe, Mapping) and probe.get("probe_failed") is True
        if probe_failed or not isinstance(probe, Mapping):
            # A probe that ran but produced no legal read-only evidence is a
            # retryable fault — but a PERSISTENT one must fail closed, or a
            # permanently broken probe (expired refreshToken, missing tool,
            # transport outage) leaves the job RUNNING forever.  Counting is
            # scoped to this recovery probe stage only; probe health lives
            # beside (never inside) restart.phase because a probe failure
            # has not entered the restart protocol.
            store.record_remote_recovery_probe_failure(
                token,
                int(job["id"]),
                remote_identity=_text(job.get("remote_identity")),
                failed_at=now.astimezone(UTC).isoformat(timespec="seconds"),
                failure_reason=(
                    _text(probe.get("diagnostic"))
                    if probe_failed
                    else "probe callback returned no evidence"
                ),
            )
            continue
        counters["recovery_probes"] += 1
        evidence = decode_recovery_evidence(job.get("recovery_evidence_json"))
        if evidence.get("probe_health") is not None:
            # A legal observation proves the probe path works: the streak
            # ends in the SAME evidence write that appends the observation
            # (one transaction — no crash gap can resurrect the old count).
            del evidence["probe_health"]
        evidence["observations"].append(observation_from_probe(probe, now=now))
        job = store.update_remote_recovery_evidence(token, int(job["id"]), evidence)
        decision = evaluate_running_stall(job, now=now)
        if decision["exhausted_ready"]:
            _abandon_recovery_exhausted(
                store, token, job, decode_recovery_evidence(job.get("recovery_evidence_json"))
            )
            counters["recovery_exhausted"] += 1
            continue
        if decision["stalled"] and restart_remote is not None:
            if not _probe_identity_matches(job, probe):
                # The write path re-validates identity itself; a probe that
                # cannot re-confirm the exact durable identity must stop the
                # restart before any remote write is dispatched.
                store.mark_remote_manual(
                    token,
                    int(job["id"]),
                    "remote stall probe identity no longer matches the durable remote identity",
                )
                continue
            if not can_start():
                # A fresh restart is deferrable tail work: the probe may have
                # consumed the remaining headroom, so the freshly saved
                # observation waits for a tick with budget.
                continue
            outcome = _apply_restart(
                store,
                token,
                job,
                decode_recovery_evidence(job.get("recovery_evidence_json")),
                restart_remote,
                now=now,
            )
            counters["recovery_restarts"] += int(bool(outcome.get("restarted")))
            counters["recovery_reconciled"] += int(bool(outcome.get("reconciled")))
    return counters
