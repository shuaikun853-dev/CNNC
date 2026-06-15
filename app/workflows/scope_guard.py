from app.schemas.workflow import WorkflowScope


class ScopeGuardResult:
    __slots__ = ("allowed", "error_code", "message")

    def __init__(self, allowed: bool, error_code: str | None = None, message: str = ""):
        self.allowed = allowed
        self.error_code = error_code
        self.message = message


CNNC_SCOPE = WorkflowScope(project="CNNC", tenant_id=188605, commodity_pool_id=222)

OUT_OF_SCOPE_MSG = "workflow 仅允许处理中核 CNNC / tenant_id=188605 / commodity_pool_id=222 范围内的数据"


def validate_request_scope(scope: WorkflowScope) -> ScopeGuardResult:
    if scope.project != "CNNC":
        return ScopeGuardResult(False, "OUT_OF_SCOPE", OUT_OF_SCOPE_MSG)
    if scope.tenant_id != 188605:
        return ScopeGuardResult(False, "OUT_OF_SCOPE", OUT_OF_SCOPE_MSG)
    if scope.commodity_pool_id != 222:
        return ScopeGuardResult(False, "OUT_OF_SCOPE", OUT_OF_SCOPE_MSG)
    return ScopeGuardResult(True)


def validate_records_scope(
    request_scope: WorkflowScope,
    records: list[dict],
) -> ScopeGuardResult:
    if not records:
        return ScopeGuardResult(True)
    for i, record in enumerate(records):
        scope_data = record.get("scope")
        if scope_data is None:
            return ScopeGuardResult(
                False,
                "OUT_OF_SCOPE",
                f"记录 {i} 缺少 scope 字段",
            )
        try:
            record_scope = WorkflowScope(**scope_data)
        except Exception:
            return ScopeGuardResult(
                False,
                "OUT_OF_SCOPE",
                f"记录 {i} scope 格式不合法",
            )
        if (
            record_scope.project != request_scope.project
            or record_scope.tenant_id != request_scope.tenant_id
            or record_scope.commodity_pool_id != request_scope.commodity_pool_id
        ):
            return ScopeGuardResult(
                False,
                "OUT_OF_SCOPE",
                f"记录 {i} scope 与请求级 scope 不一致",
            )
    return ScopeGuardResult(True)
