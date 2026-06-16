from app.schemas.workflow import (
    ManualOperationNode,
    WorkflowAction,
    WorkflowAnalyzeResponse,
    WorkflowRecordResult,
)


HIGH_RISK_ACTIONS = frozenset({
    WorkflowAction.APPROVE,
    WorkflowAction.REJECT,
    WorkflowAction.READY_TO_PUSH,
    WorkflowAction.IMPORT_CUSTOMER_LINKS,
    WorkflowAction.UNBIND_WRONG_RELATIONS,
    WorkflowAction.RECALCULATE_INDEX,
    WorkflowAction.PUSH_INDEX,
    WorkflowAction.RETRY_PUSH,
})

FORBIDDEN_EXECUTED_PHRASES = frozenset({
    "已执行",
    "已推送",
    "已解绑",
    "已驳回",
    "已重算",
    "已导入生产系统",
    "已自动推送",
    "已自动解绑",
    "已自动驳回",
    "已自动重算",
})


def enforce_recommendation_only(
    response: WorkflowAnalyzeResponse,
) -> WorkflowAnalyzeResponse:
    updated_results = []
    for r in response.record_results:
        needs_approval = r.needs_human_approval
        if r.primary_action in HIGH_RISK_ACTIONS:
            needs_approval = True

        reason = r.reason
        for phrase in FORBIDDEN_EXECUTED_PHRASES:
            if phrase in reason:
                reason = reason.replace(phrase, f"建议{phrase[1:]}")

        suggested = list(r.suggested_actions)
        if r.primary_action not in suggested:
            suggested.insert(0, r.primary_action)

        sanitized_nodes = []
        for node in r.manual_operation_nodes:
            sanitized_nodes.append(
                ManualOperationNode(
                    step=node.step,
                    title=node.title,
                    description=node.description,
                    prerequisite=node.prerequisite,
                    expected_outcome=node.expected_outcome,
                    safety_notice=node.safety_notice,
                    related_action=node.related_action,
                    is_executable_by_agent=False,
                    execution_allowed=False,
                    executed=False,
                )
            )

        updated_results.append(
            WorkflowRecordResult(
                standard_commodity_code=r.standard_commodity_code,
                primary_action=r.primary_action,
                suggested_actions=suggested,
                action=r.primary_action,
                reason=reason,
                pushable=r.pushable,
                ready_to_push=r.ready_to_push,
                needs_human_approval=needs_approval,
                execution_allowed=False,
                executed=False,
                rule_id=r.rule_id,
                evidence=r.evidence,
                manual_operation_nodes=sanitized_nodes,
            )
        )

    return WorkflowAnalyzeResponse(
        workflow_name=response.workflow_name,
        total_records=response.total_records,
        summary=response.summary,
        record_results=updated_results,
        export_files=response.export_files,
        execution_allowed=False,
        executed=False,
    )
