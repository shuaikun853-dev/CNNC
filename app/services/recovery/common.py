"""Shared recovery primitives kept identical to the pre-split module."""

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


class RecoveryRejected(RuntimeError):
    """The requested recovery does not exactly match recoverable durable state."""


BackendIdentity = tuple[str, str, str, str]


def _acquire_recovery_lease(
    store: DailyAuditStore,
    process_lock: ProcessLock,
    business_date: str,
    owner: str,
    ttl_seconds: float,
) -> LeaseToken:
    process_lock.acquire()
    try:
        return store.acquire_lease(
            business_date,
            owner,
            ttl_seconds,
            allow_unexpired_takeover=True,
            process_lock=process_lock,
        )
    except BaseException:
        process_lock.release()
        raise
