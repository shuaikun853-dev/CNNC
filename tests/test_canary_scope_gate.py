"""Issue #10 Canary 硬安全门禁（--only-mall 全链 scope）测试。

覆盖规格 §18 的 17 个 Case + CLI 参数/路径冲突组：
- CanaryScope 统一策略对象（mall_in_scope strip 后严格相等、空 mall 恒 False）；
- plan-only scope 化（同参 = 有效预演）+ validate_canary_preflight 六个
  fail-closed 结果码；
- live 四阶段全约束（Phase 1/2 越界零写 continue、Phase 3 首行 mall 断言 +
  组内 URL 上限断言、Phase 4 三证一致）；
- CLI 只读 preflight 先于任何 RW 构造器/ProcessLock/schema 迁移/任务工件；
- 显式路径守卫（CANARY_EXPLICIT_PATHS_REQUIRED）与路径策略
  （CANARY_PATH_POLICY_VIOLATION）；
- 普通模式 summary 键集合逐字节不变（无任何 canary 键）。

fixture 复用 tests/test_customer_prewarm_dispatch 的 fake store/cache/adapter
构造方式；全程 tmp_path + fake adapter，零真实远端调用、零真实沙箱/生产数据。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.services import customer_prewarm_dispatch as dispatch_service
from app.services.customer_prewarm_dispatch import (
    CANARY_JOB_LIMIT_EXCEEDED,
    CANARY_MALL_NOT_MAPPED,
    CANARY_PREFLIGHT_FAILED,
    CANARY_PRIORITY_SCOPE_VIOLATION,
    CANARY_TARGET_EMPTY,
    CANARY_URL_LIMIT_EXCEEDED,
    CanaryScope,
    CanaryScopeViolation,
    _boost_reused_job,
    _dispatch_mall_group,
    run_prewarm_dispatch_pass,
    validate_canary_preflight,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_REMOTE_REUSED,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts import customer_prewarm_dispatch as cli
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping
from tests.test_customer_prewarm_dispatch import (
    BUSINESS_DATE,
    COMIX,
    _CountingAdapter,
    candidate_by_id,
    make_candidate,
    make_state_db,
    seed_remote_work,
)
from tests.test_customer_prewarm_dispatch_cli import _seed_daily_run

XHGJ = ZhaoshangMallMapping(
    mall_name="新华光谷商城",
    source_id="SRC-XHGJ",
    mall_key="xhgj",
    domains=("xhgj.com", "www.xhgj.com"),
    vendor_keywords=("xhgj",),
)

# canary summary 的完整 §11 键集合（普通模式必须一个都不出现）。
CANARY_SUMMARY_KEYS = (
    "canary_mode",
    "only_mall",
    "canary_max_urls",
    "candidates_seen_total",
    "candidates_in_scope",
    "skipped_by_canary_filter",
    "remote_interaction_urls",
    "other_remote_tasks_created",
    "other_priority_changed",
    "priority_updates_performed",
)


def _canary_scope(only_mall: str = "xhgj", max_urls: int = 1) -> CanaryScope:
    return CanaryScope(only_mall=only_mall, max_urls=max_urls)


def _seed_multi_mall_store(runtime_root: Path) -> CustomerPrewarmStore:
    """xhgj×1 + comix(qx、空 mall 由路由解析)×1 + stbchina×1 + tmall×1。"""
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    make_candidate(
        prewarm,
        "https://www.xhgj.com/item-1.html",
        mall="xhgj",
        lookup_url="https://www.xhgj.com/item-1.html",
        document="D-X1",
    )
    make_candidate(prewarm, "https://qx.com/product-c1.html", mall="", document="D-C1")
    make_candidate(prewarm, "https://www.stbchina.cn/item/9.html", mall="", document="D-S1")
    make_candidate(prewarm, "https://detail.tmall.com/item.htm?id=9", mall="", document="D-T1")
    return prewarm


# ---- CanaryScope 策略对象 ------------------------------------------------------


def test_canary_scope_mall_in_scope_semantics():
    scope = _canary_scope("xhgj")
    assert scope.max_urls == 1 and scope.max_jobs == 1  # frozen 默认上限
    assert scope.mall_in_scope("xhgj") is True
    assert scope.mall_in_scope("  xhgj  ") is True  # strip 后严格相等
    assert scope.mall_in_scope("comix") is False
    assert scope.mall_in_scope("") is False  # 空 mall 恒 False
    assert scope.mall_in_scope(None) is False


# ---- Case 1:plan-only scope 化（同参 = 有效预演）--------------------------------


def test_case1_plan_only_canary_scopes_multi_mall_store(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = _seed_multi_mall_store(runtime_root)
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")

    summary = run_prewarm_dispatch_pass(
        prewarm,
        cache,
        business_date=BUSINESS_DATE,
        runtime_root=runtime_root,
        mappings=[COMIX, XHGJ],
        plan_only=True,
        canary_scope=_canary_scope(),
    )

    assert summary["outcome"] == "OK"
    assert summary["canary_mode"] is True
    assert summary["only_mall"] == "xhgj"
    assert summary["canary_max_urls"] == 1
    assert summary["candidates_seen_total"] == 4
    assert summary["candidates_in_scope"] == 1
    assert summary["skipped_by_canary_filter"] == 3
    # 只计划 xhgj 的 1 个 URL：0 复用 + 1 新建 + 0 待确认优先级。
    assert summary["remote_interaction_urls"] == 1
    assert summary["other_remote_tasks_created"] == 0
    assert summary["other_priority_changed"] == 0
    assert summary["priority_updates_performed"] == 0
    assert summary["canary_preflight"] == "PASS"
    # create 组只有 xhgj；staples/tmall 连 cache recheck 都不做。
    assert summary["by_mall"] == {"xhgj": {"reused": 0, "created_urls": 1}}
    assert summary["new_remote_urls_planned"] == 1
    assert summary["staples_skipped"] == 0
    assert summary["manual_skipped"] == 0
    # plan-only 零写：候选全部原地不动。
    assert all(
        row["cache_status"] == CACHE_STATUS_PENDING_PREWARM
        for row in prewarm.candidates()
    )


# ---- Case 9/10:validate_canary_preflight 单元级（顺序即优先级）--------------------


def test_case9_preflight_validator_mall_not_mapped():
    plan = {
        "new_remote_jobs_planned": 1,
        "candidates_in_scope": 1,
        "remote_interaction_urls": 1,
        "phase_errors": [],
    }
    assert validate_canary_preflight(plan, _canary_scope(), [COMIX]) == CANARY_MALL_NOT_MAPPED


def test_case10_preflight_validator_job_limit_fails_closed():
    plan = {
        "new_remote_jobs_planned": 2,
        "candidates_in_scope": 1,
        "remote_interaction_urls": 1,
        "phase_errors": [],
    }
    assert validate_canary_preflight(plan, _canary_scope(), [XHGJ]) == CANARY_JOB_LIMIT_EXCEEDED


def test_preflight_validator_full_priority_order():
    scope = _canary_scope()

    def plan(**overrides):
        base = {
            "new_remote_jobs_planned": 1,
            "candidates_in_scope": 1,
            "remote_interaction_urls": 1,
            "phase_errors": [],
        }
        base.update(overrides)
        return base

    # PASS → None。
    assert validate_canary_preflight(plan(), scope, [XHGJ]) is None
    # in-scope 候选为空 → CANARY_TARGET_EMPTY。
    assert validate_canary_preflight(plan(candidates_in_scope=0), scope, [XHGJ]) == CANARY_TARGET_EMPTY
    # URL 超限 → CANARY_URL_LIMIT_EXCEEDED。
    assert (
        validate_canary_preflight(plan(remote_interaction_urls=2), scope, [XHGJ])
        == CANARY_URL_LIMIT_EXCEEDED
    )
    # 规划本身有 phase error → CANARY_PREFLIGHT_FAILED。
    assert (
        validate_canary_preflight(plan(phase_errors=["boom"]), scope, [XHGJ])
        == CANARY_PREFLIGHT_FAILED
    )


# ---- Case 17a:普通模式 summary 键集合不变（无任何 canary 键）----------------------


def test_case17a_normal_plan_only_summary_has_no_canary_keys(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = _seed_multi_mall_store(runtime_root)
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")

    summary = run_prewarm_dispatch_pass(
        prewarm,
        cache,
        business_date=BUSINESS_DATE,
        runtime_root=runtime_root,
        mappings=[COMIX, XHGJ],
        plan_only=True,
    )

    assert "canary_mode" not in summary
    for key in CANARY_SUMMARY_KEYS:
        assert key not in summary, f"normal mode must not carry canary key {key}"
    # 普通模式语义不变：4 个候选全部被规划（staples/tmall 照常计数）。
    assert summary["candidates_total"] == 4
    assert summary["staples_skipped"] == 1
    assert summary["manual_skipped"] == 1


# ---- live 四阶段 scope 约束（Task 2）共享 helper ----------------------------------


def _run_canary_live(runtime_root, prewarm, cache, *, adapter=None, **kwargs):
    # canary 模式服务层约定与 CLI 守卫一致：max_jobs_per_pass 必须 == cap(1)。
    kwargs.setdefault("max_jobs_per_pass", 1)
    return run_prewarm_dispatch_pass(
        prewarm,
        cache,
        business_date=BUSINESS_DATE,
        runtime_root=runtime_root,
        mappings=[COMIX, XHGJ],
        adapter=adapter,
        canary_scope=_canary_scope(),
        **kwargs,
    )


def _store_row(store_path: Path, candidate_id: int) -> dict:
    connection = sqlite3.connect(str(store_path))
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM prewarm_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def _sqlite_tables(path: Path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        connection.close()


def _sqlite_columns(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        connection.close()


# ---- Case 2:live 其它商城候选行逐字段零变化 -----------------------------------------


def test_case2_live_canary_leaves_other_mall_candidate_rows_byte_identical(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    xhgj_id = make_candidate(
        prewarm, "https://www.xhgj.com/item-2.html", mall="xhgj", document="D-X2"
    )
    comix_id = make_candidate(prewarm, "https://qx.com/product-c2.html", mall="comix", document="D-C2")
    before = _store_row(prewarm.path, comix_id)
    adapter = _CountingAdapter()

    summary = _run_canary_live(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["outcome"] == "OK"
    assert summary["jobs_created"] == 1
    assert summary["candidates_seen_total"] == 2
    assert summary["candidates_in_scope"] == 1
    assert summary["skipped_by_canary_filter"] == 1
    assert summary["other_remote_tasks_created"] == 0
    # 越界候选行（status/last_checked_at/remote_job_id 等全部字段）前后一致。
    assert _store_row(prewarm.path, comix_id) == before
    # in-scope 候选正常走 create 链。
    assert candidate_by_id(prewarm, xhgj_id)["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert not [name for name in _sqlite_tables(paths.state_path) if name.startswith("prewarm_")]


# ---- Case 3:越界候选即使缓存有 current 命中也不被 absorb ----------------------------


def test_case3_live_canary_does_not_absorb_out_of_scope_cache_hit(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-c3.html"
    candidate_id = make_candidate(prewarm, url, mall="comix", document="D-C3")
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=55
    )
    cache.record_success(mall="comix", submitted_url=url, source="daily-internet", price="3.3")
    adapter = _CountingAdapter()

    summary = _run_canary_live(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["absorbed"] == 0
    assert adapter.submit_calls == 0 and adapter.prioritize_calls == 0
    row = candidate_by_id(prewarm, candidate_id)
    assert row["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED  # 原状态，非 CACHE_HIT
    assert row["remote_job_id"] == 55


# ---- Case 4/5:staples/tmall 越界候选状态绝不被改写 ---------------------------------


def test_case4_live_canary_keeps_staples_candidate_pending(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(
        prewarm, "https://www.stbchina.cn/item/4.html", mall="", document="D-S4"
    )
    adapter = _CountingAdapter()

    summary = _run_canary_live(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["staples_skipped"] == 0
    assert summary["skipped_by_canary_filter"] == 1
    assert summary["jobs_created"] == 0
    row = candidate_by_id(prewarm, candidate_id)
    assert row["cache_status"] == CACHE_STATUS_PENDING_PREWARM  # 非 STAPLES_PENDING


def test_case5_live_canary_keeps_tmall_candidate_pending(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(
        prewarm, "https://detail.tmall.com/item.htm?id=5", mall="", document="D-T5"
    )
    adapter = _CountingAdapter()

    summary = _run_canary_live(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["manual_skipped"] == 0
    assert summary["skipped_by_canary_filter"] == 1
    assert summary["jobs_created"] == 0
    row = candidate_by_id(prewarm, candidate_id)
    assert row["cache_status"] == CACHE_STATUS_PENDING_PREWARM  # 非 MANUAL_SKIP


# ---- Case 6:目标商城 1 URL happy path ----------------------------------------------


def test_case6_live_canary_single_url_happy_path(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    candidate_id = make_candidate(
        prewarm, "https://www.xhgj.com/item-6.html", mall="xhgj", document="D-X6"
    )
    adapter = _CountingAdapter()  # prioritize → UPDATED + weight_type=-1

    summary = _run_canary_live(runtime_root, prewarm, cache, adapter=adapter)

    assert summary["outcome"] == "OK"
    assert summary["jobs_created"] == 1
    # 1 个新建 URL；同 pass confirm 不重复计（URL 已在 create 处计入）。
    assert summary["remote_interaction_urls"] == 1
    assert summary["other_remote_tasks_created"] == 0
    assert summary["other_priority_changed"] == 0
    assert summary["priority_confirmed"] == 1
    assert adapter.submit_calls == 1 and adapter.prioritize_calls == 1
    row = candidate_by_id(prewarm, candidate_id)
    assert row["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert row["remote_job_id"] is not None


# ---- Case 11:_dispatch_mall_group 首行 mall 断言（第二层防御）------------------------


def test_case11_dispatch_mall_group_defensive_mall_assertion(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    store = DailyAuditStore(paths.state_path)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    adapter = _CountingAdapter()
    summary = {
        "business_date": BUSINESS_DATE,
        "phase_errors": [],
        "by_mall": {},
        "batch_sizes": [],
    }
    try:
        token = store.acquire_lease(BUSINESS_DATE, "case11", 120.0)
        try:
            with pytest.raises(CanaryScopeViolation):
                _dispatch_mall_group(
                    prewarm_store=prewarm,
                    store=store,
                    token=token,
                    paths=paths,
                    mapping=COMIX,
                    entries=[
                        {
                            "candidate": {"id": 1},
                            "routing": SimpleNamespace(lookup_url="https://qx.com/x.html"),
                        }
                    ],
                    get_adapter=lambda: adapter,
                    deadline=time.monotonic() + 50.0,
                    monotonic_value=time.monotonic,
                    max_jobs_per_pass=1,
                    created_jobs=0,
                    reuse_items=[],
                    reused_job_ids_this_pass=set(),
                    summary=summary,
                    canary_scope=_canary_scope(),
                )
        finally:
            store.release_lease(token)
    finally:
        lock.release()
    # 断言在任何 probe/写之前触发：submit=0、crawl 零创建。
    assert adapter.submit_calls == 0
    assert adapter.prepare_calls == 0
    assert _crawl_count(paths.state_path) == 0


def _crawl_count(state_path: Path) -> int:
    connection = sqlite3.connect(str(state_path))
    try:
        return int(connection.execute("SELECT COUNT(*) FROM crawl_work").fetchone()[0])
    finally:
        connection.close()


# ---- Case 12:其它商城任务的 priority 绝不被触碰（Phase 1 跳过 + 三证 tripwire）-------


def test_case12_boost_tripwire_blocks_other_mall_priority(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/product-c12.html"
    _seed_paths, job_id = seed_remote_work(
        runtime_root,
        BUSINESS_DATE,
        _CountingAdapter(),
        mapping=COMIX,
        url=url,
        job_state="SUBMITTED",
        remote_identity="comix|tid-12|tnum-12",
    )
    candidate_id = make_candidate(prewarm, url, mall="comix", document="D-C12")
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_REUSED, remote_job_id=job_id
    )
    adapter = _CountingAdapter()

    # Phase 1 已跳过该候选：根本不进 boost/confirm 列表。
    summary = _run_canary_live(runtime_root, prewarm, cache, adapter=adapter)
    assert adapter.prioritize_calls == 0
    assert summary["priority_updates_performed"] == 0

    # 第二层防御：直接调 _boost_reused_job 也被三证检查拦下（prioritize=0）。
    store = DailyAuditStore(paths.state_path)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    boost_summary = {
        "business_date": BUSINESS_DATE,
        "phase_errors": [],
        "by_mall": {},
        "batch_sizes": [],
    }
    try:
        token = store.acquire_lease(BUSINESS_DATE, "case12-boost", 120.0)
        try:
            _boost_reused_job(
                prewarm_store=prewarm,
                store=store,
                token=token,
                get_adapter=lambda: adapter,
                business_date=BUSINESS_DATE,
                candidate_id=candidate_id,
                job_id=job_id,
                deadline=time.monotonic() + 50.0,
                monotonic_value=time.monotonic,
                summary=boost_summary,
                canary_scope=_canary_scope(),
            )
        finally:
            store.release_lease(token)
    finally:
        lock.release()
    assert adapter.prioritize_calls == 0
    assert any(
        CANARY_PRIORITY_SCOPE_VIOLATION in item for item in boost_summary["phase_errors"]
    )


# ---- Case 16:老 schema 沙箱 store 仅由 RW live 迁移，state db 零 prewarm 表 --------


def test_case16_canary_rw_live_migrates_only_sandbox_store(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, BUSINESS_DATE)
    store_path = runtime_root / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(store_path)
    make_candidate(
        prewarm, "https://www.xhgj.com/item-16.html", mall="xhgj", document="D-16"
    )
    # 降级为 #9 老 schema（无 remote_job_id 列）。
    connection = sqlite3.connect(str(store_path))
    try:
        connection.execute("ALTER TABLE prewarm_candidates DROP COLUMN remote_job_id")
        connection.commit()
    finally:
        connection.close()
    assert "remote_job_id" not in _sqlite_columns(store_path, "prewarm_candidates")

    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    adapter = _CountingAdapter()
    migrated = CustomerPrewarmStore(store_path)  # RW 构造器执行幂等迁移

    summary = run_prewarm_dispatch_pass(
        migrated,
        cache,
        business_date=BUSINESS_DATE,
        runtime_root=runtime_root,
        mappings=[COMIX, XHGJ],
        adapter=adapter,
        max_jobs_per_pass=1,
        canary_scope=_canary_scope(),
    )

    assert summary["jobs_created"] == 1
    assert "remote_job_id" in _sqlite_columns(store_path, "prewarm_candidates")
    # 仅沙箱 store 迁移：per-day state db 无任何 prewarm 表。
    assert not [name for name in _sqlite_tables(paths.state_path) if name.startswith("prewarm_")]


# ---- 服务层防御：live canary 的 max-jobs 超 cap 直接 raise --------------------------


def test_service_rejects_live_canary_with_max_jobs_above_cap(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")

    with pytest.raises(CanaryScopeViolation):
        run_prewarm_dispatch_pass(
            prewarm,
            cache,
            business_date=BUSINESS_DATE,
            runtime_root=runtime_root,
            mappings=[COMIX, XHGJ],
            max_jobs_per_pass=2,
            canary_scope=_canary_scope(),
        )


# ---- Case 17b:普通 live 模式 summary 无任何 canary 键 ------------------------------


def test_case17b_normal_live_summary_has_no_canary_keys(tmp_path):
    runtime_root = tmp_path / "runtime"
    make_state_db(runtime_root, BUSINESS_DATE)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    make_candidate(prewarm, "https://www.xhgj.com/item-17.html", mall="xhgj", document="D-X17")
    make_candidate(prewarm, "https://qx.com/product-c17.html", mall="comix", document="D-C17")
    adapter = _CountingAdapter()

    summary = run_prewarm_dispatch_pass(
        prewarm,
        cache,
        business_date=BUSINESS_DATE,
        runtime_root=runtime_root,
        mappings=[COMIX, XHGJ],
        adapter=adapter,
    )

    for key in CANARY_SUMMARY_KEYS:
        assert key not in summary, f"normal mode must not carry canary key {key}"
    # 普通模式行为不变：两个商城组都正常创建。
    assert summary["jobs_created"] == 2


# ---- CLI canary 门禁（Task 3）fixtures and helpers -------------------------------


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_xhgj_mapping_workbook(path: Path) -> Path:
    """comix(EXTRA_DOMAIN_ALIASES qx.com) + xhgj(显式 商城URL 域名)。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["商城名称", "sourceId", "商城标识 identification", "商城URL"])
    sheet.append(["齐心商城", "SRC-COMIX", "comix", ""])
    sheet.append(["新华光谷商城", "SRC-XHGJ", "xhgj", "https://www.xhgj.com"])
    workbook.save(path)
    return path


def _run_canary_cli(
    runtime_root,
    state_root,
    prewarm_store_path,
    cache_path,
    workbook,
    *extra,
    only_mall="xhgj",
    json_output=None,
):
    argv = [
        "--business-date",
        BUSINESS_DATE,
        "--runtime-root",
        str(runtime_root),
        "--mapping-path",
        str(workbook),
        "--state-runtime-root",
        str(state_root),
        "--prewarm-store-path",
        str(prewarm_store_path),
        "--shared-cache-path",
        str(cache_path),
        "--only-mall",
        only_mall,
        "--max-jobs-per-pass",
        "1",  # canary CLI 守卫要求；真实调用形态
        *extra,
    ]
    if json_output is not None:
        argv += ["--json-output", str(json_output)]
    return cli.main(argv)


def _guard_rw_constructors(monkeypatch) -> None:
    """RW 构造器/ProcessLock 桩：canary 被阻断时任何 RW 访问都是 AssertionError。"""
    real_prewarm_init = CustomerPrewarmStore.__init__
    real_cache_init = SharedCrawlCache.__init__

    def _prewarm_init(self, path, *args, **kwargs):
        if not kwargs.get("read_only", False):
            raise AssertionError("RW CustomerPrewarmStore must not run before preflight PASS")
        real_prewarm_init(self, path, *args, **kwargs)

    def _cache_init(self, path, *args, **kwargs):
        if not kwargs.get("read_only", False):
            raise AssertionError("RW SharedCrawlCache must not run before preflight PASS")
        real_cache_init(self, path, *args, **kwargs)

    class _NoLock:
        def __init__(self, *args, **kwargs):
            raise AssertionError("ProcessLock must not be taken before preflight PASS")

    monkeypatch.setattr(CustomerPrewarmStore, "__init__", _prewarm_init)
    monkeypatch.setattr(SharedCrawlCache, "__init__", _cache_init)
    monkeypatch.setattr(dispatch_service, "ProcessLock", _NoLock)


def _tmp_layout(tmp_path):
    runtime_root = tmp_path / "runtime"
    state_root = tmp_path / "state"
    runtime_root.mkdir()
    state_root.mkdir()
    return (
        runtime_root,
        state_root,
        runtime_root / "customer_prewarm.sqlite3",
        runtime_root / "shared_crawl_cache.sqlite3",
    )


# ---- Case 7/15:URL 超限阻断在任何 RW/锁/迁移之前（store 字节不变、仍无列）-----------


def test_case7_and_15_url_limit_blocks_before_any_rw_write_or_migration(tmp_path, monkeypatch, capsys):
    runtime_root, state_root, store_path, cache_path = _tmp_layout(tmp_path)
    prewarm = CustomerPrewarmStore(store_path)
    SharedCrawlCache(cache_path)
    make_candidate(prewarm, "https://www.xhgj.com/item-7a.html", mall="xhgj", document="D-X7A")
    make_candidate(prewarm, "https://www.xhgj.com/item-7b.html", mall="xhgj", document="D-X7B")
    # 老 schema：预先降级掉 remote_job_id 列（Case 15）。
    connection = sqlite3.connect(str(store_path))
    try:
        connection.execute("ALTER TABLE prewarm_candidates DROP COLUMN remote_job_id")
        connection.commit()
    finally:
        connection.close()
    sha_before = _sha256(store_path)
    _guard_rw_constructors(monkeypatch)

    json_output = tmp_path / "case7.json"
    exit_code = _run_canary_cli(
        runtime_root,
        state_root,
        store_path,
        cache_path,
        _write_xhgj_mapping_workbook(tmp_path / "mapping.xlsx"),
        "--canary-max-urls",
        "1",
        json_output=json_output,
    )

    assert exit_code == 2
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["canary_preflight"] == CANARY_URL_LIMIT_EXCEEDED
    assert summary["candidates_in_scope"] == 2
    assert summary["new_remote_urls_planned"] == 2
    stdout = capsys.readouterr().out
    assert "BLOCKED" in stdout
    # Case 7：store 文件字节不变；Case 15：仍无 remote_job_id 列。
    assert _sha256(store_path) == sha_before
    assert "remote_job_id" not in _sqlite_columns(store_path, "prewarm_candidates")


# ---- Case 8:目标 mall 未映射 → 零 RW 阻断 ------------------------------------------


def test_case8_canary_mall_not_mapped_blocks_with_zero_rw(tmp_path, monkeypatch, capsys):
    runtime_root, state_root, store_path, cache_path = _tmp_layout(tmp_path)
    prewarm = CustomerPrewarmStore(store_path)
    SharedCrawlCache(cache_path)
    make_candidate(prewarm, "https://www.xhgj.com/item-8.html", mall="xhgj", document="D-X8")
    sha_before = _sha256(store_path)
    _guard_rw_constructors(monkeypatch)

    exit_code = _run_canary_cli(
        runtime_root,
        state_root,
        store_path,
        cache_path,
        _write_xhgj_mapping_workbook(tmp_path / "mapping.xlsx"),
        only_mall="not-exist",
    )

    assert exit_code == 2
    assert "CANARY_MALL_NOT_MAPPED" in capsys.readouterr().out
    assert _sha256(store_path) == sha_before


# ---- Case 14:缺任一显式路径 → exit 2 CANARY_EXPLICIT_PATHS_REQUIRED -----------------


def test_case14_canary_requires_all_explicit_paths(tmp_path, monkeypatch, capsys):
    runtime_root = tmp_path / "runtime"
    state_root = tmp_path / "state"
    runtime_root.mkdir()
    state_root.mkdir()
    store_path = runtime_root / "customer_prewarm.sqlite3"
    CustomerPrewarmStore(store_path)
    _guard_rw_constructors(monkeypatch)

    argv = [
        "--business-date",
        BUSINESS_DATE,
        "--runtime-root",
        str(runtime_root),
        "--mapping-path",
        str(_write_xhgj_mapping_workbook(tmp_path / "mapping.xlsx")),
        "--state-runtime-root",
        str(state_root),
        "--prewarm-store-path",
        str(store_path),
        # 故意缺 --shared-cache-path
        "--only-mall",
        "xhgj",
        "--max-jobs-per-pass",
        "1",
    ]
    exit_code = cli.main(argv)

    assert exit_code == 2
    assert "CANARY_EXPLICIT_PATHS_REQUIRED" in capsys.readouterr().out
    assert not (runtime_root / "shared_crawl_cache.sqlite3").exists()


# ---- 参数冲突组 / 路径策略组 --------------------------------------------------------


def test_canary_param_conflicts_fail_closed(tmp_path, capsys):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    workbook = _write_xhgj_mapping_workbook(tmp_path / "mapping.xlsx")
    base = [
        "--business-date",
        BUSINESS_DATE,
        "--runtime-root",
        str(runtime_root),
        "--mapping-path",
        str(workbook),
    ]

    assert cli.main(base + ["--only-mall", "xhgj", "--canary-max-urls", "0"]) == 2
    assert "--canary-max-urls must be at least 1" in capsys.readouterr().out
    assert cli.main(base + ["--only-mall", "xhgj", "--max-jobs-per-pass", "8"]) == 2
    assert "--max-jobs-per-pass must be 1 with --only-mall" in capsys.readouterr().out
    assert (
        cli.main(
            base + ["--only-mall", "xhgj", "--max-jobs-per-pass", "1", "--backfill-zhaoshang-history", "3"]
        )
        == 2
    )
    assert "mutually exclusive with --backfill-zhaoshang-history" in capsys.readouterr().out


def test_canary_path_policy_blocks_store_inside_state_runtime_root(tmp_path, capsys):
    runtime_root, state_root, _, cache_path = _tmp_layout(tmp_path)
    workbook = _write_xhgj_mapping_workbook(tmp_path / "mapping.xlsx")

    exit_code = _run_canary_cli(
        runtime_root,
        state_root,
        state_root / "customer_prewarm.sqlite3",  # 落在 state-runtime-root 内
        cache_path,
        workbook,
    )

    assert exit_code == 2
    stdout = capsys.readouterr().out
    assert "CANARY_PATH_POLICY_VIOLATION" in stdout
    assert not (state_root / "customer_prewarm.sqlite3").exists()


# ---- Case 13:同参 plan-only 与 live（preflight 计划字段携带）一致 --------------------


def test_case13_live_canary_summary_carries_preflight_plan_fields(tmp_path, monkeypatch):
    runtime_root, state_root, store_path, cache_path = _tmp_layout(tmp_path)
    prewarm = CustomerPrewarmStore(store_path)
    SharedCrawlCache(cache_path)
    make_candidate(prewarm, "https://www.xhgj.com/item-13.html", mall="xhgj", document="D-X13")
    make_candidate(prewarm, "https://qx.com/product-c13.html", mall="comix", document="D-C13")
    _seed_daily_run(state_root / BUSINESS_DATE / "state.sqlite3", BUSINESS_DATE)
    workbook = _write_xhgj_mapping_workbook(tmp_path / "mapping.xlsx")

    # 1) 同参 plan-only（有效预演，exit 0）。
    plan_json = tmp_path / "case13-plan.json"
    plan_code = _run_canary_cli(
        runtime_root,
        state_root,
        store_path,
        cache_path,
        workbook,
        "--canary-max-urls",
        "1",
        "--plan-only",
        json_output=plan_json,
    )
    assert plan_code == 0
    plan_summary = json.loads(plan_json.read_text(encoding="utf-8"))
    assert plan_summary["outcome"] == "OK"
    assert plan_summary["plan_only"] is True
    assert plan_summary["canary_preflight"] == "PASS"
    for key in CANARY_SUMMARY_KEYS:
        assert key in plan_summary
    assert plan_summary["candidates_in_scope"] == 1
    assert plan_summary["skipped_by_canary_filter"] == 1
    assert plan_summary["priority_updates_performed"] == 0

    # 2) live canary（fake adapter）。
    created_adapters: list[_CountingAdapter] = []

    class _TrackedAdapter(_CountingAdapter):
        def __init__(self, *, mappings=None, **kwargs):
            super().__init__(**kwargs)
            created_adapters.append(self)

    monkeypatch.setattr(dispatch_service, "CnncLegacyZhaoshangAdapter", _TrackedAdapter)

    live_json = tmp_path / "case13-live.json"
    live_code = _run_canary_cli(
        runtime_root,
        state_root,
        store_path,
        cache_path,
        workbook,
        "--canary-max-urls",
        "1",
        json_output=live_json,
    )
    assert live_code == 0
    live_summary = json.loads(live_json.read_text(encoding="utf-8"))
    assert live_summary["outcome"] == "OK"
    assert live_summary["jobs_created"] == 1
    assert live_summary["sandbox_schema_migrated"] is False  # 新 schema store 无迁移
    assert len(created_adapters) == 1
    assert created_adapters[0].submit_calls == 1
    # live 的计划字段来自 preflight 携带 → 与 plan-only 数值完全一致。
    for key in (
        "candidates_seen_total",
        "candidates_in_scope",
        "skipped_by_canary_filter",
        "remote_interaction_urls",
        "new_remote_urls_planned",
    ):
        assert live_summary[key] == plan_summary[key], key
    # in-scope 候选派发、越界候选原状。
    assert live_summary["only_mall"] == "xhgj"
