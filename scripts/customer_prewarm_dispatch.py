"""CLI for the Issue #10 customer prewarm dispatch.

Default mode runs ONE bounded dispatch pass (route → reuse/create → submit →
priority proof → cache absorb) that reuses the existing STEP1 zhaoshang
lifecycle; it never invents a second remote-task store.  Overlap is prevented
twice over: the dispatcher's own instance lock prints
``SKIPPED_ALREADY_RUNNING`` and skips the whole invocation; STEP1's current-day
process lock / durable lease prints ``LOCK_BUSY``, skips current-day work, but
still allows the historical-day reconcile/harvest stages to run under their
own owning-day locks.

``--plan-only`` builds the §26 read-only report: zero writes anywhere (no
candidate status changes, no lock files, no adapter construction, state DB
accessed ``mode=ro`` only).  The prewarm store and shared cache are opened
through their read-only modes, so a missing store file is reported as
``PREWARM_STORE_MISSING`` / ``SHARED_CACHE_MISSING`` (exit 0, diagnosis JSON)
instead of being silently created.

Lifecycle closeout (WS-A, default on): after the pass's own phases the same
lease reconciles today's identity-less ``SUBMIT_UNKNOWN`` prewarm jobs and
re-dispatches dead-job candidates (daily per-candidate budget from
``prewarm_remote_job_links`` rows of the day).  Once the pass released every
lock, the CLI first reconciles linked historical-day ``SUBMIT_UNKNOWN`` jobs
under each owning day's own ProcessLock + lease, then harvests live prewarm
jobs through the existing ``harvest_existing_customer_prewarm_job`` primitive.
The dispatch pass is budgeted at ``--budget-seconds - lifecycle_reserve``;
after it actually returns, the historical lifecycle receives that bounded
reserve as a fresh deadline window, so a late remote timeout in dispatch
cannot starve cross-day reconcile/harvest.  ``--no-lifecycle`` skips every
lifecycle stage.  Canary mode (``--only-mall``) never runs any lifecycle stage.

The three data sources are independently overridable and default to today's
layout: ``--state-runtime-root`` locates the STEP1 ``<date>/state.sqlite3``
plus per-day lock / zhaoshang job dirs (defaults to ``--runtime-root``),
``--prewarm-store-path`` and ``--shared-cache-path`` locate the two shared
stores (default ``<runtime-root>/customer_prewarm.sqlite3`` and
``<runtime-root>/shared_crawl_cache.sqlite3``).

``--backfill-zhaoshang-history N`` runs ONLY the evidence-based zhaoshang
history backfill: past N calendar days (ending at ``--business-date``
inclusive) per-day state DBs are scanned for durably EXPORTED/COMPLETED remote
jobs whose export workbook passes the full ``harvest_existing`` provenance
gate AND whose validated ``(sku, url)`` identity is bound to a SUCCESS crawl
row of the same job with ``page_price_status == OK``; only those rows are
mirrored into the shared cache as ``zhaoshang-history`` evidence
(:func:`app.services.zhaoshang_history_backfill.backfill_zhaoshang_history`);
the dispatch pass does not run.  Unbound SUCCESS crawl rows are never
recorded (no cross-proof).  Idempotent: re-runs report the same producer
events as ``already_ingested_cross_source`` (deduped across ingest sources).
A job whose evidence cannot be revalidated is isolated into ``jobs_partial``
and reported as ``HISTORICAL_BACKFILL_PARTIAL`` (exit 0 — a documented
outcome; exit 2 is reserved for unexpected failures).

``--only-mall MALL`` (canary mode) hard-scopes the ENTIRE pass — all four
phases — to one mall through the service-level :class:`CanaryScope` policy
object.  Canary mode requires all three explicit data-source paths
(``CANARY_EXPLICIT_PATHS_REQUIRED`` otherwise), ``--max-jobs-per-pass 1`` and
refuses store/cache paths inside ``--state-runtime-root`` (or named
``state.sqlite3``: ``CANARY_PATH_POLICY_VIOLATION``).  A live canary pass runs
a READ-ONLY preflight (the same-parameter scoped plan-only report over
``mode=ro`` store handles) BEFORE any RW store constructor, ProcessLock,
schema migration or job artifact; a blocked preflight
(``CANARY_MALL_NOT_MAPPED`` / ``CANARY_TARGET_EMPTY`` / ``CANARY_URL_LIMIT_EXCEEDED``
/ ``CANARY_JOB_LIMIT_EXCEEDED`` / ``CANARY_PREFLIGHT_FAILED``) exits 2 with
zero writes.  On PASS the RW live run carries the preflight plan fields plus
``sandbox_schema_migrated``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import customer_prewarm_dispatch as dispatch_service
from app.services.customer_prewarm_dispatch import (
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_MAX_JOBS_PER_PASS,
    CanaryLiveBudget,
    CanaryScope,
    run_prewarm_dispatch_pass,
)
from app.services.customer_prewarm_lifecycle import run_harvest_phase
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.incremental_audit_runtime import (
    DEFAULT_RUNTIME_ROOT,
    business_date_in_shanghai,
    runtime_paths,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from app.services.zhaoshang_history_backfill import (
    BACKFILL_SOURCE,
    backfill_zhaoshang_history,
)
from scripts.zhaoshang_mall_mapping import load_zhaoshang_mall_mappings

PREWARM_STORE_NAME = "customer_prewarm.sqlite3"
SHARED_CACHE_NAME = "shared_crawl_cache.sqlite3"


def run_cross_day_reconcile_phase(**kwargs):
    """Lazy bridge keeping plan-only and unrelated paths adapter-free."""
    from app.services.customer_prewarm_cross_day_reconcile import (
        run_cross_day_reconcile_phase as _run_cross_day_reconcile_phase,
    )

    return _run_cross_day_reconcile_phase(**kwargs)


def run_cross_day_harvest_phase(**kwargs):
    """Lazy bridge for the separate historical harvest/cache phase."""
    from app.services.customer_prewarm_cross_day_reconcile import (
        run_cross_day_harvest_phase as _run_cross_day_harvest_phase,
    )

    return _run_cross_day_harvest_phase(**kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue #10 customer prewarm dispatch: one bounded pass over "
        "the existing zhaoshang lifecycle"
    )
    parser.add_argument(
        "--business-date",
        help="business date YYYY-MM-DD; defaults to today in Asia/Shanghai",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="STEP_1 runtime root (per-day state db, prewarm store, shared "
        f"cache); default: {DEFAULT_RUNTIME_ROOT}",
    )
    parser.add_argument(
        "--state-runtime-root",
        type=Path,
        default=None,
        help="optional override for the STEP1 per-day data source "
        "(<date>/state.sqlite3, per-day lock, zhaoshang job dirs); "
        "defaults to --runtime-root",
    )
    parser.add_argument(
        "--prewarm-store-path",
        type=Path,
        default=None,
        help=f"optional override for the prewarm store; default: "
        f"<runtime-root>/{PREWARM_STORE_NAME}",
    )
    parser.add_argument(
        "--shared-cache-path",
        type=Path,
        default=None,
        help=f"optional override for the shared crawl cache; default: "
        f"<runtime-root>/{SHARED_CACHE_NAME}",
    )
    parser.add_argument(
        "--mapping-path",
        type=Path,
        default=None,
        help="optional override for the zhaoshang mall mapping workbook",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="read-only §26 report: zero writes (no locks, no adapter, state "
        "db opened mode=ro only)",
    )
    parser.add_argument(
        "--no-lifecycle",
        action="store_true",
        help="skip the lifecycle closeout stages (harvest / reconcile / "
        "redispatch); by default the pass reconciles SUBMIT_UNKNOWN jobs, "
        "re-dispatches dead-job candidates and, after the pass released the "
        "locks, harvests live prewarm jobs via the existing primitive",
    )
    parser.add_argument(
        "--backfill-zhaoshang-history",
        type=int,
        default=None,
        metavar="N",
        help="backfill-only mode: re-validate the past N per-day state DBs' "
        "EXPORTED/COMPLETED remote jobs through the harvest_existing "
        "provenance gate (calendar days ending at --business-date inclusive, "
        "1..90) and record only rows cross-proven by a bound SUCCESS crawl "
        "row into the shared cache as zhaoshang-history evidence; the "
        "dispatch pass does not run",
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=DEFAULT_BUDGET_SECONDS,
        help="wall-clock budget for one pass; must stay below the 60s schedule "
        f"interval; default {DEFAULT_BUDGET_SECONDS:g}",
    )
    parser.add_argument(
        "--max-jobs-per-pass",
        type=int,
        default=DEFAULT_MAX_JOBS_PER_PASS,
        help="upper bound of remote jobs CREATED this pass; "
        f"default {DEFAULT_MAX_JOBS_PER_PASS}",
    )
    parser.add_argument(
        "--only-mall",
        default=None,
        metavar="MALL",
        help="canary mode: hard-scope the ENTIRE pass (all four phases) to one "
        "mall; requires explicit --prewarm-store-path/--shared-cache-path/"
        "--state-runtime-root and --max-jobs-per-pass 1",
    )
    parser.add_argument(
        "--canary-max-urls",
        type=int,
        default=1,
        metavar="N",
        help="canary: max remote-interaction URLs (reuse + create + priority "
        "confirm) allowed by preflight; default 1",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="optional path to write the full summary dict as JSON (parents created)",
    )
    return parser


def _backfill_dates(business_date: str, days: int) -> list[str]:
    """The N calendar days ending at business_date, inclusive, ascending."""
    end = datetime.fromisoformat(business_date).date()
    return [
        (end - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)
    ]


def _shared_harvest_deadline(window_started: float, reserve_seconds: float) -> float:
    """Return a bounded lifecycle deadline from the caller's chosen anchor.

    The live CLI now anchors this helper after the dispatch pass returns and
    passes only the pre-reserved lifecycle slice.  Negative reserves clamp to
    the anchor so callers never receive a deadline in the past by arithmetic.
    """
    return float(window_started) + max(0.0, float(reserve_seconds))


def _write_json_output(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _run_backfill(args: argparse.Namespace, business_date: str, runtime_root: Path) -> int:
    """--backfill-zhaoshang-history mode: evidence-based mirror of past exports.

    Read-write stores (the backfill writes the sync ledger + cache rows); the
    same ``--prewarm-store-path``/``--shared-cache-path`` overrides as the
    dispatch mode apply.  ``partial`` (some jobs failed evidence validation)
    is a documented outcome: prominent marker, exit 0.
    """
    dates = _backfill_dates(business_date, args.backfill_zhaoshang_history)
    prewarm_store_path = (
        Path(args.prewarm_store_path).resolve()
        if args.prewarm_store_path is not None
        else runtime_root / PREWARM_STORE_NAME
    )
    shared_cache_path = (
        Path(args.shared_cache_path).resolve()
        if args.shared_cache_path is not None
        else runtime_root / SHARED_CACHE_NAME
    )
    try:
        summary = backfill_zhaoshang_history(
            CustomerPrewarmStore(prewarm_store_path),
            SharedCrawlCache(shared_cache_path),
            runtime_root=runtime_root,
            business_dates=dates,
        )
    except Exception as exc:  # noqa: BLE001 - scheduler-facing failure
        print(
            json.dumps(
                {
                    "business_date": business_date,
                    "outcome": "BACKFILL_FAILED",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    summary["business_date"] = business_date
    summary["mode"] = "backfill-zhaoshang-history"
    summary["backfill"] = True
    summary["backfill_dates"] = dates
    summary["source"] = BACKFILL_SOURCE
    print(
        f"customer_prewarm_dispatch backfill-zhaoshang-history business_date={business_date} "
        f"dates={len(dates)}"
    )
    print(
        f"dates_scanned={summary['dates_scanned']} "
        f"dates_missing={len(summary['dates_missing'])} "
        f"jobs_examined={summary['jobs_examined']} "
        f"jobs_validated={summary['jobs_validated']} "
        f"rows_examined={summary['rows_examined']} "
        f"rows_recorded={summary['rows_recorded']} "
        f"already_ingested_cross_source={summary['already_ingested_cross_source']} "
        f"skipped_no_workbook_match={summary['skipped_no_workbook_match']} "
        f"skipped_price_not_ok={summary['skipped_price_not_ok']} "
        f"transient_failures={summary['transient_failures']} "
        f"deterministic_failures={summary['deterministic_failures']} "
        f"errors={len(summary['errors'])}"
    )
    if summary["partial"]:
        print(
            f"HISTORICAL_BACKFILL_PARTIAL jobs_partial={len(summary['jobs_partial'])} "
            f"(documented outcome: re-run after fixing the listed jobs)"
        )
        for item in summary["jobs_partial"]:
            print(
                f"  partial job {item['job_id']} ({item['task_name'] or 'unnamed'}): "
                f"{item['reason']}"
            )
    if args.json_output is not None:
        _write_json_output(args.json_output, summary)
    return 0


def _print_missing_store_diagnosis(
    business_date: str,
    outcome: str,
    missing_path: Path,
    args: argparse.Namespace,
) -> int:
    """Plan-only diagnosis for a missing store: exit 0, NO file is created."""
    diagnosis = {
        "business_date": business_date,
        "plan_only": True,
        "outcome": outcome,
        "missing_path": str(missing_path),
    }
    print(json.dumps(diagnosis, ensure_ascii=False, sort_keys=True))
    if args.json_output is not None:
        _write_json_output(args.json_output, diagnosis)
    return 0


def _print_canary_missing_store(
    business_date: str,
    outcome: str,
    missing_path: Path,
    args: argparse.Namespace,
) -> int:
    """Canary live diagnosis for a missing store: fail-closed exit 2 (the
    plan-only exit-0 semantics never applies to a live canary run)."""
    diagnosis = {
        "business_date": business_date,
        "canary_only_mall": args.only_mall,
        "outcome": outcome,
        "missing_path": str(missing_path),
    }
    print(json.dumps(diagnosis, ensure_ascii=False, sort_keys=True))
    if args.json_output is not None:
        _write_json_output(args.json_output, diagnosis)
    return 2


def _store_has_remote_job_column(path: Path) -> bool:
    """``mode=ro`` probe: does the prewarm store already carry the
    ``remote_job_id`` column?  A missing file never creates one (False)."""
    path = Path(path)
    if not path.is_file():
        return False
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(prewarm_candidates)").fetchall()
        }
        return "remote_job_id" in columns
    finally:
        connection.close()


def _run_dispatch(args: argparse.Namespace, business_date: str, runtime_root: Path) -> int:
    mappings = load_zhaoshang_mall_mappings(args.mapping_path)
    state_runtime_root = (
        Path(args.state_runtime_root).resolve()
        if args.state_runtime_root is not None
        else None
    )
    prewarm_store_path = (
        Path(args.prewarm_store_path).resolve()
        if args.prewarm_store_path is not None
        else runtime_root / PREWARM_STORE_NAME
    )
    shared_cache_path = (
        Path(args.shared_cache_path).resolve()
        if args.shared_cache_path is not None
        else runtime_root / SHARED_CACHE_NAME
    )
    canary_scope = (
        CanaryScope(only_mall=str(args.only_mall).strip(), max_urls=args.canary_max_urls)
        if args.only_mall
        else None
    )
    if canary_scope is not None:
        # Path policy: canary stores must never alias the STEP1 state runtime
        # (that layout belongs to the production dispatcher, never a canary).
        state_root = (
            Path(args.state_runtime_root).resolve()
            if args.state_runtime_root is not None
            else runtime_root
        )
        for label, p in (("prewarm-store", prewarm_store_path), ("shared-cache", shared_cache_path)):
            if p == state_root or state_root in p.parents or p.name == "state.sqlite3":
                print(
                    json.dumps(
                        {
                            "outcome": "CANARY_PATH_POLICY_VIOLATION",
                            "path": str(p),
                            "state_runtime_root": str(state_root),
                            "role": label,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 2
    try:
        if args.plan_only:
            # Provably zero construction: read-only store handles never mkdir,
            # never initialize a schema, never touch a missing file — a missing
            # store is a diagnosis (prewarm store reported first), never a fresh
            # empty store.
            try:
                prewarm_store = CustomerPrewarmStore.open_read_only(prewarm_store_path)
            except FileNotFoundError:
                return _print_missing_store_diagnosis(
                    business_date, "PREWARM_STORE_MISSING", prewarm_store_path, args
                )
            try:
                shared_cache = SharedCrawlCache.open_read_only(shared_cache_path)
            except FileNotFoundError:
                return _print_missing_store_diagnosis(
                    business_date, "SHARED_CACHE_MISSING", shared_cache_path, args
                )
            summary = run_prewarm_dispatch_pass(
                prewarm_store,
                shared_cache,
                business_date=business_date,
                runtime_root=runtime_root,
                state_runtime_root=state_runtime_root,
                mappings=mappings,
                budget_seconds=args.budget_seconds,
                max_jobs_per_pass=args.max_jobs_per_pass,
                plan_only=True,
                canary_scope=canary_scope,
            )
            if canary_scope is not None and summary.get("canary_preflight", "PASS") != "PASS":
                print(f"canary preflight FAILED outcome={summary['canary_preflight']}")
                if args.json_output is not None:
                    _write_json_output(args.json_output, summary)
                return 2
        elif canary_scope is not None:
            # READ-ONLY preflight BEFORE any RW store constructor, ProcessLock,
            # schema migration or job artifact: the same-parameter scoped
            # plan-only pass over mode=ro handles, so a blocked canary leaves
            # every store byte-identical.
            try:
                ro_prewarm = CustomerPrewarmStore.open_read_only(prewarm_store_path)
            except FileNotFoundError:
                return _print_canary_missing_store(
                    business_date, "PREWARM_STORE_MISSING", prewarm_store_path, args
                )
            try:
                ro_cache = SharedCrawlCache.open_read_only(shared_cache_path)
            except FileNotFoundError:
                return _print_canary_missing_store(
                    business_date, "SHARED_CACHE_MISSING", shared_cache_path, args
                )
            had_remote_job_column = _store_has_remote_job_column(prewarm_store_path)
            preflight = run_prewarm_dispatch_pass(
                ro_prewarm,
                ro_cache,
                business_date=business_date,
                runtime_root=runtime_root,
                state_runtime_root=state_runtime_root,
                mappings=mappings,
                budget_seconds=args.budget_seconds,
                max_jobs_per_pass=args.max_jobs_per_pass,
                plan_only=True,
                canary_scope=canary_scope,
            )
            if preflight.get("canary_preflight", "PASS") != "PASS":
                print(
                    "customer_prewarm_dispatch canary preflight BLOCKED "
                    f"outcome={preflight['canary_preflight']}"
                )
                if args.json_output is not None:
                    _write_json_output(args.json_output, preflight)
                return 2
            # preflight PASS -> RW live, carrying the preflight target set as a
            # hard budget: any live interaction outside it fails closed.
            canary_budget = CanaryLiveBudget.from_plan(preflight, canary_scope)
            rw_prewarm = CustomerPrewarmStore(prewarm_store_path)
            rw_cache = SharedCrawlCache(shared_cache_path)
            summary = run_prewarm_dispatch_pass(
                rw_prewarm,
                rw_cache,
                business_date=business_date,
                runtime_root=runtime_root,
                state_runtime_root=state_runtime_root,
                mappings=mappings,
                budget_seconds=args.budget_seconds,
                max_jobs_per_pass=args.max_jobs_per_pass,
                plan_only=False,
                canary_scope=canary_scope,
                canary_budget=canary_budget,
            )
            for key in (
                "existing_remote_reused_planned",
                "new_remote_urls_planned",
                "new_remote_jobs_planned",
                "priority_updates_planned",
                "remote_interaction_urls",
                "canary_target_urls",
                "candidates_seen_total",
                "candidates_in_scope",
                "skipped_by_canary_filter",
            ):
                summary[key] = preflight.get(key, summary.get(key, 0))
            summary["sandbox_schema_migrated"] = (
                not had_remote_job_column and _store_has_remote_job_column(prewarm_store_path)
            )
            if any(
                "CanaryScopeViolation" in error or "CANARY_LIVE_SCOPE_DRIFT" in error
                for error in summary.get("phase_errors", [])
            ):
                # Drift hard-fail: the live pass met a state outside the
                # read-only preflight target set — scheduler must see exit 2.
                print(
                    "customer_prewarm_dispatch canary live SCOPE_DRIFT "
                    "CANARY_LIVE_SCOPE_DRIFT; fail closed"
                )
                if args.json_output is not None:
                    _write_json_output(args.json_output, summary)
                return 2
        else:
            prewarm_store = CustomerPrewarmStore(prewarm_store_path)
            shared_cache = SharedCrawlCache(shared_cache_path)
            lifecycle_on = not args.no_lifecycle
            # With the lifecycle stages on, ONE adapter instance is shared by
            # the pass and the harvest stage (its constructor performs no
            # remote I/O; construction through the dispatch service attribute
            # keeps the established test seam).  Without them the pass keeps
            # its lazy construction behavior unchanged.
            adapter = (
                dispatch_service.CnncLegacyZhaoshangAdapter(mappings=mappings)
                if lifecycle_on
                else None
            )
            # Reserve a bounded lifecycle window from the dispatch budget.
            # The lifecycle deadline is anchored AFTER the dispatch pass
            # returns so a late remote timeout cannot consume this reserve.
            lifecycle_reserve = (
                min(24.0, float(args.budget_seconds) * 0.6) if lifecycle_on else 0.0
            )
            summary = run_prewarm_dispatch_pass(
                prewarm_store,
                shared_cache,
                business_date=business_date,
                runtime_root=runtime_root,
                state_runtime_root=state_runtime_root,
                mappings=mappings,
                adapter=adapter,
                budget_seconds=float(args.budget_seconds) - lifecycle_reserve,
                max_jobs_per_pass=args.max_jobs_per_pass,
                plan_only=False,
            )
            pass_outcome = str(summary.get("outcome") or "")
            if lifecycle_on and pass_outcome in {"OK", "LOCK_BUSY"}:
                # A1 harvest stage: AFTER the pass returned (it owns its own
                # ProcessLock + lease, so it must never run under the
                # dispatcher's day lock).  Start the already-reserved lifecycle
                # window NOW instead of anchoring it at process start.  This
                # preserves the pass -> reconcile -> historical harvest safety
                # order while preventing a late remote timeout in the pass from
                # starving all historical work.
                state_root = (
                    state_runtime_root if state_runtime_root is not None else runtime_root
                )
                day_paths = runtime_paths(business_date, state_root)
                lifecycle_deadline = _shared_harvest_deadline(
                    time.monotonic(), lifecycle_reserve
                )
                lifecycle_now = time.monotonic()
                lifecycle_remaining = max(0.0, lifecycle_deadline - lifecycle_now)
                reconcile_slice = min(
                    lifecycle_remaining,
                    10.0 if lifecycle_remaining >= 20.0 else max(5.0, lifecycle_remaining * 0.6),
                )
                reconcile_deadline = lifecycle_now + reconcile_slice
                historical_deadline = reconcile_deadline + max(
                    0.0, lifecycle_deadline - reconcile_deadline
                ) / 2
                run_cross_day_reconcile_phase(
                    prewarm_store=prewarm_store,
                    current_business_date=business_date,
                    state_runtime_root=state_root,
                    get_adapter=lambda: adapter,
                    deadline=reconcile_deadline,
                    monotonic_value=time.monotonic,
                    summary=summary,
                )
                run_cross_day_harvest_phase(
                    prewarm_store=prewarm_store,
                    shared_cache=shared_cache,
                    current_business_date=business_date,
                    state_runtime_root=state_root,
                    get_adapter=lambda: adapter,
                    deadline=historical_deadline,
                    monotonic_value=time.monotonic,
                    summary=summary,
                )
                if pass_outcome == "OK":
                    run_harvest_phase(
                        day_paths.state_path,
                        day_paths.lock_path,
                        business_date,
                        deadline=lifecycle_deadline,
                        monotonic_value=time.monotonic,
                        summary=summary,
                        adapter=adapter,
                        mappings=mappings,
                    )
    except Exception as exc:  # noqa: BLE001 - scheduler-facing failure
        print(
            json.dumps(
                {
                    "business_date": business_date,
                    "outcome": "DISPATCH_FAILED",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    summary["mappings"] = len(mappings)
    print(
        f"customer_prewarm_dispatch business_date={business_date} "
        f"plan_only={bool(args.plan_only)} outcome={summary['outcome']}"
    )
    print(
        f"absorbed={summary.get('absorbed', 0)} "
        f"cache_hit_after_recheck={summary.get('cache_hit_after_recheck', 0)} "
        f"staples_skipped={summary.get('staples_skipped', 0)} "
        f"manual_skipped={summary.get('manual_skipped', 0)} "
        f"routing_blocked={summary.get('routing_blocked', 0)} "
        f"mapped_zhaoshang={summary.get('mapped_zhaoshang', 0)} "
        f"existing_remote_reused={summary.get('existing_remote_reused', 0)} "
        f"new_remote_urls={summary.get('new_remote_urls', 0)} "
        f"jobs_created={summary.get('jobs_created', 0)} "
        f"submit_unknown={summary.get('submit_unknown', 0)} "
        f"submit_not_attempted={summary.get('submit_not_attempted', 0)} "
        f"priority_confirmed={summary.get('priority_confirmed', 0)} "
        f"priority_boosted={summary.get('priority_boosted', 0)} "
        f"priority_unconfirmed={summary.get('priority_unconfirmed', 0)} "
        f"phase_errors={len(summary.get('phase_errors', []))} "
        f"reconciled={summary.get('reconciled_found', 0)} "
        f"cross_day_reconciled={summary.get('cross_day_reconciled_found', 0)} "
        f"cross_day_not_found={summary.get('cross_day_reconcile_not_found', 0)} "
        f"cross_day_lock_busy={summary.get('cross_day_reconcile_lock_busy', 0)} "
        f"redispatched={summary.get('redispatched_urls', 0)} "
        f"harvested={summary.get('harvested', 0)} "
        f"cross_day_harvested={summary.get('cross_day_harvested', 0)} "
        f"cross_day_cache_synced={summary.get('cross_day_harvest_cache_synced', 0)}"
    )
    if summary.get("lifecycle_planned"):
        planned = summary["lifecycle_planned"]
        print(
            f"lifecycle_planned harvest_jobs={planned.get('harvest_jobs', 0)} "
            f"reconcile_jobs={planned.get('reconcile_jobs', 0)} "
            f"cross_day_reconcile_jobs={planned.get('cross_day_reconcile_jobs', 0)} "
            f"cross_day_harvest_jobs={planned.get('cross_day_harvest_jobs', 0)} "
            f"redispatch_urls={planned.get('redispatch_urls', 0)} "
            f"redispatch_blocked={planned.get('redispatch_blocked', 0)}"
        )
    if canary_scope is not None:
        print(
            f"canary only_mall={summary.get('only_mall', canary_scope.only_mall)} "
            f"candidates_in_scope={summary.get('candidates_in_scope', 0)} "
            f"skipped_by_canary_filter={summary.get('skipped_by_canary_filter', 0)} "
            f"remote_interaction_urls={summary.get('remote_interaction_urls', 0)} "
            f"other_remote_tasks_created={summary.get('other_remote_tasks_created', 0)} "
            f"other_priority_changed={summary.get('other_priority_changed', 0)} "
            f"sandbox_schema_migrated={summary.get('sandbox_schema_migrated', False)}"
        )
    if args.json_output is not None:
        _write_json_output(args.json_output, summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    business_date = args.business_date or business_date_in_shanghai()
    # Hard configuration errors fail closed with exit code 2; expected runtime
    # conditions (lock conflict, missing state db) do not.
    try:
        if datetime.fromisoformat(business_date).date().isoformat() != business_date:
            raise ValueError(business_date)
    except ValueError:
        print(f"ERROR invalid --business-date {business_date!r}; expected YYYY-MM-DD")
        return 2
    if args.budget_seconds <= 0:
        print("ERROR --budget-seconds must be positive")
        return 2
    if args.max_jobs_per_pass < 1:
        print("ERROR --max-jobs-per-pass must be at least 1")
        return 2
    if args.backfill_zhaoshang_history is not None and not (
        1 <= args.backfill_zhaoshang_history <= 90
    ):
        print("ERROR --backfill-zhaoshang-history must be between 1 and 90")
        return 2
    if args.backfill_zhaoshang_history is not None and args.plan_only:
        print("ERROR --backfill-zhaoshang-history is mutually exclusive with --plan-only")
        return 2
    if args.only_mall:
        if args.canary_max_urls < 1:
            print("ERROR --canary-max-urls must be at least 1")
            return 2
        if args.max_jobs_per_pass > 1:
            print("ERROR --max-jobs-per-pass must be 1 with --only-mall")
            return 2
        if args.backfill_zhaoshang_history is not None:
            print("ERROR --only-mall is mutually exclusive with --backfill-zhaoshang-history")
            return 2
        if (
            args.prewarm_store_path is None
            or args.shared_cache_path is None
            or args.state_runtime_root is None
        ):
            print(
                "ERROR canary mode requires explicit --prewarm-store-path, "
                "--shared-cache-path and --state-runtime-root "
                "(CANARY_EXPLICIT_PATHS_REQUIRED)"
            )
            return 2

    runtime_root = Path(args.runtime_root).resolve()
    if args.backfill_zhaoshang_history is not None:
        return _run_backfill(args, business_date, runtime_root)
    return _run_dispatch(args, business_date, runtime_root)


if __name__ == "__main__":
    raise SystemExit(main())
