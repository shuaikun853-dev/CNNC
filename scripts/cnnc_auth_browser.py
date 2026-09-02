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
    login    人工一次性登录（账号 + 滑块 + 短信），会话持久化到
             data/cnnc_auth_profile/，并导出 data/cnnc_auth_storage_state.json
    harvest  用已保存会话只注入 5 个 SSO 会话 cookie（全量注入含陈旧 K5 等
             WAF cookie 会让门户首页卡在 412 挑战循环；WAF 空状态挑战自算
             反而秒过），从门户首页 https://sso.cnncecp.com/ 进入，自动跟随
             302 链（→ /portal/rest/selfcare/redirect/user →
             /esc-sso/oauth2.0/authorize）；OC 主通道从 authorize 302 响应的
             Location 头捕获 code=OC-xxx（比等浏览器落地 cnnc-web 更快，且
             避开 SPA 消费竞态），兜底通道为落地页 URL 含 code=OC-。事先在
             网络层拦截浏览器内 SPA 的 anon/account/login 请求作兜底，防止
             OC 授权码被前端消费（OC 一次性，被消费后外部换 token 报
             040000）；成功后把服务器轮换的 cookie 回写 storage_state
    smoke    离线自检：headless 启动纯净 Chrome 并 CDP 接管，打印版本验证链路

退出码:
    0   成功（harvest 向 stdout 打印单行 OC=<code>）
    1   启动链路失败（Chrome 未找到 / 端口占用 / CDP 接管失败 / 无浏览器上下文）
    2   SSO 会话已失效（SSO_LOGIN_REQUIRED，需重新 login）
    3   login 超时（LOGIN_TIMEOUT）
    4   storage_state 不存在（需先 login）
    5   harvest 30 秒内未拿到 OC
    130 用户中断
"""

import argparse
import json
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ROOT = 本脚本所在目录（scripts/）的上一级，即 cnnc-ops-agent/
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROFILE_DIR = DATA_DIR / "cnnc_auth_profile"
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


def _launch_chrome_cdp(url=None, headless=False):
    """子进程启动纯净系统 Chrome（带远程调试端口），并用 CDP 接管。

    浏览器本身不带任何自动化标记（无 --enable-automation 等 flag，
    navigator.webdriver=false），用于绕过 WAF 对 Playwright 托管浏览器的检测。

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

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # 3. 启动纯净 Chrome：无任何自动化 flag
    chrome_args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % PROFILE_DIR,
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
    """人工一次性登录：纯净 Chrome 打开真窗口完成登录，检测到 SSO 会话 cookie 后导出 storage_state。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("正在启动纯净 Chrome…请在窗口中完成 CNNC 登录（账号+滑块+短信）",
          flush=True)
    proc = browser = pw = None
    try:
        # URL 作为启动参数直接打开，不用 page.goto
        proc, browser, pw = _launch_chrome_cdp(url=LOGIN_ENTRY)
        ctx = browser.contexts[0] if browser.contexts else None
        if ctx is None:
            print("CDP_NO_CONTEXT：CDP 接管后未找到浏览器上下文", file=sys.stderr)
            return 1
        deadline = time.time() + LOGIN_TIMEOUT_SEC
        cookie_err_count = 0
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
            if "TGC" in names or "SESSION" in names:
                time.sleep(3)  # 让 cookie 稳定
                ctx.storage_state(path=str(STORAGE_STATE))
                cnn = [c for c in ctx.cookies()
                       if "cnncecp" in (c.get("domain") or "")]
                print("LOGIN_OK 已导出 storage_state -> %s" % STORAGE_STATE)
                # 只打名字和长度，不打值，防泄漏
                print("sso 域 cookie: %s" % ", ".join(
                    "%s(len=%d)" % (c["name"], len(c.get("value") or ""))
                    for c in cnn))
                return 0
            time.sleep(2)
        print("LOGIN_TIMEOUT：请人工完成登录后重试", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("LOGIN_CANCELLED：用户中断", file=sys.stderr)
        return 130
    finally:
        _close_cdp(proc, browser, pw)


def cmd_harvest(args):
    """用已保存的 SSO 会话（只注会话 cookie）从门户首页进入 302 链，收割 OC。

    主通道：监听 /esc-sso/oauth2.0/authorize 的 302 响应，从 Location 头
    提取 code=OC-xxx；兜底：落地页 URL 轮询含 code=OC-。
    """
    if not STORAGE_STATE.exists():
        print("STORAGE_STATE_NOT_FOUND：未找到 %s，请先运行 "
              "python scripts/cnnc_auth_browser.py login 完成登录" % STORAGE_STATE,
              file=sys.stderr)
        return 4

    proc = browser = pw = None
    try:
        # 默认有头：WAF 可能检测 headless，真窗口更稳
        proc, browser, pw = _launch_chrome_cdp(headless=args.headless)
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

        page = ctx.new_page()
        oc_holder = []

        def _on_response(response):
            """监听器薄封装：捕获逻辑在纯函数 capture_oc_from_response 中，
            首个 OC 生效；异常已在纯函数内吞掉，不打断收割流程。"""
            oc = capture_oc_from_response(response)
            if oc and not oc_holder:
                oc_holder.append(oc)

        page.on("response", _on_response)
        # 注意：不能用上下文级请求拦截（Playwright 路由 API / Fetch 域）拦截
        # anon/account/login——实测在 CDP 接管的浏览器上启用请求拦截会让门户
        # 首页的 302 链整体卡死。防 SPA 消费 OC 改用"捕获即关页"：拿到 OC
        # 立即 page.close()，cnnc-web 的 SPA 根本来不及发起交换请求。
        try:
            # 不能直接 goto AUTHORIZE_URL（会被 WAF 挑战页卡死），改从门户
            # 首页进入，靠 302 链自然到达 /authorize
            page.goto(HARVEST_ENTRY, wait_until="commit", timeout=20000)
        except Exception as exc:
            # 超时/异常仅告警，继续轮询
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
                try:
                    # 回写服务器轮换后的 cookie
                    ctx.storage_state(path=str(STORAGE_STATE))
                except Exception as exc:
                    print("WARN：storage_state 回写失败（不影响本次 OC）: %s" % exc,
                          file=sys.stderr)
                print("OC=%s" % code)
                return 0
            if "/esc-sso/login" in final_url or final_url.startswith(SSO_ORIGIN + "/login"):
                print("SSO_LOGIN_REQUIRED：SSO 会话已失效，请运行 "
                      "cnnc_auth_browser.py login 重新登录", file=sys.stderr)
                return 2
            time.sleep(0.3)
        print("HARVEST_TIMEOUT：未在 %d 秒内获得 OC，最终 URL: %s"
              % (HARVEST_TIMEOUT_SEC, final_url), file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("HARVEST_CANCELLED：用户中断", file=sys.stderr)
        return 130
    finally:
        _close_cdp(proc, browser, pw)


def cmd_smoke(args):
    """离线自检：仅验证 纯净 Chrome 子进程启动 + CDP 接管 链路（headless，不访问页面）。"""
    proc = browser = pw = None
    try:
        proc, browser, pw = _launch_chrome_cdp(url=None, headless=True)
        print("SMOKE_OK browser.version=%s contexts=%d"
              % (browser.version, len(browser.contexts)))
        return 0
    except Exception as exc:
        print("SMOKE_FAILED：%s" % exc, file=sys.stderr)
        return 1
    finally:
        _close_cdp(proc, browser, pw)


def main():
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="cnnc_auth_browser.py",
        description="CNNC SSO 浏览器登录 / OC 收割工具"
                    "（子进程纯净 Chrome + Playwright CDP 接管，绕过 WAF 自动化检测）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="人工一次性登录并保存 SSO 会话（storage_state）")

    hp = sub.add_parser("harvest", help="用已有会话收割 OC 授权码（stdout 打印单行 OC=<code>）")
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
