"""Circuit-breaker contracts for remote subtask-materialization stalls."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_runtime import run_runtime_command
from app.services.incremental_audit_status import collect_runtime_status
from app.services.remote_stall_breaker import (
    REMOTE_STALL_WINDOW_SECONDS,
    evaluate_remote_stall,
    remote_submit_pause_reason,
)
from app.workflows.incremental_price_audit import (
    CrawlMember,
    CrawlPlan,
    DiscoveredState,
    TickDependencies,
)

pytestmark = pytest.mark.critical_safety


BUSINESS_DATE = "2026-08-19"


def _row(v="doc-1", sku="sku-1"):
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
    }


def _evidence(job_id: int, *, error="PriorityRowsNotMaterialized", state="PRIORITY_PENDING"):
    return {
        "id": job_id,
        "state": state,
        "error_text": error,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def test_one_recent_stall_job_does_not_open_the_breaker():
    health = evaluate_remote_stall([_evidence(1)])
    assert health["stall_detected"] is False
    assert health["submit_paused"] is False
    assert health["recent_stall_jobs"] == 1


def test_three_distinct_recent_stall_jobs_open_the_breaker():
    health = evaluate_remote_stall([_evidence(index) for index in range(3)])
    assert health["stall_detected"] is True
    assert health["submit_paused"] is True
    assert health["recent_stall_jobs"] == 3
    assert health["stall_reason"] == "PRIORITY_ROWS_NOT_MATERIALIZED"
    assert health["window_seconds"] == REMOTE_STALL_WINDOW_SECONDS


def test_interface_and_legacy_manual_evidence_never_count(tmp_path):
    rows = [
        _evidence(1, error="InterfaceError"),
        _evidence(2, error="InterfaceError"),
        _evidence(3, error="InterfaceError"),
        _evidence(4, error="legacy wait-export-only requires manual review"),
        _evidence(5, error="PriorityUpdateZeroRowsError"),
        _evidence(6, error="harvest limit reached", state="MANUAL_REQUIRED"),
    ]
    health = evaluate_remote_stall(rows)
    assert health["stall_detected"] is False
    assert health["recent_stall_jobs"] == 0


def test_stale_evidence_leaves_the_window():
    stale = dict(_evidence(1))
    stale["updated_at"] = (
        datetime.now(UTC) - timedelta(seconds=REMOTE_STALL_WINDOW_SECONDS + 60)
    ).isoformat(timespec="seconds")
    assert evaluate_remote_stall([stale, _evidence(2), _evidence(3)])["stall_detected"] is False


def _seed_stall_jobs(store, lease, count, *, error="PriorityRowsNotMaterialized"):
    for index in range(count):
        job = store.prepare_remote_intent(
            lease,
            f"key-{index}",
            f"cnnc_current_opt_mall_{index}",
            f"hash-{index}",
            mall_key="mall-a",
            payload_json={},
        )
        store.claim_remote_submission(lease, int(job["id"]))
        store.reconcile_remote_job(
            lease,
            int(job["id"]),
            remote_identity=f"mall-a|task-id-{index}|task-number-{index}",
            state="PRIORITY_PENDING",
        )
        store.record_harvest(
            lease,
            int(job["id"]),
            elapsed_seconds=1.0,
            max_attempts=3,
            max_elapsed_seconds=180.0,
            error=error,
        )


def _seed_state(tmp_path, stall_count, *, error="PriorityRowsNotMaterialized"):
    state_path = tmp_path / BUSINESS_DATE / "state.sqlite3"
    store = DailyAuditStore(state_path)
    store.ensure_run(BUSINESS_DATE)
    lease = store.acquire_lease(BUSINESS_DATE, "seed", 60)
    _seed_stall_jobs(store, lease, stall_count, error=error)
    store.release_lease(lease)
    return state_path, store


def _deps(events, *, submit_calls, harvest_results=None):
    def harvest(jobs, _timeout):
        events.append(f"harvest:{len(jobs)}")
        results = harvest_results or {}
        evidence = {}
        for job in jobs:
            # Existing PENDING jobs keep stalling; anything already past
            # priority reports a legal completion transition.
            default = {
                "state": (
                    "PRIORITY_PENDING"
                    if str(job["state"]) == "PRIORITY_PENDING"
                    else "COMPLETED"
                ),
                "remote_identity": str(job["remote_identity"]),
            }
            if str(job["state"]) == "PRIORITY_PENDING":
                default["retryable_error"] = "PriorityRowsNotMaterialized"
            else:
                default["result"] = {}
            evidence[int(job["id"])] = results.get(int(job["id"]), default)
        return evidence

    def discover(_timeout):
        events.append("discover")
        return DiscoveredState(
            observed_at=datetime(2026, 8, 19, 10, 1, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=[_row()],
            evidence={"saas_count": 1, "customer_count": 1},
        )

    def detail(row, _timeout):
        events.append(f"detail:{row['vCommodityId']}")
        return {"customerPrice": "10"}

    def model(row, _timeout):
        events.append(f"model:{row['vCommodityId']}")
        return {"match_result": "SAME"}

    def plan(items, _timeout):
        members = [
            CrawlMember(
                mall_key="mall-a",
                source_id=item["row"]["vCommodityId"],
                sku_id=item["row"]["skuId"],
                exact_url=item["row"]["extLink"],
                backend_version="legacy-v1",
                row=item["row"],
            )
            for item in items
        ]
        return [
            CrawlPlan(
                mall_key="mall-a",
                logical_key="mall-a:batch",
                task_name="tick-mall-a",
                payload_hash="payload-a",
                payload_json={},
                members=members,
            )
        ]

    def submit(job, _crawls, _timeout):
        submit_calls.append(int(job["id"]))
        events.append("submit")
        return {"remote_identity": f"mall-a|new-{job['id']}", "state": "SUBMITTED"}

    return TickDependencies(
        discover=discover,
        harvest=harvest,
        detail=detail,
        model=model,
        plan_crawl=plan,
        submit_crawl=submit,
        wall_clock=lambda: datetime(2026, 8, 19, 10, 2, tzinfo=UTC),
    )


def test_open_breaker_blocks_new_submits_but_keeps_local_and_harvest_work(tmp_path):
    """Breaker 3/4/5: no /add, Detail/Model advance, existing remote harvested."""
    state_path, store = _seed_state(tmp_path, 3)
    events: list[str] = []
    submit_calls: list[int] = []
    deps = _deps(events, submit_calls=submit_calls)

    result = run_runtime_command(
        "tick",
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path,
        dependencies=deps,
        enable_zhaoshang=True,
        zhaoshang_backend="legacy",
    )

    assert result.tick.outcome != "FAILED"
    assert submit_calls == []
    assert result.tick.counters["submit_paused"] == 1
    assert any(event.startswith("detail:") for event in events)
    assert any(event.startswith("model:") for event in events)
    assert any(event.startswith("harvest:") for event in events)
    assert remote_submit_pause_reason(store, BUSINESS_DATE) is not None


def test_a_single_stall_job_does_not_pause_submission(tmp_path):
    state_path, store = _seed_state(tmp_path, 1)
    events: list[str] = []
    submit_calls: list[int] = []
    deps = _deps(events, submit_calls=submit_calls)

    result = run_runtime_command(
        "tick",
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path,
        dependencies=deps,
        enable_zhaoshang=True,
        zhaoshang_backend="legacy",
    )

    assert submit_calls, "one stall evidence must not open the breaker"
    assert result.tick.counters["crawl_submitted"] == 1


def test_new_scheduler_process_still_sees_the_open_breaker(tmp_path):
    """Breaker 6: the breaker is recomputed from durable SQLite per process."""
    state_path, store = _seed_state(tmp_path, 3)
    first_submits: list[int] = []
    second_submits: list[int] = []
    run_runtime_command(
        "tick",
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path,
        dependencies=_deps([], submit_calls=first_submits),
        enable_zhaoshang=True,
    )
    # A brand-new dependencies object mirrors a fresh scheduler process.
    run_runtime_command(
        "tick",
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path,
        dependencies=_deps([], submit_calls=second_submits),
        enable_zhaoshang=True,
    )
    assert first_submits == []
    assert second_submits == []


def test_successful_probe_of_existing_job_closes_the_breaker_without_add(tmp_path):
    """Breaker 7: probes address only existing tasks; success reopens submit.

    Submission may resume later in the same run once the probe succeeds (the
    spec's "当前run或下一run恢复正常submit"); what is forbidden is any /add
    before a probe has proven remote recovery.
    """
    state_path, store = _seed_state(tmp_path, 3)
    events: list[str] = []
    submit_calls: list[int] = []

    def probe_harvest(jobs, _timeout):
        events.append("probe")
        return {
            int(job["id"]): {
                "state": "SUBMITTED",
                "remote_identity": str(job["remote_identity"]),
                "result": {"priority_status": "PRIORITY_UPDATED", "weight_type": -1},
            }
            for job in jobs
        }

    deps = _deps(events, submit_calls=submit_calls)
    deps.harvest = probe_harvest
    run_runtime_command(
        "tick",
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path,
        dependencies=deps,
        enable_zhaoshang=True,
    )
    assert events.index("probe") < (
        events.index("submit") if "submit" in events else len(events)
    )
    assert remote_submit_pause_reason(store, BUSINESS_DATE) is None

    run_runtime_command(
        "tick",
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path,
        dependencies=_deps([], submit_calls=submit_calls),
        enable_zhaoshang=True,
    )
    assert submit_calls, "a successful probe must let submission resume"


def test_continued_zero_rows_probes_keep_the_breaker_open(tmp_path):
    """Breaker 8: probes keep stalling, so no new submit happens."""
    state_path, store = _seed_state(tmp_path, 3)
    submit_calls: list[int] = []
    for _ in range(2):
        run_runtime_command(
            "tick",
            business_date=BUSINESS_DATE,
            runtime_root=tmp_path,
            dependencies=_deps([], submit_calls=submit_calls),
            enable_zhaoshang=True,
        )
    assert submit_calls == []
    assert remote_submit_pause_reason(store, BUSINESS_DATE) is not None


def test_status_reports_remote_health_and_stall_next_action(tmp_path):
    state_path, store = _seed_state(tmp_path, 3)
    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=tmp_path)
    assert status["remote_health"]["stall_detected"] is True
    assert status["remote_health"]["submit_paused"] is True
    assert status["remote_health"]["recent_stall_jobs"] == 3
    assert status["remote_health"]["window_seconds"] == 900.0
    assert status["next_actions"] == [
        "remote stall detected; do not create additional remote tasks; "
        "wait for existing-task probe/recovery"
    ]


def test_status_without_stall_has_healthy_remote_health(tmp_path):
    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=tmp_path)
    assert status["remote_health"]["stall_detected"] is False
    assert status["remote_health"]["submit_paused"] is False
    assert "remote stall detected" not in " ".join(status["next_actions"])
