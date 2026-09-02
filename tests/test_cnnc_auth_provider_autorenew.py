# -*- coding: utf-8 -*-
"""CnncAuthProvider 自动续期（autorenew）hermetic 测试（Case 1-8, 11, 12, 14）。

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
- Case 11 SSO 冷却按真实失败时刻 last_failure_at 起算（不依赖 last_refresh_at）
- Case 12 verify 通过路径与并发 refresh 的 lost-update 防护
- Case 14 refresh deadline 契约（中途耗尽不 exchange；入口已过不 harvest）
- Case 19 harvest 双通道：tuple (OC, browser_at) 跳过外部交换发布浏览器 AT
  （ledger exchange=browser）；str 返回旧路径仍走外部交换（exchange=http）；
  _default_harvest 解析 stdout 的 OC=/AT= 两行

注入契约：provider 恒以 ``harvest_func(timeout_seconds=...)`` 关键字调用，
所有 fake harvest 均须接受该关键字参数；返回 ``str``（仅 OC）或
``tuple[str, str | None]``（双通道）均可。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
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
                   curl_files=(), monotonic=None):
    calls = {"harvest": 0, "exchange": 0, "verify": 0}

    def default_harvest(timeout_seconds=180):
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
        monotonic=monotonic if monotonic is not None else time.monotonic,
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
    # last_refresh_at 必须是最近时间；last_failure_at 与之同刻（旧版断路器按
    # last_refresh_at 起算、新版按 last_failure_at 起算，两种语义下预置均合法）
    now = _now_iso()
    _write_state(
        state_path,
        access_token=at,
        last_status=status,
        last_refresh_at=now,
        last_failure_at=now,
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

    def slow_harvest(timeout_seconds=180):
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

    def harvest(timeout_seconds=180):
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
    provider, calls = _make_provider(tmp_path, opener=opener, monotonic=lambda: 0.0)
    # 同一 Adapter / 同一 provider 实例，全程不重建；Adapter 与 provider 必须
    # 共用同一 monotonic 时钟基座——Adapter 注入的 deadline_monotonic 是
    # monotonic 绝对时刻，provider 也按同一时钟换算剩余预算
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


# --------------------------------------------------------------------- Case 11


def test_case11_sso_cooldown_starts_from_real_failure_time_not_last_refresh(tmp_path):
    """SSO 冷却断路器必须按真实失败时刻 last_failure_at 起算。

    回归 Finding 1：旧实现按 last_refresh_at 起算——AT 已 24 小时未刷新时
    冷却恒判过期，断路器失效，每个 tick 都会弹浏览器。新实现：首次 SSO
    失败写入 last_failure_at；冷却期内再刷直接拦截、不 harvest；把
    last_failure_at 拨回 1000 秒前（>900 秒冷却窗口）后恢复实际刷新。
    """
    state_path, _ledger_path = _auth_paths(tmp_path)
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    # 预置：AT 很旧 + 无 last_failure_at（旧版 state 过渡形态）
    _write_state(
        state_path,
        access_token=OLD_AT,
        last_status=CnncAuthStatus.OK,
        last_refresh_at=day_ago,
    )
    harvest_calls = []

    def sso_harvest(timeout_seconds=180):
        harvest_calls.append("harvest")
        raise CnncAuthError(CnncAuthStatus.SSO_LOGIN_REQUIRED, "SSO 会话已失效")

    provider, _calls = _make_provider(tmp_path, harvest=sso_harvest)

    # 第一次 refresh：实际 harvest，抛 SSO 并落真实失败时刻
    with pytest.raises(CnncAuthError) as first_exc:
        provider.refresh()
    assert first_exc.value.status == CnncAuthStatus.SSO_LOGIN_REQUIRED
    assert len(harvest_calls) == 1
    state = _read_state(state_path)
    assert state["last_status"] == CnncAuthStatus.SSO_LOGIN_REQUIRED
    failure_epoch = datetime.fromisoformat(state["last_failure_at"]).timestamp()
    assert abs(time.time() - failure_epoch) < 60  # ≈ now

    # 立即第二次 refresh：断路器按 last_failure_at 拦截（last_refresh_at 仍是
    # 24 小时前——证明拦截不再依赖它），不触发 harvest
    with pytest.raises(CnncAuthError) as cooldown_exc:
        provider.refresh()
    assert cooldown_exc.value.status == CnncAuthStatus.SSO_LOGIN_REQUIRED
    assert len(harvest_calls) == 1

    # last_failure_at 拨回 1000 秒前（>900 秒冷却窗口）→ 冷却过期，恢复实际刷新
    state["last_failure_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1000)
    ).isoformat()
    _write_state(state_path, **state)
    with pytest.raises(CnncAuthError) as after_cooldown_exc:
        provider.refresh()
    assert after_cooldown_exc.value.status == CnncAuthStatus.SSO_LOGIN_REQUIRED
    assert len(harvest_calls) == 2


# --------------------------------------------------------------------- Case 12


def test_case12_verify_ok_after_concurrent_refresh_never_overwrites_new_at(tmp_path):
    """verify 通过路径与并发 refresh 交错：绝不允许旧 AT 覆盖新 AT。

    交错序列（threading.Event 精确控制）：
      线程 A: ensure_valid 读到 AT-1-old → 阻塞在 verify
      主线程: refresh(force=True) 发布 AT-2-new
      放行 A: verify → True → _mark_verify_ok
    旧实现语义下本测试失败：_mark_verify_ok 在锁外做 state 读-改-写（RMW），
    会把 verify 前读到的旧 state（含 AT-1-old）整体写回，覆盖刚发布的
    AT-2-new（lost update）。现实现 mutator 只写 last_verify_at/last_status
    元数据字段，且经 mutate_state 持锁重读最新 state，绝不触碰 access_token。
    """
    state_path, _ledger_path = _auth_paths(tmp_path)
    _seed_valid_state(state_path, at=OLD_AT)

    verify_entered = threading.Event()
    release_verify = threading.Event()
    verify_calls = []

    def blocking_verify(at, timeout=30.0):
        verify_calls.append(at)
        if len(verify_calls) == 1:
            # 第一次 verify 是线程 A 对旧 AT 的验证：阻塞到 refresh 发布完成
            verify_entered.set()
            assert release_verify.wait(timeout=10), (
                "test deadlocked waiting for verify release"
            )
        return True  # 第二次是主线程 refresh 内对新 AT 的验证：立即通过

    provider, _calls = _make_provider(tmp_path, verify=blocking_verify)

    results = {}

    def ensure_worker():
        try:
            results["ensure"] = provider.ensure_valid()
        except Exception as exc:  # noqa: BLE001 — 捕获后交给断言显式失败
            results["ensure"] = exc

    # 线程 A：先读旧 AT 并阻塞在 verify 上（ensure_valid 的 verify 在锁外）
    thread_a = threading.Thread(target=ensure_worker)
    thread_a.start()
    assert verify_entered.wait(timeout=10), "ensure_valid never reached verify"

    # 主线程：并发强制刷新，发布 AT-2-new（与线程 A 共用同一路径锁）
    published = provider.refresh(force=True)
    assert published == NEW_AT

    # 放行线程 A 的 verify → 走 _mark_verify_ok 路径
    release_verify.set()
    thread_a.join(timeout=10)
    assert not thread_a.is_alive()

    assert results["ensure"] is None
    assert verify_calls == [OLD_AT, NEW_AT]  # A 验旧 AT；refresh 验新 AT
    state = _read_state(state_path)
    # 关键断言：verify 通过只更新元数据，绝不用旧值覆盖并发发布的新 AT
    assert state["access_token"] == NEW_AT
    assert state["last_verify_at"]
    assert state["last_status"] == CnncAuthStatus.OK


# --------------------------------------------------------------------- Case 14


def test_case14_refresh_deadline_exhausted_midway_skips_exchange(tmp_path):
    """harvest 返回时预算已尽 → 抛 DEADLINE_EXCEEDED，exchange/verify 不执行。

    fake harvest 尊重调用方按 deadline 截断后的 timeout_seconds 预算（真实
    subprocess 语义的近似）：deadline = now+0.1s，harvest 只拿到 0.1s 预算，
    返回后 provider 复查 deadline 即刻失败，全程 <0.5s 量级。
    """
    state_path, _ledger_path = _auth_paths(tmp_path)
    _seed_valid_state(state_path, at=OLD_AT)

    def slow_harvest(timeout_seconds=180):
        time.sleep(min(0.6, timeout_seconds))
        return "OC-X"

    provider, calls = _make_provider(tmp_path, harvest=slow_harvest)

    started = time.monotonic()
    with pytest.raises(CnncAuthError) as excinfo:
        provider.refresh(deadline_monotonic=started + 0.1)
    elapsed = time.monotonic() - started

    assert excinfo.value.status == CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED
    assert calls["exchange"] == 0
    assert calls["verify"] == 0
    assert elapsed < 0.5  # 快速失败，不做无谓的换 AT/验证
    state = _read_state(state_path)
    assert state["access_token"] == OLD_AT  # 失败不发布
    assert state["last_status"] == CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED


def test_case14_refresh_deadline_already_expired_raises_before_any_action(tmp_path):
    """deadline 已过 → 入口立即抛，不 harvest、不写任何 state。"""
    provider, calls = _make_provider(tmp_path)

    with pytest.raises(CnncAuthError) as excinfo:
        provider.refresh(deadline_monotonic=time.monotonic() - 0.001)

    assert excinfo.value.status == CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED
    assert calls["harvest"] == 0
    state_path, _ledger_path = _auth_paths(tmp_path)
    assert not state_path.exists()  # 未执行任何刷新动作


# --------------------------------------------------------------------- Case 19


def test_case19_tuple_harvest_skips_http_exchange_and_publishes_browser_at(tmp_path):
    """harvest 返回 (OC, browser_at) 双通道：跳过外部交换，直接发布浏览器 AT。

    2026-09-02 实测：午后 WAF 对裸 Python 请求（外部交换）返回 502，浏览器内
    同源 fetch 返回 200/success:true。harvest 浏览器内完成 OC→AT 交换并把 AT
    随 OC 一并带回；provider 检测到合法 browser_at 时不得再调 exchange
    （裸调用会被 WAF 打挂），验证通过后直接发布，ledger 记 exchange=browser。
    """
    state_path, ledger_path = _auth_paths(tmp_path)
    _seed_valid_state(state_path, at=OLD_AT)
    browser_at = "AT-20260902-browser01"
    assert cnnc_auth_core.AT_RE.fullmatch(browser_at)  # 用例前提：符合 AT 形态

    calls = {"exchange": 0}

    def exchange(oc, timeout=30.0):
        calls["exchange"] += 1
        return NEW_AT

    def tuple_harvest(timeout_seconds=180):
        return "OC-X", browser_at

    provider, _calls = _make_provider(tmp_path, harvest=tuple_harvest,
                                      exchange=exchange)

    at = provider.refresh()

    assert at == browser_at
    assert calls["exchange"] == 0  # 浏览器内交换成功，外部裸交换不得被调用
    state = _read_state(state_path)
    assert state["access_token"] == browser_at
    assert state["last_status"] == CnncAuthStatus.REFRESHED
    refreshed = [
        event
        for event in _read_ledger_events(ledger_path)
        if event["event"] == "CNNC_AUTH_REFRESHED"
    ]
    assert refreshed and refreshed[-1]["exchange"] == "browser"


def test_case19_str_harvest_legacy_path_still_uses_http_exchange(tmp_path):
    """兼容路径：仅返回 OC 字符串的注入实现仍走外部交换，ledger 记 exchange=http。"""
    state_path, ledger_path = _auth_paths(tmp_path)
    _seed_valid_state(state_path, at=OLD_AT)
    provider, calls = _make_provider(tmp_path)

    at = provider.refresh()

    assert at == NEW_AT
    assert calls["exchange"] == 1
    refreshed = [
        event
        for event in _read_ledger_events(ledger_path)
        if event["event"] == "CNNC_AUTH_REFRESHED"
    ]
    assert refreshed and refreshed[-1]["exchange"] == "http"


def test_case19_default_harvest_parses_oc_and_at_lines(tmp_path, monkeypatch):
    """_default_harvest 双通道解析：stdout 带 AT= 行 → (oc, at)；缺失 → (oc, None)。"""
    state_path, ledger_path = _auth_paths(tmp_path)
    provider = CnncAuthProvider(  # 不注入 harvest_func → 默认 _default_harvest
        state_path=state_path,
        ledger_path=ledger_path,
        curl_files=(),
        exchange_func=lambda oc, timeout=30.0: NEW_AT,
        verify_func=lambda at, timeout=30.0: True,
        opener=_NoNetworkOpener(),
    )

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="harvest 临时 Profile：...\nOC=OC-1-x\nAT=AT-2-y\n",
            stderr="",
        )

    monkeypatch.setattr(cnnc_auth_core.subprocess, "run", fake_run)
    assert provider._default_harvest(timeout_seconds=30.0) == ("OC-1-x", "AT-2-y")

    def fake_run_oc_only(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="OC=OC-1-x\n", stderr=""
        )

    monkeypatch.setattr(cnnc_auth_core.subprocess, "run", fake_run_oc_only)
    assert provider._default_harvest(timeout_seconds=30.0) == ("OC-1-x", None)
