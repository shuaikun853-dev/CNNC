import pytest
from app.workflows.human_approval_guard import (
    enforce_recommendation_only,
    HIGH_RISK_ACTIONS,
    FORBIDDEN_EXECUTED_PHRASES,
)
from app.schemas.workflow import WorkflowAction, WorkflowAnalyzeResponse, WorkflowRecordResult


def _make_response(actions=None, reason="test") -> WorkflowAnalyzeResponse:
    if actions is None:
        actions = [WorkflowAction.MANUAL_CHECK]
    results = []
    for act in actions:
        results.append(
            WorkflowRecordResult(
                standard_commodity_code="SP001",
                primary_action=act,
                suggested_actions=[act],
                action=act,
                reason=reason,
            )
        )
    return WorkflowAnalyzeResponse(
        workflow_name="test_wf",
        total_records=len(results),
        record_results=results,
    )


def test_enforce_execution_allowed_always_false():
    resp = _make_response([WorkflowAction.APPROVE])
    enforced = enforce_recommendation_only(resp)
    assert enforced.execution_allowed is False
    assert enforced.executed is False
    for r in enforced.record_results:
        assert r.execution_allowed is False
        assert r.executed is False


def test_high_risk_actions_need_human_approval():
    for act in HIGH_RISK_ACTIONS:
        resp = _make_response([act])
        enforced = enforce_recommendation_only(resp)
        assert enforced.record_results[0].needs_human_approval is True, f"{act.value} should need human approval"


def test_non_high_risk_still_forces_approval():
    resp = _make_response([WorkflowAction.WAIT_CRAWLER])
    resp.record_results[0] = resp.record_results[0].model_copy(update={"needs_human_approval": False})
    enforced = enforce_recommendation_only(resp)
    assert enforced.record_results[0].needs_human_approval is True


def test_forbidden_phrases_replaced_in_reason():
    for phrase in FORBIDDEN_EXECUTED_PHRASES:
        resp = _make_response(reason=f"已完成: {phrase}")
        enforced = enforce_recommendation_only(resp)
        assert phrase not in enforced.record_results[0].reason, f"Forbidden phrase '{phrase}' found in reason"


def test_allowed_recommendation_phrases_preserved():
    allowed_phrases = ["建议人工推送", "建议人工解绑", "建议人工驳回", "建议人工重算"]
    for phrase in allowed_phrases:
        resp = _make_response(reason=phrase)
        enforced = enforce_recommendation_only(resp)
        assert phrase in enforced.record_results[0].reason


def test_suggested_actions_includes_primary():
    resp = _make_response([WorkflowAction.APPROVE])
    enforced = enforce_recommendation_only(resp)
    assert enforced.record_results[0].primary_action in enforced.record_results[0].suggested_actions


def test_top_level_execution_always_false():
    resp = WorkflowAnalyzeResponse(
        workflow_name="test",
        total_records=0,
        execution_allowed=True,
        executed=True,
    )
    enforced = enforce_recommendation_only(resp)
    assert enforced.execution_allowed is False
    assert enforced.executed is False
