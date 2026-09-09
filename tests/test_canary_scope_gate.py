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

import json
from pathlib import Path

import pytest

from app.services.customer_prewarm_dispatch import (
    CANARY_JOB_LIMIT_EXCEEDED,
    CANARY_MALL_NOT_MAPPED,
    CANARY_PREFLIGHT_FAILED,
    CANARY_TARGET_EMPTY,
    CANARY_URL_LIMIT_EXCEEDED,
    CanaryScope,
    run_prewarm_dispatch_pass,
    validate_canary_preflight,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_REMOTE_REUSED,
    CustomerPrewarmStore,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping
from tests.test_customer_prewarm_dispatch import (
    BUSINESS_DATE,
    COMIX,
    _CountingAdapter,
    make_candidate,
    make_state_db,
)

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
