from decimal import Decimal

from app.schemas.workflow import (
    CustomerLinkRecord,
    PriceFeedbackRecord,
    SameProductCandidate,
    SameProductRecord,
    WorkflowAction,
    WorkflowScope,
)
from app.workflows.work_02_price_index_feedback import analyze_records, evaluate_price_feedback_record


CNNC_SCOPE = WorkflowScope(project="CNNC", tenant_id=188605, commodity_pool_id=222)


def _link() -> CustomerLinkRecord:
    return CustomerLinkRecord(
        url="https://example.test/item",
        mall_name="京东",
        price=Decimal("100"),
        is_valid_link=True,
        is_same_product=True,
    )


def _candidate() -> SameProductCandidate:
    return SameProductCandidate(
        link="https://example.test/candidate",
        mall_name="京东",
        crawl_price=Decimal("100"),
        is_same_product=True,
        is_link_valid=True,
        is_product_on_shelf=True,
    )


def _record(**kwargs) -> PriceFeedbackRecord:
    base = {
        "scope": CNNC_SCOPE,
        "standard_commodity_code": "SP001",
        "feedback_type": "价格异常",
        "customer_links": [_link()],
        "current_same_products": [
            SameProductRecord(
                scope=CNNC_SCOPE,
                standard_commodity_code="SP001",
                candidates=[_candidate()],
            )
        ],
    }
    base.update(kwargs)
    return PriceFeedbackRecord(**base)


def test_work_02_framework_placeholder_for_all_inputs():
    res = evaluate_price_feedback_record(_record(feedback_type="价格偏高"))
    assert res.rule_id == "R40_FRAMEWORK_PLACEHOLDER"
    assert res.primary_action == WorkflowAction.MANUAL_VERIFY
    assert res.suggested_actions == [WorkflowAction.MANUAL_VERIFY]
    assert res.execution_allowed is False
    assert res.executed is False
    assert res.needs_human_approval is True


def test_work_02_framework_does_not_emit_real_business_rule_ids():
    res = evaluate_price_feedback_record(_record(feedback_type="价格偏高", feedback_price=Decimal("140"), current_index_price=Decimal("100")))
    assert not res.rule_id.startswith("W")


def test_analyze_records_summary_framework_only():
    resp = analyze_records([_record().model_dump(), _record(feedback_type="价格偏低").model_dump()])
    assert resp.workflow_name == "work_02_price_index_feedback"
    assert resp.total_records == 2
    assert resp.execution_allowed is False
    assert resp.executed is False
    assert resp.summary["phase"] == "R4-0"
    assert resp.summary["mode"] == "framework_only"
    assert resp.summary["business_rules_implemented"] is False
    assert all(r.rule_id == "R40_FRAMEWORK_PLACEHOLDER" for r in resp.record_results)
