"""CLI for one finite incremental audit tick, finalize, or read-only status."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.incremental_audit_runtime import (
    DEFAULT_RUNTIME_ROOT,
    business_date_in_shanghai,
    run_runtime_command,
)
from app.services.incremental_audit_status import (
    collect_runtime_status,
    runtime_doctor,
    write_daily_status,
)


DEFAULT_COMMAND_BUDGET_SECONDS = 720.0


def _add_common_arguments(parser: argparse.ArgumentParser, *, allow_remote_writes: bool) -> None:
    parser.add_argument("--business-date")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    if not allow_remote_writes:
        return
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=DEFAULT_COMMAND_BUDGET_SECONDS,
        help="finite command wall-clock budget; default leaves scheduler overlap headroom",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--shadow",
        dest="enable_remote_writes",
        action="store_false",
        default=False,
        help="disable remote task creation (the default)",
    )
    mode.add_argument(
        "--enable-remote-writes",
        "--enable-zhaoshang",
        dest="enable_remote_writes",
        action="store_true",
        help="allow authorized Zhaoshang task creation; durable reconciliation remains mandatory",
    )
    parser.add_argument(
        "--zhaoshang-backend",
        choices=("legacy", "batch"),
        default="legacy",
        help="remote backend; batch create remains canary-gated and lacks an evidenced export contract",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Finite CNNC/SaaS incremental price-audit commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("tick", "finalize"):
        child = subparsers.add_parser(command)
        _add_common_arguments(child, allow_remote_writes=True)
        if command == "finalize":
            child.add_argument("--output", type=Path)
    status = subparsers.add_parser("status")
    _add_common_arguments(status, allow_remote_writes=False)
    doctor = subparsers.add_parser("doctor")
    _add_common_arguments(doctor, allow_remote_writes=False)
    doctor.add_argument(
        "--enable-remote-writes",
        action="store_true",
        help="also require local prerequisites for the write-capable Zhaoshang backend",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    business_date = args.business_date or business_date_in_shanghai()
    if args.command == "status":
        print(
            json.dumps(
                collect_runtime_status(business_date=business_date, runtime_root=args.runtime_root),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "doctor":
        report = runtime_doctor(
            business_date=business_date,
            runtime_root=args.runtime_root,
            repo_root=ROOT,
            enable_remote_writes=args.enable_remote_writes,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["healthy"] else 2
    started = time.monotonic()
    result = run_runtime_command(
        args.command,
        business_date=business_date,
        runtime_root=args.runtime_root,
        runtime_budget_seconds=args.budget_seconds,
        enable_zhaoshang=args.enable_remote_writes,
        zhaoshang_backend=args.zhaoshang_backend,
        output_path=getattr(args, "output", None),
    )
    elapsed_seconds = round(max(0.0, time.monotonic() - started), 3)
    materialization = getattr(result.publication, "materialization", None)
    publication_path = getattr(materialization, "output_path", None)
    status_path = None
    status = None
    if result.tick.outcome != "ALREADY_RUNNING":
        status = collect_runtime_status(business_date=business_date, runtime_root=args.runtime_root)
        status["last_command"] = {
            "command": args.command,
            "mode": "remote-write-enabled" if args.enable_remote_writes else "shadow",
            "zhaoshang_backend": args.zhaoshang_backend,
            "outcome": result.tick.outcome,
            "elapsed_seconds": elapsed_seconds,
        }
        status_path = write_daily_status(
            status,
            business_date=business_date,
            runtime_root=args.runtime_root,
        )
    print(json.dumps({
        "business_date": business_date,
        "run_id": status.get("run_id") if status is not None else None,
        "mode": "remote-write-enabled" if args.enable_remote_writes else "shadow",
        "zhaoshang_backend": args.zhaoshang_backend,
        "outcome": result.tick.outcome,
        "elapsed_seconds": elapsed_seconds,
        "frozen_snapshot_id": result.tick.frozen_snapshot_id,
        "counters": dict(result.tick.counters),
        "backlog": dict(result.tick.backlog),
        "error_type": result.tick.error,
        "publication": str(publication_path) if publication_path is not None else None,
        "publication_blocked": result.publication_blocked,
        "daily_status": str(status_path) if status_path is not None else None,
    }, ensure_ascii=False, sort_keys=True))
    if result.publication is not None:
        return 0
    if result.tick.outcome == "ALREADY_RUNNING":
        return 0
    if result.publication_blocked or result.tick.outcome in {
        "FAILED",
        "MANUAL_REVIEW_REQUIRED",
        "DISCOVERY_INCOMPLETE",
        "INCOMPLETE",
    }:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
