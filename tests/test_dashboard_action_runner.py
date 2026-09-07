"""Offline contract tests for the dashboard operator-action runner.

Fake spawn(Popen 替身)驱动:不启动真实子进程,不触网,不写生产 runtime;
runner 的意图落账 / joiner 线程收口 / 结果码映射 / 双击防重全部可观测。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from app.services.dashboard_actions import (
    DashboardActionError,
    DashboardActionRunner,
)
from app.services.dashboard_manual_ops import read_operator_actions

pytestmark = pytest.mark.critical_safety

BUSINESS_DATE = "2026-08-28"
IDENTITY = "JD|8899|1024"


class _FakeProcess:
    """Popen stand-in.

    ``hang=True`` 模拟预算耗尽(带 timeout 的 communicate 立即抛
    TimeoutExpired);``stuck=True`` 模拟真实卡死子进程(communicate 阻塞,
    直到 terminate/kill 才返回),用于观察 RUNNING 状态与双击防重。
    """

    def __init__(self, *, returncode=0, stdout="", stderr="", hang=False,
                 stuck=False, pid=4242):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self._stuck = stuck
        self.pid = pid
        self.terminated = False
        self.killed = False

    def communicate(self, timeout=None):
        if self._stuck:
            deadline = None if timeout is None else time.monotonic() + timeout
            while self._stuck:
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
                time.sleep(0.005)
            return self._stdout, self._stderr
        if self._hang and timeout is not None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return self._stdout, self._stderr

    def terminate(self):
        self.terminated = True
        self._hang = False
        self._stuck = False
        self.returncode = 1

    def kill(self):
        self.killed = True
        self._hang = False
        self._stuck = False
        self.returncode = 1


def _wait_done(handle, timeout=5.0):
    deadline = time.monotonic() + timeout
    while handle.status == "RUNNING" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert handle.status != "RUNNING", "supervisor thread did not finish"
    return handle


def _runner(tmp_path: Path, *processes: _FakeProcess):
    calls: list[dict] = []

    def spawn(argv, *, cwd, env):
        calls.append({"argv": list(argv), "cwd": cwd, "env": env})
        return processes[len(calls) - 1]

    runner = DashboardActionRunner(
        repo_root=tmp_path / "repo",
        runtime_root=tmp_path / "runtime",
        python_executable="X:/fake/python.exe",
        spawn=spawn,
    )
    return runner, calls


def _actions(tmp_path: Path):
    return read_operator_actions(tmp_path / "runtime" / BUSINESS_DATE)


def test_closeout_publication_maps_finalized(tmp_path):
    stdout = json.dumps({
        "business_date": BUSINESS_DATE, "outcome": "FINALIZED",
        "publication": r"E:\runtime\saas_audit_final_20260828.xlsx",
        "publication_blocked": None,
    })
    runner, calls = _runner(tmp_path, _FakeProcess(returncode=0, stdout=stdout))
    handle = runner.start(action_type="closeout", business_date=BUSINESS_DATE)
    assert handle.pid == 4242
    _wait_done(handle)
    assert handle.status == "SUCCEEDED"
    assert handle.outcome["result_code"] == "FINALIZED"
    assert handle.outcome["publication"].endswith("saas_audit_final_20260828.xlsx")
    # argv 走既有 CLI 子命令与 manual invocation
    argv = calls[0]["argv"]
    assert argv[0] == "X:/fake/python.exe"
    assert "closeout" in argv and "--invocation" in argv and "manual" in argv
    assert argv[argv.index("--business-date") + 1] == BUSINESS_DATE
    assert str(tmp_path / "runtime") in argv
    # durable 账本:started 先于子进程,finished 补记结果码
    records = _actions(tmp_path)
    events = [r.get("event") for r in records if r["action_id"] == handle.action_id]
    assert events[0] == "started"
    finished = [r for r in records if r.get("event") == "finished"]
    assert finished and finished[-1]["result_code"] == "FINALIZED"
    assert finished[-1]["outcome"]["publication"].endswith("saas_audit_final_20260828.xlsx")


def test_closeout_exit2_blocked_outcome(tmp_path):
    stdout = json.dumps({"outcome": "MANUAL_REVIEW_REQUIRED",
                         "publication": None, "publication_blocked": None})
    runner, _ = _runner(tmp_path, _FakeProcess(returncode=2, stdout=stdout))
    handle = _wait_done(runner.start(action_type="closeout", business_date=BUSINESS_DATE))
    assert handle.status == "FAILED"
    assert handle.outcome["result_code"] == "BLOCKED"


def test_closeout_publication_blocked_flag(tmp_path):
    stdout = json.dumps({"outcome": "INCOMPLETE", "publication": None,
                         "publication_blocked": "remote manual blockers"})
    runner, _ = _runner(tmp_path, _FakeProcess(returncode=2, stdout=stdout))
    handle = _wait_done(runner.start(action_type="closeout", business_date=BUSINESS_DATE))
    assert handle.outcome["result_code"] == "BLOCKED"
    assert handle.status == "FAILED"


def test_closeout_lock_conflict_text_maps_already_running(tmp_path):
    runner, _ = _runner(
        tmp_path,
        _FakeProcess(returncode=0, stdout="SKIPPED_ALREADY_RUNNING\nbusiness_date=x\n"),
    )
    handle = _wait_done(runner.start(action_type="closeout", business_date=BUSINESS_DATE))
    assert handle.outcome["result_code"] == "ALREADY_RUNNING"
    assert handle.status == "SUCCEEDED"


def test_closeout_timeout_terminates_subprocess(tmp_path):
    process = _FakeProcess(hang=True)
    runner, _ = _runner(tmp_path, process)
    handle = _wait_done(runner.start(action_type="closeout", business_date=BUSINESS_DATE))
    assert process.terminated and not process.killed
    assert handle.status == "FAILED"
    assert handle.outcome["result_code"] == "TIMEOUT"
    finished = [r for r in _actions(tmp_path) if r.get("event") == "finished"]
    assert finished[-1]["timeout"] is True


def test_double_click_same_target_is_in_flight(tmp_path):
    runner, _ = _runner(tmp_path, _FakeProcess(stuck=True), _FakeProcess(stuck=True))
    first = runner.start(action_type="closeout", business_date=BUSINESS_DATE)
    with pytest.raises(DashboardActionError) as excinfo:
        runner.start(action_type="closeout", business_date=BUSINESS_DATE)
    assert excinfo.value.code == "ACTION_IN_FLIGHT"
    assert excinfo.value.existing_action_id == first.action_id
    # 不同 target 不受影响
    other = runner.start(action_type="closeout", business_date="2026-08-27")
    assert other.action_id != first.action_id


def test_reexport_argv_and_outcome(tmp_path):
    stdout = json.dumps({"outcome": "REEXPORT_COMPLETED", "validated": 3})
    runner, calls = _runner(tmp_path, _FakeProcess(returncode=0, stdout=stdout))
    handle = runner.start(
        action_type="re-export",
        business_date=BUSINESS_DATE,
        remote_job_id=7,
        remote_identity=IDENTITY,
        timeout_seconds=90.0,
        action_id="act-reexport-1",
    )
    _wait_done(handle)
    argv = calls[0]["argv"]
    assert "reexport-existing-remote" in argv
    assert argv[argv.index("--remote-job-id") + 1] == "7"
    assert argv[argv.index("--remote-identity") + 1] == IDENTITY
    assert argv[argv.index("--timeout-seconds") + 1] == "90.0"
    assert argv[argv.index("--action-id") + 1] == "act-reexport-1"
    assert handle.status == "SUCCEEDED"
    assert handle.outcome["outcome"] == "REEXPORT_COMPLETED"
    # re-export/rebuild 的结果记录由 CLI 自己写;runner 只落 started 意图
    records = [r for r in _actions(tmp_path) if r["action_id"] == "act-reexport-1"]
    assert [r.get("event") for r in records] == ["started"]


def test_rebuild_failure_exit_code(tmp_path):
    stdout = json.dumps({"outcome": "REBUILD_REJECTED", "error_code": "LOCK_CONFLICT"})
    runner, calls = _runner(tmp_path, _FakeProcess(returncode=2, stdout=stdout))
    handle = runner.start(
        action_type="rebuild",
        business_date=BUSINESS_DATE,
        remote_job_id=9,
        remote_identity=IDENTITY,
    )
    _wait_done(handle)
    argv = calls[0]["argv"]
    assert "rebuild-remote-task" in argv
    assert argv[argv.index("--timeout-seconds") + 1] == "120.0"
    assert handle.status == "FAILED"
    assert handle.outcome["error_code"] == "LOCK_CONFLICT"


def test_invalid_action_type_rejects_before_any_write(tmp_path):
    runner, _ = _runner(tmp_path)
    with pytest.raises(DashboardActionError) as excinfo:
        runner.start(action_type="delete-everything", business_date=BUSINESS_DATE)
    assert excinfo.value.code == "INVALID_ACTION"
    assert _actions(tmp_path) == []


def test_durable_status_roundtrip(tmp_path):
    stdout = json.dumps({"outcome": "FINALIZED", "publication": "x.xlsx",
                         "publication_blocked": None})
    runner, _ = _runner(tmp_path, _FakeProcess(returncode=0, stdout=stdout))
    handle = _wait_done(runner.start(action_type="closeout", business_date=BUSINESS_DATE))
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    durable = runner.durable_status(handle.action_id, run_dir)
    assert durable is not None
    assert durable["action_type"] == "closeout"
    assert durable["status"] == "SUCCEEDED"
    assert durable["outcome"]["outcome"] == "FINALIZED"
    assert runner.durable_status("missing", run_dir) is None


def test_list_recent_running_first(tmp_path):
    finished_stdout = json.dumps({"outcome": "ALREADY_FINALIZED", "publication": None,
                                  "publication_blocked": None})
    runner, _ = _runner(tmp_path, _FakeProcess(stuck=True),
                        _FakeProcess(returncode=0, stdout=finished_stdout))
    running = runner.start(action_type="closeout", business_date=BUSINESS_DATE)
    done = runner.start(action_type="closeout", business_date="2026-08-27")
    _wait_done(done)
    listed = runner.list_recent()
    assert listed[0].action_id == running.action_id
    assert running.status == "RUNNING" and done.status == "SUCCEEDED"
