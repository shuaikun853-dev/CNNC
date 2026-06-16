import os
import pytest
from decimal import Decimal
from fastapi.testclient import TestClient

from app.schemas.workflow import (
    BrandInfo,
    CollectionConfig,
    ImportTemplateField,
    ManualOperationNode,
    PriceFeedbackRecord,
    SearchTermConfig,
    SupplementSameProductAnalyzeRequest,
    SupplementSameProductRecord,
    WorkflowAction,
    WorkflowAnalyzeResponse,
    WorkflowRecordResult,
    WorkflowScope,
)
from app.services.workflow_knowledge_adapter import get_workflow_knowledge_files
from app.workflows.human_approval_guard import enforce_recommendation_only
from app.workflows.router import route_workflow


def _cnnc_scope(**overrides):
    base = {"project": "CNNC", "tenant_id": 188605, "commodity_pool_id": 222}
    base.update(overrides)
    return base


def _set_token(value=None):
    if value is not None:
        os.environ["LOCAL_WORKFLOW_TOKEN"] = value
    else:
        os.environ.pop("LOCAL_WORKFLOW_TOKEN", None)


@pytest.fixture(autouse=True)
def _clean_token():
    original = os.environ.get("LOCAL_WORKFLOW_TOKEN")
    yield
    if original is not None:
        os.environ["LOCAL_WORKFLOW_TOKEN"] = original
    else:
        os.environ.pop("LOCAL_WORKFLOW_TOKEN", None)


class TestKnowledgeAdapter:
    def test_work_03_returns_four_files(self):
        files = get_workflow_knowledge_files("work_03_same_product_shortage")
        assert len(files) == 4
        assert "cnnc_work_03_supplement_same_product.md" in files
        assert "cnnc_sars_import_rules.md" in files
        assert "cnnc_base_scope.md" in files
        assert "cnnc_forbidden_actions.md" in files

    def test_unknown_workflow_returns_empty(self):
        assert get_workflow_knowledge_files("work_99_nonexistent") == []

    def test_path_traversal_returns_empty(self):
        assert get_workflow_knowledge_files("../../../etc/passwd") == []

    def test_work_01_mapping_unchanged(self):
        files = get_workflow_knowledge_files("work_01_same_product_approval")
        assert "cnnc_work_01_same_product_approval.md" in files

    def test_work_02_mapping_unchanged(self):
        files = get_workflow_knowledge_files("work_02_price_index_feedback")
        assert "cnnc_work_02_price_index_feedback.md" in files

    def test_diagnose_mapping_unchanged(self):
        files = get_workflow_knowledge_files("diagnose")
        assert "cnnc_sql_diagnosis_knowledge.md" in files


class TestRouter:
    @pytest.mark.parametrize("keyword", [
        "同款不足", "补同款", "无同款", "补同款流程",
        "同款不足筛查", "周一补同款", "周三前补同款",
    ])
    def test_work_03_keywords(self, keyword):
        resp = route_workflow(keyword)
        assert resp.workflow_name == "work_03_same_product_shortage"
        assert resp.confidence >= 0.9

    def test_work_01_keyword_unchanged(self):
        resp = route_workflow("同款审批")
        assert resp.workflow_name == "work_01_same_product_approval"

    def test_work_02_keyword_unchanged(self):
        resp = route_workflow("价格异常")
        assert resp.workflow_name == "work_02_price_index_feedback"

    def test_unknown_goes_to_ad_hoc(self):
        resp = route_workflow("随便一个问题")
        assert resp.workflow_name == "work_08_ad_hoc"


class TestSchemas:
    def test_supplement_record_sars_monitor_code_optional(self):
        rec = SupplementSameProductRecord(
            scope=WorkflowScope.model_construct(
                project="CNNC", tenant_id=188605, commodity_pool_id=222
            ),
            commodity_name="测试商品",
            sars_monitor_code="",
        )
        assert rec.sars_monitor_code == ""

    def test_supplement_record_missing_scope_rejected(self):
        with pytest.raises(Exception):
            SupplementSameProductRecord(commodity_name="测试商品")

    def test_analyze_request_empty_records_rejected(self):
        with pytest.raises(Exception):
            SupplementSameProductAnalyzeRequest(
                scope=WorkflowScope.model_construct(
                    project="CNNC", tenant_id=188605, commodity_pool_id=222
                ),
                records=[],
            )

    def test_collection_config_uses_suggest_field(self):
        cfg = CollectionConfig()
        assert cfg.suggest_enable_platform_model_matching is True
        assert not hasattr(cfg, "enable_model_matching")

    def test_workflow_record_result_manual_operation_nodes_default(self):
        r = WorkflowRecordResult(
            primary_action=WorkflowAction.MANUAL_CHECK,
            reason="test",
        )
        assert r.manual_operation_nodes == []

    def test_price_feedback_record_supports_new_fields(self):
        rec = PriceFeedbackRecord.model_construct(
            scope=WorkflowScope.model_construct(
                project="CNNC", tenant_id=188605, commodity_pool_id=222
            ),
            feedback_date="2026-06-16",
            feedback_reason="价格偏高",
        )
        assert rec.feedback_date == "2026-06-16"
        assert rec.feedback_reason == "价格偏高"

    def test_price_feedback_record_no_manual_operation_nodes(self):
        with pytest.raises(Exception):
            PriceFeedbackRecord(
                scope=WorkflowScope(project="CNNC", tenant_id=188605, commodity_pool_id=222),
                feedback_date="2026-06-16",
                manual_operation_nodes=[],
            )

    def test_brand_info_schema(self):
        bi = BrandInfo(original_brand="贝米普(BMP)", cleaned_brand="贝米普", model="DN20")
        assert bi.cleaned_brand == "贝米普"

    def test_search_term_config_schema(self):
        st = SearchTermConfig(full_search_term="贝米普 铜阀门 DN20")
        assert st.full_search_term == "贝米普 铜阀门 DN20"

    def test_import_template_field_schema(self):
        itf = ImportTemplateField(standard_commodity_code="SP001", price=Decimal("99.9"))
        assert itf.price == Decimal("99.9")


class TestManualOperationNodeGuard:
    def test_guard_forces_all_safety_fields_false(self):
        bad_node = ManualOperationNode(
            step=1,
            title="测试",
            description="测试",
            is_executable_by_agent=True,
            execution_allowed=True,
            executed=True,
        )
        rec = WorkflowRecordResult(
            primary_action=WorkflowAction.MANUAL_CHECK,
            reason="test",
            manual_operation_nodes=[bad_node],
        )
        resp = WorkflowAnalyzeResponse(
            workflow_name="test",
            total_records=1,
            record_results=[rec],
        )
        result = enforce_recommendation_only(resp)
        node = result.record_results[0].manual_operation_nodes[0]
        assert node.is_executable_by_agent is False
        assert node.execution_allowed is False
        assert node.executed is False

    def test_guard_record_still_safe(self):
        rec = WorkflowRecordResult(
            primary_action=WorkflowAction.MANUAL_CHECK,
            reason="test",
        )
        resp = WorkflowAnalyzeResponse(
            workflow_name="test",
            total_records=1,
            record_results=[rec],
        )
        result = enforce_recommendation_only(resp)
        r = result.record_results[0]
        assert r.execution_allowed is False
        assert r.executed is False
        assert r.needs_human_approval is True


class TestWork03API:
    def test_token_not_configured_rejected(self):
        _set_token(None)
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
        )
        assert resp.status_code == 403

    def test_token_wrong_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "wrong"},
        )
        assert resp.status_code == 403

    def test_token_correct_local_passes(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 200

    def test_non_cnnc_scope_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        bad_scope = {"project": "OTHER", "tenant_id": 999, "commodity_pool_id": 999}
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": bad_scope, "records": [{"scope": bad_scope, "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code in (400, 422)

    def test_mixed_scope_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={
                "scope": _cnnc_scope(),
                "records": [{"scope": _cnnc_scope(tenant_id=999999), "commodity_name": "测试"}],
            },
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code in (400, 422)

    def test_empty_records_rejected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": []},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 422

    def test_placeholder_primary_action(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        data = resp.json()
        assert data["record_results"][0]["primary_action"] == "MANUAL_CHECK"

    def test_placeholder_rule_id(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        data = resp.json()
        assert data["record_results"][0]["rule_id"] == "R35_PLACEHOLDER"

    def test_placeholder_execution_allowed_false(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        data = resp.json()
        assert data["record_results"][0]["execution_allowed"] is False

    def test_placeholder_executed_false(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        data = resp.json()
        assert data["record_results"][0]["executed"] is False

    def test_placeholder_needs_human_approval_true(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        data = resp.json()
        assert data["record_results"][0]["needs_human_approval"] is True

    def test_placeholder_no_sql_advice(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        data = resp.json()
        evidence = " ".join(data["record_results"][0].get("evidence", []))
        assert "SQL" not in evidence
        assert "sql" not in evidence.lower()

    def test_placeholder_no_brand_advice(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        data = resp.json()
        evidence = " ".join(data["record_results"][0].get("evidence", []))
        assert "品牌" not in evidence

    def test_placeholder_no_search_term_advice(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        data = resp.json()
        evidence = " ".join(data["record_results"][0].get("evidence", []))
        assert "搜索词" not in evidence

    def test_placeholder_no_import_template_advice(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post(
            "/workflows/same-product-shortage/analyze",
            json={"scope": _cnnc_scope(), "records": [{"scope": _cnnc_scope(), "commodity_name": "测试"}]},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        data = resp.json()
        evidence = " ".join(data["record_results"][0].get("evidence", []))
        assert "导入模板" not in evidence
        assert "zhImport" not in evidence

    def test_diagnose_not_affected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.post("/diagnose", json={"question": "中核同款审批"})
        assert resp.status_code in (200, 422)

    def test_health_not_affected(self):
        _set_token("correct-token")
        from app.main import app
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
