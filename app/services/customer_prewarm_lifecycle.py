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
  day are rebuilt from durable payload evidence
  (:func:`_reconstruct_prepared_job`) and re-identified through
  ``adapter.reconcile_unknown``.  ``FOUND`` reconciles the identity at
  ``PRIORITY_PENDING`` (then one prioritize confirm; only a PROVEN
  ``weight_type == -1`` moves the job to ``SUBMITTED``).  ``NOT_FOUND``/
  ``MANUAL`` bumps ``remote_jobs.recovery_attempts``; at
  :data:`RECONCILE_DEAD_THRESHOLD` the job is judged dead and handed to A3.
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

``plan_lifecycle_actions`` is the strictly read-only planner behind the
plan-only report's ``lifecycle_planned`` summary.  Every stage is
error-isolated and budget-bounded (``deadline``/``monotonic_value`` pairs,
mirroring the dispatch pass phases).  Canary mode never enters any stage (the
dispatch pass skips Phase 5 entirely and the CLI never calls the harvest
stage), so the canary summary key set stays byte-identical.
"""

from __future__ import annotations

import hashlib
import json
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
    _record_remote_job_link,
    _status_value,
    prewarm_crawl_fingerprint,
    prewarm_placeholder_sku,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_REMOTE_REUSED,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import DailyAuditStore, LeaseToken
from app.services.incremental_audit_runtime import runtime_paths
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    ReconcileStatus,
    SubmitStatus,
)
from app.services.recovery.harvest import harvest_existing_customer_prewarm_job
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
    if state in _DEAD_JOB_STATES:
        return True
    if state == "SUBMIT_UNKNOWN":
        return int(job_row.get("recovery_attempts") or 0) >= dead_threshold
    return False


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
    if link is not None:
        link_date = str(link.get("business_date") or "")
        if link_date and link_date != business_date:
            owning_day = link_date
    return owning_day, job_reader(int(job_id), owning_day)


def _collect_redispatch_candidates(
    prewarm_store: CustomerPrewarmStore,
    mappings: Sequence[ZhaoshangMallMapping],
    *,
    business_date: str,
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
            _owning_day, job_row = _owning_day_job(
                prewarm_store, candidate_id, int(raw_job_id), business_date, job_reader, phase_errors
            )
            if job_row is None:
                continue
            if not _job_is_dead(job_row, dead_threshold):
                continue
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
                {"candidate": candidate, "attempt_index": used}
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

    Returns ``{"harvest_job_ids", "reconcile_jobs", "redispatch_groups",
    "redispatch_blocked"}``; the plan-only report folds this into its
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
        job_reader=job_reader,
        dead_threshold=reconcile_dead_threshold,
        daily_cap=redispatch_daily_cap,
        phase_errors=[],
    )
    redispatch_groups = {
        mall: [entry["candidate"] for entry in entries]
        for mall, entries in collected["groups"].items()
    }
    return {
        "harvest_job_ids": harvest_job_ids,
        "reconcile_jobs": reconcile_jobs,
        "redispatch_groups": redispatch_groups,
        "redispatch_blocked": collected["blocked"],
    }


# ---- A2: reconcile identity-less SUBMIT_UNKNOWN prewarm jobs -----------------


def run_reconcile_phase(
    store: DailyAuditStore,
    token: LeaseToken,
    get_adapter: Callable[[], Any],
    paths: Any,
    *,
    deadline: float,
    monotonic_value: Callable[[], float],
    summary: dict[str, Any],
    dead_threshold: int = RECONCILE_DEAD_THRESHOLD,
) -> None:
    """Re-identify today's identity-less ``SUBMIT_UNKNOWN`` prewarm jobs.

    Caller owns the daily lease (dispatch pass Phase 5).  ``FOUND`` reconciles
    the identity at ``PRIORITY_PENDING`` and gets one prioritize confirm;
    anything else bumps ``recovery_attempts`` and, at the threshold, is judged
    dead with a phase note (the redispatch stage owns it from then on).  A job
    the tick already pushed to ``MANUAL_REQUIRED`` is dead by state and is
    never reconciled again.
    """
    phase_errors = summary["phase_errors"]
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
        attempted += 1
        try:
            prepared = _reconstruct_prepared_job(get_adapter(), summary["business_date"], job_row)
            outcome = get_adapter().reconcile_unknown(
                prepared, timeout_seconds=max(0.001, deadline - monotonic_value())
            )
            status = _status_value(outcome.status)
            if status == ReconcileStatus.FOUND.value:
                task_id = getattr(outcome, "task_id", None)
                task_number = getattr(outcome, "task_number", None)
                if not task_id or not task_number:
                    # Fail closed: incomplete identity evidence counts as a
                    # failed attempt, never as a silent success.
                    raise ValueError("reconcile returned an incomplete identity")
                mall_key = str(job_row.get("mall_key") or prepared.mall_key)
                remote_identity = f"{mall_key}|{task_id}|{task_number}"
                store.reconcile_remote_job(
                    token, job_id, remote_identity=remote_identity, state="PRIORITY_PENDING"
                )
                store.advance_crawl_work_for_job(token, job_id, "PRIORITY_PENDING")
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
                )
            else:
                new_attempts = store.bump_remote_job_recovery_attempt(token, job_id)
                summary["reconcile_not_found"] += 1
                if new_attempts >= dead_threshold:
                    phase_errors.append(
                        f"reconcile job {job_id}: judged dead after {new_attempts} "
                        "attempts; redispatch owns it"
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
        job_reader=job_reader,
        dead_threshold=dead_threshold,
        daily_cap=daily_cap,
        phase_errors=phase_errors,
    )
    summary["redispatch_blocked"] = len(collected["blocked"])
    for blocked in collected["blocked"]:
        if blocked["reason"] != "redispatch budget exhausted":
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
            error="redispatch budget exhausted",
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
        try:
            _redispatch_mall_group(
                store=store,
                token=token,
                prewarm_store=prewarm_store,
                paths=paths,
                mapping=mapping,
                entries=entries,
                get_adapter=get_adapter,
                business_date=business_date,
                deadline=deadline,
                monotonic_value=monotonic_value,
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001 - per-group isolation
            phase_errors.append(
                f"redispatch mall {mall_key}: {type(exc).__name__}: {exc}"
            )
            continue
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
    requests = [
        CrawlRequest(
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

    job = get_adapter().prepare_job(
        business_date,
        requests,
        job_dir / "input.xlsx",
        job_dir / "result.xlsx",
        task_name,
        batch_state_path=batch_state,
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
    job_row = store.prepare_remote_intent(
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

    # Pre-send boundary: below the budget floor the intent stays unclaimed so
    # a real adapter never burns it into a fake SUBMIT_UNKNOWN (same as pass).
    remaining = deadline - monotonic_value()
    if remaining < SUBMIT_MIN_SECONDS:
        phase_errors.append(
            f"redispatch mall {mall_key}: budget floor reached before claim; "
            f"job {job_id} stays SUBMIT_INTENT for resume"
        )
        return 1

    claim = store.claim_remote_submission(token, job_id)
    if claim is None:
        # Another writer dispatched this job: never double-POST.
        phase_errors.append(f"redispatch mall {mall_key}: claim lost on job {job_id}")
        return 1
    counters["jobs_created"] += 1

    remaining = deadline - monotonic_value()
    outcome = get_adapter().submit(job, timeout_seconds=max(0.001, remaining))
    status = _status_value(outcome.status)
    diagnostic = str(getattr(outcome, "diagnostic", "") or "")

    def _bind_candidates(*, error: str | None = None) -> None:
        for entry in entries:
            candidate_id = int(entry["candidate"]["id"])
            _record_remote_job_link(
                prewarm_store,
                business_date,
                candidate_id,
                job_id,
                PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
                phase_errors,
            )
            _record(
                prewarm_store,
                candidate_id,
                cache_status=str(entry["candidate"]["cache_status"]),
                remote_job_id=job_id,
                error=error,
                phase_errors=phase_errors,
            )

    if status == SubmitStatus.SUBMIT_UNKNOWN.value:
        store.mark_submit_unknown(
            token, job_id, f"prewarm redispatch submit unknown: {diagnostic}"
        )
        _bind_candidates(error="submit unknown; reconcile pending")
        counters["submit_unknown"] += 1
    elif status == SubmitStatus.NOT_ATTEMPTED.value:
        # Pre-send failure: no dispatch happened; the unclaimed intent is
        # re-planned by the next pass through the idempotent create chain.
        store.release_remote_submission_claim(
            token,
            job_id,
            f"prewarm redispatch submit not attempted: {diagnostic}",
        )
        counters["submit_not_attempted"] += 1
        phase_errors.append(
            f"redispatch mall {mall_key}: submit not attempted (pre-send); retry next pass"
            + (f" ({diagnostic})" if diagnostic else "")
        )
    elif status == SubmitStatus.SUBMITTED.value:
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
            _bind_candidates()
            counters["redispatched_urls"] += len(entries)
            _confirm_pending_priority_job(
                prewarm_store=prewarm_store,
                store=store,
                token=token,
                get_adapter=get_adapter,
                business_date=business_date,
                note_targets=[
                    (int(entry["candidate"]["id"]), str(entry["candidate"]["cache_status"]))
                    for entry in entries
                ],
                job_id=job_id,
                deadline=deadline,
                monotonic_value=monotonic_value,
                summary=summary,
                prepared=job,
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
    adapter: CnncLegacyZhaoshangAdapter | None = None,
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

    adapter_holder: list[CnncLegacyZhaoshangAdapter] = []

    def get_adapter() -> CnncLegacyZhaoshangAdapter:
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
