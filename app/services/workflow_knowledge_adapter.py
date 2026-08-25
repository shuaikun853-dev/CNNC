ALLOWED_WORKFLOW_KNOWLEDGE_FILES = frozenset({
    "cnnc_ops_index.md",
    "cnnc_base_scope.md",
    "cnnc_work_01_same_product_approval.md",
    "cnnc_work_02_price_index_feedback.md",
    "cnnc_work_03_supplement_same_product.md",
    "cnnc_work_04_failed_task_exception.md",
    "cnnc_work_05_weekly_index_update.md",
    "cnnc_work_06_index_volatility_exception.md",
    "cnnc_work_07_post_push_verification.md",
    "cnnc_work_08_ad_hoc_query.md",
    "cnnc_sql_diagnosis_knowledge.md",
    "cnnc_sars_import_rules.md",
    "cnnc_forbidden_actions.md",
})

_WORKFLOW_KNOWLEDGE_MAP: dict[str, list[str]] = {
    "work_01_same_product_approval": [
        "cnnc_work_01_same_product_approval.md",
        "cnnc_base_scope.md",
        "cnnc_forbidden_actions.md",
    ],
    "work_02_price_index_feedback": [
        "cnnc_work_02_price_index_feedback.md",
        "cnnc_sars_import_rules.md",
        "cnnc_base_scope.md",
        "cnnc_forbidden_actions.md",
    ],
    "work_03_same_product_shortage": [
        "cnnc_work_03_supplement_same_product.md",
        "cnnc_sars_import_rules.md",
        "cnnc_base_scope.md",
        "cnnc_forbidden_actions.md",
    ],
    "diagnose": [
        "cnnc_sql_diagnosis_knowledge.md",
        "cnnc_base_scope.md",
        "cnnc_forbidden_actions.md",
    ],
}


def get_workflow_knowledge_files(workflow_name: str) -> list[str]:
    files = _WORKFLOW_KNOWLEDGE_MAP.get(workflow_name, [])
    return [f for f in files if f in ALLOWED_WORKFLOW_KNOWLEDGE_FILES]


def is_allowed_knowledge_file(filename: str) -> bool:
    return filename in ALLOWED_WORKFLOW_KNOWLEDGE_FILES
