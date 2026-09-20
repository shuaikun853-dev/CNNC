"""customer_prewarm_dispatch 行为测试(Issue #10 Part B)。

真实 CustomerPrewarmStore + SharedCrawlCache + per-day DailyAuditStore
(tmp_path),注入计数 fake adapter(镜像 tests/test_incremental_audit_runtime
的 _BridgeAdapter 模式:prepare_job 返回属性兼容的 PreparedLegacyJob 替身,
submit/prioritize 返回真实 outcome 数据类)。用例编号对应 Issue #10 验收清单:
Case 1(缓存复核)、4(Staples)、5(Tmall)、6(新任务)、7(复用)、8(幂等
boost)、9(并发)、11(SUBMIT_UNKNOWN)、12(URL 证据别名)、15(缓存同步
待定)、16(优先级失败闭合重试)、17(RUNNING 停滞)、NOT_ATTEMPTED(预发送
失败重试)、预算上限。

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
    find_remote_work_for_url,
    prewarm_crawl_fingerprint,
    prewarm_placeholder_sku,
    run_prewarm_dispatch_pass,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_MANUAL_SKIP,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_REMOTE_REUSED,
    CACHE_STATUS_ROUTING_BLOCKED,
    CACHE_STATUS_STAPLES_PENDING,
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

BUSINESS_DATE = "2026-09-07"

COMIX = ZhaoshangMallMapping(
    mall_name="齐心商城",
    source_id="SRC-COMIX",
    mall_key="comix",
    domains=("qx.com", "www.qx.com"),
    vendor_keywords=("comix",),
)
NBDELI = ZhaoshangMallMapping(
    mall_name="得力商城",
    source_id="SRC-DELI",
    mall_key="nbdeli",
    domains=("b2b.nbdeli.com", "nbdeli.com", "www.nbdeli.com", "nbdeli.cn"),
    vendor_keywords=("nbdeli",),
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
    """Fake adapter with counters, mirroring _BridgeAdapter conventions."""

    def __init__(self, *, submit_outcome=None, priority_outcomes=None):
        self.prepare_calls = 0
        self.submit_calls = 0
        self.prioritize_calls = 0
        self.submit_outcome = submit_outcome or SUBMITTED_SUCCESS
        self.priority_outcomes = list(priority_outcomes or [])
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
        return self.submit_outcome

    def prioritize(self, _job, *, task_id, task_number, timeout_seconds):
        self.prioritize_calls += 1
        if self.priority_outcomes:
            return self.priority_outcomes.pop(0)
        return LegacyPriorityOutcome(PriorityStatus.UPDATED, weight_type=-1)


class _FakeClock:
    def __init__(self, values):
        self.values = list(values)
        self.tail = values[-1]

    def __call__(self):
        if self.values:
            return self.values.pop(0)
        return self.tail


# ---- fixtures and seeders -----------------------------------------------------


def make_paths(runtime_root: Path, business_date: str):
    paths = runtime_paths(business_date, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    return paths


def make_state_db(runtime_root: Path, business_date: str) -> object:
    paths = make_paths(runtime_root, business_date)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(business_date)
    return paths


def make_candidate(prewarm: CustomerPrewarmStore, url: str, *, mall="comix", lookup_url=None, document="D1"):
    upsert = prewarm.upsert_link(document, url, mall=mall, lookup_url=lookup_url or url, business_date=BUSINESS_DATE)
    candidate_id = int(upsert["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
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


def _state_rows(path, query, params=()):
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def _crawl_rows(state_path):
    return _state_rows(state_path, "SELECT * FROM crawl_work ORDER BY id")


def seed_remote_work(
    runtime_root: Path,
    business_date: str,
    adapter: _CountingAdapter,
    *,
    mapping,
    url,
    job_state="SUBMITTED",
    remote_identity=None,
    crawl_success_result=None,
):
    """Seed one bound crawl row + remote job with adapter-consistent evidence."""
    paths = make_paths(runtime_root, business_date)
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
        if crawl_success_result is not None:
            store.complete_crawl_work(token, int(crawl["id"]), crawl_success_result)
        return paths, int(job_row["id"])
    finally:
        store.release_lease(token)
        lock.release()


def seed_terminal_crawl(runtime_root: Path, business_date: str, mapping, url, *, result=None):
    paths = make_paths(runtime_root, business_date)
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
        if result is not None:
            store.complete_crawl_work(token, int(crawl["id"]), result)
        return paths
    finally:
        store.release_lease(token)
        lock.release()


def candidate_by_id(prewarm: CustomerPrewarmStore, candidate_id: int) -> dict:
    return next(row for row in prewarm.candidates() if int(row["id"]) == candidate_id)


# ---- helpers ------------------------------------------------------------------


def test_placeholder_sku_and_fingerprint_are_deterministic():
    sku = prewarm_placeholder_sku("comix", "https://qx.com/a")
    assert sku == prewarm_placeholder_sku("comix", "https://qx.com/a")
    assert sku != prewarm_placeholder_sku("comix", "https://qx.com/b")
    assert sku.startswith("zhpq-") and len(sku) == len("zhpq-") + 16
    fingerprint = prewarm_crawl_fingerprint("comix", "SRC-COMIX", sku, "https://qx.com/a", BUSINESS_DATE)
    assert fingerprint == prewarm_crawl_fingerprint(
        "comix", "SRC-COMIX", sku, "https://qx.com/a", BUSINESS_DATE
    )
    assert fingerprint != prewarm_crawl_fingerprint(
        "comix", "SRC-COMIX", sku, "https://qx.com/a", "2026-09-06"
    )


def test_find_remote_work_missing_db_returns_none(tmp_path):
    assert (
        find_remote_work_for_url(tmp_path / "missing" / "state.sqlite3", "comix", "https://qx.com/a")
        is None
    )


# ---- Case 1: 缓存复核命中(远端调用为零)---------------------------------------------


def test_case1_cache_recheck_hit_makes_zero_remote_calls(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-1.html"
    candidate_id = make_candidate(prewarm, url)
    cache.record_success(mall="comix", submitted_url=url, source="daily-internet", price="1.1")
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["outcome"] == "OK"
    assert summary["cache_hit_after_recheck"] == 1
    assert summary["jobs_created"] == 0
    assert adapter.submit_calls == 0 and adapter.prepare_calls == 0
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_CACHE_HIT
    assert _crawl_rows(paths.state_path) == []


# ---- absorb:REMOTE_* 候选在缓存行落地后翻 CACHE_HIT ----------------------------------


def test_absorb_phase_flips_remote_dispatched_to_cache_hit(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-absorb.html"
    candidate_id = make_candidate(prewarm, url)
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=55
    )
    cache.record_success(mall="comix", submitted_url=url, source="daily-internet", price="2.2")
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["absorbed"] == 1
    assert adapter.submit_calls == 0 and adapter.prioritize_calls == 0
    absorbed = candidate_by_id(prewarm, candidate_id)
    assert absorbed["cache_status"] == CACHE_STATUS_CACHE_HIT
    # remote_job_id 溯源保留
    assert absorbed["remote_job_id"] == 55
    assert _crawl_rows(paths.state_path) == []


# ---- Case 4 / Case 5:Staples 与 Tmall 路由 -----------------------------------------


def test_case4_staples_candidate_skipped_without_remote_work(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(prewarm, "https://www.stbchina.cn/item/4.html")
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["staples_skipped"] == 1
    assert summary["jobs_created"] == 0
    assert adapter.submit_calls == 0 and adapter.prioritize_calls == 0
    row = candidate_by_id(prewarm, candidate_id)
    assert row["cache_status"] == CACHE_STATUS_STAPLES_PENDING
    assert row["last_error"] is None
    assert _crawl_rows(paths.state_path) == []


def test_case5_tmall_detail_manual_skip_without_error_text(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(prewarm, "https://detail.tmall.com/item.htm?id=5")
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["manual_skipped"] == 1
    assert summary["jobs_created"] == 0
    assert adapter.submit_calls == 0 and adapter.prioritize_calls == 0
    row = candidate_by_id(prewarm, candidate_id)
    assert row["cache_status"] == CACHE_STATUS_MANUAL_SKIP
    assert row["last_error"] is None
    assert _crawl_rows(paths.state_path) == []


def test_unmapped_candidate_recorded_routing_blocked(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(prewarm, "https://unknown.example/item/1")
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["routing_blocked"] == 1
    row = candidate_by_id(prewarm, candidate_id)
    assert row["cache_status"] == CACHE_STATUS_ROUTING_BLOCKED
    assert "unmapped mall: unknown.example" == row["last_error"]
    assert _crawl_rows(paths.state_path) == []


# ---- Case 6:新任务创建 + 提交 -------------------------------------------------------


def test_routing_blocked_candidate_reclassified_after_mapping_available(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-reclassified.html"
    candidate_id = make_candidate(prewarm, url)
    prewarm.record_candidate_result(
        candidate_id,
        cache_status=CACHE_STATUS_ROUTING_BLOCKED,
        error="unmapped mall: qx.com",
    )
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["routing_blocked"] == 0
    assert summary["mapped_zhaoshang"] == 1
    assert summary["jobs_created"] == 1
    row = candidate_by_id(prewarm, candidate_id)
    assert row["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert row["remote_job_id"] is not None
    assert adapter.submit_calls == 1
    assert len(_crawl_rows(paths.state_path)) == 1



@pytest.mark.parametrize("terminal_probe", [False, True])
def test_posted_link_with_pending_candidate_cannot_create_again_next_day(tmp_path, monkeypatch, terminal_probe):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(prewarm, "https://qx.com/post-link-crash.html")
    adapter = _CountingAdapter()
    original_record = prewarm.record_candidate_result
    failed = False

    def fail_after_link(candidate, *, cache_status, **kwargs):
        nonlocal failed
        if candidate == candidate_id and cache_status == CACHE_STATUS_REMOTE_DISPATCHED and not failed:
            failed = True
            raise sqlite3.OperationalError("candidate write interrupted after remote POST")
        return original_record(candidate, cache_status=cache_status, **kwargs)

    monkeypatch.setattr(prewarm, "record_candidate_result", fail_after_link)
    run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert adapter.submit_calls == 1
    assert prewarm.candidate_by_id(candidate_id)["cache_status"] == CACHE_STATUS_PENDING_PREWARM
    assert prewarm.remote_job_link_for_candidate(candidate_id)["business_date"] == BUSINESS_DATE

    next_day = "2026-09-08"
    next_paths = make_state_db(runtime_root, next_day)
    if terminal_probe:
        with sqlite3.connect(next_paths.state_path) as connection:
            run_id = connection.execute("SELECT id FROM daily_runs LIMIT 1").fetchone()[0]
            connection.execute(
                """INSERT INTO crawl_work
                   (run_id, mall_key, source_id, sku_id, exact_url, backend_version,
                    fingerprint, state, updated_at)
                   VALUES (?, 'comix', 'SRC-COMIX', ?, ?, ?, 'terminal-probe',
                           'TERMINAL_FAILED', '2026-09-08T00:00:00+00:00')""",
                (run_id, prewarm_placeholder_sku("comix", "https://qx.com/post-link-crash.html"),
                 "https://qx.com/post-link-crash.html", BACKEND_VERSION),
            )
    planned = run_prewarm_dispatch_pass(
        prewarm, cache, business_date=next_day, runtime_root=runtime_root,
        mappings=[COMIX], adapter=adapter, plan_only=True,
    )
    assert planned["new_remote_urls"] == 0
    run_prewarm_dispatch_pass(
        prewarm, cache, business_date=next_day, runtime_root=runtime_root,
        mappings=[COMIX], adapter=adapter,
    )
    assert adapter.submit_calls == 1
    assert prewarm.candidate_by_id(candidate_id)["cache_status"] == CACHE_STATUS_PENDING_PREWARM


def test_missing_link_cannot_prioritize_same_numbered_job_next_day(tmp_path):
    runtime_root = tmp_path / "runtime"
    next_day = "2026-09-08"
    paths, job_id = seed_remote_work(
        runtime_root, next_day, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/unrelated-day-two.html",
        job_state="PRIORITY_PENDING", remote_identity="comix|other-task|other-number",
    )
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(prewarm, "https://qx.com/day-one-no-link.html")
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=job_id,
    )
    adapter = _CountingAdapter()
    run_prewarm_dispatch_pass(
        prewarm, cache, business_date=next_day, runtime_root=runtime_root,
        mappings=[COMIX], adapter=adapter,
    )
    assert adapter.prioritize_calls == 0


def test_case6_new_task_created_submitted_once(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-6.html"
    candidate_id = make_candidate(prewarm, url)
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["outcome"] == "OK"
    assert summary["jobs_created"] == 1
    assert summary["new_remote_urls"] == 1
    assert summary["batch_sizes"] == [1]
    assert summary["by_mall"] == {"comix": {"reused": 0, "created_urls": 1}}
    # Finding 3:SUBMITTED 成功后先 PRIORITY_PENDING,同一 pass 内 prioritize
    # 证明 weight_type == -1 才 reconcile 回 SUBMITTED。
    assert summary["priority_confirmed"] == 1
    assert adapter.submit_calls == 1
    assert adapter.prioritize_calls == 1

    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert candidate["remote_job_id"] is not None

    crawls = _crawl_rows(paths.state_path)
    assert len(crawls) == 1
    crawl = crawls[0]
    assert crawl["sku_id"] == prewarm_placeholder_sku("comix", url)
    assert crawl["sku_id"].startswith("zhpq-")
    assert crawl["source_id"] == "SRC-COMIX"
    assert crawl["mall_key"] == "comix"
    assert crawl["backend_version"] == BACKEND_VERSION
    assert crawl["state"] == "SUBMITTED"

    jobs = _state_rows(paths.state_path, "SELECT * FROM remote_jobs")
    assert len(jobs) == 1
    job = jobs[0]
    assert re.fullmatch(r"cnnc_current_opt_.+_\d{14}", job["task_name"])
    assert job["state"] == "SUBMITTED"
    assert job["remote_identity"] == "comix|remote-1|number-1"
    assert int(job["id"]) == int(candidate["remote_job_id"])
    payload = json.loads(job["payload_json"])
    assert payload["requests"][0]["url"] == url
    assert payload["requests"][0]["sku_id"] == crawl["sku_id"]


# ---- Case 7:已有未完成任务复用 + boost ----------------------------------------------


def test_case7_existing_bound_job_reused_and_boosted(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-7.html"
    _seeder_paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-7|number-7",
    )
    candidate_id = make_candidate(prewarm, url)
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["outcome"] == "OK"
    assert summary["existing_remote_reused"] == 1
    assert summary["jobs_reused"] == 1
    assert summary["jobs_created"] == 0
    assert summary["priority_boosted"] == 1
    assert adapter.submit_calls == 0
    # 一次 prepare = 从持久 payload 重建 prepared job(而非新建)
    assert adapter.prepare_calls == 1
    assert adapter.prioritize_calls == 1

    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_REUSED
    assert int(candidate["remote_job_id"]) == job_id
    assert len(_crawl_rows(paths.state_path)) == 1


# ---- Case 8:boost 幂等(evidence replay)--------------------------------------------


def test_case8_boost_is_idempotent_already_highest(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-8.html"
    seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-8|number-8",
    )
    candidate_id = make_candidate(prewarm, url)
    adapter = _CountingAdapter(
        priority_outcomes=[LegacyPriorityOutcome(PriorityStatus.ALREADY_HIGHEST, weight_type=-1)]
    )

    first = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert first["priority_boosted"] == 1
    assert first["phase_errors"] == []

    second = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert second["priority_boosted"] == 1
    assert second["phase_errors"] == []
    assert adapter.prioritize_calls == 2
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_REMOTE_REUSED


# ---- Case 9:并发 ------------------------------------------------------------------


def test_case9a_instance_lock_skips_without_any_call(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(prewarm, "https://qx.com/product-9a.html")
    adapter = _CountingAdapter()
    lock = ProcessLock(runtime_root / DISPATCH_LOCK_NAME)
    lock.acquire()
    try:
        summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    finally:
        lock.release()

    assert summary["outcome"] == "SKIPPED_ALREADY_RUNNING"
    assert adapter.submit_calls == 0
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_PENDING_PREWARM
    assert _crawl_rows(paths.state_path) == []


def test_case9b_claim_race_second_writer_skips_submit(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-9b.html"
    candidate_id = make_candidate(prewarm, url)
    writer_a = _CountingAdapter(
        submit_outcome=LegacySubmitOutcome(SubmitStatus.NOT_ATTEMPTED, diagnostic="pre-send blocked")
    )

    first = run_pass(runtime_root, prewarm, cache, adapter=writer_a)
    assert first["submit_not_attempted"] == 1
    assert writer_a.submit_calls == 1
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_PENDING_PREWARM

    # writer B 在 writer A 的预发送失败后抢先烧掉 claim(模拟双写者竞态)。
    store = DailyAuditStore(paths.state_path)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    try:
        token_b = store.acquire_lease(BUSINESS_DATE, "writer-b", 120.0)
        try:
            jobs = store.list_remote_jobs(token_b)
            assert len(jobs) == 1
            claim = store.claim_remote_submission(token_b, int(jobs[0]["id"]))
            assert claim is not None
        finally:
            store.release_lease(token_b)
    finally:
        lock.release()

    writer_b = _CountingAdapter()
    second = run_pass(runtime_root, prewarm, cache, adapter=writer_b)
    assert second["outcome"] == "OK"
    # 第二个写者发现 claim 已烧:绝不二次 POST。
    assert writer_b.submit_calls == 0
    assert writer_a.submit_calls + writer_b.submit_calls == 1
    assert len(_crawl_rows(paths.state_path)) == 1
    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert candidate["remote_job_id"] is not None


# ---- Case 11:SUBMIT_UNKNOWN 只对账,永不重建 ------------------------------------------


def test_case11_submit_unknown_never_recreated(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-11.html"
    candidate_id = make_candidate(prewarm, url)
    adapter = _CountingAdapter(
        submit_outcome=LegacySubmitOutcome(
            SubmitStatus.SUBMIT_UNKNOWN,
            diagnostic="timeout Authorization: Bearer TEST_TOKEN_VALUE",
        )
    )

    first = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert first["submit_unknown"] == 1
    submit_notes = [note for note in first["phase_errors"] if "SUBMIT_UNKNOWN" in note]
    assert len(submit_notes) == 1
    assert "job=1" in submit_notes[0]
    assert "mall=comix" in submit_notes[0]
    assert "task_name=" in submit_notes[0]
    assert "timeout" in submit_notes[0]
    assert "TEST_TOKEN_VALUE" not in submit_notes[0]
    assert "[REDACTED]" in submit_notes[0]
    assert adapter.submit_calls == 1
    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert "submit unknown" in (candidate["last_error"] or "")
    job = _state_rows(
        paths.state_path, "SELECT state, remote_identity, error_text FROM remote_jobs"
    )[0]
    assert job["state"] == "SUBMIT_UNKNOWN"
    assert "timeout" in job["error_text"]
    assert "TEST_TOKEN_VALUE" not in job["error_text"]

    # 第二轮:SUBMIT_UNKNOWN 是未完成绑定 → 复用,不重建、不重复提交。
    second = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert second["outcome"] == "OK"
    assert adapter.submit_calls == 1
    assert len(_crawl_rows(paths.state_path)) == 1
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED


# ---- Case 12:URL 证据别名吸收 -------------------------------------------------------


def test_case12_alias_evidence_absorbs_original_lookup_url(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    original = "https://www.jslink.com/goods/900.html"
    lookup = "https://b2b.nbdeli.com/goods/900.html"
    candidate_id = make_candidate(prewarm, original, mall="nbdeli", lookup_url=lookup)
    adapter = _CountingAdapter(
        submit_outcome=LegacySubmitOutcome(
            SubmitStatus.SUBMITTED,
            task_id="remote-12",
            task_number="number-12",
            priority_evidence="updated",
        )
    )

    first = run_pass(runtime_root, prewarm, cache, mappings=(NBDELI,), adapter=adapter)
    assert first["jobs_created"] == 1
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED

    # Phase A 同步形态:submitted=b2b 重写域,original=jslink 原始域,returned=C。
    cache.record_success(
        mall="nbdeli",
        submitted_url=lookup,
        original_url=original,
        returned_url=lookup + "?from=crawl",
        source="daily-internet",
        price="3.3",
    )

    second = run_pass(runtime_root, prewarm, cache, mappings=(NBDELI,), adapter=adapter)
    assert second["absorbed"] == 1
    absorbed = candidate_by_id(prewarm, candidate_id)
    assert absorbed["cache_status"] == CACHE_STATUS_CACHE_HIT
    assert absorbed["match_level"] == "EXACT"
    assert len(_crawl_rows(paths.state_path)) == 1
    assert adapter.submit_calls == 1


# ---- Case 15:终态成功但缓存未同步 ----------------------------------------------------


def test_case15_terminal_success_waits_for_cache_sync_then_absorbs(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-15.html"
    seed_terminal_crawl(
        runtime_root,
        BUSINESS_DATE,
        COMIX,
        url,
        result={"page_price_status": "OK", "page_price_value": "9.9"},
    )
    candidate_id = make_candidate(prewarm, url)
    adapter = _CountingAdapter()

    first = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert first["jobs_created"] == 0
    assert adapter.submit_calls == 0
    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert "cache sync pending" in (candidate["last_error"] or "")
    assert len(_crawl_rows(paths.state_path)) == 1  # 绝不重建

    # 模拟 Phase A 缓存同步落地。
    cache.record_success(mall="comix", submitted_url=url, source="zhaoshang-history", price="9.9")

    second = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert second["absorbed"] == 1
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_CACHE_HIT


# ---- Case 16:优先级确认失败闭合,下一轮重试 --------------------------------------------


def test_case16_priority_unconfirmed_fails_closed_and_retries(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-16.html"
    seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|remote-16|number-16",
    )
    candidate_id = make_candidate(prewarm, url)
    adapter = _CountingAdapter(
        priority_outcomes=[LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, diagnostic="tool died")]
    )

    first = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert first["outcome"] == "OK"
    assert first["priority_unconfirmed"] == 1
    assert first["priority_boosted"] == 0
    assert adapter.prioritize_calls == 1
    # 同状态保持(闭合失败,不丢任务也不伪造成功)。
    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_REUSED
    assert candidate["remote_job_id"] is not None

    second = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert second["priority_boosted"] == 1
    assert adapter.prioritize_calls == 2  # 下一轮重试
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_REMOTE_REUSED


# ---- Case 17:RUNNING 停滞任务绝不重建/重复提交 ----------------------------------------


def test_case17_running_stall_never_creates_or_submits(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-17.html"
    seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="RUNNING",
        remote_identity="comix|remote-17|number-17",
    )
    candidate_id = make_candidate(prewarm, url)
    adapter = _CountingAdapter()

    first = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    second = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert adapter.submit_calls == 0
    assert first["jobs_created"] == 0 and second["jobs_created"] == 0
    assert first["existing_remote_reused"] == 1
    assert len(_crawl_rows(paths.state_path)) == 1
    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_REUSED
    # 停滞任务每轮仍会做一次幂等 boost(evidence replay),但绝不重复提交/创建。
    assert adapter.prioritize_calls == 2


# ---- NOT_ATTEMPTED:预发送失败留在 PENDING_PREWARM 并重试 ------------------------------


def test_not_attempted_pre_send_failure_stays_pending_and_replans(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-na.html"
    candidate_id = make_candidate(prewarm, url)
    adapter = _CountingAdapter(
        submit_outcome=LegacySubmitOutcome(SubmitStatus.NOT_ATTEMPTED, diagnostic="runner offline")
    )

    first = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert first["submit_not_attempted"] == 1
    assert first["phase_errors"]
    # 预发送失败 = 没有派发发生:候选如实留在 PENDING_PREWARM。
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_PENDING_PREWARM
    job = _state_rows(paths.state_path, "SELECT state, post_attempted FROM remote_jobs")[0]
    assert job["state"] == "SUBMIT_INTENT"
    assert int(job["post_attempted"]) == 0

    second = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert second["submit_not_attempted"] == 1
    assert adapter.submit_calls == 2  # 两轮都是预发送失败,均可重试
    # 幂等意图:crawl 行不重复。
    assert len(_crawl_rows(paths.state_path)) == 1
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_PENDING_PREWARM


# ---- 预算上限:组间耗尽则后续商场顺延 --------------------------------------------------


def test_budget_exhaustion_between_groups_defers_remaining(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    make_candidate(prewarm, "https://qx.com/product-b1.html", document="D1")
    make_candidate(
        prewarm,
        "https://b2b.nbdeli.com/goods/b2.html",
        mall="nbdeli",
        lookup_url="https://b2b.nbdeli.com/goods/b2.html",
        document="D2",
    )
    adapter = _CountingAdapter()

    summary = run_prewarm_dispatch_pass(
        prewarm,
        cache,
        business_date=BUSINESS_DATE,
        runtime_root=runtime_root,
        mappings=[COMIX, NBDELI],
        adapter=adapter,
        budget_seconds=50.0,
        # 第 3 个读数是 claim 前预算地板检查(仍充足),第 4 个是 claim 后的超时读数。
        monotonic=_FakeClock([0.0, 0.0, 0.0, 100.0]),
    )

    assert summary["outcome"] == "OK"
    assert summary["jobs_created"] == 1
    assert summary["new_remote_urls"] == 1
    assert summary["batch_sizes"] == [1]
    assert adapter.submit_calls == 1
    assert any("budget deadline reached" in item for item in summary["phase_errors"])


# ---- 预算地板:claim 前预算耗尽则不烧 claim,留待下一轮恢复 -----------------------------


def test_budget_floor_before_claim_keeps_intent_and_resumes_next_pass(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-floor.html"
    candidate_id = make_candidate(prewarm, url)
    adapter = _CountingAdapter()

    # 时钟序列:deadline 初始化 0.0 → 组级检查 0.0(通过)→ claim 前地板检查
    # 46.0(剩余 4.0 < SUBMIT_MIN_SECONDS)。组级检查看不到这次窗口内耗尽,
    # 地板必须兜住:绝不给真实适配器 ~0.001s 超时烧出假 SUBMIT_UNKNOWN。
    first = run_pass(
        runtime_root,
        prewarm,
        cache,
        adapter=adapter,
        budget_seconds=50.0,
        monotonic=_FakeClock([0.0, 0.0, 46.0]),
    )

    assert first["outcome"] == "OK"
    assert adapter.submit_calls == 0
    # claim 未烧:远端任务行保持 SUBMIT_INTENT 且 post_attempted == 0。
    job = _state_rows(paths.state_path, "SELECT state, post_attempted FROM remote_jobs")[0]
    assert job["state"] == "SUBMIT_INTENT"
    assert int(job["post_attempted"]) == 0
    # 没有派发发生:候选如实留在 PENDING_PREWARM。
    assert candidate_by_id(prewarm, candidate_id)["cache_status"] == CACHE_STATUS_PENDING_PREWARM
    assert any("budget floor" in item for item in first["phase_errors"])

    # 第二轮正常预算:幂等意图链恢复 —— 提交一次,任务 SUBMITTED。
    second = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert second["outcome"] == "OK"
    assert adapter.submit_calls == 1
    jobs = _state_rows(
        paths.state_path, "SELECT id, state, post_attempted, remote_identity FROM remote_jobs"
    )
    assert len(jobs) == 1
    assert jobs[0]["state"] == "SUBMITTED"
    assert int(jobs[0]["post_attempted"]) == 1
    assert jobs[0]["remote_identity"] == "comix|remote-1|number-1"
    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert int(candidate["remote_job_id"]) == int(jobs[0]["id"])
    assert len(_crawl_rows(paths.state_path)) == 1  # 幂等意图:crawl 行不重复


# ---- Finding 3(Round 2):新任务的 weight_type=-1 必须被证明 --------------------------

SUBMITTED_NO_PRIORITY = LegacySubmitOutcome(
    SubmitStatus.SUBMITTED, task_id="remote-np", task_number="number-np"
)


def test_new_job_priority_confirmed_in_same_pass(tmp_path):
    """submit 无优先级证据 → 状态先落 PRIORITY_PENDING;同一 pass 内 prioritize
    证明 weight_type == -1 后才 reconcile 为 SUBMITTED(语义而非仅事件)。"""
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(prewarm, "https://qx.com/product-pf.html")
    adapter = _CountingAdapter(submit_outcome=SUBMITTED_NO_PRIORITY)

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["outcome"] == "OK"
    assert summary["jobs_created"] == 1
    assert summary["priority_confirmed"] == 1
    assert summary["priority_unconfirmed"] == 0
    assert adapter.submit_calls == 1 and adapter.prioritize_calls == 1
    job = _state_rows(paths.state_path, "SELECT state, remote_identity FROM remote_jobs")[0]
    assert job["state"] == "SUBMITTED"  # 被证明后才允许 SUBMITTED
    assert job["remote_identity"] == "comix|remote-np|number-np"
    crawl = _crawl_rows(paths.state_path)[0]
    assert crawl["state"] == "SUBMITTED"
    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert candidate["remote_job_id"] is not None


def test_new_job_priority_transient_failure_retries_without_recreate(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(prewarm, "https://qx.com/product-pt.html")
    adapter = _CountingAdapter(
        submit_outcome=SUBMITTED_NO_PRIORITY,
        priority_outcomes=[
            LegacyPriorityOutcome(
                PriorityStatus.UNCONFIRMED, diagnostic="tool died", retryable=True
            )
        ],
    )

    first = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert first["priority_unconfirmed"] == 1
    assert first["priority_confirmed"] == 0
    assert adapter.submit_calls == 1 and adapter.prioritize_calls == 1
    job = _state_rows(paths.state_path, "SELECT state FROM remote_jobs")[0]
    assert job["state"] == "PRIORITY_PENDING"  # 闭合失败:绝不伪造成 SUBMITTED
    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert "priority pending confirmation" in (candidate["last_error"] or "")

    # 第二轮:绝不重建/重提交,仅重试 prioritize;证明后 reconcile SUBMITTED。
    second = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert second["outcome"] == "OK"
    assert second["jobs_created"] == 0
    assert second["priority_confirmed"] == 1
    assert adapter.submit_calls == 1  # 跨两轮仅一次 POST
    # prepare 第 1 次 = 创建链,第 2 次 = 第 2 轮从持久 payload 重建(非新建,
    # 与 Case 7 的重建语义相同);crawl 行不重复才是"绝不重建"的硬证据。
    assert adapter.prepare_calls == 2
    assert adapter.prioritize_calls == 2
    job = _state_rows(paths.state_path, "SELECT state FROM remote_jobs")[0]
    assert job["state"] == "SUBMITTED"
    assert len(_crawl_rows(paths.state_path)) == 1


def test_new_job_priority_already_highest_confirmed_idempotent(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    make_candidate(prewarm, "https://qx.com/product-pah.html")
    adapter = _CountingAdapter(
        submit_outcome=SUBMITTED_NO_PRIORITY,
        priority_outcomes=[LegacyPriorityOutcome(PriorityStatus.ALREADY_HIGHEST, weight_type=-1)],
    )

    first = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert first["priority_confirmed"] == 1
    assert first["phase_errors"] == []
    job = _state_rows(paths.state_path, "SELECT state FROM remote_jobs")[0]
    assert job["state"] == "SUBMITTED"

    # 第二轮:任务已 SUBMITTED 且 -1 已证明 → 确认幂等,不重复 prioritize。
    second = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert second["priority_confirmed"] == 0
    assert second["phase_errors"] == []
    assert adapter.prioritize_calls == 1 and adapter.submit_calls == 1
    job = _state_rows(paths.state_path, "SELECT state FROM remote_jobs")[0]
    assert job["state"] == "SUBMITTED"


def test_new_job_priority_readback_not_negative_never_confirmed(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(prewarm, "https://qx.com/product-pr.html")
    adapter = _CountingAdapter(
        submit_outcome=SUBMITTED_NO_PRIORITY,
        priority_outcomes=[
            LegacyPriorityOutcome(
                PriorityStatus.UPDATED, weight_type=0, diagnostic="readback weight_type=0"
            )
        ],
    )

    first = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    # status=UPDATED 但 weight_type != -1:不算证明,任务必须留在 PRIORITY_PENDING。
    assert first["priority_confirmed"] == 0
    assert first["priority_unconfirmed"] == 1
    job = _state_rows(paths.state_path, "SELECT state FROM remote_jobs")[0]
    assert job["state"] == "PRIORITY_PENDING"
    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert "priority pending confirmation" in (candidate["last_error"] or "")

    # 下一轮行为不变:同一确认路径重试,证明后 SUBMITTED,绝不重建。
    second = run_pass(runtime_root, prewarm, cache, adapter=adapter)
    assert second["priority_confirmed"] == 1
    assert adapter.submit_calls == 1
    job = _state_rows(paths.state_path, "SELECT state FROM remote_jobs")[0]
    assert job["state"] == "SUBMITTED"


def test_remote_dispatched_priority_pending_job_confirmed_without_resubmit(tmp_path):
    """REMOTE_DISPATCHED + PRIORITY_PENDING(带身份)每轮重进 prioritize-only
    确认列表:绝不重建、绝不重复提交(prepare 仅为持久 payload 重建)。"""
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-rd.html"
    _seed_paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="PRIORITY_PENDING",
        remote_identity="comix|remote-rd|number-rd",
    )
    candidate_id = make_candidate(prewarm, url)
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=job_id
    )
    adapter = _CountingAdapter()

    summary = run_pass(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["outcome"] == "OK"
    assert adapter.submit_calls == 0
    assert adapter.prepare_calls == 1  # 仅 payload 重建,不是新任务
    assert adapter.prioritize_calls == 1
    assert summary["priority_confirmed"] == 1
    job = _state_rows(
        paths.state_path, "SELECT state, remote_identity FROM remote_jobs WHERE id=?", (job_id,)
    )[0]
    assert job["state"] == "SUBMITTED"
    assert job["remote_identity"] == "comix|remote-rd|number-rd"
    crawl = _crawl_rows(paths.state_path)[0]
    assert crawl["state"] == "SUBMITTED"
