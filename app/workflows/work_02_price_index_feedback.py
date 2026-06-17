from decimal import Decimal
from typing import Any, Callable

from app.schemas.workflow import (
    CustomerLinkRecord,
    ManualOperationNode,
    PriceFeedbackRecord,
    SameProductCandidate,
    SameProductRecord,
    StrictModel,
    WorkflowAction,
    WorkflowAnalyzeResponse,
    WorkflowRecordResult,
)


HIGH_PREMIUM_RATIO = Decimal("1.3")


class CustomerLinkMetrics(StrictModel):
    total_count: int = 0
    valid_count: int = 0
    valid_mall_count: int = 0
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    price_diff_pct: Decimal | None = None
    has_invalid_link: bool = False
    has_not_same_product: bool = False
    has_price_invalid: bool = False


class CurrentSameProductMetrics(StrictModel):
    valid_candidate_count: int = 0
    valid_mall_count: int = 0
    wrong_candidate_count: int = 0
    high_premium_count: int = 0
    min_price: Decimal | None = None
    max_price: Decimal | None = None


class PriceFeedbackContext(StrictModel):
    record: PriceFeedbackRecord
    customer_link_metrics: CustomerLinkMetrics
    current_same_product_metrics: CurrentSameProductMetrics


class PriceFeedbackRuleResult(StrictModel):
    rule_id: str
    matched: bool = False
    result: WorkflowRecordResult | None = None


RuleEvaluator = Callable[[PriceFeedbackContext], PriceFeedbackRuleResult]
_RULES: list[RuleEvaluator] = []


MALL_ALIASES = {
    "jd": "京东",
    "京东": "京东",
    "京东商城": "京东",
    "tmall": "天猫",
    "天猫": "天猫",
    "taobao": "淘宝",
    "淘宝": "淘宝",
    "苏宁": "苏宁",
    "苏宁易购": "苏宁",
}


def normalize_mall_name(mall_name: str | None) -> str:
    name = (mall_name or "").strip()
    if not name:
        return ""
    return MALL_ALIASES.get(name.lower(), MALL_ALIASES.get(name, name))


def get_customer_link_price(link: CustomerLinkRecord) -> Decimal | None:
    if link.page_actual_price is not None:
        return link.page_actual_price if link.page_actual_price > 0 else None
    if link.price is not None and link.price > 0:
        return link.price
    return None


def is_valid_customer_link(link: CustomerLinkRecord) -> bool:
    return (
        link.is_valid_link is True
        and link.is_same_product is True
        and normalize_mall_name(link.mall_name) != ""
        and get_customer_link_price(link) is not None
    )


def derive_customer_link_metrics(customer_links: list[CustomerLinkRecord]) -> CustomerLinkMetrics:
    valid_links = [link for link in customer_links if is_valid_customer_link(link)]
    prices = [get_customer_link_price(link) for link in valid_links]
    valid_prices = [price for price in prices if price is not None]
    min_price = min(valid_prices) if valid_prices else None
    max_price = max(valid_prices) if valid_prices else None
    price_diff_pct = None
    if min_price is not None and max_price is not None and min_price > 0:
        price_diff_pct = (max_price - min_price) / min_price

    return CustomerLinkMetrics(
        total_count=len(customer_links),
        valid_count=len(valid_links),
        valid_mall_count=len({normalize_mall_name(link.mall_name) for link in valid_links}),
        min_price=min_price,
        max_price=max_price,
        price_diff_pct=price_diff_pct,
        has_invalid_link=any(link.is_valid_link is not True for link in customer_links),
        has_not_same_product=any(link.is_same_product is not True for link in customer_links),
        has_price_invalid=any(get_customer_link_price(link) is None for link in customer_links),
    )


def get_candidate_price(candidate: SameProductCandidate) -> Decimal | None:
    if candidate.page_actual_price is not None:
        return candidate.page_actual_price if candidate.page_actual_price > 0 else None
    if candidate.crawl_price is not None and candidate.crawl_price > 0:
        return candidate.crawl_price
    return None


def is_valid_current_same_candidate(candidate: SameProductCandidate) -> bool:
    return (
        candidate.is_same_product is True
        and candidate.is_link_valid is True
        and candidate.is_product_on_shelf is True
        and normalize_mall_name(candidate.mall_name) != ""
        and get_candidate_price(candidate) is not None
    )


def collect_current_same_candidates(current_same_products: list[SameProductRecord]) -> list[SameProductCandidate]:
    candidates: list[SameProductCandidate] = []
    for record in current_same_products:
        candidates.extend(record.candidates)
    return candidates


def is_high_premium_candidate(
    candidate: SameProductCandidate,
    reference_price: Decimal | None,
    current_index_price: Decimal | None,
) -> bool:
    price = get_candidate_price(candidate)
    if price is None:
        return False
    baselines = [value for value in [reference_price, current_index_price] if value is not None and value > 0]
    return any(price > baseline * HIGH_PREMIUM_RATIO for baseline in baselines)


def derive_current_same_product_metrics(
    current_same_products: list[SameProductRecord],
    reference_price: Decimal | None,
    current_index_price: Decimal | None,
) -> CurrentSameProductMetrics:
    candidates = collect_current_same_candidates(current_same_products)
    valid_candidates = [candidate for candidate in candidates if is_valid_current_same_candidate(candidate)]
    prices = [get_candidate_price(candidate) for candidate in valid_candidates]
    valid_prices = [price for price in prices if price is not None]

    return CurrentSameProductMetrics(
        valid_candidate_count=len(valid_candidates),
        valid_mall_count=len({normalize_mall_name(candidate.mall_name) for candidate in valid_candidates}),
        wrong_candidate_count=len([candidate for candidate in candidates if candidate.is_same_product is False]),
        high_premium_count=len([
            candidate
            for candidate in candidates
            if is_high_premium_candidate(candidate, reference_price, current_index_price)
        ]),
        min_price=min(valid_prices) if valid_prices else None,
        max_price=max(valid_prices) if valid_prices else None,
    )


def build_price_feedback_context(record: PriceFeedbackRecord) -> PriceFeedbackContext:
    return PriceFeedbackContext(
        record=record,
        customer_link_metrics=derive_customer_link_metrics(record.customer_links),
        current_same_product_metrics=derive_current_same_product_metrics(
            record.current_same_products,
            record.feedback_price,
            record.current_index_price,
        ),
    )


def build_manual_operation_node(step: int, title: str, description: str) -> ManualOperationNode:
    return ManualOperationNode(
        step=step,
        title=title,
        description=description,
        prerequisite="业务知识库完整交接后由人工确认下一步处理流程",
        expected_outcome="仅完成人工核查，不触发任何生产动作",
        safety_notice="R4-0 框架版不执行导入、解绑、重算、推送等操作",
        related_action=WorkflowAction.MANUAL_VERIFY,
        is_executable_by_agent=False,
        execution_allowed=False,
        executed=False,
    )


def register_rule(rule: RuleEvaluator) -> RuleEvaluator:
    _RULES.append(rule)
    return rule


def evaluate_registered_rules(context: PriceFeedbackContext) -> PriceFeedbackRuleResult | None:
    for rule in _RULES:
        result = rule(context)
        if result.matched:
            return result
    return None


def build_framework_evidence(context: PriceFeedbackContext) -> list[str]:
    customer = context.customer_link_metrics
    current = context.current_same_product_metrics
    evidence = [
        f"customer_links.total_count={customer.total_count}",
        f"customer_links.valid_count={customer.valid_count}",
        f"customer_links.valid_mall_count={customer.valid_mall_count}",
        f"customer_links.min_price={customer.min_price}",
        f"customer_links.max_price={customer.max_price}",
        f"customer_links.price_diff_pct={customer.price_diff_pct}",
        f"customer_links.has_invalid_link={customer.has_invalid_link}",
        f"customer_links.has_not_same_product={customer.has_not_same_product}",
        f"customer_links.has_price_invalid={customer.has_price_invalid}",
        f"current_same_products.valid_candidate_count={current.valid_candidate_count}",
        f"current_same_products.valid_mall_count={current.valid_mall_count}",
        f"current_same_products.wrong_candidate_count={current.wrong_candidate_count}",
        f"current_same_products.high_premium_count={current.high_premium_count}",
        f"current_same_products.min_price={current.min_price}",
        f"current_same_products.max_price={current.max_price}",
        "knowledge_status=业务知识库未完整交接，暂不输出最终处理结论",
    ]
    if context.record.current_same_products and not collect_current_same_candidates(context.record.current_same_products):
        evidence.append("current_same_products.detail_status=缺少当前同款明细，需人工核查")
    return evidence


def evaluate_price_feedback_record(record: PriceFeedbackRecord) -> WorkflowRecordResult:
    context = build_price_feedback_context(record)
    rule_result = evaluate_registered_rules(context)
    if rule_result and rule_result.result:
        return rule_result.result

    return WorkflowRecordResult(
        standard_commodity_code=record.standard_commodity_code,
        primary_action=WorkflowAction.MANUAL_VERIFY,
        suggested_actions=[WorkflowAction.MANUAL_VERIFY],
        action=WorkflowAction.MANUAL_VERIFY,
        reason="R4-0 framework only: work_02 business rules will be implemented after complete knowledge handover",
        pushable=False,
        ready_to_push=False,
        needs_human_approval=True,
        execution_allowed=False,
        executed=False,
        rule_id="R40_FRAMEWORK_PLACEHOLDER",
        evidence=build_framework_evidence(context),
        manual_operation_nodes=[
            build_manual_operation_node(
                1,
                "人工核查 work_02 业务知识库",
                "等待完整业务流程知识库交接后，再补充 work_02 离线规则。",
            )
        ],
    )


def analyze_records(records: list[Any]) -> WorkflowAnalyzeResponse:
    results: list[WorkflowRecordResult] = []
    for rec in records:
        parsed = rec if isinstance(rec, PriceFeedbackRecord) else PriceFeedbackRecord.model_validate(rec)
        results.append(evaluate_price_feedback_record(parsed))

    return WorkflowAnalyzeResponse(
        workflow_name="work_02_price_index_feedback",
        total_records=len(results),
        summary={
            "phase": "R4-0",
            "mode": "framework_only",
            "business_rules_implemented": False,
            "total": len(results),
        },
        record_results=results,
        execution_allowed=False,
        executed=False,
    )
