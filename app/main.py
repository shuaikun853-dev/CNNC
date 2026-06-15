from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.agent.planner import build_diagnosis
from app.agent.sql_selector import get_sql_templates
from app.agent.result_analyzer import generate_result_guide
from app.agent.router import check_scope
from app.services.knowledge_loader import search_knowledge
from app.config import APP_NAME, APP_VERSION, READONLY_MODE

app = FastAPI(title=APP_NAME, version=APP_VERSION)


class DiagnoseRequest(BaseModel):
    question: str


class DiagnoseResponse(BaseModel):
    scenario: str
    confidence: float
    reason: str
    diagnosis_steps: list[str]
    related_tables: list[str]
    sql_templates: list[dict]
    result_guide: dict
    knowledge_snippets: list[dict]
    risk_notice: str


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
        "readonly_mode": READONLY_MODE,
    }


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

    diag = build_diagnosis(req.question)
    scenario = diag["scenario"]

    sql_templates = get_sql_templates(scenario)

    result_guide = generate_result_guide(scenario, sql_templates)

    snippets = search_knowledge(req.question, top_k=3)
    knowledge_snippets = [
        {"title": s["title"], "content": s["content"][:500]}
        for s in snippets
    ]

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
        risk_notice=risk_notice,
    )
