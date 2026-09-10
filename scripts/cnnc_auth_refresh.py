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
    python scripts/cnnc_auth_refresh.py --refresh-curl
    python scripts/cnnc_auth_refresh.py --status
    python scripts/cnnc_auth_refresh.py --ensure
    python scripts/cnnc_auth_refresh.py --login-and-recover
    python scripts/cnnc_auth_refresh.py --selftest

退出码（--harvest/--refresh-curl/--login-and-recover 共用一套操作语义）:
    0     成功（--harvest 兼容输出旧格式 REFRESH_OK 行）
    1     其他 CnncAuthError（stderr 打印状态码）
    2     SSO 会话失效（SSO_LOGIN_REQUIRED，需先
          python scripts/cnnc_auth_browser.py login）；无参数用法错误同为 2
    4     认证刷新成功但期望 cURL 同步不完整
          （AUTH_REFRESH_OK_CURL_SYNC_FAILED；stdout/结果工件只报文件名，
          绝不冒充登录失败 rc=1）
    5     已有 CNNC 认证维护流程（重新登录恢复/cURL 刷新）在运行
          （跨进程认证维护锁被持有；零副作用退出）
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
from zoneinfo import ZoneInfo

try:
    from scripts import cnnc_auth_core as core
    from scripts.curl_utils import load_curl_command
except ImportError:  # Direct invocation from scripts/（含命名空间 scripts 冲突）.
    import cnnc_auth_core as core
    from curl_utils import load_curl_command


ROOT = Path(__file__).resolve().parents[1]
# 手动恢复结果工件（--login-and-recover 各 outcome 都写；status 路由只读合并）
LOGIN_RECOVERY_RESULT_PATH = ROOT / "data" / "cnnc_login_recovery_result.json"
# cURL 独立刷新操作结果工件（run_curl_refresh 各 outcome 都写；2026-09-10）
CURL_REFRESH_RESULT_PATH = ROOT / "data" / "cnnc_curl_refresh_result.json"
# 跨进程 single-flight 认证维护锁路径（GPT Review Round 2 Blocker 3 +
# 2026-09-10 统一维护锁语义）：--login-and-recover 与 --refresh-curl/
# --harvest 互斥共用同一把锁，禁止并发第二个认证维护进程。复用
# daily_audit_store.ProcessLock（msvcrt/fcntl 单字节 OS 锁：进程退出自动
# 释放、inspect 探活区分"文件存在"vs"锁被持有"），不自造文件锁。
AUTH_MAINTENANCE_LOCK_PATH = ROOT / "data" / ".cnnc_login_recovery.lock"
# 兼容别名（dashboard_api/既有测试按旧名引用；与维护锁同一路径同值）：
LOGIN_RECOVERY_LOCK_PATH = AUTH_MAINTENANCE_LOCK_PATH
try:
    from app.services.daily_audit_store import LeaseUnavailable, ProcessLock
except ImportError:  # 直接运行时 sys.path[0]=scripts，补 ROOT 后重导
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.services.daily_audit_store import LeaseUnavailable, ProcessLock


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
    """人工强制刷新（--harvest）：委托 run_curl_refresh(trigger="manual")。

    与 --refresh-curl 共享同一操作实现（认证维护锁 + force 刷新 + 严格 cURL
    后置核验 + 结果工件），禁止两份刷新链。兼容：成功时在 CURL_REFRESH_OK
    行后追加旧格式 REFRESH_OK 行（AT 脱敏、last_refresh_at 取自刷新后状态，
    既有运维 grep 不破）。
    """
    provider = _default_provider()
    rc = run_curl_refresh(provider=provider, trigger="manual")
    if rc == 0:
        status = provider.get_status()
        print(
            "REFRESH_OK at=%s last_refresh_at=%s"
            % (status.get("access_token_redacted") or "",
               status.get("last_refresh_at") or "")
        )
    return rc


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


def _shanghai_today() -> str:
    """业务日期权威值：Asia/Shanghai 时区今日（YYYY-MM-DD；禁用 utcnow）。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def _atomic_write_json(path, payload) -> None:
    """结果工件共用的唯一原子写实现：tmp + os.replace 原子替换。

    OSError 尽力而为（含残留 tmp 清理），绝不影响调用方的退出码语义。
    """
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


def _write_recovery_result(path, outcome, *, at="", curl_expected=0,
                           curl_synced=0, curl_failed_files=()) -> None:
    """写手动恢复结果工件（观测用，绝不含完整凭证）。"""
    payload = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "curl_expected": int(curl_expected),
        "curl_synced": int(curl_synced),
        "curl_failed_files": list(curl_failed_files),
        "at_redacted": core.redact_secret(at),
    }
    _atomic_write_json(path, payload)


def _write_curl_refresh_result(path, outcome, *, at="", curl_expected=0,
                               curl_synced=0, curl_failed_files=(),
                               trigger="manual", business_date="") -> None:
    """写 cURL 刷新操作结果工件（观测用，绝不含完整凭证）。"""
    payload = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "business_date": business_date,
        "trigger": trigger,
        "outcome": outcome,
        "curl_expected": int(curl_expected),
        "curl_synced": int(curl_synced),
        "curl_failed_files": list(curl_failed_files),
        "at_redacted": core.redact_secret(at),
    }
    _atomic_write_json(path, payload)


def run_login_and_recover(login_runner=None, provider=None, result_path=None,
                          lock_path=None) -> int:
    """登录后自动恢复（review Finding 3）：login 成功 → force 刷新 → 发布
    → 期望 cURL 同步严格后置核验（2026-09-10）。

    函数最外层先获取跨进程 single-flight OS 锁（GPT Review Round 2
    Blocker 3，2026-09-10）：复用 daily_audit_store.ProcessLock，锁被他人
    持有（LeaseUnavailable）→ stderr 打 LOGIN_RECOVERY_ALREADY_RUNNING 并
    返回 5，绝不调用 login_runner/不写状态/不写工件；成功获取后
    try/finally 保证 login 失败/SSO 失效/cURL 部分失败/任何异常路径最终
    lock.release()（进程退出亦由 OS 自动释放）。

    login_runner/provider/result_path/lock_path 可注入（测试 seam，锁路径
    全部 tmp 注入）；默认 login_runner 阻塞等待人工完成滑块/短信，默认工件
    路径 data/cnnc_login_recovery_result.json，默认锁路径
    data/.cnnc_login_recovery.lock。
    返回码：
        0  login 成功、refresh(force=True) 发布成功且期望 cURL 全部同步
           （stdout 打 RECOVERY_OK，AT 已脱敏）
        1  login 失败（状态落 LOGIN_RECOVERY_FAILED）或其他刷新失败
        2  refresh 抛 SSO_LOGIN_REQUIRED（失败状态已由 provider 落盘）
        4  认证刷新成功但期望 cURL 同步不完整（stdout 只报文件名；绝不把
           cURL 失败冒充登录失败 rc=1）
        5  已有恢复流程在运行（LOGIN_RECOVERY_ALREADY_RUNNING，零副作用）

    refresh 内部保持宽松语义（cURL 同步失败只记 CURL_SYNC_FAILED 台账不
    阻断自动续签）；核验只发生在手动恢复入口并如实上报。五种 outcome 都写
    结果工件（RECOVERY_OK / AUTH_REFRESH_OK_CURL_SYNC_FAILED /
    LOGIN_RECOVERY_FAILED / REFRESH_FAILED，尽力而为不影响返回码；rc=5 不
    写）。全程不打印完整凭证。
    """
    lock = ProcessLock(lock_path or AUTH_MAINTENANCE_LOCK_PATH)
    try:
        lock.acquire(owner={
            "pid": os.getpid(),
            "command": "login-and-recover",
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
    except LeaseUnavailable:
        print(
            "LOGIN_RECOVERY_ALREADY_RUNNING：上一个重新登录恢复流程仍在进行，"
            "请完成当前登录窗口",
            file=sys.stderr,
        )
        return 5
    try:
        provider = provider if provider is not None else _default_provider()
        login_runner = (login_runner if login_runner is not None
                        else _default_login_runner)
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
        outcome = ("AUTH_REFRESH_OK_CURL_SYNC_FAILED" if failed
                   else "RECOVERY_OK")
        _write_recovery_result(result_path, outcome, at=at,
                               curl_expected=expected, curl_synced=len(synced),
                               curl_failed_files=failed)
        if failed:
            print(
                "AUTH_REFRESH_OK_CURL_SYNC_FAILED curl_synced=%d/%d failed=%s"
                % (len(synced), expected, ",".join(failed))
            )
            return 4
        print("RECOVERY_OK at=%s curl_synced=%d"
              % (core.redact_secret(at), len(synced)))
        return 0
    finally:
        lock.release()


def run_curl_refresh(provider=None, result_path=None, lock_path=None,
                     trigger="manual", business_date=None) -> int:
    """独立 cURL 刷新操作（--refresh-curl / --harvest 共享的唯一实现）。

    与 login-and-recover 互斥共用同一把跨进程认证维护锁
    （AUTH_MAINTENANCE_LOCK_PATH；旧名 LOGIN_RECOVERY_LOCK_PATH 为同值别名）：
    锁被持有（LeaseUnavailable）→ stderr 打 AUTH_MAINTENANCE_BUSY 并返回 5，
    绝不调用 refresh/不写工件（零副作用）；成功获取后 try/finally 保证
    SSO 失效/cURL 部分失败/任何异常路径最终 lock.release()（进程退出亦由
    OS 自动释放）。

    provider/result_path/lock_path/trigger/business_date 可注入（测试 seam，
    锁与工件路径全部 tmp 注入）；默认 provider 为 _default_provider()，默认
    工件 data/cnnc_curl_refresh_result.json。force 刷新后执行严格后置核验
    （复用 _verify_curl_sync 同一核验器，绝不复制第二套核验器；refresh 内部
    宽松语义不动）。

    business_date 权威值：参数优先；缺省用 Asia/Shanghai 今日
    （_shanghai_today，禁用 utcnow）。trigger：manual | daily-preflight。
    返回码：
        0  refresh(force=True) 成功且期望 cURL 全部同步（CURL_REFRESH_OK）
        1  其他 CnncAuthError（AUTH_REFRESH_FAILED；状态已由 provider 落盘）
        2  refresh 抛 SSO_LOGIN_REQUIRED（提示先重新登录；工件
           at_redacted=""、cURL 计数 0）
        4  刷新成功但期望 cURL 同步不完整（AUTH_REFRESH_OK_CURL_SYNC_FAILED，
           stdout/工件只报文件名）
        5  维护锁被持（零副作用）

    各 outcome 都写结果工件（completed_at/business_date/trigger/outcome/
    cURL 计数/at_redacted，原子写尽力而为不影响返回码；rc=5 不写）。
    全程不打印完整凭证。
    """
    lock = ProcessLock(lock_path or AUTH_MAINTENANCE_LOCK_PATH)
    try:
        lock.acquire(owner={
            "pid": os.getpid(),
            "command": "curl-refresh",
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
    except LeaseUnavailable:
        print(
            "AUTH_MAINTENANCE_BUSY：CNNC 认证维护（重新登录/cURL 刷新）"
            "正在进行，请先完成当前操作",
            file=sys.stderr,
        )
        return 5
    try:
        provider = provider if provider is not None else _default_provider()
        result_path = (Path(result_path) if result_path is not None
                       else CURL_REFRESH_RESULT_PATH)
        effective_business_date = business_date or _shanghai_today()
        try:
            at = provider.refresh(force=True)
        except core.CnncAuthError as exc:
            # 失败状态/审计已由 provider 落盘，这里只提示并按状态码定退出码
            if exc.status == core.CnncAuthStatus.SSO_LOGIN_REQUIRED:
                _write_curl_refresh_result(
                    result_path, "SSO_LOGIN_REQUIRED",
                    trigger=trigger, business_date=effective_business_date)
                print(
                    "SSO_LOGIN_REQUIRED：SSO 登录态已失效，请点击重新登录",
                    file=sys.stderr,
                )
                return 2
            _write_curl_refresh_result(
                result_path, "AUTH_REFRESH_FAILED",
                trigger=trigger, business_date=effective_business_date)
            print("%s: %s" % (exc.status, exc), file=sys.stderr)
            return 1
        # 严格后置核验：与 login-and-recover 复用同一核验器；无 curl_files
        # 属性的 duck-type provider 期望清单为空 = 全同步（既有语义不破）。
        curl_files = getattr(provider, "curl_files", ()) or ()
        synced, failed = _verify_curl_sync(at, curl_files)
        expected = len(synced) + len(failed)
        if failed:
            _write_curl_refresh_result(
                result_path, "AUTH_REFRESH_OK_CURL_SYNC_FAILED", at=at,
                curl_expected=expected, curl_synced=len(synced),
                curl_failed_files=failed, trigger=trigger,
                business_date=effective_business_date)
            print(
                "AUTH_REFRESH_OK_CURL_SYNC_FAILED curl_synced=%d/%d failed=%s"
                % (len(synced), expected, ",".join(failed))
            )
            return 4
        _write_curl_refresh_result(
            result_path, "CURL_REFRESH_OK", at=at, curl_expected=expected,
            curl_synced=len(synced), trigger=trigger,
            business_date=effective_business_date)
        print("CURL_REFRESH_OK curl_synced=%d/%d" % (len(synced), expected))
        return 0
    finally:
        lock.release()


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
        help="人工强制刷新（force 绕过 SSO 冷却断路器；委托 --refresh-curl "
             "同一操作层，兼容输出旧格式 REFRESH_OK 行）",
    )
    group.add_argument(
        "--refresh-curl",
        action="store_true",
        help="独立 cURL 刷新操作（严格 cURL 后置核验+认证维护锁+结果工件）",
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
    if args.refresh_curl:
        return run_curl_refresh(trigger="manual")
    if args.ensure:
        return cmd_ensure()
    if args.login_and_recover:
        return run_login_and_recover()
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
