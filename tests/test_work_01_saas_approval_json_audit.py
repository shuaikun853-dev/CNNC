import os
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.schemas.workflow import SaasSameProductApprovalItem, WorkflowAction
from app.services.link_audit_worker import LinkAuditResult, check_price_consistency, check_sku_link_consistency
from app.services.title_match_model_adapter import TitleMatchModelRequest, TitleMatchModelResult, call_title_match_model
from app.workflows.work_01_saas_approval_json_audit import (
    audit_saas_approval_item,
    audit_saas_approval_records,
    audit_title_similarity,
)


SAMPLE = {
    "vCommodityId": "679416640600166400",
    "queryStr": "世达 (SATA) 电工维修组合工具 06005A 27件/套 销售单位：套 （电工维修组合工具\\公制 27件套） （计量单位：套）",
    "syncState": "1",
    "skuId": "100237336261",
    "skuName": "世达（SATA）06005 替代款06005A 27件基本维修组套",
    "modelAuditResult": "1",
    "isMatch": "1",
    "vendorName": "jd",
    "shopName": "京东商城",
    "price": "347.48",
    "commentNum": "",
    "extLink": "https://item.jd.com/100237336261.html",
    "createTime": "2026-06-17 14:16:41",
    "updateTime": "2026-06-17 14:16:46",
    "isOperate": 1,
}


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


def _item(**overrides) -> SaasSameProductApprovalItem:
    data = dict(SAMPLE)
    data.update(overrides)
    return SaasSameProductApprovalItem(**data)


def test_parse_sample_json_and_manual_review_replacement_reason():
    result = audit_saas_approval_item(_item(), use_model=False)
    assert result.vCommodityId == "679416640600166400"
    assert result.json_price == Decimal("347.48")
    assert result.title_match_result == "MANUAL_REVIEW"
    assert "替代款" in result.reason
    assert result.primary_action == WorkflowAction.MANUAL_CHECK


def test_jd_sku_link_match():
    result, evidence = check_sku_link_consistency("100237336261", "https://item.jd.com/100237336261.html")
    assert result == "MATCH"
    assert "京东链接 SKU 与 skuId 一致" in evidence


def test_jd_sku_link_mismatch():
    result, evidence = check_sku_link_consistency("1", "https://item.jd.com/100237336261.html")
    assert result == "MISMATCH"
    assert "京东链接 SKU 与 skuId 不一致" in evidence


def test_unsupported_link_unknown():
    result, evidence = check_sku_link_consistency("1", "https://example.com/1")
    assert result == "UNKNOWN"
    assert "暂不支持该商城链接 SKU 解析，需人工核验" in evidence


def test_price_string_to_decimal():
    assert _item(price="347.48").price == Decimal("347.48")


def test_empty_price_returns_json_price_missing():
    result, evidence = check_price_consistency(_item(price="").price, Decimal("1"))
    assert result == "JSON_PRICE_MISSING"
    assert "SaaS JSON 价格为空" in evidence


def test_missing_page_actual_price_waits_check():
    result, evidence = check_price_consistency(Decimal("1"), None)
    assert result == "WAIT_PAGE_PRICE_CHECK"
    assert "缺少页面实际价格，需人工打开链接核验" in evidence


def test_page_actual_price_match():
    result, evidence = check_price_consistency(Decimal("1.00"), Decimal("1.009"))
    assert result == "MATCH"
    assert "SaaS JSON 价格与页面实际价格一致" in evidence


def test_page_actual_price_mismatch():
    result, evidence = check_price_consistency(Decimal("1.00"), Decimal("1.02"))
    assert result == "MISMATCH"
    assert "SaaS JSON 价格与页面实际价格不一致" in evidence


def test_title_model_match_likely_match():
    result, evidence = audit_title_similarity("阀门 DN20 1套", "某品牌 DN20 阀门 1套", "", "")
    assert result == "SAME"
    assert any("核心型号命中" in item for item in evidence)


def test_title_replacement_manual_review():
    result, evidence = audit_title_similarity(SAMPLE["queryStr"], SAMPLE["skuName"], "1", "1")
    assert result == "MANUAL_REVIEW"
    assert any("替代款" in item for item in evidence)


def test_model_flags_add_evidence():
    _, evidence = audit_title_similarity("阀门 DN20", "阀门 DN20", "1", "1")
    assert "SaaS模型判断为同款" in evidence


def test_model_unavailable_does_not_interrupt(monkeypatch):
    monkeypatch.delenv("TITLE_MATCH_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("TITLE_MATCH_MODEL_BASE_URL", raising=False)
    result = call_title_match_model(TitleMatchModelRequest(query_str="阀门 DN20", sku_name="阀门 DN20"))
    assert result.match_result == "MODEL_UNAVAILABLE"


def test_model_same_participates_in_approve():
    model = TitleMatchModelResult(match_result="SAME", confidence=0.91, model_match="exact")
    result = audit_saas_approval_item(_item(
        skuId="100237336261",
        extLink="https://item.jd.com/100237336261.html",
        price="10.00",
        pageActualPrice="10.00",
    ), title_model_result=model)
    assert result.title_match_result == "SAME"
    assert result.title_match_confidence == 0.91
    assert result.primary_action == WorkflowAction.APPROVE


def test_model_not_same_rejects():
    model = TitleMatchModelResult(match_result="NOT_SAME", confidence=0.88, model_match="mismatch")
    result = audit_saas_approval_item(_item(price="10.00", pageActualPrice="10.00"), title_model_result=model)
    assert result.primary_action == WorkflowAction.REJECT


def test_model_manual_review_manual_check():
    model = TitleMatchModelResult(match_result="MANUAL_REVIEW", confidence=0.5, model_match="partial")
    result = audit_saas_approval_item(_item(price="10.00", pageActualPrice="10.00"), title_model_result=model)
    assert result.primary_action == WorkflowAction.MANUAL_CHECK


def test_likely_match_sku_match_price_match_approve():
    result = audit_saas_approval_item(_item(
        queryStr="阀门 DN20 1套",
        skuName="某品牌阀门 DN20 1套",
        skuId="100237336261",
        extLink="https://item.jd.com/100237336261.html",
        price="10.00",
        pageActualPrice="10.00",
    ), use_model=False)
    assert result.title_match_result == "SAME"
    assert result.sku_link_check == "MATCH"
    assert result.price_check == "MATCH"
    assert result.primary_action == WorkflowAction.APPROVE


def test_price_mismatch_reject():
    result = audit_saas_approval_item(_item(
        queryStr="阀门 DN20",
        skuName="阀门 DN20",
        price="10.00",
        pageActualPrice="11.00",
    ), use_model=False)
    assert result.price_check == "MISMATCH"
    assert result.primary_action == WorkflowAction.REJECT


def test_sku_mismatch_reject():
    result = audit_saas_approval_item(_item(
        queryStr="阀门 DN20",
        skuName="阀门 DN20",
        skuId="1",
        extLink="https://item.jd.com/2.html",
        price="10.00",
        pageActualPrice="10.00",
    ), use_model=False)
    assert result.sku_link_check == "MISMATCH"
    assert result.primary_action == WorkflowAction.REJECT


def test_wait_page_price_check_manual_check():
    result = audit_saas_approval_item(_item(queryStr="阀门 DN20", skuName="阀门 DN20", pageActualPrice=None), use_model=False)
    assert result.price_check == "WAIT_PAGE_PRICE_CHECK"
    assert result.primary_action == WorkflowAction.MANUAL_CHECK


def test_reachable_false_reject(monkeypatch):
    import app.workflows.work_01_saas_approval_json_audit as mod

    def fake_audit_link(_request):
        return LinkAuditResult(
            reachable=False,
            off_shelf=None,
            sku_link_check="MATCH",
            page_price=Decimal("10.00"),
            price_check="MATCH",
            evidence=["链接不可访问"],
        )

    monkeypatch.setattr(mod, "audit_link", fake_audit_link)
    result = audit_saas_approval_item(_item(queryStr="阀门 DN20", skuName="阀门 DN20", price="10.00", pageActualPrice="10.00"), use_model=False)
    assert result.reachable is False
    assert result.primary_action == WorkflowAction.REJECT


def test_off_shelf_true_reject(monkeypatch):
    import app.workflows.work_01_saas_approval_json_audit as mod

    def fake_audit_link(_request):
        return LinkAuditResult(
            reachable=True,
            off_shelf=True,
            sku_link_check="MATCH",
            page_price=Decimal("10.00"),
            price_check="MATCH",
            evidence=["商品疑似下架"],
        )

    monkeypatch.setattr(mod, "audit_link", fake_audit_link)
    result = audit_saas_approval_item(_item(queryStr="阀门 DN20", skuName="阀门 DN20", price="10.00", pageActualPrice="10.00"), use_model=False)
    assert result.off_shelf is True
    assert result.primary_action == WorkflowAction.REJECT


def test_output_is_recommendation_only():
    response = audit_saas_approval_records([_item(), _item(vCommodityId="v2")], use_model=False)
    assert response.execution_allowed is False
    assert response.executed is False
    for result in response.record_results:
        assert result.execution_allowed is False
        assert result.executed is False
        assert result.needs_human_approval is True


def test_summary_group_counts():
    response = audit_saas_approval_records([
        _item(vCommodityId="v1", pageActualPrice="347.48"),
        _item(vCommodityId="v1", skuId="1", extLink="https://item.jd.com/2.html", pageActualPrice="347.48"),
    ], use_model=False)
    assert response.summary["total_records"] == 2
    assert response.summary["total_v_commodities"] == 1
    assert response.summary["sku_mismatch_count"] == 1
    assert response.summary["group.v1.candidate_count"] == 2
    assert response.summary["group.v1.valid_mall_count"] == 1


class TestSaasAuditAPI:
    def test_api_token_not_configured_rejected(self):
        _set_token(None)
        from app.main import app
        client = TestClient(app)
        resp = client.post("/workflows/same-product-approval/audit-saas-json", json={"scope": _scope(), "records": [SAMPLE]})
        assert resp.status_code == 403

    def test_api_wrong_token_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-approval/audit-saas-json",
            json={"scope": _scope(), "records": [SAMPLE]},
            headers={"X-Local-Workflow-Token": "wrong"},
        )
        assert resp.status_code == 403

    def test_api_correct_token_passes(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-approval/audit-saas-json",
            json={"scope": _scope(), "records": [SAMPLE]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["record_results"][0]["execution_allowed"] is False

    def test_api_non_cnnc_scope_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        bad_scope = {"project": "OTHER", "tenant_id": 999, "commodity_pool_id": 999}
        resp = client.post(
            "/workflows/same-product-approval/audit-saas-json",
            json={"scope": bad_scope, "records": [SAMPLE]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code in (400, 422)

    def test_api_empty_records_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-approval/audit-saas-json",
            json={"scope": _scope(), "records": []},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 422

    def test_diagnose_and_health_not_affected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        assert client.get("/health").status_code == 200
        assert client.post("/diagnose", json={"question": "中核同款审批"}).status_code in (200, 422)

    def test_work_01_original_rules_not_affected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-approval/analyze",
            json={"scope": _scope(), "records": [{"scope": _scope(), "standard_commodity_code": "SP001"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["record_results"][0]["rule_id"] == "R02_NO_CANDIDATES"

    def test_work_02_framework_not_affected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/price-index-feedback/analyze",
            json={"scope": _scope(), "records": [{"scope": _scope(), "standard_commodity_code": "SP001"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["record_results"][0]["rule_id"] == "R40_FRAMEWORK_PLACEHOLDER"

    def test_work_03_placeholder_not_affected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _scope(), "records": [{"scope": _scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["record_results"][0]["rule_id"] == "R35_PLACEHOLDER"
