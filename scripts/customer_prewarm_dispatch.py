"""CLI for the Issue #10 customer prewarm dispatch.

Default mode runs ONE bounded dispatch pass (route → reuse/create → submit →
priority boost → cache absorb) that reuses the existing STEP1 zhaoshang
lifecycle; it never invents a second remote-task store.  Overlap is prevented
twice over: the dispatcher's own instance lock prints
``SKIPPED_ALREADY_RUNNING``, and STEP1's per-day process lock / durable lease
prints ``LOCK_BUSY`` — both exit 0 (expected runtime conditions retry on the
next scheduled pass).

``--plan-only`` builds the §26 read-only report: zero writes anywhere (no
candidate status changes, no lock files, no adapter construction, state DB
accessed ``mode=ro`` only).

``--backfill-zhaoshang-history N`` runs ONLY the zhaoshang-history backfill:
past N calendar days (ending at ``--business-date`` inclusive) per-day state
DBs are mirrored into the shared cache with
``backfill_daily_history(source="zhaoshang-history")``; the dispatch pass does
not run.  Idempotent: re-runs report the rows as ``already_synced``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.customer_prewarm_dispatch import (
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_MAX_JOBS_PER_PASS,
    run_prewarm_dispatch_pass,
)
from app.services.customer_prewarm_discovery import backfill_daily_history
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.incremental_audit_runtime import (
    DEFAULT_RUNTIME_ROOT,
    business_date_in_shanghai,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts.zhaoshang_mall_mapping import load_zhaoshang_mall_mappings

PREWARM_STORE_NAME = "customer_prewarm.sqlite3"
SHARED_CACHE_NAME = "shared_crawl_cache.sqlite3"
BACKFILL_SOURCE = "zhaoshang-history"


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
        "--backfill-zhaoshang-history",
        type=int,
        default=None,
        metavar="N",
        help="backfill-only mode: mirror historical zhaoshang successes from "
        "the past N per-day state DBs (calendar days ending at "
        "--business-date inclusive, 1..90) into the shared cache as "
        "zhaoshang-history rows; the dispatch pass does not run",
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


def _write_json_output(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _run_backfill(args: argparse.Namespace, business_date: str, runtime_root: Path) -> int:
    """--backfill-zhaoshang-history mode: only mirror past state DBs."""
    dates = _backfill_dates(business_date, args.backfill_zhaoshang_history)
    try:
        summary = backfill_daily_history(
            CustomerPrewarmStore(runtime_root / PREWARM_STORE_NAME),
            SharedCrawlCache(runtime_root / SHARED_CACHE_NAME),
            runtime_root=runtime_root,
            business_dates=dates,
            source=BACKFILL_SOURCE,
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
        f"ok_synced={summary['ok_synced']} "
        f"already_synced={summary['already_synced']} "
        f"transient_failures={summary['transient_failures']} "
        f"deterministic_failures={summary['deterministic_failures']} "
        f"errors={len(summary['errors'])}"
    )
    if args.json_output is not None:
        _write_json_output(args.json_output, summary)
    return 0


def _run_dispatch(args: argparse.Namespace, business_date: str, runtime_root: Path) -> int:
    mappings = load_zhaoshang_mall_mappings(args.mapping_path)
    try:
        summary = run_prewarm_dispatch_pass(
            CustomerPrewarmStore(runtime_root / PREWARM_STORE_NAME),
            SharedCrawlCache(runtime_root / SHARED_CACHE_NAME),
            business_date=business_date,
            runtime_root=runtime_root,
            mappings=mappings,
            budget_seconds=args.budget_seconds,
            max_jobs_per_pass=args.max_jobs_per_pass,
            plan_only=bool(args.plan_only),
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
        f"priority_boosted={summary.get('priority_boosted', 0)} "
        f"priority_unconfirmed={summary.get('priority_unconfirmed', 0)} "
        f"phase_errors={len(summary.get('phase_errors', []))}"
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

    runtime_root = Path(args.runtime_root).resolve()
    if args.backfill_zhaoshang_history is not None:
        return _run_backfill(args, business_date, runtime_root)
    return _run_dispatch(args, business_date, runtime_root)


if __name__ == "__main__":
    raise SystemExit(main())
