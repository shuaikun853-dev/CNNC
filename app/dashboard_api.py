"""Read-only STEP_1 dashboard routes + strictly guarded local operator actions.

GET 层保持零业务写。新增 POST 只允许本机驾驶舱触发既有 CLI 子进程
(closeout/re-export/rebuild)或只读 refresh-progress;每个 POST 首行执行
_guard_operator_action(Host/Origin/X-Dashboard-Action-Token),绝无轮询触写。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.services.dashboard_actions import DashboardActionError, get_action_runner
from app.services.dashboard_manual_ops import (
    get_progress_service_for,
    read_operator_actions,
    refresh_job_progress,
)
from app.services.dashboard_projection import (
    build_dashboard_summary,
    empty_customer_price_projection,
    project_customer_price,
)
# Issue #13(2026-09-14):预爬看板只读投影 + 手动重爬 runner;模块级导入,
# 测试通过 monkeypatch app.dashboard_api 属性注入替身(与 build_dashboard_summary 同约定)。
from app.services.prewarm_dashboard_projection import (
    PAGE_SIZE_MAX,
    build_prewarm_dashboard_summary,
    get_prewarm_link_detail,
    list_prewarm_links,
)
from app.services.prewarm_manual_recrawl import (
    ManualRecrawlError,
    get_manual_recrawl_runner,
)
from app.services.incremental_audit_runtime import runtime_paths
from app.services.production_identity import get_production_identity_service
from app.services.saas_flow_diagnostic import get_saas_flow_diagnostic
# 常量单源（2026-09-10）:认证维护锁/cURL 结果工件路径由 scripts.cnnc_auth_refresh
# 定义,dashboard 只读引用并在下方绑定为模块常量(测试可 monkeypatch 注入 tmp)。
from scripts.cnnc_auth_refresh import (
    AUTH_MAINTENANCE_LOCK_PATH,
    CURL_REFRESH_RESULT_PATH,
)

router = APIRouter(prefix="/dashboard", tags=["step1-dashboard"])
_STATIC_DIR = Path(__file__).resolve().parent / "static" / "dashboard"
_ROOT_DIR = Path(__file__).resolve().parents[1]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# saas-flow 只接受 date(YYYY-MM-DD)与 refresh=1(缓存行为标志);
# tenant/pool/schema/table/sql 等任何 SQL 或 scope 可控参数一律 400。
_SAAS_FLOW_ALLOWED_PARAMS = frozenset({"date", "refresh"})
# customer-price 只接受 date(YYYY-MM-DD)与 filter(白名单枚举),其余一律 400。
_CUSTOMER_PRICE_ALLOWED_PARAMS = frozenset({"date", "filter"})
_CUSTOMER_PRICE_FILTERS = frozenset({"all", "unacquired", "retrying", "terminal"})

# ---- 本机受控操作台(2026-08-28):进程启动时一次性生成的操作令牌 ----
_ACTION_TOKEN = secrets.token_urlsafe(32)
# TestClient 默认 Host 是 "testserver"(历史版本 "testclient"),与回环地址
# 一起放行,仅服务于本机进程测试;浏览器只能来自 127.0.0.1/localhost。
_LOOPBACK_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "::1", "testserver", "testclient"}
)


def _valid_business_date(value: str) -> bool:
    """格式 + 真实日历日期(2026-02-31/2026-13-01 等一律 400,不得落入 503)。"""
    if not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _today() -> str:
    from app.services.incremental_audit_runtime import business_date_in_shanghai
    return business_date_in_shanghai()


# 静态资源 cache busting:磁盘 index.html 不手写 ?v=(保持纯静态契约),
# 由服务端注入 shell.css/app.css/app.js 的 mtime_ns 版本——文件更新 → URL 变 →
# 浏览器必拉新。shell.css 为 Issue #13 多页外壳抽取(Issue #13 前的 index.html
# 不引用它,replace() 缺失时为 no-op,旧页面不受影响)。
_DASHBOARD_ASSETS = ("shell.css", "app.css", "app.js")


def _asset_version(path: Path) -> str:
    return str(path.stat().st_mtime_ns)


def _dashboard_index_html() -> str:
    """index.html + 静态资源 mtime 版本注入:文件更新 → URL 变 → 浏览器必拉新。"""
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in _DASHBOARD_ASSETS:
        version = _asset_version(_STATIC_DIR / name)
        html = html.replace(f"/dashboard/static/{name}", f"/dashboard/static/{name}?v={version}")
    return html


@router.get("/", include_in_schema=True)
def dashboard_index() -> HTMLResponse:
    return HTMLResponse(_dashboard_index_html(), headers={"Cache-Control": "no-cache"})


@router.get("/api/summary")
def dashboard_summary(request: Request, date: str = Query(default=None)) -> Response:
    business_date = date or _today()
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE", "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    try:
        payload = build_dashboard_summary(
            business_date=business_date,
            progress_service=_summary_progress_service(business_date),
        )
    except Exception as exc:  # 只读投影失败统一降级,不暴露堆栈
        raise HTTPException(status_code=503, detail={
            "error_code": "STATUS_READ_FAILED", "message": "状态读取失败,请稍后重试",
            "type": type(exc).__name__}) from exc
    etag = f'"{payload["fingerprint"]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return JSONResponse(payload, headers={"ETag": etag, "Cache-Control": "no-cache"})


@router.get("/api/saas-flow")
def dashboard_saas_flow(
    request: Request,
    date: str = Query(default=None),
    refresh: str = Query(default=None),
) -> Response:
    """SaaS 首次采集链路只读诊断:独立故障边界,绝不影响 /api/summary。

    DB 不可用/未配置时返回 200 + 明确降级状态,由面板降级显示;
    只有诊断服务自身意外异常才 503。永不执行/接受任何业务写。
    """
    unknown = set(request.query_params.keys()) - _SAAS_FLOW_ALLOWED_PARAMS
    if unknown:
        raise HTTPException(status_code=400, detail={
            "error_code": "UNKNOWN_QUERY_PARAM",
            "message": "不支持的查询参数:" + ",".join(sorted(unknown))
                       + ";本接口只接受 date(YYYY-MM-DD)与 refresh=1"})
    if refresh is not None and refresh != "1":
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_REFRESH_PARAM", "message": "refresh 仅接受 1"})
    business_date = date or _today()
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE", "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    try:
        payload = get_saas_flow_diagnostic(business_date, force_refresh=refresh == "1")
    except Exception as exc:  # 诊断失败只降级本面板,不暴露堆栈/凭据
        raise HTTPException(status_code=503, detail={
            "error_code": "SAAS_FLOW_READ_FAILED", "message": "SaaS 链路诊断读取失败,请稍后重试",
            "type": type(exc).__name__}) from exc
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/api/customer-price")
def dashboard_customer_price(
    request: Request,
    date: str = Query(default=None),
    filter: str = Query(default="all"),
) -> Response:
    """客户价行级只读投影:GET 铁律——零业务写、零远端网络、文本全部脱敏。

    state 文件不存在时返回 200 空结构(今日尚未开始不是错误);filter 非法
    400 BAD_FILTER_PARAM;未知查询参数 400(与 saas-flow 同风格)。
    """
    unknown = set(request.query_params.keys()) - _CUSTOMER_PRICE_ALLOWED_PARAMS
    if unknown:
        raise HTTPException(status_code=400, detail={
            "error_code": "UNKNOWN_QUERY_PARAM",
            "message": "不支持的查询参数:" + ",".join(sorted(unknown))
                       + ";本接口只接受 date(YYYY-MM-DD)与 filter"})
    if filter not in _CUSTOMER_PRICE_FILTERS:
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_FILTER_PARAM",
            "message": "filter 仅接受 all/unacquired/retrying/terminal"})
    business_date = date or _today()
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE", "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    paths = runtime_paths(business_date)
    if not paths.state_path.is_file():
        return JSONResponse(
            empty_customer_price_projection(business_date, filter),
            headers=_NO_STORE)
    try:
        payload = project_customer_price(paths.state_path, business_date, filter=filter)
    except Exception as exc:  # 只读投影失败统一降级,不暴露堆栈
        raise HTTPException(status_code=503, detail={
            "error_code": "CUSTOMER_PRICE_READ_FAILED", "message": "客户价明细读取失败,请稍后重试",
            "type": type(exc).__name__}) from exc
    return JSONResponse(payload, headers=_NO_STORE)


# ======================================================================
# 本机受控操作台:operator-context / guarded POST actions / action 查询
# ======================================================================

_NO_STORE = {"Cache-Control": "no-store"}


class _CloseoutBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_date: str | None = None


class _ReexportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_date: str | None = None
    expected_updated_at: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)


class _RebuildBody(_ReexportBody):
    confirm: bool = False


class _RefreshProgressBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_date: str | None = None


def _guard_operator_action(request: Request, expected_action_type: str) -> None:
    """Local-only, token-bearing guard; must be the first line of every POST.

    1. Host header 的 host 名必须属于本机(端口任意;TestClient 的
       testserver/testclient 供本机进程测试)。
    2. Origin 若存在必须同源(scheme http/https 且 host 同上);缺失 Origin
       的非浏览器本机调用仍放行。
    3. X-Dashboard-Action-Token 必须等于进程启动时生成的 _ACTION_TOKEN。
    """
    host = urlsplit("//" + (request.headers.get("host") or "")).hostname
    if str(host or "").lower() not in _LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail={
            "error_code": "NON_LOCAL_HOST",
            "message": "驾驶舱操作仅允许本机(127.0.0.1/localhost)访问"})
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or \
                str(parsed.hostname or "").lower() not in _LOOPBACK_HOSTS:
            raise HTTPException(status_code=403, detail={
                "error_code": "FOREIGN_ORIGIN",
                "message": "拒绝跨源驾驶舱操作"})
    if request.headers.get("x-dashboard-action-token") != _ACTION_TOKEN:
        raise HTTPException(status_code=403, detail={
            "error_code": "INVALID_ACTION_TOKEN",
            "message": "操作令牌缺失或不正确;请从 /dashboard/api/operator-context 获取"})


def _summary_progress_service(business_date: str):
    """Live-progress overlay for the summary; any failure degrades to None."""
    try:
        paths = runtime_paths(business_date)
        if paths.state_path.is_file():
            return get_progress_service_for(paths.state_path)
    except Exception:
        return None
    return None


def _load_remote_job(job_id: int, business_date: str) -> dict | None:
    """Read-only durable lookup of one remote job inside its business date."""
    try:
        paths = runtime_paths(business_date)
    except Exception:
        return None
    if not paths.state_path.is_file():
        return None
    uri = f"file:{Path(paths.state_path).resolve().as_posix()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            """SELECT j.* FROM remote_jobs j
               JOIN daily_runs r ON r.id = j.run_id
               WHERE j.id=? AND r.business_date=?""",
            (int(job_id), business_date),
        ).fetchone()
        return dict(row) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _identity_ok(identity: object) -> bool:
    parts = str(identity or "").split("|")
    return len(parts) == 3 and all(part.strip() for part in parts)


def _resolve_action_body_date(value: str | None) -> str:
    return str(value) if value else _today()


def _start_action_or_http_error(**kwargs):
    try:
        return get_action_runner().start(**kwargs)
    except DashboardActionError as exc:
        if exc.code == "ACTION_IN_FLIGHT":
            raise HTTPException(status_code=409, detail={
                "error_code": "ACTION_IN_FLIGHT",
                "existing_action_id": exc.existing_action_id,
                "message": "同类型操作正在执行,请等待其完成"}) from exc
        raise HTTPException(status_code=500, detail={
            "error_code": exc.code, "message": str(exc)}) from exc


def _accepted_action_response(handle) -> JSONResponse:
    return JSONResponse(
        {"action_id": handle.action_id, "status": handle.status,
         "poll": f"/dashboard/api/actions/{handle.action_id}"},
        status_code=202, headers=_NO_STORE)


@router.get("/api/operator-context")
def dashboard_operator_context() -> JSONResponse:
    """One-page bootstrap for the operator console (per-process token)."""
    return JSONResponse(
        {"action_token": _ACTION_TOKEN, "business_date": _today(), "actions_enabled": True},
        headers=_NO_STORE)


@router.post("/api/actions/closeout")
def dashboard_action_closeout(request: Request, body: _CloseoutBody | None = None) -> JSONResponse:
    _guard_operator_action(request, "closeout")
    business_date = _resolve_action_body_date(body.business_date if body is not None else None)
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE",
            "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    handle = _start_action_or_http_error(
        action_type="closeout", business_date=business_date)
    return _accepted_action_response(handle)


def _validated_job_for_action(
    job_id: int, business_date: str, expected_updated_at: str | None
) -> dict:
    job = _load_remote_job(job_id, business_date)
    if job is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "JOB_NOT_FOUND",
            "message": f"业务日期 {business_date} 下不存在远端任务 {job_id}"})
    if expected_updated_at is not None and \
            str(expected_updated_at) != str(job.get("updated_at") or ""):
        raise HTTPException(status_code=409, detail={
            "error_code": "STALE_ACTION",
            "message": "任务状态已变化(durable updated_at 不匹配),请刷新后重试"})
    if not _identity_ok(job.get("remote_identity")):
        raise HTTPException(status_code=409, detail={
            "error_code": "INVALID_REMOTE_STATE",
            "message": "该任务缺少可用的精确远端身份(未提交/数据不完整),无法执行远端操作"})
    return job


@router.post("/api/jobs/{job_id}/re-export")
def dashboard_job_reexport(
    request: Request, job_id: int, body: _ReexportBody | None = None
) -> JSONResponse:
    _guard_operator_action(request, "re-export")
    business_date = _resolve_action_body_date(body.business_date if body is not None else None)
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE",
            "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    expected = body.expected_updated_at if body is not None else None
    job = _validated_job_for_action(job_id, business_date, expected)
    timeout = body.timeout_seconds if body is not None else None
    handle = _start_action_or_http_error(
        action_type="re-export",
        business_date=business_date,
        remote_job_id=int(job["id"]),
        remote_identity=str(job.get("remote_identity") or "").strip(),
        timeout_seconds=timeout,
    )
    return _accepted_action_response(handle)


@router.post("/api/jobs/{job_id}/rebuild")
def dashboard_job_rebuild(
    request: Request, job_id: int, body: _RebuildBody | None = None
) -> JSONResponse:
    _guard_operator_action(request, "rebuild")
    if body is None or body.confirm is not True:
        raise HTTPException(status_code=400, detail={
            "error_code": "REBUILD_CONFIRM_REQUIRED",
            "message": "重建会停用旧任务并创建替代任务,必须显式 confirm=true"})
    business_date = _resolve_action_body_date(body.business_date if body is not None else None)
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE",
            "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    expected = body.expected_updated_at if body is not None else None
    job = _validated_job_for_action(job_id, business_date, expected)
    timeout = body.timeout_seconds if body is not None else None
    handle = _start_action_or_http_error(
        action_type="rebuild",
        business_date=business_date,
        remote_job_id=int(job["id"]),
        remote_identity=str(job.get("remote_identity") or "").strip(),
        timeout_seconds=timeout,
    )
    return _accepted_action_response(handle)


@router.post("/api/jobs/{job_id}/refresh-progress")
def dashboard_job_refresh_progress(
    request: Request, job_id: int, body: _RefreshProgressBody | None = None
) -> JSONResponse:
    _guard_operator_action(request, "refresh-progress")
    business_date = _resolve_action_body_date(body.business_date if body is not None else None)
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE",
            "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    job = _validated_job_for_action(job_id, business_date, None)
    status = refresh_job_progress(runtime_paths(business_date).state_path, job)
    return JSONResponse(status, headers=_NO_STORE)


# ======================================================================
# CNNC 认证:只读状态 + 受控本地登录浏览器启动 + 手动 cURL 刷新
# ======================================================================

_CNNC_LOGIN_THROTTLE_SECONDS = 60.0
# 进程内单例节流:60 秒内只允许启动一次登录浏览器(monotonic 时间戳)。
_cnnc_login_launched_at: float | None = None
# 手动 cURL 刷新的独立 60 秒节流计时(与 login 节流互不影响)。
_cnnc_curl_refresh_launched_at: float | None = None
# 手动恢复结果工件(scripts/cnnc_auth_refresh.py --login-and-recover 写入);
# status 路由只读合并进 payload.last_manual_recovery,模块常量供测试注入。
_CNNC_RECOVERY_RESULT_PATH = _ROOT_DIR / "data" / "cnnc_login_recovery_result.json"
# cURL 刷新结果工件/认证维护锁路径:常量单源自 scripts.cnnc_auth_refresh,
# 绑定为模块常量供测试 monkeypatch 注入 tmp(绝不触碰真实 data/)。
_CURL_REFRESH_RESULT_PATH = CURL_REFRESH_RESULT_PATH
_AUTH_MAINTENANCE_LOCK_PATH = AUTH_MAINTENANCE_LOCK_PATH


def _load_result_artifact(path) -> dict | None:
    """只读加载结果工件 JSON;缺文件/坏 JSON/顶层非对象一律返回 None。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_manual_recovery_result() -> dict | None:
    """只读加载手动恢复结果工件(scripts --login-and-recover 写入)。"""
    return _load_result_artifact(_CNNC_RECOVERY_RESULT_PATH)


def _load_curl_refresh_result() -> dict | None:
    """只读加载 cURL 刷新结果工件(scripts --refresh-curl 写入)。"""
    return _load_result_artifact(_CURL_REFRESH_RESULT_PATH)


@router.get("/api/cnnc-auth/status")
def dashboard_cnnc_auth_status() -> JSONResponse:
    """CNNC 认证只读状态;get_status 纯本地读(state/cURL 文件),无网络副作用。"""
    from scripts.cnnc_auth_core import CnncAuthProvider
    try:
        payload = CnncAuthProvider().get_status()
    except Exception as exc:  # 读取失败只降级本面板,不暴露堆栈
        raise HTTPException(status_code=503, detail={
            "error_code": "CNNC_AUTH_STATUS_READ_FAILED",
            "message": "CNNC 认证状态读取失败,请稍后重试",
            "type": type(exc).__name__}) from exc
    recovery = _load_manual_recovery_result()
    if recovery is not None:
        payload["last_manual_recovery"] = recovery
    curl_refresh = _load_curl_refresh_result()
    if curl_refresh is not None:
        payload["last_curl_refresh"] = curl_refresh
    return JSONResponse(payload, headers=_NO_STORE)


@router.post("/api/cnnc-auth/login")
def dashboard_cnnc_auth_login(request: Request) -> JSONResponse:
    """启动登录+自动恢复后台进程(detached;不占 action 槽、不阻塞)。

    载荷为 cnnc_auth_refresh.py --login-and-recover:其内部再拉起浏览器 login,
    人工完成滑块/短信后自动 harvest→换 AT→verify→publish(登录后不自动恢复的
    review Finding 3 修复),本端点只负责 detached 拉起并立即返回。
    """
    _guard_operator_action(request, "cnnc-auth-login")
    # 跨进程 single-flight（2026-09-10 Blocker 3）：真实 --login-and-recover
    # 进程仍持 OS 锁时直接 409（检查优先于 60 秒节流——节流会随服务重启/时间
    # 流逝失效，OS 锁不会），绝不叠加拉起第二个恢复进程。inspect 只探活不改
    # 变运行状态；stale 残留（锁文件在、锁未被持有）不影响启动。锁路径经
    # scripts.cnnc_auth_refresh 模块属性解析，测试可注入 tmp。
    from app.services.daily_audit_store import ProcessLock
    from scripts.cnnc_auth_refresh import LOGIN_RECOVERY_LOCK_PATH
    lock_info = ProcessLock.inspect(LOGIN_RECOVERY_LOCK_PATH)
    if lock_info["active"]:
        # 持有者语义区分文案（owner metadata command 可读回）:cURL 刷新持有
        # → 维护提示;login-and-recover/其他 → 原登录窗口提示。error_code
        # 保持 CNNC_LOGIN_ALREADY_RUNNING 兼容（既有前端/测试不破）。
        if str(lock_info.get("command") or "") == "curl-refresh":
            busy_text = "CNNC 认证维护正在进行（cURL 刷新），请先完成当前操作"
        else:
            busy_text = "重新登录流程已经在进行中，请完成当前登录窗口"
        return JSONResponse(
            {"error": busy_text,
             "error_code": "CNNC_LOGIN_ALREADY_RUNNING",
             "status_name": "CNNC_LOGIN_ALREADY_RUNNING"},
            status_code=409, headers=_NO_STORE)
    global _cnnc_login_launched_at
    now = time.monotonic()
    if _cnnc_login_launched_at is not None and \
            now - _cnnc_login_launched_at < _CNNC_LOGIN_THROTTLE_SECONDS:
        return JSONResponse(
            {"error": "登录浏览器已在启动中（60 秒内）"}, status_code=429,
            headers=_NO_STORE)
    popen_kwargs: dict = {}
    if os.name == "nt":  # Windows:脱离本服务进程,不随 dashboard 重启被杀
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        subprocess.Popen(
            [sys.executable, str(_ROOT_DIR / "scripts" / "cnnc_auth_refresh.py"),
             "--login-and-recover"],
            cwd=str(_ROOT_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **popen_kwargs)
    except OSError as exc:  # FileNotFoundError 等:简讯返回,不暴露堆栈
        raise HTTPException(status_code=500, detail={
            "error_code": "CNNC_LOGIN_LAUNCH_FAILED",
            "message": "登录浏览器启动失败,请稍后重试"}) from exc
    _cnnc_login_launched_at = now
    return JSONResponse(
        {"started": True,
         "note": "登录浏览器已启动；完成滑块/短信后系统将自动续签并恢复（无需其他操作）"},
        status_code=202, headers=_NO_STORE)


@router.post("/api/cnnc-auth/refresh-curl")
def dashboard_cnnc_auth_refresh_curl(request: Request) -> JSONResponse:
    """手动强制刷新认证并同步 cURL（detached 拉起 --refresh-curl）。

    载荷为 scripts/cnnc_auth_refresh.py --refresh-curl:force 刷新 + 严格
    cURL 后置核验 + 结果工件（status 路由透传 last_curl_refresh）。守卫
    齐备 + 认证维护锁先检互斥（与 login 双向,零 Popen 拒绝,检查优先于
    节流）+ 60 秒独立进程内节流;结构性无任何 closeout/finalized 状态
    检查——手工刷新不受收口限制。本端点只负责 detached 拉起并立即返回。
    """
    _guard_operator_action(request, "cnnc-auth-refresh-curl")
    # 跨进程 single-flight:认证维护锁被任何持有者（login-and-recover/
    # curl-refresh）占用时直接 409,绝不叠加第二个认证维护进程。
    from app.services.daily_audit_store import ProcessLock
    if ProcessLock.inspect(_AUTH_MAINTENANCE_LOCK_PATH)["active"]:
        return JSONResponse(
            {"error": "CNNC 认证维护正在进行，请先完成当前操作",
             "error_code": "AUTH_MAINTENANCE_BUSY",
             "status_name": "AUTH_MAINTENANCE_BUSY"},
            status_code=409, headers=_NO_STORE)
    global _cnnc_curl_refresh_launched_at
    now = time.monotonic()
    if _cnnc_curl_refresh_launched_at is not None and \
            now - _cnnc_curl_refresh_launched_at < _CNNC_LOGIN_THROTTLE_SECONDS:
        return JSONResponse(
            {"error": "cURL 刷新已在启动中（60 秒内），请稍候"}, status_code=429,
            headers=_NO_STORE)
    popen_kwargs: dict = {}
    if os.name == "nt":  # Windows:脱离本服务进程,不随 dashboard 重启被杀
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        subprocess.Popen(
            [sys.executable, str(_ROOT_DIR / "scripts" / "cnnc_auth_refresh.py"),
             "--refresh-curl"],
            cwd=str(_ROOT_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **popen_kwargs)
    except OSError as exc:  # FileNotFoundError 等:简讯返回,不暴露堆栈
        raise HTTPException(status_code=500, detail={
            "error_code": "CNNC_CURL_REFRESH_LAUNCH_FAILED",
            "message": "cURL 刷新启动失败,请稍后重试"}) from exc
    _cnnc_curl_refresh_launched_at = now
    return JSONResponse(
        {"started": True,
         "note": "正在强制刷新认证并同步 cURL，完成后此处显示结果"},
        status_code=202, headers=_NO_STORE)


# ======================================================================
# 生产运行版本(Production Deployment Identity):只读部署身份投影
# ======================================================================

@router.get("/api/production-identity")
def dashboard_production_identity(request: Request) -> JSONResponse:
    """生产运行版本只读投影:进程启动身份/本地 git/GitHub 生产分支 HEAD/
    计划任务注册状态四路证据 + comparison 结论。

    纯 GET 只读;不接受任何查询参数;service.snapshot() 契约永不抛,
    此处仍兜底 503(不暴露堆栈)。绝不触发任何 git/调度器/网络写操作。
    """
    if request.query_params:
        unknown = sorted(request.query_params.keys())
        raise HTTPException(status_code=400, detail={
            "error_code": "UNKNOWN_QUERY_PARAM",
            "message": "不支持的查询参数:" + ",".join(unknown)
                       + ";本接口不接受任何查询参数"})
    try:
        payload = get_production_identity_service().snapshot()
    except Exception as exc:  # 契约兜底:snapshot 永不抛,仍防御 503
        raise HTTPException(status_code=503, detail={
            "error_code": "PRODUCTION_IDENTITY_READ_FAILED",
            "message": "生产运行版本读取失败,请稍后重试",
            "type": type(exc).__name__}) from exc
    return JSONResponse(payload, headers=_NO_STORE)


# 进程启动即捕获 startup 身份:uvicorn 导入 app.main 的此刻创建单例。
# 若懒创建到首次请求才初始化,startup SHA 会捕获于首访时刻而非进程启动,
# 从而掩盖“代码已更新但进程没重启”(git pull 后未重启的真实场景)。
get_production_identity_service()


@router.get("/api/actions")
def dashboard_actions_list() -> JSONResponse:
    runner = get_action_runner()
    running = [{"action_id": h.action_id, "action_type": h.action_type,
                "status": h.status, "target": h.target, "started_at": h.started_at,
                "business_date": h.business_date}
               for h in runner.list_recent(20) if h.status == "RUNNING"]
    try:
        recent = read_operator_actions(runtime_paths(_today()).run_dir, limit=20)
    except Exception:
        recent = []
    # 顶层 business_date:前端 closeout RUNNING 匹配的服务端权威日期
    # (bootstrap 失败时 operator-context 的 businessDate 为空,靠它兜底)。
    return JSONResponse({"running": running, "recent": recent,
                         "business_date": _today()}, headers=_NO_STORE)


@router.get("/api/actions/{action_id}")
def dashboard_action_status(action_id: str) -> JSONResponse:
    handle = get_action_runner().get(action_id)
    if handle is not None:
        return JSONResponse(
            {"action_id": handle.action_id, "action_type": handle.action_type,
             "status": handle.status, "outcome": handle.outcome,
             "started_at": handle.started_at},
            headers=_NO_STORE)
    durable = get_action_runner().durable_status(
        action_id, runtime_paths(_today()).run_dir)
    if durable is not None:
        return JSONResponse(durable, headers=_NO_STORE)
    raise HTTPException(status_code=404, detail={
        "error_code": "ACTION_NOT_FOUND", "message": "未找到该操作记录"})


# ======================================================================
# Issue #13(2026-09-14):客户链接预爬页 + 只读投影 API + 受控手动重爬。
# 薄适配层铁律:本文件无 SQL、无状态解释——只做参数白名单校验、委托
# app.services.prewarm_dashboard_projection(只读投影)/
# app.services.prewarm_manual_recrawl(受控单 URL 重爬)与错误码映射。
# ======================================================================

_PREWARM_PAGE_ASSETS = ("shell.css", "prewarm.css", "prewarm.js")
# links 列表查询参数白名单:date + 分页 + 过滤;其余一律 400。
_PREWARM_LINK_ALLOWED_PARAMS = frozenset({
    "date", "page", "page_size", "mall", "status", "source",
    "missing_title", "unverified_sku", "unresolved_sku", "failure", "cache_miss",
})
# 布尔型过滤参数只接受 "1"/"true"(大小写不敏感),其余 400 BAD_FILTER_PARAM。
_PREWARM_BOOL_FILTER_PARAMS = frozenset({
    "missing_title", "unverified_sku", "unresolved_sku", "failure", "cache_miss",
})
# page_size 上限单源:直接引用投影层的 PAGE_SIZE_MAX(不再本地复制 200)。


class _PrewarmRecrawlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_date: str | None = None
    expected_original_url: str | None = None
    expected_status: str | None = None


def _prewarm_page_html() -> str:
    """prewarm.html + 静态资源 mtime 版本注入(与 _dashboard_index_html 同法)。"""
    html = (_STATIC_DIR / "prewarm.html").read_text(encoding="utf-8")
    for name in _PREWARM_PAGE_ASSETS:
        version = _asset_version(_STATIC_DIR / name)
        html = html.replace(f"/dashboard/static/{name}", f"/dashboard/static/{name}?v={version}")
    return html


@router.get("/prewarm", include_in_schema=True)
def prewarm_page() -> HTMLResponse:
    return HTMLResponse(_prewarm_page_html(), headers={"Cache-Control": "no-cache"})


@router.get("/api/prewarm/summary")
def prewarm_summary(request: Request, date: str = Query(default=None)) -> Response:
    """预爬 KPI 只读摘要:降级负载是投影层的正常 200 返回,只有意外异常才 503。"""
    unknown = set(request.query_params.keys()) - {"date"}
    if unknown:
        raise HTTPException(status_code=400, detail={
            "error_code": "UNKNOWN_QUERY_PARAM",
            "message": "不支持的查询参数:" + ",".join(sorted(unknown))
                       + ";本接口只接受 date(YYYY-MM-DD)"})
    business_date = date or _today()
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE", "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    try:
        payload = build_prewarm_dashboard_summary(business_date)
    except Exception as exc:  # 只读投影失败统一降级,不暴露堆栈
        raise HTTPException(status_code=503, detail={
            "error_code": "PREWARM_SUMMARY_READ_FAILED", "message": "预爬状态读取失败,请稍后重试",
            "type": type(exc).__name__}) from exc
    return JSONResponse(payload, headers=_NO_STORE)


@router.get("/api/prewarm/links")
def prewarm_links(
    request: Request,
    date: str = Query(default=None),
    page: str = Query(default=None),
    page_size: str = Query(default=None),
    mall: str = Query(default=None),
    status: str = Query(default=None),
    source: str = Query(default=None),
    missing_title: str = Query(default=None),
    unverified_sku: str = Query(default=None),
    unresolved_sku: str = Query(default=None),
    failure: str = Query(default=None),
    cache_miss: str = Query(default=None),
) -> Response:
    """预爬链接分页列表(服务端过滤);分页/过滤全部在投影层完成,此处只校验。"""
    unknown = set(request.query_params.keys()) - _PREWARM_LINK_ALLOWED_PARAMS
    if unknown:
        raise HTTPException(status_code=400, detail={
            "error_code": "UNKNOWN_QUERY_PARAM",
            "message": "不支持的查询参数:" + ",".join(sorted(unknown))
                       + ";本接口只接受 date/page/page_size 与过滤参数"})
    business_date = date or _today()
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE", "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    page_value = 1
    if page is not None:
        try:
            page_value = int(page)
        except ValueError:
            raise HTTPException(status_code=400, detail={
                "error_code": "BAD_PAGE_PARAM", "message": "page 必须为 ≥1 的整数"})
        if page_value < 1:
            raise HTTPException(status_code=400, detail={
                "error_code": "BAD_PAGE_PARAM", "message": "page 必须为 ≥1 的整数"})
    page_size_value = 50
    if page_size is not None:
        try:
            page_size_value = int(page_size)
        except ValueError:
            raise HTTPException(status_code=400, detail={
                "error_code": "BAD_PAGE_SIZE_PARAM", "message": "page_size 必须为 1..200 的整数"})
        if not 1 <= page_size_value <= PAGE_SIZE_MAX:
            raise HTTPException(status_code=400, detail={
                "error_code": "BAD_PAGE_SIZE_PARAM", "message": "page_size 必须为 1..200 的整数"})
    filters: dict = {}
    for key, value in (("mall", mall), ("status", status), ("source", source)):
        if value is not None and value.strip():
            filters[key] = value.strip()
    for key, value in (("missing_title", missing_title),
                       ("unverified_sku", unverified_sku),
                       ("unresolved_sku", unresolved_sku),
                       ("failure", failure), ("cache_miss", cache_miss)):
        if value is None:
            continue
        lowered = value.strip().lower()
        if lowered not in ("1", "true"):
            raise HTTPException(status_code=400, detail={
                "error_code": "BAD_FILTER_PARAM",
                "message": key + " 仅接受 1 或 true"})
        filters[key] = True
    try:
        payload = list_prewarm_links(
            business_date, page=page_value, page_size=page_size_value, filters=filters)
    except Exception as exc:  # 只读投影失败统一降级,不暴露堆栈
        raise HTTPException(status_code=503, detail={
            "error_code": "PREWARM_LINKS_READ_FAILED", "message": "预爬链接读取失败,请稍后重试",
            "type": type(exc).__name__}) from exc
    return JSONResponse(payload, headers=_NO_STORE)


@router.get("/api/prewarm/links/{candidate_id}")
def prewarm_link_detail(
    request: Request, candidate_id: int, date: str = Query(default=None)
) -> Response:
    unknown = set(request.query_params.keys()) - {"date"}
    if unknown:
        raise HTTPException(status_code=400, detail={
            "error_code": "UNKNOWN_QUERY_PARAM",
            "message": "不支持的查询参数:" + ",".join(sorted(unknown))
                       + ";本接口只接受 date(YYYY-MM-DD)"})
    business_date = date or _today()
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE", "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    try:
        payload = get_prewarm_link_detail(business_date, int(candidate_id))
    except Exception as exc:  # 只读投影失败统一降级,不暴露堆栈
        raise HTTPException(status_code=503, detail={
            "error_code": "PREWARM_LINK_READ_FAILED", "message": "预爬链接读取失败,请稍后重试",
            "type": type(exc).__name__}) from exc
    if payload is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "PREWARM_LINK_NOT_FOUND",
            "message": f"业务日期 {business_date} 下不存在候选 {candidate_id}"})
    return JSONResponse(payload, headers=_NO_STORE)


@router.post("/api/prewarm/links/{candidate_id}/recrawl")
def prewarm_link_recrawl(
    request: Request, candidate_id: int, body: _PrewarmRecrawlBody | None = None
) -> JSONResponse:
    _guard_operator_action(request, "prewarm-recrawl")
    business_date = _resolve_action_body_date(body.business_date if body is not None else None)
    if not _valid_business_date(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE",
            "message": "business_date 必须为合法的 YYYY-MM-DD 日历日期"})
    try:
        handle = get_manual_recrawl_runner().start(
            business_date,
            candidate_id=int(candidate_id),
            expected_original_url=body.expected_original_url if body is not None else None,
            expected_status=body.expected_status if body is not None else None,
        )
    except ManualRecrawlError as exc:
        status_by_code = {
            "CANDIDATE_NOT_FOUND": 404,
            "STALE_PREWARM_LINK": 409,
            "ACTION_IN_FLIGHT": 409,
            "RECRAWL_NOT_SUPPORTED": 400,
            "STATE_DB_MISSING": 409,
        }
        detail: dict = {"error_code": exc.code, "message": str(exc)}
        if exc.code == "ACTION_IN_FLIGHT":
            detail["existing_action_id"] = exc.existing_action_id
        raise HTTPException(
            status_code=status_by_code.get(exc.code, 500), detail=detail) from exc
    return JSONResponse(
        {"action_id": handle["action_id"], "status": handle["status"],
         "route": handle["route"], "mall": handle["mall"],
         "poll": f"/dashboard/api/prewarm/recrawl-actions/{handle['action_id']}"},
        status_code=202, headers=_NO_STORE)


@router.get("/api/prewarm/recrawl-actions/{action_id}")
def prewarm_recrawl_action_status(request: Request, action_id: str) -> JSONResponse:
    if request.query_params:
        unknown = sorted(request.query_params.keys())
        raise HTTPException(status_code=400, detail={
            "error_code": "UNKNOWN_QUERY_PARAM",
            "message": "不支持的查询参数:" + ",".join(unknown)
                       + ";本接口不接受任何查询参数"})
    handle = get_manual_recrawl_runner().get(action_id)
    if handle is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "ACTION_NOT_FOUND", "message": "未找到该操作记录"})
    return JSONResponse(handle, headers=_NO_STORE)
