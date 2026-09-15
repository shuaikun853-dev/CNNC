"""Issue #10(招商局 dispatcher/canary)与 Issue #11(史泰博本地预爬)combined contract tests。

同一个 CustomerPrewarmStore 数据库上两条预爬路径的合并合同:
- Case A:非 Staples 候选只归 #10——#11 pass 整行不动,#10 pass 进入 remote lifecycle;
- Case B:Staples 绝不进 #10 dispatcher——零 remote 交互,由 #11 完成 STAPLES_PENDING→CACHE_HIT;
- Case C:CACHE_HIT 是永久终态——#10/#11 任一 pass 都不再触碰;
- Case D:旧 #9 schema 的 additive migration——remote_job_id 列 + staples sidecar 两表补齐,旧行逐字段不变;
- Case E:plan-only 零写——旧 schema 库上两个 plan-only 先后运行,schema 与文件字节均不变;
- Case F:同 DB 共存互不侵犯——A(#10 remote)与 B(#11 local)状态/remote_job_id/sidecar/缓存身份互不污染;
- Case G:STEP1 隔离——混合 pending 状态(REMOTE_DISPATCHED / STAPLES_PENDING+LOCAL_SUCCESS_CACHE_PENDING)
  不阻塞 closeout→FINAL。

全部走公共 seam(``run_staples_prewarm`` / ``run_prewarm_dispatch_pass`` /
``staples_plan_readonly``),fake adapter / fake crawler,零真实远端、零真实爬取。
P0 哨兵:毒化全部招商局模块,任何路径触碰即失败。
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

import app.services.incremental_audit_runtime as runtime_module
from app.services.crawl_url_identity import (
    SKU_STATUS_CONFIRMED,
    SKU_STATUS_UPSTREAM_PROVIDED,
    SKU_STATUS_URL_PARSED_UNVERIFIED,
)
from app.services.customer_prewarm_dispatch import run_prewarm_dispatch_pass
from app.services.customer_prewarm_discovery import prewarm_rows_from_customer_rows
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_STAPLES_PENDING,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_closeout import CloseoutConfig, run_closeout
from app.services.incremental_audit_runtime import runtime_paths
from app.services.legacy_zhaoshang_adapter import (
    LegacyPriorityOutcome,
    LegacySubmitOutcome,
    PriorityStatus,
    SubmitStatus,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from app.services.staples_prewarm import (
    STAPLES_CACHE_MALL_KEY,
    STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING,
    STAPLES_JOB_STATE_PENDING,
    STAPLES_JOB_STATE_SUCCESS,
    STAPLES_OUTCOME_SUCCESS,
    StaplesCrawlOutcome,
    StaplesPrewarmStore,
    run_staples_prewarm,
    staples_plan_readonly,
)
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
)
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

# ---- P0 哨兵:毒化全部招商局模块入口(照抄 Issue #11 模式) ---------------------

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


# ---- 测试基建(#11 侧:stack fixture / FakeCrawler / outcome) -------------------

T0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
BUSINESS_DATE = "2026-09-08"
UID = "1001358701037447"
STAPLES_URL = f"https://www.stbchina.cn/item.html?itemId={UID}"
COMIX_URL = "https://qx.com/product-int-1.html"

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


def _t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


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


@pytest.fixture()
def stack(tmp_path):
    """#11 侧三件套:同一 DB 承载 prewarm + staples sidecar,缓存独立文件。"""
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
        tmp_path=tmp_path,
    )


def _seed_candidate(prewarm, url, document_id, *, mall,
                    cache_status=CACHE_STATUS_PENDING_PREWARM):
    upserted = prewarm.upsert_link(document_id, url, mall=mall)
    candidate_id = int(upserted["candidate"]["id"])
    if cache_status is not None and cache_status != CACHE_STATUS_PENDING_PREWARM:
        # PENDING_LOOKUP 只能先到 PENDING_PREWARM(store tests 的 _candidate_in_status 同法)
        prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    if cache_status is not None:
        prewarm.record_candidate_result(candidate_id, cache_status=cache_status)
    return candidate_id


def _run_staples_pass(stack, crawler, now=T0, **kwargs):
    return run_staples_prewarm(
        prewarm_store=stack.prewarm,
        shared_cache=stack.shared,
        staples_store=stack.staples,
        crawler=crawler,
        work_dir=stack.work_dir,
        now=now,
        **kwargs,
    )


# ---- 测试基建(#10 侧:fake adapter / state db,照抄 dispatch 测试模式) --------


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
    """Attribute-compatible PreparedLegacyJob stand-in(镜像 dispatch 测试)。"""

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
    """计数 fake adapter(duck-typed prepare_job/submit/prioritize)。"""

    def __init__(self, *, submit_outcome=None):
        self.prepare_calls = 0
        self.submit_calls = 0
        self.prioritize_calls = 0
        self.submit_outcome = submit_outcome or SUBMITTED_SUCCESS
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
        return LegacyPriorityOutcome(PriorityStatus.UPDATED, weight_type=-1)


def make_state_db(runtime_root: Path, business_date: str):
    """#10 live pass 前置:per-day state db + run(照抄 dispatch 测试)。"""
    paths = runtime_paths(business_date, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(business_date)
    return paths


def _dispatch_pass(prewarm, cache, runtime_root, adapter, **kwargs):
    return run_prewarm_dispatch_pass(
        prewarm,
        cache,
        business_date=BUSINESS_DATE,
        runtime_root=runtime_root,
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


def _crawl_rows(state_path):
    return _state_rows(state_path, "SELECT * FROM crawl_work ORDER BY id")


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _schema_snapshot(path):
    """(sqlite_master 对象全集, 每表列名集合) 快照,用于 Case E/D 零变更断言。"""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        objects = sorted(
            (row["type"], row["name"])
            for row in connection.execute("SELECT type, name FROM sqlite_master").fetchall()
        )
        tables = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        columns = {
            name: sorted(
                col["name"] for col in connection.execute(f"PRAGMA table_info({name})").fetchall()
            )
            for name in tables
        }
        return objects, columns
    finally:
        connection.close()


# ---- Case A:非 Staples 候选只归 #10 --------------------------------------------


def test_case_a_non_staples_candidate_owned_by_issue10_only(stack, tmp_path):
    """同一 DB:先 #11 pass(非 Staples 整行不动、无 sidecar 作业),再 #10 pass
    进入 remote lifecycle(REMOTE_DISPATCHED + remote_job_id 记录)。"""
    candidate_id = _seed_candidate(stack.prewarm, COMIX_URL, "D1", mall="comix")
    before = stack.prewarm.candidate_by_id(candidate_id)

    crawler = FakeCrawler()
    staples_summary = _run_staples_pass(stack, crawler)
    assert crawler.calls == 0
    assert staples_summary["routed"] == 0
    assert stack.staples.jobs() == []
    # #11 pass 对非 Staples 候选零写入:整行不变
    assert stack.prewarm.candidate_by_id(candidate_id) == before

    paths = make_state_db(tmp_path / "runtime", BUSINESS_DATE)
    adapter = _CountingAdapter()
    summary = _dispatch_pass(stack.prewarm, stack.shared, tmp_path / "runtime", adapter)

    assert summary["outcome"] == "OK"
    assert summary["jobs_created"] == 1
    assert summary["priority_confirmed"] == 1
    assert adapter.submit_calls == 1 and adapter.prioritize_calls == 1

    row = stack.prewarm.candidate_by_id(candidate_id)
    assert row["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert row["remote_job_id"] is not None
    jobs = _state_rows(paths.state_path, "SELECT id, state, remote_identity FROM remote_jobs")
    assert len(jobs) == 1
    assert jobs[0]["state"] == "SUBMITTED"
    assert jobs[0]["remote_identity"] == "comix|remote-1|number-1"
    assert int(jobs[0]["id"]) == int(row["remote_job_id"])
    crawls = _crawl_rows(paths.state_path)
    assert len(crawls) == 1 and crawls[0]["state"] == "SUBMITTED"
    assert ZHAOSHANG_INVOCATIONS == 0


# ---- Case B:Staples 绝不进 #10 dispatcher ---------------------------------------


def test_case_b_staples_candidate_never_enters_dispatcher(stack, tmp_path):
    """stbchina 候选跑 #10 pass:零 adapter 交互、remote_job_id 为 NULL、无
    crawl 行;#10 只把它移交 STAPLES_PENDING(dispatch Case4 真实语义),随后
    #11 pass 在同一 pass 内补 sidecar 作业并完成 STAPLES_PENDING→CACHE_HIT+SUCCESS。"""
    candidate_id = _seed_candidate(stack.prewarm, STAPLES_URL, "D1", mall=STAPLES_CACHE_MALL_KEY)
    paths = make_state_db(tmp_path / "runtime", BUSINESS_DATE)
    adapter = _CountingAdapter()

    summary = _dispatch_pass(stack.prewarm, stack.shared, tmp_path / "runtime", adapter)

    assert summary["staples_skipped"] == 1
    assert summary["jobs_created"] == 0
    assert adapter.prepare_calls == 0
    assert adapter.submit_calls == 0 and adapter.prioritize_calls == 0
    row = stack.prewarm.candidate_by_id(candidate_id)
    # 合并树真实移交态:#10 dispatch pass 把 stbchina 候选翻成 STAPLES_PENDING
    # (tests/test_customer_prewarm_dispatch.py::test_case4 语义),绝不进 remote lifecycle。
    assert row["cache_status"] == CACHE_STATUS_STAPLES_PENDING
    assert row["remote_job_id"] is None
    assert _crawl_rows(paths.state_path) == []
    assert stack.staples.jobs() == []  # #10 绝不建 sidecar 作业

    crawler = FakeCrawler(outcomes={STAPLES_URL: _success_outcome(STAPLES_URL)})
    staples_summary = _run_staples_pass(stack, crawler)
    assert crawler.calls == 1
    assert staples_summary["jobs_claimed"] == 1
    assert staples_summary["cache_hit_after_crawl"] == 1
    row = stack.prewarm.candidate_by_id(candidate_id)
    assert row["cache_status"] == CACHE_STATUS_CACHE_HIT
    assert row["remote_job_id"] is None  # 全程无 #10 溯源
    job = stack.staples.job_for_candidate(candidate_id)
    assert job["state"] == STAPLES_JOB_STATE_SUCCESS
    entry = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, STAPLES_URL)["entry"]
    assert entry["source"] == "staples-local"
    assert ZHAOSHANG_INVOCATIONS == 0


# ---- Case C:CACHE_HIT 永久 -------------------------------------------------------


def test_case_c_cache_hit_is_permanent_across_both_passes(stack, tmp_path):
    """comix 候选经 #10 缓存复核吸收 CACHE_HIT、stbchina 候选经 #11 本地爬取
    CACHE_HIT 后,再分别跑 #10 pass 与 #11 pass:两行逐字段不变、不重爬、
    无任何 remote 交互。"""
    comix_id = _seed_candidate(stack.prewarm, COMIX_URL, "D1", mall="comix")
    staples_id = _seed_candidate(stack.prewarm, STAPLES_URL, "D2", mall=STAPLES_CACHE_MALL_KEY)
    make_state_db(tmp_path / "runtime", BUSINESS_DATE)
    adapter = _CountingAdapter()

    # comix → CACHE_HIT(#10 缓存复核吸收,dispatch Case1 语义)
    stack.shared.record_success(mall="comix", submitted_url=COMIX_URL, source="daily-internet", price="1.1")
    first = _dispatch_pass(stack.prewarm, stack.shared, tmp_path / "runtime", adapter)
    assert first["cache_hit_after_recheck"] == 1
    assert adapter.submit_calls == 0
    assert stack.prewarm.candidate_by_id(comix_id)["cache_status"] == CACHE_STATUS_CACHE_HIT

    # stbchina → CACHE_HIT(#11 本地爬取)
    crawler = FakeCrawler(outcomes={STAPLES_URL: _success_outcome(STAPLES_URL)})
    _run_staples_pass(stack, crawler)
    assert crawler.calls == 1
    assert stack.prewarm.candidate_by_id(staples_id)["cache_status"] == CACHE_STATUS_CACHE_HIT

    comix_before = stack.prewarm.candidate_by_id(comix_id)
    staples_before = stack.prewarm.candidate_by_id(staples_id)

    # 再跑 #10 pass:CACHE_HIT 不是 cache miss,两条都整行不动
    second = _dispatch_pass(stack.prewarm, stack.shared, tmp_path / "runtime", adapter)
    assert second["jobs_created"] == 0
    assert second["cache_hit_after_recheck"] == 0
    # 再跑 #11 pass:CACHE_HIT 不被路由也不被复检,终态作业不重爬
    staples_summary = _run_staples_pass(stack, crawler, now=_t(60))
    assert crawler.calls == 1  # 永不重爬
    assert staples_summary["routed"] == 0 and staples_summary["recheck_hits"] == 0

    assert stack.prewarm.candidate_by_id(comix_id) == comix_before
    assert stack.prewarm.candidate_by_id(staples_id) == staples_before
    assert stack.staples.job_for_candidate(staples_id)["state"] == STAPLES_JOB_STATE_SUCCESS
    assert adapter.submit_calls == 0 and adapter.prioritize_calls == 0
    assert ZHAOSHANG_INVOCATIONS == 0


# ---- Case D:旧 #9 schema 的 additive migration ------------------------------------


LEGACY_EXPECTED_TABLES = {
    "prewarm_documents",
    "prewarm_candidates",
    "prewarm_links",
    "prewarm_sync_ledger",
    "prewarm_recovery_runs",
    # Issue #13 Round 2: 逐日 observation 与 remote job link 两张增量表
    "prewarm_daily_observations",
    "prewarm_remote_job_links",
}


def test_case_d_legacy_schema_additive_migration_preserves_rows(tmp_path):
    """#9 时代旧库(无 remote_job_id、无 staples sidecar 表)用合并树 RW 打开:
    remote_job_id 列与 staples_prewarm_jobs/_attempts 两表 additive 补齐,
    pre-existing 表的列集与旧行数据逐字段不变。"""
    db = tmp_path / "prewarm.sqlite3"
    connection = sqlite3.connect(str(db))
    try:
        # Issue-#9 时代的 prewarm_candidates 建表语句(无 remote_job_id;
        # 与 tests/test_customer_prewarm_store.py 的 legacy fixture 同一形状)
        connection.execute(
            """CREATE TABLE prewarm_candidates (
                   id INTEGER PRIMARY KEY,
                   mall TEXT NOT NULL,
                   original_url TEXT NOT NULL,
                   lookup_url TEXT NOT NULL,
                   routing_url TEXT,
                   business_date TEXT NOT NULL,
                   source TEXT NOT NULL DEFAULT 'detail',
                   cache_status TEXT NOT NULL DEFAULT 'PENDING_LOOKUP',
                   match_level TEXT,
                   resolved_identity_id INTEGER,
                   last_error TEXT,
                   last_checked_at TEXT,
                   first_seen_at TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL,
                   UNIQUE(mall, original_url)
               )"""
        )
        connection.execute(
            """INSERT INTO prewarm_candidates
               (mall, original_url, lookup_url, routing_url, business_date, source,
                cache_status, match_level, resolved_identity_id, last_error,
                last_checked_at, first_seen_at, last_seen_at)
               VALUES ('jd', 'https://item.jd.com/1', 'https://item.jd.com/1', NULL,
                       '2026-09-06', 'detail', 'PENDING_PREWARM', NULL, NULL, 'miss',
                       NULL, '2026-09-06T00:00:00+00:00', '2026-09-06T00:00:00+00:00')"""
        )
        connection.commit()
    finally:
        connection.close()

    before_objects, before_columns = _schema_snapshot(db)

    # 合并树 RW 打开:CustomerPrewarmStore + StaplesPrewarmStore 同 DB
    store = CustomerPrewarmStore(db)
    StaplesPrewarmStore(db)

    after_objects, after_columns = _schema_snapshot(db)
    # 新增对象 = #10 的一列 + #11 的两张 sidecar 表 + #9 其余既有表,additive 无改删
    # (UNIQUE 约束带来的 sqlite_autoindex_* 内部对象不计入)
    expected_new_tables = (LEGACY_EXPECTED_TABLES | {"staples_prewarm_jobs", "staples_prewarm_attempts"}) - {
        "prewarm_candidates"
    }

    def _user_names(objects):
        return {name for _type, name in objects if not name.startswith("sqlite_autoindex")}

    assert _user_names(after_objects) - _user_names(before_objects) == expected_new_tables
    assert set(after_columns["prewarm_candidates"]) - set(before_columns["prewarm_candidates"]) == {"remote_job_id"}
    assert "staples_prewarm_jobs" in after_columns
    assert "staples_prewarm_attempts" in after_columns

    # 旧行数据逐字段不变
    rows = store.candidates()
    assert len(rows) == 1
    row = rows[0]
    assert row["mall"] == "jd"
    assert row["original_url"] == "https://item.jd.com/1"
    assert row["lookup_url"] == "https://item.jd.com/1"
    assert row["routing_url"] is None
    assert row["business_date"] == "2026-09-06"
    assert row["source"] == "detail"
    assert row["cache_status"] == CACHE_STATUS_PENDING_PREWARM
    assert row["match_level"] is None
    assert row["resolved_identity_id"] is None
    assert row["last_error"] == "miss"
    assert row["last_checked_at"] is None
    assert row["first_seen_at"] == "2026-09-06T00:00:00+00:00"
    assert row["last_seen_at"] == "2026-09-06T00:00:00+00:00"
    assert row["remote_job_id"] is None
    # 迁移后的行可继续走 #10 状态机并携带 remote_job_id
    dispatched = store.record_candidate_result(
        row["id"], cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=42
    )
    assert dispatched["remote_job_id"] == 42


# ---- Case E:plan-only 零写 ---------------------------------------------------------


def test_case_e_plan_only_zero_write_on_legacy_schema(tmp_path):
    """旧 schema prewarm DB 上,先后运行 #10 plan-only(只读 store 注入)与
    #11 plan-only(服务 seam staples_plan_readonly):两个 DB 的 schema
    (sqlite_master + table_info)与文件字节均不变,目录零新增文件。"""
    runtime_dir = tmp_path / "runtime"
    db = tmp_path / "customer_prewarm.sqlite3"
    cache = tmp_path / "shared_crawl_cache.sqlite3"
    connection = sqlite3.connect(str(db))
    try:
        connection.execute(
            """CREATE TABLE prewarm_candidates (
                   id INTEGER PRIMARY KEY,
                   mall TEXT NOT NULL,
                   original_url TEXT NOT NULL,
                   lookup_url TEXT NOT NULL,
                   routing_url TEXT,
                   business_date TEXT NOT NULL,
                   source TEXT NOT NULL DEFAULT 'detail',
                   cache_status TEXT NOT NULL DEFAULT 'PENDING_LOOKUP',
                   match_level TEXT,
                   resolved_identity_id INTEGER,
                   last_error TEXT,
                   last_checked_at TEXT,
                   first_seen_at TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL,
                   UNIQUE(mall, original_url)
               )"""
        )
        connection.execute(
            """INSERT INTO prewarm_candidates
               (mall, original_url, lookup_url, routing_url, business_date, source,
                cache_status, match_level, resolved_identity_id, last_error,
                last_checked_at, first_seen_at, last_seen_at)
               VALUES
               ('stbchina', ?, ?, NULL, '2026-09-06', 'detail', 'PENDING_PREWARM',
                NULL, NULL, NULL, NULL, '2026-09-06T00:00:00+00:00',
                '2026-09-06T00:00:00+00:00'),
               ('comix', 'https://qx.com/legacy-1.html', 'https://qx.com/legacy-1.html',
                NULL, '2026-09-06', 'detail', 'PENDING_PREWARM', NULL, NULL, NULL,
                NULL, '2026-09-06T00:00:00+00:00', '2026-09-06T00:00:00+00:00')""",
            (STAPLES_URL, STAPLES_URL),
        )
        connection.commit()
    finally:
        connection.close()
    SharedCrawlCache(cache).record_success(
        "nbdeli", "https://b2b.nbdeli.com/goods/1.html", source="seed"
    )

    before_sizes = {path: path.stat().st_size for path in (db, cache)}
    before_hash = {path: _sha256(path) for path in (db, cache)}
    before_schema = {path: _schema_snapshot(path) for path in (db, cache)}
    before_files = {entry.name for entry in tmp_path.iterdir()}

    # #10 plan-only:注入只读 store,plan-only 分支零写、零 adapter 构造
    adapter = _CountingAdapter()
    report10 = run_prewarm_dispatch_pass(
        CustomerPrewarmStore.open_read_only(db),
        SharedCrawlCache.open_read_only(cache),
        business_date=BUSINESS_DATE,
        runtime_root=runtime_dir,
        mappings=[COMIX],
        adapter=adapter,
        plan_only=True,
    )
    assert report10["outcome"] == "OK"
    assert report10["staples_skipped"] == 1
    assert report10["mapped_zhaoshang"] == 1
    assert report10["new_remote_urls_planned"] == 1
    assert report10["jobs_created"] == 0
    assert adapter.prepare_calls == 0 and adapter.submit_calls == 0

    # #11 plan-only:服务 seam 真 mode=ro,旧库(无 staples 表)按空表处理
    report11 = staples_plan_readonly(db, cache, crawler_available=True)
    assert report11["prewarm_store"] == "OK"
    assert report11["cache"] == "OK"
    assert report11["candidates_total"] == 2
    assert report11["staples_candidates"] == 1
    assert report11["local_crawl_planned"] == 1
    assert report11["cache_hit_after_recheck"] == 0

    # 零写门禁:schema 与文件字节均不变,目录零新增文件
    for path in (db, cache):
        assert _sha256(path) == before_hash[path]
        assert path.stat().st_size == before_sizes[path]
        assert _schema_snapshot(path) == before_schema[path]
    new_files = {entry.name for entry in tmp_path.iterdir()} - before_files
    assert all(name.endswith(("-shm", "-wal")) for name in new_files)


# ---- Case F:同 DB 共存互不侵犯 ------------------------------------------------------


def test_case_f_same_db_coexistence_without_cross_contamination(stack, tmp_path):
    """A(comix,#10 可路由)与 B(stbchina,#11 本地)同一 DB/同一缓存:先 #10
    pass 后 #11 pass——A 只带 #10 状态/remote_job_id 且无 sidecar 作业;B 只走
    STAPLES_PENDING→CACHE_HIT+sidecar SUCCESS;B 在 #10 pass 后整行不变,
    A 在 #11 pass 后整行不变;双路径缓存身份各自独立,B 的 SKU 绝不 CONFIRMED。"""
    a_id = _seed_candidate(stack.prewarm, COMIX_URL, "DA", mall="comix")
    b_id = _seed_candidate(
        stack.prewarm, STAPLES_URL, "DB", mall=STAPLES_CACHE_MALL_KEY,
        cache_status=CACHE_STATUS_STAPLES_PENDING,
    )
    b_before_10 = stack.prewarm.candidate_by_id(b_id)
    make_state_db(tmp_path / "runtime", BUSINESS_DATE)

    # ---- #10 fake dispatch pass:A 派发,B 整行不变
    adapter = _CountingAdapter()
    summary10 = _dispatch_pass(stack.prewarm, stack.shared, tmp_path / "runtime", adapter)
    assert summary10["jobs_created"] == 1
    a_after_10 = stack.prewarm.candidate_by_id(a_id)
    assert a_after_10["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert a_after_10["remote_job_id"] is not None
    assert stack.prewarm.candidate_by_id(b_id) == b_before_10  # B 整行不变
    assert stack.staples.job_for_candidate(a_id) is None  # sidecar 无 A 作业

    # A 路径缓存落地 = Phase A 同步语义(test_case12/15 的模拟方式):upstream 证据
    stack.shared.record_success(
        mall="comix", submitted_url=COMIX_URL, source="zhaoshang-history",
        price="9.9", real_sku_id="qx-sku-1", sku_status=SKU_STATUS_UPSTREAM_PROVIDED,
    )

    # ---- #11 fake local pass:B 爬取成功,A 整行不变
    crawler = FakeCrawler(outcomes={STAPLES_URL: _success_outcome(STAPLES_URL)})
    summary11 = _run_staples_pass(stack, crawler, now=_t(60))
    assert crawler.calls == 1
    assert summary11["crawl_success"] == 1 and summary11["cache_hit_after_crawl"] == 1
    assert stack.prewarm.candidate_by_id(a_id) == a_after_10  # A 整行不变
    b_row = stack.prewarm.candidate_by_id(b_id)
    assert b_row["cache_status"] == CACHE_STATUS_CACHE_HIT
    assert b_row["remote_job_id"] is None
    assert stack.staples.job_for_candidate(b_id)["state"] == STAPLES_JOB_STATE_SUCCESS
    assert stack.staples.job_for_candidate(a_id) is None

    # ---- 同一 SharedCrawlCache 双路径共存:各自 lookup_current 命中、身份独立
    hit_a = stack.shared.lookup_current("comix", COMIX_URL)
    hit_b = stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, STAPLES_URL)
    assert hit_a is not None and hit_b is not None
    assert hit_a["identity_id"] != hit_b["identity_id"]
    # A 路径 SKU:upstream 证据按原样保存(UPSTREAM_PROVIDED)
    assert hit_a["entry"]["sku_status"] == SKU_STATUS_UPSTREAM_PROVIDED
    assert hit_a["entry"]["real_sku_id"] == "qx-sku-1"
    # B 路径 SKU:只能 URL 解析未验证,绝不能 CONFIRMED
    assert hit_b["entry"]["sku_status"] == SKU_STATUS_URL_PARSED_UNVERIFIED
    assert hit_b["entry"]["sku_status"] != SKU_STATUS_CONFIRMED
    assert hit_b["entry"]["source"] == "staples-local"
    # 跨 mall 身份互不命中
    assert stack.shared.lookup_current("comix", STAPLES_URL) is None
    assert stack.shared.lookup_current(STAPLES_CACHE_MALL_KEY, COMIX_URL) is None
    assert ZHAOSHANG_INVOCATIONS == 0


# ---- Case G:STEP1 隔离(closeout 不被混合 pending 状态阻塞) -------------------------

CLOSEOUT_BUSINESS_DATE = "2026-08-11"
CLOSEOUT_CUTOFF = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)


class _CloseoutClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


def _closeout_row(v: str, sku: str) -> dict[str, Any]:
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": "",
        "fourCategoryId": "cat",
        "list_reference_price": "10",
        "queryStr": f"title-{v}",
        "skuName": "title",
    }


def _closeout_observation(rows) -> DiscoveredState:
    return DiscoveredState(
        observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
        headers=["vCommodityId"],
        rows=list(rows),
        source_fingerprints=[f"source-{row['vCommodityId']}" for row in rows],
        customer_fingerprints=[f"customer-{row['vCommodityId']}" for row in rows],
        evidence={"saas_count": len(rows), "customer_count": len(rows)},
        # 显式声明 customer-only prewarm 通道(生产 discoverer 语义):
        # prewarm_rows=None 表示"无通道",intake 必须整跳跳过。
        prewarm_rows=prewarm_rows_from_customer_rows(rows),
    )


def test_case_g_step1_closeout_unblocked_by_mixed_pending_states(tmp_path):
    """候选处于 REMOTE_DISPATCHED(#10 pending)与 STAPLES_PENDING +
    LOCAL_SUCCESS_CACHE_PENDING(#11 retry/cache-sync pending)混合状态时,
    STEP1 closeout(生产 intake worker 接线)照常 finalize 到 FINAL,且混合
    状态行/sidecar 作业原样保留。seam 照抄 test_customer_prewarm_step1_isolation
    与 test_closeout_transient_recovery,不改 STEP1 业务代码。"""
    prewarm_root = tmp_path / "prewarm-root"
    prewarm_root.mkdir()
    prewarm_db = prewarm_root / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(prewarm_db)
    staples = StaplesPrewarmStore(prewarm_db)

    # #10 pending:REMOTE_DISPATCHED + remote_job_id
    remote_id = _seed_candidate(prewarm, COMIX_URL, "DOC-R", mall="comix")
    prewarm.record_candidate_result(
        remote_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=77
    )
    # #11 pending:STAPLES_PENDING + sidecar LOCAL_SUCCESS_CACHE_PENDING
    staples_id = _seed_candidate(
        prewarm, STAPLES_URL, "DOC-S", mall=STAPLES_CACHE_MALL_KEY,
        cache_status=CACHE_STATUS_STAPLES_PENDING,
    )
    job = staples.ensure_job(staples_id, now=T0)
    staples.complete_job(
        int(job["id"]), None, state=STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING,
        success_payload={"submitted_url": STAPLES_URL},
    )

    clock = _CloseoutClock()
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    lock = ProcessLock(tmp_path / "closeout.lock")
    materialize_calls: list[str] = []
    deps = TickDependencies(
        discover=lambda _timeout: _closeout_observation([_closeout_row("v1", "s1")]),
        detail=lambda _row_value, _timeout: {"customerPrice": "10"},
        model_batch=lambda batch, _timeout: {
            index: {"match_result": "SAME"} for index, _row in enumerate(batch)
        },
        harvest=lambda _jobs, _timeout: {},
        # 生产装配:prewarm intake 落到同一个混合状态 DB(runtime seam 照抄 step1 隔离测试)
        prewarm_intake=runtime_module._prewarm_intake_worker(prewarm_db),
        wall_clock=lambda: datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
        monotonic=clock.monotonic,
    )
    config = TickConfig(
        business_date=CLOSEOUT_BUSINESS_DATE,
        runtime_budget_seconds=120,
        heavy_work_reserve_seconds=5,
        lease_ttl_seconds=90,
        cutoff_at=CLOSEOUT_CUTOFF,
        source_stability_seconds=0.0,
    )
    closeout = CloseoutConfig(
        wall_budget_seconds=250,
        source_observation_interval_seconds=10,
        remote_sweep_interval_seconds=10,
        final_materialize_reserve_seconds=30,
    )

    def materialize(store_arg, lock_arg, business_date, output_path):
        materialize_calls.append(business_date)
        return object()

    summary, publication, blocked, blocked_error = run_closeout(
        store, lock, config, deps, closeout,
        output_path=Path("final.xlsx"),
        materialize=materialize,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    # closeout 不被阻塞:正常 finalize 到 FINAL
    assert summary.outcome not in {"FAILED", "DISCOVERY_INCOMPLETE", "CLOSEOUT_DEADLINE"}
    assert publication is not None
    assert blocked is None and blocked_error is None
    assert materialize_calls == [CLOSEOUT_BUSINESS_DATE]
    # 生产 intake worker 正常写同一 DB(intake 成功且只隔离在计数上)
    assert summary.counters.get("prewarm_intake_ok") == 1
    assert not summary.counters.get("prewarm_intake_failures")

    # 混合 pending 状态原样保留(closeout 全程不触碰 #10/#11 pending 证据)
    row_remote = prewarm.candidate_by_id(remote_id)
    assert row_remote["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert row_remote["remote_job_id"] == 77
    row_staples = prewarm.candidate_by_id(staples_id)
    assert row_staples["cache_status"] == CACHE_STATUS_STAPLES_PENDING
    sidecar = staples.job_for_candidate(staples_id)
    assert sidecar["state"] == STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING
    assert sidecar["success_payload_json"] is not None
