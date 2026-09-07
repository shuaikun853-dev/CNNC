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
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

BACKEND_VERSION = "legacy-v1"
DISPATCH_LOCK_NAME = "customer_prewarm_dispatch.lock"
DEFAULT_BUDGET_SECONDS = 50.0
DEFAULT_MAX_JOBS_PER_PASS = 8

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
_PRIORITY_EVIDENCE_STATES = {"updated", "already_highest_priority"}

_CACHE_SYNC_PENDING_NOTE = "cache sync pending: terminal crawl success awaits Phase A cache sync"
_TERMINAL_FAILURE_NOTE = "terminal crawl failure"
_SUBMIT_UNKNOWN_NOTE = "submit unknown; reconcile pending"
_SUBMIT_NOT_ATTEMPTED_NOTE = "submit not attempted (pre-send); retry next pass"
_PRIORITY_UNCONFIRMED_PREFIX = "PRIORITY_UNCONFIRMED"


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


def _empty_summary(business_date: str, plan_only: bool) -> dict[str, Any]:
    return {
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
        "priority_boosted": 0,
        "priority_unconfirmed": 0,
        "phase_errors": [],
        "by_mall": {},
        "batch_sizes": [],
    }


def _status_of(store: CustomerPrewarmStore, status: str) -> list[dict[str, Any]]:
    return store.candidates(status)


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


def _plan_only_report(
    prewarm_store: CustomerPrewarmStore,
    shared_cache: Any,
    *,
    business_date: str,
    state_db_path: Path,
    mappings: Sequence[ZhaoshangMallMapping],
    max_jobs_per_pass: int,
) -> dict[str, Any]:
    """Build the §26 report with ZERO writes (no locks, no adapter, ro probes)."""
    summary = _empty_summary(business_date, plan_only=True)
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
    for candidate in misses:
        candidate_id = int(candidate["id"])
        routing = classify_reference_url(str(candidate["lookup_url"] or ""), mappings)
        if routing.outcome == OUTCOME_ZHAOSHANG:
            try:
                hit = shared_cache.lookup_current(routing.mall_key, routing.lookup_url)
            except Exception as exc:  # noqa: BLE001 - probe failure keeps planning
                phase_errors.append(f"recheck candidate {candidate_id}: {exc}")
                continue
            if hit is not None:
                summary["cache_hit_after_recheck"] += 1
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
                job = unfinished.get("job") or {}
                identity = str(job.get("remote_identity") or "").strip()
                if identity and str(job.get("state") or "") in _BOOSTABLE_JOB_STATES:
                    boostable_reused_planned += 1
            elif (probe or {}).get("terminal_success") or (probe or {}).get("terminal_failed"):
                # Terminal remote work already covers the URL: no new job.
                pass
            else:
                create_groups.setdefault(routing.mall_key, []).append(routing.lookup_url)
                bucket["created_urls"] += 1
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

    planned_jobs = min(len(create_groups), max_jobs_per_pass)
    summary["existing_remote_reused_planned"] = reused_planned
    summary["new_remote_urls_planned"] = sum(len(urls) for urls in create_groups.values())
    summary["new_remote_jobs_planned"] = planned_jobs
    summary["priority_updates_planned"] = boostable_reused_planned + planned_jobs
    summary["by_mall"] = by_mall
    summary["batch_sizes"] = [
        len(create_groups[mall]) for mall in list(create_groups)[:max_jobs_per_pass]
    ]
    summary["existing_remote_reused"] = reused_planned
    summary["new_remote_urls"] = summary["new_remote_urls_planned"]
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


def run_prewarm_dispatch_pass(
    prewarm_store: CustomerPrewarmStore,
    shared_cache: Any,
    *,
    business_date: str,
    runtime_root: str | Path,
    mappings: Sequence[ZhaoshangMallMapping],
    adapter: Any | None = None,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    max_jobs_per_pass: int = DEFAULT_MAX_JOBS_PER_PASS,
    monotonic: Callable[[], float] | None = None,
    plan_only: bool = False,
    now: Any = None,
) -> dict[str, Any]:
    """Run one bounded dispatch pass and return its intake-style summary.

    ``adapter`` is duck-typed (``prepare_job``/``submit``/``prioritize``); the
    production default ``CnncLegacyZhaoshangAdapter(mappings=...)`` is
    constructed lazily and ONLY when a create or boost actually needs it —
    plan-only and pure-read passes never construct it.  ``now`` is accepted for
    call-site symmetry with the discovery scan; this pass derives all evidence
    from durable state and uses no wall clock.
    """
    monotonic_value = monotonic or time.monotonic
    deadline = monotonic_value() + max(0.0, float(budget_seconds))
    paths = runtime_paths(business_date, runtime_root)

    if plan_only:
        return _plan_only_report(
            prewarm_store,
            shared_cache,
            business_date=business_date,
            state_db_path=paths.state_path,
            mappings=mappings,
            max_jobs_per_pass=max_jobs_per_pass,
        )

    summary = _empty_summary(business_date, plan_only=False)
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
) -> None:
    """Absorb → route → reuse/create → boost.  Caller owns lease and locks."""
    phase_errors = summary["phase_errors"]
    counters = summary
    reuse_items: list[dict[str, int]] = []
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
            if (
                status == CACHE_STATUS_REMOTE_REUSED
                and candidate.get("remote_job_id") is not None
            ):
                reuse_items.append(
                    {"candidate_id": candidate_id, "job_id": int(candidate["remote_job_id"])}
                )

    # ---- Phase 2: route PENDING_PREWARM misses into mall groups ----
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in prewarm_store.list_unique_cache_misses():
        candidate_id = int(candidate["id"])
        routing = classify_reference_url(str(candidate["lookup_url"] or ""), mappings)
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
            created_jobs += _dispatch_mall_group(
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
            )
        except Exception as exc:  # noqa: BLE001 - per-group isolation
            phase_errors.append(f"mall {mall_key}: {type(exc).__name__}: {exc}")
            continue

    # ---- Phase 4: reuse-boost for confirmed non-terminal remote jobs ----
    counters["jobs_reused"] = len(reused_job_ids_this_pass)
    seen_job_ids: set[int] = set()
    for item in reuse_items:
        job_id = int(item["job_id"])
        if job_id in seen_job_ids:
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
        )


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
) -> int:
    """Reuse-or-create + submit for one mall's candidate group (error-isolated
    by the caller).  Returns the number of remote jobs this group wired."""
    phase_errors = summary["phase_errors"]
    counters = summary
    mall_key = mapping.mall_key
    bucket = summary["by_mall"].setdefault(mall_key, {"reused": 0, "created_urls": 0})

    crawl_ids: list[int] = []
    batch_entries: list[Mapping[str, Any]] = []
    for entry in entries:
        candidate = entry["candidate"]
        candidate_id = int(candidate["id"])
        lookup_url = str(entry["routing"].lookup_url)
        probe = find_remote_work_for_url(paths.state_path, mall_key, lookup_url)
        unfinished = (probe or {}).get("unfinished_bound")
        if unfinished is not None:
            job_id = int(unfinished["job"]["id"])
            if _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_REMOTE_REUSED,
                remote_job_id=job_id,
                phase_errors=phase_errors,
            ):
                counters["existing_remote_reused"] += 1
                bucket["reused"] += 1
                reuse_items.append({"candidate_id": candidate_id, "job_id": job_id})
                reused_job_ids_this_pass.add(job_id)
            continue
        if (probe or {}).get("terminal_success"):
            _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
                error=_CACHE_SYNC_PENDING_NOTE,
                phase_errors=phase_errors,
            )
            continue
        if (probe or {}).get("terminal_failed"):
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
            state = (
                "SUBMITTED"
                if getattr(outcome, "priority_evidence", None) in _PRIORITY_EVIDENCE_STATES
                else "PRIORITY_PENDING"
            )
            store.reconcile_remote_job(
                token, int(job_row["id"]), remote_identity=remote_identity, state=state
            )
            store.advance_crawl_work_for_job(token, int(job_row["id"]), state)
            for entry in batch_entries:
                _record(
                    prewarm_store,
                    int(entry["candidate"]["id"]),
                    cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
                    remote_job_id=int(job_row["id"]),
                    phase_errors=phase_errors,
                )
    else:  # defensive: unknown submit status is treated fail-closed
        store.mark_submit_unknown(
            token, int(job_row["id"]), f"prewarm dispatch submit unknown status: {status}"
        )
        for entry in batch_entries:
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
) -> None:
    """One idempotent priority boost for a reused job (fail-closed)."""
    phase_errors = summary["phase_errors"]
    try:
        job_row = store.get_remote_job(job_id)
        if job_row is None:
            return
        identity = str(job_row.get("remote_identity") or "").strip()
        if not identity or str(job_row.get("state") or "") not in _BOOSTABLE_JOB_STATES:
            return  # the existing lifecycle owns identity-less / terminal jobs
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
        store.renew_lease(token, LEASE_TTL_SECONDS)
        priority = adapter.prioritize(
            prepared,
            task_id=parts[1],
            task_number=parts[2],
            timeout_seconds=max(0.001, deadline - monotonic_value()),
        )
        status = _status_value(priority.status)
        if status in (PriorityStatus.UPDATED.value, PriorityStatus.ALREADY_HIGHEST.value):
            summary["priority_boosted"] += 1
        else:
            summary["priority_unconfirmed"] += 1
            diagnostic = str(getattr(priority, "diagnostic", "") or "highest priority was not confirmed")
            _record(
                prewarm_store,
                candidate_id,
                cache_status=CACHE_STATUS_REMOTE_REUSED,
                remote_job_id=job_id,
                error=f"{_PRIORITY_UNCONFIRMED_PREFIX}: {diagnostic}",
                phase_errors=phase_errors,
            )
    except Exception as exc:  # noqa: BLE001 - boost failure never fails the pass
        phase_errors.append(f"boost job {job_id}: {type(exc).__name__}: {exc}")
