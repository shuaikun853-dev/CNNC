"""Fail-closed fan-out gate between one immutable remote payload and linked crawls."""

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
from app.services.recovery.common import BackendIdentity, RecoveryRejected



def validate_linked_crawls_against_remote_payload(
    *,
    operation: str,
    requests: Sequence[CrawlRequest],
    crawls: Sequence[Mapping[str, Any]],
    frozen_sources: set[str],
    remote_job_id: int,
    run_id: int,
) -> dict[BackendIdentity, list[Mapping[str, Any]]]:
    """Fail-closed source fan-out gate between one immutable payload and crawls.

    The durable crawl identity carries ``source_id`` while the immutable legacy
    payload holds one request per backend identity, so one submitted backend
    request may legitimately fan out to several frozen source documents: the
    payload identity counter may not exceed the linked counter.  Every weaker
    shape -- a payload identity missing from the linked set, a linked backend
    identity the payload never contained, a duplicated full durable identity, a
    source outside the frozen snapshot, or linkage to another job or run --
    stays rejected.
    """
    payload_identities: dict[BackendIdentity, CrawlRequest] = {}
    payload_result_keys: dict[tuple[str, str], BackendIdentity] = {}
    for request in requests:
        backend_key: BackendIdentity = (
            request.mall_key,
            request.sku_id,
            request.url.strip(),
            request.backend_version,
        )
        if backend_key in payload_identities:
            raise RecoveryRejected(
                f"{operation}: immutable remote payload has duplicate backend identities"
            )
        result_key = (request.sku_id, request.url.strip())
        if result_key in payload_result_keys:
            raise RecoveryRejected(
                f"{operation}: remote payload result identity is backend-ambiguous"
            )
        payload_identities[backend_key] = request
        payload_result_keys[result_key] = backend_key

    linked_counts: Counter[BackendIdentity] = Counter()
    full_identities: set[tuple[str, str, str, str, str]] = set()
    groups: dict[BackendIdentity, list[Mapping[str, Any]]] = {}
    for crawl in crawls:
        source_id = str(crawl.get("source_id") or "").strip()
        backend_key = (
            str(crawl["mall_key"]),
            str(crawl["sku_id"]),
            str(crawl["exact_url"]).strip(),
            str(crawl["backend_version"]),
        )
        if not source_id:
            raise RecoveryRejected(
                f"{operation}: linked crawl work has no durable source identity"
            )
        full_identity = (backend_key[0], source_id, backend_key[1], backend_key[2], backend_key[3])
        if full_identity in full_identities:
            raise RecoveryRejected(
                f"{operation}: linked full durable crawl identities are duplicated"
            )
        if backend_key not in payload_identities:
            raise RecoveryRejected(
                f"{operation}: linked crawl identity is outside the immutable payload"
            )
        if source_id not in frozen_sources:
            raise RecoveryRejected(
                f"{operation}: linked crawl work is outside the frozen snapshot"
            )
        if int(crawl.get("remote_job_id") or 0) != int(remote_job_id):
            raise RecoveryRejected(
                f"{operation}: linked crawl work is not bound to the exact remote job"
            )
        if int(crawl.get("run_id") or 0) != int(run_id):
            raise RecoveryRejected(
                f"{operation}: linked crawl work is outside the daily run scope"
            )
        full_identities.add(full_identity)
        linked_counts[backend_key] += 1
        groups.setdefault(backend_key, []).append(crawl)

    missing = Counter(payload_identities.keys()) - linked_counts
    if missing:
        raise RecoveryRejected(
            f"{operation}: immutable payload identities are missing from linked crawl work"
        )
    return groups
