"""Classification contracts for remote not-materialized priority failures."""

from __future__ import annotations

import json

import pytest

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    PriorityStatus,
    REMOTE_NOT_MATERIALIZED_DIAGNOSTICS,
    remote_not_materialized_diagnostic,
    retryable_priority_error_diagnostic,
)
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
)


class PriorityUpdateZeroRowsError(RuntimeError):
    pass


class PriorityScopeSafetyError(RuntimeError):
    pass


ZERO_ROWS_MESSAGE = "no matching rows for mall/task_number/create_time window"
INCOMPLETE_MESSAGE = "incomplete subtask rows: matched=5 expected=17"


def _row(index: int) -> dict[str, object]:
    return {
        "vCommodityId": f"v-{index}",
        "skuId": f"sku-{index}",
        "queryStr": f"query-{index}",
        "skuName": f"name-{index}",
        "extLink": f"https://shop.example/{index}",
        "__crawl_required": True,
    }


def test_zero_rows_after_bounded_lookup_classifies_remote_not_materialized():
    exc = PriorityUpdateZeroRowsError(ZERO_ROWS_MESSAGE)
    assert remote_not_materialized_diagnostic(exc) == "PriorityRowsNotMaterialized"
    assert remote_not_materialized_diagnostic(exc) in REMOTE_NOT_MATERIALIZED_DIAGNOSTICS


def test_incomplete_rows_classifies_remote_not_materialized():
    exc = PriorityUpdateZeroRowsError(INCOMPLETE_MESSAGE)
    assert remote_not_materialized_diagnostic(exc) == "PriorityRowsIncomplete"


def test_identity_and_update_verification_failures_stay_fail_closed():
    for message in (
        "no matching rows",
        "task_number mismatch",
        "mall mismatch",
        "create_time outside window",
        "target set changed before priority lock: matched=3 expected=17",
        "priority update affected unexpected row count: affected=2 candidates=17",
        "priority verification failed: exact target rows are not -1",
        "post-commit priority verification failed: exact target rows are not -1",
        "ZeroRows",
    ):
        assert remote_not_materialized_diagnostic(PriorityUpdateZeroRowsError(message)) is None, message
    assert remote_not_materialized_diagnostic(PriorityScopeSafetyError("task_number mismatch")) is None
    assert remote_not_materialized_diagnostic(RuntimeError(ZERO_ROWS_MESSAGE)) is None


def test_interface_error_keeps_existing_retryable_classification():
    exc = PriorityUpdateZeroRowsError(ZERO_ROWS_MESSAGE)
    assert retryable_priority_error_diagnostic(exc) is None
    try:
        raise TimeoutError("read timed out")
    except TimeoutError as caught:
        assert retryable_priority_error_diagnostic(caught) == "TimeoutError"


def _adapter_with_runner(runner):
    return CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        priority_runner=runner,
        runner=lambda *_args: pytest.fail("classification must never call /add"),
    )


def _prepared_job(tmp_path, adapter):
    batch_state = tmp_path / "BATCH_STATE.md"
    batch_state.write_text("# state\n", encoding="utf-8")
    request = CrawlRequest(
        "jd",
        "source",
        "京东",
        "sku-1",
        "https://item.jd.com/sku-1",
        "legacy-v1",
    )
    prepared = adapter.prepare_job(
        "2026-08-19",
        (request,),
        tmp_path / "input.xlsx",
        tmp_path / "result.xlsx",
        "cnnc_current_opt_京东_28089831959826",
        batch_state,
    )
    for action in ("CREATED", "ENABLED"):
        event = {
            "action": action,
            "mall": "jd",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
        }
        with batch_state.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event)}`\n")
    return prepared


def test_zero_rows_priority_outcome_is_retryable_not_manual(tmp_path):
    def runner(_job, _task_id, _task_number, _timeout):
        raise PriorityUpdateZeroRowsError(ZERO_ROWS_MESSAGE)

    adapter = _adapter_with_runner(runner)
    prepared = _prepared_job(tmp_path, adapter)
    outcome = adapter.prioritize(
        prepared, task_id="task-id", task_number="task-number", timeout_seconds=5
    )
    assert outcome.status is PriorityStatus.UNCONFIRMED
    assert outcome.retryable is True
    assert outcome.diagnostic == "PriorityRowsNotMaterialized"


def test_verification_priority_outcome_stays_non_retryable(tmp_path):
    def runner(_job, _task_id, _task_number, _timeout):
        raise PriorityUpdateZeroRowsError(
            "priority verification failed: exact target rows are not -1"
        )

    adapter = _adapter_with_runner(runner)
    prepared = _prepared_job(tmp_path, adapter)
    outcome = adapter.prioritize(
        prepared, task_id="task-id", task_number="task-number", timeout_seconds=5
    )
    assert outcome.status is PriorityStatus.UNCONFIRMED
    assert outcome.retryable is False
    assert outcome.diagnostic == "PriorityUpdateZeroRowsError"


def test_stall_retry_keeps_priority_pending_until_bounded_budget(tmp_path):
    """First not-materialized evidence must stay PRIORITY_PENDING, not MANUAL.

    The bounded budget (priority_retry_max_attempts=3) then turns the third
    failed sweep into MANUAL_REQUIRED with the precise stall diagnostic as
    durable error evidence.
    """
    store = DailyAuditStore(tmp_path / "state.sqlite")
    lease = store.acquire_lease("2026-08-11", "seed-stall", 60)
    ingested = store.ingest_snapshot(lease, [_row(index) for index in range(2)])
    job = store.prepare_remote_intent(
        lease, "key-1", "task-1", "hash-1", mall_key="mall-a", payload_json={}
    )
    store.claim_remote_submission(lease, int(job["id"]))
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity="mall-a|task-id|task-number",
        state="PRIORITY_PENDING",
    )
    for index in range(2):
        crawl = store.ensure_crawl_work(
            lease,
            "mall-a",
            f"sku-{index}",
            f"https://shop.example/{index}",
            "legacy-v1",
            f"crawl-{index}",
            source_id=f"v-{index}",
        )
        store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    store.release_lease(lease)

    def harvest(jobs, _timeout):
        return {
            int(job_row["id"]): {
                "state": "PRIORITY_PENDING",
                "remote_identity": "mall-a|task-id|task-number",
                "retryable_error": "PriorityRowsNotMaterialized",
            }
            for job_row in jobs
        }

    def discover(_timeout):
        from datetime import UTC, datetime

        return DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=[],
            evidence={"saas_count": 0, "customer_count": 0},
        )

    deps = TickDependencies(discover=discover, harvest=harvest)
    config = TickConfig(business_date="2026-08-11", lease_ttl_seconds=30)
    summaries = [
        run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)
        for _ in range(3)
    ]
    final_job = store.get_remote_job(int(job["id"]))
    assert all(summary.outcome != "MANUAL_REVIEW_REQUIRED" for summary in summaries[:1])
    assert summaries[-1].outcome == "MANUAL_REVIEW_REQUIRED"
    assert final_job["state"] == "MANUAL_REQUIRED"
    assert final_job["error_text"] == "PriorityRowsNotMaterialized"
    assert int(final_job["harvest_attempts"]) == 3
