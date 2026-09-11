"""CLI for the Issue #11 staples local prewarm: plan-only report or one bounded LOCAL pass.

Live mode is a LOCAL-ONLY sidecar over ``run_staples_prewarm``: it routes
stbchina.cn (史泰博) candidates, rechecks the shared crawl cache, retries
deferred cache syncs and crawls one bounded batch through the existing stbchina
wrapper.  It NEVER touches any 招商局 (Zhaoshang) capability — no remote task,
no zhaoshang adapter, and a staples failure is never re-routed to zhaoshang.

``--plan-only`` is TRUE read-only (§ ticket 27/28): it never constructs
``CustomerPrewarmStore`` / ``SharedCrawlCache`` / ``StaplesPrewarmStore``, never
takes the process lock, never creates or modifies any database file, and never
writes anything except the optional ``--json-output`` report (which must not
equal either database path).  Missing databases are reported via markers
(``PREWARM_STORE_MISSING`` / ``CACHE_MISSING``) and nothing is created.

中文关键句:live 模式是史泰博本地旁路,绝不触碰任何招商局能力;
``--plan-only`` 是真只读——不建 Store、不建库、不加锁、不写任何文件
(唯一例外是可选的 ``--json-output`` 报告,且该路径不得等于任一数据库路径)。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.daily_audit_store import LeaseUnavailable, ProcessLock
from app.services.incremental_audit_runtime import (
    DEFAULT_RUNTIME_ROOT,
    business_date_in_shanghai,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from app.services.staples_prewarm import (
    StaplesLocalCrawler,
    StaplesPrewarmStore,
    run_staples_prewarm,
    staples_plan_readonly,
)

PREWARM_STORE_NAME = "customer_prewarm.sqlite3"
SHARED_CACHE_NAME = "shared_crawl_cache.sqlite3"
STAPLES_LOCK_NAME = "staples_prewarm.lock"
DEFAULT_WORK_DIR_NAME = "staples_prewarm"
DEFAULT_MAX_JOBS = 20
DEFAULT_ROUTE_LIMIT = 200


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue #11 staples local prewarm: plan-only read-only report "
        "or one bounded LOCAL pass (never zhaoshang)"
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="TRUE read-only plan report: no stores, no lock, no writes; missing "
        "databases are reported and nothing is created",
    )
    parser.add_argument(
        "--business-date",
        help="business date YYYY-MM-DD; defaults to today in Asia/Shanghai",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="runtime root holding the prewarm store, shared cache and lock; "
        f"default: {DEFAULT_RUNTIME_ROOT}",
    )
    parser.add_argument(
        "--prewarm-store-path",
        type=Path,
        default=None,
        help=f"prewarm store sqlite path; default <runtime_root>/{PREWARM_STORE_NAME}",
    )
    parser.add_argument(
        "--shared-cache-path",
        type=Path,
        default=None,
        help=f"shared crawl cache sqlite path; default <runtime_root>/{SHARED_CACHE_NAME}",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="live mode only: crawler work directory (result xlsx etc.); "
        f"default <runtime_root>/{DEFAULT_WORK_DIR_NAME}",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=DEFAULT_MAX_JOBS,
        help="live mode only: upper bound of jobs crawled this pass; "
        f"default {DEFAULT_MAX_JOBS}",
    )
    parser.add_argument(
        "--route-limit",
        type=int,
        default=DEFAULT_ROUTE_LIMIT,
        help="live mode only: upper bound of candidates routed this pass; "
        f"default {DEFAULT_ROUTE_LIMIT}",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="optional path to write the full summary/plan dict as JSON "
        "(parents created); in plan-only this must not equal either database path",
    )
    return parser


def _write_json_output(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _validate_business_date(business_date: str) -> bool:
    try:
        if datetime.fromisoformat(business_date).date().isoformat() != business_date:
            raise ValueError(business_date)
    except ValueError:
        return False
    return True


def _run_plan_only(
    args: argparse.Namespace,
    business_date: str,
    prewarm_path: Path,
    cache_path: Path,
) -> int:
    """TRUE read-only plan: no store classes, no lock, no writes except --json-output."""
    plan = staples_plan_readonly(prewarm_path, cache_path)
    plan["mode"] = "plan-only"
    plan["business_date"] = business_date
    print(
        f"staples_prewarm_run plan-only business_date={business_date} "
        f"prewarm_store={plan['prewarm_store']} cache={plan['cache']} "
        f"candidates_total={plan['candidates_total']} "
        f"staples_candidates={plan['staples_candidates']} "
        f"unique_staples_urls={plan['unique_staples_urls']} "
        f"duplicate_documents={plan['duplicate_documents']} "
        f"cache_hit_after_recheck={plan['cache_hit_after_recheck']} "
        f"local_retry_planned={plan['local_retry_planned']} "
        f"local_cache_sync_pending={plan['local_cache_sync_pending']} "
        f"local_final_failed={plan['local_final_failed']} "
        f"local_crawl_planned={plan['local_crawl_planned']} "
        f"crawler_available={plan['crawler_available']}"
    )
    if args.json_output is not None:
        _write_json_output(args.json_output, plan)
    if plan["prewarm_store"] != "OK" or plan["cache"] != "OK":
        return 2
    return 0


def _run_live(
    args: argparse.Namespace,
    business_date: str,
    prewarm_path: Path,
    cache_path: Path,
    work_dir: Path,
) -> int:
    """One bounded LOCAL pass; never zhaoshang; constructor failures exit 2."""
    try:
        summary = run_staples_prewarm(
            prewarm_store=CustomerPrewarmStore(prewarm_path),
            shared_cache=SharedCrawlCache(cache_path),
            staples_store=StaplesPrewarmStore(prewarm_path),
            crawler=StaplesLocalCrawler(work_dir=work_dir),
            work_dir=work_dir,
            route_limit=int(args.route_limit),
            max_jobs=int(args.max_jobs),
        )
    except Exception as exc:  # noqa: BLE001 - scheduler-facing failure
        print(
            json.dumps(
                {
                    "business_date": business_date,
                    "outcome": "STAPLES_PREWARM_FAILED",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    summary["mode"] = "live"
    summary["business_date"] = business_date
    summary["prewarm_store_path"] = str(prewarm_path)
    summary["shared_cache_path"] = str(cache_path)
    print(
        f"staples_prewarm_run live business_date={business_date} "
        f"routed={summary['routed']} "
        f"recheck_hits={summary['recheck_hits']} "
        f"cache_sync_succeeded={summary['cache_sync_succeeded']} "
        f"cache_sync_pending={summary['cache_sync_pending']} "
        f"jobs_claimed={summary['jobs_claimed']} "
        f"crawl_success={summary['crawl_success']} "
        f"cache_hit_after_crawl={summary['cache_hit_after_crawl']} "
        f"transient_failures={summary['transient_failures']} "
        f"final_failures={summary['final_failures']} "
        f"errors={len(summary['errors'])}"
    )
    if args.json_output is not None:
        _write_json_output(args.json_output, summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    business_date = args.business_date or business_date_in_shanghai()
    # Hard configuration errors fail closed with exit code 2; expected runtime
    # conditions (lock conflict, missing databases) do not fail the plan report.
    if not _validate_business_date(business_date):
        print(f"ERROR invalid --business-date {business_date!r}; expected YYYY-MM-DD")
        return 2
    if args.max_jobs <= 0:
        print("ERROR --max-jobs must be positive")
        return 2
    if args.route_limit <= 0:
        print("ERROR --route-limit must be positive")
        return 2

    runtime_root = Path(args.runtime_root).resolve()
    prewarm_path = (
        Path(args.prewarm_store_path).resolve()
        if args.prewarm_store_path is not None
        else runtime_root / PREWARM_STORE_NAME
    )
    cache_path = (
        Path(args.shared_cache_path).resolve()
        if args.shared_cache_path is not None
        else runtime_root / SHARED_CACHE_NAME
    )
    work_dir = (
        Path(args.work_dir) if args.work_dir is not None else runtime_root / DEFAULT_WORK_DIR_NAME
    )

    if args.plan_only:
        # §28: the only allowed write is the optional --json-output report, and
        # it must never be one of the database paths (fail closed before any IO).
        if args.json_output is not None and Path(args.json_output).resolve() in (
            prewarm_path,
            cache_path,
        ):
            print(
                f"ERROR --json-output {args.json_output} equals a database path; "
                "plan-only must never write a database file"
            )
            return 2
        return _run_plan_only(args, business_date, prewarm_path, cache_path)

    lock = ProcessLock(runtime_root / STAPLES_LOCK_NAME)
    try:
        lock.acquire(
            owner={
                "pid": os.getpid(),
                "command": "staples_prewarm_run",
                "business_date": business_date,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "invocation": "cli",
            }
        )
    except LeaseUnavailable:
        print("SKIPPED_ALREADY_RUNNING")
        return 0
    try:
        return _run_live(args, business_date, prewarm_path, cache_path, work_dir)
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
