"""Read-only Zhaoshang task-progress capability.

Exact task lookup + durable-identity verification + progress query for one
remote job.  This module is strictly read-only: no disable/enable/export/add
call ever leaves it.  A business-failed progress response is NOT an
exception: the result returns ``progress=None`` so dashboard callers can
classify the state as REMOTE_UNKNOWN.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from app.services.zhaoshang_remote_task import (
    LegacyZhaoshangAdapterError,
    RemoteTaskIdentityConflictError,
    remote_business_response_ok,
    remote_task_is_enabled,
)


def _text(value: object) -> str:
    return str(value or "").strip()


def query_task_progress(
    session,
    tool,
    *,
    task_name: str,
    mall_key: str,
    task_id: str,
    task_number: str,
    deadline: float,
    monotonic: Callable[[], float],
) -> Mapping[str, Any]:
    """Exact lookup + identity verification + progress query (read-only)."""
    mall_code = tool.priority_mall_code_for_key(mall_key)
    resolved_task_name, _batch_number = tool.build_browser_task_identifiers(
        task_name, mall_key
    )
    # Identity verification boundary (same proof rules as the running
    # probe): the external tool's exact-task lookup RuntimeError sites
    # and get_task_identity's 商城标识不一致 marker are PROVEN
    # conflicts with the durable remote identity; malformed payloads
    # stay untyped so they keep counting transient.
    try:
        task = tool.check_existing_task(session, resolved_task_name, mall_code)
    except RuntimeError as exc:
        raise RemoteTaskIdentityConflictError(str(exc)) from exc
    if task is None:
        raise LegacyZhaoshangAdapterError(
            "progress query found no exact existing task"
        )
    try:
        live_task_id, live_task_number, _mall_logo = tool.get_task_identity(task, mall_code)
    except RuntimeError as exc:
        if "商城标识不一致" in str(exc):
            raise RemoteTaskIdentityConflictError(str(exc)) from exc
        raise
    if str(live_task_id) != str(task_id) or str(live_task_number) != str(task_number):
        raise RemoteTaskIdentityConflictError(
            "live task identity does not match the durable remote identity"
        )
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("progress query deadline exhausted before progress query")
    # The progress endpoint keys on the task record's batchNumber, not
    # the RW- taskNumber; mirror run_auto_task's identifier choice.
    batch_number = _text(task.get("batchNumber")) or resolved_task_name
    progress_json = tool.query_task_progress(session, batch_number)
    # Business-response gate (same rule as the probe path): a business-
    # failed progress response donates no counter evidence, so it returns
    # progress=None (REMOTE_UNKNOWN upstream) instead of raising; the raw
    # rspDesc is still surfaced for the dashboard.
    rsp_desc = _text(progress_json.get("rspDesc"))
    progress = (
        dict(tool.parse_task_progress_desc(rsp_desc))
        if remote_business_response_ok(progress_json)
        else None
    )
    return {
        "task_id": str(live_task_id),
        "task_number": str(live_task_number),
        "batch_number": batch_number,
        "progress": progress,
        "rsp_desc": rsp_desc,
        "enabled": remote_task_is_enabled(task),
        "remote_calls": 2,
    }
