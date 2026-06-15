import re
import logging
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app.agent.planner import build_diagnosis
from app.agent.sql_selector import get_sql_templates
from app.agent.result_analyzer import generate_result_guide
from app.agent.router import check_scope
from app.services.knowledge_loader import search_knowledge, load_knowledge, KnowledgeLoadError, get_knowledge_status
from app.config import APP_NAME, APP_VERSION, READONLY_MODE, KNOWLEDGE_MD_PATH, TEMPLATES_DIR
from app.schemas.workflow import (
    WorkflowAnalyzeRequest,
    WorkflowAnalyzeResponse,
    WorkflowErrorResponse,
    WorkflowRouteRequest,
    WorkflowRouteResponse,
)
from app.workflows.local_access_guard import check_local_access
from app.workflows.scope_guard import validate_request_scope, validate_records_scope
from app.workflows.router import route_workflow
from app.workflows.base import WORKFLOW_REGISTRY
from app.workflows.human_approval_guard import enforce_recommendation_only
from app.services.workflow_knowledge_adapter import get_workflow_knowledge_files

logger = logging.getLogger(__name__)

_HIGH_RISK_WRITE_INTENT_RE = re.compile(
    r"修改价格指数|价格指数修改|修改指导价|手工修改价格|"
    r"重推任务|任务重推|"
    r"高风险变更|对标链接替换|"
    r"\bupdate\b|\bdelete\b|\binsert\b|\btruncate\b|\bdrop\b|\balter\b|\bcreate\b|\breplace\b|\bmerge\b",
    re.IGNORECASE,
)

_HIGH_RISK_WRITE_NOTICE = (
    "【高风险写库 SQL 已脱敏：第一阶段不展示具体 SQL，需人工确认后处理】"
)

app = FastAPI(title=APP_NAME, version=APP_VERSION)


class DiagnoseRequest(BaseModel):
    question: str
    params: dict | None = None


class SqlParamSpec(BaseModel):
    name: str = ""
    required: bool = True
    type: str = "string"
    description: str = ""
    example: str = ""


class SqlResultHint(BaseModel):
    sql_name: str = ""
    result_hint: str = ""


class SqlTemplateItem(BaseModel):
    name: str = ""
    purpose: str = ""
    description: str = ""
    params: list[SqlParamSpec] = Field(default_factory=list)
    sql: str = ""
    safe: bool = True
    safety_reason: str = ""
    reason: str = ""
    result_hint: str = ""
    pending_params: bool = False
    missing_params: list[str] = Field(default_factory=list)
    invalid_params: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_reason: str = ""
    template_name: str = ""


class KnowledgeSnippet(BaseModel):
    title: str
    content: str


class ResultGuide(BaseModel):
    interpretation: str = ""
    per_sql_hints: list[SqlResultHint] = Field(default_factory=list)


class DiagnoseResponse(BaseModel):
    scenario: str
    confidence: float
    reason: str
    diagnosis_steps: list[str]
    related_tables: list[str]
    sql_templates: list[SqlTemplateItem]
    result_guide: ResultGuide
    knowledge_snippets: list[KnowledgeSnippet]
    knowledge_status: str = "ok"
    knowledge_error: str = ""
    missing_params: list[str] = Field(default_factory=list)
    risk_notice: str


def _check_degraded():
    issues = []
    try:
        if not Path(KNOWLEDGE_MD_PATH).exists():
            issues.append("knowledge_file_missing")
        else:
            try:
                KNOWLEDGE_MD_PATH.read_text(encoding="utf-8")
            except Exception:
                issues.append("knowledge_file_unreadable")
    except Exception:
        issues.append("knowledge_file_missing")
    try:
        if not Path(TEMPLATES_DIR).exists():
            issues.append("templates_dir_missing")
    except Exception:
        issues.append("templates_dir_missing")
    return issues


@app.get("/health")
async def health():
    issues = _check_degraded()
    template_count = 0
    try:
        tpl_dir = Path(TEMPLATES_DIR)
        if tpl_dir.exists():
            template_count = len(list(tpl_dir.glob("*.yaml")))
    except Exception:
        pass

    knowledge_issues = [i for i in issues if i.startswith("knowledge_")]
    if knowledge_issues:
        k_status = "degraded"
        k_error = "知识库不可用"
    else:
        k_status, k_error = get_knowledge_status()

    if k_status in ("error", "degraded"):
        if not knowledge_issues:
            issues.append(f"knowledge_{k_status}")

    status = "ok" if not issues else "degraded"
    return {
        "status": status,
        "service": APP_NAME,
        "version": APP_VERSION,
        "readonly_mode": READONLY_MODE,
        "db_connected": False,
        "knowledge_file_exists": Path(KNOWLEDGE_MD_PATH).exists() if _safe_path_check(KNOWLEDGE_MD_PATH) else False,
        "templates_dir_exists": Path(TEMPLATES_DIR).exists() if _safe_path_check(TEMPLATES_DIR) else False,
        "template_count": template_count,
        "knowledge_status": k_status,
        "knowledge_error": k_error,
        "issues": issues,
    }


def _safe_path_check(p) -> bool:
    try:
        Path(p)
        return True
    except Exception:
        return False


@app.post("/diagnose", response_model=DiagnoseResponse)
async def diagnose(req: DiagnoseRequest):
    scope = check_scope(req.question)
    if not scope.in_scope:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": scope.error_code,
                "message": scope.message,
            },
        )

    try:
        diag = build_diagnosis(req.question)
    except Exception as e:
        logger.error("build_diagnosis error: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"error_code": "INTERNAL_ERROR", "message": "诊断规划失败", "detail": str(type(e).__name__)},
        )

    scenario = diag["scenario"]

    try:
        sql_templates = get_sql_templates(scenario, provided_params=req.params)
    except Exception as e:
        logger.error("sql_selector error: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"error_code": "TEMPLATE_ERROR", "message": "SQL 模板加载失败", "detail": str(type(e).__name__)},
        )

    result_guide = generate_result_guide(scenario, sql_templates)

    knowledge_status = "ok"
    knowledge_error = ""
    knowledge_snippets = []

    try:
        snippets = search_knowledge(req.question, top_k=3)
        knowledge_snippets = [
            KnowledgeSnippet(title=s["title"], content=s["content"][:500])
            for s in snippets
        ]
    except KnowledgeLoadError as e:
        logger.error("knowledge_loader error: %s", e)
        knowledge_status = "error"
        knowledge_error = str(e)
    except Exception as e:
        logger.error("knowledge_loader unexpected error: %s", e)
        knowledge_status = "error"
        knowledge_error = "知识库查询异常"

    if _HIGH_RISK_WRITE_INTENT_RE.search(req.question):
        has_notice = any(
            "第一阶段不展示具体 SQL" in ks.content
            for ks in knowledge_snippets
        )
        if not has_notice:
            knowledge_snippets.insert(0, KnowledgeSnippet(
                title="高风险写库操作提示",
                content=_HIGH_RISK_WRITE_NOTICE,
            ))

    all_missing = []
    for tpl in sql_templates:
        if isinstance(tpl, dict) and tpl.get("missing_params"):
            for p in tpl["missing_params"]:
                if p not in all_missing:
                    all_missing.append(p)

    risk_notice = (
        "【只读安全提示】本系统为离线只读诊断版，不连接生产数据库，不执行任何 SQL。"
        "所有 SQL 模板仅供参考，请手动在只读副本上执行验证。"
        "严禁执行 UPDATE/DELETE/INSERT/TRUNCATE/DROP/ALTER 等写库操作。"
    )

    return DiagnoseResponse(
        scenario=scenario,
        confidence=diag["confidence"],
        reason=diag["reason"],
        diagnosis_steps=diag["diagnosis_steps"],
        related_tables=diag["related_tables"],
        sql_templates=sql_templates,
        result_guide=result_guide,
        knowledge_snippets=knowledge_snippets,
        knowledge_status=knowledge_status,
        knowledge_error=knowledge_error,
        missing_params=all_missing,
        risk_notice=risk_notice,
    )


def _get_client_host(request: Request) -> str | None:
    if request.client:
        return request.client.host
    return None


def _check_workflow_access(request: Request):
    client_host = _get_client_host(request)
    token_header = request.headers.get("X-Local-Workflow-Token")
    result = check_local_access(client_host, token_header)
    if not result.allowed:
        raise HTTPException(
            status_code=403,
            detail={"error_code": result.error_code, "message": result.message},
        )


@app.post("/workflows/route", response_model=WorkflowRouteResponse)
async def workflow_route(req: WorkflowRouteRequest, request: Request):
    _check_workflow_access(request)
    scope_result = validate_request_scope(req.scope)
    if not scope_result.allowed:
        raise HTTPException(
            status_code=400,
            detail={"error_code": scope_result.error_code, "message": scope_result.message},
        )
    return route_workflow(req.question)


@app.post("/workflows/same-product-approval/analyze", response_model=WorkflowAnalyzeResponse)
async def workflow_same_product_approval_analyze(req: WorkflowAnalyzeRequest, request: Request):
    _check_workflow_access(request)
    scope_result = validate_request_scope(req.scope)
    if not scope_result.allowed:
        raise HTTPException(
            status_code=400,
            detail={"error_code": scope_result.error_code, "message": scope_result.message},
        )
    records_scope_result = validate_records_scope(req.scope, req.records)
    if not records_scope_result.allowed:
        raise HTTPException(
            status_code=400,
            detail={"error_code": records_scope_result.error_code, "message": records_scope_result.message},
        )
    wf = WORKFLOW_REGISTRY.get("work_01_same_product_approval")
    if not wf:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "WORKFLOW_NOT_FOUND", "message": "work_01_same_product_approval 不存在"},
        )
    response = wf.analyze(req.records)
    response = enforce_recommendation_only(response)
    return response


@app.post("/workflows/price-index-feedback/analyze", response_model=WorkflowAnalyzeResponse)
async def workflow_price_index_feedback_analyze(req: WorkflowAnalyzeRequest, request: Request):
    _check_workflow_access(request)
    scope_result = validate_request_scope(req.scope)
    if not scope_result.allowed:
        raise HTTPException(
            status_code=400,
            detail={"error_code": scope_result.error_code, "message": scope_result.message},
        )
    records_scope_result = validate_records_scope(req.scope, req.records)
    if not records_scope_result.allowed:
        raise HTTPException(
            status_code=400,
            detail={"error_code": records_scope_result.error_code, "message": records_scope_result.message},
        )
    wf = WORKFLOW_REGISTRY.get("work_02_price_index_feedback")
    if not wf:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "WORKFLOW_NOT_FOUND", "message": "work_02_price_index_feedback 不存在"},
        )
    response = wf.analyze(req.records)
    response = enforce_recommendation_only(response)
    return response


@app.get("/workflows/{workflow_name}/knowledge")
async def workflow_knowledge(workflow_name: str, request: Request):
    _check_workflow_access(request)
    knowledge_files = get_workflow_knowledge_files(workflow_name)
    if not knowledge_files:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "WORKFLOW_NOT_FOUND", "message": f"workflow {workflow_name} 无知识文件映射"},
        )
    return {
        "workflow_name": workflow_name,
        "knowledge_files": knowledge_files,
        "snippets": [],
    }
