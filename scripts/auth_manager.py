from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlencode
from uuid import uuid4

try:
    from scripts.curl_utils import internal_direct_opener, load_curl_command
except ModuleNotFoundError:  # Direct invocation from scripts/.
    from curl_utils import internal_direct_opener, load_curl_command


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = ROOT / "data" / "auth_state.json"
BASIC_AUTH = "Basic c2FiZXI6c2FiZXJfc2VjcmV0"
SYSTEM_CONFIG = {
    "saas": {
        "label": "SaaS",
        "refresh_url": "http://saasportal.iio.com/api/blade-auth/token",
        "curl_sources": (
            ROOT / "data" / "latest_saas_sameAudit_queryAll.curl",
            ROOT / "data" / "saas_zhuangtai.curl",
            ROOT / "data" / "sass-same-lianjie.curl",
            ROOT / "data" / "sass-tongguo.curl",
            ROOT / "data" / "sass-bohui.curl",
        ),
    },
    "zhaoshang": {
        "label": "招商局",
        "refresh_url": "http://zhaosj.iioprod.com/api/blade-auth/token",
        "curl_sources": (
            ROOT / "data" / "latest_zhaoshang_add.curl",
            ROOT / "data" / "latest_zhaoshang_exportTask.curl",
        ),
    },
}


class AuthError(RuntimeError):
    pass


class AuthStateError(AuthError):
    pass


class RefreshTokenExpiredError(AuthError):
    pass


@dataclass(frozen=True)
class AuthTokens:
    access_token: str
    refresh_token: str


_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[tuple[str, str], threading.RLock] = {}


def _thread_lock(state_path: Path, purpose: str) -> threading.RLock:
    key = (str(state_path.resolve()), purpose)
    with _LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _process_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _header_value(headers: dict[str, Any], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    target = name.lower()
    existing = next((key for key in headers if key.lower() == target), name)
    headers[existing] = value


def _set_header_list(
    headers: list[tuple[str, str]], name: str, value: str, *, add: bool
) -> list[tuple[str, str]]:
    target = name.lower()
    result: list[tuple[str, str]] = []
    found = False
    for key, old_value in headers:
        if key.lower() == target:
            result.append((key, value))
            found = True
        else:
            result.append((key, old_value))
    if add and not found:
        result.append((name, value))
    return result


def _updated_cookie(cookie_header: str, tokens: AuthTokens) -> str:
    cookies = SimpleCookie()
    try:
        cookies.load(cookie_header or "")
    except Exception as exc:  # noqa: BLE001
        raise AuthStateError("认证 Cookie 无法安全解析") from exc
    cookies["x-access-token"] = tokens.access_token
    cookies["refreshToken"] = tokens.refresh_token
    return "; ".join(f"{name}={morsel.value}" for name, morsel in cookies.items())


def inject_auth(
    parsed_curl: dict[str, Any], tokens: AuthTokens, system: str
) -> dict[str, Any]:
    if system not in SYSTEM_CONFIG:
        raise ValueError(f"Unsupported auth system: {system}")
    prepared = dict(parsed_curl)
    headers_dict = dict(parsed_curl.get("headers_dict") or {})
    headers_list = list(parsed_curl.get("headers") or [])
    original_cookie = str(
        parsed_curl.get("cookie") or _header_value(headers_dict, "Cookie")
    )
    cookie = _updated_cookie(original_cookie, tokens)
    prepared["cookie"] = cookie
    _set_header(headers_dict, "Cookie", cookie)
    headers_list = _set_header_list(headers_list, "Cookie", cookie, add=bool(headers_list))

    has_blade_auth = bool(_header_value(headers_dict, "Blade-Auth"))
    if system == "zhaoshang" or has_blade_auth:
        blade_auth = f"bearer {tokens.access_token}"
        _set_header(headers_dict, "Blade-Auth", blade_auth)
        headers_list = _set_header_list(
            headers_list, "Blade-Auth", blade_auth, add=system == "zhaoshang"
        )
    prepared["headers_dict"] = headers_dict
    prepared["headers"] = headers_list
    return prepared


def _payload_is_auth_failure(payload: dict[str, Any]) -> bool:
    code = str(
        payload.get("rspCode")
        or payload.get("code")
        or payload.get("status")
        or ""
    ).strip()
    if code in {"401", "3333"}:
        return True
    message = " ".join(
        str(payload.get(key) or "")
        for key in ("rspDesc", "message", "msg", "error", "error_description")
    ).strip().lower()
    if message in {"unauthorized", "未授权", "请重新登录"}:
        return True
    token_terms = ("token", "令牌", "登录态")
    invalid_terms = ("expired", "invalid", "unauthorized", "失效", "过期", "无效")
    return any(term in message for term in token_terms) and any(
        term in message for term in invalid_terms
    )


class AuthManager:
    def __init__(
        self,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        curl_sources: dict[str, tuple[Path, ...]] | None = None,
        opener: Any | None = None,
        logger: Callable[[str], None] | None = print,
    ) -> None:
        self.state_path = Path(state_path)
        self.curl_sources = curl_sources or {
            name: tuple(config["curl_sources"])
            for name, config in SYSTEM_CONFIG.items()
        }
        self.opener = opener or internal_direct_opener()
        self.logger = logger

    def _lock_path(self, purpose: str) -> Path:
        return self.state_path.with_name(f".{self.state_path.name}.{purpose}.lock")

    @contextmanager
    def _locked(self, purpose: str) -> Iterator[None]:
        with _thread_lock(self.state_path, purpose):
            with _process_lock(self._lock_path(purpose)):
                yield

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AuthStateError(f"认证状态文件不存在: {self.state_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthStateError(f"认证状态文件无法读取: {self.state_path}") from exc
        if not isinstance(payload, dict):
            raise AuthStateError("认证状态必须是 JSON 对象")
        return payload

    @staticmethod
    def _tokens_from_state(state: dict[str, Any], system: str) -> AuthTokens:
        item = state.get(system)
        if not isinstance(item, dict):
            raise AuthStateError(f"auth_state.json 缺少 {system} 认证状态，禁止回退 cURL")
        access = str(item.get("access_token") or "").strip()
        refresh = str(item.get("refresh_token") or "").strip()
        if not access or not refresh:
            raise AuthStateError(f"auth_state.json 中 {system} Token 不完整，禁止回退 cURL")
        return AuthTokens(access, refresh)

    @staticmethod
    def _state_item(tokens: AuthTokens) -> dict[str, str]:
        return {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
        }

    def _atomic_write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f"{self.state_path.name}.{uuid4().hex}.tmp"
        )
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

    def _tokens_from_curl(self, system: str) -> AuthTokens:
        for path in self.curl_sources.get(system, ()):
            if not path.exists():
                continue
            parsed = load_curl_command(path)
            cookies = SimpleCookie()
            try:
                cookies.load(
                    str(
                        parsed.get("cookie")
                        or _header_value(parsed.get("headers_dict") or {}, "Cookie")
                    )
                )
            except Exception:
                continue
            access = cookies.get("x-access-token")
            refresh = cookies.get("refreshToken")
            if not access or not refresh or not access.value or not refresh.value:
                continue
            if system == "zhaoshang":
                blade = _header_value(parsed.get("headers_dict") or {}, "Blade-Auth")
                if blade.lower() != f"bearer {access.value}".lower():
                    raise AuthStateError("招商局 cURL 的 Cookie 与 Blade-Auth Token 不一致")
            return AuthTokens(access.value, refresh.value)
        raise AuthStateError(f"未找到可用于首次初始化 {system} 的 cURL Token")

    def _bootstrap_state(self) -> dict[str, Any]:
        state = {
            system: self._state_item(self._tokens_from_curl(system))
            for system in SYSTEM_CONFIG
        }
        self._atomic_write_state(state)
        if self.logger:
            self.logger("[Auth] auth_state.json initialized from immutable cURL templates")
        return state

    def _load_or_bootstrap_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return self._read_state()
        with self._locked("state"):
            if self.state_path.exists():
                return self._read_state()
            return self._bootstrap_state()

    def get_tokens(self, system: str) -> AuthTokens:
        if system not in SYSTEM_CONFIG:
            raise ValueError(f"Unsupported auth system: {system}")
        return self._tokens_from_state(self._load_or_bootstrap_state(), system)

    def _request_refresh(self, system: str, refresh_token: str) -> AuthTokens:
        config = SYSTEM_CONFIG[system]
        url = f"{config['refresh_url']}?{urlencode({'grantType': 'refresh_token', 'refreshToken': refresh_token})}"
        request = urllib.request.Request(url, method="POST")
        request.headers["Authorization"] = BASIC_AUTH
        try:
            with self.opener.open(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 401, 403}:
                raise RefreshTokenExpiredError(
                    f"{config['label']} refreshToken 已失效，需要人工重新登录 {config['label']} 获取新的登录态"
                ) from None
            raise AuthError(f"{config['label']} Token 刷新失败: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AuthError(
                f"{config['label']} Token 刷新失败: {type(exc).__name__}"
            ) from None
        except json.JSONDecodeError:
            raise AuthError(f"{config['label']} Token 刷新响应不是有效 JSON") from None

        if not isinstance(payload, dict):
            raise AuthError(f"{config['label']} Token 刷新响应格式错误")
        if _payload_is_auth_failure(payload):
            raise RefreshTokenExpiredError(
                f"{config['label']} refreshToken 已失效，需要人工重新登录 {config['label']} 获取新的登录态"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise AuthError(f"{config['label']} Token 刷新响应缺少 data")
        access = str(data.get("accessToken") or "").strip()
        rotated_refresh = str(data.get("refreshToken") or "").strip()
        if not access or not rotated_refresh:
            raise AuthError(f"{config['label']} Token 刷新响应未同时返回两个 Token")
        return AuthTokens(access, rotated_refresh)

    def _save_tokens(self, system: str, tokens: AuthTokens) -> None:
        with self._locked("state"):
            state = self._read_state()
            self._tokens_from_state(state, system)
            state[system] = self._state_item(tokens)
            self._atomic_write_state(state)

    def refresh(
        self, system: str, *, stale_access_token: str | None = None
    ) -> AuthTokens:
        if system not in SYSTEM_CONFIG:
            raise ValueError(f"Unsupported auth system: {system}")
        with self._locked(system):
            current = self.get_tokens(system)
            if stale_access_token is not None and current.access_token != stale_access_token:
                return current
            tokens = self._request_refresh(system, current.refresh_token)
            self._save_tokens(system, tokens)
            if self.logger:
                self.logger(f"[{SYSTEM_CONFIG[system]['label']}] Token refresh success")
            return tokens

    def probe_previous_refresh_token(
        self, system: str, previous_refresh_token: str
    ) -> bool:
        with self._locked(system):
            try:
                tokens = self._request_refresh(system, previous_refresh_token)
            except RefreshTokenExpiredError:
                if self.logger:
                    self.logger(
                        f"[{SYSTEM_CONFIG[system]['label']}] previous refreshToken rejected"
                    )
                return False
            self._save_tokens(system, tokens)
            if self.logger:
                self.logger(
                    f"[{SYSTEM_CONFIG[system]['label']}] previous refreshToken accepted; latest rotated pair saved"
                )
            return True

    def prepare_curl(
        self, system: str, parsed_curl: dict[str, Any], *, refresh: bool = True
    ) -> dict[str, Any]:
        tokens = self.refresh(system) if refresh else self.get_tokens(system)
        return inject_auth(parsed_curl, tokens, system)

    def export_environment(
        self, system: str = "zhaoshang", *, refresh: bool = True
    ) -> dict[str, str]:
        if system != "zhaoshang":
            raise ValueError("Only Zhaoshang uses the BLADE_AUTH/COOKIE environment bridge")
        tokens = self.refresh(system) if refresh else self.get_tokens(system)
        template = next(
            (
                load_curl_command(path)
                for path in self.curl_sources.get(system, ())
                if path.exists()
            ),
            {"cookie": "", "headers": [], "headers_dict": {}},
        )
        prepared = inject_auth(template, tokens, system)
        return {
            "BLADE_AUTH": prepared["headers_dict"]["Blade-Auth"],
            "COOKIE": prepared["cookie"],
        }

    @staticmethod
    def _authorize_request(
        request: urllib.request.Request, system: str, tokens: AuthTokens
    ) -> None:
        cookie = _header_value(request.headers, "Cookie")
        request.headers["Cookie"] = _updated_cookie(cookie, tokens)
        if system == "zhaoshang" or _header_value(request.headers, "Blade-Auth"):
            request.headers["Blade-Auth"] = f"bearer {tokens.access_token}"

    def request_json(
        self,
        system: str,
        request_factory: Callable[[], urllib.request.Request],
        *,
        read_only: bool,
        opener: Any | None = None,
        timeout: float = 60,
    ) -> dict[str, Any]:
        direct_opener = opener or internal_direct_opener()
        attempts = 2 if read_only else 1
        stale_access = ""
        for attempt in range(attempts):
            tokens = self.get_tokens(system)
            stale_access = tokens.access_token
            request = request_factory()
            self._authorize_request(request, system, tokens)
            try:
                with direct_opener.open(request, timeout=timeout) as response:
                    payload = json.loads(
                        response.read().decode("utf-8", errors="replace")
                    )
            except urllib.error.HTTPError as exc:
                if read_only and attempt == 0 and exc.code == 401:
                    if self.logger:
                        self.logger(
                            f"[{SYSTEM_CONFIG[system]['label']}] access token rejected; refreshing once"
                        )
                    self.refresh(system, stale_access_token=stale_access)
                    continue
                raise
            if not isinstance(payload, dict):
                raise AuthError("业务接口响应必须是 JSON 对象")
            if _payload_is_auth_failure(payload):
                if read_only and attempt == 0:
                    if self.logger:
                        self.logger(
                            f"[{SYSTEM_CONFIG[system]['label']}] access token rejected; refreshing once"
                        )
                    self.refresh(system, stale_access_token=stale_access)
                    continue
                raise AuthError(
                    f"{SYSTEM_CONFIG[system]['label']} 认证失败，单次刷新重试后仍未恢复"
                )
            return payload
        raise AuthError(
            f"{SYSTEM_CONFIG[system]['label']} 认证失败，单次刷新重试后仍未恢复"
        )
