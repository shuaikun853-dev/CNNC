# -*- coding: utf-8 -*-
"""scripts/cnnc_auth_browser.py login 命令与自动填充深模块的接线测试（离线）。

自动填充本体（凭证 seam / 精确 Origin 门禁 / 字段启发式 / 限速幂等）已
抽取为深模块 scripts/cnnc_login_autofill.py，其用例见
tests/test_cnnc_login_autofill.py；本文件只保留 cmd_login 集成层：

- 凭证缺失：cmd_login 经真实 create_login_autofiller 打一行 AUTO_FILL_OFF
  且登录流程不受影响
- 凭证存在：零 pages 上下文（登录页未渲染）下循环照常运转并导出
  storage_state
- 接线：循环外创建一次 autofiller，每轮 TGC 判定后调用 tick(ctx)
- cmd_login 源码级断言：绝不包含点击/提交动作

全部用例无真实浏览器、无真实网络；测试凭证均为假值，绝不出现真实密码。
"""

from __future__ import annotations

import inspect
import json
import types

import scripts.cnnc_auth_browser as browser
import scripts.cnnc_login_autofill as autofill

# 假凭证（与真实凭证无关）
FAKE_USERNAME = "autofill-user"
FAKE_PASSWORD = "autofill-pass-fake"


def _write_creds(tmp_path, payload, name="cnnc_login_credentials.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


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
    # 2026-09-15 起 mkdtemp 经 _make_browser_profile_dir 落 BROWSER_TMP_ROOT，指到 tmp_path 保证 hermetic（不写仓库 runtime/）
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", tmp_path / "browser_tmp")
    monkeypatch.setattr(browser.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(browser.shutil, "rmtree", lambda *_a, **_k: None)


def test_cmd_login_autofill_off_when_credentials_missing(
    monkeypatch, tmp_path, capsys
):
    """凭证缺失：打一行 AUTO_FILL_OFF，保持原有 TGC 轮询行为不变。"""
    monkeypatch.setattr(autofill, "CREDENTIALS_FILE", tmp_path / "missing.json")
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
    monkeypatch.setattr(autofill, "CREDENTIALS_FILE", creds_path)
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


def test_cmd_login_wires_autofiller_tick_each_round(monkeypatch, tmp_path):
    """接线：循环外创建一次 autofiller，TGC 判定未命中的轮次调用 tick(ctx)。"""

    class _RecordingAutofiller:
        def __init__(self):
            self.ticks = []

        def tick(self, context):
            self.ticks.append(context)

    recorder = _RecordingAutofiller()
    monkeypatch.setattr(browser, "create_login_autofiller",
                        lambda: recorder)
    ctx = _PollContext(n_polls_empty=1)  # 第 1 轮无 TGC → tick；第 2 轮 TGC → 退出
    _patch_login_env(monkeypatch, tmp_path, ctx)

    exit_code = browser.cmd_login(types.SimpleNamespace())

    assert exit_code == 0
    assert recorder.ticks == [ctx]


def test_cmd_login_survives_when_factory_returns_none(monkeypatch, tmp_path):
    """工厂返回 None（凭证缺失）：循环照常运转直至 TGC。"""
    monkeypatch.setattr(browser, "create_login_autofiller", lambda: None)
    ctx = _PollContext(n_polls_empty=0)
    _patch_login_env(monkeypatch, tmp_path, ctx)

    exit_code = browser.cmd_login(types.SimpleNamespace())

    assert exit_code == 0


def test_cmd_login_source_never_clicks_or_submits_form():
    """源码级断言：login 流程绝不包含点击/提交动作，且经深模块接入。"""
    source = inspect.getsource(browser.cmd_login)
    assert "click(" not in source
    assert "press(" not in source
    assert "check(" not in source
    assert "create_login_autofiller" in source
    assert "autofiller.tick(" in source
