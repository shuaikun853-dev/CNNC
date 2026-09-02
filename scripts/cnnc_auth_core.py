# -*- coding: utf-8 -*-
"""CNNC 生产认证 provider 核心模块。

CNNC 无 refresh-token API，刷新链为：
    1. subprocess 运行 ``python scripts/cnnc_auth_browser.py harvest`` 收割一次性
       OC 授权码（stdout 单行 ``OC=<code>``；浏览器内同源交换成功时在 OC= 行后
       追加单行 ``AT=<token>``；SSO 会话失效时 exit 2）。
    2. harvest 未带回 AT 时：urllib POST
       ``/cnnc-gateway/pm/n/anon/account/login``（body ``{"code": OC}``，
       带 Content-Type/mk-request/Accept/Origin/Referer，无 cookie、无 WAF）
       换取新 AT（响应 ``data.token``，兼容顶层 ``token``）。2026-09-02 实测：
       午后 WAF 对裸 Python 请求返回 502，浏览器内同源 fetch 返回
       200/success:true，故浏览器带回的 AT 优先、跳过本步裸交换。
    3. 用只读业务查询验证新 AT 有效后，原子写入 state 并同步三个业务 cURL 文件。

所有依赖（harvest/exchange/verify/opener/路径/时钟）均可注入，测试完全 hermetic。
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

try:
    from scripts.auth_manager import _payload_is_auth_failure
    from scripts.curl_utils import (
        apply_curl_headers,
        internal_direct_opener,
        load_curl_command,
    )
except ModuleNotFoundError:  # Direct invocation from scripts/.
    from auth_manager import _payload_is_auth_failure
    from curl_utils import apply_curl_headers, internal_direct_opener, load_curl_command


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = ROOT / "data" / "cnnc_auth_state.json"
DEFAULT_LEDGER_PATH = ROOT / "data" / "cnnc_auth_ledger.jsonl"
CURL_SYNC_FILES = (
    ROOT / "data" / "latest_cnnc_query.curl",
    ROOT / "data" / "detail_request.curl",
    ROOT / "data" / "cnnc_biaozhun.curl",
)

ANON_LOGIN_URL = "https://one.cnncecp.com/cnnc-gateway/pm/n/anon/account/login"
VERIFY_FALLBACK_URL = (
    "https://one.cnncecp.com/emp/onecode/improxy/iio2/"
    "quasiStandardCommodityDocumentModel/query"
)
VERIFY_FALLBACK_HEADERS: list[tuple[str, str]] = [
    ("Content-Type", "application/json"),
    ("Accept", "application/json, text/plain, */*"),
    ("mk-request", "41"),
    ("Origin", "https://one.cnncecp.com"),
    ("Referer", "https://one.cnncecp.com/cnnc-web/"),
]

HARVEST_TIMEOUT_SECONDS = 180.0
EXCHANGE_TIMEOUT_SECONDS = 30.0
VERIFY_TIMEOUT_SECONDS = 30.0
CURL_BACKUP_KEEP = 5

# SSO 断路器：harvest 失败（SSO 会话失效）后的冷却窗口，避免每个 tick 都弹浏览器。
SSO_RETRY_COOLDOWN_SECONDS = 900.0
# preflight 缓存窗口：verify 通过后 6 小时内不再发网络验证请求。
PREFLIGHT_MAX_VERIFY_AGE_SECONDS = 6 * 3600.0

AT_PATTERN = r"AT-\d+-[A-Za-z0-9]+"
AT_RE = re.compile(AT_PATTERN)
AT_COOKIE_RE = re.compile(r"token=" + AT_PATTERN)

_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class CnncAuthStatus:
    """状态机常量（Dashboard 与日志共用）。"""

    OK = "CNNC_AUTH_OK"
    REFRESHED = "CNNC_AUTH_REFRESHED"
    REFRESH_IN_PROGRESS = "CNNC_AUTH_REFRESH_IN_PROGRESS"
    SSO_LOGIN_REQUIRED = "SSO_LOGIN_REQUIRED"
    WAF_CHALLENGE_FAILED = "WAF_CHALLENGE_FAILED"
    BROWSER_START_FAILED = "BROWSER_START_FAILED"
    BROWSER_CONNECT_FAILED = "BROWSER_CONNECT_FAILED"
    OC_NOT_CAPTURED = "OC_NOT_CAPTURED"
    OC_EXCHANGE_FAILED = "OC_EXCHANGE_FAILED"
    AT_VERIFY_FAILED = "AT_VERIFY_FAILED"
    CREDENTIAL_PUBLISH_FAILED = "CREDENTIAL_PUBLISH_FAILED"
    CURL_SYNC_FAILED = "CURL_SYNC_FAILED"
    AUTH_RETRY_FAILED = "AUTH_RETRY_FAILED"
    # §16 新增状态码
    AUTH_REFRESH_DEADLINE_EXCEEDED = "AUTH_REFRESH_DEADLINE_EXCEEDED"
    CNNC_WAF_CONTEXT_STALE = "CNNC_WAF_CONTEXT_STALE"
    LOGIN_RECOVERY_FAILED = "LOGIN_RECOVERY_FAILED"


class CnncAuthError(RuntimeError):
    """带状态码的 CNNC 认证异常。"""

    def __init__(self, status: str, message: str = "") -> None:
        super().__init__(message or status)
        self.status = status


def redact_secret(value: str) -> str:
    """脱敏：保留前 7 字符 + ``***``（如 AT-128937-xxx → AT-128***）。"""
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 7:
        return "***"
    return value[:7] + "***"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_to_epoch(value: str) -> float | None:
    """ISO 时间转 epoch 秒；解析失败返回 None（调用方按过期处理）。"""
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _thread_lock(lock_path: Path) -> threading.RLock:
    key = str(lock_path.resolve())
    with _LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _process_lock(
    path: Path, *, wait_seconds: float, monotonic: Callable[[], float]
) -> Iterator[None]:
    """进程级文件锁（1 字节），限时等待。

    Windows 用 msvcrt LK_LOCK（单次阻塞至多 ~10 秒后抛 OSError），POSIX 用
    flock 非阻塞轮询；两者都在循环里受 wait_seconds 截止时间约束，超时抛
    REFRESH_IN_PROGRESS。msvcrt 文件锁随进程死亡自动释放，死进程无需清理。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = monotonic() + max(0.0, float(wait_seconds))
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        locked = False
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if monotonic() >= deadline:
                        raise CnncAuthError(
                            CnncAuthStatus.REFRESH_IN_PROGRESS, "刷新锁等待超时"
                        ) from None
                    time.sleep(0.2)
            yield
        finally:
            if locked:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def inject_auth(request: urllib.request.Request, at: str) -> None:
    """注入 AT：``access-token`` 头 + Cookie 中 ``token=AT-...`` 子串替换。

    无 Cookie 头则不添加。
    """
    request.headers["access-token"] = at
    cookie = ""
    for key, value in request.headers.items():
        if str(key).lower() == "cookie":
            cookie = str(value or "")
            break
    if cookie:
        replaced = AT_COOKIE_RE.sub(f"token={at}", cookie)
        if replaced != cookie:
            request.headers["Cookie"] = replaced


class CnncAuthProvider:
    """CNNC 生产认证 provider：harvest → OC 换 AT → 验证 → 发布 → 同步 cURL。

    注入契约（双通道）：``harvest_func(timeout_seconds: float)``，provider 恒以
    关键字调用；``timeout_seconds`` 为本次收割的超时预算，已由调用方按剩余
    deadline 截断。返回值二选一：
      - ``str``：仅 OC（旧契约，注入实现两者皆可）→ provider 走外部裸交换；
      - ``tuple[str, str | None]``：``(oc, browser_at)``，``browser_at`` 为
        harvest 浏览器内同源交换得到的 AT（未带回为 None）→ ``browser_at``
        非空且形如 ``AT-…`` 时跳过外部裸交换，直接验证并发布该 AT。
    exchange_func/verify_func 契约不变（``timeout`` 关键字）。
    """

    def __init__(
        self,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        ledger_path: Path = DEFAULT_LEDGER_PATH,
        curl_files: tuple[Path, ...] = CURL_SYNC_FILES,
        harvest_func: Callable[..., object] | None = None,
        exchange_func: Callable[..., str] | None = None,
        verify_func: Callable[..., bool] | None = None,
        opener: Any | None = None,
        browser_script: Path | None = None,
        logger: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        lock_wait_seconds: float = 120.0,
    ) -> None:
        self.state_path = Path(state_path)
        self.ledger_path = Path(ledger_path)
        self.curl_files = tuple(curl_files)
        self.opener = opener if opener is not None else internal_direct_opener()
        self.browser_script = (
            Path(browser_script)
            if browser_script is not None
            else ROOT / "scripts" / "cnnc_auth_browser.py"
        )
        self.logger = logger
        self.monotonic = monotonic
        self.lock_wait_seconds = float(lock_wait_seconds)
        self._harvest = harvest_func or self._default_harvest
        self._exchange = exchange_func or self._default_exchange
        self._verify = verify_func or self._default_verify

    # ------------------------------------------------------------------ 默认依赖

    def _default_harvest(
        self, timeout_seconds: float = HARVEST_TIMEOUT_SECONDS
    ) -> tuple[str, str | None]:
        """收割 OC（及浏览器内交换的 AT，若有）；对超时/未捕获（exit=5）做一次
        有界重试。

        返回 ``(oc, browser_at)`` 双通道：stdout 带 ``AT=`` 行时 ``browser_at``
        为 harvest 浏览器内同源交换得到的 AT，缺失时为 None（provider 回退
        外部裸交换）。实测门户 302 链偶发停滞（WAF 挑战抖动），单次重试即可
        恢复；SSO_LOGIN_REQUIRED 立即上抛不重试（铁律：不反复碰失效会话）。
        ``timeout_seconds`` 为调用方按剩余 deadline 截断后的 subprocess 超时
        预算；预算耗尽（<=0）时不再做第二次重试（deadline 契约）。
        """
        last_exit: int | None = None
        for attempt in range(2):
            if attempt == 1 and timeout_seconds <= 0.0:
                break  # 无剩余时间预算，不做第二次重试
            try:
                completed = subprocess.run(
                    [sys.executable, str(self.browser_script), "harvest"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt == 0:
                    continue
                raise CnncAuthError(
                    CnncAuthStatus.BROWSER_START_FAILED,
                    f"harvest 子进程超过 {timeout_seconds:.0f}s 未返回",
                ) from exc
            stderr = completed.stderr or ""
            if completed.returncode == 2 or "SSO_LOGIN_REQUIRED" in stderr:
                raise CnncAuthError(
                    CnncAuthStatus.SSO_LOGIN_REQUIRED,
                    "SSO 会话已失效，请先运行 cnnc_auth_browser.py login",
                )
            if completed.returncode == 4:
                raise CnncAuthError(
                    CnncAuthStatus.BROWSER_START_FAILED,
                    "storage_state 不存在，请先运行 cnnc_auth_browser.py login",
                )
            stdout = completed.stdout or ""
            match = re.search(r"^OC=(.+)$", stdout, re.MULTILINE)
            if match:
                at_match = re.search(r"^AT=(.+)$", stdout, re.MULTILINE)
                browser_at = at_match.group(1).strip() if at_match else None
                return match.group(1).strip(), browser_at
            last_exit = completed.returncode
            if attempt == 0:
                continue  # 一次有界重试
        raise CnncAuthError(
            CnncAuthStatus.OC_NOT_CAPTURED,
            f"harvest 未捕获 OC（exit={last_exit}）",
        )

    def _default_exchange(self, oc: str, timeout: float = EXCHANGE_TIMEOUT_SECONDS) -> str:
        """OC 换 AT；对网关瞬态（HTTP 502/503/504、网络抖动）做一次有界重试。

        实测 anon login 端点偶发 502（网关侧瞬时错误）；业务性失败
        （success!=true、4xx、无 token）不重试——OC 可能已被消费。
        """
        body = json.dumps({"code": oc}).encode("utf-8")
        request = urllib.request.Request(ANON_LOGIN_URL, data=body, method="POST")
        apply_curl_headers(
            request,
            [
                ("Content-Type", "application/json"),
                ("Accept", "application/json, text/plain, */*"),
                ("mk-request", "41"),
                ("Origin", "https://one.cnncecp.com"),
                ("Referer", "https://one.cnncecp.com/cnnc-web/"),
            ],
        )
        payload: Any = None
        for attempt in range(2):
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8", errors="replace"))
                break
            except urllib.error.HTTPError as exc:
                if attempt == 0 and exc.code in {502, 503, 504}:
                    # timeout 已被调用方按剩余 deadline 截断，重试 sleep 同步受限
                    time.sleep(min(1.0, max(0.0, timeout)))
                    continue
                raise CnncAuthError(
                    CnncAuthStatus.OC_EXCHANGE_FAILED, f"OC 换 AT 失败: HTTP {exc.code}"
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == 0:
                    time.sleep(min(1.0, max(0.0, timeout)))
                    continue
                raise CnncAuthError(
                    CnncAuthStatus.OC_EXCHANGE_FAILED,
                    f"OC 换 AT 请求失败: {type(exc).__name__}",
                ) from None
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise CnncAuthError(
                    CnncAuthStatus.OC_EXCHANGE_FAILED, "OC 换 AT 响应不是有效 JSON"
                ) from exc
        if not isinstance(payload, dict):
            raise CnncAuthError(CnncAuthStatus.OC_EXCHANGE_FAILED, "OC 换 AT 响应格式错误")
        if "success" in payload and payload.get("success") is not True:
            raise CnncAuthError(
                CnncAuthStatus.OC_EXCHANGE_FAILED, "OC 换 AT 接口返回 success!=true"
            )
        data = payload.get("data")
        token = str(data.get("token") or "").strip() if isinstance(data, dict) else ""
        if not token:
            token = str(payload.get("token") or "").strip()
        if not token or not AT_RE.fullmatch(token):
            raise CnncAuthError(
                CnncAuthStatus.OC_EXCHANGE_FAILED, "OC 换 AT 响应未包含有效 AT"
            )
        return token

    def _default_verify(self, at: str, timeout: float = VERIFY_TIMEOUT_SECONDS) -> bool:
        try:
            with self.opener.open(self._build_verify_request(at), timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError:
            return False
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return False
        if not isinstance(payload, dict) or _payload_is_auth_failure(payload):
            return False
        return str(payload.get("rspCode") or "") == "0000" and isinstance(
            payload.get("rows"), list
        )

    def _build_verify_request(self, at: str) -> urllib.request.Request:
        """构造只读验证请求：优先用业务查询 cURL 作模板，缺失时用固定 URL。"""
        parsed: dict[str, Any] | None = None
        if self.curl_files:
            template = Path(self.curl_files[0])
            if template.exists():
                try:
                    parsed = load_curl_command(template)
                except Exception:
                    parsed = None
        if parsed and parsed.get("url"):
            data = str(parsed.get("data") or "")
            request = urllib.request.Request(
                str(parsed["url"]),
                data=data.encode("utf-8") if data else None,
                method="POST" if data else "GET",
            )
            # Cookie 必须一并套用：CNNC 网关同时校验 cookie 里的 token=AT-...
            # （实测只带 access-token 头不带 cookie → rspCode 3333；WAF 的 K5
            # cookie 也在其中）。inject_auth 随后会把 token= 子串替换为新 AT。
            apply_curl_headers(
                request,
                parsed.get("headers") or [],
                str(parsed.get("cookie") or ""),
            )
        else:
            request = urllib.request.Request(VERIFY_FALLBACK_URL, method="GET")
            apply_curl_headers(request, VERIFY_FALLBACK_HEADERS)
            # 无模板时至少带上 token cookie，否则网关 3333
            apply_curl_headers(request, [], f"token={at}")
        inject_auth(request, at)
        return request

    # ------------------------------------------------------------------ 锁

    def _lock_path(self) -> Path:
        return self.state_path.with_name(f".{self.state_path.name}.refresh.lock")

    @contextmanager
    def _locked(self, *, wait_seconds: float | None = None) -> Iterator[None]:
        lock_path = self._lock_path()
        with _thread_lock(lock_path):
            with _process_lock(
                lock_path,
                wait_seconds=(
                    self.lock_wait_seconds if wait_seconds is None else wait_seconds
                ),
                monotonic=self.monotonic,
            ):
                self._write_lock_meta(lock_path)
                try:
                    yield
                finally:
                    self._remove_lock_meta(lock_path)

    @staticmethod
    def _write_lock_meta(lock_path: Path) -> None:
        """锁元数据（尽力而为）：死进程锁自动释放，活进程由等待超时兜底。"""
        try:
            meta = {
                "owner": platform.node(),
                "pid": os.getpid(),
                "started_at": _utc_now_iso(),
            }
            lock_path.with_name(lock_path.name + ".meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def _remove_lock_meta(lock_path: Path) -> None:
        try:
            lock_path.with_name(lock_path.name + ".meta.json").unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ state IO

    def _read_state(self) -> dict[str, Any] | None:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _atomic_write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f"{self.state_path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.state_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _failure_mutator(status: str, message: str) -> Callable[[dict[str, Any]], None]:
        """构造失败落盘 mutator：只写失败元数据，绝不触碰 access_token。"""

        def mutator(state: dict[str, Any]) -> None:
            state.setdefault("schema", 1)
            state["last_status"] = status
            state["last_error"] = message
            # 失败时刻：SSO 冷却断路器的起算点（修复旧版误用 last_refresh_at）
            state["last_failure_at"] = _utc_now_iso()

        return mutator

    def _mutate_state_locked(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        """持锁版 state 读-改-写：锁内重读最新 state → mutator(state) → 原子写。

        调用者必须已持有 self._locked()。绝不可在持锁时经由 mutate_state 再次
        进锁——Windows msvcrt 文件锁跨句柄不可重入，重入会超时/死锁。
        """
        state = self._read_state() or {}
        mutator(state)
        self._atomic_write_state(state)

    def mutate_state(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        """锁外 state 元数据修改统一入口：取 self._locked() 后走 locked 版。

        锁内重读最新 state 再改写，消除锁外 read-modify-write 的并发 lost
        update（旧 AT 覆盖新 AT）。mutator 只应修改元数据字段。
        """
        with self._locked():
            self._mutate_state_locked(mutator)

    def _record_failure(self, status: str, message: str) -> None:
        """锁外失败落盘入口（不覆盖 access_token），尽力而为。"""
        try:
            self.mutate_state(self._failure_mutator(status, message))
        except OSError:
            pass

    def _record_failure_event(self, exc: CnncAuthError) -> None:
        """refresh 持锁路径专用失败落盘（_mutate_state_locked，不再重入锁）。"""
        try:
            self._mutate_state_locked(self._failure_mutator(exc.status, str(exc)))
        except OSError:
            pass
        try:
            self.ledger(exc.status, error=str(exc))
        except OSError:
            pass

    # ------------------------------------------------------------------ 对外 API

    def _remaining_seconds(self, deadline_monotonic: float | None) -> float:
        """距绝对 deadline（monotonic 基准）的剩余秒数；未设 deadline 返回 +inf。"""
        if deadline_monotonic is None:
            return float("inf")
        return float(deadline_monotonic) - self.monotonic()

    def refresh(
        self,
        stale_access_token: str | None = None,
        *,
        force: bool = False,
        deadline_monotonic: float | None = None,
    ) -> str:
        """刷新链全程：harvest → OC 换 AT → 验证 → 发布 → 同步 cURL。返回新 AT。

        双通道：harvest 浏览器内同源交换成功时直接带回 AT（返回 tuple 且
        ``browser_at`` 形如 AT-…），跳过外部裸 HTTP 交换（2026-09-02 实测：
        午后 WAF 对裸 Python 请求返回 502）；否则回退 exchange_func。

        SSO 冷却断路器：上次刷新失败于 SSO_LOGIN_REQUIRED 且 ``last_failure_at``
        距今仍在冷却窗口内时，直接抛 CnncAuthError(SSO_LOGIN_REQUIRED)，不
        harvest（避免每 tick 弹浏览器）。``force=True`` 跳过冷却（人工显式重试）。

        deadline 契约（absolute deadline）：``deadline_monotonic`` 为
        ``time.monotonic()`` 基准的绝对截止时刻。入口已过期立即抛
        AUTH_REFRESH_DEADLINE_EXCEEDED（不做任何动作）；进程锁等待与
        harvest/exchange/verify 超时均按剩余时间截断；每步之间复查，耗尽即抛。
        """
        remaining = self._remaining_seconds(deadline_monotonic)
        if remaining <= 0.0:
            raise CnncAuthError(
                CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED,
                "刷新 deadline 已过，未执行任何刷新动作",
            )
        with self._locked(wait_seconds=min(self.lock_wait_seconds, remaining)):
            state = self._read_state()
            current = str((state or {}).get("access_token") or "").strip()
            if (
                stale_access_token is not None
                and current
                and current != stale_access_token
            ):
                return current  # 其他线程/进程已完成刷新（双检）
            if (
                not force
                and self._sso_cooldown_remaining_seconds(state) > 0.0
            ):
                raise CnncAuthError(
                    CnncAuthStatus.SSO_LOGIN_REQUIRED,
                    "SSO 冷却中，跳过刷新（15 分钟内已失败）",
                )
            try:
                remaining = self._remaining_seconds(deadline_monotonic)
                if remaining <= 0.0:
                    raise CnncAuthError(
                        CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED,
                        "刷新 deadline 在锁等待后已耗尽",
                    )
                harvest_result = self._harvest(
                    timeout_seconds=min(HARVEST_TIMEOUT_SECONDS, remaining)
                )
                if isinstance(harvest_result, tuple):
                    oc, browser_at = harvest_result
                else:
                    oc, browser_at = harvest_result, None  # 兼容仅返回 OC 的注入实现
                remaining = self._remaining_seconds(deadline_monotonic)
                if remaining <= 0.0:
                    raise CnncAuthError(
                        CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED,
                        "刷新 deadline 在 harvest 后已耗尽",
                    )
                if browser_at and AT_RE.fullmatch(str(browser_at)):
                    # 浏览器内同源交换已成功：跳过外部裸交换（午后 WAF 会对裸
                    # Python 请求返回 502），直接验证并发布浏览器带回的 AT
                    at = str(browser_at)
                    exchange_channel = "browser"
                else:
                    at = self._exchange(
                        oc, timeout=min(EXCHANGE_TIMEOUT_SECONDS, remaining)
                    )
                    exchange_channel = "http"
                remaining = self._remaining_seconds(deadline_monotonic)
                if remaining <= 0.0:
                    raise CnncAuthError(
                        CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED,
                        "刷新 deadline 在 OC 换 AT 后已耗尽",
                    )
                if not self._verify(
                    at, timeout=min(VERIFY_TIMEOUT_SECONDS, remaining)
                ):
                    raise CnncAuthError(
                        CnncAuthStatus.AT_VERIFY_FAILED, "新 AT 业务只读验证未通过，放弃发布"
                    )
            except CnncAuthError as exc:
                self._record_failure_event(exc)
                raise
            now = _utc_now_iso()
            try:
                self._atomic_write_state(
                    {
                        "schema": 1,
                        "access_token": at,
                        "obtained_at": now,
                        "source": "harvest",
                        "last_status": CnncAuthStatus.REFRESHED,
                        "last_refresh_at": now,
                        "last_failure_at": "",  # 成功发布清空失败时刻，解除冷却
                        "last_verify_at": "",
                        "last_error": "",
                    }
                )
            except OSError as exc:
                failure = CnncAuthError(
                    CnncAuthStatus.CREDENTIAL_PUBLISH_FAILED,
                    f"认证状态写入失败: {type(exc).__name__}",
                )
                self._record_failure_event(failure)
                raise failure from exc
            synced: list[str] = []
            try:
                synced = self.sync_curl_files(at)
            except CnncAuthError as exc:
                # state 已发布、运行时已可用：cURL 同步失败不视为整体失败
                try:
                    self.ledger(
                        CnncAuthStatus.CURL_SYNC_FAILED,
                        at=redact_secret(at),
                        error=str(exc),
                    )
                except OSError:
                    pass
                self._log(f"cURL 文件同步失败（不影响运行时）: {exc}")
            try:
                self.ledger(
                    "CNNC_AUTH_REFRESHED",
                    oc=redact_secret(oc),
                    at=redact_secret(at),
                    files=len(synced),
                    exchange=exchange_channel,
                )
            except OSError:
                pass
            self._log(f"refresh success, files={len(synced)}")
            return at

    def _sso_cooldown_remaining_seconds(self, state: dict[str, Any] | None = None) -> float:
        """当前 SSO 刷新冷却剩余秒数（自 state.last_failure_at 起算）；无冷却为 0。

        旧 state 无 last_failure_at 或解析失败 → 视为冷却已过期，不拦截（一次
        过渡兼容；旧版误用 last_refresh_at，昨成功今失败时冷却恒判过期）。
        """
        state = state if state is not None else self._read_state()
        if (
            str((state or {}).get("last_status") or "").strip()
            != CnncAuthStatus.SSO_LOGIN_REQUIRED
        ):
            return 0.0
        epoch = _iso_to_epoch(str((state or {}).get("last_failure_at") or ""))
        if epoch is None:
            return 0.0  # 解析失败/旧 state 无该字段：视为已过期，不拦截
        return max(0.0, SSO_RETRY_COOLDOWN_SECONDS - (time.time() - epoch))

    def preflight_check(self) -> str:
        """发现/任务前的轻量预检：返回状态字符串，绝不抛异常，默认不弹浏览器。

        - 无 access_token → 仅 bootstrap（get_access_token，非 refresh）；
        - SSO 冷却未过 → 直接返回 SSO_LOGIN_REQUIRED，不做任何动作；
        - verify 距今 < 6 小时且 last_status==OK → 零网络调用返回 OK；
        - 否则 verify 一次，失败才尝试 refresh（受冷却断路器约束）。
        """
        try:
            state = self._read_state()
            if not str((state or {}).get("access_token") or "").strip():
                try:
                    self.get_access_token()  # 仅 bootstrap，不触发 refresh
                except Exception as exc:  # noqa: BLE001 — 预检绝不抛异常
                    try:
                        self._record_failure(
                            CnncAuthStatus.BROWSER_START_FAILED,
                            f"preflight bootstrap 失败: {type(exc).__name__}",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return CnncAuthStatus.BROWSER_START_FAILED
                state = self._read_state()
            if self._sso_cooldown_remaining_seconds(state) > 0.0:
                return CnncAuthStatus.SSO_LOGIN_REQUIRED
            last_status = str((state or {}).get("last_status") or "").strip()
            verify_epoch = _iso_to_epoch(str((state or {}).get("last_verify_at") or ""))
            if (
                last_status == CnncAuthStatus.OK
                and verify_epoch is not None
                and (time.time() - verify_epoch) < PREFLIGHT_MAX_VERIFY_AGE_SECONDS
            ):
                return CnncAuthStatus.OK
            current = str((state or {}).get("access_token") or "").strip()
            if current and self._verify(current):
                self._mark_verify_ok()
                return CnncAuthStatus.OK
            try:
                self.refresh()
            except CnncAuthError as exc:
                return exc.status  # state 已由 refresh 落失败状态
            except Exception:  # noqa: BLE001 — 预检绝不抛异常
                return CnncAuthStatus.AUTH_RETRY_FAILED
            return CnncAuthStatus.REFRESHED
        except Exception:  # noqa: BLE001 — 预检绝不抛异常
            return CnncAuthStatus.AUTH_RETRY_FAILED

    def get_access_token(self) -> str:
        state = self._read_state()
        token = str((state or {}).get("access_token") or "").strip()
        if token:
            return token
        with self._locked():
            state = self._read_state()
            token = str((state or {}).get("access_token") or "").strip()
            if token:
                return token
            for path in self.curl_files:
                candidate = Path(path)
                if not candidate.exists():
                    continue
                try:
                    match = AT_RE.search(
                        candidate.read_text(encoding="utf-8", errors="replace")
                    )
                except OSError:
                    continue
                if not match:
                    continue
                at = match.group(0)
                now = _utc_now_iso()
                self._atomic_write_state(
                    {
                        "schema": 1,
                        "access_token": at,
                        "obtained_at": now,
                        "source": "bootstrap_from_curl",
                        "last_status": CnncAuthStatus.OK,
                        "last_refresh_at": "",
                        "last_failure_at": "",
                        "last_verify_at": "",
                        "last_error": "",
                    }
                )
                self._log(f"bootstrap access_token from {candidate.name}")
                return at
        raise CnncAuthError(
            CnncAuthStatus.BROWSER_START_FAILED, "无任何 CNNC 凭证，请先 login"
        )

    def request_json_cnnc(
        self,
        request_factory: Callable[[], urllib.request.Request],
        *,
        read_only: bool,
        opener: Any | None = None,
        timeout: float = 60,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        """带认证的业务 JSON 请求：只读请求允许一次「刷新 + 重试」。

        deadline 契约：``deadline_monotonic``（monotonic 基准绝对截止）透传给
        内部 refresh，每次 HTTP 尝试的超时也被剩余时间截断，耗尽即抛
        AUTH_REFRESH_DEADLINE_EXCEEDED。
        WAF 语义（§14）：HTTP 412 = WAF 上下文失效拦截，≠ AT 失效——立即抛
        CNNC_WAF_CONTEXT_STALE，不刷新不重试（禁止误导性续签循环）；
        401 与 payload 认证失败行为不变。
        """
        direct = opener if opener is not None else self.opener
        attempts = 2 if read_only else 1
        for attempt in range(attempts):
            remaining = self._remaining_seconds(deadline_monotonic)
            if remaining <= 0.0:
                raise CnncAuthError(
                    CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED,
                    "业务请求 deadline 已耗尽",
                )
            at = self.get_access_token()
            stale = at
            request = request_factory()
            inject_auth(request, at)
            try:
                with direct.open(request, timeout=min(timeout, remaining)) as response:
                    payload = json.loads(
                        response.read().decode("utf-8", errors="replace")
                    )
            except urllib.error.HTTPError as exc:
                if exc.code == 412:
                    # WAF 拦截 ≠ AT 失效：续签解决不了 412，立即上抛
                    raise CnncAuthError(
                        CnncAuthStatus.CNNC_WAF_CONTEXT_STALE,
                        "WAF 上下文失效（HTTP 412），需重新建立 WAF 会话",
                    ) from exc
                if read_only and attempt == 0 and exc.code == 401:
                    self._log("AT 被拒（HTTP 401），刷新后重试一次")
                    self.refresh(
                        stale_access_token=stale,
                        deadline_monotonic=deadline_monotonic,
                    )
                    continue
                raise
            if not isinstance(payload, dict):
                raise CnncAuthError(
                    CnncAuthStatus.AUTH_RETRY_FAILED, "业务接口响应必须是 JSON 对象"
                )
            if _payload_is_auth_failure(payload):
                if read_only and attempt == 0:
                    self._log("AT 被拒（rspCode 认证失败），刷新后重试一次")
                    self.refresh(
                        stale_access_token=stale,
                        deadline_monotonic=deadline_monotonic,
                    )
                    continue
                raise CnncAuthError(
                    CnncAuthStatus.AUTH_RETRY_FAILED, "CNNC 认证失败，刷新重试后仍未恢复"
                )
            return payload
        raise CnncAuthError(
            CnncAuthStatus.AUTH_RETRY_FAILED, "CNNC 认证失败，刷新重试后仍未恢复"
        )

    def ensure_valid(self, *, timeout: float = VERIFY_TIMEOUT_SECONDS) -> None:
        state = self._read_state()
        current = str((state or {}).get("access_token") or "").strip()
        if not current:
            self.refresh()
            return
        if self._verify(current, timeout=timeout):
            self._mark_verify_ok()
            return
        self.refresh()

    def get_status(self) -> dict[str, Any]:
        state = self._read_state() or {}
        token = str(state.get("access_token") or "").strip()
        last_status = str(state.get("last_status") or "").strip()
        if last_status:
            status = last_status
        elif token or self._bootstrap_available():
            status = CnncAuthStatus.OK
        else:
            status = CnncAuthStatus.SSO_LOGIN_REQUIRED
        return {
            "status": status,
            "access_token_redacted": redact_secret(token),
            "last_refresh_at": str(state.get("last_refresh_at") or ""),
            "last_verify_at": str(state.get("last_verify_at") or ""),
            "last_error": str(state.get("last_error") or ""),
            "login_required": status == CnncAuthStatus.SSO_LOGIN_REQUIRED,
            "cooldown_remaining_seconds": round(
                self._sso_cooldown_remaining_seconds(state), 3
            ),
        }

    # ------------------------------------------------------------------ cURL 同步

    def sync_curl_files(self, new_at: str) -> list[str]:
        """把新 AT 同步进业务 cURL 文件：tmp 全部就绪后再逐个原子替换。"""
        plan: list[tuple[Path, Path]] = []  # (目标文件, tmp)
        tmp_paths: list[Path] = []
        try:
            for path in self.curl_files:
                target = Path(path)
                if not target.exists():
                    continue
                text = target.read_bytes().decode("utf-8", errors="replace")
                new_text, count = AT_RE.subn(new_at, text)
                if count == 0:
                    continue
                tmp = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
                tmp.write_bytes(new_text.encode("utf-8"))
                tmp_paths.append(tmp)
                load_curl_command(tmp)  # 校验 tmp 仍可解析
                if new_at not in tmp.read_text(encoding="utf-8", errors="replace"):
                    raise CnncAuthError(
                        CnncAuthStatus.CURL_SYNC_FAILED, f"tmp 未包含新 AT: {target.name}"
                    )
                plan.append((target, tmp))
        except CnncAuthError:
            self._cleanup_tmps(tmp_paths)
            raise
        except (OSError, ValueError) as exc:
            self._cleanup_tmps(tmp_paths)
            raise CnncAuthError(
                CnncAuthStatus.CURL_SYNC_FAILED,
                f"cURL 同步准备失败: {type(exc).__name__}",
            ) from exc
        if not plan:
            return []

        replaced: list[tuple[Path, bytes]] = []
        stamp = time.strftime("%Y%m%d%H%M%S")
        try:
            for target, tmp in plan:
                current = target.read_bytes()
                target.with_name(f"{target.name}.bak_{stamp}").write_bytes(current)
                os.replace(tmp, target)
                replaced.append((target, current))
                self._prune_backups(target)
        except OSError as exc:
            for target, content in replaced:
                try:
                    target.write_bytes(content)
                except OSError:
                    pass
            self._cleanup_tmps([tmp for _target, tmp in plan if tmp.exists()])
            raise CnncAuthError(
                CnncAuthStatus.CURL_SYNC_FAILED,
                f"cURL 替换失败，已回滚: {type(exc).__name__}",
            ) from exc
        return [str(target) for target, _tmp in plan]

    @staticmethod
    def _cleanup_tmps(tmp_paths: list[Path]) -> None:
        for tmp in tmp_paths:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _prune_backups(target: Path) -> None:
        try:
            backups = sorted(target.parent.glob(f"{target.name}.bak_*"))
            for stale in backups[:-CURL_BACKUP_KEEP]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ 其他

    def ledger(self, event: str, **fields: Any) -> None:
        """追加 jsonl 审计事件（字段值必须已脱敏）。"""
        entry: dict[str, Any] = {"ts": _utc_now_iso(), "event": event}
        entry.update(fields)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()

    def _bootstrap_available(self) -> bool:
        for path in self.curl_files:
            candidate = Path(path)
            if not candidate.exists():
                continue
            try:
                if AT_RE.search(candidate.read_text(encoding="utf-8", errors="replace")):
                    return True
            except OSError:
                continue
        return False

    def _mark_verify_ok(self) -> None:
        """verify 通过后更新元数据（mutate_state 锁内重读，防并发 lost update）。

        mutator 只改 last_verify_at/last_status 元数据字段、绝不写 access_token：
        即使持锁重读时发现 AT 已被其他线程/进程刷新，也不会用旧值覆盖新 AT
        （Finding 2 修复的关键约束）。
        """

        def mutator(state: dict[str, Any]) -> None:
            state["last_verify_at"] = _utc_now_iso()
            state["last_status"] = CnncAuthStatus.OK

        if self._read_state() is None:
            return  # 无可读 state：保持原行为，不凭空创建 state
        self.mutate_state(mutator)

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(f"[CNNC-Auth] {message}")


def _force_utf8_stdio() -> None:
    """避免中文输出在 Windows 管道场景下因本地代码页触发 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run_selftest() -> int:
    """离线自测（不走网络/浏览器）：tmp 目录 + 注入 fake 依赖验证核心链路。

    覆盖：refresh 成功发布（last_failure_at 清空）、SSO 失败落 last_failure_at、
    冷却断路器按 last_failure_at 起算（Finding 1 回归）+ force 绕过、旧 state
    无 last_failure_at 的一次过渡兼容、deadline 已过立即抛且不 harvest、
    harvest 双通道 tuple 返回跳过外部交换（exchange=browser）与 str 返回旧
    路径仍走外部交换（exchange=http）。
    """
    oc = "OC-selftest-abcdef123456"
    new_at = "AT-20260902-selftestxyz"
    old_at = "AT-990101-oldtoken99"

    calls = {"harvest": 0}

    def fake_harvest(timeout_seconds: float = 0.0) -> str:
        calls["harvest"] += 1
        assert timeout_seconds > 0.0, "harvest 超时预算必须为正"
        return oc

    def fake_exchange(code: str, timeout: float = 0.0) -> str:
        assert code == oc, "exchange 收到的 OC 不符"
        return new_at

    def fake_verify(at: str, timeout: float = 0.0) -> bool:
        return at == new_at

    def sso_harvest(timeout_seconds: float = 0.0) -> str:
        raise CnncAuthError(
            CnncAuthStatus.SSO_LOGIN_REQUIRED, "SSO 会话已失效（selftest）"
        )

    def make_provider(
        harvest=fake_harvest,
        exchange=fake_exchange,
        verify=fake_verify,
        name="main",
    ) -> CnncAuthProvider:
        return CnncAuthProvider(
            state_path=tmp_path / f"{name}_state.json",
            ledger_path=tmp_path / f"{name}_ledger.jsonl",
            curl_files=(),
            harvest_func=harvest,
            exchange_func=exchange,
            verify_func=verify,
        )

    def read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def refreshed_events(name: str) -> list[dict]:
        path = tmp_path / f"{name}_ledger.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        return [
            json.loads(line)
            for line in lines
            if line.strip() and json.loads(line).get("event") == "CNNC_AUTH_REFRESHED"
        ]

    with tempfile.TemporaryDirectory(prefix="cnnc_auth_core_selftest_") as tmp:
        tmp_path = Path(tmp)

        # ---- 场景 1：refresh 成功发布，last_failure_at 清空
        ok_provider = make_provider(name="ok")
        at = ok_provider.refresh(force=True)
        assert at == new_at, "refresh 应返回新 AT"
        state = read_json(tmp_path / "ok_state.json")
        assert state["access_token"] == new_at, "state 未发布新 AT"
        assert state["last_status"] == CnncAuthStatus.REFRESHED
        assert state["last_failure_at"] == "", "成功发布必须清空 last_failure_at"
        assert state["last_refresh_at"], "缺少 last_refresh_at"
        assert calls["harvest"] == 1, "harvest 调用次数不符"
        ok_refreshed = refreshed_events("ok")
        assert ok_refreshed and ok_refreshed[-1].get("exchange") == "http", (
            "str 返回（仅 OC）旧路径的 ledger 应记 exchange=http"
        )

        # ---- 场景 2：SSO 失败路径，失败时刻写入 last_failure_at
        sso_provider = make_provider(harvest=sso_harvest, name="sso")
        try:
            sso_provider.refresh()
        except CnncAuthError as exc:
            assert exc.status == CnncAuthStatus.SSO_LOGIN_REQUIRED, (
                "SSO 路径状态码不符: %s" % exc.status
            )
        else:
            raise AssertionError("SSO 失效未抛 CnncAuthError")
        sso_state = read_json(tmp_path / "sso_state.json")
        assert sso_state["last_status"] == CnncAuthStatus.SSO_LOGIN_REQUIRED, (
            "失败状态未落 state"
        )
        failure_epoch = _iso_to_epoch(str(sso_state.get("last_failure_at") or ""))
        assert failure_epoch is not None and abs(time.time() - failure_epoch) < 60, (
            "失败未写 last_failure_at 或时刻异常: %r" % sso_state.get("last_failure_at")
        )

        # ---- 场景 3：冷却断路器按 last_failure_at 起算（Finding 1 回归）
        now_iso = datetime.now(timezone.utc).isoformat()
        (tmp_path / "cooldown_state.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "access_token": old_at,
                    "obtained_at": now_iso,
                    "source": "harvest",
                    "last_status": CnncAuthStatus.SSO_LOGIN_REQUIRED,
                    "last_refresh_at": now_iso,
                    "last_failure_at": now_iso,
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
        except CnncAuthError as exc:
            assert exc.status == CnncAuthStatus.SSO_LOGIN_REQUIRED, (
                "断路器拦截状态码不符: %s" % exc.status
            )
        else:
            raise AssertionError("冷却窗口内的非 force 刷新未被断路器拦截")
        assert calls["harvest"] == 1, "断路器拦截时不应调用 harvest"
        assert cd_provider.get_status()["cooldown_remaining_seconds"] > 0.0, (
            "cooldown_remaining_seconds 应以 last_failure_at 起算"
        )

        # ---- 场景 4：旧 state 无 last_failure_at → 冷却视为过期（过渡兼容）
        legacy = read_json(tmp_path / "cooldown_state.json")
        legacy.pop("last_failure_at")
        (tmp_path / "legacy_state.json").write_text(
            json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
        )
        legacy_provider = make_provider(name="legacy")
        at2 = legacy_provider.refresh()  # 不拦截，正常走完刷新
        assert at2 == new_at, "旧 state（无 last_failure_at）不应被拦截"
        assert calls["harvest"] == 2, "过渡兼容路径应实际调用 harvest"

        # ---- 场景 5：deadline 已过 → 入口立即抛，harvest 未被调用
        deadline_provider = make_provider(name="deadline")
        try:
            deadline_provider.refresh(deadline_monotonic=time.monotonic() - 0.001)
        except CnncAuthError as exc:
            assert exc.status == CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED, (
                "deadline 场景状态码不符: %s" % exc.status
            )
        else:
            raise AssertionError("过期 deadline 未抛 AUTH_REFRESH_DEADLINE_EXCEEDED")
        assert calls["harvest"] == 2, "deadline 已过时不应调用 harvest"
        assert not (tmp_path / "deadline_state.json").exists(), (
            "deadline 已过时不应写任何 state"
        )

        # ---- 场景 6：harvest 返回 (OC, AT) 双通道 → 跳过外部交换，发布浏览器 AT
        browser_at = "AT-20260902-browser01"
        assert AT_RE.fullmatch(browser_at), "用例前提：browser_at 须符合 AT 正则"
        exchange_calls: list[str] = []

        def tuple_harvest(timeout_seconds: float = 0.0):
            calls["harvest"] += 1
            assert timeout_seconds > 0.0, "harvest 超时预算必须为正"
            return oc, browser_at

        def counting_exchange(code: str, timeout: float = 0.0) -> str:
            exchange_calls.append(code)
            return new_at

        def verify_browser_or_http(at: str, timeout: float = 0.0) -> bool:
            return at in (new_at, browser_at)

        browser_provider = make_provider(
            harvest=tuple_harvest,
            exchange=counting_exchange,
            verify=verify_browser_or_http,
            name="browser",
        )
        at3 = browser_provider.refresh(force=True)
        assert at3 == browser_at, "双通道应直接发布浏览器内交换的 AT"
        assert exchange_calls == [], "浏览器内交换成功时不得再走外部裸交换"
        state = read_json(tmp_path / "browser_state.json")
        assert state["access_token"] == browser_at, "state 未发布浏览器 AT"
        assert state["last_status"] == CnncAuthStatus.REFRESHED
        br_refreshed = refreshed_events("browser")
        assert br_refreshed and br_refreshed[-1].get("exchange") == "browser", (
            "双通道路径的 ledger 应记 exchange=browser"
        )

    print("SELFTEST OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = list(sys.argv if argv is None else argv)
    command = args[1] if len(args) > 1 else "status"
    if command == "--selftest":
        return run_selftest()
    if command != "status":
        print(f"未知命令: {command}（仅支持 status / --selftest）", file=sys.stderr)
        return 2
    try:
        payload = CnncAuthProvider().get_status()
    except Exception as exc:  # noqa: BLE001 — status 子命令绝不 crash
        payload = {
            "status": CnncAuthStatus.SSO_LOGIN_REQUIRED,
            "access_token_redacted": "",
            "last_refresh_at": "",
            "last_verify_at": "",
            "last_error": type(exc).__name__,
            "login_required": True,
        }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
