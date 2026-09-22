"""customer_prewarm_discovery 行为测试(Issue #9 Phase 2)。

覆盖:URL 商城路由(jslink 改写/域名匹配/first-match/垃圾输入)、intake 幂等、
detail 扫描(历史命中/alias/MALL_PARSER/同 SKU 不合并/多文档共享候选/失败隔离)、
candidate 刷新、prewarm 扫描编排(Phase A-D + 预算 + Phase 隔离)。

用例编号对应 Issue #9 验收清单(Case 1-12)。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.services.customer_prewarm_discovery import (
    intake_documents,
    process_document,
    refresh_pending_candidates,
    route_url,
    run_prewarm_scan,
    sync_daily_internet_results,
)
from app.services.crawl_url_identity import SKU_STATUS_CONFIRMED, SKU_STATUS_UNRESOLVED
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.daily_audit_store import DailyAuditStore
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

BUSINESS_DATE = "2026-09-07"

MAPPINGS = [
    ZhaoshangMallMapping(
        mall_name="得力商城",
        source_id="S1",
        mall_key="nbdeli",
        domains=("b2b.nbdeli.com", "nbdeli.com", "www.nbdeli.com", "nbdeli.cn"),
        vendor_keywords=("nbdeli",),
    ),
    ZhaoshangMallMapping(
        mall_name="齐心商城",
        source_id="S2",
        mall_key="comix",
        domains=("qx.com", "www.qx.com", "mro.qx.com", "comix.com.cn"),
        vendor_keywords=("comix",),
    ),
    ZhaoshangMallMapping(
        mall_name="欧菲斯",
        source_id="S3",
        mall_key="ofs",
        domains=("ofs.cn", "www.ofs.cn"),
        vendor_keywords=("ofs",),
    ),
]


def detail_payload(urls):
    return {"data": {"referenceInfos": [{"referenceLink": u} for u in urls]}}


@pytest.fixture()
def prewarm_store(tmp_path):
    return CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")


@pytest.fixture()
def shared_cache(tmp_path):
    return SharedCrawlCache(tmp_path / "shared_cache.sqlite3")


def make_document(store, document_id, category="CAT-1", business_date=BUSINESS_DATE):
    return store.upsert_document(business_date, document_id, category)


def make_fetcher(payload_by_document):
    """detail_fetch 测试替身:按 documentId 返回 payload,缺失即失败。"""

    def detail_fetch(document_id, four_category_id):
        payload = payload_by_document.get(document_id)
        if payload is None:
            return {"ok": False, "payload": None, "error": f"no detail for {document_id}"}
        return {"ok": True, "payload": payload, "error": None}

    return detail_fetch


def _candidate(store):
    rows = store.candidates()
    assert len(rows) == 1
    return rows[0]


# ---- route_url 单元 ------------------------------------------------------------


@pytest.mark.parametrize("host", ["www.jslink.com", "b.jslink.com"])
def test_route_url_jslink_rewrite_is_the_deterministic_rule(host):
    routed = route_url(f"https://{host}/goods/400.html", MAPPINGS)
    assert routed.mall == "nbdeli"
    assert routed.lookup_url == "https://b2b.nbdeli.com/goods/400.html"
    assert routed.routing_url == "https://b2b.nbdeli.com/goods/400.html"


def test_route_url_no_rewrite_means_routing_url_none():
    routed = route_url("https://b2b.nbdeli.com/goods/1.html", MAPPINGS)
    assert routed.mall == "nbdeli"
    assert routed.lookup_url == "https://b2b.nbdeli.com/goods/1.html"
    assert routed.routing_url is None


def test_route_url_www_stripped_and_subdomain_domains_match():
    assert route_url("https://www.nbdeli.com/p/1", MAPPINGS).mall == "nbdeli"
    assert route_url("https://shop.nbdeli.com/p/1", MAPPINGS).mall == "nbdeli"
    assert route_url("https://mro.qx.com/x", MAPPINGS).mall == "comix"
    assert route_url("https://www.ofs.cn/product-1.html", MAPPINGS).mall == "ofs"
    # endswith(".domain") 不吞相近域名
    assert route_url("https://notnbdeli.com/p", MAPPINGS).mall == ""


def test_route_url_unmapped_host():
    routed = route_url("https://unknown.example/item/9", MAPPINGS)
    assert routed.mall == ""
    assert routed.lookup_url == "https://unknown.example/item/9"
    assert routed.routing_url is None


def test_route_url_first_match_wins():
    extended = list(MAPPINGS) + [
        ZhaoshangMallMapping(
            mall_name="重叠商城",
            source_id="S9",
            mall_key="overlap",
            domains=("qx.com",),
            vendor_keywords=("overlap",),
        )
    ]
    assert route_url("https://qx.com/x", extended).mall == "comix"


def test_route_url_empty_and_garbage_never_raise():
    empty = route_url("", MAPPINGS)
    assert empty.mall == ""
    assert empty.lookup_url == ""
    assert empty.routing_url is None
    garbage = route_url("not a url", MAPPINGS)
    assert garbage.mall == ""
    assert garbage.lookup_url == "not a url"
    assert route_url("http://[::1/open", MAPPINGS).mall == ""  # 非法 IPv6 不得抛出
    assert route_url("   ", MAPPINGS).mall == ""


# ---- intake_documents ------------------------------------------------------------


def test_intake_documents_dedupes_and_skips_empty_ids(prewarm_store):
    rows = [
        {"documentId": "DOC-1", "fourCategoryId": "C1"},
        {"documentId": "DOC-1", "fourCategoryId": "C1"},
        {"documentId": "  ", "fourCategoryId": "C2"},
        {"documentId": None, "fourCategoryId": "C3"},
        {"fourCategoryId": "C4"},
    ]
    assert intake_documents(prewarm_store, BUSINESS_DATE, rows) == 1
    assert intake_documents(prewarm_store, BUSINESS_DATE, rows, source="scanner") == 1
    pending = prewarm_store.pending_documents(10)
    assert [row["document_id"] for row in pending] == ["DOC-1"]
    assert pending[0]["four_category_id"] == "C1"


def test_intake_and_detail_persist_business_day_customer_evidence(
    prewarm_store,
    shared_cache,
):
    url = "https://b2b.nbdeli.com/goods/customer-evidence.html"
    rows = [{
        "documentId": "DOC-EVIDENCE",
        "fourCategoryId": "CAT-4",
        "fourCategoryName": "量具",
        "commodityName": "世达 SATA 91513 0-300mm",
        "referenceLink": url,
    }]
    assert intake_documents(prewarm_store, BUSINESS_DATE, rows) == 1
    before = prewarm_store.daily_document_evidence(BUSINESS_DATE, "DOC-EVIDENCE")
    assert before["customer_title"] == "世达 SATA 91513 0-300mm"
    assert before["four_category_name"] == "量具"
    assert before["customer_price"] is None

    payload = {
        "data": {
            "referenceInfos": [{
                "referenceLink": url,
                "referencePrice": "1871.52",
            }]
        }
    }
    result = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-EVIDENCE": payload}),
        business_date=BUSINESS_DATE,
    )

    assert result["detail_ok"] is True
    after = prewarm_store.daily_document_evidence(BUSINESS_DATE, "DOC-EVIDENCE")
    assert after["customer_title"] == "世达 SATA 91513 0-300mm"
    assert after["customer_price"] == "1871.52"
    assert after["customer_price_status"] == "FOUND_SINGLE_REFERENCE"
    assert after["customer_price_source"] == "DETAIL_SINGLE_REFERENCE"
    assert "1871.52" in after["customer_price_evidence"]


# ---- Case 1: 历史精确命中 ---------------------------------------------------------


def test_case1_historical_exact_hit(prewarm_store, shared_cache):
    url = "https://b2b.nbdeli.com/goods/100.html"
    shared_cache.record_success(
        mall="nbdeli",
        submitted_url=url,
        real_sku_id="SKU-1",
        sku_status=SKU_STATUS_CONFIRMED,
        price="9.90",
        source="zhaoshang-history",
        succeeded_at="2026-09-01T00:00:00+00:00",
    )
    make_document(prewarm_store, "DOC-1")
    result = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-1": detail_payload([url])}),
    )
    assert result["detail_ok"] is True
    assert result["hits"] == 1
    assert result["misses"] == 0
    candidate = _candidate(prewarm_store)
    assert candidate["cache_status"] == "CACHE_HIT"
    assert candidate["match_level"] == "EXACT"
    assert prewarm_store.list_unique_cache_misses() == []
    references = shared_cache.document_references("DOC-1")
    assert references[0]["original_url"] == url


# ---- Case 2: alias 命中 -----------------------------------------------------------


def test_case2_alias_hit(prewarm_store, shared_cache):
    shared_cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/200.html?utm=mail",
        returned_url="https://b2b.nbdeli.com/goods/200.html",
        real_sku_id="SKU-2",
        sku_status=SKU_STATUS_CONFIRMED,
        price="5.00",
        source="zhaoshang-history",
        succeeded_at="2026-09-01T00:00:00+00:00",
    )
    # 客户 URL 是提交变体(带 query):历史爬取学到的 alias 直接命中
    make_document(prewarm_store, "DOC-A")
    result_submitted = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher(
            {"DOC-A": detail_payload(["https://b2b.nbdeli.com/goods/200.html?utm=mail"])}
        ),
    )
    assert result_submitted["hits"] == 1
    # 返回变体同样命中
    make_document(prewarm_store, "DOC-B")
    result_returned = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher(
            {"DOC-B": detail_payload(["https://b2b.nbdeli.com/goods/200.html"])}
        ),
    )
    assert result_returned["hits"] == 1
    assert {row["cache_status"] for row in prewarm_store.candidates()} == {"CACHE_HIT"}


# ---- Case 3: MALL_PARSER 命中 ------------------------------------------------------


def test_case3_mall_parser_hit(prewarm_store, shared_cache):
    shared_cache.record_success(
        mall="ofs",
        submitted_url="https://www.ofs.cn/product-6287660.html",
        source="zhaoshang-history",
        sku_status=SKU_STATUS_UNRESOLVED,
        price="88.00",
        succeeded_at="2026-09-01T00:00:00+00:00",
    )
    customer_url = "https://ofs.cn/product-6287660.html?utm=x"
    make_document(prewarm_store, "DOC-3")
    result = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-3": detail_payload([customer_url])}),
    )
    assert result["hits"] == 1
    candidate = _candidate(prewarm_store)
    assert candidate["cache_status"] == "CACHE_HIT"
    assert candidate["match_level"] == "MALL_PARSER"
    assert candidate["mall"] == "ofs"


# ---- Case 4: 同 SKU 不合并 ---------------------------------------------------------


def test_case4_same_sku_no_hit(prewarm_store, shared_cache):
    shared_cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/300.html",
        real_sku_id="S9",
        sku_status=SKU_STATUS_CONFIRMED,
        price="3.00",
        source="zhaoshang-history",
        succeeded_at="2026-09-01T00:00:00+00:00",
    )
    make_document(prewarm_store, "DOC-4")
    process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher(
            {"DOC-4": detail_payload(["https://b2b.nbdeli.com/goods/301.html"])}
        ),
    )
    candidate = _candidate(prewarm_store)
    assert candidate["cache_status"] == "PENDING_PREWARM"  # 禁止 SKU-only 合并
    assert [row["id"] for row in prewarm_store.list_unique_cache_misses()] == [candidate["id"]]


# ---- Case 5: 多文档共享同一 URL 候选 -------------------------------------------------


def test_case5_multi_document_same_url(prewarm_store, shared_cache):
    shared_url = "https://b2b.nbdeli.com/goods/500.html"
    for index in (1, 2, 3):
        make_document(prewarm_store, f"DOC-{index}")
        process_document(
            prewarm_store,
            shared_cache,
            prewarm_store.pending_documents(10)[0],
            mappings=MAPPINGS,
            detail_fetch=make_fetcher({f"DOC-{index}": detail_payload([shared_url])}),
        )
    assert len(prewarm_store.links_for_document("DOC-1")) == 1
    assert len(prewarm_store.candidates()) == 1
    candidate = prewarm_store.candidates()[0]
    assert candidate["cache_status"] == "PENDING_PREWARM"
    assert [link["document_id"] for link in prewarm_store.links_for_candidate(candidate["id"])] == [
        "DOC-1",
        "DOC-2",
        "DOC-3",
    ]
    assert prewarm_store.stats()["candidates_with_multiple_documents"] == 1


# ---- Case 6: event/scanner 双入口去重 ------------------------------------------------


def test_case6_event_and_scanner_duplicates(prewarm_store, shared_cache):
    rows = [{"documentId": "DOC-1", "fourCategoryId": "CAT-1"}]
    assert intake_documents(prewarm_store, BUSINESS_DATE, rows, source="event") == 1
    assert intake_documents(prewarm_store, BUSINESS_DATE, rows, source="scanner") == 1
    assert len(prewarm_store.pending_documents(10)) == 1
    url = "https://b2b.nbdeli.com/goods/600.html"
    fetcher = make_fetcher({"DOC-1": detail_payload([url])})
    document_row = prewarm_store.pending_documents(10)[0]
    # event+scanner 双入口都处理同一文档:process 幂等,link/candidate 不膨胀
    first = process_document(
        prewarm_store,
        shared_cache,
        document_row,
        mappings=MAPPINGS,
        detail_fetch=fetcher,
    )
    second = process_document(
        prewarm_store,
        shared_cache,
        document_row,
        mappings=MAPPINGS,
        detail_fetch=fetcher,
    )
    assert first["detail_ok"] and second["detail_ok"]
    assert len(prewarm_store.links_for_document("DOC-1")) == 1
    assert len(prewarm_store.candidates()) == 1


# ---- 回归:per-link 错误不得把文档标成 DONE(链接永不丢失) ---------------------------


def test_link_error_keeps_document_pending_and_retryable(
    prewarm_store, shared_cache, monkeypatch
):
    make_document(prewarm_store, "DOC-1")
    urls = [
        "https://b2b.nbdeli.com/goods/700.html",
        "https://b2b.nbdeli.com/goods/701.html",
    ]
    real_upsert = prewarm_store.upsert_link
    calls = {"n": 0}

    def flaky_upsert(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # 第二条链接遇到 sqlite busy
            raise sqlite3.OperationalError("database is locked")
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(prewarm_store, "upsert_link", flaky_upsert)
    first = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-1": detail_payload(urls)}),
    )
    assert first["detail_ok"] is True  # detail 本身成功
    assert first["retried"] is True
    assert len(first["errors"]) == 1
    # 文档必须保持 PENDING(而不是 DONE),本轮失败链接下一轮重扫补齐
    due = datetime.now(UTC) + timedelta(days=1)
    pending = {row["document_id"]: row for row in prewarm_store.pending_documents(10, now=due)}
    assert set(pending) == {"DOC-1"}
    assert pending["DOC-1"]["detail_state"] == "PENDING"
    assert pending["DOC-1"]["detail_attempts"] == 1
    assert "link processing errors:" in str(pending["DOC-1"]["last_error"])
    assert len(prewarm_store.links_for_document("DOC-1")) == 1

    # 存储恢复后第二次扫描:同一文档处理完成,两条链接都在
    second = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10, now=due)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-1": detail_payload(urls)}),
    )
    assert second["detail_ok"] is True
    assert second["retried"] is False
    assert second["errors"] == []
    document_row = prewarm_store.upsert_document(BUSINESS_DATE, "DOC-1", "CAT-1")
    assert document_row["detail_state"] == "DONE"
    assert len(prewarm_store.links_for_document("DOC-1")) == 2


# ---- Case 8: detail 失败隔离;瞬时失败永不终态,按 backoff 推迟 -----------------------


def test_case8_transient_detail_failures_stay_recoverable(prewarm_store, shared_cache):
    make_document(prewarm_store, "DOC-1")
    make_document(prewarm_store, "DOC-2")
    payload_by_document = {"DOC-2": detail_payload(["https://b2b.nbdeli.com/goods/800.html"])}
    # DOC-1 detail 失败,DOC-2 成功:互不影响
    for document_row in prewarm_store.pending_documents(10):
        process_document(
            prewarm_store,
            shared_cache,
            document_row,
            mappings=MAPPINGS,
            detail_fetch=make_fetcher(payload_by_document),
        )
    due = datetime.now(UTC) + timedelta(days=1)
    rows = {row["document_id"]: row for row in prewarm_store.pending_documents(10, now=due)}
    assert set(rows) == {"DOC-1"}
    assert rows["DOC-1"]["detail_attempts"] == 1
    assert rows["DOC-1"]["detail_state"] == "PENDING"
    doc_row = prewarm_store.upsert_document(BUSINESS_DATE, "DOC-2", "CAT-1")
    assert doc_row["detail_state"] == "DONE"
    assert len(prewarm_store.links_for_document("DOC-2")) == 1
    # 连续 5 次失败(detail lock busy 等):永不 FAILED,只按 backoff 推迟
    for _ in range(4):
        outcome = process_document(
            prewarm_store,
            shared_cache,
            prewarm_store.pending_documents(10, now=due)[0],
            mappings=MAPPINGS,
            detail_fetch=make_fetcher({}),
        )
        assert outcome["detail_ok"] is False
    # 默认墙钟仍在 backoff 窗口内:文档被推迟,不是被丢弃
    assert prewarm_store.pending_documents(10) == []
    deferred = prewarm_store.upsert_document(BUSINESS_DATE, "DOC-1", "CAT-1")
    assert deferred["detail_state"] == "PENDING"
    assert deferred["detail_attempts"] == 5
    assert deferred["consecutive_failures"] == 5
    assert deferred["next_retry_at"] is not None
    # 注入越过 backoff 的 now → 第 6 次尝试成功 → DONE,链接落库,候选状态正确
    recovered = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10, now=due)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher(
            {"DOC-1": detail_payload(["https://b2b.nbdeli.com/goods/801.html"])}
        ),
    )
    assert recovered["detail_ok"] is True
    done = prewarm_store.upsert_document(BUSINESS_DATE, "DOC-1", "CAT-1")
    assert done["detail_state"] == "DONE"
    assert done["consecutive_failures"] == 0
    assert done["next_retry_at"] is None
    assert len(prewarm_store.links_for_document("DOC-1")) == 1
    statuses = {
        row["original_url"]: row["cache_status"] for row in prewarm_store.candidates()
    }
    assert statuses["https://b2b.nbdeli.com/goods/801.html"] == "PENDING_PREWARM"


# ---- Case 12: original_url 溯源(jslink 改写不改原文) --------------------------------


def test_mapping_change_creates_second_strictly_scoped_candidate(
    prewarm_store, shared_cache
):
    """映射表变化(同 URL 换商城)= 两个候选;link 状态指向当前路由候选,旧候选不动。"""
    url = "https://b2b.nbdeli.com/goods/900.html"
    nbdeli_only = [MAPPINGS[0]]  # b2b.nbdeli.com → mall_key "nbdeli"
    remapped_comix = ZhaoshangMallMapping(
        mall_name="齐心商城",
        source_id="S2",
        mall_key="comix",
        domains=("b2b.nbdeli.com",),
        vendor_keywords=("comix",),
    )
    make_document(prewarm_store, "DOC-1")
    document_row = prewarm_store.pending_documents(10)[0]
    fetcher = make_fetcher({"DOC-1": detail_payload([url])})
    first = process_document(
        prewarm_store,
        shared_cache,
        document_row,
        mappings=nbdeli_only,
        detail_fetch=fetcher,
    )
    assert first["detail_ok"] is True
    candidates = prewarm_store.candidates()
    assert len(candidates) == 1
    assert candidates[0]["mall"] == "nbdeli"
    first_candidate = candidates[0]

    # 映射表变化后,同一 URL 路由到另一个商城
    second = process_document(
        prewarm_store,
        shared_cache,
        document_row,
        mappings=[remapped_comix],
        detail_fetch=fetcher,
    )
    assert second["detail_ok"] is True
    by_mall = {candidate["mall"]: candidate for candidate in prewarm_store.candidates()}
    assert set(by_mall) == {"nbdeli", "comix"}
    # 旧候选逐字节保留(mall 仍是 nbdeli,绝不被改写)
    assert by_mall["nbdeli"] == first_candidate
    # 新 link 状态指向当前路由(mall B)的候选
    link = prewarm_store.links_for_document("DOC-1")[0]
    assert link["candidate_id"] == by_mall["comix"]["id"]
    assert link["mall"] == "comix"
    # 两个候选各自独立走到 PENDING_PREWARM
    assert by_mall["nbdeli"]["cache_status"] == "PENDING_PREWARM"
    assert by_mall["comix"]["cache_status"] == "PENDING_PREWARM"


def test_case12_original_url_provenance_jslink(prewarm_store, shared_cache):
    rewritten = "https://b2b.nbdeli.com/goods/400.html"
    shared_cache.record_success(
        mall="nbdeli",
        submitted_url=rewritten,
        real_sku_id="SKU-12",
        sku_status=SKU_STATUS_CONFIRMED,
        price="12.00",
        source="zhaoshang-history",
        succeeded_at="2026-09-01T00:00:00+00:00",
    )
    jslink_url = "https://www.jslink.com/goods/400.html"
    make_document(prewarm_store, "DOC-12")
    result = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-12": detail_payload([jslink_url])}),
    )
    assert result["detail_ok"] is True
    assert result["hits"] == 1
    candidate = _candidate(prewarm_store)
    assert candidate["original_url"] == jslink_url  # 原文永不改写
    assert candidate["lookup_url"] == rewritten
    assert candidate["routing_url"] == rewritten
    assert candidate["mall"] == "nbdeli"
    assert candidate["cache_status"] == "CACHE_HIT"
    link = prewarm_store.links_for_document("DOC-12")[0]
    assert link["original_url"] == jslink_url
    references = shared_cache.document_references("DOC-12")
    assert references[0]["original_url"] == rewritten  # 溯源按 lookup_url 记录


# ---- 未映射商城 / lookup 异常 / attach 失败 ------------------------------------------


def test_unmapped_mall_pending_prewarm_with_reason(prewarm_store, shared_cache):
    make_document(prewarm_store, "DOC-U")
    result = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-U": detail_payload(["https://unknown.example/item/9"])}),
    )
    assert result["detail_ok"] is True
    assert result["hits"] == 0 and result["misses"] == 1
    candidate = _candidate(prewarm_store)
    assert candidate["cache_status"] == "PENDING_PREWARM"
    assert candidate["mall"] == ""
    assert candidate["last_error"] == "unmapped mall: unknown.example"


def test_lookup_exception_becomes_retryable_miss(
    prewarm_store, shared_cache, monkeypatch
):
    url = "https://b2b.nbdeli.com/goods/950.html"
    real_lookup = shared_cache.lookup_current
    calls = {"n": 0}

    def flaky_lookup(mall, target):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("cache unavailable")
        return real_lookup(mall, target)

    monkeypatch.setattr(shared_cache, "lookup_current", flaky_lookup)
    make_document(prewarm_store, "DOC-L")
    result = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-L": detail_payload([url])}),
    )
    assert result["detail_ok"] is True
    assert result["misses"] == 1 and result["hits"] == 0
    candidate = _candidate(prewarm_store)
    assert candidate["cache_status"] == "PENDING_PREWARM"
    assert candidate["last_error"] == "lookup error: cache unavailable"
    # 缓存恢复并补上历史后,refresh 可以把它转成命中
    monkeypatch.undo()
    shared_cache.record_success(
        mall="nbdeli",
        submitted_url=url,
        real_sku_id="SKU-L",
        sku_status=SKU_STATUS_CONFIRMED,
        price="1.10",
        source="zhaoshang-history",
        succeeded_at="2026-09-02T00:00:00+00:00",
    )
    refreshed = refresh_pending_candidates(prewarm_store, shared_cache)
    assert refreshed == {"checked": 1, "hits": 1, "errors": []}
    assert _candidate(prewarm_store)["cache_status"] == "CACHE_HIT"


def test_attach_failure_is_warning_not_error(prewarm_store, shared_cache, monkeypatch):
    url = "https://b2b.nbdeli.com/goods/960.html"
    shared_cache.record_success(
        mall="nbdeli",
        submitted_url=url,
        real_sku_id="SKU-A",
        sku_status=SKU_STATUS_CONFIRMED,
        price="2.20",
        source="zhaoshang-history",
        succeeded_at="2026-09-01T00:00:00+00:00",
    )

    def broken_attach(*args, **kwargs):
        raise RuntimeError("reference store unavailable")

    monkeypatch.setattr(shared_cache, "attach_document_reference", broken_attach)
    make_document(prewarm_store, "DOC-A")
    result = process_document(
        prewarm_store,
        shared_cache,
        prewarm_store.pending_documents(10)[0],
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-A": detail_payload([url])}),
    )
    assert result["detail_ok"] is True
    assert result["hits"] == 1 and result["errors"] == []
    assert result["warnings"]
    assert _candidate(prewarm_store)["cache_status"] == "CACHE_HIT"


# ---- Phase D: candidate 刷新 --------------------------------------------------------


def test_refresh_pending_candidates(prewarm_store, shared_cache):
    miss_a = "https://b2b.nbdeli.com/goods/970.html"
    miss_b = "https://b2b.nbdeli.com/goods/971.html"
    unmapped = "https://unknown.example/item/1"
    for index, url in enumerate((miss_a, miss_b, unmapped), start=1):
        make_document(prewarm_store, f"DOC-{index}")
        process_document(
            prewarm_store,
            shared_cache,
            prewarm_store.pending_documents(10)[0],
            mappings=MAPPINGS,
            detail_fetch=make_fetcher({f"DOC-{index}": detail_payload([url])}),
        )
    assert len(prewarm_store.list_unique_cache_misses()) == 3
    # 当日互联网爬取把其中两个 URL 同步进了共享缓存
    shared_cache.record_success(
        mall="nbdeli",
        submitted_url=miss_a,
        real_sku_id="S-A",
        sku_status=SKU_STATUS_CONFIRMED,
        price="7.70",
        source="daily-internet",
        succeeded_at="2026-09-07T02:00:00+00:00",
    )
    shared_cache.record_success(
        mall="nbdeli",
        submitted_url=miss_b,
        real_sku_id="S-B",
        sku_status=SKU_STATUS_CONFIRMED,
        price="7.71",
        source="daily-internet",
        succeeded_at="2026-09-07T02:00:00+00:00",
    )
    refreshed = refresh_pending_candidates(prewarm_store, shared_cache)
    assert refreshed["checked"] == 2  # 未映射 mall=='' 的候选被跳过
    assert refreshed["hits"] == 2
    assert refreshed["errors"] == []
    # 两个已同步 URL 转 CACHE_HIT;未映射候选永远留在 MISS 列表(刷新器跳过)
    remaining = prewarm_store.list_unique_cache_misses()
    assert [row["original_url"] for row in remaining] == [unmapped]
    statuses = {row["original_url"]: row["cache_status"] for row in prewarm_store.candidates()}
    assert statuses[miss_a] == "CACHE_HIT"
    assert statuses[miss_b] == "CACHE_HIT"
    assert statuses[unmapped] == "PENDING_PREWARM"
    # 每个关联 document 都拿到溯源
    assert len(shared_cache.document_references("DOC-1")) == 1
    assert len(shared_cache.document_references("DOC-2")) == 1
    assert shared_cache.document_references("DOC-3") == []


# ---- run_prewarm_scan 编排 ------------------------------------------------------------


def _insert_audit_items(state_db_path, business_date, audit_rows):
    connection = sqlite3.connect(str(state_db_path), isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = "2026-09-07T03:00:00+00:00"
        run_id = connection.execute(
            "SELECT id FROM daily_runs WHERE business_date=?", (business_date,)
        ).fetchone()[0]
        for index, row in enumerate(audit_rows, start=1):
            connection.execute(
                """INSERT INTO audit_items
                   (run_id, v_commodity_id, sku_id, source_fingerprint,
                    customer_fingerprint, row_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    str(row.get("documentId", f"VC-{index}")),
                    str(row.get("skuId", f"SKU-{index}")),
                    f"src-fp-{index}",
                    "",
                    json.dumps(row, ensure_ascii=False),
                    now,
                ),
            )
        connection.execute("COMMIT")
    finally:
        connection.close()


def test_run_prewarm_scan_phases_in_order(tmp_path, prewarm_store, shared_cache):
    from tests.test_customer_prewarm_daily_sync import make_state_db

    url = "https://b2b.nbdeli.com/goods/930.html"
    state_db_path = make_state_db(
        tmp_path,
        BUSINESS_DATE,
        rows=[
            {
                "mall_key": "nbdeli",
                "sku_id": "S1",
                "exact_url": url,
                "result": {"page_price_status": "OK", "page_price_value": "6.6"},
                "task_name": "task-x",
            }
        ],
    )
    _insert_audit_items(
        state_db_path, BUSINESS_DATE, [{"documentId": "DOC-9", "fourCategoryId": "CAT-9"}]
    )
    summary = run_prewarm_scan(
        prewarm_store=prewarm_store,
        shared_cache=shared_cache,
        business_date=BUSINESS_DATE,
        state_db_path=state_db_path,
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-9": detail_payload([url])}),
        max_documents=10,
    )
    assert summary["phase_errors"] == []
    assert summary["state_db"] == "ok"
    assert summary["ok_synced"] == 1  # Phase A: 每日同步先跑
    assert summary["intake_count"] == 1  # Phase B: 补扫 intake
    assert summary["documents_processed"] == 1  # Phase C
    assert summary["candidate_hits"] == 1  # Phase A 同步的条目当轮即可被 C 命中
    assert summary["refresh_checked"] == 0  # Phase D: 无遗留 MISS
    assert summary["stats"]["candidates"]["CACHE_HIT"] == 1
    assert prewarm_store.list_unique_cache_misses() == []


def test_run_prewarm_scan_phase_error_isolated(tmp_path, prewarm_store, shared_cache):
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"definitely not a sqlite database")
    summary = run_prewarm_scan(
        prewarm_store=prewarm_store,
        shared_cache=shared_cache,
        business_date=BUSINESS_DATE,
        state_db_path=broken,
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({}),
        max_documents=10,
    )
    phases = {entry["phase"] for entry in summary["phase_errors"]}
    assert "daily_sync" in phases
    assert "catch_up_intake" in phases
    assert summary["documents_processed"] == 0
    assert summary["intake_count"] == 0
    assert "stats" in summary


def test_run_prewarm_scan_budget_stops_detail_phase(tmp_path, prewarm_store, shared_cache):
    from tests.test_customer_prewarm_daily_sync import make_state_db

    state_db_path = make_state_db(tmp_path, BUSINESS_DATE, rows=[])
    _insert_audit_items(
        state_db_path, BUSINESS_DATE, [{"documentId": "DOC-B", "fourCategoryId": "CAT-B"}]
    )

    class _StepClock:
        def __init__(self, step):
            self.step = step
            self.value = 0.0

        def __call__(self):
            self.value += self.step
            return self.value

    clock = _StepClock(20.0)
    summary = run_prewarm_scan(
        prewarm_store=prewarm_store,
        shared_cache=shared_cache,
        business_date=BUSINESS_DATE,
        state_db_path=state_db_path,
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-B": detail_payload(["https://b2b.nbdeli.com/goods/1.html"])}),
        max_documents=10,
        monotonic=clock,
        budget_seconds=50.0,
    )
    # start=20 → deadline=70;Phase C 首次检查 monotonic()=40,余 30>5 → 处理;
    # 第二次检查 60,余 10>5 → 但文档只有 1 个
    assert summary["documents_processed"] == 1

    _insert_audit_items(
        state_db_path, BUSINESS_DATE, [{"documentId": "DOC-C", "fourCategoryId": "CAT-C"}]
    )
    clock = _StepClock(20.0)
    summary = run_prewarm_scan(
        prewarm_store=prewarm_store,
        shared_cache=shared_cache,
        business_date=BUSINESS_DATE,
        state_db_path=state_db_path,
        mappings=MAPPINGS,
        detail_fetch=make_fetcher({"DOC-C": detail_payload(["https://b2b.nbdeli.com/goods/2.html"])}),
        max_documents=10,
        monotonic=clock,
        budget_seconds=25.0,
    )
    # start=20 → deadline=45;Phase C 首次检查 monotonic()=40,余 5,不大于 5 → 跳过
    assert summary["documents_processed"] == 0
    assert prewarm_store.pending_documents(10)[0]["document_id"] == "DOC-C"


# ---- sync_daily_internet_results 缺库冒烟(详细行为见 daily_sync 测试) ----------------


def test_sync_missing_state_db_returns_missing(prewarm_store, shared_cache, tmp_path):
    summary = sync_daily_internet_results(
        prewarm_store,
        shared_cache,
        state_db_path=tmp_path / "nope.sqlite3",
        business_date=BUSINESS_DATE,
    )
    assert summary["state_db"] == "missing"
    assert summary["scanned"] == 0
    assert summary["ok_synced"] == 0


# ---- DailyAuditStore 引用冒烟(确保状态库 schema 可建) ---------------------------------


def test_daily_audit_store_schema_available(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    assert "crawl_work" in store.table_names()
