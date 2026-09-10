# -*- coding: utf-8 -*-
"""scripts/cnnc_auth_browser.py login 自动填充测试（离线，不起浏览器）。

- load_login_credentials：正常 / 文件缺失 / 坏 JSON / 缺字段 /
  auto_fill_enabled=false / 顶层非对象 → None；绝不抛异常
- 字段判定：type=password 识别；中英文用户名启发式命中；验证码/短信/
  checkbox/hidden 不得当用户名框
- _pick_frame_fill_targets：启发式优先、fallback 顺序、只填空字段
- _autofill_once：fake page/frame 上真实决策路径（含 iframe 遍历、
  只填空字段、filled 后停止、异常吞掉不打断）
- cmd_login 集成：凭证缺失打 AUTO_FILL_OFF 且登录流程不受影响；
  凭证存在时零 pages 上下文也能正常完成 TGC 轮询

全部用例无真实浏览器、无真实网络；测试凭证均为假值，绝不出现真实密码。
"""

from __future__ import annotations

import json
import types

import scripts.cnnc_auth_browser as browser

# 假凭证（与真实凭证无关）
FAKE_USERNAME = "autofill-user"
FAKE_PASSWORD = "autofill-pass-fake"
FAKE_CREDS = {"username": FAKE_USERNAME, "password": FAKE_PASSWORD}


# ---------------------------------------------------------------- 凭证加载


def _write_creds(tmp_path, payload, name="cnnc_login_credentials.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_credentials_ok(tmp_path):
    path = _write_creds(tmp_path, {"username": FAKE_USERNAME,
                                   "password": FAKE_PASSWORD})

    assert browser.load_login_credentials(path) == FAKE_CREDS


def test_load_credentials_default_enabled_when_flag_absent(tmp_path):
    # auto_fill_enabled 缺省视为 true
    path = _write_creds(tmp_path, {"username": FAKE_USERNAME,
                                   "password": FAKE_PASSWORD,
                                   "auto_fill_enabled": True})

    assert browser.load_login_credentials(path) == FAKE_CREDS


def test_load_credentials_missing_file(tmp_path):
    assert browser.load_login_credentials(tmp_path / "nope.json") is None


def test_load_credentials_bad_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    assert browser.load_login_credentials(path) is None


def test_load_credentials_missing_username(tmp_path):
    path = _write_creds(tmp_path, {"password": FAKE_PASSWORD})

    assert browser.load_login_credentials(path) is None


def test_load_credentials_blank_password(tmp_path):
    path = _write_creds(tmp_path, {"username": FAKE_USERNAME, "password": "  "})

    assert browser.load_login_credentials(path) is None


def test_load_credentials_disabled(tmp_path):
    path = _write_creds(tmp_path, {"username": FAKE_USERNAME,
                                   "password": FAKE_PASSWORD,
                                   "auto_fill_enabled": False})

    assert browser.load_login_credentials(path) is None


def test_load_credentials_non_dict_top_level(tmp_path):
    path = tmp_path / "list.json"
    path.write_text('["username"]', encoding="utf-8")

    assert browser.load_login_credentials(path) is None


def test_load_credentials_never_raises(tmp_path):
    # 传入目录等病态路径也绝不能抛异常
    assert browser.load_login_credentials(tmp_path) is None


# ---------------------------------------------------------------- 字段判定


def _field(**overrides):
    base = {
        "type": "text", "name": "", "id": "", "placeholder": "",
        "class": "", "value": "", "visible": True,
        "disabled": False, "readonly": False,
    }
    base.update(overrides)
    return base


def test_is_password_field():
    assert browser._is_password_field({"type": "password"})
    assert browser._is_password_field({"type": "PASSWORD"})
    assert browser._is_password_field({"type": " password "})
    assert not browser._is_password_field({"type": "text"})
    assert not browser._is_password_field({})


def test_score_username_heuristics_hit():
    cases = [
        {"name": "username"},
        {"name": "j_username"},          # CAS 常见命名
        {"id": "loginName"},
        {"name": "user_name"},
        {"placeholder": "请输入账号"},
        {"placeholder": "用户名"},
        {"placeholder": "请输入工号"},
        {"name": "empno"},
        {"class": "login-input"},        # class 也参与启发式
    ]
    for attrs in cases:
        assert browser._score_username_field(attrs) > 0, attrs


def test_score_password_never_username():
    # 即使 name 叫 username，type=password 也绝不当用户名框
    attrs = {"type": "password", "name": "username", "id": "username"}
    assert browser._score_username_field(attrs) < 0


def test_score_excluded_fields_never_username():
    cases = [
        {"type": "checkbox", "name": "username"},
        {"type": "hidden", "name": "userId"},
        {"type": "radio", "name": "loginType"},
        {"name": "captcha"},
        {"name": "smsCode"},
        {"name": "verifyCode"},
        {"placeholder": "请输入验证码"},
        {"placeholder": "短信校验码"},
        {},                              # 无任何特征
    ]
    for attrs in cases:
        assert browser._score_username_field(attrs) == 0, attrs


# ------------------------------------------------------- 填充目标决策（纯）


def test_pick_typical_form():
    fields = [_field(name="username"), _field(type="password", name="password")]

    assert browser._pick_frame_fill_targets(fields) == (0, 1)


def test_pick_heuristic_skips_captcha_before_username():
    fields = [
        _field(name="captcha"),
        _field(id="loginName"),
        _field(type="password", name="password"),
    ]

    assert browser._pick_frame_fill_targets(fields) == (1, 2)


def test_pick_fallback_first_text_before_password():
    # 无任何启发式命中：fallback 到密码框之前第一个可见文本输入框
    fields = [
        _field(name="mobile"),
        _field(type="password", name="password"),
    ]

    assert browser._pick_frame_fill_targets(fields) == (0, 1)


def test_pick_fallback_skips_hidden_and_checkbox():
    fields = [
        _field(type="hidden", name="userToken"),
        _field(type="checkbox", name="agree"),
        _field(name="realname"),
        _field(type="password", name="password"),
    ]

    assert browser._pick_frame_fill_targets(fields) == (2, 3)


def test_pick_fallback_never_captcha():
    # 密码框之前只有验证码输入框时，绝不能把用户名填进验证码框
    fields = [
        _field(name="smsCode"),
        _field(type="password", name="password"),
    ]

    assert browser._pick_frame_fill_targets(fields) == (None, 1)


def test_pick_fallback_only_before_password():
    # 密码框之后的文本框不进 fallback
    fields = [
        _field(type="password", name="password"),
        _field(name="nickname"),
    ]

    assert browser._pick_frame_fill_targets(fields) == (None, 0)


def test_pick_only_fills_blank_fields():
    # 人已输入的字段绝不覆盖
    fields = [
        _field(name="username", value="human-typed"),
        _field(type="password", name="password"),
    ]

    assert browser._pick_frame_fill_targets(fields) == (None, 1)

    fields = [
        _field(name="username"),
        _field(type="password", name="password", value="human-typed"),
    ]

    assert browser._pick_frame_fill_targets(fields) == (0, None)


def test_pick_ignores_invisible_and_disabled():
    fields = [
        _field(name="username", visible=False),
        _field(name="username2", disabled=True),
        _field(type="password", name="password", readonly=True),
        _field(type="password", name="password2"),
    ]

    assert browser._pick_frame_fill_targets(fields) == (None, 3)


# ------------------------------------------------------- _autofill_once 运行时


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
        self._calls.append((self._element["name"] or self._element["id"], value))
        self._element["value"] = value


class _FakeLocatorList:
    def __init__(self, frame):
        self._frame = frame

    def nth(self, index):
        return _FakeLocator(self._frame.elements[index], self._frame.calls)


class _FakeFrame:
    """fake Playwright frame：evaluate 返回元素描述快照（模拟 iframe 表单）。"""

    def __init__(self, elements):
        self.elements = elements
        self.calls = []

    def evaluate(self, _js):
        return [dict(el) for el in self.elements]

    def locator(self, _selector):
        return _FakeLocatorList(self)


class _BoomFrame:
    """模拟导航中/已销毁的 frame：evaluate 抛错。"""

    def evaluate(self, *_args, **_kwargs):
        raise RuntimeError("Execution context was destroyed")


class _FakePage:
    def __init__(self, frames):
        self.frames = frames


class _FakeCtx:
    def __init__(self, pages):
        self.pages = pages


def test_autofill_once_fills_username_and_password_once():
    frame = _FakeFrame([
        _field(name="username"),
        _field(type="password", name="password"),
    ])
    ctx = _FakeCtx([_FakePage([frame])])
    state = {"username": False, "password": False, "warns": 0}

    browser._autofill_once(ctx, FAKE_CREDS, state)
    assert state["username"] and state["password"]
    assert frame.calls == [("username", FAKE_USERNAME),
                           ("password", FAKE_PASSWORD)]

    # 两字段各成功填过一次后停止再试（已填值不被二次填充覆盖）
    frame.elements[0]["value"] = ""
    frame.elements[1]["value"] = ""
    browser._autofill_once(ctx, FAKE_CREDS, state)
    assert frame.calls == [("username", FAKE_USERNAME),
                           ("password", FAKE_PASSWORD)]


def test_autofill_once_never_overwrites_human_input():
    frame = _FakeFrame([
        _field(name="username", value="human-typed"),
        _field(type="password", name="password"),
    ])
    ctx = _FakeCtx([_FakePage([frame])])
    state = {"username": False, "password": False, "warns": 0}

    browser._autofill_once(ctx, FAKE_CREDS, state)

    # 只填了空的密码框；用户名框保持人输入的值
    assert [name for name, _value in frame.calls] == ["password"]
    assert frame.elements[0]["value"] == "human-typed"


def test_autofill_once_traverses_frames_and_swallows_errors():
    good = _FakeFrame([
        _field(id="loginName"),
        _field(type="password", name="password"),
    ])
    ctx = _FakeCtx([
        _FakePage([_BoomFrame(), _FakeFrame([_field(name="captcha")])]),
        _FakePage([good]),
    ])
    state = {"username": False, "password": False, "warns": 0}

    browser._autofill_once(ctx, FAKE_CREDS, state)  # 绝不能抛异常

    assert state["username"] and state["password"]
    assert good.calls == [("loginName", FAKE_USERNAME),
                          ("password", FAKE_PASSWORD)]


def test_autofill_once_no_login_form_is_noop():
    ctx = _FakeCtx([_FakePage([_FakeFrame([_field(name="search")])])])
    state = {"username": False, "password": False, "warns": 0}

    browser._autofill_once(ctx, FAKE_CREDS, state)

    assert not state["username"] and not state["password"]


def test_autofill_once_warn_limited_on_fill_failure(capsys):
    class _FailFillLocator(_FakeLocator):
        def fill(self, value, timeout=None):
            raise TimeoutError("fill timeout")

    class _FailFillFrame(_FakeFrame):
        def locator(self, _selector):
            return _FailFillLocatorList(self)

    class _FailFillLocatorList(_FakeLocatorList):
        def nth(self, index):
            return _FailFillLocator(self._frame.elements[index],
                                    self._frame.calls)

    frame = _FailFillFrame([
        _field(name="username"),
        _field(type="password", name="password"),
    ])
    ctx = _FakeCtx([_FakePage([frame])])
    state = {"username": False, "password": False, "warns": 0}

    # 连续多轮失败：绝不抛异常，WARN 最多 AUTOFILL_MAX_WARNS 次
    for _ in range(browser.AUTOFILL_MAX_WARNS + 3):
        browser._autofill_once(ctx, FAKE_CREDS, state)

    assert state["warns"] == browser.AUTOFILL_MAX_WARNS + 3
    err = capsys.readouterr().err
    assert err.count("WARN：自动填充尝试失败") == browser.AUTOFILL_MAX_WARNS


# ------------------------------------------------------------ cmd_login 集成


class _FakeLoginProc:
    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


class _FakeLoginPlaywright:
    def stop(self):
        pass


class _FakeLoginBrowser:
    def __init__(self, ctx):
        self.contexts = [ctx]

    def close(self):
        pass


class _PollContext:
    """fake CDP 上下文：前 n_polls_empty 次无 TGC，之后返回 TGC。"""

    def __init__(self, n_polls_empty=1, pages=()):
        self._n_polls_empty = n_polls_empty
        self._calls = 0
        self.pages = list(pages)

    def cookies(self):
        self._calls += 1
        if self._calls <= self._n_polls_empty:
            return []
        return [{"name": "TGC", "value": "tgc-fresh",
                 "domain": "sso.cnncecp.com"}]


def _patch_login_env(monkeypatch, tmp_path, ctx):
    monkeypatch.setattr(
        browser, "_launch_chrome_cdp",
        lambda url=None, headless=False, *, user_data_dir=None: (
            _FakeLoginProc(), _FakeLoginBrowser(ctx), _FakeLoginPlaywright()))
    monkeypatch.setattr(browser, "STORAGE_STATE",
                        tmp_path / "cnnc_auth_storage_state.json")
    monkeypatch.setattr(browser.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(browser.shutil, "rmtree", lambda *_a, **_k: None)


def test_cmd_login_autofill_off_when_credentials_missing(
    monkeypatch, tmp_path, capsys
):
    """凭证缺失：打一行 AUTO_FILL_OFF，保持原有 TGC 轮询行为不变。"""
    monkeypatch.setattr(browser, "CREDENTIALS_FILE", tmp_path / "missing.json")
    ctx = _PollContext(n_polls_empty=0)  # 首轮即有 TGC（覆盖原成功路径）
    _patch_login_env(monkeypatch, tmp_path, ctx)

    exit_code = browser.cmd_login(types.SimpleNamespace())

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "AUTO_FILL_OFF" in out
    assert "LOGIN_OK" in out


def test_cmd_login_autofill_enabled_polls_without_pages(
    monkeypatch, tmp_path, capsys
):
    """凭证存在：零 pages 上下文（登录页尚未渲染）下循环照常运转并登录成功。"""
    creds_path = _write_creds(tmp_path, {
        "username": FAKE_USERNAME, "password": FAKE_PASSWORD,
        "auto_fill_enabled": True,
    })
    monkeypatch.setattr(browser, "CREDENTIALS_FILE", creds_path)
    ctx = _PollContext(n_polls_empty=1, pages=[])
    _patch_login_env(monkeypatch, tmp_path, ctx)

    exit_code = browser.cmd_login(types.SimpleNamespace())

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "AUTO_FILL_OFF" not in out
    assert "LOGIN_OK" in out
    payload = json.loads(
        (tmp_path / "cnnc_auth_storage_state.json").read_text(encoding="utf-8"))
    assert {c["name"] for c in payload["cookies"]} == {"TGC"}


def test_cmd_login_source_never_clicks_or_submits_form():
    """源码级断言：自动填充绝不包含点击/提交动作。"""
    import inspect
    source = inspect.getsource(browser.cmd_login)
    assert "click(" not in source
    assert "press(" not in source
    assert "check(" not in source
    assert "AUTO_FILL_OFF" in source
    assert "_autofill_once" in source
