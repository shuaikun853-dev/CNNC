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
import inspect
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import app.services.customer_prewarm_discovery as discovery_module
from app.services.customer_prewarm_discovery import (
    backfill_daily_history,
    is_temporary_request_code,
    process_document,
    sync_daily_internet_results,
    upgrade_upstream_sku_evidence,
)
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.crawl_url_identity import (
    SKU_STATUS_CONFIRMED,
    SKU_STATUS_UNRESOLVED,
    SKU_STATUS_UPSTREAM_PROVIDED,
    SKU_STATUS_URL_PARSED_UNVERIFIED,
    is_placeholder_code,
)
from app.services.daily_audit_store import DEFAULT_POOL_ID, DEFAULT_TENANT_ID, DailyAuditStore, ProcessLock
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


def make_state_db(tmp_path, business_date, rows, *, name="state", path=None):
    """建真实 per-day state.sqlite3:DailyAuditStore 建 schema,原始 SQL 填行。

    ``rows`` 每项可指定:mall_key、sku_id、exact_url、result(dict)、
    updated_at、task_name、state(默认 'SUCCESS')。crawl_id 由自增主键分配。
    ``path`` 可显式指定输出文件(如 <runtime_root>/<date>/state.sqlite3),
    缺省为 ``tmp_path/{name}-{business_date}.sqlite3``。
    """
    if path is None:
        path = tmp_path / f"{name}-{business_date}.sqlite3"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    # Finding B(SPEC_CONFLICT 已于 2026-09-07 由用户决策解决):请求/模板
    # 回显的 sku_id 是上游权威输入 → 诚实编码 UPSTREAM_PROVIDED(显式非
    # CONFIRMED),原值同时保留在 payload["request_sku_id"]。
    assert entry["sku_status"] == SKU_STATUS_UPSTREAM_PROVIDED
    assert entry["real_sku_id"] == "SKU-A"


    assert entry["payload"]["request_sku_id"] == "SKU-A"
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


def test_daily_sync_persists_page_title_confirmed_sku_and_comment(tmp_path, prewarm_store, shared_cache):
    url = "https://item.jd.com/100311804305.html"
    row = _ok_row(
        "jd", "zhpq-placeholder", url, "63420.5",
        "2026-09-07T02:00:00+00:00", "zhaoshang-jd-01",
    )
    row["result"].update({
        "page_sku_id": "100311804305",
        "page_title": "亿联 MeetingEye 500",
        "comment_count": 321,
        "comment_count_raw": "321",
        "channel": "jd",
    })
    path = make_state_db(tmp_path, BUSINESS_DATE, [row])

    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )

    assert summary["ok_synced"] == 1
    entry = shared_cache.lookup_current("jd", url)["entry"]
    assert entry["sku_status"] == SKU_STATUS_CONFIRMED
    assert entry["real_sku_id"] == "100311804305"
    assert entry["title"] == "亿联 MeetingEye 500"
    assert entry["price"] == "63420.5"
    assert entry["comment_count"] == 321


def test_sync_uses_one_store_wide_lock_across_entrypoints(tmp_path, prewarm_store, shared_cache):
    url = "https://b2b.nbdeli.com/goods/sync-race.html"
    path = make_state_db(
        tmp_path, BUSINESS_DATE,
        [_ok_row("nbdeli", "SKU-R", url, "13.5", "2026-09-07T02:00:00+00:00", "race-task")],
    )
    lock = ProcessLock(prewarm_store.path.with_name(f"{prewarm_store.path.name}.sync.lock"))
    lock.acquire()
    try:
        deferred = sync_daily_internet_results(
            prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
        )
    finally:
        lock.release()
    assert deferred["state_db"] == "deferred"
    assert deferred["ok_synced"] == 0
    assert shared_cache.lookup_current("nbdeli", url) is None
    retry = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert retry["ok_synced"] == 1


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


# ---- 同步总是真实写入:不再存在 dry_run 旁路 ---------------------------------------------


def test_sync_records_into_given_cache_and_never_writes_dry_run_ledger_error(
    tmp_path, prewarm_store, shared_cache
):
    """sync 永远写入调用方传入的缓存(沙箱安全由 CLI 组合路径保证);
    任何路径都不再产生 'dry_run' 账本错误。"""
    assert "dry_run" not in inspect.signature(sync_daily_internet_results).parameters
    url = "https://b2b.nbdeli.com/goods/907.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "SKU-D", url, "9.90", "2026-09-07T02:00:00+00:00", "task-d")],
    )
    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert "would_record" not in summary
    assert summary["ok_synced"] == 1
    assert shared_cache.lookup_current("nbdeli", url) is not None
    key = _state_event_key(path, BUSINESS_DATE)
    ledger = prewarm_store.ledger_get(SOURCE, key)
    assert ledger is not None
    assert ledger["cache_result_id"] is not None
    assert not ledger["error"]
    # 全库无任何 'dry_run' 账本错误行
    connection = sqlite3.connect(str(prewarm_store.path))
    try:
        dry_run_rows = connection.execute(
            "SELECT COUNT(*) FROM prewarm_sync_ledger WHERE error='dry_run'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert dry_run_rows == 0


# ---- 历史回填(Finding 5) ---------------------------------------------------------------


def _seed_history_day(runtime_root, business_date, rows):
    """在 <runtime_root>/<business_date>/state.sqlite3 造一天的历史状态库。"""
    return make_state_db(
        runtime_root,
        business_date,
        rows,
        path=Path(runtime_root) / business_date / "state.sqlite3",
    )


def test_backfill_history_idempotent_and_resumable(tmp_path, prewarm_store, shared_cache, monkeypatch):
    runtime_root = tmp_path / "runtime"
    d2, d_missing, d1 = "2026-09-05", "2026-09-06", "2026-09-07"
    url_a = "https://b2b.nbdeli.com/goods/930.html"
    url_b = "https://b2b.nbdeli.com/goods/931.html"
    path_d2 = _seed_history_day(
        runtime_root,
        d2,
        [_ok_row("nbdeli", "S1", url_a, "1.1", "2026-09-05T02:00:00+00:00", "task-a")],
    )
    path_d1 = _seed_history_day(
        runtime_root,
        d1,
        [_ok_row("nbdeli", "S2", url_b, "2.2", "2026-09-07T02:00:00+00:00", "task-b")],
    )
    dates = [d2, d_missing, d1]

    first = backfill_daily_history(
        prewarm_store, shared_cache, runtime_root=runtime_root, business_dates=dates
    )
    assert first["dates_scanned"] == 2
    assert first["dates_missing"] == [d_missing]
    assert first["errors"] == []
    assert first["scanned"] == 2
    assert first["ok_synced"] == 2
    assert set(first["per_date"]) == {d2, d_missing, d1}
    assert first["per_date"][d2]["ok_synced"] == 1
    assert first["per_date"][d1]["ok_synced"] == 1
    assert first["per_date"][d_missing]["state_db"] == "missing"
    assert len(shared_cache.history("nbdeli", url_a)) == 1
    assert len(shared_cache.history("nbdeli", url_b)) == 1

    # 再跑一遍:零新缓存行,已同步行按 already_synced 计数
    second = backfill_daily_history(
        prewarm_store, shared_cache, runtime_root=runtime_root, business_dates=dates
    )
    assert second["ok_synced"] == 0
    assert second["already_synced"] == 2
    assert len(shared_cache.history("nbdeli", url_a)) == 1
    assert len(shared_cache.history("nbdeli", url_b)) == 1

    # 中断模拟:第二个 date 的同步直接抛错 → 该日计入 errors,其余日照常处理
    real_sync = discovery_module.sync_daily_internet_results
    calls = {"n": 0}

    def interrupted_sync(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated interruption")
        return real_sync(*args, **kwargs)

    monkeypatch.setattr(discovery_module, "sync_daily_internet_results", interrupted_sync)
    third = backfill_daily_history(
        prewarm_store, shared_cache, runtime_root=runtime_root, business_dates=dates
    )
    monkeypatch.undo()
    assert len(third["errors"]) == 1
    assert third["errors"][0].startswith(f"{d_missing}: ")
    assert "simulated interruption" in third["errors"][0]
    assert third["per_date"][d2]["already_synced"] == 1  # 报错日之前照常
    assert third["per_date"][d1]["already_synced"] == 1  # 报错日之后不中断

    # 修复后重跑:每一行恰好记录一次(无重复,可续跑)
    fourth = backfill_daily_history(
        prewarm_store, shared_cache, runtime_root=runtime_root, business_dates=dates
    )
    assert fourth["ok_synced"] == 0
    assert fourth["already_synced"] == 2
    assert fourth["errors"] == []
    assert len(shared_cache.history("nbdeli", url_a)) == 1
    assert len(shared_cache.history("nbdeli", url_b)) == 1
    # 幂等账本落位在真实 per-day state 库对应的 event key 上
    for path, date in ((path_d2, d2), (path_d1, d1)):
        key = _state_event_key(path, date)
        ledger = prewarm_store.ledger_get(SOURCE, key)
        assert ledger is not None and ledger["cache_result_id"] is not None


def test_backfill_never_terminal_on_transient(tmp_path, prewarm_store, shared_cache, monkeypatch):
    runtime_root = tmp_path / "runtime"
    d = "2026-09-06"
    url = "https://b2b.nbdeli.com/goods/932.html"
    path = _seed_history_day(
        runtime_root,
        d,
        [_ok_row("nbdeli", "S3", url, "3.3", "2026-09-06T02:00:00+00:00", "task-t")],
    )

    def locked_record_success(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(shared_cache, "record_success", locked_record_success)
    first = backfill_daily_history(
        prewarm_store, shared_cache, runtime_root=runtime_root, business_dates=[d]
    )
    monkeypatch.undo()
    assert first["dates_scanned"] == 1
    assert first["transient_failures"] == 1
    assert first["ok_synced"] == 0
    assert first["errors"] == []
    key = _state_event_key(path, d)
    # claim 已丢弃:不记 ledger,下一轮允许重试(回填永不终态化)
    assert prewarm_store.ledger_get(SOURCE, key) is None
    second = backfill_daily_history(
        prewarm_store, shared_cache, runtime_root=runtime_root, business_dates=[d]
    )
    assert second["ok_synced"] == 1
    assert second["transient_failures"] == 0
    assert len(shared_cache.history("nbdeli", url)) == 1


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


# ---- Finding B (RESOLVED 2026-09-07): 上游 sku 编码 ---------------------------------------


def test_upstream_saas_sku_becomes_upstream_provided(tmp_path, prewarm_store, shared_cache):
    """上游 SaaS 输入的真实 sku → UPSTREAM_PROVIDED(显式非 CONFIRMED)。"""
    url = "https://b2b.nbdeli.com/goods/933.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "SKU-77", url, "4.4", "2026-09-07T02:00:00+00:00", "task-sku77")],
    )
    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert summary["ok_synced"] == 1
    hit = shared_cache.lookup_current("nbdeli", url)
    assert hit is not None
    entry = hit["entry"]
    # 上游证据诚实编码;绝不是页面证据 CONFIRMED
    assert entry["sku_status"] == SKU_STATUS_UPSTREAM_PROVIDED
    assert entry["real_sku_id"] == "SKU-77"
    assert entry["payload"]["request_sku_id"] == "SKU-77"


def test_upstream_empty_sku_stays_unresolved(tmp_path, prewarm_store, shared_cache):
    """上游 sku 为空 → 无任何 SKU 证据,保持 UNRESOLVED。"""
    url = "https://b2b.nbdeli.com/goods/943.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "", url, "7.7", "2026-09-07T02:00:00+00:00", "task-empty")],
    )
    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert summary["ok_synced"] == 1
    hit = shared_cache.lookup_current("nbdeli", url)
    assert hit is not None
    assert hit["entry"]["sku_status"] == SKU_STATUS_UNRESOLVED
    assert hit["entry"]["real_sku_id"] is None
    assert hit["entry"]["payload"]["request_sku_id"] == ""


def test_zhpq_upstream_never_real_sku(tmp_path, prewarm_store, shared_cache):
    """zhpq-* 占位码降级为 UNRESOLVED,但有效价格结果绝不丢失。"""
    url = "https://b2b.nbdeli.com/goods/934.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "zhpq-9", url, "1.1", "2026-09-07T02:00:00+00:00", "task-p1")],
    )
    # 占位码回显同步不崩、不落 real_sku_id,价格行照常落库
    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert summary["ok_synced"] == 1
    hit = shared_cache.lookup_current("nbdeli", url)
    assert hit is not None
    assert hit["entry"]["real_sku_id"] is None
    assert hit["entry"]["sku_status"] == SKU_STATUS_UNRESOLVED
    # 被拒的临时码原值保留(payload 溯源),价格仍在
    assert hit["entry"]["payload"]["request_sku_id"] == "zhpq-9"
    assert hit["entry"]["price"] == "1.1"
    # #7 的占位码防御原样保留:claimed SKU 的占位码直接 ValueError
    with pytest.raises(ValueError, match="placeholder"):
        shared_cache.record_success(
            mall="nbdeli",
            submitted_url="https://b2b.nbdeli.com/goods/936.html",
            real_sku_id="zhpq-1",
            sku_status=SKU_STATUS_CONFIRMED,
            source=SOURCE,
        )
    # 与 #7 冻结前缀一致
    assert is_placeholder_code("zhpq-9")
    assert not is_placeholder_code("SKU-77")


def test_row_upstream_never_real_sku(tmp_path, prewarm_store, shared_cache):
    """STEP3 模板兜底形状 ROW<n>(任意大小写)永不成为真实 SKU。"""
    url_row3 = "https://b2b.nbdeli.com/goods/935.html"
    url_row12 = "https://b2b.nbdeli.com/goods/944.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [
            _ok_row("nbdeli", "ROW3", url_row3, "2.2", "2026-09-07T02:00:00+00:00", "task-p2"),
            _ok_row("nbdeli", "row12", url_row12, "3.3", "2026-09-07T02:00:00+00:00", "task-p3"),
        ],
    )
    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert summary["ok_synced"] == 2
    for url, code in ((url_row3, "ROW3"), (url_row12, "row12")):
        hit = shared_cache.lookup_current("nbdeli", url)
        assert hit is not None
        assert hit["entry"]["real_sku_id"] is None
        assert hit["entry"]["sku_status"] == SKU_STATUS_UNRESOLVED
        assert hit["entry"]["payload"]["request_sku_id"] == code
        # 有效价格结果不因占位/临时码拒绝而丢失
        assert hit["entry"]["price"] is not None


def test_crawl_id_echo_never_real_sku(tmp_path, prewarm_store, shared_cache):
    """sku_id 等于 crawl/remote 任务名的回显永不成为真实 SKU。"""
    url = "https://b2b.nbdeli.com/goods/945.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [_ok_row("nbdeli", "task-echo-7", url, "8.8", "2026-09-07T02:00:00+00:00", "task-echo-7")],
    )
    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert summary["ok_synced"] == 1
    hit = shared_cache.lookup_current("nbdeli", url)
    assert hit is not None
    assert hit["entry"]["real_sku_id"] is None
    assert hit["entry"]["sku_status"] == SKU_STATUS_UNRESOLVED
    assert hit["entry"]["payload"]["request_sku_id"] == "task-echo-7"
    assert hit["entry"]["crawl_id"] == "task-echo-7"


def test_page_evidence_still_confirmed_for_other_producers(tmp_path, shared_cache):
    """#9 的编码约束只作用于 daily sync;#7 的页面证据模型不受限。"""
    # 真页面证据(producer 自带)仍可 CONFIRMED
    seeded = shared_cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/937.html",
        real_sku_id="PAGE-SKU",
        sku_status=SKU_STATUS_CONFIRMED,
        price="3.3",
        source="zhaoshang-history",
        succeeded_at="2026-09-01T00:00:00+00:00",
    )
    assert seeded["real_sku_id"] == "PAGE-SKU"
    assert seeded["sku_status"] == SKU_STATUS_CONFIRMED
    hit = shared_cache.lookup_current("nbdeli", "https://b2b.nbdeli.com/goods/937.html")
    assert hit["entry"]["sku_status"] == SKU_STATUS_CONFIRMED


def test_url_parser_evidence_is_url_parsed_unverified(tmp_path, shared_cache):
    """URL 解析证据(producer 自带)走 URL_PARSED_UNVERIFIED,永不 CONFIRMED。"""
    parsed = shared_cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/938.html",
        real_sku_id="PARSED-SKU",
        sku_status=SKU_STATUS_URL_PARSED_UNVERIFIED,
        price="4.4",
        source="url-parser",
        succeeded_at="2026-09-01T00:00:00+00:00",
    )
    assert parsed["sku_status"] == SKU_STATUS_URL_PARSED_UNVERIFIED
    hit = shared_cache.lookup_current("nbdeli", "https://b2b.nbdeli.com/goods/938.html")
    assert hit["entry"]["sku_status"] == SKU_STATUS_URL_PARSED_UNVERIFIED


def test_same_sku_different_urls_never_merge(tmp_path, prewarm_store, shared_cache):
    """同一 sku_id、不同 URL → 两个独立身份/候选;绝不按 SKU 合并。"""
    url_a = "https://b2b.nbdeli.com/goods/939.html"
    url_b = "https://b2b.nbdeli.com/goods/940.html"
    path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        [
            _ok_row("nbdeli", "SAME-SKU", url_a, "5.5", "2026-09-07T02:00:00+00:00", "task-u1"),
            _ok_row("nbdeli", "SAME-SKU", url_b, "6.6", "2026-09-07T02:00:00+00:00", "task-u2"),
        ],
    )
    summary = sync_daily_internet_results(
        prewarm_store, shared_cache, state_db_path=path, business_date=BUSINESS_DATE
    )
    assert summary["ok_synced"] == 2
    hit_a = shared_cache.lookup_current("nbdeli", url_a)
    hit_b = shared_cache.lookup_current("nbdeli", url_b)
    assert hit_a is not None and hit_b is not None
    # URL 身份模型:两个不同 identity,绝不按相同 sku_id 合并;
    # 各自独立命中各自的上游证据条目。
    assert hit_a["identity_id"] != hit_b["identity_id"]
    assert hit_a["entry"]["real_sku_id"] == "SAME-SKU"
    assert hit_a["entry"]["sku_status"] == SKU_STATUS_UPSTREAM_PROVIDED
    assert hit_b["entry"]["real_sku_id"] == "SAME-SKU"
    assert hit_b["entry"]["sku_status"] == SKU_STATUS_UPSTREAM_PROVIDED
    assert len(shared_cache.history("nbdeli", url_a)) == 1
    assert len(shared_cache.history("nbdeli", url_b)) == 1


# ---- is_temporary_request_code(临时码判定单元) --------------------------------------------


def test_is_temporary_request_code_shapes():
    assert is_temporary_request_code("zhpq-000001")
    assert is_temporary_request_code(" ROW3 ")
    assert is_temporary_request_code("row12")
    assert is_temporary_request_code("ROW", crawl_id="ROW") is True  # crawl_id echo
    assert is_temporary_request_code("task-1", crawl_id="task-1")
    assert not is_temporary_request_code("SKU-77")
    assert not is_temporary_request_code("")
    assert not is_temporary_request_code("   ")
    assert not is_temporary_request_code("task-1")  # 无 crawl_id 时不误伤
    assert not is_temporary_request_code("ROW3X")  # 非精确 ROW<n> 形状
    assert not is_temporary_request_code("xrow1")


# ---- R3 历史数据确定性升级(免重爬) --------------------------------------------------------


def test_historical_r3_payload_upgrades_deterministically(
    tmp_path, prewarm_store, shared_cache
):
    """R3 时代 UNRESOLVED + payload["request_sku_id"] 的历史行被一次性确定性
    升级为追加的 UPSTREAM_PROVIDED 行:历史保留、current 移到新行、免重爬、
    幂等可重跑;临时码行原样跳过。"""
    url_real = "https://b2b.nbdeli.com/goods/946.html"
    url_temp = "https://b2b.nbdeli.com/goods/947.html"
    # 模拟 R3-era 数据:UNRESOLVED + request_sku_id 保留,账本已完成指向缓存行
    real_row = shared_cache.record_success(
        mall="nbdeli",
        submitted_url=url_real,
        real_sku_id=None,
        sku_status=SKU_STATUS_UNRESOLVED,
        price="5.5",
        source=SOURCE,
        succeeded_at="2026-09-01T02:00:00+00:00",
        payload={"request_sku_id": "SKU-55"},
    )
    temp_row = shared_cache.record_success(
        mall="nbdeli",
        submitted_url=url_temp,
        real_sku_id=None,
        sku_status=SKU_STATUS_UNRESOLVED,
        price="6.6",
        source=SOURCE,
        succeeded_at="2026-09-01T02:30:00+00:00",
        payload={"request_sku_id": "ROW5"},
    )
    prewarm_store.ledger_claim(SOURCE, f"r3-event-real-{real_row['id']}")
    prewarm_store.ledger_complete(
        SOURCE, f"r3-event-real-{real_row['id']}", cache_result_id=int(real_row["id"])
    )
    prewarm_store.ledger_claim(SOURCE, f"r3-event-temp-{temp_row['id']}")
    prewarm_store.ledger_complete(
        SOURCE, f"r3-event-temp-{temp_row['id']}", cache_result_id=int(temp_row["id"])
    )

    summary = upgrade_upstream_sku_evidence(prewarm_store, shared_cache, source=SOURCE)
    assert summary == {
        "scanned": 2,
        "upgraded": 1,
        "already_upgraded": 0,
        "skipped_no_sku": 0,
        "skipped_temporary": 1,
        "failed": 0,
        "transient": 0,
    }
    # 升级行:current 移到新追加的 UPSTREAM_PROVIDED 行,历史 2 行
    hit = shared_cache.lookup_current("nbdeli", url_real)
    assert hit["entry"]["sku_status"] == SKU_STATUS_UPSTREAM_PROVIDED
    assert hit["entry"]["real_sku_id"] == "SKU-55"
    assert hit["entry"]["price"] == "5.5"
    assert hit["entry"]["payload"]["request_sku_id"] == "SKU-55"
    assert hit["entry"]["payload"]["sku_evidence_upgraded_from_result_id"] == int(
        real_row["id"]
    )
    assert len(shared_cache.history("nbdeli", url_real)) == 2
    # 临时码 R3 行原样保留,不升级
    temp_hit = shared_cache.lookup_current("nbdeli", url_temp)
    assert temp_hit["entry"]["sku_status"] == SKU_STATUS_UNRESOLVED
    assert temp_hit["entry"]["real_sku_id"] is None
    assert len(shared_cache.history("nbdeli", url_temp)) == 1

    # 重跑幂等:已升级行计入 already_upgraded,历史不再增长
    again = upgrade_upstream_sku_evidence(prewarm_store, shared_cache, source=SOURCE)
    assert again == {
        "scanned": 2,
        "upgraded": 0,
        "already_upgraded": 1,
        "skipped_no_sku": 0,
        "skipped_temporary": 1,
        "failed": 0,
        "transient": 0,
    }
    assert len(shared_cache.history("nbdeli", url_real)) == 2
