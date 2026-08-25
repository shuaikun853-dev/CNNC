import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.schemas.workflow import (
    CustomerLinkRecord,
    PriceFeedbackRecord,
    SameProductCandidate,
    SameProductRecord,
    WorkflowAction,
    WorkflowScope,
)
from app.workflows.work_02_price_index_feedback import (
    derive_current_same_product_metrics,
    derive_customer_link_metrics,
    evaluate_price_feedback_record,
    get_customer_link_price,
    is_valid_customer_link,
    normalize_mall_name,
)


CNNC_SCOPE = WorkflowScope(project="CNNC", tenant_id=188605, commodity_pool_id=222)
FORBIDDEN_EVIDENCE_PHRASES = ["建议导入", "建议解绑", "建议重算", "建议推送", "已执行", "已导入", "已解绑", "已重算", "已推送"]


def _scope(**overrides):
    base = {"project": "CNNC", "tenant_id": 188605, "commodity_pool_id": 222}
    base.update(overrides)
    return base


def _set_token(value=None):
    if value is None:
        os.environ.pop("LOCAL_WORKFLOW_TOKEN", None)
    else:
        os.environ["LOCAL_WORKFLOW_TOKEN"] = value


@pytest.fixture(autouse=True)
def _clean_token():
    original = os.environ.get("LOCAL_WORKFLOW_TOKEN")
    yield
    if original is None:
        os.environ.pop("LOCAL_WORKFLOW_TOKEN", None)
    else:
        os.environ["LOCAL_WORKFLOW_TOKEN"] = original


def _link(
    *,
    mall_name: str | None = "京东",
    price: Decimal | None = Decimal("100"),
    page_actual_price: Decimal | None = None,
    is_valid_link: bool = True,
    is_same_product: bool | None = True,
) -> CustomerLinkRecord:
    return CustomerLinkRecord(
        url="https://example.test/item",
        mall_name=mall_name,
        price=price,
        page_actual_price=page_actual_price,
        is_valid_link=is_valid_link,
        is_same_product=is_same_product,
    )


def _candidate(
    *,
    mall_name: str = "京东",
    crawl_price: Decimal | None = Decimal("100"),
    page_actual_price: Decimal | None = None,
    is_same_product: bool | None = True,
    is_link_valid: bool = True,
    is_product_on_shelf: bool = True,
) -> SameProductCandidate:
    return SameProductCandidate(
        link="https://example.test/candidate",
        mall_name=mall_name,
        crawl_price=crawl_price,
        page_actual_price=page_actual_price,
        is_same_product=is_same_product,
        is_link_valid=is_link_valid,
        is_product_on_shelf=is_product_on_shelf,
    )


def _same_record(*candidates: SameProductCandidate) -> SameProductRecord:
    return SameProductRecord(
        scope=CNNC_SCOPE,
        standard_commodity_code="SP001",
        candidates=list(candidates),
    )


def _record(**overrides) -> PriceFeedbackRecord:
    base = {
        "scope": CNNC_SCOPE,
        "standard_commodity_code": "SP001",
        "feedback_type": "价格异常",
    }
    base.update(overrides)
    return PriceFeedbackRecord(**base)


class TestCustomerLinkMetrics:
    def test_normalize_mall_name_aliases(self):
        assert normalize_mall_name("JD") == "京东"
        assert normalize_mall_name("京东商城") == "京东"
        assert normalize_mall_name("Tmall") == "天猫"
        assert normalize_mall_name("Taobao") == "淘宝"
        assert normalize_mall_name("苏宁易购") == "苏宁"

    def test_page_actual_price_takes_precedence(self):
        link = _link(price=Decimal("100"), page_actual_price=Decimal("80"))
        assert get_customer_link_price(link) == Decimal("80")

    def test_price_non_positive_invalid(self):
        link = _link(price=Decimal("0"), page_actual_price=None)
        assert is_valid_customer_link(link) is False
        metrics = derive_customer_link_metrics([link])
        assert metrics.valid_count == 0
        assert metrics.has_price_invalid is True

    def test_page_actual_price_non_positive_and_price_non_positive_invalid(self):
        link = _link(price=Decimal("0"), page_actual_price=Decimal("-1"))
        assert is_valid_customer_link(link) is False
        assert derive_customer_link_metrics([link]).valid_count == 0

    def test_invalid_link_not_counted(self):
        metrics = derive_customer_link_metrics([_link(is_valid_link=False)])
        assert metrics.valid_count == 0
        assert metrics.has_invalid_link is True

    def test_not_same_product_not_counted(self):
        metrics = derive_customer_link_metrics([_link(is_same_product=False)])
        assert metrics.valid_count == 0
        assert metrics.has_not_same_product is True

    def test_none_same_product_not_counted(self):
        metrics = derive_customer_link_metrics([_link(is_same_product=None)])
        assert metrics.valid_count == 0
        assert metrics.has_not_same_product is True

    def test_blank_mall_name_not_counted(self):
        metrics = derive_customer_link_metrics([_link(mall_name="")])
        assert metrics.valid_count == 0

    def test_two_different_malls_counted(self):
        metrics = derive_customer_link_metrics([
            _link(mall_name="京东", price=Decimal("100")),
            _link(mall_name="天猫", price=Decimal("110")),
        ])
        assert metrics.valid_count == 2
        assert metrics.valid_mall_count == 2

    def test_two_same_mall_counted_once(self):
        metrics = derive_customer_link_metrics([
            _link(mall_name="JD", price=Decimal("100")),
            _link(mall_name="京东商城", price=Decimal("110")),
        ])
        assert metrics.valid_count == 2
        assert metrics.valid_mall_count == 1

    def test_price_range_and_diff_pct(self):
        metrics = derive_customer_link_metrics([
            _link(mall_name="京东", price=Decimal("100")),
            _link(mall_name="天猫", price=Decimal("130")),
        ])
        assert metrics.min_price == Decimal("100")
        assert metrics.max_price == Decimal("130")
        assert metrics.price_diff_pct == Decimal("0.3")


class TestCurrentSameProductMetrics:
    def test_derive_valid_candidates_from_candidate_details(self):
        metrics = derive_current_same_product_metrics([
            _same_record(_candidate(mall_name="京东"), _candidate(mall_name="天猫"))
        ], None, None)
        assert metrics.valid_candidate_count == 2
        assert metrics.valid_mall_count == 2

    def test_does_not_trust_external_summary_fields(self):
        record = _same_record()
        metrics = derive_current_same_product_metrics([record], None, None)
        assert metrics.valid_candidate_count == 0
        assert metrics.valid_mall_count == 0

    def test_wrong_candidate_count(self):
        metrics = derive_current_same_product_metrics([
            _same_record(_candidate(is_same_product=False))
        ], None, None)
        assert metrics.wrong_candidate_count == 1

    def test_reference_price_high_premium_count(self):
        metrics = derive_current_same_product_metrics([
            _same_record(_candidate(crawl_price=Decimal("131")))
        ], Decimal("100"), None)
        assert metrics.high_premium_count == 1

    def test_current_index_price_high_premium_count(self):
        metrics = derive_current_same_product_metrics([
            _same_record(_candidate(crawl_price=Decimal("131")))
        ], None, Decimal("100"))
        assert metrics.high_premium_count == 1

    def test_missing_candidates_does_not_create_business_conclusion(self):
        result = evaluate_price_feedback_record(_record(current_same_products=[_same_record()]))
        assert result.rule_id == "R40_FRAMEWORK_PLACEHOLDER"
        assert result.primary_action == WorkflowAction.MANUAL_VERIFY
        assert "缺少当前同款明细，需人工核查" in " ".join(result.evidence)


class TestR40DefaultResponse:
    def test_default_response_is_framework_placeholder(self):
        result = evaluate_price_feedback_record(_record(customer_links=[_link()], current_same_products=[_same_record(_candidate())]))
        assert result.rule_id == "R40_FRAMEWORK_PLACEHOLDER"
        assert result.primary_action == WorkflowAction.MANUAL_VERIFY
        assert result.suggested_actions == [WorkflowAction.MANUAL_VERIFY]
        assert result.execution_allowed is False
        assert result.executed is False
        assert result.needs_human_approval is True
        assert result.pushable is False
        assert result.ready_to_push is False

    def test_default_response_evidence_contains_metrics_and_knowledge_status(self):
        result = evaluate_price_feedback_record(_record(customer_links=[_link()], current_same_products=[_same_record(_candidate())]))
        evidence = " ".join(result.evidence)
        assert "customer_links.total_count=1" in evidence
        assert "customer_links.valid_count=1" in evidence
        assert "current_same_products.valid_candidate_count=1" in evidence
        assert "业务知识库未完整交接，暂不输出最终处理结论" in evidence

    def test_default_response_evidence_has_no_forbidden_advice(self):
        result = evaluate_price_feedback_record(_record(customer_links=[_link()], current_same_products=[_same_record(_candidate())]))
        evidence = " ".join(result.evidence)
        for phrase in FORBIDDEN_EVIDENCE_PHRASES:
            assert phrase not in evidence

    def test_manual_operation_node_is_safe(self):
        result = evaluate_price_feedback_record(_record())
        assert result.manual_operation_nodes
        node = result.manual_operation_nodes[0]
        assert node.related_action == WorkflowAction.MANUAL_VERIFY
        assert node.is_executable_by_agent is False
        assert node.execution_allowed is False
        assert node.executed is False


class TestWork02API:
    def test_price_index_feedback_api_returns_framework_placeholder(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/price-index-feedback/analyze",
            json={"scope": _scope(), "records": [{"scope": _scope(), "standard_commodity_code": "SP001"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["phase"] == "R4-0"
        assert data["summary"]["mode"] == "framework_only"
        assert data["summary"]["business_rules_implemented"] is False
        assert data["record_results"][0]["rule_id"] == "R40_FRAMEWORK_PLACEHOLDER"
        assert data["record_results"][0]["primary_action"] == "MANUAL_VERIFY"

    def test_token_not_configured_rejected(self):
        _set_token(None)
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/price-index-feedback/analyze",
            json={"scope": _scope(), "records": [{"scope": _scope(), "standard_commodity_code": "SP001"}]},
        )
        assert resp.status_code == 403

    def test_wrong_token_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/price-index-feedback/analyze",
            json={"scope": _scope(), "records": [{"scope": _scope(), "standard_commodity_code": "SP001"}]},
            headers={"X-Local-Workflow-Token": "wrong"},
        )
        assert resp.status_code == 403

    def test_correct_token_local_passes(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/price-index-feedback/analyze",
            json={"scope": _scope(), "records": [{"scope": _scope(), "standard_commodity_code": "SP001"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 200

    def test_non_cnnc_scope_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        bad_scope = {"project": "OTHER", "tenant_id": 999, "commodity_pool_id": 999}
        resp = client.post(
            "/workflows/price-index-feedback/analyze",
            json={"scope": bad_scope, "records": [{"scope": bad_scope, "standard_commodity_code": "SP001"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code in (400, 422)

    def test_mixed_scope_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/price-index-feedback/analyze",
            json={"scope": _scope(), "records": [{"scope": _scope(tenant_id=999999), "standard_commodity_code": "SP001"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code in (400, 422)

    def test_empty_records_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/price-index-feedback/analyze",
            json={"scope": _scope(), "records": []},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 422

    def test_diagnose_not_affected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post("/diagnose", json={"question": "中核价格反馈"})
        assert resp.status_code in (200, 422)

    def test_health_not_affected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
