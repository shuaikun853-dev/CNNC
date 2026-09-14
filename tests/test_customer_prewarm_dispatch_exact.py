"""dispatch_single_candidate_exact 行为测试(Issue #13 Slice B)。

守卫式单 URL 手动重爬的招商局路径:probe → 复用+boost 或 恰好一个单 URL
任务 → 提交 → 优先级证明。硬性不变量:候选行绝不写入(CACHE_HIT 是终态,
手动重爬不得翻转任何 cache_status,包括间接路径),诚实结果
(SUBMIT_UNKNOWN / 优先级未证明)只如实上报,绝不二次 POST。

fake adapter / seeder 模式照抄 tests/test_customer_prewarm_dispatch.py。
全程零真实远端调用。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from app.services.customer_prewarm_dispatch import (
    BACKEND_VERSION,
    DISPATCH_LOCK_NAME,
    dispatch_single_candidate_exact,
    prewarm_placeholder_sku,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_runtime import runtime_paths
from app.services.legacy_zhaoshang_adapter import (
    CrawlRequest,
    LegacyPriorityOutcome,
    LegacySubmitOutcome,
    PriorityStatus,
    SubmitStatus,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

BUSINESS_DATE = "2026-09-14"

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
    """Attribute-compatible PreparedLegacyJob stand-in (dispatcher only does
    attribute access).  Keys are deterministic so a reconstruct via
    ``prepare_job`` verifies against the originally prepared evidence."""

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
    """Fake adapter with counters; ``submit_raises`` poisons submit so any
    unexpected POST fails the test loudly instead of silently passing."""

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
            raise AssertionError("manual recrawl submitted on a path that must never POST")
        return self.submit_outcome

    def prioritize(self, _job, *, task_id, task_number, timeout_seconds):
        self.prioritize_calls += 1
        if self.priority_outcomes:
            return self.priority_outcomes.pop(0)
        return LegacyPriorityOutcome(PriorityStatus.UPDATED, weight_type=-1)


# ---- fixtures and seeders (copied from test_customer_prewarm_dispatch) ---------


def make_state_db(runtime_root: Path, business_date: str):
    paths = runtime_paths(business_date, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(business_date)
    return paths


def cache_hit_candidate(prewarm: CustomerPrewarmStore, url: str, *, mall="comix", document="D1"):
    """Candidate in the terminal CACHE_HIT state (the guarded status)."""
    upsert = prewarm.upsert_link(document, url, mall=mall)
    candidate_id = int(upsert["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_CACHE_HIT)
    return candidate_id


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
    """Seed one bound crawl row + remote job with adapter-consistent evidence."""
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


def _state_rows(path, query, params=()):
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def _crawl_rows(state_path):
    return _state_rows(state_path, "SELECT * FROM crawl_work ORDER BY id")


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


# ---- guard outcomes: candidate / route -----------------------------------------


def test_candidate_not_found(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")

    result = _dispatch(runtime_root, prewarm, cache, candidate_id=424242)

    assert result["outcome"] == "CANDIDATE_NOT_FOUND"
    assert result["phase_errors"] == []


def test_staples_candidate_not_dispatchable_zero_adapter_calls(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    upsert = prewarm.upsert_link("D1", "https://www.stbchina.cn/item.html?itemId=1", mall="stbchina")
    candidate_id = int(upsert["candidate"]["id"])
    adapter = _CountingAdapter(submit_raises=True)

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "NOT_DISPATCHABLE"
    assert result["reason"] == "STAPLES"
    assert result["phase_errors"] == []
    assert adapter.prepare_calls == 0
    assert adapter.submit_calls == 0
    assert adapter.prioritize_calls == 0


def test_instance_lock_busy_skips_without_any_call(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    upsert = prewarm.upsert_link("D1", "https://qx.com/exact-lock.html", mall="comix")
    candidate_id = int(upsert["candidate"]["id"])
    adapter = _CountingAdapter(submit_raises=True)
    lock = ProcessLock(runtime_root / DISPATCH_LOCK_NAME)
    lock.acquire()
    try:
        result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)
    finally:
        lock.release()

    assert result["outcome"] == "SKIPPED_ALREADY_RUNNING"
    assert result["phase_errors"] == []
    assert adapter.prepare_calls == 0 and adapter.submit_calls == 0


# ---- Case A: 未完成任务复用 + boost,绝不新建/提交 -----------------------------------


def test_case_a_reuse_boosts_existing_job_never_creates_or_submits(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-reuse.html"
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-ea|number-ea",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    before = prewarm.candidate_by_id(candidate_id)
    # submit is poisoned: a single unexpected POST fails this test loudly.
    adapter = _CountingAdapter(submit_raises=True)

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "REUSED"
    assert result["remote_job_id"] == job_id
    assert result["remote_identity"] == "comix|remote-ea|number-ea"
    assert result["phase_errors"] == []
    # Boost needs exactly one prepare (reconstruct from durable payload) and
    # one idempotent prioritize; creation-style prepares stay at zero.
    assert adapter.prepare_calls == 1
    assert adapter.prioritize_calls == 1
    assert adapter.submit_calls == 0
    assert prewarm.candidate_by_id(candidate_id) == before  # CACHE_HIT untouched
    assert len(_crawl_rows(paths.state_path)) == 1  # no new crawl work


def test_case_a_unconfirmed_boost_blocks_candidate_write_and_stays_honest(tmp_path):
    """复用 + boost 优先级未证明:_NoCandidateWriteStore 路径下 boost 未确认
    分支的候选行 note 写入被直接跳过(D6,不再产生合成 RuntimeError
    phase_error);结果仍是诚实的 REUSED(含 priority_unconfirmed 计数与
    priority_proven False),远端任务状态/爬虫行/候选行全部原样,绝不新建、
    绝不 POST。"""
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-reuse-unconfirmed.html"
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-ea|number-ea",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    before = prewarm.candidate_by_id(candidate_id)
    # UNCONFIRMED boost(weight_type 不是 -1)+ submit 毒化:任何意外 POST 直接
    # 让测试失败(与 test_case_a 同一防泄漏手法)。
    adapter = _CountingAdapter(
        submit_raises=True,
        priority_outcomes=[
            LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic="tool died")
        ],
    )

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    # 不抛异常;复用本身仍诚实上报(未确认优先级不冒充完全成功)。
    assert result["outcome"] == "REUSED"
    assert result["remote_job_id"] == job_id
    assert result["priority_proven"] is False
    assert result["priority_unconfirmed"] == 1
    # D6:守卫店的拒绝不再以合成 RuntimeError 进 phase_errors——note 写入被
    # 直接跳过(guarded 路径绝不写候选行,note 只是 pass 内记账)。
    assert not any(
        "manual recrawl never writes candidate cache_status" in message
        for message in result["phase_errors"]
    ), result["phase_errors"]
    # 候选行前后字节级一致(CACHE_HIT 终态,含间接路径也绝不写)。
    assert prewarm.candidate_by_id(candidate_id) == before
    # 远端任务状态原样;无新建爬虫行;submit/create 计数为零。
    job = _state_rows(paths.state_path, "SELECT state FROM remote_jobs")[0]
    assert job["state"] == "SUBMITTED"
    assert len(_crawl_rows(paths.state_path)) == 1
    assert adapter.submit_calls == 0
    assert adapter.prepare_calls == 1  # 仅 boost 的 payload 重建
    assert adapter.prioritize_calls == 1


# ---- Case B: 恰好一个单 URL 新任务 + 提交 + 优先级证明 ------------------------------


def test_case_b_creates_exactly_one_single_url_job_and_proves_priority(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-create.html"
    candidate_id = cache_hit_candidate(prewarm, url)
    before = prewarm.candidate_by_id(candidate_id)
    adapter = _CountingAdapter()

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "CREATED"
    assert result["priority_confirmed"] is True
    assert result["task_number"] == "number-1"
    assert result["remote_identity"] == f"comix|remote-1|{result['task_number']}"
    assert result["phase_errors"] == []
    assert adapter.prepare_calls == 1
    assert adapter.submit_calls == 1
    assert adapter.prioritize_calls == 1
    prepared = adapter.prepared_log[0]
    # exactly ONE single-URL request carrying the lookup URL + placeholder SKU
    assert len(prepared.requests) == 1
    assert prepared.requests[0].url == url
    assert prepared.requests[0].sku_id == prewarm_placeholder_sku("comix", url)
    assert prepared.requests[0].mall_key == "comix"
    assert re.fullmatch(r"cnnc_current_opt_.+_\d{14}", prepared.task_name)
    job = _state_rows(paths.state_path, "SELECT * FROM remote_jobs")
    assert len(job) == 1
    assert job[0]["state"] == "SUBMITTED"  # priority proven -> SUBMITTED
    crawl = _crawl_rows(paths.state_path)
    assert len(crawl) == 1
    assert crawl[0]["state"] == "SUBMITTED"
    # candidate row byte-identical: CACHE_HIT is terminal and must stay so
    assert prewarm.candidate_by_id(candidate_id) == before


# ---- Case C: SUBMIT_UNKNOWN 只对账,同一 fixture 重跑绝不二次 POST --------------------


def test_case_c_submit_unknown_never_second_post(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-unknown.html"
    candidate_id = cache_hit_candidate(prewarm, url)
    adapter = _CountingAdapter(
        submit_outcome=LegacySubmitOutcome(SubmitStatus.SUBMIT_UNKNOWN, diagnostic="timeout")
    )

    first = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert first["outcome"] == "SUBMIT_UNKNOWN"
    assert first["diagnostic"] == "timeout"
    assert first["remote_job_id"] is not None
    assert adapter.submit_calls == 1
    job = _state_rows(paths.state_path, "SELECT state FROM remote_jobs")[0]
    assert job["state"] == "SUBMIT_UNKNOWN"

    # same fixture again: the unfinished bound job is reused (reconcile-only),
    # so the submit call count stays at exactly one across BOTH calls.
    second = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert second["outcome"] == "REUSED"
    assert second["remote_job_id"] == first["remote_job_id"]
    assert adapter.submit_calls == 1
    assert len(_crawl_rows(paths.state_path)) == 1  # never re-created


# ---- Case D: 优先级未证明 → 诚实 CREATED + priority_confirmed False ------------------


def test_boost_requires_weight_type_minus_one_readback(tmp_path):
    """D3:status=UPDATED 但 weight_type!= -1 的 readback 绝不算 proven boost。

    复用路径的 priority_proven 必须与 _priority_proven 同一口径:缺 -1 证据 →
    False + priority_unconfirmed 计数,优先级绝不冒充已证明。"""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-boost-w0.html"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-w0|number-w0",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    adapter = _CountingAdapter(
        submit_raises=True,
        priority_outcomes=[
            LegacyPriorityOutcome(PriorityStatus.UPDATED, weight_type=0, diagnostic="readback weight_type=0")
        ],
    )

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "REUSED"
    assert result["remote_job_id"] == job_id
    # honest tri-state: status-accepted-but-weight-unproven is NOT proven
    assert result["priority_proven"] is False
    assert result["priority_boosted"] == 0
    assert result["priority_unconfirmed"] == 1
    assert adapter.prioritize_calls == 1
    assert adapter.submit_calls == 0
    # the job row stays untouched by the boost path
    job = _state_rows(paths.state_path, "SELECT state FROM remote_jobs")[0]
    assert job["state"] == "SUBMITTED"


def test_boost_updated_with_minus_one_is_proven(tmp_path):
    """D3:UPDATED AND weight_type == -1 → priority_proven True(boosted 计数)。"""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-boost-w-1.html"
    make_state_db(runtime_root, BUSINESS_DATE)
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-wm|number-wm",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    adapter = _CountingAdapter(submit_raises=True)

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "REUSED"
    assert result["remote_job_id"] == job_id
    assert result["priority_proven"] is True
    assert result["priority_boosted"] == 1
    assert result["priority_unconfirmed"] == 0
    assert adapter.prioritize_calls == 1


def test_exact_cross_day_unfinished_job_reused_no_duplicate_create(tmp_path):
    """D4:候选跨日 linkage 指向 Day1 未完成任务时,exact 在创建前只读探测
    Day1 库并复用该任务——绝不新建 Day2 重复远端任务,也绝不写 (D2, cand,
    job) 新 linkage(D1 linkage 保持权威)。"""
    day1 = "2026-09-13"
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-crossday-reuse.html"

    # Day1 state DB: unfinished bound RUNNING job for the URL + linkage row.
    paths1 = make_state_db(runtime_root, day1)
    _paths1, job1 = seed_remote_work(
        runtime_root,
        day1,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="RUNNING",
        remote_identity="comix|remote-cd|number-cd",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    prewarm.record_remote_job_link(
        day1, candidate_id, job1, "customer-prewarm-zhaoshang"
    )

    # Day2 (action day) state DB exists WITHOUT any work for the URL.
    paths2 = make_state_db(runtime_root, BUSINESS_DATE)
    adapter = _CountingAdapter(submit_raises=True)

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "REUSED"
    assert result["remote_job_id"] == job1
    assert result["remote_identity"] == "comix|remote-cd|number-cd"
    assert result["priority_proven"] is True
    assert result["cross_day"] is True
    assert result["job_business_date"] == day1
    # create chain NEVER ran: zero prepares besides the boost reconstruct,
    # zero submits, zero new rows in the Day2 state DB.
    assert adapter.prepare_calls == 1
    assert adapter.submit_calls == 0
    assert _crawl_rows(paths2.state_path) == []
    assert _state_rows(paths2.state_path, "SELECT * FROM remote_jobs") == []
    # the Day1 job row is untouched (read-only boost)
    job1_row = _state_rows(paths1.state_path, "SELECT state FROM remote_jobs")[0]
    assert job1_row["state"] == "RUNNING"
    # no NEW linkage row dated Day2 for that job id (Day1 linkage stays
    # authoritative)
    link_rows = _state_rows(
        runtime_root / "customer_prewarm.sqlite3",
        """SELECT * FROM prewarm_remote_job_links
           WHERE business_date=? AND candidate_id=? AND remote_job_id=?""",
        (BUSINESS_DATE, candidate_id, job1),
    )
    assert link_rows == []
    link = prewarm.remote_job_link_for_candidate(candidate_id)
    assert link is not None
    assert link["business_date"] == day1
    assert link["remote_job_id"] == job1


def test_exact_cross_day_missing_state_db_falls_back_to_create(tmp_path):
    """D4 回退:linkage 指向的业务日 state.sqlite3 文件缺失 → 诚实走正常
    创建链(恰好一个单 URL 任务),绝不静默吞掉。"""
    day1 = "2026-09-13"
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-crossday-missing.html"

    candidate_id = cache_hit_candidate(prewarm, url)
    # linkage points at a day whose state DB file was never created
    prewarm.record_remote_job_link(day1, candidate_id, 777, "customer-prewarm-zhaoshang")
    assert not (runtime_root / day1 / "state.sqlite3").is_file()

    paths2 = make_state_db(runtime_root, BUSINESS_DATE)
    adapter = _CountingAdapter()

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "CREATED"
    assert result["priority_confirmed"] is True
    assert adapter.prepare_calls == 1
    assert adapter.submit_calls == 1
    assert len(_crawl_rows(paths2.state_path)) == 1


def test_guarded_reused_unconfirmed_no_runtimeerror_noise(tmp_path):
    """D6:守卫式 exact REUSED 未确认不再携带合成的 RuntimeError phase_error
    ——guarded manual 路径绝不写候选行,note 只是 pass 内记账,直接跳过。"""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-boost-quiet.html"
    make_state_db(runtime_root, BUSINESS_DATE)
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-q|number-q",
    )
    candidate_id = cache_hit_candidate(prewarm, url)
    before = prewarm.candidate_by_id(candidate_id)
    adapter = _CountingAdapter(
        submit_raises=True,
        priority_outcomes=[
            LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic="tool died")
        ],
    )

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "REUSED"
    assert result["priority_proven"] is False
    assert result["priority_unconfirmed"] == 1
    # NO synthetic guarded-write RuntimeError noise in phase_errors
    assert not any(
        "manual recrawl never writes candidate cache_status" in message
        for message in result["phase_errors"]
    ), result["phase_errors"]
    assert result["phase_errors"] == []
    # the candidate row was never written (the note write was skipped, not
    # failed): byte-identical before/after
    assert prewarm.candidate_by_id(candidate_id) == before
    assert adapter.submit_calls == 0


def test_exact_reused_none_with_terminal_success_flag(tmp_path):
    """D7:同日复用 boost 返回 None(job 状态移出可 boost 集合)时重probe;
    终态 SUCCESS → REUSED 携带 job_terminal_success=True + priority_proven
    None(诚实:任务已完成,无需也无法再 boost)。"""
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-boost-none-terminal.html"
    _paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-nt|number-nt",
    )
    # simulate a state move between probe and boost: the bound crawl row went
    # terminal SUCCESS while the job moved to a non-boostable in-flight state
    connection = sqlite3.connect(str(paths.state_path), isolation_level=None)
    try:
        connection.execute("UPDATE remote_jobs SET state='SUBMIT_UNKNOWN'")
        connection.execute("UPDATE crawl_work SET state='SUCCESS'")
    finally:
        connection.close()
    candidate_id = cache_hit_candidate(prewarm, url)
    adapter = _CountingAdapter(submit_raises=True)

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    assert result["outcome"] == "REUSED"
    assert result["remote_job_id"] == job_id
    assert result["priority_proven"] is None
    assert result["job_terminal_success"] is True
    assert result["cross_day"] is False
    assert result["job_business_date"] == BUSINESS_DATE
    assert adapter.submit_calls == 0
    assert adapter.prioritize_calls == 0
    assert len(_crawl_rows(paths.state_path)) == 1


def test_case_d_priority_unconfirmed_reported_honestly(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/exact-priority.html"
    candidate_id = cache_hit_candidate(prewarm, url)
    adapter = _CountingAdapter(
        priority_outcomes=[LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic="tool died")]
    )

    result = _dispatch(runtime_root, prewarm, cache, candidate_id, adapter=adapter)

    # The create itself succeeded, but the priority was NOT proven: this is
    # never reported as a full success.
    assert result["outcome"] == "CREATED"
    assert result["priority_confirmed"] is False
    job = _state_rows(paths.state_path, "SELECT state, remote_identity FROM remote_jobs")[0]
    assert job["state"] == "PRIORITY_PENDING"
    assert job["remote_identity"] == f"comix|remote-1|{result['task_number']}"
    crawl = _crawl_rows(paths.state_path)[0]
    assert crawl["state"] == "PRIORITY_PENDING"
    assert adapter.submit_calls == 1 and adapter.prioritize_calls == 1
