"""Dashboard operator actions on one exact existing Zhaoshang remote job.

人工重新导出/重建只针对 durable 已有任务,永不 create;全部走共享 harvest
core / supersede 契约,保留 exact identity、immutable payload、linked crawl、
ProcessLock、durable lease、fail-closed 语义。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from app.services.daily_audit_store import (
    DailyAuditStore,
    LeaseUnavailable,
    ProcessLock,
    RemoteIntentConflict,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    LegacyRestartOutcome,
    PreparedLegacyJob,
    RestartStatus,
    SubmitStatus,
)
from app.services.recovery.common import RecoveryRejected, _acquire_recovery_lease
from app.services.recovery.harvest import (
    _existing_remote_harvest_core,
    is_priority_unconfirmed_manual_family,
    live_identity_proof,
)
from app.services.recovery.no_result import EXPORT_NO_PRICE_ACCEPTED_ERRORS
from app.services.recovery.wait_export import (
    WAIT_EXPORT_MANUAL_DIAGNOSTIC,
    WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC,
)
from app.services.remote_running_recovery import (
    RemoteExecutionState,
    canonical_progress_signature,
    classify_remote_execution_state,
)
from app.services.zhaoshang_progress_probe import ZhaoshangProgressService


# The only MANUAL diagnostics a dashboard re-export may requeue from: the
# wait-export family it can itself persist, plus the exact error texts the
# export no-price channel persists (which resolve a finished task whose export
# genuinely answered zero price rows -- re-exporting it later is legal and
# must stay one click away).
REEXPORT_MANUAL_REQUEUE_DIAGNOSTICS = frozenset(
    {WAIT_EXPORT_MANUAL_DIAGNOSTIC, WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC}
    | set(EXPORT_NO_PRICE_ACCEPTED_ERRORS)
)

REEXPORT_SUCCESS_OUTCOMES = frozenset(
    {
        "REEXPORT_COMPLETED",
        "REEXPORT_COMPLETED_NO_RESULT",
        "REEXPORT_STILL_RUNNING",
        "REEXPORT_MANUAL",
    }
)

# Source states a dashboard manual rebuild may retire.  The old job must carry
# durable POST evidence (post_attempted==1); pure intents, unknown submits,
# priority waits, completed work and already-superseded rows are not eligible.
REBUILD_ALLOWED_SOURCE_STATES = frozenset(
    {"SUBMITTED", "RUNNING", "EXPORTED", "RECOVERY_EXHAUSTED", "MANUAL_REQUIRED"}
)

# Crawl evidence that stays bound to the superseded job; only unfinished work
# migrates to the replacement.
REBUILD_TERMINAL_CRAWL_STATES = frozenset({"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"})

# Durable-success outcomes: the rebuild is fully reconciled, durably queued for
# the tick engine, awaiting only the mandatory submit-unknown reconcile, or the
# job already has a recorded replacement (idempotent replay).
REBUILD_SUCCESS_OUTCOMES = frozenset(
    {
        "REBUILD_DISPATCHED",
        "REBUILD_QUEUED",
        "REBUILD_SUBMIT_UNKNOWN",
        "REBUILD_ALREADY_REPLACED",
    }
)

# Write-ahead phases of the durable rebuild protocol
# (remote_job_rebuild_actions).  A row in any of these phases owns the rebuild
# protocol for its old job: a later click resumes the same action instead of
# starting a competing one.  FAILED rows never block a fresh protocol.
REBUILD_ACTION_ACTIVE_PHASES = frozenset(
    {"INTENT", "STOP_SENT", "STOP_CONFIRMED", "TERMINAL_PROOF", "CREATE_SENT"}
)

# The runtime persists every legacy browser task as this exact display name
# (incremental_audit_runtime legacy bridge); a replacement must keep the form.
_REPLACEMENT_NAME_PATTERN = "cnnc_current_opt_{mall}_{digits}"


def _replacement_task_name(old_task_name: str, action_id: str, mall_name: str) -> str:
    """Derive the fresh batch/task name for one manual rebuild.

    Same shape the runtime itself persists -- ``cnnc_current_opt_{mall_name}``
    plus a 14-digit numeric suffix -- but seeded from (old task name, action
    id) so every rebuild action names a brand-new remote task and can never
    collide with the retired one.  A collision with the old name re-salts
    defensively (14 digits from a 256-bit digest make it practically
    impossible).
    """
    salt = f"{old_task_name}|{action_id}"
    name = ""
    for attempt in range(10):
        digest = hashlib.sha256(salt.encode("utf-8")).hexdigest()
        digits = f"{int(digest, 16) % (10 ** 14):014d}"
        candidate = _REPLACEMENT_NAME_PATTERN.format(mall=mall_name, digits=digits)
        if candidate != old_task_name:
            name = candidate
            break
        salt = f"{old_task_name}|{action_id}|{attempt + 1}"
    if not re.fullmatch(rf"cnnc_current_opt_{re.escape(mall_name)}_\d{{14}}", name):
        raise RecoveryRejected(
            "replacement task name could not be derived in the durable form"
        )
    return name



def _reexport_rejection_code(reason: str) -> str:
    """Map one shared-core rejection reason onto a stable dashboard code."""
    text = str(reason or "")
    if (
        "identity does not exactly match" in text
        or "evidence is inconsistent" in text
        or "payload cannot be reconstructed" in text
        or ("exact" in text and "evidence" in text)
        or "exact remote identity is required" in text
        or "not harvestable" in text
        or "could not enter harvest" in text
        or "requires durable POST evidence" in text
        or "terminal progress does not exactly match durable request scope" in text
    ):
        return "INVALID_REMOTE_STATE"
    if (
        "frozen daily run" in text
        or "frozen snapshot" in text
        or "snapshot scope" in text
    ):
        return "NO_SNAPSHOT_SCOPE"
    return "REEXPORT_REJECTED"


def _probe_remote_execution_state(
    job: Mapping[str, Any],
    *,
    business_date: str,
    adapter: CnncLegacyZhaoshangAdapter,
    identity_parts: list[str],
    timeout_seconds: float,
) -> tuple[
    RemoteExecutionState,
    int,
    tuple[int, int, int, int, int, int] | None,
]:
    """Read-only live classification of the exact remote task, fail-closed.

    Rebuilds the prepared job from the durable payload exactly like the
    harvest core does, then asks the adapter's progress seam.  Any failure --
    payload reconstruction, missing local files, probe transport, business
    failure, unparseable counters -- is UNKNOWN: no export is dispatched.
    Returns the classification plus the read-only remote call count the probe
    answer itself reports (0 when nothing parseable came back), so the action
    record can account for the preflight's remote calls.
    """
    try:
        payload = json.loads(str(job.get("payload_json") or ""))
        requests = tuple(CrawlRequest(**dict(row)) for row in payload["requests"])
        batch_state = Path(str(payload["batch_state"]))
        prepared = adapter.prepare_job(
            business_date,
            requests,
            Path(str(job.get("input_path") or "")),
            Path(str(job.get("result_path") or "")),
            str(job["task_name"]),
            batch_state,
        )
    except Exception:
        return RemoteExecutionState.UNKNOWN, 0, None
    try:
        answer = adapter.query_task_progress_for_job(
            prepared,
            identity_parts[1],
            identity_parts[2],
            min(30.0, timeout_seconds),
        )
    except Exception:
        return RemoteExecutionState.UNKNOWN, 0, None
    probe_remote_calls = 0
    if isinstance(answer, Mapping):
        try:
            probe_remote_calls = max(0, int(answer.get("remote_calls") or 0))
        except (TypeError, ValueError):
            probe_remote_calls = 0
    progress = answer.get("progress") if isinstance(answer, Mapping) else None
    progress_mapping = progress if isinstance(progress, Mapping) else None
    return (
        classify_remote_execution_state(progress_mapping, ""),
        probe_remote_calls,
        canonical_progress_signature(progress_mapping or {}),
    )


def _is_priority_unconfirmed_manual_job(job: Mapping[str, Any]) -> bool:
    """Strict "priority update never confirmed" durable shape (2026-09-04 hotfix).

    Exactly the shared strict family (see
    :func:`app.services.recovery.harvest.is_priority_unconfirmed_manual_family`):
    MANUAL_REQUIRED + durable POST evidence (post_attempted==1) + the bare
    legacy zero-rows diagnostic as error_text + a result_json that parses to
    ``{"state": "PRIORITY_PENDING", "priority_evidence": null}``.  Review
    round 2: NULL/empty/malformed result_json no longer counts as "null
    evidence" -- anything that is not exactly the strict shape fails closed,
    and no other MANUAL error family and no recorded priority evidence joins
    the live-proof fallback.
    """
    return is_priority_unconfirmed_manual_family(job)


def _probe_live_identity_classification(
    job: Mapping[str, Any],
    *,
    prepared: PreparedLegacyJob,
    adapter: CnncLegacyZhaoshangAdapter,
    business_date: str,
    identity_parts: list[str],
    timeout_seconds: float,
    progress_lookup: Callable[[PreparedLegacyJob, str, str, float], Mapping[str, Any]]
    | None,
) -> RemoteExecutionState:
    """The same exact live progress probe the re-export preflight runs.

    Read-only.  The adapter's exact lookup raises on any live/durable identity
    conflict and every transport/business/parse failure is UNKNOWN, so only a
    genuinely proven ACTIVE/TERMINAL answer can come back.  An injected
    ``progress_lookup`` (tests, alternative seams) is honored exactly like the
    stop-protocol precheck does.
    """
    if progress_lookup is not None:
        try:
            answer = progress_lookup(
                prepared,
                identity_parts[1],
                identity_parts[2],
                min(30.0, float(timeout_seconds)),
            )
            raw = answer.get("progress") if isinstance(answer, Mapping) else None
        except Exception:
            return RemoteExecutionState.UNKNOWN
        return classify_remote_execution_state(
            raw if isinstance(raw, Mapping) else None,
            "",
        )
    state, _probe_remote_calls, _progress_signature = _probe_remote_execution_state(
        job,
        business_date=business_date,
        adapter=adapter,
        identity_parts=identity_parts,
        timeout_seconds=timeout_seconds,
    )
    return state


def run_manual_reexport(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    remote_job_id: int,
    remote_identity: str,
    adapter: CnncLegacyZhaoshangAdapter,
    timeout_seconds: float = 180.0,
    progress_lookup: Callable[[Mapping[str, Any]], str | None] | None = None,
) -> dict[str, Any]:
    """Manually re-export one exact existing remote job; never create.

    Mandatory read-only preflight: the durable job must exist in this run with
    the exact durable identity, and the live remote execution state must be
    provably ACTIVE or TERMINAL before any export is dispatched.  An UNKNOWN
    state rejects with zero durable writes.  A proven probe answer becomes the
    factory-built live identity proof handed to the shared harvest core, whose
    exact-priority-evidence gate admits the "remote task intact, priority
    never confirmed" shape only through that proof (2026-09-04 hotfix).  The
    export itself always runs the shared harvest core (exact identity,
    immutable payload, linked crawls, ProcessLock + durable lease,
    latest-or-frozen snapshot scope, manual requeue only for the whitelisted
    diagnostics plus that one proven-proof family); no create channel exists
    on this path (``create_calls`` is always 0).  ``remote_calls`` and
    ``remote_calls_total`` account for the preflight probe plus the export
    itself, and the breakdown is recorded per click.
    """
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    expected_identity = str(remote_identity or "").strip()
    base: dict[str, Any] = {
        "business_date": business_date,
        "remote_job_id": int(remote_job_id),
        "remote_identity": expected_identity,
    }
    identity_parts = expected_identity.split("|")
    if len(identity_parts) != 3 or not all(identity_parts):
        return {
            **base,
            "outcome": "REEXPORT_REJECTED",
            "error_code": "INVALID_REMOTE_STATE",
            "reason": "exact remote identity is required",
            "preflight_remote_calls": 0,
            "export_remote_calls": 0,
            "remote_calls_total": 0,
            "remote_calls": 0,
            "create_calls": 0,
        }
    if timeout_seconds <= 0:
        raise RecoveryRejected("manual re-export timeout must be positive")

    store = DailyAuditStore(path)
    job = store.get_remote_job(int(remote_job_id))
    run = store.get_run(business_date)
    if job is None or run is None or int(job["run_id"]) != int(run["id"]):
        return {
            **base,
            "outcome": "REEXPORT_REJECTED",
            "error_code": "IDENTITY_MISMATCH",
            "reason": f"remote job id does not exist in this run: {remote_job_id}",
            "preflight_remote_calls": 0,
            "export_remote_calls": 0,
            "remote_calls_total": 0,
            "remote_calls": 0,
            "create_calls": 0,
        }
    if str(job.get("remote_identity") or "").strip() != expected_identity:
        return {
            **base,
            "outcome": "REEXPORT_REJECTED",
            "error_code": "IDENTITY_MISMATCH",
            "reason": "remote identity does not exactly match durable state",
            "preflight_remote_calls": 0,
            "export_remote_calls": 0,
            "remote_calls_total": 0,
            "remote_calls": 0,
            "create_calls": 0,
        }

    if progress_lookup is not None:
        reported = str(progress_lookup(job) or "").strip()
        classification = (
            RemoteExecutionState.ACTIVE
            if reported == RemoteExecutionState.ACTIVE.value
            else RemoteExecutionState.TERMINAL
            if reported == RemoteExecutionState.TERMINAL.value
            else RemoteExecutionState.UNKNOWN
        )
        preflight_remote_calls = 0
        progress_signature = None
    else:
        (
            classification,
            preflight_remote_calls,
            progress_signature,
        ) = _probe_remote_execution_state(
            job,
            business_date=business_date,
            adapter=adapter,
            identity_parts=identity_parts,
            timeout_seconds=timeout_seconds,
        )
    export_remote_calls = 0

    if classification is RemoteExecutionState.UNKNOWN:
        return {
            **base,
            "outcome": "REEXPORT_REJECTED",
            "error_code": "REMOTE_PROGRESS_UNKNOWN",
            "reason": (
                "remote execution state could not be proven; no export was dispatched"
            ),
            "progress_classification": RemoteExecutionState.UNKNOWN.value,
            "preflight_remote_calls": preflight_remote_calls,
            "export_remote_calls": 0,
            "remote_calls_total": preflight_remote_calls + export_remote_calls,
            "remote_calls": preflight_remote_calls + export_remote_calls,
            "create_calls": 0,
        }

    # 2026-09-04 hotfix: the proven probe answer becomes the factory-built
    # live identity proof for the shared core's exact-priority-evidence gate,
    # so the "remote task intact, priority never confirmed" shape is no longer
    # rejected as INVALID_REMOTE_STATE.  The proof is read-only evidence; the
    # core still validates identity, payload, crawls, lock and lease itself.
    proof = live_identity_proof(
        remote_identity=expected_identity,
        classification=classification,
        progress_signature=progress_signature,
    )

    try:
        core_result = _existing_remote_harvest_core(
            state_path=state_path,
            lock_path=lock_path,
            business_date=business_date,
            remote_job_id=remote_job_id,
            remote_identity=remote_identity,
            adapter=adapter,
            timeout_seconds=timeout_seconds,
            admissible_states=frozenset({"SUBMITTED", "RUNNING", "EXPORTED"}),
            allow_manual_requeue=True,
            manual_requeue_diagnostics=REEXPORT_MANUAL_REQUEUE_DIAGNOSTICS,
            source_scope="latest",
            operation="manual re-export",
            attempt_headroom=2,
            live_identity_proof=proof,
        )
    except LeaseUnavailable as exc:
        return {
            **base,
            "outcome": "REEXPORT_REJECTED",
            "error_code": "LOCK_CONFLICT",
            "reason": str(exc),
            "progress_classification": classification.value,
            "preflight_remote_calls": preflight_remote_calls,
            "export_remote_calls": 0,
            "remote_calls_total": preflight_remote_calls,
            "remote_calls": preflight_remote_calls,
            "create_calls": 0,
        }
    except RecoveryRejected as exc:
        return {
            **base,
            "outcome": "REEXPORT_REJECTED",
            "error_code": _reexport_rejection_code(str(exc)),
            "reason": str(exc),
            "progress_classification": classification.value,
            "preflight_remote_calls": preflight_remote_calls,
            "export_remote_calls": 0,
            "remote_calls_total": preflight_remote_calls,
            "remote_calls": preflight_remote_calls,
            "create_calls": 0,
        }

    result = dict(core_result)
    result["business_date"] = business_date
    result["remote_job_id"] = int(remote_job_id)
    result["remote_identity"] = expected_identity
    result["progress_classification"] = classification.value
    export_remote_calls = int(core_result.get("remote_calls") or 0)
    remote_calls_total = preflight_remote_calls + export_remote_calls
    result["preflight_remote_calls"] = preflight_remote_calls
    result["export_remote_calls"] = export_remote_calls
    result["remote_calls_total"] = remote_calls_total
    result["remote_calls"] = remote_calls_total
    result["create_calls"] = 0
    if classification is RemoteExecutionState.ACTIVE:
        result["still_active"] = True
    core_outcome = str(core_result.get("outcome") or "")
    if core_outcome == "REMOTE_EXISTING_HARVEST_RUNNING":
        result["outcome"] = "REEXPORT_STILL_RUNNING"
    elif core_outcome == "REMOTE_EXISTING_HARVEST_MANUAL":
        result["outcome"] = "REEXPORT_MANUAL"
    else:
        validated = int(core_result.get("validated") or 0)
        result["result_count"] = validated
        result["outcome"] = (
            "REEXPORT_COMPLETED" if validated > 0 else "REEXPORT_COMPLETED_NO_RESULT"
        )
    return result


def record_operator_action(
    run_dir: Path | str,
    *,
    action_type: str,
    action_id: str,
    payload: Mapping[str, Any],
    now: datetime | None = None,
) -> Path:
    """Append one audit record to the run's dashboard_actions.jsonl.

    Single-writer subprocess JSONL (same write+flush+fsync pattern as the
    runtime's BATCH_STATE recorder, simplified): every dashboard operator
    action lands as one self-describing line, newest last.
    """
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "dashboard_actions.jsonl"
    record = {
        "recorded_at": (now or datetime.now(UTC)).isoformat(timespec="milliseconds"),
        "action_type": str(action_type),
        "action_id": str(action_id),
        **dict(payload),
    }
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
    return path


def read_operator_actions(run_dir: Path | str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Read the last ``limit`` dashboard operator actions; corrupt lines skip."""
    path = Path(run_dir) / "dashboard_actions.jsonl"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        if len(rows) >= limit:
            break
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            rows.append(record)
    rows.reverse()
    return rows


def reconstruct_prepared_job(state_path, job: Mapping[str, Any]) -> PreparedLegacyJob:
    """Rebuild the ``PreparedLegacyJob`` from one durable remote_jobs row.

    The run's business date is recovered from the run directory (the state
    database always lives at ``<runtime_root>/<business_date>/state.sqlite3``),
    so read-only observers (the progress service) can reconstruct the exact
    job without any extra context.  Any malformed durable payload rejects
    with ``RecoveryRejected``; the callers inside the re-export preflight and
    the rebuild protocol keep their own injected-adapter call sites.
    """
    try:
        payload = json.loads(str(job.get("payload_json") or ""))
        requests = tuple(CrawlRequest(**dict(row)) for row in payload["requests"])
        batch_state = Path(str(payload["batch_state"]))
        input_path = Path(str(job.get("input_path") or ""))
        result_path = Path(str(job.get("result_path") or ""))
        task_name = str(job["task_name"])
        business_date = Path(state_path).resolve().parent.name
    except RecoveryRejected:
        raise
    except Exception as exc:
        raise RecoveryRejected(
            f"durable legacy payload cannot be reconstructed: {exc}"
        ) from exc
    try:
        return CnncLegacyZhaoshangAdapter().prepare_job(
            business_date,
            requests,
            input_path,
            result_path,
            task_name,
            batch_state,
        )
    except RecoveryRejected:
        raise
    except Exception as exc:
        raise RecoveryRejected(
            f"prepared legacy job cannot be rebuilt: {exc}"
        ) from exc


_progress_service_by_state: dict[str, ZhaoshangProgressService] = {}
_PROGRESS_SERVICE_STATE_LOCK = threading.Lock()


def get_progress_service_for(state_path) -> ZhaoshangProgressService:
    """One process-lifetime progress service per durable state database.

    The service's prepared_resolver is bound to one state_path, so services
    are cached per resolved state path; tests may monkeypatch this factory or
    inject their own service through ``refresh_job_progress(service=...)``.
    """
    key = str(Path(state_path).resolve())
    with _PROGRESS_SERVICE_STATE_LOCK:
        service = _progress_service_by_state.get(key)
        if service is None:
            resolved_state = Path(key)
            service = ZhaoshangProgressService(
                prepared_resolver=lambda job: reconstruct_prepared_job(
                    resolved_state, job
                )
            )
            _progress_service_by_state[key] = service
        return service


def refresh_job_progress(
    state_path,
    job: Mapping[str, Any],
    *,
    max_wait_seconds: float = 10.0,
    service: ZhaoshangProgressService | None = None,
) -> dict[str, Any]:
    """Force one read-only progress query for a single durable remote job.

    Never spawns a subprocess: the progress service owns the rate limit
    (a repeat click inside the window returns the cached entry with
    ``rate_limited=True``) and every failure degrades to an unavailable
    status dict instead of raising.
    """
    active = service if service is not None else get_progress_service_for(state_path)
    status = dict(
        active.refresh(job, max_wait_seconds=float(max_wait_seconds))
    )
    status["job_id"] = job.get("id")
    status["task_name"] = str(job.get("task_name") or "")
    return status


def run_manual_reexport_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    remote_job_id: int,
    remote_identity: str,
    timeout_seconds: float = 180.0,
    adapter: CnncLegacyZhaoshangAdapter | None = None,
    action_id: str | None = None,
) -> dict[str, Any]:
    """Runtime-root wrapper for one dashboard manual re-export click."""
    from app.services.incremental_audit_runtime import runtime_paths

    paths = runtime_paths(business_date, runtime_root)
    result = run_manual_reexport(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        remote_job_id=remote_job_id,
        remote_identity=remote_identity,
        adapter=adapter if adapter is not None else CnncLegacyZhaoshangAdapter(),
        timeout_seconds=timeout_seconds,
    )
    result.setdefault("business_date", business_date)
    record_operator_action(
        paths.run_dir,
        action_type="manual_reexport",
        action_id=(
            str(action_id)
            if action_id
            else f"reexport-{int(remote_job_id)}-{time.time_ns()}"
        ),
        payload={
            "remote_job_id": int(remote_job_id),
            "remote_identity": str(remote_identity or "").strip(),
            "outcome": result.get("outcome"),
            "error_code": result.get("error_code"),
            "reason": result.get("reason"),
            "progress_classification": result.get("progress_classification"),
            "preflight_remote_calls": result.get("preflight_remote_calls"),
            "export_remote_calls": result.get("export_remote_calls"),
            "remote_calls_total": result.get("remote_calls_total"),
            "remote_calls": result.get("remote_calls"),
            "create_calls": result.get("create_calls", 0),
        },
    )
    return result


def _resume_replaced_rebuild(
    store: DailyAuditStore,
    token: Any,
    base: dict[str, Any],
    *,
    job: Mapping[str, Any],
    replacement: Mapping[str, Any],
    adapter: CnncLegacyZhaoshangAdapter,
    business_date: str,
    timeout_seconds: float,
    state_path: Path,
    remote_calls: int,
    breakdown: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Answer a rebuild click for a job that already has a replacement row.

    Three durable shapes live behind the replacement: the new job still sits
    on an unburned SUBMIT_INTENT (its dispatch attempt never left this
    process -- resume it through the same burn-before-send claim), the claim
    is burned but the create outcome was never recorded (fail safe onto the
    tick reconcile; never a second create), or the replacement is already
    dispatched/terminal (pure informational replay with zero remote calls).
    """
    new_job_id = int(replacement["new_remote_job_id"])
    new_job = store.get_remote_job(new_job_id)
    if new_job is None or str(new_job["state"]) != "SUBMIT_INTENT":
        return {
            **base,
            "outcome": "REBUILD_ALREADY_REPLACED",
            "replacement": dict(replacement),
            "old_task_name": str(job["task_name"]),
            "new_task_name": str(replacement["new_batch_number"]),
            "new_batch_number": str(replacement["new_batch_number"]),
            "new_remote_job_id": new_job_id,
            "remote_calls": remote_calls,
        }
    action = store.get_latest_rebuild_action_for_job(int(job["id"]))
    resume_action_id = str(action["action_id"]) if action is not None else None

    def persist_phase(phase: str) -> None:
        if resume_action_id is not None:
            store.update_rebuild_action_phase(
                token, action_id=resume_action_id, phase=phase
            )

    result: dict[str, Any] = {
        **base,
        "old_task_name": str(job["task_name"]),
        "new_task_name": str(replacement["new_batch_number"]),
        "new_batch_number": str(replacement["new_batch_number"]),
        "old_remote_job_id": int(job["id"]),
        "new_remote_job_id": new_job_id,
        "replacement_id": int(replacement["id"]),
        "rebuild_action_id": resume_action_id,
        "rebuild_resumed": True,
    }
    if int(new_job["post_attempted"]) != 0:
        # Crash after the claim burned but before the create outcome was
        # durably recorded: the create may already have left this process.
        # Never re-submit -- hand the exact truth to the tick reconciler.
        store.mark_submit_unknown(
            token, new_job_id, "dashboard rebuild: dispatch interrupted after claim"
        )
        persist_phase("SUBMIT_UNKNOWN")
        return {
            **result,
            "outcome": "REBUILD_SUBMIT_UNKNOWN",
            "reason": (
                "durable replacement dispatch was interrupted after its claim; "
                "tick reconcile will confirm"
            ),
            "remote_calls": remote_calls,
        }
    try:
        new_prepared = reconstruct_prepared_job(state_path, new_job)
    except RecoveryRejected:
        return {
            **result,
            "outcome": "REBUILD_ALREADY_REPLACED",
            "reason": "replacement payload cannot be reconstructed; scheduler will dispatch",
            "remote_calls": remote_calls,
        }
    claim = store.claim_remote_submission(token, new_job_id)
    if claim is None:
        persist_phase("QUEUED")
        return {
            **result,
            "outcome": "REBUILD_QUEUED",
            "reason": "replacement submission claim unavailable; scheduler will dispatch",
            "remote_calls": remote_calls,
        }
    persist_phase("CREATE_SENT")
    remote_calls += 1
    if breakdown is not None:
        breakdown["create"] = breakdown.get("create", 0) + 1
    try:
        submit_outcome = adapter.submit(
            new_prepared, timeout_seconds=max(0.001, float(timeout_seconds))
        )
    except Exception as exc:
        store.mark_submit_unknown(
            token, new_job_id, f"dashboard rebuild: {type(exc).__name__}: {exc}"
        )
        persist_phase("SUBMIT_UNKNOWN")
        return {
            **result,
            "outcome": "REBUILD_SUBMIT_UNKNOWN",
            "reason": "legacy create-only dispatch raised; tick reconcile will confirm",
            "remote_calls": remote_calls,
        }
    if submit_outcome.status is SubmitStatus.NOT_ATTEMPTED:
        store.release_remote_submission_claim(
            token, new_job_id, f"dashboard rebuild: {submit_outcome.diagnostic}"
        )
        persist_phase("QUEUED")
        return {
            **result,
            "outcome": "REBUILD_QUEUED",
            "reason": (
                f"submit not attempted: {submit_outcome.diagnostic}; "
                "scheduler will dispatch"
            ),
            "remote_calls": remote_calls,
        }
    if submit_outcome.status is SubmitStatus.SUBMIT_UNKNOWN:
        store.mark_submit_unknown(token, new_job_id, "legacy create-only outcome unknown")
        persist_phase("SUBMIT_UNKNOWN")
        return {
            **result,
            "outcome": "REBUILD_SUBMIT_UNKNOWN",
            "reason": "legacy create-only outcome unknown; tick reconcile will confirm",
            "remote_calls": remote_calls,
        }
    if not submit_outcome.task_id or not submit_outcome.task_number:
        store.mark_submit_unknown(
            token, new_job_id, "dashboard rebuild: submit response had no remote identity"
        )
        persist_phase("SUBMIT_UNKNOWN")
        return {
            **result,
            "outcome": "REBUILD_SUBMIT_UNKNOWN",
            "reason": "submit response had no remote identity; tick reconcile will confirm",
            "remote_calls": remote_calls,
        }
    new_state = (
        "SUBMITTED"
        if submit_outcome.priority_evidence in {"updated", "already_highest_priority"}
        else "PRIORITY_PENDING"
    )
    new_identity = (
        f"{new_prepared.mall_key}|{submit_outcome.task_id}|{submit_outcome.task_number}"
    )
    store.reconcile_remote_job(
        token,
        new_job_id,
        remote_identity=new_identity,
        state=new_state,
        result={"priority_evidence": submit_outcome.priority_evidence},
    )
    if new_state != "PRIORITY_PENDING":
        store.advance_crawl_work_for_job(token, new_job_id, new_state)
    persist_phase("DISPATCHED")
    return {
        **result,
        "outcome": "REBUILD_DISPATCHED",
        "state": new_state,
        "new_remote_identity": new_identity,
        "priority_evidence": submit_outcome.priority_evidence,
        "remote_calls": remote_calls,
    }


def _run_manual_rebuild_once(
    *,
    state_path: Path,
    lock_path: Path,
    business_date: str,
    remote_job_id: int,
    remote_identity: str,
    adapter: CnncLegacyZhaoshangAdapter,
    action_id: str,
    timeout_seconds: float,
    reason: str,
    progress_lookup: Callable[[PreparedLegacyJob, str, str, float], Mapping[str, Any]] | None,
    stop_callable: Callable[[PreparedLegacyJob, str, str, float], LegacyRestartOutcome] | None,
    counters: dict[str, int] | None = None,
) -> dict[str, Any]:
    """One locked manual-rebuild attempt; every protocol answer is a dict.

    ``counters`` (2026-09-04 hotfix, optional out-parameter) accumulates the
    remote call breakdown by kind (``probe`` / ``stop`` / ``create``) so the
    operator action record can show it; it never changes protocol behavior.
    """
    expected_identity = str(remote_identity or "").strip()
    base: dict[str, Any] = {
        "action_id": action_id,
        "business_date": business_date,
        "remote_identity": expected_identity,
        "old_remote_job_id": int(remote_job_id),
    }
    store = DailyAuditStore(state_path)
    process_lock = ProcessLock(lock_path)
    remote_calls = 0
    breakdown = counters if counters is not None else {}

    def bump_remote(kind: str) -> None:
        nonlocal remote_calls
        remote_calls += 1
        breakdown[kind] = breakdown.get(kind, 0) + 1

    try:
        token = _acquire_recovery_lease(
            store,
            process_lock,
            business_date,
            f"dashboard-rebuild-{action_id}",
            max(300.0, float(timeout_seconds) + 120.0),
        )
    except LeaseUnavailable as exc:
        return {
            **base,
            "outcome": "REBUILD_REJECTED",
            "error_code": "LOCK_CONFLICT",
            "reason": str(exc),
            "remote_calls": remote_calls,
        }
    try:
        job = store.get_remote_job(int(remote_job_id))
        if job is None or int(job["run_id"]) != int(token.run_id):
            return {
                **base,
                "outcome": "REBUILD_REJECTED",
                "error_code": "JOB_NOT_FOUND",
                "reason": f"remote job id does not exist in this run: {remote_job_id}",
                "remote_calls": remote_calls,
            }
        if str(job.get("remote_identity") or "").strip() != expected_identity:
            return {
                **base,
                "outcome": "REBUILD_REJECTED",
                "error_code": "IDENTITY_MISMATCH",
                "reason": "remote identity does not exactly match durable state",
                "remote_calls": remote_calls,
            }
        # Idempotency answers before the source-state gate: a recorded
        # replacement row is the definitive durable answer, so a repeated
        # click (any action id) replays it with zero writes and zero remote
        # calls instead of tripping the SUPERSEDED state gate below.  A crash
        # between that supersede and its dispatch attempt resumes the durable
        # replacement instead of replaying blindly.
        existing = store.get_replacement_for_job(int(job["id"]))
        if existing is not None:
            return _resume_replaced_rebuild(
                store,
                token,
                base,
                job=job,
                replacement=existing,
                adapter=adapter,
                business_date=business_date,
                timeout_seconds=timeout_seconds,
                state_path=state_path,
                remote_calls=remote_calls,
                breakdown=breakdown,
            )
        job_state = str(job["state"])
        post_attempted = int(job["post_attempted"])
        if job_state not in REBUILD_ALLOWED_SOURCE_STATES or post_attempted != 1:
            return {
                **base,
                "outcome": "REBUILD_NOT_ALLOWED",
                "error_code": "REBUILD_NOT_ALLOWED",
                "reason": (
                    f"remote job state {job_state} with post_attempted="
                    f"{post_attempted} is not manually rebuildable"
                ),
                "state": job_state,
                "post_attempted": post_attempted,
                "remote_calls": remote_calls,
            }
        identity_parts = expected_identity.split("|")
        if len(identity_parts) != 3 or not all(identity_parts):
            return {
                **base,
                "outcome": "REBUILD_REJECTED",
                "error_code": "INVALID_REMOTE_STATE",
                "reason": "exact remote identity is required",
                "remote_calls": remote_calls,
            }
        task_id, task_number = identity_parts[1], identity_parts[2]

        def rejected_invalid(detail: str) -> dict[str, Any]:
            return {
                **base,
                "outcome": "REBUILD_REJECTED",
                "error_code": "INVALID_REMOTE_STATE",
                "reason": detail,
                "state": job_state,
                "remote_calls": remote_calls,
            }

        # Immutable payload reconstruction (same three-path proof as the
        # harvest core) plus exact durable priority evidence for the identity.
        try:
            payload = json.loads(str(job.get("payload_json") or ""))
            requests = tuple(CrawlRequest(**dict(row)) for row in payload["requests"])
            batch_state = Path(str(payload["batch_state"]))
            input_path = Path(str(job.get("input_path") or ""))
            result_path = Path(str(job.get("result_path") or ""))
        except Exception as exc:
            return rejected_invalid(f"durable legacy payload cannot be reconstructed: {exc}")
        if (
            not batch_state.is_file()
            or not input_path.is_file()
            or not result_path.parent.is_dir()
        ):
            return rejected_invalid("durable legacy paths are unavailable")
        try:
            prepared = adapter.prepare_job(
                business_date,
                requests,
                input_path,
                result_path,
                str(job["task_name"]),
                batch_state,
            )
        except Exception as exc:
            return rejected_invalid(f"prepared legacy job cannot be rebuilt: {exc}")
        if (
            prepared.logical_job_key != str(job["logical_key"])
            or prepared.payload_sha256 != str(job["payload_hash"])
            or prepared.mall_key != str(job.get("mall_key") or "")
        ):
            return rejected_invalid("durable legacy job evidence is inconsistent")
        # 2026-09-04 hotfix: historical accepted priority evidence stays
        # mandatory.  The only fallback is the narrow priority-unconfirmed
        # family (MANUAL_REQUIRED + post_attempted==1 + the exact bare legacy
        # zero-rows error + null durable priority_evidence) proven live by the
        # same exact read-only probe the re-export preflight runs: only a
        # genuinely proven ACTIVE/TERMINAL exact-identity answer re-opens the
        # gate; UNKNOWN, conflicts and failures all fail closed below.
        live_proof_classification: RemoteExecutionState | None = None
        if adapter.exact_priority_remote_identity(prepared) != expected_identity:
            if not _is_priority_unconfirmed_manual_job(job):
                rejection = rejected_invalid(
                    "exact durable priority evidence does not match the remote identity"
                )
                rejection["rejection_stage"] = "priority_evidence_gate"
                return rejection
            live_proof_classification = _probe_live_identity_classification(
                job,
                prepared=prepared,
                adapter=adapter,
                business_date=business_date,
                identity_parts=identity_parts,
                timeout_seconds=timeout_seconds,
                progress_lookup=progress_lookup,
            )
            bump_remote("probe")
            if live_proof_classification not in (
                RemoteExecutionState.ACTIVE,
                RemoteExecutionState.TERMINAL,
            ):
                rejection = rejected_invalid(
                    "live identity proof did not confirm the exact remote task "
                    f"({live_proof_classification.value}); the durable priority "
                    "evidence is unconfirmed and no rebuild may start"
                )
                rejection["rejection_stage"] = "live_proof"
                rejection["progress_classification"] = live_proof_classification.value
                return rejection
            # The exact task is proven live under the durable identity; keep
            # the proof classification on every later protocol answer.
            base["live_proof_classification"] = live_proof_classification.value

        # Durable write-ahead protocol: the newest action row for this old job
        # decides whether a crash left an unfinished protocol that this click
        # must resume (same action id, same persisted replacement task name).
        action = store.get_latest_rebuild_action_for_job(int(job["id"]))
        resume = False
        action_phase: str | None = None
        persisted_new_task_name = ""
        if action is not None:
            phase = str(action["phase"])
            if phase in REBUILD_ACTION_ACTIVE_PHASES:
                resume = True
                action_phase = phase
                action_id = str(action["action_id"])  # resume owns this protocol
                base["action_id"] = action_id
                persisted_new_task_name = str(action["replacement_task_name"])
                if str(action["remote_identity"]) != expected_identity:
                    return rejected_invalid(
                        "durable rebuild action identity no longer matches"
                    )
            elif phase == "FAILED":
                action = None  # a failed protocol never blocks a fresh one
            else:
                # SUPERSEDED/QUEUED/DISPATCHED/SUBMIT_UNKNOWN without a
                # replacement row is unreachable bookkeeping: fail closed.
                return {
                    **base,
                    "outcome": "REBUILD_REJECTED",
                    "error_code": "REBUILD_ACTION_STATE",
                    "reason": f"durable rebuild action phase {phase} without a replacement row",
                    "remote_calls": remote_calls,
                }

        crawls = store.crawl_work_for_job(int(job["id"]))
        eligible = [
            crawl
            for crawl in crawls
            if str(crawl["state"]) not in REBUILD_TERMINAL_CRAWL_STATES
        ]
        if not eligible:
            return {
                **base,
                "outcome": "REBUILD_NOT_ALLOWED",
                "error_code": "REBUILD_NOT_ALLOWED",
                "reason": "no eligible unfinished crawl work",
                "detail": "no eligible unfinished crawl work",
                "state": job_state,
                "post_attempted": post_attempted,
                "remote_calls": remote_calls,
            }
        skipped_terminal = len(crawls) - len(eligible)

        # Fail-closed coverage proof: every eligible crawl must map onto the
        # immutable payload; terminal crawls stay bound to the old job.
        request_keys = {
            (
                request.mall_key,
                request.sku_id,
                request.url.strip(),
                request.backend_version,
            )
            for request in prepared.requests
        }
        eligible_keys: set[tuple[str, str, str, str]] = set()
        for crawl in eligible:
            key = (
                str(crawl["mall_key"]),
                str(crawl["sku_id"]),
                str(crawl["exact_url"]).strip(),
                str(crawl["backend_version"]),
            )
            if key not in request_keys:
                return rejected_invalid(
                    f"eligible crawl work id {crawl['id']} is not covered by the immutable payload"
                )
            eligible_keys.add(key)
        replacement_requests = tuple(
            request
            for request in prepared.requests
            if (
                request.mall_key,
                request.sku_id,
                request.url.strip(),
                request.backend_version,
            )
            in eligible_keys
        )

        # Write-ahead INTENT: the protocol becomes durable before any remote
        # write, so a crash can never leave the old task stopped without
        # evidence.  On resume the persisted batch name is authoritative --
        # it is never re-derived (the action id owns the name).
        new_task_name = _replacement_task_name(
            str(job["task_name"]), action_id, prepared.mall_name
        )
        if resume:
            new_task_name = persisted_new_task_name
        else:
            try:
                store.create_rebuild_action(
                    token,
                    action_id=action_id,
                    old_remote_job_id=int(job["id"]),
                    remote_identity=expected_identity,
                    replacement_task_name=new_task_name,
                )
            except RemoteIntentConflict as exc:
                message = str(exc)
                if "active rebuild action" in message:
                    code = "REBUILD_ACTION_ACTIVE"
                elif "already has a replacement" in message:
                    code = "REBUILD_ALREADY_REPLACED"
                else:
                    code = "REBUILD_REJECTED"
                existing_action_id = None
                if code == "REBUILD_ACTION_ACTIVE":
                    latest = store.get_latest_rebuild_action_for_job(int(job["id"]))
                    existing_action_id = (
                        str(latest["action_id"]) if latest is not None else None
                    )
                return {
                    **base,
                    "outcome": "REBUILD_REJECTED",
                    "error_code": code,
                    "reason": message,
                    "existing_action_id": existing_action_id,
                    "remote_calls": remote_calls,
                }

        def persist_action_phase(
            phase: str,
            *,
            evidence: Mapping[str, Any] | None = None,
            error_text: str | None = None,
        ) -> None:
            store.update_rebuild_action_phase(
                token,
                action_id=action_id,
                phase=phase,
                stop_evidence=evidence,
                error_text=error_text,
            )

        def query_progress() -> Mapping[str, Any] | None:
            bump_remote("probe")
            if progress_lookup is not None:
                return progress_lookup(
                    prepared, task_id, task_number, min(30.0, float(timeout_seconds))
                )
            return adapter.query_task_progress_for_job(
                prepared, task_id, task_number, min(30.0, float(timeout_seconds))
            )

        progress_snapshot: dict[str, Any] | None
        classification: RemoteExecutionState
        stop_evidence: dict[str, Any]
        if resume and action_phase == "STOP_SENT":
            # Crash after the write-ahead STOP_SENT: the first disable may
            # already have left this process, so a fresh stop is forbidden.
            # Reconcile read-only and fail closed on any ambiguity.
            try:
                answer = query_progress()
                raw = answer.get("progress") if isinstance(answer, Mapping) else None
            except Exception:
                answer = None
                raw = None
            progress_snapshot = dict(raw) if isinstance(raw, Mapping) else None
            observed_enabled = (
                answer.get("enabled") if isinstance(answer, Mapping) else None
            )
            reclass = classify_remote_execution_state(
                raw if isinstance(raw, Mapping) else None,
                "",
            )
            if observed_enabled is False:
                stop_evidence = {
                    "phase": "ALREADY_DISABLED",
                    "reconciled": True,
                    "observed_enabled": False,
                    "progress": progress_snapshot,
                }
                persist_action_phase("STOP_CONFIRMED", evidence=stop_evidence)
                classification = RemoteExecutionState.ACTIVE
            elif reclass is RemoteExecutionState.TERMINAL:
                stop_evidence = {
                    "phase": "TERMINAL_BEFORE_REBUILD",
                    "reconciled": True,
                    "progress": progress_snapshot,
                }
                persist_action_phase("TERMINAL_PROOF", evidence=stop_evidence)
                classification = RemoteExecutionState.TERMINAL
            else:
                return {
                    **base,
                    "outcome": "REBUILD_REJECTED",
                    "error_code": "OLD_TASK_STOP_UNCONFIRMED",
                    "reason": (
                        "durable STOP_SENT with live task still enabled/active; "
                        "cannot prove the first disable did not land; refusing a second stop"
                    ),
                    "progress_classification": reclass.value,
                    "remote_calls": remote_calls,
                }
        elif resume and action_phase in ("STOP_CONFIRMED", "TERMINAL_PROOF"):
            # Crash after the stop was durably confirmed: no precheck, no
            # second stop -- resume straight from the persisted evidence.
            try:
                restored = json.loads(str(action.get("stop_evidence_json") or "{}"))
            except (TypeError, ValueError):
                restored = {}
            stop_evidence = dict(restored) if isinstance(restored, dict) else {}
            restored_progress = stop_evidence.get("progress")
            progress_snapshot = (
                dict(restored_progress)
                if isinstance(restored_progress, Mapping)
                else None
            )
            classification = (
                RemoteExecutionState.TERMINAL
                if action_phase == "TERMINAL_PROOF"
                or str(stop_evidence.get("classification") or "")
                == RemoteExecutionState.TERMINAL.value
                else RemoteExecutionState.ACTIVE
            )
        else:
            # Fresh protocol or resume-from-INTENT (crash before any remote
            # write): run the full read-only precheck then the stop protocol,
            # persisting every durable transition into the write-ahead row.
            try:
                progress_answer = query_progress()
                progress_raw = (
                    progress_answer.get("progress")
                    if isinstance(progress_answer, Mapping)
                    else None
                )
            except Exception:
                progress_raw = None
            progress_snapshot = dict(progress_raw) if isinstance(progress_raw, Mapping) else None
            classification = classify_remote_execution_state(
                progress_raw if isinstance(progress_raw, Mapping) else None,
                "",
            )
            if classification is RemoteExecutionState.UNKNOWN:
                # The protocol failed before any remote write: mark it FAILED
                # so a later click may open a fresh action.
                persist_action_phase("FAILED", error_text="REMOTE_PROGRESS_UNKNOWN")
                return {
                    **base,
                    "outcome": "REBUILD_REJECTED",
                    "error_code": "REMOTE_PROGRESS_UNKNOWN",
                    "reason": "remote execution state could not be proven; no rebuild was started",
                    "progress_classification": classification.value,
                    "remote_calls": remote_calls,
                }
            if classification is RemoteExecutionState.TERMINAL:
                stop_evidence = {
                    "phase": "TERMINAL_BEFORE_REBUILD",
                    "classification": RemoteExecutionState.TERMINAL.value,
                    "progress": progress_snapshot,
                }
                persist_action_phase("TERMINAL_PROOF", evidence=stop_evidence)
            else:
                # Write-ahead stop: the phase lands before the remote disable,
                # so a crash during the disable is always provable.
                persist_action_phase("STOP_SENT")
                bump_remote("stop")
                if stop_callable is not None:
                    stop = stop_callable(prepared, task_id, task_number, float(timeout_seconds))
                else:
                    stop = adapter.stop_running_task(
                        prepared, task_id, task_number, float(timeout_seconds)
                    )
                status = getattr(stop, "status", None)
                diagnostic = str(getattr(stop, "diagnostic", "") or "")

                def stop_unconfirmed(detail: str) -> dict[str, Any]:
                    # The phase intentionally stays STOP_SENT: the next run
                    # reconciles read-only instead of issuing a second stop.
                    return {
                        **base,
                        "outcome": "REBUILD_REJECTED",
                        "error_code": "OLD_TASK_STOP_UNCONFIRMED",
                        "reason": detail,
                        "progress_classification": classification.value,
                        "stop_status": getattr(status, "value", str(status)),
                        "remote_calls": remote_calls,
                    }

                if status is RestartStatus.STOPPED:
                    stop_evidence = {
                        "phase": "STOP_CONFIRMED",
                        "disable_confirmed": True,
                        "observed_enabled": False,
                        "diagnostic": diagnostic,
                    }
                    persist_action_phase("STOP_CONFIRMED", evidence=stop_evidence)
                elif status is RestartStatus.EXTERNALLY_DISABLED:
                    # Zero remote writes happened; the exact task is already
                    # disabled so it cannot double-run against the replacement.
                    stop_evidence = {
                        "phase": "ALREADY_DISABLED",
                        "observed_enabled": False,
                        "diagnostic": diagnostic,
                    }
                    persist_action_phase("STOP_CONFIRMED", evidence=stop_evidence)
                elif status is RestartStatus.NOT_RUNNING:
                    stop_evidence = {"phase": "TASK_NOT_FOUND", "diagnostic": diagnostic}
                    persist_action_phase("TERMINAL_PROOF", evidence=stop_evidence)
                elif status is RestartStatus.NO_LONGER_RUNNING:
                    # Not ACTIVE at write time: one fresh progress precheck must
                    # prove TERMINAL before any replacement may be created.
                    try:
                        recheck_answer = query_progress()
                        recheck_raw = (
                            recheck_answer.get("progress")
                            if isinstance(recheck_answer, Mapping)
                            else None
                        )
                    except Exception:
                        recheck_raw = None
                    recheck_snapshot = (
                        dict(recheck_raw) if isinstance(recheck_raw, Mapping) else None
                    )
                    recheck = classify_remote_execution_state(
                        recheck_raw if isinstance(recheck_raw, Mapping) else None,
                        "",
                    )
                    if recheck is RemoteExecutionState.TERMINAL:
                        stop_evidence = {
                            "phase": "NO_LONGER_RUNNING_TERMINAL",
                            "diagnostic": diagnostic,
                            "progress": recheck_snapshot,
                        }
                        persist_action_phase("TERMINAL_PROOF", evidence=stop_evidence)
                    else:
                        return stop_unconfirmed(
                            "write-time execution state "
                            f"{recheck.value} after NO_LONGER_RUNNING; old task stop is unconfirmed: "
                            f"{diagnostic}"
                        )
                elif status is RestartStatus.DISABLE_UNKNOWN:
                    observed = getattr(stop, "observed_enabled", None)
                    if observed is False:
                        stop_evidence = {
                            "phase": "STOP_CONFIRMED",
                            "disable_confirmed": False,
                            "reconciled": True,
                            "observed_enabled": False,
                            "diagnostic": diagnostic,
                        }
                        persist_action_phase("STOP_CONFIRMED", evidence=stop_evidence)
                    else:
                        return stop_unconfirmed(
                            "disable outcome unknown and the live task is not provably "
                            f"disabled (observed_enabled={observed!r}): {diagnostic}"
                        )
                elif status is RestartStatus.IDENTITY_CONFLICT:
                    persist_action_phase(
                        "FAILED", error_text=f"stop identity conflict: {diagnostic}"
                    )
                    return rejected_invalid(f"stop identity conflict: {diagnostic}")
                else:
                    return stop_unconfirmed(
                        f"stop protocol did not confirm the old task is stopped "
                        f"({getattr(status, 'value', str(status))}): {diagnostic}"
                    )

        new_suffix = new_task_name.rsplit("_", 1)[-1]
        new_job_dir = Path(str(job["input_path"])).parent.parent / new_suffix
        try:
            new_job_dir.mkdir(parents=True, exist_ok=True)
            new_prepared = adapter.prepare_job(
                business_date,
                replacement_requests,
                new_job_dir / "input.xlsx",
                new_job_dir / "result.xlsx",
                new_task_name,
                batch_state,
            )
        except Exception as exc:
            return rejected_invalid(f"replacement legacy job cannot be prepared: {exc}")
        new_payload = {
            "requests": [request.__dict__ for request in new_prepared.requests],
            "batch_state": str(new_prepared.batch_state_path),
        }
        replacement_logical_key = f"{new_prepared.logical_job_key}#rebuild#{action_id[:8]}"

        try:
            superseded = store.supersede_remote_job_to_replacement(
                token,
                old_job_id=int(job["id"]),
                replacement_logical_key=replacement_logical_key,
                replacement_task_name=new_task_name,
                replacement_payload_hash=new_prepared.payload_sha256,
                replacement_input_path=str(new_prepared.template_path),
                replacement_result_path=str(new_prepared.result_path),
                replacement_payload_json=new_payload,
                mall_key=prepared.mall_key,
                action_id=action_id,
                reason=reason,
                old_batch_number=str(job["task_name"]),
                new_batch_number=new_task_name,
                stop_evidence={
                    **stop_evidence,
                    "progress_observation": progress_snapshot,
                    "precheck_classification": classification.value,
                },
                eligible_crawl_ids=[int(crawl["id"]) for crawl in eligible],
                rebuild_action_id=action_id,
            )
        except RemoteIntentConflict as exc:
            message = str(exc)
            if "already has a replacement" in message:
                return {
                    **base,
                    "outcome": "REBUILD_ALREADY_REPLACED",
                    "reason": message,
                    "replacement": store.get_replacement_for_job(int(job["id"])),
                    "old_task_name": str(job["task_name"]),
                    "new_task_name": new_task_name,
                    "new_batch_number": new_task_name,
                    "stop_phase": stop_evidence.get("phase"),
                    "precheck_classification": classification.value,
                    "remote_calls": remote_calls,
                }
            if "task name is already reserved" in message or "immutable intent" in message:
                return {
                    **base,
                    "outcome": "REBUILD_REJECTED",
                    "error_code": "TASK_NAME_CONFLICT",
                    "reason": message,
                    "old_task_name": str(job["task_name"]),
                    "new_task_name": new_task_name,
                    "stop_phase": stop_evidence.get("phase"),
                    "precheck_classification": classification.value,
                    "remote_calls": remote_calls,
                }
            return {
                **base,
                "outcome": "REBUILD_REJECTED",
                "error_code": "REBUILD_REJECTED",
                "reason": message,
                "old_task_name": str(job["task_name"]),
                "new_task_name": new_task_name,
                "stop_phase": stop_evidence.get("phase"),
                "precheck_classification": classification.value,
                "remote_calls": remote_calls,
            }
        except ValueError as exc:
            return {
                **base,
                "outcome": "REBUILD_NOT_ALLOWED",
                "error_code": "REBUILD_NOT_ALLOWED",
                "reason": str(exc),
                "old_task_name": str(job["task_name"]),
                "new_task_name": new_task_name,
                "stop_phase": stop_evidence.get("phase"),
                "precheck_classification": classification.value,
                "remote_calls": remote_calls,
            }
        new_job_id = int(superseded["new_job"]["id"])
        result: dict[str, Any] = {
            **base,
            "old_task_name": str(job["task_name"]),
            "new_task_name": new_task_name,
            "new_batch_number": new_task_name,
            "old_remote_job_id": int(job["id"]),
            "new_remote_job_id": new_job_id,
            "replacement_id": int(superseded["replacement"]["id"]),
            "migrated_crawl_count": len(eligible),
            "skipped_terminal_crawl_count": skipped_terminal,
            "stop_phase": stop_evidence.get("phase"),
            "precheck_classification": classification.value,
            "rebuild_action_id": action_id,
            "remote_calls": remote_calls,
        }

        # Immediate dispatch attempt (burn-before-send): mirror the tick
        # engine's SUBMIT_INTENT semantics; every miss stays durable for the
        # scheduler to resume.  CREATE_SENT lands before the create POST, so a
        # crash can always prove the dispatch may already have left.
        claim = store.claim_remote_submission(token, new_job_id)
        if claim is None:
            persist_action_phase("QUEUED")
            return {
                **result,
                "outcome": "REBUILD_QUEUED",
                "reason": "replacement submission claim unavailable; scheduler will dispatch",
                "remote_calls": remote_calls,
            }
        persist_action_phase("CREATE_SENT")
        bump_remote("create")
        try:
            submit_outcome = adapter.submit(
                new_prepared, timeout_seconds=max(0.001, float(timeout_seconds))
            )
        except Exception as exc:
            store.mark_submit_unknown(
                token, new_job_id, f"dashboard rebuild: {type(exc).__name__}: {exc}"
            )
            persist_action_phase("SUBMIT_UNKNOWN")
            return {
                **result,
                "outcome": "REBUILD_SUBMIT_UNKNOWN",
                "reason": "legacy create-only dispatch raised; tick reconcile will confirm",
                "remote_calls": remote_calls,
            }
        if submit_outcome.status is SubmitStatus.NOT_ATTEMPTED:
            store.release_remote_submission_claim(
                token, new_job_id, f"dashboard rebuild: {submit_outcome.diagnostic}"
            )
            persist_action_phase("QUEUED")
            return {
                **result,
                "outcome": "REBUILD_QUEUED",
                "reason": (
                    f"submit not attempted: {submit_outcome.diagnostic}; "
                    "scheduler will dispatch"
                ),
                "remote_calls": remote_calls,
            }
        if submit_outcome.status is SubmitStatus.SUBMIT_UNKNOWN:
            store.mark_submit_unknown(
                token, new_job_id, "legacy create-only outcome unknown"
            )
            persist_action_phase("SUBMIT_UNKNOWN")
            return {
                **result,
                "outcome": "REBUILD_SUBMIT_UNKNOWN",
                "reason": "legacy create-only outcome unknown; tick reconcile will confirm",
                "remote_calls": remote_calls,
            }
        if not submit_outcome.task_id or not submit_outcome.task_number:
            store.mark_submit_unknown(
                token, new_job_id, "dashboard rebuild: submit response had no remote identity"
            )
            persist_action_phase("SUBMIT_UNKNOWN")
            return {
                **result,
                "outcome": "REBUILD_SUBMIT_UNKNOWN",
                "reason": "submit response had no remote identity; tick reconcile will confirm",
                "remote_calls": remote_calls,
            }
        new_state = (
            "SUBMITTED"
            if submit_outcome.priority_evidence in {"updated", "already_highest_priority"}
            else "PRIORITY_PENDING"
        )
        new_identity = (
            f"{new_prepared.mall_key}|{submit_outcome.task_id}|{submit_outcome.task_number}"
        )
        store.reconcile_remote_job(
            token,
            new_job_id,
            remote_identity=new_identity,
            state=new_state,
            result={"priority_evidence": submit_outcome.priority_evidence},
        )
        if new_state != "PRIORITY_PENDING":
            store.advance_crawl_work_for_job(token, new_job_id, new_state)
        persist_action_phase("DISPATCHED")
        return {
            **result,
            "outcome": "REBUILD_DISPATCHED",
            "state": new_state,
            "new_remote_identity": new_identity,
            "priority_evidence": submit_outcome.priority_evidence,
            "remote_calls": remote_calls,
        }
    finally:
        store.release_lease(token)
        process_lock.release()


def run_manual_rebuild(
    *,
    state_path: Path | str,
    lock_path: Path | str,
    business_date: str,
    remote_job_id: int,
    remote_identity: str,
    adapter: CnncLegacyZhaoshangAdapter,
    action_id: str | None = None,
    timeout_seconds: float = 120.0,
    reason: str = "dashboard manual rebuild",
    progress_lookup: Callable[[PreparedLegacyJob, str, str, float], Mapping[str, Any]]
    | None = None,
    stop_callable: Callable[[PreparedLegacyJob, str, str, float], LegacyRestartOutcome]
    | None = None,
) -> dict[str, Any]:
    """Manually rebuild one exact existing Zhaoshang job as a fresh task.

    Protocol (strict order): durable identity + idempotency + source-state
    gate, immutable payload reconstruction, fail-closed eligible-crawl
    coverage, then a durable write-ahead ``remote_job_rebuild_actions`` INTENT
    row that names the replacement task before anything touches the remote.
    The live progress precheck follows (UNKNOWN fails the action with zero
    remote writes), then a safe stop of the old task whenever it is still
    ACTIVE -- each stop phase (STOP_SENT/STOP_CONFIRMED/TERMINAL_PROOF) is
    persisted before and after the remote disable, so a crash can always
    prove whether the disable happened and a resumed click reconciles
    read-only instead of issuing a second stop.  One atomic supersede
    transaction migrates the eligible crawls to a fresh SUBMIT_INTENT job,
    retires the old job as SUPERSEDED and advances the action row in the same
    commit; the immediate create-only dispatch attempt mirrors the tick
    engine with a write-ahead CREATE_SENT phase (every dispatch miss stays
    durable for the scheduler, an interrupted dispatch resumes or fails safe
    onto the tick reconcile -- never a second create).  The old job's
    identity is never updated; one rebuild action lands as one
    ``dashboard_actions.jsonl`` record.
    """
    path = Path(state_path)
    if not path.is_file():
        raise RecoveryRejected(f"state database does not exist: {path}")
    resolved_action_id = str(action_id) if action_id else uuid4().hex
    remote_call_breakdown: dict[str, int] = {}
    result = _run_manual_rebuild_once(
        state_path=path,
        lock_path=Path(lock_path),
        business_date=business_date,
        remote_job_id=int(remote_job_id),
        remote_identity=remote_identity,
        adapter=adapter,
        action_id=resolved_action_id,
        timeout_seconds=float(timeout_seconds),
        reason=reason,
        progress_lookup=progress_lookup,
        stop_callable=stop_callable,
        counters=remote_call_breakdown,
    )
    record_operator_action(
        path.parent,
        action_type="rebuild",
        action_id=resolved_action_id,
        payload={
            "outcome": result.get("outcome"),
            "error_code": result.get("error_code"),
            "reason": result.get("reason"),
            "old_remote_job_id": result.get("old_remote_job_id"),
            "new_remote_job_id": result.get("new_remote_job_id"),
            "old_task_name": result.get("old_task_name"),
            "new_task_name": result.get("new_task_name"),
            "rebuild_action_id": result.get("rebuild_action_id"),
            "rebuild_resumed": result.get("rebuild_resumed"),
            "stop_phase": result.get("stop_phase"),
            "migrated_crawl_count": result.get("migrated_crawl_count"),
            "skipped_terminal_crawl_count": result.get("skipped_terminal_crawl_count"),
            "remote_calls": result.get("remote_calls"),
            "rejection_stage": result.get("rejection_stage") or result.get("error_code"),
            "progress_classification": (
                result.get("progress_classification")
                or result.get("precheck_classification")
            ),
            "live_proof_classification": result.get("live_proof_classification"),
            # Adapter operation counters (review round 2 renamed from
            # *_remote_calls: these count legacy-adapter operations -- one
            # probe/stop/create dispatch each -- not raw HTTP remote calls;
            # the manual_reexport record's preflight_remote_calls /
            # export_remote_calls / remote_calls_total are the true HTTP
            # remote-call counters and keep their names).
            "probe_operations": int(remote_call_breakdown.get("probe", 0)),
            "stop_operations": int(remote_call_breakdown.get("stop", 0)),
            "create_operations": int(remote_call_breakdown.get("create", 0)),
        },
    )
    return result


def run_manual_rebuild_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    remote_job_id: int,
    remote_identity: str,
    timeout_seconds: float = 120.0,
    adapter: CnncLegacyZhaoshangAdapter | None = None,
    action_id: str | None = None,
) -> dict[str, Any]:
    """Runtime-root wrapper for one dashboard manual rebuild click."""
    from app.services.incremental_audit_runtime import runtime_paths

    paths = runtime_paths(business_date, runtime_root)
    return run_manual_rebuild(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        remote_job_id=remote_job_id,
        remote_identity=remote_identity,
        adapter=adapter if adapter is not None else CnncLegacyZhaoshangAdapter(),
        timeout_seconds=timeout_seconds,
        action_id=action_id,
    )
