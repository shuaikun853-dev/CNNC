import json
import os
from urllib import error, request

from pydantic import Field

from app.schemas.workflow import StrictModel


class TitleMatchModelRequest(StrictModel):
    query_str: str
    sku_name: str
    model_audit_result: str = ""
    is_match: str = ""


class TitleMatchModelResult(StrictModel):
    match_result: str
    confidence: float = 0.0
    brand_match: bool | None = None
    model_match: str = ""
    spec_match: bool | None = None
    risk_points: list[str] = Field(default_factory=list)
    reason: str = ""
    raw_response: str = ""


ALLOWED_RESULTS = frozenset({"SAME", "NOT_SAME", "MANUAL_REVIEW", "MODEL_UNAVAILABLE"})
ALLOWED_MODEL_MATCH = frozenset({"exact", "partial", "mismatch", "unknown"})
RISK_TERMS = ("替代款", "通用", "适用", "升级款")


def _unavailable(reason: str) -> TitleMatchModelResult:
    return TitleMatchModelResult(
        match_result="MODEL_UNAVAILABLE",
        confidence=0.0,
        model_match="unknown",
        reason=reason,
    )


def _build_prompt(req: TitleMatchModelRequest) -> str:
    return (
        "你是中核 SaaS 同款审批预审助手。只输出 JSON，不要输出 Markdown。"
        "判断标准商品描述 query_str 与候选商品标题 sku_name 是否为同款。"
        "必须判断品牌是否一致、型号是否一致、规格/件数/计量单位是否一致、"
        "是否存在替代款/通用/适用/升级款风险词、是否可能一物多码/同型号变体、是否明显不是同款。"
        "包含替代款/通用/适用/升级款时不得直接输出 SAME，应输出 MANUAL_REVIEW。"
        "输出格式：{\"match_result\":\"SAME|NOT_SAME|MANUAL_REVIEW\",\"confidence\":0.0,"
        "\"brand_match\":true,\"model_match\":\"exact|partial|mismatch|unknown\","
        "\"spec_match\":true,\"risk_points\":[],\"reason\":\"\"}。"
        f"query_str={req.query_str}\nsku_name={req.sku_name}\n"
        f"model_audit_result={req.model_audit_result}\nis_match={req.is_match}"
    )


def _parse_model_response(raw: str, sku_name: str) -> TitleMatchModelResult:
    data = json.loads(raw)
    if "choices" in data:
        content = data["choices"][0]["message"]["content"]
        data = json.loads(content)
    result = TitleMatchModelResult(
        match_result=str(data.get("match_result", "MANUAL_REVIEW")),
        confidence=float(data.get("confidence", 0.0)),
        brand_match=data.get("brand_match"),
        model_match=str(data.get("model_match", "unknown")),
        spec_match=data.get("spec_match"),
        risk_points=list(data.get("risk_points", [])),
        reason=str(data.get("reason", "")),
        raw_response=raw,
    )
    if result.match_result not in ALLOWED_RESULTS - {"MODEL_UNAVAILABLE"}:
        result = result.model_copy(update={"match_result": "MANUAL_REVIEW", "reason": "模型返回非法 match_result，已降级人工核查"})
    if result.model_match not in ALLOWED_MODEL_MATCH:
        result = result.model_copy(update={"model_match": "unknown"})
    if any(term in sku_name for term in RISK_TERMS) and result.match_result == "SAME":
        result = result.model_copy(update={
            "match_result": "MANUAL_REVIEW",
            "risk_points": [*result.risk_points, "标题含替代款/通用/适用/升级款风险词"],
            "reason": result.reason or "标题含风险词，需人工确认是否为原型号同款",
        })
    return result


def call_title_match_model(req: TitleMatchModelRequest, timeout_seconds: float = 30.0) -> TitleMatchModelResult:
    api_key = os.environ.get("TITLE_MATCH_MODEL_API_KEY", "").strip()
    base_url = os.environ.get("TITLE_MATCH_MODEL_BASE_URL", "").strip()
    model_name = os.environ.get("TITLE_MATCH_MODEL_NAME", "").strip()
    if not api_key or not base_url:
        return _unavailable("TITLE_MATCH_MODEL_API_KEY 或 TITLE_MATCH_MODEL_BASE_URL 未配置")

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": _build_prompt(req)}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    http_req = request.Request(
        base_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(http_req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")
        return _parse_model_response(raw, req.sku_name)
    except (error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        return TitleMatchModelResult(
            match_result="MANUAL_REVIEW",
            confidence=0.0,
            model_match="unknown",
            reason=f"标题模型调用或解析失败，已降级人工核查: {type(exc).__name__}",
        )
