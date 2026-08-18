"""Evening closeout orchestrator: keep advancing one day's audit run to FINAL.

The closeout is an orchestrator, not a second business state machine.  Every
business operation — discovery, Detail, Model, submit, priority, harvest and
the final materialization — runs through the existing finite tick engine and
the existing final materializer with their durable SQLite checkpointing.  The
orchestrator only removes two artificial waits the 15-minute scheduler imposed:

* the source-stability wait: fresh authoritative observations are paced at
  ``source_observation_interval_seconds`` until two identical observations span
  ``TickConfig.source_stability_seconds``, when the latest snapshot freezes;
* the remote wait: a full remote sweep runs at most every
  ``remote_sweep_interval_seconds`` while local work continues immediately.

The whole closeout owns the production writer lock, so the scheduler's
``IgnoreNew`` policy turns overlapping wakeups into no-ops.  Any state is
already checkpointed in SQLite; a closeout stopped at its wall deadline is
continued by the next scheduled closeout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from app.services.daily_audit_store import DailyAuditStore, FrozenSnapshotConflict, ProcessLock
from app.services.incremental_audit_materializer import (
    ArtifactConflictError,
    MaterializationValidationError,
)
from app.workflows.incremental_price_audit import (
    TickConfig,
    TickDependencies,
    TickSummary,
    run_incremental_tick,
)
from app.workflows.incremental_price_audit_finalize import (
    FinalizeAlreadyRunning,
    FinalizeBlocked,
    FinalizePublication,
    is_recoverable_finalize_wait,
    materialize_frozen_daily_run,
)


# Secondary guard: never start the final materialization with less than this
# wall-clock left, even if the configured reserve was already consumed.
MIN_MATERIALIZE_SECONDS = 60.0
# Gauge-like tick counters describe the latest observation, not an amount of
# work done, so accumulating them across passes would overcount.
_GAUGE_COUNTER_KEYS = {"crawl_running", "saas_count", "customer_count", "source_count_gap"}


@dataclass(frozen=True)
class CloseoutConfig:
    wall_budget_seconds: float
    source_observation_interval_seconds: float = 300.0
    remote_sweep_interval_seconds: float = 600.0
    # Reserved for checkpoint + final.xlsx materialization + manifest + exit;
    # no new long operation starts once only this much wall clock remains.
    final_materialize_reserve_seconds: float = 120.0
    failed_pass_retry_seconds: float = 30.0
    idle_pass_retry_seconds: float = 30.0
    max_consecutive_failed_passes: int = 2

    def __post_init__(self) -> None:
        values = (
            self.wall_budget_seconds,
            self.source_observation_interval_seconds,
            self.remote_sweep_interval_seconds,
            self.final_materialize_reserve_seconds,
            self.failed_pass_retry_seconds,
            self.idle_pass_retry_seconds,
        )
        if any(value < 0 for value in values):
            raise ValueError("closeout intervals and budgets must be zero or positive")
        if self.wall_budget_seconds <= 0:
            raise ValueError("closeout wall budget must be positive")
        if self.max_consecutive_failed_passes <= 0:
            raise ValueError("closeout failed-pass limit must be positive")


def _accumulate(counters: dict[str, int], summary: TickSummary) -> None:
    for key, value in summary.counters.items():
        if key in _GAUGE_COUNTER_KEYS:
            counters[key] = int(value)
        else:
            counters[key] = counters.get(key, 0) + int(value)


def run_closeout(
    store: DailyAuditStore,
    process_lock: ProcessLock,
    config: TickConfig,
    deps: TickDependencies,
    closeout: CloseoutConfig,
    *,
    output_path: Path | str,
    materialize: Callable[..., FinalizePublication] = materialize_frozen_daily_run,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[TickSummary, FinalizePublication | None, str | None, str | None]:
    """Drive one business day to FINAL inside a single production writer.

    Returns the merged tick summary, the final publication when the day was
    materialized, and the publication blocker when a non-waiting blocker
    stopped the closeout.  Every pass reuses ``run_incremental_tick`` so all
    existing idempotency, lease and crash-recovery contracts apply unchanged.
    """
    started = monotonic()
    wall_deadline = started + closeout.wall_budget_seconds
    long_op_deadline = wall_deadline - closeout.final_materialize_reserve_seconds
    next_observation_at = started
    next_sweep_at = started
    passes = 0
    consecutive_failed = 0
    last: TickSummary | None = None
    counters: dict[str, int] = {}
    trace: list[str] = ["closeout:start"]

    def merged(outcome: str) -> TickSummary:
        return TickSummary(
            outcome,
            tuple(trace),
            counters,
            dict(last.backlog) if last is not None else {},
            last.frozen_snapshot_id if last is not None else None,
            last.error if last is not None else None,
        )

    while True:
        now = monotonic()
        run = store.get_run(config.business_date)
        if run is not None and run["state"] == "FINALIZED":
            trace.append("noop:already_finalized")
            return merged("ALREADY_FINALIZED"), None, None, None
        if now >= long_op_deadline:
            trace.append("stop:deadline")
            return merged("CLOSEOUT_DEADLINE"), None, None, None
        frozen = run is not None and run["frozen_snapshot_id"] is not None
        observation_due = now + 1e-9 >= next_observation_at
        sweep_due = now + 1e-9 >= next_sweep_at
        if frozen:
            needs_pass = (
                passes == 0
                or sweep_due
                or (last is not None and last.outcome == "PENDING_RETRY")
            )
        else:
            needs_pass = observation_due or sweep_due
        if not needs_pass:
            wake_at = long_op_deadline
            if not frozen:
                wake_at = min(wake_at, next_observation_at)
            wake_at = min(wake_at, next_sweep_at)
            sleep(max(0.0, wake_at - monotonic()))
            continue

        pass_budget = wall_deadline - monotonic()
        if pass_budget <= config.heavy_work_reserve_seconds:
            trace.append("stop:deadline")
            return merged("CLOSEOUT_DEADLINE"), None, None, None

        passes += 1
        summary = run_incremental_tick(
            store,
            process_lock,
            replace(config, runtime_budget_seconds=pass_budget),
            deps,
            finalize=True,
            skip_opening_harvest=not sweep_due,
            skip_discovery=not observation_due,
        )
        previous_outcome = last.outcome if last is not None else None
        previous_counters = dict(last.counters) if last is not None else {}
        last = summary
        _accumulate(counters, summary)
        trace.append(f"pass{passes}:{summary.outcome}")
        after_pass = monotonic()
        if summary.outcome == "ALREADY_RUNNING":
            return merged("ALREADY_RUNNING"), None, None, None
        if summary.outcome != "ALREADY_FINALIZED":
            if observation_due:
                next_observation_at = after_pass + closeout.source_observation_interval_seconds
            if sweep_due:
                next_sweep_at = after_pass + closeout.remote_sweep_interval_seconds
        if summary.outcome == "ALREADY_FINALIZED":
            return merged("ALREADY_FINALIZED"), None, None, None
        if summary.outcome in {"FAILED", "DISCOVERY_INCOMPLETE"}:
            consecutive_failed += 1
            if (
                consecutive_failed >= closeout.max_consecutive_failed_passes
                or monotonic() >= long_op_deadline
            ):
                return merged(summary.outcome), None, None, None
            sleep(closeout.failed_pass_retry_seconds)
            continue
        consecutive_failed = 0
        if summary.frozen_snapshot_id is None:
            # Source not stable yet; the observation pacing above decides when
            # the next pass may look at SaaS again.
            _idle_wait(
                closeout,
                monotonic,
                sleep,
                summary.outcome == previous_outcome
                and dict(summary.counters) == previous_counters,
            )
            continue
        if wall_deadline - monotonic() < MIN_MATERIALIZE_SECONDS:
            trace.append("stop:deadline")
            return merged("CLOSEOUT_DEADLINE"), None, None, None
        try:
            publication = materialize(
                store, process_lock, config.business_date, Path(output_path)
            )
        except FinalizeAlreadyRunning:
            trace.append("publication_overlap")
            return merged("ALREADY_RUNNING"), None, None, None
        except FinalizeBlocked as exc:
            trace.append("publication_blocked")
            if is_recoverable_finalize_wait(exc.readiness):
                # Only genuinely external pending work remains; wait for the
                # next allowed observation or remote sweep instead of exiting.
                _idle_wait(
                    closeout,
                    monotonic,
                    sleep,
                    summary.outcome == previous_outcome
                    and dict(summary.counters) == previous_counters,
                )
                continue
            return merged(summary.outcome), None, str(exc), type(exc).__name__
        except (FrozenSnapshotConflict, ArtifactConflictError, MaterializationValidationError) as exc:
            return merged(summary.outcome), None, str(exc), type(exc).__name__
        trace.append("publication:materialized")
        return merged(summary.outcome), publication, None, None


def _idle_wait(
    closeout: CloseoutConfig,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    repeated_without_progress: bool,
) -> None:
    """Avoid a hot loop when a pass repeats without moving any counter."""
    if repeated_without_progress:
        sleep(closeout.idle_pass_retry_seconds)
