# -*- coding: utf-8 -*-
"""CNNC 认证刷新运维/诊断入口（人工兜底专用）。

注意：日常生产中的认证刷新由 ``scripts/cnnc_auth_core.CnncAuthProvider``
在业务请求路径上自动完成（AT 失效 → harvest OC → 换 AT → 验证 → 发布 →
同步 cURL）。本脚本不再承担每日主流程，仅用于：

    - 诊断：查看认证状态（--status，零网络零浏览器）
    - 人工兜底：SSO 会话存活但 AT 失效时强制刷新（--harvest，force 绕过
      SSO 冷却断路器）
    - 运维预检：发现/任务前确认凭证可用（--ensure）
    - 登录后自动恢复（--login-and-recover，Dashboard 重登按钮的后台载荷）：
      阻塞等待浏览器 login 完成 → 自动 harvest → 换 AT → verify → publish
    - 离线自测：不触碰网络/浏览器验证 provider 刷新链（--selftest）

用法:
    python scripts/cnnc_auth_refresh.py --harvest
    python scripts/cnnc_auth_refresh.py --status
    python scripts/cnnc_auth_refresh.py --ensure
    python scripts/cnnc_auth_refresh.py --login-and-recover
    python scripts/cnnc_auth_refresh.py --selftest

退出码:
    0     成功
    1     其他 CnncAuthError（stderr 打印状态码）
    2     SSO 会话失效（SSO_LOGIN_REQUIRED，需先
          python scripts/cnnc_auth_browser.py login）；无参数用法错误同为 2
    4     认证刷新成功但期望 cURL 同步不完整
          （AUTH_REFRESH_OK_CURL_SYNC_FAILED；仅 --login-and-recover，
          stdout/结果工件只报文件名，绝不冒充登录失败 rc=1）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

try:
    from scripts import cnnc_auth_core as core
    from scripts.curl_utils import load_curl_command
except ImportError:  # Direct invocation from scripts/（含命名空间 scripts 冲突）.
    import cnnc_auth_core as core
    from curl_utils import load_curl_command


ROOT = Path(__file__).resolve().parents[1]
# 手动恢复结果工件（--login-and-recover 各 outcome 都写；status 路由只读合并）
LOGIN_RECOVERY_RESULT_PATH = ROOT / "data" / "cnnc_login_recovery_result.json"


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


# login 子进程预算与 cnnc_auth_browser.LOGIN_TIMEOUT_SEC(15 分钟)一致，另加
# 60 秒余量给浏览器启动/会话导出；超时视为登录失败（对应 browser 退出码 3）。
LOGIN_TIMEOUT_SECONDS = 15 * 60
_BROWSER_SCRIPT = Path(__file__).resolve().parent / "cnnc_auth_browser.py"


def _default_login_runner() -> int:
    """阻塞运行浏览器 login 子进程并返回其退出码。

    仅在 --login-and-recover 模式下执行：本进程本身是 Dashboard 拉起的
    detached 后台进程，阻塞等待人工完成滑块/短信是有意行为。超时映射为
    browser login 超时退出码 3，走失败落盘路径。
    """
    try:
        completed = subprocess.run(
            [sys.executable, str(_BROWSER_SCRIPT), "login"],
            timeout=LOGIN_TIMEOUT_SECONDS + 60.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 3
    return int(completed.returncode)


def _login_failed_mutator(rc: int):
    """构造 login 失败落盘 mutator：只写失败元数据，绝不触碰 access_token。"""

    def mutator(state: dict) -> None:
        state["last_status"] = core.CnncAuthStatus.LOGIN_RECOVERY_FAILED
        state["last_error"] = "login exit=%d" % rc
        state["last_failure_at"] = datetime.now(timezone.utc).isoformat()

    return mutator


def _verify_curl_sync(at: str, curl_files) -> tuple[list[str], list[str]]:
    """手动恢复严格后置条件：逐个核验期望 cURL 文件（2026-09-10）。

    对每个期望文件检查：存在、load_curl_command 可解析、内容含新 AT（与
    cnnc_auth_core.sync_curl_files 的注入/自检同语义：AT 明文出现）。任何
    一项不满足即该文件未同步。只读核验，绝不改写文件、绝不动 refresh 内部
    的宽松语义。

    返回 (synced_names, failed_names)；名字只含文件名，绝不含路径或 AT。
    """
    synced: list[str] = []
    failed: list[str] = []
    for path in curl_files or ():
        name = Path(path).name
        try:
            target = Path(path)
            text = target.read_text(encoding="utf-8", errors="replace")
            load_curl_command(target)
            if not at or at not in text:
                raise ValueError("new AT not injected")
        except Exception:  # noqa: BLE001 — 读取/解析/缺失一律按未同步
            failed.append(name)
        else:
            synced.append(name)
    return synced, failed


def _write_recovery_result(path, outcome, *, at="", curl_expected=0,
                           curl_synced=0, curl_failed_files=()) -> None:
    """写手动恢复结果工件（观测用，绝不含完整凭证）。

    tmp + os.replace 原子替换；OSError 尽力而为（含残留 tmp 清理），绝不
    影响退出码语义。
    """
    payload = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "curl_expected": int(curl_expected),
        "curl_synced": int(curl_synced),
        "curl_failed_files": list(curl_failed_files),
        "at_redacted": core.redact_secret(at),
    }
    tmp = None
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def run_login_and_recover(login_runner=None, provider=None, result_path=None) -> int:
    """登录后自动恢复（review Finding 3）：login 成功 → force 刷新 → 发布
    → 期望 cURL 同步严格后置核验（2026-09-10）。

    login_runner/provider/result_path 可注入（测试 seam）；默认 login_runner
    阻塞等待人工完成滑块/短信，默认工件路径 data/cnnc_login_recovery_result.json。
    返回码：
        0  login 成功、refresh(force=True) 发布成功且期望 cURL 全部同步
           （stdout 打 RECOVERY_OK，AT 已脱敏）
        1  login 失败（状态落 LOGIN_RECOVERY_FAILED）或其他刷新失败
        2  refresh 抛 SSO_LOGIN_REQUIRED（失败状态已由 provider 落盘）
        4  认证刷新成功但期望 cURL 同步不完整（stdout 只报文件名；绝不把
           cURL 失败冒充登录失败 rc=1）

    refresh 内部保持宽松语义（cURL 同步失败只记 CURL_SYNC_FAILED 台账不
    阻断自动续签）；核验只发生在手动恢复入口并如实上报。四种 outcome 都写
    结果工件（RECOVERY_OK / AUTH_REFRESH_OK_CURL_SYNC_FAILED /
    LOGIN_RECOVERY_FAILED / REFRESH_FAILED，尽力而为不影响返回码）。全程
    不打印完整凭证。
    """
    provider = provider if provider is not None else _default_provider()
    login_runner = login_runner if login_runner is not None else _default_login_runner
    result_path = (Path(result_path) if result_path is not None
                   else LOGIN_RECOVERY_RESULT_PATH)
    rc = int(login_runner())
    if rc != 0:
        try:
            provider.mutate_state(_login_failed_mutator(rc))
        except OSError:
            pass  # 状态落盘尽力而为，不改变返回码语义
        _write_recovery_result(result_path, "LOGIN_RECOVERY_FAILED")
        print(
            "LOGIN_RECOVERY_FAILED：登录未完成/失败（exit=%d），请重试或手工运行 "
            "python scripts/cnnc_auth_browser.py login" % rc,
            file=sys.stderr,
        )
        return 1
    try:
        at = provider.refresh(force=True)  # force：人工登录显式重试，绕过 SSO 冷却
    except core.CnncAuthError as exc:
        # 失败状态/审计已由 provider 落盘，这里只提示并按状态码定退出码
        _write_recovery_result(result_path, "REFRESH_FAILED")
        print("REFRESH_FAILED %s: %s" % (exc.status, exc), file=sys.stderr)
        return 2 if exc.status == core.CnncAuthStatus.SSO_LOGIN_REQUIRED else 1
    # 严格后置核验（2026-09-10）：真 provider 有 curl_files 属性；无该属性的
    # duck-type provider 期望清单为空 = 全同步（既有调用方语义不破）。
    curl_files = getattr(provider, "curl_files", ()) or ()
    synced, failed = _verify_curl_sync(at, curl_files)
    expected = len(synced) + len(failed)
    outcome = "AUTH_REFRESH_OK_CURL_SYNC_FAILED" if failed else "RECOVERY_OK"
    _write_recovery_result(result_path, outcome, at=at, curl_expected=expected,
                           curl_synced=len(synced), curl_failed_files=failed)
    if failed:
        print(
            "AUTH_REFRESH_OK_CURL_SYNC_FAILED curl_synced=%d/%d failed=%s"
            % (len(synced), expected, ",".join(failed))
        )
        return 4
    print("RECOVERY_OK at=%s curl_synced=%d"
          % (core.redact_secret(at), len(synced)))
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

    def fake_harvest(timeout_seconds: float = 180) -> str:
        calls["harvest"] += 1
        return oc

    def fake_exchange(code: str, timeout: float = 0.0) -> str:
        calls["exchange"] += 1
        assert code == oc, "exchange 收到的 OC 不符"
        return new_at

    def fake_verify(at: str, timeout: float = 0.0) -> bool:
        calls["verify"] += 1
        return at == new_at

    def sso_harvest(timeout_seconds: float = 180) -> str:
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
                    "last_failure_at": now_iso,  # 冷却断路器按该字段起算
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
        "--login-and-recover",
        action="store_true",
        help="Dashboard 重登按钮使用；login 完成→自动 harvest→换 AT→verify→publish",
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
    if args.login_and_recover:
        return run_login_and_recover()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
