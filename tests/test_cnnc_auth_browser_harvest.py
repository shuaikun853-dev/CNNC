# -*- coding: utf-8 -*-
"""scripts/cnnc_auth_browser.py 纯函数测试（Case 9-10）。

- Case 9  capture_oc_from_response：Location 头 OC 捕获规则（无 ctx.route）
- Case 10 select_session_cookies：只保留白名单会话 cookie，不注陈旧 K5

全部为纯函数 + 假 response 对象，无浏览器、无网络、无子进程。
"""

from __future__ import annotations

from pathlib import Path

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
