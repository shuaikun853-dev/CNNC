"""Explicit, narrowly scoped recovery operations for one durable audit run.

Compatibility facade: the implementation lives in ``app.services.recovery``.
This module permanently re-exports the frozen public surface so existing
``from app.services.incremental_audit_recovery import X`` imports keep working.
"""

from app.services.recovery.common import (
    BackendIdentity,
    RecoveryRejected,
    _acquire_recovery_lease,
)
from app.services.recovery.harvest import (
    run_existing_remote_harvest,
    run_local_remote_harvest_recovery,
    run_partial_exact_harvest_recovery,
)
from app.services.recovery.model import (
    requeue_model_transient_failures,
    release_model_recovery,
    run_model_recovery,
)
from app.services.recovery.no_result import (
    EXPORT_NO_PRICE_ACCEPTED_ERRORS,
    EXPORT_NO_PRICE_CRAWL_REASON,
    EXPORT_NO_PRICE_NO_RESULT_LIMIT,
    EXPORT_NO_PRICE_RSP_DESC,
    EXPORT_NO_PRICE_TASK_FINISHED_DESC,
    WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC,
    resolve_exported_no_result,
    run_remote_export_no_price_resolution,
)
from app.services.recovery.priority import (
    ZERO_ROWS_PRIORITY_RECOVERY_DIAGNOSTICS,
    run_existing_remote_priority_recovery,
    run_existing_remote_priority_zero_rows_recovery,
)
from app.services.recovery.validation import (
    validate_linked_crawls_against_remote_payload,
)
from app.services.recovery.wait_export import (
    WAIT_EXPORT_MANUAL_DIAGNOSTIC,
    WAIT_EXPORT_RECOVERY_ACCEPTED_ERRORS,
    WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC,
    run_remote_wait_export_recovery,
)
