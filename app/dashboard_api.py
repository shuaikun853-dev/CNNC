"""Read-only STEP_1 dashboard routes + strictly guarded local operator actions.

GET 层保持零业务写。新增 POST 只允许本机驾驶舱触发既有 CLI 子进程
(closeout/re-export/rebuild)或只读 refresh-progress;每个 POST 首行执行
_guard_operator_action(Host/Origin/X-Dashboard-Action-Token),绝无轮询触写。
"""
from __future__ import annotations

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
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.services.dashboard_actions import DashboardActionError, get_action_runner
from app.services.dashboard_manual_ops import (
    get_progress_service_for,
    read_operator_actions,
    refresh_job_progress,
)
from app.services.dashboard_projection import build_dashboard_summary
from app.services.incremental_audit_runtime import runtime_paths
from app.services.saas_flow_diagnostic import get_saas_flow_diagnostic

router = APIRouter(prefix="/dashboard", tags=["step1-dashboard"])
_STATIC_DIR = Path(__file__).resolve().parent / "static" / "dashboard"
_ROOT_DIR = Path(__file__).resolve().parents[1]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# saas-flow 只接受 date(YYYY-MM-DD)与 refresh=1(缓存行为标志);
# tenant/pool/schema/table/sql 等任何 SQL 或 scope 可控参数一律 400。
_SAAS_FLOW_ALLOWED_PARAMS = frozenset({"date", "refresh"})

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


@router.get("/", include_in_schema=True)
def dashboard_index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


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
# CNNC 认证:只读状态 + 受控本地登录浏览器启动
# ======================================================================

_CNNC_LOGIN_THROTTLE_SECONDS = 60.0
# 进程内单例节流:60 秒内只允许启动一次登录浏览器(monotonic 时间戳)。
_cnnc_login_launched_at: float | None = None


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
    return JSONResponse(payload, headers=_NO_STORE)


@router.post("/api/cnnc-auth/login")
def dashboard_cnnc_auth_login(request: Request) -> JSONResponse:
    """启动本地 CNNC SSO 登录浏览器(detached 子进程;不占 action 槽、不阻塞)。"""
    _guard_operator_action(request, "cnnc-auth-login")
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
            [sys.executable, str(_ROOT_DIR / "scripts" / "cnnc_auth_browser.py"), "login"],
            cwd=str(_ROOT_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **popen_kwargs)
    except OSError as exc:  # FileNotFoundError 等:简讯返回,不暴露堆栈
        raise HTTPException(status_code=500, detail={
            "error_code": "CNNC_LOGIN_LAUNCH_FAILED",
            "message": "登录浏览器启动失败,请稍后重试"}) from exc
    _cnnc_login_launched_at = now
    return JSONResponse(
        {"started": True, "note": "登录浏览器已启动，完成滑块/短信后系统自动恢复"},
        status_code=202, headers=_NO_STORE)


@router.get("/api/actions")
def dashboard_actions_list() -> JSONResponse:
    runner = get_action_runner()
    running = [{"action_id": h.action_id, "action_type": h.action_type,
                "status": h.status, "target": h.target, "started_at": h.started_at}
               for h in runner.list_recent(20) if h.status == "RUNNING"]
    try:
        recent = read_operator_actions(runtime_paths(_today()).run_dir, limit=20)
    except Exception:
        recent = []
    return JSONResponse({"running": running, "recent": recent}, headers=_NO_STORE)


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
