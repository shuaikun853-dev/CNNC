import pytest
from app.workflows.scope_guard import (
    validate_request_scope,
    validate_records_scope,
    ScopeGuardResult,
)
from app.schemas.workflow import WorkflowScope


def _cnnc_scope():
    return WorkflowScope(project="CNNC", tenant_id=188605, commodity_pool_id=222)


def test_cnnc_scope_passes():
    result = validate_request_scope(_cnnc_scope())
    assert result.allowed is True
    assert result.error_code is None


def test_non_cnnc_project_rejected():
    scope = WorkflowScope.model_construct(project="OTHER", tenant_id=188605, commodity_pool_id=222)
    result = validate_request_scope(scope)
    assert result.allowed is False
    assert result.error_code == "OUT_OF_SCOPE"


def test_wrong_tenant_id_rejected():
    scope = WorkflowScope.model_construct(project="CNNC", tenant_id=874649, commodity_pool_id=222)
    result = validate_request_scope(scope)
    assert result.allowed is False
    assert result.error_code == "OUT_OF_SCOPE"


def test_wrong_commodity_pool_id_rejected():
    scope = WorkflowScope.model_construct(project="CNNC", tenant_id=188605, commodity_pool_id=999)
    result = validate_request_scope(scope)
    assert result.allowed is False
    assert result.error_code == "OUT_OF_SCOPE"


def test_records_scope_all_match():
    req_scope = _cnnc_scope()
    records = [
        {"scope": {"project": "CNNC", "tenant_id": 188605, "commodity_pool_id": 222}, "standard_commodity_code": "SP001"},
        {"scope": {"project": "CNNC", "tenant_id": 188605, "commodity_pool_id": 222}, "standard_commodity_code": "SP002"},
    ]
    result = validate_records_scope(req_scope, records)
    assert result.allowed is True


def test_records_scope_one_mismatch_whole_batch_rejected():
    req_scope = _cnnc_scope()
    records = [
        {"scope": {"project": "CNNC", "tenant_id": 188605, "commodity_pool_id": 222}, "standard_commodity_code": "SP001"},
        {"scope": {"project": "CNNC", "tenant_id": 874649, "commodity_pool_id": 222}, "standard_commodity_code": "SP002"},
    ]
    result = validate_records_scope(req_scope, records)
    assert result.allowed is False
    assert result.error_code == "OUT_OF_SCOPE"
    assert "1" in result.message


def test_records_scope_missing_scope_rejected():
    req_scope = _cnnc_scope()
    records = [
        {"standard_commodity_code": "SP001"},
    ]
    result = validate_records_scope(req_scope, records)
    assert result.allowed is False
    assert result.error_code == "OUT_OF_SCOPE"


def test_records_scope_invalid_scope_format_rejected():
    req_scope = _cnnc_scope()
    records = [
        {"scope": {"project": "INVALID"}, "standard_commodity_code": "SP001"},
    ]
    result = validate_records_scope(req_scope, records)
    assert result.allowed is False
    assert "格式不合法" in result.message


def test_empty_records_passes():
    req_scope = _cnnc_scope()
    result = validate_records_scope(req_scope, [])
    assert result.allowed is True
