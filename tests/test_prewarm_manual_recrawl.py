"""prewarm_manual_recrawl 行为测试(Issue #13 Slice B)。

守卫式单 URL 手动重爬 runner:守卫顺序(库缺失 → 404 → 竞态链接 → 不支持
路由 → in-flight)、史泰博本地爬取(写缓存、绝不写候选)、招商局精确派发
(诚实透传)、双击防抖与 §44 业务隔离(closeout/STEP2/STEP3/push 模块在
手动重爬动作期间绝不 import)。

全程零真实爬虫/网络/子进程:fakes only。
"""

from __future__ import annotations

import json
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
from app.services.shared_crawl_cache import SharedCrawlCache
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

    def __init__(self):
        self.calls = 0
        self.gate = threading.Event()

    def __call__(self, _prewarm_store, _shared_cache, **kwargs):
        self.calls += 1
        self.gate.wait(timeout=5)
        return {"outcome": "CREATED", "remote_job_id": int(kwargs["candidate_id"])}


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

    # cache: new current row with the manual-recrawl source; history kept
    hit = runtime.cache.lookup_current(STAPLES_CACHE_MALL_KEY, STAPLES_URL)
    assert hit is not None
    assert hit["entry"]["source"] == "manual-recrawl-staples"
    # cache stores the normalized price representation (string form of 12.5)
    assert float(hit["entry"]["price"]) == 12.5
    assert hit["history_count"] == 2
    history = runtime.cache.history(STAPLES_CACHE_MALL_KEY, STAPLES_URL)
    sources = {int(row["id"]): str(row["source"]) for row in history}
    assert sources[old_entry_id] == "staples-local"  # old row still queryable
    assert len(history) == 2

    # candidate row untouched
    assert runtime.prewarm.candidate_by_id(candidate_id) == before

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


# ---- zhaoshang dispatch ------------------------------------------------------------


def test_zhaoshang_success_and_honest_failure_passthrough(runtime):
    candidate_ok = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D1", mall="comix")
    candidate_unknown = _seed_candidate(runtime.prewarm, COMIX_URL_2, "D2", mall="comix")
    dispatch = RecordingDispatch(
        results={
            candidate_ok: {"outcome": "CREATED", "remote_job_id": 7, "priority_confirmed": True},
            candidate_unknown: {"outcome": "SUBMIT_UNKNOWN", "remote_job_id": 9, "diagnostic": "timeout"},
        }
    )
    runner = _make_runner(
        runtime, crawler_factory=_poison_crawler_factory, dispatch_exact=dispatch
    )

    handle_ok = runner.start(BUSINESS_DATE, candidate_id=candidate_ok)
    runner._join_for_tests(handle_ok["action_id"])
    finished_ok = runner.get(handle_ok["action_id"])
    assert finished_ok["status"] == "SUCCEEDED"
    assert finished_ok["outcome"]["dispatch"]["outcome"] == "CREATED"
    assert finished_ok["route"] == "zhaoshang"
    assert finished_ok["mall"] == "comix"

    handle_unknown = runner.start(BUSINESS_DATE, candidate_id=candidate_unknown)
    runner._join_for_tests(handle_unknown["action_id"])
    finished_unknown = runner.get(handle_unknown["action_id"])
    # honest failure: the dispatch outcome is passed through verbatim
    assert finished_unknown["status"] == "FAILED"
    assert finished_unknown["outcome"]["dispatch"]["outcome"] == "SUBMIT_UNKNOWN"

    assert dispatch.calls == 2
    assert all(
        call.get("candidate_id") in (candidate_ok, candidate_unknown)
        for call in dispatch.seen_kwargs
    )


# ---- in-flight guard ----------------------------------------------------------------


def test_double_click_same_url_is_in_flight_different_urls_independent(runtime):
    candidate_staples = _seed_candidate(
        runtime.prewarm, STAPLES_URL, "D1", mall="stbchina"
    )
    candidate_z1 = _seed_candidate(runtime.prewarm, COMIX_URL_1, "D2", mall="comix")
    candidate_z2 = _seed_candidate(runtime.prewarm, COMIX_URL_2, "D3", mall="comix")
    from app.services.prewarm_manual_recrawl import ManualRecrawlError

    gate_crawler = GateCrawler(outcomes={STAPLES_URL: _success_outcome(STAPLES_URL)})
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

    assert runner.get(first["action_id"])["status"] == "SUCCEEDED"
    assert runner.get(second["action_id"])["status"] == "SUCCEEDED"
    assert runner.get(third["action_id"])["status"] == "SUCCEEDED"
    assert gate_crawler.calls == 1  # the second click never re-crawled

    # in-flight slot cleared: the same URL can start again after finishing
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
    assert runner.get(handle_zhaoshang["action_id"])["status"] == "SUCCEEDED"
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
