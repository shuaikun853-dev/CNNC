"""zhaoshang-history 历史回填门禁(Issue #10 Review Round 2, Finding 2)。

只允许"真证据链"入缓存:remote_jobs(EXPORTED/COMPLETED + export_path)经
真实 ``CnncLegacyZhaoshangAdapter.harvest_existing`` 完整溯源门(批次号 ==
task_name、商城标识 == mall_key、(sku,url) 多重集相等)重验,且同一
``(sku_id, exact_url)`` 身份必须绑定到该任务的 SUCCESS crawl 行
(page_price_status == OK)—— 双证合一才记账。种子用真实 DailyAuditStore +
lease 建 ``prepare_remote_intent → claim → reconcile(EXPORTED, export_path)
→ ensure_crawl_work → link → complete_crawl_work`` 链和真实工作簿
(openpyxl,零 monkeypatch、零远端调用)。

覆盖:happy path(SKU 编码/payload 嵌入 producer key)、重跑幂等、与
daily-internet 同步的跨源去重、导出工作簿缺失 → partial、无绑定的 SUCCESS
行永不入缓存(Finding 2 核心语义)、绝不 submit/prioritize/reconcile、
占位 SKU 降级 UNRESOLVED 且价格结果不丢。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from openpyxl import Workbook

from app.services.customer_prewarm_discovery import sync_daily_internet_results
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.crawl_url_identity import (
    SKU_STATUS_UNRESOLVED,
    SKU_STATUS_UPSTREAM_PROVIDED,
)
from app.services.daily_audit_store import DailyAuditStore
from app.services.legacy_zhaoshang_adapter import (
    CrawlRequest,
    CnncLegacyZhaoshangAdapter,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from app.services.zhaoshang_history_backfill import (
    BACKFILL_SOURCE,
    backfill_zhaoshang_history,
)
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

BUSINESS_DATE = "2026-09-07"
MALL = "jd"
SKU = "SKU-1"
URL = "https://item.jd.com/18a.html"
PRICE = "6.6"
BACKEND_VERSION = "legacy-v1"

MAPPING = ZhaoshangMallMapping(
    mall_name="京东",
    source_id="1",
    mall_key="jd",
    domains=("jd.com",),
    vendor_keywords=("jd", "京东"),
)


def make_task_name(index: int) -> str:
    """生产形状的任务名(批次号),索引保证同日内唯一。"""
    return f"cnnc_current_opt_京东_2026090718{index:014d}"


def _write_provenance_result(
    path: Path,
    rows,
    *,
    task_name: str,
    mall_key: str,
) -> None:
    """导出工作簿:商品编码/原始链接/商品价格 + 执行编号/批次号/商城标识溯源列。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["商品编码", "原始链接", "商品价格", "执行编号", "批次号", "商城标识"])
    for index, row in enumerate(rows):
        code, url_value, price_value = row
        sheet.append([code, url_value, price_value, f"exec-{index}", task_name, mall_key])
    workbook.save(path)


def seed_exported_zhaoshang_job(
    runtime_root: Path,
    business_date: str,
    *,
    sku_id: str = SKU,
    url: str = URL,
    mall_key: str = MALL,
    price: str = PRICE,
    task_name: str | None = None,
    source_id: str = "doc-1",
) -> dict:
    """真实证据链种子:DailyAuditStore 远端任务 + 绑定 crawl 行 + 真实工作簿。"""
    state_path = runtime_root / business_date / "state.sqlite3"
    store = DailyAuditStore(state_path)
    token = store.acquire_lease(business_date, "seed-zhaoshang-history", 120.0)
    job_dir = runtime_root / business_date / "zhaoshang" / mall_key / "00000000000001"
    job_dir.mkdir(parents=True, exist_ok=True)
    batch_state = runtime_root / business_date / "BATCH_STATE.md"
    if not batch_state.is_file():
        batch_state.write_text("# state\n", encoding="utf-8")
    task_value = task_name or make_task_name(1)
    request = CrawlRequest(
        mall_key=mall_key,
        mall_source_id=MAPPING.source_id,
        mall_name=MAPPING.mall_name,
        sku_id=sku_id,
        url=url,
        backend_version=BACKEND_VERSION,
    )
    adapter = CnncLegacyZhaoshangAdapter(mappings=[MAPPING], db_env_path=None)
    prepared = adapter.prepare_job(
        business_date,
        (request,),
        job_dir / "input.xlsx",
        job_dir / "result.xlsx",
        task_value,
        batch_state_path=batch_state,
    )
    job_row = store.prepare_remote_intent(
        token,
        prepared.logical_job_key,
        prepared.task_name,
        prepared.payload_sha256,
        mall_key=prepared.mall_key,
        input_path=str(prepared.template_path),
        result_path=str(prepared.result_path),
        payload_json={
            "requests": [
                {
                    "mall_key": request.mall_key,
                    "mall_source_id": request.mall_source_id,
                    "mall_name": request.mall_name,
                    "sku_id": request.sku_id,
                    "url": request.url,
                    "backend_version": request.backend_version,
                }
            ],
            "batch_state": str(batch_state),
        },
    )
    store.claim_remote_submission(token, int(job_row["id"]))
    crawl = store.ensure_crawl_work(
        token,
        mall_key,
        sku_id,
        url,
        BACKEND_VERSION,
        f"fp-{task_value}",
        source_id=source_id,
    )
    store.link_crawl_work_to_remote_job(token, int(crawl["id"]), int(job_row["id"]))
    store.reconcile_remote_job(
        token,
        int(job_row["id"]),
        remote_identity=f"{mall_key}|task-id-{task_value}|task-no-{task_value}",
        state="EXPORTED",
        export_path=str(job_dir / "result.xlsx"),
    )
    _write_provenance_result(
        job_dir / "result.xlsx",
        [[sku_id, url, price]],
        task_name=task_value,
        mall_key=mall_key,
    )
    store.complete_crawl_work(
        token,
        int(crawl["id"]),
        {"page_price_status": "OK", "page_price_value": price},
    )
    store.release_lease(token)
    return {
        "job_id": int(job_row["id"]),
        "crawl_id": int(crawl["id"]),
        "task_name": prepared.task_name,
        "result_path": job_dir / "result.xlsx",
        "state_path": state_path,
    }


def run_backfill(runtime_root: Path, dates, *, adapter=None) -> dict:
    return backfill_zhaoshang_history(
        CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3"),
        SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3"),
        runtime_root=runtime_root,
        business_dates=list(dates),
        adapter=adapter,
    )


def _cache_row_count(cache_path: Path) -> int:
    connection = sqlite3.connect(str(cache_path))
    try:
        return int(connection.execute("SELECT COUNT(*) FROM crawl_results").fetchone()[0])
    finally:
        connection.close()


def _crawl_result_json(state_path: Path, crawl_id: int) -> str:
    connection = sqlite3.connect(str(state_path))
    try:
        return str(
            connection.execute(
                "SELECT result_json FROM crawl_work WHERE id=?", (crawl_id,)
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _expected_event_key(business_date: str, task_name: str, crawl_id: int, raw: str) -> str:
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{business_date}:{task_name}:{crawl_id}:{digest}"


# ---- Case 1:happy path —— 双证合一入缓存 --------------------------------------------


def test_case1_happy_path_records_only_cross_proven_row(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    seeded = seed_exported_zhaoshang_job(runtime_root, BUSINESS_DATE)

    summary = run_backfill(runtime_root, [BUSINESS_DATE])

    assert summary["partial"] is False
    assert summary["dates_scanned"] == 1
    assert summary["jobs_examined"] == 1
    assert summary["jobs_validated"] == 1
    assert summary["jobs_partial"] == []
    assert summary["rows_examined"] == 1
    assert summary["rows_recorded"] == 1
    assert summary["already_ingested_cross_source"] == 0

    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    hit = cache.lookup_current(MALL, URL)
    assert hit is not None
    entry = hit["entry"]
    assert entry["source"] == BACKFILL_SOURCE
    assert entry["sku_status"] == SKU_STATUS_UPSTREAM_PROVIDED
    assert entry["real_sku_id"] == SKU
    assert entry["price"] == PRICE
    raw = _crawl_result_json(seeded["state_path"], seeded["crawl_id"])
    expected_key = _expected_event_key(
        BUSINESS_DATE, seeded["task_name"], seeded["crawl_id"], raw
    )
    assert entry["payload"]["source_event_key"] == expected_key
    assert entry["payload"]["request_sku_id"] == SKU
    assert entry["payload"]["crawl_work_id"] == seeded["crawl_id"]
    assert entry["payload"]["remote_job_id"] == seeded["job_id"]


# ---- Case 2:重跑幂等 ----------------------------------------------------------------


def test_case2_rerun_is_idempotent(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    seed_exported_zhaoshang_job(runtime_root, BUSINESS_DATE)

    first = run_backfill(runtime_root, [BUSINESS_DATE])
    assert first["rows_recorded"] == 1
    rows_after_first = _cache_row_count(runtime_root / "shared_crawl_cache.sqlite3")
    assert rows_after_first == 1

    second = run_backfill(runtime_root, [BUSINESS_DATE])

    assert second["partial"] is False
    assert second["rows_recorded"] == 0
    assert second["already_ingested_cross_source"] == 1
    assert _cache_row_count(runtime_root / "shared_crawl_cache.sqlite3") == rows_after_first


# ---- Case 3:跨源去重 —— 同一真实导出成功绝不重复追加 --------------------------------


def test_case3_cross_source_dedupe_with_daily_sync(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    seeded = seed_exported_zhaoshang_job(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")

    sync_summary = sync_daily_internet_results(
        prewarm,
        cache,
        state_db_path=seeded["state_path"],
        business_date=BUSINESS_DATE,
    )
    assert sync_summary["ok_synced"] == 1
    rows_after_sync = _cache_row_count(runtime_root / "shared_crawl_cache.sqlite3")
    assert rows_after_sync == 1

    summary = run_backfill(runtime_root, [BUSINESS_DATE])

    assert summary["rows_recorded"] == 0
    assert summary["already_ingested_cross_source"] >= 1
    assert _cache_row_count(runtime_root / "shared_crawl_cache.sqlite3") == rows_after_sync


# ---- Case 4:导出工作簿缺失 → partial(隔离,绝不崩溃、绝不入缓存)--------------------


def test_case4_missing_export_workbook_is_isolated_partial(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    seeded = seed_exported_zhaoshang_job(runtime_root, BUSINESS_DATE)
    Path(seeded["result_path"]).unlink()

    summary = run_backfill(runtime_root, [BUSINESS_DATE])

    assert summary["partial"] is True
    assert len(summary["jobs_partial"]) == 1
    item = summary["jobs_partial"][0]
    assert item["job_id"] == seeded["job_id"]
    assert item["task_name"] == seeded["task_name"]
    assert "harvest status=FAILED" in item["reason"]
    assert summary["jobs_validated"] == 0
    assert summary["rows_examined"] == 0
    assert summary["rows_recorded"] == 0
    assert _cache_row_count(runtime_root / "shared_crawl_cache.sqlite3") == 0


# ---- Case 5(核心语义):无绑定的 SUCCESS 行永不入缓存 --------------------------------


def test_case5_unbound_success_row_is_never_recorded(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    store = DailyAuditStore(runtime_root / BUSINESS_DATE / "state.sqlite3")
    token = store.acquire_lease(BUSINESS_DATE, "seed-unbound", 120.0)
    crawl = store.ensure_crawl_work(
        token, MALL, SKU, URL, BACKEND_VERSION, "fp-unbound", source_id="doc-x"
    )
    store.complete_crawl_work(
        token, int(crawl["id"]), {"page_price_status": "OK", "page_price_value": PRICE}
    )
    store.release_lease(token)
    connection = sqlite3.connect(str(runtime_root / BUSINESS_DATE / "state.sqlite3"))
    try:
        bound, state = connection.execute(
            "SELECT remote_job_id, state FROM crawl_work WHERE id=?", (int(crawl["id"]),)
        ).fetchone()
    finally:
        connection.close()
    assert bound is None and state == "SUCCESS"  # 前置成立:行真实存在且未绑定

    summary = run_backfill(runtime_root, [BUSINESS_DATE])

    assert summary["partial"] is False
    assert summary["jobs_examined"] == 0
    assert summary["rows_examined"] == 0
    assert summary["rows_recorded"] == 0
    assert _cache_row_count(runtime_root / "shared_crawl_cache.sqlite3") == 0


# ---- Case 6:绝不 create / prioritize / reconcile ------------------------------------


def test_case6_backfill_never_submits_prioritizes_or_reconciles(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    seed_exported_zhaoshang_job(runtime_root, BUSINESS_DATE)

    class _ForbiddenRemoteAdapter(CnncLegacyZhaoshangAdapter):
        def submit(self, job, *, timeout_seconds):
            raise AssertionError("backfill must never submit a remote task")

        def prioritize(self, job, **kwargs):
            raise AssertionError("backfill must never prioritize a remote task")

        def reconcile_unknown(self, job, *, timeout_seconds):
            raise AssertionError("backfill must never reconcile a remote task")

    summary = run_backfill(
        runtime_root,
        [BUSINESS_DATE],
        adapter=_ForbiddenRemoteAdapter(mappings=[MAPPING], db_env_path=None),
    )

    assert summary["partial"] is False
    assert summary["rows_recorded"] == 1


# ---- Case 7:占位 SKU 降级 UNRESOLVED,价格结果不丢 -----------------------------------


def test_case7_placeholder_sku_recorded_unresolved_with_price_payload(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    placeholder = "zhpq-abc123"
    seed_exported_zhaoshang_job(runtime_root, BUSINESS_DATE, sku_id=placeholder)

    summary = run_backfill(runtime_root, [BUSINESS_DATE])

    assert summary["rows_recorded"] == 1
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    entry = cache.lookup_current(MALL, URL)["entry"]
    assert entry["sku_status"] == SKU_STATUS_UNRESOLVED
    assert entry["real_sku_id"] is None
    assert entry["payload"]["request_sku_id"] == placeholder
    assert entry["price"] == PRICE
