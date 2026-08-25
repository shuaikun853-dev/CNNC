"""Offline crash, rollover, and real-process overlap regressions."""

from __future__ import annotations

from datetime import UTC, datetime
import multiprocessing
from queue import Empty
import time

import pytest

import app.services.incremental_audit_runtime as runtime_module
from app.services.daily_audit_store import (
    DailyAuditStore,
    LeaseToken,
    ProcessLock,
    StaleLease,
)
from app.services.incremental_audit_runtime import _detail_worker
from app.services.saas_audit_core import detail_fingerprint
from app.services.incremental_audit_runtime import runtime_paths
from app.workflows.incremental_price_audit import (
    CrawlMember,
    CrawlPlan,
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
)


BUSINESS_DATE = "2026-08-11"


def _row(index: int = 1) -> dict[str, str]:
    return {
        "vCommodityId": f"document-{index}",
        "skuId": f"sku-{index}",
        "extLink": f"https://shop.example/items/{index}",
        "queryStr": f"customer title {index}",
        "skuName": f"sku title {index}",
        "fourCategoryId": "category-1",
        "list_reference_price": "10",
    }


def _config(business_date: str = BUSINESS_DATE, *, model_batch_size: int = 10) -> TickConfig:
    return TickConfig(
        business_date=business_date,
        runtime_budget_seconds=60,
        heavy_work_reserve_seconds=1,
        lease_ttl_seconds=60,
        model_batch_size=model_batch_size,
        cutoff_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
        if business_date == BUSINESS_DATE else None,
    )


def _dependencies(
    rows,
    *,
    detail=None,
    model_batch=None,
    observed_at=None,
) -> TickDependencies:
    snapshot_rows = [dict(row) for row in rows]
    return TickDependencies(
        discover=lambda _timeout: DiscoveredState(
            observed_at=observed_at
            or datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
            headers=tuple(snapshot_rows[0]) if snapshot_rows else (),
            rows=snapshot_rows,
            evidence={
                "saas_count": len(snapshot_rows),
                "customer_count": len(snapshot_rows),
            },
        ),
        harvest=lambda _jobs, _timeout: {},
        detail=detail or (lambda _row_value, _timeout: {"customerPrice": "10"}),
        model_batch=model_batch or (
            lambda batch, _timeout: {
                index: {"match_result": "SAME"}
                for index, _row_value in enumerate(batch)
            }
        ),
        wall_clock=lambda: datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
    )


def _hold_crashed_claim(
    state_path: str,
    lock_path: str,
    ready,
    release,
    token_queue,
) -> None:
    store = DailyAuditStore(state_path)
    lock = ProcessLock(lock_path).acquire()
    token = store.acquire_lease(
        BUSINESS_DATE,
        "crashing-child",
        ttl_seconds=600,
        allow_unexpired_takeover=True,
        process_lock=lock,
    )
    row = _row()
    item_id = store.ingest_snapshot(token, [row])["members"][0]["audit_item_id"]
    work = store.ensure_stage_work(
        token,
        item_id,
        "DETAIL",
        detail_fingerprint(row, BUSINESS_DATE),
    )
    store.claim_stage_work(token, work["id"], max_attempts=3, max_elapsed_seconds=300)
    token_queue.put((token.run_id, token.business_date, token.owner, token.generation))
    ready.set()
    release.wait(120)


def _run_blocking_detail_tick(
    state_path: str,
    lock_path: str,
    entered,
    release,
    active,
    peak,
    outcome_queue,
) -> None:
    def detail(_row_value, _timeout):
        with active.get_lock():
            active.value += 1
            peak.value = max(peak.value, active.value)
        entered.set()
        release.wait(120)
        with active.get_lock():
            active.value -= 1
        return {"customerPrice": "10"}

    summary = run_incremental_tick(
        DailyAuditStore(state_path),
        ProcessLock(lock_path),
        _config(),
        _dependencies([_row()], detail=detail),
    )
    outcome_queue.put(summary.outcome)


def _run_real_detail_worker(
    detail_lock_path: str,
    started,
    waited_for_lock,
    release,
    active,
    peak,
    outcome_queue,
) -> None:
    import app.services.incremental_audit_runtime as runtime

    def fake_detail_api(_template, _document_id, _category_id, timeout=0):
        with active.get_lock():
            active.value += 1
            peak.value = max(peak.value, active.value)
        started.set()
        release.wait(120)
        with active.get_lock():
            active.value -= 1
        return {"ok": True, "payload": {"data": {"referencePrice": "10"}}}

    def wait_for_lock(seconds):
        waited_for_lock.set()
        time.sleep(seconds)

    runtime.call_detail_api = fake_detail_api
    detail = runtime._detail_worker(
        {},
        detail_lock_path=detail_lock_path,
        request_interval_seconds=0,
        sleep=wait_for_lock,
    )
    result = detail(_row(), 30)
    outcome_queue.put(result["customerPriceStatus"])


def _process_context():
    # Spawn exercises Windows production semantics even when CI is not Windows.
    return multiprocessing.get_context("spawn")


def test_real_process_overlap_and_crash_takeover_are_fenced(tmp_path):
    context = _process_context()
    state_path = tmp_path / "state.sqlite3"
    lock_path = tmp_path / "tick.lock"
    ready = context.Event()
    release = context.Event()
    token_queue = context.Queue()
    child = context.Process(
        target=_hold_crashed_claim,
        args=(str(state_path), str(lock_path), ready, release, token_queue),
    )
    child.start()
    try:
        assert ready.wait(15), "child did not acquire the process lock"
        old_token_values = token_queue.get(timeout=5)
        overlapping = run_incremental_tick(
            DailyAuditStore(state_path),
            ProcessLock(lock_path),
            _config(),
            _dependencies([_row()]),
        )
        assert overlapping.outcome == "ALREADY_RUNNING"
    finally:
        child.terminate()
        child.join(15)
    assert not child.is_alive()

    store = DailyAuditStore(state_path)
    resumed = run_incremental_tick(
        store,
        ProcessLock(lock_path),
        _config(),
        _dependencies([_row()]),
    )
    assert resumed.counters["recovered_stage_claims"] == 1
    old_token = LeaseToken(*old_token_values)
    with pytest.raises(StaleLease):
        store.update_daily_metrics(old_token, {"must_not_commit": 1})


def test_same_day_process_overlap_keeps_detail_peak_at_one(tmp_path):
    context = _process_context()
    state_path = tmp_path / "state.sqlite3"
    lock_path = tmp_path / "tick.lock"
    entered = context.Event()
    release = context.Event()
    active = context.Value("i", 0)
    peak = context.Value("i", 0)
    outcomes = context.Queue()
    first = context.Process(
        target=_run_blocking_detail_tick,
        args=(str(state_path), str(lock_path), entered, release, active, peak, outcomes),
    )
    first.start()
    try:
        assert entered.wait(15), "first tick never entered Detail"
        second = run_incremental_tick(
            DailyAuditStore(state_path),
            ProcessLock(lock_path),
            _config(),
            _dependencies([_row(2)]),
        )
        assert second.outcome == "ALREADY_RUNNING"
    finally:
        release.set()
        first.join(15)
        if first.is_alive():
            first.terminate()
            first.join(5)
    assert first.exitcode == 0
    try:
        assert outcomes.get(timeout=5) == "COMPLETED"
    except Empty as exc:  # pragma: no cover - diagnostic for process failures.
        raise AssertionError("first tick did not return an outcome") from exc
    assert peak.value == 1
    assert active.value == 0


def test_cross_date_real_detail_workers_share_one_process_lock(tmp_path):
    context = _process_context()
    runtime_root = tmp_path / "incremental"
    first_path = runtime_paths("2026-08-11", runtime_root).detail_lock_path
    second_path = runtime_paths("2026-08-12", runtime_root).detail_lock_path
    assert first_path == second_path
    first_started = context.Event()
    second_started = context.Event()
    first_waited = context.Event()
    second_waited = context.Event()
    release = context.Event()
    active = context.Value("i", 0)
    peak = context.Value("i", 0)
    outcomes = context.Queue()
    first = context.Process(
        target=_run_real_detail_worker,
        args=(
            str(first_path), first_started, first_waited, release,
            active, peak, outcomes,
        ),
    )
    second = context.Process(
        target=_run_real_detail_worker,
        args=(
            str(second_path), second_started, second_waited, release,
            active, peak, outcomes,
        ),
    )
    first.start()
    try:
        assert first_started.wait(15), "first Detail worker never entered the request"
        second.start()
        assert second_waited.wait(15), "second Detail worker did not wait on the global lock"
        assert second_started.is_set() is False
        assert active.value == 1
        assert peak.value == 1
    finally:
        release.set()
        first.join(15)
        second.join(15)
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(5)
    assert first.exitcode == 0
    assert second.exitcode == 0
    assert sorted(outcomes.get(timeout=5) for _ in range(2)) == [
        "FOUND_SINGLE_REFERENCE",
        "FOUND_SINGLE_REFERENCE",
    ]
    assert peak.value == 1
    assert active.value == 0


def test_shared_runtime_detail_lock_serializes_workers_from_two_business_dates(tmp_path, monkeypatch):
    lock_path = tmp_path / "incremental" / "detail.lock"
    active = 0
    peak = 0
    calls = 0

    def detail_api(_template, _document_id, _category_id, *, timeout):
        nonlocal active, peak, calls
        active += 1
        peak = max(peak, active)
        calls += 1
        active -= 1
        return {"ok": True, "payload": {"referenceInfos": [{"referencePrice": "10"}]}}

    monkeypatch.setattr(runtime_module, "call_detail_api", detail_api)
    first = _detail_worker({}, detail_lock_path=lock_path, request_interval_seconds=0)
    second = _detail_worker({}, detail_lock_path=lock_path, request_interval_seconds=0)
    row = {"vCommodityId": "v1", "skuId": "s1", "fourCategoryId": "c"}

    first(row, 5)
    second(row, 5)

    assert calls == 2
    assert peak == 1


def test_new_day_does_not_recover_old_day_claim_until_explicit_old_day_tick(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    old_row = _row()
    old_token = store.acquire_lease(BUSINESS_DATE, "seed-old", ttl_seconds=60)
    item_id = store.ingest_snapshot(old_token, [old_row])["members"][0]["audit_item_id"]
    work = store.ensure_stage_work(
        old_token,
        item_id,
        "DETAIL",
        detail_fingerprint(old_row, BUSINESS_DATE),
    )
    store.claim_stage_work(old_token, work["id"], max_attempts=3, max_elapsed_seconds=300)
    store.release_lease(old_token)

    next_day = "2026-08-12"
    next_summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "next-day.lock"),
        _config(next_day),
        _dependencies(
            [_row(2)],
            observed_at=datetime(2026, 8, 12, 1, 0, tzinfo=UTC),
        ),
    )
    assert next_summary.counters["recovered_stage_claims"] == 0
    assert store.get_stage_work(work["id"])["state"] == "CLAIMED"

    old_summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "old-day.lock"),
        _config(),
        _dependencies([old_row]),
    )
    assert old_summary.counters["recovered_stage_claims"] == 1
    assert store.get_stage_work(work["id"])["state"] == "SUCCESS"
    assert store.get_run(BUSINESS_DATE)["id"] != store.get_run(next_day)["id"]


def test_one_bad_model_batch_is_contained_and_later_batches_advance(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    calls = 0

    def model_batch(batch, _timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("malformed response for this batch")
        return {
            index: {"match_result": "SAME"}
            for index, _row_value in enumerate(batch)
        }

    summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        _config(model_batch_size=3),
        _dependencies([_row(index) for index in range(1, 10)], model_batch=model_batch),
    )
    token = store.acquire_lease(BUSINESS_DATE, "inspect", ttl_seconds=60)
    model_work = store.list_stage_work(token, stage="MODEL")
    store.release_lease(token)

    assert calls == 3
    assert sum(work["state"] == "TERMINAL_FAILED" for work in model_work) == 3
    assert sum(work["state"] == "SUCCESS" for work in model_work) == 6
    assert summary.outcome == "MANUAL_REVIEW_REQUIRED"


def test_post_response_crash_becomes_unknown_and_reconciles_without_second_post(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    post_calls = 0

    def plan(items, _timeout):
        members = tuple(
            CrawlMember(
                "mall-a",
                str(item["row"]["vCommodityId"]),
                str(item["row"]["skuId"]),
                str(item["row"]["extLink"]),
                "test-v1",
                item["row"],
            )
            for item in items
        )
        return [
            CrawlPlan(
                "mall-a",
                "logical-job-a",
                "task-a",
                "payload-a",
                payload_json={"fixture": True},
                members=members,
            )
        ]

    def submit(_job, _crawls, _timeout):
        nonlocal post_calls
        post_calls += 1
        return {"remote_identity": "remote-created-before-crash", "state": "SUBMITTED"}

    crashing = _dependencies([_row()])
    crashing.plan_crawl = plan
    crashing.submit_crawl = submit
    original_reconcile = store.reconcile_remote_job
    first_reconcile = True

    def crash_before_local_reconcile(*args, **kwargs):
        nonlocal first_reconcile
        if first_reconcile:
            first_reconcile = False
            raise SystemExit("crash after server response")
        return original_reconcile(*args, **kwargs)

    store.reconcile_remote_job = crash_before_local_reconcile
    with pytest.raises(SystemExit):
        run_incremental_tick(
            store,
            ProcessLock(tmp_path / "tick.lock"),
            _config(),
            crashing,
        )
    store.reconcile_remote_job = original_reconcile

    def harvest(jobs, _timeout):
        return {
            int(job["id"]): {
                "state": "COMPLETED",
                "remote_identity": "remote-created-before-crash",
                "crawl_results": {
                    int(crawl["id"]): {"page_price_value": "9.9"}
                    for crawl in job["crawl_work"]
                },
            }
            for job in jobs
        }

    resumed = _dependencies([_row()])
    resumed.plan_crawl = plan
    resumed.submit_crawl = lambda *_args: pytest.fail("must not blindly POST again")
    resumed.harvest = harvest
    summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        _config(),
        resumed,
    )

    token = store.acquire_lease(BUSINESS_DATE, "inspect-final", ttl_seconds=60)
    jobs = store.list_remote_jobs(token)
    crawls = store.list_crawl_work(token)
    store.release_lease(token)
    assert post_calls == 1
    assert summary.counters["recovered_submit_unknown"] == 1
    assert len(jobs) == 1 and jobs[0]["state"] == "COMPLETED"
    assert all(crawl["state"] == "SUCCESS" for crawl in crawls)


def test_freeze_commit_crash_is_idempotently_recovered(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    observed = _dependencies(
        [_row()], observed_at=datetime(2026, 8, 11, 9, 51, tzinfo=UTC)
    )
    run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        _config(),
        observed,
    )
    snapshot_id = int(store.latest_snapshot(BUSINESS_DATE)["id"])
    # The finalize observation repeats identical content ten minutes later, so
    # the source-stability window is satisfied by the two authoritative rows.
    dependencies = _dependencies(
        [_row()], observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC)
    )
    original_freeze = store.freeze_snapshot

    def freeze_then_crash(token, candidate_snapshot_id):
        original_freeze(token, candidate_snapshot_id)
        raise SystemExit("crash after freeze commit")

    store.freeze_snapshot = freeze_then_crash
    with pytest.raises(SystemExit):
        run_incremental_tick(
            store,
            ProcessLock(tmp_path / "tick.lock"),
            _config(),
            dependencies,
            finalize=True,
        )
    store.freeze_snapshot = original_freeze

    resumed = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        _config(),
        dependencies,
        finalize=True,
    )
    frozen_snapshot = store.list_snapshots(BUSINESS_DATE)[-1]
    assert snapshot_id < int(frozen_snapshot["id"])
    assert resumed.frozen_snapshot_id == int(frozen_snapshot["id"])
    assert store.get_run(BUSINESS_DATE)["frozen_snapshot_id"] == int(frozen_snapshot["id"])
