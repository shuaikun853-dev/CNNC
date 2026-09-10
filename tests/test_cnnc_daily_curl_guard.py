# -*- coding: utf-8 -*-
"""ensure_daily_curl_fresh 每日 cURL freshness guard hermetic 测试（2026-09-10）。

覆盖（全部 tmp 注入 + fake runner，绝不触碰真实 data/、绝不发网络/子进程）：
- Case A 昨日工件 CURL_REFRESH_OK 3/3 → 不新鲜：fake runner 恰好被调 1 次
  且 trigger=daily-preflight、business_date=传入参数；guard 根本不读
  closeout/finalize 状态（当日是否已 finalized 与判定完全无关）
- Case B 同日工件 CURL_REFRESH_OK 3/3 → runner 零调用
  （DAILY_CURL_ALREADY_FRESH）；连续三次调用全部 noop（持久幂等）
- Case C 进程重启语义：guard 无任何内存/模块级缓存，每次调用重读工件，
  只凭工件即可判新鲜（与 Case B 同断言天然成立）
- Case D 昨日工件 AUTH_REFRESH_OK_CURL_SYNC_FAILED → 今日刷新一次
- Case E 工件缺失/坏 JSON → 容错视为不新鲜 → 调 runner
- Case F fake runner rc=2 → SSO_LOGIN_REQUIRED；monkeypatch
  subprocess.Popen 禁触断言：guard 全程零子进程/浏览器调用
- Case G fake runner rc=4 → CURL_SYNC_PARTIAL 且工件不满足新鲜判定 →
  再次 guard 会再调 runner（部分失败不算新鲜，下一次 tick 会再尝试）
- rc=1 → AUTH_REFRESH_FAILED；rc=5 → AUTH_MAINTENANCE_BUSY（放行）
- fake runner raise → DAILY_CURL_REFRESH_ERROR 绝不上抛
- 同日工件但 curl_expected=0（空期望清单）→ 不算新鲜（防 0/0 误判）
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import cnnc_auth_refresh

TODAY = "2026-09-10"
YESTERDAY = "2026-09-09"


def _write_artifact(result_path, *, business_date=TODAY, outcome="CURL_REFRESH_OK",
                    curl_expected=3, curl_synced=3) -> None:
    """按 run_curl_refresh 工件 schema 写一份 fake 结果工件。"""
    payload = {
        "completed_at": "2026-09-10T00:00:00+00:00",
        "business_date": business_date,
        "trigger": "daily-preflight",
        "outcome": outcome,
        "curl_expected": curl_expected,
        "curl_synced": curl_synced,
        "curl_failed_files": [],
        "at_redacted": "AT-XXXX***",
    }
    Path(result_path).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _recorder(returncode=0, artifact=None, error=None):
    """fake run_curl_refresh：记录 kwargs，可注入返回码/落盘工件/异常。"""
    calls = []

    def runner(**kwargs):
        calls.append(dict(kwargs))
        if error is not None:
            raise error
        if artifact is not None:
            _write_artifact(kwargs["result_path"], **artifact)
        return returncode

    runner.calls = calls
    return runner


def _guard(tmp_path, runner, business_date=TODAY):
    return cnnc_auth_refresh.ensure_daily_curl_fresh(
        business_date,
        result_path=tmp_path / "curl_refresh_result.json",
        lock_path=tmp_path / "maintenance.lock",
        runner=runner,
    )


# ------------------------------------------------------------------- Case A


def test_case_a_yesterday_fresh_artifact_triggers_one_preflight_refresh(tmp_path):
    """昨日工件 3/3 全同步 → 今日不新鲜：恰好刷新一次且参数正确。

    即便同日已有 finalized 证据字段，guard 也不读 closeout 状态——判定
    只认工件本身（本测试不构造任何 closeout 数据即为反证）。
    """
    result_path = tmp_path / "curl_refresh_result.json"
    _write_artifact(result_path, business_date=YESTERDAY)
    runner = _recorder(returncode=0)

    status = _guard(tmp_path, runner)

    assert status == "DAILY_CURL_REFRESH_OK"
    assert len(runner.calls) == 1  # 恰好一次，不重试不叠加
    assert runner.calls[0]["trigger"] == "daily-preflight"
    assert runner.calls[0]["business_date"] == TODAY


# ------------------------------------------------------------------- Case B


def test_case_b_same_day_fresh_artifact_is_noop_three_consecutive_calls(tmp_path):
    """同日工件 CURL_REFRESH_OK 3/3 → runner 零调用；连续三次全部 noop。"""
    result_path = tmp_path / "curl_refresh_result.json"
    _write_artifact(result_path, business_date=TODAY)
    runner = _recorder(returncode=0)

    for _ in range(3):
        assert _guard(tmp_path, runner) == "DAILY_CURL_ALREADY_FRESH"

    assert runner.calls == []  # 新鲜期内绝不触发任何刷新动作


# ------------------------------------------------------------------- Case C


def test_case_c_process_restart_semantics_only_reads_artifact(tmp_path):
    """进程重启语义：无任何内存状态，只凭持久工件判新鲜。

    guard 不持有模块级缓存（每次调用重读工件），因此"重启后第一次调用"
    与 Case B 同断言天然成立：新 recorder + 同一份工件 → 依旧零刷新。
    """
    result_path = tmp_path / "curl_refresh_result.json"
    _write_artifact(result_path, business_date=TODAY)
    fresh_process_runner = _recorder(returncode=0)  # 模拟重启后的新进程

    status = _guard(tmp_path, fresh_process_runner)

    assert status == "DAILY_CURL_ALREADY_FRESH"
    assert fresh_process_runner.calls == []


# ------------------------------------------------------------------- Case D


def test_case_d_yesterday_partial_sync_artifact_refreshes_today(tmp_path):
    """昨日工件 outcome=AUTH_REFRESH_OK_CURL_SYNC_FAILED → 今日刷新一次。"""
    result_path = tmp_path / "curl_refresh_result.json"
    _write_artifact(
        result_path, business_date=YESTERDAY,
        outcome="AUTH_REFRESH_OK_CURL_SYNC_FAILED", curl_expected=3,
        curl_synced=2)
    runner = _recorder(returncode=0)

    status = _guard(tmp_path, runner)

    assert status == "DAILY_CURL_REFRESH_OK"
    assert len(runner.calls) == 1


# ------------------------------------------------------------------- Case E


@pytest.mark.parametrize("mode", ["missing", "bad_json"])
def test_case_e_missing_or_corrupt_artifact_counts_as_stale(tmp_path, mode):
    """工件缺失/坏 JSON 容错：一律视为不新鲜 → 调 runner 刷新。"""
    result_path = tmp_path / "curl_refresh_result.json"
    if mode == "bad_json":
        result_path.write_text("{not-json", encoding="utf-8")
    runner = _recorder(returncode=0)

    status = _guard(tmp_path, runner)

    assert status == "DAILY_CURL_REFRESH_OK"
    assert len(runner.calls) == 1


# ------------------------------------------------------------------- Case F


def test_case_f_sso_required_maps_status_with_zero_subprocess_calls(
        tmp_path, monkeypatch):
    """fake runner rc=2 → SSO_LOGIN_REQUIRED；guard 全程零子进程/浏览器。"""
    result_path = tmp_path / "curl_refresh_result.json"
    _write_artifact(result_path, business_date=YESTERDAY)
    runner = _recorder(returncode=2)

    def _no_popen(*args, **kwargs):
        raise AssertionError("guard must never spawn a subprocess/browser")

    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    status = _guard(tmp_path, runner)

    assert status == "SSO_LOGIN_REQUIRED"  # 只上报状态，绝不自动弹登录
    assert len(runner.calls) == 1


# ------------------------------------------------------------------- Case G


def test_case_g_partial_sync_is_never_fresh_and_retries_next_guard(tmp_path):
    """rc=4 → CURL_SYNC_PARTIAL；部分失败不算新鲜 → 再次 guard 再调 runner。

    语义：部分失败日下一次 tick 会再尝试刷新（docstring 契约）。
    """
    result_path = tmp_path / "curl_refresh_result.json"
    _write_artifact(result_path, business_date=YESTERDAY)
    runner = _recorder(
        returncode=4,
        artifact={"business_date": TODAY,
                  "outcome": "AUTH_REFRESH_OK_CURL_SYNC_FAILED",
                  "curl_expected": 3, "curl_synced": 2})

    assert _guard(tmp_path, runner) == "CURL_SYNC_PARTIAL"
    assert len(runner.calls) == 1
    # 工件虽是同日但 outcome 非 CURL_REFRESH_OK → 仍不满足新鲜判定
    assert _guard(tmp_path, runner) == "CURL_SYNC_PARTIAL"
    assert len(runner.calls) == 2


# ------------------------------------------------------------- rc 映射/异常


@pytest.mark.parametrize(
    "returncode,expected",
    [(1, "AUTH_REFRESH_FAILED"), (5, "AUTH_MAINTENANCE_BUSY")],
)
def test_runner_return_code_mapping(tmp_path, returncode, expected):
    result_path = tmp_path / "curl_refresh_result.json"
    _write_artifact(result_path, business_date=YESTERDAY)
    runner = _recorder(returncode=returncode)

    assert _guard(tmp_path, runner) == expected  # busy/失败均只上报，放行调用方


def test_runner_exception_maps_to_error_and_never_propagates(tmp_path):
    """runner 抛异常 → DAILY_CURL_REFRESH_ERROR；guard 绝不向 tick 上抛。"""
    result_path = tmp_path / "curl_refresh_result.json"
    _write_artifact(result_path, business_date=YESTERDAY)
    runner = _recorder(error=RuntimeError("boom"))

    status = _guard(tmp_path, runner)  # 若上抛本行即炸

    assert status == "DAILY_CURL_REFRESH_ERROR"
    assert len(runner.calls) == 1


def test_same_day_ok_with_zero_expected_is_not_fresh(tmp_path):
    """同日 CURL_REFRESH_OK 但 curl_expected=0（空期望清单）→ 不算新鲜。

    防 0/0 误判：没有任何 cURL 被核验过的"全同步"不是新鲜证据。
    """
    result_path = tmp_path / "curl_refresh_result.json"
    _write_artifact(result_path, business_date=TODAY, curl_expected=0,
                    curl_synced=0)
    runner = _recorder(returncode=0)

    status = _guard(tmp_path, runner)

    assert status == "DAILY_CURL_REFRESH_OK"
    assert len(runner.calls) == 1
