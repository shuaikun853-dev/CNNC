"""Explicit, narrowly scoped recovery operations for one durable audit run."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.services.daily_audit_store import DailyAuditStore, LeaseToken, ProcessLock
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    ExportNoPriceProbe,
    HarvestStatus,
    PriorityStatus,
    REMOTE_NOT_MATERIALIZED_DIAGNOSTICS,
    REMOTE_NOT_MATERIALIZED_LEGACY_DIAGNOSTIC,
    is_retryable_priority_error_diagnostic,
)
from app.services.saas_audit_core import model_fingerprint
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
from app.services.recovery.wait_export import (
    WAIT_EXPORT_MANUAL_DIAGNOSTIC,
    WAIT_EXPORT_RECOVERY_ACCEPTED_ERRORS,
    WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC,
    run_remote_wait_export_recovery,
)
from app.services.recovery.priority import (
    ZERO_ROWS_PRIORITY_RECOVERY_DIAGNOSTICS,
    run_existing_remote_priority_recovery,
    run_existing_remote_priority_zero_rows_recovery,
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
from app.services.recovery.validation import (
    validate_linked_crawls_against_remote_payload,
)
from app.services.recovery.common import (
    BackendIdentity,
    RecoveryRejected,
    _acquire_recovery_lease,
)


# Precisely classified REMOTE_NOT_MATERIALIZED evidence plus the bare legacy
# type name persisted before classification existed. The bare name is admitted
# only because the zero-rows recovery re-derives the classification from a live
# probe of the exact task before any durable state transition.
# job with the RUNNING diagnostic; the wait-export recovery exists exactly to
# give such genuinely still-running remotes another bounded wait, so both
# diagnostics it can itself persist stay admissible entry points.
# workbook: the remote task finished and the export endpoint itself answered
# with the exact no-price response and a zero count.
