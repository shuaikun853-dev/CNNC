"""Customer prewarm lifecycle closeout (WS-A): harvest / reconcile / redispatch.

Three bounded lifecycle stages behind the Issue #10 dispatcher (spec:
``docs/superpowers/specs/2026-09-16-prewarm-lifecycle-closeout-design.md``).
They reuse the EXISTING STEP1 remote-task store and the existing recovery
primitive — no second remote-task store is introduced:

- harvest (A1): ``sku_id LIKE 'zhpq-%'`` bound jobs in ``SUBMITTED``/``RUNNING``/
  ``EXPORTED`` with a remote identity are handed one by one to the existing
  :func:`harvest_existing_customer_prewarm_job` primitive (which owns its own
  ProcessLock + durable lease — this stage therefore runs OUTSIDE the dispatch
  pass, called by the CLI after the pass released every lock).
- reconcile (A2): identity-less ``SUBMIT_UNKNOWN`` prewarm jobs of the leased
  day with a current exact candidate link are rebuilt from durable payload evidence
  (:func:`_reconstruct_prepared_job`) and re-identified through
  ``adapter.reconcile_unknown``.  ``FOUND`` reconciles the identity at
  ``PRIORITY_PENDING`` (then one prioritize confirm; only a PROVEN
  ``weight_type == -1`` moves the job to ``SUBMITTED``).  Conclusive
  ``NOT_FOUND`` bumps ``remote_jobs.recovery_attempts``; identity-conflict
  ``MANUAL`` and inconclusive lookups cannot authorize A3 redispatch.
- redispatch (A3): candidates in ``REMOTE_DISPATCHED``/``REMOTE_REUSED`` whose
  bound job is dead (``MANUAL_REQUIRED`` / ``TERMINAL_FAILED`` /
  ``SUBMIT_UNKNOWN`` with ``recovery_attempts >= threshold``) are re-created
  through the idempotent STEP1 create chain.  The owning day of a bound job is
  taken from the candidate's latest ``prewarm_remote_job_links`` row (the same
  cross-day defense as the dispatch pass) — cross-day dead jobs are re-dispatch
  candidates too and their new job is built under the PASS business date.  The
  redispatch request carries a distinct deterministic ``zhpq-`` placeholder SKU
  (``redispatch-<n>`` salt, ``n`` = the candidate's link rows of the day) so
  the idempotent intent keys cannot collide with the dead job's evidence; the
  per-candidate daily budget is the candidate's ``prewarm_remote_job_links``
  row count of the day (cap :data:`REDISPATCH_DAILY_CAP`).

Cross-day identity-less ``SUBMIT_UNKNOWN`` jobs are deliberately handled by
``customer_prewarm_cross_day_reconcile`` after the dispatch pass has released
today's lock.  That phase acquires each owning day's own lock and lease; this
module's A2 implementation remains today-only.

``plan_lifecycle_actions`` is the strictly read-only planner behind the
plan-only report's ``lifecycle_planned`` summary, including the cross-day
target count.  Every stage is
error-isolated and budget-bounded (``deadline``/``monotonic_value`` pairs,
mirroring the dispatch pass phases).  Canary mode never enters any stage (the
dispatch pass skips Phase 5 entirely and the CLI never calls the harvest
stage), so the canary summary key set stays byte-identical.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.services import customer_prewarm_dispatch as dispatch_service
from app.services.customer_prewarm_discovery import _open_state_db_readonly
from app.services.customer_prewarm_dispatch import (
    BACKEND_VERSION,
    LEASE_TTL_SECONDS,
    SUBMIT_MIN_SECONDS,
    _candidate_mall,
    _confirm_pending_priority_job,
    _reconstruct_prepared_job,
    _record,
    _status_value,
    prewarm_crawl_fingerprint,
    prewarm_placeholder_sku,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_REMOTE_REUSED,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import DailyAuditStore, LeaseToken, LeaseUnavailable, ProcessLock
from app.services.incremental_audit_runtime import runtime_paths

def harvest_existing_customer_prewarm_job(**kwargs: Any) -> dict[str, Any]:
    """Lazy bridge to the recovery primitive.

    Importing the lifecycle planner must stay free of Zhaoshang module access;
    the heavy recovery module is loaded only when a real harvest is attempted.
    The wrapper also preserves the established monkeypatch seam used by tests.
    """
    from app.services.recovery.harvest import (
        harvest_existing_customer_prewarm_job as _harvest_existing_customer_prewarm_job,
    )

    return _harvest_existing_customer_prewarm_job(**kwargs)

from app.services.shared_crawl_cache import PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

# ---- lifecycle budget constants (plan WS-A) ---------------------------------

RECONCILE_DEAD_THRESHOLD = 4
REDISPATCH_DAILY_CAP = 2
HARVEST_PER_PASS = 3
RECONCILE_PER_PASS = 6
HARVEST_PER_JOB_TIMEOUT_SECONDS = 90.0

# Job states that make a candidate's binding dead for redispatch planning.
# ``SUBMIT_UNKNOWN`` is dead only once its recovery attempts hit the threshold.
_DEAD_JOB_STATES = frozenset({"MANUAL_REQUIRED", "TERMINAL_FAILED"})
_RECONCILE_MANUAL_BLOCKER = "prewarm reconcile identity conflict: "

_ZHPQ_SKU_PREFIX = "zhpq-"

_HARVEST_JOBS_SQL = """
SELECT j.id AS job_id, j.remote_identity AS remote_identity
FROM remote_jobs j
JOIN crawl_work c ON c.remote_job_id = j.id
WHERE c.sku_id LIKE 'zhpq-%'
  AND j.state IN ('SUBMITTED', 'RUNNING', 'EXPORTED')
  AND TRIM(COALESCE(j.remote_identity, '')) <> ''
ORDER BY j.id
"""

_RECONCILE_JOBS_SQL = """
SELECT j.id AS job_id, COALESCE(j.recovery_attempts, 0) AS recovery_attempts
FROM remote_jobs j
WHERE j.state = 'SUBMIT_UNKNOWN'
  AND TRIM(COALESCE(j.remote_identity, '')) = ''
  AND EXISTS (
      SELECT 1 FROM crawl_work c
      WHERE c.remote_job_id = j.id AND c.sku_id LIKE 'zhpq-%'
  )
ORDER BY j.id
"""

_CRAWL_STATES_FOR_JOB_SQL = """
SELECT c.state AS state
FROM crawl_work c
WHERE c.remote_job_id = ?
ORDER BY c.id
"""

# ---- WS-B: failed customer links projected into the final workbook -----------

# Cross-day prewarm store lives at the runtime root (next to the per-day
# directories), the same placement the scan CLI and the tick intake use.
PREWARM_STORE_FILENAME = "customer_prewarm.sqlite3"

# Final-workbook failure reasons (spec B / user ruling 2026-09-16: NO_RESULT is
# a failure; texts are the business-visible wording of the 结果 column).
FAILED_LINK_REASON_SUBMIT_UNKNOWN = "提交超时无身份"
FAILED_LINK_REASON_NO_RESULT = "远端无匹配价格行"
FAILED_LINK_REASON_TERMINAL = "终端失败"
FAILED_LINK_REASON_REDISPATCH_EXHAUSTED = "重排超限"
FAILED_LINK_REASON_REDISPATCH_UNKNOWN = "重排提交结果不明，需人工核对"


def redispatch_placeholder_sku(mall_key: str, url: str, attempt_index: int) -> str:
    """Deterministic ``zhpq-`` placeholder SKU for the attempt-th redispatch.

    The salt keeps the request identity (hence the idempotent intent's logical
    key) distinct from the dead job's immutable payload evidence, so the create
    chain builds a genuinely NEW remote job instead of idempotently returning
    the dead one.  The ``zhpq-`` prefix is preserved so every later harvest /
    reconcile / redispatch query still recognizes the job as prewarm work.
    """
    digest = hashlib.sha256(
        f"{mall_key}\n{url}\nredispatch-{int(attempt_index)}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{_ZHPQ_SKU_PREFIX}{digest}"


def _read_job_row_readonly(state_db_path: str | Path, job_id: int) -> dict[str, Any] | None:
    """``mode=ro`` probe of one ``remote_jobs`` row (never creates the DB)."""
    connection = _open_state_db_readonly(state_db_path)
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT * FROM remote_jobs WHERE id=?", (int(job_id),)
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def _job_is_dead(job_row: Mapping[str, Any], dead_threshold: int) -> bool:
    state = str(job_row.get("state") or "")
    if state == "MANUAL_REQUIRED":
        if str(job_row.get("error_text") or "").startswith(_RECONCILE_MANUAL_BLOCKER):
            return False
        if int(job_row.get("harvest_attempts") or 0):
            return False  # remote export was attempted but could not be proven safe
        if int(job_row.get("post_attempted") or 0) and not str(
            job_row.get("remote_identity") or ""
        ).strip():
            return False
    if state in _DEAD_JOB_STATES:
        return True
    if state == "SUBMIT_UNKNOWN":
        return int(job_row.get("recovery_attempts") or 0) >= dead_threshold
    return False


def _mark_reconcile_manual(
    store: DailyAuditStore, token: LeaseToken, job_id: int, diagnostic: str
) -> None:
    store.mark_remote_manual(
        token,
        job_id,
        _RECONCILE_MANUAL_BLOCKER + (diagnostic.strip() or "remote identity cannot be proven"),
    )


def _owning_day_job(
    prewarm_store: CustomerPrewarmStore,
    candidate_id: int,
    job_id: int,
    business_date: str,
    job_reader: Callable[[int, str], dict[str, Any] | None],
    phase_errors: list[str],
) -> tuple[str, dict[str, Any] | None]:
    """The bound job's row plus the day whose state DB owns it.

    Same cross-day defense as the dispatch pass: the candidate's latest
    linkage row dates the binding — a link on another day means the bare job
    id belongs to THAT day's state DB and must never be resolved against
    today's DB (an unrelated same-numbered job could live there).
    """
    owning_day = business_date
    try:
        link = prewarm_store.remote_job_link_for_candidate(int(candidate_id))
    except Exception as exc:  # noqa: BLE001 - probe failure never breaks the phase
        phase_errors.append(
            f"candidate {candidate_id}: remote job link probe: {type(exc).__name__}: {exc}"
        )
        return owning_day, None
    if link is None or (
        int(link.get("remote_job_id") or 0) != int(job_id)
        or str(link.get("producer") or "") != PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
    ):
        phase_errors.append(f"candidate {candidate_id}: latest remote job link mismatch")
        return owning_day, None
    link_date = str(link.get("business_date") or "")
    try:
        if (
            date.fromisoformat(link_date).isoformat() != link_date
            or date.fromisoformat(link_date) > date.fromisoformat(business_date)
        ):
            raise ValueError("noncanonical or future owning day")
    except ValueError:
        phase_errors.append(f"candidate {candidate_id}: invalid remote job owning day")
        return owning_day, None
    if link_date and link_date != business_date:
        owning_day = link_date
    return owning_day, job_reader(int(job_id), owning_day)


def _has_successful_crawl_for_url_readonly(
    state_path: Path, job_id: int, exact_url: str
) -> bool:
    connection = _open_state_db_readonly(state_path)
    if connection is None:
        return False
    try:
        rows = connection.execute(
            """SELECT result_json FROM crawl_work
               WHERE remote_job_id=? AND exact_url=? AND state='SUCCESS'
                 AND sku_id LIKE 'zhpq-%' AND result_json IS NOT NULL""",
            (job_id, exact_url),
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        try:
            result = json.loads(str(row["result_json"]))
        except (TypeError, ValueError):
            continue
        if isinstance(result, Mapping) and result.get("page_price_status") == "OK":
            return True
    return False


def _collect_redispatch_candidates(
    prewarm_store: CustomerPrewarmStore,
    mappings: Sequence[ZhaoshangMallMapping],
    *,
    business_date: str,
    state_runtime_root: Path,
    job_reader: Callable[[int, str], dict[str, Any] | None],
    dead_threshold: int,
    daily_cap: int,
    phase_errors: list[str],
) -> dict[str, Any]:
    """Dead-job candidates grouped by mall, plus the budget-blocked set.

    Read-only by construction (the caller decides whether the blocked set also
    earns a candidate ``last_error`` note).  ``attempt_index`` carries the
    candidate's link rows of the day so the redispatch SKU is deterministic
    and distinct per attempt.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    blocked: list[dict[str, Any]] = []
    for status in (CACHE_STATUS_REMOTE_DISPATCHED, CACHE_STATUS_REMOTE_REUSED):
        for candidate in prewarm_store.candidates(status):
            candidate_id = int(candidate["id"])
            raw_job_id = candidate.get("remote_job_id")
            if raw_job_id is None:
                continue
            owning_day, job_row = _owning_day_job(
                prewarm_store, candidate_id, int(raw_job_id), business_date, job_reader, phase_errors
            )
            if job_row is None:
                continue
            if not _job_is_dead(job_row, dead_threshold):
                continue
            try:
                prior_intent = prewarm_store.latest_redispatch_intent_for_candidate(candidate_id)
            except Exception as exc:  # noqa: BLE001 - uncertain prior POST forbids create
                phase_errors.append(f"candidate {candidate_id}: redispatch intent probe failed: {type(exc).__name__}: {exc}")
                continue
            if (
                prior_intent is not None
                and str(prior_intent.get("producer") or "") == PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
                and str(prior_intent.get("business_date") or "") != business_date
                and (
                    str(prior_intent.get("business_date") or "") != owning_day
                    or int(prior_intent.get("remote_job_id") or 0) != int(raw_job_id)
                )
            ):
                blocked.append({"candidate_id": candidate_id, "reason": "prior redispatch intent unresolved"})
                phase_errors.append(f"candidate {candidate_id}: prior redispatch intent may have POSTed; create deferred")
                continue
            if _has_successful_crawl_for_url_readonly(
                runtime_paths(owning_day, state_runtime_root).state_path,
                int(raw_job_id),
                str(candidate.get("lookup_url") or candidate.get("original_url") or ""),
            ):
                continue  # cache mirror/absorb owns an already successful URL
            mall = _candidate_mall(candidate, mappings)
            if not any(m.mall_key == mall for m in mappings):
                blocked.append({"candidate_id": candidate_id, "reason": "mall_mapping_missing"})
                continue
            try:
                used = prewarm_store.remote_job_link_count(business_date, candidate_id)
            except Exception as exc:  # noqa: BLE001 - probe failure never breaks the phase
                phase_errors.append(
                    f"candidate {candidate_id}: link count probe: {type(exc).__name__}: {exc}"
                )
                continue
            if used >= daily_cap:
                blocked.append({"candidate_id": candidate_id, "reason": "redispatch budget exhausted"})
                continue
            groups.setdefault(mall, []).append(
                {"candidate": candidate, "attempt_index": used, "owning_day": owning_day}
            )
    return {"groups": groups, "blocked": blocked}


def plan_lifecycle_actions(
    state_db_path: str | Path,
    prewarm_store: CustomerPrewarmStore,
    business_date: str,
    mappings: Sequence[ZhaoshangMallMapping],
    *,
    reconcile_dead_threshold: int = RECONCILE_DEAD_THRESHOLD,
    redispatch_daily_cap: int = REDISPATCH_DAILY_CAP,
    state_runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    """Strictly read-only (``mode=ro``) lifecycle planning for one day.

    Returns ``{"harvest_job_ids", "reconcile_jobs",
    "cross_day_reconcile_jobs", "redispatch_groups", "redispatch_blocked"}``;
    the plan-only report folds this into its
    ``lifecycle_planned`` summary.  Missing state DB / empty stores yield an
    empty plan — never a write.
    """
    state_path = Path(state_db_path)
    harvest_job_ids: list[int] = []
    reconcile_jobs: list[dict[str, Any]] = []
    connection = _open_state_db_readonly(state_path)
    if connection is not None:
        try:
            harvest_job_ids = [
                int(row["job_id"]) for row in connection.execute(_HARVEST_JOBS_SQL).fetchall()
            ]
            for row in connection.execute(_RECONCILE_JOBS_SQL).fetchall():
                attempts = int(row["recovery_attempts"] or 0)
                if attempts < reconcile_dead_threshold:
                    reconcile_jobs.append(
                        {"job_id": int(row["job_id"]), "recovery_attempts": attempts}
                    )
        finally:
            connection.close()

    state_root = (
        Path(state_runtime_root)
        if state_runtime_root is not None
        else state_path.parent.parent
    )

    def job_reader(job_id: int, owning_day: str) -> dict[str, Any] | None:
        return _read_job_row_readonly(runtime_paths(owning_day, state_root).state_path, job_id)

    collected = _collect_redispatch_candidates(
        prewarm_store,
        mappings,
        business_date=business_date,
        state_runtime_root=state_root,
        job_reader=job_reader,
        dead_threshold=reconcile_dead_threshold,
        daily_cap=redispatch_daily_cap,
        phase_errors=[],
    )
    redispatch_groups = {
        mall: [entry["candidate"] for entry in entries]
        for mall, entries in collected["groups"].items()
    }
    from app.services.customer_prewarm_cross_day_reconcile import (
        plan_cross_day_reconcile_jobs,
    )

    cross_day_reconcile_jobs = plan_cross_day_reconcile_jobs(
        prewarm_store,
        current_business_date=business_date,
        state_runtime_root=state_root,
        dead_threshold=reconcile_dead_threshold,
    )
    from app.services.customer_prewarm_cross_day_reconcile import (
        plan_cross_day_harvest_jobs,
    )

    cross_day_harvest_jobs = plan_cross_day_harvest_jobs(
        prewarm_store,
        current_business_date=business_date,
        state_runtime_root=state_root,
    )
    return {
        "harvest_job_ids": harvest_job_ids,
        "reconcile_jobs": reconcile_jobs,
        "cross_day_reconcile_jobs": cross_day_reconcile_jobs,
        "cross_day_harvest_jobs": cross_day_harvest_jobs,
        "redispatch_groups": redispatch_groups,
        "redispatch_blocked": collected["blocked"],
    }


def _crawl_states_for_job_readonly(state_db_path: str | Path, job_id: int) -> list[str]:
    """``mode=ro`` probe: the crawl row states bound to one remote job."""
    connection = _open_state_db_readonly(state_db_path)
    if connection is None:
        return []
    try:
        rows = connection.execute(_CRAWL_STATES_FOR_JOB_SQL, (int(job_id),)).fetchall()
    finally:
        connection.close()
    return [str(row["state"] or "") for row in rows]


def _failed_link_reason(
    *,
    job_row: Mapping[str, Any] | None,
    job_is_dead: bool,
    crawl_states: Sequence[str],
    redispatch_links_used: int,
    daily_cap: int,
) -> str | None:
    """The workbook failure reason for one candidate, or ``None`` when it lives.

    Precedence: an exhausted redispatch budget is the day's final disposition;
    a NO_RESULT crawl row beats the coarser job-state reasons; a dead
    SUBMIT_UNKNOWN job is the submit-timeout wording; every other dead job or
    terminally failed crawl row is 终端失败.
    """
    if job_is_dead and redispatch_links_used >= daily_cap:
        return FAILED_LINK_REASON_REDISPATCH_EXHAUSTED
    if "NO_RESULT" in crawl_states:
        return FAILED_LINK_REASON_NO_RESULT
    if str((job_row or {}).get("state") or "") == "MANUAL_REQUIRED":
        return FAILED_LINK_REASON_TERMINAL
    if job_is_dead:
        if str((job_row or {}).get("state") or "") == "SUBMIT_UNKNOWN":
            return FAILED_LINK_REASON_SUBMIT_UNKNOWN
        return FAILED_LINK_REASON_TERMINAL
    if "TERMINAL_FAILED" in crawl_states:
        return FAILED_LINK_REASON_TERMINAL
    return None


def failed_customer_link_rows(
    business_date: str,
    runtime_root: str | Path,
    mappings: Sequence[ZhaoshangMallMapping],
    *,
    dead_threshold: int = RECONCILE_DEAD_THRESHOLD,
    redispatch_daily_cap: int = REDISPATCH_DAILY_CAP,
) -> list[dict[str, Any]]:
    """Strictly read-only WS-B query: customer links whose crawl failed today.

    Scope (user ruling 2026-09-16): candidates observed on ``business_date``
    (``prewarm_daily_observations`` — the store's authoritative day index) that
    are NOT ``CACHE_HIT`` and carry a failure — a dead bound job
    (``MANUAL_REQUIRED`` / ``TERMINAL_FAILED`` / ``SUBMIT_UNKNOWN`` judged dead
    by ``dead_threshold``), a ``NO_RESULT`` or ``TERMINAL_FAILED`` crawl row on
    the bound job, or an exhausted redispatch budget (the day's
    ``prewarm_remote_job_links`` count at ``redispatch_daily_cap``).  Only the
    two remote statuses can carry a binding, so they are the only candidates
    enumerated.  Rows are deduped by ``(mall, original_url)`` (the store's
    candidate identity) and sorted deterministically.

    Every probe is ``mode=ro`` (missing state DBs / stores yield fewer rows,
    never a write); a candidate whose linkage probes fail is skipped fail-open,
    exactly like the lifecycle planning stages.  Returns per row: ``mall``,
    ``mall_name`` (mapped via ``mappings``, falling back to the mall key),
    ``original_url``, ``document_ids``, ``reason``, ``last_task_name``,
    ``last_time``, plus durable provenance (``job_state``, ``remote_job_id``,
    ``candidate_id``, ``redispatch_links_used``, ``owning_day``).
    """
    prewarm_path = Path(runtime_root) / PREWARM_STORE_FILENAME
    if not prewarm_path.is_file():
        return []
    prewarm_store = CustomerPrewarmStore.open_read_only(prewarm_path)
    scoped_ids = prewarm_store.candidate_ids_for_business_date(business_date)
    if not scoped_ids:
        return []
    state_root = Path(runtime_root)

    def job_reader(job_id: int, owning_day: str) -> dict[str, Any] | None:
        return _read_job_row_readonly(runtime_paths(owning_day, state_root).state_path, job_id)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    probe_errors: list[str] = []
    for status in (CACHE_STATUS_REMOTE_DISPATCHED, CACHE_STATUS_REMOTE_REUSED):
        for candidate in prewarm_store.candidates(status):
            candidate_id = int(candidate["id"])
            if candidate_id not in scoped_ids:
                continue
            identity = (str(candidate["mall"] or ""), str(candidate["original_url"] or ""))
            if identity in seen:
                continue
            raw_job_id = candidate.get("remote_job_id")
            if raw_job_id is None:
                continue
            owning_day, job_row = _owning_day_job(
                prewarm_store, candidate_id, int(raw_job_id), business_date, job_reader, probe_errors
            )
            if job_row is None:
                continue
            crawl_states = _crawl_states_for_job_readonly(
                runtime_paths(owning_day, state_root).state_path, int(raw_job_id)
            )
            used = prewarm_store.remote_job_link_count(business_date, candidate_id)
            marker = prewarm_store.latest_redispatch_intent_for_candidate(candidate_id)
            unresolved_marker = bool(
                marker is not None
                and str(marker.get("business_date") or "") < business_date
                and str(marker.get("producer") or "") == PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
                and (
                    str(marker.get("business_date") or "") != owning_day
                    or int(marker.get("remote_job_id") or 0) != int(raw_job_id)
                )
            )
            reason = (
                FAILED_LINK_REASON_REDISPATCH_UNKNOWN if unresolved_marker else
                _failed_link_reason(
                    job_row=job_row,
                    job_is_dead=job_row is not None and _job_is_dead(job_row, dead_threshold),
                    crawl_states=crawl_states,
                    redispatch_links_used=used,
                    daily_cap=redispatch_daily_cap,
                )
            )
            if reason is None:
                continue
            seen.add(identity)
            mall = str(candidate["mall"] or "")
            rows.append(
                {
                    "mall": mall,
                    "mall_name": next(
                        (m.mall_name for m in mappings if m.mall_key == mall),
                        mall,
                    ),
                    "original_url": str(candidate["original_url"] or ""),
                    "document_ids": sorted(
                        {
                            str(link["document_id"])
                            for link in prewarm_store.links_for_candidate(candidate_id)
                            if str(link.get("document_id") or "")
                        }
                    ),
                    "reason": reason,
                    "last_task_name": str((job_row or {}).get("task_name") or ""),
                    "last_time": str(
                        candidate.get("last_checked_at") or candidate.get("last_seen_at") or ""
                    ),
                    "job_state": str((job_row or {}).get("state") or ""),
                    "remote_job_id": int(raw_job_id),
                    "candidate_id": candidate_id,
                    "redispatch_links_used": used,
                    "owning_day": owning_day,
                }
            )
    rows.sort(key=lambda row: (row["mall"], row["original_url"]))
    return rows


# ---- A2: reconcile identity-less SUBMIT_UNKNOWN prewarm jobs -----------------


def run_reconcile_phase(
    store: DailyAuditStore,
    token: LeaseToken,
    get_adapter: Callable[[], Any],
    paths: Any,
    *,
    prewarm_store: CustomerPrewarmStore | None = None,
    deadline: float,
    monotonic_value: Callable[[], float],
    summary: dict[str, Any],
    dead_threshold: int = RECONCILE_DEAD_THRESHOLD,
) -> None:
    """Re-identify today's identity-less ``SUBMIT_UNKNOWN`` prewarm jobs.

    Caller owns the daily lease (dispatch pass Phase 5).  ``FOUND`` reconciles
    the identity at ``PRIORITY_PENDING`` and gets one prioritize confirm;
    only conclusive ``NOT_FOUND`` bumps ``recovery_attempts`` toward A3.
    Remote I/O requires a current exact candidate link; the link is fenced
    through each local transition and rechecked before the remote priority call.
    """
    phase_errors = summary["phase_errors"]

    def linked(job_id: int) -> bool:
        return prewarm_store is None or prewarm_store.has_current_remote_job_link(
            business_date=summary["business_date"],
            remote_job_id=job_id,
            producer=PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
        )

    def link_fence(job_id: int):
        if prewarm_store is None:
            return nullcontext(True)
        return prewarm_store.fence_current_remote_job_link(
            business_date=summary["business_date"],
            remote_job_id=job_id,
            producer=PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
        )

    try:
        unknown_jobs = store.list_remote_jobs(token, states={"SUBMIT_UNKNOWN"})
    except Exception as exc:  # noqa: BLE001 - discovery failure never fails the pass
        phase_errors.append(f"reconcile discovery: {type(exc).__name__}: {exc}")
        return
    targets: list[tuple[dict[str, Any], int]] = []
    for job_row in unknown_jobs:
        job_id = int(job_row["id"])
        if str(job_row.get("remote_identity") or "").strip():
            continue  # identity present: the existing lifecycle owns it
        try:
            crawls = store.crawl_work_for_job(job_id)
        except Exception as exc:  # noqa: BLE001 - per-job isolation
            phase_errors.append(f"reconcile job {job_id}: {type(exc).__name__}: {exc}")
            continue
        if not any(str(crawl.get("sku_id") or "").startswith(_ZHPQ_SKU_PREFIX) for crawl in crawls):
            continue  # not prewarm work
        if not linked(job_id):
            continue  # an orphan or superseded job cannot own remote recovery
        attempts = int(job_row.get("recovery_attempts") or 0)
        if attempts >= dead_threshold:
            phase_errors.append(
                f"reconcile job {job_id}: judged dead (recovery_attempts={attempts}); "
                "redispatch owns it"
            )
            continue
        targets.append((job_row, attempts))

    attempted = 0
    for job_row, _attempts in targets:
        job_id = int(job_row["id"])
        if attempted >= RECONCILE_PER_PASS:
            phase_errors.append(
                f"reconcile cap {RECONCILE_PER_PASS} reached; job {job_id} deferred to next pass"
            )
            break
        if monotonic_value() >= deadline:
            phase_errors.append(
                f"budget deadline reached; reconcile of job {job_id} deferred"
            )
            break
        if not linked(job_id):
            continue
        attempted += 1
        try:
            prepared = _reconstruct_prepared_job(get_adapter(), summary["business_date"], job_row)
            outcome = get_adapter().reconcile_unknown(
                prepared, timeout_seconds=max(0.001, deadline - monotonic_value())
            )
            status = _status_value(outcome.status)
            if status == "MANUAL":
                with link_fence(job_id) as owned:
                    if owned:
                        _mark_reconcile_manual(
                            store, token, job_id, str(getattr(outcome, "diagnostic", "") or "")
                        )
                        phase_errors.append(f"reconcile job {job_id}: manual identity conflict; no auto redispatch")
            elif status == "FOUND":
                task_id = getattr(outcome, "task_id", None)
                task_number = getattr(outcome, "task_number", None)
                if not task_id or not task_number:
                    with link_fence(job_id) as owned:
                        if owned:
                            _mark_reconcile_manual(store, token, job_id, "incomplete FOUND identity")
                            phase_errors.append(f"reconcile job {job_id}: incomplete FOUND identity; no auto redispatch")
                    continue
                mall_key = str(job_row.get("mall_key") or prepared.mall_key)
                remote_identity = f"{mall_key}|{task_id}|{task_number}"
                with link_fence(job_id) as owned:
                    if not owned:
                        continue
                    store.reconcile_remote_job_and_crawl_work(
                        token, job_id, remote_identity=remote_identity
                    )
                summary["reconciled_found"] += 1
                # One prioritize confirm; only a PROVEN weight_type == -1
                # reconciles the job to SUBMITTED (note_targets=[]: zero
                # candidate writes by construction).
                _confirm_pending_priority_job(
                    prewarm_store=None,
                    store=store,
                    token=token,
                    get_adapter=get_adapter,
                    business_date=summary["business_date"],
                    note_targets=[],
                    job_id=job_id,
                    deadline=deadline,
                    monotonic_value=monotonic_value,
                    summary=summary,
                    prepared=prepared,
                    pre_remote_guard=lambda: linked(job_id),
                    result_commit_fence=lambda: link_fence(job_id),
                )
            elif status == "NOT_FOUND" and not str(getattr(outcome, "diagnostic", "") or "").strip():
                with link_fence(job_id) as owned:
                    if not owned:
                        continue
                    new_attempts = store.bump_remote_job_recovery_attempt(token, job_id)
                summary["reconcile_not_found"] += 1
                if new_attempts >= dead_threshold:
                    phase_errors.append(
                        f"reconcile job {job_id}: judged dead after {new_attempts} "
                        "attempts; redispatch owns it"
                    )
            else:
                phase_errors.append(
                    f"reconcile job {job_id}: inconclusive remote lookup; deferred"
                )
        except Exception as exc:  # noqa: BLE001 - per-job isolation
            phase_errors.append(f"reconcile job {job_id}: {type(exc).__name__}: {exc}")


# ---- A3: re-dispatch dead-job candidates through the STEP1 create chain ------


def run_redispatch_phase(
    store: DailyAuditStore,
    token: LeaseToken,
    prewarm_store: CustomerPrewarmStore,
    paths: Any,
    mappings: Sequence[ZhaoshangMallMapping],
    get_adapter: Callable[[], Any],
    *,
    business_date: str,
    deadline: float,
    monotonic_value: Callable[[], float],
    max_jobs_per_pass: int,
    summary: dict[str, Any],
    dead_threshold: int = RECONCILE_DEAD_THRESHOLD,
    daily_cap: int = REDISPATCH_DAILY_CAP,
) -> None:
    """Create fresh remote work for candidates whose bound job is dead.

    Caller owns the daily lease.  Per candidate the daily budget is its
    ``prewarm_remote_job_links`` row count of ``business_date`` (cap
    ``daily_cap``); an exhausted candidate keeps its dead binding and earns a
    ``"redispatch budget exhausted"`` ``last_error`` note.  Cross-day dead
    jobs (owning day dated by the linkage row) are redispatched too — the new
    job is built under the PASS business date.
    """
    phase_errors = summary["phase_errors"]
    state_runtime_root = Path(paths.state_path).parent.parent

    def job_reader(job_id: int, owning_day: str) -> dict[str, Any] | None:
        if owning_day == business_date:
            return store.get_remote_job(int(job_id))
        # Cross-day: strictly read-only probe of the owning day's state DB.
        return _read_job_row_readonly(
            runtime_paths(owning_day, state_runtime_root).state_path, job_id
        )

    collected = _collect_redispatch_candidates(
        prewarm_store,
        mappings,
        business_date=business_date,
        state_runtime_root=state_runtime_root,
        job_reader=job_reader,
        dead_threshold=dead_threshold,
        daily_cap=daily_cap,
        phase_errors=phase_errors,
    )
    summary["redispatch_blocked"] = len(collected["blocked"])
    for blocked in collected["blocked"]:
        if blocked["reason"] not in {"redispatch budget exhausted", "prior redispatch intent unresolved"}:
            continue
        candidate = prewarm_store.candidate_by_id(int(blocked["candidate_id"]))
        if candidate is None:
            continue
        # Same-status idempotent record: only last_error is refreshed (the
        # dead binding and the remote_job_id provenance stay untouched).
        _record(
            prewarm_store,
            int(candidate["id"]),
            cache_status=str(candidate["cache_status"]),
            error=str(blocked["reason"]),
            phase_errors=phase_errors,
        )
    if not collected["groups"]:
        return

    created_budget = int(max_jobs_per_pass) - int(summary.get("jobs_created", 0) or 0)
    for mall_key, entries in collected["groups"].items():
        if created_budget <= 0:
            phase_errors.append(
                f"mall {mall_key}: max jobs per pass ({max_jobs_per_pass}) reached; "
                "redispatch deferred to next pass"
            )
            break
        if monotonic_value() >= deadline:
            phase_errors.append(
                f"budget deadline reached; redispatch of mall {mall_key} deferred to next pass"
            )
            break
        mapping = next((m for m in mappings if m.mall_key == mall_key), None)
        if mapping is None:
            continue  # already blocked upstream
        scan_lock = ProcessLock(
            prewarm_store.path.parent / "customer_prewarm.lock"
        )
        try:
            scan_lock.acquire()
        except LeaseUnavailable:
            phase_errors.append(f"redispatch mall {mall_key}: customer prewarm scan lock busy")
            continue
        try:
            try:
                fresh_entries: list[Mapping[str, Any]] = []
                for entry in entries:
                    candidate_id = int(entry["candidate"]["id"])
                    try:
                        current = prewarm_store.candidate_by_id(candidate_id)
                        latest = prewarm_store.remote_job_link_for_candidate(candidate_id)
                    except Exception as exc:  # noqa: BLE001 - no proof, no create
                        phase_errors.append(f"redispatch candidate {candidate_id}: link recheck failed: {type(exc).__name__}: {exc}")
                        continue
                    if (
                        current is not None
                        and str(current["cache_status"]) == str(entry["candidate"]["cache_status"])
                        and int(current["remote_job_id"] or 0) == int(entry["candidate"]["remote_job_id"])
                        and latest is not None
                        and str(latest["business_date"]) == str(entry["owning_day"])
                        and int(latest["remote_job_id"]) == int(entry["candidate"]["remote_job_id"])
                        and str(latest["producer"]) == PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
                    ):
                        fresh_entries.append(entry)
                if not fresh_entries:
                    continue
                _redispatch_mall_group(
                    store=store,
                    token=token,
                    prewarm_store=prewarm_store,
                    paths=paths,
                    mapping=mapping,
                    entries=fresh_entries,
                    get_adapter=get_adapter,
                    business_date=business_date,
                    deadline=deadline,
                    monotonic_value=monotonic_value,
                    max_jobs_per_pass=max_jobs_per_pass,
                    summary=summary,
                )
            except Exception as exc:  # noqa: BLE001 - per-group isolation
                phase_errors.append(
                    f"redispatch mall {mall_key}: {type(exc).__name__}: {exc}"
                )
                continue
        finally:
            scan_lock.release()
        created_budget -= 1


def _redispatch_mall_group(
    *,
    store: DailyAuditStore,
    token: LeaseToken,
    prewarm_store: CustomerPrewarmStore,
    paths: Any,
    mapping: ZhaoshangMallMapping,
    entries: Sequence[Mapping[str, Any]],
    get_adapter: Callable[[], Any],
    business_date: str,
    deadline: float,
    monotonic_value: Callable[[], float],
    max_jobs_per_pass: int,
    summary: dict[str, Any],
) -> int:
    """One mall's dead-job candidates through the idempotent create chain.

    Deliberate near-copy of ``_dispatch_mall_group``'s create path minus the
    reuse/probe logic (the binding is already known dead) — same fingerprint,
    task-name, BATCH_STATE, claim → submit → confirm semantics and the same
    honest SUBMIT_UNKNOWN / NOT_ATTEMPTED handling.  Returns the number of
    remote jobs wired.
    """
    phase_errors = summary["phase_errors"]
    counters = summary
    mall_key = mapping.mall_key
    bucket = summary["by_mall"].setdefault(mall_key, {"reused": 0, "created_urls": 0})

    crawl_ids: list[int] = []
    existing_job_ids: set[int] = set()
    work_job_ids: list[int | None] = []
    requests = [
        dispatch_service.CrawlRequest(
            mall_key=mall_key,
            mall_source_id=mapping.source_id,
            mall_name=mapping.mall_name,
            sku_id=redispatch_placeholder_sku(
                mall_key, str(entry["candidate"]["lookup_url"] or entry["candidate"]["original_url"]), int(entry["attempt_index"])
            ),
            url=str(entry["candidate"]["lookup_url"] or entry["candidate"]["original_url"]),
            backend_version=BACKEND_VERSION,
        )
        for entry in entries
    ]
    for request in requests:
        work = store.ensure_crawl_work(
            token,
            mall_key,
            request.sku_id,
            request.url,
            BACKEND_VERSION,
            prewarm_crawl_fingerprint(
                mall_key, mapping.source_id, request.sku_id, request.url, business_date
            ),
            source_id=mapping.source_id,
        )
        crawl_ids.append(int(work["id"]))
        work_job_ids.append(int(work["remote_job_id"]) if work.get("remote_job_id") is not None else None)
        if work.get("remote_job_id") is not None:
            existing_job_ids.add(int(work["remote_job_id"]))

    # A previous POST may have bound this exact crawl intent before a candidate
    # moved to CACHE_HIT or a prewarm-DB write failed. Reattach surviving
    # candidates to that durable job rather than fingerprinting a smaller group.
    if len(set(work_job_ids)) > 1:
        groups_by_prior: dict[int | None, list[Mapping[str, Any]]] = {}
        for entry, prior_job_id in zip(entries, work_job_ids):
            groups_by_prior.setdefault(prior_job_id, []).append(entry)
        wired = 0
        for prior_job_id, subgroup in groups_by_prior.items():
            if prior_job_id is None and monotonic_value() >= deadline:
                phase_errors.append(f"redispatch mall {mall_key}: new subgroup deferred by budget")
                continue
            wired += _redispatch_mall_group(
                store=store, token=token, prewarm_store=prewarm_store,
                paths=paths, mapping=mapping, entries=subgroup,
                get_adapter=get_adapter, business_date=business_date,
                deadline=deadline, monotonic_value=monotonic_value,
                max_jobs_per_pass=max_jobs_per_pass,
                summary=summary,
            )
        return wired

    resume_unposted: dict[str, Any] | None = None
    if existing_job_ids:
        prior_id = next(iter(existing_job_ids))
        prior = store.get_remote_job(prior_id)
        prior_state = str((prior or {}).get("state") or "")
        prior_payload = (prior or {}).get("payload_json") or "{}"
        if isinstance(prior_payload, str):
            prior_payload = json.loads(prior_payload)
        prior_requests = prior_payload.get("requests", [])
        request_keys = {(request.mall_key, request.sku_id, request.url) for request in requests}
        prior_keys = {
            (str(item.get("mall_key") or ""), str(item.get("sku_id") or ""), str(item.get("url") or ""))
            for item in prior_requests if isinstance(item, dict)
        }
        if prior is None or not request_keys.issubset(prior_keys):
            phase_errors.append(f"redispatch mall {mall_key}: prior crawl job {prior_id} not proven; manual review")
            return 0
        if prior_state == "SUBMIT_INTENT" and not int(prior.get("post_attempted") or 0):
            if request_keys != prior_keys:
                phase_errors.append(f"redispatch mall {mall_key}: unposted prior group changed; manual review")
                return 0
            resume_unposted = prior
        else:
            if prior_state == "SUBMIT_INTENT" and int(prior.get("post_attempted") or 0):
                store.mark_submit_unknown(token, prior_id, "interrupted after redispatch POST claim")
                prior_state = "SUBMIT_UNKNOWN"
            if (
                not int(prior.get("post_attempted") or 0)
                or prior_state not in {"SUBMIT_UNKNOWN", "PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "COMPLETED"}
                or (prior_state != "SUBMIT_UNKNOWN" and not str(prior.get("remote_identity") or "").strip())
            ):
                phase_errors.append(f"redispatch mall {mall_key}: prior crawl job {prior_id} not proven; manual review")
                return 0
        if resume_unposted is None:
            bindings = [
                (int(entry["candidate"]["id"]), str(entry["owning_day"]), int(entry["candidate"]["remote_job_id"]))
                for entry in entries
            ]
            if prewarm_store.bind_redispatched_remote_job_batch(
                business_date, bindings, prior_id, PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
                error="submit unknown; reconcile pending" if prior_state == "SUBMIT_UNKNOWN" else None,
            ):
                summary["redispatched_urls"] += len(bindings)
                if prior_state == "PRIORITY_PENDING":
                    _confirm_pending_priority_job(
                        prewarm_store=prewarm_store, store=store, token=token,
                        get_adapter=get_adapter, business_date=business_date,
                        note_targets=[(int(entry["candidate"]["id"]), str(entry["candidate"]["cache_status"])) for entry in entries],
                        job_id=prior_id, deadline=deadline, monotonic_value=monotonic_value,
                        summary=summary,
                        pre_remote_guard=lambda: prewarm_store.has_current_remote_job_link(
                            business_date=business_date, remote_job_id=prior_id,
                            producer=PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
                        ),
                    )
            else:
                phase_errors.append(f"redispatch mall {mall_key}: prior crawl job {prior_id} binding changed")
            return 0

    # Mirror legacy_zhaoshang_bridge.finalize byte for byte (same as the pass).
    task_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "business_date": business_date,
                "requests": [
                    request.identity
                    for request in sorted(requests, key=lambda value: value.identity)
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    numeric_suffix = f"{int(task_fingerprint, 16) % (10 ** 14):014d}"
    task_name = f"cnnc_current_opt_{requests[0].mall_name}_{numeric_suffix}"
    job_dir = paths.run_dir / "zhaoshang" / mall_key / numeric_suffix
    job_dir.mkdir(parents=True, exist_ok=True)
    batch_state = paths.run_dir / "BATCH_STATE.md"
    if not batch_state.is_file():
        batch_state.parent.mkdir(parents=True, exist_ok=True)
        batch_state.touch()

    job = (
        _reconstruct_prepared_job(get_adapter(), business_date, resume_unposted)
        if resume_unposted is not None
        else get_adapter().prepare_job(
            business_date,
            requests,
            job_dir / "input.xlsx",
            job_dir / "result.xlsx",
            task_name,
            batch_state_path=batch_state,
        )
    )
    payload_json = {
        "requests": [
            {
                "mall_key": request.mall_key,
                "mall_source_id": request.mall_source_id,
                "mall_name": request.mall_name,
                "sku_id": request.sku_id,
                "url": request.url,
                "backend_version": request.backend_version,
            }
            for request in requests
        ],
        "batch_state": str(batch_state),
    }
    job_row = resume_unposted or store.prepare_remote_intent(
        token,
        job.logical_job_key,
        job.task_name,
        job.payload_sha256,
        mall_key=job.mall_key,
        input_path=str(job.template_path),
        result_path=str(job.result_path),
        payload_json=payload_json,
    )
    job_id = int(job_row["id"])
    store.link_crawl_work_batch_to_remote_job(token, crawl_ids, job_id)
    store.renew_lease(token, LEASE_TTL_SECONDS)
    counters["redispatch_jobs"] += 1
    counters["new_remote_urls"] += len(entries)
    summary["batch_sizes"].append(len(entries))
    bucket["created_urls"] += len(entries)

    def _binding_matches(entry: Mapping[str, Any], expected_job_id: int, expected_day: str) -> bool:
        candidate_id = int(entry["candidate"]["id"])
        current = prewarm_store.candidate_by_id(candidate_id)
        link = prewarm_store.remote_job_link_for_candidate(candidate_id)
        return bool(
            current is not None
            and str(current["cache_status"]) == str(entry["candidate"]["cache_status"])
            and int(current["remote_job_id"] or 0) == expected_job_id
            and link is not None
            and str(link["business_date"]) == expected_day
            and int(link["remote_job_id"]) == expected_job_id
            and str(link["producer"]) == PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
        )

    def _old_bindings_current() -> bool:
        return all(
            _binding_matches(
                entry, int(entry["candidate"]["remote_job_id"]), str(entry["owning_day"])
            )
            for entry in entries
        )

    def _new_bindings_current(targets: Sequence[tuple[int, str]]) -> bool:
        target_ids = {candidate_id for candidate_id, _status in targets}
        return all(
            _binding_matches(entry, job_id, business_date)
            for entry in entries
            if int(entry["candidate"]["id"]) in target_ids
        )

    def _bind_candidates(*, error: str | None = None) -> list[tuple[int, str]]:
        current_entries = [
            entry for entry in entries
            if _binding_matches(
                entry, int(entry["candidate"]["remote_job_id"]), str(entry["owning_day"])
            )
        ]
        bindings = [
            (
                int(entry["candidate"]["id"]),
                str(entry["owning_day"]),
                int(entry["candidate"]["remote_job_id"]),
            )
            for entry in current_entries
        ]
        if not bindings:
            return []
        try:
            bound = prewarm_store.bind_redispatched_remote_job_batch(
                business_date, bindings, job_id,
                PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG, error=error,
            )
        except Exception as exc:  # noqa: BLE001 - remote task may already be sent
            phase_errors.append(
                f"redispatch job {job_id}: batch binding failed: {type(exc).__name__}: {exc}"
            )
            return []
        if not bound:
            phase_errors.append(f"redispatch job {job_id}: batch binding changed")
            return []
        return [
            (int(entry["candidate"]["id"]), str(entry["candidate"]["cache_status"]))
            for entry in current_entries
        ]

    # Pre-send boundary: below the budget floor the intent stays unclaimed so
    # a real adapter never burns it into a fake SUBMIT_UNKNOWN (same as pass).
    remaining = deadline - monotonic_value()
    if remaining < SUBMIT_MIN_SECONDS:
        phase_errors.append(
            f"redispatch mall {mall_key}: budget floor reached before claim; "
            f"job {job_id} stays SUBMIT_INTENT for resume"
        )
        return 1

    if not _old_bindings_current():
        phase_errors.append(f"redispatch mall {mall_key}: candidate binding changed before submit")
        return 1

    if int(counters.get("jobs_created", 0) or 0) >= max_jobs_per_pass:
        phase_errors.append(f"redispatch mall {mall_key}: max jobs per pass reached before claim")
        return 0

    intent_bindings = [
        (int(entry["candidate"]["id"]), str(entry["owning_day"]), int(entry["candidate"]["remote_job_id"]))
        for entry in entries
    ]
    if not prewarm_store.record_redispatch_intent_batch(
        business_date, intent_bindings, job_id, PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
    ):
        phase_errors.append(f"redispatch mall {mall_key}: intent marker binding changed before POST")
        return 0

    claim = store.claim_remote_submission(token, job_id)
    if claim is None:
        # A prior pass may have POSTed this exact intent but crashed before
        # candidate binding. Reattach only its proven durable identity; never POST twice.
        existing = store.get_remote_job(job_id)
        existing_state = str((existing or {}).get("state") or "")
        if (
            existing is not None
            and int(existing.get("post_attempted") or 0)
            and existing_state in {"SUBMIT_UNKNOWN", "PRIORITY_PENDING", "SUBMITTED", "RUNNING", "EXPORTED", "COMPLETED"}
            and (existing_state == "SUBMIT_UNKNOWN" or str(existing.get("remote_identity") or "").strip())
        ):
            bound_targets = _bind_candidates(
                error="submit unknown; reconcile pending" if existing_state == "SUBMIT_UNKNOWN" else None
            )
            if bound_targets and existing_state == "PRIORITY_PENDING":
                _confirm_pending_priority_job(
                    prewarm_store=prewarm_store, store=store, token=token,
                    get_adapter=get_adapter, business_date=business_date,
                    note_targets=bound_targets, job_id=job_id, deadline=deadline,
                    monotonic_value=monotonic_value, summary=summary, prepared=job,
                    pre_remote_guard=lambda: _new_bindings_current(bound_targets),
                )
            if not bound_targets:
                phase_errors.append(f"redispatch mall {mall_key}: prior job {job_id} could not be rebound")
            return 1
        phase_errors.append(f"redispatch mall {mall_key}: claim lost on job {job_id}")
        return 1
    if not _old_bindings_current():
        store.release_remote_submission_claim(
            token, job_id, "candidate binding changed before redispatch POST"
        )
        phase_errors.append(f"redispatch mall {mall_key}: candidate binding changed before POST")
        return 1
    counters["jobs_created"] += 1

    remaining = deadline - monotonic_value()
    outcome = get_adapter().submit(job, timeout_seconds=max(0.001, remaining))
    status = _status_value(outcome.status)
    diagnostic = str(getattr(outcome, "diagnostic", "") or "")

    if status == dispatch_service.SubmitStatus.SUBMIT_UNKNOWN.value:
        store.mark_submit_unknown(
            token, job_id, f"prewarm redispatch submit unknown: {diagnostic}"
        )
        _bind_candidates(error="submit unknown; reconcile pending")
        counters["submit_unknown"] += 1
    elif status == dispatch_service.SubmitStatus.NOT_ATTEMPTED.value:
        # Pre-send failure: no dispatch happened; the unclaimed intent is
        # re-planned by the next pass through the idempotent create chain.
        store.release_remote_submission_claim(
            token,
            job_id,
            f"prewarm redispatch submit not attempted: {diagnostic}",
        )
        try:
            prewarm_store.clear_redispatch_intent_batch(
                business_date,
                [int(entry["candidate"]["id"]) for entry in entries],
                job_id,
            )
        except Exception as exc:  # noqa: BLE001 - retained marker is fail-closed
            phase_errors.append(f"redispatch job {job_id}: clear unattempted marker failed: {type(exc).__name__}: {exc}")
        counters["submit_not_attempted"] += 1
        phase_errors.append(
            f"redispatch mall {mall_key}: submit not attempted (pre-send); retry next pass"
            + (f" ({diagnostic})" if diagnostic else "")
        )
    elif status == dispatch_service.SubmitStatus.SUBMITTED.value:
        task_id = getattr(outcome, "task_id", None)
        task_number = getattr(outcome, "task_number", None)
        if not task_id or not task_number:
            # Fail closed like the pass: no identity evidence means the
            # outcome is unknown, reconcile-only, never a silent success.
            store.mark_submit_unknown(
                token, job_id, "prewarm redispatch submit returned incomplete identity"
            )
            _bind_candidates(error="submit unknown; reconcile pending")
            counters["submit_unknown"] += 1
        else:
            remote_identity = f"{mall_key}|{task_id}|{task_number}"
            # weight_type=-1 is PROVEN, never assumed: hold at
            # PRIORITY_PENDING until the same-pass confirm proves it.
            store.reconcile_remote_job(
                token, job_id, remote_identity=remote_identity, state="PRIORITY_PENDING"
            )
            store.advance_crawl_work_for_job(token, job_id, "PRIORITY_PENDING")
            bound_targets = _bind_candidates()
            counters["redispatched_urls"] += len(bound_targets)
            if not bound_targets:
                phase_errors.append(f"redispatch mall {mall_key}: no current candidate bound; priority deferred")
                return 1
            _confirm_pending_priority_job(
                prewarm_store=prewarm_store,
                store=store,
                token=token,
                get_adapter=get_adapter,
                business_date=business_date,
                note_targets=bound_targets,
                job_id=job_id,
                deadline=deadline,
                monotonic_value=monotonic_value,
                summary=summary,
                prepared=job,
                pre_remote_guard=lambda: _new_bindings_current(bound_targets),
            )
    else:  # defensive: unknown submit status is treated fail-closed
        store.mark_submit_unknown(
            token, job_id, f"prewarm redispatch submit unknown status: {status}"
        )
        _bind_candidates(error="submit unknown; reconcile pending")
        counters["submit_unknown"] += 1
    return 1


# ---- A1: harvest live prewarm jobs through the existing primitive ------------


def run_harvest_phase(
    state_path: str | Path,
    lock_path: str | Path,
    business_date: str,
    *,
    deadline: float,
    monotonic_value: Callable[[], float],
    summary: dict[str, Any],
    per_job_timeout_seconds: float = HARVEST_PER_JOB_TIMEOUT_SECONDS,
    max_jobs: int = HARVEST_PER_PASS,
    adapter: Any | None = None,
    mappings: Sequence[ZhaoshangMallMapping] | None = None,
) -> None:
    """Harvest up to ``max_jobs`` live prewarm jobs via the existing primitive.

    MUST be called OUTSIDE the dispatch pass (the primitive acquires the
    per-day ProcessLock + durable lease itself).  ``RUNNING`` outcomes are the
    normal in-flight case (a phase note, never an error); every failure is
    isolated into ``phase_errors``.
    """
    phase_errors = summary["phase_errors"]
    summary.setdefault("harvest_attempted", 0)
    summary.setdefault("harvested", 0)
    connection = _open_state_db_readonly(state_path)
    if connection is None:
        return
    try:
        rows = connection.execute(_HARVEST_JOBS_SQL).fetchall()
    finally:
        connection.close()
    targets: dict[int, str] = {}
    for row in rows:
        targets.setdefault(int(row["job_id"]), str(row["remote_identity"] or ""))

    adapter_holder: list[Any] = []

    def get_adapter() -> Any:
        if not adapter_holder:
            # Constructed through the dispatch service's attribute so the
            # established test seam (monkeypatching
            # customer_prewarm_dispatch.CnncLegacyZhaoshangAdapter) covers the
            # harvest stage exactly like the dispatch pass.
            adapter_holder.append(
                adapter
                if adapter is not None
                else dispatch_service.CnncLegacyZhaoshangAdapter(mappings=mappings)
            )
        return adapter_holder[0]

    attempted = 0
    for job_id, identity in targets.items():
        if attempted >= int(max_jobs):
            phase_errors.append(
                f"harvest cap {max_jobs} reached; job {job_id} deferred to next pass"
            )
            break
        if monotonic_value() >= deadline:
            phase_errors.append(
                f"budget deadline reached; harvest of job {job_id} deferred"
            )
            break
        attempted += 1
        summary["harvest_attempted"] += 1
        try:
            result = harvest_existing_customer_prewarm_job(
                state_path=state_path,
                lock_path=lock_path,
                business_date=business_date,
                remote_job_id=job_id,
                remote_identity=identity,
                adapter=get_adapter(),
                timeout_seconds=max(
                    0.001,
                    min(float(per_job_timeout_seconds), deadline - monotonic_value()),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - per-job isolation
            phase_errors.append(f"harvest job {job_id}: {type(exc).__name__}: {exc}")
            continue
        outcome = str(result.get("outcome") or "")
        if outcome == "REMOTE_EXISTING_HARVEST_COMPLETED":
            summary["harvested"] += 1
        elif outcome == "REMOTE_EXISTING_HARVEST_RUNNING":
            phase_errors.append(
                f"harvest job {job_id}: still running on the remote; next pass"
            )
        else:
            phase_errors.append(f"harvest job {job_id}: {outcome or 'unknown outcome'}")
