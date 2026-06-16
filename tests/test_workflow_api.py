import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _setup():
    import app.services.knowledge_loader as mod
    mod._sections = None
    mod._load_error = None
    from app.main import app
    yield app


def _cnnc_scope():
    return {"project": "CNNC", "tenant_id": 188605, "commodity_pool_id": 222}


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


def test_workflow_token_not_configured_rejected():
    _set_token(None)
    from app.main import app
    client = TestClient(app)
    resp = client.post(
        "/workflows/route",
        json={"scope": _cnnc_scope(), "question": "中核同款审批"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "LOCAL_WORKFLOW_TOKEN_REQUIRED"


def test_workflow_invalid_token_rejected():
    _set_token("correct-token")
    from app.main import app
    client = TestClient(app)
    resp = client.post(
        "/workflows/route",
        json={"scope": _cnnc_scope(), "question": "中核同款审批"},
        headers={"X-Local-Workflow-Token": "wrong-token"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error_code"] == "INVALID_LOCAL_WORKFLOW_TOKEN"


def test_workflow_correct_token_passes():
    _set_token("correct-token")
    from app.main import app
    client = TestClient(app)
    resp = client.post(
        "/workflows/route",
        json={"scope": _cnnc_scope(), "question": "中核新增监控商品同款审批"},
        headers={"X-Local-Workflow-Token": "correct-token"},
    )
    assert resp.status_code == 200


def test_health_not_affected_by_workflow_token():
    _set_token(None)
    from app.main import app
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_diagnose_not_affected_by_workflow_token():
    _set_token(None)
    from app.main import app
    client = TestClient(app)
    resp = client.post(
        "/diagnose",
        json={"question": "中核每周巡检"},
    )
    assert resp.status_code == 200


def test_route_non_cnnc_question_rejected_even_with_valid_scope():
    _set_token("correct-token")
    from app.main import app
    client = TestClient(app)
    for question in ["宏伟同款审批", "南水北调价格反馈", "华盛价格指数处理", "租户874649同款审批"]:
        resp = client.post(
            "/workflows/route",
            json={"scope": _cnnc_scope(), "question": question},
            headers={"X-Local-Workflow-Token": "correct-token"},
        )
        assert resp.status_code == 400, f"Expected 400 for question: {question}"
        assert resp.json()["detail"]["error_code"] == "OUT_OF_SCOPE"


def test_route_cnnc_question_accepted():
    _set_token("correct-token")
    from app.main import app
    client = TestClient(app)
    resp = client.post(
        "/workflows/route",
        json={"scope": _cnnc_scope(), "question": "中核新增监控商品同款审批"},
        headers={"X-Local-Workflow-Token": "correct-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow_name"] == "work_01_same_product_approval"


def test_analyze_empty_records_rejected():
    _set_token("correct-token")
    from app.main import app
    client = TestClient(app)
    resp = client.post(
        "/workflows/same-product-approval/analyze",
        json={"scope": _cnnc_scope(), "records": []},
        headers={"X-Local-Workflow-Token": "correct-token"},
    )
    assert resp.status_code == 422


def test_analyze_work_01_placeholder():
    _set_token("correct-token")
    from app.main import app
    client = TestClient(app)
    resp = client.post(
        "/workflows/same-product-approval/analyze",
        json={
            "scope": _cnnc_scope(),
            "records": [
                {
                    "scope": _cnnc_scope(),
                    "standard_commodity_code": "SP001",
                }
            ],
        },
        headers={"X-Local-Workflow-Token": "correct-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["execution_allowed"] is False
    assert data["executed"] is False
    assert data["total_records"] == 1
    assert data["record_results"][0]["primary_action"] == "SUPPLEMENT_SAME_PRODUCT"
    assert data["record_results"][0]["rule_id"] == "R02_NO_CANDIDATES"
    assert data["record_results"][0]["execution_allowed"] is False
    assert data["record_results"][0]["executed"] is False
    assert data["record_results"][0]["needs_human_approval"] is True


def test_analyze_work_02_placeholder():
    _set_token("correct-token")
    from app.main import app
    client = TestClient(app)
    resp = client.post(
        "/workflows/price-index-feedback/analyze",
        json={
            "scope": _cnnc_scope(),
            "records": [
                {
                    "scope": _cnnc_scope(),
                    "standard_commodity_code": "SP002",
                    "feedback_type": "价格偏高",
                }
            ],
        },
        headers={"X-Local-Workflow-Token": "correct-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["execution_allowed"] is False
    assert data["record_results"][0]["primary_action"] == "MANUAL_VERIFY"


def test_knowledge_endpoint():
    _set_token("correct-token")
    from app.main import app
    client = TestClient(app)
    resp = client.get(
        "/workflows/work_01_same_product_approval/knowledge",
        headers={"X-Local-Workflow-Token": "correct-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow_name"] == "work_01_same_product_approval"
    assert "cnnc_work_01_same_product_approval.md" in data["knowledge_files"]


def test_knowledge_unknown_workflow():
    _set_token("correct-token")
    from app.main import app
    client = TestClient(app)
    resp = client.get(
        "/workflows/unknown_workflow/knowledge",
        headers={"X-Local-Workflow-Token": "correct-token"},
    )
    assert resp.status_code == 404
