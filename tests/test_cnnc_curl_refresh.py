# -*- coding: utf-8 -*-
"""run_curl_refresh 独立 cURL 刷新操作层 hermetic 测试（2026-09-10）。

覆盖（全部 provider/result_path/lock_path 注入 tmp fake，绝不触碰真实 data/）：
- Seam A 成功：refresh 成功 + 3 个含新 AT 的可解析 cURL → rc=0、
  CURL_REFRESH_OK curl_synced=3/3、工件字段完整（trigger/business_date/
  at_redacted 前 7+***）、refresh 恰好一次且 force=True、锁 owner.command
  =curl-refresh
- Seam B SSO 失效：rc=2、工件 outcome=SSO_LOGIN_REQUIRED、stdout 无
  CURL_REFRESH_OK；fake provider 无任何浏览器/子进程 seam——本函数本就
  不该碰浏览器，若实现引入即测试爆炸
- Seam C 参数化严格后置核验：全同步 rc=0；旧 AT/文件缺失/不可解析 →
  rc=4 AUTH_REFRESH_OK_CURL_SYNC_FAILED（failed 只文件名，stdout/工件
  绝无全量 AT）
- 其他状态码（AT_VERIFY_FAILED）→ rc=1 AUTH_REFRESH_FAILED
- Seam H 维护锁互斥：锁被持 → rc=5 零副作用；运行中 run_login_and_recover
  → rc=5；各 outcome/异常路径 finally 释放锁
- CLI 兼容：--harvest 委托 run_curl_refresh（成功/SSO 两态）、--refresh-curl
  新参数、互斥组、LOGIN_RECOVERY_LOCK_PATH 别名值不变
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services.daily_audit_store import ProcessLock
from scripts import cnnc_auth_core as core
from scripts import cnnc_auth_refresh

NEW_AT = "AT-2-new-xyz999"
OLD_AT = "AT-1-old-abc000"
CURL_NAMES = ("latest_cnnc_query.curl", "detail_request.curl", "cnnc_biaozhun.curl")


class _FakeCurlRefreshProvider:
    """duck-type provider：只实现 run_curl_refresh 用到的 seam（refresh）。"""

    def __init__(self, curl_files=(), refresh_result="", refresh_error=None):
        self.curl_files = tuple(curl_files)
        self.refresh_calls = []
        self._refresh_result = refresh_result
        self._refresh_error = refresh_error

    def refresh(self, *args, **kwargs):
        self.refresh_calls.append((args, dict(kwargs)))
        if self._refresh_error is not None:
            raise self._refresh_error
        return self._refresh_result


class _CliFakeProvider(_FakeCurlRefreshProvider):
    """CLI 层 fake：cmd_harvest 兼容行还读 get_status（与真 provider 同形）。"""

    def get_status(self):
        return {
            "access_token_redacted": core.redact_secret(self._refresh_result),
            "last_refresh_at": "2026-09-10T08:00:00+00:00",
        }


def _write_curl_file(tmp_path, name, at):
    """写一个可被 load_curl_command 解析、含给定 AT 的 fake cURL 文件。"""
    path = tmp_path / name
    path.write_text(
        'curl "https://detail.invalid/quasiStandardCommodityDocumentModel/detail" '
        '-H "access-token: %s" -H "Content-Type: application/json" '
        "--data-raw '{}'" % at,
        encoding="utf-8",
    )
    return path


def _read_artifact(result_path):
    return json.loads(Path(result_path).read_text(encoding="utf-8"))


def _patch_cli_paths(monkeypatch, tmp_path):
    """CLI 测试把结果工件/维护锁路径全部重定向到 tmp，绝不触碰真实 data/。"""
    monkeypatch.setattr(cnnc_auth_refresh, "CURL_REFRESH_RESULT_PATH",
                        tmp_path / "curl_refresh_result.json")
    monkeypatch.setattr(cnnc_auth_refresh, "AUTH_MAINTENANCE_LOCK_PATH",
                        tmp_path / "maintenance.lock")


# ------------------------------------------------------------------- Seam A


def test_seam_a_success_reports_full_sync_and_writes_artifact(tmp_path, capsys):
    files = [_write_curl_file(tmp_path, name, NEW_AT) for name in CURL_NAMES]
    provider = _FakeCurlRefreshProvider(curl_files=files, refresh_result=NEW_AT)
    result_path = tmp_path / "curl_refresh_result.json"
    lock_path = tmp_path / "maintenance.lock"
    observed = {}

    real_refresh = provider.refresh

    def refresh(*args, **kwargs):
        observed.update(ProcessLock.inspect(lock_path))  # 运行中锁 owner 元数据
        return real_refresh(*args, **kwargs)

    provider.refresh = refresh

    rc = cnnc_auth_refresh.run_curl_refresh(
        provider=provider, result_path=result_path, lock_path=lock_path)

    assert rc == 0
    assert provider.refresh_calls == [((), {"force": True})]  # 恰好一次 force
    assert observed.get("command") == "curl-refresh"  # 维护锁 owner 区分操作
    out = capsys.readouterr().out
    assert "CURL_REFRESH_OK" in out
    assert "curl_synced=3/3" in out
    assert NEW_AT not in out  # 全量凭证绝不打印
    artifact = _read_artifact(result_path)
    assert artifact["outcome"] == "CURL_REFRESH_OK"
    assert artifact["trigger"] == "manual"
    assert artifact["business_date"] == datetime.now(
        ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")  # Asia/Shanghai 今日
    assert artifact["at_redacted"] == NEW_AT[:7] + "***"  # 前 7 + ***
    assert artifact["curl_expected"] == 3
    assert artifact["curl_synced"] == 3
    assert artifact["curl_failed_files"] == []
    assert artifact["completed_at"]  # UTC ISO 时间戳
    assert NEW_AT not in Path(result_path).read_text(encoding="utf-8")


def test_business_date_param_and_daily_preflight_trigger(tmp_path, capsys):
    """business_date 参数权威（覆盖缺省）；trigger=daily-preflight 入工件。"""
    provider = _FakeCurlRefreshProvider(refresh_result=NEW_AT)  # 无 curl_files
    result_path = tmp_path / "curl_refresh_result.json"

    rc = cnnc_auth_refresh.run_curl_refresh(
        provider=provider, result_path=result_path,
        lock_path=tmp_path / "maintenance.lock",
        trigger="daily-preflight", business_date="2026-09-01")

    assert rc == 0  # 期望清单为空 = 全同步（与 login-and-recover 同语义）
    assert "curl_synced=0/0" in capsys.readouterr().out
    artifact = _read_artifact(result_path)
    assert artifact["trigger"] == "daily-preflight"
    assert artifact["business_date"] == "2026-09-01"


# ------------------------------------------------------------------- Seam B


def test_seam_b_sso_login_required_maps_to_2(tmp_path, capsys):
    provider = _FakeCurlRefreshProvider(
        refresh_error=core.CnncAuthError(
            core.CnncAuthStatus.SSO_LOGIN_REQUIRED, "SSO 会话已失效"))
    result_path = tmp_path / "curl_refresh_result.json"

    rc = cnnc_auth_refresh.run_curl_refresh(
        provider=provider, result_path=result_path,
        lock_path=tmp_path / "maintenance.lock")

    assert rc == 2
    assert provider.refresh_calls == [((), {"force": True})]
    captured = capsys.readouterr()
    assert "CURL_REFRESH_OK" not in captured.out
    assert "SSO 登录态已失效" in captured.err  # 提示重新登录，不弹浏览器
    artifact = _read_artifact(result_path)
    assert artifact["outcome"] == "SSO_LOGIN_REQUIRED"
    assert artifact["at_redacted"] == ""
    assert artifact["curl_expected"] == 0
    assert artifact["curl_synced"] == 0


def test_other_auth_error_maps_to_auth_refresh_failed(tmp_path, capsys):
    provider = _FakeCurlRefreshProvider(
        refresh_error=core.CnncAuthError(
            core.CnncAuthStatus.AT_VERIFY_FAILED, "verify failed"))
    result_path = tmp_path / "curl_refresh_result.json"

    rc = cnnc_auth_refresh.run_curl_refresh(
        provider=provider, result_path=result_path,
        lock_path=tmp_path / "maintenance.lock")

    assert rc == 1
    captured = capsys.readouterr()
    assert "CURL_REFRESH_OK" not in captured.out
    assert "AT_VERIFY_FAILED" in captured.err
    artifact = _read_artifact(result_path)
    assert artifact["outcome"] == "AUTH_REFRESH_FAILED"
    assert artifact["at_redacted"] == ""


# ------------------------------------------------------------------- Seam C


@pytest.mark.parametrize(
    "case", ["all_synced", "stale_at", "missing_file", "unparsable"]
)
def test_seam_c_postcondition_matrix(tmp_path, capsys, case):
    good_a = _write_curl_file(tmp_path, CURL_NAMES[0], NEW_AT)
    good_b = _write_curl_file(tmp_path, CURL_NAMES[2], NEW_AT)
    bad_name = CURL_NAMES[1]
    if case == "stale_at":
        middle = _write_curl_file(tmp_path, bad_name, OLD_AT)  # 不含新 AT
    elif case == "missing_file":
        middle = tmp_path / bad_name  # 从未创建
    elif case == "unparsable":
        middle = tmp_path / bad_name
        middle.write_text("not a curl command at all", encoding="utf-8")
    else:
        middle = _write_curl_file(tmp_path, bad_name, NEW_AT)
    provider = _FakeCurlRefreshProvider(
        curl_files=(good_a, middle, good_b), refresh_result=NEW_AT)
    result_path = tmp_path / "curl_refresh_result.json"

    rc = cnnc_auth_refresh.run_curl_refresh(
        provider=provider, result_path=result_path,
        lock_path=tmp_path / "maintenance.lock")

    captured = capsys.readouterr()
    artifact = _read_artifact(result_path)
    if case == "all_synced":
        assert rc == 0
        assert "CURL_REFRESH_OK" in captured.out
        assert artifact["outcome"] == "CURL_REFRESH_OK"
    else:
        assert rc == 4  # 刷新成功但 cURL 同步不完整
        assert "AUTH_REFRESH_OK_CURL_SYNC_FAILED" in captured.out
        assert "curl_synced=2/3" in captured.out
        assert ("failed=%s" % bad_name) in captured.out  # 只文件名，绝无路径
        assert artifact["outcome"] == "AUTH_REFRESH_OK_CURL_SYNC_FAILED"
        assert artifact["curl_failed_files"] == [bad_name]
    assert artifact["curl_expected"] == 3
    assert artifact["curl_synced"] == (3 if case == "all_synced" else 2)
    assert NEW_AT not in captured.out
    assert NEW_AT not in Path(result_path).read_text(encoding="utf-8")


# ------------------------------------------------------------------- Seam H


def test_seam_h_lock_held_returns_5_zero_side_effects(tmp_path, capsys):
    """维护锁被他人持有 → rc=5、refresh 零调用、不写工件、stderr 提示。"""
    lock_path = tmp_path / "maintenance.lock"
    holder = ProcessLock(lock_path).acquire(
        owner={"pid": 12345, "command": "login-and-recover"})
    try:
        provider = _FakeCurlRefreshProvider(refresh_result=NEW_AT)
        result_path = tmp_path / "curl_refresh_result.json"

        rc = cnnc_auth_refresh.run_curl_refresh(
            provider=provider, result_path=result_path, lock_path=lock_path)

        assert rc == 5
        assert provider.refresh_calls == []  # 零副作用：绝不触发刷新
        assert not result_path.exists()  # 绝不写结果工件
    finally:
        holder.release()
    assert "AUTH_MAINTENANCE_BUSY" in capsys.readouterr().err


def test_seam_h_login_recover_blocked_while_curl_refresh_runs(tmp_path, capsys):
    """互斥反向：curl-refresh 持锁运行期间，run_login_and_recover → rc=5。"""
    lock_path = tmp_path / "maintenance.lock"
    result_path = tmp_path / "curl_refresh_result.json"
    observed = {}

    def refresh(*args, **kwargs):
        observed["active_during"] = ProcessLock.inspect(lock_path)["active"]
        observed["login_rc"] = cnnc_auth_refresh.run_login_and_recover(
            login_runner=lambda: 0,
            provider=_FakeCurlRefreshProvider(refresh_result=NEW_AT),
            result_path=tmp_path / "recovery_result.json",
            lock_path=lock_path)  # 注入同一把锁
        return NEW_AT

    provider = _FakeCurlRefreshProvider(refresh_result=NEW_AT)
    provider.refresh = refresh

    rc = cnnc_auth_refresh.run_curl_refresh(
        provider=provider, result_path=result_path, lock_path=lock_path)

    assert rc == 0
    assert observed["active_during"] is True
    assert observed["login_rc"] == 5  # 维护锁互斥：恢复流程被拒零副作用
    assert "LOGIN_RECOVERY_ALREADY_RUNNING" in capsys.readouterr().err
    assert ProcessLock.inspect(lock_path)["active"] is False  # 结束后已释放


@pytest.mark.parametrize("scenario", ["ok", "sso", "curl_partial", "raise"])
def test_seam_h_lock_released_on_every_path(tmp_path, scenario):
    """rc=0/2/4 及异常路径 finally 释放锁，无泄漏。"""
    lock_path = tmp_path / "maintenance.lock"
    result_path = tmp_path / "curl_refresh_result.json"
    if scenario == "sso":
        provider = _FakeCurlRefreshProvider(refresh_error=core.CnncAuthError(
            core.CnncAuthStatus.SSO_LOGIN_REQUIRED, "SSO 失效（锁释放）"))
    elif scenario == "curl_partial":
        good = _write_curl_file(tmp_path, CURL_NAMES[0], NEW_AT)
        bad = tmp_path / CURL_NAMES[1]
        bad.write_text("not a curl command", encoding="utf-8")
        provider = _FakeCurlRefreshProvider(
            curl_files=(good, bad), refresh_result=NEW_AT)
    elif scenario == "raise":
        provider = _FakeCurlRefreshProvider()

        def boom(*args, **kwargs):
            provider.refresh_calls.append((args, dict(kwargs)))
            raise RuntimeError("boom")

        provider.refresh = boom
    else:
        provider = _FakeCurlRefreshProvider(refresh_result=NEW_AT)

    if scenario == "raise":
        with pytest.raises(RuntimeError):
            cnnc_auth_refresh.run_curl_refresh(
                provider=provider, result_path=result_path, lock_path=lock_path)
    else:
        rc = cnnc_auth_refresh.run_curl_refresh(
            provider=provider, result_path=result_path, lock_path=lock_path)
        assert rc == {"ok": 0, "sso": 2, "curl_partial": 4}[scenario]

    assert ProcessLock.inspect(lock_path)["active"] is False


# ------------------------------------------------------------- CLI 兼容层


def test_login_recovery_lock_path_alias_value_unchanged():
    """旧常量名/值不变（dashboard_api 与既有测试按旧名引用同一把锁）。"""
    assert cnnc_auth_refresh.LOGIN_RECOVERY_LOCK_PATH == (
        cnnc_auth_refresh.ROOT / "data" / ".cnnc_login_recovery.lock")
    assert cnnc_auth_refresh.AUTH_MAINTENANCE_LOCK_PATH == (
        cnnc_auth_refresh.LOGIN_RECOVERY_LOCK_PATH)
    assert cnnc_auth_refresh.CURL_REFRESH_RESULT_PATH.name == (
        "cnnc_curl_refresh_result.json")


def test_cli_harvest_delegates_to_curl_refresh_success(
        tmp_path, monkeypatch, capsys):
    _patch_cli_paths(monkeypatch, tmp_path)
    provider = _CliFakeProvider(refresh_result=NEW_AT)
    monkeypatch.setattr(cnnc_auth_refresh, "_default_provider", lambda: provider)

    rc = cnnc_auth_refresh.main(["--harvest"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "CURL_REFRESH_OK" in out
    assert "REFRESH_OK at=%s" % core.redact_secret(NEW_AT) in out  # 兼容旧行
    assert "last_refresh_at=2026-09-10T08:00:00+00:00" in out
    assert NEW_AT not in out
    artifact = _read_artifact(tmp_path / "curl_refresh_result.json")
    assert artifact["outcome"] == "CURL_REFRESH_OK"  # 证明 --harvest 走新操作层
    assert provider.refresh_calls == [((), {"force": True})]


def test_cli_harvest_sso_required_maps_to_2(tmp_path, monkeypatch, capsys):
    _patch_cli_paths(monkeypatch, tmp_path)
    provider = _CliFakeProvider(refresh_error=core.CnncAuthError(
        core.CnncAuthStatus.SSO_LOGIN_REQUIRED, "SSO 失效（CLI）"))
    monkeypatch.setattr(cnnc_auth_refresh, "_default_provider", lambda: provider)

    rc = cnnc_auth_refresh.main(["--harvest"])

    assert rc == 2
    captured = capsys.readouterr()
    assert "CURL_REFRESH_OK" not in captured.out
    assert "SSO 登录态已失效" in captured.err
    artifact = _read_artifact(tmp_path / "curl_refresh_result.json")
    assert artifact["outcome"] == "SSO_LOGIN_REQUIRED"


def test_cli_refresh_curl_flag_runs_operation(tmp_path, monkeypatch, capsys):
    _patch_cli_paths(monkeypatch, tmp_path)
    provider = _CliFakeProvider(refresh_result=NEW_AT)
    monkeypatch.setattr(cnnc_auth_refresh, "_default_provider", lambda: provider)

    rc = cnnc_auth_refresh.main(["--refresh-curl"])

    assert rc == 0
    assert "CURL_REFRESH_OK" in capsys.readouterr().out
    assert provider.refresh_calls == [((), {"force": True})]
    assert _read_artifact(tmp_path / "curl_refresh_result.json")[
        "outcome"] == "CURL_REFRESH_OK"


def test_cli_harvest_and_refresh_curl_are_mutually_exclusive():
    with pytest.raises(SystemExit) as excinfo:
        cnnc_auth_refresh.main(["--harvest", "--refresh-curl"])
    assert excinfo.value.code == 2
