import pytest
from app.services.workflow_knowledge_adapter import (
    get_workflow_knowledge_files,
    is_allowed_knowledge_file,
    ALLOWED_WORKFLOW_KNOWLEDGE_FILES,
)


def test_work_01_returns_whitelist_files():
    files = get_workflow_knowledge_files("work_01_same_product_approval")
    assert len(files) == 3
    assert "cnnc_work_01_same_product_approval.md" in files
    assert "cnnc_base_scope.md" in files
    assert "cnnc_forbidden_actions.md" in files
    for f in files:
        assert f in ALLOWED_WORKFLOW_KNOWLEDGE_FILES


def test_work_02_returns_whitelist_files():
    files = get_workflow_knowledge_files("work_02_price_index_feedback")
    assert len(files) == 4
    assert "cnnc_work_02_price_index_feedback.md" in files
    assert "cnnc_sars_import_rules.md" in files
    assert "cnnc_base_scope.md" in files
    assert "cnnc_forbidden_actions.md" in files
    for f in files:
        assert f in ALLOWED_WORKFLOW_KNOWLEDGE_FILES


def test_diagnose_returns_sql_knowledge():
    files = get_workflow_knowledge_files("diagnose")
    assert "cnnc_sql_diagnosis_knowledge.md" in files
    assert "cnnc_base_scope.md" in files


def test_unknown_workflow_returns_empty():
    files = get_workflow_knowledge_files("unknown_workflow")
    assert files == []


def test_path_traversal_returns_empty():
    assert get_workflow_knowledge_files("../cnnc_work_01_same_product_approval.md") == []
    assert get_workflow_knowledge_files("../../data/docs/cnnc_work_01_same_product_approval.md") == []
    assert get_workflow_knowledge_files("C:\\Windows\\system32\\drivers\\etc\\hosts") == []


def test_is_allowed_knowledge_file():
    assert is_allowed_knowledge_file("cnnc_base_scope.md") is True
    assert is_allowed_knowledge_file("../../../etc/passwd") is False
    assert is_allowed_knowledge_file("宏伟_workflow.md") is False


def test_no_arbitrary_file_read():
    for malicious in [
        "../../etc/passwd",
        "../../../etc/shadow",
        "cnnc_work_01_same_product_approval.md/../../../etc/passwd",
        "CNNC_WORK_01_SAME_PRODUCT_APPROVAL.MD",
    ]:
        assert get_workflow_knowledge_files(malicious) == []
