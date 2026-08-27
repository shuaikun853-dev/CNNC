"""Read-only STEP_1 dashboard routes. GET only, zero business writes."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app.services.dashboard_projection import build_dashboard_summary
from app.services.saas_flow_diagnostic import get_saas_flow_diagnostic

router = APIRouter(prefix="/dashboard", tags=["step1-dashboard"])
_STATIC_DIR = Path(__file__).resolve().parent / "static" / "dashboard"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# saas-flow 只接受 date(YYYY-MM-DD)与 refresh=1(缓存行为标志);
# tenant/pool/schema/table/sql 等任何 SQL 或 scope 可控参数一律 400。
_SAAS_FLOW_ALLOWED_PARAMS = frozenset({"date", "refresh"})


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
        payload = build_dashboard_summary(business_date=business_date)
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
