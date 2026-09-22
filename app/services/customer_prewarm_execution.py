# Shared exact-identity remote execution for prewarm phases.
# The dispatcher owns ordering/budgets; phases never import the dispatcher.
from __future__ import annotations
import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.customer_reference_routing import classify_reference_url
from app.services.daily_audit_store import DailyAuditStore, LeaseToken
from app.services.runtime_context import _logical_key_matches_rebuild
from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter, CrawlRequest, PriorityStatus, SubmitStatus
from app.services.shared_crawl_cache import PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
from app.integrations.zhaoshang_mall_mapping import ZhaoshangMallMapping

BACKEND_VERSION = "legacy-v1"

CANARY_PRIORITY_SCOPE_VIOLATION = "CANARY_PRIORITY_SCOPE_VIOLATION"

class CanaryScopeViolation(RuntimeError):
    """A live canary pass attempted remote work outside ``--only-mall``."""

@dataclass(frozen=True)
class CanaryScope:
    """唯一 canary 策略对象：贯穿 CLI 与 service 全部四阶段的 scope 判定。

    ``mall_in_scope`` 在 strip 后严格相等（空 mall 恒 False），绝不做前缀或
    包含匹配。  ``max_urls``/``max_jobs`` 是 preflight 与 Phase 3 断言共用的
    硬上限（默认各 1，与 CLI 默认值一致）。
    """

    only_mall: str
    max_urls: int = 1
    max_jobs: int = 1

    def mall_in_scope(self, mall: Any) -> bool:
        value = str(mall or "").strip()
        return bool(value) and value == self.only_mall

class CanaryLiveBudget:
    """Live canary 硬预算：preflight 批准的 (mall, exact_url) 目标集合。

    成员检查即容量检查——集合在构造时已验证 |set| <= max_urls，live 中任何
    不在集合内的 interaction 都是 CANARY_LIVE_SCOPE_DRIFT，fail closed。
    """

    def __init__(self, only_mall: str, max_urls: int, allowed_urls) -> None:
        pairs = {(str(m).strip(), str(u).strip()) for m, u in allowed_urls}
        if len(pairs) > int(max_urls):
            raise CanaryScopeViolation(
                f"canary target set has {len(pairs)} urls, exceeds max {max_urls}"
            )
        self.only_mall = only_mall
        self.max_urls = int(max_urls)
        self._allowed = frozenset(pairs)

    @classmethod
    def from_plan(cls, plan: Mapping[str, Any], canary_scope: CanaryScope) -> "CanaryLiveBudget":
        pairs = [tuple(item) for item in plan.get("canary_target_urls") or []]
        return cls(canary_scope.only_mall, canary_scope.max_urls, pairs)

    def check(self, mall: Any, exact_url: Any) -> tuple[str, str]:
        key = (str(mall or "").strip(), str(exact_url or "").strip())
        if key not in self._allowed:
            raise CanaryScopeViolation(
                f"CANARY_LIVE_SCOPE_DRIFT: live interaction {key!r} is not in "
                "the read-only preflight target set"
            )
        return key

    def revalidate(self, current_urls) -> int:
        """Live 零写 revalidation：current ⊆ allowed（shrink 合法，增长即漂移）。

        返回 current 集合大小；任一 live actionable 目标不在 preflight 集合内
        时 raise CanaryScopeViolation（CANARY_LIVE_SCOPE_DRIFT），调用方必须
        在任何业务写之前 fail closed。
        """
        current = {(str(m).strip(), str(u).strip()) for m, u in current_urls}
        drift = current - self._allowed
        if drift:
            raise CanaryScopeViolation(
                f"CANARY_LIVE_SCOPE_DRIFT: live actionable targets not in the "
                f"preflight set: {sorted(drift)!r}"
            )
        return len(current)

LEASE_TTL_SECONDS = 120.0

SUBMIT_MIN_SECONDS = 5.0

_PRIORITY_PENDING_NOTE = "priority pending confirmation"

def _status_value(value: Any) -> str:
    """Normalize enum-or-plain statuses to their string value."""
    return str(getattr(value, "value", value))

def prewarm_placeholder_sku(mall_key: str, url: str) -> str:
    """Deterministic ``zhpq-`` placeholder SKU for one (mall, url) pair."""
    digest = hashlib.sha256(f"{mall_key}\n{url}".encode("utf-8")).hexdigest()[:16]
    return f"zhpq-{digest}"

def prewarm_crawl_fingerprint(
    mall_key: str, source_id: str, sku_id: str, exact_url: str, business_date: str
) -> str:
    """Stable fingerprint for one prewarm crawl row (canonical JSON, sha256)."""
    payload = {
        "identity": {
            "mall_key": mall_key,
            "source_id": source_id,
            "sku_id": sku_id,
            "exact_url": exact_url,
            "backend_version": BACKEND_VERSION,
        },
        "business_date": business_date,
        "prewarm": True,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

def _priority_proven(priority: Any) -> bool:
    """True only when the outcome status is accepted AND weight_type == -1.

    A status without the DB-side ``weight_type = -1`` proof is NOT a confirmed
    boost (fail-closed; the job stays ``PRIORITY_PENDING``).
    """
    status = _status_value(getattr(priority, "status", None))
    return status in (
        PriorityStatus.UPDATED.value,
        PriorityStatus.ALREADY_HIGHEST.value,
    ) and getattr(priority, "weight_type", None) == -1

def _candidate_mall(candidate: Mapping[str, Any], mappings: Sequence[ZhaoshangMallMapping]) -> str:
    mall = str(candidate.get("mall") or "").strip()
    if mall:
        return mall
    return classify_reference_url(str(candidate.get("lookup_url") or ""), mappings).mall_key

def _record(
    prewarm_store: CustomerPrewarmStore,
    candidate_id: int,
    *,
    cache_status: str,
    match_level: str | None = None,
    resolved_identity_id: int | None = None,
    error: str | None = None,
    remote_job_id: int | None = None,
    phase_errors: list[str],
) -> bool:
    try:
        prewarm_store.record_candidate_result(
            candidate_id,
            cache_status=cache_status,
            match_level=match_level,
            resolved_identity_id=resolved_identity_id,
            error=error,
            remote_job_id=remote_job_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - per-candidate isolation
        phase_errors.append(f"candidate {candidate_id}: {type(exc).__name__}: {exc}")
        return False

def _reconstruct_prepared_job(
    adapter: Any, business_date: str, job_row: Mapping[str, Any]
) -> Any:
    """Rebuild the immutable prepared job from durable payload evidence.

    Mirrors ``incremental_audit_runtime.legacy_zhaoshang_bridge.reconstruct``:
    payload_json → CrawlRequests → ``adapter.prepare_job`` with the SAME
    paths/task_name, then logical-key + payload-hash verification.  Raises on
    any mismatch (fail-closed; the caller skips the boost).
    """
    payload_value = job_row.get("payload_json") or "{}"
    payload = json.loads(payload_value) if isinstance(payload_value, str) else dict(payload_value)
    requests = [CrawlRequest(**dict(row)) for row in payload["requests"]]
    batch_path = Path(str(payload["batch_state"]))
    input_path = Path(str(job_row.get("input_path") or ""))
    result_path = Path(str(job_row.get("result_path") or ""))
    if not batch_path.is_file() or not input_path.is_file() or not result_path.parent.is_dir():
        raise ValueError("durable legacy paths are unavailable")
    rebuilt = adapter.prepare_job(
        business_date,
        requests,
        input_path,
        result_path,
        str(job_row.get("task_name") or ""),
        batch_state_path=batch_path,
    )
    if not _logical_key_matches_rebuild(
        str(job_row.get("logical_key") or ""), rebuilt.logical_job_key
    ) or rebuilt.payload_sha256 != str(job_row.get("payload_hash") or ""):
        raise ValueError("durable legacy job evidence does not match immutable payload")
    return rebuilt

def _job_interaction_pairs(job_row: Mapping[str, Any]) -> list[tuple[Any, Any]]:
    """job payload_json 里的 (mall_key, url) 对（canary 预算身份，durable 证据）。"""
    payload = job_row.get("payload_json") or "{}"
    data = json.loads(payload) if isinstance(payload, str) else dict(payload)
    return [(row.get("mall_key"), row.get("url")) for row in data.get("requests", [])]

def _confirm_pending_priority_job(
    *,
    prewarm_store: CustomerPrewarmStore | None,
    store: DailyAuditStore,
    token: LeaseToken,
    get_adapter: Callable[[], Any],
    business_date: str,
    note_targets: Sequence[tuple[int, str]],
    job_id: int,
    deadline: float,
    monotonic_value: Callable[[], float],
    summary: dict[str, Any],
    prepared: Any | None = None,
    canary_scope: CanaryScope | None = None,
    canary_budget: CanaryLiveBudget | None = None,
    result_commit_guard: Callable[[], bool] | None = None,
    result_commit_fence: Callable[[], Any] | None = None,
    pre_remote_guard: Callable[[], bool] | None = None,
) -> bool | None:
    """One prioritize-only confirm for a job still exactly ``PRIORITY_PENDING``
    with a remote identity (fail-closed; never creates or submits).

    Returns ``True`` when ``weight_type == -1`` was PROVEN (status
    ``UPDATED``/``ALREADY_HIGHEST`` AND weight_type -1): the job reconciles to
    ``SUBMITTED``.  Returns ``False`` when the confirm attempt ran (or was
    budget-deferred) without proof: the job stays ``PRIORITY_PENDING`` and the
    next pass retries.  Returns ``None`` when the job is not a confirm
    candidate (missing / identity-less / already moved on) so the reuse-boost
    path may own it.  ``prepared`` may carry the in-memory job of a fresh
    create; otherwise it is reconstructed from durable payload evidence.
    ``result_commit_guard`` is a final ownership check for cross-day callers;
    a false result discards otherwise proven remote evidence before any state
    write.  Same-day callers leave it unset and keep their existing behavior.
    ``result_commit_fence`` additionally serializes the ownership check through
    the historical state commit when ownership lives in another database.
    ``pre_remote_guard`` checks cross-day linkage after lease renewal and
    immediately before the remote priority write. It does not replace the
    post-call commit fence.
    """
    if note_targets and prewarm_store is None:
        raise ValueError("prewarm_store is required when candidate notes are requested")
    phase_errors = summary["phase_errors"]
    try:
        job_row = store.get_remote_job(job_id)
        if job_row is None:
            return None
        identity = str(job_row.get("remote_identity") or "").strip()
        if not identity or str(job_row.get("state") or "") != "PRIORITY_PENDING":
            return None  # already proven or lifecycle-owned: no second attempt
        if prepared is None:
            prepared = _reconstruct_prepared_job(get_adapter(), business_date, job_row)
        parts = identity.split("|")
        if (
            len(parts) != 3
            or parts[0] != prepared.mall_key
            or not parts[1]
            or not parts[2]
        ):
            raise ValueError("legacy remote task identity is invalid")
        if canary_scope is not None and not (
            str(job_row.get("mall_key") or "") == canary_scope.only_mall
            and parts[0] == canary_scope.only_mall
            and prepared.mall_key == canary_scope.only_mall
        ):
            # Second defensive layer: all THREE mall evidences (remote_jobs
            # mall_key column, identity first segment, prepared.mall_key) must
            # prove the canary target before any prioritize call is made.
            summary["phase_errors"].append(
                f"priority confirm job {job_id}: {CANARY_PRIORITY_SCOPE_VIOLATION} "
                f"(job mall evidence does not prove {canary_scope.only_mall!r})"
            )
            return False
        if canary_budget is not None:
            # Second canary layer (per call): the job's durable payload
            # (mall, url) pairs must all sit inside the preflight budget
            # before any prioritize call is made.
            for mall_value, url_value in _job_interaction_pairs(job_row):
                canary_budget.check(mall_value, url_value)
        remaining = deadline - monotonic_value()
        if remaining < SUBMIT_MIN_SECONDS:
            phase_errors.append(
                f"priority confirm of job {job_id} deferred: budget floor reached; "
                "job stays PRIORITY_PENDING"
            )
            return False
        store.renew_lease(token, LEASE_TTL_SECONDS)
        if pre_remote_guard is not None and not pre_remote_guard():
            summary["priority_unconfirmed"] += 1
            return False
        priority = get_adapter().prioritize(
            prepared,
            task_id=parts[1],
            task_number=parts[2],
            timeout_seconds=max(0.001, deadline - monotonic_value()),
        )
        if canary_scope is not None:
            summary["priority_updates_performed"] += 1
        if _priority_proven(priority):
            if result_commit_guard is not None and not result_commit_guard():
                summary["priority_unconfirmed"] += 1
                return False
            commit_fence = (
                result_commit_fence()
                if result_commit_fence is not None
                else nullcontext(True)
            )
            with commit_fence as commit_allowed:
                if not commit_allowed:
                    summary["priority_unconfirmed"] += 1
                    return False
                store.confirm_remote_job_priority_and_crawl_work(
                    token,
                    job_id,
                    remote_identity=identity,
                )
            summary["priority_confirmed"] += 1
            return True
        summary["priority_unconfirmed"] += 1
        diagnostic = str(
            getattr(priority, "diagnostic", "") or "highest priority was not confirmed"
        )
        phase_errors.append(f"priority confirm job {job_id}: not proven ({diagnostic})")
        _record_priority_pending_note(
            prewarm_store, note_targets, job_id, diagnostic, phase_errors
        )
        return False
    except Exception as exc:  # noqa: BLE001 - confirm failure never fails the pass
        summary["priority_unconfirmed"] += 1
        phase_errors.append(f"priority confirm job {job_id}: {type(exc).__name__}: {exc}")
        _record_priority_pending_note(
            prewarm_store,
            note_targets,
            job_id,
            f"{type(exc).__name__}: {exc}",
            phase_errors,
        )
        return False

def _record_priority_pending_note(
    prewarm_store: CustomerPrewarmStore | None,
    note_targets: Sequence[tuple[int, str]],
    job_id: int,
    diagnostic: str,
    phase_errors: list[str],
) -> None:
    """Keep each bound candidate at its REMOTE_* status with the pending note.

    Same-status records never erase prior evidence (the store only refreshes
    ``last_error`` when a non-NULL note is supplied).
    """
    if not note_targets:
        return
    if prewarm_store is None:
        raise ValueError("prewarm_store is required when candidate notes are requested")
    for candidate_id, cache_status in note_targets:
        _record(
            prewarm_store,
            candidate_id,
            cache_status=cache_status,
            remote_job_id=job_id,
            error=f"{_PRIORITY_PENDING_NOTE}: {diagnostic}",
            phase_errors=phase_errors,
        )


def harvest_existing_customer_prewarm_job(**kwargs: Any) -> dict[str, Any]:
    """Lazy bridge to the recovery primitive.

    Importing the lifecycle planner must stay free of Zhaoshang module access;
    the heavy recovery module is loaded only when a real harvest is attempted.
    The wrapper also preserves the established monkeypatch seam used by tests.
    """
    from app.services.recovery.harvest import (
        harvest_existing_customer_prewarm_job as _harvest_existing_customer_prewarm_job,
    )

    return _harvest_existing_customer_prewarm_job(**kwargs)

RECONCILE_DEAD_THRESHOLD = 4

_RECONCILE_MANUAL_BLOCKER = "prewarm reconcile identity conflict: "

def _terminal_priority_manual_live_proof(
    *,
    state_path: str | Path,
    business_date: str,
    job_row: Mapping[str, Any],
    adapter: Any,
    timeout_seconds: float,
) -> Any | None:
    """Build proof only for the exact customer-prewarm scope-safety incident."""
    from app.services.recovery.harvest import (
        is_terminal_nonretryable_priority_manual_family,
        live_identity_proof,
    )
    from app.services.remote_execution_policy import (
        RemoteExecutionState,
        canonical_progress_signature,
        classify_remote_execution_state,
    )

    if not is_terminal_nonretryable_priority_manual_family(job_row):
        return None
    identity = str(job_row.get("remote_identity") or "").strip()
    identity_parts = identity.split("|")
    if len(identity_parts) != 3 or not all(identity_parts):
        return None
    try:
        prepared = _reconstruct_prepared_job(adapter, business_date, job_row)
        if (
            adapter.exact_nonretryable_unconfirmed_priority_remote_identity(prepared)
            != identity
        ):
            return None
        prewarm_path = Path(state_path).resolve().parent.parent / "customer_prewarm.sqlite3"
        prewarm = CustomerPrewarmStore.open_read_only(prewarm_path)
        if not prewarm.exact_current_remote_job_group_matches(
            business_date=business_date,
            remote_job_id=int(job_row["id"]),
            producer=PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
            mall=str(job_row.get("mall_key") or "").strip(),
            lookup_urls=tuple(request.url for request in prepared.requests),
        ):
            return None
        answer = adapter.query_task_progress_for_job(
            prepared,
            identity_parts[1],
            identity_parts[2],
            min(30.0, timeout_seconds),
        )
    except Exception:
        return None
    progress = answer.get("progress") if isinstance(answer, Mapping) else None
    progress_mapping = progress if isinstance(progress, Mapping) else None
    classification = classify_remote_execution_state(progress_mapping, "")
    if classification is not RemoteExecutionState.TERMINAL:
        return None
    signature = canonical_progress_signature(progress_mapping or {})
    if signature is None:
        return None
    return live_identity_proof(
        remote_identity=identity,
        classification=classification,
        progress_signature=signature,
    )

def _mark_reconcile_manual(
    store: DailyAuditStore, token: LeaseToken, job_id: int, diagnostic: str
) -> None:
    store.mark_remote_manual(
        token,
        job_id,
        _RECONCILE_MANUAL_BLOCKER + (diagnostic.strip() or "remote identity cannot be proven"),
    )
