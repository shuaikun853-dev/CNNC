"""Cross-day identity and priority recovery regression tests.

All state lives below ``tmp_path``.  The adapter is a counting fake and no
test creates or contacts a real Zhaoshang task.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager

import pytest

from app.services import customer_prewarm_dispatch as dispatch_service
from app.services import customer_prewarm_cross_day_reconcile as cross_day
from app.services import customer_prewarm_lifecycle as lifecycle
from app.services.customer_prewarm_cross_day_reconcile import (
    CROSS_DAY_HARVEST_PER_PASS,
    CROSS_DAY_RECONCILE_PER_JOB_TIMEOUT_SECONDS,
    CROSS_DAY_RECONCILE_PER_PASS,
    plan_cross_day_reconcile_jobs,
    plan_cross_day_harvest_jobs,
    run_cross_day_reconcile_phase,
    run_cross_day_harvest_phase,
)
from app.services.customer_prewarm_lifecycle import (
    RECONCILE_DEAD_THRESHOLD,
    plan_lifecycle_actions,
)
from app.services.customer_prewarm_dispatch import run_prewarm_dispatch_pass
from app.services.customer_prewarm_store import CACHE_STATUS_CACHE_HIT, CustomerPrewarmStore
from app.services.daily_audit_store import ProcessLock
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    LegacyReconcileOutcome,
    ReconcileStatus,
)
from tests.test_customer_prewarm_lifecycle import (
    COMIX,
    RECONCILE_FOUND,
    RECONCILE_NOT_FOUND,
    TODAY,
    YESTERDAY,
    _CountingAdapter,
    _new_summary,
    _remote_job_row,
    _state_rows,
    leased,
    make_state_db,
    run_redispatch,
    seed_remote_candidate,
    seed_remote_work,
    seed_unknown_job,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts import customer_prewarm_dispatch as cli


def test_planner_finds_cross_day_unknown_and_dedupes_candidate_fanout(tmp_path):
    """Removing linkage-based discovery would strand an old unknown job."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    _paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/cross-day-unknown.html",
    )
    for index in range(50):
        seed_remote_candidate(
            prewarm,
            f"https://qx.com/cross-day-candidate-{index}.html",
            job_id,
            business_date=YESTERDAY,
            document=f"D-{index}",
        )

    assert plan_cross_day_reconcile_jobs(
        prewarm,
        current_business_date=TODAY,
        state_runtime_root=runtime_root,
    ) == [(YESTERDAY, job_id)]


def test_planner_excludes_same_day_unknown(tmp_path):
    """Moving same-day ownership into this phase would double-reconcile it."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    _paths, job_id = seed_unknown_job(
        runtime_root,
        TODAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/same-day-unknown.html",
    )
    seed_remote_candidate(prewarm, "https://qx.com/same-day-unknown.html", job_id)

    assert plan_cross_day_reconcile_jobs(
        prewarm,
        current_business_date=TODAY,
        state_runtime_root=runtime_root,
    ) == []


@pytest.mark.parametrize(
    "state",
    ["COMPLETED", "SUBMITTED", "RUNNING", "MANUAL_REQUIRED"],
)
def test_planner_excludes_historical_jobs_outside_submit_unknown(tmp_path, state):
    """Broadening the state predicate could touch ordinary lifecycle work."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url=f"https://qx.com/historical-{state.lower()}.html",
    )
    with sqlite3.connect(paths.state_path) as connection:
        connection.execute("UPDATE remote_jobs SET state=? WHERE id=?", (state, job_id))
    seed_remote_candidate(
        prewarm,
        f"https://qx.com/historical-{state.lower()}.html",
        job_id,
        business_date=YESTERDAY,
    )

    assert plan_cross_day_reconcile_jobs(
        prewarm,
        current_business_date=TODAY,
        state_runtime_root=runtime_root,
    ) == []


def test_planner_excludes_unknown_with_remote_identity(tmp_path):
    """Identity-bearing jobs belong to priority/harvest, not identity lookup."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/unknown-with-identity.html",
    )
    with sqlite3.connect(paths.state_path) as connection:
        connection.execute(
            "UPDATE remote_jobs SET remote_identity=? WHERE id=?",
            ("comix|already-found|RW-FOUND", job_id),
        )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/unknown-with-identity.html",
        job_id,
        business_date=YESTERDAY,
    )

    assert plan_cross_day_reconcile_jobs(
        prewarm,
        current_business_date=TODAY,
        state_runtime_root=runtime_root,
    ) == []


def test_planner_excludes_non_prewarm_step1_job(tmp_path):
    """Dropping the zhpq proof would reconcile an ordinary STEP1 job."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/ordinary-step1.html",
    )
    with sqlite3.connect(paths.state_path) as connection:
        connection.execute(
            "UPDATE crawl_work SET sku_id='ordinary-step1-sku' WHERE remote_job_id=?",
            (job_id,),
        )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/ordinary-step1.html",
        job_id,
        business_date=YESTERDAY,
    )

    assert plan_cross_day_reconcile_jobs(
        prewarm,
        current_business_date=TODAY,
        state_runtime_root=runtime_root,
    ) == []


def test_planner_excludes_zhpq_job_linked_by_wrong_producer(tmp_path):
    """The zhpq prefix alone must not claim work owned by another producer."""
    runtime_root = tmp_path / "runtime"
    prewarm_path = runtime_root / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(prewarm_path)
    _paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/wrong-producer.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/wrong-producer.html",
        job_id,
        business_date=YESTERDAY,
    )
    with sqlite3.connect(prewarm_path) as connection:
        connection.execute(
            "UPDATE prewarm_remote_job_links SET producer='ordinary-step1'"
        )

    assert plan_cross_day_reconcile_jobs(
        prewarm,
        current_business_date=TODAY,
        state_runtime_root=runtime_root,
    ) == []


@pytest.mark.parametrize("owning_day", ["2026-09-18", "../escaped-day", "not-a-date"])
def test_planner_rejects_non_historical_or_noncanonical_link_dates(
    tmp_path, owning_day
):
    """A corrupt linkage date must never become a filesystem target."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    candidate_id = seed_remote_candidate(
        prewarm,
        f"https://qx.com/invalid-day-{owning_day.replace('/', '-')}.html",
        88,
        business_date=owning_day,
    )

    assert plan_cross_day_reconcile_jobs(
        prewarm,
        current_business_date=TODAY,
        state_runtime_root=runtime_root,
    ) == []
    assert prewarm.candidate_by_id(candidate_id)["remote_job_id"] == 88


def test_atomic_found_transition_rolls_back_job_when_crawl_update_fails(tmp_path):
    """A crash/failure between FOUND writes must not strand a half-transition."""
    runtime_root = tmp_path / "runtime"
    paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/atomic-found.html",
    )
    with sqlite3.connect(paths.state_path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_priority_crawl_update
               BEFORE UPDATE OF state ON crawl_work
               WHEN NEW.state = 'PRIORITY_PENDING'
               BEGIN
                   SELECT RAISE(ABORT, 'injected crawl update failure');
               END"""
        )

    with leased(paths, YESTERDAY) as (store, token):
        with pytest.raises(sqlite3.IntegrityError, match="injected crawl"):
            store.reconcile_remote_job_and_crawl_work(
                token,
                job_id,
                remote_identity="comix|atomic-task|atomic-number",
                state="PRIORITY_PENDING",
            )

    job = _remote_job_row(paths.state_path, job_id)
    assert job["state"] == "SUBMIT_UNKNOWN"
    assert not str(job["remote_identity"] or "")
    assert {
        row["state"]
        for row in _state_rows(
            paths.state_path,
            "SELECT state FROM crawl_work WHERE remote_job_id=?",
            (job_id,),
        )
    } == {"PENDING"}


def test_atomic_priority_confirmation_rolls_back_job_when_crawl_update_fails(
    tmp_path,
):
    """A crawl write failure must not split PRIORITY_PENDING and SUBMITTED."""
    from app.services.legacy_zhaoshang_adapter import (
        LegacyPriorityOutcome,
        PriorityStatus,
    )

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/atomic-priority-confirm.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/atomic-priority-confirm.html",
        job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter(
        reconcile_outcome=RECONCILE_FOUND,
        priority_outcomes=[
            LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, weight_type=None),
            LegacyPriorityOutcome(PriorityStatus.ALREADY_HIGHEST, weight_type=-1),
        ],
    )
    _run_cross_day(runtime_root, prewarm, adapter)
    with sqlite3.connect(old_paths.state_path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_submitted_crawl_update
               BEFORE UPDATE OF state ON crawl_work
               WHEN NEW.state = 'SUBMITTED'
               BEGIN
                   SELECT RAISE(ABORT, 'injected submitted crawl update failure');
               END"""
        )

    _run_cross_day(runtime_root, prewarm, adapter)

    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "PRIORITY_PENDING"
    assert {
        row["state"]
        for row in _state_rows(
            old_paths.state_path,
            "SELECT state FROM crawl_work WHERE remote_job_id=?",
            (job_id,),
        )
    } == {"PRIORITY_PENDING"}


def _run_cross_day(runtime_root, prewarm, adapter, **overrides):
    summary = overrides.pop("summary", _new_summary())
    run_cross_day_reconcile_phase(
        prewarm_store=prewarm,
        current_business_date=TODAY,
        state_runtime_root=runtime_root,
        get_adapter=lambda: adapter,
        deadline=overrides.pop("deadline", 10_000.0),
        monotonic_value=overrides.pop("monotonic_value", lambda: 1.0),
        summary=summary,
        **overrides,
    )
    return summary


def test_manual_reconcile_never_makes_historical_candidate_redispatchable(tmp_path):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/manual-conflict.html",
    )
    candidate_id = seed_remote_candidate(
        prewarm, "https://qx.com/manual-conflict.html", job_id,
        business_date=YESTERDAY,
    )
    today_paths = make_state_db(runtime_root, TODAY)
    adapter = _CountingAdapter(
        reconcile_outcome=LegacyReconcileOutcome(
            ReconcileStatus.MANUAL,
            diagnostic="exact task reconciliation returned multiple or malformed matches",
        )
    )

    for _ in range(RECONCILE_DEAD_THRESHOLD):
        _run_cross_day(runtime_root, prewarm, adapter)

    plan = plan_lifecycle_actions(
        today_paths.state_path, prewarm, TODAY, [COMIX],
        state_runtime_root=runtime_root,
    )
    assert plan["redispatch_groups"] == {}
    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "MANUAL_REQUIRED"
    assert candidate_id not in {
        int(candidate["id"])
        for group in plan["redispatch_groups"].values()
        for candidate in group
    }
    assert adapter.submit_calls == 0


@pytest.mark.parametrize(
    "reason",
    [
        "multiple", "mall", "task_name", "subtask_count",
        "incomplete_identity", "evidence_conflict", "not_enabled",
    ],
)
def test_real_adapter_provenance_conflicts_are_manual(tmp_path, reason):
    seed_adapter = _CountingAdapter()
    seed_unknown_job(
        tmp_path / "runtime", YESTERDAY, seed_adapter, mapping=COMIX,
        url=f"https://qx.com/adapter-manual-{reason}.html",
    )
    prepared = seed_adapter.prepared_log[-1]
    remote = {
        "taskId": "id-1", "taskNumber": "number-1",
        "taskName": prepared.task_name, "mallLogo": "comix",
        "expectedSubtaskCount": 1, "state": "1",
    }
    if reason == "multiple":
        remote = [remote, dict(remote)]
    elif reason == "mall":
        remote["mallLogo"] = "other-mall"
    elif reason == "task_name":
        remote["taskName"] = "different-task"
    elif reason == "subtask_count":
        remote["expectedSubtaskCount"] = 2
    elif reason == "incomplete_identity":
        remote["taskNumber"] = ""
    elif reason == "not_enabled":
        remote["state"] = "0"
    else:
        for action in ("CREATED", "ENABLED"):
            event = {
                "action": action, "mall": "comix", "task_name": prepared.task_name,
                "task_id": "other-id", "task_number": "other-number",
                "expected_subtask_count": 1,
            }
            with prepared.batch_state_path.open("a", encoding="utf-8") as stream:
                stream.write(f"- `{json.dumps(event)}`\n")
    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[COMIX], reconciler=lambda *_args: remote
    )

    outcome = adapter.reconcile_unknown(prepared, timeout_seconds=12)

    assert outcome.status is ReconcileStatus.MANUAL


def test_inconclusive_historical_not_found_cannot_age_into_redispatch(tmp_path):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/old-lookup-inconclusive.html",
    )
    seed_remote_candidate(
        prewarm, "https://qx.com/old-lookup-inconclusive.html", job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter(
        reconcile_outcome=LegacyReconcileOutcome(
            ReconcileStatus.NOT_FOUND,
            diagnostic="exact task reconciliation is not yet conclusive",
        )
    )

    for _ in range(RECONCILE_DEAD_THRESHOLD):
        _run_cross_day(runtime_root, prewarm, adapter)

    job = _remote_job_row(old_paths.state_path, job_id)
    assert job["state"] == "SUBMIT_UNKNOWN"
    assert job["recovery_attempts"] == 0
    assert adapter.submit_calls == 0


def test_rebind_after_found_commit_blocks_priority_remote_write(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/rebind-before-priority.html",
    )
    candidate_id = seed_remote_candidate(
        prewarm, "https://qx.com/rebind-before-priority.html", job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter(reconcile_outcome=RECONCILE_FOUND)
    original_fence = prewarm.fence_current_remote_job_link
    fence_calls = 0

    @contextmanager
    def rebind_after_first_commit(**kwargs):
        nonlocal fence_calls
        with original_fence(**kwargs) as owned:
            yield owned
        fence_calls += 1
        if fence_calls == 1:
            prewarm.record_candidate_result(
                candidate_id, cache_status="REMOTE_DISPATCHED", remote_job_id=99
            )
            prewarm.record_remote_job_link(TODAY, candidate_id, 99, "customer-prewarm-zhaoshang")

    monkeypatch.setattr(prewarm, "fence_current_remote_job_link", rebind_after_first_commit)
    _run_cross_day(runtime_root, prewarm, adapter)

    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "PRIORITY_PENDING"
    assert adapter.prioritize_calls == 0
    assert adapter.submit_calls == 0


def test_cli_harvests_historical_submitted_job_from_owning_day(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    today_paths = make_state_db(runtime_root, TODAY)
    old_paths, job_id = seed_remote_work(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/historical-harvest-owner.html",
        job_state="SUBMITTED", remote_identity="comix|old-task|old-number",
    )
    seed_remote_candidate(
        prewarm, "https://qx.com/historical-harvest-owner.html", job_id,
        business_date=YESTERDAY,
    )
    seen = []
    monkeypatch.setattr(cli, "load_zhaoshang_mall_mappings", lambda _path: [COMIX])
    monkeypatch.setattr(
        cli, "run_prewarm_dispatch_pass",
        lambda *_args, **_kwargs: {"outcome": "OK", "phase_errors": []},
    )
    monkeypatch.setattr(cli, "run_cross_day_reconcile_phase", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli, "run_cross_day_harvest_phase",
        lambda **kwargs: seen.append(("historical", kwargs["current_business_date"])),
        raising=False,
    )
    monkeypatch.setattr(
        cli, "run_harvest_phase",
        lambda state_path, _lock_path, business_date, **_kwargs: seen.append(
            ("today", business_date, state_path)
        ),
    )

    assert cli.main([
        "--business-date", TODAY, "--runtime-root", str(runtime_root),
        "--budget-seconds", "10",
    ]) == 0
    assert seen == [
        ("historical", TODAY),
        ("today", TODAY, today_paths.state_path),
    ]


def test_historical_harvest_success_mirrors_cache_then_candidate_absorbs(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    make_state_db(runtime_root, TODAY)
    url = "https://qx.com/historical-complete-cache.html"
    old_paths, job_id = seed_remote_work(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
        url=url, job_state="SUBMITTED", remote_identity="comix|old-task|old-number",
    )
    candidate_id = seed_remote_candidate(prewarm, url, job_id, business_date=YESTERDAY)
    harvest_calls = []

    def fake_primitive(**kwargs):
        harvest_calls.append((kwargs["business_date"], kwargs["remote_job_id"]))
        assert kwargs["state_path"] == old_paths.state_path
        with sqlite3.connect(old_paths.state_path) as connection:
            connection.execute(
                "UPDATE remote_jobs SET state='COMPLETED' WHERE id=?", (job_id,)
            )
            connection.execute(
                "UPDATE crawl_work SET state='SUCCESS', result_json=? WHERE remote_job_id=?",
                (json.dumps({"page_price_status": "OK", "page_price_value": "19.50"}), job_id),
            )
        return {"outcome": "REMOTE_EXISTING_HARVEST_COMPLETED", "create_calls": 0}

    monkeypatch.setattr(lifecycle, "harvest_existing_customer_prewarm_job", fake_primitive)
    monkeypatch.setattr(cli, "load_zhaoshang_mall_mappings", lambda _path: [COMIX])
    monkeypatch.setattr(
        cli, "run_prewarm_dispatch_pass",
        lambda *_args, **_kwargs: {"outcome": "OK", "phase_errors": []},
    )
    monkeypatch.setattr(cli, "run_cross_day_reconcile_phase", lambda **_kwargs: None)

    assert cli.main([
        "--business-date", TODAY, "--runtime-root", str(runtime_root),
        "--budget-seconds", "30",
    ]) == 0
    assert harvest_calls == [(YESTERDAY, job_id)]
    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "COMPLETED"
    assert cache.lookup_current("comix", url) is not None

    summary = run_prewarm_dispatch_pass(
        prewarm, cache, business_date=TODAY, runtime_root=runtime_root,
        mappings=[COMIX], adapter=_CountingAdapter(),
    )
    assert summary["absorbed"] == 1
    assert prewarm.candidate_by_id(candidate_id)["cache_status"] == CACHE_STATUS_CACHE_HIT


@pytest.mark.parametrize("terminal_state", ["COMPLETED", "MANUAL_REQUIRED"])
def test_historical_terminal_success_retries_cache_mirror_without_remote_call(
    tmp_path, terminal_state
):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = f"https://qx.com/terminal-sync-{terminal_state}.html"
    old_paths, job_id = seed_remote_work(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
        url=url, job_state="SUBMITTED", remote_identity="comix|old|number",
    )
    seed_remote_candidate(prewarm, url, job_id, business_date=YESTERDAY)
    with sqlite3.connect(old_paths.state_path) as connection:
        connection.execute(
            "UPDATE remote_jobs SET state=? WHERE id=?", (terminal_state, job_id)
        )
        connection.execute(
            "UPDATE crawl_work SET state='SUCCESS', result_json=? WHERE remote_job_id=?",
            (json.dumps({"page_price_status": "OK", "page_price_value": "21"}), job_id),
        )

    summary = _new_summary()
    run_cross_day_harvest_phase(
        prewarm_store=prewarm, shared_cache=cache,
        current_business_date=TODAY, state_runtime_root=runtime_root,
        get_adapter=lambda: (_ for _ in ()).throw(AssertionError("remote adapter used")),
        deadline=1000.0, monotonic_value=lambda: 1.0, summary=summary,
    )

    assert summary["cross_day_harvest_cache_synced"] == 1
    assert cache.lookup_current("comix", url) is not None


def test_partial_historical_manual_success_cannot_create_before_cache_mirror(tmp_path):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    today_paths = make_state_db(runtime_root, TODAY)
    url = "https://qx.com/partial-old-success.html"
    old_paths, job_id = seed_remote_work(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
        url=url, job_state="SUBMITTED", remote_identity="comix|old|number",
    )
    seed_remote_candidate(prewarm, url, job_id, business_date=YESTERDAY)
    with sqlite3.connect(old_paths.state_path) as connection:
        connection.execute(
            "UPDATE remote_jobs SET state='MANUAL_REQUIRED' WHERE id=?", (job_id,)
        )
        connection.execute(
            "UPDATE crawl_work SET state='SUCCESS', result_json=? WHERE remote_job_id=?",
            (json.dumps({"page_price_status": "OK", "page_price_value": "21"}), job_id),
        )

    plan = plan_lifecycle_actions(
        today_paths.state_path, prewarm, TODAY, [COMIX],
        state_runtime_root=runtime_root,
    )

    assert plan["cross_day_harvest_jobs"] == [(YESTERDAY, job_id)]
    assert plan["redispatch_groups"] == {}


def test_historical_harvest_cursor_reaches_later_running_job(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    job_ids = []
    for index in range(CROSS_DAY_HARVEST_PER_PASS + 1):
        url = f"https://qx.com/old-running-{index}.html"
        _paths, job_id = seed_remote_work(
            runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
            url=url, job_state="RUNNING",
            remote_identity=f"comix|task-{index}|number-{index}",
        )
        seed_remote_candidate(
            prewarm, url, job_id, business_date=YESTERDAY,
            document=f"RUNNING-{index}",
        )
        job_ids.append(job_id)
    calls = []
    monkeypatch.setattr(
        lifecycle, "harvest_existing_customer_prewarm_job",
        lambda **kwargs: calls.append(kwargs["remote_job_id"])
        or {"outcome": "REMOTE_EXISTING_HARVEST_RUNNING", "create_calls": 0},
    )
    for _ in range(2):
        run_cross_day_harvest_phase(
            prewarm_store=prewarm, shared_cache=cache,
            current_business_date=TODAY, state_runtime_root=runtime_root,
            get_adapter=lambda: _CountingAdapter(), deadline=1000.0,
            monotonic_value=lambda: 1.0, summary=_new_summary(),
        )

    assert calls[:CROSS_DAY_HARVEST_PER_PASS] == job_ids[:CROSS_DAY_HARVEST_PER_PASS]
    assert job_ids[-1] in calls[CROSS_DAY_HARVEST_PER_PASS:]


def test_historical_harvest_dedupes_by_day_and_job_not_bare_id(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    days = ("2026-09-14", YESTERDAY)
    expected = []
    for day in days:
        url = f"https://qx.com/colliding-job-{day}.html"
        _paths, job_id = seed_remote_work(
            runtime_root, day, _CountingAdapter(), mapping=COMIX,
            url=url, job_state="RUNNING",
            remote_identity=f"comix|task-{day}|number-{day}",
        )
        seed_remote_candidate(prewarm, url, job_id, business_date=day, document=day)
        expected.append((day, job_id))
    calls = []
    monkeypatch.setattr(
        lifecycle, "harvest_existing_customer_prewarm_job",
        lambda **kwargs: calls.append((kwargs["business_date"], kwargs["remote_job_id"]))
        or {"outcome": "REMOTE_EXISTING_HARVEST_RUNNING", "create_calls": 0},
    )

    assert expected[0][1] == expected[1][1]
    assert plan_cross_day_harvest_jobs(
        prewarm, current_business_date=TODAY, state_runtime_root=runtime_root
    ) == expected
    run_cross_day_harvest_phase(
        prewarm_store=prewarm, shared_cache=cache,
        current_business_date=TODAY, state_runtime_root=runtime_root,
        get_adapter=lambda: _CountingAdapter(), deadline=1000.0,
        monotonic_value=lambda: 1.0, summary=_new_summary(),
    )
    assert calls == expected


def test_historical_harvest_lock_busy_and_missing_db_have_zero_remote_calls(tmp_path):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    old_paths, job_id = seed_remote_work(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/old-harvest-lock.html", job_state="SUBMITTED",
        remote_identity="comix|old-lock|old-number",
    )
    seed_remote_candidate(
        prewarm, "https://qx.com/old-harvest-lock.html", job_id,
        business_date=YESTERDAY,
    )
    missing_day = "2026-09-14"
    seed_remote_candidate(
        prewarm, "https://qx.com/old-harvest-missing.html", 404,
        business_date=missing_day, document="MISSING",
    )
    adapter = _CountingAdapter()
    held = ProcessLock(old_paths.lock_path)
    held.acquire(owner={"test": "held"})
    try:
        summary = _new_summary()
        run_cross_day_harvest_phase(
            prewarm_store=prewarm, shared_cache=cache,
            current_business_date=TODAY, state_runtime_root=runtime_root,
            get_adapter=lambda: adapter, deadline=1000.0,
            monotonic_value=lambda: 1.0, summary=summary,
        )
    finally:
        held.release()

    assert summary["cross_day_harvest_lock_busy"] == 1
    assert adapter.submit_calls == adapter.prioritize_calls == 0
    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "SUBMITTED"
    assert not (runtime_root / missing_day / "state.sqlite3").exists()


def test_historical_harvest_discards_rebound_link_before_remote_call(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/harvest-rebound.html"
    _paths, job_id = seed_remote_work(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
        url=url, job_state="SUBMITTED", remote_identity="comix|old|number",
    )
    candidate_id = seed_remote_candidate(prewarm, url, job_id, business_date=YESTERDAY)
    original_plan = cross_day.plan_cross_day_harvest_jobs
    calls = []

    def rebind_after_plan(*args, **kwargs):
        planned = original_plan(*args, **kwargs)
        prewarm.record_candidate_result(
            candidate_id, cache_status="REMOTE_DISPATCHED", remote_job_id=99
        )
        prewarm.record_remote_job_link(TODAY, candidate_id, 99, "customer-prewarm-zhaoshang")
        return planned

    monkeypatch.setattr(cross_day, "plan_cross_day_harvest_jobs", rebind_after_plan)
    monkeypatch.setattr(
        lifecycle, "harvest_existing_customer_prewarm_job",
        lambda **_kwargs: calls.append(1),
    )
    run_cross_day_harvest_phase(
        prewarm_store=prewarm, shared_cache=cache,
        current_business_date=TODAY, state_runtime_root=runtime_root,
        get_adapter=lambda: _CountingAdapter(), deadline=1000.0,
        monotonic_value=lambda: 1.0, summary=_new_summary(),
    )
    assert calls == []


def test_found_updates_only_historical_db_once_and_uses_owning_day(tmp_path):
    """Wrong dedupe/day selection would reconcile twice or write today's DB."""
    runtime_root = tmp_path / "runtime"
    today_paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/found-old.html",
    )
    with leased(old_paths, YESTERDAY) as (store, token):
        store.bump_remote_job_recovery_attempt(token, job_id)
        store.bump_remote_job_recovery_attempt(token, job_id)
    for index in range(2):
        seed_remote_candidate(
            prewarm,
            f"https://qx.com/found-bound-{index}.html",
            job_id,
            business_date=YESTERDAY,
            document=f"FOUND-{index}",
        )
    adapter = _CountingAdapter(reconcile_outcome=RECONCILE_FOUND)

    summary = _run_cross_day(runtime_root, prewarm, adapter)

    assert summary["cross_day_reconcile_planned"] == 1
    assert summary["cross_day_reconcile_attempted"] == 1
    assert summary["cross_day_reconciled_found"] == 1
    assert adapter.reconcile_calls == 1
    assert adapter.reconcile_log[0].business_date == YESTERDAY
    assert adapter.submit_calls == 0
    historical = _remote_job_row(old_paths.state_path, job_id)
    assert historical["remote_identity"] == "comix|found-task|found-number"
    assert historical["state"] == "SUBMITTED"
    assert historical["recovery_attempts"] == 2
    assert {
        row["state"]
        for row in _state_rows(
            old_paths.state_path,
            "SELECT state FROM crawl_work WHERE remote_job_id=?",
            (job_id,),
        )
    } == {"SUBMITTED"}
    assert _state_rows(today_paths.state_path, "SELECT id FROM remote_jobs") == []


def test_found_without_priority_proof_remains_pending_in_historical_db(tmp_path):
    """FOUND alone must not claim SUBMITTED without weight_type=-1 proof."""
    from app.services.legacy_zhaoshang_adapter import (
        LegacyPriorityOutcome,
        PriorityStatus,
    )

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/found-priority-unconfirmed.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/found-priority-unconfirmed.html",
        job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter(
        reconcile_outcome=RECONCILE_FOUND,
        priority_outcomes=[
            LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, weight_type=None)
        ],
    )

    summary = _run_cross_day(runtime_root, prewarm, adapter)

    assert summary["cross_day_reconciled_found"] == 1
    assert summary["priority_confirmed"] == 0
    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "PRIORITY_PENDING"
    assert {
        row["state"]
        for row in _state_rows(
            old_paths.state_path,
            "SELECT state FROM crawl_work WHERE remote_job_id=?",
            (job_id,),
        )
    } == {"PRIORITY_PENDING"}


def test_priority_pending_retries_priority_next_pass_without_reconcile_or_submit(
    tmp_path,
):
    """A FOUND identity must stay live across passes until priority is proven."""
    from app.services.legacy_zhaoshang_adapter import (
        LegacyPriorityOutcome,
        PriorityStatus,
    )

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/priority-pending-next-pass.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/priority-pending-next-pass.html",
        job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter(
        reconcile_outcome=RECONCILE_FOUND,
        priority_outcomes=[
            LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, weight_type=None),
            LegacyPriorityOutcome(PriorityStatus.ALREADY_HIGHEST, weight_type=-1),
        ],
    )

    first = _run_cross_day(runtime_root, prewarm, adapter)

    assert first["cross_day_reconcile_planned"] == 1
    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "PRIORITY_PENDING"
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 1
    assert adapter.submit_calls == 0

    second = _run_cross_day(runtime_root, prewarm, adapter)

    assert second["cross_day_reconcile_planned"] == 1
    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "SUBMITTED"
    assert {
        row["state"]
        for row in _state_rows(
            old_paths.state_path,
            "SELECT state FROM crawl_work WHERE remote_job_id=?",
            (job_id,),
        )
    } == {"SUBMITTED"}
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 2
    assert adapter.submit_calls == 0

    third = _run_cross_day(runtime_root, prewarm, adapter)

    assert third["cross_day_reconcile_planned"] == 0
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 2
    assert adapter.submit_calls == 0


def test_found_priority_deferred_by_budget_recovers_on_next_pass(tmp_path):
    """Budget exhaustion after FOUND must defer only the priority action."""
    from app.services.legacy_zhaoshang_adapter import (
        LegacyPriorityOutcome,
        PriorityStatus,
    )

    class Clock:
        value = 1.0

        def __call__(self):
            return self.value

    clock = Clock()

    class BudgetExhaustingAdapter(_CountingAdapter):
        def reconcile_unknown(self, job, *, timeout_seconds):
            outcome = super().reconcile_unknown(job, timeout_seconds=timeout_seconds)
            clock.value = 27.0
            return outcome

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/priority-budget-next-pass.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/priority-budget-next-pass.html",
        job_id,
        business_date=YESTERDAY,
    )
    adapter = BudgetExhaustingAdapter(
        reconcile_outcome=RECONCILE_FOUND,
        priority_outcomes=[
            LegacyPriorityOutcome(PriorityStatus.ALREADY_HIGHEST, weight_type=-1)
        ],
    )

    first = _run_cross_day(
        runtime_root,
        prewarm,
        adapter,
        deadline=100.0,
        monotonic_value=clock,
    )

    assert first["priority_confirmed"] == 0
    assert adapter.prioritize_calls == 0
    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "PRIORITY_PENDING"

    clock.value = 1.0
    second = _run_cross_day(
        runtime_root,
        prewarm,
        adapter,
        deadline=100.0,
        monotonic_value=clock,
    )

    assert second["priority_confirmed"] == 1
    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "SUBMITTED"
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 1
    assert adapter.submit_calls == 0


def test_priority_exception_recovers_on_next_pass_without_reconcile(tmp_path):
    """A transient priority exception must leave the durable retry state live."""
    from app.services.legacy_zhaoshang_adapter import (
        LegacyPriorityOutcome,
        PriorityStatus,
    )

    class TransientPriorityAdapter(_CountingAdapter):
        def prioritize(self, job, *, task_id, task_number, timeout_seconds):
            self.prioritize_calls += 1
            if self.prioritize_calls == 1:
                raise TimeoutError("injected transient priority timeout")
            return LegacyPriorityOutcome(
                PriorityStatus.ALREADY_HIGHEST,
                weight_type=-1,
            )

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/priority-exception-next-pass.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/priority-exception-next-pass.html",
        job_id,
        business_date=YESTERDAY,
    )
    adapter = TransientPriorityAdapter(reconcile_outcome=RECONCILE_FOUND)

    _run_cross_day(runtime_root, prewarm, adapter)
    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "PRIORITY_PENDING"

    _run_cross_day(runtime_root, prewarm, adapter)

    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "SUBMITTED"
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 2
    assert adapter.submit_calls == 0


@pytest.mark.parametrize(
    "remote_identity",
    ["comix|task-only", "wrong-mall|task|number", "comix||number", "comix|task|"],
)
def test_priority_pending_malformed_identity_fails_closed(tmp_path, remote_identity):
    """Malformed recovered identity must never authorize priority or SUBMITTED."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url=f"https://qx.com/malformed-{hashlib.sha256(remote_identity.encode()).hexdigest()}.html",
    )
    with sqlite3.connect(old_paths.state_path) as connection:
        connection.execute(
            "UPDATE remote_jobs SET state='PRIORITY_PENDING', remote_identity=? WHERE id=?",
            (remote_identity, job_id),
        )
        connection.execute(
            "UPDATE crawl_work SET state='PRIORITY_PENDING' WHERE remote_job_id=?",
            (job_id,),
        )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/malformed-bound.html",
        job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter()

    summary = _run_cross_day(runtime_root, prewarm, adapter)

    assert summary["cross_day_reconcile_planned"] == 1
    assert _remote_job_row(old_paths.state_path, job_id)["state"] == "PRIORITY_PENDING"
    assert adapter.reconcile_calls == 0
    assert adapter.prioritize_calls == 0
    assert adapter.submit_calls == 0


def test_not_found_bumps_historical_attempt_once(tmp_path):
    """Losing the historical write would leave attempts frozen across days."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/not-found-old.html",
    )
    with leased(old_paths, YESTERDAY) as (store, token):
        store.bump_remote_job_recovery_attempt(token, job_id)
        store.bump_remote_job_recovery_attempt(token, job_id)
    seed_remote_candidate(
        prewarm,
        "https://qx.com/not-found-old.html",
        job_id,
        business_date=YESTERDAY,
    )

    summary = _run_cross_day(
        runtime_root,
        prewarm,
        _CountingAdapter(reconcile_outcome=RECONCILE_NOT_FOUND),
    )

    assert summary["cross_day_reconcile_not_found"] == 1
    assert _remote_job_row(old_paths.state_path, job_id)["recovery_attempts"] == 3


def test_incomplete_found_identity_blocks_auto_redispatch(tmp_path):
    """A partially found remote identity never proves replacement is safe."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/incomplete-found.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/incomplete-found.html",
        job_id,
        business_date=YESTERDAY,
    )
    incomplete = LegacyReconcileOutcome(
        ReconcileStatus.FOUND, task_id="task-only", task_number=None
    )

    summary = _run_cross_day(
        runtime_root,
        prewarm,
        _CountingAdapter(reconcile_outcome=incomplete),
    )

    assert summary["cross_day_reconcile_not_found"] == 0
    job = _remote_job_row(old_paths.state_path, job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    assert job["recovery_attempts"] == 0
    today_paths = make_state_db(runtime_root, TODAY)
    assert plan_lifecycle_actions(
        today_paths.state_path, prewarm, TODAY, [COMIX],
        state_runtime_root=runtime_root,
    )["redispatch_groups"] == {}


def test_fourth_not_found_makes_existing_redispatch_planner_own_candidate(tmp_path):
    """Threshold completion must hand off to the single existing redispatch path."""
    runtime_root = tmp_path / "runtime"
    today_paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/dead-after-four.html",
    )
    with leased(old_paths, YESTERDAY) as (store, token):
        for _ in range(RECONCILE_DEAD_THRESHOLD - 1):
            store.bump_remote_job_recovery_attempt(token, job_id)
    candidate_id = seed_remote_candidate(
        prewarm,
        "https://qx.com/dead-after-four.html",
        job_id,
        business_date=YESTERDAY,
    )

    summary = _run_cross_day(
        runtime_root,
        prewarm,
        _CountingAdapter(reconcile_outcome=RECONCILE_NOT_FOUND),
    )

    assert summary["cross_day_reconcile_dead"] == 1
    assert _remote_job_row(old_paths.state_path, job_id)["recovery_attempts"] == 4
    assert plan_cross_day_reconcile_jobs(
        prewarm,
        current_business_date=TODAY,
        state_runtime_root=runtime_root,
    ) == []
    lifecycle_plan = plan_lifecycle_actions(
        today_paths.state_path,
        prewarm,
        TODAY,
        [COMIX],
        state_runtime_root=runtime_root,
    )
    assert [
        int(row["id"])
        for row in lifecycle_plan["redispatch_groups"][COMIX.mall_key]
    ] == [candidate_id]


def test_next_pass_redispatch_creates_today_job_without_overwriting_old_job(tmp_path):
    """Cross-day reconcile itself must never become a second create path."""
    runtime_root = tmp_path / "runtime"
    today_paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, old_job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/redispatch-next-pass.html",
    )
    with leased(old_paths, YESTERDAY) as (store, token):
        for _ in range(RECONCILE_DEAD_THRESHOLD - 1):
            store.bump_remote_job_recovery_attempt(token, old_job_id)
    candidate_id = seed_remote_candidate(
        prewarm,
        "https://qx.com/redispatch-next-pass.html",
        old_job_id,
        business_date=YESTERDAY,
    )
    # Daily DB ids are independent and both first rows would naturally be 1.
    # Seed an unrelated today row so this fixture can also prove the candidate
    # switched to a distinct numeric id, not merely a distinct owning day.
    seed_unknown_job(
        runtime_root,
        TODAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/unrelated-today.html",
    )
    reconcile_adapter = _CountingAdapter(reconcile_outcome=RECONCILE_NOT_FOUND)
    _run_cross_day(runtime_root, prewarm, reconcile_adapter)
    assert reconcile_adapter.submit_calls == 0

    redispatch_adapter = _CountingAdapter()
    redispatch_summary = run_redispatch(
        runtime_root, today_paths, prewarm, redispatch_adapter
    )

    candidate = prewarm.candidate_by_id(candidate_id)
    new_job_id = int(candidate["remote_job_id"])
    assert redispatch_summary["redispatch_jobs"] == 1
    assert new_job_id != old_job_id
    assert _remote_job_row(old_paths.state_path, old_job_id)["state"] == "SUBMIT_UNKNOWN"
    assert _remote_job_row(old_paths.state_path, old_job_id)["recovery_attempts"] == 4
    assert _remote_job_row(today_paths.state_path, new_job_id)["state"] == "SUBMITTED"
    link = prewarm.remote_job_link_for_candidate(candidate_id)
    assert (link["business_date"], int(link["remote_job_id"])) == (TODAY, new_job_id)


def test_historical_lock_busy_is_zero_write_and_zero_remote_call(tmp_path):
    """A competing historical owner must defer without consuming an attempt."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/lock-busy-old.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/lock-busy-old.html",
        job_id,
        business_date=YESTERDAY,
    )
    held = ProcessLock(old_paths.lock_path)
    held.acquire(owner={"test": "held"})
    adapter = _CountingAdapter(reconcile_outcome=RECONCILE_NOT_FOUND)
    try:
        summary = _run_cross_day(runtime_root, prewarm, adapter)
    finally:
        held.release()

    assert summary["cross_day_reconcile_lock_busy"] == 1
    assert adapter.reconcile_calls == 0
    job = _remote_job_row(old_paths.state_path, job_id)
    assert job["state"] == "SUBMIT_UNKNOWN"
    assert job["recovery_attempts"] == 0


def test_missing_historical_db_is_deferred_without_creating_it(tmp_path):
    """Following a stale link must not initialize a missing historical DB."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    missing_day = "2026-09-14"
    seed_remote_candidate(
        prewarm,
        "https://qx.com/missing-old-db.html",
        404,
        business_date=missing_day,
    )
    adapter = _CountingAdapter()

    summary = _run_cross_day(runtime_root, prewarm, adapter)

    assert summary["cross_day_reconcile_deferred"] == 1
    assert adapter.reconcile_calls == 0
    assert not (runtime_root / missing_day / "state.sqlite3").exists()
    assert any("historical state DB missing" in note for note in summary["phase_errors"])


def test_cross_day_pass_cap_and_per_job_timeout_are_bounded(tmp_path):
    """A backlog must neither exceed four calls nor give one call the pass budget."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    for index in range(CROSS_DAY_RECONCILE_PER_PASS + 2):
        _paths, job_id = seed_unknown_job(
            runtime_root,
            YESTERDAY,
            _CountingAdapter(),
            mapping=COMIX,
            url=f"https://qx.com/capped-{index}.html",
        )
        seed_remote_candidate(
            prewarm,
            f"https://qx.com/capped-{index}.html",
            job_id,
            business_date=YESTERDAY,
            document=f"CAP-{index}",
        )

    class TimeoutRecordingAdapter(_CountingAdapter):
        def __init__(self):
            super().__init__(reconcile_outcome=RECONCILE_NOT_FOUND)
            self.timeouts = []

        def reconcile_unknown(self, job, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            return super().reconcile_unknown(job, timeout_seconds=timeout_seconds)

    adapter = TimeoutRecordingAdapter()
    summary = _run_cross_day(runtime_root, prewarm, adapter, deadline=1000.0)

    assert adapter.reconcile_calls == CROSS_DAY_RECONCILE_PER_PASS
    assert summary["cross_day_reconcile_attempted"] == CROSS_DAY_RECONCILE_PER_PASS
    assert summary["cross_day_reconcile_deferred"] == 2
    assert all(
        0 < timeout <= CROSS_DAY_RECONCILE_PER_JOB_TIMEOUT_SECONDS
        for timeout in adapter.timeouts
    )


def test_cross_day_cap_rotates_persistent_pending_jobs_without_starvation(tmp_path):
    """Four permanently pending jobs must not starve later historical jobs."""
    from app.services.legacy_zhaoshang_adapter import (
        LegacyPriorityOutcome,
        PriorityStatus,
    )

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    job_ids = []
    for index in range(6):
        paths, job_id = seed_unknown_job(
            runtime_root,
            YESTERDAY,
            _CountingAdapter(),
            mapping=COMIX,
            url=f"https://qx.com/fair-priority-{index}.html",
        )
        with sqlite3.connect(paths.state_path) as connection:
            connection.execute(
                """UPDATE remote_jobs
                   SET state='PRIORITY_PENDING', remote_identity=? WHERE id=?""",
                (f"comix|fair-task-{job_id}|fair-number-{job_id}", job_id),
            )
            connection.execute(
                "UPDATE crawl_work SET state='PRIORITY_PENDING' WHERE remote_job_id=?",
                (job_id,),
            )
        seed_remote_candidate(
            prewarm,
            f"https://qx.com/fair-priority-bound-{index}.html",
            job_id,
            business_date=YESTERDAY,
            document=f"FAIR-{index}",
        )
        job_ids.append(job_id)

    class RecordingUnconfirmedAdapter(_CountingAdapter):
        def __init__(self):
            super().__init__()
            self.priority_task_ids = []

        def prioritize(self, job, *, task_id, task_number, timeout_seconds):
            self.prioritize_calls += 1
            self.priority_task_ids.append(task_id)
            return LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, weight_type=None)

    adapter = RecordingUnconfirmedAdapter()

    _run_cross_day(runtime_root, prewarm, adapter)
    first_pass_tasks = list(adapter.priority_task_ids)
    _run_cross_day(runtime_root, prewarm, adapter)
    second_pass_tasks = adapter.priority_task_ids[len(first_pass_tasks) :]

    assert first_pass_tasks == [f"fair-task-{job_id}" for job_id in job_ids[:4]]
    assert set(second_pass_tasks).issuperset(
        {f"fair-task-{job_id}" for job_id in job_ids[4:]}
    )
    assert all(
        _remote_job_row(paths.state_path, job_id)["state"] == "PRIORITY_PENDING"
        for job_id in job_ids
    )
    assert adapter.reconcile_calls == 0
    assert adapter.submit_calls == 0


def test_expired_absolute_deadline_defers_all_jobs_without_lock_or_remote(tmp_path):
    """Cross-day work must share the process deadline rather than reset it."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    _paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/budget-expired.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/budget-expired.html",
        job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter()

    summary = _run_cross_day(
        runtime_root,
        prewarm,
        adapter,
        deadline=5.0,
        monotonic_value=lambda: 5.0,
    )

    assert summary["cross_day_reconcile_attempted"] == 0
    assert summary["cross_day_reconcile_deferred"] == 1
    assert adapter.reconcile_calls == 0


def test_target_linkage_is_revalidated_before_historical_remote_call(
    tmp_path, monkeypatch
):
    """A candidate rebound after discovery must not trigger work on its old job."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    _old_paths, old_job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/rebound-old.html",
    )
    candidate_id = seed_remote_candidate(
        prewarm,
        "https://qx.com/rebound-old.html",
        old_job_id,
        business_date=YESTERDAY,
    )
    original_plan = cross_day.plan_cross_day_reconcile_jobs

    def plan_then_rebind(*args, **kwargs):
        planned = original_plan(*args, **kwargs)
        prewarm.record_candidate_result(
            candidate_id,
            cache_status="REMOTE_DISPATCHED",
            remote_job_id=999,
        )
        prewarm.record_remote_job_link(TODAY, candidate_id, 999, "test-rebind")
        return planned

    monkeypatch.setattr(cross_day, "plan_cross_day_reconcile_jobs", plan_then_rebind)
    adapter = _CountingAdapter(reconcile_outcome=RECONCILE_NOT_FOUND)

    summary = _run_cross_day(runtime_root, prewarm, adapter)

    assert adapter.reconcile_calls == 0
    assert summary["cross_day_reconcile_attempted"] == 0
    assert summary["cross_day_reconcile_deferred"] == 1


def test_linkage_rebound_during_remote_lookup_prevents_historical_write(tmp_path):
    """A remote result for a newly unbound old job must be discarded."""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, old_job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/rebound-during-reconcile.html",
    )
    candidate_id = seed_remote_candidate(
        prewarm,
        "https://qx.com/rebound-during-reconcile.html",
        old_job_id,
        business_date=YESTERDAY,
    )

    class RebindingAdapter(_CountingAdapter):
        def reconcile_unknown(self, job, *, timeout_seconds):
            outcome = super().reconcile_unknown(job, timeout_seconds=timeout_seconds)
            prewarm.record_candidate_result(
                candidate_id,
                cache_status="REMOTE_DISPATCHED",
                remote_job_id=999,
            )
            prewarm.record_remote_job_link(TODAY, candidate_id, 999, "test-rebind")
            return outcome

    adapter = RebindingAdapter(reconcile_outcome=RECONCILE_NOT_FOUND)

    summary = _run_cross_day(runtime_root, prewarm, adapter)

    assert adapter.reconcile_calls == 1
    assert summary["cross_day_reconcile_not_found"] == 0
    assert summary["cross_day_reconcile_deferred"] == 1
    historical = _remote_job_row(old_paths.state_path, old_job_id)
    assert historical["state"] == "SUBMIT_UNKNOWN"
    assert historical["recovery_attempts"] == 0
    assert not str(historical["remote_identity"] or "")


def test_linkage_rebound_during_priority_discards_proven_result(tmp_path):
    """A priority result for an unbound historical job must not advance state."""
    from app.services.legacy_zhaoshang_adapter import (
        LegacyPriorityOutcome,
        PriorityStatus,
    )

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, old_job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/rebound-during-priority.html",
    )
    candidate_id = seed_remote_candidate(
        prewarm,
        "https://qx.com/rebound-during-priority.html",
        old_job_id,
        business_date=YESTERDAY,
    )

    class RebindingPriorityAdapter(_CountingAdapter):
        def prioritize(self, job, *, task_id, task_number, timeout_seconds):
            self.prioritize_calls += 1
            if self.prioritize_calls == 1:
                return LegacyPriorityOutcome(
                    PriorityStatus.UNCONFIRMED,
                    weight_type=None,
                )
            prewarm.record_candidate_result(
                candidate_id,
                cache_status="REMOTE_DISPATCHED",
                remote_job_id=999,
            )
            prewarm.record_remote_job_link(TODAY, candidate_id, 999, "test-rebind")
            return LegacyPriorityOutcome(
                PriorityStatus.ALREADY_HIGHEST,
                weight_type=-1,
            )

    adapter = RebindingPriorityAdapter(reconcile_outcome=RECONCILE_FOUND)
    _run_cross_day(runtime_root, prewarm, adapter)

    summary = _run_cross_day(runtime_root, prewarm, adapter)

    historical = _remote_job_row(old_paths.state_path, old_job_id)
    assert historical["state"] == "PRIORITY_PENDING"
    assert {
        row["state"]
        for row in _state_rows(
            old_paths.state_path,
            "SELECT state FROM crawl_work WHERE remote_job_id=?",
            (old_job_id,),
        )
    } == {"PRIORITY_PENDING"}
    assert summary["cross_day_reconcile_deferred"] == 1
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 2
    assert adapter.submit_calls == 0


def test_linkage_rebound_after_found_discards_priority_result(tmp_path):
    """The same ownership fence must protect immediate FOUND confirmation."""
    from app.services.legacy_zhaoshang_adapter import (
        LegacyPriorityOutcome,
        PriorityStatus,
    )

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, old_job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/rebound-after-found.html",
    )
    candidate_id = seed_remote_candidate(
        prewarm,
        "https://qx.com/rebound-after-found.html",
        old_job_id,
        business_date=YESTERDAY,
    )

    class RebindingPriorityAdapter(_CountingAdapter):
        def prioritize(self, job, *, task_id, task_number, timeout_seconds):
            self.prioritize_calls += 1
            prewarm.record_candidate_result(
                candidate_id,
                cache_status="REMOTE_DISPATCHED",
                remote_job_id=999,
            )
            prewarm.record_remote_job_link(TODAY, candidate_id, 999, "test-rebind")
            return LegacyPriorityOutcome(
                PriorityStatus.ALREADY_HIGHEST,
                weight_type=-1,
            )

    adapter = RebindingPriorityAdapter(reconcile_outcome=RECONCILE_FOUND)

    summary = _run_cross_day(runtime_root, prewarm, adapter)

    assert summary["cross_day_reconciled_found"] == 1
    assert summary["cross_day_reconcile_deferred"] == 1
    assert _remote_job_row(old_paths.state_path, old_job_id)["state"] == "PRIORITY_PENDING"
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 1
    assert adapter.submit_calls == 0


def test_priority_commit_holds_linkage_fence_until_historical_write(
    tmp_path, monkeypatch
):
    """A rebind cannot linearize between final linkage check and DB commit."""
    from threading import Event, Thread

    from app.services.daily_audit_store import DailyAuditStore
    from app.services.legacy_zhaoshang_adapter import (
        LegacyPriorityOutcome,
        PriorityStatus,
    )

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, old_job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/priority-commit-fence.html",
    )
    candidate_id = seed_remote_candidate(
        prewarm,
        "https://qx.com/priority-commit-fence.html",
        old_job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter(
        reconcile_outcome=RECONCILE_FOUND,
        priority_outcomes=[
            LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, weight_type=None),
            LegacyPriorityOutcome(PriorityStatus.ALREADY_HIGHEST, weight_type=-1),
        ],
    )
    _run_cross_day(runtime_root, prewarm, adapter)

    original = DailyAuditStore.confirm_remote_job_priority_and_crawl_work
    writer_started = Event()
    writer_finished = Event()
    writer_threads = []
    writer_finished_before_commit = []

    def fenced_commit(store, token, job_id, *, remote_identity):
        def rebind():
            writer_started.set()
            prewarm.record_candidate_result(
                candidate_id,
                cache_status="REMOTE_DISPATCHED",
                remote_job_id=999,
            )
            prewarm.record_remote_job_link(TODAY, candidate_id, 999, "test-rebind")
            writer_finished.set()

        writer = Thread(target=rebind)
        writer_threads.append(writer)
        writer.start()
        assert writer_started.wait(1.0)
        writer_finished_before_commit.append(writer_finished.wait(0.1))
        return original(
            store,
            token,
            job_id,
            remote_identity=remote_identity,
        )

    monkeypatch.setattr(
        DailyAuditStore,
        "confirm_remote_job_priority_and_crawl_work",
        fenced_commit,
    )

    _run_cross_day(runtime_root, prewarm, adapter)
    for writer in writer_threads:
        writer.join(2.0)

    assert writer_finished_before_commit == [False]
    assert writer_finished.is_set()
    assert _remote_job_row(old_paths.state_path, old_job_id)["state"] == "SUBMITTED"
    assert adapter.reconcile_calls == 1
    assert adapter.submit_calls == 0


def test_found_commit_holds_linkage_fence_until_historical_write(
    tmp_path, monkeypatch
):
    """FOUND ownership cannot change between the fenced check and commit."""
    from threading import Event, Thread

    from app.services.daily_audit_store import DailyAuditStore

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, old_job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/found-commit-fence.html",
    )
    candidate_id = seed_remote_candidate(
        prewarm,
        "https://qx.com/found-commit-fence.html",
        old_job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter(reconcile_outcome=RECONCILE_FOUND)

    original = DailyAuditStore.reconcile_remote_job_and_crawl_work
    writer_started = Event()
    writer_finished = Event()
    writer_threads = []
    writer_finished_before_commit = []

    def fenced_commit(store, token, job_id, *, remote_identity, state):
        def rebind():
            writer_started.set()
            prewarm.record_candidate_result(
                candidate_id,
                cache_status="REMOTE_DISPATCHED",
                remote_job_id=999,
            )
            prewarm.record_remote_job_link(TODAY, candidate_id, 999, "test-rebind")
            writer_finished.set()

        writer = Thread(target=rebind)
        writer_threads.append(writer)
        writer.start()
        assert writer_started.wait(1.0)
        writer_finished_before_commit.append(writer_finished.wait(0.1))
        return original(
            store,
            token,
            job_id,
            remote_identity=remote_identity,
            state=state,
        )

    monkeypatch.setattr(
        DailyAuditStore,
        "reconcile_remote_job_and_crawl_work",
        fenced_commit,
    )

    _run_cross_day(runtime_root, prewarm, adapter)
    for writer in writer_threads:
        writer.join(2.0)

    assert writer_finished_before_commit == [False]
    assert writer_finished.is_set()
    historical_state = _remote_job_row(old_paths.state_path, old_job_id)["state"]
    assert historical_state in {"PRIORITY_PENDING", "SUBMITTED"}
    assert {
        row["state"]
        for row in _state_rows(
            old_paths.state_path,
            "SELECT state FROM crawl_work WHERE remote_job_id=?",
            (old_job_id,),
        )
    } == {historical_state}
    assert adapter.reconcile_calls == 1
    assert adapter.submit_calls == 0


def test_not_found_commit_holds_linkage_fence_until_attempt_write(
    tmp_path, monkeypatch
):
    """A stale negative lookup cannot increment recovery attempts."""
    from threading import Event, Thread

    from app.services.daily_audit_store import DailyAuditStore

    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    old_paths, old_job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/not-found-commit-fence.html",
    )
    candidate_id = seed_remote_candidate(
        prewarm,
        "https://qx.com/not-found-commit-fence.html",
        old_job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter(reconcile_outcome=RECONCILE_NOT_FOUND)

    original = DailyAuditStore.bump_remote_job_recovery_attempt
    writer_started = Event()
    writer_finished = Event()
    writer_threads = []
    writer_finished_before_commit = []

    def fenced_commit(store, token, job_id):
        def rebind():
            writer_started.set()
            prewarm.record_candidate_result(
                candidate_id,
                cache_status="REMOTE_DISPATCHED",
                remote_job_id=999,
            )
            prewarm.record_remote_job_link(TODAY, candidate_id, 999, "test-rebind")
            writer_finished.set()

        writer = Thread(target=rebind)
        writer_threads.append(writer)
        writer.start()
        assert writer_started.wait(1.0)
        writer_finished_before_commit.append(writer_finished.wait(0.1))
        return original(store, token, job_id)

    monkeypatch.setattr(
        DailyAuditStore,
        "bump_remote_job_recovery_attempt",
        fenced_commit,
    )

    _run_cross_day(runtime_root, prewarm, adapter)
    for writer in writer_threads:
        writer.join(2.0)

    assert writer_finished_before_commit == [False]
    assert writer_finished.is_set()
    historical = _remote_job_row(old_paths.state_path, old_job_id)
    assert historical["state"] == "SUBMIT_UNKNOWN"
    assert historical["recovery_attempts"] == 1
    assert adapter.reconcile_calls == 1
    assert adapter.submit_calls == 0


def test_plan_only_reports_cross_day_jobs_with_zero_write_lock_adapter_or_remote(
    tmp_path, monkeypatch
):
    """Adding cross-day planning must preserve the strict plan-only contract."""
    runtime_root = tmp_path / "runtime"
    prewarm_path = runtime_root / "customer_prewarm.sqlite3"
    cache_path = runtime_root / "shared_crawl_cache.sqlite3"
    prewarm = CustomerPrewarmStore(prewarm_path)
    cache = SharedCrawlCache(cache_path)
    today_paths = make_state_db(runtime_root, TODAY)
    old_paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/plan-only-cross-day.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/plan-only-cross-day.html",
        job_id,
        business_date=YESTERDAY,
    )
    _historical_paths, harvest_job_id = seed_remote_work(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/plan-only-old-harvest.html",
        job_state="SUBMITTED", remote_identity="comix|old-harvest|number",
    )
    seed_remote_candidate(
        prewarm, "https://qx.com/plan-only-old-harvest.html", harvest_job_id,
        business_date=YESTERDAY, document="PLAN-HARVEST",
    )

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    hashes = {
        path: digest(path)
        for path in (prewarm_path, cache_path, old_paths.state_path, today_paths.state_path)
    }

    class NoLock:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("plan-only constructed a ProcessLock")

    class NoAdapter:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("plan-only constructed an adapter")

    monkeypatch.setattr(dispatch_service, "ProcessLock", NoLock)
    monkeypatch.setattr(dispatch_service, "CnncLegacyZhaoshangAdapter", NoAdapter)

    summary = run_prewarm_dispatch_pass(
        CustomerPrewarmStore.open_read_only(prewarm_path),
        SharedCrawlCache.open_read_only(cache_path),
        business_date=TODAY,
        runtime_root=runtime_root,
        mappings=[COMIX],
        plan_only=True,
    )

    assert summary["lifecycle_planned"]["cross_day_reconcile_jobs"] == 1
    assert summary["lifecycle_planned"]["cross_day_harvest_jobs"] == 1
    assert all(digest(path) == before for path, before in hashes.items())
    assert not (runtime_root / TODAY / "incremental_audit.lock").exists()


def test_cli_runs_cross_day_after_today_locks_release_before_harvest_with_shared_deadline(
    tmp_path, monkeypatch
):
    """Moving cross-day work inside the pass would reintroduce nested day locks."""
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    make_state_db(runtime_root, TODAY)
    order = []
    observed = {}

    def fake_pass(*_args, **_kwargs):
        order.append("pass")
        return {"outcome": "OK", "phase_errors": []}

    def fake_cross_day(**kwargs):
        order.append("cross-day")
        observed["cross_deadline"] = kwargs["deadline"]
        today_lock = runtime_root / TODAY / "incremental_audit.lock"
        assert not ProcessLock.inspect(today_lock)["active"]

    def fake_harvest(*_args, **kwargs):
        order.append("harvest")
        observed["harvest_deadline"] = kwargs["deadline"]

    def fake_historical_harvest(**kwargs):
        order.append("historical-harvest")
        observed["historical_deadline"] = kwargs["deadline"]
        assert not ProcessLock.inspect(runtime_root / TODAY / "incremental_audit.lock")["active"]

    monkeypatch.setattr(cli, "run_prewarm_dispatch_pass", fake_pass)
    monkeypatch.setattr(cli, "run_cross_day_reconcile_phase", fake_cross_day)
    monkeypatch.setattr(cli, "run_cross_day_harvest_phase", fake_historical_harvest)
    monkeypatch.setattr(cli, "run_harvest_phase", fake_harvest)
    monkeypatch.setattr(cli, "load_zhaoshang_mall_mappings", lambda _path: [COMIX])

    exit_code = cli.main(
        [
            "--business-date",
            TODAY,
            "--runtime-root",
            str(runtime_root),
            "--budget-seconds",
            "10",
        ]
    )

    assert exit_code == 0
    assert order == ["pass", "cross-day", "historical-harvest", "harvest"]
    assert observed["cross_deadline"] == observed["historical_deadline"] == observed["harvest_deadline"]


def test_cli_no_lifecycle_skips_cross_day_phase(tmp_path, monkeypatch):
    """The existing lifecycle kill switch must cover the new phase too."""
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    monkeypatch.setattr(cli, "load_zhaoshang_mall_mappings", lambda _path: [COMIX])
    calls = []
    monkeypatch.setattr(
        cli,
        "run_cross_day_reconcile_phase",
        lambda **_kwargs: calls.append("cross-day"),
    )
    monkeypatch.setattr(
        cli,
        "run_cross_day_harvest_phase",
        lambda **_kwargs: calls.append("historical-harvest"),
    )

    assert cli.main(
        [
            "--business-date",
            TODAY,
            "--runtime-root",
            str(runtime_root),
            "--no-lifecycle",
        ]
    ) == 0
    assert calls == []
