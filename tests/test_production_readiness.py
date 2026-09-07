"""生产就绪只读检查的封闭测试（fake runner / fake 时钟 / tmp_path 假仓库）。

全部用例都不真实运行 powershell / git / tzutil，也不触碰生产目录。
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from app.services import legacy_zhaoshang_adapter
from app.services.production_readiness import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    STATUS_WARN,
    CheckResult,
    ReadinessReport,
    build_readiness_report,
)

EXPECTED_BRANCH = "prod/closeout-20260818"
FIXED_CLOCK = datetime(2026, 9, 4, 12, 0, 0)

SCHEDULER_TASK_GREEN = {
    "TaskName": "CNNC-PriceAudit-Incremental",
    "State": "Ready",
    "Triggers": [
        {"StartBoundary": "2026-09-01T09:00:00"},
        {"StartBoundary": "2026-09-01T17:00:00"},
        {"StartBoundary": "2026-09-01T18:00:00"},
        {"StartBoundary": "2026-09-01T20:35:00"},
    ],
    "Settings": {
        "MultipleInstances": {"Policy": "IgnoreNew"},
        "ExecutionTimeLimit": "PT32M",
    },
    "Actions": [
        {
            "Execute": "powershell.exe",
            "Arguments": (
                '-NoProfile -NonInteractive -File '
                '"E:\\repo\\scripts\\run_incremental_price_audit_scheduled.ps1" '
                "-RepoRoot E:\\repo"
            ),
        }
    ],
}

SCHEDULER_INFO_GREEN = {
    "LastRunTime": "2026-09-04T09:00:00",
    "LastTaskResult": 0,
    "NextRunTime": "2026-09-04T17:00:00",
    "NumberOfMissedRuns": 0,
}

# Windows PowerShell 5.1 的 ConvertTo-Json 会把 CimInstance 序列化成
# CimInstanceProperties（Name/Value）嵌套；枚举为 int（State 3=Ready，
# MultipleInstances 2=IgnoreNew），StartBoundary 带 +08:00 后缀。
SCHEDULER_TASK_CIM = {
    "TaskName": "CNNC-PriceAudit-Incremental",
    "State": 3,
    "Triggers": [
        {
            "CimInstanceProperties": [
                {"Name": "StartBoundary", "Value": "2026-08-28T09:00:00+08:00"},
                {"Name": "Enabled", "Value": True},
            ]
        },
        {
            "CimInstanceProperties": [
                {"Name": "StartBoundary", "Value": "2026-08-28T17:00:00+08:00"},
                {"Name": "Enabled", "Value": True},
            ]
        },
        {
            "CimInstanceProperties": [
                {"Name": "StartBoundary", "Value": "2026-08-28T18:00:00+08:00"},
                {"Name": "Enabled", "Value": True},
            ]
        },
        {
            "CimInstanceProperties": [
                {"Name": "StartBoundary", "Value": "2026-08-28T20:35:00+08:00"},
                {"Name": "Enabled", "Value": True},
            ]
        },
    ],
    "Settings": {
        "CimInstanceProperties": [
            {"Name": "MultipleInstances", "Value": 2},
            {"Name": "ExecutionTimeLimit", "Value": "PT32M"},
        ]
    },
    "Actions": [
        {
            "CimInstanceProperties": [
                {"Name": "Execute", "Value": "powershell.exe"},
                {
                    "Name": "Arguments",
                    "Value": (
                        '-NoProfile -File "E:\\repo\\scripts\\'
                        'run_incremental_price_audit_scheduled.ps1"'
                    ),
                },
            ]
        }
    ],
}


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeRunner:
    """按命令返回预置输出的替身；绝不执行真实外部命令。"""

    def __init__(
        self,
        *,
        tz: str = "China Standard Time",
        git_mode: str = "ok",
        branch: str = EXPECTED_BRANCH,
        dirty: str = "",
        scheduler: str = "ok",
        task_style: str = "plain",
    ) -> None:
        self.tz = tz
        self.git_mode = git_mode
        self.branch = branch
        self.dirty = dirty
        # scheduler: ok / missing / timeout；task_style: plain / cim
        self.scheduler = scheduler
        self.task_style = task_style
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        program = Path(cmd[0]).name.lower() if cmd else ""
        if program == "git":
            if self.git_mode == "unavailable":
                raise FileNotFoundError(f"git 不存在: {cmd}")
            sub = cmd[3] if len(cmd) > 3 else ""
            if sub == "rev-parse":
                if self.git_mode == "error":
                    return _result(128, "", "fatal: not a git repository")
                arg = cmd[4] if len(cmd) > 4 else ""
                if arg == "HEAD":
                    return _result(0, "2bc7cc6" + "0" * 33 + "\n", "")
                return _result(0, "true\n", "")
            if sub == "branch":
                return _result(0, self.branch + "\n", "")
            if sub == "status":
                return _result(0, self.dirty, "")
            return _result(1, "", f"unexpected git subcommand: {sub}")
        if program == "tzutil":
            return _result(0, self.tz + "\n", "")
        if program in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
            if self.scheduler == "timeout":
                raise subprocess.TimeoutExpired(cmd, timeout)
            if self.scheduler == "missing":
                return _result(
                    1,
                    "",
                    "Get-ScheduledTask : No MSFT_ScheduledTask objects found "
                    "with name 'CNNC-PriceAudit-Incremental'.",
                )
            text = " ".join(cmd)
            if "Get-ScheduledTaskInfo" in text:
                return _result(0, json.dumps(SCHEDULER_INFO_GREEN), "")
            if "CNNC-Dashboard-ReadOnly" in text:
                return _result(0, "True\n", "")
            task_payload = (
                SCHEDULER_TASK_CIM if self.task_style == "cim" else SCHEDULER_TASK_GREEN
            )
            return _result(0, json.dumps(task_payload), "")
        raise AssertionError(f"unexpected command: {cmd}")


def _by_name(report: ReadinessReport, name: str) -> CheckResult:
    matching = [check for check in report.checks if check.name == name]
    assert matching, f"missing check {name}; got {[c.name for c in report.checks]}"
    return matching[0]


def _make_green_repo(root: Path) -> None:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "auth_state.json").write_text("{}", encoding="utf-8")
    (data / "model.env").write_text("MODEL_ENDPOINT=stub\n", encoding="utf-8")
    (data / "detail_request.curl").write_text("# stub curl\n", encoding="utf-8")
    (data / "latest_saas_sameAudit_queryAll.curl").write_text("# stub curl\n", encoding="utf-8")
    (data / "cnnc_auth_ledger.jsonl").write_text("{}\n", encoding="utf-8")
    runtime = root / "runtime" / "incremental_price_audit"
    runtime.mkdir(parents=True, exist_ok=True)


def _build(repo: Path, runner: FakeRunner, clock=None) -> ReadinessReport:
    return build_readiness_report(
        repo,
        expected_branch=EXPECTED_BRANCH,
        runner=runner,
        wall_clock=clock or (lambda: FIXED_CLOCK),
    )


def test_all_green_ready(tmp_path: Path, monkeypatch) -> None:
    _make_green_repo(tmp_path)
    tool = tmp_path / "external_tools" / "auto_crawl_task.py"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("# stub external tool\n", encoding="utf-8")
    monkeypatch.setattr(legacy_zhaoshang_adapter, "EXTERNAL_TOOL_PATH", tool)

    runner = FakeRunner()
    report = _build(tmp_path, runner, clock=lambda: datetime.now())

    statuses = {check.name: check.status for check in report.checks}
    assert statuses == {
        "Operating system": STATUS_PASS,
        "Git repository": STATUS_PASS,
        "Python runtime": STATUS_PASS,
        "Timezone": STATUS_PASS,
        "Task scheduler": STATUS_PASS,
        "Auth state": STATUS_PASS,
        "External tools": STATUS_PASS,
    }
    assert report.overall == "READY"
    assert report.exit_code() == 0
    # 所有 powershell 调用都必须是只读查询（-NoProfile -NonInteractive）。
    for cmd in runner.calls:
        if cmd and Path(cmd[0]).name.lower().startswith("powershell"):
            assert "-NoProfile" in cmd
            assert "-NonInteractive" in cmd
            assert "Get-ScheduledTask" in " ".join(cmd)


def test_git_unavailable_unknown(tmp_path: Path) -> None:
    """git 可执行缺失或查询非零退出都收敛为 UNKNOWN，程序不崩。"""
    for mode in ("unavailable", "error"):
        runner = FakeRunner(git_mode=mode)
        report = _build(tmp_path, runner)
        git_check = _by_name(report, "Git repository")
        assert git_check.status == STATUS_UNKNOWN, mode
    assert report.overall == "NOT READY"
    assert report.exit_code() == 1


def test_scheduler_task_missing_fail(tmp_path: Path) -> None:
    runner = FakeRunner(scheduler="missing")
    report = _build(tmp_path, runner)
    scheduler_check = _by_name(report, "Task scheduler")
    assert scheduler_check.status == STATUS_FAIL
    assert "CNNC-PriceAudit-Incremental" in scheduler_check.summary


def test_scheduler_cim_style_json_parses(tmp_path: Path) -> None:
    """Windows PowerShell 5.1 的 CimInstanceProperties 嵌套格式也能校验 PASS。"""
    runner = FakeRunner(task_style="cim")
    report = _build(tmp_path, runner)
    scheduler_check = _by_name(report, "Task scheduler")
    assert scheduler_check.status == STATUS_PASS
    joined = "\n".join(scheduler_check.details)
    assert "State: Ready" in joined


def test_scheduler_timeout_unknown(tmp_path: Path) -> None:
    runner = FakeRunner(scheduler="timeout")
    report = _build(tmp_path, runner)
    scheduler_check = _by_name(report, "Task scheduler")
    assert scheduler_check.status == STATUS_UNKNOWN


def test_timezone_mismatch_warn(tmp_path: Path) -> None:
    runner = FakeRunner(tz="Tokyo Standard Time")
    report = _build(tmp_path, runner)
    timezone_check = _by_name(report, "Timezone")
    assert timezone_check.status == STATUS_WARN
    joined = "\n".join(timezone_check.details)
    assert "Expected" in joined
    assert "Actual" in joined
    assert "Tokyo Standard Time" in joined
    assert "China Standard Time" in joined


def test_auth_state_missing_fail(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "model.env").write_text("MODEL_ENDPOINT=stub\n", encoding="utf-8")
    (data / "detail_request.curl").write_text("# stub\n", encoding="utf-8")
    (data / "latest_saas_sameAudit_queryAll.curl").write_text("# stub\n", encoding="utf-8")
    (data / "cnnc_auth_ledger.jsonl").write_text("{}\n", encoding="utf-8")

    report = _build(tmp_path, FakeRunner())
    auth_check = _by_name(report, "Auth state")
    assert auth_check.status == STATUS_FAIL
    assert "auth_state.json" in "\n".join(auth_check.details)
    assert report.exit_code() == 1


def test_external_tool_missing_fail(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "no_such_dir" / "auto_crawl_task.py"
    monkeypatch.setattr(legacy_zhaoshang_adapter, "EXTERNAL_TOOL_PATH", missing)

    report = _build(tmp_path, FakeRunner())
    tools_check = _by_name(report, "External tools")
    assert tools_check.status == STATUS_FAIL
    assert str(missing) in "\n".join(tools_check.details)


def test_to_dict_json_serializable() -> None:
    report = ReadinessReport(
        checks=[CheckResult("X", STATUS_PASS, "ok", ["detail-1"])]
    )
    payload = report.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload["overall"] == "READY"
    assert payload["worst_status"] == STATUS_PASS
    assert payload["checks"][0]["name"] == "X"
    assert "detail-1" in encoded


def test_exit_code_mapping() -> None:
    all_pass = ReadinessReport(
        checks=[CheckResult("A", STATUS_PASS, "", [])]
    )
    assert all_pass.exit_code() == 0
    assert all_pass.overall == "READY"

    unknown_only = ReadinessReport(
        checks=[CheckResult("A", STATUS_UNKNOWN, "", [])]
    )
    assert unknown_only.exit_code() == 1
    assert unknown_only.overall == "NOT READY"
    assert unknown_only.worst_status == STATUS_UNKNOWN

    empty = ReadinessReport()
    assert empty.exit_code() == 1
    assert empty.worst_status == STATUS_UNKNOWN
