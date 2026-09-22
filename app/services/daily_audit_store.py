"""Compatibility surface for the daily SQLite store.

AuditRunStore owns snapshots/stages/finalization; RemoteExecutionStore owns
remote jobs and crawl results. Both retain the same transaction/fencing kernel.
New callers should depend on the capability they need. Existing callers keep
the original DailyAuditStore API; no database schema or SQL behavior changes.
"""
from app.services.audit_state_kernel import (
    DEFAULT_TENANT_ID,
    DEFAULT_POOL_ID,
    _REQUIRED_FINAL_METRICS,
    _REQUIRED_FORCE_FINAL_METRICS,
    FINALIZATION_INTENT_KEY,
    LeaseUnavailable,
    StaleLease,
    FrozenSnapshotConflict,
    RemoteIntentConflict,
    LeaseToken,
    ProcessLock,
    hashlib,
    json,
    math,
    os,
    sqlite3,
    contextmanager,
    dataclass,
    UTC,
    date,
    datetime,
    timedelta,
    Path,
    time_ns,
    Any,
    Iterator,
    Mapping,
    Sequence,
    CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES,
    is_placeholder_code,
    valid_page_price_result,
)
from app.services.audit_run_store import AuditRunStore
from app.services.remote_execution_store import RemoteExecutionStore


class DailyAuditStore(AuditRunStore, RemoteExecutionStore):
    """Backwards-compatible combined view over one database and lease model."""
