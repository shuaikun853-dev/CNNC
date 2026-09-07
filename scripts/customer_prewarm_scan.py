"""CLI for the Issue #9 customer prewarm scan: one bounded, read-only pass.

The scanner is a sidecar of STEP_1: it never discovers new business work,
never creates a remote task, and never writes the STEP_1 state database.
Every minute it runs phases A-D (daily sync, catch-up intake, detail scan,
candidate refresh) against the shared crawl cache under a wall-clock budget.
Overlap is prevented by a non-blocking process lock; a second instance prints
``SKIPPED_ALREADY_RUNNING`` and exits 0.  Dry-run mode routes every write into
an explicit sandbox directory and keeps the shared cache read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.customer_prewarm_discovery import run_prewarm_scan
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.daily_audit_store import LeaseUnavailable, ProcessLock
from app.services.incremental_audit_runtime import (
    DEFAULT_RUNTIME_ROOT,
    PROJECT_DATA_ROOT,
    _apply_cnnc_at,
    business_date_in_shanghai,
    runtime_paths,
    shared_cnnc_provider,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts.audit_saas_excel_file import call_detail_api
from scripts.curl_utils import load_curl_command
from scripts.zhaoshang_mall_mapping import load_zhaoshang_mall_mappings


DETAIL_LOCK_NAME = "detail.lock"
PREWARM_STORE_NAME = "customer_prewarm.sqlite3"
PREWARM_LOCK_NAME = "customer_prewarm.lock"
SHARED_CACHE_NAME = "shared_crawl_cache.sqlite3"
DEFAULT_BUDGET_SECONDS = 50.0
DEFAULT_MAX_DOCUMENTS = 10
DEFAULT_DETAIL_TIMEOUT_SECONDS = 30.0


def _disabled_detail_fetch(_document_id: str, _four_category_id: str) -> dict[str, object]:
    """Stand-in fetcher used when detail scanning is disabled.

    Phase C is skipped by passing ``max_documents=0`` so no pending document
    consumes an attempt; this fetcher exists only as a never-called fallback.
    """
    return {"ok": False, "payload": None, "error": "detail disabled"}


def _build_detail_fetcher(
    template: dict,
    *,
    detail_lock_path: Path,
    detail_timeout: float,
    provider: object,
):
    """Read-only detail fetcher mirroring the runtime ``_detail_worker`` auth.

    Per call it takes the global detail lock with a short bounded wait (the
    runtime detail worker owns it while ticking), applies the current access
    token, and on a single 401 rejection refreshes once and resends once.
    """
    template_config = dict(template)

    def detail_fetch(document_id: str, four_category_id: str) -> dict[str, object]:
        deadline = time.monotonic() + detail_timeout
        lock = ProcessLock(detail_lock_path)
        while True:
            try:
                lock.acquire()
                break
            except LeaseUnavailable:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"ok": False, "payload": None, "error": "detail lock busy"}
                time.sleep(min(0.05, remaining))
        try:
            config = dict(template_config)
            current_at = None
            if provider is not None:
                current_at = provider.get_access_token()
                _apply_cnnc_at(config, current_at)
            response = call_detail_api(
                config,
                document_id,
                four_category_id,
                timeout=detail_timeout,
            )
            if (
                provider is not None
                and not response.get("ok")
                and response.get("http_status") == 401
            ):
                # AT rejected over HTTP: one refresh and one resend; the second
                # result is authoritative (same single-retry contract as the
                # runtime detail worker).  provider.refresh raises on failure.
                current_at = provider.refresh(
                    stale_access_token=current_at,
                    deadline_monotonic=deadline,
                )
                config = dict(template_config)
                _apply_cnnc_at(config, current_at)
                response = call_detail_api(
                    config,
                    document_id,
                    four_category_id,
                    timeout=detail_timeout,
                )
        finally:
            lock.release()
        return {
            "ok": bool(response.get("ok")),
            "payload": response.get("payload"),
            "error": response.get("error"),
        }

    return detail_fetch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue #9 customer prewarm scan: bounded read-only sidecar pass"
    )
    parser.add_argument(
        "--business-date",
        help="business date YYYY-MM-DD; defaults to today in Asia/Shanghai",
    )
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=DEFAULT_RUNTIME_ROOT,
        help="STEP_1 runtime root (state db, detail lock, prewarm store); "
        f"default: {DEFAULT_RUNTIME_ROOT}",
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=DEFAULT_BUDGET_SECONDS,
        help="wall-clock budget for one pass; must stay below the 60s schedule "
        f"interval; default {DEFAULT_BUDGET_SECONDS:g}",
    )
    parser.add_argument(
        "--max-documents",
        type=int,
        default=DEFAULT_MAX_DOCUMENTS,
        help="upper bound of documents detailed this pass; "
        f"default {DEFAULT_MAX_DOCUMENTS}",
    )
    parser.add_argument(
        "--refresh-limit",
        type=int,
        default=None,
        help="optional bound on Phase D pending-candidate refreshes",
    )
    parser.add_argument(
        "--detail-timeout",
        type=float,
        default=DEFAULT_DETAIL_TIMEOUT_SECONDS,
        help=f"per-detail bounded wait including the global detail lock; default {DEFAULT_DETAIL_TIMEOUT_SECONDS:g}",
    )
    parser.add_argument(
        "--skip-detail",
        action="store_true",
        help="skip Phase C entirely: the pass uses max_documents=0 so no "
        "pending document consumes an attempt and no detail request is made",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="route prewarm writes into the sandbox root and keep the shared "
        "crawl cache read-only (lookup only, record_success never called)",
    )
    parser.add_argument(
        "--sandbox-root",
        type=Path,
        default=None,
        help="dry-run only write target; default: <runtime_root>/prewarm_dry_run "
        "(only used with --dry-run)",
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        default=None,
        help="shared crawl cache path; default <runtime_root>/shared_crawl_cache.sqlite3, "
        "in dry-run <sandbox_root>/shared_crawl_cache.sqlite3",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="optional path to write the full summary dict as JSON (parents created)",
    )
    parser.add_argument(
        "--mapping-path",
        type=Path,
        default=None,
        help="optional override for the zhaoshang mall mapping workbook",
    )
    return parser


def _resolve_paths(args: argparse.Namespace, business_date: str):
    """Resolve all concrete paths; returns a dict of resolved Path values."""
    runtime_root = Path(args.runtime_root).resolve()
    paths = runtime_paths(business_date, runtime_root)
    sandbox_root = (
        Path(args.sandbox_root).resolve()
        if args.sandbox_root is not None
        else runtime_root / "prewarm_dry_run"
    )
    if args.dry_run:
        prewarm_store_path = sandbox_root / PREWARM_STORE_NAME
        lock_path = sandbox_root / PREWARM_LOCK_NAME
    else:
        prewarm_store_path = runtime_root / PREWARM_STORE_NAME
        lock_path = runtime_root / PREWARM_LOCK_NAME
    if args.cache_path is not None:
        cache_path = Path(args.cache_path).resolve()
    elif args.dry_run:
        cache_path = sandbox_root / SHARED_CACHE_NAME
    else:
        cache_path = runtime_root / SHARED_CACHE_NAME
    return {
        "runtime_root": runtime_root,
        "sandbox_root": sandbox_root,
        "state_db_path": paths.state_path,
        "prewarm_store_path": prewarm_store_path,
        "lock_path": lock_path,
        "cache_path": cache_path,
        "detail_lock_path": runtime_root / DETAIL_LOCK_NAME,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    business_date = args.business_date or business_date_in_shanghai()
    # Hard configuration errors fail closed with exit code 2; expected runtime
    # conditions (lock conflict, missing state db, missing template) do not.
    try:
        if datetime.fromisoformat(business_date).date().isoformat() != business_date:
            raise ValueError(business_date)
    except ValueError:
        print(f"ERROR invalid --business-date {business_date!r}; expected YYYY-MM-DD")
        return 2
    if args.budget_seconds <= 0:
        print("ERROR --budget-seconds must be positive")
        return 2
    if args.max_documents < 0:
        print("ERROR --max-documents must not be negative")
        return 2
    if args.refresh_limit is not None and args.refresh_limit < 0:
        print("ERROR --refresh-limit must not be negative")
        return 2
    if args.detail_timeout <= 0:
        print("ERROR --detail-timeout must be positive")
        return 2
    if args.sandbox_root is not None and not args.dry_run:
        print("ERROR --sandbox-root is only used with --dry-run")
        return 2

    resolved = _resolve_paths(args, business_date)
    lock = ProcessLock(resolved["lock_path"])
    try:
        lock.acquire(
            owner={
                "pid": os.getpid(),
                "command": "customer_prewarm_scan",
                "business_date": business_date,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "invocation": "cli",
            }
        )
    except LeaseUnavailable:
        print("SKIPPED_ALREADY_RUNNING")
        return 0
    try:
        # Detail phase C is skipped by max_documents=0 (no attempts consumed);
        # a missing detail template degrades to the same behaviour.
        detail_fetch = _disabled_detail_fetch
        max_documents = 0
        if not args.skip_detail:
            template_path = PROJECT_DATA_ROOT / "detail_request.curl"
            try:
                template = (
                    load_curl_command(template_path) if template_path.is_file() else None
                )
            except Exception as exc:  # noqa: BLE001 - degraded template is expected
                print(f"WARNING detail template unreadable ({type(exc).__name__}); behaving like --skip-detail")
                template = None
            if template is None:
                if template_path.is_file():
                    print("WARNING detail template empty; behaving like --skip-detail")
                else:
                    print(f"WARNING detail template missing: {template_path}; behaving like --skip-detail")
            else:
                detail_fetch = _build_detail_fetcher(
                    template,
                    detail_lock_path=resolved["detail_lock_path"],
                    detail_timeout=args.detail_timeout,
                    provider=shared_cnnc_provider(),
                )
                max_documents = args.max_documents

        mappings = load_zhaoshang_mall_mappings(args.mapping_path)
        try:
            summary = run_prewarm_scan(
                prewarm_store=CustomerPrewarmStore(resolved["prewarm_store_path"]),
                shared_cache=SharedCrawlCache(resolved["cache_path"]),
                business_date=business_date,
                state_db_path=resolved["state_db_path"],
                mappings=mappings,
                detail_fetch=detail_fetch,
                max_documents=max_documents,
                refresh_limit=args.refresh_limit,
                dry_run=args.dry_run,
                budget_seconds=args.budget_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - scheduler-facing failure
            print(
                json.dumps(
                    {
                        "business_date": business_date,
                        "outcome": "SCAN_FAILED",
                        "error_type": type(exc).__name__,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2
        summary["business_date"] = business_date
        summary["dry_run"] = bool(args.dry_run)
        summary["detail_enabled"] = detail_fetch is not _disabled_detail_fetch
        summary["mappings"] = len(mappings)
        print(
            f"customer_prewarm_scan business_date={business_date} "
            f"dry_run={bool(args.dry_run)} detail_enabled={summary['detail_enabled']}"
        )
        print(
            f"intake={summary.get('intake_count', 0)} "
            f"documents_processed={summary.get('documents_processed', 0)} "
            f"detail_failures={summary.get('detail_failures', 0)} "
            f"candidate_hits={summary.get('candidate_hits', 0)} "
            f"candidate_misses={summary.get('candidate_misses', 0)} "
            f"refresh_checked={summary.get('refresh_checked', 0)} "
            f"refresh_hits={summary.get('refresh_hits', 0)} "
            f"ok_synced={summary.get('ok_synced', 0)} "
            f"already_synced={summary.get('already_synced', 0)} "
            f"would_record={summary.get('would_record', 0)} "
            f"phase_errors={len(summary.get('phase_errors', []))}"
        )
        if args.json_output is not None:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(
                json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
