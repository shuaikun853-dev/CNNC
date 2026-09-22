"""Narrow create-only / wait-export-only seam for the legacy Zhaoshang tool."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib import error
from urllib.parse import parse_qsl, urlparse

from openpyxl import Workbook, load_workbook

try:
    from pymysql.constants import CR as PyMySQLClientError
    from pymysql.constants import ER as PyMySQLServerError
    from pymysql.err import InterfaceError as PyMySQLInterfaceError
    from pymysql.err import OperationalError as PyMySQLOperationalError
except ImportError:  # The legacy priority backend is optional outside production.
    _PYMYSQL_INTERFACE_PRIORITY_ERRORS: tuple[type[BaseException], ...] = ()
    _PYMYSQL_OPERATIONAL_PRIORITY_ERRORS: tuple[type[BaseException], ...] = ()
    _RETRYABLE_PYMYSQL_OPERATIONAL_CODES: frozenset[int] = frozenset()
else:
    _PYMYSQL_INTERFACE_PRIORITY_ERRORS = (PyMySQLInterfaceError,)
    _PYMYSQL_OPERATIONAL_PRIORITY_ERRORS = (PyMySQLOperationalError,)
    _RETRYABLE_PYMYSQL_OPERATIONAL_CODES = frozenset(
        {
            PyMySQLServerError.CON_COUNT_ERROR,
            PyMySQLServerError.SERVER_SHUTDOWN,
            PyMySQLServerError.TOO_MANY_USER_CONNECTIONS,
            PyMySQLServerError.LOCK_WAIT_TIMEOUT,
            PyMySQLServerError.LOCK_TABLE_FULL,
            PyMySQLServerError.LOCK_DEADLOCK,
            PyMySQLClientError.CR_CONNECTION_ERROR,
            PyMySQLClientError.CR_CONN_HOST_ERROR,
            PyMySQLClientError.CR_IPSOCK_ERROR,
            PyMySQLClientError.CR_SERVER_GONE_ERROR,
            PyMySQLClientError.CR_SERVER_LOST,
            PyMySQLClientError.CR_SERVER_LOST_EXTENDED,
        }
    )

# Re-exports: these names moved to zhaoshang_remote_task (2026-09 Zhaoshang
# boundary migration); kept importable from here for existing callers/tests.
from app.services.zhaoshang_remote_task import (
    LegacyPreSendFailure,
    LegacyProbeEvidenceError,
    LegacyZhaoshangAdapterError,
    PriorityEvidenceConflictError,
    RemoteTaskIdentityConflictError,
    SubmissionEvidenceConflictError,
    TemplateConflictError,
    remote_task_is_enabled,
)
from app.services.zhaoshang_result_adapter import (
    read_zhaoshang_result_xlsx,
    read_zhaoshang_result_xlsx_by_identity,
)
from app.services.zhaoshang_task_progress import query_task_progress
from app.services.saas_audit_core import parse_decimal
from app.integrations.auth_manager import AuthManager
from app.integrations.curl_utils import internal_subprocess_env
from app.integrations.zhaoshang_tool_config import load_zhaoshang_db_env
from app.integrations.zhaoshang_mall_mapping import (
    ZhaoshangMallMapping,
    load_zhaoshang_mall_mappings,
)


EXTERNAL_TOOL_PATH = Path(
    r"E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py"
)
DEFAULT_DB_ENV_PATH = Path(__file__).resolve().parents[2] / "data" / "ZHAOSHANG_DB.env"
TEMPLATE_HEADERS = ("商品编码", "url")
RESULT_URL_HEADERS = frozenset({"原始链接", "商品详情地址", "url", "URL", "商品链接", "链接", "商品URL"})
RESULT_CODE_HEADERS = frozenset({
    "商品编码",
    "商品最小品类单元Id",
    "商品最小品类单元id",
    "skuId",
    "sku_id",
    "code",
})
RESULT_PAGE_SKU_HEADERS = frozenset({"商品最小品类单元Id", "商品最小品类单元id"})
RESULT_TITLE_HEADERS = frozenset({"商品标题/名称", "商品标题", "商品名称", "title", "product_title"})
RESULT_COMMENT_HEADERS = frozenset({"评论", "评论数", "销量/评论数", "comment", "comment_count"})
RESULT_EXECUTION_HEADERS = frozenset({"执行编号", "executionNumber", "execution_number"})
RESULT_BATCH_HEADERS = frozenset({"批次号", "batchNumber", "batch_number", "BATCH_NUMBER"})
RESULT_MALL_HEADERS = frozenset({"商城标识", "mall", "mallLogo", "mall_key", "MALL_LOGO"})


def _parse_export_comment_count(value: Any) -> int | None:
    raw = _text(value).replace(",", "").strip()
    if not raw or raw in {"未识别", "N/A", "NA", "None", "null"}:
        return None
    parsed = parse_decimal(raw)
    if parsed is None or parsed < 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


RETRYABLE_PRIORITY_ERROR_DIAGNOSTICS = frozenset(
    {"TimeoutError", "ConnectionError", "InterfaceError"}
    | {
        f"OperationalError[{code}]"
        for code in _RETRYABLE_PYMYSQL_OPERATIONAL_CODES
    }
)


def retryable_priority_error_diagnostic(exc: BaseException) -> str | None:
    """Classify only explicit transient types used by the priority backend."""
    if isinstance(exc, TimeoutError):
        return "TimeoutError"
    if isinstance(exc, ConnectionError):
        return "ConnectionError"
    if _PYMYSQL_INTERFACE_PRIORITY_ERRORS and isinstance(
        exc, _PYMYSQL_INTERFACE_PRIORITY_ERRORS
    ):
        return "InterfaceError"
    if _PYMYSQL_OPERATIONAL_PRIORITY_ERRORS and isinstance(
        exc, _PYMYSQL_OPERATIONAL_PRIORITY_ERRORS
    ):
        code = exc.args[0] if exc.args else None
        if type(code) is int and code in _RETRYABLE_PYMYSQL_OPERATIONAL_CODES:
            return f"OperationalError[{code}]"
    return None


def is_retryable_priority_error_diagnostic(value: object) -> bool:
    """Validate a previously persisted normalized priority diagnostic."""
    return str(value or "").strip() in RETRYABLE_PRIORITY_ERROR_DIAGNOSTICS


# The legacy priority core raises PriorityUpdateZeroRowsError from both
# lookup-exhaustion sites ("rows not materialized yet") and several fail-closed
# verification sites (lock-set drift, UPDATE rowcount, post-commit readback).
# Only the two anchored lookup messages below prove REMOTE_NOT_MATERIALIZED;
# every other raise keeps the fail-closed manual path.
_REMOTE_NOT_MATERIALIZED_TYPE_NAMES = frozenset(
    {
        "PriorityUpdateZeroRowsError",
        "PriorityRowsNotMaterializedError",
        "PriorityRowsIncompleteError",
    }
)
REMOTE_NOT_MATERIALIZED_DIAGNOSTICS = frozenset(
    {"PriorityRowsNotMaterialized", "PriorityRowsIncomplete"}
)
# The pre-classification production build persisted only the bare exception
# type name for every PriorityUpdateZeroRowsError site, so that legacy string
# cannot by itself distinguish zero-rows from verification failures. It is
# admitted only by the zero-rows recovery, which re-derives the classification
# from a live probe before any state transition.
REMOTE_NOT_MATERIALIZED_LEGACY_DIAGNOSTIC = "PriorityUpdateZeroRowsError"


def remote_not_materialized_diagnostic(exc: BaseException) -> str | None:
    """Classify only bounded-lookup exhaustion for the exact task identity.

    Zero rows after the bounded retry loop and an incomplete-but-valid row set
    both mean the remote scheduler has not finished materializing this exact
    taskNumber/mall; identity or UPDATE verification anomalies are intentionally
    excluded and stay fail-closed manual.
    """
    if type(exc).__name__ not in _REMOTE_NOT_MATERIALIZED_TYPE_NAMES:
        return None
    message = str(exc).strip()
    if message.startswith("no matching rows for mall/task_number/create_time window"):
        return "PriorityRowsNotMaterialized"
    if message.startswith("incomplete subtask rows: matched="):
        return "PriorityRowsIncomplete"
    return None


# Only provably transient OS-level spawn failures may auto-retry.  Memory
# pressure (ERROR_NOT_ENOUGH_MEMORY, ERROR_OUTOFMEMORY, ERROR_COMMITMENT_LIMIT)
# and interrupted/again resource conditions can genuinely heal on a later
# identical attempt; every other local spawn error is treated as deterministic.
_TRANSIENT_SPAWN_WINERRORS = frozenset({8, 14, 1455})
_TRANSIENT_SPAWN_ERRNOS = frozenset({errno.ENOMEM, errno.EAGAIN, errno.EINTR})


def _wait_export_startup_failure_is_transient(exc: BaseException) -> bool:
    """Classify a wait-export startup failure against a transient allowlist.

    The production pre-send wrapper preserves the original error as
    ``__cause__``, so classification unwraps it first.  Deterministic local
    spawn problems (missing file/path, permission denied, invalid executable
    image, elevation required) and every unrecognized failure stay manual:
    fail closed.  Auth-refresh deadlines and connection/timeout-class setup
    errors are the known-transient exceptions.
    """
    cause: BaseException = exc
    if isinstance(exc, LegacyPreSendFailure) and isinstance(exc.__cause__, BaseException):
        cause = exc.__cause__
    if isinstance(cause, (TimeoutError, ConnectionError)):
        return True
    if isinstance(cause, error.URLError):
        reason = cause.reason
        return isinstance(reason, (TimeoutError, ConnectionError))
    if isinstance(cause, InterruptedError):
        return True
    if isinstance(cause, OSError):
        winerror = getattr(cause, "winerror", None)
        if winerror is not None:
            return winerror in _TRANSIENT_SPAWN_WINERRORS
        return cause.errno in _TRANSIENT_SPAWN_ERRNOS
    return False


class SubmitStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class HarvestStatus(str, Enum):
    EXPORTED = "EXPORTED"
    RUNNING = "RUNNING"
    MANUAL = "MANUAL"
    FAILED = "FAILED"


class ReconcileStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    MANUAL = "MANUAL"


class PriorityStatus(str, Enum):
    UPDATED = "PRIORITY_UPDATED"
    ALREADY_HIGHEST = "PRIORITY_ALREADY_HIGHEST"
    UNCONFIRMED = "PRIORITY_UNCONFIRMED"


class RestartStatus(str, Enum):
    RESTARTED = "RESTARTED"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    NOT_RUNNING = "NOT_RUNNING"
    EXTERNALLY_DISABLED = "EXTERNALLY_DISABLED"
    NO_LONGER_RUNNING = "NO_LONGER_RUNNING"
    PRE_WRITE_FAILED = "PRE_WRITE_FAILED"
    DISABLE_UNKNOWN = "DISABLE_UNKNOWN"
    DISABLE_UNCONFIRMED = "DISABLE_UNCONFIRMED"
    ENABLE_UNKNOWN = "ENABLE_UNKNOWN"
    ENABLE_UNCONFIRMED = "ENABLE_UNCONFIRMED"
    # Stop-only outcome: the single exact disable was confirmed and no enable
    # follows (dashboard manual-ops stop seam).
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class CrawlRequest:
    mall_key: str
    mall_source_id: str
    mall_name: str
    sku_id: str
    url: str
    backend_version: str

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (self.mall_key, self.mall_source_id, self.sku_id, self.url.strip(), self.backend_version)


@dataclass(frozen=True)
class PreparedLegacyJob:
    business_date: str
    mall_key: str
    mall_source_id: str
    mall_name: str
    task_name: str
    template_path: Path
    result_path: Path
    batch_state_path: Path
    requests: tuple[CrawlRequest, ...]
    logical_job_key: str
    payload_sha256: str


@dataclass(frozen=True)
class LegacySubmitOutcome:
    status: SubmitStatus
    task_id: str | None = None
    task_number: str | None = None
    priority_evidence: str | None = None
    diagnostic: str = ""


@dataclass(frozen=True)
class LegacyHarvestOutcome:
    status: HarvestStatus
    result_map: Mapping[str, Mapping[str, Any]] | None = None
    # URL-only maps remain for legacy callers. Incremental consumers must use
    # this source identity to avoid merging two SKUs with one exact URL.
    result_by_identity: Mapping[tuple[str, str], Mapping[str, Any]] | None = None
    reused_existing: bool = False
    diagnostic: str = ""
    validation_summary: Mapping[str, int] | None = None
    no_result_identities: frozenset[tuple[str, str]] = frozenset()
    manual_identities: Mapping[tuple[str, str], str] | None = None


@dataclass(frozen=True)
class PartialExactHarvestOutcome:
    exact_matches: Mapping[tuple[str, str], Mapping[str, Any]]
    exact_absent: frozenset[tuple[str, str]]
    ambiguous: Mapping[tuple[str, str], str]
    validation_summary: Mapping[str, int]


@dataclass(frozen=True)
class LegacyReconcileOutcome:
    status: ReconcileStatus
    task_id: str | None = None
    task_number: str | None = None
    diagnostic: str = ""


@dataclass(frozen=True)
class LegacyPriorityOutcome:
    status: PriorityStatus
    weight_type: int | None = None
    diagnostic: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class ExportNoPriceProbe:
    """Read-only live evidence that one finished task exports no price rows."""

    task_status_rsp_desc: str
    progress: Mapping[str, int]
    export_rsp_desc: str
    export_count: int
    remote_calls: int


@dataclass(frozen=True)
class RunningTaskProbe:
    """Read-only live evidence for one exact still-RUNNING remote task."""

    task_id: str
    task_number: str
    mall: str
    enabled: bool | None
    task_status_rsp_desc: str
    progress: Mapping[str, int]
    export_had_result: bool
    export_count: int | None
    export_rsp_desc: str
    remote_calls: int


@dataclass(frozen=True)
class LegacyRestartOutcome:
    """Outcome of the single exact disable->enable restart protocol."""

    status: RestartStatus
    disable_confirmed: bool = False
    enable_confirmed: bool = False
    observed_enabled: bool | None = None
    disable_response: Mapping[str, Any] | None = None
    enable_response: Mapping[str, Any] | None = None
    diagnostic: str = ""


class _BoundedReadOnlySession:
    """Clamp legacy helper HTTP calls to the tick's remaining reconcile budget."""

    def __init__(
        self,
        session: Any,
        timeout_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        deadline: float | None = None,
    ) -> None:
        self._session = session
        self._monotonic = monotonic
        self._deadline = (
            float(deadline)
            if deadline is not None
            else self._monotonic() + float(timeout_seconds)
        )

    def _bounded_timeout(self, requested: Any) -> float:
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("legacy read-only reconciliation deadline is exhausted")
        try:
            requested_timeout = float(requested)
        except (TypeError, ValueError):
            requested_timeout = remaining
        return min(remaining, max(requested_timeout, 0.001))

    def post(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["timeout"] = self._bounded_timeout(kwargs.get("timeout"))
        return self._session.post(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["timeout"] = self._bounded_timeout(kwargs.get("timeout"))
        return self._session.get(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def _text(value: object) -> str:
    return str(value or "").strip()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _temporary_environment(values: Mapping[str, str]):
    """Expose state-backed auth only while constructing the legacy session."""
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update({key: str(value) for key, value in values.items()})
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


_ZHAOSHANG_ENV_LOCK = threading.RLock()


@contextmanager
def _locked_temporary_environment(values: Mapping[str, str]):
    """Serialize the whole process-env mutation window across threads.

    ``_temporary_environment`` mutates ``os.environ`` (process-global), so two
    overlapping callers cross-contaminate auth or leave residue.  Every
    zhaoshang seam that keeps the temporary env alive across downstream calls
    (the priority DB connection constructor reads ZHAOSHANG_DB_* dynamically)
    must hold this lock for the entire window.  HTTP helpers never re-read the
    environment (auth is copied into ``session.headers`` by ``build_session``),
    so session-based seams use ``_locked_session_construction`` instead and
    keep their network calls concurrent.
    """
    with _ZHAOSHANG_ENV_LOCK:
        with _temporary_environment(values):
            yield


@contextmanager
def _locked_session_construction(session_environment, build):
    """Lock only env-injection + session construction; yield the raw session.

    The legacy constructor copies BLADE_AUTH/COOKIE into the session headers
    and never re-reads the environment afterwards, so serializing the
    construction window removes the os.environ race while the returned
    session's HTTP calls stay fully concurrent.
    """
    with _ZHAOSHANG_ENV_LOCK:
        with _temporary_environment(session_environment):
            yield build()


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    host = _text(host).lower().lstrip(".")
    return bool(host) and any(
        host == str(domain).lower().lstrip(".")
        or host.endswith("." + str(domain).lower().lstrip("."))
        for domain in domains
        if str(domain).strip()
    )
