"""staples_prewarm 行为测试(Issue #11)。

覆盖:域名门与 itemId 解析、失败分类与有界退避、STAPLES_PENDING 路由态迁移、
sidecar 作业状态机(claim/租约/陈旧令牌/手动重试)、四阶段编排(路由→缓存
复检→缓存补同步→本地爬取)、成功/瞬时/终局失败、LOCAL_SUCCESS_CACHE_PENDING
缓存补同步、URL 证据链、SKU 证据降级(绝不 CONFIRMED)、计划只读助手,以及
P0 哨兵:史泰博路径绝不触碰任何招商局模块。全部用假爬虫/假执行器,无网络、
无真实子进程、无 E:\\.A2026 依赖。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.services.crawl_url_identity import (
    MALL_IDENTITY_PARSERS,
    SKU_STATUS_CONFIRMED,
    SKU_STATUS_UNRESOLVED,
    SKU_STATUS_UPSTREAM_PROVIDED,
    SKU_STATUS_URL_PARSED_UNVERIFIED,
    resolve_mall_product_identity,
    stbchina_product_identity,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_LOOKUP,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_STAPLES_PENDING,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import LeaseUnavailable
from app.services.shared_crawl_cache import SharedCrawlCache
from app.services.staples_prewarm import (
    STAPLES_CACHE_MALL_KEY,
    STAPLES_FINAL_FAILURE_REASONS,
    STAPLES_JOB_STATE_FAILED,
    STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING,
    STAPLES_JOB_STATE_PENDING,
    STAPLES_JOB_STATE_RETRY_WAIT,
    STAPLES_JOB_STATE_RUNNING,
    STAPLES_JOB_STATE_SUCCESS,
    STAPLES_OUTCOME_FINAL_FAILURE,
    STAPLES_OUTCOME_SUCCESS,
    STAPLES_OUTCOME_TRANSIENT_FAILURE,
    STAPLES_PREWARM_SOURCE,
    StaplesCrawlOutcome,
    StaplesLocalCrawler,
    StaplesPrewarmStore,
    classify_staples_failure,
    is_staples_url,
    read_stbchina_failure_reasons,
    run_staples_prewarm,
    staples_plan_readonly,
    staples_retry_backoff_seconds,
)
from scripts import staples_prewarm_run as cli

# ---- P0 哨兵:毒化全部招商局模块入口 ---------------------------------------

ZHAOSHANG_MODULES = (
    "app.services.legacy_zhaoshang_adapter",
    "app.services.zhaoshang_remote_task",
    "app.services.zhaoshang_task_progress",
    "app.services.batch_zhaoshang_adapter",
    "app.services.zhaoshang_result_adapter",
    "app.services.zhaoshang_progress_probe",
)

ZHAOSHANG_INVOCATIONS = 0


class _ZhaoshangTripwire:
    """sys.modules 哨兵:任何属性访问都记为一次招商局调用并立即失败。"""

    def __init__(self, module_name: str) -> None:
        object.__setattr__(self, "_module_name", module_name)

    def __getattr__(self, item: str) -> Any:
        global ZHAOSHANG_INVOCATIONS
        ZHAOSHANG_INVOCATIONS += 1
        raise AssertionError(
            f"Staples path invoked Zhaoshang: {self._module_name}.{item}"
        )


@pytest.fixture(autouse=True)
def _poison_zhaoshang(monkeypatch):
    """每个测试期间把全部招商局模块条目替换为哨兵对象(autouse)。"""
    global ZHAOSHANG_INVOCATIONS
    ZHAOSHANG_INVOCATIONS = 0
    for module_name in ZHAOSHANG_MODULES:
        monkeypatch.setitem(sys.modules, module_name, _ZhaoshangTripwire(module_name))
    yield


# ---- 测试基建 ----------------------------------------------------------------

T0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
UID = "1001358701037447"
URL = f"https://www.stbchina.cn/item.html?itemId={UID}"


def _t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _iso(value: datetime) -> str:
    """Store 时间格式:UTC 微秒 ISO(与仓库约定一致)。"""
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class FakeExecutor:
    """实现 (urls, work_dir) 契约的假 run_stbchina_fallback。"""

    def __init__(self, records=None, error=None):
        self.calls: list[tuple[list[str], Path]] = []
        self.records = dict(records or {})
        self.error = error

    def __call__(self, urls, work_dir):
        self.calls.append((list(urls), Path(work_dir)))
        if self.error is not None:
            raise self.error
        return {url: self.records[url] for url in urls if url in self.records}


class FakeFailureReader:
    """实现 failure_reader(xlsx_path) 契约的假失败原因读取器。"""

    def __init__(self, reasons=None):
        self.reasons = dict(reasons or {})
        self.paths: list[Path] = []

    def __call__(self, xlsx_path):
        self.paths.append(Path(xlsx_path))
        return dict(self.reasons)


class LegacyLikeExecutor:
    """复刻 run_stbchina_fallback 的旧档短路契约(monitor_stb_fallback.py:189-192):

    若收到的 work_dir 下已存在 stbchina_missing_result.xlsx 且覆盖全部请求
    URL,则直接返回旧记录、不启动爬取;否则"爬取"并在该目录写下结果档。
    """

    def __init__(self, stale_records=None, fresh_records=None):
        self.calls: list[tuple[list[str], Path]] = []
        self.crawled: list[bool] = []
        self.stale_records = dict(stale_records or {})
        self.fresh_records = dict(fresh_records or {})

    def __call__(self, urls, work_dir):
        urls = list(urls)
        work_dir = Path(work_dir)
        self.calls.append((urls, work_dir))
        result_path = work_dir / "stbchina_missing_result.xlsx"
        if result_path.is_file():
            known = set(self.stale_records) | set(self.fresh_records)
            if all(url in known for url in urls):
                self.crawled.append(False)  # 旧档短路:未爬取
                return {url: self.stale_records[url] for url in urls if url in self.stale_records}
        self.crawled.append(True)
        rows = [
            ["t", "", url, str(self.fresh_records[url]["price"]), "", "成功", "", "", ""]
            for url in urls
            if url in self.fresh_records
        ]
        _write_result_xlsx(result_path, rows=rows)
        return {url: self.fresh_records[url] for url in urls if url in self.fresh_records}


class FakeCrawler:
    """脚本化假爬虫:记录 crawl 次数,按 original_url 返回预置 outcome。"""

    def __init__(self, outcomes=None, error=None):
        self.calls = 0
        self.seen_requests: list[list[dict]] = []
        self.outcomes = dict(outcomes or {})
        self.error = error

    def crawl(self, requests):
        self.calls += 1
        self.seen_requests.append([dict(r) for r in requests])
        if self.error is not None:
            raise self.error
        return {
            str(r["original_url"]): self.outcomes[str(r["original_url"])]
            for r in requests
            if str(r["original_url"]) in self.outcomes
        }


class _EnsureJobFailingStaplesStore:
    """staples_store 代理:ensure_job 命中目标 candidate_id 即抛异常,其余全委托。"""

    def __init__(self, inner, target_candidate_id, exc):
        self._inner = inner
        self._target = int(target_candidate_id)
        self._exc = exc

    def ensure_job(self, candidate_id, now=None):
        if int(candidate_id) == self._target:
            raise self._exc
        return self._inner.ensure_job(candidate_id, now=now)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _RecordResultFailingPrewarmStore:
    """prewarm_store 代理:record_candidate_result 命中目标 candidate_id 即抛异常。"""

    def __init__(self, inner, target_candidate_id, exc):
        self._inner = inner
        self._target = int(target_candidate_id)
        self._exc = exc

    def record_candidate_result(self, candidate_id, **kwargs):
        if int(candidate_id) == self._target:
            raise self._exc
        return self._inner.record_candidate_result(candidate_id, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _transient_outcome(url, diagnostic="页面超时", **overrides):
    fields: dict[str, Any] = dict(
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
    fields.update(overrides)
    return StaplesCrawlOutcome(**fields)


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


def _write_result_xlsx(path, rows):
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(
        ["商品标题", "uid", "商品链接", "商品价格", "评论数", "采集状态", "失败原因", "采集时间", "耗时秒"]
    )
    for row in rows:
        sheet.append(row)
    workbook.save(path)


@pytest.fixture()
def stack(tmp_path):
    db_path = tmp_path / "prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(db_path)
    shared = SharedCrawlCache(tmp_path / "cache.sqlite3")
    staples = StaplesPrewarmStore(db_path)
    return types.SimpleNamespace(
        db_path=db_path,
        cache_path=tmp_path / "cache.sqlite3",
        prewarm=prewarm,
        shared=shared,
        staples=staples,
        work_dir=tmp_path / "work",
    )


def _seed_candidate(prewarm, url=URL, document_id="D1", *, mall=STAPLES_CACHE_MALL_KEY,
                    cache_status=CACHE_STATUS_PENDING_PREWARM):
    upserted = prewarm.upsert_link(document_id, url, mall=mall)
    candidate_id = int(upserted["candidate"]["id"])
    if cache_status is not None:
        prewarm.record_candidate_result(candidate_id, cache_status=cache_status)
    return candidate_id


def _run(stack, crawler, now=T0, **kwargs):
    return run_staples_prewarm(
        prewarm_store=stack.prewarm,
        shared_cache=stack.shared,
        staples_store=stack.staples,
        crawler=crawler,
        work_dir=stack.work_dir,
        now=now,
        **kwargs,
    )


def _default_adapter(stack, executor, failure_reader=None):
    return StaplesLocalCrawler(
        executor=executor, failure_reader=failure_reader, work_dir=stack.work_dir
    )


# ---- 纯函数:域名门 / 身份解析 / 失败分类 / 退避 -------------------------------


def test_is_staples_url_hostname_gate():
    assert is_staples_url("https://www.stbchina.cn/item.html?itemId=1")
    assert is_staples_url("https://stbchina.cn/item.html?itemId=1")
    assert is_staples_url("https://sub.stbchina.cn/item.html?itemId=1")
    assert not is_staples_url("https://stbchina.cn.evil.example/item.html?itemId=1")
    assert not is_staples_url("https://notstbchina.cn/item.html?itemId=1")
    assert not is_staples_url("https://example.com/?redirect=https://stbchina.cn/item.html")
    assert not is_staples_url("https://example.com/stbchinax/item.html")
    assert not is_staples_url("")
    assert not is_staples_url(None)


def test_stbchina_product_identity_parses_item_id():
    assert stbchina_product_identity(URL) == UID
    assert stbchina_product_identity("https://stbchina.cn/item.html/?itemId=7") == "7"
    assert stbchina_product_identity("https://www.stbchina.cn/item.html?itemId=abc123") == "abc123"
    assert stbchina_product_identity("https://www.stbchina.cn/item.html?itemId=%209%20") == "9"
    assert stbchina_product_identity("https://www.stbchina.cn/item.html?itemId=1&itemId=2") == "1"


def test_stbchina_product_identity_conservative_misses():
    assert stbchina_product_identity("https://www.stbchina.cn/other?itemId=1") is None
    assert stbchina_product_identity("https://stbchina.cn.evil.example/item.html?itemId=1") is None
    assert stbchina_product_identity("https://www.stbchina.cn/item.html?itemIdX=1") is None
    assert stbchina_product_identity("https://www.stbchina.cn/item.html") is None
    assert stbchina_product_identity("https://www.stbchina.cn/x?u=/item.html?itemId=1") is None
    assert stbchina_product_identity("https://www.stbchina.cn/page#item.html?itemId=1") is None
    assert stbchina_product_identity("") is None
    assert stbchina_product_identity(None) is None


def test_stbchina_parser_registered_and_ofs_kept():
    assert MALL_IDENTITY_PARSERS["stbchina"] is stbchina_product_identity
    assert "ofs" in MALL_IDENTITY_PARSERS
    assert resolve_mall_product_identity("stbchina", URL) == UID
    assert resolve_mall_product_identity("jd", URL) is None


def test_classify_staples_failure_taxonomy():
    for reason in STAPLES_FINAL_FAILURE_REASONS:
        assert classify_staples_failure(reason) == "FINAL"
    assert classify_staples_failure("商品不存在;附加说明") == "FINAL"
    assert classify_staples_failure("商品已下架；附加说明") == "FINAL"
    for reason in ("页面超时", "页面打不开", "需要登录", "价格需要登录后查看", "触发验证码",
                   "访问受限", "商品价格未识别", "页面结构变化", "其他异常", "", "  "):
        assert classify_staples_failure(reason) == "TRANSIENT", reason
    assert classify_staples_failure(None) == "TRANSIENT"
    assert classify_staples_failure("商品不存在但是误报") == "FINAL"  # startswith 主 token


def test_staples_retry_backoff_bounded():
    assert staples_retry_backoff_seconds(1) == 60
    assert staples_retry_backoff_seconds(2) == 120
    assert staples_retry_backoff_seconds(3) == 300
    assert staples_retry_backoff_seconds(10) == 300


def test_next_transient_failure_state_policy_boundaries(monkeypatch):
    """Blocker A2:全部 transient 路径共用的纯函数策略;N-1/N 边界。"""
    import app.services.staples_prewarm as staples_module

    monkeypatch.setattr(staples_module, "STAPLES_MAX_CONSECUTIVE_TRANSIENT_FAILURES", 4)
    # 预算内:RETRY_WAIT + 有界退避(按本次失败后的次数 1/2/3 → 60/120/300)
    within = staples_module.next_transient_failure_state(0, "页面超时", T0)
    assert within["state"] == STAPLES_JOB_STATE_RETRY_WAIT
    assert within["reason"] == "页面超时"
    assert within["next_retry_at"] == _t(60)
    assert staples_module.next_transient_failure_state(1, "页面超时", _t(60))["next_retry_at"] == _t(180)
    assert staples_module.next_transient_failure_state(2, "页面超时", _t(180))["next_retry_at"] == _t(480)
    # 第 N 次:FAILED,reason 同时含 "retry exhausted" 与原 diagnostic
    exhausted = staples_module.next_transient_failure_state(3, "页面超时", _t(480))
    assert exhausted["state"] == STAPLES_JOB_STATE_FAILED
    assert exhausted["next_retry_at"] is None
    assert exhausted["reason"] == "staples retry exhausted after 4 attempts: 页面超时"
    assert "retry exhausted" in exhausted["reason"] and "页面超时" in exhausted["reason"]


def test_crawler_crash_path_uses_same_policy(stack, monkeypatch):
    """Blocker A2:crawler.crawl 崩溃路径与 outcome 路径走同一 policy(monkeypatch 预算=2)。"""
    import app.services.staples_prewarm as staples_module

    monkeypatch.setattr(staples_module, "STAPLES_MAX_CONSECUTIVE_TRANSIENT_FAILURES", 2)
    cid = _seed_candidate(stack.prewarm)
    crawler = FakeCrawler(error=RuntimeError("kaboom"))
    _run(stack, crawler, now=T0)
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_RETRY_WAIT  # 预算内
    _run(stack, crawler, now=_t(60))
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_FAILED  # 第 2 次:耗尽
    assert "retry exhausted" in job["last_error"]
    assert "kaboom" in job["last_error"]
    # 终态:不再认领、不再爬
    _run(stack, crawler, now=_t(360))
    assert crawler.calls == 2


# ---- store:路由态迁移与 candidate_by_id ----------------------------------------


def test_store_transitions_staples_pending_routing_state(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    cid = _seed_candidate(store, cache_status=CACHE_STATUS_PENDING_LOOKUP)
    store.record_candidate_result(cid, cache_status=CACHE_STATUS_PENDING_PREWARM)
    # PENDING_PREWARM -> STAPLES_PENDING ok
    row = store.record_candidate_result(cid, cache_status=CACHE_STATUS_STAPLES_PENDING)
    assert row["cache_status"] == CACHE_STATUS_STAPLES_PENDING
    # 同状态幂等刷新
    again = store.record_candidate_result(cid, cache_status=CACHE_STATUS_STAPLES_PENDING)
    assert again["cache_status"] == CACHE_STATUS_STAPLES_PENDING
    # STAPLES_PENDING -> CACHE_HIT ok(终态)
    hit = store.record_candidate_result(
        cid, cache_status=CACHE_STATUS_CACHE_HIT, match_level="EXACT", resolved_identity_id=11
    )
    assert hit["cache_status"] == CACHE_STATUS_CACHE_HIT and hit["resolved_identity_id"] == 11
    # CACHE_HIT 是终态,永不回退/改道
    with pytest.raises(ValueError):
        store.record_candidate_result(cid, cache_status=CACHE_STATUS_STAPLES_PENDING)
    # STAPLES_PENDING -> PENDING_PREWARM 不允许
    cid2 = _seed_candidate(store, url=URL.replace(UID, "2"), document_id="D2",
                           cache_status=CACHE_STATUS_PENDING_PREWARM)
    store.record_candidate_result(cid2, cache_status=CACHE_STATUS_STAPLES_PENDING)
    with pytest.raises(ValueError):
        store.record_candidate_result(cid2, cache_status=CACHE_STATUS_PENDING_PREWARM)


def test_candidate_by_id(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    cid = _seed_candidate(store)
    row = store.candidate_by_id(cid)
    assert row is not None and row["original_url"] == URL
    assert store.candidate_by_id(999999) is None


# ---- sidecar 作业状态机 ---------------------------------------------------------


def test_staples_store_shares_prewarm_file_and_ensure_job_idempotent(stack):
    assert "staples_prewarm_jobs" in stack.prewarm.table_names()
    assert "staples_prewarm_attempts" in stack.prewarm.table_names()
    cid = _seed_candidate(stack.prewarm)
    first = stack.staples.ensure_job(cid, now=T0)
    second = stack.staples.ensure_job(cid, now=_t(30))
    assert first["id"] == second["id"]
    assert second["state"] == STAPLES_JOB_STATE_PENDING
    assert second["attempt_count"] == 0
    assert second["created_at"] == first["created_at"] == _iso(T0)
    assert stack.staples.jobs() == [second]
    assert stack.staples.jobs(STAPLES_JOB_STATE_PENDING) == [second]
    assert stack.staples.jobs(STAPLES_JOB_STATE_FAILED) == []
    assert stack.staples.job_for_candidate(999999) is None
    assert stack.staples.stats() == {STAPLES_JOB_STATE_PENDING: 1}


def test_claim_lease_and_stale_token(stack):
    cid = _seed_candidate(stack.prewarm)
    stack.staples.ensure_job(cid, now=T0)
    claimed = stack.staples.claim_ready_jobs(1, now=T0, submitted_url_of=lambda c: URL)
    assert len(claimed) == 1
    job = claimed[0]
    assert job["state"] == STAPLES_JOB_STATE_RUNNING
    assert job["attempt_count"] == 1 and job["claim_token"]
    assert job["lease_expires_at"] == _iso(_t(1500))
    assert job["next_retry_at"] is None
    with stack.staples._reader() as connection:
        attempt = dict(connection.execute(
            "SELECT * FROM staples_prewarm_attempts WHERE job_id=?", (job["id"],)
        ).fetchone())
    assert attempt["attempt_no"] == 1 and attempt["submitted_url"] == URL
    assert attempt["started_at"] == _iso(T0) and attempt["finished_at"] is None
    # 陈旧令牌:错 token 完成必须失败(fail-closed)
    with pytest.raises(ValueError, match="stale claim on staples job"):
        stack.staples.complete_job(job["id"], "wrong-token", state=STAPLES_JOB_STATE_SUCCESS)
    done = stack.staples.complete_job(
        job["id"], job["claim_token"], state=STAPLES_JOB_STATE_SUCCESS,
        attempt_outcome="SUCCESS", now=_t(5),
    )
    assert done["state"] == STAPLES_JOB_STATE_SUCCESS
    assert done["claim_token"] is None and done["lease_expires_at"] is None
    assert done["consecutive_failures"] == 0
    with stack.staples._reader() as connection:
        attempt = dict(connection.execute(
            "SELECT * FROM staples_prewarm_attempts WHERE job_id=?", (job["id"],)
        ).fetchone())
    assert attempt["finished_at"] == _iso(_t(5)) and attempt["outcome"] == "SUCCESS"


def test_claim_reclaims_expired_lease(stack):
    cid = _seed_candidate(stack.prewarm)
    stack.staples.ensure_job(cid, now=T0)
    first = stack.staples.claim_ready_jobs(1, now=T0)[0]
    # 租约未过期:第二个 worker 拿不到
    assert stack.staples.claim_ready_jobs(1, now=_t(1499)) == []
    # 租约过期:可被回收(attempt 递增、新 token)
    reclaimed = stack.staples.claim_ready_jobs(1, now=_t(1500))
    assert len(reclaimed) == 1
    second = reclaimed[0]
    assert second["id"] == first["id"]
    assert second["attempt_count"] == 2 and second["claim_token"] != first["claim_token"]
    with pytest.raises(ValueError, match="stale claim on staples job"):
        stack.staples.complete_job(
            first["id"], first["claim_token"], state=STAPLES_JOB_STATE_SUCCESS
        )


def test_complete_without_claim_requires_unclaimed_job(stack):
    cid = _seed_candidate(stack.prewarm)
    job = stack.staples.ensure_job(cid, now=T0)
    # 未认领作业可用 claim_token=None 直接完成(缓存吸收路径专用)
    done = stack.staples.complete_job(
        job["id"], None, state=STAPLES_JOB_STATE_SUCCESS, reason="cache-hit-absorbed"
    )
    assert done["state"] == STAPLES_JOB_STATE_SUCCESS and done["last_error"] == "cache-hit-absorbed"
    # 已认领作业不接受 claim_token=None
    cid2 = _seed_candidate(stack.prewarm, url=URL.replace(UID, "3"), document_id="D3")
    job2 = stack.staples.ensure_job(cid2, now=T0)
    claimed = stack.staples.claim_ready_jobs(1, now=_t(1))[0]
    assert claimed["id"] == job2["id"]
    with pytest.raises(ValueError, match="stale claim on staples job"):
        stack.staples.complete_job(job2["id"], None, state=STAPLES_JOB_STATE_SUCCESS)
    # 非法状态值
    with pytest.raises(ValueError, match="state must be one of"):
        stack.staples.complete_job(job["id"], None, state="WHATEVER")


def test_request_staples_retry_only_failed_local(stack):
    cid = _seed_candidate(stack.prewarm)
    stack.staples.ensure_job(cid, now=T0)
    with pytest.raises(ValueError, match="no staples job"):
        stack.staples.request_staples_retry(999999, now=T0)
    claimed = stack.staples.claim_ready_jobs(1, now=T0)[0]
    job = stack.staples.complete_job(
        claimed["id"], claimed["claim_token"], state=STAPLES_JOB_STATE_RETRY_WAIT,
        reason="页面超时", next_retry_at=_t(60), attempt_outcome="TRANSIENT_FAILURE",
    )
    with pytest.raises(ValueError, match="only FAILED jobs"):
        stack.staples.request_staples_retry(cid, now=_t(1))
    failed = stack.staples.complete_job(
        job["id"], None, state=STAPLES_JOB_STATE_FAILED, reason="商品不存在",
        attempt_outcome="FINAL_FAILURE",
    )
    assert failed["consecutive_failures"] == 1  # RETRY_WAIT 计数保留
    retried = stack.staples.request_staples_retry(cid, now=_t(10))
    assert retried["state"] == STAPLES_JOB_STATE_PENDING
    assert retried["next_retry_at"] is None
    assert retried["manual_retry_requested_at"] == _iso(_t(10))
    assert retried["attempt_count"] == 1  # 计数器/历史保留
    assert retried["consecutive_failures"] == 0  # 手动重试清零连续失败预算
    assert retried["last_error"] == "商品不存在"


def test_manual_retry_resets_consecutive_failure_budget(stack):
    """Blocker A3:耗尽 FAILED 后手动重试清零 consecutive_failures,保留 attempt_count。"""
    cid = _seed_candidate(stack.prewarm)
    crawler = FakeCrawler(outcomes={URL: _transient_outcome(URL, "页面超时")})
    for now in (T0, _t(60), _t(180), _t(480)):  # 耗尽至 FAILED
        _run(stack, crawler, now=now)
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_FAILED
    assert job["attempt_count"] == 4
    assert job["consecutive_failures"] == 3
    # 手动重试:PENDING + 预算清零 + attempt 累计保留
    retried = stack.staples.request_staples_retry(cid, now=_t(600))
    assert retried["state"] == STAPLES_JOB_STATE_PENDING
    assert retried["consecutive_failures"] == 0
    assert retried["attempt_count"] == 4
    # 再次瞬时失败:RETRY_WAIT 而非立刻 FAILED(预算已重置)
    _run(stack, crawler, now=_t(660))
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_RETRY_WAIT
    assert job["consecutive_failures"] == 1


# ---- 编排:Case 1-19 -------------------------------------------------------------


def test_case01_non_staples_candidates_never_routed(stack):
    for mall, document_id, url in (
        ("jd", "D1", "https://www.jd.com/product/1.html"),
        ("qx", "D2", "https://www.qx.com/p/2.html"),
        ("officemate", "D3", "https://www.officemate.cn/p/3.html"),
    ):
        _seed_candidate(stack.prewarm, url=url, document_id=document_id, mall=mall)
    crawler = FakeCrawler()
    summary = _run(stack, crawler)
    assert crawler.calls == 0
    assert summary["routed"] == 0
    assert stack.staples.jobs() == []
    assert all(
        row["cache_status"] == CACHE_STATUS_PENDING_PREWARM
        for row in stack.prewarm.candidates()
    )


def test_route_limit_consumed_only_by_eligible_staples_candidates(stack):
    """Blocker D1(饿死):route_limit 只能被 eligible Staples 候选消耗。

    两个非 Staples 候选(jd/comix)按 id 序排在 Staples 候选之前且永远保持
    PENDING_PREWARM;route_limit=1 时它们绝不能吃掉路由预算,否则 Staples
    候选在本 pass 以及之后每个 pass 都排不进预算,永久饿死(Issue #11)。
    """
    jd_cid = _seed_candidate(
        stack.prewarm, "https://www.jd.com/product/1.html", "DJ1", mall="jd"
    )
    comix_cid = _seed_candidate(
        stack.prewarm, "https://www.comix.cn/p/2.html", "DC1", mall="comix"
    )
    staples_cid = _seed_candidate(stack.prewarm)
    crawler = FakeCrawler()
    summary = _run(stack, crawler, route_limit=1)
    # Staples 候选被路由(翻出 PENDING_PREWARM 并建立 sidecar 作业)
    assert summary["routed"] == 1
    assert stack.prewarm.candidate_by_id(staples_cid)[
        "cache_status"
    ] == CACHE_STATUS_STAPLES_PENDING
    assert stack.staples.job_for_candidate(staples_cid) is not None
    # 路由的候选确实进入本 pass 本地爬取
    assert crawler.calls == 1
    assert crawler.seen_requests == [[{"candidate_id": staples_cid, "original_url": URL}]]
    assert summary["errors"] == []
    # 非 Staples 候选行为不变:零写入、无作业
    for cid in (jd_cid, comix_cid):
        assert stack.prewarm.candidate_by_id(cid)[
            "cache_status"
        ] == CACHE_STATUS_PENDING_PREWARM
        assert stack.staples.job_for_candidate(cid) is None


def test_route_limit_one_routes_first_staples_only(stack):
    """防修过头:route_limit=1 时本 pass 只路由第一个 Staples 候选。

    候选 id 序 = 非Staples A / Staples A / 非Staples B / Staples B:
    预算只被 Staples A 消耗;Staples B 等下一 pass;非 Staples 行逐字段
    不变(过滤发生在任何写之前,含 last_checked_at)。
    """
    jd_cid = _seed_candidate(
        stack.prewarm, "https://www.jd.com/product/1.html", "DJ1", mall="jd"
    )
    url_a = "https://www.stbchina.cn/item.html?itemId=201"
    url_b = "https://www.stbchina.cn/item.html?itemId=202"
    staples_a_cid = _seed_candidate(stack.prewarm, url_a, "DA")
    comix_cid = _seed_candidate(
        stack.prewarm, "https://www.comix.cn/p/2.html", "DC1", mall="comix"
    )
    staples_b_cid = _seed_candidate(stack.prewarm, url_b, "DB")
    non_staples_before = {
        cid: stack.prewarm.candidate_by_id(cid) for cid in (jd_cid, comix_cid)
    }
    crawler = FakeCrawler(
        outcomes={
            url_a: _success_outcome(url_a, real_sku_id="201", crawler_uid_raw="201"),
            url_b: _success_outcome(url_b, real_sku_id="202", crawler_uid_raw="202"),
        }
    )
    summary = _run(stack, crawler, route_limit=1)
    assert summary["routed"] == 1
    # Staples A:已路由并本地爬取成功
    assert stack.prewarm.candidate_by_id(staples_a_cid)[
        "cache_status"
    ] == CACHE_STATUS_CACHE_HIT
    assert stack.staples.job_for_candidate(staples_a_cid)[
        "state"
    ] == STAPLES_JOB_STATE_SUCCESS
    # Staples B:仍 PENDING_PREWARM、无作业(等下一 pass)
    assert stack.prewarm.candidate_by_id(staples_b_cid)[
        "cache_status"
    ] == CACHE_STATUS_PENDING_PREWARM
    assert stack.staples.job_for_candidate(staples_b_cid) is None
    # 非 Staples 行逐字段不变
    for cid in (jd_cid, comix_cid):
        assert stack.prewarm.candidate_by_id(cid) == non_staples_before[cid]
    # 只爬了 Staples A
    assert crawler.calls == 1
    assert crawler.seen_requests == [
        [{"candidate_id": staples_a_cid, "original_url": url_a}]
    ]


def test_second_pass_routes_remaining_staples_after_limit(stack):
    """前向进展:route_limit 是吞吐界不是饿死界——Pass1 路由 A,Pass2 路由 B。"""
    _seed_candidate(stack.prewarm, "https://www.jd.com/product/1.html", "DJ1", mall="jd")
    url_a = "https://www.stbchina.cn/item.html?itemId=201"
    url_b = "https://www.stbchina.cn/item.html?itemId=202"
    staples_a_cid = _seed_candidate(stack.prewarm, url_a, "DA")
    _seed_candidate(stack.prewarm, "https://www.comix.cn/p/2.html", "DC1", mall="comix")
    staples_b_cid = _seed_candidate(stack.prewarm, url_b, "DB")
    crawler = FakeCrawler(
        outcomes={
            url_a: _success_outcome(url_a, real_sku_id="201", crawler_uid_raw="201"),
            url_b: _success_outcome(url_b, real_sku_id="202", crawler_uid_raw="202"),
        }
    )
    summary1 = _run(stack, crawler, route_limit=1)
    assert summary1["routed"] == 1
    assert stack.prewarm.candidate_by_id(staples_b_cid)[
        "cache_status"
    ] == CACHE_STATUS_PENDING_PREWARM
    # 第二个 pass(now 递增):Staples B 也被路由并爬取成功
    summary2 = _run(stack, crawler, now=_t(60), route_limit=1)
    assert summary2["routed"] == 1
    assert stack.prewarm.candidate_by_id(staples_b_cid)[
        "cache_status"
    ] == CACHE_STATUS_CACHE_HIT
    assert stack.staples.job_for_candidate(staples_b_cid)[
        "state"
    ] == STAPLES_JOB_STATE_SUCCESS
    assert crawler.calls == 2
    assert [req["original_url"] for req in crawler.seen_requests[0]] == [url_a]
    assert [req["original_url"] for req in crawler.seen_requests[1]] == [url_b]


def test_route_budget_consumed_when_ensure_job_fails_after_status_flip(stack):
    """Case D:ensure_job 在状态翻转后失败,预算已被 A 消耗,本 pass 绝不处理 B。"""
    url_a = "https://www.stbchina.cn/item.html?itemId=301"
    url_b = "https://www.stbchina.cn/item.html?itemId=302"
    a_cid = _seed_candidate(stack.prewarm, url_a, "DA301")
    b_cid = _seed_candidate(stack.prewarm, url_b, "DB302")
    stack.staples = _EnsureJobFailingStaplesStore(
        stack.staples, a_cid, RuntimeError("ensure_job boom")
    )
    crawler = FakeCrawler()
    summary = _run(stack, crawler, route_limit=1)
    # A:状态已翻转(record_candidate_result 已生效,业务变更已发生)但无作业
    assert stack.prewarm.candidate_by_id(a_cid)[
        "cache_status"
    ] == CACHE_STATUS_STAPLES_PENDING
    assert stack.staples.job_for_candidate(a_cid) is None
    # B:完全未动——A 失败也消耗预算,route_limit=1 不允许越界路由 B
    assert stack.prewarm.candidate_by_id(b_cid)[
        "cache_status"
    ] == CACHE_STATUS_PENDING_PREWARM
    assert stack.staples.job_for_candidate(b_cid) is None
    assert summary["routed"] == 0
    assert any(
        e.startswith("route candidate") and str(a_cid) in e
        for e in summary["errors"]
    )
    assert crawler.calls == 0


def test_route_budget_consumed_when_record_candidate_result_fails(stack):
    """Case E:record_candidate_result 自身失败同样消耗预算,B 本 pass 不被处理。"""
    url_a = "https://www.stbchina.cn/item.html?itemId=303"
    url_b = "https://www.stbchina.cn/item.html?itemId=304"
    a_cid = _seed_candidate(stack.prewarm, url_a, "DA303")
    b_cid = _seed_candidate(stack.prewarm, url_b, "DB304")
    stack.prewarm = _RecordResultFailingPrewarmStore(
        stack.prewarm, a_cid, RuntimeError("record boom")
    )
    crawler = FakeCrawler()
    summary = _run(stack, crawler, route_limit=1)
    # A:record 失败,状态未翻转、无作业
    assert stack.prewarm.candidate_by_id(a_cid)[
        "cache_status"
    ] == CACHE_STATUS_PENDING_PREWARM
    assert stack.staples.job_for_candidate(a_cid) is None
    # B:完全未动
    assert stack.prewarm.candidate_by_id(b_cid)[
        "cache_status"
    ] == CACHE_STATUS_PENDING_PREWARM
    assert stack.staples.job_for_candidate(b_cid) is None
    assert summary["routed"] == 0
    assert any(
        e.startswith("route candidate") and str(a_cid) in e
        for e in summary["errors"]
    )
    assert crawler.calls == 0


def test_case02_preseeded_cache_hit_absorbs_without_crawl(stack):
    cid = _seed_candidate(stack.prewarm)
    stack.shared.record_success(STAPLES_CACHE_MALL_KEY, URL, source="seed", price=3.5)
    crawler = FakeCrawler()
    summary = _run(stack, crawler)
    assert crawler.calls == 0
    assert summary["routed"] == 1 and summary["recheck_hits"] == 1
    candidate = stack.prewarm.candidate_by_id(cid)
    assert candidate["cache_status"] == CACHE_STATUS_CACHE_HIT
    assert candidate["match_level"] == "EXACT"
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_SUCCESS
    assert job["last_error"] == "cache-hit-absorbed"
    references = stack.shared.document_references("D1")
    assert len(references) == 1
    assert references[0]["mall"] == STAPLES_CACHE_MALL_KEY


def test_case03_crawl_success_records_cache_hit(stack):
    cid = _seed_candidate(stack.prewarm)
    crawler = FakeCrawler(outcomes={URL: _success_outcome(URL)})
    summary = _run(stack, crawler)
    assert crawler.calls == 1
    assert summary["jobs_claimed"] == 1
    assert summary["crawl_success"] == 1 and summary["cache_hit_after_crawl"] == 1
    hit = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, URL)
    assert hit is not None
    entry = hit["entry"]
    assert entry["source"] == STAPLES_PREWARM_SOURCE
    assert entry["sku_status"] == SKU_STATUS_URL_PARSED_UNVERIFIED
    assert entry["real_sku_id"] == UID
    candidate = stack.prewarm.candidate_by_id(cid)
    assert candidate["cache_status"] == CACHE_STATUS_CACHE_HIT
    assert candidate["match_level"] == hit["match_level"]
    assert candidate["resolved_identity_id"] == hit["identity_id"]
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_SUCCESS
    assert job["cache_result_id"] == entry["id"]
    assert ZHAOSHANG_INVOCATIONS == 0


def test_case04_two_documents_share_one_candidate_one_crawl(stack):
    _seed_candidate(stack.prewarm, document_id="D1")
    stack.prewarm.upsert_link("D2", URL, mall=STAPLES_CACHE_MALL_KEY)
    crawler = FakeCrawler(outcomes={URL: _success_outcome(URL)})
    summary = _run(stack, crawler)
    assert crawler.calls == 1
    assert summary["crawl_success"] == 1
    staples_candidates = [
        row for row in stack.prewarm.candidates() if row["mall"] == STAPLES_CACHE_MALL_KEY
    ]
    assert len(staples_candidates) == 1
    assert len(stack.staples.jobs()) == 1
    assert len(stack.shared.document_references("D1")) == 1
    assert len(stack.shared.document_references("D2")) == 1


def test_case05_second_worker_cannot_double_claim(stack):
    cid = _seed_candidate(stack.prewarm)
    store_a = StaplesPrewarmStore(stack.db_path)
    store_b = StaplesPrewarmStore(stack.db_path)
    store_a.ensure_job(cid, now=T0)
    claimed = store_a.claim_ready_jobs(1, now=T0)
    assert len(claimed) == 1
    job_a = claimed[0]
    assert store_b.claim_ready_jobs(1, now=T0) == []
    # worker A 完成全部职责:爬取 + 写缓存 + 翻转候选 + 完成作业
    crawler = FakeCrawler(outcomes={URL: _success_outcome(URL)})
    assert crawler.crawl([{"candidate_id": cid, "original_url": URL}])
    stack.shared.record_success(
        STAPLES_CACHE_MALL_KEY, URL, source=STAPLES_PREWARM_SOURCE,
        price=12.5, title="史泰博商品", real_sku_id=UID,
        sku_status=SKU_STATUS_URL_PARSED_UNVERIFIED,
    )
    hit = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, URL)
    stack.prewarm.record_candidate_result(
        cid, cache_status=CACHE_STATUS_CACHE_HIT,
        match_level=hit["match_level"], resolved_identity_id=hit["identity_id"],
    )
    store_a.complete_job(
        job_a["id"], job_a["claim_token"], state=STAPLES_JOB_STATE_SUCCESS,
        cache_result_id=int(hit["entry"]["id"]), attempt_outcome="SUCCESS", now=_t(10),
    )
    summary = _run(stack, crawler, now=_t(20))
    assert summary["jobs_claimed"] == 0
    assert stack.prewarm.candidate_by_id(cid)["cache_status"] == CACHE_STATUS_CACHE_HIT


def test_case06_transient_failure_backoff_gates_next_claim(stack):
    cid = _seed_candidate(stack.prewarm)
    executor = FakeExecutor(error=RuntimeError("史泰博专用脚本失败"))
    crawler = _default_adapter(stack, executor, FakeFailureReader())
    summary = _run(stack, crawler, now=T0)
    assert executor.calls and summary["transient_failures"] == 1
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_RETRY_WAIT
    assert job["consecutive_failures"] == 1
    assert job["next_retry_at"] == _iso(_t(60))
    # 退避窗口内:不认领、不再爬
    summary_wait = _run(stack, crawler, now=_t(30))
    assert summary_wait["jobs_claimed"] == 0
    assert len(executor.calls) == 1
    # 窗口过后:再次认领并爬取
    summary_retry = _run(stack, crawler, now=_t(60))
    assert summary_retry["jobs_claimed"] == 1
    assert len(executor.calls) == 2
    assert stack.staples.job_for_candidate(cid)["consecutive_failures"] == 2


def test_case07_transient_failures_bounded_then_failed(stack):
    cid = _seed_candidate(stack.prewarm)
    executor = FakeExecutor(error=RuntimeError("页面超时"))
    crawler = _default_adapter(stack, executor, FakeFailureReader())
    nows = [T0, _t(60), _t(180), _t(480)]
    next_retries = []
    states = []
    for now in nows:
        _run(stack, crawler, now=now)
        job = stack.staples.job_for_candidate(cid)
        states.append(job["state"])
        if job["state"] == STAPLES_JOB_STATE_RETRY_WAIT:
            next_retries.append(datetime.fromisoformat(job["next_retry_at"]))
    # 前 3 次 RETRY_WAIT(1m/2m/5m),第 4 次达到预算转 FAILED
    assert states == [
        STAPLES_JOB_STATE_RETRY_WAIT,
        STAPLES_JOB_STATE_RETRY_WAIT,
        STAPLES_JOB_STATE_RETRY_WAIT,
        STAPLES_JOB_STATE_FAILED,
    ]
    deltas = [
        int((later - earlier).total_seconds())
        for earlier, later in zip(next_retries, next_retries[1:])
    ]
    assert deltas == [120, 300]
    assert (next_retries[0] - T0).total_seconds() == 60
    job = stack.staples.job_for_candidate(cid)
    assert job["consecutive_failures"] == 3  # FAILED 保持 consecutive
    assert "retry exhausted" in job["last_error"]
    assert "页面超时" in job["last_error"]
    # 终态作业永不重爬
    _run(stack, crawler, now=_t(780))
    assert len(executor.calls) == 4


def test_transient_failures_exhaust_to_failed_after_max_attempts(stack):
    """Blocker A:瞬时失败必须有 attempt 预算,第 N 次转 FAILED,不再无限 RETRY_WAIT。"""
    cid = _seed_candidate(stack.prewarm)
    crawler = FakeCrawler(outcomes={URL: _transient_outcome(URL, "页面超时")})
    # 前 3 次:RETRY_WAIT(有界退避)
    for now in (T0, _t(60), _t(180)):
        summary = _run(stack, crawler, now=now)
        assert summary["transient_failures"] == 1
        job = stack.staples.job_for_candidate(cid)
        assert job["state"] == STAPLES_JOB_STATE_RETRY_WAIT
    # 第 4 次:FAILED,reason 同时含 retry exhausted 与原 diagnostic
    summary4 = _run(stack, crawler, now=_t(480))
    assert crawler.calls == 4
    assert summary4["final_failures"] == 1
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_FAILED
    assert "retry exhausted" in job["last_error"]
    assert "页面超时" in job["last_error"]
    # 第 5 个 pass:FAILED 不再认领、不再爬
    summary5 = _run(stack, crawler, now=_t(780))
    assert summary5["jobs_claimed"] == 0
    assert crawler.calls == 4


def test_case08_final_failure_is_terminal(stack):
    cid = _seed_candidate(stack.prewarm)
    executor = FakeExecutor(records={})
    crawler = _default_adapter(stack, executor, FakeFailureReader({URL: "商品不存在"}))
    summary = _run(stack, crawler, now=T0)
    assert summary["final_failures"] == 1
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_FAILED
    assert job["last_error"] == "商品不存在"
    assert summary["errors"] == []
    # 终态作业永不重爬
    _run(stack, crawler, now=_t(3600))
    assert len(executor.calls) == 1


def test_case09_manual_retry_stays_local(stack):
    cid = _seed_candidate(stack.prewarm)
    executor = FakeExecutor(records={})
    crawler = _default_adapter(stack, executor, FakeFailureReader({URL: "商品已下架"}))
    _run(stack, crawler, now=T0)
    assert stack.staples.job_for_candidate(cid)["state"] == STAPLES_JOB_STATE_FAILED
    retried = stack.staples.request_staples_retry(cid, now=_t(10))
    assert retried["state"] == STAPLES_JOB_STATE_PENDING
    crawler_success = FakeCrawler(outcomes={URL: _success_outcome(URL)})
    summary = _run(stack, crawler_success, now=_t(20))
    assert len(executor.calls) + crawler_success.calls == 2
    assert summary["cache_hit_after_crawl"] == 1
    assert stack.prewarm.candidate_by_id(cid)["cache_status"] == CACHE_STATUS_CACHE_HIT
    assert ZHAOSHANG_INVOCATIONS == 0


def test_case10_crawler_unavailable_is_transient(stack):
    cid = _seed_candidate(stack.prewarm)
    executor = FakeExecutor(error=RuntimeError("史泰博专用脚本不存在"))
    crawler = _default_adapter(stack, executor, FakeFailureReader())
    summary = _run(stack, crawler, now=T0)
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_RETRY_WAIT
    assert "史泰博专用脚本不存在" in job["last_error"]
    assert summary["final_failures"] == 0
    assert summary["transient_failures"] == 1


def test_case11_cache_entry_shape_matches_generic_contract(stack):
    _seed_candidate(stack.prewarm)
    crawler = FakeCrawler(outcomes={URL: _success_outcome(URL)})
    _run(stack, crawler)
    staples_entry = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, URL)["entry"]
    assert staples_entry["source"] == STAPLES_PREWARM_SOURCE
    # STEP3 消费形态:与非史泰博写入的 entry 字段集完全一致
    reference_url = "https://www.ofs.cn/product-6287660.html"
    stack.shared.record_success("ofs", reference_url, source="daily-sync", price=2.0)
    reference_entry = stack.shared.lookup_current("ofs", reference_url)["entry"]
    assert set(staples_entry.keys()) == set(reference_entry.keys())


def test_case12_url_chain_evidence_preserved(stack):
    original = "https://stbchina.cn/item.html?itemId=7"
    submitted = "https://www.stbchina.cn/item.html?itemId=7"
    returned = "https://www.stbchina.cn/item.html?itemId=7&from=rec"
    cid = _seed_candidate(stack.prewarm, url=original)
    outcome = _success_outcome(
        original, submitted_url=submitted, returned_url=returned, real_sku_id="7",
        crawler_uid_raw="7",
    )
    crawler = FakeCrawler(outcomes={original: outcome})
    _run(stack, crawler)
    entry = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, original)["entry"]
    assert entry["original_url"] == original
    assert entry["submitted_url"] == submitted
    assert entry["returned_url"] == returned
    assert len({original, submitted, returned}) == 3
    assert stack.prewarm.candidate_by_id(cid)["original_url"] == original


def test_case13_cache_write_transient_defers_to_local_cache_pending(stack, monkeypatch):
    cid = _seed_candidate(stack.prewarm)
    original_record = stack.shared.record_success
    write_calls = {"n": 0}

    def flaky_record_success(mall, submitted_url, **kwargs):
        write_calls["n"] += 1
        if write_calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_record(mall, submitted_url, **kwargs)

    monkeypatch.setattr(stack.shared, "record_success", flaky_record_success)
    crawler = FakeCrawler(outcomes={URL: _success_outcome(URL)})
    summary = _run(stack, crawler, now=T0)
    assert crawler.calls == 1
    assert summary["crawl_success"] == 1 and summary["cache_hit_after_crawl"] == 0
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING
    assert "cache write transient" in job["last_error"]
    assert stack.prewarm.candidate_by_id(cid)["cache_status"] == CACHE_STATUS_STAPLES_PENDING
    # 第二遍:不重新爬取,补同步成功并翻转候选
    summary_sync = _run(stack, crawler, now=_t(600))
    assert crawler.calls == 1
    assert summary_sync["cache_sync_succeeded"] == 1
    assert stack.prewarm.candidate_by_id(cid)["cache_status"] == CACHE_STATUS_CACHE_HIT
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_SUCCESS
    hit = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, URL)
    assert hit is not None and job["cache_result_id"] == hit["entry"]["id"]


def _make_pending_job_via_failed_cache_write(stack, crawler, monkeypatch):
    """Pass1:crawl 成功但缓存写瞬断 → 作业进入 LOCAL_SUCCESS_CACHE_PENDING。"""
    original_record = stack.shared.record_success
    write_calls = {"n": 0}

    def flaky_record_success(mall, submitted_url, **kwargs):
        write_calls["n"] += 1
        if write_calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_record(mall, submitted_url, **kwargs)

    monkeypatch.setattr(stack.shared, "record_success", flaky_record_success)
    summary = _run(stack, crawler, now=T0)
    assert summary["crawl_success"] == 1 and summary["cache_hit_after_crawl"] == 0
    return original_record


def test_cache_sync_lookup_error_keeps_pending(stack, monkeypatch):
    """Blocker B1:补同步写成功但 lookup_current 抛异常 → 保持待同步,绝不假 SUCCESS。"""
    cid = _seed_candidate(stack.prewarm)
    crawler = FakeCrawler(outcomes={URL: _success_outcome(URL)})
    _make_pending_job_via_failed_cache_write(stack, crawler, monkeypatch)
    assert stack.staples.job_for_candidate(cid)[
        "state"
    ] == STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING
    # Pass2:写成功,但 lookup_current 瞬断(arm 开关保证只影响第二个 pass)
    original_lookup = stack.shared.lookup_current
    state = {"armed": False}

    def flaky_lookup(mall, url):
        if state["armed"]:
            raise sqlite3.OperationalError("cache lookup blew up")
        return original_lookup(mall, url)

    monkeypatch.setattr(stack.shared, "lookup_current", flaky_lookup)
    state["armed"] = True
    summary2 = _run(stack, crawler, now=_t(600))
    # 不重新爬取,不假报 SUCCESS
    assert crawler.calls == 1
    assert summary2["cache_sync_succeeded"] == 0
    assert summary2["cache_sync_pending"] == 1
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING
    assert stack.prewarm.candidate_by_id(cid)["cache_status"] == CACHE_STATUS_STAPLES_PENDING


def test_cache_sync_lookup_miss_keeps_pending(stack, monkeypatch):
    """Blocker B2:补同步写成功但 lookup MISS → 保持待同步并留下诊断,绝不假 SUCCESS。"""
    cid = _seed_candidate(stack.prewarm)
    crawler = FakeCrawler(outcomes={URL: _success_outcome(URL)})
    _make_pending_job_via_failed_cache_write(stack, crawler, monkeypatch)
    # Pass2:写成功,但 lookup 返回 None(arm 开关只影响第二个 pass)
    original_lookup = stack.shared.lookup_current
    state = {"armed": False}

    def miss_lookup(mall, url):
        if state["armed"]:
            return None
        return original_lookup(mall, url)

    monkeypatch.setattr(stack.shared, "lookup_current", miss_lookup)
    state["armed"] = True
    summary2 = _run(stack, crawler, now=_t(600))
    assert crawler.calls == 1
    assert summary2["cache_sync_succeeded"] == 0
    assert summary2["cache_sync_pending"] == 1
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING
    assert "cache lookup missed after write" in job["last_error"]
    assert stack.prewarm.candidate_by_id(cid)["cache_status"] == CACHE_STATUS_STAPLES_PENDING


def test_cache_sync_pending_recovers_on_third_pass(stack, monkeypatch):
    """Blocker B3 三 Pass 恢复链:写瞬断→lookup 异常→真实 hit,全程只爬一次。"""
    cid = _seed_candidate(stack.prewarm)
    crawler = FakeCrawler(outcomes={URL: _success_outcome(URL)})
    # Pass1:crawl 成功但 record_success 抛 OperationalError → 待同步
    _make_pending_job_via_failed_cache_write(stack, crawler, monkeypatch)
    assert crawler.calls == 1
    assert stack.staples.job_for_candidate(cid)[
        "state"
    ] == STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING
    # Pass2:写成功但 lookup 抛异常 → 仍待同步,crawler 不再被调用
    original_lookup = stack.shared.lookup_current
    state = {"armed": False}

    def flaky_lookup(mall, url):
        if state["armed"]:
            raise sqlite3.OperationalError("cache lookup blew up")
        return original_lookup(mall, url)

    monkeypatch.setattr(stack.shared, "lookup_current", flaky_lookup)
    state["armed"] = True
    summary2 = _run(stack, crawler, now=_t(600))
    assert crawler.calls == 1
    assert summary2["cache_sync_succeeded"] == 0 and summary2["cache_sync_pending"] == 1
    assert stack.staples.job_for_candidate(cid)[
        "state"
    ] == STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING
    # Pass3:lookup 真实 hit → 候选 CACHE_HIT、作业 SUCCESS、cache_result_id 非空
    monkeypatch.setattr(stack.shared, "lookup_current", original_lookup)
    summary3 = _run(stack, crawler, now=_t(1200))
    assert crawler.calls == 1  # 全程只爬一次
    candidate = stack.prewarm.candidate_by_id(cid)
    assert candidate["cache_status"] == CACHE_STATUS_CACHE_HIT
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_SUCCESS
    hit = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, URL)
    assert hit is not None
    assert job["cache_result_id"] == int(hit["entry"]["id"])
    assert summary3["recheck_hits"] + summary3["cache_sync_succeeded"] == 1


def test_case14_confirmed_sku_stored_verbatim(stack):
    _seed_candidate(stack.prewarm)
    outcome = _success_outcome(
        URL, real_sku_id="888777666", sku_status=SKU_STATUS_CONFIRMED, crawler_uid_raw=""
    )
    crawler = FakeCrawler(outcomes={URL: outcome})
    _run(stack, crawler)
    entry = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, URL)["entry"]
    assert entry["sku_status"] == SKU_STATUS_CONFIRMED
    assert entry["real_sku_id"] == "888777666"


def test_case15_upstream_provided_sku_stored_verbatim(stack):
    _seed_candidate(stack.prewarm)
    outcome = _success_outcome(
        URL, real_sku_id="sku-xyz-1", sku_status=SKU_STATUS_UPSTREAM_PROVIDED, crawler_uid_raw=""
    )
    crawler = FakeCrawler(outcomes={URL: outcome})
    _run(stack, crawler)
    entry = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, URL)["entry"]
    assert entry["sku_status"] == SKU_STATUS_UPSTREAM_PROVIDED
    assert entry["real_sku_id"] == "sku-xyz-1"


def test_case16_default_adapter_uid_becomes_url_parsed_unverified(stack):
    cid = _seed_candidate(stack.prewarm)
    executor = FakeExecutor(
        records={URL: {"price": 12.5, "title": "史泰博商品", "uid": UID, "comment": "123"}}
    )
    crawler = _default_adapter(stack, executor, FakeFailureReader())
    _run(stack, crawler)
    entry = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, URL)["entry"]
    assert entry["sku_status"] == SKU_STATUS_URL_PARSED_UNVERIFIED
    assert entry["real_sku_id"] == UID
    assert stack.prewarm.candidate_by_id(cid)["cache_status"] == CACHE_STATUS_CACHE_HIT


def test_case17_empty_uid_stays_unresolved(stack):
    _seed_candidate(stack.prewarm)
    executor = FakeExecutor(records={URL: {"price": 9.9, "title": "t", "uid": "", "comment": ""}})
    crawler = _default_adapter(stack, executor, FakeFailureReader())
    _run(stack, crawler)
    entry = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, URL)["entry"]
    assert entry["sku_status"] == SKU_STATUS_UNRESOLVED
    assert entry["real_sku_id"] is None


def test_case18_placeholder_uid_never_stored_but_kept_in_payload(stack):
    _seed_candidate(stack.prewarm)
    executor = FakeExecutor(
        records={URL: {"price": 9.9, "title": "t", "uid": "zhpq-abc123", "comment": "5"}}
    )
    crawler = _default_adapter(stack, executor, FakeFailureReader())
    _run(stack, crawler)
    entry = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, URL)["entry"]
    assert entry["sku_status"] == SKU_STATUS_UNRESOLVED
    assert entry["real_sku_id"] is None
    assert entry["payload"]["crawler_uid_raw"] == "zhpq-abc123"
    assert entry["payload"]["url_evidence"]["original"] == URL


def test_case19_step1_files_stay_staples_free():
    repo_root = Path(__file__).resolve().parents[1]
    for relative in (
        "app/services/incremental_audit_runtime.py",
        "app/workflows/incremental_price_audit.py",
    ):
        text = (repo_root / relative).read_text(encoding="utf-8").lower()
        assert "staples" not in text, relative


def test_case19_runner_absorbs_crawler_crash_and_transition_errors(stack, monkeypatch):
    cid = _seed_candidate(stack.prewarm)
    crashing = FakeCrawler(error=ValueError("kaboom"))
    summary = _run(stack, crashing, now=T0)
    assert summary["transient_failures"] == 1 and summary["errors"] == []
    job = stack.staples.job_for_candidate(cid)
    assert job["state"] == STAPLES_JOB_STATE_RETRY_WAIT
    assert "kaboom" in job["last_error"]
    # 候选迁移失败:收集进 errors,绝不抛出
    stack.shared.record_success(STAPLES_CACHE_MALL_KEY, URL, source="late-seed", price=1.0)

    def broken(candidate_id, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(stack.prewarm, "record_candidate_result", broken)
    summary2 = _run(stack, FakeCrawler(), now=_t(600))
    assert summary2["recheck_hits"] == 1
    assert any("cache-hit flip" in error for error in summary2["errors"])
    assert stack.staples.job_for_candidate(cid)["state"] == STAPLES_JOB_STATE_SUCCESS


def test_zhaoshang_tripwire_zero_after_full_pass(stack):
    cid = _seed_candidate(stack.prewarm)
    executor = FakeExecutor(
        records={URL: {"price": 12.5, "title": "史泰博商品", "uid": UID, "comment": "123"}}
    )
    crawler = _default_adapter(stack, executor, FakeFailureReader())
    summary = _run(stack, crawler)
    assert summary["cache_hit_after_crawl"] == 1
    assert stack.prewarm.candidate_by_id(cid)["cache_status"] == CACHE_STATUS_CACHE_HIT
    assert ZHAOSHANG_INVOCATIONS == 0


# ---- 适配器细节 ------------------------------------------------------------------


def test_crawl_uses_fresh_attempt_directory(stack):
    """Blocker C1:crawl 每次把独立 attempt 目录传给 executor,绝不传 base work_dir。"""
    base = stack.work_dir
    base.mkdir(parents=True, exist_ok=True)
    executor = FakeExecutor(
        records={URL: {"price": 12.5, "title": "t", "uid": UID, "comment": "1"}}
    )
    crawler = StaplesLocalCrawler(
        executor=executor, failure_reader=FakeFailureReader(), work_dir=base
    )
    outcomes = crawler.crawl([{"original_url": URL}])
    assert len(executor.calls) == 1
    attempt_dir = executor.calls[0][1]
    assert attempt_dir != base  # RED:当前传入 base work_dir
    assert attempt_dir.parent == base / "attempts"
    assert attempt_dir.parent.parent == base
    assert outcomes[URL].status == STAPLES_OUTCOME_SUCCESS


def test_crawl_failure_reader_reads_fresh_attempt_dir_not_base(tmp_path):
    """Blocker C2:base 目录旧档(含 requested URL 的终局失败原因)不得泄漏进本次判定。"""
    base = tmp_path / "work"
    base.mkdir()
    _write_result_xlsx(base / "stbchina_missing_result.xlsx", rows=[
        ["", "", URL, "3.0", "", "失败", "商品已下架", "", ""],  # 旧档:终局失败
    ])
    executor = FakeExecutor(records={})  # 本次 executor 未返回该 URL 记录
    crawler = StaplesLocalCrawler(executor=executor, work_dir=base)  # 默认 failure reader
    outcomes = crawler.crawl([{"original_url": URL}])
    assert len(executor.calls) == 1
    # 只能按"本次 attempt 无结果"判 TRANSIENT,绝不继承旧档的 商品已下架
    assert outcomes[URL].status == STAPLES_OUTCOME_TRANSIENT_FAILURE
    assert outcomes[URL].diagnostic == "采集失败，无原因记录"


def test_crawl_stale_base_result_xlsx_cannot_short_circuit(stack):
    """Blocker C2:base 目录旧结果档不能让本次爬取被短路;结果必须来自 executor。"""
    stale_record = {"price": 3.0, "title": "旧标题", "uid": UID, "comment": "1"}
    fresh_record = {"price": 99.0, "title": "新标题", "uid": UID, "comment": "2"}
    base = stack.work_dir
    base.mkdir(parents=True, exist_ok=True)
    _write_result_xlsx(base / "stbchina_missing_result.xlsx", rows=[
        ["旧标题", UID, URL, "3.0", "1", "成功", "", "", ""],  # 覆盖 requested URL 的旧档
    ])
    legacy = LegacyLikeExecutor(stale_records={URL: stale_record}, fresh_records={URL: fresh_record})
    crawler = StaplesLocalCrawler(executor=legacy, failure_reader=FakeFailureReader(), work_dir=base)
    outcomes = crawler.crawl([{"original_url": URL}])
    assert len(legacy.calls) == 1
    assert legacy.crawled == [True]  # executor MUST actually crawl:旧档不得短路
    assert outcomes[URL].status == STAPLES_OUTCOME_SUCCESS
    assert outcomes[URL].price == 99.0  # 结果来自 executor 返回值,而非旧档的 3.0


def test_crawl_consecutive_attempts_isolated(stack):
    """Blocker C3:连续两次 crawl 目录互不可见;A 留下的结果档不短路 B。"""
    base = stack.work_dir
    base.mkdir(parents=True, exist_ok=True)
    legacy = LegacyLikeExecutor(
        stale_records={URL: {"price": 3.0, "title": "旧", "uid": UID, "comment": "1"}},
        fresh_records={URL: {"price": 66.0, "title": "新B", "uid": UID, "comment": "9"}},
    )
    crawler = StaplesLocalCrawler(
        executor=legacy, failure_reader=FakeFailureReader(), work_dir=base
    )
    first = crawler.crawl([{"original_url": URL}])
    dir_a = legacy.calls[0][1]
    assert legacy.crawled == [True]
    assert (dir_a / "stbchina_missing_result.xlsx").is_file()  # A 在自己目录留下结果档
    second = crawler.crawl([{"original_url": URL}])
    dir_b = legacy.calls[1][1]
    assert legacy.crawled == [True, True]  # B 未被 A 的结果档短路
    assert dir_a != dir_b  # RED 语义:旧实现两次都是同一个 base work_dir
    assert dir_a.parent == dir_b.parent == base / "attempts"
    assert first[URL].price == 66.0 and second[URL].price == 66.0


def test_adapter_price_zero_or_missing_is_transient(stack):
    crawler = _default_adapter(
        stack, FakeExecutor(records={URL: {"price": 0.0, "title": "t", "uid": UID, "comment": "1"}})
    )
    outcomes = crawler.crawl([{"original_url": URL}])
    assert outcomes[URL].status == STAPLES_OUTCOME_TRANSIENT_FAILURE
    assert outcomes[URL].diagnostic == "价格不可见"
    crawler2 = _default_adapter(
        stack, FakeExecutor(records={URL: {"title": "t", "uid": UID, "comment": "1"}})
    )
    outcomes2 = crawler2.crawl([{"original_url": URL}])
    assert outcomes2[URL].status == STAPLES_OUTCOME_TRANSIENT_FAILURE
    assert outcomes2[URL].diagnostic == "价格不可见"


def test_adapter_comment_digits_only(stack):
    records = {"price": 8.8, "title": "t", "uid": UID, "comment": "1,234"}
    crawler = _default_adapter(stack, FakeExecutor(records={URL: records}))
    assert crawler.crawl([{"original_url": URL}])[URL].comment_count == 1234
    unknown = {"price": 8.8, "title": "t", "uid": UID, "comment": "未识别"}
    crawler2 = _default_adapter(stack, FakeExecutor(records={URL: unknown}))
    outcome = crawler2.crawl([{"original_url": URL}])[URL]
    assert outcome.comment_count is None
    assert outcome.crawler_comment_raw == "未识别"
    empty = {"price": 8.8, "title": "t", "uid": UID, "comment": ""}
    crawler3 = _default_adapter(stack, FakeExecutor(records={URL: empty}))
    assert crawler3.crawl([{"original_url": URL}])[URL].comment_count is None


def test_read_stbchina_failure_reasons_parses_xlsx(tmp_path):
    url_ok = "https://www.stbchina.cn/item.html?itemId=1"
    url_final = "https://www.stbchina.cn/item.html?itemId=2"
    url_transient = "https://www.stbchina.cn/item.html?itemId=3"
    path = tmp_path / "stbchina_missing_result.xlsx"
    _write_result_xlsx(path, rows=[
        ["t", "1", url_ok, "9.9", "10", "成功", "", "2026-09-08", "1"],
        ["", "", url_final, "", "", "失败", "商品不存在", "", ""],
        ["", "", url_transient, "", "", "失败", "页面超时", "", ""],
    ])
    reasons = read_stbchina_failure_reasons(path)
    assert reasons == {url_final: "商品不存在", url_transient: "页面超时"}
    assert read_stbchina_failure_reasons(tmp_path / "missing.xlsx") == {}


class WritingExecutor:
    """按 legacy 契约把结果 xlsx 写进收到的 work_dir 的假执行器。"""

    def __init__(self, records=None, rows=None):
        self.records = dict(records or {})
        self.rows = list(rows or [])
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, urls, work_dir):
        self.calls.append((list(urls), Path(work_dir)))
        _write_result_xlsx(Path(work_dir) / "stbchina_missing_result.xlsx", rows=self.rows)
        return {url: self.records[url] for url in urls if url in self.records}


def test_default_adapter_reads_result_xlsx_and_classifies(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    url_ok = "https://www.stbchina.cn/item.html?itemId=1"
    url_final = "https://www.stbchina.cn/item.html?itemId=2"
    url_transient = "https://www.stbchina.cn/item.html?itemId=3"
    # executor 把结果档写进它收到的 attempt 目录(legacy 契约),默认 reader
    # 从同一 attempt 目录读回:失败原因判定基于本次新鲜结果。
    executor = WritingExecutor(
        records={url_ok: {"price": 9.9, "title": "t", "uid": "1", "comment": "10"}},
        rows=[
            ["t", "1", url_ok, "9.9", "10", "成功", "", "2026-09-08", "1"],
            ["", "", url_final, "", "", "失败", "商品不存在", "", ""],
            ["", "", url_transient, "", "", "失败", "页面超时", "", ""],
        ],
    )
    crawler = StaplesLocalCrawler(executor=executor, work_dir=work)
    outcomes = crawler.crawl([{"original_url": url} for url in (url_ok, url_final, url_transient)])
    assert len(executor.calls) == 1
    assert executor.calls[0][1] != work  # 独立 attempt 目录(Blocker C1)
    assert (executor.calls[0][1] / "stbchina_missing_result.xlsx").is_file()
    assert outcomes[url_ok].status == STAPLES_OUTCOME_SUCCESS
    assert outcomes[url_final].status == STAPLES_OUTCOME_FINAL_FAILURE
    assert outcomes[url_final].diagnostic == "商品不存在"
    assert outcomes[url_transient].status == STAPLES_OUTCOME_TRANSIENT_FAILURE
    assert outcomes[url_transient].diagnostic == "页面超时"


# ---- 计划只读助手 ---------------------------------------------------------------


def _patch_fake_monitor(tmp_path, monkeypatch, script_exists=False):
    script = tmp_path / "fake_stb" / "stbchina_fast_crawler.py"
    if script_exists:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# fake stb crawler", encoding="utf-8")
    monkeypatch.setitem(
        sys.modules, "scripts.monitor_stb_fallback", types.SimpleNamespace(STB_SCRIPT=script)
    )
    return script


def test_plan_readonly_missing_paths_create_nothing(tmp_path, monkeypatch):
    _patch_fake_monitor(tmp_path, monkeypatch)
    prewarm_db = tmp_path / "missing" / "prewarm.sqlite3"
    cache_db = tmp_path / "missing" / "cache.sqlite3"
    report = staples_plan_readonly(prewarm_db, cache_db)
    assert report["prewarm_store"] == "PREWARM_STORE_MISSING"
    assert report["cache"] == "CACHE_MISSING"
    for key in (
        "candidates_total", "staples_candidates", "unique_staples_urls",
        "duplicate_documents", "cache_hit_after_recheck", "local_retry_planned",
        "local_cache_sync_pending", "local_final_failed", "local_crawl_planned",
    ):
        assert report[key] == 0, key
    assert not prewarm_db.exists()
    assert not cache_db.exists()


def test_plan_readonly_crawler_availability_lazy_default(tmp_path, monkeypatch):
    script = _patch_fake_monitor(tmp_path, monkeypatch, script_exists=False)
    report = staples_plan_readonly(tmp_path / "p.sqlite3", tmp_path / "c.sqlite3")
    assert report["crawler_available"] is False
    assert report["crawler_script_exists"] is False
    assert report["crawler_script"] == str(script)
    existing = _patch_fake_monitor(tmp_path, monkeypatch, script_exists=True)
    report2 = staples_plan_readonly(tmp_path / "p2.sqlite3", tmp_path / "c2.sqlite3")
    assert report2["crawler_available"] is True
    assert report2["crawler_script_exists"] is True
    assert report2["crawler_script"] == str(existing)
    explicit = staples_plan_readonly(
        tmp_path / "p3.sqlite3", tmp_path / "c3.sqlite3", crawler_available=True
    )
    assert explicit["crawler_available"] is True


def test_plan_readonly_populated_counts(stack):
    prewarm = stack.prewarm
    urls = {
        index: f"https://www.stbchina.cn/item.html?itemId=100{index}" for index in range(1, 6)
    }
    _seed_candidate(prewarm, urls[1], "DA")  # A: miss, 无作业 -> 计划爬取
    b = _seed_candidate(prewarm, urls[2], "DB")  # B: RETRY_WAIT -> 计划爬取
    _seed_candidate(prewarm, urls[3], "DC")  # C: 缓存命中
    d = _seed_candidate(prewarm, urls[4], "DD1")  # D: 本地待同步 + 双文档
    prewarm.upsert_link("DD2", urls[4], mall=STAPLES_CACHE_MALL_KEY)
    e = _seed_candidate(prewarm, urls[5], "DE")  # E: FAILED
    _seed_candidate(prewarm, "https://www.jd.com/product/1.html", "DF", mall="jd")  # 非史泰博

    def _job(candidate_id, state, **kwargs):
        job = stack.staples.ensure_job(candidate_id, now=T0)
        return stack.staples.complete_job(int(job["id"]), None, state=state, **kwargs)

    _job(b, STAPLES_JOB_STATE_RETRY_WAIT, reason="页面超时", next_retry_at=_t(300))
    _job(d, STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING,
         success_payload={"submitted_url": urls[4]})
    _job(e, STAPLES_JOB_STATE_FAILED, reason="商品不存在")
    stack.shared.record_success(STAPLES_CACHE_MALL_KEY, urls[3], source="seed", price=2.0)

    report = staples_plan_readonly(stack.db_path, stack.cache_path, crawler_available=True)
    assert report["prewarm_store"] == "OK"
    assert report["cache"] == "OK"
    assert report["candidates_total"] == 6
    assert report["staples_candidates"] == 5
    assert report["unique_staples_urls"] == 5
    assert report["duplicate_documents"] == 1
    assert report["cache_hit_after_recheck"] == 1
    assert report["local_retry_planned"] == 1
    assert report["local_cache_sync_pending"] == 1
    assert report["local_final_failed"] == 1
    assert report["local_crawl_planned"] == 2  # A(无作业) + B(RETRY_WAIT)
    assert report["crawler_available"] is True


# ---- CLI:staples_prewarm_run(§ ticket 20/21 + live 冒烟) --------------------------

BUSINESS_DATE_CLI = "2026-09-08"


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _patch_cli_monitor(tmp_path, monkeypatch):
    """注入带爬取哨兵的假 monitor_stb_fallback:plan-only 一旦爬取立即失败。

    与 _patch_fake_monitor 同法,但 run_stbchina_fallback 记录调用并抛
    AssertionError,用于证明 plan-only 绝不触发任何爬取。
    """
    script = tmp_path / "fake_stb" / "stbchina_fast_crawler.py"
    calls: list[list[str]] = []

    def _poison_executor(urls, work_dir):
        calls.append([str(url) for url in urls])
        raise AssertionError("plan-only must not crawl")

    monkeypatch.setitem(
        sys.modules,
        "scripts.monitor_stb_fallback",
        types.SimpleNamespace(STB_SCRIPT=script, run_stbchina_fallback=_poison_executor),
    )
    return calls


def test_case20_plan_only_zero_write(tmp_path, monkeypatch, capsys):
    crawl_calls = _patch_cli_monitor(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    db = runtime / "customer_prewarm.sqlite3"
    cache = runtime / "shared_crawl_cache.sqlite3"
    prewarm = CustomerPrewarmStore(db)
    StaplesPrewarmStore(db)  # sidecar 表与真实部署同文件
    cid = _seed_candidate(prewarm)
    assert cid >= 1
    SharedCrawlCache(cache).record_success(
        "nbdeli", "https://b2b.nbdeli.com/goods/1.html", source="seed"
    )
    json_out = tmp_path / "plan.json"
    before_sizes = {path: path.stat().st_size for path in (db, cache)}
    before_hash = {path: _sha256(path) for path in (db, cache)}
    before_files = {entry.name for entry in runtime.iterdir()}

    exit_code = cli.main(
        [
            "--plan-only",
            "--business-date",
            BUSINESS_DATE_CLI,
            "--prewarm-store-path",
            str(db),
            "--shared-cache-path",
            str(cache),
            "--json-output",
            str(json_out),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "staples_prewarm_run plan-only" in out
    assert "crawler_available=False" in out
    plan = json.loads(json_out.read_text(encoding="utf-8"))
    assert plan["mode"] == "plan-only"
    assert plan["business_date"] == BUSINESS_DATE_CLI
    assert plan["staples_candidates"] >= 1
    assert plan["local_crawl_planned"] >= 1
    # 零写入门禁:两个 DB 字节恒等(SHA256)且大小不变
    for path in (db, cache):
        assert _sha256(path) == before_hash[path]
        assert path.stat().st_size == before_sizes[path]
    # DB 目录零新增文件(仅容忍 SQLite 只读访问的瞬态 -shm/-wal 伴生)
    new_files = {entry.name for entry in runtime.iterdir()} - before_files
    assert all(name.endswith(("-shm", "-wal")) for name in new_files)
    # 假执行器从未被调用:plan-only 绝不爬取
    assert crawl_calls == []


def test_case20b_plan_only_both_dbs_missing_exit_2(tmp_path, monkeypatch, capsys):
    _patch_cli_monitor(tmp_path, monkeypatch)
    db = tmp_path / "customer_prewarm.sqlite3"
    cache = tmp_path / "shared_crawl_cache.sqlite3"

    exit_code = cli.main(
        [
            "--plan-only",
            "--business-date",
            BUSINESS_DATE_CLI,
            "--prewarm-store-path",
            str(db),
            "--shared-cache-path",
            str(cache),
        ]
    )

    assert exit_code == 2
    out = capsys.readouterr().out
    assert "PREWARM_STORE_MISSING" in out
    assert "CACHE_MISSING" in out
    # 缺库只上报,绝不创建
    assert not db.exists()
    assert not cache.exists()


def test_case20c_plan_only_cache_missing_exit_2(tmp_path, monkeypatch, capsys):
    _patch_cli_monitor(tmp_path, monkeypatch)
    db = tmp_path / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(db)
    assert _seed_candidate(prewarm) >= 1
    cache = tmp_path / "shared_crawl_cache.sqlite3"
    db_hash = _sha256(db)

    exit_code = cli.main(
        [
            "--plan-only",
            "--business-date",
            BUSINESS_DATE_CLI,
            "--prewarm-store-path",
            str(db),
            "--shared-cache-path",
            str(cache),
        ]
    )

    assert exit_code == 2
    out = capsys.readouterr().out
    assert "CACHE_MISSING" in out
    assert "PREWARM_STORE_MISSING" not in out
    assert not cache.exists()
    assert _sha256(db) == db_hash


def test_case20d_plan_only_json_output_equals_db_rejected(tmp_path, monkeypatch, capsys):
    _patch_cli_monitor(tmp_path, monkeypatch)
    db = tmp_path / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(db)
    assert _seed_candidate(prewarm) >= 1
    cache = tmp_path / "shared_crawl_cache.sqlite3"
    db_hash = _sha256(db)

    exit_code = cli.main(
        [
            "--plan-only",
            "--business-date",
            BUSINESS_DATE_CLI,
            "--prewarm-store-path",
            str(db),
            "--shared-cache-path",
            str(cache),
            "--json-output",
            str(db),
        ]
    )

    assert exit_code == 2
    assert "ERROR" in capsys.readouterr().out
    # 拒绝发生在任何 IO 之前:DB 字节恒等
    assert _sha256(db) == db_hash


def test_cli_live_json_output_equals_db_rejected(tmp_path, monkeypatch, capsys):
    """live 模式同款守卫:--json-output 等于任一 DB 路径必须 exit 2,且在任何写之前拒绝。"""
    _patch_cli_monitor(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    db = runtime / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(db)
    StaplesPrewarmStore(db)  # sidecar 表与真实部署同文件
    assert _seed_candidate(prewarm) >= 1
    cache = runtime / "shared_crawl_cache.sqlite3"
    SharedCrawlCache(cache).record_success(
        "nbdeli", "https://b2b.nbdeli.com/goods/1.html", source="seed"
    )
    before_hash = {path: _sha256(path) for path in (db, cache)}
    before_files = {entry.name for entry in runtime.iterdir()}

    exit_code = cli.main(
        [
            "--business-date",
            BUSINESS_DATE_CLI,
            "--runtime-root",
            str(runtime),
            "--json-output",
            str(db),
        ]
    )

    assert exit_code == 2
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "equals a database path" in out
    # 拒绝发生在任何写之前(锁/DB/-wal/-shm 都未产生):两个 DB 字节恒等
    for path in (db, cache):
        assert _sha256(path) == before_hash[path]
    assert {entry.name for entry in runtime.iterdir()} == before_files


def test_case21_plan_only_db_absent_parent_dir_not_created(tmp_path, monkeypatch):
    _patch_cli_monitor(tmp_path, monkeypatch)
    missing_dir = tmp_path / "nope"
    cache_dir = tmp_path / "also-missing"

    exit_code = cli.main(
        [
            "--plan-only",
            "--business-date",
            BUSINESS_DATE_CLI,
            "--prewarm-store-path",
            str(missing_dir / "x.sqlite3"),
            "--shared-cache-path",
            str(cache_dir / "cache.sqlite3"),
        ]
    )

    assert exit_code == 2
    assert not missing_dir.exists()
    assert not cache_dir.exists()


def test_cli_plan_only_default_business_date_and_json_on_failure(tmp_path, monkeypatch):
    """缺省 business_date 走 Asia/Shanghai 注入点;缺库失败路径仍写出 plan JSON。"""
    _patch_cli_monitor(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "business_date_in_shanghai", lambda now=None: BUSINESS_DATE_CLI)
    runtime = tmp_path / "runtime"
    json_out = tmp_path / "plan.json"

    exit_code = cli.main(["--plan-only", "--runtime-root", str(runtime), "--json-output", str(json_out)])

    assert exit_code == 2  # 缺省路径下两个库都不存在
    plan = json.loads(json_out.read_text(encoding="utf-8"))
    assert plan["mode"] == "plan-only"
    assert plan["business_date"] == BUSINESS_DATE_CLI
    assert plan["prewarm_store"] == "PREWARM_STORE_MISSING"
    assert not (runtime / "customer_prewarm.sqlite3").exists()
    assert not (runtime / "shared_crawl_cache.sqlite3").exists()


def test_cli_invalid_arguments_fail_closed(capsys):
    assert cli.main(["--business-date", "20260908"]) == 2
    assert cli.main(["--business-date", BUSINESS_DATE_CLI, "--max-jobs", "0"]) == 2
    assert cli.main(["--business-date", BUSINESS_DATE_CLI, "--route-limit", "-1"]) == 2
    out = capsys.readouterr().out
    assert out.count("ERROR") == 3


def test_cli_help():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0


def test_cli_live_lock_conflict_skips_before_any_store(tmp_path, monkeypatch, capsys):
    class _LockedLock:
        def __init__(self, path):
            self.path = Path(path)

        def acquire(self, owner=None):
            raise LeaseUnavailable(f"locked: {self.path}")

        def release(self):  # pragma: no cover - acquire 失败后绝不应 release
            raise AssertionError("lock must not be released when acquire failed")

    class _NoStore:
        def __init__(self, *args, **kwargs):
            raise AssertionError("stores must not be constructed when the lock is held")

    monkeypatch.setattr(cli, "ProcessLock", _LockedLock)
    monkeypatch.setattr(cli, "CustomerPrewarmStore", _NoStore)
    monkeypatch.setattr(cli, "SharedCrawlCache", _NoStore)
    monkeypatch.setattr(cli, "StaplesPrewarmStore", _NoStore)
    monkeypatch.setattr(cli, "StaplesLocalCrawler", _NoStore)
    runtime = tmp_path / "runtime"

    exit_code = cli.main(["--business-date", BUSINESS_DATE_CLI, "--runtime-root", str(runtime)])

    assert exit_code == 0
    assert "SKIPPED_ALREADY_RUNNING" in capsys.readouterr().out
    # 锁先行:任何 store/目录都未被创建
    assert not runtime.exists()


def test_cli_live_happy_path_local_pass_end_to_end(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / "runtime"
    db = runtime / "customer_prewarm.sqlite3"
    cache_db = runtime / "shared_crawl_cache.sqlite3"
    prewarm = CustomerPrewarmStore(db)
    StaplesPrewarmStore(db)
    cid = _seed_candidate(prewarm)
    SharedCrawlCache(cache_db).record_success(
        "nbdeli", "https://b2b.nbdeli.com/goods/1.html", source="seed"
    )
    executor = FakeExecutor(
        records={URL: {"price": 12.5, "title": "史泰博商品", "uid": UID, "comment": "123"}}
    )
    created: list[Any] = []

    def _fake_crawler_factory(*, work_dir):
        crawler = StaplesLocalCrawler(
            executor=executor, failure_reader=FakeFailureReader(), work_dir=work_dir
        )
        created.append(crawler)
        return crawler

    monkeypatch.setattr(cli, "StaplesLocalCrawler", _fake_crawler_factory)
    json_out = tmp_path / "summary.json"

    exit_code = cli.main(
        [
            "--business-date",
            BUSINESS_DATE_CLI,
            "--runtime-root",
            str(runtime),
            "--json-output",
            str(json_out),
        ]
    )

    assert exit_code == 0
    assert len(created) == 1
    assert len(executor.calls) == 1 and len(executor.calls[0][0]) == 1
    out = capsys.readouterr().out
    assert "staples_prewarm_run live" in out
    summary = json.loads(json_out.read_text(encoding="utf-8"))
    assert summary["mode"] == "live"
    assert summary["business_date"] == BUSINESS_DATE_CLI
    assert summary["routed"] == 1
    assert summary["jobs_claimed"] == 1
    assert summary["crawl_success"] == 1
    assert summary["cache_hit_after_crawl"] == 1
    assert summary["errors"] == []
    assert summary["prewarm_store_path"] == str(db.resolve())
    candidate = CustomerPrewarmStore(db).candidate_by_id(cid)
    assert candidate["cache_status"] == CACHE_STATUS_CACHE_HIT
    assert ZHAOSHANG_INVOCATIONS == 0
