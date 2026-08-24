from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_runtime import RuntimeResult, run_runtime_command, runtime_paths
from app.services.incremental_audit_status import (
    collect_production_writer_status,
    collect_runtime_status,
    runtime_doctor,
    write_daily_status,
)
from app.workflows.incremental_price_audit import DiscoveredState, TickDependencies, TickSummary
from scripts import incremental_price_audit as cli


BUSINESS_DATE = "2026-08-11"


def test_read_only_status_reports_active_writer_without_changing_state(tmp_path):
    root = tmp_path / "runtime"
    paths = runtime_paths(BUSINESS_DATE, root)
    DailyAuditStore(paths.state_path).ensure_run(BUSINESS_DATE)
    before = paths.state_path.read_bytes()
    lock = ProcessLock(paths.lock_path).acquire(owner={
        "business_date": BUSINESS_DATE,
        "pid": 456,
        "command": "tick",
        "started_at": "2026-08-11T10:00:00+08:00",
        "invocation": "scheduler",
    })
    try:
        status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)
        writer = collect_production_writer_status(
            business_date=BUSINESS_DATE,
            runtime_root=root,
        )
    finally:
        lock.release()

    assert status["production_writer"]["active"] is True
    assert status["production_writer"]["message"] == "production run currently active"
    assert writer["command"] == "tick"
    assert writer["invocation"] == "scheduler"
    assert paths.state_path.read_bytes() == before


def _dependencies() -> TickDependencies:
    return TickDependencies(
        discover=lambda _timeout: DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
            headers=["vCommodityId", "skuId"],
            rows=[
                {
                    "vCommodityId": "v1",
                    "documentId": "v1",
                    "skuId": "sku-1",
                    "extLink": "",
                    "fourCategoryId": "category-1",
                }
            ],
            source_fingerprints=["source-v1"],
            customer_fingerprints=["customer-v1"],
            evidence={"saas_count": 1, "customer_count": 1},
        ),
        detail=lambda _row, _timeout: {"customerPrice": "10"},
        model=lambda _row, _timeout: {
            "match_result": "SAME",
            "confidence": 1,
            "model_match": "exact",
            "spec_match": True,
        },
        wall_clock=lambda: datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
    )


def test_operational_status_has_stable_shape_and_stage_counts(tmp_path):
    root = tmp_path / "runtime"
    run_runtime_command(
        "tick",
        business_date=BUSINESS_DATE,
        runtime_root=root,
        dependencies=_dependencies(),
    )

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["state"] == "OPEN"
    assert isinstance(status["run_id"], int)
    assert status["latest_snapshot"]["row_count"] == 1
    assert status["counts"] == {
        "discovered": 1,
        "unique_items": 1,
        "new": 1,
        "changed": 0,
        "processed": 1,
    }
    assert status["detail"]["completed"] == 1
    assert status["model"]["completed"] == 1
    assert status["crawl"]["pending"] == 0
    assert status["remote"]["unknown"] == 0
    assert status["final"]["finalized"] is False
    assert status["final"]["pending"] == 0
    assert status["lease"] == {"active": False, "stale": False, "expires_at": None}


def test_status_computes_pending_instead_of_trusting_stale_tick_metric(tmp_path):
    root = tmp_path / "runtime"
    dependencies = _dependencies()
    dependencies = TickDependencies(
        discover=dependencies.discover,
        detail=dependencies.detail,
        model=lambda _row, _timeout: (_ for _ in ()).throw(
            ValueError("permanent model contract failure")
        ),
        wall_clock=dependencies.wall_clock,
    )
    run_runtime_command(
        "tick",
        business_date=BUSINESS_DATE,
        runtime_root=root,
        dependencies=dependencies,
    )
    state_path = runtime_paths(BUSINESS_DATE, root).state_path
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE daily_runs SET metrics_json=? WHERE business_date=?",
            ('{"final_pending":0}', BUSINESS_DATE),
        )

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["model"]["terminal"] == 1
    assert status["final"]["pending"] == status["final"]["computed_pending"] == 1


def test_status_keeps_detail_terminal_evidence_without_counting_it_as_final_pending(tmp_path):
    root = tmp_path / "runtime"
    dependencies = _dependencies()
    dependencies = TickDependencies(
        discover=dependencies.discover,
        detail=lambda _row, _timeout: (_ for _ in ()).throw(
            ValueError("permanent detail failure")
        ),
        model=dependencies.model,
        wall_clock=dependencies.wall_clock,
    )

    for _attempt in range(3):
        run_runtime_command(
            "tick",
            business_date=BUSINESS_DATE,
            runtime_root=root,
            dependencies=dependencies,
        )
    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["detail"]["terminal"] == 1
    assert status["model"]["completed"] == 1
    assert status["final"]["pending"] == status["final"]["computed_pending"] == 0


def test_status_reports_no_result_as_nonblocking_business_outcome(tmp_path):
    root = tmp_path / "runtime"
    paths = runtime_paths(BUSINESS_DATE, root)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(BUSINESS_DATE)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-no-result", 60)
    job = store.prepare_remote_intent(
        lease,
        "mall:hash",
        "daily-20260811-mall",
        "payload-a",
        result_path=str(paths.run_dir / "mall-result.xlsx"),
    )
    store.claim_remote_submission(lease, int(job["id"]))
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity="mall|task-id|task-number",
        state="COMPLETED",
        result={"https://other/item": {"price": "12.30"}},
    )
    crawl = store.ensure_crawl_work(
        lease,
        "mall",
        "sku-1",
        "https://shop.example/item",
        "legacy-v1",
        "crawl-fp",
        source_id="v-1",
    )
    store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    error = "legacy export missing safe skuId / exact URL result"
    store.fail_crawl_work(lease, int(crawl["id"]), error, retryable=False)
    store.complete_crawl_work_without_result(lease, int(crawl["id"]), reason=error)
    store.release_lease(lease)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["crawl"]["no_result"] == 1
    assert status["crawl"]["manual"] == 0
    assert status["final"]["computed_pending"] == 0


def test_status_counts_required_snapshot_rows_without_crawl_work_as_unplanned(tmp_path):
    root = tmp_path / "runtime"
    dependencies = _dependencies()
    dependencies = TickDependencies(
        discover=lambda _timeout: DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
            headers=["vCommodityId", "skuId", "extLink"],
            rows=[
                {
                    "vCommodityId": "v1",
                    "documentId": "v1",
                    "skuId": "sku-1",
                    "extLink": "https://shop.example/item",
                    "fourCategoryId": "category-1",
                    "__crawl_required": True,
                }
            ],
            source_fingerprints=["source-v1"],
            customer_fingerprints=["customer-v1"],
        ),
        detail=dependencies.detail,
        model=dependencies.model,
        wall_clock=dependencies.wall_clock,
    )
    run_runtime_command(
        "tick",
        business_date=BUSINESS_DATE,
        runtime_root=root,
        dependencies=dependencies,
    )

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["crawl"]["unplanned"] == 1
    assert status["final"]["computed_pending"] == 1
    assert status["next_actions"] == [
        "inspect required snapshot rows without crawl work; do not finalize or submit without explicit authorization"
    ]


def _seed_pending_crawl_mix(root):
    """Build one run whose crawl.pending spans every next-action bucket."""
    paths = runtime_paths(BUSINESS_DATE, root)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(BUSINESS_DATE)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-breakdown", 60)
    created = {}
    for name, remote_state in (
        ("priority", "PRIORITY_PENDING"),
        ("remote", "SUBMITTED"),
        ("manual", "SUBMITTED"),
    ):
        job = store.prepare_remote_intent(lease, f"{name}", f"{name}", f"payload-{name}")
        store.claim_remote_submission(lease, int(job["id"]))
        job = store.reconcile_remote_job(
            lease,
            int(job["id"]),
            remote_identity=f"remote-{name}",
            state=remote_state,
        )
        crawl = store.ensure_crawl_work(
            lease,
            f"mall-{name}",
            f"sku-{name}",
            f"https://shop.example/{name}",
            "legacy-v1",
            f"fingerprint-{name}",
            source_id=f"v-{name}",
        )
        store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
        created[name] = (job, crawl)
    store.mark_remote_manual(lease, int(created["manual"][0]["id"]), "priority runner failed")
    unbound = store.ensure_crawl_work(
        lease,
        "mall-unbound",
        "sku-unbound",
        "https://shop.example/unbound",
        "legacy-v1",
        "fingerprint-unbound",
        source_id="v-unbound",
    )
    retryable = store.ensure_crawl_work(
        lease,
        "mall-retryable",
        "sku-retryable",
        "https://shop.example/retryable",
        "legacy-v1",
        "fingerprint-retryable",
        source_id="v-retryable",
    )
    store.fail_crawl_work(lease, int(retryable["id"]), "transient", retryable=True)
    store.release_lease(lease)
    return store, created, unbound, retryable


def test_status_breaks_down_crawl_pending_by_next_required_action(tmp_path):
    root = tmp_path / "runtime"
    _seed_pending_crawl_mix(root)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    crawl = status["crawl"]
    # Legacy aggregate stays unchanged and additive buckets sum to it.
    assert crawl["pending"] == 5
    assert (
        crawl["waiting_submit"]
        + crawl["awaiting_priority"]
        + crawl["awaiting_remote"]
        + crawl["blocked_manual"]
        + crawl["retryable"]
        + crawl["unclassified"]
        == crawl["pending"]
    )
    assert crawl["waiting_submit"] == 1
    assert crawl["awaiting_priority"] == 1
    assert crawl["awaiting_remote"] == 1
    assert crawl["blocked_manual"] == 1
    assert crawl["retryable"] == 1
    assert crawl["unclassified"] == 0
    # A crawl already bound to a remote job must not read as "not submitted".
    assert crawl["waiting_submit"] != crawl["pending"]


def test_status_remote_breakdown_exposes_awaiting_priority_and_harvest(tmp_path):
    root = tmp_path / "runtime"
    _seed_pending_crawl_mix(root)

    remote = collect_runtime_status(
        business_date=BUSINESS_DATE, runtime_root=root
    )["remote"]

    assert remote["pending"] == 2
    assert remote["awaiting_priority"] == 1
    assert remote["awaiting_harvest"] == 1
    assert remote["manual"] == 1


def test_status_final_blocker_summary_decomposes_computed_pending(tmp_path):
    root = tmp_path / "runtime"
    _seed_pending_crawl_mix(root)

    final = collect_runtime_status(
        business_date=BUSINESS_DATE, runtime_root=root
    )["final"]

    assert final["computed_pending"] == 8
    assert final["blocker_summary"] == {
        "detail_pending": 0,
        "model_pending": 0,
        "model_terminal": 0,
        "crawl_pending": 5,
        "crawl_unplanned": 0,
        "crawl_unknown": 0,
        "crawl_manual": 0,
        "remote_intent": 0,
        "remote_pending": 2,
        "remote_unknown": 0,
        "remote_manual": 1,
    }
    assert sum(final["blocker_summary"].values()) == final["computed_pending"]


def test_status_pending_breakdown_reports_unclassified_instead_of_guessing(tmp_path):
    root = tmp_path / "runtime"
    paths = runtime_paths(BUSINESS_DATE, root)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(BUSINESS_DATE)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-unclassified", 60)
    job = store.prepare_remote_intent(lease, "odd", "odd", "payload-odd")
    store.claim_remote_submission(lease, int(job["id"]))
    store.reconcile_remote_job(
        lease, int(job["id"]), remote_identity="remote-odd", state="COMPLETED"
    )
    crawl = store.ensure_crawl_work(
        lease,
        "mall-odd",
        "sku-odd",
        "https://shop.example/odd",
        "legacy-v1",
        "fingerprint-odd",
        source_id="v-odd",
    )
    store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    store.release_lease(lease)

    crawl_status = collect_runtime_status(
        business_date=BUSINESS_DATE, runtime_root=root
    )["crawl"]

    # A PENDING crawl behind a COMPLETED remote job has no provable next
    # action; status must say unclassified rather than invent one.
    assert crawl_status["pending"] == 1
    assert crawl_status["unclassified"] == 1
    assert crawl_status["waiting_submit"] == 0


def test_missing_status_and_status_command_are_read_only(tmp_path, capsys):
    root = tmp_path / "missing"

    assert cli.main(["status", "--business-date", BUSINESS_DATE, "--runtime-root", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["state"] == "MISSING"
    assert payload["run_id"] is None
    assert payload["counts"]["discovered"] == 0
    assert not root.exists()


def test_daily_status_is_atomic_convenience_snapshot(tmp_path):
    root = tmp_path / "runtime"
    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    output = write_daily_status(status, business_date=BUSINESS_DATE, runtime_root=root)

    assert output == runtime_paths(BUSINESS_DATE, root).run_dir / "daily_status.json"
    assert json.loads(output.read_text(encoding="utf-8"))["state"] == "MISSING"
    assert not list(output.parent.glob(".daily_status.json.*.tmp"))


def test_tick_cli_is_shadow_with_720_second_budget_by_default(tmp_path, monkeypatch, capsys):
    captured = {}

    def run(command, **kwargs):
        captured.update({"command": command, **kwargs})
        return RuntimeResult(TickSummary("COMPLETED", (), {}, {}, None))

    monkeypatch.setattr(cli, "run_runtime_command", run)
    root = tmp_path / "runtime"

    assert cli.main(["tick", "--business-date", BUSINESS_DATE, "--runtime-root", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert captured["runtime_budget_seconds"] == 720
    assert captured["enable_zhaoshang"] is False
    assert payload["mode"] == "shadow"
    assert payload["zhaoshang_backend"] == "legacy"
    assert payload["elapsed_seconds"] >= 0
    assert Path(payload["daily_status"]).is_file()


def test_recover_model_cli_forwards_only_explicit_stage_ids(tmp_path, monkeypatch, capsys):
    captured = {}

    def recover(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "MODEL_RECOVERY_PENDING_REVIEW",
            "stage_work_ids": kwargs["stage_work_ids"],
            "remote_writes": 0,
        }

    monkeypatch.setattr(cli, "run_model_recovery_command", recover)

    exit_code = cli.main([
        "recover-model",
        "--business-date", BUSINESS_DATE,
        "--runtime-root", str(tmp_path / "runtime"),
        "--stage-work-id", "1025",
        "--stage-work-id", "1034",
    ])

    assert exit_code == 0
    assert captured["business_date"] == BUSINESS_DATE
    assert captured["stage_work_ids"] == [1025, 1034]
    assert json.loads(capsys.readouterr().out)["remote_writes"] == 0


def test_requeue_model_transient_cli_requires_exact_count_and_attempts(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    def requeue(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "MODEL_TRANSIENT_REQUEUED",
            "recovered": kwargs["expected_count"],
            "remote_writes": 0,
        }

    monkeypatch.setattr(cli, "run_requeue_model_transient_command", requeue)

    exit_code = cli.main([
        "requeue-model-transient",
        "--business-date", BUSINESS_DATE,
        "--runtime-root", str(tmp_path / "runtime"),
        "--expected-count", "54",
        "--expected-attempts", "1",
    ])

    assert exit_code == 0
    assert captured == {
        "business_date": BUSINESS_DATE,
        "runtime_root": tmp_path / "runtime",
        "expected_count": 54,
        "expected_attempts": 1,
    }
    assert json.loads(capsys.readouterr().out) == {
        "outcome": "MODEL_TRANSIENT_REQUEUED",
        "recovered": 54,
        "remote_writes": 0,
    }


def test_release_model_recovery_cli_requires_remote_submit_acknowledgement(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    def release(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "MODEL_RECOVERY_RELEASED",
            "stage_work_ids": kwargs["stage_work_ids"],
            "remote_calls": 0,
        }

    monkeypatch.setattr(cli, "run_release_model_recovery_command", release)
    argv = [
        "release-model-recovery",
        "--business-date", BUSINESS_DATE,
        "--runtime-root", str(tmp_path / "runtime"),
        "--stage-work-id", "1025",
        "--stage-work-id", "1034",
    ]

    assert cli.main(argv) == 2
    assert captured == {}
    assert json.loads(capsys.readouterr().out)["outcome"] == "RECOVERY_REJECTED"

    assert cli.main([*argv, "--allow-future-remote-submit"]) == 0
    assert captured["stage_work_ids"] == [1025, 1034]
    assert json.loads(capsys.readouterr().out)["remote_calls"] == 0


def test_recover_remote_harvest_cli_requires_exact_job_and_identity(tmp_path, monkeypatch, capsys):
    captured = {}

    def recover(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "REMOTE_HARVEST_RECOVERY_COMPLETED",
            "remote_job_id": kwargs["remote_job_id"],
            "remote_calls": 0,
        }

    monkeypatch.setattr(cli, "run_remote_harvest_recovery_command", recover)

    exit_code = cli.main([
        "recover-remote-harvest",
        "--business-date", BUSINESS_DATE,
        "--runtime-root", str(tmp_path / "runtime"),
        "--remote-job-id", "5",
        "--remote-identity", "mall|task-id|task-number",
    ])

    assert exit_code == 0
    assert captured["remote_job_id"] == 5
    assert captured["remote_identity"] == "mall|task-id|task-number"
    assert json.loads(capsys.readouterr().out)["remote_calls"] == 0


def test_partial_exact_harvest_cli_is_local_only_and_forwards_exact_identity(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    def recover(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "PARTIAL_EXACT_HARVEST_COMPLETED",
            "remote_job_id": kwargs["remote_job_id"],
            "remote_calls": 0,
            "add_calls": 0,
        }

    monkeypatch.setattr(cli, "run_partial_exact_harvest_recovery_command", recover)

    exit_code = cli.main([
        "recover-partial-exact-harvest",
        "--business-date", BUSINESS_DATE,
        "--runtime-root", str(tmp_path / "runtime"),
        "--remote-job-id", "5",
        "--remote-identity", "mall|task-id|task-number",
    ])

    assert exit_code == 0
    assert captured["remote_job_id"] == 5
    assert captured["remote_identity"] == "mall|task-id|task-number"
    output = json.loads(capsys.readouterr().out)
    assert output["remote_calls"] == 0
    assert output["add_calls"] == 0


def test_resolve_no_result_cli_forwards_one_exact_crawl_and_remote_identity(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    def recover(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "CRAWL_NO_RESULT_RESOLVED",
            "crawl_work_id": kwargs["crawl_work_id"],
            "remote_calls": 0,
        }

    monkeypatch.setattr(cli, "run_no_result_recovery_command", recover)

    exit_code = cli.main([
        "resolve-no-result",
        "--business-date", BUSINESS_DATE,
        "--runtime-root", str(tmp_path / "runtime"),
        "--crawl-work-id", "12",
        "--remote-job-id", "2",
        "--remote-identity", "mall|task-id|task-number",
    ])

    assert exit_code == 0
    assert captured["crawl_work_id"] == 12
    assert captured["remote_job_id"] == 2
    assert captured["remote_identity"] == "mall|task-id|task-number"
    assert json.loads(capsys.readouterr().out)["remote_calls"] == 0


def test_remote_priority_recovery_cli_forwards_only_exact_existing_job(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    def recover(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "REMOTE_PRIORITY_RECOVERY_COMPLETED",
            "remote_job_id": kwargs["remote_job_id"],
            "add_calls": 0,
        }

    monkeypatch.setattr(cli, "run_remote_priority_recovery_command", recover)

    exit_code = cli.main([
        "recover-remote-priority",
        "--business-date", BUSINESS_DATE,
        "--runtime-root", str(tmp_path / "runtime"),
        "--remote-job-id", "19",
        "--remote-identity", "mall|task-id|task-number",
        "--expected-current-state", "MANUAL_REQUIRED",
        "--expected-result-state", "PRIORITY_PENDING",
        "--expected-error-text", "InterfaceError",
        "--timeout-seconds", "60",
    ])

    assert exit_code == 0
    assert captured["remote_job_id"] == 19
    assert captured["remote_identity"] == "mall|task-id|task-number"
    assert captured["expected_current_state"] == "MANUAL_REQUIRED"
    assert captured["expected_result_state"] == "PRIORITY_PENDING"
    assert captured["expected_error_text"] == "InterfaceError"
    assert captured["timeout_seconds"] == 60
    assert json.loads(capsys.readouterr().out)["add_calls"] == 0


def test_existing_remote_harvest_cli_forwards_only_exact_existing_job(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    def harvest(**kwargs):
        captured.update(kwargs)
        return {
            "outcome": "REMOTE_EXISTING_HARVEST_COMPLETED",
            "remote_job_id": kwargs["remote_job_id"],
            "remote_calls": 1,
            "add_calls": 0,
        }

    monkeypatch.setattr(cli, "run_existing_remote_harvest_command", harvest)

    exit_code = cli.main([
        "harvest-existing-remote",
        "--business-date", BUSINESS_DATE,
        "--runtime-root", str(tmp_path / "runtime"),
        "--remote-job-id", "13",
        "--remote-identity", "mall|task-id|task-number",
        "--timeout-seconds", "60",
    ])

    assert exit_code == 0
    assert captured["remote_job_id"] == 13
    assert captured["remote_identity"] == "mall|task-id|task-number"
    assert captured["timeout_seconds"] == 60
    assert json.loads(capsys.readouterr().out)["add_calls"] == 0


def test_remote_write_flag_is_explicit_and_old_name_remains_compatible(tmp_path, monkeypatch, capsys):
    enabled = []

    def run(_command, **kwargs):
        enabled.append(kwargs["enable_zhaoshang"])
        return RuntimeResult(TickSummary("COMPLETED", (), {}, {}, None))

    monkeypatch.setattr(cli, "run_runtime_command", run)
    for flag in ("--enable-remote-writes", "--enable-zhaoshang"):
        cli.main(
            [
                "tick",
                "--business-date",
                BUSINESS_DATE,
                "--runtime-root",
                str(tmp_path / flag.removeprefix("--")),
                flag,
            ]
        )
        capsys.readouterr()
    assert enabled == [True, True]


def test_doctor_checks_components_without_exposing_model_values(tmp_path):
    root = tmp_path / "repo"
    data = root / "data"
    data.mkdir(parents=True)
    for name in (
        "latest_saas_sameAudit_queryAll.curl",
        "latest_cnnc_query.curl",
        "detail_request.curl",
    ):
        (data / name).write_text("curl https://example.invalid", encoding="utf-8")
    secret = "not-for-doctor-output"
    (data / "model.env").write_text(
        "\n".join(
            (
                f"TITLE_MATCH_MODEL_API_KEY={secret}",
                "TITLE_MATCH_MODEL_BASE_URL=https://example.invalid",
                "TITLE_MATCH_MODEL_NAME=test-model",
            )
        ),
        encoding="utf-8",
    )

    report = runtime_doctor(
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path / "runtime",
        repo_root=root,
    )

    assert report["healthy"] is True
    assert report["network_calls"] == 0
    assert report["scheduler_changes"] == 0
    assert report["status"]["state"] == "MISSING"
    assert secret not in json.dumps(report)


def _run_powershell_script(script: Path, script_arguments: list[str]):
    """Run a scheduled-operations script with UTF-8 stdout.

    ``powershell.exe`` writes redirected stdout in the console code page
    (GBK on this deployment host), which contains the repo's Chinese path and
    is not decodable as the UTF-8 JSON these scripts emit.  Forcing
    ``[Console]::OutputEncoding`` inside the child makes the contract
    deterministic on every host without touching the production scripts.
    """
    encoded = str(script).replace("'", "''")
    rendered = " ".join(
        value if value.startswith("-") else f"'{value.replace(chr(39), chr(39) * 2)}'"
        for value in script_arguments
    )
    command = f"[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; & '{encoded}' {rendered}"
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        check=False,
    )


def test_scheduler_scripts_preview_weekday_production_plan_without_mutation():
    root = Path(__file__).resolve().parents[1]
    register = root / "scripts/register_incremental_price_audit_tasks.ps1"
    unregister = root / "scripts/unregister_incremental_price_audit_tasks.ps1"

    registered = _run_powershell_script(register, ["-EnableRemoteWrites"])
    removed = _run_powershell_script(unregister, [])

    assert registered.returncode == 0, registered.stderr.decode("utf-8", errors="replace")
    assert removed.returncode == 0, removed.stderr.decode("utf-8", errors="replace")
    register_plan = json.loads(registered.stdout.decode("utf-8-sig"))
    unregister_plan = json.loads(removed.stdout.decode("utf-8-sig"))
    assert register_plan["Applied"] is False
    assert register_plan["MultipleInstances"] == "IgnoreNew"
    assert register_plan["ExecutionTimeLimit"] == "PT32M"
    assert register_plan["BudgetSeconds"] == 720
    assert register_plan["CloseoutBudgetSeconds"] == 1680
    assert register_plan["RemoteWrites"] is True
    assert len(register_plan["Tasks"]) == 1
    task = register_plan["Tasks"][0]
    assert task["TaskName"] == "CNNC-PriceAudit-Incremental"
    assert task["TriggerType"] == "Weekly"
    assert task["DaysOfWeek"] == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    assert "Saturday" not in task["DaysOfWeek"]
    assert "Sunday" not in task["DaysOfWeek"]
    assert task["WeeksInterval"] == 1
    assert task["StartBoundary"] == "09:00"
    assert task["LastPlannedStart"] == "20:00"
    # One task, three weekly triggers: the daytime 15-minute baseline ends at
    # 16:45, the 17:00-17:55 peak window and the 18:00-20:00 closeout window
    # wake up every 5 minutes, and 20:00 stays the last automatic start.
    assert [
        (
            trigger["Name"],
            trigger["StartBoundary"],
            trigger["RepetitionInterval"],
            trigger["RepeatFor"],
            trigger["LastPlannedStart"],
        )
        for trigger in task["Triggers"]
    ] == [
        ("Baseline", "09:00", "PT15M", "PT7H46M", "16:45"),
        ("Peak", "17:00", "PT5M", "PT56M", "17:55"),
        ("Closeout", "18:00", "PT5M", "PT2H1M", "20:00"),
    ]
    for trigger in task["Triggers"]:
        assert trigger["DaysOfWeek"] == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        assert "Saturday" not in trigger["DaysOfWeek"]
        assert "Sunday" not in trigger["DaysOfWeek"]
    assert task["TickTimes"] == "09:00-16:00 hourly; 17:00-17:50 every 5 minutes"
    assert task["HarvestOnlyTimes"].startswith("09:15-16:45 quarter-hour slots")
    assert "17:55" in task["HarvestOnlyTimes"]
    assert task["CloseoutHours"].startswith("18:00-20:00 every 5 minutes")
    assert "20:00 is the last automatic start" in task["CloseoutHours"]
    assert task["FinalizeHours"] == "none after 20:00; operators run an explicit manual closeout instead"
    assert "run_incremental_price_audit_scheduled.ps1" in task["Arguments"]
    assert "-BudgetSeconds 720" in task["Arguments"]
    assert "-CloseoutBudgetSeconds 1680" in task["Arguments"]
    assert "-HardTimeoutMinutes 32" in task["Arguments"]
    assert "-ZhaoshangBackend legacy" in task["Arguments"]
    assert "-EnableRemoteWrites" in task["Arguments"]
    assert unregister_plan["Applied"] is False
    assert unregister_plan["TaskNames"] == ["CNNC-PriceAudit-Incremental"]


def test_scheduled_runner_routes_hours_without_running_python(tmp_path):
    root = Path(__file__).resolve().parents[1]
    runner = root / "scripts/run_incremental_price_audit_scheduled.ps1"
    runtime_root = tmp_path / "runtime"
    expected = (
        ("2026-08-13T09:00:00", ["tick"]),
        ("2026-08-13T09:15:00", ["harvest-only"]),
        ("2026-08-13T09:30:00", ["harvest-only"]),
        ("2026-08-13T09:45:00", ["harvest-only"]),
        ("2026-08-13T10:00:00", ["tick"]),
        ("2026-08-13T10:15:00", ["harvest-only"]),
        ("2026-08-13T10:30:00", ["harvest-only"]),
        ("2026-08-13T10:45:00", ["harvest-only"]),
        ("2026-08-13T11:15:00", ["harvest-only"]),
        ("2026-08-13T12:45:00", ["harvest-only"]),
        ("2026-08-13T13:30:00", ["harvest-only"]),
        ("2026-08-13T14:45:00", ["harvest-only"]),
        ("2026-08-13T15:15:00", ["harvest-only"]),
        ("2026-08-13T16:00:00", ["tick"]),
        ("2026-08-13T16:15:00", ["harvest-only"]),
        ("2026-08-13T16:30:00", ["harvest-only"]),
        ("2026-08-13T16:45:00", ["harvest-only"]),
        ("2026-08-13T16:50:00", []),
        # 17:00-17:50 is the SaaS new-data peak: every 5-minute wakeup is a
        # full tick with unchanged business semantics.
        ("2026-08-13T17:00:00", ["tick"]),
        ("2026-08-13T17:05:00", ["tick"]),
        ("2026-08-13T17:10:00", ["tick"]),
        ("2026-08-13T17:15:00", ["tick"]),
        ("2026-08-13T17:20:00", ["tick"]),
        ("2026-08-13T17:25:00", ["tick"]),
        ("2026-08-13T17:30:00", ["tick"]),
        ("2026-08-13T17:35:00", ["tick"]),
        ("2026-08-13T17:40:00", ["tick"]),
        ("2026-08-13T17:45:00", ["tick"]),
        ("2026-08-13T17:50:00", ["tick"]),
        # 17:55 only sweeps already-submitted Zhaoshang remote jobs ahead of
        # the 18:00 closeout; it must never be a full tick.
        ("2026-08-13T17:55:00", ["harvest-only"]),
        ("2026-08-13T17:52:00", []),
        ("2026-08-13T17:58:00", []),
        # 18:00-20:00 closeout start opportunities every 5 minutes; IgnoreNew
        # on the task drops overlaps, so these are opportunities, not runs.
        ("2026-08-13T18:00:00", ["closeout"]),
        ("2026-08-13T18:05:00", ["closeout"]),
        ("2026-08-13T18:10:00", ["closeout"]),
        ("2026-08-13T18:15:00", ["closeout"]),
        ("2026-08-13T18:20:00", ["closeout"]),
        ("2026-08-13T18:25:00", ["closeout"]),
        ("2026-08-13T18:30:00", ["closeout"]),
        ("2026-08-13T18:45:00", ["closeout"]),
        ("2026-08-13T19:00:00", ["closeout"]),
        ("2026-08-13T19:15:00", ["closeout"]),
        ("2026-08-13T19:30:00", ["closeout"]),
        ("2026-08-13T19:45:00", ["closeout"]),
        ("2026-08-13T19:55:00", ["closeout"]),
        ("2026-08-13T20:00:00", ["closeout"]),
        ("2026-08-13T18:03:00", []),
        ("2026-08-13T20:05:00", []),
        ("2026-08-13T20:15:00", []),
        ("2026-08-13T20:30:00", []),
        ("2026-08-13T21:00:00", []),
        ("2026-08-13T08:00:00", []),
        ("2026-08-13T08:45:00", []),
        ("2026-08-15T09:00:00", []),
        ("2026-08-15T09:15:00", []),
        ("2026-08-15T17:30:00", []),
        ("2026-08-15T18:00:00", []),
        ("2026-08-16T19:00:00", []),
    )

    for timestamp, phases in expected:
        completed = _run_powershell_script(
            runner,
            [
                "-RepoRoot", str(root),
                "-RuntimeRoot", str(runtime_root),
                "-Preview",
                "-At", timestamp,
                "-EnableRemoteWrites",
            ],
        )

        assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
        plan = json.loads(completed.stdout.decode("utf-8-sig"))
        assert plan["Phases"] == phases
        assert plan["RemoteWrites"] is True
        assert plan["BudgetSeconds"] == 720
        assert plan["CloseoutBudgetSeconds"] == 1680
        for command in plan["Commands"]:
            arguments = command["Arguments"]
            assert arguments[-2:] == ["--zhaoshang-backend", "legacy"]
            assert ["--invocation", "scheduler"] == arguments[
                arguments.index("--invocation"):arguments.index("--invocation") + 2
            ]
            assert command["Phase"] != "drain-only"
            budget = arguments[arguments.index("--budget-seconds") + 1]
            assert budget == ("1680" if command["Phase"] == "closeout" else "720")
            if command["Phase"] == "harvest-only":
                # The sweep has no create surface, so the runner must not pass
                # any remote-write capability flag to it.
                assert "--enable-remote-writes" not in arguments
                assert "--shadow" not in arguments
            else:
                assert "--enable-remote-writes" in arguments

    assert not runtime_root.exists()


def _run_scheduled_runner_with_fake_cli(
    tmp_path: Path,
    exit_code: int,
    *,
    lock_log_between_lines: bool = False,
    stdout_lines: int = 1,
    stderr_lines: int = 1,
    crash: bool = False,
):
    root = Path(__file__).resolve().parents[1]
    runner = root / "scripts/run_incremental_price_audit_scheduled.ps1"
    fake_repo = tmp_path / f"fake-repo-{exit_code}"
    fake_scripts = fake_repo / "scripts"
    fake_scripts.mkdir(parents=True)
    fake_cli_lines = [
        "import os",
        "import sys",
        f"[print(f'fake-stdout-{{line}}') for line in range({stdout_lines})]",
        f"[print(f'fake-stderr-{{line}}', file=sys.stderr) for line in range({stderr_lines})]",
    ]
    if lock_log_between_lines:
        fake_cli_lines.extend(
            (
                "import ctypes",
                "import pathlib",
                "import time",
                'runtime_root = pathlib.Path(sys.argv[sys.argv.index("--runtime-root") + 1])',
                'log_path = runtime_root / "scheduler_logs" / "2026-08-13.log"',
                "deadline = time.monotonic() + 5",
                "while time.monotonic() < deadline:",
                "    if log_path.exists() and b\"fake-stdout\" in log_path.read_bytes():",
                "        break",
                "    time.sleep(0.01)",
                "handle = ctypes.windll.kernel32.CreateFileW(",
                "    str(log_path), 0x80000000, 1, None, 3, 0, None",
                ")",
                "invalid_handle = ctypes.c_void_p(-1).value",
                'print("fake-after-lock", flush=True)',
                "if handle != invalid_handle:",
                "    time.sleep(0.5)",
                "    ctypes.windll.kernel32.CloseHandle(handle)",
            )
        )
    fake_cli_lines.extend(
        (
            'raise RuntimeError("simulated Python crash")'
            if crash
            else 'raise SystemExit(int(os.environ["FAKE_CLI_EXIT"]))',
        )
    )
    (fake_scripts / "incremental_price_audit.py").write_text(
        "\n".join(fake_cli_lines), encoding="utf-8"
    )
    runtime_root = tmp_path / f"runtime-{exit_code}"
    harness = tmp_path / f"invoke-runner-{exit_code}.ps1"
    harness.write_text(
        """param(
    [string] $Runner,
    [string] $FakeRepo,
    [string] $PythonExe,
    [string] $RuntimeRoot
)
function Get-Date { [datetime]'2026-08-13T17:00:00+08:00' }
& $Runner `
    -RepoRoot $FakeRepo `
    -PythonExe $PythonExe `
    -RuntimeRoot $RuntimeRoot `
    -BudgetSeconds 60 `
    -CloseoutBudgetSeconds 60 `
    -ProcessExitGraceSeconds 0 `
    -HardTimeoutMinutes 3
exit $LASTEXITCODE
""",
        encoding="ascii",
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-Runner",
            str(runner),
            "-FakeRepo",
            str(fake_repo),
            "-PythonExe",
            sys.executable,
            "-RuntimeRoot",
            str(runtime_root),
        ],
        capture_output=True,
        check=False,
        env={**os.environ, "FAKE_CLI_EXIT": str(exit_code)},
    )
    log_path = runtime_root / "scheduler_logs" / "2026-08-13.log"
    return completed, log_path.read_bytes()


def test_scheduled_runner_logs_stderr_without_overriding_success_exit_code(tmp_path):
    completed, log_bytes = _run_scheduled_runner_with_fake_cli(tmp_path, 0)

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert b"\x00" not in log_bytes
    log = log_bytes.decode("utf-8-sig")
    assert "fake-stdout" in log
    assert "fake-stderr" in log
    assert "END phase=tick exit_code=0" in log
    assert "DONE elapsed_seconds=" in log


def test_scheduled_runner_returns_exit_two_after_logging_stderr(tmp_path):
    completed, log_bytes = _run_scheduled_runner_with_fake_cli(tmp_path, 2)

    assert completed.returncode == 2, completed.stderr.decode("utf-8", errors="replace")
    assert b"\x00" not in log_bytes
    log = log_bytes.decode("utf-8-sig")
    assert "fake-stdout" in log
    assert "fake-stderr" in log
    assert "END phase=tick exit_code=2" in log
    assert "STOP reason=phase_failed phase=tick exit_code=2" in log
    assert "DONE elapsed_seconds=" not in log


def test_scheduled_runner_preserves_exit_seven(tmp_path):
    completed, log_bytes = _run_scheduled_runner_with_fake_cli(tmp_path, 7)

    assert completed.returncode == 7, completed.stderr.decode("utf-8", errors="replace")
    log = log_bytes.decode("utf-8-sig")
    assert "END phase=tick exit_code=7" in log
    assert "STOP reason=phase_failed phase=tick exit_code=7" in log


def test_scheduled_runner_logs_python_crash_and_returns_nonzero(tmp_path):
    completed, log_bytes = _run_scheduled_runner_with_fake_cli(
        tmp_path, 1, crash=True
    )

    assert completed.returncode == 1
    log = log_bytes.decode("utf-8-sig")
    assert "RuntimeError: simulated Python crash" in log
    assert "END phase=tick exit_code=1" in log
    assert "STOP reason=phase_failed phase=tick exit_code=1" in log


def test_scheduled_runner_keeps_log_open_across_native_output_lines(tmp_path):
    completed, log_bytes = _run_scheduled_runner_with_fake_cli(
        tmp_path, 0, lock_log_between_lines=True
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    log = log_bytes.decode("utf-8-sig")
    assert "fake-stdout" in log
    assert "fake-after-lock" in log
    assert "fake-stderr" in log
    assert "END phase=tick exit_code=0" in log
    assert "DONE elapsed_seconds=" in log


def test_scheduled_runner_drains_thousands_of_stdout_lines(tmp_path):
    completed, log_bytes = _run_scheduled_runner_with_fake_cli(
        tmp_path, 0, stdout_lines=3000
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    log = log_bytes.decode("utf-8-sig")
    assert "fake-stdout-0" in log
    assert "fake-stdout-2999" in log
    assert "END phase=tick exit_code=0" in log
    assert "DONE elapsed_seconds=" in log


def test_scheduled_runner_drains_thousands_of_stderr_lines(tmp_path):
    completed, log_bytes = _run_scheduled_runner_with_fake_cli(
        tmp_path, 0, stderr_lines=3000
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    log = log_bytes.decode("utf-8-sig")
    assert "fake-stderr-0" in log
    assert "fake-stderr-2999" in log
    assert "END phase=tick exit_code=0" in log
    assert "DONE elapsed_seconds=" in log


def _process_exists(pid: int) -> bool:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
        ],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def _wait_for_process_exit(pid: int, timeout: float = 8) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.05)
    return not _process_exists(pid)


def _start_scheduled_runner_with_hanging_tree(tmp_path: Path, *, budget_seconds: int):
    root = Path(__file__).resolve().parents[1]
    runner = root / "scripts/run_incremental_price_audit_scheduled.ps1"
    fake_repo = tmp_path / "fake-repo-hanging-tree"
    fake_scripts = fake_repo / "scripts"
    fake_scripts.mkdir(parents=True)
    pid_path = tmp_path / "hanging-tree-pids.json"
    (fake_scripts / "incremental_price_audit.py").write_text(
        "\n".join(
            (
                "import json",
                "import os",
                "import pathlib",
                "import subprocess",
                "import sys",
                "import time",
                "time.sleep(0.2)",
                "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])",
                'pathlib.Path(os.environ["FAKE_PID_PATH"]).write_text(',
                "    json.dumps({'child': os.getpid(), 'grandchild': grandchild.pid}),",
                "    encoding='ascii',",
                ")",
                'print("hanging-child-ready", flush=True)',
                "while True:",
                "    time.sleep(1)",
            )
        ),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runtime-hanging-tree"
    harness = tmp_path / "invoke-runner-hanging-tree.ps1"
    harness.write_text(
        f"""param(
    [string] $Runner,
    [string] $FakeRepo,
    [string] $PythonExe,
    [string] $RuntimeRoot
)
function Get-Date {{ [datetime]'2026-08-13T17:00:00+08:00' }}
& $Runner `
    -RepoRoot $FakeRepo `
    -PythonExe $PythonExe `
    -RuntimeRoot $RuntimeRoot `
    -BudgetSeconds {budget_seconds} `
    -CloseoutBudgetSeconds 60 `
    -ProcessExitGraceSeconds 0 `
    -HardTimeoutMinutes 3
exit $LASTEXITCODE
""",
        encoding="ascii",
    )
    process = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-Runner",
            str(runner),
            "-FakeRepo",
            str(fake_repo),
            "-PythonExe",
            sys.executable,
            "-RuntimeRoot",
            str(runtime_root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "FAKE_PID_PATH": str(pid_path)},
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not pid_path.is_file():
        time.sleep(0.05)
    assert pid_path.is_file(), "the hanging child did not publish its process ids"
    return process, json.loads(pid_path.read_text(encoding="ascii")), runtime_root


def test_scheduled_runner_timeout_kills_child_and_grandchild(tmp_path):
    process, pids, runtime_root = _start_scheduled_runner_with_hanging_tree(
        tmp_path, budget_seconds=1
    )
    stdout, stderr = process.communicate(timeout=12)

    assert process.returncode == 124, stderr.decode("utf-8", errors="replace")
    assert stdout == b""
    assert _wait_for_process_exit(pids["child"])
    assert _wait_for_process_exit(pids["grandchild"])
    log = (runtime_root / "scheduler_logs" / "2026-08-13.log").read_text(
        encoding="utf-8"
    )
    assert "TIMEOUT phase=tick" in log
    assert "END phase=tick exit_code=124" in log
    assert "STOP reason=phase_failed phase=tick exit_code=124" in log


def test_scheduled_runner_interruption_kills_child_and_grandchild(tmp_path):
    process, pids, _runtime_root = _start_scheduled_runner_with_hanging_tree(
        tmp_path, budget_seconds=60
    )
    process.terminate()
    process.wait(timeout=8)

    assert _wait_for_process_exit(pids["child"])
    assert _wait_for_process_exit(pids["grandchild"])


def test_scheduled_runner_log_open_failure_or_noop_is_bounded_and_starts_no_child(tmp_path):
    root = Path(__file__).resolve().parents[1]
    runner = root / "scripts/run_incremental_price_audit_scheduled.ps1"
    fake_repo = tmp_path / "fake-repo-log-failure"
    fake_scripts = fake_repo / "scripts"
    fake_scripts.mkdir(parents=True)
    child_started = tmp_path / "log-failure-child-started"
    (fake_scripts / "incremental_price_audit.py").write_text(
        f"import pathlib; pathlib.Path({str(child_started)!r}).write_text('started')\n",
        encoding="utf-8",
    )
    invalid_runtime_root = tmp_path / "runtime-is-a-file"
    invalid_runtime_root.write_text("not a directory", encoding="ascii")
    started = time.monotonic()
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
            "-RepoRoot",
            str(fake_repo),
            "-PythonExe",
            sys.executable,
            "-RuntimeRoot",
            str(invalid_runtime_root),
        ],
        capture_output=True,
        check=False,
        timeout=8,
    )

    now = datetime.now()
    scheduled = now.weekday() < 5 and (
        (now.minute == 0 and 9 <= now.hour <= 16)
        or (9 <= now.hour <= 16 and now.minute in {15, 30, 45})
        or (now.hour == 17 and now.minute % 5 == 0)  # 17:00-17:50 ticks + 17:55 drain
        or (now.hour in {18, 19} and now.minute % 5 == 0)
        or (now.hour == 20 and now.minute == 0)
    )
    assert completed.returncode != 0 if scheduled else completed.returncode == 0
    assert time.monotonic() - started < 8
    assert not child_started.exists()


def test_scheduled_process_host_writer_failure_kills_process_tree(tmp_path):
    root = Path(__file__).resolve().parents[1]
    host_source = root / "scripts/scheduled_process_host.cs"
    fake_cli = tmp_path / "writer-failure-child.py"
    pid_path = tmp_path / "writer-failure-pids.json"
    fake_cli.write_text(
        "\n".join(
            (
                "import json",
                "import os",
                "import pathlib",
                "import subprocess",
                "import sys",
                "import time",
                "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])",
                f"pathlib.Path({str(pid_path)!r}).write_text(json.dumps({{'child': os.getpid(), 'grandchild': grandchild.pid}}), encoding='ascii')",
                "for line in range(100):",
                "    print(f'writer-failure-{line}', flush=True)",
                "time.sleep(300)",
            )
        ),
        encoding="utf-8",
    )
    harness = tmp_path / "invoke-writer-failure.ps1"
    harness.write_text(
        """param(
    [string] $HostSource,
    [string] $PythonExe,
    [string] $FakeCli,
    [string] $WorkingDirectory
)
Add-Type -Path $HostSource | Out-Null
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
public sealed class CnncThrowingWriter : TextWriter
{
    private int lines = 0;
    public override Encoding Encoding { get { return Encoding.UTF8; } }
    public override void WriteLine(string value)
    {
        lines++;
        if (lines >= 2) { throw new IOException("simulated log writer failure"); }
    }
}
'@ | Out-Null
$result = [CnncScheduledProcessHost]::Run(
    $PythonExe,
    [string[]]@("-u", $FakeCli),
    $WorkingDirectory,
    [CnncThrowingWriter]::new(),
    10000
)
$result | ConvertTo-Json -Compress
exit $result.ExitCode
""",
        encoding="ascii",
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-HostSource",
            str(host_source),
            "-PythonExe",
            sys.executable,
            "-FakeCli",
            str(fake_cli),
            "-WorkingDirectory",
            str(tmp_path),
        ],
        capture_output=True,
        check=False,
        timeout=12,
    )

    assert completed.returncode == 74, completed.stderr.decode("utf-8", errors="replace")
    result = json.loads(completed.stdout.decode("utf-8-sig"))
    assert result["WriterFailed"] is True
    assert result["TimedOut"] is False
    assert result["Diagnostic"] == "IOException"
    pids = json.loads(pid_path.read_text(encoding="ascii"))
    assert _wait_for_process_exit(pids["child"])
    assert _wait_for_process_exit(pids["grandchild"])


def test_scheduled_runner_exits_when_its_stdout_is_not_consumed(tmp_path):
    root = Path(__file__).resolve().parents[1]
    runner = root / "scripts/run_incremental_price_audit_scheduled.ps1"
    fake_repo = tmp_path / "fake-repo-blocked-stdout"
    fake_scripts = fake_repo / "scripts"
    fake_scripts.mkdir(parents=True)
    child_exited = tmp_path / "child-exited"
    (fake_scripts / "incremental_price_audit.py").write_text(
        "\n".join(
            (
                "import pathlib",
                'print("X" * 1_000_000, flush=True)',
                f"pathlib.Path({str(child_exited)!r}).write_text('done', encoding='ascii')",
            )
        ),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runtime-blocked-stdout"
    harness = tmp_path / "invoke-runner-blocked-stdout.ps1"
    harness.write_text(
        """param(
    [string] $Runner,
    [string] $FakeRepo,
    [string] $PythonExe,
    [string] $RuntimeRoot
)
function Get-Date { [datetime]'2026-08-13T17:00:00+08:00' }
& $Runner `
    -RepoRoot $FakeRepo `
    -PythonExe $PythonExe `
    -RuntimeRoot $RuntimeRoot `
    -BudgetSeconds 60 `
    -CloseoutBudgetSeconds 60 `
    -ProcessExitGraceSeconds 0 `
    -HardTimeoutMinutes 3
exit $LASTEXITCODE
""",
        encoding="ascii",
    )
    process = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            "-Runner",
            str(runner),
            "-FakeRepo",
            str(fake_repo),
            "-PythonExe",
            sys.executable,
            "-RuntimeRoot",
            str(runtime_root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        return_code = process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        process.wait(timeout=5)
        return_code = None

    assert child_exited.is_file(), "the fake Python child did not exit"
    assert return_code == 0, "the scheduled wrapper remained blocked on its own stdout"
    log = (runtime_root / "scheduler_logs" / "2026-08-13.log").read_text(
        encoding="utf-8"
    )
    assert "END phase=tick exit_code=0" in log
    assert "DONE elapsed_seconds=" in log
