"""Finite, dependency-injected incremental price-audit tick engine.

The engine deliberately cannot hard-kill a Python callback.  It always passes
the smaller of the configured external timeout, stage allowance and remaining
tick budget to injected workers; adapters must honour that value.  State is
checkpointed before and after every external operation, so a later Task
Scheduler invocation can continue without a daemon or polling loop.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Sequence

from app.services.daily_audit_store import DailyAuditStore, LeaseToken, LeaseUnavailable, ProcessLock
from app.services.saas_audit_core import crawl_fingerprint, detail_fingerprint, model_fingerprint


@dataclass(frozen=True)
class TickConfig:
    business_date: str
    runtime_budget_seconds: float = 720
    heavy_work_reserve_seconds: float = 120
    lease_ttl_seconds: float = 1440
    cutoff_at: datetime | None = None
    detail_max_attempts: int = 3
    detail_max_elapsed_seconds: float = 300
    # The adapter performs its bounded HTTP retries inside one durable claim.
    # A second claim exists only so an uncatchable process crash can replay.
    model_max_attempts: int = 2
    model_max_elapsed_seconds: float = 300
    model_batch_size: int = 10
    external_timeout_seconds: float = 60
    model_name: str = "title-match"
    model_prompt_version: str = "v1"
    model_schema_version: str = "v1"
    detail_schema_version: str = "detail-v1"
    harvest_max_attempts: int = 120
    harvest_max_elapsed_seconds: float = 21600
    harvest_batch_size: int = 6

    def __post_init__(self) -> None:
        numeric = (
            self.runtime_budget_seconds, self.heavy_work_reserve_seconds, self.lease_ttl_seconds,
            self.detail_max_elapsed_seconds, self.model_max_elapsed_seconds,
            self.external_timeout_seconds, self.harvest_max_elapsed_seconds,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("tick limits must be finite")
        if self.runtime_budget_seconds < 0 or self.heavy_work_reserve_seconds < 0:
            raise ValueError("tick runtime budget and reserve must be valid")
        if self.runtime_budget_seconds and self.heavy_work_reserve_seconds >= self.runtime_budget_seconds:
            raise ValueError("heavy-work reserve must be smaller than tick runtime budget")
        if self.lease_ttl_seconds <= 0 or self.external_timeout_seconds <= 0:
            raise ValueError("lease TTL and external timeout must be positive")
        if self.detail_max_attempts <= 0 or self.model_max_attempts <= 0:
            raise ValueError("stage attempt limits must be positive")
        if self.model_batch_size <= 0:
            raise ValueError("model batch size must be positive")
        if self.detail_max_elapsed_seconds <= 0 or self.model_max_elapsed_seconds <= 0:
            raise ValueError("stage elapsed limits must be positive")
        if self.harvest_max_attempts <= 0 or self.harvest_max_elapsed_seconds <= 0:
            raise ValueError("harvest limits must be positive")
        if self.harvest_batch_size <= 0:
            raise ValueError("harvest batch size must be positive")

    @property
    def cutoff_utc(self) -> datetime | None:
        if self.cutoff_at is None:
            return None
        if self.cutoff_at.tzinfo is None or self.cutoff_at.utcoffset() is None:
            raise ValueError("cutoff_at must have an explicit timezone")
        return self.cutoff_at.astimezone(UTC)


@dataclass(frozen=True)
class DiscoveredState:
    observed_at: datetime
    headers: Sequence[str]
    rows: Sequence[Mapping[str, Any]]
    source_fingerprints: Sequence[str] | None = None
    customer_fingerprints: Sequence[str] | None = None
    complete: bool = True
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrawlPlan:
    mall_key: str
    logical_key: str
    task_name: str
    payload_hash: str
    payload_json: Any | None = None
    input_path: str | None = None
    result_path: str | None = None
    members: Sequence["CrawlMember"] = ()


@dataclass(frozen=True)
class CrawlMember:
    mall_key: str
    source_id: str
    sku_id: str
    exact_url: str
    backend_version: str
    row: Mapping[str, Any]


@dataclass
class TickDependencies:
    discover: Callable[[float], DiscoveredState]
    harvest: Callable[[Sequence[Mapping[str, Any]], float], Mapping[int, Mapping[str, Any]] | None] | None = None
    detail: Callable[[Mapping[str, Any], float], Any] | None = None
    model: Callable[[Mapping[str, Any], float], Any] | None = None
    # Optional durable batching seam. The callback returns one result for every
    # zero-based request index; per-item `model` remains for test/legacy use.
    model_batch: Callable[[Sequence[Mapping[str, Any]], float], Mapping[int, Any]] | None = None
    plan_crawl: Callable[[Sequence[Mapping[str, Any]], float], Sequence[CrawlPlan]] | None = None
    # Planner only proposes memberships. This callback must build the immutable
    # task name, payload hash, input manifest/template and payload from exactly
    # the pending members it receives; it must not perform remote writes.
    finalize_crawl_plan: Callable[[CrawlPlan, Sequence[CrawlMember], float], CrawlPlan] | None = None
    submit_crawl: Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]], float], Mapping[str, Any]] | None = None
    monotonic: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass
class TickBudget:
    started_at: float
    total_seconds: float
    reserve_seconds: float
    monotonic: Callable[[], float]

    def remaining(self) -> float:
        return max(0.0, self.total_seconds - (self.monotonic() - self.started_at))

    def can_start_heavy(self) -> bool:
        return self.remaining() > self.reserve_seconds

    def callback_timeout(self, configured_timeout: float, stage_remaining: float | None = None) -> float:
        values = [configured_timeout, self.remaining()]
        if stage_remaining is not None:
            values.append(stage_remaining)
        return max(0.0, min(values))


@dataclass(frozen=True)
class TickSummary:
    outcome: str
    phase_trace: tuple[str, ...]
    counters: Mapping[str, int]
    backlog: Mapping[str, int]
    frozen_snapshot_id: int | None
    error: str | None = None


def _empty_counters() -> dict[str, int]:
    return {
        "recovered_stage_claims": 0, "recovered_submit_unknown": 0, "today_discovered": 0,
        "delta_new": 0, "delta_changed": 0, "detail_requests": 0, "model_attempts": 0,
        "crawl_submitted": 0, "crawl_harvested": 0, "submit_unknown": 0, "crawl_running": 0,
    }


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


def _safe_error(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return f"{type(exc).__name__}(status={code})" if isinstance(code, int) else type(exc).__name__


def _coerce_observed(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("discovered observed_at must have an explicit timezone")
    return value.astimezone(UTC)


def _harvest(store: DailyAuditStore, token: LeaseToken, deps: TickDependencies, budget: TickBudget, counters: dict[str, int], config: TickConfig) -> None:
    jobs = store.list_remote_jobs(token, states={"SUBMITTED", "RUNNING", "EXPORTED", "SUBMIT_UNKNOWN"})[:config.harvest_batch_size]
    if deps.harvest is None:
        return
    if not jobs:
        deps.harvest((), budget.callback_timeout(config.external_timeout_seconds))
        return
    claimed = [
        job for job in (
            store.claim_harvest(token, int(job["id"]), max_attempts=config.harvest_max_attempts, max_elapsed_seconds=config.harvest_max_elapsed_seconds)
            for job in jobs
        ) if job is not None
    ]
    if not claimed:
        return
    store.renew_lease(token, max(1, budget.remaining() + budget.reserve_seconds))
    started = budget.monotonic()
    try:
        enriched_claimed = [
            {**dict(job), "crawl_work": store.crawl_work_for_job(int(job["id"]))}
            for job in claimed
        ]
        result = deps.harvest(enriched_claimed, budget.callback_timeout(config.external_timeout_seconds)) or {}
    except Exception as exc:
        elapsed = budget.monotonic() - started
        for job in claimed:
            store.record_harvest(token, int(job["id"]), elapsed_seconds=elapsed, max_attempts=config.harvest_max_attempts, max_elapsed_seconds=config.harvest_max_elapsed_seconds, error=_safe_error(exc))
        return
    elapsed = budget.monotonic() - started
    for job in claimed:
        job_id = int(job["id"])
        evidence = result.get(job_id) or result.get(str(job_id))
        evidence_elapsed = elapsed
        if isinstance(evidence, Mapping):
            try:
                candidate = float(evidence.get("elapsed_seconds"))
            except (TypeError, ValueError):
                candidate = -1
            if math.isfinite(candidate) and candidate >= 0:
                evidence_elapsed = candidate
        if not isinstance(evidence, Mapping):
            store.record_harvest(token, job_id, elapsed_seconds=elapsed, max_attempts=config.harvest_max_attempts, max_elapsed_seconds=config.harvest_max_elapsed_seconds)
            continue
        if evidence.get("manual_error"):
            store.mark_remote_manual(token, job_id, str(evidence["manual_error"]))
            continue
        crawl_results = evidence.get("crawl_results")
        if isinstance(crawl_results, Mapping):
            for raw_crawl_id, crawl_result in crawl_results.items():
                if isinstance(crawl_result, Mapping) and crawl_result.get("terminal"):
                    store.fail_crawl_work(token, int(raw_crawl_id), str(crawl_result.get("error", "terminal crawler result")), retryable=False)
                else:
                    store.complete_crawl_work(token, int(raw_crawl_id), crawl_result)
                counters["crawl_harvested"] += 1
        state = str(evidence.get("state", "RUNNING"))
        remote_identity = str(evidence.get("remote_identity") or store.get_remote_job(job_id).get("remote_identity") or "")
        if not remote_identity:
            store.record_harvest(token, job_id, elapsed_seconds=evidence_elapsed, max_attempts=config.harvest_max_attempts, max_elapsed_seconds=config.harvest_max_elapsed_seconds)
            continue
        store.reconcile_remote_job(
            token, job_id, remote_identity=remote_identity, state=state,
            export_path=evidence.get("export_path"), result=evidence.get("result"),
        )
        store.advance_crawl_work_for_job(token, job_id, state)
        store.record_harvest(token, job_id, elapsed_seconds=evidence_elapsed, max_attempts=config.harvest_max_attempts, max_elapsed_seconds=config.harvest_max_elapsed_seconds)


def _stage(
    store: DailyAuditStore,
    token: LeaseToken,
    item: Mapping[str, Any],
    stage: str,
    fingerprint: str,
    callback: Callable[[Mapping[str, Any], float], Any] | None,
    budget: TickBudget,
    external_timeout_seconds: float,
    max_attempts: int,
    max_elapsed_seconds: float,
    counters: dict[str, int],
    *,
    retry_caught_errors: bool = True,
) -> bool:
    if not budget.can_start_heavy():
        return False
    work = store.ensure_stage_work(token, int(item["id"]), stage, fingerprint)
    reused = store.reuse_successful_stage_work(token, int(work["id"]))
    if reused is not None:
        work = reused
    if work["state"] == "SUCCESS":
        return True
    if callback is None or not budget.can_start_heavy():
        return False
    claimed = store.claim_stage_work(token, int(work["id"]), max_attempts=max_attempts, max_elapsed_seconds=max_elapsed_seconds)
    if claimed is None:
        return store.get_stage_work(int(work["id"]))["state"] == "SUCCESS"
    started = budget.monotonic()
    timeout = budget.callback_timeout(
        external_timeout_seconds,
        max_elapsed_seconds - float(claimed["elapsed_seconds"]),
    )
    store.renew_lease(token, max(1, budget.remaining() + budget.reserve_seconds))
    try:
        result = callback(item["row"], timeout)
    except Exception as exc:
        elapsed = budget.monotonic() - started
        store.fail_stage_work(
            token, int(work["id"]), elapsed_seconds=elapsed, error=_safe_error(exc),
            retryable=retry_caught_errors, max_attempts=max_attempts,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        return False
    elapsed = budget.monotonic() - started
    if float(claimed["elapsed_seconds"]) + elapsed > max_elapsed_seconds:
        store.fail_stage_work(
            token, int(work["id"]), elapsed_seconds=elapsed,
            error="stage callback exceeded elapsed limit", retryable=retry_caught_errors,
            max_attempts=max_attempts, max_elapsed_seconds=max_elapsed_seconds,
        )
        return False
    store.complete_stage_work(token, int(work["id"]), result)
    counters["detail_requests" if stage == "DETAIL" else "model_attempts"] += 1
    return True


def _stage_model_batches(
    store: DailyAuditStore,
    token: LeaseToken,
    items: Sequence[Mapping[str, Any]],
    config: TickConfig,
    callback: Callable[[Sequence[Mapping[str, Any]], float], Mapping[int, Any]],
    budget: TickBudget,
    counters: dict[str, int],
) -> None:
    """Claim/reuse durable MODEL work and make at most one HTTP call per batch."""
    grouped: dict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
    for item in items:
        if not budget.can_start_heavy():
            break
        fingerprint = model_fingerprint(
            item["row"], config.model_name, config.model_prompt_version, config.model_schema_version,
        )
        work = store.ensure_stage_work(token, int(item["id"]), "MODEL", fingerprint)
        reused = store.reuse_successful_stage_work(token, int(work["id"]))
        if reused is None and work["state"] != "SUCCESS":
            grouped.setdefault(fingerprint, []).append((item, work))

    groups = list(grouped.values())
    for start in range(0, len(groups), config.model_batch_size):
        if not budget.can_start_heavy():
            return
        claimed_groups: list[list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = []
        for group in groups[start:start + config.model_batch_size]:
            if not budget.can_start_heavy():
                break
            claimed: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
            for item, work in group:
                if not budget.can_start_heavy():
                    break
                current = store.claim_stage_work(
                    token, int(work["id"]), max_attempts=config.model_max_attempts,
                    max_elapsed_seconds=config.model_max_elapsed_seconds,
                )
                if current is not None:
                    claimed.append((item, current))
            if claimed:
                claimed_groups.append(claimed)
        if not claimed_groups:
            continue
        stage_remaining = min(
            config.model_max_elapsed_seconds - float(work["elapsed_seconds"])
            for group in claimed_groups for _item, work in group
        )
        timeout = budget.callback_timeout(config.external_timeout_seconds, stage_remaining)
        started = budget.monotonic()
        store.renew_lease(token, max(1, budget.remaining() + budget.reserve_seconds))
        try:
            results = callback([group[0][0]["row"] for group in claimed_groups], timeout)
            expected_indexes = set(range(len(claimed_groups)))
            if set(results) != expected_indexes:
                raise ValueError("model_batch must return exactly one result for every request index")
        except Exception as exc:
            elapsed = budget.monotonic() - started
            for group in claimed_groups:
                for _item, work in group:
                    store.fail_stage_work(
                        token, int(work["id"]), elapsed_seconds=elapsed, error=_safe_error(exc), retryable=False,
                        max_attempts=config.model_max_attempts, max_elapsed_seconds=config.model_max_elapsed_seconds,
                    )
            continue
        elapsed = budget.monotonic() - started
        for index, group in enumerate(claimed_groups):
            for _item, work in group:
                if float(work["elapsed_seconds"]) + elapsed > config.model_max_elapsed_seconds:
                    store.fail_stage_work(
                        token, int(work["id"]), elapsed_seconds=elapsed, error="stage callback exceeded elapsed limit",
                        retryable=False, max_attempts=config.model_max_attempts,
                        max_elapsed_seconds=config.model_max_elapsed_seconds,
                    )
                else:
                    store.complete_stage_work(token, int(work["id"]), results[index])
        counters["model_attempts"] += 1


def _submit(
    store: DailyAuditStore,
    token: LeaseToken,
    eligible_items: Sequence[Mapping[str, Any]],
    config: TickConfig,
    deps: TickDependencies,
    budget: TickBudget,
    counters: dict[str, int],
) -> None:
    if deps.submit_crawl is None or not budget.can_start_heavy():
        return

    def dispatch(job: Mapping[str, Any], crawls: Sequence[Mapping[str, Any]]) -> None:
        if not budget.can_start_heavy():
            return
        claim = store.claim_remote_submission(token, int(job["id"]))
        if claim is None:
            return
        store.renew_lease(token, max(1, budget.remaining() + budget.reserve_seconds))
        try:
            result = deps.submit_crawl(claim, crawls, budget.callback_timeout(config.external_timeout_seconds))
        except Exception as exc:
            store.mark_submit_unknown(token, int(job["id"]), _safe_error(exc))
            counters["submit_unknown"] += 1
            return
        remote_identity = str(result.get("remote_identity") or result.get("task_id") or "")
        if not remote_identity:
            store.mark_submit_unknown(token, int(job["id"]), "submit response had no remote identity")
            counters["submit_unknown"] += 1
            return
        state = str(result.get("state", "SUBMITTED"))
        store.reconcile_remote_job(token, int(job["id"]), remote_identity=remote_identity, state=state, result=result)
        store.advance_crawl_work_for_job(token, int(job["id"]), state)
        counters["crawl_submitted"] += 1

    # An interrupted tick may have committed its immutable intent and all
    # crawl bindings before it claimed POST dispatch. Resume that exact intent;
    # never rebuild a template or send a second logical job.
    for pending_intent in store.list_remote_jobs(token, states={"SUBMIT_INTENT"}):
        if not budget.can_start_heavy():
            return
        bound = store.crawl_work_for_job(int(pending_intent["id"]))
        if bound:
            dispatch(pending_intent, bound)
    if deps.plan_crawl is None or not budget.can_start_heavy():
        return
    store.renew_lease(token, max(1, budget.remaining() + budget.reserve_seconds))
    plans = deps.plan_crawl(eligible_items, budget.callback_timeout(config.external_timeout_seconds))
    for plan in plans:
        if not budget.can_start_heavy():
            return
        crawls: list[Mapping[str, Any]] = []
        pending_members: list[CrawlMember] = []
        seen_crawl_ids: set[int] = set()
        for member in plan.members:
            if not budget.can_start_heavy():
                return
            fingerprint = crawl_fingerprint(member.row, member.mall_key, config.business_date, member.backend_version)
            crawl = store.ensure_crawl_work(
                token, member.mall_key, member.sku_id, member.exact_url, member.backend_version, fingerprint, source_id=member.source_id,
            )
            reused_crawl = store.reuse_successful_crawl_work(token, int(crawl["id"]))
            if reused_crawl is not None:
                crawl = reused_crawl
            if (
                int(crawl["id"]) not in seen_crawl_ids
                and crawl["remote_job_id"] is None
                and crawl["state"] in {"PENDING", "RETRYABLE_FAILED"}
            ):
                crawls.append(crawl)
                seen_crawl_ids.add(int(crawl["id"]))
                pending_members.append(member)
        if not crawls:
            continue
        if deps.finalize_crawl_plan is None:
            # A full proposal is safe only when it already equals the durable
            # request multiset. Otherwise old members could leak into a new
            # template before the submit callback sees the filtered crawls.
            proposal_ids = {(member.mall_key, member.source_id, member.sku_id, member.exact_url.strip(), member.backend_version) for member in plan.members}
            pending_ids = {(member.mall_key, member.source_id, member.sku_id, member.exact_url.strip(), member.backend_version) for member in pending_members}
            if proposal_ids != pending_ids or len(plan.members) != len(pending_members):
                continue
        else:
            plan = deps.finalize_crawl_plan(plan, tuple(pending_members), budget.callback_timeout(config.external_timeout_seconds))
            if tuple(plan.members) != tuple(pending_members):
                raise ValueError("finalize_crawl_plan must preserve the exact pending member sequence")
            # Plan finalization may write a local immutable template.  Do not
            # turn that local work into a durable remote intent once the tick
            # has crossed its heavy-work reserve; the next tick can reuse it.
            if not budget.can_start_heavy():
                return
        job = store.prepare_remote_intent(
            token, plan.logical_key, plan.task_name, plan.payload_hash, mall_key=plan.mall_key,
            input_path=plan.input_path, result_path=plan.result_path, payload_json=plan.payload_json,
        )
        crawls = store.link_crawl_work_batch_to_remote_job(token, [int(crawl["id"]) for crawl in crawls], int(job["id"]))
        dispatch(job, crawls)


def _backlog(
    store: DailyAuditStore,
    token: LeaseToken,
    snapshot_items: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, int]:
    stages = store.list_stage_work(token)
    crawls = store.list_crawl_work(token)
    jobs = store.list_remote_jobs(token)
    missing_stages = 0
    if snapshot_items is not None:
        current_item_ids = {int(item["id"]) for item in snapshot_items}
        established = {
            (int(work["audit_item_id"]), str(work["stage"]))
            for work in stages
        }
        missing_stages = sum(
            (audit_item_id, stage) not in established
            for audit_item_id in current_item_ids
            for stage in ("DETAIL", "MODEL")
        )
    return {
        "stage_pending": missing_stages + sum(
            work["state"] in {"PENDING", "CLAIMED", "RETRYABLE_FAILED"}
            for work in stages
        ),
        "stage_terminal": sum(work["state"] == "TERMINAL_FAILED" for work in stages),
        "crawl_pending": sum(work["state"] in {"PENDING", "RUNNING", "RETRYABLE_FAILED", "SUBMITTED", "EXPORTED", "SUBMIT_UNKNOWN"} for work in crawls),
        "remote_pending": sum(job["state"] in {"SUBMITTED", "RUNNING", "EXPORTED"} for job in jobs),
        "remote_unknown": sum(job["state"] == "SUBMIT_UNKNOWN" for job in jobs),
        "remote_manual": sum(job["state"] == "MANUAL_REQUIRED" for job in jobs),
    }


def run_incremental_tick(
    store: DailyAuditStore,
    process_lock: ProcessLock,
    config: TickConfig,
    deps: TickDependencies,
    *,
    finalize: bool = False,
) -> TickSummary:
    """Run one bounded tick; it never starts a daemon or waits for submitted jobs."""
    counters = _empty_counters()
    trace: list[str] = []
    cutoff = config.cutoff_utc
    now = deps.wall_clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("wall_clock must return an explicit-timezone datetime")
    if finalize and cutoff is not None and now.astimezone(UTC) < cutoff:
        return TickSummary("FINALIZE_NOT_READY", tuple(trace), counters, {}, None)
    try:
        process_lock.acquire()
    except LeaseUnavailable:
        return TickSummary("ALREADY_RUNNING", tuple(trace), counters, {}, None)

    token: LeaseToken | None = None
    snapshot_items: Sequence[Mapping[str, Any]] | None = None
    try:
        store.ensure_run(config.business_date, cutoff_at=cutoff.isoformat(timespec="seconds") if cutoff else None)
        try:
            token = store.acquire_lease(
                config.business_date, f"tick-{id(process_lock)}",
                max(5, config.lease_ttl_seconds, config.runtime_budget_seconds + config.heavy_work_reserve_seconds),
                allow_unexpired_takeover=True,
                process_lock=process_lock,
            )
        except LeaseUnavailable:
            return TickSummary("ALREADY_RUNNING", tuple(trace), counters, {}, None)
        budget = TickBudget(deps.monotonic(), config.runtime_budget_seconds, config.heavy_work_reserve_seconds, deps.monotonic)
        trace.append("recover")
        counters["recovered_stage_claims"] = store.recover_claimed_stage_work(token)
        counters["recovered_submit_unknown"] = store.recover_ambiguous_remote_submissions(token)
        if config.runtime_budget_seconds == 0:
            trace.append("checkpoint")
            backlog = _backlog(store, token)
            store.update_daily_metrics(token, counters)
            return TickSummary("BUDGET_EXHAUSTED", tuple(trace), counters, backlog, None)
        trace.append("harvest")
        _harvest(store, token, deps, budget, counters, config)
        if not budget.can_start_heavy():
            trace.append("checkpoint")
            backlog = _backlog(store, token)
            store.update_daily_metrics(token, counters)
            frozen = store.get_run(config.business_date)
            return TickSummary(
                "BUDGET_EXHAUSTED", tuple(trace), counters, backlog,
                frozen["frozen_snapshot_id"] if frozen else None,
            )

        run = store.get_run(config.business_date)
        assert run is not None
        snapshot: Mapping[str, Any] | None = None
        if run["frozen_snapshot_id"] is not None:
            snapshot = store.get_snapshot(int(run["frozen_snapshot_id"]))
        else:
            existing_cutoff_snapshot = (
                store.first_snapshot_at_or_after(config.business_date, cutoff.isoformat(timespec="seconds"))
                if finalize and cutoff is not None else None
            )
            if existing_cutoff_snapshot is not None:
                run = store.freeze_snapshot(token, int(existing_cutoff_snapshot["id"]))
                snapshot = existing_cutoff_snapshot
            else:
                trace.append("discover")
                discovered = deps.discover(budget.callback_timeout(config.external_timeout_seconds))
                if not discovered.complete:
                    return TickSummary("DISCOVERY_INCOMPLETE", tuple(trace), counters, _backlog(store, token), None)
                observed = _coerce_observed(discovered.observed_at)
                ingested = store.ingest_snapshot(
                    token, list(discovered.rows), observed.isoformat(timespec="seconds"),
                    source_fingerprints=discovered.source_fingerprints, customer_fingerprints=discovered.customer_fingerprints,
                    headers=discovered.headers, evidence=discovered.evidence,
                )
                snapshot = ingested["snapshot"]
                counters["today_discovered"] = int(ingested["member_count"])
                counters["delta_new"] = int(ingested["status_counts"].get("NEW", 0))
                counters["delta_changed"] = int(ingested["status_counts"].get("CHANGED", 0))
                if finalize and cutoff is not None:
                    first = store.first_snapshot_at_or_after(config.business_date, cutoff.isoformat(timespec="seconds"))
                    if first is None:
                        return TickSummary("FINALIZE_SNAPSHOT_NOT_READY", tuple(trace), counters, _backlog(store, token), None)
                    run = store.freeze_snapshot(token, int(first["id"]))
                    snapshot = first
        assert snapshot is not None
        snapshot_id = int(snapshot["id"])
        items = store.ordered_snapshot_items(snapshot_id)
        snapshot_items = items

        trace.append("detail")
        for item in items:
            if not budget.can_start_heavy():
                break
            if not _stage(
                store, token, item, "DETAIL", detail_fingerprint(item["row"], config.business_date, config.detail_schema_version),
                deps.detail, budget, config.external_timeout_seconds, config.detail_max_attempts, config.detail_max_elapsed_seconds, counters,
            ):
                continue
        trace.append("model")
        if deps.model_batch is not None:
            _stage_model_batches(store, token, items, config, deps.model_batch, budget, counters)
        else:
            for item in items:
                if not budget.can_start_heavy():
                    break
                _stage(
                    store, token, item, "MODEL",
                    model_fingerprint(item["row"], config.model_name, config.model_prompt_version, config.model_schema_version),
                    deps.model, budget, config.external_timeout_seconds, config.model_max_attempts, config.model_max_elapsed_seconds, counters,
                    retry_caught_errors=False,
                )
        trace.append("submit")
        if budget.can_start_heavy():
            model_by_item = {int(work["audit_item_id"]): work for work in store.list_stage_work(token, stage="MODEL")}
            eligible = [item for item in items if model_by_item.get(int(item["id"]), {}).get("state") == "SUCCESS"]
            _submit(store, token, eligible, config, deps, budget, counters)
        trace.append("checkpoint")
        backlog = _backlog(store, token, snapshot_items)
        if backlog["stage_terminal"] or backlog["remote_manual"]:
            outcome = "MANUAL_REVIEW_REQUIRED"
        elif backlog["remote_unknown"]:
            outcome = "WAITING_REMOTE_RECONCILIATION"
        elif backlog["remote_pending"]:
            outcome = "WAITING_REMOTE"
        elif not budget.can_start_heavy() and (backlog["stage_pending"] or backlog["crawl_pending"]):
            outcome = "BUDGET_EXHAUSTED"
        elif backlog["stage_pending"] or backlog["crawl_pending"]:
            outcome = "PENDING_RETRY"
        else:
            outcome = "COMPLETED"
        counters["crawl_running"] = backlog["crawl_pending"]
        store.update_daily_metrics(token, {**counters, "final_pending": sum(backlog.values())})
        final_run = store.get_run(config.business_date)
        return TickSummary(outcome, tuple(trace), counters, backlog, final_run["frozen_snapshot_id"] if final_run else None)
    except Exception as exc:
        if token is None:
            return TickSummary("FAILED", tuple(trace), counters, {}, None, _safe_error(exc))
        try:
            return TickSummary(
                "FAILED", tuple(trace), counters,
                _backlog(store, token, snapshot_items), None, _safe_error(exc),
            )
        except BaseException:
            return TickSummary("FAILED", tuple(trace), counters, {}, None, _safe_error(exc))
    finally:
        if token is not None:
            store.release_lease(token)
        process_lock.release()
