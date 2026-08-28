"""Finalize/status contracts for the manual-rebuild SUPERSEDED supersede flow."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_status import collect_runtime_status
from app.workflows.incremental_price_audit_finalize import build_frozen_audit_artifact


BUSINESS_DATE = "2026-08-11"


def _row(v="v-1", sku="sku-1", url="https://shop.example/item", **extra):
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": url,
        "queryStr": "客户标题",
        "sassVCommodityName": "客户标题",
        "skuName": "互联网标题",
        "vendorName": "商城",
        "shopName": "店铺",
        "price": "10",
        "fourCategoryId": "cat-1",
        "fourCategoryName": "分类",
        "list_reference_price": "10",
        "customer_lookup_status": "MATCHED",
        "__crawl_required": bool(url),
        **extra,
    }


def _seed_frozen(tmp_path: Path):
    rows = [
        _row("v-1", "sku-1", "https://shop.example/item-1"),
        _row("v-2", "sku-2", "https://shop.example/item-2"),
    ]
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(BUSINESS_DATE, cutoff_at="2026-08-11T10:00:00+00:00")
    token = store.acquire_lease(BUSINESS_DATE, "seed", 60)
    snapshot = store.ingest_snapshot(
        token,
        rows,
        "2026-08-11T10:01:00+00:00",
        headers=[name for name in rows[0] if not name.startswith("__")],
        evidence={"observed_count": len(rows)},
    )["snapshot"]
    store.freeze_snapshot(token, snapshot["id"])
    items = store.ordered_snapshot_items(snapshot["id"])
    for item in {item["id"]: item for item in items}.values():
        detail = store.ensure_stage_work(token, item["id"], "DETAIL", f"detail-{item['id']}")
        store.claim_stage_work(token, detail["id"], max_attempts=3, max_elapsed_seconds=300)
        store.complete_stage_work(token, detail["id"], {
            "customerPrice": "10",
            "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "",
            "customerPriceEvidence": "customer=10",
        })
        model = store.ensure_stage_work(token, item["id"], "MODEL", f"model-{item['id']}")
        store.claim_stage_work(token, model["id"], max_attempts=1, max_elapsed_seconds=300)
        store.complete_stage_work(token, model["id"], {
            "match_result": "SAME", "confidence": 0.95, "model_match": "exact", "reason": "一致",
        })
    for row in rows:
        store.ensure_crawl_work(
            token,
            "mall",
            row["skuId"],
            row["extLink"],
            "legacy-v1",
            f"crawl-fp-{row['vCommodityId']}",
            source_id=row["vCommodityId"],
        )
    store.release_lease(token)
    return store, snapshot


def _prepare_old_job(store, token):
    job = store.prepare_remote_intent(
        token,
        "mall:natural",
        "daily-20260811-mall",
        "payload-a",
        mall_key="mall",
        input_path="run/zhaoshang/mall/input.xlsx",
        result_path="run/zhaoshang/mall/result.xlsx",
        payload_json={"requests": [{"sku_id": "sku-1"}]},
    )
    store.claim_remote_submission(token, int(job["id"]))
    store.reconcile_remote_job(
        token, int(job["id"]), remote_identity="mall|task-id|task-number", state="SUBMITTED"
    )
    return job


def _supersede(store, token, old_job_id, eligible_crawl_ids):
    return store.supersede_remote_job_to_replacement(
        token,
        old_job_id=old_job_id,
        replacement_logical_key="mall:natural#rebuild#abcd1234",
        replacement_task_name="daily-20260811-mall-rebuild-1",
        replacement_payload_hash="payload-rebuild",
        replacement_input_path="run/zhaoshang/mall/rebuild-input.xlsx",
        replacement_result_path="run/zhaoshang/mall/rebuild-result.xlsx",
        replacement_payload_json={"requests": [{"sku_id": "sku-1"}]},
        mall_key="mall",
        action_id="rebuild-20260828-0001",
        reason="RUNNING stall after confirmed restart",
        old_batch_number="RW-100",
        new_batch_number="RW-101",
        stop_evidence={"stop": "confirmed", "restarts": 1},
        eligible_crawl_ids=eligible_crawl_ids,
    )


def test_superseded_old_job_does_not_block_finalize(tmp_path):
    store, _snapshot = _seed_frozen(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "rebuild", 60)
    old_job = _prepare_old_job(store, token)
    first, second = store.list_crawl_work(token)
    store.link_crawl_work_batch_to_remote_job(
        token, [int(first["id"]), int(second["id"])], int(old_job["id"])
    )
    # One crawl already carries terminal evidence and stays with the old job.
    store.complete_crawl_work(
        token, int(first["id"]),
        {"page_price_value": "9.5", "page_price_status": "OK", "page_price_source": "招商局"},
    )
    result = _supersede(store, token, int(old_job["id"]), [int(second["id"])])
    new_job_id = int(result["new_job"]["id"])
    store.claim_remote_submission(token, new_job_id)
    store.reconcile_remote_job(
        token, new_job_id, remote_identity="mall|task-id-2|task-number-2", state="COMPLETED"
    )
    store.complete_crawl_work(
        token, int(second["id"]),
        {"page_price_value": "8.5", "page_price_status": "OK", "page_price_source": "招商局"},
    )

    built = build_frozen_audit_artifact(store, token, output_identity="daily-final-20260811")

    assert built.readiness.remote_invalid == 0
    assert built.readiness.remote_pending == 0
    assert built.readiness.remote_manual == 0
    assert built.readiness.ready is True
    assert built.artifact is not None
    store.release_lease(token)


def test_superseded_without_replacement_row_stays_a_blocker(tmp_path):
    store, _snapshot = _seed_frozen(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "dirty", 60)
    old_job = _prepare_old_job(store, token)
    for crawl in store.list_crawl_work(token):
        store.link_crawl_work_to_remote_job(token, int(crawl["id"]), int(old_job["id"]))
        store.complete_crawl_work(
            token, int(crawl["id"]),
            {"page_price_value": "9.5", "page_price_status": "OK", "page_price_source": "招商局"},
        )
    # Simulate dirty data: SUPERSEDED state without any replacement lineage.
    connection = sqlite3.connect(str(store.path), timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("UPDATE remote_jobs SET state='SUPERSEDED' WHERE id=?", (int(old_job["id"]),))
        connection.commit()
    finally:
        connection.close()

    built = build_frozen_audit_artifact(store, token, output_identity="daily-final-20260811")

    assert built.readiness.remote_invalid == 1
    assert built.readiness.ready is False
    assert built.artifact is None
    store.release_lease(token)


def test_pending_replacement_counts_once_and_never_the_old_job(tmp_path):
    store, _snapshot = _seed_frozen(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "rebuild-pending", 60)
    old_job = _prepare_old_job(store, token)
    first, second = store.list_crawl_work(token)
    store.link_crawl_work_batch_to_remote_job(
        token, [int(first["id"]), int(second["id"])], int(old_job["id"])
    )
    store.complete_crawl_work(
        token, int(first["id"]),
        {"page_price_value": "9.5", "page_price_status": "OK", "page_price_source": "招商局"},
    )
    result = _supersede(store, token, int(old_job["id"]), [int(second["id"])])
    new_job_id = int(result["new_job"]["id"])
    store.claim_remote_submission(token, new_job_id)
    store.reconcile_remote_job(
        token, new_job_id, remote_identity="mall|task-id-2|task-number-2", state="SUBMITTED"
    )

    built = build_frozen_audit_artifact(store, token, output_identity="daily-final-20260811")

    assert built.readiness.remote_pending == 1
    assert built.readiness.remote_intent == 0
    assert built.readiness.remote_invalid == 0
    assert built.readiness.remote_manual == 0
    assert built.readiness.ready is False
    assert built.artifact is None
    store.release_lease(token)


def test_status_reports_superseded_without_pending_contribution(tmp_path):
    store = DailyAuditStore(tmp_path / BUSINESS_DATE / "state.sqlite3")
    store.ensure_run(BUSINESS_DATE)
    token = store.acquire_lease(BUSINESS_DATE, "seed", 60)
    old_job = _prepare_old_job(store, token)
    crawl = store.ensure_crawl_work(
        token,
        "mall",
        "sku-1",
        "https://shop.example/item-1",
        "legacy-v1",
        "crawl-fp",
        source_id="v-1",
    )
    store.link_crawl_work_to_remote_job(token, int(crawl["id"]), int(old_job["id"]))
    _supersede(store, token, int(old_job["id"]), [int(crawl["id"])])
    store.release_lease(token)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=tmp_path)

    assert status["remote"]["superseded"] == 1
    assert status["remote"]["intent"] == 1
    assert status["crawl"]["pending"] == 1
    # SUPERSEDED never joins computed_final_pending: one pending crawl plus
    # one SUBMIT_INTENT replacement equals two, not three.
    assert status["final"]["pending"] == 2
    assert status["final"]["computed_pending"] == 2


def test_logical_key_rebuild_suffix_rule():
    from app.services.incremental_audit_runtime import _logical_key_matches_rebuild

    assert _logical_key_matches_rebuild("abc", "abc") is True
    assert _logical_key_matches_rebuild("abc#rebuild#1a2b3c4d", "abc") is True
    assert _logical_key_matches_rebuild("abc#rebuild#", "abc") is True
    assert _logical_key_matches_rebuild("abcd#rebuild#1a2b3c4d", "abc") is False
    assert _logical_key_matches_rebuild("abc#rebuild2#1a2b3c4d", "abc") is False
    assert _logical_key_matches_rebuild("other", "abc") is False
