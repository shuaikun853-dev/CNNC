from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from app.services.incremental_audit_runtime import RuntimeResult, run_runtime_command, runtime_paths
from app.services.incremental_audit_status import (
    collect_runtime_status,
    runtime_doctor,
    write_daily_status,
)
from app.workflows.incremental_price_audit import DiscoveredState, TickDependencies, TickSummary
from scripts import incremental_price_audit as cli


BUSINESS_DATE = "2026-08-11"


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
        model=lambda _row, _timeout: (_ for _ in ()).throw(TimeoutError()),
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


def test_scheduler_scripts_default_to_non_mutating_preview():
    root = Path(__file__).resolve().parents[1]
    register = root / "scripts/register_incremental_price_audit_tasks.ps1"
    unregister = root / "scripts/unregister_incremental_price_audit_tasks.ps1"

    registered = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(register), "-RepoRoot", str(root)],
        capture_output=True,
        check=False,
    )
    removed = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(unregister)],
        capture_output=True,
        check=False,
    )

    assert registered.returncode == 0, registered.stderr.decode("utf-8", errors="replace")
    assert removed.returncode == 0, removed.stderr.decode("utf-8", errors="replace")
    register_plan = json.loads(registered.stdout.decode("utf-8-sig"))
    unregister_plan = json.loads(removed.stdout.decode("utf-8-sig"))
    assert register_plan["Applied"] is False
    assert register_plan["MultipleInstances"] == "IgnoreNew"
    assert register_plan["ExecutionTimeLimit"] == "PT14M"
    assert all("--budget-seconds 720 --shadow" in task["Arguments"] for task in register_plan["Tasks"])
    assert unregister_plan["Applied"] is False
