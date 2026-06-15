from app.schemas.workflow import WorkflowRouteResponse


_ROUTE_RULES: list[tuple[list[str], str, float]] = [
    (["同款审批", "新增监控", "同款匹配"], "work_01_same_product_approval", 0.9),
    (["价格异常", "勘误", "反馈", "价格偏高", "价格偏低", "客户反馈"], "work_02_price_index_feedback", 0.9),
    (["同款不足", "补同款", "无同款"], "work_03_same_product_shortage", 0.85),
    (["失败班车", "处理失败"], "work_04_failed_bus", 0.85),
    (["价格指数更新", "周度计算"], "work_05_weekly_index_update", 0.85),
    (["波动", "正负10%", "异常波动"], "work_06_index_fluctuation", 0.85),
    (["推送", "推送失败", "推送后核查"], "work_07_push_check", 0.85),
]

DEFAULT_WORKFLOW = "work_08_ad_hoc"
DEFAULT_CONFIDENCE = 0.4


def route_workflow(question: str) -> WorkflowRouteResponse:
    best_workflow = DEFAULT_WORKFLOW
    best_confidence = DEFAULT_CONFIDENCE

    for keywords, workflow_name, base_confidence in _ROUTE_RULES:
        hits = [kw for kw in keywords if kw in question]
        if hits:
            confidence = min(base_confidence + 0.05 * (len(hits) - 1), 0.98)
            if confidence > best_confidence:
                best_workflow = workflow_name
                best_confidence = confidence

    from app.workflows.base import WORKFLOW_REGISTRY
    wf = WORKFLOW_REGISTRY.get(best_workflow)
    required = wf.required_inputs if wf else []
    optional = wf.optional_inputs if wf else []

    return WorkflowRouteResponse(
        workflow_name=best_workflow,
        confidence=best_confidence,
        required_inputs=required,
        optional_inputs=optional,
        execution_allowed=False,
        executed=False,
    )
