# -*- coding: utf-8 -*-
"""CnncAuthProvider 自动续期（autorenew）hermetic 测试（Case 1-8）。

全部依赖（harvest/exchange/verify/opener/state/curl 路径）注入，无真实
网络、无浏览器、无子进程。覆盖：

- Case 1  AT 有效时不刷新
- Case 2  AT 失效自动恢复（刷新 + 重试一次）
- Case 3  并发只刷一次（锁内双检 + 真线程串行化）
- Case 4  SSO 失效断路器（不无限重试）+ force 绕过冷却
- Case 5  exchange 失败不覆盖旧凭证
- Case 6  verify 失败不 publish
- Case 7  cURL 第 2 个文件替换失败不半更新（f1 回滚、f2 未变、state 已发布）
- Case 8  已运行的 Adapter 拿到新 AT（同一 provider 实例、不重建）
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.cnnc_auth_core as cnnc_auth_core
from app.services.cnnc_saas_snapshot_adapter import CnncSaasSnapshotAdapter
from scripts.cnnc_auth_core import CnncAuthError, CnncAuthProvider, CnncAuthStatus

OLD_AT = "AT-1-old"
NEW_AT = "AT-2-new"
CUSTOMER_QUERY_URL = (
    "https://one.cnncecp.com/emp/onecode/improxy/iio2/"
    "quasiStandardCommodityDocumentModel/query"
)
AUTH_FAILURE_PAYLOAD = {"rspCode": "3333", "rspDesc": "OPS认证失败"}


# --------------------------------------------------------------------- fakes


class _FakeHeaders:
    def get_content_charset(self):
        return "utf-8"


class _FakeResponse:
    headers = _FakeHeaders()

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _NoNetworkOpener:
    """守门 opener：provider 默认 exchange/verify 被注入后不应触网。"""

    def open(self, *_args, **_kwargs):
        raise AssertionError("tests must not perform real network I/O")


class _FakeOpener:
    """按序返回预设 payload / 异常，并记录收到的请求。"""

    def __init__(self, events):
        self.events = list(events)
        self.requests = []

    def open(self, request, timeout=60):
        self.requests.append(request)
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return _FakeResponse(event)


# -------------------------------------------------------------------- helpers


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _auth_paths(tmp_path):
    auth_dir = tmp_path / "auth"
    return auth_dir / "cnnc_auth_state.json", auth_dir / "cnnc_auth_ledger.jsonl"


def _write_state(path: Path, **fields) -> None:
    payload = {"schema": 1}
    payload.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_ledger_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _make_provider(tmp_path, *, harvest=None, exchange=None, verify=None, opener=None,
                   curl_files=()):
    calls = {"harvest": 0, "exchange": 0, "verify": 0}

    def default_harvest():
        calls["harvest"] += 1
        return "OC-X"

    def default_exchange(oc, timeout=30.0):
        calls["exchange"] += 1
        return NEW_AT

    def default_verify(at, timeout=30.0):
        calls["verify"] += 1
        return True

    state_path, ledger_path = _auth_paths(tmp_path)
    provider = CnncAuthProvider(
        state_path=state_path,
        ledger_path=ledger_path,
        curl_files=tuple(curl_files),
        harvest_func=harvest or default_harvest,
        exchange_func=exchange or default_exchange,
        verify_func=verify or default_verify,
        opener=opener if opener is not None else _NoNetworkOpener(),
    )
    return provider, calls


def _query_factory():
    def factory():
        body = json.dumps({"sassCheckStatuss": ["0"]}).encode("utf-8")
        return urllib.request.Request(CUSTOMER_QUERY_URL, data=body, method="POST")

    return factory


def _curl_text(at: str) -> str:
    return (
        'curl "https://one.cnncecp.com/emp/onecode/improxy/iio2/'
        'quasiStandardCommodityDocumentModel/query"'
        ' -H "Content-Type: application/json"'
        ' -H "mk-request: 41"'
        f' -H "token: {at}"'
        " --data-raw '{\"sassCheckStatuss\":[\"0\"]}'"
    )


def _saas_config():
    return {
        "url": "https://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryAll",
        "method": "POST",
        "headers": [],
        "cookie": "",
        "data": json.dumps({"tenantId": "188605", "commodityPoolId": "222"}),
    }


def _customer_config():
    return {
        "url": CUSTOMER_QUERY_URL,
        "method": "POST",
        "headers": [],
        "cookie": "",
        "data": json.dumps({"sassCheckStatuss": ["0"]}),
    }


def _seed_valid_state(state_path: Path, at: str = OLD_AT,
                      status: str = CnncAuthStatus.OK) -> None:
    # last_refresh_at 必须是最近时间：SSO 冷却断路器按它起算
    _write_state(
        state_path,
        access_token=at,
        last_status=status,
        last_refresh_at=_now_iso(),
    )


# ---------------------------------------------------------------------- Case 1


def test_case1_valid_at_business_request_skips_refresh(tmp_path):
    state_path, _ledger_path = _auth_paths(tmp_path)
    _seed_valid_state(state_path, at="AT-111-aaa")
    opener = _FakeOpener([{"rspCode": "0000", "rows": []}])
    provider, calls = _make_provider(tmp_path, opener=opener)

    payload = provider.request_json_cnnc(
        _query_factory(), read_only=True, opener=opener
    )

    assert payload == {"rspCode": "0000", "rows": []}
    assert calls["harvest"] == 0
    assert len(opener.requests) == 1
    assert opener.requests[0].headers["access-token"] == "AT-111-aaa"


# ---------------------------------------------------------------------- Case 2


def test_case2_auth_failure_triggers_refresh_and_retry_once(tmp_path):
    state_path, ledger_path = _auth_paths(tmp_path)
    _seed_valid_state(state_path, at=OLD_AT)
    opener = _FakeOpener(
        [
            dict(AUTH_FAILURE_PAYLOAD),
            {"rspCode": "0000", "rows": [{"documentId": "d1"}]},
        ]
    )
    provider, calls = _make_provider(tmp_path, opener=opener)

    payload = provider.request_json_cnnc(
        _query_factory(), read_only=True, opener=opener
    )

    assert payload["rspCode"] == "0000"
    assert payload["rows"] == [{"documentId": "d1"}]
    assert calls == {"harvest": 1, "exchange": 1, "verify": 1}
    state = _read_state(state_path)
    assert state["access_token"] == NEW_AT
    assert state["last_status"] == CnncAuthStatus.REFRESHED
    assert opener.requests[0].headers["access-token"] == OLD_AT
    assert opener.requests[1].headers["access-token"] == NEW_AT
    events = [event["event"] for event in _read_ledger_events(ledger_path)]
    assert "CNNC_AUTH_REFRESHED" in events


# ---------------------------------------------------------------------- Case 3


def test_case3a_refresh_double_check_reuses_fresh_token_without_harvest(tmp_path):
    state_path, _ledger_path = _auth_paths(tmp_path)
    _seed_valid_state(state_path, at=NEW_AT, status=CnncAuthStatus.REFRESHED)
    provider, calls = _make_provider(tmp_path)

    at = provider.refresh(stale_access_token=OLD_AT)

    assert at == NEW_AT
    assert calls["harvest"] == 0


def test_case3b_concurrent_refresh_serializes_and_harvests_once(tmp_path):
    state_path, _ledger_path = _auth_paths(tmp_path)
    entered_harvest = threading.Event()
    release = threading.Event()
    harvest_calls = []

    def slow_harvest():
        harvest_calls.append("harvest")
        entered_harvest.set()
        assert release.wait(timeout=10), "test deadlocked waiting for release"
        return "OC-X"

    provider, _calls = _make_provider(tmp_path, harvest=slow_harvest)
    _seed_valid_state(state_path, at=OLD_AT)

    results = {}

    def worker(name, **kwargs):
        try:
            results[name] = provider.refresh(**kwargs)
        except Exception as exc:  # noqa: BLE001 — 捕获后交给断言显式失败
            results[name] = exc

    # 线程 A：先进入 refresh 并持有刷新锁（阻塞在慢 harvest 上）
    thread_a = threading.Thread(target=worker, args=("a",))
    thread_a.start()
    assert entered_harvest.wait(timeout=10), "refresh thread never reached harvest"

    # 线程 B：带旧 AT 并发刷新，阻塞在刷新锁上排队
    thread_b = threading.Thread(
        target=worker, args=("b",), kwargs={"stale_access_token": OLD_AT}
    )
    thread_b.start()
    time.sleep(0.2)  # 给 B 时间进入等锁状态（不依赖该时长的正确性）
    assert harvest_calls == ["harvest"]  # B 排队期间绝不触发第二次 harvest

    # 放行 A → 发布新 AT → B 获锁后锁内双检命中，直接复用新 AT
    release.set()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()

    assert results["a"] == NEW_AT
    assert results["b"] == NEW_AT
    assert harvest_calls == ["harvest"]
    state = _read_state(state_path)
    assert state["access_token"] == NEW_AT


# ---------------------------------------------------------------------- Case 4


def test_case4_sso_failure_breaker_and_force_bypass(tmp_path):
    state_path, _ledger_path = _auth_paths(tmp_path)
    _seed_valid_state(state_path, at=OLD_AT)
    harvest_calls = []
    mode = {"fail": True}

    def harvest():
        harvest_calls.append("harvest")
        if mode["fail"]:
            raise CnncAuthError(CnncAuthStatus.SSO_LOGIN_REQUIRED, "SSO 会话已失效")
        return "OC-X"

    provider, _calls = _make_provider(tmp_path, harvest=harvest)
    opener = _FakeOpener([dict(AUTH_FAILURE_PAYLOAD)])

    with pytest.raises(CnncAuthError) as excinfo:
        provider.request_json_cnnc(_query_factory(), read_only=True, opener=opener)

    assert excinfo.value.status == CnncAuthStatus.SSO_LOGIN_REQUIRED
    assert len(harvest_calls) == 1  # 只 harvest 一次，无循环重试
    state = _read_state(state_path)
    assert state["last_status"] == CnncAuthStatus.SSO_LOGIN_REQUIRED

    # 冷却期内再次刷新：断路器直接拦截，不再触发 harvest
    with pytest.raises(CnncAuthError) as cooldown_exc:
        provider.refresh()
    assert cooldown_exc.value.status == CnncAuthStatus.SSO_LOGIN_REQUIRED
    assert len(harvest_calls) == 1

    # force=True 绕过冷却（人工显式重试）
    mode["fail"] = False
    at = provider.refresh(force=True)

    assert at == NEW_AT
    state = _read_state(state_path)
    assert state["access_token"] == NEW_AT
    assert state["last_status"] == CnncAuthStatus.REFRESHED


# ---------------------------------------------------------------------- Case 5


def test_case5_exchange_failure_preserves_old_credentials(tmp_path):
    state_path, ledger_path = _auth_paths(tmp_path)
    curl = tmp_path / "curl" / "latest_cnnc_query.curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    curl.write_text(_curl_text(OLD_AT), encoding="utf-8")
    _seed_valid_state(state_path, at=OLD_AT)

    def exchange(oc, timeout=30.0):
        raise CnncAuthError(CnncAuthStatus.OC_EXCHANGE_FAILED, "OC 换 AT 失败")

    provider, calls = _make_provider(tmp_path, exchange=exchange, curl_files=(curl,))

    with pytest.raises(CnncAuthError) as excinfo:
        provider.refresh()

    assert excinfo.value.status == CnncAuthStatus.OC_EXCHANGE_FAILED
    assert calls["harvest"] == 1
    state = _read_state(state_path)
    assert state["access_token"] == OLD_AT
    curl_now = curl.read_text(encoding="utf-8")
    assert "AT-1-old" in curl_now
    assert "AT-2-new" not in curl_now


# ---------------------------------------------------------------------- Case 6


def test_case6_verify_failure_does_not_publish(tmp_path):
    state_path, _ledger_path = _auth_paths(tmp_path)
    curl = tmp_path / "curl" / "latest_cnnc_query.curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    curl.write_text(_curl_text(OLD_AT), encoding="utf-8")
    _seed_valid_state(state_path, at=OLD_AT)

    def verify(at, timeout=30.0):
        return False

    provider, calls = _make_provider(tmp_path, verify=verify, curl_files=(curl,))

    with pytest.raises(CnncAuthError) as excinfo:
        provider.refresh()

    assert excinfo.value.status == CnncAuthStatus.AT_VERIFY_FAILED
    assert calls["harvest"] == 1
    state = _read_state(state_path)
    assert state["access_token"] == OLD_AT
    assert state["last_status"] == CnncAuthStatus.AT_VERIFY_FAILED
    curl_now = curl.read_text(encoding="utf-8")
    assert "AT-1-old" in curl_now
    assert "AT-2-new" not in curl_now


# ---------------------------------------------------------------------- Case 7


def test_case7_second_curl_replace_failure_no_partial_update(tmp_path, monkeypatch):
    state_path, ledger_path = _auth_paths(tmp_path)
    curl_dir = tmp_path / "curl"
    f1 = curl_dir / "latest_cnnc_query.curl"
    f2 = curl_dir / "detail_request.curl"
    curl_dir.mkdir(parents=True, exist_ok=True)
    f1.write_text(_curl_text(OLD_AT), encoding="utf-8")
    f2.write_text(_curl_text(OLD_AT), encoding="utf-8")
    _seed_valid_state(state_path, at=OLD_AT)
    provider, _calls = _make_provider(tmp_path, curl_files=(f1, f2))

    real_replace = os.replace

    def flaky_replace(src, dst, *args, **kwargs):
        if os.fspath(dst).endswith(f2.name):
            raise OSError("simulated second-file replace failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(cnnc_auth_core.os, "replace", flaky_replace)

    # sync_curl_files 本身必须抛 CURL_SYNC_FAILED，且 f1 回滚原文、f2 未变
    with pytest.raises(CnncAuthError) as excinfo:
        provider.sync_curl_files(NEW_AT)
    assert excinfo.value.status == CnncAuthStatus.CURL_SYNC_FAILED
    assert f1.read_text(encoding="utf-8") == _curl_text(OLD_AT)
    assert f2.read_text(encoding="utf-8") == _curl_text(OLD_AT)
    assert list(curl_dir.glob(f"{f1.name}.bak_*"))  # 替换前已写 bak，允许存在

    # refresh 全程：state 先发布、curl 后同步；同步失败不回滚 state（设计如此）
    at = provider.refresh()

    assert at == NEW_AT
    state = _read_state(state_path)
    # 设计如此：state 已发布、运行时已可用，cURL 同步失败不回滚 state
    assert state["access_token"] == NEW_AT
    assert state["last_status"] == CnncAuthStatus.REFRESHED
    assert f1.read_text(encoding="utf-8") == _curl_text(OLD_AT)
    assert f2.read_text(encoding="utf-8") == _curl_text(OLD_AT)
    events = [event["event"] for event in _read_ledger_events(ledger_path)]
    assert "CNNC_AUTH_REFRESHED" in events


# ---------------------------------------------------------------------- Case 8


def test_case8_running_adapter_picks_up_refreshed_token_without_restart(tmp_path):
    state_path, _ledger_path = _auth_paths(tmp_path)
    _seed_valid_state(state_path, at=OLD_AT)
    customer_rows = [{"documentId": "c1"}]
    opener = _FakeOpener(
        [
            {"rspCode": "0000", "count": 0, "rows": []},  # saas page 1
            dict(AUTH_FAILURE_PAYLOAD),  # customer 第 1 次：AT 已失效
            {"rspCode": "0000", "count": 1, "rows": customer_rows},  # 第 2 次：新 AT
        ]
    )
    provider, calls = _make_provider(tmp_path, opener=opener)
    # 同一 Adapter / 同一 provider 实例，全程不重建
    adapter = CnncSaasSnapshotAdapter(
        _saas_config(),
        _customer_config(),
        auth_manager=None,
        cnnc_provider=provider,
        opener=opener,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    snapshot = adapter.fetch(source_label="autorenew-case8")

    assert snapshot.saas_count == 0
    assert snapshot.customer_count == len(customer_rows)
    assert [
        (page.side, page.page, page.row_count, page.count)
        for page in snapshot.page_evidence
    ] == [
        ("saas", 1, 0, 0),
        ("customer", 1, 1, 1),
    ]
    assert calls["harvest"] == 1
    customer_requests = [
        request
        for request in opener.requests
        if "quasiStandardCommodityDocumentModel" in request.full_url
    ]
    assert len(customer_requests) == 2
    assert customer_requests[0].headers["access-token"] == OLD_AT
    assert customer_requests[1].headers["access-token"] == NEW_AT
