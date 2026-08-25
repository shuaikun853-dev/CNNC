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
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping, Sequence
from urllib import error

from app.services.daily_audit_store import DailyAuditStore, LeaseToken, LeaseUnavailable, ProcessLock
from app.services.remote_running_recovery import (
    RECOVERY_CALLBACK_TIMEOUT_SECONDS,
    run_running_recovery_pass,
)
from app.services.saas_audit_core import crawl_fingerprint, detail_fingerprint, model_fingerprint
from app.services.title_match_model_adapter import (
    ModelCallRetryExhaustedError,
    ModelResponseContractError,
)


class SubmitNotAttemptedError(RuntimeError):
    """A submit callback provably never dispatched the remote create.

    The burn-then-send claim discipline still applies: the workflow must
    durably record that no POST could have started, so the next tick may
    safely reclaim the same immutable intent. Anything merely uncertain
    must keep raising so it lands in SUBMIT_UNKNOWN instead.
    """


@dataclass(frozen=True)
class TickConfig:
    business_date: str
    runtime_budget_seconds: float = 720
    heavy_work_reserve_seconds: float = 120
    lease_ttl_seconds: float = 1440
    cutoff_at: datetime | None = None
    detail_max_attempts: int = 3
    detail_max_elapsed_seconds: float = 300
    # NOTE: every *_max_elapsed_seconds budget below (model, harvest, priority)
    # measures cumulative callback ACTIVE time — the sum of per-claim elapsed
    # recorded by fail_stage_work/record_harvest while a callback actually
    # ran.  It is NOT the wall-clock age of the work since its first attempt:
    # scheduler gaps, backoff windows and process downtime never consume it.
    # The adapter performs its bounded HTTP retries inside one durable claim.
    # The durable replay window spans several scheduler passes so a short
    # provider outage (transport error or model-response contract drift)
    # stays recoverable; claims are spaced by an exponential backoff derived
    # from the durable failure timestamp.
    model_max_attempts: int = 8
    model_max_elapsed_seconds: float = 900
    model_batch_size: int = 10
    external_timeout_seconds: float = 60
    snapshot_discovery_timeout_seconds: float = 180
    model_name: str = "title-match"
    model_prompt_version: str = "v1"
    model_schema_version: str = "v1"
    detail_schema_version: str = "detail-v1"
    harvest_max_attempts: int = 120
    harvest_max_elapsed_seconds: float = 21600
    # Only adapter-confirmed retryable Priority failures (transient transport,
    # rows not yet materialized) consume this budget; non-retryable failures go
    # MANUAL on first evidence.  The window matches the harvest budget so a
    # remotely slow task keeps probing instead of blocking the day's closeout;
    # like harvest_max_elapsed_seconds it counts cumulative probe callback
    # time, not the wall-clock age of the pending job.
    priority_retry_max_attempts: int = 120
    priority_retry_max_elapsed_seconds: float = 21600
    harvest_batch_size: int = 6
    # Authoritative observations with identical (row_count, ordered_hash) must
    # span at least this long before a finalize freeze may trust them.  A single
    # complete pagination only proves one fetch was internally consistent.
    source_stability_seconds: float = 600

    def __post_init__(self) -> None:
        numeric = (
            self.runtime_budget_seconds, self.heavy_work_reserve_seconds, self.lease_ttl_seconds,
            self.detail_max_elapsed_seconds, self.model_max_elapsed_seconds,
            self.external_timeout_seconds, self.snapshot_discovery_timeout_seconds,
            self.harvest_max_elapsed_seconds, self.priority_retry_max_elapsed_seconds,
            self.source_stability_seconds,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("tick limits must be finite")
        if self.runtime_budget_seconds < 0 or self.heavy_work_reserve_seconds < 0:
            raise ValueError("tick runtime budget and reserve must be valid")
        if self.runtime_budget_seconds and self.heavy_work_reserve_seconds >= self.runtime_budget_seconds:
            raise ValueError("heavy-work reserve must be smaller than tick runtime budget")
        if (
            self.lease_ttl_seconds <= 0
            or self.external_timeout_seconds <= 0
            or self.snapshot_discovery_timeout_seconds <= 0
        ):
            raise ValueError("lease TTL and external timeouts must be positive")
        if self.detail_max_attempts <= 0 or self.model_max_attempts <= 0:
            raise ValueError("stage attempt limits must be positive")
        if self.model_batch_size <= 0:
            raise ValueError("model batch size must be positive")
        if self.detail_max_elapsed_seconds <= 0 or self.model_max_elapsed_seconds <= 0:
            raise ValueError("stage elapsed limits must be positive")
        if (
            self.harvest_max_attempts <= 0
            or self.harvest_max_elapsed_seconds <= 0
            or self.priority_retry_max_attempts <= 0
            or self.priority_retry_max_elapsed_seconds <= 0
        ):
            raise ValueError("harvest limits must be positive")
        if self.harvest_batch_size <= 0:
            raise ValueError("harvest batch size must be positive")
        if self.source_stability_seconds < 0:
            raise ValueError("source stability window must be zero or positive")

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
    # Durable remote-stall circuit breaker. A non-empty reason must stop this
    # tick before it claims or dispatches any new remote submission; existing
    # remote jobs stay harvestable so probes and exports continue.
    submit_gate: Callable[[], str | None] | None = None
    # RUNNING-stall recovery seams. ``remote_recovery_probe`` is read-only and
    # appends durable progress evidence; ``remote_recovery_restart`` performs
    # the single exact disable->enable and is only wired under explicit
    # remote-write authorization. Both receive a timeout derived from the
    # tick's live remaining budget at call time. Both default to absent: the
    # pass is a no-op.
    remote_recovery_probe: Callable[[Mapping[str, Any], float], Mapping[str, Any] | None] | None = None
    remote_recovery_restart: Callable[[Mapping[str, Any], str | None, float], Mapping[str, Any]] | None = None
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
        "harvest_batches": 0, "harvest_swept": 0, "submit_paused": 0, "submit_not_attempted": 0,
        "customer_count": 0, "saas_count": 0, "source_count_gap": 0,
    }


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


def _safe_error(exc: BaseException) -> str:
    # This typed pre-send message is built only from the legacy adapter's
    # fixed diagnostic vocabulary, so it can be persisted verbatim.
    if isinstance(exc, SubmitNotAttemptedError):
        return str(exc)
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return f"{type(exc).__name__}(status={code})"
    attempts = getattr(exc, "attempts", None)
    last_cause = getattr(exc, "last_cause", None)
    if isinstance(attempts, int) and hasattr(exc, "elapsed_seconds"):
        cause_name = type(last_cause).__name__ if isinstance(last_cause, BaseException) else "deadline"
        cause_code = getattr(last_cause, "code", None)
        status = f",status={cause_code}" if isinstance(cause_code, int) else ""
        return f"{type(exc).__name__}(attempts={attempts},last_cause={cause_name}{status})"
    return type(exc).__name__


def _coerce_observed(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("discovered observed_at must have an explicit timezone")
    return value.astimezone(UTC)


def source_stability_seconds(
    store: DailyAuditStore, business_date: str, snapshot: Mapping[str, Any]
) -> float:
    """Seconds the snapshot's content has been observed unchanged, ending at it.

    Uses only the day's authoritative observations already persisted by
    production discovery.  A single observation has zero stability; an
    interleaved observation with different content restarts the timer because
    the upstream source demonstrably changed after that run started.
    """
    observations = store.list_snapshots(business_date)
    position = next(
        (
            index
            for index, observed in enumerate(observations)
            if int(observed["id"]) == int(snapshot["id"])
        ),
        None,
    )
    if position is None:
        return 0.0
    identity = (int(snapshot["row_count"]), str(snapshot["ordered_hash"]))
    started_at: datetime | None = None
    latest_at: datetime | None = None
    for observed in reversed(observations[: position + 1]):
        if (int(observed["row_count"]), str(observed["ordered_hash"])) != identity:
            break
        moment = datetime.fromisoformat(str(observed["observed_at"]))
        if latest_at is None:
            latest_at = moment
        started_at = moment
    if started_at is None or latest_at is None:
        return 0.0
    return max(0.0, (latest_at - started_at).total_seconds())


_HARVESTABLE_REMOTE_STATES = {
    "PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "SUBMIT_UNKNOWN",
}


def _harvest_limits(
    state: object, config: TickConfig
) -> tuple[int, float]:
    if str(state) == "PRIORITY_PENDING":
        return (
            min(config.harvest_max_attempts, config.priority_retry_max_attempts),
            min(
                config.harvest_max_elapsed_seconds,
                config.priority_retry_max_elapsed_seconds,
            ),
        )
    return config.harvest_max_attempts, config.harvest_max_elapsed_seconds


def _harvest(
    store: DailyAuditStore,
    token: LeaseToken,
    deps: TickDependencies,
    budget: TickBudget,
    counters: dict[str, int],
    config: TickConfig,
    *,
    skip_job_ids: frozenset[int] | set[int] = frozenset(),
) -> set[int]:
    """Sweep every eligible remote job once, in batches, within one round.

    The eligible set is snapshotted when the sweep starts; each remote job is
    checked at most once per sweep even when an earlier batch leaves it
    RUNNING, and jobs that only become eligible during the sweep (for example
    submissions made later in the same tick) are left to the next sweep.  The
    batch size bounds one request against the remote system, never the whole
    round; the tick budget stops the sweep at a safe checkpoint.
    """
    if deps.harvest is None:
        return set()
    snapshot = [
        job
        for job in sorted(
            store.list_remote_jobs(token, states=_HARVESTABLE_REMOTE_STATES),
            key=lambda job: (int(job["harvest_attempts"]), int(job["id"])),
        )
        if int(job["id"]) not in skip_job_ids
    ]
    if not snapshot:
        deps.harvest((), budget.callback_timeout(config.external_timeout_seconds))
        return set()
    swept: set[int] = set()
    for start in range(0, len(snapshot), config.harvest_batch_size):
        if not budget.can_start_heavy():
            break
        batch = snapshot[start : start + config.harvest_batch_size]
        _harvest_batch(store, token, deps, budget, counters, config, batch)
        swept.update(int(job["id"]) for job in batch)
    return swept


def _harvest_batch(
    store: DailyAuditStore,
    token: LeaseToken,
    deps: TickDependencies,
    budget: TickBudget,
    counters: dict[str, int],
    config: TickConfig,
    jobs: Sequence[Mapping[str, Any]],
) -> None:
    claimed = []
    claim_limits: dict[int, tuple[int, float]] = {}
    for job in jobs:
        max_attempts, max_elapsed_seconds = _harvest_limits(job["state"], config)
        claim = store.claim_harvest(
            token,
            int(job["id"]),
            max_attempts=max_attempts,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        if claim is not None:
            claimed.append(claim)
            claim_limits[int(claim["id"])] = (max_attempts, max_elapsed_seconds)
    if not claimed:
        return
    counters["harvest_batches"] = counters.get("harvest_batches", 0) + 1
    counters["harvest_swept"] = counters.get("harvest_swept", 0) + len(claimed)
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
            max_attempts, max_elapsed_seconds = claim_limits[int(job["id"])]
            store.record_harvest(token, int(job["id"]), elapsed_seconds=elapsed, max_attempts=max_attempts, max_elapsed_seconds=max_elapsed_seconds, error=_safe_error(exc))
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
            max_attempts, max_elapsed_seconds = claim_limits[job_id]
            store.record_harvest(token, job_id, elapsed_seconds=elapsed, max_attempts=max_attempts, max_elapsed_seconds=max_elapsed_seconds)
            continue
        if evidence.get("manual_error"):
            store.mark_remote_manual(token, job_id, str(evidence["manual_error"]))
            continue
        crawl_results = evidence.get("crawl_results")
        manual_crawl_results: list[tuple[int, str]] = []
        if isinstance(crawl_results, Mapping):
            for raw_crawl_id, crawl_result in crawl_results.items():
                if isinstance(crawl_result, Mapping) and crawl_result.get("manual"):
                    manual_crawl_results.append(
                        (
                            int(raw_crawl_id),
                            str(crawl_result.get("error", "harvest provenance is ambiguous")),
                        )
                    )
                elif isinstance(crawl_result, Mapping) and crawl_result.get("no_result"):
                    store.complete_partial_exact_no_result(
                        token,
                        int(raw_crawl_id),
                        remote_job_id=job_id,
                        remote_identity=str(
                            evidence.get("remote_identity")
                            or store.get_remote_job(job_id).get("remote_identity")
                            or ""
                        ),
                        result_path=str(
                            evidence.get("export_path")
                            or store.get_remote_job(job_id).get("result_path")
                            or ""
                        ),
                        reason=str(
                            crawl_result.get(
                                "reason", "provenance-valid remote request has no result row"
                            )
                        ),
                    )
                elif isinstance(crawl_result, Mapping) and crawl_result.get("terminal"):
                    store.fail_crawl_work(token, int(raw_crawl_id), str(crawl_result.get("error", "terminal crawler result")), retryable=False)
                else:
                    store.complete_crawl_work(token, int(raw_crawl_id), crawl_result)
                counters["crawl_harvested"] += 1
        state = str(evidence.get("state", "RUNNING"))
        remote_identity = str(evidence.get("remote_identity") or store.get_remote_job(job_id).get("remote_identity") or "")
        if not remote_identity:
            max_attempts, max_elapsed_seconds = claim_limits[job_id]
            store.record_harvest(token, job_id, elapsed_seconds=evidence_elapsed, max_attempts=max_attempts, max_elapsed_seconds=max_elapsed_seconds)
            continue
        store.reconcile_remote_job(
            token, job_id, remote_identity=remote_identity, state=state,
            export_path=evidence.get("export_path"), result=evidence.get("result"),
        )
        if manual_crawl_results:
            store.mark_remote_manual(
                token, job_id, "harvest has unresolved provenance mappings"
            )
            for crawl_id, error in manual_crawl_results:
                store.mark_partial_exact_crawl_manual(
                    token,
                    crawl_id,
                    remote_job_id=job_id,
                    remote_identity=remote_identity,
                    error=error,
                )
        elif state != "PRIORITY_PENDING":
            store.advance_crawl_work_for_job(token, job_id, state)
        retryable_error = (
            str(evidence["retryable_error"])
            if state == "PRIORITY_PENDING" and evidence.get("retryable_error")
            else None
        )
        max_attempts, max_elapsed_seconds = (
            claim_limits[job_id]
            if state == "PRIORITY_PENDING"
            else _harvest_limits(state, config)
        )
        store.record_harvest(
            token,
            job_id,
            elapsed_seconds=evidence_elapsed,
            max_attempts=max_attempts,
            max_elapsed_seconds=max_elapsed_seconds,
            error=retryable_error,
        )


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
    retry_classifier: Callable[[BaseException], bool] | None = None,
    retry_backoff: bool = False,
    wall_clock: Callable[[], datetime] | None = None,
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
    if retry_backoff and _durable_retry_backoff_blocks_claim(
        work, wall_clock or (lambda: datetime.now(UTC))
    ):
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
            retryable=(
                retry_classifier(exc) if retry_classifier is not None else retry_caught_errors
            ),
            max_attempts=max_attempts,
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


def _is_retryable_model_failure(exc: BaseException) -> bool:
    """Classify transport/infrastructure and response-contract failures as retryable."""
    cause: BaseException = exc
    if isinstance(exc, ModelCallRetryExhaustedError):
        if not isinstance(exc.last_cause, BaseException):
            return False
        cause = exc.last_cause
    if isinstance(cause, ModelResponseContractError):
        return True
    if isinstance(cause, error.HTTPError):
        return cause.code in {408, 429} or 500 <= cause.code <= 599
    if isinstance(cause, error.URLError):
        reason = cause.reason
        return isinstance(reason, (TimeoutError, ConnectionError))
    return isinstance(cause, (TimeoutError, ConnectionError))


def _model_retry_backoff_seconds(attempts: int) -> float:
    """Exponential durable-attempt spacing: 60s, 120s, 240s, capped at 300s."""
    if attempts <= 0:
        return 0.0
    return min(60.0 * (2 ** (attempts - 1)), 300.0)


def _durable_retry_backoff_blocks_claim(
    work: Mapping[str, Any], wall_clock: Callable[[], datetime]
) -> bool:
    """True while a RETRYABLE_FAILED stage row is still inside its backoff window.

    The due time is derived entirely from durable state (``updated_at`` plus a
    schedule over ``attempts``), so the spacing survives process restarts and
    needs no schema change.  A wall clock behind the durable failure timestamp
    proves no trustworthy elapsed time; the row stays immediately claimable so
    offline fixed-clock contracts keep their historic behaviour.
    """
    if str(work["state"]) != "RETRYABLE_FAILED":
        return False
    attempts = int(work["attempts"])
    if attempts <= 0:
        return False
    try:
        failed_at = datetime.fromisoformat(str(work["updated_at"]))
    except (TypeError, ValueError):
        return False
    now = wall_clock().astimezone(UTC)
    if now < failed_at:
        return False
    return now < failed_at + timedelta(seconds=_model_retry_backoff_seconds(attempts))


def _model_items_with_retry_backlog(
    store: DailyAuditStore,
    token: LeaseToken,
    current_items: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep same-day retryable MODEL work claimable across later snapshots."""
    result = list(current_items)
    current_ids = {int(item["id"]) for item in current_items}
    retryable = store.list_stage_work(
        token, stage="MODEL", states={"RETRYABLE_FAILED"}
    )
    for work in retryable:
        item_id = int(work["audit_item_id"])
        if item_id in current_ids:
            continue
        item = store.get_audit_item(item_id)
        if item is None:
            raise ValueError(f"retryable MODEL work has no audit item: {work['id']}")
        item["row"] = json.loads(str(item["row_json"]))
        result.append(item)
    return result


def _stage_model_batches(
    store: DailyAuditStore,
    token: LeaseToken,
    items: Sequence[Mapping[str, Any]],
    config: TickConfig,
    callback: Callable[[Sequence[Mapping[str, Any]], float], Mapping[int, Any]],
    budget: TickBudget,
    counters: dict[str, int],
    wall_clock: Callable[[], datetime] | None = None,
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
                if _durable_retry_backoff_blocks_claim(
                    work, wall_clock or (lambda: datetime.now(UTC))
                ):
                    continue
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
            retryable = _is_retryable_model_failure(exc)
            for group in claimed_groups:
                for _item, work in group:
                    store.fail_stage_work(
                        token, int(work["id"]), elapsed_seconds=elapsed, error=_safe_error(exc),
                        retryable=retryable,
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
    # The gate must run before any claim: a claimed submission that is then
    # abandoned would durably burn post_attempted evidence as SUBMIT_UNKNOWN.
    gate_reason = deps.submit_gate() if deps.submit_gate is not None else None
    if gate_reason:
        counters["submit_paused"] = counters.get("submit_paused", 0) + 1
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
        except SubmitNotAttemptedError as exc:
            # Provably pre-send: the burned claim may retreat because no POST
            # could have started; the next tick reclaims the same intent.
            store.release_remote_submission_claim(token, int(job["id"]), _safe_error(exc))
            counters["submit_not_attempted"] += 1
            return
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
        if state != "PRIORITY_PENDING":
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
            work["state"] in {
                "PENDING",
                "CLAIMED",
                "RETRYABLE_FAILED",
                "RECOVERED_PENDING_REVIEW",
            }
            for work in stages
        ),
        "detail_terminal": sum(
            work["stage"] == "DETAIL" and work["state"] == "TERMINAL_FAILED"
            for work in stages
        ),
        "model_terminal": sum(
            work["stage"] == "MODEL" and work["state"] == "TERMINAL_FAILED"
            for work in stages
        ),
        "crawl_pending": sum(work["state"] in {"PENDING", "RUNNING", "RETRYABLE_FAILED", "SUBMITTED", "EXPORTED", "SUBMIT_UNKNOWN"} for work in crawls),
        "remote_pending": sum(job["state"] in {"PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED"} for job in jobs),
        "remote_unknown": sum(job["state"] == "SUBMIT_UNKNOWN" for job in jobs),
        "remote_manual": sum(job["state"] == "MANUAL_REQUIRED" for job in jobs),
    }


_DRAIN_REMOTE_STATES = {
    "PRIORITY_PENDING",
    "SUBMITTED",
    "RUNNING",
    "EXPORTED",
    "SUBMIT_UNKNOWN",
}


def _remote_drain_progress(
    store: DailyAuditStore,
    token: LeaseToken,
) -> tuple[tuple[Any, ...], dict[str, int]]:
    jobs = store.list_remote_jobs(token)
    crawls = store.list_crawl_work(token)
    signature = (
        tuple(
            (
                int(job["id"]),
                str(job["state"]),
                str(job.get("export_path") or ""),
                str(job.get("result_json") or ""),
                str(job.get("error_text") or ""),
            )
            for job in jobs
        ),
        tuple(
            (
                int(crawl["id"]),
                str(crawl["state"]),
                int(crawl["remote_job_id"]) if crawl.get("remote_job_id") is not None else None,
                bool(crawl.get("result_json")),
                str(crawl.get("error_text") or ""),
            )
            for crawl in crawls
        ),
    )
    return signature, _backlog(store, token)


def run_existing_remote_drain(
    store: DailyAuditStore,
    process_lock: ProcessLock,
    config: TickConfig,
    deps: TickDependencies,
    *,
    max_iterations: int = 1000,
) -> TickSummary:
    """Drain only durable remote work; never discover, stage, plan, or submit."""
    if max_iterations <= 0:
        raise ValueError("drain max_iterations must be positive")
    counters = _empty_counters()
    counters.update({"drain_iterations": 0, "drain_completed": 0})
    trace: list[str] = []
    run = store.get_run(config.business_date)
    if run is None:
        return TickSummary("DRAIN_NO_EXISTING_RUN", tuple(trace), counters, {}, None)
    now = deps.wall_clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("wall_clock must return an explicit-timezone datetime")
    cutoff = config.cutoff_utc
    if cutoff is not None and now.astimezone(UTC) < cutoff:
        return TickSummary("DRAIN_NOT_READY", tuple(trace), counters, {}, None)
    try:
        process_lock.acquire()
    except LeaseUnavailable:
        return TickSummary("ALREADY_RUNNING", tuple(trace), counters, {}, None)

    token: LeaseToken | None = None
    try:
        try:
            token = store.acquire_lease(
                config.business_date,
                f"drain-{id(process_lock)}",
                max(
                    5,
                    config.lease_ttl_seconds,
                    config.runtime_budget_seconds + config.heavy_work_reserve_seconds,
                ),
                allow_unexpired_takeover=True,
                process_lock=process_lock,
            )
        except LeaseUnavailable:
            return TickSummary("ALREADY_RUNNING", tuple(trace), counters, {}, None)
        run = store.get_run(config.business_date)
        assert run is not None
        if run["state"] == "FINALIZED":
            _, backlog = _remote_drain_progress(store, token)
            return TickSummary(
                "ALREADY_FINALIZED",
                ("noop:already_finalized",),
                counters,
                backlog,
                run["frozen_snapshot_id"],
            )
        if cutoff is not None and run["frozen_snapshot_id"] is None:
            _, backlog = _remote_drain_progress(store, token)
            return TickSummary(
                "DRAIN_SNAPSHOT_NOT_READY", tuple(trace), counters, backlog, None
            )

        budget = TickBudget(
            deps.monotonic(),
            config.runtime_budget_seconds,
            config.heavy_work_reserve_seconds,
            deps.monotonic,
        )
        trace.append("recover_submit_unknown")
        counters["recovered_submit_unknown"] = store.recover_ambiguous_remote_submissions(
            token
        )
        before_signature, before_backlog = _remote_drain_progress(store, token)
        completed_before = sum(
            job["state"] == "COMPLETED" for job in store.list_remote_jobs(token)
        )
        stop_reason = "drained"

        while before_backlog["remote_pending"] or before_backlog["remote_unknown"]:
            if counters["drain_iterations"] >= max_iterations:
                stop_reason = "max_iterations"
                break
            available = budget.remaining() - budget.reserve_seconds
            if available <= 0:
                stop_reason = "deadline"
                break
            iteration_budget = TickBudget(
                deps.monotonic(), available, 0, deps.monotonic
            )
            trace.append("drain_batch")
            _harvest(store, token, deps, iteration_budget, counters, config)
            counters["drain_iterations"] += 1
            after_signature, after_backlog = _remote_drain_progress(store, token)
            if not after_backlog["remote_pending"] and not after_backlog["remote_unknown"]:
                before_signature, before_backlog = after_signature, after_backlog
                break
            if after_signature == before_signature:
                stop_reason = "no_progress"
                before_signature, before_backlog = after_signature, after_backlog
                break
            before_signature, before_backlog = after_signature, after_backlog

        completed_after = sum(
            job["state"] == "COMPLETED" for job in store.list_remote_jobs(token)
        )
        counters["drain_completed"] = completed_after - completed_before
        trace.extend((f"stop:{stop_reason}", "checkpoint"))
        backlog = before_backlog
        if backlog["remote_manual"]:
            outcome = "MANUAL_REVIEW_REQUIRED"
        elif backlog["remote_unknown"]:
            outcome = "WAITING_REMOTE_RECONCILIATION"
        elif backlog["remote_pending"]:
            outcome = {
                "deadline": "BUDGET_EXHAUSTED",
                "max_iterations": "DRAIN_MAX_ITERATIONS",
                "no_progress": "DRAIN_NO_PROGRESS",
            }.get(stop_reason, "WAITING_REMOTE")
        else:
            outcome = "DRAINED"
        store.update_daily_metrics(token, counters)
        final_run = store.get_run(config.business_date)
        return TickSummary(
            outcome,
            tuple(trace),
            counters,
            backlog,
            final_run["frozen_snapshot_id"] if final_run else None,
        )
    except Exception as exc:
        if token is None:
            return TickSummary("FAILED", tuple(trace), counters, {}, None, _safe_error(exc))
        try:
            _, backlog = _remote_drain_progress(store, token)
        except BaseException:
            backlog = {}
        return TickSummary("FAILED", tuple(trace), counters, backlog, None, _safe_error(exc))
    finally:
        if token is not None:
            store.release_lease(token)
        process_lock.release()


def run_harvest_only(
    store: DailyAuditStore,
    process_lock: ProcessLock,
    config: TickConfig,
    deps: TickDependencies,
) -> TickSummary:
    """Sweep already-submitted remote jobs exactly once; never create or finalize.

    This is the daytime quarter-hour counterpart of the drain: one existing
    :func:`_harvest` sweep over the durable eligible set, so a finished remote
    job is exported and ingested without waiting for the next hourly tick.
    Discovery, staging, planning, submission and finalization do not exist on
    this path, and a day with no eligible remote job is a pure local no-op.
    """
    counters = _empty_counters()
    trace: list[str] = []
    run = store.get_run(config.business_date)
    if run is None:
        return TickSummary("HARVEST_NO_EXISTING_RUN", tuple(trace), counters, {}, None)
    now = deps.wall_clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("wall_clock must return an explicit-timezone datetime")
    try:
        process_lock.acquire()
    except LeaseUnavailable:
        return TickSummary("ALREADY_RUNNING", tuple(trace), counters, {}, None)

    token: LeaseToken | None = None
    try:
        try:
            token = store.acquire_lease(
                config.business_date,
                f"harvest-{id(process_lock)}",
                max(
                    5,
                    config.lease_ttl_seconds,
                    config.runtime_budget_seconds + config.heavy_work_reserve_seconds,
                ),
                allow_unexpired_takeover=True,
                process_lock=process_lock,
            )
        except LeaseUnavailable:
            return TickSummary("ALREADY_RUNNING", tuple(trace), counters, {}, None)
        run = store.get_run(config.business_date)
        assert run is not None
        if run["state"] == "FINALIZED":
            _, backlog = _remote_drain_progress(store, token)
            return TickSummary(
                "ALREADY_FINALIZED",
                ("noop:already_finalized",),
                counters,
                backlog,
                run["frozen_snapshot_id"],
            )
        trace.append("recover_submit_unknown")
        counters["recovered_submit_unknown"] = store.recover_ambiguous_remote_submissions(
            token
        )
        eligible = store.list_remote_jobs(token, states=_HARVESTABLE_REMOTE_STATES)
        if not eligible:
            trace.append("noop:nothing_eligible")
            backlog = _backlog(store, token)
            return TickSummary(
                "HARVEST_NOTHING_ELIGIBLE",
                tuple(trace),
                counters,
                backlog,
                run["frozen_snapshot_id"],
            )
        budget = TickBudget(
            deps.monotonic(),
            config.runtime_budget_seconds,
            config.heavy_work_reserve_seconds,
            deps.monotonic,
        )
        trace.append("harvest")
        _harvest(store, token, deps, budget, counters, config)
        trace.append("checkpoint")
        backlog = _backlog(store, token)
        if backlog["remote_manual"]:
            outcome = "MANUAL_REVIEW_REQUIRED"
        elif backlog["remote_unknown"]:
            outcome = "WAITING_REMOTE_RECONCILIATION"
        elif backlog["remote_pending"]:
            outcome = "WAITING_REMOTE"
        else:
            outcome = "HARVESTED"
        counters["crawl_running"] = backlog["crawl_pending"]
        store.update_daily_metrics(token, counters)
        final_run = store.get_run(config.business_date)
        return TickSummary(
            outcome,
            tuple(trace),
            counters,
            backlog,
            final_run["frozen_snapshot_id"] if final_run else None,
        )
    except Exception as exc:
        if token is None:
            return TickSummary("FAILED", tuple(trace), counters, {}, None, _safe_error(exc))
        try:
            _, backlog = _remote_drain_progress(store, token)
        except BaseException:
            backlog = {}
        return TickSummary("FAILED", tuple(trace), counters, backlog, None, _safe_error(exc))
    finally:
        if token is not None:
            store.release_lease(token)
        process_lock.release()


def run_incremental_tick(
    store: DailyAuditStore,
    process_lock: ProcessLock,
    config: TickConfig,
    deps: TickDependencies,
    *,
    finalize: bool = False,
    skip_opening_harvest: bool = False,
    skip_discovery: bool = False,
) -> TickSummary:
    """Run one bounded tick; it never starts a daemon or waits for submitted jobs.

    ``skip_opening_harvest`` and ``skip_discovery`` are narrow phase controls for
    the evening closeout orchestrator, which paces source observations and full
    remote sweeps itself.  The defaults keep ordinary tick/finalize behaviour:
    every tick sweeps eligible remote jobs once and discovers the source when
    the run is not frozen.
    """
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
        run = store.get_run(config.business_date)
        assert run is not None
        if run["state"] == "FINALIZED":
            return TickSummary(
                "ALREADY_FINALIZED",
                ("noop:already_finalized",),
                counters,
                {},
                run["frozen_snapshot_id"],
            )
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
        swept_remote_jobs: set[int] = set()
        if skip_opening_harvest:
            trace.append("harvest_deferred")
        else:
            swept_remote_jobs = _harvest(store, token, deps, budget, counters, config)
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
        elif skip_discovery:
            # A sweep-only pass: no new observation, so there are no snapshot
            # items to preprocess this pass.  Durable pending work (remote
            # submissions, stage retry backlog) still advances.
            trace.append("discover_deferred")
        else:
            trace.append("discover")
            discovered = deps.discover(
                budget.callback_timeout(config.snapshot_discovery_timeout_seconds)
            )
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
        snapshot_id = int(snapshot["id"]) if snapshot is not None else None
        items: Sequence[Mapping[str, Any]] = (
            store.ordered_snapshot_items(snapshot_id) if snapshot_id is not None else ()
        )
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
        model_items = _model_items_with_retry_backlog(store, token, items)
        if deps.model_batch is not None:
            _stage_model_batches(store, token, model_items, config, deps.model_batch, budget, counters, deps.wall_clock)
        else:
            for item in model_items:
                if not budget.can_start_heavy():
                    break
                _stage(
                    store, token, item, "MODEL",
                    model_fingerprint(item["row"], config.model_name, config.model_prompt_version, config.model_schema_version),
                    deps.model, budget, config.external_timeout_seconds, config.model_max_attempts, config.model_max_elapsed_seconds, counters,
                    retry_classifier=_is_retryable_model_failure,
                    retry_backoff=True,
                    wall_clock=deps.wall_clock,
                )
        trace.append("submit")
        if budget.can_start_heavy():
            model_by_item = {int(work["audit_item_id"]): work for work in store.list_stage_work(token, stage="MODEL")}
            eligible = [
                item for item in model_items
                if model_by_item.get(int(item["id"]), {}).get("state") == "SUCCESS"
            ]
            _submit(store, token, eligible, config, deps, budget, counters)
        if counters["crawl_submitted"] and budget.can_start_heavy() and store.list_remote_jobs(
            token, states=_HARVESTABLE_REMOTE_STATES
        ):
            trace.append("post_harvest")
            # Jobs already checked by this run's opening sweep must not be
            # polled twice in one tick; only submissions made after it apply.
            _harvest(
                store, token, deps, budget, counters, config,
                skip_job_ids=swept_remote_jobs,
            )
        if deps.remote_recovery_probe is not None and budget.can_start_heavy():
            # Tail-position recovery: core intake (discovery/detail/model/
            # submit/post-harvest) has already claimed its budget, and every
            # recovery callback receives a timeout intersected with the
            # tick's live remaining budget so the sweep can never starve it.
            trace.append("running_recovery")

            def _budget_recovery_probe(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
                return deps.remote_recovery_probe(
                    job, budget.callback_timeout(RECOVERY_CALLBACK_TIMEOUT_SECONDS)
                )

            def _budget_recovery_restart(
                job: Mapping[str, Any], resume_phase: str | None
            ) -> Mapping[str, Any]:
                return deps.remote_recovery_restart(
                    job, resume_phase, budget.callback_timeout(RECOVERY_CALLBACK_TIMEOUT_SECONDS)
                )

            recovery_counters = run_running_recovery_pass(
                store,
                token,
                probe_remote=_budget_recovery_probe,
                restart_remote=(
                    _budget_recovery_restart if deps.remote_recovery_restart is not None else None
                ),
                now=deps.wall_clock(),
                can_start=lambda: budget.can_start_heavy(),
            )
            for key, value in recovery_counters.items():
                counters[key] = counters.get(key, 0) + int(value)
        trace.append("checkpoint")
        backlog = _backlog(store, token, snapshot_items)
        waiting_source_count = False
        if finalize and run["frozen_snapshot_id"] is None:
            if snapshot is None:
                # A sweep-only pass observed nothing new; freezing is decided by
                # a pass that actually carries an authoritative observation.
                waiting_source_count = True
            else:
                try:
                    evidence = json.loads(str(snapshot.get("evidence_json") or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    evidence = {}
                saas_count = evidence.get("saas_count")
                customer_count = evidence.get("customer_count")
                valid_saas_count = (
                    isinstance(saas_count, int)
                    and not isinstance(saas_count, bool)
                    and saas_count >= 0
                )
                valid_customer_count = (
                    isinstance(customer_count, int)
                    and not isinstance(customer_count, bool)
                    and customer_count >= 0
                )
                if valid_saas_count:
                    counters["saas_count"] = saas_count
                if valid_customer_count:
                    counters["customer_count"] = customer_count
                if valid_saas_count and valid_customer_count:
                    counters["source_count_gap"] = customer_count - saas_count
                if (
                    valid_saas_count
                    and int(snapshot["row_count"]) == saas_count
                    and source_stability_seconds(store, config.business_date, snapshot)
                    + 1e-9
                    >= config.source_stability_seconds
                ):
                    run = store.freeze_snapshot(token, snapshot_id)
                else:
                    waiting_source_count = True
        if backlog["model_terminal"] or backlog["remote_manual"]:
            outcome = "MANUAL_REVIEW_REQUIRED"
        elif backlog["remote_unknown"]:
            outcome = "WAITING_REMOTE_RECONCILIATION"
        elif waiting_source_count:
            outcome = "WAITING_SOURCE_COUNT"
        elif backlog["remote_pending"]:
            outcome = "WAITING_REMOTE"
        elif not budget.can_start_heavy() and (backlog["stage_pending"] or backlog["crawl_pending"]):
            outcome = "BUDGET_EXHAUSTED"
        elif backlog["stage_pending"] or backlog["crawl_pending"]:
            outcome = "PENDING_RETRY"
        else:
            outcome = "COMPLETED"
        counters["crawl_running"] = backlog["crawl_pending"]
        final_pending = sum(
            value for key, value in backlog.items() if key != "detail_terminal"
        )
        store.update_daily_metrics(token, {**counters, "final_pending": final_pending})
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
