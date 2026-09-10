# -*- coding: utf-8 -*-
"""scripts/cnnc_login_autofill.py 深模块测试（离线，只测公开接口，不起浏览器）。

- 凭证 seam load_login_credentials：正常 / 文件缺失 / 坏 JSON / 缺字段 /
  空白值 / auto_fill_enabled=false / 顶层非对象 → None；绝不抛异常
- TRUSTED_LOGIN_ORIGINS：精确白名单 = {"https://sso.cnncecp.com"}
- Origin 门禁（deny-by-default，经 LoginAutofiller.tick 公开路径）：
  只填 https://sso.cnncecp.com（显式 :443 等价）；第三方域 / 仿冒子域 /
  http 明文 / about:blank / data: / 端口不同一律跳过，且门禁先于任何
  字段读取（未过门禁连 frame.evaluate 都不做）
- 填充行为（经 tick）：启发式命中与 fallback、只填空字段、验证码类
  绝不填、DOM 漂移跳过下轮重评、幂等、注入 clock 验证限速、异常吞掉
  （WARN 限次）
- create_login_autofiller：凭证缺失 → None + 一行 AUTO_FILL_OFF 提示

fake frame 与 Playwright 同构：带 .url 属性、.evaluate(JS) 返回字段描述
列表（{type,name,id,placeholder,className,value,disabled,readonly,visible}，
与 _COLLECT_INPUTS_JS 输出同构）、locator("input").nth(idx) 可记录 fill 调用。

全部用例无真实浏览器、无真实网络；测试凭证均为假值，绝不出现真实密码。
"""

from __future__ import annotations

import json

import pytest

import scripts.cnnc_login_autofill as autofill
from scripts.cnnc_login_autofill import (
    AUTOFILL_MAX_WARNS,
    TRUSTED_LOGIN_ORIGINS,
    LoginAutofiller,
    create_login_autofiller,
    load_login_credentials,
)

# 假凭证（与真实凭证无关）
FAKE_USERNAME = "autofill-user"
FAKE_PASSWORD = "autofill-pass-fake"
FAKE_CREDS = {"username": FAKE_USERNAME, "password": FAKE_PASSWORD}

# 可信登录表单 Origin（SSO 服务真实所在域）
TRUSTED_URL = "https://sso.cnncecp.com/esc-sso/login"


# ---------------------------------------------------------------- fake 对象


def _field(**overrides):
    """与 _COLLECT_INPUTS_JS 输出同构的输入框描述（注意键是 className）。"""
    base = {
        "type": "text", "name": "", "id": "", "placeholder": "",
        "className": "", "value": "", "visible": True,
        "disabled": False, "readonly": False,
    }
    base.update(overrides)
    return base


class _FakeLocator:
    """fake frame.locator('input').nth(i)：绑定到 fake 元素并记录 fill。"""

    def __init__(self, element, calls):
        self._element = element
        self._calls = calls

    def evaluate(self, _js):
        return {"type": self._element["type"],
                "name": self._element["name"],
                "id": self._element["id"]}

    def fill(self, value, timeout=None):
        key = (self._element["name"] or self._element["id"]
               or self._element["className"])
        self._calls.append((key, value))
        self._element["value"] = value


class _FakeLocatorList:
    def __init__(self, frame):
        self._frame = frame

    def nth(self, index):
        frame = self._frame
        element = frame.elements[index]
        if frame.drift_shift:
            # 模拟 DOM 漂移：locator 二次解析到的元素与采样索引不一致
            element = frame.elements[
                (index + frame.drift_shift) % len(frame.elements)]
        return frame.locator_cls(element, frame.calls)


class _FakeFrame:
    """fake Playwright frame：.url + .evaluate 返回字段描述快照。"""

    locator_cls = _FakeLocator

    def __init__(self, elements, url=TRUSTED_URL):
        self.elements = elements
        self.url = url
        self.calls = []
        self.evaluate_calls = 0
        self.drift_shift = 0

    def evaluate(self, _js):
        self.evaluate_calls += 1
        return [dict(el) for el in self.elements]

    def locator(self, _selector):
        return _FakeLocatorList(self)


class _FailFillLocator(_FakeLocator):
    def fill(self, value, timeout=None):
        raise TimeoutError("fill timeout")


class _FailFillFrame(_FakeFrame):
    """locator.fill 必抛（模拟超时）：验证 WARN 限次。"""

    locator_cls = _FailFillLocator


class _BoomFrame:
    """模拟导航中/已销毁的 frame：evaluate 抛错。"""

    url = TRUSTED_URL

    def evaluate(self, *_args, **_kwargs):
        raise RuntimeError("Execution context was destroyed")


class _BoomUrlFrame:
    """模拟已销毁 frame：.url 属性 getter 本身抛错（Playwright teardown
    阶段访问已分离 frame 的 .url 可能抛错，tick 绝不能因此向调用方抛异常）。
    """

    @property
    def url(self):
        raise RuntimeError("Frame was detached")

    def evaluate(self, *_args, **_kwargs):
        raise AssertionError("url 不可信的 frame 绝不应进入字段读取")


class _FakePage:
    def __init__(self, frames):
        self.frames = frames


class _FakeCtx:
    def __init__(self, pages):
        self.pages = pages


def _make_autofiller(**kwargs):
    kwargs.setdefault("credentials", dict(FAKE_CREDS))
    return LoginAutofiller(**kwargs)


def _calls_names(frame):
    return [name for name, _value in frame.calls]


# ---------------------------------------------------------------- 凭证 seam


def _write_creds(tmp_path, payload, name="cnnc_login_credentials.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_credentials_ok(tmp_path):
    path = _write_creds(tmp_path, {"username": FAKE_USERNAME,
                                   "password": FAKE_PASSWORD})

    assert load_login_credentials(path) == FAKE_CREDS


def test_load_credentials_default_enabled_when_flag_absent(tmp_path):
    # auto_fill_enabled 缺省视为 true
    path = _write_creds(tmp_path, {"username": FAKE_USERNAME,
                                   "password": FAKE_PASSWORD,
                                   "auto_fill_enabled": True})

    assert load_login_credentials(path) == FAKE_CREDS


def test_load_credentials_missing_file(tmp_path):
    assert load_login_credentials(tmp_path / "nope.json") is None


def test_load_credentials_bad_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    assert load_login_credentials(path) is None


def test_load_credentials_missing_username(tmp_path):
    path = _write_creds(tmp_path, {"password": FAKE_PASSWORD})

    assert load_login_credentials(path) is None


def test_load_credentials_blank_password(tmp_path):
    path = _write_creds(tmp_path, {"username": FAKE_USERNAME, "password": "  "})

    assert load_login_credentials(path) is None


def test_load_credentials_disabled(tmp_path):
    path = _write_creds(tmp_path, {"username": FAKE_USERNAME,
                                   "password": FAKE_PASSWORD,
                                   "auto_fill_enabled": False})

    assert load_login_credentials(path) is None


def test_load_credentials_non_dict_top_level(tmp_path):
    path = tmp_path / "list.json"
    path.write_text('["username"]', encoding="utf-8")

    assert load_login_credentials(path) is None


def test_load_credentials_never_raises(tmp_path):
    # 传入目录等病态路径也绝不能抛异常
    assert load_login_credentials(tmp_path) is None


# ---------------------------------------------------------- 工厂 + 白名单


def test_trusted_origins_is_exact_sso_whitelist():
    # 精确白名单：只有 SSO 登录表单真实所在的 origin（证据见模块 docstring）
    assert TRUSTED_LOGIN_ORIGINS == frozenset({"https://sso.cnncecp.com"})


def test_create_autofiller_returns_none_and_prints_off(tmp_path, capsys):
    filler = create_login_autofiller(tmp_path / "missing.json")

    assert filler is None
    out = capsys.readouterr().out
    assert "AUTO_FILL_OFF" in out
    assert "需手动输入账号密码" in out


def test_create_autofiller_returns_filler_for_valid_creds(tmp_path):
    path = _write_creds(tmp_path, {"username": FAKE_USERNAME,
                                   "password": FAKE_PASSWORD})

    filler = create_login_autofiller(path)

    assert isinstance(filler, LoginAutofiller)


def test_create_autofiller_default_path_reads_module_constant(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(autofill, "CREDENTIALS_FILE",
                        tmp_path / "missing.json")

    assert create_login_autofiller() is None
    assert "AUTO_FILL_OFF" in capsys.readouterr().out


# ------------------------------------------------------------ Origin 门禁


def test_tick_fills_typical_form_on_trusted_origin():
    frame = _FakeFrame([
        _field(name="username"),
        _field(type="password", name="password"),
    ])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == [("username", FAKE_USERNAME),
                           ("password", FAKE_PASSWORD)]


def test_tick_trusted_origin_with_explicit_443_is_equivalent():
    frame = _FakeFrame([
        _field(name="username"),
        _field(type="password", name="password"),
    ], url="https://sso.cnncecp.com:443/esc-sso/login")

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert _calls_names(frame) == ["username", "password"]


@pytest.mark.parametrize("url", [
    "https://evil.com/login",
    "https://sso.cnncecp.com.evil.com/login",   # 仿冒子域：精确匹配拒绝
    "http://sso.cnncecp.com/login",             # 明文 http：拒绝
    "https://sso.cnncecp.com:8443/login",       # 端口不同 = 不同 origin
    "about:blank",
    "data:text/html,<input>",
    "",                                         # 无 URL（防御）
])
def test_untrusted_origins_never_read_or_fill(url, capsys):
    """门禁先于任何字段读取：未过门禁的 frame 连 evaluate 都不做。"""
    frame = _FakeFrame([
        _field(name="username"),
        _field(type="password", name="password"),
    ], url=url)

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == []
    assert frame.evaluate_calls == 0
    out = capsys.readouterr().out
    assert "AUTO_FILL：" not in out


def test_skip_log_dedup_per_origin_and_bounded(capsys):
    evil_a = _FakeFrame([_field(name="username")], url="https://evil.com/a")
    evil_b = _FakeFrame([_field(name="username")], url="https://evil.com/b")
    other = _FakeFrame([_field(name="username")], url="https://other.com/")

    _make_autofiller().tick(_FakeCtx([_FakePage([evil_a, evil_b, other])]))

    out = capsys.readouterr().out
    # 同一 origin 只打一次；不同 origin 各打一次
    assert out.count("AUTO_FILL_SKIP origin=https://evil.com") == 1
    assert out.count("AUTO_FILL_SKIP origin=https://other.com") == 1


# ------------------------------------------------- 填充行为（经 tick 公开路径）


def test_tick_heuristic_skips_captcha_before_username():
    frame = _FakeFrame([
        _field(name="captcha"),
        _field(id="loginName"),
        _field(type="password", name="password"),
    ])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == [("loginName", FAKE_USERNAME),
                           ("password", FAKE_PASSWORD)]


def test_tick_classname_hint_counts_for_username_match():
    # 启发式来源与 _COLLECT_INPUTS_JS 同构：className 参与命中
    frame = _FakeFrame([
        _field(name="keyword", placeholder="搜索"),
        _field(className="css-username-box"),
        _field(type="password", name="password"),
    ])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == [("css-username-box", FAKE_USERNAME),
                           ("password", FAKE_PASSWORD)]


def test_tick_fallback_first_text_before_password():
    # 无任何启发式命中：fallback 到密码框之前第一个可见文本输入框
    frame = _FakeFrame([
        _field(name="mobile"),
        _field(type="password", name="password"),
    ])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == [("mobile", FAKE_USERNAME),
                           ("password", FAKE_PASSWORD)]


def test_tick_fallback_skips_hidden_and_checkbox():
    frame = _FakeFrame([
        _field(type="hidden", name="userToken"),
        _field(type="checkbox", name="agree"),
        _field(name="realname"),
        _field(type="password", name="password"),
    ])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == [("realname", FAKE_USERNAME),
                           ("password", FAKE_PASSWORD)]


def test_tick_fallback_never_captcha():
    # 密码框之前只有验证码输入框时，绝不能把用户名填进验证码框
    frame = _FakeFrame([
        _field(name="smsCode"),
        _field(type="password", name="password"),
    ])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == [("password", FAKE_PASSWORD)]


def test_tick_fallback_only_before_password():
    # 密码框之后的文本框不进 fallback
    frame = _FakeFrame([
        _field(type="password", name="password"),
        _field(name="nickname"),
    ])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == [("password", FAKE_PASSWORD)]


@pytest.mark.parametrize("captcha_field", [
    _field(name="captcha"),
    _field(name="smsCode"),
    _field(name="otp"),
    _field(name="verifyCode"),
    _field(placeholder="请输入验证码"),
    _field(placeholder="短信校验码"),
])
def test_tick_never_fills_captcha_or_otp_fields(captcha_field):
    frame = _FakeFrame([captcha_field])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == []


def test_tick_never_overwrites_human_username():
    frame = _FakeFrame([
        _field(name="username", value="human-typed"),
        _field(type="password", name="password"),
    ])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert _calls_names(frame) == ["password"]
    assert frame.elements[0]["value"] == "human-typed"


def test_tick_never_overwrites_human_password():
    frame = _FakeFrame([
        _field(name="username"),
        _field(type="password", name="password", value="human-typed"),
    ])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert _calls_names(frame) == ["username"]
    assert frame.elements[1]["value"] == "human-typed"


def test_tick_ignores_invisible_disabled_readonly():
    frame = _FakeFrame([
        _field(name="username", visible=False),
        _field(name="username2", disabled=True),
        _field(type="password", name="password", readonly=True),
        _field(type="password", name="password2"),
    ])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == [("password2", FAKE_PASSWORD)]


def test_tick_skips_on_dom_drift_then_refills_next_round():
    """DOM 漂移（locator 二次解析签名不符）：本轮跳过不盲填，下轮重评。"""
    frame = _FakeFrame([
        _field(name="username"),
        _field(type="password", name="password"),
    ])
    now = [0.0]
    filler = _make_autofiller(clock=lambda: now[0])
    frame.drift_shift = 1

    filler.tick(_FakeCtx([_FakePage([frame])]))
    assert frame.calls == []  # 签名不符：绝不盲填

    frame.drift_shift = 0
    now[0] += 10.0  # 推进注入时钟越过限速窗口
    filler.tick(_FakeCtx([_FakePage([frame])]))
    assert _calls_names(frame) == ["username", "password"]


def test_tick_idempotent_after_both_filled():
    frame = _FakeFrame([
        _field(name="username"),
        _field(type="password", name="password"),
    ])
    now = [0.0]
    filler = _make_autofiller(clock=lambda: now[0])

    filler.tick(_FakeCtx([_FakePage([frame])]))
    assert len(frame.calls) == 2

    # 模拟 SPA 重渲染清空字段，并推进时间越过限速窗口：已填过绝不重写
    frame.elements[0]["value"] = ""
    frame.elements[1]["value"] = ""
    now[0] += 10.0
    filler.tick(_FakeCtx([_FakePage([frame])]))

    assert len(frame.calls) == 2
    assert frame.evaluate_calls == 1  # 幂等直接返回，连字段读取都不再做


def test_tick_rate_limited_by_injected_clock():
    frame = _FakeFrame([_field(name="search")])
    now = [0.0]
    filler = _make_autofiller(interval_seconds=3.5, clock=lambda: now[0])
    ctx = _FakeCtx([_FakePage([frame])])

    filler.tick(ctx)
    assert frame.evaluate_calls == 1

    now[0] = 1.0  # 未到间隔：直接返回，连字段读取都不做
    filler.tick(ctx)
    assert frame.evaluate_calls == 1

    now[0] = 10.0  # 越过间隔：重新尝试
    filler.tick(ctx)
    assert frame.evaluate_calls == 2


def test_tick_swallows_frame_evaluate_errors():
    good = _FakeFrame([
        _field(id="loginName"),
        _field(type="password", name="password"),
    ])
    ctx = _FakeCtx([
        _FakePage([_BoomFrame(), _FakeFrame([_field(name="captcha")])]),
        _FakePage([good]),
    ])

    _make_autofiller().tick(ctx)  # 绝不能抛异常（cmd_login 轮询循环不死）

    assert good.calls == [("loginName", FAKE_USERNAME),
                          ("password", FAKE_PASSWORD)]


def test_tick_tolerates_frame_url_getter_error_and_keeps_processing(capsys):
    """已销毁 frame 的 .url getter 抛错：按不可信 frame 跳过，tick 绝不
    向调用方抛异常，同页/后页其他 frame 仍正常填充。"""
    broken = _BoomUrlFrame()
    good = _FakeFrame([
        _field(id="loginName"),
        _field(type="password", name="password"),
    ])
    ctx = _FakeCtx([
        _FakePage([broken, _FakeFrame([_field(name="captcha")])]),
        _FakePage([good]),
    ])

    _make_autofiller().tick(ctx)  # 绝不能抛异常

    assert good.calls == [("loginName", FAKE_USERNAME),
                          ("password", FAKE_PASSWORD)]


def test_tick_warn_limited_on_fill_failure(capsys):
    frame = _FailFillFrame([
        _field(name="username"),
        _field(type="password", name="password"),
    ])
    now = [0.0]
    filler = _make_autofiller(clock=lambda: now[0])

    # 连续多轮失败：绝不抛异常，WARN 最多 AUTOFILL_MAX_WARNS 次（之后静默）
    for _ in range(AUTOFILL_MAX_WARNS + 3):
        filler.tick(_FakeCtx([_FakePage([frame])]))
        now[0] += 10.0

    err = capsys.readouterr().err
    assert err.count("WARN：自动填充尝试失败") == AUTOFILL_MAX_WARNS


def test_tick_no_login_form_is_noop():
    frame = _FakeFrame([_field(name="search")])

    _make_autofiller().tick(_FakeCtx([_FakePage([frame])]))

    assert frame.calls == []


# ------------------------------------------------------------ 安全边界


def test_module_source_never_clicks_or_submits():
    """源码级断言：填充器绝不包含点击/提交动作。"""
    import inspect

    source = inspect.getsource(autofill)
    assert ".click(" not in source
    assert ".press(" not in source
    assert ".check(" not in source
    assert ".submit(" not in source
    assert "AUTO_FILL_OFF" in source
