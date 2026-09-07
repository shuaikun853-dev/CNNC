"""Dashboard operator-action runner 的真实子进程验证(§64)。

全部走真实 ``subprocess.Popen``(_default_spawn,不注入 fake spawn):stub CLI
脚本放在 tmp 仓库里,子进程回显 argv/cwd/env 供断言。离线:零网络、零业务写、
不触碰真实仓库 runtime;真实 CLI 只跑 argparse ``--help``(同样零网络)。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.services import dashboard_actions
from app.services.dashboard_actions import DashboardActionError, DashboardActionRunner
from app.services.dashboard_manual_ops import read_operator_actions

pytestmark = pytest.mark.critical_safety

BUSINESS_DATE = "2026-01-01"

# __EXPECTED_CWD__ 会被替换成 repr(str(repo)),Windows 反斜杠安全。
STUB_SOURCE = """import json
import os
import sys
import time

EXPECTED_CWD = __EXPECTED_CWD__


def main() -> int:
    mode = os.environ.get("DASHBOARD_STUB_MODE", "")
    if mode == "hang":
        time.sleep(120)  # print 前阻塞,模拟真实卡死的 closeout
    cwd = os.getcwd()
    payload = {
        "stub": True,
        "cwd_ok": cwd.lower() == EXPECTED_CWD.lower()
                  or cwd.lower().startswith(EXPECTED_CWD.lower()),
        "script": sys.argv[0],
        "argv": sys.argv[1:],
        "env_marker": os.environ.get("DASHBOARD_SMOKE_MARKER", ""),
        "outcome": "ALREADY_FINALIZED",
        "publication": None,
        "publication_blocked": None,
    }
    # closeout 的 stdout JSON 会被 runner 解析后丢弃大部分字段,argv 等观测
    # 数据通过 sidecar 落盘供测试读取(cwd=repo,tmp 隔离)。
    with open(os.path.join(cwd, "stub_seen.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    if mode == "blocked":
        blocked = dict(payload)
        blocked["outcome"] = "MANUAL_REVIEW_REQUIRED"
        blocked["publication_blocked"] = "manual review required by stub"
        print(json.dumps(blocked))
        return 2
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


@pytest.fixture
def repo_stub(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    runtime_root = tmp_path / "runtime"
    script = repo / "scripts" / "incremental_price_audit.py"
    script.write_text(
        STUB_SOURCE.replace("__EXPECTED_CWD__", repr(str(repo))),
        encoding="utf-8",
    )
    return repo, runtime_root


def _wait_terminal(handle, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while handle.status == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert handle.status != "RUNNING", (
        f"supervisor did not finish within {timeout}s: {handle.outcome!r}"
    )
    return handle


def _stub_seen(repo: Path) -> dict:
    return json.loads((repo / "stub_seen.json").read_text(encoding="utf-8"))


def test_real_subprocess_closeout_success(repo_stub, monkeypatch):
    repo, runtime_root = repo_stub
    runner = DashboardActionRunner(repo_root=repo, runtime_root=runtime_root)
    # env 继承验证:哨兵经 os.environ 传给真实子进程(monkeypatch 自动清理)
    monkeypatch.setenv("DASHBOARD_SMOKE_MARKER", "sentinel")

    handle = _wait_terminal(runner.start(action_type="closeout", business_date=BUSINESS_DATE))

    assert handle.status == "SUCCEEDED"
    assert handle.outcome["result_code"] == "ALREADY_FINALIZED"
    assert handle.outcome["exit_code"] == 0
    assert handle.outcome["publication"] is None

    seen = _stub_seen(repo)
    assert seen["cwd_ok"] is True, seen
    assert seen["env_marker"] == "sentinel"
    # stub 回显 sys.argv:script 单独回显,argv 为其后的参数
    assert Path(seen["script"]) == repo / "scripts" / "incremental_price_audit.py"
    argv = seen["argv"]
    assert argv[0] == "closeout"
    assert argv[argv.index("--business-date") + 1] == BUSINESS_DATE
    assert argv[argv.index("--runtime-root") + 1] == str(runtime_root)
    assert argv[argv.index("--invocation") + 1] == "manual"

    # durable 账本:started 先于子进程,finished 由 runner 补记
    jsonl = runtime_root / BUSINESS_DATE / "dashboard_actions.jsonl"
    assert jsonl.is_file()
    records = [
        record for record in read_operator_actions(runtime_root / BUSINESS_DATE)
        if record["action_id"] == handle.action_id
    ]
    assert [record.get("event") for record in records] == ["started", "finished"]
    assert records[-1]["result_code"] == "ALREADY_FINALIZED"
    assert records[-1]["target"] == f"{BUSINESS_DATE}:-"


def test_real_subprocess_timeout_terminate(repo_stub, monkeypatch):
    repo, runtime_root = repo_stub
    monkeypatch.setitem(dashboard_actions._MAX_WAIT_SECONDS, "closeout", 2.0)
    monkeypatch.setenv("DASHBOARD_STUB_MODE", "hang")
    runner = DashboardActionRunner(repo_root=repo, runtime_root=runtime_root)

    started = time.monotonic()
    handle = _wait_terminal(
        runner.start(action_type="closeout", business_date=BUSINESS_DATE),
        timeout=15.0,
    )
    elapsed = time.monotonic() - started

    assert handle.status == "FAILED"
    assert handle.outcome["result_code"] == "TIMEOUT"
    assert elapsed < 15.0
    finished = [
        record for record in read_operator_actions(runtime_root / BUSINESS_DATE)
        if record.get("event") == "finished"
    ]
    assert finished and finished[-1]["timeout"] is True
    # 注:Windows 上不强测子进程已消亡(OpenProcess/pid 复用语义不可靠);
    # 终止由 runner 的 terminate→(grace)→kill 链负责,此处验证状态与结果码映射。
    assert handle.pid is not None


def test_double_click_real_subprocess_is_in_flight(repo_stub, monkeypatch):
    repo, runtime_root = repo_stub
    monkeypatch.setitem(dashboard_actions._MAX_WAIT_SECONDS, "closeout", 2.0)
    monkeypatch.setenv("DASHBOARD_STUB_MODE", "hang")
    runner = DashboardActionRunner(repo_root=repo, runtime_root=runtime_root)

    first = runner.start(action_type="closeout", business_date=BUSINESS_DATE)
    with pytest.raises(DashboardActionError) as excinfo:
        runner.start(action_type="closeout", business_date=BUSINESS_DATE)
    assert excinfo.value.code == "ACTION_IN_FLIGHT"
    assert excinfo.value.existing_action_id == first.action_id

    # 等第一个跑完(joiner 释放 (type,target) 槽位),不把卡死进程留给解释器退出
    _wait_terminal(first, timeout=15.0)
    assert first.status == "FAILED" and first.outcome["result_code"] == "TIMEOUT"


def test_closeout_blocked_exit2_mapping(repo_stub, monkeypatch):
    repo, runtime_root = repo_stub
    monkeypatch.setenv("DASHBOARD_STUB_MODE", "blocked")
    runner = DashboardActionRunner(repo_root=repo, runtime_root=runtime_root)

    handle = _wait_terminal(runner.start(action_type="closeout", business_date=BUSINESS_DATE))

    assert handle.status == "FAILED"
    assert handle.outcome["result_code"] == "BLOCKED"
    assert handle.outcome["exit_code"] == 2
    assert handle.outcome["outcome"] == "MANUAL_REVIEW_REQUIRED"
    assert handle.outcome["publication_blocked"] == "manual review required by stub"


def test_real_cli_exists_and_help_offline(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "incremental_price_audit.py"
    assert script.is_file(), f"real CLI missing: {script}"

    for args, needle in (
        (["--help"], "closeout"),
        (["reexport-existing-remote", "--help"], "--remote-job-id"),
        (["rebuild-remote-task", "--help"], "--remote-job-id"),
    ):
        proc = subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            timeout=60,
            cwd=str(repo),
        )
        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        assert proc.returncode == 0, f"{args}: rc={proc.returncode} stderr={stderr[-400:]}"
        assert needle in stdout, f"{args}: {needle!r} missing from --help output"

    # rebuild 的 --action-id 是 help=argparse.SUPPRESS 的隐藏 flag(不出现在
    # --help),但 dashboard rebuild 点击确实会传它——用纯 argparse 解析验证
    # CLI 接受该 flag(只 parse、不执行命令,零网络零业务写)。
    spec = importlib.util.spec_from_file_location("_incremental_price_audit_cli", script)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)  # 脚本带 __main__ guard,导入无副作用
    parsed = cli.build_parser().parse_args([
        "rebuild-remote-task",
        "--business-date", "2026-01-01",
        "--runtime-root", str(tmp_path),
        "--remote-job-id", "7",
        "--remote-identity", "jd|x|1",
        "--action-id", "smoke-action-1",
    ])
    assert parsed.action_id == "smoke-action-1"
