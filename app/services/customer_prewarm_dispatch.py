"""Issue #10 customer prewarm dispatcher: turn cache misses into remote work.

One bounded pass reuses the EXISTING STEP1 zhaoshang lifecycle — it never
introduces a second remote-task store:

- absorb: candidates already in ``REMOTE_DISPATCHED``/``REMOTE_REUSED`` flip to
  ``CACHE_HIT`` as soon as a real :meth:`SharedCrawlCache.lookup_current` hit
  appears (Phase A cache sync is the only cache writer; export success never
  flips the status directly).
- route: ``PENDING_PREWARM`` misses are classified by
  :func:`classify_reference_url` (Staples first, then the Tmall manual-skip
  host, then the mall mapping).  Every zhaoshang candidate gets one zero-cost
  cache recheck before any remote work is planned.
- reuse/create per mall group: a read-only state-DB probe
  (:func:`find_remote_work_for_url`) decides reuse vs create BEFORE anything is
  written — an unfinished bound job is reused (never re-created), a terminal
  crawl row waits for the Phase A cache sync (success) or is reported failed,
  and only truly missing work goes through the exact STEP1 create chain:
  ``ensure_crawl_work`` (``zhpq-`` placeholder SKU) → ``prepare_remote_intent``
  (idempotent logical key) → ``link_crawl_work_batch_to_remote_job`` →
  ``claim_remote_submission`` (atomic post_attempted burn) → ``adapter.submit``
  → ``reconcile_remote_job`` + ``advance_crawl_work_for_job``.  The task
  fingerprint / numeric suffix / ``cnnc_current_opt_*`` task name mirror
  ``incremental_audit_runtime.legacy_zhaoshang_bridge.finalize`` byte for byte.
  SUBMIT_UNKNOWN reconciles only (never re-created); a pre-send NOT_ATTEMPTED
  releases the burned claim and leaves the candidate ``PENDING_PREWARM`` so the
  next pass re-plans truthfully (the transition table forbids
  ``REMOTE_* -> PENDING_PREWARM`` because no dispatch happened — and here none
  did).
- boost: reused jobs with a confirmed remote identity in a non-terminal state
  get one idempotent :meth:`adapter.prioritize` call (evidence replay is
  durable); ``UNCONFIRMED`` fails closed and is retried next pass.
- priority proof: a weight_type of ``-1`` is PROVEN, never assumed.  A fresh
  create is reconciled to ``PRIORITY_PENDING`` first (submit-time
  ``priority_evidence`` is ignored for state) and then confirmed in the same
  pass via :meth:`adapter.prioritize`; only an outcome whose status is
  ``UPDATED``/``ALREADY_HIGHEST`` AND whose ``weight_type == -1`` reconciles
  the job to ``SUBMITTED`` (counter ``priority_confirmed``).  Anything else —
  ``UNCONFIRMED``, an exception, or a readback whose weight_type is not -1 —
  leaves the job ``PRIORITY_PENDING`` and retries next pass (candidates
  ``REMOTE_DISPATCHED``/``REMOTE_REUSED`` stay, never re-created).
  ``priority_boosted`` counts only PROVEN boosts of reused jobs (status
  ``UPDATED``/``ALREADY_HIGHEST`` AND readback ``weight_type == -1``; a
  status-accepted-but-weight-unproven outcome takes the unconfirmed path with
  counter ``priority_unconfirmed``).  ``PRIORITY_PENDING`` jobs with a remote
  identity re-enter the confirm list from BOTH remote candidate statuses every
  pass.  Round 3 cross-day protection: pass Phase 1 never resolves a
  candidate's bound job by bare id against today's DB when a linkage row dates
  that job on an earlier day (skipped with a phase note — the manual-recrawl
  exact path owns cross-day work through a strictly read-only probe of that
  day's state DB and a no-write boost).
- plan-only: the whole report is built from read-only probes and returns
  BEFORE any lock, store write, or adapter construction — provably zero-write.

Concurrency: the pass takes its own instance lock
(``customer_prewarm_dispatch.lock`` next to the runtime root; busy →
``SKIPPED_ALREADY_RUNNING``) and then STEP1's per-day process lock + durable
lease (busy → ``LOCK_BUSY``).  All candidate writes happen only after the lease
is held, so every skipped outcome is write-free by construction.  DB-level
backstops (logical-key idempotency, post_attempted burn, remote_identity
partial unique index) make a lost race a benign no-op.

Every phase is error-isolated: one bad candidate, URL, or mall group is
counted into ``phase_errors`` and the pass continues.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.services.customer_prewarm_discovery import _open_state_db_readonly
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_MANUAL_SKIP,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_REMOTE_REUSED,
    CACHE_STATUS_ROUTING_BLOCKED,
    CACHE_STATUS_STAPLES_PENDING,
    CustomerPrewarmStore,
)
from app.services.customer_reference_routing import (
    OUTCOME_MANUAL_SKIP,
    OUTCOME_STAPLES,
    OUTCOME_UNMAPPED,
    OUTCOME_ZHAOSHANG,
    classify_reference_url,
)
from app.services.daily_audit_store import (
    DailyAuditStore,
    LeaseToken,
    LeaseUnavailable,
    ProcessLock,
)
from app.services.incremental_audit_runtime import (
    _logical_key_matches_rebuild,
    runtime_paths,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    PriorityStatus,
    SubmitStatus,
)
from app.services.shared_crawl_cache import (
    PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
    PRODUCER_MANUAL_RECRAWL_ZHAOSHANG,
)
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

BACKEND_VERSION = "legacy-v1"
DISPATCH_LOCK_NAME = "customer_prewarm_dispatch.lock"
DEFAULT_BUDGET_SECONDS = 50.0
DEFAULT_MAX_JOBS_PER_PASS = 8

# ---- Issue #10 canary hard scope gate ---------------------------------------
# 只读 preflight 的 fail-closed 结果码（值 = 同名字符串，scheduler 直接匹配）。
CANARY_MALL_NOT_MAPPED = "CANARY_MALL_NOT_MAPPED"
CANARY_TARGET_EMPTY = "CANARY_TARGET_EMPTY"
CANARY_URL_LIMIT_EXCEEDED = "CANARY_URL_LIMIT_EXCEEDED"
CANARY_JOB_LIMIT_EXCEEDED = "CANARY_JOB_LIMIT_EXCEEDED"
CANARY_PREFLIGHT_FAILED = "CANARY_PREFLIGHT_FAILED"
# live 第二层防御：Phase 4 三证不一致时的 phase_error 记号。
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


LEASE_OWNER = "customer_prewarm_dispatch"
LEASE_TTL_SECONDS = 120.0

# Pre-claim budget floor: below it the intent stays unclaimed for the resume path.
SUBMIT_MIN_SECONDS = 5.0

# Remote-job states that prove live (or replayable) work for the exact URL:
# reuse never re-creates.  ``SUBMIT_INTENT`` is deliberately absent — an intent
# whose POST never started (pre-send NOT_ATTEMPTED) must be re-planned through
# the idempotent create chain, not treated as dispatched work.
_INFLIGHT_JOB_STATES = frozenset(
    {
        "PENDING",
        "PRIORITY_PENDING",
        "SUBMITTED",
        "RUNNING",
        "EXPORTED",
        "RETRYABLE_FAILED",
        "SUBMIT_UNKNOWN",
    }
)
_TERMINAL_CRAWL_STATES = frozenset({"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"})
_REPLANNABLE_CRAWL_STATES = frozenset({"PENDING", "RETRYABLE_FAILED"})
# Only these job states justify a priority boost outside the create run; the
# existing lifecycle owns everything else (e.g. SUBMIT_UNKNOWN reconciliation).
_BOOSTABLE_JOB_STATES = frozenset(
    {"PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED"}
)

_CACHE_SYNC_PENDING_NOTE = "cache sync pending: terminal crawl success awaits Phase A cache sync"
_TERMINAL_FAILURE_NOTE = "terminal crawl failure"
_SUBMIT_UNKNOWN_NOTE = "submit unknown; reconcile pending"
_SUBMIT_NOT_ATTEMPTED_NOTE = "submit not attempted (pre-send); retry next pass"
_PRIORITY_UNCONFIRMED_PREFIX = "PRIORITY_UNCONFIRMED"
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


def find_remote_work_for_url(
    state_db_path: str | Path,
    mall_key: str,
    exact_url: str,
    backend_version: str = BACKEND_VERSION,
) -> dict[str, Any] | None:
    """Read-only (``mode=ro``) probe: what remote work already covers one URL?

    Returns ``None`` when the state DB is missing; otherwise::

        {"unfinished_bound": {"crawl_id", "crawl_state", "job": {...}} | None,
         "terminal_success": bool, "terminal_failed": bool,
         "replannable_count": int}

    ``unfinished_bound`` matches the first crawl row bound to a remote job in
    one of the in-flight states (``SUBMIT_INTENT`` is NOT one: an intent whose
    POST never started stays re-plannable).  Terminal classification reads the
    crawl row states; ``replannable_count`` counts unbound rows the create
    chain may pick up.  The per-day state DB only holds that day's run, so no
    date filter is needed.
    """
    connection = _open_state_db_readonly(state_db_path)
    if connection is None:
        return None
    try:
        rows = connection.execute(
            """SELECT c.id AS crawl_id, c.state AS crawl_state,
                      c.remote_job_id AS crawl_remote_job_id,
                      j.id AS job_id, j.state AS job_state,
                      j.remote_identity AS remote_identity
               FROM crawl_work c
               LEFT JOIN remote_jobs j ON j.id = c.remote_job_id
               WHERE c.mall_key = ? AND c.exact_url = ? AND c.backend_version = ?
               ORDER BY c.id""",
            (mall_key, exact_url.strip(), backend_version),
        ).fetchall()
    finally:
        connection.close()

    unfinished: dict[str, Any] | None = None
    terminal_success = False
    terminal_failed = False
    replannable_count = 0
    for row in rows:
        crawl_state = str(row["crawl_state"] or "")
        job_state = str(row["job_state"] or "")
        bound = row["crawl_remote_job_id"] is not None
        if bound and job_state in _INFLIGHT_JOB_STATES and unfinished is None:
            unfinished = {
                "crawl_id": int(row["crawl_id"]),
                "crawl_state": crawl_state,
                "job": {
                    "id": int(row["job_id"]),
                    "state": job_state,
                    "remote_identity": str(row["remote_identity"] or ""),
                },
            }
        if crawl_state == "SUCCESS":
            terminal_success = True
        elif crawl_state == "TERMINAL_FAILED":
            terminal_failed = True
        if not bound and crawl_state in _REPLANNABLE_CRAWL_STATES:
            replannable_count += 1
    return {
        "unfinished_bound": unfinished,
        "terminal_success": terminal_success,
        "terminal_failed": terminal_failed,
        "replannable_count": replannable_count,
    }


def _empty_summary(
    business_date: str,
    plan_only: bool,
    canary_scope: CanaryScope | None = None,
) -> dict[str, Any]:
    summary = {
        "outcome": "OK",
        "business_date": business_date,
        "plan_only": bool(plan_only),
        "absorbed": 0,
        "cache_hit_after_recheck": 0,
        "staples_skipped": 0,
        "manual_skipped": 0,
        "routing_blocked": 0,
        "mapped_zhaoshang": 0,
        "existing_remote_reused": 0,
        "new_remote_urls": 0,
        "jobs_created": 0,
        "jobs_reused": 0,
        "submit_unknown": 0,
        "submit_not_attempted": 0,
        # priority_confirmed: distinct jobs PROVEN weight_type == -1 this pass
        # (same-pass confirms of fresh creates + PRIORITY_PENDING confirms of
        # REMOTE_* candidates; state reconciled to SUBMITTED).
        # priority_boosted: PROVEN boosts of reused jobs — status accepted
        # (UPDATED/ALREADY_HIGHEST) AND readback weight_type == -1.  Round 3
        # D3: a status-accepted-but-weight-unproven outcome counts into
        # priority_unconfirmed instead (never claimed as proven).
        "priority_confirmed": 0,
        "priority_boosted": 0,
        "priority_unconfirmed": 0,
        "phase_errors": [],
        "by_mall": {},
        "batch_sizes": [],
    }
    if canary_scope is None:
        # WS-A lifecycle counters (normal mode ONLY): the canary key set must
        # stay byte-identical, so these keys exist exclusively without scope.
        # harvest_attempted / harvested are filled by run_harvest_phase,
        # which the CLI calls after the pass released every lock.
        summary.update(
            {
                "reconciled_found": 0,
                "reconcile_not_found": 0,
                "cross_day_reconcile_planned": 0,
                "cross_day_reconcile_attempted": 0,
                "cross_day_reconciled_found": 0,
                "cross_day_reconcile_not_found": 0,
                "cross_day_reconcile_lock_busy": 0,
                "cross_day_reconcile_deferred": 0,
                "cross_day_reconcile_dead": 0,
                "redispatched_urls": 0,
                "redispatch_jobs": 0,
                "redispatch_blocked": 0,
                "harvest_attempted": 0,
                "harvested": 0,
            }
        )
    if canary_scope is not None:
        # §11 canary 键集合：scope 存在时才追加（普通模式键集合逐字节不变）。
        summary.update(
            {
                "canary_mode": True,
                "only_mall": canary_scope.only_mall,
                "canary_max_urls": canary_scope.max_urls,
                "candidates_seen_total": 0,
                "candidates_in_scope": 0,
                "skipped_by_canary_filter": 0,
                "remote_interaction_urls": 0,
                # Tripwire counters: Phase 1-4 scope enforcement must keep both
                # at 0; a nonzero value means a defensive layer fired.
                "other_remote_tasks_created": 0,
                "other_priority_changed": 0,
                "priority_updates_performed": 0,
            }
        )
    return summary


def _status_of(store: CustomerPrewarmStore, status: str) -> list[dict[str, Any]]:
    return store.candidates(status)


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


def _read_remote_job_readonly(state_db_path: str | Path, job_id: int) -> dict[str, Any] | None:
    """mode=ro probe of one remote_jobs row (plan-only priority planning)."""
    connection = _open_state_db_readonly(state_db_path)
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT id, state, remote_identity FROM remote_jobs WHERE id=?",
            (int(job_id),),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def _read_remote_job_row_readonly(state_db_path: str | Path, job_id: int) -> dict[str, Any] | None:
    """Full ``remote_jobs`` row probe (``mode=ro``) for the cross-day boost
    (Round 3 D4): the row lives in ANOTHER day's state DB and is only ever
    read from here — the owning day's lifecycle stays authoritative."""
    connection = _open_state_db_readonly(state_db_path)
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT * FROM remote_jobs WHERE id=?",
            (int(job_id),),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def _candidate_mall(candidate: Mapping[str, Any], mappings: Sequence[ZhaoshangMallMapping]) -> str:
    mall = str(candidate.get("mall") or "").strip()
    if mall:
        return mall
    return classify_reference_url(str(candidate.get("lookup_url") or ""), mappings).mall_key


def _cross_day_link(
    prewarm_store: Any,
    candidate_id: int,
    business_date: str,
    phase_errors: list[str],
) -> dict[str, Any] | None:
    """Latest candidate linkage when it dates the bound job on ANOTHER day.

    Round 3 D5 guard: a candidate whose latest linkage row names a different
    ``business_date`` has its remote job owned by that day's state DB —
    resolving the bare ``remote_job_id`` against today's DB can hit an
    unrelated same-numbered job.  Returns ``None`` (keep current behavior)
    when there is no linkage, when the linkage is same-day, or when the probe
    itself fails (isolated into ``phase_errors``)."""
    try:
        link = prewarm_store.remote_job_link_for_candidate(int(candidate_id))
    except Exception as exc:  # noqa: BLE001 - probe failure never breaks the pass
        phase_errors.append(
            f"candidate {candidate_id}: remote job link probe: {type(exc).__name__}: {exc}"
        )
        return None
    if link is None:
        return None
    link_date = str(link.get("business_date") or "")
    if not link_date or link_date == business_date:
        return None
    return link


def _cross_day_skip_note(candidate_id: int, link: Mapping[str, Any]) -> str:
    """Phase note for a candidate skipped because its job lives on another day."""
    return (
        f"candidate {int(candidate_id)}: cross-day job "
        f"{link.get('remote_job_id')}@{link.get('business_date')} skipped "
        "(manual recrawl path owns cross-day work)"
    )


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


def _record_remote_job_link(
    prewarm_store: CustomerPrewarmStore,
    business_date: str,
    candidate_id: int,
    job_id: int,
    producer: str,
    phase_errors: list[str],
) -> None:
    """Best-effort candidate↔remote-job linkage (Issue #13 Round 2).

    Pure bookkeeping at every site where a candidate is bound to a remote job
    id; a failure is collected into ``phase_errors`` and never breaks the
    dispatch outcome (and never touches the candidate's cache_status).
    """
    try:
        prewarm_store.record_remote_job_link(
            business_date, int(candidate_id), int(job_id), producer
        )
    except Exception as exc:  # noqa: BLE001 - linkage is best-effort
        phase_errors.append(
            f"candidate {candidate_id} job link {job_id}: {type(exc).__name__}: {exc}"
        )


def validate_canary_preflight(
    plan: Mapping[str, Any],
    canary_scope: CanaryScope,
    mappings: Sequence[ZhaoshangMallMapping],
) -> str | None:
    """Fail-closed gate over one scoped plan-only report (PASS → ``None``).

    顺序即优先级：mall 未映射 > 目标为空 > URL 超限 > 任务超限 > 规划自身
    phase error。  只读 preflight（plan-only 路径先于任何锁/写返回）复用同
    一个 scope predicate，因此同参 plan-only 就是有效预演。
    """
    if not any(m.mall_key == canary_scope.only_mall for m in mappings):
        return CANARY_MALL_NOT_MAPPED
    if int(plan.get("candidates_in_scope", 0) or 0) < 1:
        return CANARY_TARGET_EMPTY
    if int(plan.get("remote_interaction_urls", 0) or 0) > canary_scope.max_urls:
        return CANARY_URL_LIMIT_EXCEEDED
    if int(plan.get("new_remote_jobs_planned", 0) or 0) > canary_scope.max_jobs:
        return CANARY_JOB_LIMIT_EXCEEDED
    if plan.get("phase_errors"):
        return CANARY_PREFLIGHT_FAILED
    return None


def _scope_gate(
    summary: dict[str, Any],
    routing: Any,
    canary_scope: CanaryScope | None,
) -> bool:
    """§12 共享 scope predicate：plan-only 与 live Phase 2 调用同一函数。

    返回该候选是否 in-scope；越界候选（staples/tmall/unmapped/非目标商城）
    在任何 cache 探测或写之前被计数并跳过。
    """
    if canary_scope is None:
        return True
    summary["candidates_seen_total"] += 1
    if routing.outcome != OUTCOME_ZHAOSHANG or not canary_scope.mall_in_scope(
        routing.mall_key
    ):
        summary["skipped_by_canary_filter"] += 1
        return False
    summary["candidates_in_scope"] += 1
    return True


def _plan_only_report(
    prewarm_store: CustomerPrewarmStore,
    shared_cache: Any,
    *,
    business_date: str,
    state_db_path: Path,
    mappings: Sequence[ZhaoshangMallMapping],
    max_jobs_per_pass: int,
    canary_scope: CanaryScope | None = None,
    state_runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build the §26 report with ZERO writes (no locks, no adapter, ro probes)."""
    summary = _empty_summary(business_date, plan_only=True, canary_scope=canary_scope)
    phase_errors = summary["phase_errors"]
    by_mall: dict[str, dict[str, int]] = {}

    def mall_bucket(mall_key: str) -> dict[str, int]:
        return by_mall.setdefault(mall_key, {"reused": 0, "created_urls": 0})

    misses = prewarm_store.list_unique_cache_misses()
    summary["candidates_total"] = len(misses)
    summary["comix_qx_count"] = 0
    summary["ofs_officemate_count"] = 0
    summary["staples_stbchina_count"] = 0
    summary["tmall_manual_skip_count"] = 0
    create_groups: dict[str, list[str]] = {}
    reused_planned = 0
    boostable_reused_planned = 0
    # Job ids already covered by a planned reuse-boost; the PRIORITY_PENDING
    # bucket below must not count the same job twice.
    seen_priority_job_ids: set[int] = set()
    # Canary target identity set: every (mall, exact_url) pair a live pass may
    # interact with remotely (reuse + create + pending-priority confirm).  The
    # live branch carries this stable set as a CanaryLiveBudget, so anything
    # added between preflight and live is CANARY_LIVE_SCOPE_DRIFT.
    target_pairs: set[tuple[str, str]] = set()
    for candidate in misses:
        candidate_id = int(candidate["id"])
        routing = classify_reference_url(str(candidate["lookup_url"] or ""), mappings)
        if not _scope_gate(summary, routing, canary_scope):
            continue  # BEFORE cache recheck — mirrors live zero-write skip
        if routing.outcome == OUTCOME_ZHAOSHANG:
            try:
                hit = shared_cache.lookup_current(routing.mall_key, routing.lookup_url)
            except Exception as exc:  # noqa: BLE001 - probe failure keeps planning
                phase_errors.append(f"recheck candidate {candidate_id}: {exc}")
                continue
            if hit is not None:
                summary["cache_hit_after_recheck"] += 1
                continue
            try:
                latest_link = prewarm_store.remote_job_link_for_candidate(candidate_id)
            except Exception as exc:  # noqa: BLE001 - uncertain link cannot authorize create
                phase_errors.append(f"candidate {candidate_id}: remote link probe failed; create omitted: {type(exc).__name__}: {exc}")
                continue
            if latest_link is not None and str(latest_link.get("business_date") or "") != business_date:
                phase_errors.append(f"candidate {candidate_id}: prior-day remote link present; create omitted")
                continue
            summary["mapped_zhaoshang"] += 1
            if routing.mall_key == "comix" and routing.host.endswith("qx.com"):
                summary["comix_qx_count"] += 1
            elif routing.mall_key == "ofs" and routing.host.endswith("officemate.cn"):
                summary["ofs_officemate_count"] += 1
            probe = find_remote_work_for_url(state_db_path, routing.mall_key, routing.lookup_url)
            bucket = mall_bucket(routing.mall_key)
            unfinished = (probe or {}).get("unfinished_bound")
            if unfinished is not None:
                reused_planned += 1
                bucket["reused"] += 1
                target_pairs.add((routing.mall_key, routing.lookup_url))
                job = unfinished.get("job") or {}
                job_id = int(job.get("id") or 0)
                if job_id:
                    seen_priority_job_ids.add(job_id)
                identity = str(job.get("remote_identity") or "").strip()
                if identity and str(job.get("state") or "") in _BOOSTABLE_JOB_STATES:
                    boostable_reused_planned += 1
            elif (probe or {}).get("terminal_success") or (probe or {}).get("terminal_failed"):
                # Terminal remote work already covers the URL: no new job.
                pass
            else:
                create_groups.setdefault(routing.mall_key, []).append(routing.lookup_url)
                bucket["created_urls"] += 1
                target_pairs.add((routing.mall_key, routing.lookup_url))
        elif routing.outcome == OUTCOME_STAPLES:
            summary["staples_skipped"] += 1
            if routing.host.endswith("stbchina.cn"):
                summary["staples_stbchina_count"] += 1
        elif routing.outcome == OUTCOME_MANUAL_SKIP:
            summary["manual_skipped"] += 1
            if routing.host.endswith("detail.tmall.com"):
                summary["tmall_manual_skip_count"] += 1
        else:
            summary["routing_blocked"] += 1

    # Third bucket: REMOTE_* candidates whose bound job still needs exactly one
    # prioritize call — a PRIORITY_PENDING confirm (Phase 4a, both statuses) or
    # a REMOTE_REUSED boost of a job in _BOOSTABLE_JOB_STATES (Phase 4b).  Every
    # actionable (mall, url) pair joins the canary target set so the live Phase
    # 4 budget guard matches the plan; pending_priority_planned keeps counting
    # only the PENDING confirms.
    pending_priority_planned = 0
    for status in (CACHE_STATUS_REMOTE_DISPATCHED, CACHE_STATUS_REMOTE_REUSED):
        for candidate in prewarm_store.candidates(status):
            if canary_scope is not None:
                summary["candidates_seen_total"] += 1
                if not canary_scope.mall_in_scope(_candidate_mall(candidate, mappings)):
                    continue  # no job read, no priority planning
                summary["candidates_in_scope"] += 1
            job_id = candidate.get("remote_job_id")
            if job_id is None or int(job_id) in seen_priority_job_ids:
                continue
            candidate_id = int(candidate["id"])
            if str(candidate.get("business_date") or "") != business_date:
                try:
                    latest = prewarm_store.remote_job_link_for_candidate(candidate_id)
                except Exception as exc:  # noqa: BLE001 - plan must fail closed
                    phase_errors.append(f"candidate {candidate_id}: link proof unavailable: {type(exc).__name__}: {exc}")
                    continue
                if not (
                    latest is not None
                    and str(latest.get("business_date") or "") == business_date
                    and int(latest.get("remote_job_id") or 0) == int(job_id)
                    and str(latest.get("producer") or "") == PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
                ):
                    if (
                        latest is not None
                        and int(latest.get("remote_job_id") or 0) == int(job_id)
                        and str(latest.get("producer") or "") == PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
                        and str(latest.get("business_date") or "") != business_date
                    ):
                        phase_errors.append(_cross_day_skip_note(candidate_id, latest))
                    else:
                        phase_errors.append(f"candidate {candidate_id}: no exact current-day remote link; priority omitted")
                    continue
            link = _cross_day_link(prewarm_store, candidate_id, business_date, phase_errors)
            if link is not None:
                # D5 (plan-only): never count a cross-day-linked candidate's
                # bare job id against today's state DB.
                phase_errors.append(_cross_day_skip_note(candidate_id, link))
                continue
            job = _read_remote_job_readonly(state_db_path, int(job_id))
            if job is None:
                continue
            job_state = str(job.get("state") or "")
            has_identity = bool(str(job.get("remote_identity") or "").strip())
            if not _job_is_priority_actionable(job_state, has_identity, status):
                continue
            target_pairs.add(
                (_candidate_mall(candidate, mappings), str(candidate.get("lookup_url") or ""))
            )
            if job_state == "PRIORITY_PENDING":
                seen_priority_job_ids.add(int(job_id))
                pending_priority_planned += 1

    planned_jobs = min(len(create_groups), max_jobs_per_pass)
    summary["existing_remote_reused_planned"] = reused_planned
    summary["new_remote_urls_planned"] = sum(len(urls) for urls in create_groups.values())
    summary["new_remote_jobs_planned"] = planned_jobs
    # Every planned priority update needs one prioritize call until -1 is
    # proven: boostable reused jobs, ALL planned new jobs (creates are never
    # assumed -1), and already-dispatched jobs still PRIORITY_PENDING.
    summary["priority_updates_planned"] = (
        boostable_reused_planned + planned_jobs + pending_priority_planned
    )
    summary["by_mall"] = by_mall
    summary["batch_sizes"] = [
        len(create_groups[mall]) for mall in list(create_groups)[:max_jobs_per_pass]
    ]
    summary["existing_remote_reused"] = reused_planned
    summary["new_remote_urls"] = summary["new_remote_urls_planned"]
    if canary_scope is None:
        # WS-A plan summary (normal mode only, strictly read-only probes —
        # plan_lifecycle_actions opens the state DB mode=ro and never writes).
        from app.services.customer_prewarm_lifecycle import plan_lifecycle_actions

        lifecycle_plan = plan_lifecycle_actions(
            state_db_path,
            prewarm_store,
            business_date,
            mappings,
            state_runtime_root=state_runtime_root,
        )
        summary["lifecycle_planned"] = {
            "harvest_jobs": len(lifecycle_plan["harvest_job_ids"]),
            "reconcile_jobs": len(lifecycle_plan["reconcile_jobs"]),
            "cross_day_reconcile_jobs": len(
                lifecycle_plan["cross_day_reconcile_jobs"]
            ),
            "cross_day_harvest_jobs": len(lifecycle_plan["cross_day_harvest_jobs"]),
            "redispatch_urls": sum(
                len(entries) for entries in lifecycle_plan["redispatch_groups"].values()
            ),
            "redispatch_blocked": len(lifecycle_plan["redispatch_blocked"]),
        }
    if canary_scope is not None:
        # §3 口径 = URL 身份集合（durable 且 adapter 无关）：复用 + 新建 + 待
        # 确认优先级的 (mall, exact_url) 对，去重后一个 URL 只计一次。
        summary["canary_target_urls"] = [list(pair) for pair in sorted(target_pairs)]
        summary["remote_interaction_urls"] = len(target_pairs)
        summary["canary_preflight"] = (
            validate_canary_preflight(summary, canary_scope, mappings) or "PASS"
        )
    return summary


def _ensure_batch_state_file(run_dir: Path) -> Path:
    """``<run>/BATCH_STATE.md`` must exist before ``prepare_job``; create empty
    ONLY when absent (STEP1's own writer owns the content)."""
    batch_state = run_dir / "BATCH_STATE.md"
    if not batch_state.is_file():
        batch_state.parent.mkdir(parents=True, exist_ok=True)
        batch_state.touch()
    return batch_state


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


def _job_is_priority_actionable(
    job_state: str, has_identity: bool, cache_status: str | None
) -> bool:
    """统一"可行动交互"判定：live 对一个 REMOTE_* 候选的 bound job 调
    prioritize 当且仅当本 predicate 为真——job 有 remote_identity，且
    state == ``PRIORITY_PENDING``（Phase 4a confirm，两种候选状态都进 confirm
    列表），或候选是 ``REMOTE_REUSED``（只有它进 reuse_items）且
    state ∈ :data:`_BOOSTABLE_JOB_STATES`（Phase 4b boost）。  preflight 第三
    桶与 live Phase 4 前置守卫必须共用同一 predicate，否则预算集合与 live
    交互集合两侧口径漂移。  ``cache_status=None`` 按 REMOTE_REUSED 处理
    （Phase 3 reuse 条目天然是 REMOTE_REUSED 记录）。"""
    if not has_identity:
        return False
    if job_state == "PRIORITY_PENDING":
        return True
    return (cache_status or CACHE_STATUS_REMOTE_REUSED) == CACHE_STATUS_REMOTE_REUSED and (
        job_state in _BOOSTABLE_JOB_STATES
    )


def run_prewarm_dispatch_pass(
    prewarm_store: CustomerPrewarmStore,
    shared_cache: Any,
    *,
    business_date: str,
    runtime_root: str | Path,
    state_runtime_root: str | Path | None = None,
    mappings: Sequence[ZhaoshangMallMapping],
    adapter: Any | None = None,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    max_jobs_per_pass: int = DEFAULT_MAX_JOBS_PER_PASS,
    monotonic: Callable[[], float] | None = None,
    plan_only: bool = False,
    now: Any = None,
    canary_scope: CanaryScope | None = None,
    canary_budget: CanaryLiveBudget | None = None,
) -> dict[str, Any]:
    """Run one bounded dispatch pass and return its intake-style summary.

    ``adapter`` is duck-typed (``prepare_job``/``submit``/``prioritize``); the
    production default ``CnncLegacyZhaoshangAdapter(mappings=...)`` is
    constructed lazily and ONLY when a create or boost actually needs it —
    plan-only and pure-read passes never construct it.  ``now`` is accepted for
    call-site symmetry with the discovery scan; this pass derives all evidence
    from durable state and uses no wall clock.

    ``state_runtime_root`` optionally separates the STEP1 data source (per-day
    ``<date>/state.sqlite3``, per-day lock and zhaoshang job dirs) from
    ``runtime_root`` (prewarm-store / shared-cache base and the dispatcher's
    own instance lock); ``None`` keeps both on ``runtime_root``.

    ``canary_scope`` (``--only-mall``) hard-scopes the ENTIRE pass to one mall:
    the plan-only branch becomes a scoped read-only preflight (same predicate
    as live), and the live branch enforces all four phases through the same
    :class:`CanaryScope` policy object with layered defensive assertions.
    ``canary_budget`` is the live-only hard budget (:class:`CanaryLiveBudget`)
    built from the read-only preflight's ``canary_target_urls``; a live canary
    pass without it fails closed (plan-only ignores the parameter).
    """
    if canary_scope is not None and not plan_only and max_jobs_per_pass > canary_scope.max_jobs:
        raise CanaryScopeViolation(
            f"--max-jobs-per-pass {max_jobs_per_pass} exceeds canary cap {canary_scope.max_jobs}"
        )
    if canary_scope is not None and not plan_only and canary_budget is None:
        # TOCTOU guard: a live canary without the preflight target set could
        # interact with URLs approved by nobody — fail closed before any write.
        raise CanaryScopeViolation("live canary requires a preflight budget")
    monotonic_value = monotonic or time.monotonic
    deadline = monotonic_value() + max(0.0, float(budget_seconds))
    paths = runtime_paths(
        business_date,
        state_runtime_root if state_runtime_root is not None else runtime_root,
    )

    if plan_only:
        return _plan_only_report(
            prewarm_store,
            shared_cache,
            business_date=business_date,
            state_db_path=paths.state_path,
            mappings=mappings,
            max_jobs_per_pass=max_jobs_per_pass,
            canary_scope=canary_scope,
            state_runtime_root=(
                state_runtime_root if state_runtime_root is not None else runtime_root
            ),
        )

    summary = _empty_summary(business_date, plan_only=False, canary_scope=canary_scope)
    phase_errors = summary["phase_errors"]
    counters = summary

    adapter_holder: list[Any] = []

    def get_adapter() -> Any:
        if not adapter_holder:
            adapter_holder.append(
                adapter if adapter is not None else CnncLegacyZhaoshangAdapter(mappings=mappings)
            )
        return adapter_holder[0]

    lock_owner = {
        "pid": os.getpid(),
        "command": "customer_prewarm_dispatch",
        "business_date": business_date,
    }
    instance_lock = ProcessLock(Path(runtime_root) / DISPATCH_LOCK_NAME)
    try:
        instance_lock.acquire(owner=lock_owner)
    except LeaseUnavailable:
        summary["outcome"] = "SKIPPED_ALREADY_RUNNING"
        return summary
    try:
        day_lock = ProcessLock(paths.lock_path)
        try:
            day_lock.acquire(owner=lock_owner)
        except LeaseUnavailable:
            summary["outcome"] = "LOCK_BUSY"
            return summary
        try:
            if not paths.state_path.is_file():
                summary["outcome"] = "STATE_DB_MISSING"
                return summary
            store = DailyAuditStore(paths.state_path)
            if store.get_run(business_date) is None:
                summary["outcome"] = "STATE_DB_MISSING"
                return summary
            try:
                token = store.acquire_lease(
                    business_date,
                    LEASE_OWNER,
                    LEASE_TTL_SECONDS,
                    allow_unexpired_takeover=True,
                    process_lock=day_lock,
                )
            except LeaseUnavailable:
                summary["outcome"] = "LOCK_BUSY"
                return summary
            try:
                _run_leased_pass(
                    prewarm_store=prewarm_store,
                    shared_cache=shared_cache,
                    store=store,
                    token=token,
                    paths=paths,
                    mappings=mappings,
                    get_adapter=get_adapter,
                    deadline=deadline,
                    monotonic_value=monotonic_value,
                    max_jobs_per_pass=max_jobs_per_pass,
                    summary=summary,
                    canary_scope=canary_scope,
                    canary_budget=canary_budget,
                )
            finally:
                try:
                    store.release_lease(token)
                except Exception as exc:  # noqa: BLE001 - release must never mask the pass
                    phase_errors.append(f"lease release: {type(exc).__name__}: {exc}")
        finally:
            day_lock.release()
    finally:
        instance_lock.release()
    return summary


def _run_leased_pass(
    *,
    prewarm_store: CustomerPrewarmStore,
    shared_cache: Any,
    store: DailyAuditStore,
    token: LeaseToken,
    paths: Any,
    mappings: Sequence[ZhaoshangMallMapping],
    get_adapter: Callable[[], Any],
    deadline: float,
    monotonic_value: Callable[[], float],
    max_jobs_per_pass: int,
    summary: dict[str, Any],
    canary_scope: CanaryScope | None = None,
    canary_budget: CanaryLiveBudget | None = None,
) -> None:
    """Absorb → route → reuse/create → priority proof → boost.  Caller owns lease and locks."""
    phase_errors = summary["phase_errors"]
    counters = summary

    # ---- Phase 0 (canary): zero-write live revalidation barrier ----
    # Re-derive the CURRENT actionable target set through the SAME read-only
    # plan logic (_plan_only_report has no write path) and require it to be a
    # subset of the preflight-approved budget.  Anything that grew between
    # preflight and live is drift and must fail closed BEFORE Phase 1 absorbs
    # or mutates any candidate (target shrink is safe and allowed).
    if canary_scope is not None and canary_budget is not None:
        revalidation = _plan_only_report(
            prewarm_store,
            shared_cache,
            business_date=summary["business_date"],
            state_db_path=paths.state_path,
            mappings=mappings,
            max_jobs_per_pass=max_jobs_per_pass,
            canary_scope=canary_scope,
        )
        try:
            summary["canary_revalidated_targets"] = canary_budget.revalidate(
                revalidation.get("canary_target_urls") or []
            )
        except CanaryScopeViolation as violation:
            phase_errors.append(f"live revalidation barrier: {violation}")
            summary["outcome"] = "CANARY_LIVE_SCOPE_DRIFT"
            return

    reuse_items: list[dict[str, Any]] = []
    # confirm_items: REMOTE_* candidates bound to a job that may still be
    # PRIORITY_PENDING — they re-enter the prioritize-only confirm pass (Phase
    # 4a) every pass until weight_type == -1 is proven.
    confirm_items: list[dict[str, Any]] = []
    # jobs_reused must count only NEW reuses wired this pass (phase 3); the
    # absorb phase also feeds reuse_items with boost-retry entries.
    reused_job_ids_this_pass: set[int] = set()

    # ---- Phase 1: absorb REMOTE_* candidates whose cache row has landed ----
    # REMOTE_REUSED candidates whose job still needs its priority also re-enter
    # the boost list every pass: prioritize is durable evidence replay, so the
    # retry is idempotent and an UNCONFIRMED pass is simply retried.
    for status in (CACHE_STATUS_REMOTE_DISPATCHED, CACHE_STATUS_REMOTE_REUSED):
        for candidate in _status_of(prewarm_store, status):
            candidate_id = int(candidate["id"])
            mall = _candidate_mall(candidate, mappings)
            if canary_scope is not None:
                counters["candidates_seen_total"] += 1
                if not canary_scope.mall_in_scope(mall):
                    counters["skipped_by_canary_filter"] += 1
                    continue  # NO cache lookup, NO record, NO reuse/confirm lists
                counters["candidates_in_scope"] += 1
            if not mall:
                continue
            try:
                hit = shared_cache.lookup_current(mall, str(candidate["lookup_url"] or ""))
            except Exception as exc:  # noqa: BLE001 - per-candidate isolation
                phase_errors.append(f"absorb candidate {candidate_id}: {exc}")
                continue
            if hit is not None:
                if _record(
                    prewarm_store,
                    candidate_id,
                    cache_status=CACHE_STATUS_CACHE_HIT,
                    match_level=hit.get("match_level"),
                    resolved_identity_id=int(hit["identity_id"]),
                    phase_errors=phase_errors,
                ):
                    counters["absorbed"] += 1
                continue
            if candidate.get("remote_job_id") is not None:
                if str(candidate.get("business_date") or "") != summary["business_date"]:
                    try:
                        latest = prewarm_store.remote_job_link_for_candidate(candidate_id)
                    except Exception as exc:  # noqa: BLE001 - never trust an unproven bare ID
                        phase_errors.append(f"candidate {candidate_id}: link proof unavailable: {type(exc).__name__}: {exc}")
                        continue
                    if not (
                        latest is not None
                        and str(latest.get("business_date") or "") == summary["business_date"]
                        and int(latest.get("remote_job_id") or 0) == int(candidate["remote_job_id"])
                        and str(latest.get("producer") or "") == PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
                    ):
                        if (
                            latest is not None
                            and int(latest.get("remote_job_id") or 0) == int(candidate["remote_job_id"])
                            and str(latest.get("producer") or "") == PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
                            and str(latest.get("business_date") or "") != summary["business_date"]
                        ):
                            phase_errors.append(_cross_day_skip_note(candidate_id, latest))
                        else:
                            phase_errors.append(f"candidate {candidate_id}: no exact current-day remote link; priority deferred")
                        continue
                link = _cross_day_link(
                    prewarm_store, candidate_id, summary["business_date"], phase_errors
                )
                if link is not None:
                    # D5: the candidate's bound job lives on an earlier day's
                    # state DB.  Resolving the bare id against today's DB could
                    # confirm/boost an unrelated same-numbered job — skip the
                    # candidate from the confirm/reuse lists this pass; the
                    # manual-recrawl exact path owns cross-day work.
                    phase_errors.append(_cross_day_skip_note(candidate_id, link))
                    continue
                item = {
                    "candidate_id": candidate_id,
                    "job_id": int(candidate["remote_job_id"]),
                    "cache_status": status,
                }
                if status == CACHE_STATUS_REMOTE_REUSED:
                    reuse_items.append(item)
                confirm_items.append(item)

    # ---- Phase 2: route PENDING_PREWARM misses into mall groups ----
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in prewarm_store.list_unique_cache_misses():
        candidate_id = int(candidate["id"])
        routing = classify_reference_url(str(candidate["lookup_url"] or ""), mappings)
        if not _scope_gate(summary, routing, canary_scope):
            continue  # BEFORE cache recheck / any record — zero-write skip
        if routing.outcome == OUTCOME_ZHAOSHANG:
            try:
                hit = shared_cache.lookup_current(routing.mall_key, routing.lookup_url)
            except Exception as exc:  # noqa: BLE001 - recheck failure retries next pass
                phase_errors.append(f"recheck candidate {candidate_id}: {exc}")
                continue
            if hit is not None:
                if _record(
                    prewarm_store,
                    candidate_id,
                    cache_status=CACHE_STATUS_CACHE_HIT,
                    match_level=hit.get("match_level"),
                    resolved_identity_id=int(hit["identity_id"]),
                    phase_errors=phase_errors,
                ):
                    counters["cache_hit_after_recheck"] += 1
                continue
            # A prior POST can durably record its job link just before the
            # candidate-status write fails. On the next day the candidate is
            # still PENDING, but today's DB cannot prove the old task absent.
            try:
                latest_link = prewarm_store.remote_job_link_for_candidate(candidate_id)
            except Exception as exc:  # noqa: BLE001 - uncertain provenance is not create permission
                phase_errors.append(
                    f"candidate {candidate_id}: remote link probe failed; create deferred: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if latest_link is not None and str(latest_link.get("business_date") or "") != summary["business_date"]:
                probe = find_remote_work_for_url(paths.state_path, routing.mall_key, routing.lookup_url)
                exact_old_binding = (
                    candidate.get("remote_job_id") is not None
                    and int(latest_link.get("remote_job_id") or 0) == int(candidate["remote_job_id"])
                    and str(latest_link.get("producer") or "") == PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
                )
                if exact_old_binding and ((probe or {}).get("terminal_success") or (probe or {}).get("terminal_failed")):
                    _record(
                        prewarm_store, candidate_id,
                        cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
                        error=(
                            _CACHE_SYNC_PENDING_NOTE if (probe or {}).get("terminal_success")
                            else _TERMINAL_FAILURE_NOTE
                        ),
                        phase_errors=phase_errors,
                    )
                    continue
                phase_errors.append(
                    f"candidate {candidate_id}: prior-day remote link present; create deferred"
                )
                _record(
                    prewarm_store, candidate_id,
                    cache_status=str(candidate["cache_status"]),
                    error="prior-day remote link unresolved; manual review",
                    phase_errors=phase_errors,
                )
                continue
            counters["mapped_zhaoshang"] += 1
            groups.setdefault(routing.mall_key, []).append(
                {"candidate": candidate, "routing": routing}
            )
        elif routing.outcome == OUTCOME_STAPLES:
            if _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_STAPLES_PENDING,
                phase_errors=phase_errors,
            ):
                counters["staples_skipped"] += 1
        elif routing.outcome == OUTCOME_MANUAL_SKIP:
            if _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_MANUAL_SKIP,
                phase_errors=phase_errors,
            ):
                counters["manual_skipped"] += 1
        else:
            if _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_ROUTING_BLOCKED,
                error=f"unmapped mall: {routing.host}" if routing.host else "unmapped mall",
                phase_errors=phase_errors,
            ):
                counters["routing_blocked"] += 1

    # ---- Phase 3: per-mall reuse/create + submit ----
    created_jobs = 0
    for mall_key, entries in groups.items():
        if monotonic_value() >= deadline:
            phase_errors.append(f"budget deadline reached; mall {mall_key} deferred to next pass")
            break
        mapping = next((m for m in mappings if m.mall_key == mall_key), None)
        if mapping is None:
            phase_errors.append(f"mall {mall_key}: no ZhaoshangMallMapping loaded; deferred")
            continue
        try:
            wired = _dispatch_mall_group(
                prewarm_store=prewarm_store,
                store=store,
                token=token,
                paths=paths,
                mapping=mapping,
                entries=entries,
                get_adapter=get_adapter,
                deadline=deadline,
                monotonic_value=monotonic_value,
                max_jobs_per_pass=max_jobs_per_pass,
                created_jobs=created_jobs,
                reuse_items=reuse_items,
                reused_job_ids_this_pass=reused_job_ids_this_pass,
                summary=summary,
                canary_scope=canary_scope,
                canary_budget=canary_budget,
            )
        except CanaryScopeViolation as exc:
            # A canary scope breach in ANY group terminates the WHOLE pass:
            # no further groups and Phase 4 must never run afterwards
            # (canary fail-closed: drift → zero further remote interactions).
            phase_errors.append(f"mall {mall_key}: CanaryScopeViolation: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - per-group isolation
            phase_errors.append(f"mall {mall_key}: {type(exc).__name__}: {exc}")
            continue
        if canary_scope is not None and mall_key != canary_scope.only_mall:
            # Tripwire: the first-line group assertion must have raised long
            # before; a nonzero value means the defensive layer fired.
            counters["other_remote_tasks_created"] += wired
        created_jobs += wired
    # Same-pass confirms of fresh creates happened inside Phase 3; the §3
    # interaction count below must not double-count them (the URL was already
    # counted at create).
    if canary_scope is not None:
        priority_updates_before_phase4 = summary["priority_updates_performed"]

    # ---- Phase 4: priority proof (PRIORITY_PENDING jobs) + reuse-boost ----
    counters["jobs_reused"] = len(reused_job_ids_this_pass)
    # Whole-set pre-priority budget guard: before ANY prioritize call of this
    # phase, every (mall, url) payload pair of the confirm+boost job set must
    # still be inside the preflight budget.  One drifted pair blocks 4a AND 4b
    # entirely (whole-set failure semantics: no target is prioritized first).
    phase4_budget_blocked = False
    if canary_scope is not None and canary_budget is not None:
        # Only entries live would actually prioritize (the same actionable
        # predicate as the preflight target set: PENDING confirms + REMOTE_REUSED
        # boosts with identity) join the whole-set guard — a REMOTE_DISPATCHED
        # candidate over a SUBMITTED job (4a skips, 4b does not own it) makes
        # zero prioritize calls and must never block Phase 4 by itself.
        pending_pairs: set[tuple[str, str]] = set()
        for item in [*confirm_items, *reuse_items]:
            job_row = store.get_remote_job(int(item["job_id"]))
            if job_row is None:
                continue
            if not _job_is_priority_actionable(
                str(job_row.get("state") or ""),
                bool(str(job_row.get("remote_identity") or "").strip()),
                item.get("cache_status"),
            ):
                continue
            for mall_value, url_value in _job_interaction_pairs(job_row):
                pending_pairs.add((str(mall_value), str(url_value)))
        try:
            for pair in sorted(pending_pairs):
                canary_budget.check(*pair)
        except CanaryScopeViolation as violation:
            phase4_budget_blocked = True
            phase_errors.append(f"phase 4 pre-priority budget guard: {violation}")
    if not phase4_budget_blocked:
        # 4a: prove weight_type == -1 for jobs still exactly PRIORITY_PENDING
        # with a remote identity (prioritize-only; never create/submit).
        confirm_targets: dict[int, list[tuple[int, str]]] = {}
        for item in confirm_items:
            confirm_targets.setdefault(int(item["job_id"]), []).append(
                (int(item["candidate_id"]), str(item["cache_status"]))
            )
        confirm_attempted_job_ids: set[int] = set()
        for job_id, targets in confirm_targets.items():
            if monotonic_value() >= deadline:
                phase_errors.append(
                    f"budget deadline reached; priority confirm of job {job_id} deferred"
                )
                break
            if _confirm_pending_priority_job(
                prewarm_store=prewarm_store,
                store=store,
                token=token,
                get_adapter=get_adapter,
                business_date=summary["business_date"],
                note_targets=targets,
                job_id=job_id,
                deadline=deadline,
                monotonic_value=monotonic_value,
                summary=summary,
                canary_scope=canary_scope,
                canary_budget=canary_budget,
            ) is not None:
                confirm_attempted_job_ids.add(job_id)
        # 4b: reuse-boost — idempotent evidence replay for SUBMITTED/RUNNING/
        # EXPORTED reused jobs.  Jobs the confirm pass owned this pass (attempted,
        # confirmed, or budget-deferred) are skipped so one pass never calls
        # prioritize twice on the same job.
        seen_job_ids: set[int] = set()
        for item in reuse_items:
            job_id = int(item["job_id"])
            if job_id in seen_job_ids or job_id in confirm_attempted_job_ids:
                continue
            if monotonic_value() >= deadline:
                phase_errors.append(f"budget deadline reached; boost of job {job_id} deferred")
                break
            seen_job_ids.add(job_id)
            _boost_reused_job(
                prewarm_store=prewarm_store,
                store=store,
                token=token,
                get_adapter=get_adapter,
                business_date=summary["business_date"],
                candidate_id=int(item["candidate_id"]),
                job_id=job_id,
                deadline=deadline,
                monotonic_value=monotonic_value,
                summary=summary,
                canary_scope=canary_scope,
                canary_budget=canary_budget,
            )
    if canary_scope is not None:
        # §3 口径：本 pass 真实发生的远端交互 = 复用 wire + 新建 URL + Phase 4
        # 的 confirm/boost 调用；同 pass 内对新建任务的 confirm 不另计（该 URL
        # 已在 create 处计入）。
        summary["remote_interaction_urls"] = (
            counters["existing_remote_reused"]
            + counters["new_remote_urls"]
            + summary["priority_updates_performed"]
            - priority_updates_before_phase4
        )
        # Canary mode: the whole lifecycle stage is skipped — its summary key
        # set must stay byte-identical and no remote/write side effect may
        # happen outside the preflight budget.
        phase_errors.append("lifecycle skipped in canary mode")
        return

    # ---- Phase 5: lifecycle closeout (reconcile + redispatch) ----------------
    # Normal mode only, with the lease still held (error-isolated like every
    # phase; both stages re-check the same pass deadline before each action).
    if monotonic_value() < deadline:
        try:
            from app.services.customer_prewarm_lifecycle import run_reconcile_phase

            run_reconcile_phase(
                store=store,
                token=token,
                prewarm_store=prewarm_store,
                get_adapter=get_adapter,
                paths=paths,
                deadline=deadline,
                monotonic_value=monotonic_value,
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001 - stage isolation
            phase_errors.append(f"lifecycle reconcile: {type(exc).__name__}: {exc}")
    else:
        phase_errors.append("budget deadline reached; lifecycle reconcile deferred")
    if monotonic_value() < deadline:
        try:
            from app.services.customer_prewarm_lifecycle import run_redispatch_phase

            run_redispatch_phase(
                store=store,
                token=token,
                prewarm_store=prewarm_store,
                paths=paths,
                mappings=mappings,
                get_adapter=get_adapter,
                business_date=summary["business_date"],
                deadline=deadline,
                monotonic_value=monotonic_value,
                max_jobs_per_pass=max_jobs_per_pass,
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001 - stage isolation
            phase_errors.append(f"lifecycle redispatch: {type(exc).__name__}: {exc}")
    else:
        phase_errors.append("budget deadline reached; lifecycle redispatch deferred")


def _dispatch_mall_group(
    *,
    prewarm_store: CustomerPrewarmStore,
    store: DailyAuditStore,
    token: LeaseToken,
    paths: Any,
    mapping: ZhaoshangMallMapping,
    entries: Sequence[Mapping[str, Any]],
    get_adapter: Callable[[], Any],
    deadline: float,
    monotonic_value: Callable[[], float],
    max_jobs_per_pass: int,
    created_jobs: int,
    reuse_items: list[dict[str, int]],
    reused_job_ids_this_pass: set[int],
    summary: dict[str, Any],
    canary_scope: CanaryScope | None = None,
    canary_budget: CanaryLiveBudget | None = None,
) -> int:
    """Reuse-or-create + submit for one mall's candidate group (error-isolated
    by the caller).  Returns the number of remote jobs this group wired."""
    if canary_scope is not None and mapping.mall_key != canary_scope.only_mall:
        # Second defensive layer: an out-of-scope group must never even probe.
        raise CanaryScopeViolation(
            f"canary scope {canary_scope.only_mall!r} violated by mall group {mapping.mall_key!r}"
        )
    phase_errors = summary["phase_errors"]
    counters = summary
    mall_key = mapping.mall_key
    bucket = summary["by_mall"].setdefault(mall_key, {"reused": 0, "created_urls": 0})

    # Canary Pass A (zero-write): probe + budget drift check + classify/count
    # EVERY entry before the write loop below.  This closes the Round-1 gaps —
    # the group cap assertion used to sit AFTER the per-entry reuse records and
    # was skipped entirely by reuse-only groups (empty batch_entries).
    precomputed_probes: dict[str, Any] = {}
    if canary_scope is not None:
        live_interactions = 0
        for entry in entries:
            lookup_url = str(entry["routing"].lookup_url)
            probe = find_remote_work_for_url(paths.state_path, mall_key, lookup_url)
            precomputed_probes[lookup_url] = probe
            unfinished = (probe or {}).get("unfinished_bound") is not None
            terminal = bool(
                (probe or {}).get("terminal_success") or (probe or {}).get("terminal_failed")
            )
            if canary_budget is not None and (unfinished or not terminal):
                # Preflight target set holds only actionable URLs (reuse +
                # create); a terminal-classified entry makes zero remote calls
                # (cache-sync record only), so budget-checking it here would
                # raise a false CANARY_LIVE_SCOPE_DRIFT after an honest PASS.
                canary_budget.check(mall_key, lookup_url)  # drift -> raise, zero-write
            if unfinished or not terminal:
                live_interactions += 1
        if live_interactions > canary_scope.max_urls:
            raise CanaryScopeViolation(
                f"mall {mall_key}: live remote interactions {live_interactions} exceed "
                f"canary max urls {canary_scope.max_urls} (CANARY_LIVE_URL_LIMIT_EXCEEDED)"
            )

    crawl_ids: list[int] = []
    batch_entries: list[Mapping[str, Any]] = []
    reused_here = 0
    for entry in entries:
        candidate = entry["candidate"]
        candidate_id = int(candidate["id"])
        lookup_url = str(entry["routing"].lookup_url)
        probe = (
            precomputed_probes.get(lookup_url)
            if canary_scope is not None
            else find_remote_work_for_url(paths.state_path, mall_key, lookup_url)
        )
        unfinished = (probe or {}).get("unfinished_bound")
        if unfinished is not None:
            job_id = int(unfinished["job"]["id"])
            _record_remote_job_link(
                prewarm_store,
                summary["business_date"],
                candidate_id,
                job_id,
                PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
                phase_errors,
            )
            if _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_REMOTE_REUSED,
                remote_job_id=job_id,
                phase_errors=phase_errors,
            ):
                counters["existing_remote_reused"] += 1
                reused_here += 1
                bucket["reused"] += 1
                reuse_items.append({"candidate_id": candidate_id, "job_id": job_id})
                reused_job_ids_this_pass.add(job_id)
            continue
        if (probe or {}).get("terminal_success"):
            # Cross-day rule (Round 2 review): only a FIRST-EVER binding may be
            # linked under today's business_date.  A remote_job_id the candidate
            # already carried before this pass was bound on an earlier day whose
            # own linkage row stays authoritative — re-linking that job under
            # today would fabricate a same-day dispatch that never happened.
            # This branch binds nothing itself (the probe classifies terminal
            # rows as booleans only), so it records no linkage at all.
            _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
                error=_CACHE_SYNC_PENDING_NOTE,
                phase_errors=phase_errors,
            )
            continue
        if (probe or {}).get("terminal_failed"):
            # Same cross-day rule as terminal_success: a pre-existing binding
            # belongs to its earlier day (authoritative linkage there); this
            # branch binds nothing, so it records no linkage either.
            _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
                error=_TERMINAL_FAILURE_NOTE,
                phase_errors=phase_errors,
            )
            continue
        # Missing or re-plannable work: the idempotent STEP1 create chain owns it.
        sku_id = prewarm_placeholder_sku(mall_key, lookup_url)
        fingerprint = prewarm_crawl_fingerprint(
            mall_key, mapping.source_id, sku_id, lookup_url, summary["business_date"]
        )
        work = store.ensure_crawl_work(
            token,
            mall_key,
            sku_id,
            lookup_url,
            BACKEND_VERSION,
            fingerprint,
            source_id=mapping.source_id,
        )
        crawl_ids.append(int(work["id"]))
        batch_entries.append(entry)

    if not batch_entries:
        return 0
    if canary_scope is not None and reused_here + len(batch_entries) > canary_scope.max_urls:
        # Second defensive layer: the whole group's remote side effects must
        # stay within the canary URL cap — raise BEFORE anything is submitted.
        raise CanaryScopeViolation(
            f"mall {mall_key}: group remote side effects {reused_here + len(batch_entries)} "
            f"exceed canary max urls {canary_scope.max_urls}"
        )
    if created_jobs >= max_jobs_per_pass:
        phase_errors.append(
            f"mall {mall_key}: max jobs per pass ({max_jobs_per_pass}) reached; "
            f"{len(batch_entries)} url(s) deferred to next pass"
        )
        return 0

    requests = [
        CrawlRequest(
            mall_key=mall_key,
            mall_source_id=mapping.source_id,
            mall_name=mapping.mall_name,
            sku_id=prewarm_placeholder_sku(mall_key, str(entry["routing"].lookup_url)),
            url=str(entry["routing"].lookup_url),
            backend_version=BACKEND_VERSION,
        )
        for entry in batch_entries
    ]
    # Mirror legacy_zhaoshang_bridge.finalize byte for byte: fingerprint over
    # sorted identities, 14-digit suffix, cnnc_current_opt task name, job dir.
    task_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "business_date": summary["business_date"],
                "requests": [
                    request.identity
                    for request in sorted(requests, key=lambda value: value.identity)
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    numeric_suffix = f"{int(task_fingerprint, 16) % (10 ** 14):014d}"
    task_name = f"cnnc_current_opt_{requests[0].mall_name}_{numeric_suffix}"
    job_dir = paths.run_dir / "zhaoshang" / mall_key / numeric_suffix
    job_dir.mkdir(parents=True, exist_ok=True)
    batch_state = _ensure_batch_state_file(paths.run_dir)

    job = get_adapter().prepare_job(
        summary["business_date"],
        requests,
        job_dir / "input.xlsx",
        job_dir / "result.xlsx",
        task_name,
        batch_state_path=batch_state,
    )
    payload_json = {
        "requests": [
            {
                "mall_key": request.mall_key,
                "mall_source_id": request.mall_source_id,
                "mall_name": request.mall_name,
                "sku_id": request.sku_id,
                "url": request.url,
                "backend_version": request.backend_version,
            }
            for request in requests
        ],
        "batch_state": str(batch_state),
    }
    job_row = store.prepare_remote_intent(
        token,
        job.logical_job_key,
        job.task_name,
        job.payload_sha256,
        mall_key=job.mall_key,
        input_path=str(job.template_path),
        result_path=str(job.result_path),
        payload_json=payload_json,
    )
    store.link_crawl_work_batch_to_remote_job(token, crawl_ids, int(job_row["id"]))
    store.renew_lease(token, LEASE_TTL_SECONDS)
    counters["jobs_created"] += 1
    counters["new_remote_urls"] += len(batch_entries)
    summary["batch_sizes"].append(len(batch_entries))
    bucket["created_urls"] += len(batch_entries)

    # Pre-send boundary: budget spent between the group check and here must
    # never claim the intent — a real adapter would time out (~0.001s) and the
    # job would be burned into SUBMIT_UNKNOWN although no POST ever started.
    remaining = deadline - monotonic_value()
    if remaining < SUBMIT_MIN_SECONDS:
        phase_errors.append(
            f"mall {mall_key}: budget floor reached before claim; "
            f"job {job_row['id']} stays SUBMIT_INTENT for resume"
        )
        return 1

    claim = store.claim_remote_submission(token, int(job_row["id"]))
    if claim is None:
        # Another writer dispatched this job: never double-POST.
        for entry in batch_entries:
            _record_remote_job_link(
                prewarm_store,
                summary["business_date"],
                int(entry["candidate"]["id"]),
                int(job_row["id"]),
                PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
                phase_errors,
            )
            _record(
                prewarm_store,
                int(entry["candidate"]["id"]),
                cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
                remote_job_id=int(job_row["id"]),
                phase_errors=phase_errors,
            )
        return 1

    remaining = deadline - monotonic_value()
    outcome = get_adapter().submit(job, timeout_seconds=max(0.001, remaining))
    status = _status_value(outcome.status)
    diagnostic = str(getattr(outcome, "diagnostic", "") or "")
    if status == SubmitStatus.SUBMIT_UNKNOWN.value:
        store.mark_submit_unknown(
            token, int(job_row["id"]), f"prewarm dispatch submit unknown: {diagnostic}"
        )
        for entry in batch_entries:
            _record_remote_job_link(
                prewarm_store,
                summary["business_date"],
                int(entry["candidate"]["id"]),
                int(job_row["id"]),
                PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
                phase_errors,
            )
            _record(
                prewarm_store,
                int(entry["candidate"]["id"]),
                cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
                remote_job_id=int(job_row["id"]),
                error=_SUBMIT_UNKNOWN_NOTE,
                phase_errors=phase_errors,
            )
        counters["submit_unknown"] += 1
    elif status == SubmitStatus.NOT_ATTEMPTED.value:
        # Pre-send failure: no dispatch happened, so the candidates stay
        # PENDING_PREWARM truthfully (REMOTE_* → PENDING_PREWARM is forbidden
        # by the transition table) and the unclaimed intent is re-planned by
        # the next pass through the idempotent create chain.
        store.release_remote_submission_claim(
            token,
            int(job_row["id"]),
            f"prewarm dispatch submit not attempted: {diagnostic}",
        )
        counters["submit_not_attempted"] += 1
        phase_errors.append(
            f"mall {mall_key}: {_SUBMIT_NOT_ATTEMPTED_NOTE}"
            + (f" ({diagnostic})" if diagnostic else "")
        )
    elif status == SubmitStatus.SUBMITTED.value:
        task_id = getattr(outcome, "task_id", None)
        task_number = getattr(outcome, "task_number", None)
        if not task_id or not task_number:
            # Fail closed like the bridge: no identity evidence means the
            # outcome is unknown, reconcile-only, never a silent success.
            store.mark_submit_unknown(
                token, int(job_row["id"]), "prewarm dispatch submit returned incomplete identity"
            )
            for entry in batch_entries:
                _record_remote_job_link(
                    prewarm_store,
                    summary["business_date"],
                    int(entry["candidate"]["id"]),
                    int(job_row["id"]),
                    PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
                    phase_errors,
                )
                _record(
                    prewarm_store,
                    int(entry["candidate"]["id"]),
                    cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
                    remote_job_id=int(job_row["id"]),
                    error=_SUBMIT_UNKNOWN_NOTE,
                    phase_errors=phase_errors,
                )
            counters["submit_unknown"] += 1
        else:
            remote_identity = f"{mall_key}|{task_id}|{task_number}"
            # weight_type=-1 is PROVEN, never assumed: submit-time
            # priority_evidence is ignored for state and the job holds at
            # PRIORITY_PENDING until the same-pass prioritize confirm proves it
            # (or the next pass retries via the Phase 4 confirm list).
            store.reconcile_remote_job(
                token, int(job_row["id"]), remote_identity=remote_identity, state="PRIORITY_PENDING"
            )
            store.advance_crawl_work_for_job(token, int(job_row["id"]), "PRIORITY_PENDING")
            for entry in batch_entries:
                _record_remote_job_link(
                    prewarm_store,
                    summary["business_date"],
                    int(entry["candidate"]["id"]),
                    int(job_row["id"]),
                    PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
                    phase_errors,
                )
                _record(
                    prewarm_store,
                    int(entry["candidate"]["id"]),
                    cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
                    remote_job_id=int(job_row["id"]),
                    phase_errors=phase_errors,
                )
            _confirm_pending_priority_job(
                prewarm_store=prewarm_store,
                store=store,
                token=token,
                get_adapter=get_adapter,
                business_date=summary["business_date"],
                note_targets=[
                    (int(entry["candidate"]["id"]), CACHE_STATUS_REMOTE_DISPATCHED)
                    for entry in batch_entries
                ],
                job_id=int(job_row["id"]),
                deadline=deadline,
                monotonic_value=monotonic_value,
                summary=summary,
                prepared=job,
                canary_scope=canary_scope,
                canary_budget=canary_budget,
            )
    else:  # defensive: unknown submit status is treated fail-closed
        store.mark_submit_unknown(
            token, int(job_row["id"]), f"prewarm dispatch submit unknown status: {status}"
        )
        for entry in batch_entries:
            _record_remote_job_link(
                prewarm_store,
                summary["business_date"],
                int(entry["candidate"]["id"]),
                int(job_row["id"]),
                PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
                phase_errors,
            )
            _record(
                prewarm_store,
                int(entry["candidate"]["id"]),
                cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
                remote_job_id=int(job_row["id"]),
                error=_SUBMIT_UNKNOWN_NOTE,
                phase_errors=phase_errors,
            )
        counters["submit_unknown"] += 1
    return 1


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


def _boost_reused_job(
    *,
    prewarm_store: CustomerPrewarmStore,
    store: DailyAuditStore,
    token: LeaseToken,
    get_adapter: Callable[[], Any],
    business_date: str,
    candidate_id: int,
    job_id: int,
    deadline: float,
    monotonic_value: Callable[[], float],
    summary: dict[str, Any],
    canary_scope: CanaryScope | None = None,
    canary_budget: CanaryLiveBudget | None = None,
) -> bool | None:
    """One idempotent priority boost for a reused job (fail-closed).

    Issue #13 Round 3 D3: ``True`` means PROVEN — outcome status accepted
    (``UPDATED``/``ALREADY_HIGHEST``) AND readback ``weight_type == -1``
    (the same honest bar as :func:`_priority_proven` / the confirm path).
    ``False`` = attempted-but-unconfirmed (including status accepted without
    the -1 readback), budget-deferred, canary-blocked, or the failure path —
    the counter ``priority_unconfirmed`` is honest bookkeeping, never a
    proven boost.  ``None`` = not actionable (missing job, missing identity,
    or a non-boostable state the existing lifecycle owns).
    """
    phase_errors = summary["phase_errors"]
    try:
        job_row = store.get_remote_job(job_id)
        if job_row is None:
            return None
        identity = str(job_row.get("remote_identity") or "").strip()
        if not identity or str(job_row.get("state") or "") not in _BOOSTABLE_JOB_STATES:
            return None  # the existing lifecycle owns identity-less / terminal jobs
        adapter = get_adapter()
        prepared = _reconstruct_prepared_job(adapter, business_date, job_row)
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
            # Second defensive layer: all THREE mall evidences must prove the
            # canary target before any prioritize call is made.
            phase_errors.append(
                f"boost job {job_id}: {CANARY_PRIORITY_SCOPE_VIOLATION} "
                f"(job mall evidence does not prove {canary_scope.only_mall!r})"
            )
            return False
        if canary_budget is not None:
            # Second canary layer (per call): the job's durable payload
            # (mall, url) pairs must all sit inside the preflight budget
            # before any prioritize call is made.
            for mall_value, url_value in _job_interaction_pairs(job_row):
                canary_budget.check(mall_value, url_value)
        store.renew_lease(token, LEASE_TTL_SECONDS)
        priority = adapter.prioritize(
            prepared,
            task_id=parts[1],
            task_number=parts[2],
            timeout_seconds=max(0.001, deadline - monotonic_value()),
        )
        if canary_scope is not None:
            summary["priority_updates_performed"] += 1
        if _priority_proven(priority):
            # D3: PROVEN means status accepted AND readback weight_type == -1
            # — a status-accepted-but-weight-unproven outcome takes the
            # unconfirmed path below instead of claiming a proven boost.
            summary["priority_boosted"] += 1
            return True
        summary["priority_unconfirmed"] += 1
        diagnostic = str(getattr(priority, "diagnostic", "") or "highest priority was not confirmed")
        if not getattr(prewarm_store, "_no_candidate_writes", False):
            # D6: the guarded manual path never writes candidate rows; the
            # unconfirmed note is pass-only bookkeeping, so skip the doomed
            # _record round-trip instead of synthesizing RuntimeError
            # phase_errors on every guarded exact-REUSED-unconfirmed pass.
            _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_REMOTE_REUSED,
                remote_job_id=job_id,
                error=f"{_PRIORITY_UNCONFIRMED_PREFIX}: {diagnostic}",
                phase_errors=phase_errors,
            )
        return False
    except Exception as exc:  # noqa: BLE001 - boost failure never fails the pass
        phase_errors.append(f"boost job {job_id}: {type(exc).__name__}: {exc}")
        return False


def _boost_cross_day_job(
    *,
    prewarm_store: Any,
    store: DailyAuditStore,
    token: LeaseToken,
    get_adapter: Callable[[], Any],
    link_date: str,
    job_row: Mapping[str, Any],
    candidate_id: int,
    deadline: float,
    monotonic_value: Callable[[], float],
    summary: dict[str, Any],
) -> bool | None:
    """Read-only priority boost of ANOTHER day's unfinished job (Round 3 D4).

    Same prioritize logic as :func:`_boost_reused_job` with three hard
    differences: the job row comes from the link date's state DB (opened
    read-only — this function never writes that DB and never resolves the job
    against today's DB), the prepared job is reconstructed under ``link_date``
    (the payload's logical key dates from the original day), and no state
    reconciles happen — the owning day's lifecycle stays authoritative and no
    linkage row is written (the existing ``(link_date, candidate_id, job_id)``
    row remains the authoritative binding).  Only TODAY's lease is renewed so
    the long remote call cannot expire the currently held lease.  Tri-state
    return mirrors :func:`_boost_reused_job` (proven ``True`` counts
    ``priority_boosted``; everything else is honest and retried).
    """
    phase_errors = summary["phase_errors"]
    job_id = int(job_row.get("id") or 0)
    try:
        identity = str(job_row.get("remote_identity") or "").strip()
        if not identity or str(job_row.get("state") or "") not in _BOOSTABLE_JOB_STATES:
            return None
        adapter = get_adapter()
        prepared = _reconstruct_prepared_job(adapter, link_date, job_row)
        parts = identity.split("|")
        if (
            len(parts) != 3
            or parts[0] != prepared.mall_key
            or not parts[1]
            or not parts[2]
        ):
            raise ValueError("legacy remote task identity is invalid")
        store.renew_lease(token, LEASE_TTL_SECONDS)
        priority = adapter.prioritize(
            prepared,
            task_id=parts[1],
            task_number=parts[2],
            timeout_seconds=max(0.001, deadline - monotonic_value()),
        )
        if _priority_proven(priority):
            summary["priority_boosted"] += 1
            return True
        summary["priority_unconfirmed"] += 1
        diagnostic = str(getattr(priority, "diagnostic", "") or "highest priority was not confirmed")
        if not getattr(prewarm_store, "_no_candidate_writes", False):
            # D6 guard, same rationale as the same-day boost's unconfirmed
            # branch: the guarded manual path never writes candidate rows.
            _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_REMOTE_REUSED,
                remote_job_id=job_id,
                error=f"{_PRIORITY_UNCONFIRMED_PREFIX}: {diagnostic}",
                phase_errors=phase_errors,
            )
        return False
    except Exception as exc:  # noqa: BLE001 - cross-day boost failure never fails the dispatch
        phase_errors.append(
            f"cross-day boost job {job_id}@{link_date}: {type(exc).__name__}: {exc}"
        )
        return False


# ---- Issue #13 Slice B: guarded single-URL manual recrawl ---------------------


class _NoCandidateWriteStore:
    """Read-candidate proxy over :class:`CustomerPrewarmStore`.

    Manual recrawl NEVER changes a candidate's ``cache_status`` — including
    the indirect path: a boost's unconfirmed branch records a candidate note
    through ``record_candidate_result``.  The class flag ``_no_candidate_writes``
    lets write-shaped helpers (:func:`_boost_reused_job` / Round 3 D6, and the
    cross-day variant) skip that pass-only note bookkeeping up front instead
    of failing here; any candidate write that still reaches this proxy fails
    closed loudly while every other attribute delegates untouched.
    """

    _no_candidate_writes = True

    def __init__(self, store: CustomerPrewarmStore) -> None:
        self._store = store

    def record_candidate_result(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("manual recrawl never writes candidate cache_status")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def dispatch_single_candidate_exact(
    prewarm_store: CustomerPrewarmStore,
    shared_cache: Any,
    *,
    business_date: str,
    runtime_root: str | Path,
    candidate_id: int,
    mappings: Sequence[ZhaoshangMallMapping],
    adapter: Any | None = None,
    budget_seconds: float = 90.0,
    state_runtime_root: str | Path | None = None,
    monotonic: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Dispatch EXACTLY ONE candidate's URL through the existing lifecycle.

    Issue #13 manual recrawl semantics (hard invariants):

    - candidate rows are NEVER written — ``record_candidate_result`` is never
      called, directly or indirectly (``CACHE_HIT`` stays terminal; the shared
      cache and remote lifecycle carry all new evidence).
    - probe first (:func:`find_remote_work_for_url`): an unfinished bound job
      is reused with one idempotent :func:`_boost_reused_job` boost (result
      ``REUSED``); terminal crawl rows mean the URL is finished — per the
      manual-recrawl intent a fresh job is still created exactly once.
    - create: the single-URL batch walks the exact STEP1 chain (placeholder
      SKU, fingerprint, ``cnnc_current_opt_*`` task name, ``BATCH_STATE.md``),
      then claim → submit → honest outcome handling: ``SUBMIT_UNKNOWN`` /
      incomplete identity reconciles only; pre-send ``NOT_ATTEMPTED`` releases
      the burned claim; ``SUBMITTED`` reconciles ``PRIORITY_PENDING`` and gets
      one same-pass :func:`_confirm_pending_priority_job` confirm with
      ``note_targets=[]`` (no candidate writes by construction).

    Locks/lease mirror :func:`run_prewarm_dispatch_pass` byte for byte except
    the instance-lock owner carries ``"invocation": "manual-recrawl"``.  All
    skip outcomes are write-free by construction.  ``shared_cache`` is part of
    the call contract for symmetry; the function itself performs no cache
    writes (cache sync owns absorption, never the dispatcher).
    """
    candidate = prewarm_store.candidate_by_id(int(candidate_id))
    if candidate is None:
        return {"outcome": "CANDIDATE_NOT_FOUND", "phase_errors": []}
    routing = classify_reference_url(str(candidate["original_url"] or ""), mappings)
    if routing.outcome != OUTCOME_ZHAOSHANG:
        return {
            "outcome": "NOT_DISPATCHABLE",
            "reason": routing.outcome,
            "phase_errors": [],
        }

    monotonic_value = monotonic or time.monotonic
    deadline = monotonic_value() + max(0.0, float(budget_seconds))
    effective_state_root = (
        state_runtime_root if state_runtime_root is not None else runtime_root
    )
    paths = runtime_paths(business_date, effective_state_root)
    summary: dict[str, Any] = {
        "outcome": "OK",
        "business_date": business_date,
        "candidate_id": int(candidate_id),
        "mall_key": routing.mall_key,
        "lookup_url": routing.lookup_url,
        "phase_errors": [],
        "priority_confirmed": 0,
        "priority_boosted": 0,
        "priority_unconfirmed": 0,
    }
    phase_errors = summary["phase_errors"]
    guarded_store = _NoCandidateWriteStore(prewarm_store)
    adapter_holder: list[Any] = []

    def get_adapter() -> Any:
        if not adapter_holder:
            adapter_holder.append(
                adapter if adapter is not None else CnncLegacyZhaoshangAdapter(mappings=mappings)
            )
        return adapter_holder[0]

    lock_owner = {
        "pid": os.getpid(),
        "command": "customer_prewarm_dispatch",
        "business_date": business_date,
        "invocation": "manual-recrawl",
    }
    instance_lock = ProcessLock(Path(runtime_root) / DISPATCH_LOCK_NAME)
    try:
        instance_lock.acquire(owner=lock_owner)
    except LeaseUnavailable:
        summary["outcome"] = "SKIPPED_ALREADY_RUNNING"
        return summary
    try:
        day_lock = ProcessLock(paths.lock_path)
        try:
            day_lock.acquire(owner=lock_owner)
        except LeaseUnavailable:
            summary["outcome"] = "LOCK_BUSY"
            return summary
        try:
            if not paths.state_path.is_file():
                summary["outcome"] = "STATE_DB_MISSING"
                return summary
            store = DailyAuditStore(paths.state_path)
            if store.get_run(business_date) is None:
                summary["outcome"] = "STATE_DB_MISSING"
                return summary
            try:
                token = store.acquire_lease(
                    business_date,
                    LEASE_OWNER,
                    LEASE_TTL_SECONDS,
                    allow_unexpired_takeover=True,
                    process_lock=day_lock,
                )
            except LeaseUnavailable:
                summary["outcome"] = "LOCK_BUSY"
                return summary
            try:
                return _dispatch_exact_leased(
                    prewarm_store=guarded_store,
                    store=store,
                    token=token,
                    paths=paths,
                    routing=routing,
                    candidate_id=int(candidate_id),
                    mappings=mappings,
                    get_adapter=get_adapter,
                    business_date=business_date,
                    deadline=deadline,
                    monotonic_value=monotonic_value,
                    summary=summary,
                    state_runtime_root=effective_state_root,
                )
            finally:
                try:
                    store.release_lease(token)
                except Exception as exc:  # noqa: BLE001 - release never masks the result
                    phase_errors.append(f"lease release: {type(exc).__name__}: {exc}")
        finally:
            day_lock.release()
    finally:
        instance_lock.release()


def _dispatch_exact_leased(
    *,
    prewarm_store: Any,
    store: DailyAuditStore,
    token: LeaseToken,
    paths: Any,
    routing: Any,
    candidate_id: int,
    mappings: Sequence[ZhaoshangMallMapping],
    get_adapter: Callable[[], Any],
    business_date: str,
    deadline: float,
    monotonic_value: Callable[[], float],
    summary: dict[str, Any],
    state_runtime_root: str | Path,
) -> dict[str, Any]:
    """Probe → reuse-boost or single-URL create+submit.  Caller owns the lease.

    Return contract (Round 3, consumed by the manual-recrawl runner): every
    ``REUSED`` summary carries ``remote_job_id`` (int), ``remote_identity``
    (str), ``priority_proven`` (True proven / False unconfirmed / None not
    actionable), ``cross_day`` (bool) and ``job_business_date`` (str — the
    business_date of the state DB owning the reused job; equals the action
    day unless ``cross_day`` is True).  ``job_terminal_success: True`` is
    added ONLY when ``priority_proven`` is None and the re-probe found a
    terminal SUCCESS crawl row for the URL (D7).  ``CREATED`` summaries carry
    ``remote_job_id``/``task_name``/``task_number``/``remote_identity``/
    ``priority_confirmed`` and are same-day by construction.
    """
    phase_errors = summary["phase_errors"]
    mall_key = routing.mall_key
    lookup_url = routing.lookup_url

    probe = find_remote_work_for_url(paths.state_path, mall_key, lookup_url)
    unfinished = (probe or {}).get("unfinished_bound")
    if unfinished is not None:
        job = unfinished.get("job") or {}
        job_id = int(job.get("id") or 0)
        _record_remote_job_link(
            prewarm_store,
            business_date,
            candidate_id,
            job_id,
            PRODUCER_MANUAL_RECRAWL_ZHAOSHANG,
            phase_errors,
        )
        boost_proven = _boost_reused_job(
            prewarm_store=prewarm_store,
            store=store,
            token=token,
            get_adapter=get_adapter,
            business_date=business_date,
            candidate_id=candidate_id,
            job_id=job_id,
            deadline=deadline,
            monotonic_value=monotonic_value,
            summary=summary,
        )
        job_terminal_success = False
        if boost_proven is None:
            # D7: the job is no longer boostable (its state moved on between
            # the probe and the boost).  Re-probe before reporting: a terminal
            # SUCCESS crawl row means the URL is actually finished — report
            # that honestly via ``job_terminal_success`` instead of a bare
            # None.  terminal_failed / still-unfinished keeps the plain REUSED
            # + None semantics.
            reprobe = find_remote_work_for_url(paths.state_path, mall_key, lookup_url)
            job_terminal_success = bool((reprobe or {}).get("terminal_success"))
        summary["outcome"] = "REUSED"
        summary["remote_job_id"] = job_id
        summary["remote_identity"] = str(job.get("remote_identity") or "")
        # Issue #13 Round 2/3: surface the honest boost proof (True=proven,
        # False=unconfirmed/failed, None=not actionable) for the caller.
        summary["priority_proven"] = boost_proven
        summary["cross_day"] = False
        summary["job_business_date"] = business_date
        if job_terminal_success:
            summary["job_terminal_success"] = True
        return summary

    # ---- Round 3 D4: cross-day linkage probe (strictly read-only) --------
    # The candidate may be linked to an unfinished job living on an EARLIER
    # day's state DB.  Today's probe cannot see it, so without this check the
    # create chain below would fabricate a duplicate live remote task for the
    # same URL.  The link date's DB is opened read-only; no linkage row is
    # written here (the existing (link_date, candidate, job) row stays
    # authoritative), and the boost is the no-write cross-day variant.
    link = _cross_day_link(prewarm_store, candidate_id, business_date, phase_errors)
    if link is not None:
        link_date = str(link["business_date"])
        link_db = runtime_paths(link_date, state_runtime_root).state_path
        cross_unfinished = (find_remote_work_for_url(link_db, mall_key, lookup_url) or {}).get(
            "unfinished_bound"
        )
        if link.get("remote_job_id") is not None and cross_unfinished is not None:
            job_id = int(cross_unfinished["job"]["id"])
            link_job_row = _read_remote_job_row_readonly(link_db, job_id)
            if link_job_row is not None:
                boost_proven = _boost_cross_day_job(
                    prewarm_store=prewarm_store,
                    store=store,
                    token=token,
                    get_adapter=get_adapter,
                    link_date=link_date,
                    job_row=link_job_row,
                    candidate_id=candidate_id,
                    deadline=deadline,
                    monotonic_value=monotonic_value,
                    summary=summary,
                )
                summary["outcome"] = "REUSED"
                summary["remote_job_id"] = job_id
                summary["remote_identity"] = str(link_job_row.get("remote_identity") or "")
                summary["priority_proven"] = boost_proven
                summary["cross_day"] = True
                summary["job_business_date"] = link_date
                return summary
            # The linked job row vanished from the owning day's DB: the stale
            # linkage is not actionable — fall through to the honest create.
        # Missing link-date DB, no unfinished bound work there, or no job row:
        # treat as no cross-day work and fall through to the normal same-day
        # decision (create per the manual-recrawl intent).

    mapping = next((m for m in mappings if m.mall_key == mall_key), None)
    if mapping is None:
        summary["outcome"] = "MALL_NOT_MAPPED"
        summary["reason"] = f"no ZhaoshangMallMapping loaded for mall {mall_key!r}"
        return summary

    # ---- create: exactly one single-URL job through the idempotent chain ----
    sku_id = prewarm_placeholder_sku(mall_key, lookup_url)
    fingerprint = prewarm_crawl_fingerprint(
        mall_key, mapping.source_id, sku_id, lookup_url, business_date
    )
    work = store.ensure_crawl_work(
        token,
        mall_key,
        sku_id,
        lookup_url,
        BACKEND_VERSION,
        fingerprint,
        source_id=mapping.source_id,
    )
    requests = [
        CrawlRequest(
            mall_key=mall_key,
            mall_source_id=mapping.source_id,
            mall_name=mapping.mall_name,
            sku_id=sku_id,
            url=lookup_url,
            backend_version=BACKEND_VERSION,
        )
    ]
    # Mirror legacy_zhaoshang_bridge.finalize byte for byte (same as the pass).
    # DEFERRED REFACTOR (Issue #13 reviews): this create/submit chain is a
    # consciously kept duplicate of _dispatch_mall_group's — extracting a
    # shared primitive would rewrite the production-frozen pass path mid
    # review round; first refactor target once the branch merges.
    task_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "business_date": business_date,
                "requests": [
                    request.identity
                    for request in sorted(requests, key=lambda value: value.identity)
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    numeric_suffix = f"{int(task_fingerprint, 16) % (10 ** 14):014d}"
    task_name = f"cnnc_current_opt_{requests[0].mall_name}_{numeric_suffix}"
    job_dir = paths.run_dir / "zhaoshang" / mall_key / numeric_suffix
    job_dir.mkdir(parents=True, exist_ok=True)
    batch_state = _ensure_batch_state_file(paths.run_dir)

    job = get_adapter().prepare_job(
        business_date,
        requests,
        job_dir / "input.xlsx",
        job_dir / "result.xlsx",
        task_name,
        batch_state_path=batch_state,
    )
    payload_json = {
        "requests": [
            {
                "mall_key": request.mall_key,
                "mall_source_id": request.mall_source_id,
                "mall_name": request.mall_name,
                "sku_id": request.sku_id,
                "url": request.url,
                "backend_version": request.backend_version,
            }
            for request in requests
        ],
        "batch_state": str(batch_state),
    }
    job_row = store.prepare_remote_intent(
        token,
        job.logical_job_key,
        job.task_name,
        job.payload_sha256,
        mall_key=job.mall_key,
        input_path=str(job.template_path),
        result_path=str(job.result_path),
        payload_json=payload_json,
    )
    job_id = int(job_row["id"])
    summary["remote_job_id"] = job_id
    summary["task_name"] = task_name
    store.link_crawl_work_batch_to_remote_job(token, [int(work["id"])], job_id)
    store.renew_lease(token, LEASE_TTL_SECONDS)

    # Pre-send boundary: below the budget floor the intent stays unclaimed so
    # a real adapter never burns into a fake SUBMIT_UNKNOWN (same as the pass).
    remaining = deadline - monotonic_value()
    if remaining < SUBMIT_MIN_SECONDS:
        phase_errors.append(
            f"manual recrawl: budget floor reached before claim; job {job_id} "
            "stays SUBMIT_INTENT for resume"
        )
        summary["outcome"] = "SUBMIT_DEFERRED"
        summary["note"] = "SUBMIT_INTENT for resume"
        return summary

    claim = store.claim_remote_submission(token, job_id)
    if claim is None:
        # Another writer dispatched this job: never double-POST.
        summary["outcome"] = "CLAIM_LOST"
        return summary

    remaining = deadline - monotonic_value()
    outcome = get_adapter().submit(job, timeout_seconds=max(0.001, remaining))
    status = _status_value(outcome.status)
    diagnostic = str(getattr(outcome, "diagnostic", "") or "")
    task_id = getattr(outcome, "task_id", None)
    task_number = getattr(outcome, "task_number", None)
    if status == SubmitStatus.SUBMIT_UNKNOWN.value or (
        status == SubmitStatus.SUBMITTED.value and (not task_id or not task_number)
    ):
        # Fail closed like the pass: reconcile-only, never a silent success.
        if status == SubmitStatus.SUBMIT_UNKNOWN.value:
            note = f"manual recrawl submit unknown: {diagnostic}"
        else:
            note = "manual recrawl submit returned incomplete identity"
            diagnostic = diagnostic or "incomplete identity"
        store.mark_submit_unknown(token, job_id, note)
        # The job exists (POST may have landed): record the linkage either way.
        _record_remote_job_link(
            prewarm_store,
            business_date,
            candidate_id,
            job_id,
            PRODUCER_MANUAL_RECRAWL_ZHAOSHANG,
            phase_errors,
        )
        summary["outcome"] = "SUBMIT_UNKNOWN"
        summary["diagnostic"] = diagnostic
        return summary
    if status == SubmitStatus.NOT_ATTEMPTED.value:
        # Pre-send failure: no dispatch happened; the unclaimed intent is
        # re-planned by the next call through the idempotent create chain.
        store.release_remote_submission_claim(
            token, job_id, f"manual recrawl submit not attempted: {diagnostic}"
        )
        summary["outcome"] = "SUBMIT_NOT_ATTEMPTED"
        summary["diagnostic"] = diagnostic
        phase_errors.append(
            f"manual recrawl: {_SUBMIT_NOT_ATTEMPTED_NOTE}"
            + (f" ({diagnostic})" if diagnostic else "")
        )
        return summary
    if status == SubmitStatus.SUBMITTED.value:
        remote_identity = f"{mall_key}|{task_id}|{task_number}"
        # weight_type=-1 is PROVEN, never assumed: hold at PRIORITY_PENDING
        # until the same-pass confirm proves it (note_targets=[] -> zero
        # candidate writes by construction).
        store.reconcile_remote_job(
            token, job_id, remote_identity=remote_identity, state="PRIORITY_PENDING"
        )
        store.advance_crawl_work_for_job(token, job_id, "PRIORITY_PENDING")
        _record_remote_job_link(
            prewarm_store,
            business_date,
            candidate_id,
            job_id,
            PRODUCER_MANUAL_RECRAWL_ZHAOSHANG,
            phase_errors,
        )
        confirmed = _confirm_pending_priority_job(
            prewarm_store=prewarm_store,
            store=store,
            token=token,
            get_adapter=get_adapter,
            business_date=business_date,
            note_targets=[],
            job_id=job_id,
            deadline=deadline,
            monotonic_value=monotonic_value,
            summary=summary,
            prepared=job,
        )
        summary["outcome"] = "CREATED"
        summary["task_number"] = task_number
        summary["remote_identity"] = remote_identity
        summary["priority_confirmed"] = bool(confirmed)
        return summary
    # defensive: unknown submit status is treated fail-closed
    store.mark_submit_unknown(
        token, job_id, f"manual recrawl submit unknown status: {status}"
    )
    summary["outcome"] = "SUBMIT_UNKNOWN"
    summary["diagnostic"] = f"unknown submit status: {status}"
    return summary
