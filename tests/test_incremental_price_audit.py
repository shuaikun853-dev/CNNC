"""Offline contracts for the finite incremental price-audit tick."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.saas_audit_core import build_records
from app.workflows.work_01_saas_approval_json_audit import audit_saas_approval_records
from openpyxl import load_workbook
import scripts.audit_saas_excel_file as legacy_excel
from app.workflows.incremental_price_audit import (
    CrawlPlan,
    CrawlMember,
    DiscoveredState,
    TickConfig,
    TickDependencies,
    _safe_error,
    run_incremental_tick,
)


@dataclass
class Clock:
    value: float = 0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _row(v="doc-1", sku="sku-1", **extra):
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": " https://shop.example/item ",
        "fourCategoryId": "cat",
        "list_reference_price": "10",
        "queryStr": "title",
        "skuName": "title",
        "mall_key": "mall-a",
        **extra,
    }


def _config(clock, *, date="2026-08-11", budget=60, reserve=10):
    return TickConfig(
        business_date=date,
        runtime_budget_seconds=budget,
        heavy_work_reserve_seconds=reserve,
        lease_ttl_seconds=90,
        cutoff_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
    )


def _deps(events, rows, clock, *, post_raises=False):
    def harvest(jobs, timeout):
        events.append("harvest")
        return {}

    def discover(timeout):
        events.append("discover")
        return DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=rows,
            source_fingerprints=[f"source-{row_value['vCommodityId']}-{row_value['skuId']}" for row_value in rows],
            customer_fingerprints=[f"customer-{row_value['fourCategoryId']}-{row_value['list_reference_price']}" for row_value in rows],
        )

    def detail(row, timeout):
        events.append(f"detail:{row['vCommodityId']}")
        return {"customerPrice": "10"}

    def model(row, timeout):
        events.append(f"model:{row['vCommodityId']}")
        return {"match_result": "SAME"}

    def plan(items, timeout):
        members = [
            CrawlMember(
                mall_key=item["row"]["mall_key"], source_id=item["row"]["vCommodityId"], sku_id=item["row"]["skuId"],
                exact_url=item["row"]["extLink"], backend_version="legacy-v1", row=item["row"],
            )
            for item in items
        ]
        return [CrawlPlan(
            mall_key="mall-a", logical_key="mall-a:batch", task_name="tick-mall-a", payload_hash="payload-a",
            payload_json={"mall": "mall-a"}, members=members,
        )]

    def submit(job, crawl_items, timeout):
        events.append("submit")
        if post_raises:
            raise TimeoutError("post timed out")
        return {"remote_identity": "task-1", "state": "SUBMITTED"}

    def finalize(plan, members, timeout):
        payload = {"members": [member.sku_id for member in members]}
        suffix = "-".join(payload["members"])
        return CrawlPlan(
            mall_key=plan.mall_key,
            logical_key=f"{plan.mall_key}:{suffix}",
            task_name=f"tick-{suffix}",
            payload_hash=f"payload-{suffix}",
            payload_json=payload,
            members=tuple(members),
        )

    return TickDependencies(
        discover=discover, harvest=harvest, detail=detail, model=model, plan_crawl=plan,
        submit_crawl=submit, finalize_crawl_plan=finalize, monotonic=clock.monotonic, wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
    )


def test_tick_order_idempotency_and_duplicate_membership(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    events: list[str] = []
    clock = Clock()
    deps = _deps(events, [_row(), _row()], clock)
    config = _config(clock)

    first = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)
    second = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)

    assert first.outcome == "WAITING_REMOTE"
    assert list(first.phase_trace[:4]) == ["recover", "harvest", "discover", "detail"]
    assert events.count("harvest") == 2
    assert events.count("detail:doc-1") == 1
    assert events.count("model:doc-1") == 1
    assert events.count("submit") == 1
    snapshot = store.latest_snapshot("2026-08-11")
    assert snapshot is not None
    assert len(store.ordered_snapshot_items(snapshot["id"])) == 2
    assert second.counters["detail_requests"] == 0


def test_overlap_returns_before_dependencies_and_next_date_is_independent(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    events: list[str] = []
    clock = Clock()
    lock = ProcessLock(tmp_path / "tick.lock").acquire()
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), _deps(events, [_row()], clock))
    lock.release()
    assert summary.outcome == "ALREADY_RUNNING"
    assert events == []
    run_incremental_tick(store, ProcessLock(tmp_path / "other.lock"), _config(clock, date="2026-08-12"), _deps(events, [_row()], clock))
    assert store.get_run("2026-08-11") is None
    assert store.get_run("2026-08-12") is not None


def test_crash_claim_is_recovered_and_budget_retains_backlog(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    events: list[str] = []
    clock = Clock()
    deps = _deps(events, [_row()], clock)
    original_discover = deps.discover

    def late_discover(timeout):
        value = original_discover(timeout)
        clock.advance(55)
        return value

    deps.discover = late_discover
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock, budget=60, reserve=10), deps)
    assert summary.outcome == "BUDGET_EXHAUSTED"
    assert events == ["harvest", "discover"]
    token = store.acquire_lease("2026-08-11", "test", 30)
    item = store.ordered_snapshot_items(store.latest_snapshot("2026-08-11")["id"])[0]
    work = store.ensure_stage_work(token, item["id"], "DETAIL", "manual-crash")
    store.claim_stage_work(token, work["id"], max_attempts=1, max_elapsed_seconds=10)
    store.release_lease(token)
    clock.value = 0
    resumed = run_incremental_tick(store, ProcessLock(tmp_path / "tick2.lock"), _config(clock), deps)
    assert resumed.counters["recovered_stage_claims"] == 1


def test_submit_timeout_is_unknown_and_never_blindly_resent(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    events: list[str] = []
    clock = Clock()
    config = _config(clock)
    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, _deps(events, [_row()], clock, post_raises=True))
    assert events.count("submit") == 1
    rerun = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, _deps(events, [_row()], clock, post_raises=True))
    assert events.count("submit") == 1
    inspect = store.acquire_lease("2026-08-11", "inspect", 30)
    assert store.list_remote_jobs(inspect, states={"SUBMIT_UNKNOWN"})
    store.release_lease(inspect)


def test_stage_limits_and_finalize_freeze_behavior(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    deps = _deps(events, [_row()], clock)
    before = TickDependencies(**{**deps.__dict__, "wall_clock": lambda: datetime(2026, 8, 11, 9, 59, tzinfo=UTC)})
    config = _config(clock)
    assert run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, before, finalize=True).outcome == "FINALIZE_NOT_READY"
    final = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps, finalize=True)
    frozen = final.frozen_snapshot_id
    assert frozen is not None
    changed = _deps(events, [_row("doc-2", "sku-2")], clock)
    rerun = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, changed, finalize=True)
    assert rerun.frozen_snapshot_id == frozen
    assert events.count("discover") == 1


def test_customer_change_reprocesses_changed_item_and_elapsed_cap_is_terminal(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    config = TickConfig(
        business_date="2026-08-11", runtime_budget_seconds=60, heavy_work_reserve_seconds=5,
        lease_ttl_seconds=30, detail_max_attempts=1, detail_max_elapsed_seconds=1,
        model_max_attempts=1, model_max_elapsed_seconds=1,
    )
    deps = _deps(events, [_row()], clock)

    def slow_detail(row, timeout):
        clock.advance(2)
        return {"late": True}

    deps.detail = slow_detail
    first = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)
    token = store.acquire_lease("2026-08-11", "inspect", 30)
    assert store.list_stage_work(token, stage="DETAIL")[0]["state"] == "TERMINAL_FAILED"
    store.release_lease(token)
    clock.value = 0
    changed = _deps(events, [_row(list_reference_price="11")], clock)
    changed.detail = lambda row, timeout: {"customerPrice": "11"}
    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, changed)
    token = store.acquire_lease("2026-08-11", "inspect-2", 30)
    assert len(store.list_stage_work(token, stage="DETAIL")) == 2
    store.release_lease(token)
    assert first.counters["detail_requests"] == 0


def test_unexpected_discovery_error_releases_lock_and_lease(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    deps = _deps(events, [_row()], clock)
    deps.discover = lambda timeout: (_ for _ in ()).throw(RuntimeError("discovery exploded"))
    lock = ProcessLock(tmp_path / "tick.lock")
    failed = run_incremental_tick(store, lock, _config(clock), deps)
    assert failed.outcome == "FAILED"
    assert lock.held is False
    healthy = run_incremental_tick(store, lock, _config(clock), _deps(events, [_row()], clock))
    assert healthy.outcome == "WAITING_REMOTE"


def test_hard_post_crash_is_recovered_unknown_without_resend_and_zero_budget_calls_nothing(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    deps = _deps(events, [_row()], clock)
    deps.submit_crawl = lambda job, rows, timeout: (_ for _ in ()).throw(SystemExit("hard stop"))
    with pytest.raises(SystemExit):
        run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps)
    resumed = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), _deps(events, [_row()], clock))
    assert resumed.counters["recovered_submit_unknown"] == 1
    assert events.count("submit") == 0
    zero_events: list[str] = []
    zero = TickConfig(business_date="2026-08-12", runtime_budget_seconds=0, heavy_work_reserve_seconds=0, lease_ttl_seconds=1)
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "zero.lock"), zero, _deps(zero_events, [_row()], clock))
    assert summary.outcome == "BUDGET_EXHAUSTED"
    assert zero_events == []


def test_finalize_freezes_earliest_existing_post_cutoff_snapshot_and_persists_evidence(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    store.ensure_run("2026-08-11", cutoff_at="2026-08-11T10:00:00+00:00")
    token = store.acquire_lease("2026-08-11", "seed", 30)
    earliest = store.ingest_snapshot(
        token, [_row("early", "s-1")], "2026-08-11T10:01:00+00:00",
        headers=["vCommodityId"], evidence={"snapshot_path": "safe/path"},
    )["snapshot"]
    store.ingest_snapshot(token, [_row("later", "s-2")], "2026-08-11T10:02:00+00:00")
    store.release_lease(token)
    events: list[str] = []
    final = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), _deps(events, [_row("new", "s-3")], clock), finalize=True)
    assert final.frozen_snapshot_id == earliest["id"]
    assert events == ["harvest", "detail:early", "model:early", "submit"]
    assert store.get_snapshot(earliest["id"])["headers_json"] == '["vCommodityId"]'


def test_unchanged_snapshot_is_observed_again_and_can_freeze_across_cutoff(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    config = _config(clock)
    row = _row()
    first = _deps([], [row], clock)
    first.discover = lambda _timeout: DiscoveredState(
        observed_at=datetime(2026, 8, 11, 9, 59, tzinfo=UTC),
        headers=["vCommodityId"],
        rows=[row],
        source_fingerprints=["same-source"],
        customer_fingerprints=["same-customer"],
    )
    second = _deps([], [row], clock)
    second.discover = lambda _timeout: DiscoveredState(
        observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
        headers=["vCommodityId"],
        rows=[row],
        source_fingerprints=["same-source"],
        customer_fingerprints=["same-customer"],
    )

    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, first)
    finalized = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        config,
        second,
        finalize=True,
    )

    snapshots = store.list_snapshots("2026-08-11")
    assert len(snapshots) == 2
    assert snapshots[0]["ordered_hash"] == snapshots[1]["ordered_hash"]
    assert snapshots[0]["observed_at"] == "2026-08-11T09:59:00+00:00"
    assert snapshots[1]["observed_at"] == "2026-08-11T10:01:00+00:00"
    assert finalized.frozen_snapshot_id == snapshots[1]["id"]


def test_persisted_external_error_is_type_only_and_does_not_store_secrets():
    message = _safe_error(RuntimeError("https://service.invalid/path?token=do-not-store"))

    assert message == "RuntimeError"
    assert "do-not-store" not in message


def test_hard_stage_crash_remains_claimed_then_next_tick_recovers(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    crashing = _deps(events, [_row()], clock)
    crashing.detail = lambda row, timeout: (_ for _ in ()).throw(SystemExit("hard stop"))
    with pytest.raises(SystemExit):
        run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), crashing)
    token = store.acquire_lease("2026-08-11", "inspect", 30)
    assert store.list_stage_work(token, stage="DETAIL")[0]["state"] == "CLAIMED"
    store.release_lease(token)
    resumed = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), _deps(events, [_row()], clock))
    assert resumed.counters["recovered_stage_claims"] == 1


def test_unknown_submit_harvest_reconciles_exact_identity_and_persists_crawl_success(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    config = _config(clock)
    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, _deps(events, [_row()], clock, post_raises=True))
    token = store.acquire_lease("2026-08-11", "inspect", 30)
    job = store.list_remote_jobs(token, states={"SUBMIT_UNKNOWN"})[0]
    crawl = store.crawl_work_for_job(job["id"])[0]
    store.release_lease(token)
    deps = _deps(events, [_row()], clock)
    deps.harvest = lambda jobs, timeout: {
        job["id"]: {"state": "COMPLETED", "remote_identity": "exact-task", "crawl_results": {crawl["id"]: {"price": "10"}}}
    }
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)
    token = store.acquire_lease("2026-08-11", "verify", 30)
    assert store.get_remote_job(job["id"])["remote_identity"] == "exact-task"
    assert store.get_crawl_work(crawl["id"])["state"] == "SUCCESS"
    store.release_lease(token)
    assert summary.outcome in {"COMPLETED", "PENDING_RETRY"}


def test_harvest_exception_is_bounded_and_becomes_manual_not_tick_failure(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    config = TickConfig(business_date="2026-08-11", runtime_budget_seconds=60, heavy_work_reserve_seconds=5, lease_ttl_seconds=30, harvest_max_attempts=1, harvest_max_elapsed_seconds=1)
    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, _deps(events, [_row()], clock))
    failing = _deps(events, [_row()], clock)
    failing.harvest = lambda jobs, timeout: (_ for _ in ()).throw(TimeoutError("harvest timeout"))
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, failing)
    token = store.acquire_lease("2026-08-11", "verify", 30)
    assert store.list_remote_jobs(token)[0]["state"] == "MANUAL_REQUIRED"
    store.release_lease(token)
    assert summary.outcome == "MANUAL_REVIEW_REQUIRED"


def test_second_tick_submits_only_delta_and_preserves_completed_old_crawl(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    submitted: list[list[str]] = []
    first_rows = [_row("old", "old-sku"), _row("old", "old-sku")]
    second_rows = [*first_rows, _row("new", "new-sku")]

    def dependencies(rows, harvest=None):
        deps = _deps([], rows, clock)
        def plan(items, timeout):
            members = [CrawlMember("mall-a", item["row"]["vCommodityId"], item["row"]["skuId"], item["row"]["extLink"], "legacy-v1", item["row"]) for item in items]
            suffix = str(len(members))
            return [CrawlPlan("mall-a", f"mall-a:{suffix}", f"task-{suffix}", f"payload-{suffix}", members=members)]
        deps.plan_crawl = plan
        deps.submit_crawl = lambda job, crawls, timeout: (submitted.append([crawl["sku_id"] for crawl in crawls]) or {"remote_identity": f"remote-{job['id']}", "state": "SUBMITTED"})
        if harvest is not None:
            deps.harvest = harvest
        return deps

    config = _config(clock)
    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, dependencies(first_rows))
    token = store.acquire_lease("2026-08-11", "inspect", 30)
    old_job = store.list_remote_jobs(token)[0]
    old_crawl = store.crawl_work_for_job(old_job["id"])[0]
    store.release_lease(token)
    harvest = lambda jobs, timeout: {old_job["id"]: {"state": "COMPLETED", "remote_identity": old_job["remote_identity"], "crawl_results": {old_crawl["id"]: {"price": "10"}}}}
    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, dependencies(second_rows, harvest))
    token = store.acquire_lease("2026-08-11", "verify", 30)
    assert submitted == [["old-sku"], ["new-sku"]]
    assert store.get_crawl_work(old_crawl["id"])["remote_job_id"] == old_job["id"]
    assert store.get_crawl_work(old_crawl["id"])["state"] == "SUCCESS"
    jobs = store.list_remote_jobs(token)
    assert len(jobs) == 2
    new_job = next(job for job in jobs if job["id"] != old_job["id"])
    assert new_job["payload_hash"] == "payload-new-sku"
    assert new_job["task_name"] == "tick-new-sku"
    assert new_job["payload_json"] == '{"members":["new-sku"]}'
    store.release_lease(token)


def test_frozen_ordered_duplicates_materialize_equivalent_legacy_record_results(tmp_path):
    rows = [_row("doc", "sku"), _row("doc", "sku"), _row("other", "other-sku")]
    enriched = [{"customerPrice": "10", "customerPriceStatus": "EXISTS", "customerPriceSource": "fixture", "customerPriceWarning": "", "customerPriceEvidence": ""} for _ in rows]
    records = build_records(rows, enriched)
    response = audit_saas_approval_records(records, use_model=False, use_link_audit=False, crawl_results=[], use_crawl_result=False)
    direct = tmp_path / "direct.xlsx"
    legacy_excel.write_audit_workbook(direct, list(rows[0]), rows, enriched, response)
    store = DailyAuditStore(tmp_path / "state.sqlite")
    token = store.acquire_lease("2026-08-11", "materialize", 30)
    snapshot = store.ingest_snapshot(token, rows)["snapshot"]
    store.freeze_snapshot(token, snapshot["id"])
    frozen_rows = [item["row"] for item in store.ordered_snapshot_items(snapshot["id"])]
    frozen_records = build_records(frozen_rows, enriched)
    frozen_response = audit_saas_approval_records(frozen_records, use_model=False, use_link_audit=False, crawl_results=[], use_crawl_result=False)
    materialized = tmp_path / "materialized.xlsx"
    legacy_excel.write_audit_workbook(materialized, list(rows[0]), frozen_rows, enriched, frozen_response)
    def values(path):
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb["record_results"]
            return [tuple(row) for row in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    assert values(materialized) == values(direct)


def test_harvest_at_reserve_boundary_checkpoints_without_discovery_or_new_work(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    deps = _deps(events, [_row()], clock)
    def exhausting_harvest(jobs, timeout):
        events.append("harvest")
        clock.advance(50)
        return {}
    deps.harvest = exhausting_harvest
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock, budget=60, reserve=10), deps, finalize=True)
    assert summary.outcome == "BUDGET_EXHAUSTED"
    assert summary.phase_trace == ("recover", "harvest", "checkpoint")
    assert events == ["harvest"]
    assert store.get_run("2026-08-11") is not None


def test_post_discovery_reserve_stops_stage_preprocessing_but_counts_uninitialized_backlog(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    rows = [_row(f"doc-{index}", f"sku-{index}") for index in range(3)]
    deps = _deps([], rows, clock)
    original_discover = deps.discover

    def exhausting_discover(timeout):
        discovered = original_discover(timeout)
        clock.advance(55)
        return discovered

    deps.discover = exhausting_discover

    summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        _config(clock, budget=60, reserve=10),
        deps,
    )

    token = store.acquire_lease("2026-08-11", "inspect", 30)
    stages = store.list_stage_work(token)
    store.release_lease(token)
    assert stages == []
    assert summary.backlog["stage_pending"] == 6
    assert summary.outcome == "BUDGET_EXHAUSTED"


def test_model_preprocessing_stops_traversing_when_local_work_reaches_reserve(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    rows = [_row(f"doc-{index}", f"sku-{index}", queryStr=f"title-{index}") for index in range(4)]
    deps = _deps([], rows, clock)
    deps.detail = None
    requests: list[list[str]] = []
    deps.model_batch = lambda batch, timeout: (
        requests.append([str(row["skuId"]) for row in batch])
        or {index: {"match_result": "SAME"} for index, _row_value in enumerate(batch)}
    )
    original_ensure = store.ensure_stage_work

    def advancing_ensure(token, audit_item_id, stage, fingerprint):
        work = original_ensure(token, audit_item_id, stage, fingerprint)
        if stage == "MODEL":
            clock.advance(6)
        return work

    store.ensure_stage_work = advancing_ensure

    summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        TickConfig(
            business_date="2026-08-11",
            runtime_budget_seconds=20,
            heavy_work_reserve_seconds=5,
            lease_ttl_seconds=30,
        ),
        deps,
    )

    token = store.acquire_lease("2026-08-11", "inspect", 30)
    models = store.list_stage_work(token, stage="MODEL")
    store.release_lease(token)
    assert len(models) == 3
    assert requests == []
    assert summary.backlog["stage_pending"] == 8
    assert summary.outcome == "BUDGET_EXHAUSTED"


@pytest.mark.parametrize(
    ("runtime", "external", "stage_limit", "expected"),
    [(30, 7, 20, 7), (30, 60, 11, 11), (5, 60, 20, 5)],
)
def test_stage_callback_timeout_is_minimum_of_external_stage_and_tick_limits(
    tmp_path, runtime, external, stage_limit, expected,
):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    observed: list[float] = []
    deps = _deps([], [_row()], clock)
    deps.detail = lambda row, timeout: (observed.append(timeout) or {"customerPrice": "10"})
    deps.model = lambda row, timeout: (observed.append(timeout) or {"match_result": "SAME"})
    config = TickConfig(
        business_date="2026-08-11", runtime_budget_seconds=runtime, heavy_work_reserve_seconds=0,
        lease_ttl_seconds=30, external_timeout_seconds=external,
        detail_max_elapsed_seconds=stage_limit, model_max_elapsed_seconds=stage_limit,
    )

    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)

    assert observed[:2] == [expected, expected]


def test_plan_finalization_crossing_reserve_does_not_prepare_or_post_remote_intent(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    prepared: list[tuple[str, list[str]]] = []
    posted: list[int] = []
    deps = _deps([], [_row()], clock)

    def finalize(plan, members, timeout):
        prepared.append((plan.mall_key, [member.sku_id for member in members]))
        clock.advance(5)
        return CrawlPlan(
            plan.mall_key, "prepared-only", "prepared-only", "prepared-only",
            payload_json={"members": [member.sku_id for member in members]}, members=tuple(members),
        )

    deps.finalize_crawl_plan = finalize
    deps.submit_crawl = lambda job, crawls, timeout: (posted.append(int(job["id"])) or {"remote_identity": "never", "state": "SUBMITTED"})
    config = TickConfig(
        business_date="2026-08-11", runtime_budget_seconds=10, heavy_work_reserve_seconds=5,
        lease_ttl_seconds=30,
    )

    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)

    token = store.acquire_lease("2026-08-11", "inspect", 30)
    crawls = store.list_crawl_work(token)
    assert prepared == [("mall-a", ["sku-1"])]
    assert posted == []
    assert store.list_remote_jobs(token) == []
    assert crawls[0]["remote_job_id"] is None
    assert crawls[0]["state"] == "PENDING"
    store.release_lease(token)
    assert summary.outcome == "BUDGET_EXHAUSTED"


def test_linking_remote_intent_at_reserve_checkpoints_without_dispatching_post(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    posted: list[int] = []
    deps = _deps([], [_row()], clock)
    deps.submit_crawl = lambda job, crawls, timeout: (
        posted.append(int(job["id"]))
        or {"remote_identity": "must-not-post", "state": "SUBMITTED"}
    )
    original_link = store.link_crawl_work_batch_to_remote_job

    def exhausting_link(token, crawl_work_ids, job_id):
        linked = original_link(token, crawl_work_ids, job_id)
        clock.advance(5)
        return linked

    store.link_crawl_work_batch_to_remote_job = exhausting_link

    summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        TickConfig(
            business_date="2026-08-11",
            runtime_budget_seconds=10,
            heavy_work_reserve_seconds=5,
            lease_ttl_seconds=30,
        ),
        deps,
    )

    token = store.acquire_lease("2026-08-11", "inspect", 30)
    jobs = store.list_remote_jobs(token)
    crawls = store.list_crawl_work(token)
    store.release_lease(token)
    assert posted == []
    assert len(jobs) == 1 and jobs[0]["state"] == "SUBMIT_INTENT"
    assert len(crawls) == 1 and crawls[0]["remote_job_id"] == jobs[0]["id"]
    assert summary.outcome == "BUDGET_EXHAUSTED"


def test_model_batch_deduplicates_stage_fingerprints_and_uses_bounded_network_calls(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    rows = [_row(f"doc-{index}", f"sku-{index}", queryStr=f"title-{index}") for index in range(25)]
    rows.append(dict(rows[0]))
    requests: list[list[str]] = []
    deps = _deps([], rows, clock)

    def model_batch(batch, timeout):
        requests.append([str(row["skuId"]) for row in batch])
        return {
            index: {"match_result": "SAME", "confidence": 0.9, "model_match": "exact"}
            for index, _row_value in enumerate(batch)
        }

    deps.model_batch = model_batch
    config = TickConfig(
        business_date="2026-08-11", runtime_budget_seconds=60, heavy_work_reserve_seconds=5,
        lease_ttl_seconds=30, model_batch_size=10,
    )
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)

    token = store.acquire_lease("2026-08-11", "inspect", 30)
    models = store.list_stage_work(token, stage="MODEL")
    store.release_lease(token)
    assert [len(batch) for batch in requests] == [10, 10, 5]
    assert sum(len(batch) for batch in requests) == 25
    # Exact duplicate snapshot members preserve output multiplicity but share
    # one durable audit item and therefore one model result.
    assert len(models) == 25
    assert all(work["state"] == "SUCCESS" for work in models)
    assert summary.counters["model_attempts"] == 3


def test_model_hard_crash_uses_one_durable_replay_then_succeeds(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    calls = 0
    crashing = _deps([], [_row()], clock)

    def hard_crash(batch, timeout):
        nonlocal calls
        calls += 1
        raise SystemExit("hard model crash")

    crashing.model_batch = hard_crash
    with pytest.raises(SystemExit):
        run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), crashing)

    token = store.acquire_lease("2026-08-11", "inspect-crash", 30)
    interrupted = store.list_stage_work(token, stage="MODEL")[0]
    store.release_lease(token)
    assert interrupted["state"] == "CLAIMED"
    assert interrupted["attempts"] == 1

    resumed = _deps([], [_row()], clock)

    def succeed(batch, timeout):
        nonlocal calls
        calls += 1
        return {0: {"match_result": "SAME"}}

    resumed.model_batch = succeed
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), resumed)

    token = store.acquire_lease("2026-08-11", "inspect-resume", 30)
    completed = store.list_stage_work(token, stage="MODEL")[0]
    store.release_lease(token)
    assert calls == 2
    assert summary.counters["recovered_stage_claims"] == 1
    assert completed["state"] == "SUCCESS"
    assert completed["attempts"] == 2


@pytest.mark.parametrize("batched", [False, True])
def test_caught_model_failure_is_terminal_after_one_durable_claim(tmp_path, batched):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    calls = 0
    failing = _deps([], [_row()], clock)

    def fail(*_args):
        nonlocal calls
        calls += 1
        raise RuntimeError("bounded adapter failure")

    if batched:
        failing.model_batch = fail
    else:
        failing.model = fail
    config = TickConfig(
        business_date="2026-08-11",
        runtime_budget_seconds=60,
        heavy_work_reserve_seconds=5,
        lease_ttl_seconds=30,
        model_max_attempts=2,
    )

    first = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, failing)
    second = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, failing)

    token = store.acquire_lease("2026-08-11", "inspect", 30)
    model = store.list_stage_work(token, stage="MODEL")[0]
    store.release_lease(token)
    assert calls == 1
    assert model["state"] == "TERMINAL_FAILED"
    assert model["attempts"] == 1
    assert first.outcome == "MANUAL_REVIEW_REQUIRED"
    assert second.outcome == "MANUAL_REVIEW_REQUIRED"


def test_linked_submit_intent_crash_resumes_exactly_once_without_replanning(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    deps = _deps(events, [_row()], clock)
    original_claim = store.claim_remote_submission
    store.claim_remote_submission = lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("after durable links"))
    with pytest.raises(SystemExit):
        run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps)
    token = store.acquire_lease("2026-08-11", "inspect", 30)
    interrupted = store.list_remote_jobs(token, states={"SUBMIT_INTENT"})[0]
    bound = store.crawl_work_for_job(interrupted["id"])
    assert len(bound) == 1 and bound[0]["remote_job_id"] == interrupted["id"]
    store.release_lease(token)
    store.claim_remote_submission = original_claim

    resumed = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps)
    token = store.acquire_lease("2026-08-11", "verify", 30)
    jobs = store.list_remote_jobs(token)
    store.release_lease(token)
    assert events.count("submit") == 1
    assert len(jobs) == 1
    assert jobs[0]["id"] == interrupted["id"]
    assert jobs[0]["payload_hash"] == interrupted["payload_hash"]
    assert resumed.outcome == "WAITING_REMOTE"


def test_last_allowed_harvest_persists_terminal_result_instead_of_manual_review(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    config = TickConfig(
        business_date="2026-08-11", runtime_budget_seconds=60, heavy_work_reserve_seconds=5,
        lease_ttl_seconds=30, harvest_max_attempts=1, harvest_max_elapsed_seconds=60,
    )
    first = _deps([], [_row()], clock)
    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, first)
    token = store.acquire_lease("2026-08-11", "inspect", 30)
    job = store.list_remote_jobs(token)[0]
    crawl = store.crawl_work_for_job(job["id"])[0]
    store.release_lease(token)
    completed = _deps([], [_row()], clock)
    completed.harvest = lambda jobs, timeout: {
        job["id"]: {"state": "COMPLETED", "remote_identity": job["remote_identity"], "crawl_results": {crawl["id"]: {"price": "10"}}}
    }

    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, completed)

    token = store.acquire_lease("2026-08-11", "verify", 30)
    assert store.get_remote_job(job["id"])["state"] == "COMPLETED"
    assert store.get_crawl_work(crawl["id"])["state"] == "SUCCESS"
    store.release_lease(token)


def test_harvest_claims_only_bounded_batch_and_uses_one_callback_deadline(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    store.ensure_run("2026-08-11")
    token = store.acquire_lease("2026-08-11", "seed", 60)
    for index in range(100):
        job = store.prepare_remote_intent(token, f"job-{index}", f"job-{index}", f"payload-{index}")
        store.claim_remote_submission(token, job["id"])
        store.reconcile_remote_job(token, job["id"], remote_identity=f"remote-{index}", state="SUBMITTED")
    store.release_lease(token)
    observed: list[tuple[int, float]] = []
    deps = _deps([], [], clock)
    def harvest(jobs, timeout):
        observed.append((len(jobs), timeout))
        clock.advance(timeout)
        return {}
    deps.harvest = harvest
    config = TickConfig(
        business_date="2026-08-11", runtime_budget_seconds=30, heavy_work_reserve_seconds=1,
        lease_ttl_seconds=60, external_timeout_seconds=10, harvest_batch_size=6,
    )

    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)

    token = store.acquire_lease("2026-08-11", "verify", 60)
    attempts = [job["harvest_attempts"] for job in store.list_remote_jobs(token)]
    store.release_lease(token)
    assert observed == [(6, 10)]
    assert sum(attempts) == 6


def test_harvest_persists_per_job_elapsed_evidence_not_whole_batch_elapsed(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    store.ensure_run("2026-08-11")
    token = store.acquire_lease("2026-08-11", "seed", 60)
    jobs = []
    for index in range(2):
        job = store.prepare_remote_intent(token, f"job-{index}", f"job-{index}", f"payload-{index}")
        store.claim_remote_submission(token, job["id"])
        jobs.append(store.reconcile_remote_job(token, job["id"], remote_identity=f"remote-{index}", state="SUBMITTED"))
    store.release_lease(token)
    deps = _deps([], [], clock)
    def harvest(claimed, timeout):
        clock.advance(1)
        first = {"state": "RUNNING", "remote_identity": jobs[0]["remote_identity"], "elapsed_seconds": 1}
        clock.advance(10)
        second = {"state": "RUNNING", "remote_identity": jobs[1]["remote_identity"], "elapsed_seconds": 10}
        return {jobs[0]["id"]: first, jobs[1]["id"]: second}
    deps.harvest = harvest
    config = TickConfig(business_date="2026-08-11", runtime_budget_seconds=60, heavy_work_reserve_seconds=1, lease_ttl_seconds=60)

    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)

    token = store.acquire_lease("2026-08-11", "verify", 60)
    elapsed = [store.get_remote_job(job["id"])["harvest_elapsed_seconds"] for job in jobs]
    store.release_lease(token)
    assert elapsed == [1, 10]


def test_new_source_with_same_backend_sku_url_reuses_completed_crawl_without_post(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    first_rows = [_row("source-a", "same-sku")]
    second_rows = [*first_rows, _row("source-b", "same-sku")]
    config = _config(clock)
    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, _deps(events, first_rows, clock))
    token = store.acquire_lease("2026-08-11", "inspect", 30)
    job = store.list_remote_jobs(token)[0]
    crawl = store.crawl_work_for_job(job["id"])[0]
    store.release_lease(token)
    second = _deps(events, second_rows, clock)
    second.harvest = lambda jobs, timeout: {
        job["id"]: {"state": "COMPLETED", "remote_identity": job["remote_identity"], "crawl_results": {crawl["id"]: {"price": "10"}}}
    }
    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, second)

    token = store.acquire_lease("2026-08-11", "verify", 30)
    crawls = store.list_crawl_work(token)
    jobs = store.list_remote_jobs(token)
    store.release_lease(token)
    assert events.count("submit") == 1
    assert len(jobs) == 1
    assert {(crawl["source_id"], crawl["sku_id"]) for crawl in crawls} == {("source-a", "same-sku"), ("source-b", "same-sku")}
    assert all(crawl["state"] == "SUCCESS" for crawl in crawls)


def test_new_source_follows_same_pending_backend_request_without_second_post(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    clock = Clock()
    events: list[str] = []
    first_rows = [_row("source-a", "same-sku")]
    second_rows = [
        *first_rows,
        _row("source-b", "same-sku"),
        _row("source-c", "new-sku", extLink="https://shop.example/new-item"),
    ]
    config = _config(clock)

    run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        config,
        _deps(events, first_rows, clock),
    )
    run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        config,
        _deps(events, second_rows, clock),
    )

    token = store.acquire_lease("2026-08-11", "verify", 30)
    crawls = store.list_crawl_work(token)
    jobs = store.list_remote_jobs(token)
    store.release_lease(token)
    assert events.count("submit") == 2
    assert len(jobs) == 2
    assert len(crawls) == 3
    first_job = min(jobs, key=lambda job: int(job["id"]))
    second_job = max(jobs, key=lambda job: int(job["id"]))
    by_source = {crawl["source_id"]: crawl for crawl in crawls}
    assert by_source["source-a"]["remote_job_id"] == first_job["id"]
    assert by_source["source-b"]["remote_job_id"] == first_job["id"]
    assert by_source["source-c"]["remote_job_id"] == second_job["id"]
