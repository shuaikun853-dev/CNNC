# -*- coding: utf-8 -*-
"""CNNC 认证恢复链路 hermetic 测试（Case 13, 15, 16）。

- Case 13 run_login_and_recover：login 成功→force 刷新→发布；login 失败/
  refresh SSO 失败的退出码与失败落盘
- Case 15 detail 请求 HTTP 401 → refresh 一次（携带 deadline）→ 重试成功
- Case 16 detail 请求连续两次 401 → 失败上抛，禁止第三次请求

全部依赖（login_runner/provider/call_detail_api）注入，无真实网络、无浏览器、
无子进程。provider 以 duck-type fake 注入，只实现被测路径用到的 seam。
"""

from __future__ import annotations

import time

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
    """duck-type CnncAuthProvider：run_login_and_recover 只用 refresh/mutate_state。"""

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


def test_case13_login_and_recover_success_publishes_refreshed_token(capsys):
    new_at = "AT-9-new-xyz999"
    provider = _FakeRecoveryProvider(refresh_result=new_at)

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 0, provider=provider
    )

    assert rc == 0
    # 恰好刷新一次，且 force=True（人工登录显式重试，绕过 SSO 冷却）
    assert provider.refresh_calls == [((), {"force": True})]
    assert provider.mutated_states == []  # 成功路径不写失败状态
    out = capsys.readouterr().out
    assert "RECOVERY_OK" in out
    assert core.redact_secret(new_at) in out  # AT-9-ne***（前 7 字符 + ***）
    assert new_at not in out  # 全量凭证绝不打印


def test_case13_login_failure_records_login_recovery_failed(capsys):
    provider = _FakeRecoveryProvider(refresh_result=NEW_AT)

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 1, provider=provider
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


def test_case13_refresh_sso_failure_maps_to_exit_2(capsys):
    sso_error = core.CnncAuthError(
        core.CnncAuthStatus.SSO_LOGIN_REQUIRED, "SSO 会话已失效"
    )
    provider = _FakeRecoveryProvider(refresh_error=sso_error)

    rc = cnnc_auth_refresh.run_login_and_recover(
        login_runner=lambda: 0, provider=provider
    )

    assert rc == 2  # SSO 失效专用退出码：提示先运行 browser login
    assert provider.refresh_calls == [((), {"force": True})]
    assert provider.mutated_states == []  # 失败状态由 provider 落盘，这里不重复写
    err = capsys.readouterr().err
    assert "REFRESH_FAILED" in err
    assert core.CnncAuthStatus.SSO_LOGIN_REQUIRED in err


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
