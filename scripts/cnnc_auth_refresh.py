# -*- coding: utf-8 -*-
"""CNNC 认证刷新运维/诊断入口（人工兜底专用）。

注意：日常生产中的认证刷新由 ``scripts/cnnc_auth_core.CnncAuthProvider``
在业务请求路径上自动完成（AT 失效 → harvest OC → 换 AT → 验证 → 发布 →
同步 cURL）。本脚本不再承担每日主流程，仅用于：

    - 诊断：查看认证状态（--status，零网络零浏览器）
    - 人工兜底：SSO 会话存活但 AT 失效时强制刷新（--harvest，force 绕过
      SSO 冷却断路器）
    - 运维预检：发现/任务前确认凭证可用（--ensure）
    - 离线自测：不触碰网络/浏览器验证 provider 刷新链（--selftest）

用法:
    python scripts/cnnc_auth_refresh.py --harvest
    python scripts/cnnc_auth_refresh.py --status
    python scripts/cnnc_auth_refresh.py --ensure
    python scripts/cnnc_auth_refresh.py --selftest

退出码:
    0     成功
    1     其他 CnncAuthError（stderr 打印状态码）
    2     SSO 会话失效（SSO_LOGIN_REQUIRED，需先
          python scripts/cnnc_auth_browser.py login）；无参数用法错误同为 2
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    from scripts import cnnc_auth_core as core
except ImportError:  # Direct invocation from scripts/（含命名空间 scripts 冲突）.
    import cnnc_auth_core as core


def _force_utf8_stdio():
    """避免中文输出在 Windows 管道场景下因本地代码页触发 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _handle_auth_error(exc: core.CnncAuthError) -> int:
    """统一错误出口：SSO 失效 → 提示浏览器登录 exit 2；其他状态码 exit 1。"""
    if exc.status == core.CnncAuthStatus.SSO_LOGIN_REQUIRED:
        print(
            "SSO_LOGIN_REQUIRED：请运行 python scripts/cnnc_auth_browser.py login",
            file=sys.stderr,
        )
        return 2
    print("%s: %s" % (exc.status, exc), file=sys.stderr)
    return 1


def _default_provider() -> core.CnncAuthProvider:
    """默认路径的 provider（state/ledger/cURL 文件均在 data/ 下）。"""
    return core.CnncAuthProvider()


def cmd_harvest() -> int:
    """人工强制刷新：force 绕过 SSO 冷却断路器（人工兜底场景）。

    provider 内部已同步业务 cURL 文件，这里不重复同步；状态详情用 --status。
    """
    provider = _default_provider()
    try:
        at = provider.refresh(force=True)
    except core.CnncAuthError as exc:
        return _handle_auth_error(exc)
    last_refresh_at = provider.get_status().get("last_refresh_at") or ""
    print(
        "REFRESH_OK at=%s last_refresh_at=%s"
        % (core.redact_secret(at), last_refresh_at)
    )
    return 0


def cmd_status() -> int:
    """只读打印认证状态 JSON（读 state/cURL 文件，不发网络请求、不弹浏览器）。"""
    try:
        payload = _default_provider().get_status()
    except Exception as exc:  # noqa: BLE001 — status 子命令绝不 crash
        payload = {
            "status": core.CnncAuthStatus.SSO_LOGIN_REQUIRED,
            "access_token_redacted": "",
            "last_refresh_at": "",
            "last_verify_at": "",
            "last_error": type(exc).__name__,
            "login_required": True,
            "cooldown_remaining_seconds": 0.0,
        }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_ensure() -> int:
    """运维预检：确保凭证可用（必要时刷新），成功打印状态 JSON。"""
    provider = _default_provider()
    try:
        provider.ensure_valid()
    except core.CnncAuthError as exc:
        return _handle_auth_error(exc)
    print(json.dumps(provider.get_status(), ensure_ascii=False))
    return 0


def run_selftest() -> int:
    """离线自测（不走网络/浏览器）：tmp 目录 + 注入 fake 依赖验证刷新链。

    覆盖：refresh 成功路径（state 发布 + ledger 脱敏审计）、SSO 失效路径
    （上抛 + 落失败状态/审计）、SSO 冷却断路器 + force 绕过、stale 双检。
    """
    oc = "OC-selftest-abcdef123456"
    new_at = "AT-20260902-selftestxyz"
    old_at = "AT-990101-oldtoken99"

    calls = {"harvest": 0, "exchange": 0, "verify": 0}

    def fake_harvest() -> str:
        calls["harvest"] += 1
        return oc

    def fake_exchange(code: str, timeout: float = 0.0) -> str:
        calls["exchange"] += 1
        assert code == oc, "exchange 收到的 OC 不符"
        return new_at

    def fake_verify(at: str, timeout: float = 0.0) -> bool:
        calls["verify"] += 1
        return at == new_at

    def sso_harvest() -> str:
        raise core.CnncAuthError(
            core.CnncAuthStatus.SSO_LOGIN_REQUIRED, "SSO 会话已失效（selftest）"
        )

    def make_provider(harvest=fake_harvest, name="main") -> core.CnncAuthProvider:
        return core.CnncAuthProvider(
            state_path=tmp_path / f"{name}_state.json",
            ledger_path=tmp_path / f"{name}_ledger.jsonl",
            curl_files=(),
            harvest_func=harvest,
            exchange_func=fake_exchange,
            verify_func=fake_verify,
        )

    def read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def read_ledger(path: Path) -> list[dict]:
        text = path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    with TemporaryDirectory(prefix="cnnc_auth_selftest_") as tmp:
        tmp_path = Path(tmp)

        # ---- 场景 1：refresh 成功路径（force 不受冷却影响 + state/ledger 断言）
        ok_provider = make_provider(name="ok")
        at = ok_provider.refresh(force=True)
        assert at == new_at, "refresh 应返回新 AT"
        state = read_json(tmp_path / "ok_state.json")
        assert state["access_token"] == new_at, "state 未发布新 AT"
        assert state["last_status"] == core.CnncAuthStatus.REFRESHED
        assert state["source"] == "harvest"
        assert state["last_refresh_at"], "缺少 last_refresh_at"
        assert calls == {"harvest": 1, "exchange": 1, "verify": 1}, (
            "刷新链调用次数不符: %r" % calls
        )
        entries = read_ledger(tmp_path / "ok_ledger.jsonl")
        refreshed = [e for e in entries if e["event"] == "CNNC_AUTH_REFRESHED"]
        assert len(refreshed) == 1, "ledger 缺少 REFRESHED 审计事件"
        assert refreshed[0]["oc"] == core.redact_secret(oc) == oc[:7] + "***", (
            "ledger OC 未按前 7 字符脱敏"
        )
        assert refreshed[0]["at"] == core.redact_secret(new_at) == new_at[:7] + "***", (
            "ledger AT 未按前 7 字符脱敏"
        )
        ledger_text = (tmp_path / "ok_ledger.jsonl").read_text(encoding="utf-8")
        assert oc not in ledger_text and new_at not in ledger_text, (
            "ledger 泄漏完整密钥"
        )

        # ---- 场景 2：SSO 失效路径（上抛 CnncAuthError + 落失败状态/审计）
        sso_provider = make_provider(harvest=sso_harvest, name="sso")
        try:
            sso_provider.refresh()
        except core.CnncAuthError as exc:
            assert exc.status == core.CnncAuthStatus.SSO_LOGIN_REQUIRED, (
                "SSO 路径状态码不符: %s" % exc.status
            )
        else:
            raise AssertionError("SSO 失效未抛 CnncAuthError")
        sso_state = read_json(tmp_path / "sso_state.json")
        assert sso_state["last_status"] == core.CnncAuthStatus.SSO_LOGIN_REQUIRED, (
            "失败状态未落 state"
        )
        assert sso_state.get("last_error"), "失败原因未落 state.last_error"
        sso_entries = read_ledger(tmp_path / "sso_ledger.jsonl")
        assert any(
            e["event"] == core.CnncAuthStatus.SSO_LOGIN_REQUIRED for e in sso_entries
        ), "SSO 失败未记审计"

        # ---- 场景 3：SSO 冷却断路器（非 force 拦截）+ force 绕过 + stale 双检
        now_iso = datetime.now(timezone.utc).isoformat()
        (tmp_path / "cooldown_state.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "access_token": old_at,
                    "obtained_at": now_iso,
                    "source": "harvest",
                    "last_status": core.CnncAuthStatus.SSO_LOGIN_REQUIRED,
                    "last_refresh_at": now_iso,
                    "last_verify_at": "",
                    "last_error": "SSO（selftest 预置冷却窗口）",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        cd_provider = make_provider(name="cooldown")
        try:
            cd_provider.refresh()  # 非 force：冷却窗口内必须直接拒绝
        except core.CnncAuthError as exc:
            assert exc.status == core.CnncAuthStatus.SSO_LOGIN_REQUIRED, (
                "断路器拦截状态码不符: %s" % exc.status
            )
        else:
            raise AssertionError("冷却窗口内的非 force 刷新未被断路器拦截")
        assert calls["harvest"] == 1, "断路器拦截时不应调用 harvest"

        at2 = cd_provider.refresh(force=True)
        assert at2 == new_at, "force 刷新应绕过冷却并成功"
        assert calls["harvest"] == 2, "force 刷新应实际调用 harvest"

        at3 = cd_provider.refresh(stale_access_token=old_at)
        assert at3 == new_at, "stale 双检应返回 state 中的新 AT"
        assert calls["harvest"] == 2, "stale 双检命中时不应再次 harvest"

    print("SELFTEST OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="cnnc_auth_refresh.py",
        description=(
            "CNNC 认证刷新运维/诊断入口。日常生产由 CnncAuthProvider 自动刷新，"
            "本脚本仅用于诊断与人工兜底。"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--harvest",
        action="store_true",
        help="人工强制刷新（force 绕过 SSO 冷却断路器；需浏览器 SSO 会话存活）",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="只读打印认证状态 JSON（零网络、零浏览器）",
    )
    group.add_argument(
        "--ensure",
        action="store_true",
        help="运维预检：确保凭证可用（必要时自动刷新）",
    )
    group.add_argument(
        "--selftest",
        action="store_true",
        help="离线自测（不走网络/浏览器）",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if args.harvest:
        return cmd_harvest()
    if args.ensure:
        return cmd_ensure()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
