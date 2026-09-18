"""WS-B: 爬取失败的客户链接按原生行格式进最终表。

口径（用户拍板，覆盖 spec 旧表述）：不新建 sheet；失败链接按最终表本来的行
格式追加进 ``record_results``，结果列（页面价格状态）写"爬取失败"，价格列
留空；既有行任何字段零改动；追加发生在构建期（冻结/哈希之前），materializer
校验与 manifest 哈希自然覆盖。

关键不变量：
- 失败行与成功行同列同序（表头逐列一致，追加在既有行之后）；
- 无失败（缺 prewarm store / 当日无候选）零追加；
- 既有行 frozen_rows / enriched_rows / record_results 与不带失败行时逐字段
  一致（前后哈希对照，工作簿层面逐单元格一致）；
- 失败原因文案：死 SUBMIT_UNKNOWN=提交超时无身份；NO_RESULT=远端无匹配价格行；
  重排超限=重排超限；MANUAL_REQUIRED/TERMINAL_FAILED=终端失败；
- 当日 scope（prewarm_daily_observations）之外的候选与 CACHE_HIT 不进表；
- 只读数据源（mode=ro state DB + read-only prewarm store），零写、零真实远端调用。
"""

from __future__ import annotations

import json

import sqlite3
from pathlib import Path

import pytest
from openpyxl import load_workbook

from app.services.customer_prewarm_lifecycle import (
    FAILED_LINK_REASON_NO_RESULT,
    FAILED_LINK_REASON_REDISPATCH_EXHAUSTED,
    FAILED_LINK_REASON_SUBMIT_UNKNOWN,
    FAILED_LINK_REASON_TERMINAL,
    failed_customer_link_rows,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import DailyAuditStore, ProcessLock, RemoteIntentConflict
from app.services.incremental_audit_materializer import LegacyWorkbookMaterializer
from app.services.incremental_audit_runtime import runtime_paths
from app.services.incremental_audit_status import collect_runtime_status
from app.workflows.incremental_price_audit_finalize import (
    build_frozen_audit_artifact,
    materialize_frozen_daily_run,
)
from scripts import zhaoshang_mall_mapping as mapping_module
from scripts.audit_saas_excel_file import write_audit_workbook
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

BD = "2026-09-15"
YESTERDAY = "2026-09-14"
PRODUCER = "customer-prewarm-zhaoshang"
NO_ROW_REASON = "legacy export missing safe skuId / exact URL result"

# 真实 SaaS 导出的 legacy 表头（不含写入层追加列；真实最终表即此结构）。
LEGACY_HEADERS = [
    "vCommodityId",
    "documentId",
    "queryStr",
    "sassVCommodityName",
    "skuId",
    "skuName",
    "vendorName",
    "shopName",
    "price",
    "extLink",
    "commentNum",
    "createTime",
    "updateTime",
    "modelAuditResult",
    "isMatch",
]

COMIX = ZhaoshangMallMapping(
    mall_name="齐心商城",
    source_id="SRC-COMIX",
    mall_key="comix",
    domains=("comix.example",),
    vendor_keywords=("comix",),
)


# ---- seeders ------------------------------------------------------------------


def _row(v="v-1", sku="sku-1", url="https://shop.example/item", **extra):
    return {
        "vCommodityId": v,
        "documentId": v,
        "queryStr": "客户标题",
        "sassVCommodityName": "客户标题",
        "skuId": sku,
        "skuName": "互联网标题",
        "vendorName": "商城",
        "shopName": "店铺",
        "price": "10",
        "extLink": url,
        "commentNum": "",
        "createTime": "2026-09-14 09:49:08",
        "updateTime": "2026-09-14 09:49:12",
        "modelAuditResult": "1",
        "isMatch": "1",
        "__crawl_required": bool(url),
        **extra,
    }


def _seed_frozen_day(
    runtime_root: Path,
    business_date: str = BD,
    *,
    customer_price: str = "10",
    complete_crawl: bool = True,
) -> DailyAuditStore:
    """One frozen, finalize-ready day at the standard runtime layout."""
    paths = runtime_paths(business_date, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(business_date, cutoff_at=f"{business_date}T10:00:00+00:00")
    token = store.acquire_lease(business_date, "seed", 60)
    rows = [
        _row("v-1", "sku-1", "https://shop.example/item"),
        _row("v-2", "sku-2", ""),
    ]
    snapshot = store.ingest_snapshot(
        token,
        rows,
        f"{business_date}T10:01:00+00:00",
        headers=list(LEGACY_HEADERS),
        evidence={"observed_count": len(rows)},
    )["snapshot"]
    store.freeze_snapshot(token, snapshot["id"])
    for item in store.ordered_snapshot_items(snapshot["id"]):
        detail = store.ensure_stage_work(token, item["id"], "DETAIL", f"detail-{item['id']}")
        store.claim_stage_work(token, detail["id"], max_attempts=3, max_elapsed_seconds=300)
        store.complete_stage_work(token, detail["id"], {
            "customerPrice": customer_price,
            "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "",
            "customerPriceEvidence": f"customer={customer_price}",
        })
        model = store.ensure_stage_work(token, item["id"], "MODEL", f"model-{item['id']}")
        store.claim_stage_work(token, model["id"], max_attempts=1, max_elapsed_seconds=300)
        store.complete_stage_work(token, model["id"], {
            "match_result": "SAME", "confidence": 0.95, "model_match": "exact", "reason": "一致",
        })
    crawl = store.ensure_crawl_work(
        token, "mall", "sku-1", "https://shop.example/item", "legacy-v1", "crawl-fp",
        source_id="v-1",
    )
    if complete_crawl:
        store.complete_crawl_work(token, crawl["id"], {
            "page_price_value": "9.5", "page_price_status": "OK", "page_price_source": "招商局",
        })
    store.release_lease(token)
    return store


def _seed_missing_frozen_rows(runtime_root: Path, count: int) -> DailyAuditStore:
    """Frozen rows with complete Detail/Model and unresolved page-price work."""
    paths = runtime_paths(BD, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(BD, cutoff_at=f"{BD}T10:00:00+00:00")
    token = store.acquire_lease(BD, "seed-missing", 60)
    rows = [
        _row(
            f"DOC-FROZEN-{index}",
            f"sku-frozen-{index}",
            f"https://shop.example/frozen-{index}",
        )
        for index in range(count)
    ]
    snapshot = store.ingest_snapshot(
        token,
        rows,
        f"{BD}T10:01:00+00:00",
        headers=list(LEGACY_HEADERS),
        evidence={"observed_count": len(rows)},
    )["snapshot"]
    store.freeze_snapshot(token, snapshot["id"])
    for item in store.ordered_snapshot_items(snapshot["id"]):
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
            "match_result": "SAME",
            "confidence": 0.95,
            "model_match": "exact",
            "reason": "一致",
        })
        row = item["row"]
        store.ensure_crawl_work(
            token,
            "mall",
            row["skuId"],
            row["extLink"],
            "legacy-v1",
            f"crawl-{row['skuId']}",
            source_id=row["vCommodityId"],
        )
    store.release_lease(token)
    return store


def _projected_failure(index: int, *, document_id: str | None = None, url: str | None = None):
    document = document_id or f"DOC-FAILED-{index}"
    original_url = url or f"https://comix.example/failed-{index}"
    return {
        "mall": "comix",
        "mall_name": "齐心商城",
        "original_url": original_url,
        "document_id": document,
        "document_ids": [document],
        "reason": FAILED_LINK_REASON_TERMINAL,
        "last_task_name": f"task-{index}",
        "last_time": f"{BD}T10:00:00+00:00",
        "job_state": "MANUAL_REQUIRED",
        "remote_job_id": index + 1,
        "candidate_id": index + 1,
        "redispatch_links_used": 1,
        "owning_day": BD,
        "customer_evidence": {},
    }


def _seed_remote_job(
    store: DailyAuditStore,
    token,
    runtime_root: Path,
    *,
    logical_key: str,
    task_name: str,
    state: str,
) -> int:
    """One remote job in the requested dead/no-result shape; returns its id."""
    paths = runtime_paths(BD, runtime_root)
    job = store.prepare_remote_intent(
        token, logical_key, task_name, f"payload-{logical_key}",
        result_path=str(paths.run_dir / f"result-{logical_key}.xlsx"),
    )
    job_id = int(job["id"])
    if state == "MANUAL_REQUIRED":
        store.mark_remote_manual(token, job_id, "remote failure")
    elif state == "DEAD_SUBMIT_UNKNOWN":
        assert store.claim_remote_submission(token, job_id) is not None
        store.mark_submit_unknown(token, job_id, "submit unknown")
        for _ in range(4):
            store.bump_remote_job_recovery_attempt(token, job_id)
    elif state == "COMPLETED_NO_RESULT":
        assert store.claim_remote_submission(token, job_id) is not None
        store.reconcile_remote_job(
            token, job_id,
            remote_identity=f"comix|task-{job_id}|number-{job_id}",
            state="COMPLETED",
            result={"https://other/item": {"price": "12.30"}},
        )
        return job_id
    elif state == "SUBMITTED":
        assert store.claim_remote_submission(token, job_id) is not None
        store.reconcile_remote_job(
            token, job_id,
            remote_identity=f"comix|task-{job_id}|number-{job_id}",
            state="SUBMITTED",
        )
    return job_id


def _seed_failed_candidate(
    prewarm: CustomerPrewarmStore,
    store: DailyAuditStore,
    token,
    runtime_root: Path,
    *,
    url: str,
    document: str,
    job_state: str,
    mall: str = "comix",
    observe_date: str = BD,
    extra_links_today: int = 0,
) -> tuple[int, int]:
    """One prewarm candidate + its bound remote job; returns (candidate_id, job_id)."""
    upsert = prewarm.upsert_link(document, url, mall=mall, business_date=observe_date)
    candidate_id = int(upsert["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)

    job_id = _seed_remote_job(
        store, token, runtime_root,
        logical_key=f"lk-{candidate_id}",
        task_name=f"cnnc_current_opt_test_{candidate_id:06d}",
        state=job_state,
    )
    if job_state == "COMPLETED_NO_RESULT":
        crawl = store.ensure_crawl_work(
            token, mall, f"zhpq-{candidate_id}", url, "legacy-v1", f"fp-{candidate_id}",
            source_id="SRC",
        )
        store.link_crawl_work_to_remote_job(token, int(crawl["id"]), job_id)
        store.fail_crawl_work(token, int(crawl["id"]), NO_ROW_REASON, retryable=False)
        store.complete_crawl_work_without_result(token, int(crawl["id"]), reason=NO_ROW_REASON)

    prewarm.record_candidate_result(
        candidate_id,
        cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
        remote_job_id=job_id,
    )
    prewarm.record_remote_job_link(BD, candidate_id, job_id, PRODUCER)
    for offset in range(extra_links_today):
        filler = _seed_remote_job(
            store, token, runtime_root,
            logical_key=f"lk-fill-{candidate_id}-{offset}",
            task_name=f"cnnc_current_opt_fill_{candidate_id}_{offset}",
            state="SUBMITTED",
        )
        prewarm.record_remote_job_link(BD, candidate_id, filler, PRODUCER)
    if extra_links_today:
        prewarm.record_remote_job_link(BD, candidate_id, job_id, PRODUCER)
    return candidate_id, job_id


def _seed_failure_scenario(runtime_root: Path):
    """The full classification scenario: 3 failures + 2 exclusions."""
    store = _seed_frozen_day(runtime_root)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "seed-prewarm", 60)
    try:
        _, job_dead = _seed_failed_candidate(
            prewarm, store, token, runtime_root,
            url="https://comix.example/item-a", document="DOC-A",
            job_state="DEAD_SUBMIT_UNKNOWN",
        )
        _, job_no_result = _seed_failed_candidate(
            prewarm, store, token, runtime_root,
            url="https://comix.example/item-b", document="DOC-B",
            job_state="COMPLETED_NO_RESULT",
        )
        _, job_over_limit = _seed_failed_candidate(
            prewarm, store, token, runtime_root,
            url="https://comix.example/item-c", document="DOC-C",
            job_state="MANUAL_REQUIRED",
            extra_links_today=1,
        )
        # Exclusion 1: a CACHE_HIT candidate is terminal success, never a failure.
        hit = prewarm.upsert_link("DOC-D", "https://comix.example/item-d", mall="comix", business_date=BD)
        prewarm.record_candidate_result(int(hit["candidate"]["id"]), cache_status=CACHE_STATUS_PENDING_PREWARM)
        prewarm.record_candidate_result(int(hit["candidate"]["id"]), cache_status=CACHE_STATUS_CACHE_HIT)
        # Exclusion 2: a dead-bound candidate observed only on another day.
        _seed_failed_candidate(
            prewarm, store, token, runtime_root,
            url="https://comix.example/item-e", document="DOC-E",
            job_state="MANUAL_REQUIRED", observe_date=YESTERDAY,
        )
    finally:
        store.release_lease(token)
    return store, prewarm, {
        "dead": job_dead,
        "no_result": job_no_result,
        "over_limit": job_over_limit,
    }


# ---- failed_customer_link_rows (unit) -----------------------------------------


def test_failed_link_rows_classifies_no_result_dead_job_and_budget_exhausted(tmp_path):
    _store, _prewarm, jobs = _seed_failure_scenario(tmp_path)

    rows = failed_customer_link_rows(BD, tmp_path, [COMIX])

    by_url = {row["original_url"]: row for row in rows}
    assert set(by_url) == {
        "https://comix.example/item-a",
        "https://comix.example/item-b",
        "https://comix.example/item-c",
    }
    dead = by_url["https://comix.example/item-a"]
    assert dead["reason"] == FAILED_LINK_REASON_SUBMIT_UNKNOWN
    assert dead["job_state"] == "SUBMIT_UNKNOWN"
    no_result = by_url["https://comix.example/item-b"]
    assert no_result["reason"] == FAILED_LINK_REASON_NO_RESULT
    over_limit = by_url["https://comix.example/item-c"]
    assert over_limit["reason"] == FAILED_LINK_REASON_REDISPATCH_EXHAUSTED
    assert over_limit["redispatch_links_used"] == 2
    for url, row in by_url.items():
        assert row["mall"] == "comix"
        assert row["mall_name"] == "齐心商城"
        assert row["document_ids"] == [f"DOC-{url[-1].upper()}"]
        assert row["last_task_name"].startswith("cnnc_current_opt_test_")
        assert row["last_time"]
        assert row["remote_job_id"] in jobs.values()
    assert rows == sorted(rows, key=lambda row: (row["mall"], row["original_url"]))


def test_failed_link_rows_excludes_other_day_candidates_and_cache_hits(tmp_path):
    _store, _prewarm, _jobs = _seed_failure_scenario(tmp_path)

    rows = failed_customer_link_rows(BD, tmp_path, [COMIX])

    urls = {row["original_url"] for row in rows}
    assert "https://comix.example/item-d" not in urls  # CACHE_HIT
    assert "https://comix.example/item-e" not in urls  # observed only yesterday


def test_failed_link_rows_with_missing_store_is_empty(tmp_path):
    _seed_frozen_day(tmp_path)

    assert failed_customer_link_rows(BD, tmp_path, [COMIX]) == []


def test_failed_link_rows_unmapped_mall_falls_back_to_mall_key(tmp_path):
    store = _seed_frozen_day(tmp_path)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "seed-unmapped", 60)
    try:
        _seed_failed_candidate(
            prewarm, store, token, tmp_path,
            url="https://unknown-mall.example/item-x", document="DOC-X",
            job_state="MANUAL_REQUIRED", mall="unknownmall",
        )
    finally:
        store.release_lease(token)

    rows = failed_customer_link_rows(BD, tmp_path, [])

    assert len(rows) == 1
    assert rows[0]["mall"] == "unknownmall"
    assert rows[0]["mall_name"] == "unknownmall"


@pytest.mark.parametrize("harvest_attempts,error_text", [
    (0, "prewarm reconcile identity conflict: ambiguous exact matches"),
    (1, "exact EXPORTED evidence does not match durable remote identity"),
])
def test_manual_blocker_remains_visible_in_final_failure_rows(
    tmp_path, harvest_attempts, error_text
):
    store = _seed_frozen_day(tmp_path)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "seed-manual-blocker", 60)
    try:
        _candidate_id, job_id = _seed_failed_candidate(
            prewarm, store, token, tmp_path,
            url="https://comix.example/manual-blocker", document="DOC-M",
            job_state="MANUAL_REQUIRED",
        )
    finally:
        store.release_lease(token)
    with sqlite3.connect(runtime_paths(BD, tmp_path).state_path) as connection:
        connection.execute(
            "UPDATE remote_jobs SET harvest_attempts=?, error_text=? WHERE id=?",
            (harvest_attempts, error_text, job_id),
        )
    rows = failed_customer_link_rows(BD, tmp_path, [COMIX])
    assert len(rows) == 1
    assert rows[0]["reason"] == FAILED_LINK_REASON_TERMINAL


def test_failed_link_rows_does_not_attribute_colliding_job_after_link_mismatch(tmp_path):
    store = _seed_frozen_day(tmp_path)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "seed-mismatched-link", 60)
    try:
        candidate_id, job_id = _seed_failed_candidate(
            prewarm, store, token, tmp_path,
            url="https://comix.example/mismatched-link", document="DOC-M",
            job_state="MANUAL_REQUIRED",
        )
        crawl = store.ensure_crawl_work(
            token, "comix", "zhpq-mismatch", "https://comix.example/mismatched-link",
            "legacy-v1", "fp-mismatch", source_id="SRC",
        )
        store.link_crawl_work_to_remote_job(token, int(crawl["id"]), job_id)
        store.fail_crawl_work(token, int(crawl["id"]), NO_ROW_REASON, retryable=False)
    finally:
        store.release_lease(token)
    prewarm.record_remote_job_link(BD, candidate_id, job_id + 100, PRODUCER)
    assert failed_customer_link_rows(BD, tmp_path, [COMIX]) == []

def test_failed_link_rows_use_only_today_document_evidence(tmp_path):
    store = _seed_frozen_day(tmp_path)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "seed-cross-day", 60)
    try:
        candidate_id, _job_id = _seed_failed_candidate(
            prewarm,
            store,
            token,
            tmp_path,
            url="https://comix.example/shared",
            document="DOC-OLD",
            job_state="MANUAL_REQUIRED",
            observe_date=YESTERDAY,
        )
        current = prewarm.upsert_link(
            "DOC-TODAY",
            "https://comix.example/shared",
            mall="comix",
            business_date=BD,
        )
        assert int(current["candidate"]["id"]) == candidate_id
    finally:
        store.release_lease(token)

    rows = failed_customer_link_rows(BD, tmp_path, [COMIX])

    assert len(rows) == 1
    assert rows[0]["document_id"] == "DOC-TODAY"
    assert rows[0]["document_ids"] == ["DOC-TODAY"]


def test_failed_link_rows_keep_two_today_documents_for_same_url(tmp_path):
    store = _seed_frozen_day(tmp_path)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "seed-two-docs", 60)
    try:
        _seed_failed_candidate(
            prewarm,
            store,
            token,
            tmp_path,
            url="https://comix.example/shared-today",
            document="DOC-A",
            job_state="MANUAL_REQUIRED",
        )
        prewarm.upsert_link(
            "DOC-B",
            "https://comix.example/shared-today",
            mall="comix",
            business_date=BD,
        )
    finally:
        store.release_lease(token)

    rows = failed_customer_link_rows(BD, tmp_path, [COMIX])

    assert [(row["document_id"], row["original_url"]) for row in rows] == [
        ("DOC-A", "https://comix.example/shared-today"),
        ("DOC-B", "https://comix.example/shared-today"),
    ]


def test_customer_only_failed_row_uses_persisted_title_and_price(tmp_path, monkeypatch):
    monkeypatch.setattr(mapping_module, "load_zhaoshang_mall_mappings", lambda: [COMIX])
    store = _seed_frozen_day(tmp_path)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "seed-customer-evidence", 60)
    try:
        _seed_failed_candidate(
            prewarm,
            store,
            token,
            tmp_path,
            url="https://comix.example/customer-only",
            document="DOC-CUSTOMER",
            job_state="MANUAL_REQUIRED",
        )
    finally:
        store.release_lease(token)
    prewarm.record_daily_document_evidence(
        BD,
        "DOC-CUSTOMER",
        customer_title="世达 SATA 91513 0-300mm",
        four_category_id="CAT-4",
        four_category_name="量具",
        customer_price="1871.52",
        customer_price_status="FOUND",
        customer_price_source="DETAIL_REFERENCE_INFOS",
        customer_price_warning="",
        customer_price_evidence="customer=1871.52",
    )
    output = tmp_path / "customer-only.xlsx"

    publication = materialize_frozen_daily_run(
        store, ProcessLock(tmp_path / "customer-only.lock"), BD, output,
    )

    assert publication.materialization is not None
    headers, rows = _data_rows(output)
    failed = rows[-1]
    assert failed[headers.index("客户标题")] == "世达 SATA 91513 0-300mm"
    assert failed[headers.index("客户价")] == 1871.52
    assert failed[headers.index("客户价状态")] == "已找到"
    assert failed[headers.index("页面价格状态")] == "爬取失败"


def test_existing_frozen_identity_is_overlaid_not_appended(tmp_path, monkeypatch):
    monkeypatch.setattr(mapping_module, "load_zhaoshang_mall_mappings", lambda: [COMIX])
    store = _seed_frozen_day(tmp_path, customer_price="1871.52")
    failure = _projected_failure(
        1,
        document_id="v-1",
        url="https://shop.example/item",
    )
    monkeypatch.setattr(
        "app.services.customer_prewarm_lifecycle.failed_customer_link_rows",
        lambda *_args, **_kwargs: [failure],
    )
    output = tmp_path / "deduped.xlsx"

    publication = materialize_frozen_daily_run(
        store, ProcessLock(tmp_path / "dedupe.lock"), BD, output,
    )

    assert publication.materialization is not None
    assert publication.materialization.row_count == 2
    headers, rows = _data_rows(output)
    matched = rows[0]
    assert matched[headers.index("客户标题")] == "客户标题"
    assert matched[headers.index("客户价")] == 1871.52
    assert matched[headers.index("页面价格状态")] == "爬取失败"
    assert str(matched[headers.index("原因")]).startswith("爬取失败：")


def test_seven_plus_sixty_eight_is_one_authoritative_projection(tmp_path, monkeypatch):
    store = _seed_missing_frozen_rows(tmp_path, 7)
    failures = [_projected_failure(index) for index in range(68)]
    monkeypatch.setattr(
        "app.services.customer_prewarm_lifecycle.failed_customer_link_rows",
        lambda *_args, **_kwargs: failures,
    )
    monkeypatch.setattr(mapping_module, "load_zhaoshang_mall_mappings", lambda: [COMIX])

    status = collect_runtime_status(business_date=BD, runtime_root=tmp_path)
    token = store.acquire_lease(BD, "build-75", 60)
    try:
        built = build_frozen_audit_artifact(
            store,
            token,
            output_identity="daily-final-75",
            tolerate_pending=True,
            include_failed_customer_links=True,
        )
    finally:
        store.release_lease(token)
    output = tmp_path / "projection-75.xlsx"
    materialized = LegacyWorkbookMaterializer().materialize(built.artifact, output)
    headers, rows = _data_rows(output)
    success = {"成功", "价格为0", "OK", "PRICE_IS_ZERO"}
    actual_missing = sum(
        1
        for row in rows
        if row[headers.index("互联网链接")]
        and row[headers.index("页面价格状态")] not in success
    )

    assert built.readiness.frozen_missing_page_price_rows == 7
    assert built.readiness.failed_customer_link_rows == 68
    assert built.readiness.missing_page_price_rows == 75
    assert status["final"]["missing_page_price_rows"] == 75
    assert status["final"]["frozen_missing_page_price_rows"] == 7
    assert status["final"]["failed_customer_link_rows"] == 68
    assert materialized.row_count == 75
    assert actual_missing == 75
    manifest = json.loads(materialized.manifest_path.read_text(encoding="utf-8"))
    assert manifest["row_count"] == 75


def test_finalized_missing_count_is_immutable_after_prewarm_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(mapping_module, "load_zhaoshang_mall_mappings", lambda: [COMIX])
    store, _prewarm, _jobs = _seed_failure_scenario(tmp_path)
    output = tmp_path / "immutable.xlsx"

    publication = materialize_frozen_daily_run(
        store, ProcessLock(tmp_path / "immutable.lock"), BD, output,
    )
    assert publication.readiness.missing_page_price_rows == 3
    committed_metrics = json.loads(store.get_run(BD)["metrics_json"])
    assert {key: committed_metrics[key] for key in (
        "final_missing_page_price_rows", "final_frozen_missing_page_price_rows",
        "final_failed_customer_link_rows", "final_appended_failed_customer_link_rows",
    )} == {
        "final_missing_page_price_rows": 3,
        "final_frozen_missing_page_price_rows": 0,
        "final_failed_customer_link_rows": 3,
        "final_appended_failed_customer_link_rows": 3,
    }
    with sqlite3.connect(str(tmp_path / "customer_prewarm.sqlite3")) as connection:
        connection.execute("DELETE FROM prewarm_daily_observations WHERE business_date=?", (BD,))

    status = collect_runtime_status(business_date=BD, runtime_root=tmp_path)

    assert status["final"]["finalized"] is True
    assert status["final"]["missing_page_price_rows"] == 3
    assert status["final"]["frozen_missing_page_price_rows"] == 0
    assert status["final"]["failed_customer_link_rows"] == 3
    assert status["final"]["appended_failed_customer_link_rows"] == 3
    assert json.loads(store.get_run(BD)["metrics_json"]) == committed_metrics


def test_legacy_finalized_status_uses_workbook_when_projection_metrics_are_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(mapping_module, "load_zhaoshang_mall_mappings", lambda: [COMIX])
    store, _prewarm, _jobs = _seed_failure_scenario(tmp_path)
    output = tmp_path / "legacy-finalized.xlsx"
    materialize_frozen_daily_run(
        store, ProcessLock(tmp_path / "legacy-finalized.lock"), BD, output,
    )
    state_path = runtime_paths(BD, tmp_path).state_path
    with sqlite3.connect(str(state_path)) as connection:
        metrics = json.loads(connection.execute(
            "SELECT metrics_json FROM daily_runs WHERE business_date=?",
            (BD,),
        ).fetchone()[0])
        for key in (
            "final_missing_page_price_rows",
            "final_frozen_missing_page_price_rows",
            "final_failed_customer_link_rows",
        ):
            metrics.pop(key, None)
        connection.execute(
            "UPDATE daily_runs SET metrics_json=? WHERE business_date=?",
            (json.dumps(metrics), BD),
        )
    with sqlite3.connect(str(tmp_path / "customer_prewarm.sqlite3")) as connection:
        connection.execute("DELETE FROM prewarm_daily_observations WHERE business_date=?", (BD,))

    status = collect_runtime_status(business_date=BD, runtime_root=tmp_path)

    assert status["final"]["missing_page_price_rows"] == 3


def _bind_candidate_to_job(
    prewarm: CustomerPrewarmStore,
    store: DailyAuditStore,
    token,
    *,
    url: str,
    document: str,
    job_id: int,
    mall: str = "comix",
    existing_crawl_id: int | None = None,
) -> int:
    candidate = prewarm.upsert_link(document, url, mall=mall, business_date=BD)["candidate"]
    candidate_id = int(candidate["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=job_id,
    )
    prewarm.record_remote_job_link(BD, candidate_id, job_id, PRODUCER)
    if existing_crawl_id is None:
        crawl = store.ensure_crawl_work(
            token, mall, f"zhpq-{candidate_id}", url, "legacy-v1", f"fp-{candidate_id}",
            source_id=document,
        )
        crawl_id = int(crawl["id"])
    else:
        crawl_id = existing_crawl_id
    store.link_crawl_work_to_remote_job(token, crawl_id, job_id)
    return crawl_id

def _local_price(value: str = "218.37") -> dict[str, str]:
    return {
        "page_price_value": value,
        "page_price_status": "OK",
        "page_price_source": "本地兜底:comix",
    }

def test_stale_submit_unknown_local_success_has_one_successful_final_row(tmp_path, monkeypatch):
    monkeypatch.setattr(mapping_module, "load_zhaoshang_mall_mappings", lambda: [COMIX])
    store = _seed_frozen_day(tmp_path, complete_crawl=False)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "local-final", 60)
    try:
        job_id = _seed_remote_job(
            store, token, tmp_path, logical_key="local-final", task_name="local-final-job",
            state="DEAD_SUBMIT_UNKNOWN",
        )
        crawl = store.list_crawl_work(token)[0]
        crawl_id = _bind_candidate_to_job(
            prewarm, store, token, url="https://shop.example/item", document="v-1",
            job_id=job_id, mall="mall", existing_crawl_id=int(crawl["id"]),
        )
        store.complete_crawl_work(token, crawl_id, _local_price())
        built = build_frozen_audit_artifact(
            store, token, output_identity="local-final", include_failed_customer_links=True,
        )
    finally:
        store.release_lease(token)

    assert store.get_remote_job(job_id)["state"] == "SUBMIT_UNKNOWN"
    assert failed_customer_link_rows(BD, tmp_path, [COMIX]) == []
    assert built.readiness.ready is True
    assert built.readiness.remote_pending == 0
    assert built.artifact is not None
    matching = [
        row for row in built.artifact.frozen_rows
        if row.get("extLink") == "https://shop.example/item"
    ]
    assert len(matching) == 1
    assert matching[0]["__page_price_status"] == "OK"
    assert str(matching[0]["__page_price_value"]) == "218.37"
    assert len(built.artifact.audit_response.record_results) == 2

def test_shared_remote_job_suppresses_only_locally_resolved_urls(tmp_path):
    store = _seed_frozen_day(tmp_path)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "shared-job", 60)
    try:
        job_id = _seed_remote_job(
            store, token, tmp_path, logical_key="shared", task_name="shared-job",
            state="COMPLETED_NO_RESULT",
        )
        crawls = {
            suffix: _bind_candidate_to_job(
                prewarm, store, token, url=f"https://comix.example/item-{suffix}",
                document=f"DOC-{suffix}", job_id=job_id,
            )
            for suffix in "ABCD"
        }
        for suffix in "AB":
            store.complete_crawl_work(token, crawls[suffix], _local_price())
        store.fail_crawl_work(token, crawls["C"], "terminal remote failure", retryable=False)
        store.fail_crawl_work(token, crawls["D"], NO_ROW_REASON, retryable=False)
        store.complete_crawl_work_without_result(token, crawls["D"], reason=NO_ROW_REASON)
        # A late terminal job state must not erase already persisted per-URL evidence.
        store.mark_remote_manual(token, job_id, "late remote terminal")
    finally:
        store.release_lease(token)

    by_url = {row["original_url"]: row for row in failed_customer_link_rows(BD, tmp_path, [COMIX])}
    assert set(by_url) == {
        "https://comix.example/item-C", "https://comix.example/item-D",
    }
    assert by_url["https://comix.example/item-D"]["reason"] == FAILED_LINK_REASON_NO_RESULT
    assert by_url["https://comix.example/item-C"]["reason"] == FAILED_LINK_REASON_TERMINAL

def test_late_remote_failure_cannot_replace_local_success_or_project_failure(tmp_path):
    store = _seed_frozen_day(tmp_path)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "late-remote", 60)
    try:
        job_id = _seed_remote_job(
            store, token, tmp_path, logical_key="late", task_name="late-job",
            state="DEAD_SUBMIT_UNKNOWN",
        )
        url = "https://comix.example/late"
        crawl_id = _bind_candidate_to_job(
            prewarm, store, token, url=url, document="DOC-LATE", job_id=job_id,
        )
        store.complete_crawl_work(token, crawl_id, _local_price())
        before = store.get_crawl_work(crawl_id)
        store.mark_remote_manual(token, job_id, "late remote terminal")
        with pytest.raises(RemoteIntentConflict, match="immutable"):
            store.fail_crawl_work(token, crawl_id, "late remote failure", retryable=False)
        after = store.get_crawl_work(crawl_id)
    finally:
        store.release_lease(token)

    assert after["result_json"] == before["result_json"]
    assert json.loads(after["result_json"])["page_price_value"] == "218.37"
    assert all(row["original_url"] != url for row in failed_customer_link_rows(BD, tmp_path, [COMIX]))

def test_no_result_sibling_does_not_make_local_success_fail(tmp_path):
    store = _seed_frozen_day(tmp_path)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    token = store.acquire_lease(BD, "no-result-sibling", 60)
    try:
        job_id = _seed_remote_job(
            store, token, tmp_path, logical_key="no-result-sibling",
            task_name="no-result-sibling-job", state="COMPLETED_NO_RESULT",
        )
        local_url = "https://comix.example/local"
        no_result_url = "https://comix.example/no-result"
        local_crawl = _bind_candidate_to_job(
            prewarm, store, token, url=local_url, document="DOC-LOCAL", job_id=job_id,
        )
        missing_crawl = _bind_candidate_to_job(
            prewarm, store, token, url=no_result_url, document="DOC-NO-RESULT", job_id=job_id,
        )
        store.complete_crawl_work(token, local_crawl, _local_price())
        store.fail_crawl_work(token, missing_crawl, NO_ROW_REASON, retryable=False)
        store.complete_crawl_work_without_result(token, missing_crawl, reason=NO_ROW_REASON)
    finally:
        store.release_lease(token)

    by_url = {row["original_url"]: row for row in failed_customer_link_rows(BD, tmp_path, [COMIX])}
    assert set(by_url) == {no_result_url}
    assert by_url[no_result_url]["reason"] == FAILED_LINK_REASON_NO_RESULT


# ---- final workbook projection (integration) ----------------------------------


def _data_rows(path: Path) -> tuple[list[str], list[tuple]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(workbook["record_results"].iter_rows(values_only=True))
    finally:
        workbook.close()
    return [str(value) for value in rows[0]], [tuple(row) for row in rows[1:]]


def test_final_workbook_appends_failed_links_with_native_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(mapping_module, "load_zhaoshang_mall_mappings", lambda: [COMIX])
    store, _prewarm, jobs = _seed_failure_scenario(tmp_path)
    output = tmp_path / "saas_audit_final.xlsx"

    publication = materialize_frozen_daily_run(
        store, ProcessLock(tmp_path / "finalize.lock"), BD, output,
    )

    headers, data_rows = _data_rows(output)
    # Native row format: same header row as any ordinary final, rows appended
    # after the frozen rows (同列同序), no extra sheet.
    assert publication.materialization is not None
    assert publication.materialization.row_count == 2 + 3
    assert len(data_rows) == 5

    def column(name: str) -> int:
        return headers.index(name)

    failed_rows = data_rows[2:]
    assert [tuple(row[column("互联网链接")] for row in failed_rows)] == [(
        "https://comix.example/item-a",
        "https://comix.example/item-b",
        "https://comix.example/item-c",
    )]
    assert [row[column("店铺名称")] for row in failed_rows] == ["齐心商城"] * 3
    assert [row[column("文档ID")] for row in failed_rows] == ["DOC-A", "DOC-B", "DOC-C"]
    assert [row[column("页面价格状态")] for row in failed_rows] == ["爬取失败"] * 3
    for row in failed_rows:
        assert row[column("爬取页面价格")] in (None, "")  # 价格列留空
        assert row[column("页面价格来源")] in (None, "")
        assert str(row[column("原因")]).startswith("爬取失败：")
        assert "failed_remote_task=" in str(row[column("证据")])
        assert row[column("需人工审批")] == "FALSE"
        assert row[column("允许执行")] == "FALSE"
        assert row[column("已执行")] == "FALSE"
        assert row[column("建议操作")] == "无操作"
    reasons = [str(row[column("原因")]) for row in failed_rows]
    assert reasons == [
        f"爬取失败：{FAILED_LINK_REASON_SUBMIT_UNKNOWN}",
        f"爬取失败：{FAILED_LINK_REASON_NO_RESULT}",
        f"爬取失败：{FAILED_LINK_REASON_REDISPATCH_EXHAUSTED}",
    ]
    # 既有行的结果列零改动：成功爬取行保持"成功"。
    assert data_rows[0][column("页面价格状态")] == "成功"
    # summary sheet 的业务计数不含失败行（总记录数保持冻结行数）。
    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        summary_rows = list(workbook["summary"].iter_rows(values_only=True))
    finally:
        workbook.close()
    total_index = list(summary_rows[0]).index("总记录数")
    assert summary_rows[1][total_index] == 2


def test_final_workbook_without_failed_links_appends_zero_rows(tmp_path):
    store = _seed_frozen_day(tmp_path)
    output = tmp_path / "saas_audit_final.xlsx"

    publication = materialize_frozen_daily_run(
        store, ProcessLock(tmp_path / "finalize.lock"), BD, output,
    )

    headers, data_rows = _data_rows(output)
    assert publication.materialization is not None
    assert publication.materialization.row_count == 2
    assert len(data_rows) == 2


def test_existing_rows_are_identical_with_and_without_failed_links(tmp_path, monkeypatch):
    monkeypatch.setattr(mapping_module, "load_zhaoshang_mall_mappings", lambda: [COMIX])
    store, _prewarm, _jobs = _seed_failure_scenario(tmp_path)
    plain_output = tmp_path / "plain.xlsx"

    token = store.acquire_lease(BD, "build-plain", 60)
    try:
        plain = build_frozen_audit_artifact(store, token, output_identity="daily-final")
    finally:
        store.release_lease(token)
    token = store.acquire_lease(BD, "build-with", 60)
    try:
        with_failures = build_frozen_audit_artifact(
            store, token, output_identity="daily-final",
            include_failed_customer_links=True,
        )
    finally:
        store.release_lease(token)

    n = len(plain.artifact.frozen_rows)
    assert n == 2
    # Audit summary stays unchanged; readiness gains only final-projection
    # diagnostics for the three appended business rows.
    assert with_failures.readiness.frozen_rows == plain.readiness.frozen_rows
    assert with_failures.readiness.frozen_missing_page_price_rows == 0
    assert with_failures.readiness.failed_customer_link_rows == 3
    assert with_failures.readiness.appended_failed_customer_link_rows == 3
    assert with_failures.readiness.missing_page_price_rows == 3
    assert with_failures.artifact.audit_response.summary == plain.artifact.audit_response.summary
    # 既有行逐字段零改动（前后哈希对照）。
    assert with_failures.artifact.frozen_rows[:n] == plain.artifact.frozen_rows
    assert with_failures.artifact.enriched_rows[:n] == plain.artifact.enriched_rows
    assert (
        [result.model_dump() for result in with_failures.artifact.audit_response.record_results[:n]]
        == [result.model_dump() for result in plain.artifact.audit_response.record_results]
    )
    # 失败行只追加在既有行之后。
    assert len(with_failures.artifact.frozen_rows) == n + 3
    assert len(with_failures.artifact.audit_response.record_results) == n + 3

    # 工作簿层面：真实 writer 逐单元格对照。
    write_audit_workbook(
        plain_output,
        list(plain.artifact.headers),
        [dict(row) for row in plain.artifact.frozen_rows],
        [dict(row) for row in plain.artifact.enriched_rows],
        plain.artifact.audit_response,
    )
    final_output = tmp_path / "saas_audit_final.xlsx"
    materialize_frozen_daily_run(
        store, ProcessLock(tmp_path / "finalize.lock"), BD, final_output,
    )
    plain_headers, plain_rows = _data_rows(plain_output)
    final_headers, final_rows = _data_rows(final_output)
    assert final_headers == plain_headers
    assert final_rows[:n] == plain_rows[:n]
