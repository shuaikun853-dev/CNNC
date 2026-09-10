# -*- coding: utf-8 -*-
"""CNNC SSO 浏览器登录 / OC 收割工具（子进程纯净 Chrome + Playwright CDP 接管）。

背景:
    CNNC 门户的 /authorize 端点带瑞数类 WAF，纯 HTTP 客户端一律返回 412
    （完整浏览器头 + 字节级正确 cookie 仍 412，已穷举验证），必须用真实
    浏览器执行其 JS。但 Playwright 直接 launch 的浏览器带自动化标记
    （navigator.webdriver=true、--enable-automation 等），WAF 挑战 JS 检测
    到后挂起不渲染，表现为窗口白屏。

    因此本脚本改为：subprocess 启动普通 chrome.exe（无任何自动化 flag，
    仅带 --remote-debugging-port），再用 Playwright connect_over_cdp 接管。
    这种浏览器没有任何自动化痕迹，WAF 挑战正常放行，不再白屏。

    另外：直接 goto /authorize 会被 WAF 挑战页（412 中转页）卡死，因此
    harvest 改从门户首页 sso.cnncecp.com/ 进入，靠 302 链自然到达
    /authorize（实测 2 秒内走完整条链）。

用法:
    python scripts/cnnc_auth_browser.py login
    python scripts/cnnc_auth_browser.py harvest [--headless]
    python scripts/cnnc_auth_browser.py smoke

命令:
    login    人工一次性登录（账号 + 滑块 + 短信）。浏览器以一次性临时
             Profile（空 cookie）启动，从根上杜绝陈旧 TGC 被"崩溃恢复"
             还原导致启动即误判登录成功、窗口秒关（TGC 出现只可能来自
             本次真人登录新签发）；检测到 SSO 根凭证 TGC 后只把 5 个
             SSO 会话白名单 cookie 导出到 data/cnnc_auth_storage_state.json
             （登录成功的会话仍经 storage_state 持久化）。支持 SSO 登录
             表单账号密码自动填充：凭证文件 data/
             cnnc_login_credentials.json（已被 .gitignore 忽略）存在且
             auto_fill_enabled 非 false 时，自动把账号/密码填入登录表单
             （含 iframe 场景，只填空字段、不点击不提交），人只做滑块/
             短信验证与提交；文件缺失则保持纯手动输入
    harvest  用已保存会话只注入 5 个 SSO 会话 cookie（全量注入含陈旧 K5 等
             WAF cookie 会让门户首页卡在 412 挑战循环；WAF 空状态挑战自算
             反而秒过），从门户首页 https://sso.cnncecp.com/ 进入，自动跟随
             302 链（→ /portal/rest/selfcare/redirect/user →
             /esc-sso/oauth2.0/authorize）；OC 主通道从 authorize 302 响应的
             Location 头捕获 code=OC-xxx（比等浏览器落地 cnnc-web 更快，且
             避开 SPA 消费竞态），兜底通道为落地页 URL 含 code=OC-。收割到 OC
             后立即关闭收割页（防 SPA 消费一次性 OC），并预先打开的
             one.cnncecp.com 预热页（同源、已过 WAF 挑战）内执行 fetch
             anon/account/login 完成 OC→AT 交换（2026-09-02 实测：午后 WAF
             对裸 Python 请求返回 502，浏览器内同源 fetch 返回
             200/success:true）；交换成功在 OC= 行后追加打印单行 AT=<token>，
             失败仅 WARN、回退外部裸 HTTP 交换（cnnc_auth_core 通道）。成功后
             把服务器轮换的 cookie 经白名单过滤回写
             storage_state（只存 5 个 SSO 会话 cookie，陈旧 K5/SF/UqZ 等
             WAF cookie 不落盘）。浏览器每次使用一次性临时 Profile（系统
             TEMP，用后即删），从根上隔离固定 Profile 累积的陈旧持久
             K5/SF/UqZ cookie（曾导致 harvest 带陈旧 WAF 状态启动、卡进
             挑战循环）
    smoke    离线自检：headless 启动纯净 Chrome（一次性临时 Profile）并
             CDP 接管，打印版本验证链路

退出码:
    0   成功（harvest 向 stdout 打印 OC=<code>；浏览器内交换成功时在 OC= 行
        后追加单行 AT=<token>）
    1   启动链路失败（Chrome 未找到 / 端口占用 / CDP 接管失败 / 无浏览器上下文）
    2   SSO 会话已失效（SSO_LOGIN_REQUIRED，需重新 login）
    3   login 超时（LOGIN_TIMEOUT）
    4   storage_state 不存在（需先 login）
    5   harvest 60 秒内未拿到 OC
    130 用户中断
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

# 登录表单自动填充深模块（限速/幂等/精确 Origin 门禁内聚在 autofiller.tick）
try:  # 兼容两种入口：作为 scripts.* 包导入（pytest）与直接脚本执行
    from scripts.cnnc_login_autofill import create_login_autofiller
except ImportError:  # 直接 python scripts/cnnc_auth_browser.py 时的 sys.path[0]=scripts
    from cnnc_login_autofill import create_login_autofiller

# ROOT = 本脚本所在目录（scripts/）的上一级，即 cnnc-ops-agent/
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STORAGE_STATE = DATA_DIR / "cnnc_auth_storage_state.json"

# AUTHORIZE_URL 仅作说明保留，不再作为 harvest 的 goto 目标：直接 goto 该
# URL 会被 WAF 挑战页（412 中转页）卡死。harvest 改用 HARVEST_ENTRY（门户
# 首页）进入，由首页 302 链自然到达 /authorize。
AUTHORIZE_URL = (
    "https://sso.cnncecp.com/esc-sso/oauth2.0/authorize"
    "?client_id=4dbc816bf265438e&response_type=code"
    "&redirect_uri=https://one.cnncecp.com/cnnc-web/"
    "&oauth_timestamp=1670842317113"
)
HARVEST_ENTRY = "https://sso.cnncecp.com/"
LOGIN_ENTRY = "https://one.cnncecp.com/cnnc-web/"

SSO_ORIGIN = "https://sso.cnncecp.com"
OC_RE = re.compile(r"OC-[\w-]+")
# AT 形态（与 cnnc_auth_core.AT_RE 一致）：校验浏览器内交换返回的 token
AT_RE = re.compile(r"AT-\d+-[A-Za-z0-9]+")

# 只注入 SSO 会话 cookie；陈旧 WAF cookie（K5/SF/UqZB 等）会让 WAF 挑战
# 卡进慢速循环，实测空 WAF 状态自算反而秒过（2026-09-02 v4 实验）
SESSION_COOKIE_NAMES = {
    "TGC", "SESSION", "GWSID1-sso.cnncecp.com", "mfaQueryValue",
    "org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE",
}


def select_session_cookies(cookies):
    """纯函数：从 storage_state 的 cookie 列表中只保留 SSO 会话 cookie（白名单）。

    陈旧 WAF cookie（K5/SF/UqZB/token 等）一律剔除：全量注入会让门户首页
    卡进 WAF 412 挑战循环，WAF 空状态挑战自算反而秒过。
    """
    return [c for c in (cookies or []) if c.get("name") in SESSION_COOKIE_NAMES]


# 实测（2026-09-02 v4 实验）：白名单 5 cookie + 空 WAF 状态即可走通门户
# 302 链；K5/SF/UqZ 等 WAF cookie 由每次 harvest 的临时浏览器现场生成，
# 不长期保存，storage_state 只落白名单会话 cookie。
def save_session_state(cookies: list, path) -> list:
    """从浏览器全量 cookie 中筛选 SSO 会话白名单并原子写入最小 storage_state。

    原子写：先写同目录 tmp(uuid) 文件再 os.replace，避免读到半截文件。
    返回本次落盘的 cookie 名单。
    """
    session = select_session_cookies(cookies)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name("%s.tmp-%s" % (path.name, uuid.uuid4().hex))
    try:
        tmp_path.write_text(
            json.dumps({"cookies": session, "origins": []},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return [c.get("name", "") for c in session]


def capture_oc_from_response(response):
    """纯函数：主通道——从 /oauth2.0/authorize 302 响应的 Location 头捕获 OC。

    比等浏览器落地 cnnc-web 再读 URL 更快，且避开 SPA 消费 OC 的竞态。
    必须是 CNNC 外部 OAuth（redirect 到 one.cnncecp.com）；SSO 内部 OAuth
    （client fab04 → /ngw/login?code=OC-...）也产生 OC- 码，抓错会导致换
    token 040000。任何异常吞掉并返回 None，绝不让监听器打断收割流程。
    """
    try:
        if response.status not in (301, 302, 303, 307, 308):
            return None
        if "/oauth2.0/authorize" not in (response.url or ""):
            return None
        location = (response.headers or {}).get("location", "")
        if "one.cnncecp.com" not in location:
            return None
        if "code=OC-" not in location:
            return None
        match = OC_RE.search(location)
        if match:
            return match.group(0)
        return None
    except Exception:
        return None

LOGIN_TIMEOUT_SEC = 15 * 60
# 门户 302 链正常约 5 秒完成；留 60 秒余量覆盖高负载/挑战变慢场景
# （实测满载 CPU 时 30 秒会被挤爆导致 HARVEST_TIMEOUT）。
HARVEST_TIMEOUT_SEC = 60

# 纯净系统 Chrome 路径候选（按顺序取第一个存在的）
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
# CDP 远程调试端口候选区间（含头不含尾）
CDP_PORT_RANGE = range(9333, 9341)

CDP_READY_TIMEOUT_SEC = 25


def _force_utf8_stdio():
    """避免中文输出在 Windows 管道场景下因本地代码页触发 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _launch_chrome_cdp(url=None, headless=False, *, user_data_dir):
    """子进程启动纯净系统 Chrome（带远程调试端口），并用 CDP 接管。

    浏览器本身不带任何自动化标记（无 --enable-automation 等 flag，
    navigator.webdriver=false），用于绕过 WAF 对 Playwright 托管浏览器的检测。

    user_data_dir 必填（keyword-only）：调用方显式传入一次性临时目录
    （login/harvest/smoke 均如此）。空 cookie Profile 启动从根上隔离陈旧
    持久 cookie（TGC/K5/SF/UqZ），杜绝"崩溃恢复"还原出僵尸会话状态。

    返回 (proc, browser, playwright)；调用方负责在 finally 中调用
    _close_cdp(proc, browser, pw) 完成清理。任何失败路径由本函数自行清理。
    """
    from playwright.sync_api import sync_playwright

    # 1. 定位系统 Chrome
    exe = None
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            exe = candidate
            break
    if not exe:
        print("CHROME_NOT_FOUND：未在以下路径找到 chrome.exe：%s"
              % " ; ".join(CHROME_CANDIDATES), file=sys.stderr)
        raise RuntimeError("系统 Chrome 未找到")

    # 2. 选一个空闲的 CDP 端口（连不上 = 没人监听 = 可用）
    port = None
    for cand in CDP_PORT_RANGE:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(0.5)
            s.connect(("127.0.0.1", cand))
        except OSError:
            port = cand
            break
        finally:
            s.close()
    if port is None:
        raise RuntimeError("CDP 调试端口 %d-%d 全部被占用"
                           % (CDP_PORT_RANGE[0], CDP_PORT_RANGE[-1] - 1))

    user_data_dir = Path(user_data_dir)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    # 3. 启动纯净 Chrome：无任何自动化 flag
    chrome_args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % user_data_dir,
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        chrome_args.append("--headless=new")
    if url:
        chrome_args.append(url)
    proc = subprocess.Popen(chrome_args)

    # 4. 轮询 CDP 端点就绪
    try:
        version_url = "http://127.0.0.1:%d/json/version" % port
        deadline = time.time() + CDP_READY_TIMEOUT_SEC
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "Chrome 子进程提前退出（exit=%s），可能已有使用同一 "
                    "user-data-dir 的 Chrome 实例在运行，请先关闭后重试"
                    % proc.returncode)
            try:
                with urllib.request.urlopen(version_url, timeout=2) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                pass
            time.sleep(0.3)
        if not ready:
            raise RuntimeError("等待 CDP 端点就绪超时: %s" % version_url)

        # 5. CDP 接管
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        except Exception:
            try:
                pw.stop()
            except Exception:
                pass
            raise
        return proc, browser, pw
    except BaseException:
        _kill_chrome(proc)
        raise


def _kill_chrome(proc):
    """终止脚本启动的 Chrome 子进程（尽力而为，不抛错）。"""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except Exception:
                pass
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
    except Exception:
        pass


def _close_cdp(proc, browser, pw):
    """清理 CDP 接管资源：断开连接 -> 杀 Chrome 子进程 -> 停 Playwright。"""
    if browser is not None:
        try:
            browser.close()  # 仅断开 CDP 连接
        except Exception:
            pass
    if proc is not None:
        _kill_chrome(proc)
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


def cmd_login(args):
    """人工一次性登录：纯净 Chrome 以一次性临时 Profile（空 cookie）打开
    真窗口完成登录，检测到本次登录新签发的 SSO 根凭证 TGC 后只导出 SSO
    会话白名单 cookie 到 storage_state。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("正在启动纯净 Chrome…请在窗口中完成 CNNC 登录（账号+滑块+短信）",
          flush=True)
    # 一次性临时 Profile（系统 TEMP，仓库外），空 cookie 启动：持久 Profile
    # 里物理残留的陈旧 TGC 会被"崩溃恢复"还原（_close_cdp 硬杀 Chrome 从不
    # 优雅关闭），启动 ~2 秒即误判"登录成功"秒关窗，用户永远没机会输密码
    # （2026-09-03 实锤 bug）。改空 Profile 后，TGC 出现只可能来自本次真人
    # 登录新签发，窗口保持打开直到用户真正完成登录。
    login_dir = Path(tempfile.mkdtemp(prefix="cnnc_login_"))
    proc = browser = pw = None
    try:
        # URL 作为启动参数直接打开，不用 page.goto
        proc, browser, pw = _launch_chrome_cdp(url=LOGIN_ENTRY,
                                               user_data_dir=login_dir)
        ctx = browser.contexts[0] if browser.contexts else None
        if ctx is None:
            print("CDP_NO_CONTEXT：CDP 接管后未找到浏览器上下文", file=sys.stderr)
            return 1
        deadline = time.time() + LOGIN_TIMEOUT_SEC
        cookie_err_count = 0
        # 登录表单自动填充（主线程内联，零线程）：循环外创建一次；凭证缺失
        # 时 create_login_autofiller 打一行 AUTO_FILL_OFF 并返回 None，保持
        # 原有纯手动行为。限速/幂等/精确 Origin 门禁/异常吞噬全部内聚在
        # autofiller.tick 内。TGC 检测到后自然退出循环即停止填充。
        autofiller = create_login_autofiller()
        while time.time() < deadline:
            try:
                # 注意：CDP 接管的浏览器上，带 URL 过滤的 ctx.cookies(url)
                # 拿不到 cookie（会一直空转），必须无参数全量拉取后按
                # domain 字段过滤
                all_cookies = ctx.cookies()
            except Exception as exc:
                cookie_err_count += 1
                # 浏览器被用户手动关闭时 cookies() 会抛错，直接退出，
                # 不要傻等 15 分钟
                if (proc is None or proc.poll() is not None
                        or "closed" in str(exc).lower()):
                    print("BROWSER_CLOSED：浏览器已被用户关闭，退出登录等待",
                          file=sys.stderr)
                    return 130
                print("WARN：第 %d 次读取 cookie 失败（继续轮询）: %s"
                      % (cookie_err_count, exc), file=sys.stderr)
                time.sleep(2)
                continue
            cookie_err_count = 0
            cnn = [c for c in all_cookies if "cnncecp" in (c.get("domain") or "")]
            names = {c["name"] for c in cnn}
            # 只认 TGC（SSO 根凭证）：SESSION 单独出现不足以证明登录完成
            if "TGC" in names:
                time.sleep(3)  # 让 cookie 稳定
                # 只落白名单 cookie，不全量导出（陈旧 K5/SF/UqZ 不落盘）
                saved = save_session_state(ctx.cookies(), STORAGE_STATE)
                cnn = [c for c in ctx.cookies()
                       if "cnncecp" in (c.get("domain") or "")]
                print("LOGIN_OK 已导出 storage_state -> %s" % STORAGE_STATE)
                print("session_state_saved: %s" % ", ".join(saved))
                # 只打名字和长度，不打值，防泄漏
                print("sso 域 cookie: %s" % ", ".join(
                    "%s(len=%d)" % (c["name"], len(c.get("value") or ""))
                    for c in cnn))
                return 0
            # 登录表单自动填充：限速尝试（不点击不提交；异常已在内
            # 部吞掉，绝不影响下面的 TGC 轮询节奏）
            if autofiller is not None:
                autofiller.tick(ctx)
            time.sleep(2)
        print("LOGIN_TIMEOUT：请人工完成登录后重试", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("LOGIN_CANCELLED：用户中断", file=sys.stderr)
        return 130
    finally:
        # Windows 下临时目录被 Chrome 进程占用时删不掉：必须先断开 CDP 并
        # 杀掉 Chrome 子进程，再删除临时 Profile 目录（同 harvest）
        _close_cdp(proc, browser, pw)
        shutil.rmtree(login_dir, ignore_errors=True)


def cmd_harvest(args):
    """用已保存的 SSO 会话（只注会话 cookie）从门户首页进入 302 链，收割 OC。

    主通道：监听 /esc-sso/oauth2.0/authorize 的 302 响应，从 Location 头
    提取 code=OC-xxx；兜底：落地页 URL 轮询含 code=OC-。
    启动后先在 one.cnncecp.com 开预热页让 WAF 挑战完成（2026-09-02 实测：
    午后 WAF 对裸 Python 请求返回 502，浏览器内同源 fetch 返回
    200/success:true）；收割到 OC 后关闭收割页，在预热页上执行同源 fetch
    anon/account/login 交换，成功时在 OC= 行后追加打印 AT=<token>，失败仅
    WARN 回退外部裸交换。
    浏览器用一次性临时 Profile：固定持久 Profile 目录会随运行累积陈旧
    K5/SF/UqZ 持久 cookie，harvest 浏览器会带着陈旧 WAF 状态启动、卡进
    挑战循环，因此与 login 一样每次都用一次性临时 Profile（互不共享状态）。
    """
    if not STORAGE_STATE.exists():
        print("STORAGE_STATE_NOT_FOUND：未找到 %s，请先运行 "
              "python scripts/cnnc_auth_browser.py login 完成登录" % STORAGE_STATE,
              file=sys.stderr)
        return 4

    # 一次性临时 Profile（系统 TEMP，仓库外），用后即删
    harvest_dir = Path(tempfile.mkdtemp(prefix="cnnc_harvest_"))
    print("harvest 临时 Profile：%s" % harvest_dir)
    proc = browser = pw = None
    try:
        # 默认有头：WAF 可能检测 headless，真窗口更稳
        proc, browser, pw = _launch_chrome_cdp(
            headless=args.headless, user_data_dir=harvest_dir)
        ctx = browser.contexts[0] if browser.contexts else None
        if ctx is None:
            print("CDP_NO_CONTEXT：CDP 接管后未找到浏览器上下文", file=sys.stderr)
            return 1

        # 只注入 SSO 会话 cookie：全量注入含陈旧 WAF cookie（K5/SF 等）会让
        # 门户首页卡在 WAF 412 挑战循环；WAF 空状态挑战自算反而秒过
        try:
            state = json.loads(STORAGE_STATE.read_text(encoding="utf-8"))
        except Exception as exc:
            print("WARN：storage_state 读取失败: %s" % exc, file=sys.stderr)
            state = {}
        session_cookies = select_session_cookies(state.get("cookies", []))
        if not session_cookies:
            print("NO_SESSION_COOKIE：storage_state 中无 SSO 会话 cookie，请运行 "
                  "python scripts/cnnc_auth_browser.py login 重新登录",
                  file=sys.stderr)
            return 2
        try:
            ctx.add_cookies(session_cookies)
        except Exception as exc:
            print("WARN：会话 cookie 注入失败（继续）: %s" % exc, file=sys.stderr)
        # 只打数量与名字，不打值，防泄漏
        print("已注入 %d 个 SSO 会话 cookie: %s" % (
            len(session_cookies),
            ", ".join(c.get("name", "") for c in session_cookies)))

        # 预热页（2026-09-02 实测）：先在 one.cnncecp.com 开一页让 WAF 挑战
        # 完成，后续 OC→AT 交换在该页上以同源 fetch 执行；预热失败仅 WARN
        # 继续（回退外部裸交换），不打断收割。
        warm = ctx.new_page()
        try:
            warm.goto(LOGIN_ENTRY, wait_until="commit", timeout=30000)
        except Exception as exc:
            print("WARN：预热导航异常（继续，回退外部交换）: %s" % exc,
                  file=sys.stderr)
        # 等预热页真正加载完成（挑战页 title 为空/readyState 不 complete），
        # 盲等 3 秒在挑战慢时不够（实测间歇性回退外部 502 的根因）
        warm_deadline0 = time.time() + 20
        while time.time() < warm_deadline0:
            try:
                state = warm.evaluate(
                    "() => document.readyState + '|' + (document.title || '')")
            except Exception:
                state = ""
            if state.startswith("complete|") and len(state) > len("complete|"):
                break
            time.sleep(0.5)

        page = ctx.new_page()
        oc_holder = []

        def _on_response(response):
            """监听器薄封装：捕获逻辑在纯函数 capture_oc_from_response 中，
            首个 OC 生效；异常已在纯函数内吞掉，不打断收割流程。"""
            oc = capture_oc_from_response(response)
            if oc and not oc_holder:
                oc_holder.append(oc)

        # 两段式（2026-09-02 实测矩阵）：全新 Profile 的首次 WAF 挑战与 CDP
        # Network 域插桩（response 监听器）互相干扰会卡死 412；先无监听器
        # 热身导航让挑战自然通过（Profile 变热），再插桩+二次导航收割。
        try:
            page.goto(HARVEST_ENTRY, wait_until="commit", timeout=20000)
        except Exception as exc:
            print("WARN：热身导航异常（继续）: %s" % exc, file=sys.stderr)
        warm_deadline = time.time() + 25
        while time.time() < warm_deadline:
            try:
                warm_url = page.url
            except Exception:
                warm_url = ""
            # 挑战页 URL 与入口相同；一旦离开入口说明挑战已过。
            # 注意：热身段出现的 ?code=OC- 不采集——此时无屏蔽，SPA 会抢先
            # 消费一次性 OC（实测败因），OC 任由它消费，二次导航重新签发。
            if warm_url and warm_url.split("#")[0] != HARVEST_ENTRY:
                break
            time.sleep(0.5)

        # 二次导航前先在收割页屏蔽 anon/account/login（CDP Network 域
        # setBlockedURLs——非 Fetch 域路由，不启用请求拦截，与 302 链无冲突；
        # 热 Profile 上 Network 插桩已证无碍）：SPA 的 OC 消费请求被浏览器
        # 层直接掐断，一次性 OC 必然保留给预热页的受控交换。
        try:
            cdp_session = ctx.new_cdp_session(page)
            cdp_session.send("Network.enable")
            cdp_session.send("Network.setBlockedURLs",
                             {"urls": ["*anon/account/login*"]})
        except Exception as exc:
            print("WARN：CDP 屏蔽 anon login 失败（OC 可能被 SPA 抢先消费）: %s"
                  % exc, file=sys.stderr)

        page.on("response", _on_response)
        try:
            # 二次导航：热 Profile 上挑战已过，302 链即时完成，
            # 监听器从 authorize 302 的 Location 头捕获 OC。
            page.goto(HARVEST_ENTRY, wait_until="commit", timeout=20000)
        except Exception as exc:
            print("WARN：goto HARVEST_ENTRY 异常（继续轮询）: %s" % exc,
                  file=sys.stderr)

        code = ""
        final_url = ""
        deadline = time.time() + HARVEST_TIMEOUT_SEC
        while time.time() < deadline:
            if oc_holder:
                # 通道一：302 Location 头捕获的 OC
                code = oc_holder[0]
            else:
                # 通道二（兜底）：落地页 URL 含 code=OC-
                try:
                    final_url = page.url
                except Exception:
                    final_url = ""
                if "code=OC-" in final_url:
                    m = OC_RE.search(final_url)
                    if m:
                        code = m.group(0)
            if code:
                # 立即关页：掐断 cnnc-web SPA 对一次性 OC 的消费竞态
                try:
                    page.close()
                except Exception:
                    pass
                # 浏览器内同源交换（2026-09-02 实测：午后 WAF 对裸 Python 请求
                # 返回 502，浏览器内同源 fetch 返回 200/success:true）：在已过
                # WAF 挑战的预热页上执行 fetch；失败重载预热页重试一次，仍败
                # 仅 WARN、回退外部裸交换。
                browser_at = ""
                for attempt in range(2):
                    result = {"status": 0, "ok": False, "token": ""}
                    try:
                        result = warm.evaluate(
                            """async (code) => {
                                try {
                                    const r = await fetch(
                                        '/cnnc-gateway/pm/n/anon/account/login', {
                                            method: 'POST',
                                            headers: {
                                                'Content-Type': 'application/json',
                                                'mk-request': '41',
                                                'Accept':
                                                    'application/json, text/plain, */*'
                                            },
                                            body: JSON.stringify({code})
                                        });
                                    const j = await r.json().catch(() => null);
                                    const token =
                                        (j && ((j.data && j.data.token) || j.token))
                                        || '';
                                    return {status: r.status,
                                            ok: !!(j && j.success), token};
                                } catch (e) {
                                    return {status: 0, ok: false, token: ''};
                                }
                            }""", code)
                    except Exception as exc:
                        print("WARN：浏览器内交换执行异常（尝试 %d）: %s"
                              % (attempt + 1, exc), file=sys.stderr)
                    if isinstance(result, dict):
                        candidate = str(result.get("token") or "").strip()
                        if (result.get("ok") and candidate
                                and AT_RE.fullmatch(candidate)):
                            browser_at = candidate
                            break
                    # 重载预热页（重过 WAF）再试一次
                    if attempt == 0:
                        try:
                            warm.goto(LOGIN_ENTRY, wait_until="commit",
                                      timeout=30000)
                            time.sleep(3)
                        except Exception:
                            pass
                if not browser_at:
                    print("WARN：浏览器内交换失败（status=%s），回退外部交换"
                          % (result.get("status") if isinstance(result, dict)
                             else "?"), file=sys.stderr)
                try:
                    # 回写：只保存 SSO 会话白名单 cookie（不再全量导出，
                    # 陈旧 K5/SF/UqZ 不落盘，由每次临时浏览器现场生成）
                    names = save_session_state(ctx.cookies(), STORAGE_STATE)
                    print("session_state_saved: %s" % ", ".join(names))
                except Exception as exc:
                    print("WARN：storage_state 回写失败（不影响本次 OC）: %s" % exc,
                          file=sys.stderr)
                print("OC=%s" % code)
                if browser_at:
                    # 浏览器内交换成功：追加单行 AT，provider 据此跳过外部裸交换
                    print("AT=%s" % browser_at)
                return 0
            if "/esc-sso/login" in final_url or final_url.startswith(SSO_ORIGIN + "/login"):
                print("SSO_LOGIN_REQUIRED：SSO 会话已失效，请运行 "
                      "cnnc_auth_browser.py login 重新登录", file=sys.stderr)
                return 2
            time.sleep(0.3)
        # 超时收尾：采样页面内容识别"登录页原地渲染"（SSO 已死但门户首页
        # 不跳 /login 的形态）——映射为 SSO_LOGIN_REQUIRED（exit 2），让
        # provider 的 SSO 冷却断路器生效，避免每 tick 重启浏览器。
        login_markers = ""
        try:
            page_html = page.content()[:4000]
            for marker in ("mfaLogin", "esc-sso/login", "账号登录", "登录密码"):
                if marker in page_html:
                    login_markers = marker
                    break
        except Exception:
            page_html = ""
        if login_markers:
            print("SSO_LOGIN_REQUIRED：SSO 会话已失效（页面特征 %s），请运行 "
                  "cnnc_auth_browser.py login 重新登录" % login_markers, file=sys.stderr)
            return 2
        print("HARVEST_TIMEOUT：未在 %d 秒内获得 OC，最终 URL: %s"
              % (HARVEST_TIMEOUT_SEC, final_url), file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("HARVEST_CANCELLED：用户中断", file=sys.stderr)
        return 130
    finally:
        # Windows 下临时目录被 Chrome 进程占用时删不掉：必须先断开 CDP 并
        # 杀掉 Chrome 子进程，再删除临时 Profile 目录
        _close_cdp(proc, browser, pw)
        shutil.rmtree(harvest_dir, ignore_errors=True)


def cmd_smoke(args):
    """离线自检：仅验证 纯净 Chrome 子进程启动 + CDP 接管 链路（headless，不访问页面）。

    同 harvest 模式：一次性临时 Profile（mkdtemp + rmtree），不污染持久 Profile。
    """
    smoke_dir = Path(tempfile.mkdtemp(prefix="cnnc_harvest_"))
    proc = browser = pw = None
    try:
        proc, browser, pw = _launch_chrome_cdp(url=None, headless=True,
                                               user_data_dir=smoke_dir)
        print("SMOKE_OK browser.version=%s contexts=%d"
              % (browser.version, len(browser.contexts)))
        return 0
    except Exception as exc:
        print("SMOKE_FAILED：%s" % exc, file=sys.stderr)
        return 1
    finally:
        # 先杀 Chrome 进程再删目录（Windows 目录占用限制，同 harvest）
        _close_cdp(proc, browser, pw)
        shutil.rmtree(smoke_dir, ignore_errors=True)


def main():
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="cnnc_auth_browser.py",
        description="CNNC SSO 浏览器登录 / OC 收割工具"
                    "（子进程纯净 Chrome + Playwright CDP 接管，绕过 WAF 自动化检测）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="人工一次性登录并保存 SSO 会话（storage_state）")

    hp = sub.add_parser(
        "harvest",
        help="用已有会话收割 OC 授权码（stdout 打印 OC=<code>；浏览器内交换"
             "成功时追加单行 AT=<token>）")
    hp.add_argument("--headless", action="store_true",
                    help="无头模式运行（默认有头：WAF 可能检测 headless，真窗口更稳）")

    sub.add_parser("smoke", help="离线自检：headless 启动纯净 Chrome 并 CDP 接管，验证链路")

    args = parser.parse_args()
    if args.command == "login":
        return cmd_login(args)
    if args.command == "smoke":
        return cmd_smoke(args)
    return cmd_harvest(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
