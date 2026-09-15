"""prewarm_manual_recrawl 行为测试(Issue #13 Slice B + Review Round 2 B4 + Round 3 F3)。

守卫式单 URL 手动重爬 runner:守卫顺序(库缺失 → 404 → 竞态链接 → 不支持
路由 → in-flight)、史泰博本地爬取(写缓存带 producer、绝不写候选)、招商局
诚实生命周期(派发 ≠ 成功:CREATED 确认/REUSED 证实 → WAITING_REMOTE,未
证实 → PRIORITY_PENDING 永不自动翻转,其余 → FAILED 诚实透传;REUSED +
priority_proven None → 终态成功(job_terminal_success)→ WAITING_REMOTE,
否则 → FAILED 诚实透传;SUCCEEDED 只能由 get() 懒完成证明 —— 共享缓存出现
started_at 之后、payload (business_date, remote_job_id) 与本动作派发任务
完全一致的新 current 行(Round 4 Slice G2 精确关联;producer 仅诊断展示,
绝不当门槛 —— REUSED 的 AUTO 客户预爬任务其同步行 producer 仍是
customer-prewarm-zhaoshang,但 (date, job_id) 一致就必须完成;不相关的
qualifying 行只记一次 superseded_current_producer 注记并继续等待);远端
任务终态 FAILED → 动作 FAILED(failed_by=remote_job_terminal_failed))。
Round 3 F3:D8 in-flight key 全程持有(status 非终态期间同一 URL 二次发起
409;懒终态翻转立即释放;PRIORITY_PENDING/派发失败允许操作员重试)、
D11 每次懒终态翻转落 completed JSONL 事件、双击防抖与 §44 业务隔离
(closeout/STEP2/STEP3/push 模块在手动重爬动作期间绝不 import)。

全程零真实爬虫/网络/子进程:爬虫/派发用 fakes,完成证明用 tmp_path 下真实
SharedCrawlCache,远端状态库用最小 remote_jobs 表(镜像投影测试造库方式)。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import types
from typing import Any

import pytest

from app.services.crawl_url_identity import (
    SKU_STATUS_UNRESOLVED,
    SKU_STATUS_URL_PARSED_UNVERIFIED,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    CustomerPrewarmStore,
)
from app.services.incremental_audit_runtime import DEFAULT_RUNTIME_ROOT
from app.services.shared_crawl_cache import (
    PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
    PRODUCER_DAILY_INTERNET,
    PRODUCER_MANUAL_RECRAWL_STAPLES,
    PRODUCER_MANUAL_RECRAWL_ZHAOSHANG,
    SharedCrawlCache,
)
from app.services.staples_prewarm import (
    STAPLES_CACHE_MALL_KEY,
    STAPLES_OUTCOME_SUCCESS,
    STAPLES_OUTCOME_TRANSIENT_FAILURE,
    StaplesCrawlOutcome,
)
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

BUSINESS_DATE = "2026-09-14"
UID = "1001358701037447"
STAPLES_URL = f"https://www.stbchina.cn/item.html?itemId={UID}"
COMIX_URL_1 = "https://qx.com/manual-recrawl-1.html"
COMIX_URL_2 = "https://qx.com/manual-recrawl-2.html"

COMIX = ZhaoshangMallMapping(
    mall_name="齐心商城",
    source_id="SRC-COMIX",
    mall_key="comix",
    domains=("qx.com", "www.qx.com"),
    vendor_keywords=("comix",),
)

PREWARM_DB_NAME = "customer_prewarm.sqlite3"
SHARED_CACHE_NAME = "shared_crawl_cache.sqlite3"
ACTIONS_LOG_NAME = "manual_recrawl_actions.jsonl"


# ---- fakes ---------------------------------------------------------------------


class RecordingCrawler:
    """Records construction + crawl calls; returns scripted outcomes."""

    def __init__(self, outcomes=None):
        self.constructed = 0
        self.calls = 0
        self.seen_requests: list[list[dict]] = []
        self.outcomes = dict(outcomes or {})

    def crawl(self, requests):
        self.calls += 1
        self.seen_requests.append([dict(r) for r in requests])
        return {
            str(r["original_url"]): self.outcomes[str(r["original_url"])]
            for r in requests
            if str(r["original_url"]) in self.outcomes
        }


class GateCrawler:
    """Blocks inside crawl until released (drives in-flight timing)."""

    def __init__(self, outcomes=None):
        self.constructed = 0
        self.calls = 0
        self.seen_requests: list[list[dict]] = []
        self.gate = threading.Event()
        self.outcomes = dict(outcomes or {})

    def crawl(self, requests):
        self.calls += 1
        self.seen_requests.append([dict(r) for r in requests])
        self.gate.wait(timeout=5)
        return {
            str(r["original_url"]): self.outcomes[str(r["original_url"])]
            for r in requests
            if str(r["original_url"]) in self.outcomes
        }


class RecordingDispatch:
    """Records dispatch_exact calls; returns scripted results per candidate."""

    def __init__(self, results=None, *, raises=False):
        self.calls = 0
        self.seen_kwargs: list[dict] = []
        self.results = dict(results or {})
        self.raises = raises

    def __call__(self, _prewarm_store, _shared_cache, **kwargs):
        self.calls += 1
        self.seen_kwargs.append(dict(kwargs))
        if self.raises:
            raise AssertionError("dispatch_exact must never be called on this path")
        candidate_id = int(kwargs["candidate_id"])
        return self.results.get(candidate_id, {"outcome": "CREATED", "remote_job_id": candidate_id})


class GateDispatch:
    """Blocks inside dispatch_exact until released (independent in-flight)."""

    def __init__(self, result=None):
        self.calls = 0
        self.gate = threading.Event()
        self.result = dict(result or {})

    def __call__(self, _prewarm_store, _shared_cache, **kwargs):
        self.calls += 1
        self.gate.wait(timeout=5)
        return dict(
            {"outcome": "CREATED", "remote_job_id": int(kwargs["candidate_id"]),
             "priority_confirmed": True},
            **self.result,
        )


def _poison_crawler_factory():
    raise AssertionError("zhaoshang path must never construct a staples crawler")


# ---- outcome helpers ------------------------------------------------------------


def _success_outcome(url, *, price=12.5, title="史泰博商品", real_sku_id=UID,
                     sku_status=SKU_STATUS_URL_PARSED_UNVERIFIED, comment_count=123,
                     crawler_uid_raw=UID, **overrides):
    fields: dict[str, Any] = dict(
        status=STAPLES_OUTCOME_SUCCESS,
        original_url=url,
        submitted_url=url,
        returned_url=url,
        canonical_url=None,
        title=title,
        price=price,
        comment_count=comment_count,
        real_sku_id=real_sku_id,
        sku_status=sku_status,
        crawler_uid_raw=crawler_uid_raw,
    )
    fields.update(overrides)
    return StaplesCrawlOutcome(**fields)


def _transient_outcome(url, *, diagnostic="网络抖动"):
    return StaplesCrawlOutcome(
        status=STAPLES_OUTCOME_TRANSIENT_FAILURE,
        original_url=url,
        submitted_url=url,
        returned_url=None,
        canonical_url=None,
        title=None,
        price=None,
        comment_count=None,
        real_sku_id=None,
        sku_status=SKU_STATUS_UNRESOLVED,
        diagnostic=diagnostic,
    )


# ---- fixtures -------------------------------------------------------------------


@pytest.fixture()
def runtime(tmp_path):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / PREWARM_DB_NAME)
    cache = SharedCrawlCache(runtime_root / SHARED_CACHE_NAME)
    return types.SimpleNamespace(
        runtime_root=runtime_root,
        prewarm=prewarm,
        cache=cache,
    )


def _make_runner(runtime, *, crawler_factory=None, dispatch_exact=None):
    from app.services.prewarm_manual_recrawl import ManualRecrawlRunner

    return ManualRecrawlRunner(
        runtime_root=runtime.runtime_root,
        crawler_factory=crawler_factory,
        dispatch_exact=dispatch_exact,
        mappings_loader=lambda: [COMIX],
    )


def _seed_candidate(prewarm, url, document_id, *, mall, cache_status=CACHE_STATUS_PENDING_PREWARM):
    upserted = prewarm.upsert_link(document_id, url, mall=mall)
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    if cache_status != CACHE_STATUS_PENDING_PREWARM:
        prewarm.record_candidate_result(candidate_id, cache_status=cache_status)
    return candidate_id


def _write_state_db(runtime_root, jobs, *, business_date=BUSINESS_DATE):
    """最小 STEP1 状态库(镜像 test_prewarm_dashboard_projection 的造库方式):
    runtime_root/<business_date>/state.sqlite3,remote_jobs 只建 runner 读取的
    最小列(id/state)。"""
    day_dir = runtime_root / business_date
    day_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(day_dir / "state.sqlite3"))
    try:
        connection.execute(
            "CREATE TABLE remote_jobs (id INTEGER PRIMARY KEY, state TEXT NOT NULL)"
        )
        for job_id, state in jobs:
            connection.execute(
                "INSERT INTO remote_jobs (id, state) VALUES (?, ?)", (job_id, state)
            )
        connection.commit()
    finally:
        connection.close()


# ---- lazy-completion row helpers (Round 4 Slice G2 correlation) -------------------


def _sync_payload(business_date=BUSINESS_DATE, remote_job_id=7):
    """远端同步行的 payload 形状(镜像 sync_daily_internet_results)。"""
    return {
        "business_date": business_date,
        "remote_job_id": remote_job_id,
        "result": {"page_price_status": "OK", "page_price_value": "13.5"},
    }


def _write_sync_row(cache, url, payload, *, producer=PRODUCER_MANUAL_RECRAWL_ZHAOSHANG,
                    price="13.5"):
    """真实 SharedCrawlCache RW 写入一条远端同步成功行(succeeded_at=now →
    qualifying 时间;payload 按 live daily-sync 契约携带 (date, job_id))。"""
    return cache.record_success(
        mall="comix",
        submitted_url=url,
        source="daily-internet",
        producer=producer,
        price=price,
        title="新结果标题",
        payload=payload,
    )


# ---- guard order ----------------------------------------------------------------


def test_missing_prewarm_db_raises_state_db_missing_before_any_construction(tmp_path):
    runtime_root = tmp_path / "runtime"
    crawler = RecordingCrawler()
    dispatch = RecordingDispatch()
    from app.services.prewarm_manual_recrawl import ManualRecrawlError, ManualRecrawlRunner

    runner = ManualRecrawlRunner(
        runtime_root=runtime_root,
        crawler_factory=lambda: crawler,
        dispatch_exact=dispatch,
        mappings_loader=lambda: [COMIX],
    )

    with pytest.raises(ManualRecrawlError) as excinfo:
        runner.start(BUSINESS_DATE, candidate_id=1)

    assert excinfo.value.code == "STATE_DB_MISSING"
    assert crawler.constructed == 0
    assert crawler.calls == 0
    assert dispatch.calls == 0
    # nothing was created on disk by the failed start
    assert not (runtime_root / PREWARM_DB_NAME).exists()
    assert not (runtime_root / SHARED_CACHE_NAME).exists()


def test_candidate_guards_not_found_and_stale_link(runtime):
    crawler = RecordingCrawler()
    dispatch = RecordingDispatch()
    runner = _make_runner(
        runtime, crawler_factory=lambda: crawler, dispatch_exact=dispatch
    )
    from app.services.prewarm_manual_recrawl import ManualRecrawlError

    with pytest.raises(ManualRecrawlError) as excinfo:
        runner.start(BUSINESS_DATE, candidate_id=424242)
    assert excinfo.value.code == "CANDIDATE_NOT_FOUND"

    candidate_id = _seed_candidate(
        runtime.prewarm, STAPLES_URL, "D1", mall="stbchina"
    )

    with pytest.raises(ManualRecrawlError) as excinfo:
        runner.start(
            BUSINESS_DATE,
            candidate_id=candidate_id,
            expected_original_url="https://www.stbchina.cn/item.html?itemId=other",
        )
    assert excinfo.value.code == "STALE_PREWARM_LINK"

    with pytest.raises(ManualRecrawlError) as excinfo:
        runner.start(BUSINESS_DATE, candidate_id=candidate_id, expected_status="CACHE_HIT")
    assert excinfo.value.code == "STALE_PREWARM_LINK"

    # synchronous guards never reach the worker side
    assert crawler.constructed == 0 and crawler.calls == 0
    assert dispatch.calls == 0


def test_unsupported_route_rejected(runtime):
    from app.services.prewarm_manual_recrawl import ManualRecrawlError

    runner = _make_runner(runtime)
    candidate_id = _seed_candidate(
        runtime.prewarm, "https://detail.tmall.com/item.htm?id=5", "D1", mall="tmall"
    )

    with pytest.raises(ManualRecrawlError) as excinfo:
        runner.start(BUSINESS_DATE, candidate_id=candidate_id)

    assert excinfo.value.code == "RECRAWL_NOT_SUPPORTED"
    assert "MANUAL_SKIP" in str(excinfo.value)


# ---- staples success --------------------------------------------------------------


def test_staples_success_rewrites_cache_preserves_history_and_candidate(runtime):
    # pre-existing current success row (yesterday's prewarm)
    runtime.cache.record_success(
        mall=STAPLES_CACHE_MALL_KEY,
        submitted_url=STAPLES_URL,
        source="staples-local",
        price="9.9",
    )
    old_hit = runtime.cache.lookup_current(STAPLES_CACHE_MALL_KEY, STAPLES_URL)
    assert old_hit is not None
    old_entry_id = int(old_hit["entry"]["id"])

    candidate_id = _seed_candidate(
        runtime.prewarm, STAPLES_URL, "D1", mall="stbchina", cache_status=CACHE_STATUS_CACHE_HIT
    )
    before = runtime.prewarm.candidate_by_id(candidate_id)
    crawler = RecordingCrawler(outcomes={STAPLES_URL: _success_outcome(STAPLES_URL)})
    dispatch = RecordingDispatch(raises=True)  # poison: never allowed here
    runner = _make_runner(
        runtime, crawler_factory=lambda: crawler, dispatch_exact=dispatch
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)

    assert handle["status"] == "RUNNING"
    assert handle["route"] == "staples-local"
    assert handle["mall"] == "stbchina"
    assert handle["original_url"] == STAPLES_URL
    runner._join_for_tests(handle["action_id"])

    finished = runner.get(handle["action_id"])
    assert finished is not None
    assert finished["status"] == "SUCCEEDED"
    assert finished["finished_at"] is not None
    assert finished["outcome"]["result_id"] > 0
    assert finished["outcome"]["price"] == 12.5

    # crawler saw EXACTLY one request with the EXACT URL
    assert crawler.calls == 1
    assert crawler.seen_requests == [[{"original_url": STAPLES_URL}]]
    assert dispatch.calls == 0

    # cache: new current row with the manual-recrawl source+producer; history kept
    hit = runtime.cache.lookup_current(STAPLES_CACHE_MALL_KEY, STAPLES_URL)
    assert hit is not None
    assert hit["entry"]["source"] == "manual-recrawl-staples"
    assert hit["entry"]["producer"] == PRODUCER_MANUAL_RECRAWL_STAPLES
    # cache stores the normalized price representation (string form of 12.5)
    assert float(hit["entry"]["price"]) == 12.5
    assert hit["history_count"] == 2
    history = runtime.cache.history(STAPLES_CACHE_MALL_KEY, STAPLES_URL)
    sources = {int(row["id"]): str(row["source"]) for row in history}
    assert sources[old_entry_id] == "staples-local"  # old row still queryable
    assert len(history) == 2

    # candidate row untouched
    assert runtime.prewarm.candidate_by_id(candidate_id) == before

    # §15 honesty: the synchronous staples success carries the cache proof
    assert finished["outcome"]["completion_proof"] == "cache_current"

    # durable JSONL intent log: started + finished, no secrets
    log_path = runtime.runtime_root / "customer_prewarm" / ACTIONS_LOG_NAME
    assert log_path.is_file()
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["started", "finished"]
    assert all(event["action_id"] == handle["action_id"] for event in events)
    assert all(event["business_date"] == BUSINESS_DATE for event in events)
    assert events[1]["status"] == "SUCCEEDED"
    assert "outcome_summary" in events[1]


def test_staples_failure_writes_nothing(runtime):
    runtime.cache.record_success(
        mall=STAPLES_CACHE_MALL_KEY,
        submitted_url=STAPLES_URL,
        source="staples-local",
        price="9.9",
    )
    old_hit = runtime.cache.lookup_current(STAPLES_CACHE_MALL_KEY, STAPLES_URL)
    old_entry_id = int(old_hit["entry"]["id"])
    candidate_id = _seed_candidate(
        runtime.prewarm, STAPLES_URL, "D1", mall="stbchina", cache_status=CACHE_STATUS_CACHE_HIT
    )
    before = runtime.prewarm.candidate_by_id(candidate_id)
    crawler = RecordingCrawler(outcomes={STAPLES_URL: _transient_outcome(STAPLES_URL)})
    runner = _make_runner(runtime, crawler_factory=lambda: crawler, dispatch_exact=RecordingDispatch())

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])

    finished = runner.get(handle["action_id"])
    assert finished["status"] == "FAILED"
    assert finished["outcome"]["outcome"] == "TRANSIENT_FAILURE"
    assert finished["outcome"]["diagnostic"] == "网络抖动"

    # cache untouched: current is still the old row, history_count still 1
    hit = runtime.cache.lookup_current(STAPLES_CACHE_MALL_KEY, STAPLES_URL)
    assert int(hit["entry"]["id"]) == old_entry_id
    assert hit["entry"]["source"] == "staples-local"
    assert hit["history_count"] == 1
    # candidate untouched
    assert runtime.prewarm.candidate_by_id(candidate_id) == before


def test_staples_lock_busy_reports_lock_busy_honestly(runtime, monkeypatch):
    from app.services.daily_audit_store import ProcessLock
    from scripts.staples_prewarm_run import STAPLES_LOCK_NAME

    candidate_id = _seed_candidate(
        runtime.prewarm, STAPLES_URL, "D1", mall="stbchina"
    )
    lock = ProcessLock(runtime.runtime_root / STAPLES_LOCK_NAME)
    lock.acquire()
    try:
        crawler = RecordingCrawler(outcomes={STAPLES_URL: _success_outcome(STAPLES_URL)})
        runner = _make_runner(runtime, crawler_factory=lambda: crawler)
        handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
        runner._join_for_tests(handle["action_id"])
    finally:
        lock.release()

    finished = runner.get(handle["action_id"])
    assert finished["status"] == "FAILED"
    assert finished["outcome"]["outcome"] == "LOCK_BUSY"
    assert crawler.calls == 0  # never crawled while the lock was held elsewhere


# ---- zhaoshang honest lifecycle (Round 2 Blocker 4) --------------------------------


def _cache_snapshot(cache, mall, url):
    """(current entry id, history [(id, source, succeeded_at)]) — 写入边界断言用。"""
    hit = cache.lookup_current(mall, url)
    current_id = int(hit["entry"]["id"]) if hit is not None else None
    history = [
        (int(row["id"]), str(row["source"]))
        for row in cache.history(mall, url)
    ]
    return current_id, history


def test_dispatch_created_confirmed_is_waiting_not_success(runtime):
    """派发 ≠ 成功:CREATED+priority_confirmed → WAITING_REMOTE,不是 SUCCEEDED。

    无新缓存行时 get() 保持 WAITING_REMOTE;旧 current 行(早于 started_at)
    仍然 current 且可查。
    """
    runtime.cache.record_success(
        mall="comix", submitted_url=COMIX_URL_1, source="daily-internet", price="9.9"
    )
    old_current_id, _ = _cache_snapshot(runtime.cache, "comix", COMIX_URL_1)
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": True}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    assert handle["status"] == "RUNNING"
    runner._join_for_tests(handle["action_id"])

    waiting = runner.get(handle["action_id"])
    assert waiting is not None
    assert waiting["status"] == "WAITING_REMOTE"  # NOT SUCCEEDED
    assert waiting["outcome"]["dispatch"]["outcome"] == "CREATED"
    assert waiting["outcome"]["dispatch"]["priority_confirmed"] is True
    assert waiting["route"] == "zhaoshang"
    assert waiting["mall"] == "comix"
    assert "completion_proof" not in waiting["outcome"]
    assert "new_result" not in waiting["outcome"]

    # 派发只发生一次;后续 get() 不再派发、不再翻转
    assert dispatch.calls == 1
    for _ in range(3):
        again = runner.get(handle["action_id"])
        assert again["status"] == "WAITING_REMOTE"
    assert dispatch.calls == 1

    # 旧 current 行仍然 current 且可查(动作没写缓存)
    hit = runtime.cache.lookup_current("comix", COMIX_URL_1)
    assert int(hit["entry"]["id"]) == old_current_id
    assert hit["entry"]["succeeded_at"] < waiting["started_at"]


def test_exact_job_id_new_current_succeeds(runtime):
    """G2 相关性语义 6(既有 happy path 更新):手动 producer 行,payload
    (business_date, remote_job_id) 与 CREATED 派发(无 job_business_date →
    动作日兜底)完全一致 + qualifying 时间 → get() → SUCCEEDED。

    完成证明 completion_proof=cache_current + new_result 摘要(白名单,无
    payload);旧行保留(history_count 递增)。无相关行时绝不翻转。
    """
    runtime.cache.record_success(
        mall="comix", submitted_url=COMIX_URL_1, source="daily-internet", price="9.9"
    )
    old_current_id, _ = _cache_snapshot(runtime.cache, "comix", COMIX_URL_1)
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": True}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    assert runner.get(handle["action_id"])["status"] == "WAITING_REMOTE"

    # 远端同步落地:真实 SharedCrawlCache RW 写入新成功行(succeeded_at=now)
    new_row = _write_sync_row(runtime.cache, COMIX_URL_1, _sync_payload(remote_job_id=7))

    done = runner.get(handle["action_id"])
    assert done["status"] == "SUCCEEDED"
    assert done["outcome"]["completion_proof"] == "cache_current"
    new_result = done["outcome"]["new_result"]
    assert int(new_result["result_id"]) == int(new_row["id"])
    assert new_result["price"] == "13.5"
    assert new_result["title"] == "新结果标题"
    assert new_result["producer"] == PRODUCER_MANUAL_RECRAWL_ZHAOSHANG
    assert new_result["succeeded_at"]
    assert "payload" not in new_result  # payload 只用于内部证明,绝不出白名单
    # 派发透传与完成证明共存
    assert done["outcome"]["dispatch"]["outcome"] == "CREATED"

    # 旧行保留为历史:history_count 2,旧 entry 仍可查
    hit = runtime.cache.lookup_current("comix", COMIX_URL_1)
    assert int(hit["entry"]["id"]) == int(new_row["id"])
    assert hit["history_count"] == 2
    history_ids = {int(row["id"]) for row in runtime.cache.history("comix", COMIX_URL_1)}
    assert history_ids == {old_current_id, int(new_row["id"])}

    # 终态幂等:后续 get() 保持 SUCCEEDED
    assert runner.get(handle["action_id"])["status"] == "SUCCEEDED"


# ---- G2 correlation matrix (Round 4 Slice G2) --------------------------------------


def test_wrong_remote_job_cannot_complete(runtime):
    """G2 相关性矩阵 1:同 producer(手动)但 payload.remote_job_id 属于另一
    任务(例如另一并发手动动作)→ 不是本动作的完成:保持 WAITING_REMOTE、
    记一次 superseded 注记,绝不 SUCCEEDED。"""
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": True}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )
    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    assert runner.get(handle["action_id"])["status"] == "WAITING_REMOTE"

    _write_sync_row(runtime.cache, COMIX_URL_1, _sync_payload(remote_job_id=999))

    waiting = runner.get(handle["action_id"])
    assert waiting["status"] == "WAITING_REMOTE"
    assert waiting["outcome"]["superseded_current_producer"] == PRODUCER_MANUAL_RECRAWL_ZHAOSHANG
    assert "completion_proof" not in waiting["outcome"]
    assert "new_result" not in waiting["outcome"]
    # 重复轮询绝不翻转(setdefault 注记不刷屏)
    again = runner.get(handle["action_id"])
    assert again["status"] == "WAITING_REMOTE"
    assert again["outcome"]["superseded_current_producer"] == PRODUCER_MANUAL_RECRAWL_ZHAOSHANG


def test_missing_payload_remote_job_cannot_complete(runtime):
    """G2 相关性矩阵 2:手动 producer 行 payload 缺 remote_job_id(手动/
    staples record_success 自带 payload 的真实形态)→ 无法证明相关性,
    保持 WAITING_REMOTE,绝不 SUCCEEDED。"""
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": True}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )
    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])

    _write_sync_row(runtime.cache, COMIX_URL_1, {"business_date": BUSINESS_DATE})

    waiting = runner.get(handle["action_id"])
    assert waiting["status"] == "WAITING_REMOTE"
    assert "completion_proof" not in waiting["outcome"]
    assert "new_result" not in waiting["outcome"]


def test_job_date_mismatch_cannot_complete(runtime):
    """G2 相关性矩阵 3:同 remote_job_id 但 payload.business_date ≠ 派发任务日
    (CREATED → 动作日兜底)→ 不同任务/不同日,保持 WAITING_REMOTE。"""
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": True}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )
    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])

    _write_sync_row(
        runtime.cache, COMIX_URL_1, _sync_payload(business_date="2026-09-13", remote_job_id=7)
    )

    waiting = runner.get(handle["action_id"])
    assert waiting["status"] == "WAITING_REMOTE"
    assert "completion_proof" not in waiting["outcome"]
    assert "new_result" not in waiting["outcome"]


def test_other_producer_different_job_cannot_complete(runtime):
    """G2 相关性矩阵 4:daily-internet 行(另一任务的同步结果,qualifying
    时间)→ 不是本动作的完成,仅记一次 superseded 来源注记并继续等待。"""
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": True}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )
    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])

    _write_sync_row(
        runtime.cache,
        COMIX_URL_1,
        _sync_payload(remote_job_id=5),
        producer=PRODUCER_DAILY_INTERNET,
    )

    waiting = runner.get(handle["action_id"])
    assert waiting["status"] == "WAITING_REMOTE"
    assert waiting["outcome"]["superseded_current_producer"] == PRODUCER_DAILY_INTERNET
    assert "completion_proof" not in waiting["outcome"]
    assert "new_result" not in waiting["outcome"]


def test_reused_customer_prewarm_job_completes_by_exact_job_id(runtime):
    """G2 核心缺陷修复(REUSED 路径):复用任务是 AUTO 客户预爬先建的 →
    linkage PK 首绑 producer=customer-prewarm-zhaoshang;远端同步行的
    producer 因此不是手动,但 payload (business_date, remote_job_id) 与
    REUSED 派发的 (job_business_date, remote_job_id) 完全一致 → 必须完成。
    producer 永不当门槛,只在 new_result 里做诊断展示;payload 不外泄。"""
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={
            candidate_id: {
                "outcome": "REUSED",
                "remote_job_id": 3,
                "priority_proven": True,
                "cross_day": False,
                "job_business_date": BUSINESS_DATE,
            }
        }
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )
    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    assert runner.get(handle["action_id"])["status"] == "WAITING_REMOTE"

    # linkage 首绑保留 AUTO producer(真实形态),同步行 producer 随 linkage
    row = _write_sync_row(
        runtime.cache,
        COMIX_URL_1,
        _sync_payload(business_date=BUSINESS_DATE, remote_job_id=3),
        producer=PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG,
    )

    done = runner.get(handle["action_id"])
    assert done["status"] == "SUCCEEDED"
    assert done["outcome"]["completion_proof"] == "cache_current"
    new_result = done["outcome"]["new_result"]
    assert int(new_result["result_id"]) == int(row["id"])
    assert new_result["producer"] == PRODUCER_CUSTOMER_PREWARM_ZHAOSHANG
    assert "payload" not in new_result
    assert done["outcome"]["dispatch"]["outcome"] == "REUSED"
    assert "superseded_current_producer" not in done["outcome"]
    # 终态幂等
    assert runner.get(handle["action_id"])["status"] == "SUCCEEDED"


def test_created_unconfirmed_is_priority_pending_never_success(runtime):
    """CREATED 未确认优先级 → PRIORITY_PENDING;新缓存行也不自动翻转。"""
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": False}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    pending = runner.get(handle["action_id"])
    assert pending["status"] == "PRIORITY_PENDING"
    assert pending["outcome"]["dispatch"]["priority_confirmed"] is False

    # 远端爬完了也不会自动翻转 —— 只有新动作能重新证明优先级
    runtime.cache.record_success(
        mall="comix",
        submitted_url=COMIX_URL_1,
        source="daily-internet",
        producer=PRODUCER_MANUAL_RECRAWL_ZHAOSHANG,
        price="15.0",
    )
    assert dispatch.calls == 1
    still = runner.get(handle["action_id"])
    assert still["status"] == "PRIORITY_PENDING"  # never SUCCEEDED
    assert "completion_proof" not in still["outcome"]
    assert dispatch.calls == 1  # poll path never re-dispatches

    # D8:PRIORITY_PENDING 是操作员可重试的终态 —— in-flight 已释放,
    # 同一 URL 立即可发起新动作(其 probe/boost 可能重新证明优先级)。
    retry = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    assert retry["action_id"] != handle["action_id"]
    runner._join_for_tests(retry["action_id"])


def test_reused_unconfirmed_is_priority_pending(runtime):
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "REUSED", "remote_job_id": 3, "priority_proven": False}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    finished = runner.get(handle["action_id"])
    assert finished["status"] == "PRIORITY_PENDING"
    assert finished["outcome"]["dispatch"]["outcome"] == "REUSED"
    assert finished["outcome"]["dispatch"]["priority_proven"] is False


def test_reused_none_terminal_success_is_waiting(runtime):
    """D12:REUSED + priority_proven None + job_terminal_success → WAITING_REMOTE。

    不可证实优先级但再探测发现终态 SUCCESS 爬取行:URL 实际已完成 —— 诚实
    等待(不是成功也不是失败),outcome 记 priority_moot_terminal_success。
    """
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={
            candidate_id: {
                "outcome": "REUSED",
                "remote_job_id": 3,
                "priority_proven": None,
                "cross_day": False,
                "job_business_date": BUSINESS_DATE,
                "job_terminal_success": True,
            }
        }
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    waiting = runner.get(handle["action_id"])
    assert waiting["status"] == "WAITING_REMOTE"
    assert waiting["outcome"]["priority_moot_terminal_success"] is True
    assert waiting["outcome"]["dispatch"]["outcome"] == "REUSED"
    assert waiting["outcome"]["dispatch"]["priority_proven"] is None
    assert "completion_proof" not in waiting["outcome"]  # 仍未带缓存证明


def test_reused_none_other_is_failed(runtime):
    """D12:REUSED + priority_proven None(不可证实且非终态成功)→ FAILED。

    旧行为是 PRIORITY_PENDING(永远无人收尾);新语义:None 意味着该复用
    任务不可提升(SUBMIT_UNKNOWN/可重试),诚实失败并原样透传 dispatch。
    """
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch_dict = {
        "outcome": "REUSED",
        "remote_job_id": 3,
        "priority_proven": None,
        "cross_day": False,
        "job_business_date": BUSINESS_DATE,
    }
    dispatch = RecordingDispatch(results={candidate_id: dict(dispatch_dict)})
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    finished = runner.get(handle["action_id"])
    assert finished["status"] == "FAILED"
    assert finished["outcome"]["dispatch"] == dispatch_dict  # verbatim


def test_reused_proven_is_waiting_remote(runtime):
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "REUSED", "remote_job_id": 3, "priority_proven": True}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    waiting = runner.get(handle["action_id"])
    assert waiting["status"] == "WAITING_REMOTE"
    assert waiting["outcome"]["dispatch"]["priority_proven"] is True


def test_dispatch_outcomes_map_to_failed_honestly(runtime):
    """SUBMIT_UNKNOWN / LOCK_BUSY / NOT_DISPATCHABLE → FAILED 诚实透传,旧 current 不动。"""
    runtime.cache.record_success(
        mall="comix", submitted_url=COMIX_URL_1, source="daily-internet", price="9.9"
    )
    old_current_id, old_history = _cache_snapshot(runtime.cache, "comix", COMIX_URL_1)
    c1 = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    c2 = _seed_candidate(runtime.prewarm, COMIX_URL_2, "D2", mall="comix")
    c3 = _seed_candidate(runtime.prewarm, "https://qx.com/manual-recrawl-3.html", "D3", mall="comix")
    dispatch = RecordingDispatch(
        results={
            c1: {"outcome": "SUBMIT_UNKNOWN", "remote_job_id": 9, "diagnostic": "timeout"},
            c2: {"outcome": "LOCK_BUSY"},
            c3: {"outcome": "NOT_DISPATCHABLE", "reason": "MANUAL_SKIP"},
        }
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    for candidate_id, expected_outcome in (
        (c1, "SUBMIT_UNKNOWN"),
        (c2, "LOCK_BUSY"),
        (c3, "NOT_DISPATCHABLE"),
    ):
        handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
        runner._join_for_tests(handle["action_id"])
        finished = runner.get(handle["action_id"])
        assert finished["status"] == "FAILED", expected_outcome
        assert finished["outcome"]["dispatch"]["outcome"] == expected_outcome
    assert dispatch.calls == 3  # 每个失败动作只 POST 一次,绝不二次派发

    # 旧 current 完全未动
    current_id, history = _cache_snapshot(runtime.cache, "comix", COMIX_URL_1)
    assert current_id == old_current_id
    assert history == old_history


def test_old_current_preserved_while_waiting_and_on_failure(runtime):
    """WAITING_REMOTE 与 FAILED 全程零缓存写入:快照前后 current/history 不变。"""
    runtime.cache.record_success(
        mall="comix", submitted_url=COMIX_URL_1, source="daily-internet", price="9.9"
    )
    before_current, before_history = _cache_snapshot(runtime.cache, "comix", COMIX_URL_1)
    c_waiting = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    c_failed = _seed_candidate(runtime.prewarm, COMIX_URL_2, "D2", mall="comix")
    dispatch = RecordingDispatch(
        results={
            c_waiting: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": True},
            c_failed: {"outcome": "SUBMIT_UNKNOWN", "diagnostic": "timeout"},
        }
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle_waiting = runner.start(BUSINESS_DATE, candidate_id=c_waiting)
    runner._join_for_tests(handle_waiting["action_id"])
    assert runner.get(handle_waiting["action_id"])["status"] == "WAITING_REMOTE"
    current_id, history = _cache_snapshot(runtime.cache, "comix", COMIX_URL_1)
    assert current_id == before_current and history == before_history

    handle_failed = runner.start(BUSINESS_DATE, candidate_id=c_failed)
    runner._join_for_tests(handle_failed["action_id"])
    assert runner.get(handle_failed["action_id"])["status"] == "FAILED"
    current_id, history = _cache_snapshot(runtime.cache, "comix", COMIX_URL_1)
    assert current_id == before_current and history == before_history


def test_waiting_remote_survives_evaluation_error(runtime, monkeypatch):
    """完成评估绝不抛错:lookup_current 崩溃 → 保持 WAITING_REMOTE + last_check_error。"""
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": True}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )
    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])

    def boom(_self, mall, url):
        raise RuntimeError("cache reader exploded")

    monkeypatch.setattr(
        "app.services.shared_crawl_cache.SharedCrawlCache.lookup_current", boom
    )
    result = runner.get(handle["action_id"])
    assert result["status"] == "WAITING_REMOTE"
    assert "RuntimeError" in result["outcome"]["last_check_error"]
    monkeypatch.undo()
    assert runner.get(handle["action_id"])["status"] == "WAITING_REMOTE"


def test_remote_job_terminal_failed_fails_action(runtime):
    """D9:远端任务终态 FAILED → 动作 FAILED(failed_by),旧 current 不动。

    派发 dict 带 remote_job_id + job_business_date:runner 以 mode=ro 打开
    runtime_root/<job_business_date>/state.sqlite3 读该任务状态。COMPLETED/
    在途不翻转(仍走缓存证明路径);FAILED 则诚实失败并原样保留 dispatch。
    """
    runtime.cache.record_success(
        mall="comix", submitted_url=COMIX_URL_1, source="daily-internet", price="9.9"
    )
    old_current_id, old_history = _cache_snapshot(runtime.cache, "comix", COMIX_URL_1)
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch_dict = {
        "outcome": "CREATED",
        "remote_job_id": 7,
        "priority_confirmed": True,
        "job_business_date": BUSINESS_DATE,
    }
    dispatch = RecordingDispatch(results={candidate_id: dict(dispatch_dict)})
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    # 任务仍在途(状态库尚未有 FAILED 行)→ 保持 WAITING_REMOTE
    assert runner.get(handle["action_id"])["status"] == "WAITING_REMOTE"

    _write_state_db(runtime.runtime_root, [(7, "FAILED")])
    finished = runner.get(handle["action_id"])
    assert finished["status"] == "FAILED"
    assert finished["outcome"]["failed_by"] == "remote_job_terminal_failed"
    assert finished["outcome"]["dispatch"] == dispatch_dict  # dispatch 原样保留
    assert finished["finished_at"] is not None

    # 旧 current 完全未动(零缓存写入)
    current_id, history = _cache_snapshot(runtime.cache, "comix", COMIX_URL_1)
    assert current_id == old_current_id
    assert history == old_history

    # D8:FAILED 终态释放 in-flight —— 同一 URL 立即可重试
    retry = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    assert retry["action_id"] != handle["action_id"]


def test_remote_job_terminal_failed_created_without_job_date_uses_action_day(runtime):
    """F4(补 F3 残留):CREATED 派发 dict 无 ``job_business_date``(F2 契约:
    CREATED 即当日构造)→ 动作日兜底。当日真实状态库中该任务终态 FAILED
    → get() 诚实失败(failed_by=remote_job_terminal_failed),不再永远等待。"""
    runtime.cache.record_success(
        mall="comix", submitted_url=COMIX_URL_1, source="daily-internet", price="9.9"
    )
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    # CREATED 形态:remote_job_id + priority_confirmed,无 job_business_date
    dispatch = RecordingDispatch(
        results={
            candidate_id: {
                "outcome": "CREATED",
                "remote_job_id": 7,
                "priority_confirmed": True,
            }
        }
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )
    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    assert runner.get(handle["action_id"])["status"] == "WAITING_REMOTE"

    # 动作日(默认日)真实状态库:任务 7 终态 FAILED
    _write_state_db(runtime.runtime_root, [(7, "FAILED")])
    finished = runner.get(handle["action_id"])
    assert finished["status"] == "FAILED"
    assert finished["outcome"]["failed_by"] == "remote_job_terminal_failed"
    assert finished["outcome"]["dispatch"]["outcome"] == "CREATED"
    assert finished["finished_at"] is not None


def test_remote_job_terminal_failed_missing_db_keeps_waiting(runtime):
    """D9 兜底:状态库缺失/不可读 → 记 last_check_error,保持 WAITING_REMOTE,绝不抛。"""
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={
            candidate_id: {
                "outcome": "CREATED",
                "remote_job_id": 7,
                "priority_confirmed": True,
                "job_business_date": BUSINESS_DATE,
            }
        }
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )
    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])

    result = runner.get(handle["action_id"])
    assert result["status"] == "WAITING_REMOTE"  # never raise, never fail-closed
    assert "last_check_error" in result["outcome"]
    # 后续轮询仍可继续(不卡死评估路径)
    assert runner.get(handle["action_id"])["status"] == "WAITING_REMOTE"


def test_uncorrelated_row_noted_once_then_exact_row_completes(runtime):
    """G2 相关性语义(取代 D10 producer 门槛):qualifying 时间落地但不携带
    本任务 (date, job_id) 的新 current 行 → 不是完成,仅记一次
    superseded_current_producer 并保持 WAITING_REMOTE(D9 的失败路径最终
    兜底);随后 (date, job_id) 精确一致的手动行落地 → SUCCEEDED 带完成
    证明,identity 的 current 变为手动行、历史按序保留(含被顶掉的不相关行)。
    """
    runtime.cache.record_success(
        mall="comix", submitted_url=COMIX_URL_1, source="daily-internet", price="9.9"
    )
    old_current_id, _ = _cache_snapshot(runtime.cache, "comix", COMIX_URL_1)
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    dispatch = RecordingDispatch(
        results={candidate_id: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": True}}
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    assert runner.get(handle["action_id"])["status"] == "WAITING_REMOTE"

    # 不相关新 current 行(qualifying 时间;旧式 daily 行,无 job payload)
    # → 不成功,记一次 superseded 来源
    daily_row = runtime.cache.record_success(
        mall="comix",
        submitted_url=COMIX_URL_1,
        source="daily-internet",
        producer=PRODUCER_DAILY_INTERNET,
        price="11.0",
    )
    waiting = runner.get(handle["action_id"])
    assert waiting["status"] == "WAITING_REMOTE"
    assert waiting["outcome"]["superseded_current_producer"] == PRODUCER_DAILY_INTERNET
    assert "completion_proof" not in waiting["outcome"]
    again = runner.get(handle["action_id"])
    assert again["outcome"]["superseded_current_producer"] == PRODUCER_DAILY_INTERNET  # 只记一次

    # (date, job_id) 精确一致的本任务手动行 → SUCCEEDED
    manual_row = _write_sync_row(runtime.cache, COMIX_URL_1, _sync_payload(remote_job_id=7))
    done = runner.get(handle["action_id"])
    assert done["status"] == "SUCCEEDED"
    assert done["outcome"]["completion_proof"] == "cache_current"
    assert int(done["outcome"]["new_result"]["result_id"]) == int(manual_row["id"])
    assert done["outcome"]["new_result"]["producer"] == PRODUCER_MANUAL_RECRAWL_ZHAOSHANG

    # current = 手动行;daily 行不再 current;历史按序保留旧行
    hit = runtime.cache.lookup_current("comix", COMIX_URL_1)
    assert int(hit["entry"]["id"]) == int(manual_row["id"])
    history_ids = [int(row["id"]) for row in runtime.cache.history("comix", COMIX_URL_1)]
    assert history_ids == [int(old_current_id), int(daily_row["id"]), int(manual_row["id"])]


def test_inflight_held_until_terminal(runtime):
    """D8:WAITING_REMOTE 全程持有 in-flight key;懒终态翻转立即释放。

    - CREATED+confirmed 派发后(WAITING_REMOTE),同一 URL 二次 start 报
      ACTION_IN_FLIGHT(前端 409);
    - 懒 SUCCEEDED(真实缓存写入 manual producer + get())后二次 start 成功;
    - D9 的 FAILED 同样释放 key(下一次 start 允许)。
    """
    candidate_id = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    candidate_failed = _seed_candidate(runtime.prewarm, COMIX_URL_2, "D2", mall="comix")
    dispatch = RecordingDispatch(
        results={
            candidate_id: {"outcome": "CREATED", "remote_job_id": 21, "priority_confirmed": True},
            candidate_failed: {
                "outcome": "CREATED",
                "remote_job_id": 22,
                "priority_confirmed": True,
                "job_business_date": BUSINESS_DATE,
            },
        }
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )
    from app.services.prewarm_manual_recrawl import ManualRecrawlError

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    assert runner.get(handle["action_id"])["status"] == "WAITING_REMOTE"

    # WAITING_REMOTE 期间同一 URL 二次发起 → ACTION_IN_FLIGHT(API 映射 409)
    with pytest.raises(ManualRecrawlError) as excinfo:
        runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    assert excinfo.value.code == "ACTION_IN_FLIGHT"
    assert excinfo.value.existing_action_id == handle["action_id"]

    # 懒 SUCCEEDED(精确关联 (date, job 21) 的手动新 current 行)→ key 释放
    _write_sync_row(
        runtime.cache, COMIX_URL_1, _sync_payload(remote_job_id=21), price="14.0"
    )
    assert runner.get(handle["action_id"])["status"] == "SUCCEEDED"
    again = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    assert again["action_id"] != handle["action_id"]
    runner._join_for_tests(again["action_id"])

    # D9 的 FAILED 也释放 key
    _write_state_db(runtime.runtime_root, [(22, "FAILED")])
    failed_handle = runner.start(BUSINESS_DATE, candidate_id=candidate_failed)
    runner._join_for_tests(failed_handle["action_id"])
    assert runner.get(failed_handle["action_id"])["status"] == "FAILED"
    retry = runner.start(BUSINESS_DATE, candidate_id=candidate_failed)
    assert retry["action_id"] != failed_handle["action_id"]


def test_completed_jsonl_event(runtime):
    """D11:每次懒终态翻转(懒 SUCCEEDED / D9 FAILED)追加 completed JSONL 事件。

    事件字段:action_id/event/status/route/mall/original_url/business_date/
    outcome_summary(shallow)/recorded_at;worker 的 finished 事件保持不变。
    """
    c_ok = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    c_failed = _seed_candidate(runtime.prewarm, COMIX_URL_2, "D2", mall="comix")
    dispatch = RecordingDispatch(
        results={
            c_ok: {"outcome": "CREATED", "remote_job_id": 31, "priority_confirmed": True},
            c_failed: {
                "outcome": "CREATED",
                "remote_job_id": 32,
                "priority_confirmed": True,
                "job_business_date": BUSINESS_DATE,
            },
        }
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    ok_handle = runner.start(BUSINESS_DATE, candidate_id=c_ok)
    runner._join_for_tests(ok_handle["action_id"])
    _write_sync_row(runtime.cache, COMIX_URL_1, _sync_payload(remote_job_id=31), price="16.0")
    assert runner.get(ok_handle["action_id"])["status"] == "SUCCEEDED"

    _write_state_db(runtime.runtime_root, [(32, "FAILED")])
    failed_handle = runner.start(BUSINESS_DATE, candidate_id=c_failed)
    runner._join_for_tests(failed_handle["action_id"])
    assert runner.get(failed_handle["action_id"])["status"] == "FAILED"

    log_path = runtime.runtime_root / "customer_prewarm" / ACTIONS_LOG_NAME
    assert log_path.is_file()
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    ok_completed = [
        e for e in events
        if e["event"] == "completed" and e["action_id"] == ok_handle["action_id"]
    ]
    assert len(ok_completed) == 1
    assert ok_completed[0]["status"] == "SUCCEEDED"
    assert ok_completed[0]["route"] == "zhaoshang"
    assert ok_completed[0]["mall"] == "comix"
    assert ok_completed[0]["original_url"] == COMIX_URL_1
    assert ok_completed[0]["business_date"] == BUSINESS_DATE
    assert ok_completed[0]["outcome_summary"]["completion_proof"] == "cache_current"
    assert ok_completed[0]["recorded_at"]

    failed_completed = [
        e for e in events
        if e["event"] == "completed" and e["action_id"] == failed_handle["action_id"]
    ]
    assert len(failed_completed) == 1
    assert failed_completed[0]["status"] == "FAILED"
    assert failed_completed[0]["outcome_summary"]["failed_by"] == "remote_job_terminal_failed"

    # worker 的 finished 事件仍按原样存在(每次动作 started/finished 各一条)
    for handle in (ok_handle, failed_handle):
        same = [e for e in events if e["action_id"] == handle["action_id"]]
        assert [e["event"] for e in same] == ["started", "finished", "completed"]


# ---- in-flight guard ----------------------------------------------------------------


def test_double_click_same_url_is_in_flight_different_urls_independent(runtime):
    candidate_staples = _seed_candidate(
        runtime.prewarm, STAPLES_URL, "D1", mall="stbchina"
    )
    candidate_z1 = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D2", mall="comix")
    candidate_z2 = _seed_candidate(runtime.prewarm, COMIX_URL_2, "D3", mall="comix")
    from app.services.prewarm_manual_recrawl import ManualRecrawlError

    gate_crawler = GateCrawler(outcomes={STAPLES_URL: _success_outcome(STAPLES_URL)})
    # D12 语义:确认的 CREATED → WAITING_REMOTE(不再止于 PRIORITY_PENDING)
    gate_dispatch = GateDispatch()
    runner = _make_runner(
        runtime, crawler_factory=lambda: gate_crawler, dispatch_exact=gate_dispatch
    )

    first = runner.start(BUSINESS_DATE, candidate_id=candidate_staples)
    # double click on the SAME URL before the first action finishes
    with pytest.raises(ManualRecrawlError) as excinfo:
        runner.start(BUSINESS_DATE, candidate_id=candidate_staples)
    assert excinfo.value.code == "ACTION_IN_FLIGHT"
    assert excinfo.value.existing_action_id == first["action_id"]

    # a different URL on the zhaoshang route runs independently
    second = runner.start(BUSINESS_DATE, candidate_id=candidate_z1)
    third = runner.start(BUSINESS_DATE, candidate_id=candidate_z2)
    assert second["action_id"] != third["action_id"]

    gate_crawler.gate.set()
    gate_dispatch.gate.set()
    runner._join_for_tests(first["action_id"])
    runner._join_for_tests(second["action_id"])
    runner._join_for_tests(third["action_id"])

    assert runner.get(first["action_id"])["status"] == "SUCCEEDED"  # staples sync crawl
    # D12:CREATED + priority_confirmed → WAITING_REMOTE(远端任务合法在途)
    assert runner.get(second["action_id"])["status"] == "WAITING_REMOTE"
    assert runner.get(third["action_id"])["status"] == "WAITING_REMOTE"
    assert gate_crawler.calls == 1  # the second click never re-crawled

    # D8:WAITING_REMOTE 期间同一 URL 的二次发起 → ACTION_IN_FLIGHT(API 409)
    with pytest.raises(ManualRecrawlError) as excinfo:
        runner.start(BUSINESS_DATE, candidate_id=candidate_z1)
    assert excinfo.value.code == "ACTION_IN_FLIGHT"
    assert excinfo.value.existing_action_id == second["action_id"]

    # staples worker 端即达终态 → key 已释放:同一 URL 可再次发起
    fourth = runner.start(BUSINESS_DATE, candidate_id=candidate_staples)
    runner._join_for_tests(fourth["action_id"])
    assert runner.get(fourth["action_id"])["status"] == "SUCCEEDED"


# ---- §44 business isolation -----------------------------------------------------------


_ISOLATION_MODULE_NAMES = (
    "scripts.process_monitor_no_push_zhaoshang",  # STEP3
    "scripts.export_current_monitor_status",  # STEP2
    "scripts.audit_saas_excel_file",  # closeout/finalize entry
    "scripts.push_saas_commodity_price",  # push entry
    "app.workflows",  # workflow package
)


class _IsolationSentinel:
    """sys.modules sentinel: ANY attribute access or set means an import/use."""

    def __init__(self, module_name: str) -> None:
        object.__setattr__(self, "_module_name", module_name)

    def __getattr__(self, item: str) -> Any:
        raise AssertionError(
            f"manual recrawl touched isolated module "
            f"{object.__getattribute__(self, '_module_name')}.{item}"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        raise AssertionError(
            f"manual recrawl bound an attribute on isolated module "
            f"{object.__getattribute__(self, '_module_name')}.{name}"
        )


def test_business_isolation_no_closeout_or_step_modules_touched(runtime, monkeypatch):
    candidate_staples = _seed_candidate(
        runtime.prewarm, STAPLES_URL, "D1", mall="stbchina"
    )
    candidate_zhaoshang = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D2", mall="comix")
    sentinels = {name: _IsolationSentinel(name) for name in _ISOLATION_MODULE_NAMES}
    for name, sentinel in sentinels.items():
        monkeypatch.setitem(sys.modules, name, sentinel)
    modules_before = set(sys.modules)

    crawler = RecordingCrawler(outcomes={STAPLES_URL: _success_outcome(STAPLES_URL)})
    dispatch = RecordingDispatch()
    runner = _make_runner(
        runtime, crawler_factory=lambda: crawler, dispatch_exact=dispatch
    )

    handle_staples = runner.start(BUSINESS_DATE, candidate_id=candidate_staples)
    handle_zhaoshang = runner.start(BUSINESS_DATE, candidate_id=candidate_zhaoshang)
    runner._join_for_tests(handle_staples["action_id"])
    runner._join_for_tests(handle_zhaoshang["action_id"])

    assert runner.get(handle_staples["action_id"])["status"] == "SUCCEEDED"
    # RecordingDispatch 默认 CREATED 无优先级证明 → 诚实 PRIORITY_PENDING
    assert runner.get(handle_zhaoshang["action_id"])["status"] == "PRIORITY_PENDING"
    # the sentinel objects are still in place: none of the isolated modules
    # was imported (or re-imported) during either action
    for name, sentinel in sentinels.items():
        assert sys.modules.get(name) is sentinel


# ---- runner singleton -----------------------------------------------------------------


def test_get_manual_recrawl_runner_is_process_singleton():
    from app.services.prewarm_manual_recrawl import get_manual_recrawl_runner

    runner = get_manual_recrawl_runner()
    assert get_manual_recrawl_runner() is runner
    assert runner.runtime_root == DEFAULT_RUNTIME_ROOT
