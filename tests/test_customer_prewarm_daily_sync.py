"""customer_prewarm_daily_sync 行为测试(Issue #9 Phase 2,Phase A 每日同步)。

基于真实 per-day state.sqlite3(DailyAuditStore 建 schema + 原始 SQL 填
daily_runs/remote_jobs/crawl_work/audit_items 行)验证:
crawl_work SUCCESS 行按 page_price_status=='OK' 的行级语义同步进
SharedCrawlCache;幂等账本防重复;确定性失败记账不重试;瞬时失败丢 claim
下轮重试;崩溃恢复(link 或 drop 陈旧 claim)。

用例编号对应 Issue #9 验收清单(Case 9-11)。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from app.services.customer_prewarm_discovery import (
    process_document,
    sync_daily_internet_results,
)
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.crawl_url_identity import SKU_STATUS_CONFIRMED
from app.services.daily_audit_store import DEFAULT_POOL_ID, DEFAULT_TENANT_ID, DailyAuditStore
from app.services.shared_crawl_cache import AliasConflictError, SharedCrawlCache
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

SOURCE = "daily-internet"
BUSINESS_DATE = "2026-09-07"

MAPPINGS = [
    ZhaoshangMallMapping(
        mall_name="得力商城",
        source_id="S1",
        mall_key="nbdeli",
        domains=("b2b.nbdeli.com", "nbdeli.com", "www.nbdeli.com", "nbdeli.cn"),
        vendor_keywords=("nbdeli",),
    )
]


def detail_payload(urls):
    return {"data": {"referenceInfos": [{"referenceLink": u} for u in urls]}}


@pytest.fixture()
def prewarm_store(tmp_path):
    return CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")


@pytest.fixture()
def shared_cache(tmp_path):
    return SharedCrawlCache(tmp_path / "shared_cache.sqlite3")


# ---- state.sqlite3 fixture -----------------------------------------------------


def make_state_db(tmp_path, business_date, rows, *, name="state"):
    """建真实 per-day state.sqlite3:DailyAuditStore 建 schema,原始 SQL 填行。

    ``rows`` 每项可指定:mall_key、sku_id、exact_url、result(dict)、
    updated_at、task_name、state(默认 'SUCCESS')。crawl_id 由自增主键分配。
    """
    path = tmp_path / f"{name}-{business_date}.sqlite3"
    DailyAuditStore(path)  # schema creation (idempotent)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """INSERT INTO daily_runs (business_date, tenant_id, pool_id, state,
                                       created_at, updated_at)
               VALUES (?, ?, ?, 'OPEN', ?, ?)""",
            (business_date, DEFAULT_TENANT_ID, DEFAULT_POOL_ID, now, now),
        )
        run_id = int(cursor.lastrowid)
        for index, row in enumerate(rows, start=1):
            task_name = row.get("task_name") or ""
            job_id = None
            if task_name:
                cursor = connection.execute(
                    """INSERT INTO remote_jobs (run_id, logical_key, task_name,
                                                payload_hash, mall_key, state,
                                                created_at, updated_at)
                       VALUES (?, ?, ?, 'payload-hash', ?, 'SUCCEEDED', ?, ?)""",
                    (run_id, f"logical-{index}", task_name, row.get("mall_key") or "", now, now),
                )
                job_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO crawl_work (run_id, mall_key, source_id, sku_id,
                                           exact_url, backend_version, fingerprint,
                                           state, remote_job_id, result_json, updated_at)
                   VALUES (?, ?, '', ?, ?, 'v1', ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    row.get("mall_key") or "",
                    row.get("sku_id") or "",
                    row.get("exact_url") or "",
                    f"fp-{index}",
                    row.get("state", "SUCCESS"),
                    job_id,
                    json.dumps(row["result"], ensure_ascii=False),
                    row.get("updated_at", now),
                ),
            )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return path


def _crawl_rows(path):
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT c.id AS crawl_id, c.mall_key, c.sku_id, c.exact_url, c.result_json,
                      c.updated_at, c.remote_job_id, r.task_name
               FROM crawl_work c LEFT JOIN remote_jobs r ON r.id=c.remote_job_id
               ORDER BY c.id"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _event_key(business_date, task_label, crawl_id, result_json):
    """镜像生产公式:daily-internet:{date}:{task_label}:{crawl_id}:{sha12}。"""
    digest = hashlib.sha256(result_json.encode("utf-8")).hexdigest()[:12]
    return f"{business_date}:{task_label}:{crawl_id}:{digest}"


def _state_event_key(path, business_date, row_index=0):
    row = _crawl_rows(path)[row_index]
    task_label = row["task_name"] or f"crawl-{row['crawl_id']}"
    return _event_key(business_date, task_label, row["crawl_id"], row["result_json"])


def _backdate_ledger_claims(store, created_at):
    connection = sqlite3.connect(str(store.path), isolation_level=None)
    try:
        connection.execute("UPDATE prewarm_sync_ledger SET created_at=?", (created_at,))
    finally:
        connection.close()


def _ok_row(mall_key, sku_id, exact_url, price, updated_at, task_name):
    return {
        "mall_key": mall_key,
        "sku_id": sku_id,
        "exact_url": exact_url,
        "result": {"page_price_status": "OK", "page_price_value": price},
        "updated_at": updated_at,
        "task_name": task_name,
    }


# ---- Case 9: 每日同步后客户 URL 立即命中 ----------------------------------------------


def test_case9_daily_sync_then_customer_hit(tmp_path, prewarm_store, shared_cache):
    url = "https://b2b.nbdeli.com/goods/900.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "SKU-A", url, "12.5", "2026-09-07T02:00:00+00:00", "zhaoshang-nbdeli-01")],
    )
    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert summary["state_db"] == "ok"
    assert summary["scanned"] == 1
    assert summary["ok_synced"] == 1
    hit = shared_cache.lookup_current("nbdeli", url)
    assert hit is not None
    entry = hit["entry"]
    assert entry["price"] == "12.5"
    # succeeded_at 取自 crawl_work.updated_at(时区归一后比较时刻)
    assert datetime.fromisoformat(entry["succeeded_at"]) == datetime.fromisoformat(
        "2026-09-07T02:00:00+00:00"
    )
    assert entry["sku_status"] == SKU_STATUS_CONFIRMED
    assert entry["real_sku_id"] == "SKU-A"
    assert entry["payload"]["source_event_key"] == _state_event_key(path, BUSINESS_DATE)
    # 客户 referenceLink 发现当轮即可复用刚同步的条目
    prewarm_store.upsert_document(BUSINESS_DATE, "DOC-1", "CAT-1")
    result = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=_detail_fetch({"DOC-1": detail_payload([url])}),
    )
    assert result["detail_ok"] is True
    assert result["hits"] == 1 and result["misses"] == 0
    assert prewarm_store.list_unique_cache_misses() == []


def _detail_fetch(payload_by_document):
    def detail_fetch(document_id, four_category_id):
        payload = payload_by_document.get(document_id)
        if payload is None:
            return {"ok": False, "payload": None, "error": "no detail"}
        return {"ok": True, "payload": payload, "error": None}

    return detail_fetch


# ---- Case 10: 同步幂等 ---------------------------------------------------------------


def test_case10_sync_idempotent(tmp_path, prewarm_store, shared_cache):
    url = "https://b2b.nbdeli.com/goods/901.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "SKU-B", url, "4.40", "2026-09-07T02:00:00+00:00", "task-b")],
    )
    first = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert first["ok_synced"] == 1
    history = shared_cache.history("nbdeli", url)
    assert len(history) == 1
    second = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert second["already_synced"] == 1
    assert second["ok_synced"] == 0
    assert len(shared_cache.history("nbdeli", url)) == 1
    assert prewarm_store.stats()["ledger"]["completed"] == 1
    ledger = prewarm_store.ledger_get(SOURCE, _state_event_key(path, BUSINESS_DATE))
    assert ledger is not None and ledger["cache_result_id"] is not None


# ---- Case 11: 同一 crawl 行真实新导出 → 追加历史 ------------------------------------------


def test_case11_new_real_crawl_appends_history(tmp_path, prewarm_store, shared_cache):
    url = "https://b2b.nbdeli.com/goods/902.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "SKU-C", url, "12.5", "2026-09-07T02:00:00+00:00", "task-c")],
    )
    sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    # 原地改写同一 crawl 行:一次真正的新导出(新价格、新 updated_at)→ 新 event key
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute(
            "UPDATE crawl_work SET result_json=?, updated_at=? WHERE exact_url=?",
            (
                json.dumps({"page_price_status": "OK", "page_price_value": "15.0"}),
                "2026-09-07T05:00:00+00:00",
                url,
            ),
        )
    finally:
        connection.close()
    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert summary["already_synced"] == 0
    assert summary["ok_synced"] == 1
    history = shared_cache.history("nbdeli", url)
    assert len(history) == 2  # 历史追加,不覆盖
    assert shared_cache.lookup_current("nbdeli", url)["entry"]["price"] == "15.0"  # 最新者胜


# ---- 非 OK 行与非法行跳过 ---------------------------------------------------------------


def test_non_ok_and_invalid_rows_skipped(tmp_path, prewarm_store, shared_cache):
    url_zero = "https://b2b.nbdeli.com/goods/905.html"
    url_failed = "https://b2b.nbdeli.com/goods/906.html"
    url_invalid = "https://unknown.example/item/1"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [
            {
                "mall_key": "nbdeli",
                "sku_id": "S1",
                "exact_url": url_zero,
                "result": {"page_price_status": "PRICE_IS_ZERO", "page_price_value": "0"},
                "task_name": "task-zero",
            },
            {
                "mall_key": "nbdeli",
                "sku_id": "S2",
                "exact_url": url_failed,
                "result": {"page_price_status": "FAILED"},
                "task_name": "task-failed",
            },
            {  # mall_key 为空 → 非法行
                "mall_key": "",
                "sku_id": "S3",
                "exact_url": url_invalid,
                "result": {"page_price_status": "OK", "page_price_value": "3.3"},
                "task_name": "task-invalid",
            },
        ],
    )
    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert summary["scanned"] == 3
    assert summary["skipped_non_ok"] == 2
    assert summary["skipped_invalid"] == 1
    assert summary["ok_synced"] == 0
    # 缓存中什么都不落
    assert shared_cache.lookup_current("nbdeli", url_zero) is None
    assert shared_cache.lookup_current("nbdeli", url_failed) is None
    # 非法行走确定性失败账本
    assert prewarm_store.stats()["ledger"]["errored"] == 1
    state_rows = _crawl_rows(path)
    invalid_row = state_rows[2]
    key = _event_key(BUSINESS_DATE, "task-invalid", invalid_row["crawl_id"], invalid_row["result_json"])
    ledger = prewarm_store.ledger_get(SOURCE, key)
    assert ledger is not None
    assert ledger["error"].startswith("invalid crawl row")


# ---- dry-run ---------------------------------------------------------------------------


def test_dry_run_records_nothing_would_record(tmp_path, prewarm_store, shared_cache):
    url = "https://b2b.nbdeli.com/goods/907.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "SKU-D", url, "9.90", "2026-09-07T02:00:00+00:00", "task-d")],
    )
    summary = sync_daily_internet_results(
        prewarm_store,
        shared_cache,
        state_db_path=path,
        business_date=BUSINESS_DATE,
        dry_run=True,
    )
    assert summary["would_record"] == 1
    assert summary["ok_synced"] == 0
    assert shared_cache.lookup_current("nbdeli", url) is None
    key = _state_event_key(path, BUSINESS_DATE)
    ledger = prewarm_store.ledger_get(SOURCE, key)
    assert ledger is not None
    assert ledger["error"] == "dry_run"
    assert ledger["cache_result_id"] is None


# ---- 瞬时失败:丢 claim,下轮重试 ------------------------------------------------------------


def test_transient_failure_retries_next_scan(
    tmp_path, prewarm_store, shared_cache, monkeypatch
):
    url = "https://b2b.nbdeli.com/goods/908.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "SKU-E", url, "6.60", "2026-09-07T02:00:00+00:00", "task-e")],
    )

    def locked_record_success(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(shared_cache, "record_success", locked_record_success)
    first = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert first["transient_failures"] == 1
    assert first["ok_synced"] == 0
    key = _state_event_key(path, BUSINESS_DATE)
    # claim 已被丢弃:账本无痕,下一轮允许重记
    assert prewarm_store.ledger_get(SOURCE, key) is None
    assert prewarm_store.stats()["ledger"] == {"pending": 0, "completed": 0, "errored": 0}
    monkeypatch.undo()
    second = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert second["ok_synced"] == 1
    assert second["transient_failures"] == 0
    assert len(shared_cache.history("nbdeli", url)) == 1


# ---- 确定性失败:记账不重试 ------------------------------------------------------------------


def test_deterministic_failure_ledgered_not_retried(
    tmp_path, prewarm_store, shared_cache, monkeypatch
):
    url = "https://b2b.nbdeli.com/goods/909.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "SKU-F", url, "8.80", "2026-09-07T02:00:00+00:00", "task-f")],
    )

    def conflicting_record_success(*args, **kwargs):
        raise AliasConflictError("alias conflict boom")

    monkeypatch.setattr(shared_cache, "record_success", conflicting_record_success)
    first = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert first["deterministic_failures"] == 1
    assert first["ok_synced"] == 0
    key = _state_event_key(path, BUSINESS_DATE)
    ledger = prewarm_store.ledger_get(SOURCE, key)
    assert ledger is not None
    assert "alias conflict boom" in (ledger["error"] or "")
    monkeypatch.undo()
    second = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert second["errored_before"] == 1  # 记账行不重试
    assert second["ok_synced"] == 0
    assert shared_cache.history("nbdeli", url) == []


# ---- 崩溃恢复:link 或 drop 陈旧 claim -------------------------------------------------------


def test_crash_recovery_links_or_drops_stale_claims(tmp_path, prewarm_store, shared_cache):
    url_a = "https://b2b.nbdeli.com/goods/910.html"
    url_b = "https://b2b.nbdeli.com/goods/911.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [
            _ok_row("nbdeli", "S1", url_a, "7.7", "2026-09-07T01:00:00+00:00", "task-a"),
            _ok_row("nbdeli", "S2", url_b, "8.8", "2026-09-07T01:30:00+00:00", "task-b"),
        ],
    )
    state_rows = _crawl_rows(path)
    key_a = _event_key(
        BUSINESS_DATE, "task-a", state_rows[0]["crawl_id"], state_rows[0]["result_json"]
    )
    key_b = _event_key(
        BUSINESS_DATE, "task-b", state_rows[1]["crawl_id"], state_rows[1]["result_json"]
    )
    # 场景 A:record_success 已提交但 ledger 未 finalize(进程崩溃在两者之间)
    seeded = shared_cache.record_success(
        mall="nbdeli",
        submitted_url=url_a,
        real_sku_id="S1",
        sku_status=SKU_STATUS_CONFIRMED,
        price="7.7",
        source=SOURCE,
        succeeded_at="2026-09-07T01:00:00+00:00",
        payload={"source_event_key": key_a, "business_date": BUSINESS_DATE},
    )
    assert len(shared_cache.history("nbdeli", url_a)) == 1
    # 两个陈旧的未 finalize claim(直接写库把 created_at 拨回过去)
    prewarm_store.ledger_claim(SOURCE, key_a)
    prewarm_store.ledger_claim(SOURCE, key_b)
    _backdate_ledger_claims(prewarm_store, "2026-09-06T00:00:00+00:00")

    now = datetime(2026, 9, 7, 3, 0, 0, tzinfo=UTC)
    summary = sync_daily_internet_results(
        prewarm_store,
        shared_cache,
        state_db_path=path,
        business_date=BUSINESS_DATE,
        now=now,
    )
    assert summary["recovered_claims"] == 1  # key_a:自检命中,补链
    assert summary["dropped_claims"] == 1  # key_b:自检未命中,丢弃允许重记
    linked = prewarm_store.ledger_get(SOURCE, key_a)
    assert linked["cache_result_id"] == seeded["id"]  # 补链到既有缓存行
    assert summary["already_synced"] == 1  # 行 A 不重复写入
    assert len(shared_cache.history("nbdeli", url_a)) == 1  # 无重复行
    assert summary["ok_synced"] == 1  # 行 B 重记成功
    assert len(shared_cache.history("nbdeli", url_b)) == 1
    assert prewarm_store.stats()["ledger"] == {"pending": 0, "completed": 2, "errored": 0}


# ---- state db 缺失 -------------------------------------------------------------------------


def test_missing_state_db_returns_missing_not_raise(tmp_path, prewarm_store, shared_cache):
    summary = sync_daily_internet_results(
        prewarm_store,
        shared_cache,
        state_db_path=tmp_path / "missing.sqlite3",
        business_date=BUSINESS_DATE,
    )
    assert summary["state_db"] == "missing"
    assert summary["scanned"] == 0
    assert summary["ok_synced"] == 0


# ---- find_id_by_source_event_key(SharedCrawlCache 新增探针) ---------------------------------


def test_find_id_by_source_event_key(tmp_path, shared_cache):
    assert shared_cache.find_id_by_source_event_key(SOURCE, "evt-none") is None
    first = shared_cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/920.html",
        source=SOURCE,
        payload={"source_event_key": "evt-1"},
    )
    second = shared_cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/921.html",
        source=SOURCE,
        payload={"note": "evt-1 embedded twice"},
    )
    assert shared_cache.find_id_by_source_event_key(SOURCE, "evt-1") == max(
        first["id"], second["id"]
    )
    # 其他 source 不可见;空输入守卫
    assert shared_cache.find_id_by_source_event_key("other-source", "evt-1") is None
    assert shared_cache.find_id_by_source_event_key(SOURCE, "   ") is None
    assert shared_cache.find_id_by_source_event_key("", "evt-1") is None
