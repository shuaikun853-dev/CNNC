import re
from decimal import Decimal

from app.schemas.workflow import (
    SaasSameProductApprovalAuditResponse,
    SaasSameProductApprovalAuditResult,
    SaasSameProductApprovalItem,
    WorkflowAction,
)
from app.services.link_audit_worker import (
    LinkAuditRequest,
    check_price_consistency,
    check_sku_link_consistency,
    audit_link,
)
from app.services.title_match_model_adapter import (
    TitleMatchModelRequest,
    TitleMatchModelResult,
    call_title_match_model,
)


RISK_WORDS = ("替代款", "通用", "适用", "升级款")
REVIEW_TERMS = RISK_WORDS
HIGHER_THAN_CUSTOMER_PRICE_RATIO = Decimal("1.30")
SPEC_RE = re.compile(r"\d+\s*(?:件套|件|套)")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]*")


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


def get_customer_price(record: SaasSameProductApprovalItem) -> Decimal | None:
    if record.customerPrice is not None:
        return record.customerPrice
    if record.customer_price is not None:
        return record.customer_price
    if record.referencePrice is not None:
        return record.referencePrice
    return record.reference_price


def get_candidate_price(record: SaasSameProductApprovalItem, page_actual_price: Decimal | None) -> Decimal | None:
    if page_actual_price is not None:
        return page_actual_price
    return record.price


def check_candidate_price_against_customer_price(
    candidate_price: Decimal | None,
    customer_price: Decimal | None,
) -> tuple[str, list[str]]:
    if customer_price is None:
        return "CUSTOMER_PRICE_MISSING", ["客户价格为空，无法执行 30% 高价过滤"]
    if candidate_price is None:
        return "CANDIDATE_PRICE_MISSING", ["候选价格为空，无法执行 30% 高价过滤"]
    threshold = customer_price * HIGHER_THAN_CUSTOMER_PRICE_RATIO
    if candidate_price > threshold:
        return "HIGHER_THAN_CUSTOMER_PRICE_30", [
            f"客户价格={customer_price}",
            f"候选价格={candidate_price}",
            f"高价阈值=客户价格*1.3={threshold}",
            "候选价格高于客户价格30%以上",
        ]
    return "WITHIN_THRESHOLD", [
        f"客户价格={customer_price}",
        f"候选价格={candidate_price}",
        f"高价阈值=客户价格*1.3={threshold}",
        "候选价格未超过客户价格30%",
    ]


def detect_title_risk_words(query_str: str, sku_name: str) -> list[str]:
    text = f"{query_str or ''} {sku_name or ''}"
    return [word for word in RISK_WORDS if word in text]


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
        return "MANUAL_REVIEW", ["缺少标准商品描述或 SKU 标题"]

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
        return "SAME", evidence
    if query_models:
        return "MANUAL_REVIEW", evidence
    return "MANUAL_REVIEW", evidence


def _title_result_from_model_or_rule(
    item: SaasSameProductApprovalItem,
    use_model: bool,
) -> tuple[str, float, list[str]]:
    rule_result, rule_evidence = audit_title_similarity(
        item.queryStr,
        item.skuName,
        item.modelAuditResult,
        item.isMatch,
    )
    if not use_model:
        return rule_result, 0.0, rule_evidence

    model_result = call_title_match_model(
        TitleMatchModelRequest(
            query_str=item.queryStr,
            sku_name=item.skuName,
            model_audit_result=item.modelAuditResult,
            is_match=item.isMatch,
        )
    )
    evidence = [
        f"title_model.match_result={model_result.match_result}",
        f"title_model.confidence={model_result.confidence}",
    ]
    if model_result.reason:
        evidence.append(f"title_model.reason={model_result.reason}")
    evidence.extend([f"title_model.risk={risk}" for risk in model_result.risk_points])
    evidence.extend(rule_evidence)

    if model_result.match_result == "MODEL_UNAVAILABLE":
        return "MODEL_UNAVAILABLE", model_result.confidence, evidence
    return model_result.match_result, model_result.confidence, evidence


def _decide_action(
    title_result: str,
    sku_check: str,
    price_check: str,
    price_vs_customer_check: str,
    reachable: bool | None,
    off_shelf: bool | None,
) -> tuple[WorkflowAction, list[WorkflowAction]]:
    if price_vs_customer_check == "HIGHER_THAN_CUSTOMER_PRICE_30":
        return WorkflowAction.REJECT, [WorkflowAction.REJECT, WorkflowAction.MANUAL_CHECK]
    if title_result == "NOT_SAME" or sku_check == "MISMATCH" or price_check == "MISMATCH" or reachable is False or off_shelf is True:
        return WorkflowAction.REJECT, [WorkflowAction.REJECT, WorkflowAction.MANUAL_CHECK]
    if (
        title_result == "SAME"
        and sku_check in {"MATCH", "UNKNOWN"}
        and price_check == "MATCH"
        and reachable is not False
        and off_shelf is not True
    ):
        return WorkflowAction.APPROVE, [WorkflowAction.APPROVE]
    return WorkflowAction.MANUAL_CHECK, [WorkflowAction.MANUAL_CHECK]


def _reason(
    title_result: str,
    sku_check: str,
    price_check: str,
    price_vs_customer_check: str,
    reachable: bool | None,
    off_shelf: bool | None,
    evidence: list[str],
) -> str:
    if price_vs_customer_check == "HIGHER_THAN_CUSTOMER_PRICE_30":
        return "候选价格高于客户价格30%以上，不建议作为同款审批数据，建议人工驳回前复核"
    if title_result == "MANUAL_REVIEW" and any("替代款" in item for item in evidence):
        return "型号命中，但标题含“替代款”，需人工确认是否为原型号同款"
    if title_result == "NOT_SAME":
        return "标题模型判断非同款，建议人工驳回前复核"
    if reachable is False:
        return "候选链接不可访问，建议人工驳回前复核"
    if off_shelf is True:
        return "候选商品疑似下架，建议人工驳回前复核"
    if sku_check == "MISMATCH":
        return "SKU 与链接不一致，建议人工驳回前复核"
    if price_check == "MISMATCH":
        return "SaaS JSON 价格与页面实际价格不一致，建议人工驳回前复核"
    if price_check == "WAIT_PAGE_PRICE_CHECK":
        return "缺少页面实际价格，待页面价格核验"
    if title_result == "SAME" and price_check == "MATCH":
        return "标题模型判断同款且价格一致，建议人工审批通过"
    return "需人工核查同款关系、SKU 链接和价格一致性"


def audit_saas_approval_item(
    item: SaasSameProductApprovalItem,
    use_model: bool = False,
    use_link_audit: bool = True,
    title_model_result: TitleMatchModelResult | None = None,
) -> SaasSameProductApprovalAuditResult:
    if title_model_result is not None:
        title_result = title_model_result.match_result
        title_confidence = title_model_result.confidence
        title_evidence = [
            f"title_model.match_result={title_model_result.match_result}",
            f"title_model.confidence={title_model_result.confidence}",
        ]
        if title_model_result.reason:
            title_evidence.append(f"title_model.reason={title_model_result.reason}")
        title_evidence.extend([f"title_model.risk={risk}" for risk in title_model_result.risk_points])
    else:
        title_result, title_confidence, title_evidence = _title_result_from_model_or_rule(item, use_model)

    page_price = get_page_actual_price(item)
    if use_link_audit:
        link_result = audit_link(LinkAuditRequest(
            ext_link=item.extLink,
            sku_id=item.skuId,
            vendor_name=item.vendorName,
            shop_name=item.shopName,
            json_price=item.price,
            page_actual_price=page_price,
        ))
    else:
        sku_check, sku_evidence = check_sku_link_consistency(item.skuId, item.extLink)
        price_check, price_evidence = check_price_consistency(item.price, page_price)
        from app.services.link_audit_worker import LinkAuditResult
        link_result = LinkAuditResult(
            sku_link_check=sku_check,
            page_price=page_price,
            price_check=price_check,
            evidence=[*sku_evidence, *price_evidence],
        )

    customer_price = get_customer_price(item)
    candidate_price = get_candidate_price(item, link_result.page_price)
    price_vs_customer_check, customer_price_evidence = check_candidate_price_against_customer_price(candidate_price, customer_price)
    evidence = [
        f"vendor={normalize_mall_name(item.vendorName) or normalize_mall_name(item.shopName)}",
        *title_evidence,
        *link_result.evidence,
        *customer_price_evidence,
    ]
    action, suggested = _decide_action(
        title_result,
        link_result.sku_link_check,
        link_result.price_check,
        price_vs_customer_check,
        link_result.reachable,
        link_result.off_shelf,
    )
    risk_words = detect_title_risk_words(item.queryStr, item.skuName)
    if risk_words:
        evidence.append(f"命中标题风险词：{','.join(risk_words)}")
        if action == WorkflowAction.APPROVE:
            action = WorkflowAction.MANUAL_CHECK
            suggested = [WorkflowAction.MANUAL_CHECK]
    reason = _reason(title_result, link_result.sku_link_check, link_result.price_check, price_vs_customer_check, link_result.reachable, link_result.off_shelf, evidence)
    if risk_words and action == WorkflowAction.MANUAL_CHECK:
        reason = "标题包含替代款/通用/适用/升级款等风险词，需人工确认是否为原型号同款"
    return SaasSameProductApprovalAuditResult(
        vCommodityId=item.vCommodityId,
        skuId=item.skuId,
        extLink=item.extLink,
        queryStr=item.queryStr,
        skuName=item.skuName,
        vendorName=item.vendorName,
        shopName=item.shopName,
        json_price=item.price,
        page_actual_price=link_result.page_price,
        customer_price=customer_price,
        price_vs_customer_check=price_vs_customer_check,
        title_match_result=title_result,
        title_match_confidence=title_confidence,
        sku_link_check=link_result.sku_link_check,
        price_check=link_result.price_check,
        reachable=link_result.reachable,
        off_shelf=link_result.off_shelf,
        primary_action=action,
        suggested_actions=suggested,
        reason=reason,
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
    use_model: bool = True,
    use_link_audit: bool = True,
) -> SaasSameProductApprovalAuditResponse:
    results = [audit_saas_approval_item(record, use_model=use_model, use_link_audit=use_link_audit) for record in records]
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
        "higher_than_customer_price_30_count": len([r for r in results if r.price_vs_customer_check == "HIGHER_THAN_CUSTOMER_PRICE_30"]),
        "customer_price_missing_count": len([r for r in results if r.price_vs_customer_check == "CUSTOMER_PRICE_MISSING"]),
        "model_unavailable_count": len([r for r in results if r.title_match_result == "MODEL_UNAVAILABLE"]),
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
