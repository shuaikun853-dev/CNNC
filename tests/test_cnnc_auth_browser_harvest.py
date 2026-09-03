# -*- coding: utf-8 -*-
"""scripts/cnnc_auth_browser.py 测试（Case 9-10, 17-18）。

- Case 9  capture_oc_from_response：Location 头 OC 捕获规则（无 ctx.route）
- Case 10 select_session_cookies：只保留白名单会话 cookie，不注陈旧 K5
- Case 17 harvest 用一次性临时 Profile（Popen argv 断言 + 清理行为 + 源码断言）
- Case 18 save_session_state 白名单落盘：不泄漏 WAF cookie、原子覆盖
- Case 20 cmd_harvest 浏览器内 OC→AT 交换存在（源码级：同源 fetch
  anon/account/login + 追加打印 AT= 行）
- Case 21 cmd_login 一次性临时 Profile（行为级：传给 _launch_chrome_cdp 的
  user_data_dir 非 None 非 PROFILE_DIR、返回后 rmtree；失败路径 finally 仍
  rmtree；源码级：cmd_login 无 PROFILE_DIR、_launch_chrome_cdp 无回退分支、
  user_data_dir 必填）

Case 9/10 为纯函数 + 假 response 对象；Case 17/18 的浏览器/Playwright/子进程
依赖全部 fake，无真实浏览器、无真实网络。
"""

from __future__ import annotations

import inspect
import json
import sys
import types
from pathlib import Path

import pytest

import scripts.cnnc_auth_browser as browser

AUTHORIZE_URL = (
    "https://sso.cnncecp.com/esc-sso/oauth2.0/authorize"
    "?client_id=4dbc816bf265438e&response_type=code"
    "&redirect_uri=https://one.cnncecp.com/cnnc-web/"
)

WHITELIST_NAMES = [
    "TGC",
    "SESSION",
    "GWSID1-sso.cnncecp.com",
    "mfaQueryValue",
    "org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE",
]


class _FakeResponse:
    """模拟 Playwright response：status / url / headers(dict)。"""

    def __init__(self, status, url, headers=None):
        self.status = status
        self.url = url
        self.headers = headers if headers is not None else {}


# ---------------------------------------------------------------------- Case 9


def test_case9_captures_oc_from_external_authorize_redirect():
    response = _FakeResponse(
        302,
        AUTHORIZE_URL,
        {"location": "https://one.cnncecp.com/cnnc-web/?code=OC-999001-abc"},
    )

    assert browser.capture_oc_from_response(response) == "OC-999001-abc"


def test_case9_ignores_internal_sso_oauth_code():
    # SSO 内部 OAuth（→ /ngw/login?code=OC-...）的码不能抓，抓错换 token 报 040000
    response = _FakeResponse(
        302,
        AUTHORIZE_URL,
        {"location": "https://sso.cnncecp.com/ngw/login?code=OC-999002-xyz"},
    )

    assert browser.capture_oc_from_response(response) is None


def test_case9_ignores_non_redirect_response():
    response = _FakeResponse(
        200,
        AUTHORIZE_URL,
        {"location": "https://one.cnncecp.com/cnnc-web/?code=OC-999001-abc"},
    )

    assert browser.capture_oc_from_response(response) is None


def test_case9_ignores_location_without_code():
    response = _FakeResponse(
        302,
        AUTHORIZE_URL,
        {"location": "https://one.cnncecp.com/cnnc-web/"},
    )

    assert browser.capture_oc_from_response(response) is None


def test_case9_ignores_redirect_from_non_authorize_url():
    response = _FakeResponse(
        302,
        "https://sso.cnncecp.com/portal/rest/selfcare/redirect/user",
        {"location": "https://one.cnncecp.com/cnnc-web/?code=OC-999001-abc"},
    )

    assert browser.capture_oc_from_response(response) is None


def test_case9_harvest_source_never_uses_context_route_interception():
    source = Path(browser.__file__).read_text(encoding="utf-8")
    assert "ctx.route" not in source


# --------------------------------------------------------------------- Case 10


def test_case10_select_session_cookies_drops_stale_waf_cookies():
    cookies = [
        {"name": "K5CjYVsXPtdVO", "value": "stale-k5-1", "domain": ".cnncecp.com"},
        {"name": "TGC", "value": "tgc-1", "domain": "sso.cnncecp.com"},
        {"name": "K5CjYVsXPtdVP", "value": "stale-k5-2", "domain": ".cnncecp.com"},
        {"name": "SF_cookie_54", "value": "sf", "domain": ".cnncecp.com"},
        {"name": "SESSION", "value": "sess-1", "domain": "sso.cnncecp.com"},
        {"name": "UqZBpD3nviExample", "value": "uq", "domain": ".cnncecp.com"},
        {"name": "token", "value": "stale-token", "domain": ".cnncecp.com"},
        {"name": "GWSID1-sso.cnncecp.com", "value": "gws", "domain": "sso.cnncecp.com"},
        {"name": "mfaQueryValue", "value": "mfa", "domain": "sso.cnncecp.com"},
        {
            "name": "org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE",
            "value": "zh_CN",
            "domain": "sso.cnncecp.com",
        },
    ]

    selected = browser.select_session_cookies(cookies)

    assert sorted(c["name"] for c in selected) == sorted(WHITELIST_NAMES)


def test_case10_select_session_cookies_handles_empty_input():
    assert browser.select_session_cookies([]) == []
    assert browser.select_session_cookies(None) == []


def test_case10_session_cookie_names_whitelist_exact():
    assert browser.SESSION_COOKIE_NAMES == set(WHITELIST_NAMES)


# --------------------------------------------------------------------- Case 17


class _FakeChromeProc:
    """fake Popen 返回值：足以支撑 CDP 就绪轮询与 _kill_chrome 清理。"""

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


class _FakeCdpVersionResponse:
    """fake http://127.0.0.1:<port>/json/version 响应（首个轮询即就绪）。"""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeCdpBrowser:
    contexts = []  # 空 contexts：_launch_chrome_cdp 不触页，调用方按需处理

    def close(self):
        pass


class _FakeChromium:
    def connect_over_cdp(self, _endpoint):
        return _FakeCdpBrowser()


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()

    def stop(self):
        pass


def _install_fake_playwright(monkeypatch):
    """预置 fake playwright 模块到 sys.modules。

    _launch_chrome_cdp 在函数体内 `from playwright.sync_api import
    sync_playwright`，该 import 会命中 sys.modules 中的 fake，无需真实
    playwright，也不会启动任何驱动进程。
    """

    def fake_sync_playwright():
        class _Handle:
            def start(self):
                return _FakePlaywright()

        return _Handle()

    fake_pkg = types.ModuleType("playwright")
    fake_api = types.ModuleType("playwright.sync_api")
    fake_api.sync_playwright = fake_sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_api)


def test_case17_launch_chrome_cdp_uses_ephemeral_user_data_dir(monkeypatch, tmp_path):
    """_launch_chrome_cdp 必须把显式传入的临时目录用作 --user-data-dir。

    harvest/smoke 场景传入一次性临时目录（系统 TEMP）；argv 绝不出现持久
    PROFILE_DIR（data/cnnc_auth_profile）——固定 Profile 会累积陈旧 K5/SF/UqZ
    持久 cookie，导致 harvest 卡进 WAF 挑战循环。
    """
    ephemeral = tmp_path / "ephemeral-harvest-profile"
    captured = {}

    def fake_popen(argv):
        captured["argv"] = [str(arg) for arg in argv]
        return _FakeChromeProc()

    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        browser.urllib.request, "urlopen", lambda *_a, **_k: _FakeCdpVersionResponse()
    )
    _install_fake_playwright(monkeypatch)

    proc, cdp_browser, pw = browser._launch_chrome_cdp(
        url=None, headless=True, user_data_dir=ephemeral
    )
    try:
        argv = captured["argv"]
        assert "--user-data-dir=%s" % ephemeral in argv
        assert "--headless=new" in argv
        assert not any("cnnc_auth_profile" in arg for arg in argv)
    finally:
        browser._close_cdp(proc, cdp_browser, pw)


def test_case17_cmd_harvest_removes_temp_profile_even_on_launch_failure(
    monkeypatch, tmp_path
):
    """cmd_harvest 结束后临时 Profile 必须被删除（覆盖 finally 清理路径）。

    用受控目录替换 tempfile.mkdtemp，让 _launch_chrome_cdp 抛错，验证
    finally 里的 rmtree 仍以该临时目录为参数执行；成功路径的 rmtree/mkdtemp
    存在性由源码断言兜底（覆盖层级注明：本用例 = 行为级失败路径 + 源码级）。
    """
    storage_state = tmp_path / "cnnc_auth_storage_state.json"
    storage_state.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    monkeypatch.setattr(browser, "STORAGE_STATE", storage_state)

    controlled = tmp_path / "controlled-harvest-profile"
    controlled.mkdir()
    monkeypatch.setattr(
        browser.tempfile, "mkdtemp", lambda *_a, **_k: str(controlled)
    )
    rmtree_calls = []

    def fake_rmtree(path, *_args, **_kwargs):
        rmtree_calls.append(Path(path))

    monkeypatch.setattr(browser.shutil, "rmtree", fake_rmtree)

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated launch failure")

    monkeypatch.setattr(browser, "_launch_chrome_cdp", boom)

    with pytest.raises(RuntimeError, match="simulated launch failure"):
        browser.cmd_harvest(types.SimpleNamespace(headless=True))

    assert rmtree_calls == [controlled]


def test_case17_cmd_harvest_source_never_touches_persistent_profile():
    """源码级断言：cmd_harvest 只用 mkdtemp 临时 Profile + rmtree 清理。"""
    source = inspect.getsource(browser.cmd_harvest)
    assert "PROFILE_DIR" not in source
    assert "tempfile.mkdtemp" in source
    assert "shutil.rmtree" in source


# --------------------------------------------------------------------- Case 18


def _full_browser_cookies(value_suffix=""):
    """10 个 cookie：5 个 SSO 会话白名单 + 5 个不得落盘的 WAF/陈旧 cookie。"""
    values = {name: name + value_suffix for name in WHITELIST_NAMES}
    return [
        {"name": "K5CjYVsXPtdVO", "value": "stale-k5-vo", "domain": ".cnncecp.com"},
        {"name": "TGC", "value": values["TGC"], "domain": "sso.cnncecp.com"},
        {"name": "K5CjYVsXPtdVP", "value": "stale-k5-vp", "domain": ".cnncecp.com"},
        {"name": "SF_cookie_54", "value": "sf", "domain": ".cnncecp.com"},
        {"name": "SESSION", "value": values["SESSION"], "domain": "sso.cnncecp.com"},
        {"name": "UqZBpD3nviExample", "value": "uq", "domain": ".cnncecp.com"},
        {"name": "token", "value": "stale-token", "domain": ".cnncecp.com"},
        {
            "name": "GWSID1-sso.cnncecp.com",
            "value": values["GWSID1-sso.cnncecp.com"],
            "domain": "sso.cnncecp.com",
        },
        {
            "name": "mfaQueryValue",
            "value": values["mfaQueryValue"],
            "domain": "sso.cnncecp.com",
        },
        {
            "name": (
                "org.springframework.web.servlet.i18n."
                "CookieLocaleResolver.LOCALE"
            ),
            "value": values[
                "org.springframework.web.servlet.i18n."
                "CookieLocaleResolver.LOCALE"
            ],
            "domain": "sso.cnncecp.com",
        },
    ]


def test_case18_save_session_state_persists_whitelist_only(tmp_path):
    path = tmp_path / "storage" / "cnnc_auth_storage_state.json"

    saved = browser.save_session_state(_full_browser_cookies(), path)

    # 名单恰为白名单 5 个（顺序与输入一致），origins 恒为空
    assert sorted(saved) == sorted(WHITELIST_NAMES)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(c["name"] for c in payload["cookies"]) == sorted(WHITELIST_NAMES)
    assert payload["origins"] == []
    # 文件全文不含任何 WAF/陈旧 cookie 的名字或 token= 形态
    text = path.read_text(encoding="utf-8")
    for forbidden in (
        "K5CjYVsXPtdVO",
        "K5CjYVsXPtdVP",
        "SF_cookie_54",
        "UqZBpD3n",
        "token=",
        "stale-k5",
        "stale-token",
    ):
        assert forbidden not in text


def test_case18_save_session_state_second_call_atomically_overwrites(tmp_path):
    path = tmp_path / "storage" / "cnnc_auth_storage_state.json"
    browser.save_session_state(_full_browser_cookies(), path)

    saved = browser.save_session_state(_full_browser_cookies("-v2"), path)

    assert sorted(saved) == sorted(WHITELIST_NAMES)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert {c["value"] for c in payload["cookies"]} == {
        name + "-v2" for name in WHITELIST_NAMES
    }
    # 原子写不残留 tmp(uuid) 半截文件
    assert list(path.parent.glob(path.name + ".tmp-*")) == []


# --------------------------------------------------------------------- Case 20


def test_case20_cmd_harvest_source_exchanges_oc_in_browser_and_prints_at():
    """源码级断言：cmd_harvest 在浏览器内做同源 fetch 交换并追加打印 AT 行。

    2026-09-02 实测：午后 WAF 对裸 Python 请求返回 502，浏览器内同源 fetch
    返回 200/success:true。cmd_harvest 收割 OC 后必须在浏览器内执行
    anon/account/login fetch，成功时在 OC= 行后追加打印 AT=<token>（provider
    的 _default_harvest 据此解析双通道返回）。
    """
    source = inspect.getsource(browser.cmd_harvest)
    assert "anon/account/login" in source  # 浏览器内同源 fetch 交换存在
    assert "AT=%s" in source  # 交换成功时追加打印 AT 行


# --------------------------------------------------------------------- Case 21

# 持久 Profile 路径（login 旧实现专用，2026-09-03 起已废弃；此常量仅用于
# 断言 login 绝不再传入该路径）
PERSISTENT_PROFILE = (
    Path(browser.__file__).resolve().parent.parent / "data" / "cnnc_auth_profile"
)


class _FakeLoginContext:
    """fake CDP 上下文：cookies() 返回含 TGC 的 cookie（模拟真人登录已完成）。"""

    def cookies(self):
        return [
            {"name": "TGC", "value": "tgc-fresh", "domain": "sso.cnncecp.com"},
            {"name": "SESSION", "value": "sess-fresh", "domain": "sso.cnncecp.com"},
            {"name": "K5CjYVsXPtdVO", "value": "waf-k5", "domain": ".cnncecp.com"},
        ]


class _FakeLoginBrowser:
    def __init__(self):
        self.contexts = [_FakeLoginContext()]

    def close(self):
        pass


def test_case21_cmd_login_launches_ephemeral_profile_and_removes_it(
    monkeypatch, tmp_path
):
    """cmd_login 必须用一次性临时 Profile 启动，返回后删除该目录。

    持久 PROFILE_DIR 中物理残留的陈旧 TGC 会被"崩溃恢复"还原，cmd_login
    启动 ~2 秒即误判登录成功并关窗，用户永远没机会输密码。login 改为空
    cookie 临时 Profile 启动后，TGC 只可能来自本次真人登录新签发。
    """
    captured = {}

    def fake_launch(url=None, headless=False, *, user_data_dir=None):
        captured["url"] = url
        captured["user_data_dir"] = user_data_dir
        return _FakeChromeProc(), _FakeLoginBrowser(), _FakePlaywright()

    monkeypatch.setattr(browser, "_launch_chrome_cdp", fake_launch)
    storage_state = tmp_path / "cnnc_auth_storage_state.json"
    monkeypatch.setattr(browser, "STORAGE_STATE", storage_state)
    monkeypatch.setattr(browser.time, "sleep", lambda *_a, **_k: None)
    rmtree_calls = []

    def fake_rmtree(path, *_args, **_kwargs):
        rmtree_calls.append(Path(path))

    monkeypatch.setattr(browser.shutil, "rmtree", fake_rmtree)

    exit_code = browser.cmd_login(types.SimpleNamespace())

    assert exit_code == 0
    udd = captured["user_data_dir"]
    # (1) 传给 _launch_chrome_cdp 的是一次性临时目录：非 None、非持久 PROFILE_DIR
    assert udd is not None
    assert Path(udd) != PERSISTENT_PROFILE
    assert "cnnc_auth_profile" not in str(udd)
    assert captured["url"] == browser.LOGIN_ENTRY
    # (2) cmd_login 返回后临时目录已被 rmtree（成功路径的 finally 清理）
    assert rmtree_calls == [Path(udd)]
    # 成功路径不回归：storage_state 只落白名单 cookie（陈旧 K5 不落盘）
    payload = json.loads(storage_state.read_text(encoding="utf-8"))
    assert {c["name"] for c in payload["cookies"]} == {"TGC", "SESSION"}


def test_case21_cmd_login_removes_temp_profile_even_on_launch_failure(
    monkeypatch, tmp_path
):
    """mkdtemp 成功但 _launch_chrome_cdp 抛错时，finally 仍必须 rmtree。

    失败清理路径，风格同 test_case17 的 cmd_harvest 失败路径用例。
    """
    controlled = tmp_path / "controlled-login-profile"
    controlled.mkdir()
    monkeypatch.setattr(
        browser.tempfile, "mkdtemp", lambda *_a, **_k: str(controlled)
    )
    rmtree_calls = []

    def fake_rmtree(path, *_args, **_kwargs):
        rmtree_calls.append(Path(path))

    monkeypatch.setattr(browser.shutil, "rmtree", fake_rmtree)

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated login launch failure")

    monkeypatch.setattr(browser, "_launch_chrome_cdp", boom)

    with pytest.raises(RuntimeError, match="simulated login launch failure"):
        browser.cmd_login(types.SimpleNamespace())

    assert rmtree_calls == [controlled]


def test_case21_cmd_login_source_uses_ephemeral_profile():
    """源码级断言：cmd_login 只用 mkdtemp 临时 Profile + rmtree 清理。"""
    source = inspect.getsource(browser.cmd_login)
    assert "PROFILE_DIR" not in source
    assert "tempfile.mkdtemp" in source
    assert "shutil.rmtree" in source


def test_case21_launch_chrome_cdp_source_has_no_persistent_fallback():
    """源码级断言：_launch_chrome_cdp 不再有 user_data_dir=PROFILE_DIR 回退。"""
    source = inspect.getsource(browser._launch_chrome_cdp)
    assert "user_data_dir = PROFILE_DIR" not in source


def test_case21_launch_chrome_cdp_requires_user_data_dir():
    """user_data_dir 必填：缺省时参数绑定期即抛 TypeError（无任何副作用）。"""
    with pytest.raises(TypeError):
        browser._launch_chrome_cdp()
