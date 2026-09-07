"""Offline contracts: crawl submission decoupled from MODEL, and the
planning_pending vs unplanned (真 blocker) status semantics.

Business rule under test: whether a commodity needs a Zhaoshang crawl has
nothing to do with the MODEL same-audit outcome.  A crawl-required row must
get its remote task as soon as the crawl planner's own inputs (the discovered
row) exist, while the local MODEL pass — successful, failed or still running —
continues in parallel.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.dashboard_projection import compute_overall_status
from app.services.incremental_audit_runtime import runtime_paths
from app.services.incremental_audit_status import collect_runtime_status
from app.workflows.incremental_price_audit import (
    CrawlMember,
    CrawlPlan,
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
)

pytestmark = pytest.mark.critical_safety


@dataclass
class Clock:
    value: float = 0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _row(v="doc-1", sku="sku-1", **extra):
    row = {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": " https://shop.example/item ",
        "fourCategoryId": "cat",
        "list_reference_price": "10",
        "queryStr": "title",
        "skuName": "title",
        "mall_key": "mall-a",
        "__crawl_required": True,
        "__crawl_mall_key": "mall-a",
        "__crawl_mall_source_id": "mall-a-001",
        "__crawl_backend_version": "legacy-v1",
    }
    row.update(extra)
    return row


def _config(clock, *, date="2026-08-11", budget=60, reserve=10):
    return TickConfig(
        business_date=date,
        runtime_budget_seconds=budget,
        heavy_work_reserve_seconds=reserve,
        lease_ttl_seconds=90,
        cutoff_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
    )


def _deps(events, rows, clock, *, with_submit=True, plan_returns_none=False):
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
            evidence={"saas_count": len(rows), "customer_count": len(rows)},
        )

    def detail(row, timeout):
        events.append(f"detail:{row['vCommodityId']}")
        # Full evidence shape the production _detail_worker returns; a bare
        # price without customerPriceStatus is fail-closed INVALID evidence
        # under the round-2 rule table and would turn the status red.
        return {
            "customerPrice": "10",
            "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "",
            "customerPriceEvidence": "参考价1=10",
        }

    def model(row, timeout):
        events.append(f"model:{row['vCommodityId']}")
        return {"match_result": "SAME"}

    def plan(items, timeout):
        events.append("plan")
        if plan_returns_none:
            return []
        members = [
            CrawlMember(
                mall_key=item["row"]["mall_key"], source_id=item["row"]["vCommodityId"], sku_id=item["row"]["skuId"],
                exact_url=item["row"]["extLink"], backend_version="legacy-v1", row=item["row"],
            )
            for item in items
            if bool(item["row"].get("__crawl_required"))
        ]
        return [CrawlPlan(
            mall_key="mall-a", logical_key="mall-a:batch", task_name="tick-mall-a", payload_hash="payload-a",
            payload_json={"mall": "mall-a"}, members=members,
        )]

    def submit(job, crawl_items, timeout):
        events.append("submit")
        return {"remote_identity": "task-1", "state": "SUBMITTED"}

    def finalize(plan, members, timeout):
        suffix = "-".join(member.sku_id for member in members)
        return CrawlPlan(
            mall_key=plan.mall_key,
            logical_key=f"{plan.mall_key}:{suffix}",
            task_name=f"tick-{suffix}",
            payload_hash=f"payload-{suffix}",
            payload_json={"mall": plan.mall_key},
            members=tuple(members),
        )

    return TickDependencies(
        discover=discover, harvest=harvest, detail=detail, model=model,
        plan_crawl=plan if with_submit else None,
        submit_crawl=submit if with_submit else None,
        finalize_crawl_plan=finalize if with_submit else None,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
    )


def _remote_job_states(state_path):
    uri = f"file:{state_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT id, state FROM remote_jobs ORDER BY id")]
    finally:
        connection.close()


def _store_for(tmp_path, date="2026-08-11"):
    return DailyAuditStore(runtime_paths(date, tmp_path).state_path)


def test_crawl_submission_precedes_model_and_survives_model_failure(tmp_path):
    store = _store_for(tmp_path)
    events: list[str] = []
    clock = Clock()
    deps = _deps(events, [_row()], clock)

    def failing_model(row, timeout):
        events.append(f"model:{row['vCommodityId']}")
        raise ValueError("model unavailable")

    deps.model = failing_model

    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps)

    # Remote submission happened even though MODEL never succeeded.
    assert summary.counters["crawl_submitted"] == 1
    jobs = _remote_job_states(runtime_paths("2026-08-11", tmp_path).state_path)
    assert jobs and jobs[0]["state"] == "SUBMITTED"
    token = store.acquire_lease("2026-08-11", "inspect", 30)
    model_states = {str(work["state"]) for work in store.list_stage_work(token, stage="MODEL")}
    store.release_lease(token)
    assert "SUCCESS" not in model_states
    # Phase order: submission is planned right after discovery, before MODEL.
    assert events.index("submit") < events.index("model:doc-1")
    assert list(summary.phase_trace[:4]) == ["recover", "harvest", "discover", "submit"]


def test_model_state_is_not_a_crawl_eligibility_gate_while_model_still_runs(tmp_path):
    """Mid-MODEL observation: the crawl work and remote task already exist."""
    store = _store_for(tmp_path)
    events: list[str] = []
    clock = Clock()
    observed: dict[str, object] = {}

    def observing_model(row, timeout):
        events.append(f"model:{row['vCommodityId']}")
        observed["remote_jobs"] = _remote_job_states(runtime_paths("2026-08-11", tmp_path).state_path)
        return {"match_result": "SAME"}

    deps = _deps(events, [_row()], clock)
    deps.model = observing_model

    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps)

    # While the MODEL callback was still executing (its stage row was not yet
    # SUCCESS), the durable remote task for the crawl-required row existed.
    assert observed["remote_jobs"] and observed["remote_jobs"][0]["state"] == "SUBMITTED"


def test_status_reports_planning_pending_not_manual_before_planner_chance(tmp_path):
    """Shadow day: required row without crawl work is a transient, not manual."""
    store = _store_for(tmp_path)
    events: list[str] = []
    clock = Clock()
    deps = _deps(events, [_row()], clock, with_submit=False)

    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps)

    status = collect_runtime_status(business_date="2026-08-11", runtime_root=tmp_path)
    assert status["crawl"]["planning_pending"] == 1
    assert status["crawl"]["unplanned"] == 0
    assert status["final"]["blocker_summary"]["crawl_planning_pending"] == 1
    assert status["final"]["blocker_summary"]["crawl_unplanned"] == 0
    # Components still decompose computed_pending exactly.
    summary = status["final"]["blocker_summary"]
    assert sum(summary.values()) == status["final"]["computed_pending"]
    assert status["next_actions"] == [
        "run the next tick so crawl planning and pending crawl work can advance; "
        "required rows without crawl work are not yet a manual blocker"
    ]
    overall = compute_overall_status(status)
    assert overall["color"] != "red"
    assert all(item["title"] != "存在未规划采集的必爬行" for item in overall["attention"])


def test_unplanned_is_a_real_blocker_after_a_completed_submit_pass(tmp_path):
    """Planner ran to completion yet a required row still cannot be planned."""
    store = _store_for(tmp_path)
    events: list[str] = []
    clock = Clock()
    deps = _deps(events, [_row()], clock, with_submit=True, plan_returns_none=True)

    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps)

    status = collect_runtime_status(business_date="2026-08-11", runtime_root=tmp_path)
    assert status["crawl"]["planning_pending"] == 0
    assert status["crawl"]["unplanned"] == 1
    assert status["final"]["blocker_summary"]["crawl_unplanned"] == 1
    assert status["next_actions"] == [
        "inspect required snapshot rows without crawl work; do not finalize or submit without explicit authorization"
    ]
    overall = compute_overall_status(status)
    assert overall["word"] == "需要人工处理"
    assert any(item["title"] == "存在未规划采集的必爬行" for item in overall["attention"])


def test_submit_pass_snapshot_metric_survives_partial_metrics_merge(tmp_path):
    """The durable planner-chance flag must not be wiped by a later sweep."""
    store = _store_for(tmp_path)
    events: list[str] = []
    clock = Clock()
    deps = _deps(events, [_row()], clock, with_submit=True, plan_returns_none=True)

    first = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps)
    assert first.counters["crawl_submitted"] == 0
    status = collect_runtime_status(business_date="2026-08-11", runtime_root=tmp_path)
    assert status["crawl"]["unplanned"] == 1

    # Sweep-style commands merge only their own counters into daily metrics;
    # such a partial merge must never clear the recorded submit pass.
    token = store.acquire_lease("2026-08-11", "sweep", 30)
    store.update_daily_metrics(token, {"crawl_running": 0, "harvest_batches": 0})
    store.release_lease(token)
    status = collect_runtime_status(business_date="2026-08-11", runtime_root=tmp_path)
    assert status["metrics"].get("submit_pass_snapshot_id") == status["latest_snapshot"]["id"]
    assert status["crawl"]["unplanned"] == 1
    assert status["crawl"]["planning_pending"] == 0


def test_new_snapshot_resets_planner_chance_to_planning_pending(tmp_path):
    """A newer observation without a completed pass returns to the transient."""
    store = _store_for(tmp_path)
    events: list[str] = []
    clock = Clock()
    deps = _deps(events, [_row()], clock, with_submit=True, plan_returns_none=True)
    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps)
    assert collect_runtime_status(business_date="2026-08-11", runtime_root=tmp_path)["crawl"]["unplanned"] == 1

    # Budget dies during discovery of a NEW snapshot: the planner never gets a
    # chance for it, so the required row is pending planning again, not manual.
    late_deps = _deps(events, [_row("doc-2", "sku-2")], clock, with_submit=True, plan_returns_none=True)
    original_discover = late_deps.discover

    def late_discover(timeout):
        value = original_discover(timeout)
        clock.advance(55)
        return value

    late_deps.discover = late_discover
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick2.lock"), _config(clock), late_deps)
    assert summary.outcome == "BUDGET_EXHAUSTED"
    status = collect_runtime_status(business_date="2026-08-11", runtime_root=tmp_path)
    assert status["crawl"]["planning_pending"] == 1
    assert status["crawl"]["unplanned"] == 0
    overall = compute_overall_status(status)
    assert overall["color"] != "red"


def test_non_required_row_never_creates_crawl_work(tmp_path):
    store = _store_for(tmp_path)
    events: list[str] = []
    clock = Clock()
    rows = [_row(v="doc-plain", sku="sku-plain", __crawl_required=False, extLink="", __crawl_mall_key="", __crawl_mall_source_id="", __crawl_backend_version="")]
    deps = _deps(events, rows, clock)

    run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps)

    token = store.acquire_lease("2026-08-11", "inspect", 30)
    assert store.list_crawl_work(token) == []
    store.release_lease(token)
    status = collect_runtime_status(business_date="2026-08-11", runtime_root=tmp_path)
    assert status["crawl"]["planning_pending"] == 0
    assert status["crawl"]["unplanned"] == 0
