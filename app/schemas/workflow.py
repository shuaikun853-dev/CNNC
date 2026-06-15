from enum import Enum
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowScope(StrictModel):
    project: str = Field(..., pattern=r"^CNNC$")
    tenant_id: int = Field(..., ge=188605, le=188605)
    commodity_pool_id: int = Field(..., ge=222, le=222)


class WorkflowAction(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    WAIT_CRAWLER = "WAIT_CRAWLER"
    SUPPLEMENT_SAME_PRODUCT = "SUPPLEMENT_SAME_PRODUCT"
    DO_NOT_PUSH = "DO_NOT_PUSH"
    MANUAL_CHECK = "MANUAL_CHECK"
    FEEDBACK_CUSTOMER = "FEEDBACK_CUSTOMER"
    READY_TO_PUSH = "READY_TO_PUSH"
    IMPORT_CUSTOMER_LINKS = "IMPORT_CUSTOMER_LINKS"
    UNBIND_WRONG_RELATIONS = "UNBIND_WRONG_RELATIONS"
    RECALCULATE_INDEX = "RECALCULATE_INDEX"
    PUSH_INDEX = "PUSH_INDEX"
    ASK_CUSTOMER_RECHECK = "ASK_CUSTOMER_RECHECK"
    MANUAL_VERIFY = "MANUAL_VERIFY"
    FORWARD_TO_WORK01 = "FORWARD_TO_WORK01"
    NO_ACTION = "NO_ACTION"
    WAIT_COLLECTION = "WAIT_COLLECTION"
    RETRY_PUSH = "RETRY_PUSH"
    CONFIRM = "CONFIRM"
    ALERT = "ALERT"
    EXEMPTION = "EXEMPTION"
    QUERY = "QUERY"
    EXPORT = "EXPORT"


class ExportFormat(str, Enum):
    XLSX = "xlsx"
    CSV = "csv"
    MD = "md"
    TXT = "txt"


class ExportFileInfo(StrictModel):
    filename: str
    format: ExportFormat
    sheet_name: str | None = None
    record_count: int = 0


class WorkflowRecordResult(StrictModel):
    standard_commodity_code: str = ""
    primary_action: WorkflowAction
    suggested_actions: list[WorkflowAction] = []
    action: WorkflowAction | None = None
    reason: str
    pushable: bool = False
    ready_to_push: bool = False
    needs_human_approval: bool = True
    execution_allowed: bool = False
    executed: bool = False
    rule_id: str = ""
    evidence: list[str] = []


class WorkflowAnalyzeResponse(StrictModel):
    workflow_name: str
    total_records: int
    summary: dict[str, int | str | float | bool] = {}
    record_results: list[WorkflowRecordResult] = []
    export_files: list[ExportFileInfo] = []
    execution_allowed: bool = False
    executed: bool = False


class WorkflowRouteRequest(StrictModel):
    scope: WorkflowScope
    question: str
    file_hint: str | None = None


class WorkflowRouteResponse(StrictModel):
    workflow_name: str
    confidence: float
    alternative_workflows: list[str] = []
    required_inputs: list[str] = []
    optional_inputs: list[str] = []
    execution_allowed: bool = False
    executed: bool = False


class SameProductCandidate(StrictModel):
    link: str
    mall_name: str = ""
    crawl_price: Decimal | None = None
    page_actual_price: Decimal | None = None
    comment_count: int | None = None
    same_product_type: str = ""
    is_link_valid: bool = True
    is_product_on_shelf: bool = True
    is_same_product: bool | None = None


class SameProductRecord(StrictModel):
    scope: WorkflowScope
    standard_commodity_code: str
    variant_code: str = ""
    sars_monitor_code: str = ""
    commodity_name: str = ""
    supplier_product_name: str = ""
    mall_recommended_name: str = ""
    current_status: str = ""
    reference_price: Decimal | None = None
    candidates: list[SameProductCandidate] = []


class CustomerLinkRecord(StrictModel):
    url: str
    mall_name: str | None = None
    product_name: str | None = None
    price: Decimal | None = None
    page_actual_price: Decimal | None = None
    is_valid_link: bool = False
    is_same_product: bool = False
    comment_count: int = 0
    issue: str | None = None


class PriceFeedbackRecord(StrictModel):
    scope: WorkflowScope
    standard_commodity_code: str = ""
    variant_code: str = ""
    sars_monitor_code: str = ""
    document_id: str = ""
    commodity_name: str = ""
    feedback_source: str = ""
    feedback_type: str = ""
    feedback_detail: str = ""
    customer_links: list[CustomerLinkRecord] = []
    current_same_products: list[SameProductRecord] = []
    current_index_price: Decimal | None = None
    customer_expected_price: Decimal | None = None
    actual_market_price: Decimal | None = None
    need_code_mapping: bool = False
    mapped_sars_monitor_code: str = ""
    priority: str = "normal"
    current_status: str = ""


class WorkflowAnalyzeRequest(StrictModel):
    scope: WorkflowScope
    records: list[dict]
    export_format: ExportFormat = ExportFormat.XLSX


class WorkflowErrorResponse(StrictModel):
    error_code: str
    message: str
    detail: str | None = None
