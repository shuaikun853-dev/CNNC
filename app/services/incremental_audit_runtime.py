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
import time as monotonic_time
import time as time_module
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from app.services.cnnc_saas_snapshot_adapter import CnncSaasSnapshotAdapter
from app.services.batch_zhaoshang_adapter import (
    BatchProgressStatus,
    BatchReconcileStatus,
    BatchSubmitStatus,
    CnncBatchZhaoshangAdapter,
)
from app.services.daily_audit_store import (
    DailyAuditStore,
    FrozenSnapshotConflict,
    LeaseUnavailable,
    ProcessLock,
)
from app.services.incremental_audit_materializer import (
    ArtifactConflictError,
    MaterializationValidationError,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    HarvestStatus,
    ReconcileStatus,
    SubmitStatus,
)
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
    TickConfig,
    TickDependencies,
    TickSummary,
    run_incremental_tick,
)
from app.workflows.incremental_price_audit_finalize import (
    FinalizeAlreadyRunning,
    FinalizeBlocked,
    FinalizePublication,
    FinalizeReadiness,
    materialize_frozen_daily_run,
)
from scripts.audit_saas_excel_file import call_detail_api
from scripts.auth_manager import AuthManager
from scripts.curl_utils import load_curl_command
from app.services.title_match_model_adapter import (
    TITLE_MATCH_PROMPT_VERSION,
    TITLE_MATCH_SCHEMA_VERSION,
    TitleMatchModelRequest,
    call_title_match_model,
    call_title_match_model_batch,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_RUNTIME_ROOT = Path("runtime") / "incremental_price_audit"
_MODEL_CONFIGURATION_KEYS = (
    "TITLE_MATCH_MODEL_API_KEY",
    "TITLE_MATCH_MODEL_BASE_URL",
    "TITLE_MATCH_MODEL_NAME",
)
_BATCH_STATE_START = "<!-- incremental-runtime-state:start -->"
_BATCH_STATE_END = "<!-- incremental-runtime-state:end -->"
FINALIZE_MATERIALIZE_RESERVE_SECONDS = 60.0


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


def business_date_in_shanghai(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("runtime clock must return an explicit-timezone datetime")
    return current.astimezone(SHANGHAI).date().isoformat()


def cutoff_for_business_date(business_date: str) -> datetime:
    day = datetime.fromisoformat(business_date).date()
    return datetime.combine(day, time(18, 0), tzinfo=SHANGHAI).astimezone(UTC)


def runtime_paths(business_date: str, root: Path | str = DEFAULT_RUNTIME_ROOT) -> RuntimePaths:
    root_path = Path(root).resolve()
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
    if result.publication_blocked:
        return "BLOCKED", result.publication_blocked, "none", "resolve blocker and rerun finalize"
    if outcome == "MANUAL_REVIEW_REQUIRED":
        return "WAITING_USER", result.tick.error or outcome, "none", "review durable manual items, then rerun the finite command"
    if outcome in {"FAILED", "DISCOVERY_INCOMPLETE", "INCOMPLETE"}:
        return "BLOCKED", result.tick.error or outcome, "none", "resolve blocker and rerun the finite command"
    if command == "finalize" and outcome in {"FINALIZE_NOT_READY", "FINALIZE_SNAPSHOT_NOT_READY"}:
        return "ACTIVE", "none", "none", "rerun finalize at or after the cutoff snapshot is available"
    return "ACTIVE", "none", "none", "run the next finite tick or finalize"


def _is_recoverable_finalize_wait(readiness: FinalizeReadiness | None) -> bool:
    if readiness is None:
        return False
    has_pending = any((readiness.detail_pending, readiness.model_pending, readiness.crawl_pending))
    has_actionable_blocker = any((
        readiness.model_terminal,
        readiness.crawl_terminal,
        readiness.crawl_unplanned,
        readiness.crawl_invalid_price,
    ))
    return has_pending and not has_actionable_blocker


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
    try:
        with _batch_state_update_lock(path):
            existing = path.read_text(encoding="utf-8")
            updated = _replace_batch_state_block(existing, managed_block)
            if not updated.endswith("\n"):
                updated += "\n"
            updated += event_line
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{time_module.time_ns()}.tmp")
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(updated)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    except TimeoutError:
        # SQLite and remote-job checkpoints are authoritative. Metadata lock
        # contention must not turn an already checkpointed command into a
        # false production failure; the next tick can refresh the handoff file.
        return


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
        snapshot = adapter.fetch(source_label="incremental-runtime", max_elapsed_seconds=timeout_seconds)
        customer = _customer_index(snapshot.customer_rows)
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
        )

    return discover


def _detail_worker(
    template: Mapping[str, Any],
    *,
    detail_lock_path: Path | str | None = None,
    request_interval_seconds: float = 0.5,
    sleep: Callable[[float], None] = time_module.sleep,
    monotonic: Callable[[], float] = time_module.monotonic,
    lock_factory: Callable[[Path | str], ProcessLock] = ProcessLock,
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
            return {
                "customerPrice": None,
                "customerPriceStatus": "LOOKUP_FAILED",
                "customerPriceSource": "",
                "customerPriceWarning": "customer export did not contain this documentId",
                "customerPriceEvidence": "",
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
        detail_lock = lock_factory(detail_lock_path) if detail_lock_path is not None else None
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
            response = call_detail_api(
                dict(template),
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
        if not response.get("ok"):
            raise RuntimeError("detail request failed")
        return extract_customer_price_from_detail(response.get("payload"))

    return detail


def _read_model_configuration(path: Path = Path("data/model.env")) -> dict[str, str]:
    values = {
        key: os.environ[key].strip()
        for key in _MODEL_CONFIGURATION_KEYS
        if os.environ.get(key, "").strip()
    }
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            parsed_value = value.strip().strip('"').strip("'")
            if key in _MODEL_CONFIGURATION_KEYS and key not in values and parsed_value:
                values[key] = parsed_value
    return values


def _load_model_configuration(path: Path = Path("data/model.env")) -> dict[str, str]:
    values = _read_model_configuration(path)
    if any(not values.get(key, "").strip() for key in _MODEL_CONFIGURATION_KEYS):
        raise RuntimeConfigurationError("title-match model configuration is incomplete")
    return values


def _configured_model_name(path: Path = Path("data/model.env")) -> str:
    model_name = _read_model_configuration(path).get("TITLE_MATCH_MODEL_NAME", "").strip()
    if not model_name:
        raise RuntimeConfigurationError("TITLE_MATCH_MODEL_NAME is required for the model fingerprint")
    return model_name


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
    with _temporary_model_environment(configuration):
        results = call_title_match_model_batch(
            requests,
            timeout_seconds=max(0.001, timeout_seconds),
            max_attempts=3,
            max_elapsed_seconds=max(0.001, timeout_seconds),
        )
    return {index: _persistable_model_result(result) for index, result in results.items()}


def _runtime_model_batch_size() -> int:
    raw = os.environ.get("TITLE_MATCH_MODEL_BATCH_SIZE", "").strip()
    if not raw:
        return 10
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeConfigurationError("TITLE_MATCH_MODEL_BATCH_SIZE must be a positive integer") from exc
    if value <= 0:
        raise RuntimeConfigurationError("TITLE_MATCH_MODEL_BATCH_SIZE must be a positive integer")
    return value


def legacy_zhaoshang_bridge(
    adapter: CnncLegacyZhaoshangAdapter,
    *,
    paths: RuntimePaths,
    business_date: str,
    monotonic: Callable[[], float] = monotonic_time.monotonic,
) -> tuple[Callable[[list[Mapping[str, Any]], float], list[CrawlPlan]], Callable[[CrawlPlan, tuple[CrawlMember, ...], float], CrawlPlan], Callable[[Mapping[str, Any], list[Mapping[str, Any]], float], Mapping[str, Any]], Callable[[list[Mapping[str, Any]], float], Mapping[int, Mapping[str, Any]]]]:
    """Bridge only legacy create-only and wait-export-only modes.

    No priority API is called here. The legacy tool retains its own controlled
    priority behaviour; this bridge neither imports nor invokes its SQL code.
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
        request = adapter.request_from_saas_row(row, backend_version=backend_version)
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
        if tuple(plan.members) != members:
            raise ValueError("legacy template must use exactly the durable pending members")
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
        if rebuilt.logical_job_key != logical_key or rebuilt.payload_sha256 != str(job.get("payload_hash") or ""):
            raise ValueError("durable legacy job evidence does not match immutable payload")
        prepared[logical_key] = rebuilt
        return rebuilt

    def remote_identity(prepared_job: Any, task_id: str | None, task_number: str | None) -> str:
        if not task_id or not task_number:
            raise RuntimeConfigurationError("legacy remote task identity is incomplete")
        return f"{prepared_job.mall_key}|{task_id}|{task_number}"

    def submit(job: Mapping[str, Any], _crawls: list[Mapping[str, Any]], timeout: float) -> Mapping[str, Any]:
        try:
            prepared_job = reconstruct(job)
        except Exception as exc:
            raise RuntimeConfigurationError("missing exact prepared legacy job; refusing create") from exc
        outcome = adapter.submit(prepared_job, timeout_seconds=max(0.001, timeout))
        if outcome.status is SubmitStatus.SUBMIT_UNKNOWN:
            raise TimeoutError("legacy create-only outcome unknown")
        if outcome.status is not SubmitStatus.SUBMITTED:
            raise RuntimeConfigurationError("legacy create-only was not accepted")
        return {"remote_identity": remote_identity(prepared_job, outcome.task_id, outcome.task_number), "state": "SUBMITTED"}

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
                        publish({"state": "SUBMITTED", "remote_identity": identity})
                elif reconcile.status is ReconcileStatus.MANUAL:
                    publish({"manual_error": "exact legacy unknown-submit reconciliation did not confirm a task"})
                continue
            outcome = adapter.harvest(prepared_job, timeout_seconds=job_timeout)
            if outcome.status is HarvestStatus.EXPORTED:
                result_map = dict(outcome.result_map or {})
                result_by_identity = dict(outcome.result_by_identity or {})
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
                    if value is None:
                        crawl_results[int(crawl["id"])] = {
                            "terminal": True,
                            "error": "legacy export missing safe skuId / exact URL result",
                        }
                    else:
                        crawl_results[int(crawl["id"])] = value
                state = "COMPLETED" if len(crawl_results) == len(job.get("crawl_work", [])) else "EXPORTED"
                publish({"state": state, "remote_identity": str(job.get("remote_identity") or "legacy"), "result": result_map, "crawl_results": crawl_results})
            elif outcome.status is HarvestStatus.RUNNING:
                publish({"state": "RUNNING", "remote_identity": str(job.get("remote_identity") or "legacy")})
            else:
                publish({"manual_error": "legacy wait-export-only requires manual review"})
        return results

    return propose, finalize, submit, harvest


def batch_zhaoshang_bridge(
    adapter: CnncBatchZhaoshangAdapter,
    legacy_adapter: CnncLegacyZhaoshangAdapter,
    *,
    paths: RuntimePaths,
    business_date: str,
) -> tuple[Callable[[list[Mapping[str, Any]], float], list[CrawlPlan]], Callable[[CrawlPlan, tuple[CrawlMember, ...], float], CrawlPlan], Callable[[Mapping[str, Any], list[Mapping[str, Any]], float], Mapping[str, Any]], Callable[[list[Mapping[str, Any]], float], Mapping[int, Mapping[str, Any]]]]:
    """Wire the evidenced batch create/progress contract into durable jobs.

    The local company skill does not specify a batch export API. Completed
    jobs therefore become manual export items instead of guessing or mixing a
    legacy task identity with a batch task identity.
    """
    prepared: dict[str, Any] = {}

    def resolve(row: Mapping[str, Any]) -> CrawlRequest | None:
        request = legacy_adapter.request_from_saas_row(row, backend_version="batch-url-v1")
        if request is not None and not request.url:
            return None
        return request

    def propose(items: list[Mapping[str, Any]], _timeout: float) -> list[CrawlPlan]:
        requests: list[CrawlRequest] = []
        members_by_identity: dict[tuple[str, str, str, str, str], list[CrawlMember]] = {}
        for item in items:
            request = resolve(item["row"])
            if request is None:
                continue
            source_id = normalize_cell_str(item["row"].get("vCommodityId") or item["row"].get("documentId"))
            member = CrawlMember(
                request.mall_key, source_id, request.sku_id, request.url,
                request.backend_version, item["row"],
            )
            requests.append(request)
            members_by_identity.setdefault(request.identity, []).append(member)
        if not requests:
            return []
        plans = []
        for job in adapter.prepare_jobs(business_date, requests).jobs:
            members = tuple(
                member
                for request in job.requests
                for member in members_by_identity.get(request.identity, ())
            )
            prepared[job.logical_job_key] = job
            plans.append(CrawlPlan(
                job.mall_key, job.logical_job_key, job.task_name,
                job.payload_sha256, payload_json=job.durable_payload(), members=members,
            ))
        return plans

    def finalize(plan: CrawlPlan, members: tuple[CrawlMember, ...], _timeout: float) -> CrawlPlan:
        if tuple(plan.members) != members:
            raise RuntimeConfigurationError("batch plan membership changed before durable intent")
        return plan

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
            return {"remote_identity": outcome.remote_identity.durable_value(), "state": "SUBMITTED"}
        if outcome.status is BatchSubmitStatus.REJECTED:
            raise RuntimeConfigurationError("batch create was deterministically rejected")
        if outcome.status is BatchSubmitStatus.NOT_ATTEMPTED:
            raise RuntimeConfigurationError("batch create is not enabled")
        raise TimeoutError("batch create outcome is unknown")

    def harvest(jobs: list[Mapping[str, Any]], timeout: float) -> Mapping[int, Mapping[str, Any]]:
        results: dict[int, Mapping[str, Any]] = {}
        per_job_timeout = max(0.001, timeout / max(1, len(jobs)))
        for job in jobs:
            try:
                prepared_job = reconstruct(job)
                if str(job.get("state")) == "SUBMIT_UNKNOWN":
                    reconciliation = adapter.reconcile_unknown(prepared_job, timeout_seconds=per_job_timeout)
                    if reconciliation.status is BatchReconcileStatus.MANUAL:
                        results[int(job["id"])] = {"manual_error": reconciliation.diagnostic or "batch identity conflict"}
                    elif reconciliation.status is BatchReconcileStatus.FOUND and reconciliation.remote_identity is not None:
                        results[int(job["id"])] = {
                            "state": "SUBMITTED",
                            "remote_identity": reconciliation.remote_identity.durable_value(),
                        }
                    continue
                _response, progress = adapter.query_progress(prepared_job, timeout_seconds=per_job_timeout)
                identity = str(job.get("remote_identity") or prepared_job.remote_identity.durable_value())
                if progress.status is BatchProgressStatus.COMPLETED:
                    results[int(job["id"])] = {
                        "manual_error": "batch task completed but no locally evidenced export contract is available"
                    }
                elif progress.status is BatchProgressStatus.FAILED:
                    results[int(job["id"])] = {"manual_error": "batch task reported failed subtasks"}
                else:
                    results[int(job["id"])] = {"state": "RUNNING", "remote_identity": identity}
            except Exception:
                continue
        return results

    return propose, finalize, submit, harvest


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
        Path("data/latest_saas_sameAudit_queryAll.curl"),
        Path("data/latest_cnnc_query.curl"),
        auth_manager=AuthManager(),
    )
    detail_template = load_curl_command(Path("data/detail_request.curl"))
    if zhaoshang_backend not in {"legacy", "batch"}:
        raise RuntimeConfigurationError("Zhaoshang backend must be legacy or batch")
    legacy_adapter: CnncLegacyZhaoshangAdapter | None = None
    resolver: Callable[[Mapping[str, Any]], CrawlRequest | None] | None = None
    if paths is not None and business_date is not None:
        legacy_adapter = CnncLegacyZhaoshangAdapter()
        backend_version = "legacy-v1" if zhaoshang_backend == "legacy" else "batch-url-v1"
        resolver = lambda row: legacy_adapter.request_from_saas_row(row, backend_version=backend_version)
    dependencies = TickDependencies(
        discover=snapshot_discoverer(snapshot_adapter, crawl_request_resolver=resolver),
        detail=_detail_worker(detail_template, detail_lock_path=paths.detail_lock_path if paths is not None else None),
        model=_model_worker,
        model_batch=_model_batch_worker,
    )
    if paths is not None and business_date is not None:
        assert legacy_adapter is not None
        if zhaoshang_backend == "batch":
            planner, finalizer, submitter, harvester = batch_zhaoshang_bridge(
                CnncBatchZhaoshangAdapter(write_enabled=enable_zhaoshang),
                legacy_adapter,
                paths=paths,
                business_date=business_date,
            )
        else:
            planner, finalizer, submitter, harvester = legacy_zhaoshang_bridge(legacy_adapter, paths=paths, business_date=business_date)
        # Read-only wait/export and exact unknown-submit reconciliation remain
        # available on ordinary ticks. Only planner+submit are write-capable.
        dependencies.harvest = harvester
    if enable_zhaoshang:
        if paths is None or business_date is None:
            raise RuntimeConfigurationError("Zhaoshang runtime paths and business date are required")
        dependencies.plan_crawl = planner
        dependencies.finalize_crawl_plan = finalizer
        dependencies.submit_crawl = submitter
        dependencies.harvest = harvester
    return dependencies


def run_runtime_command(
    command: str,
    *,
    business_date: str | None = None,
    runtime_root: Path | str = DEFAULT_RUNTIME_ROOT,
    runtime_budget_seconds: float = 720,
    enable_zhaoshang: bool = False,
    zhaoshang_backend: str = "legacy",
    dependencies: TickDependencies | None = None,
    output_path: Path | str | None = None,
    now: datetime | None = None,
    materialize: Callable[..., FinalizePublication] = materialize_frozen_daily_run,
    monotonic: Callable[[], float] = monotonic_time.monotonic,
) -> RuntimeResult:
    command_started = monotonic()
    if command not in {"tick", "finalize"}:
        raise ValueError("runtime command must be tick or finalize")
    day = business_date or business_date_in_shanghai(now)
    paths = runtime_paths(day, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    _ensure_batch_state(
        paths,
        enable_zhaoshang=enable_zhaoshang,
        zhaoshang_backend=zhaoshang_backend,
    )
    store = DailyAuditStore(paths.state_path)
    deps = dependencies or build_default_dependencies(
        enable_zhaoshang=enable_zhaoshang,
        zhaoshang_backend=zhaoshang_backend,
        paths=paths,
        business_date=day,
    )
    config = TickConfig(
        business_date=day,
        runtime_budget_seconds=runtime_budget_seconds,
        cutoff_at=cutoff_for_business_date(day),
        model_name=_configured_model_name(),
        model_prompt_version=TITLE_MATCH_PROMPT_VERSION,
        model_schema_version=TITLE_MATCH_SCHEMA_VERSION,
        model_batch_size=_runtime_model_batch_size(),
    )
    summary = run_incremental_tick(store, ProcessLock(paths.lock_path), config, deps, finalize=command == "finalize")
    if (
        command == "finalize"
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
        command != "finalize"
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
            ProcessLock(paths.lock_path),
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
