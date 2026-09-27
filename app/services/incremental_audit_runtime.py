"""Runtime composition for finite incremental price-audit commands.

Construction is intentionally side-effect free. Network discovery happens only
inside an explicit tick/finalize command; Zhaoshang create is additionally
gated by ``enable_zhaoshang`` and is not enabled by the CLI by default.
"""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import sys
import time as monotonic_time
import time as time_module
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from app.services.cnnc_saas_snapshot_adapter import CnncSaasSnapshotAdapter
from app.services.customer_price_semantics import (
    CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES,
    DetailBusinessRetryRequired,
    DetailRequestError,
    is_retryable_detail_http_status,
)
from app.services.customer_prewarm_discovery import intake_documents
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.batch_zhaoshang_adapter import (
    BatchProgressStatus,
    BatchReconcileStatus,
    BatchSubmitStatus,
    CnncBatchZhaoshangAdapter,
    CONTRACT_VERSION as BATCH_CONTRACT_VERSION,
)
from app.services.batch_zhaoshang_priority import (
    BatchMaterializationProbe,
    BatchMaterializationStatus,
    BatchMaterializationTarget,
    BatchPriorityAttempt,
    BatchPriorityAttemptStatus,
    connect_batch_priority_from_env_file,
    ensure_batch_priority,
    probe_batch_materialization,
)
from app.services.batch_sql_result_adapter import (
    ALLOWED_MALLS,
    BatchResultEvidence,
    BatchResultStatus,
    BatchResultTarget,
    BatchSqlResultAdapter,
    batch_sql_result_to_crawl_result,
)
from app.services.zhaoshang_url_identity import is_batch_submitted_url_supported
from app.services.daily_audit_store import (
    DailyAuditStore,
    FrozenSnapshotConflict,
    LeaseUnavailable,
    ProcessLock,
)
from app.services.incremental_audit_closeout import (
    CloseoutConfig,
    run_closeout,
)
from app.services.incremental_audit_materializer import (
    ArtifactConflictError,
    MaterializationValidationError,
)
from app.services.incremental_local_fallback import execute_pinned_crawler
from app.services.incremental_audit_recovery import (
    RecoveryRejected,
    requeue_model_transient_failures,
    release_model_recovery,
    resolve_exported_no_result,
    run_existing_remote_harvest,
    run_existing_remote_priority_recovery,
    run_existing_remote_priority_zero_rows_recovery,
    run_local_remote_harvest_recovery,
    run_model_recovery,
    run_partial_exact_harvest_recovery,
    run_remote_export_no_price_resolution,
    run_remote_wait_export_recovery,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    HarvestStatus,
    LegacyProbeEvidenceError,
    PriorityStatus,
    ReconcileStatus,
    RemoteTaskIdentityConflictError,
    RestartStatus,
    SubmitStatus,
)
from app.services.remote_stall_breaker import remote_submit_pause_reason
from app.services.saas_audit_core import (
    SAAS_PENDING_HEADERS,
    customer_fingerprint,
    customer_row,
    join_saas_with_customer,
    is_valid_reference_price,
    normalize_cell_str,
    normalize_saas_pending_row,
    parse_decimal,
    source_fingerprint,
)
from app.workflows.incremental_price_audit import (
    CrawlMember,
    CrawlPlan,
    DiscoveredState,
    SubmitNotAttemptedError,
    TickConfig,
    TickDependencies,
    TickSummary,
    run_existing_remote_drain,
    run_harvest_only,
    run_incremental_tick,
)
from app.workflows.incremental_price_audit_finalize import (
    NON_TOLERABLE_BLOCKERS,
    FinalizeAlreadyRunning,
    FinalizeBlocked,
    FinalizePublication,
    FinalizeReadiness,
    ForceFinalizeRejected,
    ReviewPublication,
    is_recoverable_finalize_wait,
    materialize_frozen_daily_run,
    materialize_frozen_review,
    materialize_forced_frozen_daily_run,
)
from scripts import cnnc_auth_refresh
from scripts.audit_saas_excel_file import call_detail_api
from scripts.auth_manager import AuthManager, _payload_is_auth_failure
from scripts.cnnc_auth_core import AT_COOKIE_RE, CnncAuthProvider
from scripts.curl_utils import load_curl_command
from app.services.title_match_model_adapter import (
    TITLE_MATCH_PROMPT_VERSION,
    TITLE_MATCH_SCHEMA_VERSION,
    TitleMatchModelRequest,
    call_title_match_model,
    call_title_match_model_batch,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_DATA_ROOT = PROJECT_ROOT / "data"
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "runtime" / "incremental_price_audit"
_MODEL_CONFIGURATION_KEYS = (
    "TITLE_MATCH_MODEL_API_KEY",
    "TITLE_MATCH_MODEL_BASE_URL",
    "TITLE_MATCH_MODEL_NAME",
)
# Optional incremental-runtime tuning knobs.  Deliberately NOT part of
# _MODEL_CONFIGURATION_KEYS: the required gate must stay "all three secrets
# present"; these tune the bounded parallel model dispatcher and are read
# env-first, then data/model.env, then defaults.  The incremental batch size
# uses its own TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE name because the
# legacy work_01 path already owns TITLE_MATCH_MODEL_BATCH_SIZE in the
# process environment (default 30) — reusing that name would let a
# historical data/model.env entry silently re-tune STEP_1 on upgrade, so the
# incremental runtime never reads the work_01 key.  (work_01 reads
# TITLE_MATCH_MODEL_CONCURRENCY / TITLE_MATCH_MODEL_BATCH_CONCURRENCY from
# the process environment only and never sees data/model.env.)
_MODEL_RUNTIME_TUNING_KEYS = (
    "TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE",
    "TITLE_MATCH_MODEL_MAX_CONCURRENCY",
)
_BATCH_STATE_START = "<!-- incremental-runtime-state:start -->"
_BATCH_STATE_END = "<!-- incremental-runtime-state:end -->"
_BATCH_STATE_SNAPSHOT_DIR = "batch_state_snapshots"
_BATCH_STATE_SNAPSHOT_GLOB = "BATCH_STATE_*.md"
_BATCH_STATE_ALIAS_RETRY_DELAYS = (0.0, 0.05, 0.15)
FINALIZE_MATERIALIZE_RESERVE_SECONDS = 60.0

# CNNC 认证 provider 进程级单例（懒加载）。生产安静运行：logger=None。
_cnnc_provider_singleton: CnncAuthProvider | None = None


def shared_cnnc_provider() -> CnncAuthProvider:
    global _cnnc_provider_singleton
    if _cnnc_provider_singleton is None:
        _cnnc_provider_singleton = CnncAuthProvider(logger=None)
    return _cnnc_provider_singleton


def _apply_cnnc_at(config: dict[str, Any], at: str) -> None:
    """把当前 AT 注入 detail 请求配置：headers 的 access-token 项 + cookie 的 token=AT-...。

    config 是模板的浅拷贝；headers 必须重建列表，避免污染共享模板。
    """
    headers = config.get("headers")
    if isinstance(headers, list):
        config["headers"] = [
            (name, at) if str(name).lower() == "access-token" else (name, value)
            for name, value in headers
        ]
    cookie = str(config.get("cookie") or "")
    if cookie:
        config["cookie"] = AT_COOKIE_RE.sub(f"token={at}", cookie)


class RuntimeConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimePaths:
    business_date: str
    run_dir: Path
    state_path: Path
    lock_path: Path
    detail_lock_path: Path
    final_output_path: Path


@dataclass(frozen=True)
class RuntimeResult:
    tick: TickSummary
    publication: FinalizePublication | None = None
    publication_blocked: str | None = None
    publication_error_type: str | None = None
    lock_conflict: Mapping[str, Any] | None = None


def business_date_in_shanghai(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("runtime clock must return an explicit-timezone datetime")
    return current.astimezone(SHANGHAI).date().isoformat()


def cutoff_for_business_date(business_date: str) -> datetime:
    day = datetime.fromisoformat(business_date).date()
    return datetime.combine(day, time(18, 0), tzinfo=SHANGHAI).astimezone(UTC)


def runtime_paths(business_date: str, root: Path | str = DEFAULT_RUNTIME_ROOT) -> RuntimePaths:
    # Preserve an explicitly supplied worktree/Junction alias. Path.resolve()
    # follows Windows Junctions and can silently move runtime I/O into another
    # worktree even though the scheduler passed a different production root.
    root_path = Path(root).absolute()
    run_dir = root_path / business_date
    return RuntimePaths(
        business_date=business_date,
        run_dir=run_dir,
        state_path=run_dir / "state.sqlite3",
        lock_path=run_dir / "incremental_price_audit.lock",
        # Detail is globally serialized across business dates.  A previous-day
        # recovery and today's tick otherwise own different per-day locks.
        detail_lock_path=root_path / "detail.lock",
        final_output_path=run_dir / f"saas_audit_final_{business_date.replace('-', '')}.xlsx",
    )


def _record_recovery_event(paths: RuntimePaths, result: Mapping[str, Any]) -> None:
    path = paths.run_dir / "BATCH_STATE.md"
    if not path.is_file():
        return
    safe_event = {
        "command": str(result["outcome"]),
        "business_date": paths.business_date,
    }
    for key in (
        "frozen_snapshot_id",
        "stage_work_ids",
        "remote_job_id",
        "remote_identity",
        "crawl_work_id",
        "crawl_completed",
        "crawl_success",
        "crawl_no_result",
        "crawl_manual",
        "written_success",
        "written_no_result",
        "written_manual",
        "skipped_terminal",
        "manual_crawl_work_ids",
        "remote_state",
        "expected",
        "matched",
        "weight_type_minus_one",
        "downloaded",
        "validated",
        "positive_price",
        "real_zero",
        "price_invalid",
        "missing_crawl_work_ids",
        "row_count",
        "manual_count",
        "output_path",
        "sha256",
        "expected_count",
        "expected_attempts",
        "resolved",
        "resolved_crawl_work_ids",
        "resolved_source_ids",
        "no_result_before",
        "no_result_after",
        "no_result_limit",
        "task_status_rsp_desc",
        "export_rsp_desc",
        "export_count",
        "error_type",
        "last_cause",
        "recovered",
        "skipped",
        "failed",
    ):
        if key in result:
            safe_event[key] = result[key]
    event_line = f"- recovery: {json.dumps(safe_event, ensure_ascii=True, sort_keys=True)}\n"
    with _batch_state_update_lock(path):
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(event_line)
            stream.flush()
            os.fsync(stream.fileno())


def _opportunistic_recovery_closeout(
    paths: RuntimePaths,
    recovery: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Materialize the final workbook when a recovery just cleared the last blocker.

    Returns ``None`` (and touches nothing) when the day has no frozen snapshot
    yet or is already finalized.  A readiness block is reported as
    ``NOT_READY``; the recovery result itself must never fail because the
    opportunistic closeout could not run.
    """
    business_date = str(recovery.get("business_date") or "")
    if not business_date or not paths.state_path.is_file():
        return None
    store = DailyAuditStore(paths.state_path)
    try:
        run = store.get_run(business_date)
    except Exception:
        return None
    if run is None or run.get("frozen_snapshot_id") is None or run["state"] == "FINALIZED":
        return None
    try:
        publication = materialize_frozen_daily_run(
            store,
            ProcessLock(paths.lock_path),
            business_date,
            paths.final_output_path,
        )
    except Exception as exc:
        return {
            "finalized": False,
            "reason": "NOT_READY",
            "error_type": type(exc).__name__,
        }
    materialization = getattr(publication, "materialization", None)
    return {
        "finalized": True,
        "output_path": str(getattr(materialization, "output_path", paths.final_output_path)),
        "finalized_rows": int(getattr(materialization, "row_count", 0) or 0),
    }


def _attach_recovery_closeout(paths: RuntimePaths, result: dict[str, Any]) -> dict[str, Any]:
    closeout = _opportunistic_recovery_closeout(paths, result)
    if closeout is None:
        return result
    result["closeout"] = closeout
    _record_recovery_event(
        paths,
        {
            "outcome": "RECOVERY_CLOSEOUT_FINALIZED" if closeout["finalized"] else "RECOVERY_CLOSEOUT_NOT_READY",
            "business_date": paths.business_date,
            "row_count": closeout.get("finalized_rows", 0),
            "output_path": closeout.get("output_path"),
        },
    )
    return result


def run_model_recovery_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    stage_work_ids: list[int],
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = run_model_recovery(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        stage_work_ids=stage_work_ids,
        model_batch=_model_batch_worker,
        model_name=_configured_model_name(),
        model_prompt_version=TITLE_MATCH_PROMPT_VERSION,
        model_schema_version=TITLE_MATCH_SCHEMA_VERSION,
        timeout_seconds=timeout_seconds,
    )
    _record_recovery_event(paths, result)
    return _attach_recovery_closeout(paths, result)


def run_requeue_model_transient_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    expected_count: int,
    expected_attempts: int,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = requeue_model_transient_failures(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        expected_count=expected_count,
        expected_attempts=expected_attempts,
    )
    _record_recovery_event(paths, result)
    return _attach_recovery_closeout(paths, result)


def run_release_model_recovery_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    stage_work_ids: list[int],
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = release_model_recovery(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        stage_work_ids=stage_work_ids,
    )
    _record_recovery_event(paths, result)
    return _attach_recovery_closeout(paths, result)


def run_remote_harvest_recovery_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    remote_job_id: int,
    remote_identity: str,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = run_local_remote_harvest_recovery(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        remote_job_id=remote_job_id,
        remote_identity=remote_identity,
        adapter=CnncLegacyZhaoshangAdapter(),
    )
    _record_recovery_event(paths, result)
    return _attach_recovery_closeout(paths, result)


def run_partial_exact_harvest_recovery_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    remote_job_id: int,
    remote_identity: str,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = run_partial_exact_harvest_recovery(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        remote_job_id=remote_job_id,
        remote_identity=remote_identity,
        adapter=CnncLegacyZhaoshangAdapter(),
    )
    _record_recovery_event(paths, result)
    return _attach_recovery_closeout(paths, result)


def run_no_result_recovery_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    crawl_work_id: int,
    remote_job_id: int,
    remote_identity: str,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = resolve_exported_no_result(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        crawl_work_id=crawl_work_id,
        remote_job_id=remote_job_id,
        remote_identity=remote_identity,
        adapter=CnncLegacyZhaoshangAdapter(),
    )
    _record_recovery_event(paths, result)
    return _attach_recovery_closeout(paths, result)


def run_remote_priority_recovery_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    remote_job_id: int,
    remote_identity: str,
    expected_current_state: str = "PRIORITY_PENDING",
    expected_result_state: str | None = None,
    expected_error_text: str | None = None,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = run_existing_remote_priority_recovery(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        remote_job_id=remote_job_id,
        remote_identity=remote_identity,
        expected_current_state=expected_current_state,
        expected_result_state=expected_result_state,
        expected_error_text=expected_error_text,
        adapter=CnncLegacyZhaoshangAdapter(),
        timeout_seconds=timeout_seconds,
    )
    _record_recovery_event(paths, result)
    if result.get("outcome") == "REMOTE_PRIORITY_RECOVERY_COMPLETED":
        # One bounded targeted wait/export of exactly the recovered job; the
        # other remote jobs keep their own sweep pacing.
        try:
            result["targeted_harvest"] = run_existing_remote_harvest(
                state_path=paths.state_path,
                lock_path=paths.lock_path,
                business_date=business_date,
                remote_job_id=remote_job_id,
                remote_identity=remote_identity,
                adapter=CnncLegacyZhaoshangAdapter(),
                timeout_seconds=timeout_seconds,
            )
        except RecoveryRejected as exc:
            result["targeted_harvest"] = {
                "outcome": "REMOTE_PRIORITY_TARGETED_HARVEST_SKIPPED",
                "reason": str(exc),
            }
        if isinstance(result["targeted_harvest"], dict):
            _record_recovery_event(paths, result["targeted_harvest"])
    return _attach_recovery_closeout(paths, result)


def run_remote_priority_zero_rows_recovery_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    remote_job_id: int,
    remote_identity: str,
    expected_error_text: str,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = run_existing_remote_priority_zero_rows_recovery(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        remote_job_id=remote_job_id,
        remote_identity=remote_identity,
        expected_error_text=expected_error_text,
        adapter=CnncLegacyZhaoshangAdapter(),
        timeout_seconds=timeout_seconds,
    )
    _record_recovery_event(paths, result)
    if result.get("outcome") == "REMOTE_PRIORITY_ZERO_ROWS_RECOVERY_COMPLETED":
        # One bounded targeted wait/export of exactly the recovered job; the
        # other remote jobs keep their own sweep pacing.
        try:
            result["targeted_harvest"] = run_existing_remote_harvest(
                state_path=paths.state_path,
                lock_path=paths.lock_path,
                business_date=business_date,
                remote_job_id=remote_job_id,
                remote_identity=remote_identity,
                adapter=CnncLegacyZhaoshangAdapter(),
                timeout_seconds=timeout_seconds,
            )
        except RecoveryRejected as exc:
            result["targeted_harvest"] = {
                "outcome": "REMOTE_PRIORITY_ZERO_ROWS_TARGETED_HARVEST_SKIPPED",
                "reason": str(exc),
            }
        if isinstance(result["targeted_harvest"], dict):
            _record_recovery_event(paths, result["targeted_harvest"])
    return _attach_recovery_closeout(paths, result)


def run_existing_remote_harvest_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    remote_job_id: int,
    remote_identity: str,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = run_existing_remote_harvest(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        remote_job_id=remote_job_id,
        remote_identity=remote_identity,
        adapter=CnncLegacyZhaoshangAdapter(),
        timeout_seconds=timeout_seconds,
    )
    _record_recovery_event(paths, result)
    return _attach_recovery_closeout(paths, result)


def run_remote_wait_export_recovery_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    remote_job_id: int,
    remote_identity: str,
    expected_error_text: str,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = run_remote_wait_export_recovery(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        remote_job_id=remote_job_id,
        remote_identity=remote_identity,
        expected_error_text=expected_error_text,
        adapter=CnncLegacyZhaoshangAdapter(),
        timeout_seconds=timeout_seconds,
    )
    _record_recovery_event(paths, result)
    return _attach_recovery_closeout(paths, result)


def run_remote_export_no_price_resolution_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    remote_job_id: int,
    remote_identity: str,
    expected_error_text: str,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    result = run_remote_export_no_price_resolution(
        state_path=paths.state_path,
        lock_path=paths.lock_path,
        business_date=business_date,
        remote_job_id=remote_job_id,
        remote_identity=remote_identity,
        expected_error_text=expected_error_text,
        adapter=CnncLegacyZhaoshangAdapter(),
        timeout_seconds=timeout_seconds,
    )
    _record_recovery_event(paths, result)
    return _attach_recovery_closeout(paths, result)


def run_review_export_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    paths = runtime_paths(business_date, runtime_root)
    resolved_output = (
        Path(output_path)
        if output_path is not None
        else paths.run_dir / f"saas_audit_review_{business_date.replace('-', '')}.xlsx"
    )
    publication: ReviewPublication = materialize_frozen_review(
        DailyAuditStore(paths.state_path),
        ProcessLock(paths.lock_path),
        business_date,
        resolved_output,
    )
    result = {
        "outcome": "REVIEW_EXPORTED",
        "business_date": business_date,
        "output_path": str(publication.output_path),
        "row_count": publication.row_count,
        "manual_count": publication.manual_count,
        "sha256": publication.sha256,
        "remote_calls": 0,
        "add_calls": 0,
    }
    _record_recovery_event(paths, result)
    return result


def run_force_finalize_command(
    *,
    business_date: str,
    runtime_root: Path | str,
    max_incomplete: int = 10,
    output_path: Path | str | None = None,
    accept_unresolved_crawl: bool = False,
    expected_missing_page_price_rows: int | None = None,
) -> dict[str, Any]:
    """Guarded force-finalize: publish the formal final despite remote long tail.

    The command never creates or cancels remote tasks and makes no network
    calls; it only reads durable state, reuses the formal final writer, and
    records the force audit metadata on the daily run.
    """
    paths = runtime_paths(business_date, runtime_root)
    resolved_output = (
        Path(output_path) if output_path is not None else paths.final_output_path
    )
    if not paths.state_path.is_file():
        return {
            "outcome": "FORCE_FINALIZE_NOT_FROZEN",
            "error_type": "FORCE_FINALIZE_NOT_FROZEN",
            "business_date": business_date,
            "output_path": str(resolved_output.resolve()),
            "finalized": False,
        }
    store = DailyAuditStore(paths.state_path)
    try:
        publication = materialize_forced_frozen_daily_run(
            store,
            ProcessLock(paths.lock_path),
            business_date,
            resolved_output,
            max_incomplete=max_incomplete,
            accept_unresolved_crawl=accept_unresolved_crawl,
            expected_missing_page_price_rows=expected_missing_page_price_rows,
        )
    except ForceFinalizeRejected as exc:
        blockers: dict[str, int] = {}
        if exc.readiness is not None:
            for name in (*NON_TOLERABLE_BLOCKERS, "crawl_awaiting_remote", "remote_awaiting_remote"):
                value = int(getattr(exc.readiness, name))
                if value:
                    blockers[name] = value
        return {
            "outcome": exc.error_type,
            "error_type": exc.error_type,
            "business_date": business_date,
            "output_path": str(resolved_output.resolve()),
            "max_incomplete": max_incomplete,
            "accept_unresolved_crawl": accept_unresolved_crawl,
            "blockers": blockers,
            "finalized": False,
        }
    if publication.already_finalized:
        return {
            "outcome": "ALREADY_FINALIZED",
            "business_date": business_date,
            "frozen_snapshot_id": publication.frozen_snapshot_id,
            "forced_incomplete_count": publication.forced_incomplete_count,
            "remote_incomplete_job_count": publication.remote_incomplete_job_count,
            "blocker_count": publication.blocker_count,
            "max_incomplete": max_incomplete,
            "missing_page_price_rows": publication.readiness.missing_page_price_rows,
            "output_path": str(publication.output_path) if publication.output_path is not None else str(resolved_output.resolve()),
            "forced": publication.forced,
            "manifest_recorded": True,
            "finalized": True,
        }
    materialization = publication.materialization
    assert materialization is not None
    result = {
        "outcome": "FORCE_FINALIZED",
        "business_date": business_date,
        "frozen_snapshot_id": publication.frozen_snapshot_id,
        "frozen_row_count": publication.readiness.frozen_rows,
        "forced_incomplete_count": publication.forced_incomplete_count,
        "remote_incomplete_job_count": publication.remote_incomplete_job_count,
        "blocker_count": publication.blocker_count,
        "max_incomplete": max_incomplete,
        "missing_page_price_rows": publication.readiness.missing_page_price_rows,
        "output_path": str(materialization.output_path),
        "manifest_recorded": True,
        "finalized": True,
        "remote_calls": 0,
        "add_calls": 0,
    }
    _record_recovery_event(paths, result)
    return result


def _batch_state_block(
    paths: RuntimePaths,
    *,
    status: str,
    enable_zhaoshang: bool,
    zhaoshang_backend: str = "legacy",
    output_path: Path,
    blocker: str,
    completed_actions: str,
    next_action: str,
) -> str:
    return "\n".join((
        _BATCH_STATE_START,
        f"status: {status}",
        "step: STEP_1_PRICE_AUDIT",
        f"business_date: {paths.business_date}",
        "scope: tenant_id=188605, commodity_pool_id=222",
        f"authorization: zhaoshang_create={'enabled' if enable_zhaoshang else 'disabled'}; backend={zhaoshang_backend}",
        f"completed_nonrepeatable_actions: {completed_actions}",
        f"output: {output_path.resolve()}",
        f"blocker: {blocker}",
        f"next_action: {next_action}",
        "background_process: none",
        "context_reminder: none",
        _BATCH_STATE_END,
    ))


def _initial_batch_state(paths: RuntimePaths, *, enable_zhaoshang: bool, zhaoshang_backend: str = "legacy") -> str:
    return "\n".join((
        "# Incremental price audit batch",
        _batch_state_block(
            paths,
            status="ACTIVE",
            enable_zhaoshang=enable_zhaoshang,
            zhaoshang_backend=zhaoshang_backend,
            output_path=paths.final_output_path,
            blocker="none",
            completed_actions="none",
            next_action="run finite tick or finalize after cutoff",
        ),
        "",
        "## Runtime events",
        "",
    ))


def _ensure_batch_state(paths: RuntimePaths, *, enable_zhaoshang: bool, zhaoshang_backend: str = "legacy") -> Path:
    """Create the human handoff marker exclusively; SQLite remains authoritative."""
    path = paths.run_dir / "BATCH_STATE.md"
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_initial_batch_state(paths, enable_zhaoshang=enable_zhaoshang, zhaoshang_backend=zhaoshang_backend))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        pass
    return path


@contextmanager
def _batch_state_update_lock(path: Path, *, timeout_seconds: float = 30.0):
    lock_path = Path(f"{path}.lock")
    deadline = time_module.monotonic() + timeout_seconds
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                stale = time_module.time() - lock_path.stat().st_mtime > 300
            except FileNotFoundError:
                continue
            if stale:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time_module.monotonic() >= deadline:
                raise TimeoutError("timed out updating BATCH_STATE.md")
            time_module.sleep(0.05)
        else:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.close(descriptor)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _replace_batch_state_block(existing: str, managed_block: str) -> str:
    start = existing.find(_BATCH_STATE_START)
    end = existing.find(_BATCH_STATE_END, start + len(_BATCH_STATE_START)) if start >= 0 else -1
    if start >= 0 and end >= 0:
        end += len(_BATCH_STATE_END)
        return f"{existing[:start]}{managed_block}{existing[end:]}"

    # Migrate an early incremental state document without deleting priority
    # evidence appended by the legacy tool. Unknown documents are preserved in
    # full below the managed metadata rather than rewritten heuristically.
    preserved = existing.rstrip("\n")
    return "\n".join((
        "# Incremental price audit batch",
        managed_block,
        "",
        "## Preserved pre-managed batch state",
        preserved,
        "",
    ))


def _batch_result_metadata(
    result: RuntimeResult,
    *,
    command: str,
) -> tuple[str, str, str, str]:
    outcome = result.tick.outcome
    if result.publication is not None:
        return "COMPLETE", "none", "formal_excel_materialized", "none; formal Excel is materialized"
    if outcome == "ALREADY_FINALIZED":
        return "COMPLETE", "none", "formal_excel_already_materialized", "none; daily run is already finalized"
    if result.publication_blocked:
        return "BLOCKED", result.publication_blocked, "none", "resolve blocker and rerun finalize"
    if outcome == "MANUAL_REVIEW_REQUIRED":
        return "WAITING_USER", result.tick.error or outcome, "none", "review durable manual items, then rerun the finite command"
    if outcome in {"FAILED", "DISCOVERY_INCOMPLETE", "INCOMPLETE"}:
        return "BLOCKED", result.tick.error or outcome, "none", "resolve blocker and rerun the finite command"
    if command in {"finalize", "drain-only"} and outcome in {
        "FINALIZE_NOT_READY",
        "FINALIZE_SNAPSHOT_NOT_READY",
        "DRAIN_NOT_READY",
        "DRAIN_SNAPSHOT_NOT_READY",
    }:
        return "ACTIVE", "none", "none", "rerun after the cutoff snapshot is available"
    if outcome in {"DRAIN_NO_PROGRESS", "DRAIN_MAX_ITERATIONS", "BUDGET_EXHAUSTED"}:
        return "ACTIVE", "none", "none", "rerun drain-only for the remaining durable remote work"
    if outcome == "CLOSEOUT_DEADLINE":
        return "ACTIVE", "none", "none", "the next scheduled closeout continues the durable SQLite state"
    if outcome == "WAITING_SOURCE_COUNT":
        return "ACTIVE", "none", "none", "rerun finalize to discover the latest source counts"
    return "ACTIVE", "none", "none", "run the next finite tick, drain-only, or finalize"


def _is_recoverable_finalize_wait(readiness: FinalizeReadiness | None) -> bool:
    return is_recoverable_finalize_wait(readiness)


def latest_batch_state_snapshot_path(paths: RuntimePaths) -> Path | None:
    """Return the newest immutable human-status snapshot, if one exists."""
    snapshot_dir = paths.run_dir / _BATCH_STATE_SNAPSHOT_DIR
    if not snapshot_dir.is_dir():
        return None
    return max(snapshot_dir.glob(_BATCH_STATE_SNAPSHOT_GLOB), key=lambda item: item.name, default=None)


def _write_batch_state_snapshot(
    path: Path,
    *,
    managed_block: str,
    event_line: str,
    alias_status: str,
) -> Path:
    """Write one immutable status snapshot without replacing an existing file."""
    snapshot_dir = path.parent / _BATCH_STATE_SNAPSHOT_DIR
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    previous = sorted(snapshot_dir.glob(_BATCH_STATE_SNAPSHOT_GLOB), key=lambda item: item.name)
    # Counters change on normal heartbeats; only control-state changes pin history.
    event = json.loads(event_line.removeprefix("- event: ").strip())
    signature = json.dumps([managed_block, {key: value for key, value in event.items() if key not in {"counters", "backlog"}}], sort_keys=True)
    previous_signature = None
    if previous:
        try:
            previous_signature = next((line.removeprefix("retention_signature: ") for line in previous[-1].read_text(encoding="utf-8").splitlines() if line.startswith("retention_signature: ")), None)
        except OSError:
            pass
    retention = "heartbeat" if previous_signature == signature else "transition"
    content = "\n".join((
        "# Incremental price audit batch status snapshot",
        f"retention: {retention}",
        f"retention_signature: {signature}",
        f"legacy_batch_state: {path.name}",
        f"legacy_alias_refresh: {alias_status}",
        managed_block,
        "",
        "## Runtime event",
        event_line.rstrip("\n"),
        "",
    ))
    for attempt in range(4):
        stamp = int(time_module.time_ns())
        snapshot = snapshot_dir / (
            f"BATCH_STATE_{stamp:020d}_{os.getpid()}_{attempt}.md"
        )
        try:
            with snapshot.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Only snapshots explicitly classified by this version are eligible.
            # Unknown legacy files and state-transition evidence stay untouched.
            for old in previous[:-4]:
                try:
                    if "\nretention: heartbeat\n" in old.read_text(encoding="utf-8"):
                        old.unlink()
                except OSError:
                    pass
            return snapshot
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate a unique BATCH_STATE snapshot")


def _refresh_legacy_batch_state_alias(path: Path, updated: str) -> str:
    """Best-effort compatibility refresh; the immutable snapshot is the fallback."""
    last_error: OSError | None = None
    for delay in _BATCH_STATE_ALIAS_RETRY_DELAYS:
        if delay > 0:
            time_module.sleep(delay)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{time_module.time_ns()}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return "updated"
        except OSError as exc:
            last_error = exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Best effort only: metadata temp cleanup must not reverse an
                # already checkpointed business command on Windows.
                pass
    if last_error is None:
        return "blocked"
    winerror = getattr(last_error, "winerror", None)
    suffix = f" winerror={winerror}" if winerror is not None else ""
    return f"blocked {type(last_error).__name__}{suffix}"


def _record_batch_result(
    paths: RuntimePaths,
    result: RuntimeResult,
    *,
    command: str,
    enable_zhaoshang: bool,
    zhaoshang_backend: str = "legacy",
    output_path: Path,
) -> None:
    # The lock winner owns the active run metadata. An overlap observer must
    # not race it and replace a more recent status block.
    if result.tick.outcome == "ALREADY_RUNNING":
        return
    path = _ensure_batch_state(paths, enable_zhaoshang=enable_zhaoshang, zhaoshang_backend=zhaoshang_backend)
    status, blocker, completed_actions, next_action = _batch_result_metadata(result, command=command)
    blocker = " ".join(str(blocker).splitlines()).strip()[:500] or "none"
    managed_block = _batch_state_block(
        paths,
        status=status,
        enable_zhaoshang=enable_zhaoshang,
        zhaoshang_backend=zhaoshang_backend,
        output_path=output_path,
        blocker=blocker,
        completed_actions=completed_actions,
        next_action=next_action,
    )
    safe_event = {
        "command": command,
        "outcome": result.tick.outcome,
        "frozen_snapshot_id": result.tick.frozen_snapshot_id,
        "counters": {str(key): int(value) for key, value in result.tick.counters.items()},
        "backlog": {str(key): int(value) for key, value in result.tick.backlog.items()},
    }
    if result.tick.error:
        safe_event["error_type"] = str(result.tick.error).split("(", 1)[0][:100]
    if result.publication is not None:
        safe_event["publication"] = "materialized"
    if result.publication_blocked:
        safe_event["publication_blocked"] = blocker
        safe_event["error_type"] = result.publication_error_type or "FinalizeBlocked"
    event_line = f"- event: {json.dumps(safe_event, ensure_ascii=True, sort_keys=True)}\n"

    def write_snapshot(alias_status: str) -> None:
        try:
            _write_batch_state_snapshot(
                path,
                managed_block=managed_block,
                event_line=event_line,
                alias_status=alias_status,
            )
        except OSError as exc:
            # SQLite and remote-job checkpoints are authoritative.  Human
            # handoff files must never turn an already checkpointed command
            # into a false production failure.
            print(
                f"[batch-state] snapshot write skipped: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    try:
        with _batch_state_update_lock(path):
            alias_status = "skipped unreadable legacy alias"
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError as exc:
                alias_status = f"skipped unreadable {type(exc).__name__}"
            else:
                updated = _replace_batch_state_block(existing, managed_block)
                if not updated.endswith("\n"):
                    updated += "\n"
                if not updated.endswith(event_line):
                    updated += event_line
                alias_status = _refresh_legacy_batch_state_alias(path, updated)
            write_snapshot(alias_status)
    except TimeoutError:
        # A unique snapshot does not need the compatibility lock.  Preserve the
        # newest human-readable status even when another metadata writer owns it.
        write_snapshot("skipped batch-state lock timeout")


def _customer_index(rows: tuple[dict[str, Any], ...]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for raw in rows:
        document_id = normalize_cell_str(raw.get("documentId"))
        if not document_id:
            continue
        if document_id in index:
            raise RuntimeConfigurationError(f"duplicate customer documentId: {document_id}")
        index[document_id] = customer_row(raw)
    return index


def snapshot_discoverer(
    adapter: CnncSaasSnapshotAdapter,
    *,
    crawl_request_resolver: Callable[[Mapping[str, Any]], CrawlRequest | None] | None = None,
) -> Callable[[float], DiscoveredState]:
    def discover(timeout_seconds: float) -> DiscoveredState:
        if timeout_seconds <= 0:
            raise RuntimeConfigurationError("snapshot discovery has no remaining tick budget")
        provider = getattr(adapter, "cnnc_provider", None)
        if provider is not None:
            try:
                provider.preflight_check()
            except Exception:
                pass  # preflight 失败不阻塞发现；lazy 刷新路径兜底
        snapshot = adapter.fetch(source_label="incremental-runtime", max_elapsed_seconds=timeout_seconds)
        customer = _customer_index(snapshot.customer_rows)
        # Issue #9 sidecar-only channel: every observed customer document,
        # including customer-only docs with no SaaS pending row.  A row lacking
        # fourCategoryId keeps "" (WAITING_CATEGORY semantics: the prewarm
        # store parks it out of detail scanning until a later sighting supplies
        # one).  NEVER consumed by STEP1 stages; only the prewarm intake hook
        # reads it.
        prewarm_rows = [
            {
                "documentId": document_id,
                "fourCategoryId": str(row.get("fourCategoryId") or ""),
                "fourCategoryName": str(row.get("fourCategoryName") or ""),
                "commodityName": str(row.get("commodityName") or ""),
                "referenceLink": str(row.get("referenceLink") or ""),
            }
            for document_id, row in sorted(customer.items())
        ]
        saas_rows = [normalize_saas_pending_row(dict(row)) for row in snapshot.saas_rows]
        joined = join_saas_with_customer(saas_rows, customer)
        # Eligibility is part of the authoritative daily snapshot. An URL on
        # an unsupported mall must not block finalization; a vendor-mapped
        # legacy request can be eligible even when the URL is blank.
        for row in joined:
            crawl_request = crawl_request_resolver(row) if crawl_request_resolver is not None else None
            row["__crawl_required"] = crawl_request is not None
            row["__crawl_mall_key"] = crawl_request.mall_key if crawl_request is not None else ""
            row["__crawl_mall_source_id"] = crawl_request.mall_source_id if crawl_request is not None else ""
            row["__crawl_backend_version"] = crawl_request.backend_version if crawl_request is not None else ""
        customer_fps = [
            customer_fingerprint(customer.get(normalize_cell_str(row.get("vCommodityId")), {}))
            for row in saas_rows
        ]
        return DiscoveredState(
            observed_at=snapshot.observed_at_utc,
            headers=SAAS_PENDING_HEADERS,
            rows=joined,
            source_fingerprints=[source_fingerprint(row) for row in joined],
            customer_fingerprints=customer_fps,
            complete=True,
            evidence={
                "source_label": snapshot.source_label or "",
                "saas_count": snapshot.saas_count,
                "customer_count": snapshot.customer_count,
                "pages": [evidence.__dict__ for evidence in snapshot.page_evidence],
            },
            prewarm_rows=prewarm_rows,
        )

    return discover


def _prewarm_intake_worker(store_path: Path) -> Callable[[str, Sequence[Mapping[str, Any]]], None]:
    """Issue #9 sidecar intake (lazy, fail-open): the prewarm store is only
    constructed on the first intake call, inside the tick hook's try/except.

    A corrupt/locked/unwritable prewarm DB therefore can never break STEP1
    dependency composition; it only counts ``prewarm_intake_failures``.
    Exceptions propagate to the tick hook, which counts them and continues;
    this worker itself performs no network or remote action.
    """
    store: CustomerPrewarmStore | None = None

    def intake(business_date: str, rows: Sequence[Mapping[str, Any]]) -> None:
        nonlocal store
        if store is None:
            store = CustomerPrewarmStore(store_path)
        intake_documents(store, business_date, rows, source="event")

    return intake


def _detail_worker(
    template: Mapping[str, Any],
    *,
    detail_lock_path: Path | str | None = None,
    request_interval_seconds: float = 0.5,
    sleep: Callable[[float], None] = time_module.sleep,
    monotonic: Callable[[], float] = time_module.monotonic,
    lock_factory: Callable[[Path | str], ProcessLock] = ProcessLock,
    cnnc_provider: Any | None = None,
) -> Callable[[Mapping[str, Any], float], Mapping[str, Any]]:
    from app.services.saas_audit_core import extract_customer_price_from_detail

    def detail(row: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]:
        existing_price = parse_decimal(row.get("customerPrice"))
        if is_valid_reference_price(existing_price):
            return {
                "customerPrice": existing_price,
                "customerPriceStatus": "EXISTS",
                "customerPriceSource": "EXCEL_INPUT",
                "customerPriceWarning": "",
                "customerPriceEvidence": "existing input reference price",
            }
        if normalize_cell_str(row.get("customer_lookup_status")) == "NOT_FOUND":
            # 2026-09-11 rule change: the authoritative customer snapshot round
            # completed (fetch guards count drift/exhaustion) and this
            # documentId is absent — "nothing to process this round", a
            # non-blocking skip, never a technical failure.
            return {
                "customerPrice": None,
                "customerPriceStatus": "CUSTOMER_NOT_FOUND",
                "customerPriceSource": "",
                "customerPriceWarning": "customer export did not contain this documentId",
                "customerPriceEvidence": "authoritative customer snapshot round complete; documentId absent",
            }
        document_id = normalize_cell_str(row.get("vCommodityId") or row.get("documentId"))
        category_id = normalize_cell_str(row.get("fourCategoryId"))
        if not document_id or not category_id:
            return {
                "customerPrice": None,
                "customerPriceStatus": "DETAIL_REQUEST_SKIPPED",
                "customerPriceSource": "",
                "customerPriceWarning": "missing documentId or fourCategoryId",
                "customerPriceEvidence": "",
            }
        started = monotonic()
        detail_deadline = started + timeout_seconds  # detail 总预算（绝对单调时刻）
        detail_lock = lock_factory(detail_lock_path) if detail_lock_path is not None else None

        def _auth_rejected(resp: Mapping[str, Any]) -> bool:
            payload_obj = resp.get("payload")
            return (
                resp.get("ok")
                and isinstance(payload_obj, dict)
                and _payload_is_auth_failure(payload_obj)
            ) or (
                (not resp.get("ok"))
                and resp.get("http_status") == 401
            )

        try:
            if detail_lock is not None:
                while True:
                    try:
                        detail_lock.acquire()
                        break
                    except LeaseUnavailable:
                        remaining = timeout_seconds - (monotonic() - started)
                        if remaining <= 0:
                            raise TimeoutError("global Detail concurrency lock deadline exhausted")
                        sleep(min(0.05, remaining))
            remaining = timeout_seconds - (monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Detail deadline exhausted before request")
            config = dict(template)
            current_at: str | None = None
            if cnnc_provider is not None:
                current_at = cnnc_provider.get_access_token()
                _apply_cnnc_at(config, current_at)
            response = call_detail_api(
                config,
                document_id,
                category_id,
                timeout=max(0.001, remaining),
            )
            if cnnc_provider is not None and _auth_rejected(response):
                # AT 被拒（payload 认证失败或 HTTP 401）：刷新一次并重发（第二次
                # 结果为准，不第三次请求）。refresh 受 SSO 冷却断路器与 detail
                # 总预算 deadline 约束，其 CnncAuthError 是 RuntimeError 子类，
                # 直接上抛（fail-fast）。
                current_at = cnnc_provider.refresh(
                    stale_access_token=current_at,
                    deadline_monotonic=detail_deadline,
                )
                config = dict(template)
                _apply_cnnc_at(config, current_at)
                remaining = timeout_seconds - (monotonic() - started)
                response = call_detail_api(
                    config,
                    document_id,
                    category_id,
                    timeout=max(0.001, remaining),
                )
        finally:
            if detail_lock is not None:
                detail_lock.release()
        remaining = max(0.0, timeout_seconds - (monotonic() - started))
        if request_interval_seconds > 0 and remaining >= request_interval_seconds:
            sleep(request_interval_seconds)
        if (
            cnnc_provider is not None
            and _auth_rejected(response)
        ):
            try:
                auth_status = str(
                    (cnnc_provider.get_status() or {}).get("status") or ""
                )
            except Exception:
                auth_status = ""  # duck-typed provider 无 get_status 时不掩盖原错误
            suffix = f"（认证状态: {auth_status}）" if auth_status else ""
            raise RuntimeError(f"CNNC 认证失败：刷新重试后仍被拒{suffix}")
        if not response.get("ok"):
            # Typed transport failure: keep the real HTTP status so the durable
            # retry classifier can separate transient (None/408/429/5xx/403/412)
            # from deterministic request errors (other 4xx).
            raise DetailRequestError(
                http_status=response.get("http_status"),
                message=response.get("error"),
                retryable=is_retryable_detail_http_status(response.get("http_status")),
            )
        payload = response.get("payload")
        result = extract_customer_price_from_detail(payload)
        # RC-1: a retryable business failure must never masquerade as a
        # successful completion.  NOT_FOUND / REQUEST_FAILED / PARSE_FAILED are
        # non-deterministic outcomes: raise so the durable stage records a
        # retryable failure with the structured (sanitized, decodable) error
        # prefix.  Deterministic skips (EXISTS / CUSTOMER_NOT_FOUND /
        # DETAIL_REQUEST_SKIPPED) still return normally — re-running them
        # cannot change the answer.
        if str(result.get("customerPriceStatus") or "") in CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES:
            raise DetailBusinessRetryRequired(result)
        return result

    return detail


def _read_model_configuration(path: Path | None = None) -> dict[str, str]:
    if path is None:
        path = PROJECT_DATA_ROOT / "model.env"
    values = {
        key: os.environ[key].strip()
        for key in _MODEL_CONFIGURATION_KEYS
        if os.environ.get(key, "").strip()
    }
    for key, parsed_value in _parse_env_file(path).items():
        if key in _MODEL_CONFIGURATION_KEYS and key not in values and parsed_value:
            values[key] = parsed_value
    return values


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    parsed: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        parsed_value = value.strip().strip('"').strip("'")
        if key and parsed_value:
            parsed[key] = parsed_value
    return parsed


def _read_model_runtime_tuning(path: Path | None = None) -> dict[str, str]:
    """Optional dispatcher tuning: process environment wins, then
    data/model.env; only keys actually present are returned."""
    if path is None:
        path = PROJECT_DATA_ROOT / "model.env"
    values = {
        key: os.environ[key].strip()
        for key in _MODEL_RUNTIME_TUNING_KEYS
        if os.environ.get(key, "").strip()
    }
    for key, parsed_value in _parse_env_file(path).items():
        if key in _MODEL_RUNTIME_TUNING_KEYS and key not in values:
            values[key] = parsed_value
    return values


def _load_model_configuration(path: Path | None = None) -> dict[str, str]:
    values = _read_model_configuration(path)
    if any(not values.get(key, "").strip() for key in _MODEL_CONFIGURATION_KEYS):
        raise RuntimeConfigurationError("title-match model configuration is incomplete")
    return values


def _configured_model_name(path: Path | None = None) -> str:
    model_name = _read_model_configuration(path).get("TITLE_MATCH_MODEL_NAME", "").strip()
    # Production composition already calls _load_model_configuration() while
    # building the model dependency.  The fallback keeps dependency-injected
    # unit tests and offline tools independent from untracked local secrets.
    return model_name or "injected-title-match"


@contextmanager
def _temporary_model_environment(configuration: Mapping[str, str]):
    previous = {key: os.environ.get(key) for key in configuration}
    try:
        os.environ.update({key: str(value) for key, value in configuration.items()})
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _export_model_environment(configuration: Mapping[str, str]) -> None:
    """Idempotently export model configuration for concurrent batch workers.

    The batch callback fans out over a bounded thread pool once
    TITLE_MATCH_MODEL_MAX_CONCURRENCY exceeds one, and mutate-then-restore of
    ``os.environ`` would race between threads.  The export only fills keys that
    are absent from the environment — an explicit environment pin keeps its
    value (the same precedence ``_read_model_configuration`` applies) — and
    writing the same resolved value twice is benign, so no lock is required.
    The scheduled process is one-shot, so leaving the keys exported is safe.
    """
    for key, value in configuration.items():
        if os.environ.get(key, "").strip():
            continue
        os.environ[key] = str(value)


def _persistable_model_result(result: Any) -> Mapping[str, Any]:
    payload = result.model_dump(mode="json")
    payload.pop("raw_response", None)
    return payload


def _model_worker(row: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]:
    configuration = _load_model_configuration()
    with _temporary_model_environment(configuration):
        result = call_title_match_model(
            TitleMatchModelRequest(
                query_str=normalize_cell_str(row.get("queryStr")),
                sku_name=normalize_cell_str(row.get("skuName")),
                model_audit_result=normalize_cell_str(row.get("modelAuditResult")),
                is_match=normalize_cell_str(row.get("isMatch")),
            ),
            timeout_seconds=max(0.001, timeout_seconds),
            max_attempts=3,
            max_elapsed_seconds=max(0.001, timeout_seconds),
        )
    return _persistable_model_result(result)


def _model_batch_worker(rows: list[Mapping[str, Any]], timeout_seconds: float) -> Mapping[int, Mapping[str, Any]]:
    configuration = _load_model_configuration()
    requests = [
        TitleMatchModelRequest(
            query_str=normalize_cell_str(row.get("queryStr")),
            sku_name=normalize_cell_str(row.get("skuName")),
            model_audit_result=normalize_cell_str(row.get("modelAuditResult")),
            is_match=normalize_cell_str(row.get("isMatch")),
        )
        for row in rows
    ]
    _export_model_environment(configuration)
    results = call_title_match_model_batch(
        requests,
        timeout_seconds=max(0.001, timeout_seconds),
        max_attempts=3,
        max_elapsed_seconds=max(0.001, timeout_seconds),
    )
    return {index: _persistable_model_result(result) for index, result in results.items()}


def _runtime_model_batch_size(path: Path | None = None) -> int:
    """Rows per model batch; default 10.  Tuned through the incremental-only
    TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE (process environment first, then
    data/model.env); anything absent/0/negative or non-integer fails closed.

    The work_01 key TITLE_MATCH_MODEL_BATCH_SIZE is deliberately NOT read
    here — a historical data/model.env value written for work_01 (default 30)
    must never silently activate on the incremental pipeline."""
    raw = _read_model_runtime_tuning(path).get("TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE", "")
    if not raw:
        return 10
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeConfigurationError(
            "TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE must be a positive integer"
        ) from exc
    if value <= 0:
        raise RuntimeConfigurationError(
            "TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE must be a positive integer"
        )
    return value


def _runtime_model_max_concurrency(path: Path | None = None) -> int:
    """Bounded parallel model batch HTTP calls; validated by the offline
    300/500-row benchmarks in tests/test_model_parallel_batches.py.

    Tunable per deployment through TITLE_MATCH_MODEL_MAX_CONCURRENCY (process
    environment first, then data/model.env); anything absent/0/negative or
    non-integer fails closed."""
    raw = _read_model_runtime_tuning(path).get("TITLE_MATCH_MODEL_MAX_CONCURRENCY", "")
    if not raw:
        return 6
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeConfigurationError("TITLE_MATCH_MODEL_MAX_CONCURRENCY must be a positive integer") from exc
    if value <= 0:
        raise RuntimeConfigurationError("TITLE_MATCH_MODEL_MAX_CONCURRENCY must be a positive integer")
    return value


def _logical_key_matches_rebuild(logical_key: str, natural_key: str) -> bool:
    """A manual rebuild replacement persists the natural adapter logical key
    plus a '#rebuild#' action-id suffix, so reconstruction accepts that exact
    suffix family in addition to the unchanged natural key."""
    return logical_key == natural_key or logical_key.startswith(f"{natural_key}#rebuild#")


def _remote_job_backend(job: Mapping[str, Any]) -> str | None:
    """Recover the creator backend from immutable persisted payload evidence."""

    raw = job.get("payload_json") or "{}"
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("contract_version") == BATCH_CONTRACT_VERSION:
        return "batch"
    requests = payload.get("requests")
    if not isinstance(requests, list) or not requests:
        return None
    if any(not isinstance(request, Mapping) for request in requests):
        return None
    versions = {
        normalize_cell_str(request.get("backend_version"))
        for request in requests
    }
    if versions and "" not in versions and all(
        version.startswith("legacy-") for version in versions
    ):
        return "legacy"
    return None


def _remote_job_disposition(
    job: Mapping[str, Any],
    *,
    selected_backend: str,
    batch_priority_enabled: bool,
) -> str:
    backend = _remote_job_backend(job)
    if backend is None:
        return "invalid"
    if (
        backend == "batch"
        and str(job.get("state")) == "PRIORITY_PENDING"
        and not batch_priority_enabled
    ):
        return "deferred"
    return "accept"


def _route_zhaoshang_harvest(
    legacy_harvest: Callable[[list[Mapping[str, Any]], float], Mapping[int, Mapping[str, Any]]],
    batch_harvest: Callable[[list[Mapping[str, Any]], float], Mapping[int, Mapping[str, Any]]],
) -> Callable[[list[Mapping[str, Any]], float], Mapping[int, Mapping[str, Any]]]:
    """Route persisted jobs by immutable payload, never by current CLI choice."""

    def harvest(
        jobs: list[Mapping[str, Any]], timeout: float
    ) -> Mapping[int, Mapping[str, Any]]:
        grouped = {
            "legacy": [job for job in jobs if _remote_job_backend(job) == "legacy"],
            "batch": [job for job in jobs if _remote_job_backend(job) == "batch"],
        }
        total = sum(len(values) for values in grouped.values())
        results: dict[int, Mapping[str, Any]] = {}
        for backend, callback in (("legacy", legacy_harvest), ("batch", batch_harvest)):
            selected = grouped[backend]
            if not selected:
                continue
            share = max(0.001, float(timeout) * len(selected) / max(1, total))
            try:
                results.update(callback(selected, share) or {})
            except Exception as exc:
                error = f"{backend} harvest failed: {type(exc).__name__}"
                for job in selected:
                    evidence: dict[str, Any] = {
                        "state": str(job.get("state") or "RUNNING"),
                        "retryable_error": error,
                    }
                    identity = str(job.get("remote_identity") or "").strip()
                    if identity:
                        evidence["remote_identity"] = identity
                    results[int(job["id"])] = evidence
        return results

    return harvest


def _zhaoshang_backend_version_for_mall(
    mall_key: str, *, url: str, selected_backend: str
) -> str:
    if selected_backend not in {"legacy", "batch"}:
        raise RuntimeConfigurationError(
            "Zhaoshang backend must be legacy or batch"
        )
    if (
        selected_backend == "batch"
        and mall_key in ALLOWED_MALLS
        and is_batch_submitted_url_supported(mall_key, url)
    ):
        return "batch-url-v1"
    return "legacy-v1"


def _route_zhaoshang_creation(
    legacy_plan: Callable[[list[Mapping[str, Any]], float], list[CrawlPlan]],
    legacy_finalize: Callable[[CrawlPlan, tuple[CrawlMember, ...], float], CrawlPlan],
    legacy_submit: Callable[
        [Mapping[str, Any], list[Mapping[str, Any]], float], Mapping[str, Any]
    ],
    batch_plan: Callable[[list[Mapping[str, Any]], float], list[CrawlPlan]],
    batch_finalize: Callable[[CrawlPlan, tuple[CrawlMember, ...], float], CrawlPlan],
    batch_submit: Callable[
        [Mapping[str, Any], list[Mapping[str, Any]], float], Mapping[str, Any]
    ],
) -> tuple[
    Callable[[list[Mapping[str, Any]], float], list[CrawlPlan]],
    Callable[[CrawlPlan, tuple[CrawlMember, ...], float], CrawlPlan],
    Callable[
        [Mapping[str, Any], list[Mapping[str, Any]], float], Mapping[str, Any]
    ],
]:
    """Route new work by frozen policy and resumed intent by durable payload."""

    def backend_for_version(value: object) -> str:
        version = normalize_cell_str(value)
        if version == "batch-url-v1":
            return "batch"
        if version.startswith("legacy-"):
            return "legacy"
        raise RuntimeConfigurationError(
            "crawl creation has no valid durable backend version"
        )

    def plan(
        items: list[Mapping[str, Any]], timeout: float
    ) -> list[CrawlPlan]:
        grouped: dict[str, list[Mapping[str, Any]]] = {
            "legacy": [],
            "batch": [],
        }
        for item in items:
            row = item.get("row")
            if not isinstance(row, Mapping):
                raise RuntimeConfigurationError("crawl planning item has no row")
            if "__crawl_required" in row and not bool(row.get("__crawl_required")):
                continue
            grouped[
                backend_for_version(row.get("__crawl_backend_version"))
            ].append(item)
        plans: list[CrawlPlan] = []
        for backend, callback in (
            ("legacy", legacy_plan),
            ("batch", batch_plan),
        ):
            selected = grouped[backend]
            if not selected:
                continue
            produced = list(callback(selected, timeout))
            for proposal in produced:
                proposal_backends = {
                    backend_for_version(member.backend_version)
                    for member in proposal.members
                }
                if proposal_backends != {backend}:
                    raise RuntimeConfigurationError(
                        "crawl planner crossed durable backend boundaries"
                    )
            plans.extend(produced)
        return plans

    def finalize(
        proposal: CrawlPlan,
        members: tuple[CrawlMember, ...],
        timeout: float,
    ) -> CrawlPlan:
        backends = {
            backend_for_version(member.backend_version) for member in members
        }
        if len(backends) != 1:
            raise RuntimeConfigurationError(
                "crawl plan must contain a single durable backend"
            )
        backend = next(iter(backends))
        callback = legacy_finalize if backend == "legacy" else batch_finalize
        return callback(proposal, members, timeout)

    def submit(
        job: Mapping[str, Any],
        crawls: list[Mapping[str, Any]],
        timeout: float,
    ) -> Mapping[str, Any]:
        backend = _remote_job_backend(job)
        if backend == "legacy":
            return legacy_submit(job, crawls, timeout)
        if backend == "batch":
            return batch_submit(job, crawls, timeout)
        raise RuntimeConfigurationError(
            "remote intent has no valid durable backend"
        )

    return plan, finalize, submit


def _exact_batch_crawl_mapping(
    prepared_job: Any,
    crawl_work: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    mapping: dict[str, Mapping[str, Any]] = {}
    source_ids = tuple(
        getattr(prepared_job, "source_ids", ())
        or ("" for _request in prepared_job.requests)
    )
    for request, source_id, execute_number in zip(
        prepared_job.requests,
        source_ids,
        prepared_job.execute_numbers,
        strict=True,
    ):
        matches = [
            crawl
            for crawl in crawl_work
            if normalize_cell_str(crawl.get("mall_key")) == request.mall_key
            and normalize_cell_str(crawl.get("sku_id")) == request.sku_id
            and normalize_cell_str(crawl.get("exact_url")) == request.url.strip()
            and normalize_cell_str(crawl.get("backend_version"))
            == request.backend_version
            and (
                not source_id
                or normalize_cell_str(crawl.get("source_id")) == source_id
            )
        ]
        if len(matches) != 1:
            raise RuntimeConfigurationError(
                "batch executeNumber must map to exactly one linked crawl_work"
            )
        mapping[execute_number] = matches[0]
    if len(mapping) != len(prepared_job.execute_numbers):
        raise RuntimeConfigurationError(
            "batch executeNumber mapping is incomplete"
        )
    mapped_ids = {int(crawl["id"]) for crawl in mapping.values()}
    if len(mapped_ids) != len(mapping) or len(crawl_work) != len(mapping):
        raise RuntimeConfigurationError(
            "batch executeNumber must map to exactly one linked crawl_work"
        )
    return mapping


def legacy_zhaoshang_bridge(
    adapter: CnncLegacyZhaoshangAdapter,
    *,
    paths: RuntimePaths,
    business_date: str,
    monotonic: Callable[[], float] = monotonic_time.monotonic,
) -> tuple[Callable[[list[Mapping[str, Any]], float], list[CrawlPlan]], Callable[[CrawlPlan, tuple[CrawlMember, ...], float], CrawlPlan], Callable[[Mapping[str, Any], list[Mapping[str, Any]], float], Mapping[str, Any]], Callable[[list[Mapping[str, Any]], float], Mapping[int, Mapping[str, Any]]], Callable[[Mapping[str, Any]], Mapping[str, Any] | None], Callable[[Mapping[str, Any], str | None], Mapping[str, Any]]]:
    """Bridge only legacy create-only and wait-export-only modes.

    No priority API is called here. The legacy tool retains its own controlled
    priority behaviour; this bridge neither imports nor invokes its SQL code.
    The returned recovery pair exposes the read-only RUNNING probe and the
    single exact disable->enable restart for the stall-recovery pass.
    """
    batch_state = paths.run_dir / "BATCH_STATE.md"
    prepared: dict[str, Any] = {}

    def resolve_persisted_request(row: Mapping[str, Any]) -> CrawlRequest | None:
        has_persisted_policy = "__crawl_required" in row
        if has_persisted_policy and not bool(row.get("__crawl_required")):
            return None
        backend_version = (
            normalize_cell_str(row.get("__crawl_backend_version"))
            if has_persisted_policy
            else "legacy-v1"
        )
        if has_persisted_policy and not backend_version:
            raise RuntimeConfigurationError("frozen crawl policy has no backend version")
        expected_mall = normalize_cell_str(row.get("__crawl_mall_key"))
        if has_persisted_policy and expected_mall == "jslink":
            request = adapter.request_from_saas_row(
                row,
                backend_version=backend_version,
                preserve_historical_identity=True,
            )
        else:
            request = adapter.request_from_saas_row(
                row, backend_version=backend_version
            )
        if not has_persisted_policy:
            return request
        if request is None:
            raise RuntimeConfigurationError("frozen crawl policy is no longer resolvable")
        expected_policy = (
            normalize_cell_str(row.get("__crawl_mall_key")),
            normalize_cell_str(row.get("__crawl_mall_source_id")),
            backend_version,
        )
        actual_policy = (request.mall_key, request.mall_source_id, request.backend_version)
        if not all(expected_policy) or actual_policy != expected_policy:
            raise RuntimeConfigurationError("frozen crawl policy conflicts with the current legacy mapping")
        return request

    def propose(items: list[Mapping[str, Any]], _timeout: float) -> list[CrawlPlan]:
        groups: dict[tuple[str, str], list[CrawlMember]] = {}
        for item in items:
            request = resolve_persisted_request(item["row"])
            if request is None:
                continue
            source_identity = normalize_cell_str(item["row"].get("vCommodityId") or item["row"].get("documentId"))
            if not source_identity:
                raise RuntimeConfigurationError("eligible crawl member has no SaaS source identity")
            member = CrawlMember(request.mall_key, source_identity, request.sku_id, request.url, request.backend_version, item["row"])
            groups.setdefault((request.mall_key, request.mall_source_id), []).append(member)
        return [CrawlPlan(mall_key=key[0], logical_key=f"proposal:{key[0]}:{key[1]}", task_name="", payload_hash="", members=tuple(members)) for key, members in groups.items()]

    def finalize(plan: CrawlPlan, members: tuple[CrawlMember, ...], _timeout: float) -> CrawlPlan:
        if not members:
            raise ValueError("legacy template requires durable pending members")
        proposal_index = 0
        for member in members:
            while proposal_index < len(plan.members) and plan.members[proposal_index] != member:
                proposal_index += 1
            if proposal_index >= len(plan.members):
                raise ValueError("legacy template members must be an ordered subset of the proposal")
            proposal_index += 1
        _ensure_batch_state(paths, enable_zhaoshang=True)
        requests = []
        for member in members:
            source = resolve_persisted_request(member.row)
            if source is None or (
                source.mall_key,
                source.mall_source_id,
                source.sku_id,
                source.url.strip(),
                source.backend_version,
            ) != (
                member.mall_key,
                normalize_cell_str(member.row.get("__crawl_mall_source_id")) or source.mall_source_id,
                member.sku_id,
                member.exact_url.strip(),
                member.backend_version,
            ):
                raise RuntimeConfigurationError("legacy mall mapping changed for durable crawl member")
            requests.append(source)
        task_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "business_date": business_date,
                    "requests": [request.identity for request in sorted(requests, key=lambda value: value.identity)],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        numeric_suffix = f"{int(task_fingerprint, 16) % (10 ** 14):014d}"
        # The legacy tool normalizes every browser task to this exact display
        # name before both creation and priority logging. Persist that final
        # identity up front so submit evidence, unknown reconciliation and
        # wait-export-only all address the same remote task.
        task_name = f"cnnc_current_opt_{requests[0].mall_name}_{numeric_suffix}"
        job_dir = paths.run_dir / "zhaoshang" / plan.mall_key / numeric_suffix
        job_dir.mkdir(parents=True, exist_ok=True)
        job = adapter.prepare_job(business_date, requests, job_dir / "input.xlsx", job_dir / "result.xlsx", task_name, batch_state)
        prepared[job.logical_job_key] = job
        payload = {"requests": [request.__dict__ for request in job.requests], "batch_state": str(job.batch_state_path)}
        return CrawlPlan(plan.mall_key, job.logical_job_key, job.task_name, job.payload_sha256, payload_json=payload, input_path=str(job.template_path), result_path=str(job.result_path), members=members)

    def reconstruct(job: Mapping[str, Any]) -> Any:
        """Rebuild immutable legacy evidence after a process crash, never POST."""
        logical_key = str(job["logical_key"])
        cached = prepared.get(logical_key)
        if cached is not None:
            return cached
        payload_value = job.get("payload_json") or "{}"
        payload = json.loads(payload_value) if isinstance(payload_value, str) else dict(payload_value)
        requests = [CrawlRequest(**dict(row)) for row in payload["requests"]]
        batch_path = Path(str(payload["batch_state"]))
        input_path = Path(str(job.get("input_path") or ""))
        result_path = Path(str(job.get("result_path") or ""))
        if not batch_path.is_file() or not input_path.is_file() or not result_path.parent.is_dir():
            raise ValueError("durable legacy paths are unavailable")
        rebuilt = adapter.prepare_job(
            business_date, requests, input_path, result_path, str(job["task_name"]), batch_path
        )
        if not _logical_key_matches_rebuild(logical_key, rebuilt.logical_job_key) or rebuilt.payload_sha256 != str(job.get("payload_hash") or ""):
            raise ValueError("durable legacy job evidence does not match immutable payload")
        prepared[logical_key] = rebuilt
        return rebuilt

    def remote_identity(prepared_job: Any, task_id: str | None, task_number: str | None) -> str:
        if not task_id or not task_number:
            raise RuntimeConfigurationError("legacy remote task identity is incomplete")
        return f"{prepared_job.mall_key}|{task_id}|{task_number}"

    def remote_identity_parts(prepared_job: Any, value: object) -> tuple[str, str]:
        parts = str(value or "").split("|")
        if len(parts) != 3 or parts[0] != prepared_job.mall_key or not parts[1] or not parts[2]:
            raise RuntimeConfigurationError("legacy remote task identity is invalid")
        return parts[1], parts[2]

    def submit(job: Mapping[str, Any], _crawls: list[Mapping[str, Any]], timeout: float) -> Mapping[str, Any]:
        try:
            prepared_job = reconstruct(job)
        except Exception as exc:
            raise RuntimeConfigurationError("missing exact prepared legacy job; refusing create") from exc
        outcome = adapter.submit(prepared_job, timeout_seconds=max(0.001, timeout))
        if outcome.status is SubmitStatus.SUBMIT_UNKNOWN:
            raise TimeoutError("legacy create-only outcome unknown")
        if outcome.status is not SubmitStatus.SUBMITTED:
            raise SubmitNotAttemptedError(
                f"legacy create-only never dispatched: {outcome.diagnostic}"
            )
        state = (
            "SUBMITTED"
            if outcome.priority_evidence in {"updated", "already_highest_priority"}
            else "PRIORITY_PENDING"
        )
        return {
            "remote_identity": remote_identity(prepared_job, outcome.task_id, outcome.task_number),
            "state": state,
            "priority_evidence": outcome.priority_evidence,
        }

    def harvest(jobs: list[Mapping[str, Any]], timeout: float) -> Mapping[int, Mapping[str, Any]]:
        results: dict[int, Mapping[str, Any]] = {}
        deadline = monotonic() + max(0.0, timeout)
        for index, job in enumerate(jobs):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            job_timeout = max(0.001, remaining / max(1, len(jobs) - index))
            job_started = monotonic()
            def publish(payload: Mapping[str, Any]) -> None:
                results[int(job["id"])] = {**payload, "elapsed_seconds": max(0.0, monotonic() - job_started)}
            try:
                prepared_job = reconstruct(job)
            except Exception:
                publish({"manual_error": "exact legacy job reconstruction is unavailable"})
                continue
            if str(job.get("state")) == "SUBMIT_UNKNOWN":
                reconcile = adapter.reconcile_unknown(prepared_job, timeout_seconds=job_timeout)
                if reconcile.status is ReconcileStatus.FOUND:
                    try:
                        identity = remote_identity(prepared_job, reconcile.task_id, reconcile.task_number)
                    except RuntimeConfigurationError:
                        publish({"manual_error": "exact legacy reconciliation returned incomplete identity"})
                    else:
                        publish({"state": "PRIORITY_PENDING", "remote_identity": identity})
                elif reconcile.status is ReconcileStatus.MANUAL:
                    publish({"manual_error": "exact legacy unknown-submit reconciliation did not confirm a task"})
                continue
            if str(job.get("state")) == "PRIORITY_PENDING":
                try:
                    task_id, task_number = remote_identity_parts(prepared_job, job.get("remote_identity"))
                except RuntimeConfigurationError:
                    publish({"manual_error": "priority pending job has invalid remote identity"})
                    continue
                priority = adapter.prioritize(
                    prepared_job,
                    task_id=task_id,
                    task_number=task_number,
                    timeout_seconds=job_timeout,
                )
                if priority.status in {PriorityStatus.UPDATED, PriorityStatus.ALREADY_HIGHEST}:
                    publish({
                        "state": "SUBMITTED",
                        "remote_identity": str(job.get("remote_identity")),
                        "result": {
                            "priority_status": priority.status.value,
                            "weight_type": priority.weight_type,
                        },
                    })
                elif priority.retryable:
                    publish({
                        "state": "PRIORITY_PENDING",
                        "remote_identity": str(job.get("remote_identity")),
                        "retryable_error": (
                            priority.diagnostic
                            or "transient priority infrastructure failure"
                        ),
                        "result": {
                            "state": "PRIORITY_PENDING",
                            "priority_status": priority.status.value,
                            "diagnostic": priority.diagnostic,
                        },
                    })
                else:
                    publish({"manual_error": priority.diagnostic or "highest priority was not confirmed"})
                continue
            outcome = adapter.harvest(prepared_job, timeout_seconds=job_timeout)
            if outcome.status is HarvestStatus.EXPORTED:
                result_map = dict(outcome.result_map or {})
                result_by_identity = dict(outcome.result_by_identity or {})
                no_result_identities = set(outcome.no_result_identities)
                manual_identities = dict(outcome.manual_identities or {})
                sku_count_by_url: dict[str, set[str]] = {}
                for request in prepared_job.requests:
                    sku_count_by_url.setdefault(request.url.strip(), set()).add(request.sku_id)
                crawl_results: dict[int, Mapping[str, Any]] = {}
                for crawl in job.get("crawl_work", []):
                    exact_url = normalize_cell_str(crawl.get("exact_url"))
                    sku_id = normalize_cell_str(crawl.get("sku_id"))
                    value = result_by_identity.get((sku_id, exact_url))
                    # A URL-only export is safe only when this submitted job
                    # proves that the URL represents exactly one SKU. The
                    # durable crawl identity includes source_id, so this same
                    # pair can still fan out to multiple source documents.
                    if value is None and len(sku_count_by_url.get(exact_url, set())) == 1:
                        value = result_map.get(exact_url)
                    identity = (sku_id, exact_url)
                    if value is not None:
                        crawl_results[int(crawl["id"])] = value
                    elif identity in no_result_identities:
                        crawl_results[int(crawl["id"])] = {
                            "no_result": True,
                            "reason": "provenance-valid remote request has no result row",
                        }
                    elif identity in manual_identities:
                        crawl_results[int(crawl["id"])] = {
                            "manual": True,
                            "error": manual_identities[identity],
                        }
                    else:
                        crawl_results[int(crawl["id"])] = {
                            "manual": True,
                            "error": "provenance-valid row lacks durable request-group mapping",
                        }
                state = (
                    "EXPORTED"
                    if any(
                        isinstance(value, Mapping) and value.get("manual")
                        for value in crawl_results.values()
                    )
                    else "COMPLETED"
                )
                publish({
                    "state": state,
                    "remote_identity": str(job.get("remote_identity") or "legacy"),
                    "export_path": str(prepared_job.result_path),
                    "result": result_map,
                    "crawl_results": crawl_results,
                })
            elif outcome.status is HarvestStatus.RUNNING:
                publish({"state": "RUNNING", "remote_identity": str(job.get("remote_identity") or "legacy")})
            else:
                publish({"manual_error": "legacy wait-export-only requires manual review"})
        return results

    def recovery_probe(job: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any] | None:
        try:
            prepared_job = reconstruct(job)
            task_id, task_number = remote_identity_parts(prepared_job, job.get("remote_identity"))
            probe = adapter.probe_running_task(
                prepared_job, task_id, task_number, float(timeout_seconds)
            )
        except RemoteTaskIdentityConflictError:
            # Round 7 IMPORTANT B: a PROVEN conflict between the live exact
            # task and the durable remote identity is permanent, not a
            # transient probe fault — the recovery pass fail-closes to manual
            # on the first occurrence instead of counting toward the cap.
            return {
                "probe_identity_conflict": True,
                "diagnostic": "probe raised RemoteTaskIdentityConflictError",
            }
        except LegacyProbeEvidenceError:
            # Round 7 BLOCKER A: the probe completed at the transport level
            # but the adapter proved it carries no usable execution evidence
            # (business-failed/malformed/contradictory progress, no trusted
            # terminal status, no actual export result).  This is a durable
            # probe failure for the recovery pass, distinct from transient
            # transport faults only by kind — both count toward the cap.
            return {
                "probe_failed": True,
                "failure_kind": "UNUSABLE_EVIDENCE",
                "diagnostic": "probe raised LegacyProbeEvidenceError",
            }
        except Exception as exc:
            # A probe that cannot re-confirm the exact remote task proves
            # nothing and never blocks the tick — but since Round 6 the
            # failure is reported explicitly so the durable layer can count
            # consecutive probe failures instead of retrying a permanent
            # fault (expired refreshToken, missing tool) forever.  Only the
            # exception TYPE is recorded; message bodies never enter
            # durable storage.
            return {"probe_failed": True, "diagnostic": f"probe raised {type(exc).__name__}"}
        return {
            "remote_mall": probe.mall,
            "remote_task_id": probe.task_id,
            "remote_task_number": probe.task_number,
            "progress": dict(probe.progress),
            "export_count": probe.export_count,
            "export_rsp_desc": probe.export_rsp_desc,
            "export_had_result": probe.export_had_result,
            "task_status_rsp_desc": probe.task_status_rsp_desc,
        }

    def recovery_restart(
        job: Mapping[str, Any], resume_phase: str | None, timeout_seconds: float
    ) -> Mapping[str, Any]:
        from app.services.remote_running_recovery import classify_restart_exception

        try:
            prepared_job = reconstruct(job)
            task_id, task_number = remote_identity_parts(prepared_job, job.get("remote_identity"))
        except Exception as exc:
            # The exact local identity cannot be rebuilt: fail closed before
            # any remote write is even considered.
            return {
                "status": "IDENTITY_CONFLICT",
                "restarted": False,
                "diagnostic": f"restart identity could not be reconstructed: {type(exc).__name__}",
            }
        try:
            outcome = adapter.restart_running_task(
                prepared_job,
                task_id,
                task_number,
                float(timeout_seconds),
                resume_phase=resume_phase,
            )
        except Exception as exc:
            # Since Round 5 the production adapter converts its own escapes
            # into explicit boundary outcomes (PRE_WRITE_FAILED for proven
            # zero-write failures), so this branch is defense-in-depth for
            # injected runners and unforeseen escapes only: the classifier
            # keeps the resumed direction reconcile-only and can never
            # authorize a fresh second write.
            status = classify_restart_exception(exc, resume_phase)
            return {
                "status": status,
                "restarted": False,
                "observed_enabled": None,
                "diagnostic": f"restart dispatch raised {type(exc).__name__}; awaiting phase reconciliation",
            }
        return {
            "status": outcome.status.value,
            "restarted": outcome.status is RestartStatus.RESTARTED,
            "reconciled": resume_phase is not None,
            "observed_enabled": outcome.observed_enabled,
            "diagnostic": outcome.diagnostic,
            "disable_evidence": {
                "response": dict(outcome.disable_response) if outcome.disable_response else None,
                "confirmed_disabled": outcome.disable_confirmed,
            },
            "enable_evidence": {
                "response": dict(outcome.enable_response) if outcome.enable_response else None,
                "confirmed_enabled": outcome.enable_confirmed,
            },
        }

    return propose, finalize, submit, harvest, recovery_probe, recovery_restart


def batch_zhaoshang_bridge(
    adapter: CnncBatchZhaoshangAdapter,
    legacy_adapter: CnncLegacyZhaoshangAdapter,
    *,
    paths: RuntimePaths,
    business_date: str,
    priority_runner: Callable[[Any, float], BatchPriorityAttempt] | None = None,
    result_adapter: BatchSqlResultAdapter | None = None,
    materialization_probe_runner: Callable[
        [Any, float], BatchMaterializationProbe
    ]
    | None = None,
    request_resolver: Callable[[Mapping[str, Any]], CrawlRequest | None] | None = None,
    source_id_resolver: Callable[[Mapping[str, Any]], str] | None = None,
    purpose: str = "price-audit",
    monotonic: Callable[[], float] = monotonic_time.monotonic,
) -> tuple[Callable[[list[Mapping[str, Any]], float], list[CrawlPlan]], Callable[[CrawlPlan, tuple[CrawlMember, ...], float], CrawlPlan], Callable[[Mapping[str, Any], list[Mapping[str, Any]], float], Mapping[str, Any]], Callable[[list[Mapping[str, Any]], float], Mapping[int, Mapping[str, Any]]]]:
    """Wire batch create/progress plus the evidenced exact SQL result path."""
    prepared: dict[str, Any] = {}

    def resolve(row: Mapping[str, Any]) -> CrawlRequest | None:
        request = (
            request_resolver(row)
            if request_resolver is not None
            else legacy_adapter.request_from_saas_row(row, backend_version="batch-url-v1")
        )
        if request is not None and not request.url:
            return None
        return request

    def resolve_source_id(row: Mapping[str, Any]) -> str:
        if source_id_resolver is not None:
            return normalize_cell_str(source_id_resolver(row))
        return normalize_cell_str(row.get("vCommodityId") or row.get("documentId"))

    def propose(items: list[Mapping[str, Any]], _timeout: float) -> list[CrawlPlan]:
        requests: list[CrawlRequest] = []
        source_ids: list[str] = []
        members_by_identity: dict[
            tuple[tuple[str, str, str, str, str], str], CrawlMember
        ] = {}
        for item in items:
            request = resolve(item["row"])
            if request is None:
                continue
            source_id = resolve_source_id(item["row"])
            if not source_id:
                raise RuntimeConfigurationError(
                    "eligible batch crawl member has no SaaS source identity"
                )
            member = CrawlMember(
                request.mall_key, source_id, request.sku_id, request.url,
                request.backend_version, item["row"],
            )
            requests.append(request)
            source_ids.append(source_id)
            members_by_identity.setdefault((request.identity, source_id), member)
        if not requests:
            return []
        plans = []
        for job in adapter.prepare_jobs(
            business_date, requests, source_ids=source_ids, purpose=purpose
        ).jobs:
            members = tuple(
                members_by_identity[(request.identity, source_id)]
                for request, source_id in zip(
                    job.requests, job.source_ids, strict=True
                )
            )
            prepared[job.logical_job_key] = job
            plans.append(CrawlPlan(
                job.mall_key, job.logical_job_key, job.task_name,
                job.payload_sha256, payload_json=job.durable_payload(), members=members,
            ))
        return plans

    def finalize(plan: CrawlPlan, members: tuple[CrawlMember, ...], _timeout: float) -> CrawlPlan:
        if not members:
            raise RuntimeConfigurationError(
                "batch plan requires at least one durable crawl member"
            )
        proposal_index = 0
        for member in members:
            while (
                proposal_index < len(plan.members)
                and plan.members[proposal_index] != member
            ):
                proposal_index += 1
            if proposal_index >= len(plan.members):
                raise RuntimeConfigurationError(
                    "batch plan members must be an ordered subset of the proposal"
                )
            proposal_index += 1
        if tuple(plan.members) == members:
            return plan

        requests: list[CrawlRequest] = []
        source_ids: list[str] = []
        for member in members:
            request = resolve(member.row)
            source_id = resolve_source_id(member.row)
            if request is None or (
                request.mall_key,
                request.sku_id,
                request.url.strip(),
                request.backend_version,
                source_id,
            ) != (
                member.mall_key,
                member.sku_id,
                member.exact_url.strip(),
                member.backend_version,
                member.source_id,
            ):
                raise RuntimeConfigurationError(
                    "batch mapping changed for durable crawl member"
                )
            requests.append(request)
            source_ids.append(source_id)
        finalized_jobs = adapter.prepare_jobs(
            business_date, requests, source_ids=source_ids, purpose=purpose
        ).jobs
        if len(finalized_jobs) != 1:
            raise RuntimeConfigurationError(
                "batch pending subset no longer belongs to one slice"
            )
        job = finalized_jobs[0]
        prepared[job.logical_job_key] = job
        return CrawlPlan(
            job.mall_key,
            job.logical_job_key,
            job.task_name,
            job.payload_sha256,
            payload_json=job.durable_payload(),
            members=members,
        )

    def reconstruct(job: Mapping[str, Any]) -> Any:
        logical_key = str(job["logical_key"])
        cached = prepared.get(logical_key)
        if cached is not None:
            return cached
        raw = job.get("payload_json") or "{}"
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        restored = adapter.restore_job(
            payload,
            expected_logical_job_key=logical_key,
            expected_payload_sha256=str(job.get("payload_hash") or ""),
        )
        prepared[logical_key] = restored
        return restored

    def submit(job: Mapping[str, Any], _crawls: list[Mapping[str, Any]], timeout: float) -> Mapping[str, Any]:
        prepared_job = reconstruct(job)
        outcome = adapter.submit(prepared_job, timeout_seconds=max(0.001, timeout))
        if outcome.status is BatchSubmitStatus.SUBMITTED and outcome.remote_identity is not None:
            return {
                "remote_identity": outcome.remote_identity.durable_value(),
                "state": "PRIORITY_PENDING",
            }
        if outcome.status is BatchSubmitStatus.REJECTED:
            raise RuntimeConfigurationError("batch create was deterministically rejected")
        if outcome.status is BatchSubmitStatus.NOT_ATTEMPTED:
            raise RuntimeConfigurationError("batch create is not enabled")
        raise TimeoutError("batch create outcome is unknown")

    def harvest(jobs: list[Mapping[str, Any]], timeout: float) -> Mapping[int, Mapping[str, Any]]:
        results: dict[int, Mapping[str, Any]] = {}
        deadline = monotonic() + max(0.0, timeout)
        for index, job in enumerate(jobs):
            remaining = deadline - monotonic()
            if remaining <= 0:
                for deferred in jobs[index:]:
                    results[int(deferred["id"])] = {"deferred": True}
                break
            job_deadline = monotonic() + (
                remaining / max(1, len(jobs) - index)
            )

            def remaining_timeout() -> float:
                available = min(deadline, job_deadline) - monotonic()
                if available <= 0:
                    raise TimeoutError("batch harvest deadline is exhausted")
                return max(0.001, available)

            try:
                prepared_job = reconstruct(job)
            except Exception:
                results[int(job["id"])] = {
                    "manual_error": "exact batch job reconstruction is unavailable"
                }
                continue
            try:
                if str(job.get("state")) == "SUBMIT_UNKNOWN":
                    reconciliation = adapter.reconcile_unknown(
                        prepared_job, timeout_seconds=remaining_timeout()
                    )
                    if reconciliation.status is BatchReconcileStatus.MANUAL:
                        results[int(job["id"])] = {"manual_error": reconciliation.diagnostic or "batch identity conflict"}
                    elif reconciliation.status is BatchReconcileStatus.FOUND and reconciliation.remote_identity is not None:
                        results[int(job["id"])] = {
                            "state": "PRIORITY_PENDING",
                            "remote_identity": reconciliation.remote_identity.durable_value(),
                        }
                    elif reconciliation.status is BatchReconcileStatus.NOT_FOUND:
                        results[int(job["id"])] = {
                            "state": "SUBMIT_UNKNOWN",
                            "reconcile_status": "NOT_FOUND",
                            "reconcile_diagnostic": reconciliation.diagnostic or "",
                        }
                    continue
                if str(job.get("state")) == "PRIORITY_PENDING":
                    identity = str(job.get("remote_identity") or "").strip()
                    try:
                        adapter.validate_remote_identity(prepared_job, identity)
                        if str(job.get("mall_key") or "") != prepared_job.mall_key:
                            raise ValueError("persisted mall differs from durable batch job")
                    except ValueError:
                        results[int(job["id"])] = {
                            "manual_error": "batch priority identity conflict"
                        }
                        continue
                    if priority_runner is None:
                        results[int(job["id"])] = {
                            "state": "PRIORITY_PENDING",
                            "remote_identity": identity,
                            "retryable_error": "batch priority writes are disabled",
                            "result": {
                                "state": "PRIORITY_PENDING",
                                "priority_status": "PENDING",
                                "diagnostic": "batch priority writes are disabled",
                            },
                        }
                        continue
                    priority = priority_runner(
                        prepared_job, remaining_timeout()
                    )
                    if priority.status is BatchPriorityAttemptStatus.CONFLICT:
                        results[int(job["id"])] = {
                            "manual_error": priority.diagnostic
                            or "batch priority identity conflict"
                        }
                    elif priority.status is BatchPriorityAttemptStatus.PENDING:
                        results[int(job["id"])] = {
                            "state": "PRIORITY_PENDING",
                            "remote_identity": identity,
                            "retryable_error": priority.diagnostic
                            or "batch rows are not fully materialized",
                            "result": {
                                "state": "PRIORITY_PENDING",
                                "priority_status": "PENDING",
                                "materialization_status": (
                                    priority.materialization.value
                                    if priority.materialization is not None
                                    else None
                                ),
                                "diagnostic": priority.diagnostic,
                            },
                        }
                    elif priority.priority is None:
                        results[int(job["id"])] = {
                            "manual_error": "batch priority confirmation has no readback evidence"
                        }
                    else:
                        evidence = priority.priority
                        results[int(job["id"])] = {
                            "state": "SUBMITTED",
                            "remote_identity": identity,
                            "result": {
                                "priority_status": evidence.status.value,
                                "schema": evidence.schema,
                                "expected_count": evidence.expected_count,
                                "affected_rows": evidence.affected_rows,
                                "priority_updated": evidence.priority_updated,
                                "already_highest": evidence.already_highest,
                                "already_completed": evidence.already_completed,
                                "already_failed": evidence.already_failed,
                            },
                        }
                    continue
                identity = str(
                    job.get("remote_identity")
                    or prepared_job.remote_identity.durable_value()
                )
                try:
                    adapter.validate_remote_identity(prepared_job, identity)
                    if str(job.get("mall_key") or "") != prepared_job.mall_key:
                        raise ValueError("persisted mall differs from durable batch job")
                except ValueError:
                    results[int(job["id"])] = {
                        "manual_error": "batch result identity conflict"
                    }
                    continue
                _response, progress = adapter.query_progress(
                    prepared_job, timeout_seconds=remaining_timeout()
                )
                if result_adapter is None or materialization_probe_runner is None:
                    if progress.status is BatchProgressStatus.FAILED:
                        results[int(job["id"])] = {
                            "manual_error": "batch task reported failed subtasks"
                        }
                    elif progress.status is BatchProgressStatus.COMPLETED:
                        results[int(job["id"])] = {
                            "manual_error": "batch SQL result reader is not configured"
                        }
                    else:
                        results[int(job["id"])] = {
                            "state": "RUNNING",
                            "remote_identity": identity,
                        }
                    continue
                try:
                    crawl_by_execute = _exact_batch_crawl_mapping(
                        prepared_job, tuple(job.get("crawl_work") or ())
                    )
                except RuntimeConfigurationError as exc:
                    results[int(job["id"])] = {"manual_error": str(exc)}
                    continue
                probe = materialization_probe_runner(
                    prepared_job, remaining_timeout()
                )
                if probe.status in {
                    BatchMaterializationStatus.IDENTITY_CONFLICT,
                    BatchMaterializationStatus.SCHEMA_CONFLICT,
                }:
                    results[int(job["id"])] = {
                        "manual_error": probe.diagnostic
                        or "batch materialization identity conflict"
                    }
                    continue
                if probe.status is not BatchMaterializationStatus.MATERIALIZED:
                    results[int(job["id"])] = {
                        "state": "RUNNING",
                        "remote_identity": identity,
                    }
                    continue
                rows_by_execute: dict[str, Mapping[str, Any]] = {}
                for row in probe.rows:
                    execute_number = normalize_cell_str(row.get("exec_num"))
                    if not execute_number or execute_number in rows_by_execute:
                        rows_by_execute = {}
                        break
                    rows_by_execute[execute_number] = row
                if set(rows_by_execute) != set(prepared_job.execute_numbers):
                    results[int(job["id"])] = {
                        "manual_error": "batch materialization rows are not an exact executeNumber set"
                    }
                    continue
                targets = tuple(
                    BatchResultTarget(
                        execute_number=execute_number,
                        expected_sku=request.sku_id,
                        expected_url=request.url,
                        evidence=BatchResultEvidence(
                            task_status=normalize_cell_str(
                                rows_by_execute[execute_number].get("task_status")
                            ),
                            query_progress=progress.status.value,
                        ),
                        purpose=prepared_job.purpose,
                    )
                    for request, execute_number in zip(
                        prepared_job.requests,
                        prepared_job.execute_numbers,
                        strict=True,
                    )
                )
                sql_results = result_adapter.fetch_batch_results(
                    prepared_job.mall_key,
                    targets,
                    timeout_seconds=remaining_timeout(),
                )
                if (
                    len(sql_results) != len(targets)
                    or {value.execute_number for value in sql_results}
                    != set(prepared_job.execute_numbers)
                ):
                    results[int(job["id"])] = {
                        "manual_error": "batch SQL results are not an exact executeNumber set"
                    }
                    continue
                crawl_results: dict[int, Mapping[str, Any]] = {}
                pending = False
                manual = False
                for sql_result in sql_results:
                    crawl_id = int(crawl_by_execute[sql_result.execute_number]["id"])
                    if sql_result.status is BatchResultStatus.FOUND:
                        crawl_results[crawl_id] = batch_sql_result_to_crawl_result(
                            prepared_job.mall_key,
                            sql_result,
                            purpose=prepared_job.purpose,
                        )
                    elif sql_result.status is BatchResultStatus.NOT_YET_VISIBLE:
                        pending = True
                    elif sql_result.status is BatchResultStatus.CRAWLER_FAILED:
                        crawl_results[crawl_id] = {
                            "terminal": True,
                            "error": sql_result.diagnostic
                            or "crawler task failed before an exact SQL result was found",
                        }
                    elif sql_result.status is BatchResultStatus.TERMINAL_NO_RESULT:
                        crawl_results[crawl_id] = {
                            "no_result": True,
                            "reason": sql_result.diagnostic
                            or "terminal no-result evidence was supplied",
                        }
                    else:
                        manual = True
                        fallback = (
                            "task_status 02 with zero rows requires spider-log or equivalent terminal evidence"
                            if sql_result.status
                            is BatchResultStatus.EVIDENCE_INCOMPLETE
                            else "batch SQL result evidence is incomplete"
                        )
                        crawl_results[crawl_id] = {
                            "manual": True,
                            "error": sql_result.diagnostic
                            or fallback,
                        }
                state = "EXPORTED" if manual else "RUNNING" if pending else "COMPLETED"
                results[int(job["id"])] = {
                    "state": state,
                    "remote_identity": identity,
                    "crawl_results": crawl_results,
                }
            except Exception as exc:
                retryable: dict[str, Any] = {
                    "state": str(job.get("state") or "RUNNING"),
                    "retryable_error": (
                        "batch harvest read failed: " + type(exc).__name__
                    ),
                }
                identity = str(job.get("remote_identity") or "").strip()
                if identity:
                    retryable["remote_identity"] = identity
                results[int(job["id"])] = retryable
        return results

    return propose, finalize, submit, harvest


def _batch_sql_runtime_components(
    *,
    monotonic: Callable[[], float] = monotonic_time.monotonic,
    env_path: Path | str | None = None,
) -> tuple[
    BatchSqlResultAdapter,
    Callable[[Any, float], BatchMaterializationProbe],
]:
    env_path = (
        Path(env_path)
        if env_path is not None
        else PROJECT_DATA_ROOT / "ZHAOSHANG_DB.env"
    )

    def connect_for_results() -> Any:
        return connect_batch_priority_from_env_file(
            env_path, timeout_seconds=20.0
        )

    def connect_for_results_with_timeout(timeout_seconds: float) -> Any:
        return connect_batch_priority_from_env_file(
            env_path, timeout_seconds=max(0.001, timeout_seconds)
        )

    def materialization_probe_runner(
        prepared_job: Any, timeout_seconds: float
    ) -> BatchMaterializationProbe:
        deadline = monotonic() + max(0.0, timeout_seconds)
        connection = connect_batch_priority_from_env_file(
            env_path, timeout_seconds=max(0.001, timeout_seconds)
        )
        try:
            return probe_batch_materialization(
                connection,
                BatchMaterializationTarget(
                    execute_numbers=prepared_job.execute_numbers,
                    expected_mall=prepared_job.mall_key,
                ),
                deadline=deadline,
                monotonic=monotonic,
            )
        finally:
            try:
                if monotonic() < deadline:
                    connection.rollback()
            finally:
                connection.close()

    return (
        BatchSqlResultAdapter(
            connect_for_results,
            timed_connection_factory=connect_for_results_with_timeout,
            monotonic=monotonic,
        ),
        materialization_probe_runner,
    )


def build_default_dependencies(
    *,
    enable_zhaoshang: bool = False,
    zhaoshang_backend: str = "legacy",
    paths: RuntimePaths | None = None,
    business_date: str | None = None,
) -> TickDependencies:
    """Build production read-only discovery; unsafe write adapters stay disabled.

    Detail and model use their existing bounded read-only adapters. Zhaoshang
    is wired only under the explicit CLI flag; ordinary ticks have no planner
    or submitter and therefore cannot create a remote task.
    """
    snapshot_adapter = CnncSaasSnapshotAdapter(
        PROJECT_DATA_ROOT / "latest_saas_sameAudit_queryAll.curl",
        PROJECT_DATA_ROOT / "latest_cnnc_query.curl",
        auth_manager=AuthManager(),
        cnnc_provider=shared_cnnc_provider(),
    )
    detail_template = load_curl_command(PROJECT_DATA_ROOT / "detail_request.curl")
    if zhaoshang_backend not in {"legacy", "batch"}:
        raise RuntimeConfigurationError("Zhaoshang backend must be legacy or batch")
    legacy_adapter: CnncLegacyZhaoshangAdapter | None = None
    resolver: Callable[[Mapping[str, Any]], CrawlRequest | None] | None = None
    if paths is not None and business_date is not None:
        legacy_adapter = CnncLegacyZhaoshangAdapter()

        def resolver(row: Mapping[str, Any]) -> CrawlRequest | None:
            legacy_request = legacy_adapter.request_from_saas_row(
                row, backend_version="legacy-v1"
            )
            if legacy_request is None:
                return None
            backend_version = _zhaoshang_backend_version_for_mall(
                legacy_request.mall_key,
                url=legacy_request.url,
                selected_backend=zhaoshang_backend,
            )
            if backend_version == legacy_request.backend_version:
                return legacy_request
            return CrawlRequest(
                mall_key=legacy_request.mall_key,
                mall_source_id=legacy_request.mall_source_id,
                mall_name=legacy_request.mall_name,
                sku_id=legacy_request.sku_id,
                url=legacy_request.url,
                backend_version=backend_version,
            )
    dependencies = TickDependencies(
        discover=snapshot_discoverer(snapshot_adapter, crawl_request_resolver=resolver),
        detail=_detail_worker(
            detail_template,
            detail_lock_path=paths.detail_lock_path if paths is not None else None,
            cnnc_provider=shared_cnnc_provider(),
        ),
        model=_model_worker,
        model_batch=_model_batch_worker,
    )
    if paths is not None and business_date is not None:
        assert legacy_adapter is not None
        (
            legacy_planner,
            legacy_finalizer,
            legacy_submitter,
            legacy_harvester,
            recovery_probe,
            recovery_restart,
        ) = legacy_zhaoshang_bridge(
            legacy_adapter, paths=paths, business_date=business_date
        )
        batch_priority_runner = None
        if enable_zhaoshang:
            def batch_priority_runner(
                prepared_job: Any, timeout_seconds: float
            ) -> BatchPriorityAttempt:
                target = BatchMaterializationTarget(
                    execute_numbers=prepared_job.execute_numbers,
                    expected_mall=prepared_job.mall_key,
                )
                return ensure_batch_priority(
                    lambda remaining: connect_batch_priority_from_env_file(
                        PROJECT_DATA_ROOT / "ZHAOSHANG_DB.env",
                        timeout_seconds=remaining,
                    ),
                    target,
                    timeout_seconds=timeout_seconds,
                )

        result_adapter, probe_runner = _batch_sql_runtime_components()
        (
            batch_planner,
            batch_finalizer,
            batch_submitter,
            batch_harvester,
        ) = batch_zhaoshang_bridge(
            CnncBatchZhaoshangAdapter(
                write_enabled=enable_zhaoshang and zhaoshang_backend == "batch"
            ),
            legacy_adapter,
            paths=paths,
            business_date=business_date,
            priority_runner=batch_priority_runner,
            result_adapter=result_adapter,
            materialization_probe_runner=probe_runner,
        )
        if zhaoshang_backend == "batch":
            planner, finalizer, submitter = _route_zhaoshang_creation(
                legacy_planner,
                legacy_finalizer,
                legacy_submitter,
                batch_planner,
                batch_finalizer,
                batch_submitter,
            )
        else:
            planner, finalizer, submitter = (
                legacy_planner,
                legacy_finalizer,
                legacy_submitter,
            )
        harvester = _route_zhaoshang_harvest(
            legacy_harvester, batch_harvester
        )
        # Legacy RUNNING restart remains isolated from batch progress recovery.
        dependencies.remote_recovery_probe = recovery_probe
        if enable_zhaoshang:
            dependencies.remote_recovery_restart = recovery_restart
        dependencies.remote_recovery_accepts = (
            lambda job: _remote_job_backend(job) == "legacy"
        )
        # Read-only wait/export and exact unknown-submit reconciliation remain
        # available on ordinary ticks. Only planner+submit are write-capable.
        dependencies.harvest = harvester
        def job_disposition(job: Mapping[str, Any]) -> str:
            return _remote_job_disposition(
                job,
                selected_backend=zhaoshang_backend,
                batch_priority_enabled=enable_zhaoshang,
            )

        dependencies.remote_job_disposition = job_disposition
        dependencies.harvest_accepts = lambda job: job_disposition(job) == "accept"
        # Issue #9 sidecar: best-effort enqueue of discovered documents into
        # the prewarm inbox. The tick hook catches every exception and only
        # counts it; drain-only dependencies stay without it on purpose.
        dependencies.prewarm_intake = _prewarm_intake_worker(
            paths.run_dir.parent / "customer_prewarm.sqlite3"
        )
        dependencies.local_fallback = lambda claims, tick_id, timeout_seconds: execute_pinned_crawler(
            claims,
            run_dir=paths.run_dir,
            tick_id=tick_id,
            timeout_seconds=timeout_seconds,
        )
    if enable_zhaoshang:
        if paths is None or business_date is None:
            raise RuntimeConfigurationError("Zhaoshang runtime paths and business date are required")
        dependencies.plan_crawl = planner
        dependencies.finalize_crawl_plan = finalizer
        dependencies.submit_crawl = submitter
        dependencies.harvest = harvester
    return dependencies


def build_drain_dependencies(
    *,
    zhaoshang_backend: str,
    paths: RuntimePaths,
    business_date: str,
) -> TickDependencies:
    """Compose only the existing-remote lifecycle; no create surface exists."""
    if zhaoshang_backend not in {"legacy", "batch"}:
        raise RuntimeConfigurationError("Zhaoshang backend must be legacy or batch")
    adapter = CnncLegacyZhaoshangAdapter()
    _planner, _finalizer, _submitter, legacy_harvester, _probe, _restart = legacy_zhaoshang_bridge(
        adapter,
        paths=paths,
        business_date=business_date,
    )
    result_adapter, probe_runner = _batch_sql_runtime_components()

    def batch_priority_runner(
        prepared_job: Any, timeout_seconds: float
    ) -> BatchPriorityAttempt:
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

    _batch_planner, _batch_finalizer, _batch_submitter, batch_harvester = batch_zhaoshang_bridge(
        CnncBatchZhaoshangAdapter(write_enabled=False),
        adapter,
        paths=paths,
        business_date=business_date,
        priority_runner=batch_priority_runner,
        result_adapter=result_adapter,
        materialization_probe_runner=probe_runner,
    )
    harvester = _route_zhaoshang_harvest(
        legacy_harvester, batch_harvester
    )

    def discover_forbidden(_timeout: float) -> DiscoveredState:
        raise RuntimeConfigurationError("drain-only cannot discover new business work")

    def disposition(job: Mapping[str, Any]) -> str:
        return _remote_job_disposition(
            job,
            selected_backend=zhaoshang_backend,
            batch_priority_enabled=True,
        )
    return TickDependencies(
        discover=discover_forbidden,
        harvest=harvester,
        harvest_accepts=lambda job: disposition(job) == "accept",
        remote_job_disposition=disposition,
    )


def run_runtime_command(
    command: str,
    *,
    business_date: str | None = None,
    runtime_root: Path | str = DEFAULT_RUNTIME_ROOT,
    runtime_budget_seconds: float = 720,
    drain_max_iterations: int = 1000,
    enable_zhaoshang: bool = False,
    zhaoshang_backend: str = "legacy",
    dependencies: TickDependencies | None = None,
    output_path: Path | str | None = None,
    now: datetime | None = None,
    materialize: Callable[..., FinalizePublication] = materialize_frozen_daily_run,
    monotonic: Callable[[], float] = monotonic_time.monotonic,
    invocation: str = "manual",
    source_stability_seconds: float | None = None,
    closeout_observation_interval_seconds: float = 300.0,
    closeout_remote_sweep_interval_seconds: float = 600.0,
    closeout_final_reserve_seconds: float = 120.0,
    closeout_sleep: Callable[[float], None] = monotonic_time.sleep,
) -> RuntimeResult:
    if command not in {"tick", "finalize", "drain-only", "harvest-only", "closeout"}:
        raise ValueError("runtime command must be tick, drain-only, harvest-only, finalize, or closeout")
    if command == "drain-only" and enable_zhaoshang:
        raise ValueError("drain-only forbids the remote-create capability")
    if command == "harvest-only" and enable_zhaoshang:
        raise ValueError("harvest-only forbids the remote-create capability")
    if invocation not in {"manual", "scheduler"}:
        raise ValueError("invocation must be manual or scheduler")
    if source_stability_seconds is not None and source_stability_seconds < 0:
        raise ValueError("source stability window must be zero or positive")
    day = business_date or business_date_in_shanghai(now)
    paths = runtime_paths(day, runtime_root)
    started_at = now or datetime.now(UTC)
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("runtime clock must return an explicit-timezone datetime")
    production_lock = ProcessLock(paths.lock_path)
    owner = {
        "business_date": day,
        "pid": os.getpid(),
        "command": command,
        "started_at": started_at.astimezone(SHANGHAI).isoformat(timespec="seconds"),
        "invocation": invocation,
    }
    try:
        production_lock.acquire(owner=owner)
    except LeaseUnavailable:
        conflict = ProcessLock.inspect(paths.lock_path)
        conflict.update({
            "active": True,
            "business_date": day,
            "requested": command,
            "lock_identity": production_lock.identity,
            "requested_invocation": invocation,
        })
        return RuntimeResult(
            TickSummary("ALREADY_RUNNING", (), {}, {}, None),
            lock_conflict=conflict,
        )
    try:
        return _run_runtime_command_locked(
            command,
            day=day,
            paths=paths,
            process_lock=production_lock,
            runtime_budget_seconds=runtime_budget_seconds,
            drain_max_iterations=drain_max_iterations,
            enable_zhaoshang=enable_zhaoshang,
            zhaoshang_backend=zhaoshang_backend,
            dependencies=dependencies,
            output_path=output_path,
            materialize=materialize,
            monotonic=monotonic,
            source_stability_seconds=source_stability_seconds,
            closeout_config=CloseoutConfig(
                wall_budget_seconds=runtime_budget_seconds,
                source_observation_interval_seconds=closeout_observation_interval_seconds,
                remote_sweep_interval_seconds=closeout_remote_sweep_interval_seconds,
                final_materialize_reserve_seconds=closeout_final_reserve_seconds,
            ),
            closeout_sleep=closeout_sleep,
        )
    finally:
        production_lock.release()


def _ensure_runtime_daily_curl_fresh(deps, day: str, *, deadline_monotonic=None) -> None:
    """每日 cURL freshness guard（2026-09-10）：tick/finalize/closeout（真正
    抓 CNNC customer 侧快照的命令）在进入引擎前确认为当日 cURL 已同步。
    判定只读 results 工件、与 closeout/finalize 状态解耦；任何状态都只打一
    行日志并放行——busy/SSO/部分失败交给既有 lazy refresh 401 兜底语义，
    guard 绝不 raise、绝不自动弹登录。经模块属性引用（而非 from-import 函
    数名），测试可 monkeypatch 注入。closeout 与 tick/finalize 在命令分发前
    统一执行一次，其内部多个 incremental pass 因此不重复触发。
    """
    try:
        guard_status = cnnc_auth_refresh.ensure_daily_curl_fresh(
            business_date=day,
            provider=getattr(deps, "cnnc_provider", None),
            deadline_monotonic=deadline_monotonic,
        )
    except Exception:  # noqa: BLE001 — guard 绝不影响命令执行
        guard_status = "DAILY_CURL_GUARD_ERROR"
    print("daily-curl-guard: %s" % guard_status)


def _run_runtime_command_locked(
    command: str,
    *,
    day: str,
    paths: RuntimePaths,
    process_lock: ProcessLock,
    runtime_budget_seconds: float,
    drain_max_iterations: int,
    enable_zhaoshang: bool,
    zhaoshang_backend: str,
    dependencies: TickDependencies | None,
    output_path: Path | str | None,
    materialize: Callable[..., FinalizePublication],
    monotonic: Callable[[], float],
    source_stability_seconds: float | None = None,
    closeout_config: CloseoutConfig | None = None,
    closeout_sleep: Callable[[float], None] = monotonic_time.sleep,
) -> RuntimeResult:
    command_started = monotonic()
    if command in {"drain-only", "harvest-only"} and not paths.state_path.is_file():
        outcome = "DRAIN_NO_EXISTING_RUN" if command == "drain-only" else "HARVEST_NO_EXISTING_RUN"
        return RuntimeResult(TickSummary(outcome, (), {}, {}, None))
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    _ensure_batch_state(
        paths,
        enable_zhaoshang=enable_zhaoshang,
        zhaoshang_backend=zhaoshang_backend,
    )
    store = DailyAuditStore(paths.state_path)
    existing_run = store.get_run(day)
    if existing_run is not None and existing_run["state"] == "FINALIZED":
        result = RuntimeResult(
            TickSummary(
                "ALREADY_FINALIZED",
                ("noop:already_finalized",),
                {},
                {},
                existing_run["frozen_snapshot_id"],
            )
        )
        _record_batch_result(
            paths,
            result,
            command=command,
            enable_zhaoshang=enable_zhaoshang,
            zhaoshang_backend=zhaoshang_backend,
            output_path=Path(output_path).resolve() if output_path else paths.final_output_path,
        )
        return result
    if dependencies is not None:
        deps = dependencies
    elif command in {"drain-only", "harvest-only"}:
        deps = build_drain_dependencies(
            zhaoshang_backend=zhaoshang_backend,
            paths=paths,
            business_date=day,
        )
    else:
        deps = build_default_dependencies(
            enable_zhaoshang=enable_zhaoshang,
            zhaoshang_backend=zhaoshang_backend,
            paths=paths,
            business_date=day,
        )
    # 每日 cURL freshness guard：tick/finalize/closeout 统一在命令分发前执行
    # 一次（deps 构建后紧邻处）；closeout 内部的多个 incremental pass 由此不
    # 再重复触发。drain-only/harvest-only 只收既有远端任务，语义保持不触发。
    if command in {"tick", "finalize", "closeout"}:
        _ensure_runtime_daily_curl_fresh(
            deps, day, deadline_monotonic=command_started + runtime_budget_seconds,
        )
    if deps.submit_crawl is not None and deps.submit_gate is None:
        # Durable remote-stall breaker: while recent SQLite evidence shows the
        # remote is not materializing subtasks, this tick must not create any
        # new remote task. The gate is recomputed from persisted evidence on
        # every process, so a scheduler restart cannot forget an open breaker.
        deps.submit_gate = lambda: remote_submit_pause_reason(store, day)
    lifecycle_only = command in {"drain-only", "harvest-only"}
    config = TickConfig(
        business_date=day,
        runtime_budget_seconds=runtime_budget_seconds,
        cutoff_at=cutoff_for_business_date(day),
        model_name="unused-by-existing-remote-commands" if lifecycle_only else _configured_model_name(),
        model_prompt_version=TITLE_MATCH_PROMPT_VERSION,
        model_schema_version=TITLE_MATCH_SCHEMA_VERSION,
        model_batch_size=10 if lifecycle_only else _runtime_model_batch_size(),
        model_max_concurrency=1 if lifecycle_only else _runtime_model_max_concurrency(),
        **(
            {"source_stability_seconds": source_stability_seconds}
            if source_stability_seconds is not None
            else {}
        ),
    )
    if command == "closeout":
        assert closeout_config is not None
        summary, publication, blocked, blocked_error_type = run_closeout(
            store,
            process_lock,
            config,
            deps,
            closeout_config,
            output_path=Path(output_path) if output_path else paths.final_output_path,
            materialize=materialize,
            monotonic=monotonic,
            sleep=closeout_sleep,
        )
        result = RuntimeResult(
            summary,
            publication=publication,
            publication_blocked=blocked,
            publication_error_type=blocked_error_type,
        )
        _record_batch_result(
            paths,
            result,
            command=command,
            enable_zhaoshang=enable_zhaoshang,
            zhaoshang_backend=zhaoshang_backend,
            output_path=Path(output_path).resolve() if output_path else paths.final_output_path,
        )
        return result
    if command == "drain-only":
        summary = run_existing_remote_drain(
            store,
            process_lock,
            config,
            deps,
            max_iterations=drain_max_iterations,
        )
    elif command == "harvest-only":
        summary = run_harvest_only(store, process_lock, config, deps)
    else:
        summary = run_incremental_tick(
            store,
            process_lock,
            config,
            deps,
            finalize=command == "finalize",
        )
    publication_candidate = (
        command == "finalize"
        or (command == "drain-only" and summary.outcome == "DRAINED")
    )
    if (
        publication_candidate
        and summary.frozen_snapshot_id is not None
        and summary.outcome not in {
            "FAILED",
            "ALREADY_RUNNING",
            "BUDGET_EXHAUSTED",
            "FINALIZE_SNAPSHOT_NOT_READY",
            "INCOMPLETE",
        }
        and runtime_budget_seconds - max(0.0, monotonic() - command_started)
        < FINALIZE_MATERIALIZE_RESERVE_SECONDS
    ):
        summary = TickSummary(
            "BUDGET_EXHAUSTED",
            (*summary.phase_trace, "publication_deferred"),
            summary.counters,
            summary.backlog,
            summary.frozen_snapshot_id,
            summary.error,
        )
    # The backlog intentionally includes pre-cutoff work.  A cutoff snapshot
    # may supersede that work, so only the frozen scope's readiness can decide
    # publication.  The materializer makes that decision and is idempotent.
    if (
        not publication_candidate
        or summary.frozen_snapshot_id is None
        or summary.outcome in {
            "FAILED",
            "ALREADY_RUNNING",
            "BUDGET_EXHAUSTED",
            "FINALIZE_SNAPSHOT_NOT_READY",
            "INCOMPLETE",
        }
    ):
        result = RuntimeResult(summary)
        _record_batch_result(
            paths,
            result,
            command=command,
            enable_zhaoshang=enable_zhaoshang,
            zhaoshang_backend=zhaoshang_backend,
            output_path=Path(output_path).resolve() if output_path else paths.final_output_path,
        )
        return result
    try:
        publication = materialize(
            store,
            process_lock,
            day,
            Path(output_path) if output_path else paths.final_output_path,
        )
    except FinalizeAlreadyRunning:
        deferred = TickSummary(
            "ALREADY_RUNNING",
            (*summary.phase_trace, "publication_overlap"),
            summary.counters,
            summary.backlog,
            summary.frozen_snapshot_id,
            summary.error,
        )
        return RuntimeResult(deferred)
    except FinalizeBlocked as exc:
        if _is_recoverable_finalize_wait(exc.readiness):
            result = RuntimeResult(summary)
            _record_batch_result(
                paths,
                result,
                command=command,
                enable_zhaoshang=enable_zhaoshang,
                zhaoshang_backend=zhaoshang_backend,
                output_path=Path(output_path).resolve() if output_path else paths.final_output_path,
            )
            return result
        result = RuntimeResult(
            summary,
            publication_blocked=str(exc),
            publication_error_type=type(exc).__name__,
        )
        _record_batch_result(
            paths,
            result,
            command=command,
            enable_zhaoshang=enable_zhaoshang,
            zhaoshang_backend=zhaoshang_backend,
            output_path=Path(output_path).resolve() if output_path else paths.final_output_path,
        )
        return result
    except (FrozenSnapshotConflict, ArtifactConflictError, MaterializationValidationError) as exc:
        result = RuntimeResult(
            summary,
            publication_blocked=str(exc),
            publication_error_type=type(exc).__name__,
        )
        _record_batch_result(
            paths,
            result,
            command=command,
            enable_zhaoshang=enable_zhaoshang,
            zhaoshang_backend=zhaoshang_backend,
            output_path=Path(output_path).resolve() if output_path else paths.final_output_path,
        )
        return result
    result = RuntimeResult(summary, publication=publication)
    _record_batch_result(
        paths,
        result,
        command=command,
        enable_zhaoshang=enable_zhaoshang,
        zhaoshang_backend=zhaoshang_backend,
        output_path=Path(output_path).resolve() if output_path else paths.final_output_path,
    )
    return result


def read_runtime_status(
    *, business_date: str,
    runtime_root: Path | str = DEFAULT_RUNTIME_ROOT,
) -> dict[str, Any]:
    """Read status without creating a directory, database, lock, or network call."""
    paths = runtime_paths(business_date, runtime_root)
    if not paths.state_path.is_file():
        return {"business_date": business_date, "state": "MISSING", "state_path": str(paths.state_path)}
    uri = f"file:{paths.state_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT business_date, state, frozen_snapshot_id, final_output_path, final_manifest_hash, metrics_json FROM daily_runs WHERE business_date=?",
            (business_date,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return {"business_date": business_date, "state": "MISSING", "state_path": str(paths.state_path)}
    result = dict(row)
    result["metrics"] = json.loads(result.pop("metrics_json") or "{}")
    result["state_path"] = str(paths.state_path)
    return result
