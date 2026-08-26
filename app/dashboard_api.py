"""Read-only STEP_1 dashboard routes. GET only, zero business writes."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app.services.dashboard_projection import build_dashboard_summary

router = APIRouter(prefix="/dashboard", tags=["step1-dashboard"])
_STATIC_DIR = Path(__file__).resolve().parent / "static" / "dashboard"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _today() -> str:
    from app.services.incremental_audit_runtime import business_date_in_shanghai
    return business_date_in_shanghai()


@router.get("/", include_in_schema=True)
def dashboard_index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@router.get("/api/summary")
def dashboard_summary(request: Request, date: str = Query(default=None)) -> Response:
    business_date = date or _today()
    if not _DATE_RE.match(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE", "message": "business_date 必须为 YYYY-MM-DD"})
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
