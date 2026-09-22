# Pure remote-state and durable-evidence policy shared by stores and adapters.
from __future__ import annotations
import json
from enum import Enum
from typing import Any, Mapping

PROGRESS_FIELDS: tuple[str, ...] = (
    "total",
    "pending",
    "running",
    "failed",
    "waiting_db",
    "finished",
)

TERMINAL_STATUS_MARKERS: tuple[str, ...] = ("本次任务执行完毕",)

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
    from app.services.runtime_context import RuntimeConfigurationError

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
    from app.services.runtime_context import RuntimeConfigurationError

    if isinstance(exc, (RuntimeConfigurationError, ValueError, KeyError, TypeError)):
        return "IDENTITY_CONFLICT"
    if isinstance(exc, RuntimeError):
        message = str(exc)
        if any(marker in message for marker in ("不唯一", "不一致", "没有拿到", "identity")):
            return "IDENTITY_CONFLICT"
    return "PRE_WRITE_FAILED"

def _text(value: object) -> str:
    return str(value or "").strip()

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
        return {"version": 1, "observations": [], "restart": None, "probe_health": None, "submit_reconcile": None}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"version": 1, "observations": [], "restart": None, "probe_health": None, "submit_reconcile": None}
    if not isinstance(decoded, dict):
        return {"version": 1, "observations": [], "restart": None, "probe_health": None, "submit_reconcile": None}
    observations = decoded.get("observations")
    if not isinstance(observations, list):
        observations = []
    restart = decoded.get("restart")
    if not isinstance(restart, dict):
        restart = None
    probe_health = decoded.get("probe_health")
    if not isinstance(probe_health, dict):
        probe_health = None
    submit_reconcile = decoded.get("submit_reconcile")
    if not isinstance(submit_reconcile, dict):
        submit_reconcile = None
    return {
        "version": 1,
        "observations": [row for row in observations if isinstance(row, dict)],
        "restart": restart,
        "probe_health": probe_health,
        "submit_reconcile": submit_reconcile,
    }

def encode_recovery_evidence(evidence: Mapping[str, Any]) -> str:
    return json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

RECOVERY_MAX_CONSECUTIVE_PROBE_FAILURES = 5
