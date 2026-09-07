"""Remote-task semantics seed module for the legacy Zhaoshang boundary.

Owns the identity-conflict exception family and the enabled-state reading of
remote task records.  First step of the 2026-09 Zhaoshang boundary migration
out of ``legacy_zhaoshang_adapter``; the adapter re-exports every public name
so existing importers keep working unchanged.  Also owns the Zhaoshang
remote business-response gate (moved in by the 2026-09 review fix r1),
shared by the progress and recovery paths.
"""

from __future__ import annotations

from typing import Any, Mapping


def _text(value: object) -> str:
    return str(value or "").strip()


class LegacyZhaoshangAdapterError(RuntimeError):
    pass


class TemplateConflictError(LegacyZhaoshangAdapterError):
    pass


class PriorityEvidenceConflictError(LegacyZhaoshangAdapterError):
    pass


class SubmissionEvidenceConflictError(LegacyZhaoshangAdapterError):
    pass


class LegacyPreSendFailure(LegacyZhaoshangAdapterError):
    """The legacy subprocess provably never started; no remote POST occurred."""


class LegacyProbeEvidenceError(LegacyZhaoshangAdapterError):
    """A read-only probe completed with no usable execution evidence.

    Transport success (HTTP answered, no exception) does not by itself prove
    anything about the remote task: a business-failed, malformed, or
    contradictory progress payload with no trusted terminal status and no
    actual export result leaves the execution state unprovable, so the probe
    refuses to return it as a healthy result and surfaces this typed failure
    instead.
    """


class RemoteTaskIdentityConflictError(LegacyZhaoshangAdapterError):
    """The live exact task provably conflicts with the durable remote identity.

    Raised only where the conflict is PROVEN (live task id/number mismatch
    against the durable identity, a non-unique exact task lookup, or a mall
    mismatch reported by the external tool).  Malformed payloads, timeouts,
    and transport faults are NOT identity conflicts and stay untyped.
    """


def remote_task_is_enabled(remote: Mapping[str, Any]) -> bool:
    enabled = remote.get("enabled")
    if isinstance(enabled, bool):
        return enabled
    state = _text(
        remote.get("state")
        or remote.get("taskState")
        or remote.get("task_state")
        or remote.get("enableState")
    ).lower()
    return state in {"1", "enabled", "enable", "running", "active"}


# Business-response evidence gate (review round 5): the external tool's own
# write endpoints (auto_crawl_task.py enable/disable) accept a payload only
# when ``success`` is not explicitly False and ``rspCode`` is absent or the
# one proven success code "0000"; absent fields stay admissible because the
# platform legitimately omits them.
PROVEN_BUSINESS_SUCCESS_RSP_CODES: frozenset[str] = frozenset({"0000"})


def remote_business_response_ok(payload: object) -> bool:
    """Mirror the external tool's business-success rule for read evidence.

    Only a business-successful status/progress response may donate evidence
    (counters, terminal marker text); a ``success=false`` or non-``0000``
    ``rspCode`` payload is stripped to its unprovable form by the callers so
    it can never authorize stall proof or a fresh restart write.  Absent
    ``success``/``rspCode`` fields remain admissible.
    """
    if not isinstance(payload, Mapping):
        return False
    if "success" in payload and payload.get("success") is not True:
        return False
    rsp_code = _text(payload.get("rspCode"))
    if rsp_code and rsp_code not in PROVEN_BUSINESS_SUCCESS_RSP_CODES:
        return False
    return True
