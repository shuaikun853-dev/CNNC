"""Cached degraded finalization: snapshot-gate evaluator plus guarded runner.

This module is the guarded "cached" fallback for the 2026-08-31 incident
shape: business evidence all green, several identical authoritative
snapshots stable for hours, yet the authoritative source itself is
unavailable (auth expired), so the strict closeout can never reach its
final freeze.  It freezes the day's last-known-good snapshot and
materializes the formal workbook purely from durable SQLite state.

Structural no-network guarantee:

- The module takes no ``TickDependencies`` (no ``deps`` parameter at all),
  so it cannot call discover / Detail / Model / submit / priority / harvest.
- Every input comes from ``DailyAuditStore`` reads or the caller; the only
  writable effects are ``freeze_snapshot_with_metrics`` /
  ``prepare_finalization`` / ``materialize`` (local workbook) /
  ``mark_finalized`` / ``update_daily_metrics``.
- Provenance (``finalization_mode=CACHED_DEGRADED``, degraded reason and
  side, cached snapshot id / observed_at / stability, freeze timestamp) is
  committed atomically with the freeze into ``daily_runs.metrics_json`` —
  no schema migration, no secrets.

Snapshot-side readiness gates, evaluated by
``evaluate_cached_finalization_readiness`` before anything else, each
raising ``CachedFinalizeBlocked`` with a fixed reason code and
count/timestamp/id detail only (never response bodies or tokens):

1. ``BEFORE_CUTOFF``             now >= cutoff_at
2. ``NO_RUN``                    the daily run exists
3. ``NO_SNAPSHOTS``              at least one authoritative observation
4. ``SINGLE_OBSERVATION``        tail run length >= 2
5. ``TAIL_CONTENT_DIFFERS``      the last observations share (row_count, ordered_hash)
6. ``STABILITY_BELOW_THRESHOLD`` trailing identical span >= threshold
7. ``MEMBERS_INCOMPLETE``        ordered members match row_count, headers/evidence parse
8. ``SAAS_COUNT_MISMATCH``       evidence saas_count is a valid int equal to row_count

The business-side gate (Detail/Model SUCCESS, crawl and remote evidence)
is the formal ``FinalizeReadiness`` computed by
``build_frozen_audit_artifact`` for the candidate snapshot — the same
single rule set strict/review/force finalization uses, never a second
private copy.  The runner evaluates it before the freeze, so every refusal
(``READINESS_BLOCKED``) leaves the day unfrozen.

The candidate snapshot is the last observation (by observed_at, id), i.e.
the naturally last-known-good one.  The runner mirrors
``materialize_frozen_daily_run`` (one process lock, one lease) but freezes
the evaluated candidate first, committing the frozen pointer and the
cached provenance metrics in one transaction.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from app.services.daily_audit_store import DailyAuditStore, LeaseToken, LeaseUnavailable, ProcessLock
from app.services.incremental_audit_materializer import LegacyWorkbookMaterializer
from app.workflows.incremental_price_audit import source_stability_seconds
from app.workflows.incremental_price_audit_finalize import (
    FinalizePublication,
    build_frozen_audit_artifact,
)


CACHED_FINALIZATION_MODE = "CACHED_DEGRADED"
STRICT_FINALIZATION_MODE = "STRICT"

_STABILITY_EPSILON_SECONDS = 1e-9


@dataclass(frozen=True)
class CachedFinalizePlan:
    """Pure-read evaluation result: which snapshot may be frozen and why."""

    snapshot_id: int
    row_count: int
    ordered_hash: str
    observed_at: str
    stability_seconds: float
    tail_run_length: int


class CachedFinalizeBlocked(RuntimeError):
    """A guarded refusal to cache-finalize; detail holds counts/times/ids only."""

    def __init__(self, reason_code: str, detail: Mapping[str, Any]) -> None:
        self.reason_code = reason_code
        self.detail = dict(detail)
        super().__init__(f"{reason_code}: {sorted(self.detail.items())}")


@dataclass(frozen=True)
class CachedFinalizeOutcome:
    outcome: str  # "CACHED_FINALIZED" | "ALREADY_FINALIZED"
    publication: FinalizePublication | None
    cached_snapshot_id: int | None
    last_source_observed_at: str | None
    source_stability_seconds: float


def _decode_json(text: Any) -> Any:
    try:
        return json.loads(str(text or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _valid_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _already_finalized(run: Mapping[str, Any] | None) -> CachedFinalizeOutcome | None:
    if run is None or str(run["state"]) != "FINALIZED":
        return None
    frozen = run["frozen_snapshot_id"]
    return CachedFinalizeOutcome(
        "ALREADY_FINALIZED",
        None,
        int(frozen) if frozen is not None else None,
        None,
        0.0,
    )


def evaluate_cached_finalization_readiness(
    store: DailyAuditStore,
    token: LeaseToken,
    *,
    cutoff_at: datetime | None,
    now: datetime,
    source_stability_threshold_seconds: float,
) -> CachedFinalizePlan:
    """Return the cacheable candidate plan, or raise CachedFinalizeBlocked.

    Pure reads only: no freeze, no workbook, no network, no mutation.
    """
    if cutoff_at is not None and now < cutoff_at:
        raise CachedFinalizeBlocked("BEFORE_CUTOFF", {
            "now": now.isoformat(timespec="seconds"),
            "cutoff_at": cutoff_at.isoformat(timespec="seconds"),
        })
    run = store.get_run(token.business_date)
    if run is None:
        raise CachedFinalizeBlocked("NO_RUN", {"business_date": token.business_date})
    snapshots = store.list_snapshots(token.business_date)
    if not snapshots:
        raise CachedFinalizeBlocked("NO_SNAPSHOTS", {
            "business_date": token.business_date,
            "snapshot_count": 0,
        })
    candidate = snapshots[-1]
    candidate_id = int(candidate["id"])
    identity = (int(candidate["row_count"]), str(candidate["ordered_hash"]))
    tail_run_length = 0
    for observed in reversed(snapshots):
        if (int(observed["row_count"]), str(observed["ordered_hash"])) != identity:
            break
        tail_run_length += 1
    if tail_run_length < 2:
        raise CachedFinalizeBlocked(
            "SINGLE_OBSERVATION" if len(snapshots) == 1 else "TAIL_CONTENT_DIFFERS",
            {
                "snapshot_count": len(snapshots),
                "candidate_snapshot_id": candidate_id,
                "tail_run_length": tail_run_length,
            },
        )
    computed_stability = source_stability_seconds(store, token.business_date, candidate)
    if computed_stability + _STABILITY_EPSILON_SECONDS < source_stability_threshold_seconds:
        raise CachedFinalizeBlocked("STABILITY_BELOW_THRESHOLD", {
            "snapshot_id": candidate_id,
            "computed": computed_stability,
            "required": source_stability_threshold_seconds,
        })

    row_count = int(candidate["row_count"])
    items = store.ordered_snapshot_items(candidate_id)
    headers = _decode_json(candidate.get("headers_json"))
    evidence = _decode_json(candidate.get("evidence_json"))
    if (
        len(items) != row_count
        or not isinstance(headers, list)
        or not headers
        or not isinstance(evidence, dict)
    ):
        raise CachedFinalizeBlocked("MEMBERS_INCOMPLETE", {
            "snapshot_id": candidate_id,
            "expected_members": row_count,
            "actual_members": len(items),
        })
    saas_count = evidence.get("saas_count")
    if not _valid_count(saas_count) or saas_count != row_count:
        raise CachedFinalizeBlocked("SAAS_COUNT_MISMATCH", {
            "snapshot_id": candidate_id,
            "row_count": row_count,
            "saas_count": saas_count if _valid_count(saas_count) else str(type(saas_count).__name__),
        })

    return CachedFinalizePlan(
        snapshot_id=candidate_id,
        row_count=row_count,
        ordered_hash=str(candidate["ordered_hash"]),
        observed_at=str(candidate["observed_at"]),
        stability_seconds=computed_stability,
        tail_run_length=tail_run_length,
    )


def run_cached_finalization(
    store: DailyAuditStore,
    process_lock: ProcessLock,
    business_date: str,
    output_path: Path | str,
    *,
    cutoff_at: datetime | None,
    now: datetime,
    source_stability_threshold_seconds: float,
    degraded_reason_code: str,
    degraded_source_side: str,
    materializer: LegacyWorkbookMaterializer | None = None,
) -> CachedFinalizeOutcome:
    """Freeze the evaluated last-known-good snapshot and materialize the final.

    Orchestration mirrors ``materialize_frozen_daily_run`` (one process
    lock, one lease, release in ``finally``) with the cached evaluation and
    freeze in front.  Raises ``CachedFinalizeBlocked`` on every guarded
    refusal; the day stays unfrozen whenever a gate fires pre-freeze.
    """
    already = _already_finalized(store.get_run(business_date))
    if already is not None:
        return already
    try:
        process_lock.acquire()
    except LeaseUnavailable as exc:
        raise CachedFinalizeBlocked("LOCK_HELD", {"business_date": business_date}) from exc
    token: LeaseToken | None = None
    try:
        token = store.acquire_lease(
            business_date,
            f"cached-finalize-{os.getpid()}",
            ttl_seconds=600,
            allow_unexpired_takeover=True,
            process_lock=process_lock,
        )
        run = store.get_run(business_date)
        already = _already_finalized(run)
        if already is not None:
            return already
        plan = evaluate_cached_finalization_readiness(
            store,
            token,
            cutoff_at=cutoff_at,
            now=now,
            source_stability_threshold_seconds=source_stability_threshold_seconds,
        )
        # The formal, shared business-readiness gate runs BEFORE the freeze:
        # the same FinalizeReadiness strict/review/force finalization uses,
        # computed for the candidate snapshot, so no second rule set exists
        # and every refusal leaves the day unfrozen.
        built = build_frozen_audit_artifact(
            store,
            token,
            output_identity=f"daily-final-{business_date.replace('-', '')}",
            snapshot_id=plan.snapshot_id,
        )
        if not built.readiness.ready or built.artifact is None:
            raise CachedFinalizeBlocked("READINESS_BLOCKED", {
                "snapshot_id": plan.snapshot_id,
                "readiness": asdict(built.readiness),
            })
        frozen = run["frozen_snapshot_id"] if run is not None else None
        if frozen is not None and int(frozen) != plan.snapshot_id:
            raise CachedFinalizeBlocked("NOT_FROZEN_CONFLICT", {
                "frozen_snapshot_id": int(frozen),
                "candidate_snapshot_id": plan.snapshot_id,
            })
        # One transaction commits the frozen pointer and the cached
        # provenance; the same-id rerun merges (provenance backfill).
        store.freeze_snapshot_with_metrics(token, plan.snapshot_id, {
            "finalization_mode": CACHED_FINALIZATION_MODE,
            "cached_snapshot_id": plan.snapshot_id,
            "cached_last_source_observed_at": plan.observed_at,
            "cached_source_stability_seconds": plan.stability_seconds,
            "degraded_reason_code": degraded_reason_code,
            "degraded_source_side": degraded_source_side,
            "cached_freeze_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "frozen_snapshot_id": plan.snapshot_id,
        })
        resolved_output = Path(output_path).resolve()
        store.prepare_finalization(token, str(resolved_output))
        publisher = materializer or LegacyWorkbookMaterializer()
        result = publisher.materialize(built.artifact, resolved_output)
        store.mark_finalized(token, str(resolved_output), result.manifest_sha256)
        store.update_daily_metrics(token, {
            "finalized_rows": result.row_count,
            "cached_finalized_at": datetime.now(UTC).isoformat(timespec="seconds"),
        })
        return CachedFinalizeOutcome(
            "CACHED_FINALIZED",
            FinalizePublication(built.readiness, result),
            plan.snapshot_id,
            plan.observed_at,
            plan.stability_seconds,
        )
    finally:
        if token is not None:
            store.release_lease(token)
        process_lock.release()
