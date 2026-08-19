"""CLI for finite incremental audit tick, closeout, finalize, or status."""

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
    run_existing_remote_harvest_command,
    run_model_recovery_command,
    run_no_result_recovery_command,
    run_partial_exact_harvest_recovery_command,
    run_requeue_model_transient_command,
    run_review_export_command,
    run_release_model_recovery_command,
    run_remote_harvest_recovery_command,
    run_remote_priority_recovery_command,
    run_remote_priority_zero_rows_recovery_command,
    run_remote_wait_export_recovery_command,
    run_runtime_command,
)
from app.services.incremental_audit_recovery import RecoveryRejected
from app.services.incremental_audit_status import (
    collect_production_writer_status,
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
    for command in ("tick", "finalize", "closeout"):
        child = subparsers.add_parser(command)
        _add_common_arguments(child, allow_remote_writes=True)
        child.add_argument(
            "--invocation",
            choices=("manual", "scheduler"),
            default="manual",
            help=argparse.SUPPRESS,
        )
        if command in {"finalize", "closeout"}:
            child.add_argument("--output", type=Path)
        if command == "closeout":
            child.add_argument(
                "--source-stability-seconds",
                type=float,
                default=600.0,
                help="identical authoritative observations must span this long before freeze",
            )
            child.add_argument(
                "--source-observation-interval-seconds",
                type=float,
                default=300.0,
                help="minimum spacing between SaaS observations before freeze",
            )
            child.add_argument(
                "--remote-sweep-interval-seconds",
                type=float,
                default=600.0,
                help="minimum spacing between full remote sweeps during closeout",
            )
    drain = subparsers.add_parser("drain-only")
    _add_common_arguments(drain, allow_remote_writes=False)
    drain.add_argument(
        "--budget-seconds",
        type=float,
        default=DEFAULT_COMMAND_BUDGET_SECONDS,
        help="finite drain wall-clock budget, including final materialization reserve",
    )
    drain.add_argument("--max-iterations", type=int, default=1000)
    drain.add_argument(
        "--zhaoshang-backend",
        choices=("legacy",),
        default="legacy",
    )
    drain.add_argument(
        "--invocation",
        choices=("manual", "scheduler"),
        default="manual",
        help=argparse.SUPPRESS,
    )
    harvest_only = subparsers.add_parser("harvest-only")
    _add_common_arguments(harvest_only, allow_remote_writes=False)
    harvest_only.add_argument(
        "--budget-seconds",
        type=float,
        default=DEFAULT_COMMAND_BUDGET_SECONDS,
        help="finite harvest-only wall-clock budget",
    )
    harvest_only.add_argument(
        "--zhaoshang-backend",
        choices=("legacy",),
        default="legacy",
    )
    harvest_only.add_argument(
        "--invocation",
        choices=("manual", "scheduler"),
        default="manual",
        help=argparse.SUPPRESS,
    )
    status = subparsers.add_parser("status")
    _add_common_arguments(status, allow_remote_writes=False)
    lock_status = subparsers.add_parser("lock-status")
    _add_common_arguments(lock_status, allow_remote_writes=False)
    doctor = subparsers.add_parser("doctor")
    _add_common_arguments(doctor, allow_remote_writes=False)
    doctor.add_argument(
        "--enable-remote-writes",
        action="store_true",
        help="also require local prerequisites for the write-capable Zhaoshang backend",
    )
    recover_model = subparsers.add_parser("recover-model")
    _add_common_arguments(recover_model, allow_remote_writes=False)
    recover_model.add_argument(
        "--stage-work-id",
        action="append",
        type=int,
        required=True,
        dest="stage_work_ids",
        help="exact TERMINAL_FAILED MODEL stage_work id; repeat for each target",
    )
    recover_model.add_argument("--timeout-seconds", type=float, default=300.0)
    requeue_model = subparsers.add_parser("requeue-model-transient")
    _add_common_arguments(requeue_model, allow_remote_writes=False)
    requeue_model.add_argument("--expected-count", type=int, required=True)
    requeue_model.add_argument("--expected-attempts", type=int, required=True)
    release_model = subparsers.add_parser("release-model-recovery")
    _add_common_arguments(release_model, allow_remote_writes=False)
    release_model.add_argument(
        "--stage-work-id",
        action="append",
        type=int,
        required=True,
        dest="stage_work_ids",
        help="exact RECOVERED_PENDING_REVIEW MODEL stage_work id; repeat for each target",
    )
    release_model.add_argument(
        "--allow-future-remote-submit",
        action="store_true",
        help="acknowledge that released model results become eligible for later normal remote submission",
    )
    recover_remote = subparsers.add_parser("recover-remote-harvest")
    _add_common_arguments(recover_remote, allow_remote_writes=False)
    recover_remote.add_argument("--remote-job-id", type=int, required=True)
    recover_remote.add_argument("--remote-identity", required=True)
    recover_partial = subparsers.add_parser("recover-partial-exact-harvest")
    _add_common_arguments(recover_partial, allow_remote_writes=False)
    recover_partial.add_argument("--remote-job-id", type=int, required=True)
    recover_partial.add_argument("--remote-identity", required=True)
    resolve_no_result = subparsers.add_parser("resolve-no-result")
    _add_common_arguments(resolve_no_result, allow_remote_writes=False)
    resolve_no_result.add_argument("--crawl-work-id", type=int, required=True)
    resolve_no_result.add_argument("--remote-job-id", type=int, required=True)
    resolve_no_result.add_argument("--remote-identity", required=True)
    recover_priority = subparsers.add_parser("recover-remote-priority")
    _add_common_arguments(recover_priority, allow_remote_writes=False)
    recover_priority.add_argument("--remote-job-id", type=int, required=True)
    recover_priority.add_argument("--remote-identity", required=True)
    recover_priority.add_argument(
        "--expected-current-state",
        choices=("PRIORITY_PENDING", "MANUAL_REQUIRED"),
        default="PRIORITY_PENDING",
    )
    recover_priority.add_argument("--expected-result-state")
    recover_priority.add_argument("--expected-error-text")
    recover_priority.add_argument("--timeout-seconds", type=float, default=300.0)
    recover_zero_rows = subparsers.add_parser("recover-remote-priority-zero-rows")
    _add_common_arguments(recover_zero_rows, allow_remote_writes=False)
    recover_zero_rows.add_argument("--remote-job-id", type=int, required=True)
    recover_zero_rows.add_argument("--remote-identity", required=True)
    recover_zero_rows.add_argument(
        "--expected-error-text",
        required=True,
        help="exact persisted REMOTE_NOT_MATERIALIZED diagnostic (or the legacy PriorityUpdateZeroRowsError type name)",
    )
    recover_zero_rows.add_argument("--timeout-seconds", type=float, default=300.0)
    harvest_existing = subparsers.add_parser("harvest-existing-remote")
    _add_common_arguments(harvest_existing, allow_remote_writes=False)
    harvest_existing.add_argument("--remote-job-id", type=int, required=True)
    harvest_existing.add_argument("--remote-identity", required=True)
    harvest_existing.add_argument("--timeout-seconds", type=float, default=300.0)
    recover_wait_export = subparsers.add_parser("recover-remote-wait-export")
    _add_common_arguments(recover_wait_export, allow_remote_writes=False)
    recover_wait_export.add_argument("--remote-job-id", type=int, required=True)
    recover_wait_export.add_argument("--remote-identity", required=True)
    recover_wait_export.add_argument(
        "--expected-error-text",
        required=True,
        help="exact persisted wait-export manual diagnostic the recovery is scoped to",
    )
    recover_wait_export.add_argument("--timeout-seconds", type=float, default=300.0)
    export_review = subparsers.add_parser("export-review")
    _add_common_arguments(export_review, allow_remote_writes=False)
    export_review.add_argument("--output-path", type=Path)
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
    if args.command == "lock-status":
        print(
            json.dumps(
                collect_production_writer_status(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                ),
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
    if args.command == "export-review":
        try:
            review = run_review_export_command(
                business_date=business_date,
                runtime_root=args.runtime_root,
                output_path=args.output_path,
            )
        except Exception as exc:
            print(json.dumps({
                "business_date": business_date,
                "outcome": "REVIEW_EXPORT_FAILED",
                "error_type": type(exc).__name__,
            }, ensure_ascii=False, sort_keys=True))
            return 2
        print(json.dumps(review, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command in {
        "recover-model",
        "requeue-model-transient",
        "release-model-recovery",
        "recover-remote-harvest",
        "recover-partial-exact-harvest",
        "resolve-no-result",
        "recover-remote-priority",
        "recover-remote-priority-zero-rows",
        "recover-remote-wait-export",
        "harvest-existing-remote",
    }:
        try:
            if args.command == "recover-model":
                recovery = run_model_recovery_command(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                    stage_work_ids=args.stage_work_ids,
                    timeout_seconds=args.timeout_seconds,
                )
            elif args.command == "requeue-model-transient":
                recovery = run_requeue_model_transient_command(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                    expected_count=args.expected_count,
                    expected_attempts=args.expected_attempts,
                )
            elif args.command == "release-model-recovery":
                if not args.allow_future_remote_submit:
                    raise RecoveryRejected(
                        "release requires --allow-future-remote-submit acknowledgement"
                    )
                recovery = run_release_model_recovery_command(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                    stage_work_ids=args.stage_work_ids,
                )
            elif args.command == "recover-remote-harvest":
                recovery = run_remote_harvest_recovery_command(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                    remote_job_id=args.remote_job_id,
                    remote_identity=args.remote_identity,
                )
            elif args.command == "recover-partial-exact-harvest":
                recovery = run_partial_exact_harvest_recovery_command(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                    remote_job_id=args.remote_job_id,
                    remote_identity=args.remote_identity,
                )
            elif args.command == "resolve-no-result":
                recovery = run_no_result_recovery_command(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                    crawl_work_id=args.crawl_work_id,
                    remote_job_id=args.remote_job_id,
                    remote_identity=args.remote_identity,
                )
            elif args.command == "recover-remote-priority":
                recovery = run_remote_priority_recovery_command(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                    remote_job_id=args.remote_job_id,
                    remote_identity=args.remote_identity,
                    expected_current_state=args.expected_current_state,
                    expected_result_state=args.expected_result_state,
                    expected_error_text=args.expected_error_text,
                    timeout_seconds=args.timeout_seconds,
                )
            elif args.command == "recover-remote-priority-zero-rows":
                recovery = run_remote_priority_zero_rows_recovery_command(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                    remote_job_id=args.remote_job_id,
                    remote_identity=args.remote_identity,
                    expected_error_text=args.expected_error_text,
                    timeout_seconds=args.timeout_seconds,
                )
            elif args.command == "recover-remote-wait-export":
                recovery = run_remote_wait_export_recovery_command(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                    remote_job_id=args.remote_job_id,
                    remote_identity=args.remote_identity,
                    expected_error_text=args.expected_error_text,
                    timeout_seconds=args.timeout_seconds,
                )
            else:
                recovery = run_existing_remote_harvest_command(
                    business_date=business_date,
                    runtime_root=args.runtime_root,
                    remote_job_id=args.remote_job_id,
                    remote_identity=args.remote_identity,
                    timeout_seconds=args.timeout_seconds,
                )
        except Exception as exc:
            print(json.dumps({
                "business_date": business_date,
                "outcome": "RECOVERY_REJECTED" if isinstance(exc, RecoveryRejected) else "RECOVERY_FAILED",
                "error_type": type(exc).__name__,
            }, ensure_ascii=False, sort_keys=True))
            return 2
        print(json.dumps(recovery, ensure_ascii=False, sort_keys=True))
        return 0
    started = time.monotonic()
    enable_remote_writes = getattr(args, "enable_remote_writes", False)
    zhaoshang_backend = getattr(args, "zhaoshang_backend", "legacy")
    mode = (
        args.command
        if args.command in {"drain-only", "harvest-only"}
        else "remote-write-enabled" if enable_remote_writes else "shadow"
    )
    result = run_runtime_command(
        args.command,
        business_date=business_date,
        runtime_root=args.runtime_root,
        runtime_budget_seconds=args.budget_seconds,
        drain_max_iterations=getattr(args, "max_iterations", 1000),
        enable_zhaoshang=enable_remote_writes,
        zhaoshang_backend=zhaoshang_backend,
        output_path=getattr(args, "output", None),
        invocation=args.invocation,
        source_stability_seconds=getattr(args, "source_stability_seconds", None),
        closeout_observation_interval_seconds=getattr(
            args, "source_observation_interval_seconds", 300.0
        ),
        closeout_remote_sweep_interval_seconds=getattr(
            args, "remote_sweep_interval_seconds", 600.0
        ),
    )
    elapsed_seconds = round(max(0.0, time.monotonic() - started), 3)
    if result.lock_conflict is not None:
        conflict = result.lock_conflict
        print("SKIPPED_ALREADY_RUNNING")
        print(f"business_date={business_date}")
        print(f"requested={args.command}")
        print(f"lock_identity={conflict['lock_identity']}")
        owner_fields = (
            ("owner_pid", "pid"),
            ("owner_command", "command"),
            ("owner_started_at", "started_at"),
            ("owner_invocation", "invocation"),
        )
        for output_name, source_name in owner_fields:
            if conflict.get(source_name) not in (None, ""):
                print(f"{output_name}={conflict[source_name]}")
        return 0
    materialization = getattr(result.publication, "materialization", None)
    publication_path = getattr(materialization, "output_path", None)
    status_path = None
    status = None
    if result.tick.outcome != "ALREADY_RUNNING":
        status = collect_runtime_status(business_date=business_date, runtime_root=args.runtime_root)
        status["last_command"] = {
            "command": args.command,
            "mode": mode,
            "zhaoshang_backend": zhaoshang_backend,
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
        "mode": mode,
        "zhaoshang_backend": zhaoshang_backend,
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
