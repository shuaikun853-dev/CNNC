# -*- coding: utf-8 -*-
"""CNNC 认证恢复链路 hermetic 测试（Case 13, 15, 16 + 2026-09-10 cURL 后置条件）。

- Case 13 run_login_and_recover：login 成功→force 刷新→发布；login 失败/
  refresh SSO 失败的退出码与失败落盘
- 2026-09-10 严格 cURL 后置条件（不改 refresh 内部宽松语义）：refresh 成功后
  核验期望 cURL 文件（存在/可解析/含新 AT），全同步 rc=0 RECOVERY_OK；部分
  失败 rc=4 AUTH_REFRESH_OK_CURL_SYNC_FAILED（只报文件名）；四种 outcome 都
  写结果工件（tmp 注入，绝不触碰真实 data/）
- Case 15 detail 请求 HTTP 401 → refresh 一次（携带 deadline）→ 重试成功
- Case 16 detail 请求连续两次 401 → 失败上抛，禁止第三次请求

全部依赖（login_runner/provider/call_detail_api/result_path）注入，无真实网络、
无浏览器、无子进程。provider 以 duck-type fake 注入，只实现被测路径用到的 seam。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.services.incremental_audit_runtime import _detail_worker
from scripts import cnnc_auth_core as core
from scripts import cnnc_auth_refresh

OLD_AT = "AT-1-old"
NEW_AT = "AT-2-new"

# detail 请求模板：headers 为 (name, value) 元组列表，_apply_cnnc_at 会把
# access-token 项替换为当前 AT（模板本体不被污染）
DETAIL_TEMPLATE = {
    "url": "https://detail.invalid/quasiStandardCommodityDocumentModel/detail",
    "headers": [("access-token", "PLACEHOLDER")],
}


def _detail_response_401():
    return {"ok": False, "http_status": 401, "payload": None, "error": "HTTP 401"}


def _detail_response_ok():
    return {
        "ok": True,
        "payload": {
            "rspCode": "0000",
            "data": {"referenceInfos": [{"referencePrice": "12.34"}]},
        },
        "error": "",
    }


# ---------------------------------------------------------------------- Case 13


class _FakeRecoveryProvider:
    """duck-type CnncAuthProvider：run_login_and_recover 只用 refresh/mutate_state。

    无 curl_files 属性的形态与旧 fake 兼容：后置条件核验对无该属性的
    provider 视为期望清单为空（全部同步），既有语义不破。
    """

    def __init__(self, refresh_result="", refresh_error=None):
        self.refresh_calls = []
        self.mutated_states = []
        self._refresh_result = refresh_result
        self._refresh_error = refresh_error

    def refresh(self, *args, **kwargs):
        self.refresh_calls.append((args, dict(kwargs)))
        if self._refresh_error is not None:
            raise self._refresh_error
        return self._refresh_result

    def mutate_state(self, mutator):
        state = {}
        mutator(state)
        self.mutated_states.append(state)


class _FakeCurlSyncProvider(_FakeRecoveryProvider):
    """带 curl_files 属性（与真 provider 同名 duck-type hook）的恢复 provider。"""

    def __init__(self, curl_files=(), refresh_result="", refresh_error=None):
        super().__init__(refresh_result=refresh_result, refresh_error=refresh_error)
        self.curl_files = tuple(curl_files)


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


def test_case13_login_and_recover_success_publishes_refreshed_token(
    tmp_path, capsys
):
    new_at = "AT-9-new-xyz999"
    provider = _FakeRecoveryProvider(refresh_result=new_at)
    result_path = tmp_path / "recovery_result.json"

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 0, provider=provider, result_path=result_path,
        lock_path=tmp_path / "login_recovery.lock",  # 绝不触碰真实 data/
    )

    assert rc == 0
    # 恰好刷新一次，且 force=True（人工登录显式重试，绕过 SSO 冷却）
    assert provider.refresh_calls == [((), {"force": True})]
    assert provider.mutated_states == []  # 成功路径不写失败状态
    out = capsys.readouterr().out
    assert "RECOVERY_OK" in out
    assert core.redact_secret(new_at) in out  # AT-9-ne***（前 7 字符 + ***）
    assert new_at not in out  # 全量凭证绝不打印
    # 无 curl_files 属性 → 期望清单为空 = 全同步（rc 仍为 0）
    artifact = _read_artifact(result_path)
    assert artifact["outcome"] == "RECOVERY_OK"
    assert artifact["curl_expected"] == 0
    assert artifact["curl_synced"] == 0


def test_case13_login_failure_records_login_recovery_failed(tmp_path, capsys):
    provider = _FakeRecoveryProvider(refresh_result=NEW_AT)
    result_path = tmp_path / "recovery_result.json"

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 1, provider=provider, result_path=result_path,
        lock_path=tmp_path / "login_recovery.lock",  # 绝不触碰真实 data/
    )

    assert rc == 1
    assert provider.refresh_calls == []  # login 失败绝不触发刷新
    assert len(provider.mutated_states) == 1
    state = provider.mutated_states[0]
    assert state["last_status"] == core.CnncAuthStatus.LOGIN_RECOVERY_FAILED
    assert state["last_error"] == "login exit=1"
    assert state["last_failure_at"]  # 失败时刻落盘（SSO 冷却断路器依据）
    err = capsys.readouterr().err
    assert "LOGIN_RECOVERY_FAILED" in err
    # login 失败同样写结果工件（outcome=LOGIN_RECOVERY_FAILED，未到刷新环节）
    artifact = _read_artifact(result_path)
    assert artifact["outcome"] == "LOGIN_RECOVERY_FAILED"
    assert artifact["curl_expected"] == 0
    assert artifact["curl_synced"] == 0
    assert artifact["at_redacted"] == ""  # 绝无 AT 形态的值


def test_case13_refresh_sso_failure_maps_to_exit_2(tmp_path, capsys):
    sso_error = core.CnncAuthError(
        core.CnncAuthStatus.SSO_LOGIN_REQUIRED, "SSO 会话已失效"
    )
    provider = _FakeRecoveryProvider(refresh_error=sso_error)
    result_path = tmp_path / "recovery_result.json"

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 0, provider=provider, result_path=result_path,
        lock_path=tmp_path / "login_recovery.lock",  # 绝不触碰真实 data/
    )

    assert rc == 2  # SSO 失效专用退出码：提示先运行 browser login
    assert provider.refresh_calls == [((), {"force": True})]
    assert provider.mutated_states == []  # 失败状态由 provider 落盘，这里不重复写
    err = capsys.readouterr().err
    assert "REFRESH_FAILED" in err
    assert core.CnncAuthStatus.SSO_LOGIN_REQUIRED in err
    # refresh 失败写结果工件（outcome=REFRESH_FAILED），绝不冒充登录失败
    artifact = _read_artifact(result_path)
    assert artifact["outcome"] == "REFRESH_FAILED"
    assert artifact["at_redacted"] == ""


# ------------------------------------------------- 2026-09-10 cURL 严格后置条件


def test_recovery_all_curl_synced_reports_recovery_ok(tmp_path, capsys):
    files = [
        _write_curl_file(tmp_path, name, NEW_AT)
        for name in ("latest_cnnc_query.curl", "detail_request.curl",
                     "cnnc_biaozhun.curl")
    ]
    provider = _FakeCurlSyncProvider(curl_files=files, refresh_result=NEW_AT)
    result_path = tmp_path / "recovery_result.json"

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 0, provider=provider, result_path=result_path,
        lock_path=tmp_path / "login_recovery.lock",  # 绝不触碰真实 data/
    )

    assert rc == 0
    assert provider.refresh_calls == [((), {"force": True})]  # 既有断言保持
    out = capsys.readouterr().out
    assert "RECOVERY_OK" in out and "curl_synced=3" in out
    assert NEW_AT not in out  # 全量凭证绝不打印
    artifact = _read_artifact(result_path)
    assert artifact["outcome"] == "RECOVERY_OK"
    assert artifact["curl_expected"] == 3
    assert artifact["curl_synced"] == 3
    assert artifact["curl_failed_files"] == []
    assert artifact["at_redacted"] == core.redact_secret(NEW_AT)
    assert artifact["completed_at"]  # UTC ISO 时间戳
    assert NEW_AT not in Path(result_path).read_text(encoding="utf-8")


def test_recovery_partial_curl_sync_failure_reports_file_names_only(
    tmp_path, capsys
):
    good_a = _write_curl_file(tmp_path, "latest_cnnc_query.curl", NEW_AT)
    stale = _write_curl_file(tmp_path, "detail_request.curl", OLD_AT)  # 不含新 AT
    good_b = _write_curl_file(tmp_path, "cnnc_biaozhun.curl", NEW_AT)
    provider = _FakeCurlSyncProvider(
        curl_files=(good_a, stale, good_b), refresh_result=NEW_AT
    )
    result_path = tmp_path / "recovery_result.json"

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 0, provider=provider, result_path=result_path,
        lock_path=tmp_path / "login_recovery.lock",  # 绝不触碰真实 data/
    )

    assert rc == 4  # 认证成功但 cURL 同步不完整（绝不冒充登录失败 rc=1）
    out = capsys.readouterr().out
    assert "AUTH_REFRESH_OK_CURL_SYNC_FAILED" in out
    assert "curl_synced=2/3" in out
    assert "failed=detail_request.curl" in out  # 只文件名
    assert NEW_AT not in out
    artifact = _read_artifact(result_path)
    assert artifact["outcome"] == "AUTH_REFRESH_OK_CURL_SYNC_FAILED"
    assert artifact["curl_expected"] == 3
    assert artifact["curl_synced"] == 2
    assert artifact["curl_failed_files"] == ["detail_request.curl"]
    assert NEW_AT not in Path(result_path).read_text(encoding="utf-8")


def test_recovery_missing_curl_file_counts_as_failed(tmp_path, capsys):
    present = _write_curl_file(tmp_path, "cnnc_biaozhun.curl", NEW_AT)
    missing = tmp_path / "detail_request.curl"  # 从未创建
    provider = _FakeCurlSyncProvider(
        curl_files=(present, missing), refresh_result=NEW_AT
    )
    result_path = tmp_path / "recovery_result.json"

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 0, provider=provider, result_path=result_path,
        lock_path=tmp_path / "login_recovery.lock",  # 绝不触碰真实 data/
    )

    assert rc == 4
    assert "curl_synced=1/2" in capsys.readouterr().out
    artifact = _read_artifact(result_path)
    assert artifact["curl_failed_files"] == ["detail_request.curl"]
    assert artifact["curl_synced"] == 1


def test_recovery_unparsable_curl_file_counts_as_failed(tmp_path, capsys):
    present = _write_curl_file(tmp_path, "cnnc_biaozhun.curl", NEW_AT)
    broken = tmp_path / "detail_request.curl"
    broken.write_text("not a curl command at all", encoding="utf-8")
    provider = _FakeCurlSyncProvider(
        curl_files=(present, broken), refresh_result=NEW_AT
    )

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 0, provider=provider,
        result_path=tmp_path / "recovery_result.json",
        lock_path=tmp_path / "login_recovery.lock",  # 绝不触碰真实 data/
    )

    assert rc == 4
    artifact = _read_artifact(tmp_path / "recovery_result.json")
    assert artifact["curl_failed_files"] == ["detail_request.curl"]
    assert artifact["curl_synced"] == 1


def test_recovery_without_curl_files_attr_means_no_expectations(tmp_path, capsys):
    """旧 duck-type fake（无 curl_files 属性）期望为空 = 全同步，rc=0 不破。"""
    provider = _FakeRecoveryProvider(refresh_result=NEW_AT)
    result_path = tmp_path / "recovery_result.json"

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 0, provider=provider, result_path=result_path,
        lock_path=tmp_path / "login_recovery.lock",  # 绝不触碰真实 data/
    )

    assert rc == 0
    assert "curl_synced=0" in capsys.readouterr().out
    artifact = _read_artifact(result_path)
    assert artifact["outcome"] == "RECOVERY_OK"
    assert artifact["curl_expected"] == 0


def test_artifact_write_failure_does_not_change_rc(tmp_path):
    """工件落盘 OSError 尽力而为：绝不影响退出码语义。"""
    provider = _FakeCurlSyncProvider(refresh_result=NEW_AT)

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 0, provider=provider,
        result_path=tmp_path,  # 目录路径 → 写入必然 OSError
        lock_path=tmp_path / "login_recovery.lock",  # 绝不触碰真实 data/
    )

    assert rc == 0


# --------------------------------------- 跨进程 single-flight 锁（Blocker 3）
# 2026-09-10：run_login_and_recover 全程持有 OS 级 ProcessLock（复用
# daily_audit_store，msvcrt/fcntl 字节锁，进程退出自动释放）。锁被他人持有
# → rc=5 零副作用；锁路径全部 tmp 注入，绝不触碰真实 data/。


def test_login_lock_held_by_other_returns_5_without_side_effects(tmp_path, capsys):
    """锁已被他人持有 → rc=5、login_runner 零调用、不写状态/工件、stderr 提示。"""
    from app.services.daily_audit_store import ProcessLock

    lock_path = tmp_path / "login_recovery.lock"
    holder = ProcessLock(lock_path).acquire(owner={"pid": 12345})
    try:
        runner_calls = []

        def runner():
            runner_calls.append(1)
            return 0

        provider = _FakeRecoveryProvider(refresh_result=NEW_AT)
        result_path = tmp_path / "recovery_result.json"

        rc = cnnc_auth_refresh.run_login_and_recover(
            login_runner=runner, provider=provider,
            result_path=result_path, lock_path=lock_path)

        assert rc == 5
        assert runner_calls == []  # 绝不调用 login_runner
        assert provider.refresh_calls == []  # 绝不触发刷新
        assert not result_path.exists()  # 绝不写结果工件
    finally:
        holder.release()
    assert "LOGIN_RECOVERY_ALREADY_RUNNING" in capsys.readouterr().err


@pytest.mark.parametrize("expected_rc", [0, 1, 2, 4])
def test_login_lock_active_during_run_and_released_after(tmp_path, expected_rc):
    """login 运行全程锁被持有（inspect active=True）；结束后 rc=0/1/2/4 各
    路径锁都已释放（try/finally 语义，无泄漏）。"""
    from app.services.daily_audit_store import ProcessLock

    lock_path = tmp_path / "login_recovery.lock"
    observed = {}

    def runner():
        observed["active_during"] = ProcessLock.inspect(lock_path)["active"]
        return 1 if expected_rc == 1 else 0

    if expected_rc == 2:
        provider = _FakeRecoveryProvider(refresh_error=core.CnncAuthError(
            core.CnncAuthStatus.SSO_LOGIN_REQUIRED, "SSO 失效（锁测试）"))
    elif expected_rc == 4:
        good = _write_curl_file(tmp_path, "latest_cnnc_query.curl", NEW_AT)
        bad = tmp_path / "detail_request.curl"
        bad.write_text("not a curl command", encoding="utf-8")
        provider = _FakeCurlSyncProvider(
            curl_files=(good, bad), refresh_result=NEW_AT)
    else:
        provider = _FakeCurlSyncProvider(refresh_result=NEW_AT)

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=runner, provider=provider,
        result_path=tmp_path / "recovery_result.json", lock_path=lock_path)

    assert rc == expected_rc
    assert observed["active_during"] is True
    assert ProcessLock.inspect(lock_path)["active"] is False


def test_login_lock_released_when_runner_raises(tmp_path):
    """login_runner 抛异常上抛后锁必须已释放（finally 兜底，不等进程退出）。"""
    from app.services.daily_audit_store import ProcessLock

    lock_path = tmp_path / "login_recovery.lock"

    def runner():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cnnc_auth_refresh.run_login_and_recover(
            login_runner=runner, provider=_FakeRecoveryProvider(),
            result_path=tmp_path / "recovery_result.json",
            lock_path=lock_path)

    assert ProcessLock.inspect(lock_path)["active"] is False


# ----------------------------------------------------------------- Case 15/16


class _FakeDetailProvider:
    """duck-type provider：只实现 _detail_worker 用到的三个方法。"""

    def __init__(self):
        self.refresh_calls = []

    def get_access_token(self):
        return OLD_AT

    def refresh(self, *args, **kwargs):
        self.refresh_calls.append(
            {"kwargs": dict(kwargs), "monotonic_at_refresh": time.monotonic()}
        )
        return NEW_AT

    def get_status(self):
        return {"status": core.CnncAuthStatus.REFRESHED}


def _make_worker(monkeypatch, responder):
    detail_calls = []

    def fake_call_detail_api(config, document_id, category_id, timeout):
        detail_calls.append(
            {"config": dict(config), "document_id": document_id}
        )
        return responder(len(detail_calls))

    monkeypatch.setattr(
        "app.services.incremental_audit_runtime.call_detail_api",
        fake_call_detail_api,
    )
    provider = _FakeDetailProvider()
    worker = _detail_worker(
        dict(DETAIL_TEMPLATE), request_interval_seconds=0, cnnc_provider=provider
    )
    return worker, provider, detail_calls


def test_case15_detail_401_refreshes_once_then_retry_succeeds(monkeypatch):
    worker, provider, detail_calls = _make_worker(
        monkeypatch,
        lambda n: _detail_response_401() if n == 1 else _detail_response_ok(),
    )

    result = worker({"vCommodityId": "v-1", "fourCategoryId": "cat-1"}, 10)

    assert result["customerPriceStatus"] == "FOUND_SINGLE_REFERENCE"
    assert str(result["customerPrice"]) == "12.34"
    # 恰好两次请求、一次刷新；重试请求携带刷新后的新 AT
    assert len(detail_calls) == 2
    assert detail_calls[0]["config"]["headers"] == [("access-token", OLD_AT)]
    assert detail_calls[1]["config"]["headers"] == [("access-token", NEW_AT)]
    assert len(provider.refresh_calls) == 1
    refresh = provider.refresh_calls[0]
    assert refresh["kwargs"]["stale_access_token"] == OLD_AT
    # deadline_monotonic == started + timeout_seconds（monotonic 绝对截止时刻）
    remaining = refresh["kwargs"]["deadline_monotonic"] - refresh["monotonic_at_refresh"]
    assert 0 < remaining <= 10


def test_case16_detail_401_twice_raises_without_third_request(monkeypatch):
    worker, provider, detail_calls = _make_worker(
        monkeypatch, lambda _n: _detail_response_401()
    )

    with pytest.raises(RuntimeError) as excinfo:
        worker({"vCommodityId": "v-2", "fourCategoryId": "cat-2"}, 10)

    # 第二次结果为准：失败上抛，且禁止第三次请求（最多刷新一次）
    assert "刷新重试后仍被拒" in str(excinfo.value)
    assert len(detail_calls) == 2
    assert len(provider.refresh_calls) == 1
