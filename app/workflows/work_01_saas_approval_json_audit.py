import re
from decimal import Decimal
from urllib.parse import urlparse

from app.schemas.workflow import (
    SaasSameProductApprovalAuditResponse,
    SaasSameProductApprovalAuditResult,
    SaasSameProductApprovalItem,
    WorkflowAction,
)


PRICE_TOLERANCE = Decimal("0.01")
REVIEW_TERMS = ("替代款", "升级款", "通用", "适用")
SPEC_RE = re.compile(r"\d+\s*(?:件套|件|套)")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]*")
JD_SKU_RE = re.compile(r"/([0-9]+)\.html(?:$|[?#])")


def normalize_mall_name(mall_name: str | None) -> str:
    name = (mall_name or "").strip()
    if not name:
        return ""
    aliases = {
        "jd": "京东",
        "京东": "京东",
        "京东商城": "京东",
        "tmall": "天猫",
        "天猫": "天猫",
        "天猫商城": "天猫",
        "taobao": "淘宝",
        "淘宝": "淘宝",
        "suning": "苏宁",
        "苏宁": "苏宁",
        "苏宁易购": "苏宁",
    }
    return aliases.get(name.lower(), aliases.get(name, name))


def get_page_actual_price(item: SaasSameProductApprovalItem) -> Decimal | None:
    if item.pageActualPrice is not None:
        return item.pageActualPrice
    return item.page_actual_price


def check_sku_link_consistency(sku_id: str, ext_link: str) -> tuple[str, list[str]]:
    sku = (sku_id or "").strip()
    link = (ext_link or "").strip()
    if not sku or not link:
        return "UNKNOWN", ["缺少 skuId 或 extLink，无法核验"]

    host = urlparse(link).netloc.lower()
    if "jd.com" in host:
        match = JD_SKU_RE.search(link)
        if not match:
            return "UNKNOWN", ["京东链接未识别到 SKU，需人工核验"]
        link_sku = match.group(1)
        if link_sku == sku:
            return "MATCH", ["京东链接 SKU 与 skuId 一致"]
        return "MISMATCH", ["京东链接 SKU 与 skuId 不一致"]

    return "UNKNOWN", ["暂不支持该商城链接 SKU 解析，需人工核验"]


def check_price_consistency(json_price: Decimal | None, page_actual_price: Decimal | None) -> tuple[str, list[str]]:
    if json_price is None:
        return "JSON_PRICE_MISSING", ["SaaS JSON 价格为空"]
    if page_actual_price is None:
        return "WAIT_PAGE_PRICE_CHECK", ["缺少页面实际价格，需人工打开链接核验"]
    if abs(json_price - page_actual_price) <= PRICE_TOLERANCE:
        return "MATCH", ["SaaS JSON 价格与页面实际价格一致"]
    return "MISMATCH", ["SaaS JSON 价格与页面实际价格不一致"]


def _extract_models(text: str) -> list[str]:
    models: list[str] = []
    for raw in MODEL_RE.findall(text):
        token = raw.strip("-_/")
        if not token:
            continue
        has_digit = any(ch.isdigit() for ch in token)
        has_alpha = any(ch.isalpha() for ch in token)
        if has_digit and (has_alpha or len(token) >= 4):
            models.append(token.upper())
    models.sort(key=lambda value: (any(ch.isalpha() for ch in value), len(value)), reverse=True)
    return models


def _extract_specs(text: str) -> set[str]:
    return {re.sub(r"\s+", "", value) for value in SPEC_RE.findall(text)}


def audit_title_similarity(
    query_str: str,
    sku_name: str,
    model_audit_result: str,
    is_match: str,
) -> tuple[str, list[str]]:
    query = (query_str or "").strip()
    sku = (sku_name or "").strip()
    if not query or not sku:
        return "UNKNOWN", ["缺少标准商品描述或 SKU 标题"]

    evidence: list[str] = []
    if model_audit_result == "1" and is_match == "1":
        evidence.append("SaaS模型判断为同款")

    query_models = _extract_models(query)
    sku_upper = sku.upper()
    matched_models = [model for model in query_models if model in sku_upper]
    has_review_term = any(term in sku for term in REVIEW_TERMS)

    if matched_models:
        evidence.append(f"核心型号命中: {matched_models[0]}")
    elif query_models:
        evidence.append("核心型号未完全命中，需人工确认")
    else:
        evidence.append("未识别到明确核心型号，需人工确认")

    query_specs = _extract_specs(query)
    sku_specs = _extract_specs(sku)
    if query_specs and query_specs.intersection(sku_specs):
        evidence.append("规格/件数疑似一致")

    if has_review_term:
        evidence.append("标题含替代款/升级款/通用/适用，需人工确认是否为原型号同款")
        return "MANUAL_REVIEW", evidence

    if matched_models:
        return "LIKELY_MATCH", evidence
    if query_models:
        return "MANUAL_REVIEW", evidence
    if model_audit_result == "1" and is_match == "1":
        return "MANUAL_REVIEW", evidence
    return "UNKNOWN", evidence


def _decide_action(title_result: str, sku_check: str, price_check: str) -> tuple[WorkflowAction, list[WorkflowAction]]:
    if sku_check == "MISMATCH" or price_check == "MISMATCH" or title_result == "LIKELY_NOT_MATCH":
        return WorkflowAction.REJECT, [WorkflowAction.REJECT, WorkflowAction.MANUAL_CHECK]
    if title_result == "LIKELY_MATCH" and sku_check in {"MATCH", "UNKNOWN"} and price_check == "MATCH":
        return WorkflowAction.APPROVE, [WorkflowAction.APPROVE]
    return WorkflowAction.MANUAL_CHECK, [WorkflowAction.MANUAL_CHECK]


def _reason(title_result: str, sku_check: str, price_check: str, evidence: list[str]) -> str:
    if title_result == "MANUAL_REVIEW" and any("替代款" in item for item in evidence):
        return "型号命中，但标题含“替代款”，需人工确认是否为原型号同款"
    if sku_check == "MISMATCH":
        return "SKU 与链接不一致，建议人工驳回前复核"
    if price_check == "MISMATCH":
        return "SaaS JSON 价格与页面实际价格不一致，建议人工驳回前复核"
    if price_check == "WAIT_PAGE_PRICE_CHECK":
        return "缺少页面实际价格，待页面价格核验"
    if title_result == "LIKELY_MATCH" and price_check == "MATCH":
        return "疑似同款且价格一致，建议人工审批通过"
    return "需人工核查同款关系、SKU 链接和价格一致性"


def audit_saas_approval_item(item: SaasSameProductApprovalItem) -> SaasSameProductApprovalAuditResult:
    title_result, title_evidence = audit_title_similarity(
        item.queryStr,
        item.skuName,
        item.modelAuditResult,
        item.isMatch,
    )
    sku_check, sku_evidence = check_sku_link_consistency(item.skuId, item.extLink)
    page_price = get_page_actual_price(item)
    price_check, price_evidence = check_price_consistency(item.price, page_price)
    evidence = [
        f"vendor={normalize_mall_name(item.vendorName) or normalize_mall_name(item.shopName)}",
        *title_evidence,
        *sku_evidence,
        *price_evidence,
    ]
    action, suggested = _decide_action(title_result, sku_check, price_check)
    return SaasSameProductApprovalAuditResult(
        vCommodityId=item.vCommodityId,
        skuId=item.skuId,
        extLink=item.extLink,
        queryStr=item.queryStr,
        skuName=item.skuName,
        vendorName=item.vendorName,
        shopName=item.shopName,
        json_price=item.price,
        page_actual_price=page_price,
        title_match_result=title_result,
        sku_link_check=sku_check,
        price_check=price_check,
        primary_action=action,
        suggested_actions=suggested,
        reason=_reason(title_result, sku_check, price_check, evidence),
        evidence=evidence,
        needs_human_approval=True,
        execution_allowed=False,
        executed=False,
    )


def enforce_saas_audit_recommendation_only(
    response: SaasSameProductApprovalAuditResponse,
) -> SaasSameProductApprovalAuditResponse:
    safe_results: list[SaasSameProductApprovalAuditResult] = []
    for result in response.record_results:
        safe_results.append(result.model_copy(update={
            "needs_human_approval": True,
            "execution_allowed": False,
            "executed": False,
        }))
    return response.model_copy(update={
        "record_results": safe_results,
        "execution_allowed": False,
        "executed": False,
    })


def audit_saas_approval_records(
    records: list[SaasSameProductApprovalItem],
) -> SaasSameProductApprovalAuditResponse:
    results = [audit_saas_approval_item(record) for record in records]
    groups: dict[str, list[SaasSameProductApprovalAuditResult]] = {}
    for result in results:
        groups.setdefault(result.vCommodityId, []).append(result)

    summary: dict[str, int | str | float | bool] = {
        "total_records": len(results),
        "total_v_commodities": len(groups),
        "approve_suggestion_count": len([r for r in results if r.primary_action == WorkflowAction.APPROVE]),
        "manual_check_count": len([r for r in results if r.primary_action == WorkflowAction.MANUAL_CHECK]),
        "reject_suggestion_count": len([r for r in results if r.primary_action == WorkflowAction.REJECT]),
        "wait_page_price_check_count": len([r for r in results if r.price_check == "WAIT_PAGE_PRICE_CHECK"]),
        "price_mismatch_count": len([r for r in results if r.price_check == "MISMATCH"]),
        "sku_mismatch_count": len([r for r in results if r.sku_link_check == "MISMATCH"]),
    }
    for v_commodity_id, group_results in groups.items():
        prefix = f"group.{v_commodity_id}"
        summary[f"{prefix}.candidate_count"] = len(group_results)
        summary[f"{prefix}.approve_suggestion_count"] = len([r for r in group_results if r.primary_action == WorkflowAction.APPROVE])
        summary[f"{prefix}.manual_check_count"] = len([r for r in group_results if r.primary_action == WorkflowAction.MANUAL_CHECK])
        summary[f"{prefix}.reject_suggestion_count"] = len([r for r in group_results if r.primary_action == WorkflowAction.REJECT])
        summary[f"{prefix}.valid_mall_count"] = len({normalize_mall_name(r.shopName or r.vendorName) for r in group_results if normalize_mall_name(r.shopName or r.vendorName)})

    return enforce_saas_audit_recommendation_only(
        SaasSameProductApprovalAuditResponse(
            total_records=len(results),
            summary=summary,
            record_results=results,
            execution_allowed=False,
            executed=False,
        )
    )
