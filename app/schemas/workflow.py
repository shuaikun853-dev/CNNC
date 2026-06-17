from enum import Enum
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class ManualOperationNode(StrictModel):
    step: int
    title: str
    description: str
    prerequisite: str = ""
    expected_outcome: str = ""
    safety_notice: str = ""
    related_action: WorkflowAction | None = None
    is_executable_by_agent: bool = False
    execution_allowed: bool = False
    executed: bool = False


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
    manual_operation_nodes: list[ManualOperationNode] = Field(default_factory=list)


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
    is_same_product: bool | None = False
    comment_count: int = 0
    issue: str | None = None


class BrandInfo(StrictModel):
    original_brand: str = ""
    cleaned_brand: str = ""
    brand_source: str = ""
    model: str = ""
    specification: str = ""


class SearchTermConfig(StrictModel):
    full_search_term: str = ""
    concise_search_term: str = ""
    fallback_search_term: str = ""
    primary_term: str = ""


class CollectionConfig(StrictModel):
    data_source: str = ""
    recall_count: int = 60
    recall_threshold: int = 60
    suggest_enable_platform_model_matching: bool = True
    mall_scope: str = ""


class ImportTemplateField(StrictModel):
    standard_commodity_code: str = ""
    commodity_name: str = ""
    document_id: str = ""
    internet_title: str = ""
    url: str = ""
    sku_id: str = ""
    supplier_code: str = ""
    supplier_name: str = ""
    shop_name: str = ""
    price: Decimal | None = None
    comment_count: int = 0


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
    feedback_date: str = ""
    feedback_reason: str = ""
    feedback_price: Decimal | None = None
    code_mapping_status: str = ""
    customer_reply_draft: str = ""
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
    records: list[dict] = Field(..., min_length=1)
    export_format: ExportFormat = ExportFormat.XLSX


class SameProductAnalyzeRequest(StrictModel):
    scope: WorkflowScope
    records: list[SameProductRecord] = Field(..., min_length=1)
    export_format: ExportFormat = ExportFormat.XLSX


class PriceFeedbackAnalyzeRequest(StrictModel):
    scope: WorkflowScope
    records: list[PriceFeedbackRecord] = Field(..., min_length=1)
    export_format: ExportFormat = ExportFormat.XLSX


class SupplementSameProductRecord(StrictModel):
    scope: WorkflowScope
    sars_monitor_code: str = ""
    standard_commodity_code: str = ""
    variant_code: str = ""
    commodity_name: str = ""
    current_same_count: int = 0
    current_valid_same_count: int = 0
    current_same_products: list[SameProductCandidate] = Field(default_factory=list)
    reference_price: Decimal | None = None
    current_index_price: Decimal | None = None
    brand_info: BrandInfo | None = None
    existing_mall_codes: list[str] = Field(default_factory=list)
    need_code_mapping: bool = False
    mapped_sars_monitor_code: str = ""
    current_status: str = ""


class SupplementSameProductAnalyzeRequest(StrictModel):
    scope: WorkflowScope
    records: list[SupplementSameProductRecord] = Field(..., min_length=1)
    export_format: ExportFormat | None = None


class SaasSameProductApprovalItem(StrictModel):
    vCommodityId: str = ""
    queryStr: str = ""
    syncState: str = ""
    skuId: str = ""
    skuName: str = ""
    modelAuditResult: str = ""
    isMatch: str = ""
    vendorName: str = ""
    shopName: str = ""
    price: Decimal | None = None
    commentNum: str = ""
    extLink: str = ""
    createTime: str = ""
    updateTime: str = ""
    isOperate: int | None = None
    pageActualPrice: Decimal | None = None
    page_actual_price: Decimal | None = None

    @field_validator("price", "pageActualPrice", "page_actual_price", mode="before")
    @classmethod
    def _empty_price_to_none(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value


class SaasSameProductApprovalAuditRequest(StrictModel):
    scope: WorkflowScope
    records: list[SaasSameProductApprovalItem] = Field(..., min_length=1)


class SaasSameProductApprovalAuditResult(StrictModel):
    vCommodityId: str = ""
    skuId: str = ""
    extLink: str = ""
    queryStr: str = ""
    skuName: str = ""
    vendorName: str = ""
    shopName: str = ""
    json_price: Decimal | None = None
    page_actual_price: Decimal | None = None
    title_match_result: str = ""
    sku_link_check: str = ""
    price_check: str = ""
    primary_action: WorkflowAction
    suggested_actions: list[WorkflowAction] = Field(default_factory=list)
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    needs_human_approval: bool = True
    execution_allowed: bool = False
    executed: bool = False


class SaasSameProductApprovalAuditResponse(StrictModel):
    workflow_name: str = "work_01_same_product_approval"
    total_records: int
    summary: dict[str, int | str | float | bool] = Field(default_factory=dict)
    record_results: list[SaasSameProductApprovalAuditResult] = Field(default_factory=list)
    execution_allowed: bool = False
    executed: bool = False


class WorkflowErrorResponse(StrictModel):
    error_code: str
    message: str
    detail: str | None = None
