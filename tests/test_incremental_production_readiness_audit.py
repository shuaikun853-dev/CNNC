"""Fail-closed invariants exercised by the production-readiness audit.

These tests intentionally describe the required production contract. They are
kept separate from the implementation regression suite so an audit can report
unmet invariants without changing production code.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.services.daily_audit_store import DailyAuditStore, RemoteIntentConflict
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    PreparedLegacyJob,
    ReconcileStatus,
)
from app.workflows.incremental_price_audit_finalize import build_frozen_audit_artifact
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping


BUSINESS_DATE = "2026-08-11"


def _row() -> dict[str, object]:
    return {
        "vCommodityId": "v-1",
        "documentId": "v-1",
        "skuId": "sku-1",
        "extLink": "https://item.example/sku-1",
        "queryStr": "customer title",
        "sassVCommodityName": "customer title",
        "skuName": "internet title",
        "vendorName": "mall",
        "shopName": "shop",
        "price": "10",
        "fourCategoryId": "cat-1",
        "fourCategoryName": "category",
        "list_reference_price": "10",
        "customer_lookup_status": "MATCHED",
        "__crawl_required": True,
    }


def _seed_successful_crawl_with_running_remote_job(tmp_path: Path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    store.ensure_run(BUSINESS_DATE, cutoff_at=f"{BUSINESS_DATE}T10:00:00+00:00")
    lease = store.acquire_lease(BUSINESS_DATE, "audit", ttl_seconds=60)
    row = _row()
    snapshot = store.ingest_snapshot(
        lease,
        [row],
        observed_at=f"{BUSINESS_DATE}T10:01:00+00:00",
        headers=[name for name in row if not name.startswith("__")],
        evidence={"observed_count": 1},
    )["snapshot"]
    store.freeze_snapshot(lease, int(snapshot["id"]))
    item = store.ordered_snapshot_items(int(snapshot["id"]))[0]

    detail = store.ensure_stage_work(lease, int(item["id"]), "DETAIL", "detail-fp")
    store.claim_stage_work(lease, int(detail["id"]), max_attempts=3, max_elapsed_seconds=300)
    store.complete_stage_work(
        lease,
        int(detail["id"]),
        {
            "customerPrice": "10",
            "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "",
            "customerPriceEvidence": "customer=10",
        },
    )
    model = store.ensure_stage_work(lease, int(item["id"]), "MODEL", "model-fp")
    store.claim_stage_work(lease, int(model["id"]), max_attempts=1, max_elapsed_seconds=300)
    store.complete_stage_work(
        lease,
        int(model["id"]),
        {
            "match_result": "SAME",
            "confidence": 0.95,
            "model_match": "exact",
            "reason": "same product",
        },
    )

    crawl = store.ensure_crawl_work(
        lease,
        "mall-a",
        "sku-1",
        "https://item.example/sku-1",
        "legacy-v1",
        "crawl-fp",
        source_id="v-1",
    )
    job = store.prepare_remote_intent(
        lease,
        "mall-a:payload",
        "cnnc-audit-20260811-mall-a",
        "payload-a",
    )
    store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    store.claim_remote_submission(lease, int(job["id"]))
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity="mall-a|task-id|task-number",
        state="RUNNING",
    )
    store.complete_crawl_work(
        lease,
        int(crawl["id"]),
        {
            "page_price_value": "9.50",
            "page_price_status": "OK",
            "page_price_source": "zhaoshang",
        },
    )
    return store, lease


def test_formal_final_is_blocked_while_any_remote_job_is_pending(tmp_path: Path):
    store, lease = _seed_successful_crawl_with_running_remote_job(tmp_path)

    built = build_frozen_audit_artifact(
        store,
        lease,
        output_identity="formal-final",
    )

    assert built.artifact is None, "a RUNNING remote job must prohibit formal finalization"


def test_formal_final_rejects_completed_remote_without_exact_identity(tmp_path: Path):
    store, lease = _seed_successful_crawl_with_running_remote_job(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE remote_jobs SET state='COMPLETED', remote_identity=NULL WHERE run_id=?",
            (lease.run_id,),
        )

    built = build_frozen_audit_artifact(
        store,
        lease,
        output_identity="formal-final",
    )

    assert built.artifact is None
    assert built.readiness.remote_invalid == 1


def test_remote_identity_is_unique_within_one_daily_run(tmp_path: Path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    lease = store.acquire_lease(BUSINESS_DATE, "audit", ttl_seconds=60)
    first = store.prepare_remote_intent(lease, "mall-a:first", "task-a", "payload-a")
    second = store.prepare_remote_intent(lease, "mall-a:second", "task-b", "payload-b")
    for job in (first, second):
        store.claim_remote_submission(lease, int(job["id"]))

    identity = "mall-a|same-task-id|same-task-number"
    store.reconcile_remote_job(
        lease,
        int(first["id"]),
        remote_identity=identity,
        state="SUBMITTED",
    )
    with pytest.raises(RemoteIntentConflict):
        store.reconcile_remote_job(
            lease,
            int(second["id"]),
            remote_identity=identity,
            state="SUBMITTED",
        )


def test_exact_remote_lookup_recovers_post_success_response_timeout(tmp_path: Path):
    state_path = tmp_path / "BATCH_STATE.md"
    task_name = "cnnc-audit-20260811-mall-a"
    intent = {
        "timestamp": "2026-08-11T10:01:02+08:00",
        "mall": "mall-a",
        "task_name": task_name,
        "action": "ADD_INTENT",
        "expected_subtask_count": 0,
    }
    state_path.write_text(
        f"# state\n- `{json.dumps(intent, sort_keys=True)}`\n",
        encoding="utf-8",
    )
    mapping = ZhaoshangMallMapping(
        mall_name="Mall A",
        source_id="1",
        mall_key="mall-a",
        domains=("example.com",),
        vendor_keywords=("mall-a",),
    )
    def record_event(path, **event):
        durable = {
            "timestamp": "2026-08-11T10:02:00+08:00",
            "mall": event.pop("mall_key"),
            "task_name": event.pop("task_name"),
            **event,
        }
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(durable, sort_keys=True)}`\n")

    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[mapping],
        db_env_path=None,
        reconciler=lambda task_name, mall_key, timeout_seconds: {
            "taskId": "task-id",
            "taskNumber": "task-number",
            "taskName": task_name,
            "mallLogo": mall_key,
            "expectedSubtaskCount": 0,
            "state": "1",
        },
        event_recorder=record_event,
    )
    job = PreparedLegacyJob(
        business_date=BUSINESS_DATE,
        mall_key="mall-a",
        mall_source_id="1",
        mall_name="Mall A",
        task_name=task_name,
        template_path=tmp_path / "template.xlsx",
        result_path=tmp_path / "result.xlsx",
        batch_state_path=state_path,
        requests=(),
        logical_job_key="mall-a:payload",
        payload_sha256="payload-a",
    )

    outcome = adapter.reconcile_unknown(job, timeout_seconds=12)

    assert outcome.status is ReconcileStatus.FOUND
    assert (outcome.task_id, outcome.task_number) == ("task-id", "task-number")
