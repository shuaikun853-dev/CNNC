import os


LOCAL_WORKFLOW_TOKEN_ENV = "LOCAL_WORKFLOW_TOKEN"
WORKFLOW_TOKEN_HEADER = "X-Local-Workflow-Token"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class LocalAccessResult:
    __slots__ = ("allowed", "error_code", "message")

    def __init__(self, allowed: bool, error_code: str | None = None, message: str = ""):
        self.allowed = allowed
        self.error_code = error_code
        self.message = message


def check_local_access(client_host: str | None, token_header: str | None) -> LocalAccessResult:
    if client_host and client_host not in LOOPBACK_HOSTS:
        return LocalAccessResult(False, "LOCAL_ACCESS_DENIED", f"workflow API 仅允许本地访问，当前来源: {client_host}")

    expected_token = os.environ.get(LOCAL_WORKFLOW_TOKEN_ENV)
    if not expected_token:
        return LocalAccessResult(
            False,
            "LOCAL_WORKFLOW_TOKEN_REQUIRED",
            "环境变量 LOCAL_WORKFLOW_TOKEN 未配置，workflow API 不可用",
        )

    if not token_header:
        return LocalAccessResult(
            False,
            "INVALID_LOCAL_WORKFLOW_TOKEN",
            "请求头 X-Local-Workflow-Token 缺失",
        )

    if token_header != expected_token:
        return LocalAccessResult(
            False,
            "INVALID_LOCAL_WORKFLOW_TOKEN",
            "X-Local-Workflow-Token 不匹配",
        )

    return LocalAccessResult(True)
