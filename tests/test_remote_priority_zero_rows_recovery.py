"""Recovery contracts for MANUAL zero-rows remote priority jobs."""

from __future__ import annotations

import json

import pytest

from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_recovery import (
    RecoveryRejected,
    run_existing_remote_priority_zero_rows_recovery,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
)


BUSINESS_DATE = "2026-08-19"
IDENTITY = "jd|task-id|task-number"


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


class _Probe:
    def __init__(self, outcome: str = "success") -> None:
        self.outcome = outcome
        self.calls = 0

    def __call__(self, job, task_id, task_number, _timeout):
        self.calls += 1
        if self.outcome == "zero-rows":
            raise PriorityUpdateZeroRowsError(ZERO_ROWS_MESSAGE)
        if self.outcome == "incomplete":
            raise PriorityUpdateZeroRowsError(INCOMPLETE_MESSAGE)
        if self.outcome == "identity":
            raise PriorityScopeSafetyError("task_number mismatch")
        event = {
            "action": "PRIORITY_UPDATED",
            "mall": job.mall_key,
            "task_name": job.task_name,
            "task_id": task_id,
            "task_number": task_number,
            "affected_rows": len(job.requests),
        }
        with job.batch_state_path.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event)}`\n")
        return {"priority_state": "updated", "affected_rows": len(job.requests)}


def _seed_zero_rows_manual_job(
    tmp_path,
    *,
    error_text="PriorityUpdateZeroRowsError",
    state="MANUAL_REQUIRED",
    freeze_run=True,
):
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    state_path = run_dir / "state.sqlite3"
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.parent.mkdir(parents=True)
    batch_state.write_text("# state\n", encoding="utf-8")
    probe = _Probe()
    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        priority_runner=probe,
        runner=lambda *_args: pytest.fail("zero-rows recovery must never call /add"),
    )
    requests = tuple(
        CrawlRequest(
            "jd",
            "source",
            "京东",
            f"sku-{index}",
            f"https://item.jd.com/{index}",
            "legacy-v1",
        )
        for index in range(3)
    )
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        requests,
        run_dir / "input.xlsx",
        run_dir / "result.xlsx",
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

    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-zero-rows", 60)
    ingested = store.ingest_snapshot(lease, [_row(index) for index in range(3)])
    if freeze_run:
        store.freeze_snapshot(lease, int(ingested["snapshot"]["id"]))
    payload = {
        "requests": [request.__dict__ for request in requests],
        "batch_state": str(batch_state),
    }
    job = store.prepare_remote_intent(
        lease,
        prepared.logical_job_key,
        prepared.task_name,
        prepared.payload_sha256,
        mall_key=prepared.mall_key,
        input_path=str(prepared.template_path),
        result_path=str(prepared.result_path),
        payload_json=payload,
    )
    store.claim_remote_submission(lease, int(job["id"]))
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity=IDENTITY,
        state="PRIORITY_PENDING",
    )
    for index, request in enumerate(requests):
        crawl = store.ensure_crawl_work(
            lease,
            request.mall_key,
            request.sku_id,
            request.url,
            request.backend_version,
            f"crawl-{index}",
            source_id=f"v-{index}",
        )
        store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    if state == "MANUAL_REQUIRED":
        store.mark_remote_manual(lease, int(job["id"]), error_text)
    store.release_lease(lease)
    return {
        "state_path": state_path,
        "lock_path": run_dir / "incremental_price_audit.lock",
        "adapter": adapter,
        "probe": probe,
        "job_id": int(job["id"]),
        "store": store,
        "prepared": prepared,
    }


def _recover(seed, **overrides):
    return run_existing_remote_priority_zero_rows_recovery(
        state_path=seed["state_path"],
        lock_path=seed["lock_path"],
        business_date=BUSINESS_DATE,
        remote_job_id=seed["job_id"],
        remote_identity=IDENTITY,
        expected_error_text=overrides.pop(
            "expected_error_text", "PriorityUpdateZeroRowsError"
        ),
        adapter=overrides.pop("adapter", seed["adapter"]),
        timeout_seconds=overrides.pop("timeout_seconds", 60.0),
        **overrides,
    )


def test_recovery_requeues_manual_job_after_live_rematerialization(tmp_path):
    seed = _seed_zero_rows_manual_job(tmp_path)
    result = _recover(seed)
    assert result["outcome"] == "REMOTE_PRIORITY_ZERO_ROWS_RECOVERY_COMPLETED"
    assert result["add_calls"] == 0
    assert result["priority_calls"] == 1
    assert result["expected"] == 3 and result["matched"] == 3
    job = seed["store"].get_remote_job(seed["job_id"])
    assert job["state"] == "SUBMITTED"
    assert job["error_text"] is None
    assert {crawl["state"] for crawl in seed["store"].crawl_work_for_job(seed["job_id"])} == {
        "SUBMITTED"
    }


def test_recovery_is_idempotent_and_never_repeats_priority_or_export(tmp_path):
    seed = _seed_zero_rows_manual_job(tmp_path)
    first = _recover(seed)
    assert first["outcome"] == "REMOTE_PRIORITY_ZERO_ROWS_RECOVERY_COMPLETED"
    with pytest.raises(RecoveryRejected):
        _recover(seed)
    assert seed["probe"].calls == 1
    job = seed["store"].get_remote_job(seed["job_id"])
    assert job["state"] == "SUBMITTED"


def test_recovery_rejects_when_rows_are_still_not_materialized(tmp_path):
    seed = _seed_zero_rows_manual_job(tmp_path)
    seed["probe"].outcome = "zero-rows"
    with pytest.raises(RecoveryRejected, match="still not materialized"):
        _recover(seed)
    job = seed["store"].get_remote_job(seed["job_id"])
    assert job["state"] == "MANUAL_REQUIRED"
    assert job["error_text"] == "PriorityUpdateZeroRowsError"
    assert seed["probe"].calls == 1


def test_recovery_rejects_partial_rows(tmp_path):
    seed = _seed_zero_rows_manual_job(tmp_path)
    seed["probe"].outcome = "incomplete"
    with pytest.raises(RecoveryRejected, match="still not materialized"):
        _recover(seed)
    assert seed["store"].get_remote_job(seed["job_id"])["state"] == "MANUAL_REQUIRED"


def test_recovery_rejects_live_identity_or_validation_anomaly(tmp_path):
    seed = _seed_zero_rows_manual_job(tmp_path)
    seed["probe"].outcome = "identity"
    with pytest.raises(RecoveryRejected, match="PriorityScopeSafetyError"):
        _recover(seed)
    assert seed["store"].get_remote_job(seed["job_id"])["state"] == "MANUAL_REQUIRED"


def test_recovery_rejects_wrong_remote_identity(tmp_path):
    seed = _seed_zero_rows_manual_job(tmp_path)
    with pytest.raises(RecoveryRejected, match="remote identity"):
        run_existing_remote_priority_zero_rows_recovery(
            state_path=seed["state_path"],
            lock_path=seed["lock_path"],
            business_date=BUSINESS_DATE,
            remote_job_id=seed["job_id"],
            remote_identity="jd|other-id|task-number",
            expected_error_text="PriorityUpdateZeroRowsError",
            adapter=seed["adapter"],
            timeout_seconds=60.0,
        )
    assert seed["probe"].calls == 0


def test_recovery_rejects_changed_or_inadmissible_error_text(tmp_path):
    seed = _seed_zero_rows_manual_job(tmp_path)
    with pytest.raises(RecoveryRejected, match="error evidence changed"):
        _recover(seed, expected_error_text="PriorityRowsNotMaterialized")
    with pytest.raises(RecoveryRejected, match="REMOTE_NOT_MATERIALIZED"):
        _recover(seed, expected_error_text="PriorityRowsNotMaterialized-x")


def test_recovery_rejects_interface_error_manual_like_job60(tmp_path):
    seed = _seed_zero_rows_manual_job(tmp_path, error_text="InterfaceError")
    with pytest.raises(RecoveryRejected, match="REMOTE_NOT_MATERIALIZED"):
        _recover(seed, expected_error_text="InterfaceError")
    assert seed["probe"].calls == 0
    assert seed["store"].get_remote_job(seed["job_id"])["state"] == "MANUAL_REQUIRED"


def test_recovery_rejects_legacy_wait_export_manual_like_job54(tmp_path):
    seed = _seed_zero_rows_manual_job(
        tmp_path, error_text="legacy wait-export-only requires manual review"
    )
    with pytest.raises(RecoveryRejected, match="REMOTE_NOT_MATERIALIZED"):
        _recover(seed, expected_error_text="legacy wait-export-only requires manual review")
    assert seed["probe"].calls == 0


def test_recovery_requires_frozen_daily_run(tmp_path):
    seed = _seed_zero_rows_manual_job(tmp_path, freeze_run=False)
    with pytest.raises(RecoveryRejected, match="frozen"):
        _recover(seed)
    assert seed["probe"].calls == 0


def test_recovery_requires_exact_manual_state_and_post_evidence(tmp_path):
    pending = _seed_zero_rows_manual_job(
        tmp_path,
        error_text="PriorityRowsNotMaterialized",
        state="PRIORITY_PENDING",
    )
    with pytest.raises(RecoveryRejected, match="state changed"):
        _recover(pending, expected_error_text="PriorityRowsNotMaterialized")
    assert pending["probe"].calls == 0


def test_recovery_accepts_precise_future_diagnostics(tmp_path):
    seed = _seed_zero_rows_manual_job(
        tmp_path, error_text="PriorityRowsNotMaterialized"
    )
    result = _recover(seed, expected_error_text="PriorityRowsNotMaterialized")
    assert result["outcome"] == "REMOTE_PRIORITY_ZERO_ROWS_RECOVERY_COMPLETED"
    assert seed["store"].get_remote_job(seed["job_id"])["state"] == "SUBMITTED"
