from abc import ABC, abstractmethod
from typing import Any

from app.schemas.workflow import WorkflowAction, WorkflowAnalyzeResponse, WorkflowRecordResult


class WorkflowBase(ABC):
    workflow_name: str = ""
    display_name: str = ""
    required_inputs: list[str] = []
    optional_inputs: list[str] = []
    possible_actions: list[WorkflowAction] = []

    @abstractmethod
    def analyze(self, records: list[Any]) -> WorkflowAnalyzeResponse:
        ...

    def get_knowledge_refs(self) -> list[str]:
        return []


class SameProductApprovalWorkflow(WorkflowBase):
    workflow_name = "work_01_same_product_approval"
    display_name = "新增监控商品同款审批处理和价格指数同步"
    required_inputs = ["standard_commodity_code"]
    optional_inputs = ["variant_code", "sars_monitor_code", "crawl_price", "page_actual_price", "reference_price"]
    possible_actions = [
        WorkflowAction.APPROVE,
        WorkflowAction.REJECT,
        WorkflowAction.WAIT_CRAWLER,
        WorkflowAction.SUPPLEMENT_SAME_PRODUCT,
        WorkflowAction.DO_NOT_PUSH,
        WorkflowAction.MANUAL_CHECK,
        WorkflowAction.FEEDBACK_CUSTOMER,
        WorkflowAction.READY_TO_PUSH,
    ]

    def analyze(self, records: list[Any]) -> WorkflowAnalyzeResponse:
        results = []
        for rec in records:
            code = rec.get("standard_commodity_code", "") if isinstance(rec, dict) else ""
            results.append(
                WorkflowRecordResult(
                    standard_commodity_code=code,
                    primary_action=WorkflowAction.MANUAL_CHECK,
                    suggested_actions=[WorkflowAction.MANUAL_CHECK],
                    action=WorkflowAction.MANUAL_CHECK,
                    reason="R2 only: work_01 business rules not implemented yet",
                    pushable=False,
                    ready_to_push=False,
                    needs_human_approval=True,
                    execution_allowed=False,
                    executed=False,
                    rule_id="R2_PLACEHOLDER",
                )
            )
        return WorkflowAnalyzeResponse(
            workflow_name=self.workflow_name,
            total_records=len(results),
            summary={"status": "NOT_IMPLEMENTED", "message": "R2 only: workflow base layer created; work_01 rules will be implemented in R3"},
            record_results=results,
            execution_allowed=False,
            executed=False,
        )

    def get_knowledge_refs(self) -> list[str]:
        return [
            "cnnc_work_01_same_product_approval.md",
            "cnnc_base_scope.md",
            "cnnc_forbidden_actions.md",
        ]


class PriceIndexFeedbackWorkflow(WorkflowBase):
    workflow_name = "work_02_price_index_feedback"
    display_name = "价格指数勘误和运营反馈问题处理"
    required_inputs = ["standard_commodity_code"]
    optional_inputs = ["variant_code", "sars_monitor_code", "feedback_type", "feedback_detail"]
    possible_actions = [
        WorkflowAction.IMPORT_CUSTOMER_LINKS,
        WorkflowAction.UNBIND_WRONG_RELATIONS,
        WorkflowAction.RECALCULATE_INDEX,
        WorkflowAction.PUSH_INDEX,
        WorkflowAction.SUPPLEMENT_SAME_PRODUCT,
        WorkflowAction.WAIT_COLLECTION,
        WorkflowAction.ASK_CUSTOMER_RECHECK,
        WorkflowAction.MANUAL_VERIFY,
        WorkflowAction.FORWARD_TO_WORK01,
        WorkflowAction.NO_ACTION,
    ]

    def analyze(self, records: list[Any]) -> WorkflowAnalyzeResponse:
        results = []
        for rec in records:
            code = rec.get("standard_commodity_code", "") if isinstance(rec, dict) else ""
            results.append(
                WorkflowRecordResult(
                    standard_commodity_code=code,
                    primary_action=WorkflowAction.MANUAL_VERIFY,
                    suggested_actions=[WorkflowAction.MANUAL_VERIFY],
                    action=WorkflowAction.MANUAL_VERIFY,
                    reason="R2 only: work_02 business rules not implemented yet",
                    pushable=False,
                    ready_to_push=False,
                    needs_human_approval=True,
                    execution_allowed=False,
                    executed=False,
                    rule_id="R2_PLACEHOLDER",
                )
            )
        return WorkflowAnalyzeResponse(
            workflow_name=self.workflow_name,
            total_records=len(results),
            summary={"status": "NOT_IMPLEMENTED", "message": "R2 only: workflow base layer created; work_02 rules will be implemented in R4"},
            record_results=results,
            execution_allowed=False,
            executed=False,
        )

    def get_knowledge_refs(self) -> list[str]:
        return [
            "cnnc_work_02_price_index_feedback.md",
            "cnnc_sars_import_rules.md",
            "cnnc_base_scope.md",
            "cnnc_forbidden_actions.md",
        ]


WORKFLOW_REGISTRY: dict[str, WorkflowBase] = {
    "work_01_same_product_approval": SameProductApprovalWorkflow(),
    "work_02_price_index_feedback": PriceIndexFeedbackWorkflow(),
}
