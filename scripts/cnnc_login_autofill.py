# -*- coding: utf-8 -*-
"""CNNC SSO 登录表单自动填充深模块（供 cnnc_auth_browser.py login 使用）。

职责内聚（对 cmd_login 只暴露 tick 一个动作）：
    - 凭证 seam：load_login_credentials（缺省读
      data/cnnc_login_credentials.json；任何异常/缺失/禁用一律返回 None，
      绝不抛出）
    - LoginAutofiller：限速 + 幂等 + 精确 Origin 门禁 + 字段启发式 + 填充
      执行全部收口在 tick(context)；cmd_login 循环外创建一次、循环内每轮
      TGC 判定后调用 tick 即可
    - create_login_autofiller：工厂。凭证缺失时打一行 AUTO_FILL_OFF 并
      返回 None，调用方保持原有纯手动输入行为

安全边界（与抽取前一致并加固）：
    - 只填"当前为空"的字段，绝不覆盖人已输入内容
    - 绝不点击、绝不提交表单（人负责滑块/短信验证与提交）
    - 日志只打字段名与 filled/skip origin，绝不含凭证值
    - 单次尝试异常一律吞掉（WARN 限次），绝不打断 TGC 轮询主流程
    - 精确可信 Origin 门禁（deny-by-default，加固项）：抽取前任何 frame
      都可能被填充；现在只填 Origin 精确等于白名单的 frame

可信 Origin 白名单证据（2026-09-10 评审确认）：
    SSO 登录表单只出现在 sso.cnncecp.com 上——cnnc_auth_browser 的
    AUTHORIZE_URL（sso.cnncecp.com/esc-sso/oauth2.0/authorize）、harvest
    入口 HARVEST_ENTRY（sso.cnncecp.com/）、storage_state 会话 cookie 白
    名单（SESSION_COOKIE_NAMES 均为 sso.cnncecp.com 域）三方一致；
    login 入口 LOGIN_ENTRY 是 one.cnncecp.com 门户（重定向到 SSO，本身
    无登录表单）。因此白名单精确为 {"https://sso.cnncecp.com"}：scheme
    必须 https 且 (host, port) 规范化后精确相等（显式 :443 与缺省等价）；
    禁止 endswith/子域通配——sso.cnncecp.com.evil.com、http 明文、
    about:blank、data:、第三方域一律拒绝。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
from pathlib import Path

# ROOT = 本模块所在目录（scripts/）的上一级，即 cnnc-ops-agent/
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# 可信登录 Origin 白名单：精确字符串匹配（证据链见模块 docstring）
TRUSTED_LOGIN_ORIGINS: frozenset[str] = frozenset({"https://sso.cnncecp.com"})

# 凭证文件：{"username": "...", "password": "...", "auto_fill_enabled": true}
CREDENTIALS_FILE = DATA_DIR / "cnnc_login_credentials.json"
# 填充尝试限速：登录页 302 链 + WAF 挑战 + 表单渲染需要数秒，每 3.5 秒
# 试一轮足够；两个字段各成功填过一次后即停止再试
AUTOFILL_INTERVAL_SEC = 3.5
# 单字段 fill 超时：失败快速返回，避免拖慢 2 秒粒度的 TGC 轮询
AUTOFILL_FILL_TIMEOUT_MS = 2000
# 填充异常最多 WARN 次数（之后静默，避免刷屏）
AUTOFILL_MAX_WARNS = 3
# 被跳过 origin 的去重打印上限（防御性有界，防异常页面刷屏）
_MAX_SKIP_ORIGIN_LINES = 32


def load_login_credentials(path=None):
    """读取登录自动填充凭证；任何异常/缺失/禁用一律返回 None，绝不抛出。

    规则：文件缺失、坏 JSON、顶层非对象、缺 username/password、值为空白、
    auto_fill_enabled 为 false（缺省视为 true）→ 返回 None；否则返回
    {"username": ..., "password": ...}。纯函数，可离线单测。
    """
    if path is None:
        path = CREDENTIALS_FILE
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("auto_fill_enabled", True):
        return None
    username = data.get("username")
    password = data.get("password")
    for value in (username, password):
        if not isinstance(value, str) or not value.strip():
            return None
    return {"username": username, "password": password}


# --------------------------------------------------------------------------
# 字段启发式（纯函数，可离线单测）
# --------------------------------------------------------------------------

# 用户名框启发式提示词（不区分大小写子串匹配 name/id/placeholder/class）
USERNAME_HINTS = (
    "username", "user", "account", "loginname", "login_name", "login",
    "账号", "用户名", "工号", "empno", "emp_code",
)
# 验证码/短信码类字段绝不能当用户名框。注意含 "code"：emp_code 类字段会被
# 一并排除——真实 SSO 登录框用 username/loginName 类命名，可接受
CAPTCHA_HINTS = ("captcha", "sms", "code", "verify", "验证码", "短信", "校验")
# 非文本类 input：既不参与用户名启发式，也不进 fallback
NON_TEXT_INPUT_TYPES = frozenset((
    "checkbox", "radio", "hidden", "submit", "button", "image",
    "file", "reset", "range", "color", "date", "time",
))


def _attrs_text(attrs):
    """拼接 name/id/placeholder/class 供子串匹配（统一小写）。纯函数。

    兼容两种来源键：_COLLECT_INPUTS_JS 输出 className，手写描述可能用
    class——两者都参与启发式。
    """
    return " ".join(
        str(attrs.get(key) or "")
        for key in ("name", "id", "placeholder", "class", "className")
    ).lower()


def _is_password_field(attrs):
    """密码框判定：type 含 password（不区分大小写）。纯函数。"""
    return "password" in str(attrs.get("type") or "").strip().lower()


def _looks_like_captcha(attrs):
    """验证码/短信码类字段判定（name/id/placeholder/class 含 captcha/sms/code 等）。"""
    text = _attrs_text(attrs)
    return any(hint in text for hint in CAPTCHA_HINTS)


def _score_username_field(attrs):
    """用户名框启发式打分。纯函数，可离线单测。

    返回：-1 = 密码框（绝不当用户名）；0 = 未命中或排除（checkbox/hidden
    等非文本类型、验证码类字段）；>0 = 命中 USERNAME_HINTS（中英文均支持）。
    """
    if _is_password_field(attrs):
        return -1
    if str(attrs.get("type") or "").strip().lower() in NON_TEXT_INPUT_TYPES:
        return 0
    if _looks_like_captcha(attrs):
        return 0
    text = _attrs_text(attrs)
    return 1 if any(hint in text for hint in USERNAME_HINTS) else 0


def _is_blank(value):
    """只填空字段：当前值去空白后为空才允许填充，不覆盖人已输入内容。"""
    return not str(value or "").strip()


def _field_signature(attrs):
    """字段签名 (type, name, id)：填充前二次确认 DOM 目标未漂移。"""
    return (str(attrs.get("type") or "").strip().lower(),
            str(attrs.get("name") or ""),
            str(attrs.get("id") or ""))


def _eligible_field(field):
    """字段可填性：可见、未禁用、非只读。"""
    return (field.get("visible") and not field.get("disabled")
            and not field.get("readonly"))


def _pick_frame_fill_targets(fields):
    """纯决策：从单个 frame 的输入框描述列表（document 顺序）选出
    (username_idx, password_idx)，找不到的返回 None。可离线单测。

    - 密码框 = 第一个可见/启用/非只读的 password 输入框
    - 用户名框 = 启发式最高分者；无命中时 fallback 到"密码框之前第一个
      可见的非 password/非排除类型/非验证码类 输入框"
    - 只返回当前值为空的索引（只填空字段，不覆盖人已输入）
    """
    usable = [i for i, f in enumerate(fields) if _eligible_field(f)]
    pwd_idx = next(
        (i for i in usable if _is_password_field(fields[i])), None)
    user_idx, best = None, 0
    for i in usable:
        score = _score_username_field(fields[i])
        if score > best:
            user_idx, best = i, score
    if user_idx is None and pwd_idx is not None:
        for i in usable:
            if i >= pwd_idx:
                break
            field = fields[i]
            if (_is_password_field(field) or _looks_like_captcha(field)
                    or str(field.get("type") or "").strip().lower()
                    in NON_TEXT_INPUT_TYPES):
                continue
            user_idx = i
            break
    if user_idx is not None and not _is_blank(fields[user_idx].get("value")):
        user_idx = None
    if pwd_idx is not None and not _is_blank(fields[pwd_idx].get("value")):
        pwd_idx = None
    return user_idx, pwd_idx


# 在 frame 内收集全部 input 描述（document 顺序，与 frame.locator("input")
# 的 nth() 顺序一致）；visible = 几何尺寸 + display/visibility 综合判断。
_COLLECT_INPUTS_JS = """() => Array.from(document.querySelectorAll('input')).map((el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return {
        type: String(el.type || '').toLowerCase(),
        name: el.getAttribute('name') || '',
        id: el.getAttribute('id') || '',
        placeholder: el.getAttribute('placeholder') || '',
        className: String(el.className || ''),
        value: String(el.value || ''),
        disabled: !!el.disabled,
        readonly: !!el.readOnly,
        visible: style.display !== 'none' && style.visibility !== 'hidden'
            && rect.width > 0 && rect.height > 0
    };
})"""


# --------------------------------------------------------------------------
# 精确可信 Origin 门禁（deny-by-default）
# --------------------------------------------------------------------------

def _normalized_origin(url):
    """从 frame URL 规范化出 origin 字符串；非 https / 无 host 返回 None。

    - scheme 必须是 https（http / about / data 等一律 None）
    - host 统一小写；显式 :443 与缺省端口等价，其余端口参与 origin
    - URL 解析失败 / 非法端口 → None（deny）
    """
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        if (parts.scheme or "").lower() != "https":
            return None
        hostname = (parts.hostname or "").lower()
        if not hostname:
            return None
        try:
            port = parts.port
        except ValueError:
            return None
    except ValueError:
        return None
    if port is None or port == 443:
        return "https://%s" % hostname
    return "https://%s:%d" % (hostname, port)


def _origin_display(url):
    """被跳过 frame 的日志 origin 显示（截断，防 data: 超长 URL 刷屏）。"""
    raw = str(url or "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw[:120] or "<unknown>"
    if parts.netloc:
        return "%s://%s" % ((parts.scheme or "").lower(),
                            (parts.netloc or "").lower())
    return raw[:120] or "<unknown>"


# --------------------------------------------------------------------------
# 填充器
# --------------------------------------------------------------------------

class LoginAutofiller:
    """SSO 登录表单自动填充器：限速 + 幂等 + 精确 Origin 门禁内聚。

    用法：cmd_login 循环外 create_login_autofiller() 创建一次，循环内每轮
    TGC 判定后调用 tick(context)。未到限速间隔、或两字段各成功填过一次后，
    tick 直接返回（连字段读取都不做）。所有异常在内部吞掉（WARN 限次），
    绝不打断 TGC 轮询主流程。

    clock 可注入（默认 time.monotonic）便于离线单测限速逻辑。
    """

    def __init__(self, credentials, interval_seconds=AUTOFILL_INTERVAL_SEC,
                 warn_limit=AUTOFILL_MAX_WARNS, clock=time.monotonic):
        self._creds = dict(credentials)
        self._interval = interval_seconds
        self._warn_limit = warn_limit
        self._clock = clock
        self._filled = {"username": False, "password": False}
        self._warns = 0
        self._next_tick_at = None
        self._skipped_origins = set()

    def tick(self, context) -> None:
        """登录轮询循环每轮调用；自带限速与幂等，绝不向调用方抛异常。

        两字段都已成功填过（幂等）或未到限速间隔时直接返回。
        """
        if self._filled["username"] and self._filled["password"]:
            return
        now = self._clock()
        if self._next_tick_at is not None and now < self._next_tick_at:
            return
        self._next_tick_at = now + self._interval
        self._autofill_once(context)

    # ---------------------------------------------------------------- 内部

    def _autofill_once(self, ctx):
        """单轮填充尝试：遍历 ctx.pages 与各 page.frames（含 iframe 表单）。"""
        for page in list(getattr(ctx, "pages", None) or []):
            try:
                frames = list(page.frames)
            except Exception:
                continue
            for frame in frames:
                if self._filled["username"] and self._filled["password"]:
                    return
                # Origin 门禁先于任何字段读取（deny-by-default）
                if not self._origin_allowed(getattr(frame, "url", "")):
                    continue
                try:
                    fields = frame.evaluate(_COLLECT_INPUTS_JS)
                except Exception:
                    continue  # 导航中/已关闭：静默跳过该 frame
                if not isinstance(fields, list):
                    continue
                try:
                    self._fill_frame_fields(frame, fields)
                except Exception:
                    self._warns += 1
                    if self._warns <= self._warn_limit:
                        print("WARN：自动填充尝试失败（第 %d 次，继续轮询）"
                              % self._warns, file=sys.stderr)

    def _origin_allowed(self, url) -> bool:
        """精确可信 Origin 门禁：不在白名单的 frame 一律拒绝并按 origin
        去重打一行 AUTO_FILL_SKIP（有界，防刷屏）。"""
        if _normalized_origin(url) in TRUSTED_LOGIN_ORIGINS:
            return True
        origin = _origin_display(url)
        if (origin not in self._skipped_origins
                and len(self._skipped_origins) < _MAX_SKIP_ORIGIN_LINES):
            self._skipped_origins.add(origin)
            print("AUTO_FILL_SKIP origin=%s" % origin, flush=True)
        return False

    def _fill_frame_fields(self, frame, fields):
        """在单个 frame 内执行已决策的填充；填充前二次确认目标未漂移。

        用 locator.fill（Playwright 自带 input 事件派发，SPA 框架可感知），
        不点击、不提交。DOM 与采样时签名不一致则本轮放弃（下一轮重新评估）。
        异常向上抛给 _autofill_once 统一限次 WARN。
        """
        user_idx, pwd_idx = _pick_frame_fill_targets(fields)
        for kind, idx in (("username", user_idx), ("password", pwd_idx)):
            if idx is None or self._filled[kind]:
                continue
            expected = _field_signature(fields[idx])
            locator = frame.locator("input").nth(idx)
            actual = locator.evaluate(
                "el => ({type: String(el.type || '').toLowerCase(),"
                " name: el.getAttribute('name') || '',"
                " id: el.getAttribute('id') || ''})")
            if _field_signature(actual) != expected:
                return  # DOM 漂移：本轮放弃，下一轮重新评估，绝不盲填
            locator.fill(self._creds[kind], timeout=AUTOFILL_FILL_TIMEOUT_MS)
            self._filled[kind] = True
            # 日志只打字段名与 filled，绝不含凭证值
            print("AUTO_FILL：%s filled" % kind, flush=True)


def create_login_autofiller(credential_path=None):
    """cmd_login 唯一入口：加载凭证并构造填充器。

    凭证缺失/禁用/坏文件 → 打一行 AUTO_FILL_OFF 并返回 None（调用方保持
    纯手动输入行为）；成功返回 LoginAutofiller。绝不抛异常。
    """
    path = (Path(credential_path) if credential_path is not None
            else CREDENTIALS_FILE)
    creds = load_login_credentials(path)
    if creds is None:
        print("AUTO_FILL_OFF：未配置 %s，需手动输入账号密码" % path, flush=True)
        return None
    return LoginAutofiller(creds)
