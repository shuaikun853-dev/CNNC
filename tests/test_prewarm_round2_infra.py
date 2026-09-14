"""Issue #13 Review Round 2 — Slice R1 基础设施行为测试。

覆盖 Round-2 设计 B2(逐日 observation + remote job link)、B3(producer
溯源列 + 常量 + 写入方分类)、B4(_boost_reused_job 三态返回 + exact 路径
priority_proven)。迁移约束:纯增量(CREATE TABLE IF NOT EXISTS + 守卫式
ALTER TABLE),旧库原地升级不丢数据,GET/只读路径绝不迁移。

fake adapter / seeder 模式照抄 tests/test_customer_prewarm_dispatch*.py。
全程 tmp_path,零真实远端调用,绝不触碰生产数据库。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.customer_prewarm_dispatch import (
    BACKEND_VERSION,
    _boost_reused_job,
    dispatch_single_candidate_exact,
    find_remote_work_for_url,
    prewarm_placeholder_sku,
    run_prewarm_dispatch_pass,
)
from app.services.customer_prewarm_discovery import sync_daily_internet_results
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import DEFAULT_POOL_ID, DEFAULT_TENANT_ID, DailyAuditStore, ProcessLock
from app.services.incremental_audit_runtime import runtime_paths
from app.services.legacy_zhaoshang_adapter import (
    CrawlRequest,
    LegacyPriorityOutcome,
    LegacySubmitOutcome,
    PriorityStatus,
    SubmitStatus,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from app.services.staples_prewarm import _write_staples_success
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

BUSINESS_DATE = "2026-09-14"
SOURCE = "daily-internet"

COMIX = ZhaoshangMallMapping(
    mall_name="齐心商城",
    source_id="SRC-COMIX",
    mall_key="comix",
    domains=("qx.com", "www.qx.com"),
    vendor_keywords=("comix",),
)

SUBMITTED_SUCCESS = LegacySubmitOutcome(
    SubmitStatus.SUBMITTED,
    task_id="remote-1",
    task_number="number-1",
    priority_evidence="updated",
)


# ---- fake adapter / seeders (copied from test_customer_prewarm_dispatch*) ------


def _fake_canonical(business_date, requests):
    return json.dumps(
        {
            "business_date": business_date,
            "requests": [
                request.identity
                for request in sorted(requests, key=lambda value: value.identity)
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class _FakePreparedJob:
    def __init__(self, business_date, requests, task_name, template_path, result_path, batch_state_path):
        canonical = _fake_canonical(business_date, requests)
        self.business_date = business_date
        self.mall_key = requests[0].mall_key
        self.mall_source_id = requests[0].mall_source_id
        self.mall_name = requests[0].mall_name
        self.task_name = task_name
        self.template_path = Path(template_path)
        self.result_path = Path(result_path)
        self.batch_state_path = Path(batch_state_path)
        self.requests = tuple(requests)
        self.logical_job_key = "logical-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        self.payload_sha256 = "payload-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def payload_json(self):
        return {
            "requests": [
                {
                    "mall_key": request.mall_key,
                    "mall_source_id": request.mall_source_id,
                    "mall_name": request.mall_name,
                    "sku_id": request.sku_id,
                    "url": request.url,
                    "backend_version": request.backend_version,
                }
                for request in self.requests
            ],
            "batch_state": str(self.batch_state_path),
        }


class _CountingAdapter:
    def __init__(self, *, submit_outcome=None, priority_outcomes=None, submit_raises=False):
        self.prepare_calls = 0
        self.submit_calls = 0
        self.prioritize_calls = 0
        self.submit_outcome = submit_outcome or SUBMITTED_SUCCESS
        self.priority_outcomes = list(priority_outcomes or [])
        self.submit_raises = submit_raises
        self.prepared_log: list[_FakePreparedJob] = []

    def prepare_job(self, business_date, requests, template_path, result_path, task_name, batch_state_path):
        self.prepare_calls += 1
        template_path = Path(template_path)
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text("immutable-prewarm", encoding="utf-8")
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        job = _FakePreparedJob(
            business_date, list(requests), task_name, template_path, result_path, batch_state_path
        )
        self.prepared_log.append(job)
        return job

    def submit(self, _job, *, timeout_seconds):
        self.submit_calls += 1
        if self.submit_raises:
            raise AssertionError("submitted on a path that must never POST")
        return self.submit_outcome

    def prioritize(self, _job, *, task_id, task_number, timeout_seconds):
        self.prioritize_calls += 1
        if self.priority_outcomes:
            return self.priority_outcomes.pop(0)
        return LegacyPriorityOutcome(PriorityStatus.UPDATED, weight_type=-1)


class _ExplodingPrioritizeAdapter(_CountingAdapter):
    """prioritize raises: exercises the boost except path (fail-closed)."""

    def prioritize(self, _job, *, task_id, task_number, timeout_seconds):
        self.prioritize_calls += 1
        raise RuntimeError("remote tool died")


def make_state_db(runtime_root: Path, business_date: str):
    paths = runtime_paths(business_date, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(business_date)
    return paths


def seed_remote_work(
    runtime_root: Path,
    business_date: str,
    adapter: _CountingAdapter,
    *,
    mapping,
    url,
    job_state="SUBMITTED",
    remote_identity=None,
):
    paths = runtime_paths(business_date, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(business_date)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    try:
        token = store.acquire_lease(business_date, "seeder", 120.0)
        sku = prewarm_placeholder_sku(mapping.mall_key, url)
        crawl = store.ensure_crawl_work(
            token, mapping.mall_key, sku, url, BACKEND_VERSION, "seed-fp", source_id=mapping.source_id
        )
        batch_state = paths.run_dir / "BATCH_STATE.md"
        if not batch_state.is_file():
            batch_state.touch()
        requests = [
            CrawlRequest(mapping.mall_key, mapping.source_id, mapping.mall_name, sku, url, BACKEND_VERSION)
        ]
        seed_dir = paths.run_dir / "zhaoshang" / mapping.mall_key / "seed"
        prepared = adapter.prepare_job(
            business_date,
            requests,
            seed_dir / "input.xlsx",
            seed_dir / "result.xlsx",
            f"cnnc_current_opt_{mapping.mall_name}_00000000000001",
            batch_state_path=batch_state,
        )
        job_row = store.prepare_remote_intent(
            token,
            prepared.logical_job_key,
            prepared.task_name,
            prepared.payload_sha256,
            mall_key=mapping.mall_key,
            input_path=str(prepared.template_path),
            result_path=str(prepared.result_path),
            payload_json=prepared.payload_json(),
        )
        store.link_crawl_work_to_remote_job(token, int(crawl["id"]), int(job_row["id"]))
        if remote_identity:
            claim = store.claim_remote_submission(token, int(job_row["id"]))
            assert claim is not None
            store.reconcile_remote_job(
                token, int(job_row["id"]), remote_identity=remote_identity, state=job_state
            )
            store.advance_crawl_work_for_job(token, int(job_row["id"]), job_state)
        return paths, int(job_row["id"])
    finally:
        store.release_lease(token)
        lock.release()


def cache_hit_candidate(prewarm: CustomerPrewarmStore, url: str, *, mall="comix", document="D1"):
    upsert = prewarm.upsert_link(document, url, mall=mall)
    candidate_id = int(upsert["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_CACHE_HIT)
    return candidate_id


def run_pass(runtime_root, prewarm, cache, *, mappings=(COMIX,), adapter=None, **kwargs):
    return run_prewarm_dispatch_pass(
        prewarm,
        cache,
        business_date=BUSINESS_DATE,
        runtime_root=runtime_root,
        mappings=list(mappings),
        adapter=adapter,
        **kwargs,
    )


def _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=None, **kwargs):
    return dispatch_single_candidate_exact(
        prewarm,
        cache,
        business_date=BUSINESS_DATE,
        runtime_root=runtime_root,
        candidate_id=candidate_id,
        mappings=[COMIX],
        adapter=adapter,
        **kwargs,
    )


def _state_rows(path, query, params=()):
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def _store_rows(store_path, query, params=()):
    return _state_rows(store_path, query, params)


def _observations(store_path):
    return _store_rows(
        store_path,
        """SELECT business_date, candidate_id, document_id, original_url,
                  first_seen_at, last_seen_at
           FROM prewarm_daily_observations
           ORDER BY business_date, candidate_id, document_id""",
    )


def make_sync_state_db(tmp_path, business_date, rows, *, name="state"):
    """per-day state DB for the daily-sync producer test (真实 schema + 原始 SQL 行)。

    ``rows``: mall_key / sku_id / exact_url / result / updated_at / task_name;
    带 task_name 的行同时建 remote_jobs 行并绑定 crawl_work.remote_job_id。
    """
    path = Path(tmp_path) / f"{name}-{business_date}.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    DailyAuditStore(path)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    job_ids: list[int | None] = []
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
            job_ids.append(job_id)
            connection.execute(
                """INSERT INTO crawl_work (run_id, mall_key, source_id, sku_id,
                                           exact_url, backend_version, fingerprint,
                                           state, remote_job_id, result_json, updated_at)
                   VALUES (?, ?, '', ?, ?, 'v1', ?, 'SUCCESS', ?, ?, ?)""",
                (
                    run_id,
                    row.get("mall_key") or "",
                    row.get("sku_id") or "",
                    row.get("exact_url") or "",
                    f"fp-{index}",
                    job_id,
                    json.dumps(row["result"], ensure_ascii=False),
                    row.get("updated_at", now),
                ),
            )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return path, job_ids


# ---- B3: producer 常量单一出处 ----------------------------------------------------


def test_producer_constants_single_home():
    from app.services.shared_crawl_cache import (
        PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
        PRODUCER_DAILY_INTERNET,
        PRODUCER_MANUAL_RECRAWL_STAPLES,
        PRODUCER_MANUAL_RECRAWL_ZHAOSHANG,
        PRODUCER_STAPLES_LOCAL,
    )

    assert PRODUCER_DAILY_INTERNET == "daily-internet"
    assert PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG == "customer-prewarm-zhaoshang"
    assert PRODUCER_STAPLES_LOCAL == "staples-local"
    assert PRODUCER_MANUAL_RECRAWL_STAPLES == "manual-recrawl-staples"
    assert PRODUCER_MANUAL_RECRAWL_ZHAOSHANG == "manual-recrawl-zhaoshang"


# ---- B3: crawl_results.producer 增量列 + record_success 持久化 ----------------------


def test_record_success_persists_producer_and_projects_it(tmp_path):
    cache_path = tmp_path / "shared_cache.sqlite3"
    cache = SharedCrawlCache(cache_path)
    with_producer = cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/r2-1.html",
        source=SOURCE,
        producer="customer-prewarm-zhaoshang",
    )
    without_producer = cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/r2-2.html",
        source=SOURCE,
    )
    # 行投影 dict 携带 producer(默认 None,不破坏既有调用方)
    assert with_producer["producer"] == "customer-prewarm-zhaoshang"
    assert without_producer["producer"] is None
    hit = cache.lookup_current("nbdeli", "https://b2b.nbdeli.com/goods/r2-1.html")
    assert hit is not None and hit["entry"]["producer"] == "customer-prewarm-zhaoshang"
    history = cache.history("nbdeli", "https://b2b.nbdeli.com/goods/r2-2.html")
    assert len(history) == 1 and history[0]["producer"] is None
    # 真实落库列
    rows = _state_rows(
        cache_path,
        "SELECT original_url, producer FROM crawl_results ORDER BY id",
    )
    assert rows[0]["producer"] == "customer-prewarm-zhaoshang"
    assert rows[1]["producer"] is None


def test_migration_additive_old_cache_schema_upgrades_without_data_loss(tmp_path):
    """旧列集缓存库原地升级:producer 列补齐,既有行逐字段保真。"""
    cache_path = tmp_path / "old_cache.sqlite3"
    cache = SharedCrawlCache(cache_path)
    seeded = cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/old-1.html",
        real_sku_id="OLD-SKU",
        sku_status="UPSTREAM_PROVIDED",
        price="7.7",
        source=SOURCE,
        succeeded_at="2026-09-01T02:00:00+00:00",
        payload={"source_event_key": "evt-old-1"},
    )
    # 模拟旧库:去掉 producer 列
    connection = sqlite3.connect(str(cache_path), isolation_level=None)
    try:
        connection.execute("ALTER TABLE crawl_results DROP COLUMN producer")
        names = {
            row[1]
            for row in connection.execute("PRAGMA table_info(crawl_results)").fetchall()
        }
        assert "producer" not in names
    finally:
        connection.close()

    upgraded = SharedCrawlCache(cache_path)

    columns = {row["name"] for row in _state_rows(cache_path, "PRAGMA table_info(crawl_results)")}
    assert "producer" in columns
    rows = _state_rows(cache_path, "SELECT * FROM crawl_results WHERE id=?", (seeded["id"],))
    assert len(rows) == 1
    old_view = dict(seeded)
    new_view = dict(rows[0])
    new_view.pop("payload", None)
    old_view.pop("payload", None)
    new_view.pop("producer", None)
    old_view.pop("producer", None)  # seeded 值已含新列默认 None
    # 既有行逐字段保真(唯一差异是新增的 producer 列,值为 NULL)
    assert new_view == old_view
    assert rows[0]["producer"] is None
    hit = upgraded.lookup_current("nbdeli", "https://b2b.nbdeli.com/goods/old-1.html")
    assert hit is not None and hit["entry"]["price"] == "7.7"


# ---- B2: store 两张新表 + 增量迁移 + bootstrap -------------------------------------


def test_migration_additive_old_store_schema_recreates_new_tables(tmp_path):
    store_path = tmp_path / "prewarm.sqlite3"
    CustomerPrewarmStore(store_path)
    connection = sqlite3.connect(str(store_path), isolation_level=None)
    try:
        connection.execute("DROP TABLE prewarm_daily_observations")
        connection.execute("DROP TABLE prewarm_remote_job_links")
    finally:
        connection.close()

    CustomerPrewarmStore(store_path)

    names = {
        row["name"]
        for row in _store_rows(
            store_path,
            "SELECT name FROM sqlite_master WHERE type='table'",
        )
    }
    assert "prewarm_daily_observations" in names
    assert "prewarm_remote_job_links" in names


def test_bootstrap_derives_observations_from_document_days(tmp_path):
    """旧库(无 observation 表)+ 既有 documents×links → RW 打开时按文档日
    派生 observation;重开幂等(零重复行)。"""
    store_path = tmp_path / "prewarm.sqlite3"
    CustomerPrewarmStore(store_path)
    connection = sqlite3.connect(str(store_path), isolation_level=None)
    try:
        connection.execute("DROP TABLE prewarm_daily_observations")
        connection.execute("DROP TABLE prewarm_remote_job_links")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO prewarm_documents
               (document_id, business_date, four_category_id, source, detail_state,
                detail_attempts, first_seen_at, last_seen_at)
               VALUES ('DOC-1', '2026-09-10', 'CAT-1', 'event', 'DONE', 1,
                       '2026-09-10T00:00:00+00:00', '2026-09-10T01:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO prewarm_documents
               (document_id, business_date, four_category_id, source, detail_state,
                detail_attempts, first_seen_at, last_seen_at)
               VALUES ('DOC-2', '2026-09-11', 'CAT-2', 'event', 'DONE', 1,
                       '2026-09-11T00:00:00+00:00', '2026-09-11T01:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO prewarm_candidates
               (id, mall, original_url, lookup_url, business_date, source,
                cache_status, first_seen_at, last_seen_at)
               VALUES (1, 'nbdeli', 'https://b2b.nbdeli.com/goods/b-1.html',
                       'https://b2b.nbdeli.com/goods/b-1.html', '2026-09-10', 'detail',
                       'PENDING_PREWARM', '2026-09-10T00:00:00+00:00',
                       '2026-09-10T01:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO prewarm_candidates
               (id, mall, original_url, lookup_url, business_date, source,
                cache_status, first_seen_at, last_seen_at)
               VALUES (2, 'nbdeli', 'https://b2b.nbdeli.com/goods/b-2.html',
                       'https://b2b.nbdeli.com/goods/b-2.html', '2026-09-11', 'detail',
                       'PENDING_PREWARM', '2026-09-11T00:00:00+00:00',
                       '2026-09-11T01:00:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO prewarm_links
               (document_id, original_url, mall, candidate_id, source,
                first_seen_at, last_seen_at)
               VALUES ('DOC-1', 'https://b2b.nbdeli.com/goods/b-1.html', 'nbdeli', 1,
                       'detail', '2026-09-10T00:30:00+00:00', '2026-09-10T00:45:00+00:00')"""
        )
        connection.execute(
            """INSERT INTO prewarm_links
               (document_id, original_url, mall, candidate_id, source,
                first_seen_at, last_seen_at)
               VALUES ('DOC-2', 'https://b2b.nbdeli.com/goods/b-2.html', 'nbdeli', 2,
                       'detail', '2026-09-11T00:30:00+00:00', '2026-09-11T00:45:00+00:00')"""
        )
        connection.execute("COMMIT")
    finally:
        connection.close()

    # RW 重开:派生 bootstrap(document-day 证据)
    store = CustomerPrewarmStore(store_path)
    rows = _observations(store_path)
    assert rows == [
        {
            "business_date": "2026-09-10",
            "candidate_id": 1,
            "document_id": "DOC-1",
            "original_url": "https://b2b.nbdeli.com/goods/b-1.html",
            "first_seen_at": "2026-09-10T00:30:00+00:00",
            "last_seen_at": "2026-09-10T00:45:00+00:00",
        },
        {
            "business_date": "2026-09-11",
            "candidate_id": 2,
            "document_id": "DOC-2",
            "original_url": "https://b2b.nbdeli.com/goods/b-2.html",
            "first_seen_at": "2026-09-11T00:30:00+00:00",
            "last_seen_at": "2026-09-11T00:45:00+00:00",
        },
    ]
    assert store.candidate_ids_for_business_date("2026-09-10") == {1}
    assert store.candidate_ids_for_business_date("2026-09-11") == {2}

    # 再次 RW 打开:INSERT OR IGNORE 幂等,零重复
    CustomerPrewarmStore(store_path)
    assert len(_observations(store_path)) == 2


# ---- B2: record_daily_observation + upsert_link 内联 observation -------------------


def test_upsert_link_records_daily_observation(tmp_path):
    store_path = tmp_path / "prewarm.sqlite3"
    store = CustomerPrewarmStore(store_path)
    store.upsert_document("2026-09-14", "DOC-1", "CAT-1")
    url = "https://qx.com/product-obs.html"

    upsert = store.upsert_link("DOC-1", url, mall="comix", business_date="2026-09-14")
    candidate_id = int(upsert["candidate"]["id"])

    rows = _observations(store_path)
    assert len(rows) == 1
    assert rows[0]["business_date"] == "2026-09-14"
    assert rows[0]["candidate_id"] == candidate_id
    assert rows[0]["document_id"] == "DOC-1"
    assert rows[0]["original_url"] == url
    assert rows[0]["first_seen_at"] == rows[0]["last_seen_at"]

    # 同一 document+url 在第二个业务日再被看到 → 新增一行(逐日范围)
    store.upsert_link("DOC-1", url, mall="comix", business_date="2026-09-15")
    rows = _observations(store_path)
    assert [row["business_date"] for row in rows] == ["2026-09-14", "2026-09-15"]
    assert store.candidate_ids_for_business_date("2026-09-14") == {candidate_id}
    assert store.candidate_ids_for_business_date("2026-09-15") == {candidate_id}
    assert store.candidate_ids_for_business_date("2026-09-16") == set()


def test_record_daily_observation_upsert_refreshes_last_seen(tmp_path):
    store_path = tmp_path / "prewarm.sqlite3"
    store = CustomerPrewarmStore(store_path)
    first = datetime(2026, 9, 14, 1, 0, 0, tzinfo=UTC)
    second = datetime(2026, 9, 14, 5, 0, 0, tzinfo=UTC)

    store.record_daily_observation(
        "2026-09-14", 7, "DOC-9", "https://qx.com/refresh.html", now=first
    )
    store.record_daily_observation(
        "2026-09-14", 7, "DOC-9", "https://qx.com/refresh.html", now=second
    )
    rows = _observations(store_path)
    assert len(rows) == 1  # PK 命中 → 不新增行
    # store 归一化为 UTC 微秒 ISO 格式
    assert datetime.fromisoformat(rows[0]["first_seen_at"]) == first
    assert datetime.fromisoformat(rows[0]["last_seen_at"]) == second


# ---- B2: remote job link 读写 + 查询辅助 -------------------------------------------


def test_record_remote_job_link_and_producer_roundtrip(tmp_path):
    store_path = tmp_path / "prewarm.sqlite3"
    store = CustomerPrewarmStore(store_path)
    store.record_remote_job_link(
        "2026-09-14", 11, 17, "customer-prewarm-zhaoshang"
    )
    assert store.remote_job_producer("2026-09-14", 17) == "customer-prewarm-zhaoshang"
    # 跨日/未记录的 job 不误判
    assert store.remote_job_producer("2026-09-15", 17) is None
    assert store.remote_job_producer("2026-09-14", 99) is None

    link = store.remote_job_link_for_candidate(11)
    assert link is not None
    assert link["business_date"] == "2026-09-14"
    assert link["remote_job_id"] == 17
    assert link["producer"] == "customer-prewarm-zhaoshang"
    assert store.remote_job_link_for_candidate(424242) is None


def test_remote_job_link_for_candidate_latest_wins(tmp_path):
    store_path = tmp_path / "prewarm.sqlite3"
    store = CustomerPrewarmStore(store_path)
    same_moment = datetime(2026, 9, 14, 2, 0, 0, tzinfo=UTC)
    later = datetime(2026, 9, 15, 2, 0, 0, tzinfo=UTC)

    store.record_remote_job_link(
        "2026-09-14", 11, 17, "customer-prewarm-zhaoshang", now=same_moment
    )
    # recorded_at 并列时取 max rowid(后写入者)
    store.record_remote_job_link(
        "2026-09-15", 11, 20, "manual-recrawl-zhaoshang", now=same_moment
    )
    link = store.remote_job_link_for_candidate(11)
    assert link["remote_job_id"] == 20

    # 更晚的 recorded_at 胜出
    store.record_remote_job_link(
        "2026-09-14", 11, 18, "customer-prewarm-zhaoshang", now=later
    )
    link = store.remote_job_link_for_candidate(11)
    assert link["remote_job_id"] == 18
    assert link["business_date"] == "2026-09-14"


# ---- B3: daily sync producer 分类 ---------------------------------------------------


def test_sync_producer_classification(tmp_path):
    prewarm_store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    cache_path = tmp_path / "shared_cache.sqlite3"
    shared_cache = SharedCrawlCache(cache_path)
    url_linked = "https://b2b.nbdeli.com/goods/p-linked.html"
    url_daily = "https://b2b.nbdeli.com/goods/p-daily.html"
    path, job_ids = make_sync_state_db(
        tmp_path,
        BUSINESS_DATE,
        [
            {
                "mall_key": "nbdeli",
                "sku_id": "SKU-A",
                "exact_url": url_linked,
                "result": {"page_price_status": "OK", "page_price_value": "1.1"},
                "updated_at": "2026-09-14T02:00:00+00:00",
                "task_name": "task-prewarm-linked",
            },
            {
                "mall_key": "nbdeli",
                "sku_id": "SKU-B",
                "exact_url": url_daily,
                "result": {"page_price_status": "OK", "page_price_value": "2.2"},
                "updated_at": "2026-09-14T02:30:00+00:00",
            },
        ],
    )
    linked_job_id = job_ids[0]
    assert linked_job_id is not None and job_ids[1] is None

    # 该 crawl 行的 remote job 是客户预爬派发的 → 记 linkage
    upsert = prewarm_store.upsert_link("DOC-P", url_linked, mall="nbdeli")
    prewarm_store.record_remote_job_link(
        BUSINESS_DATE,
        int(upsert["candidate"]["id"]),
        linked_job_id,
        "customer-prewarm-zhaoshang",
    )

    summary = sync_daily_internet_results(
        prewarm_store,
        shared_cache,
        state_db_path=path,
        business_date=BUSINESS_DATE,
    )
    assert summary["state_db"] == "ok"
    assert summary["ok_synced"] == 2

    rows = {
        row["original_url"]: row["producer"]
        for row in _state_rows(cache_path, "SELECT original_url, producer FROM crawl_results")
    }
    # 有 linkage 的 job → customer-prewarm-zhaoshang;无 linkage → daily-internet
    assert rows[url_linked] == "customer-prewarm-zhaoshang"
    assert rows[url_daily] == "daily-internet"


# ---- B3: staples 本地预爬写入 producer ----------------------------------------------


def test_staples_write_carries_producer(tmp_path):
    shared_cache = SharedCrawlCache(tmp_path / "shared_cache.sqlite3")
    evidence = {
        "submitted_url": "https://www.stbchina.cn/item/r2-9.html",
        "original_url": "https://www.stbchina.cn/item/r2-9.html",
        "sku_status": "UNRESOLVED",
        "title": "staples item",
        "price": "9.9",
        "comment_count": 3,
    }
    row = _write_staples_success(shared_cache, evidence)
    assert row["producer"] == "staples-local"
    rows = _state_rows(
        tmp_path / "shared_cache.sqlite3",
        "SELECT producer FROM crawl_results",
    )
    assert rows == [{"producer": "staples-local"}]


# ---- B4: _boost_reused_job 三态返回(既有计数不变) -----------------------------------


def _boost_summary():
    return {"phase_errors": [], "priority_boosted": 0, "priority_unconfirmed": 0}


def test_boost_return_proven_true(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    url = "https://qx.com/boost-proven.html"
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-bp|number-bp",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    adapter = _CountingAdapter()  # 默认 UPDATED 证明
    store = DailyAuditStore(paths.state_path)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    try:
        token = store.acquire_lease(BUSINESS_DATE, "boost-test", 120.0)
        summary = _boost_summary()
        result = _boost_reused_job(
            prewarm_store=prewarm,
            store=store,
            token=token,
            get_adapter=lambda: adapter,
            business_date=BUSINESS_DATE,
            candidate_id=candidate_id,
            job_id=job_id,
            deadline=time.monotonic() + 50.0,
            monotonic_value=time.monotonic,
            summary=summary,
        )
        store.release_lease(token)
    finally:
        lock.release()
    assert result is True
    assert summary["priority_boosted"] == 1  # 既有计数行为不变
    assert summary["priority_unconfirmed"] == 0


def test_boost_return_unconfirmed_false(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    url = "https://qx.com/boost-unconfirmed.html"
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-bu|number-bu",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    adapter = _CountingAdapter(
        priority_outcomes=[LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic="nope")]
    )
    store = DailyAuditStore(paths.state_path)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    try:
        token = store.acquire_lease(BUSINESS_DATE, "boost-test", 120.0)
        summary = _boost_summary()
        result = _boost_reused_job(
            prewarm_store=prewarm,
            store=store,
            token=token,
            get_adapter=lambda: adapter,
            business_date=BUSINESS_DATE,
            candidate_id=candidate_id,
            job_id=job_id,
            deadline=time.monotonic() + 50.0,
            monotonic_value=time.monotonic,
            summary=summary,
        )
        store.release_lease(token)
    finally:
        lock.release()
    assert result is False
    assert summary["priority_unconfirmed"] == 1  # 既有计数行为不变
    assert summary["priority_boosted"] == 0


def test_boost_return_identity_less_none(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    url = "https://qx.com/boost-identity-less.html"
    adapter = _CountingAdapter()
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        adapter,
        mapping=COMIX,
        url=url,
        remote_identity=None,  # 无 remote identity → 不可行动
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    summary = _boost_summary()
    result = _boost_reused_job(
        prewarm_store=prewarm,
        store=DailyAuditStore(runtime_paths(BUSINESS_DATE, runtime_root).state_path),
        token=None,
        get_adapter=lambda: adapter,
        business_date=BUSINESS_DATE,
        candidate_id=candidate_id,
        job_id=job_id,
        deadline=time.monotonic() + 50.0,
        monotonic_value=time.monotonic,
        summary=summary,
    )
    assert result is None
    assert adapter.prioritize_calls == 0
    assert summary["priority_boosted"] == 0 and summary["priority_unconfirmed"] == 0


def test_boost_return_exception_false(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    url = "https://qx.com/boost-exception.html"
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-be|number-be",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    adapter = _ExplodingPrioritizeAdapter()
    store = DailyAuditStore(paths.state_path)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    try:
        token = store.acquire_lease(BUSINESS_DATE, "boost-test", 120.0)
        summary = _boost_summary()
        result = _boost_reused_job(
            prewarm_store=prewarm,
            store=store,
            token=token,
            get_adapter=lambda: adapter,
            business_date=BUSINESS_DATE,
            candidate_id=candidate_id,
            job_id=job_id,
            deadline=time.monotonic() + 50.0,
            monotonic_value=time.monotonic,
            summary=summary,
        )
        store.release_lease(token)
    finally:
        lock.release()
    assert result is False  # except 路径 → False
    # 既有 except 行为不变:该路径从不增计数,只记 phase_error
    assert summary["priority_unconfirmed"] == 0
    assert summary["priority_boosted"] == 0
    assert any("RuntimeError" in message for message in summary["phase_errors"])


# ---- E: dispatch pass 在候选↔job 绑定点写 job link ----------------------------------


def test_dispatch_pass_writes_job_links(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-link.html"
    upsert = prewarm.upsert_link("D1", url, mall="comix")
    candidate_id = int(upsert["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["outcome"] == "OK"
    assert summary["jobs_created"] == 1
    candidate = prewarm.candidate_by_id(candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    job_id = int(candidate["remote_job_id"])
    link = prewarm.remote_job_link_for_candidate(candidate_id)
    assert link is not None
    assert link["business_date"] == BUSINESS_DATE
    assert link["remote_job_id"] == job_id
    assert link["producer"] == "customer-prewarm-zhaoshang"


def test_dispatch_pass_writes_job_links_for_reused_job(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-link-reuse.html"
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-lr|number-lr",
    )
    upsert = prewarm.upsert_link("D1", url, mall="comix")
    candidate_id = int(upsert["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["existing_remote_reused"] == 1
    link = prewarm.remote_job_link_for_candidate(candidate_id)
    assert link is not None
    assert link["remote_job_id"] == job_id
    assert link["producer"] == "customer-prewarm-zhaoshang"


# ---- E: exact 路径写 manual link + priority_proven 三态 -----------------------------


def test_dispatch_exact_create_writes_manual_link(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = cache_hit_candidate(prewarm, "https://qx.com/exact-link-create.html")
    adapter = _CountingAdapter()

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "CREATED"
    link = prewarm.remote_job_link_for_candidate(candidate_id)
    assert link is not None
    assert link["business_date"] == BUSINESS_DATE
    assert link["remote_job_id"] == int(result["remote_job_id"])
    assert link["producer"] == "manual-recrawl-zhaoshang"


def test_dispatch_exact_submit_unknown_still_writes_manual_link(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = cache_hit_candidate(prewarm, "https://qx.com/exact-link-unknown.html")
    adapter = _CountingAdapter(
        submit_outcome=LegacySubmitOutcome(SubmitStatus.SUBMIT_UNKNOWN, diagnostic="timeout")
    )

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "SUBMIT_UNKNOWN"
    link = prewarm.remote_job_link_for_candidate(candidate_id)
    assert link is not None
    assert link["remote_job_id"] == int(result["remote_job_id"])
    assert link["producer"] == "manual-recrawl-zhaoshang"


def test_dispatch_exact_reuse_priority_proven_true(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-link-proven.html"
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-lp|number-lp",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    before = prewarm.candidate_by_id(candidate_id)
    adapter = _CountingAdapter(submit_raises=True)

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "REUSED"
    assert result["remote_job_id"] == job_id
    assert result["priority_proven"] is True
    link = prewarm.remote_job_link_for_candidate(candidate_id)
    assert link is not None
    assert link["remote_job_id"] == job_id
    assert link["producer"] == "manual-recrawl-zhaoshang"
    # 候选行仍然绝不写入(既有不变量保持)
    assert prewarm.candidate_by_id(candidate_id) == before


def test_dispatch_exact_reuse_priority_proven_false(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-link-unproven.html"
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-lu|number-lu",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    before = prewarm.candidate_by_id(candidate_id)
    adapter = _CountingAdapter(
        submit_raises=True,
        priority_outcomes=[LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic="died")],
    )

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "REUSED"
    assert result["priority_proven"] is False
    assert prewarm.candidate_by_id(candidate_id) == before


def test_dispatch_exact_reuse_not_actionable_proven_none(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-link-none.html"
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-ln|number-ln",
    )
    # identity-less 但处于 boostable 态:清掉 remote_identity(旧库脏数据形态)
    connection = sqlite3.connect(str(paths.state_path), isolation_level=None)
    try:
        connection.execute(
            "UPDATE remote_jobs SET remote_identity=NULL WHERE id=?", (job_id,)
        )
    finally:
        connection.close()
    candidate_id = cache_hit_candidate(prewarm, url)
    before = prewarm.candidate_by_id(candidate_id)
    adapter = _CountingAdapter(submit_raises=True)

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "REUSED"
    assert result["remote_job_id"] == job_id
    assert result["priority_proven"] is None
    assert adapter.prioritize_calls == 0
    assert prewarm.candidate_by_id(candidate_id) == before
