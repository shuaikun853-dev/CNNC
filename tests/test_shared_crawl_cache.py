"""Behavioural tests for the shared crawl cache and URL identity model (Issue #7).

Pure data-layer tests: every test builds its own ``SharedCrawlCache`` under
``tmp_path`` and never touches the production ``runtime/`` tree.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from app.services.crawl_url_identity import (
    MALL_IDENTITY_PARSERS,
    PLACEHOLDER_PREFIXES_DEFAULT,
    SKU_STATUSES,
    SKU_STATUS_CONFIRMED,
    SKU_STATUS_UNRESOLVED,
    SKU_STATUS_URL_PARSED_UNVERIFIED,
    is_placeholder_code,
    ofs_product_identity,
    resolve_mall_product_identity,
)
from app.services.shared_crawl_cache import AliasConflictError, SharedCrawlCache


JD_URL = "https://item.jd.com/100012043978.html"


@pytest.fixture()
def cache_path(tmp_path):
    return tmp_path / "cache.sqlite3"


@pytest.fixture()
def cache(cache_path):
    return SharedCrawlCache(cache_path)


def _current_row_count(cache: SharedCrawlCache, identity_id: int | None = None) -> int:
    """Direct sqlite3 read of is_current rows; bypasses the public API on purpose."""
    connection = sqlite3.connect(str(cache.path))
    try:
        if identity_id is None:
            sql = "SELECT COUNT(*) FROM crawl_results WHERE is_current=1"
            params: tuple = ()
        else:
            sql = "SELECT COUNT(*) FROM crawl_results WHERE identity_id=? AND is_current=1"
            params = (identity_id,)
        return int(connection.execute(sql, params).fetchone()[0])
    finally:
        connection.close()


# ---- URL identity module (Level 3 parser) -----------------------------------


def test_ofs_product_identity_parsing():
    assert ofs_product_identity("https://www.ofs.cn/product-6287660.html") == "6287660"
    assert (
        ofs_product_identity("https://www.ofs.cn/548789saweqsaeqwwe/product-6287660.html")
        == "6287660"
    )
    assert ofs_product_identity("https://www.ofs.cn/product-6287660.html/") == "6287660"
    assert ofs_product_identity("https://www.ofs.cn/PRODUCT-6287660.HTML") == "6287660"
    assert ofs_product_identity("https://www.ofs.cn/other/6287660") is None
    assert ofs_product_identity("https://www.ofs.cn/product-abc.html") is None
    assert ofs_product_identity("") is None
    assert ofs_product_identity("not a url") is None
    # Query/fragment tails must never contribute an identity: only the URL
    # path counts, so a disguised /product-<digits>.html suffix is a MISS.
    assert ofs_product_identity("https://www.ofs.cn/redirect?to=/product-999.html") is None
    assert (
        ofs_product_identity("https://www.ofs.cn/redirect?to=https://evil.cn/product-123.html")
        is None
    )
    assert ofs_product_identity("https://www.ofs.cn/x#product-999.html") is None


def test_resolve_mall_product_identity_is_mall_scoped():
    assert resolve_mall_product_identity("ofs", "https://www.ofs.cn/product-1.html") == "1"
    # jd has no registered parser in v1: conservative MISS even for an ofs URL.
    assert resolve_mall_product_identity("jd", "https://www.ofs.cn/product-1.html") is None
    assert resolve_mall_product_identity("", "https://www.ofs.cn/product-1.html") is None
    assert resolve_mall_product_identity("ofs", "") is None
    assert resolve_mall_product_identity("ofs", "   ") is None
    assert set(MALL_IDENTITY_PARSERS) == {"ofs"}


def test_placeholder_and_status_constants():
    assert is_placeholder_code("zhpq-000001")
    assert is_placeholder_code("  ZHPQ-7 ")
    assert not is_placeholder_code("123456")
    assert not is_placeholder_code("")
    assert is_placeholder_code("xx-1", prefixes=("xx-",))
    assert SKU_STATUSES == frozenset(
        {"CONFIRMED", "URL_PARSED_UNVERIFIED", "UNRESOLVED"}
    )
    assert PLACEHOLDER_PREFIXES_DEFAULT == ("zhpq-",)


# ---- Case 1..10 --------------------------------------------------------------


def test_case1_exact_url(cache):
    entry = cache.record_success(
        mall="jd",
        submitted_url=JD_URL,
        price="109.00",
        source="unit-test",
    )
    hit = cache.lookup_current("jd", JD_URL)
    assert hit is not None
    assert hit["match_level"] == "EXACT"
    assert hit["entry"]["id"] == entry["id"]
    assert hit["entry"]["price"] == "109.00"


def test_case2_rewritten_url_maps_to_same_entry(cache):
    original = "https://www.ofs.cn/548789saweqsaeqwwe/product-6287660.html"
    returned = "https://www.ofs.cn/product-6287660.html"
    entry = cache.record_success(
        mall="ofs",
        submitted_url=original,
        returned_url=returned,
        source="unit-test",
    )
    hit_original = cache.lookup_current("ofs", original)
    hit_returned = cache.lookup_current("ofs", returned)
    assert hit_original is not None
    assert hit_returned is not None
    assert hit_original["entry"]["id"] == entry["id"]
    assert hit_returned["entry"]["id"] == entry["id"]
    assert hit_original["entry"]["id"] == hit_returned["entry"]["id"]


def test_case3_multiple_successes_keep_history_and_single_current(cache):
    url = "https://item.jd.com/100012043978.html"
    cache.record_success(
        mall="jd",
        submitted_url=url,
        price="30",
        source="unit-test",
        succeeded_at="2026-08-20T10:00:00+00:00",
    )
    cache.record_success(
        mall="jd",
        submitted_url=url,
        price="28",
        source="unit-test",
        succeeded_at="2026-08-25T10:00:00+00:00",
    )
    history = cache.history("jd", url)
    assert len(history) == 2
    assert [row["price"] for row in history] == ["30", "28"]
    assert [row["succeeded_at"] for row in history] == [
        "2026-08-20T10:00:00+00:00",
        "2026-08-25T10:00:00+00:00",
    ]
    current = cache.lookup_current("jd", url)
    assert current is not None
    assert current["entry"]["price"] == "28"
    assert current["entry"]["is_current"] is True
    assert current["history_count"] == 2
    assert _current_row_count(cache, current["identity_id"]) == 1


def test_case4_same_sku_across_malls_not_merged(cache):
    url = "https://mall.example.com/item/1"
    for mall in ("jd", "zkh"):
        cache.record_success(
            mall=mall,
            submitted_url=url,
            real_sku_id="123",
            sku_status=SKU_STATUS_CONFIRMED,
            source="unit-test",
        )
    jd_hit = cache.lookup_current("jd", url)
    zkh_hit = cache.lookup_current("zkh", url)
    assert jd_hit is not None
    assert zkh_hit is not None
    assert jd_hit["entry"]["id"] != zkh_hit["entry"]["id"]
    assert jd_hit["identity_id"] != zkh_hit["identity_id"]
    assert jd_hit["entry"]["mall"] == "jd"
    assert zkh_hit["entry"]["mall"] == "zkh"
    assert len(cache.history("jd", url)) == 1
    assert len(cache.history("zkh", url)) == 1


def test_case5_same_mall_different_urls_same_sku_not_merged(cache):
    url_a = "https://item.jd.com/111.html"
    url_b = "https://item.jd.com/222.html"
    entry_a = cache.record_success(
        mall="jd",
        submitted_url=url_a,
        real_sku_id="123",
        sku_status=SKU_STATUS_CONFIRMED,
        source="unit-test",
    )
    entry_b = cache.record_success(
        mall="jd",
        submitted_url=url_b,
        real_sku_id="123",
        sku_status=SKU_STATUS_CONFIRMED,
        source="unit-test",
    )
    assert entry_a["identity_id"] != entry_b["identity_id"]
    hit_a = cache.lookup_current("jd", url_a)
    hit_b = cache.lookup_current("jd", url_b)
    assert hit_a is not None
    assert hit_b is not None
    assert hit_a["entry"]["id"] == entry_a["id"]
    assert hit_b["entry"]["id"] == entry_b["id"]
    assert hit_a["entry"]["id"] != hit_b["entry"]["id"]


def test_case6_url_parsed_sku_stays_unverified(cache):
    entry = cache.record_success(
        mall="ofs",
        submitted_url="https://www.ofs.cn/product-6287660.html",
        real_sku_id="6287660",
        sku_status=SKU_STATUS_URL_PARSED_UNVERIFIED,
        source="unit-test",
    )
    assert entry["sku_status"] == "URL_PARSED_UNVERIFIED"
    hit = cache.lookup_current("ofs", "https://www.ofs.cn/product-6287660.html")
    assert hit is not None
    # No API may silently promote URL-parsed evidence to CONFIRMED: the stored
    # state must come back exactly as written.
    assert hit["entry"]["sku_status"] == "URL_PARSED_UNVERIFIED"
    assert hit["entry"]["real_sku_id"] == "6287660"


def test_case7_placeholder_sku_rejected(cache):
    url = "https://item.jd.com/777.html"
    cache.record_success(
        mall="jd",
        submitted_url=url,
        crawl_id="zhpq-000001",
        source="unit-test",
    )
    with pytest.raises(ValueError):
        cache.record_success(
            mall="jd",
            submitted_url=url,
            real_sku_id="zhpq-000001",
            sku_status=SKU_STATUS_CONFIRMED,
            source="unit-test",
        )
    with pytest.raises(ValueError):
        cache.record_success(
            mall="jd",
            submitted_url=url,
            crawl_id="zhpq-000002",
            real_sku_id="zhpq-000002",
            sku_status=SKU_STATUS_CONFIRMED,
            source="unit-test",
        )
    hit = cache.lookup_current("jd", url)
    assert hit is not None
    assert hit["entry"]["real_sku_id"] != "zhpq-000001"


def test_case8_unresolved_success_saved(cache):
    url = "https://item.jd.com/888.html"
    entry = cache.record_success(mall="jd", submitted_url=url, source="unit-test")
    hit = cache.lookup_current("jd", url)
    assert hit is not None
    assert hit["entry"]["id"] == entry["id"]
    assert hit["entry"]["real_sku_id"] is None
    assert hit["entry"]["sku_status"] == "UNRESOLVED"


def test_case9_document_provenance(cache):
    url_a = "https://www.ofs.cn/product-6287660.html"
    url_b = "https://www.ofs.cn/548789saweqsaeqwwe/product-6287660.html"
    cache.record_success(
        mall="ofs",
        submitted_url=url_a,
        returned_url=url_a,
        source="unit-test",
    )
    ref_a = cache.attach_document_reference(
        "DOC-A", mall="ofs", url=url_a, source="unit-test"
    )
    ref_b = cache.attach_document_reference(
        "DOC-B", mall="ofs", url=url_b, source="unit-test"
    )
    assert ref_a is not None
    assert ref_b is not None
    docs_a = cache.document_references("DOC-A")
    docs_b = cache.document_references("DOC-B")
    assert len(docs_a) == 1
    assert docs_a[0]["original_url"] == url_a
    assert len(docs_b) == 1
    assert docs_b[0]["original_url"] == url_b
    assert docs_a[0]["identity_id"] == docs_b[0]["identity_id"]
    current = cache.lookup_current("ofs", url_a)
    assert current is not None
    assert current["entry"]["is_current"] is True
    assert _current_row_count(cache, current["identity_id"]) == 1


def test_case10_alias_conflict_fails_closed(cache):
    url1 = "https://item.jd.com/1.html"
    url2 = "https://item.jd.com/2.html"
    first = cache.record_success(mall="jd", submitted_url=url1, source="unit-test")
    with pytest.raises(AliasConflictError):
        cache.record_success(
            mall="jd",
            submitted_url=url2,
            returned_url=url1,
            source="unit-test",
        )
    # The whole transaction rolled back: no residue for the conflicting record.
    assert cache.lookup_current("jd", url2) is None
    hit1 = cache.lookup_current("jd", url1)
    assert hit1 is not None
    assert hit1["entry"]["id"] == first["id"]


# ---- Behavioural hard rules --------------------------------------------------


def test_sku_status_validation(cache):
    with pytest.raises(ValueError):
        cache.record_success(
            mall="jd",
            submitted_url=JD_URL,
            sku_status=SKU_STATUS_CONFIRMED,
            source="unit-test",
        )
    with pytest.raises(ValueError):
        cache.record_success(
            mall="jd",
            submitted_url=JD_URL,
            real_sku_id="123",
            sku_status=SKU_STATUS_UNRESOLVED,
            source="unit-test",
        )
    with pytest.raises(ValueError):
        cache.record_success(
            mall="jd",
            submitted_url=JD_URL,
            sku_status="TOTALLY_MADE_UP",
            source="unit-test",
        )


def test_current_never_two(cache):
    url = "https://item.jd.com/100012043978.html"
    for index in range(3):
        cache.record_success(
            mall="jd",
            submitted_url=url,
            price=str(100 + index),
            source="unit-test",
        )
    assert _current_row_count(cache) == 1


def test_late_older_success_does_not_steal_current(cache):
    url = "https://item.jd.com/555.html"
    cache.record_success(
        mall="jd",
        submitted_url=url,
        price="30",
        source="unit-test",
        succeeded_at="2026-08-20T10:00:00+00:00",
    )
    cache.record_success(
        mall="jd",
        submitted_url=url,
        price="28",
        source="unit-test",
        succeeded_at="2026-08-25T10:00:00+00:00",
    )
    cache.record_success(
        mall="jd",
        submitted_url=url,
        price="26",
        source="unit-test",
        succeeded_at="2026-08-22T10:00:00+00:00",
    )
    current = cache.lookup_current("jd", url)
    assert current is not None
    assert current["entry"]["price"] == "28"
    assert current["entry"]["is_current"] is True
    assert len(cache.history("jd", url)) == 3


def test_lookup_miss_returns_none(cache):
    assert cache.lookup_current("jd", "https://item.jd.com/not-recorded.html") is None
    assert cache.lookup_current("", JD_URL) is None
    assert cache.lookup_current("jd", "   ") is None
    assert cache.history("jd", "https://item.jd.com/not-recorded.html") == []


def test_mall_parser_level3_hit(cache):
    url = "https://www.ofs.cn/product-6287660.html"
    entry = cache.record_success(
        mall="ofs",
        submitted_url=url,
        returned_url=url,
        source="unit-test",
    )
    # This URL shape was never registered as an alias; only Level 3 can find it.
    unregistered = "https://www.ofs.cn/xxx/product-6287660.html"
    hit = cache.lookup_current("ofs", unregistered)
    assert hit is not None
    assert hit["match_level"] == "MALL_PARSER"
    assert hit["entry"]["id"] == entry["id"]


def test_reopen_idempotent(cache, cache_path):
    entry = cache.record_success(
        mall="jd",
        submitted_url=JD_URL,
        price="109.00",
        source="unit-test",
    )
    tables_before = SharedCrawlCache(cache_path).table_names()
    reopened = SharedCrawlCache(cache_path)
    assert reopened.table_names() == tables_before
    hit = reopened.lookup_current("jd", JD_URL)
    assert hit is not None
    assert hit["entry"]["id"] == entry["id"]


# ---- Validation and normalisation details ------------------------------------


def test_required_fields_fail_closed(cache):
    with pytest.raises(ValueError):
        cache.record_success(mall="  ", submitted_url=JD_URL, source="unit-test")
    with pytest.raises(ValueError):
        cache.record_success(mall="jd", submitted_url="   ", source="unit-test")
    with pytest.raises(ValueError):
        cache.record_success(mall="jd", submitted_url=JD_URL, source="")


def test_succeeded_at_requires_explicit_utc_offset(cache):
    with pytest.raises(ValueError):
        cache.record_success(
            mall="jd",
            submitted_url=JD_URL,
            source="unit-test",
            succeeded_at="2026-08-20T10:00:00",
        )
    entry = cache.record_success(
        mall="jd",
        submitted_url=JD_URL,
        source="unit-test",
        succeeded_at="2026-08-20T18:00:00+08:00",
    )
    assert entry["succeeded_at"] == "2026-08-20T10:00:00+00:00"


def test_price_accepts_numeric_types(cache):
    entry = cache.record_success(
        mall="jd",
        submitted_url="https://item.jd.com/300.html",
        price=Decimal("30.10"),
        source="unit-test",
    )
    assert entry["price"] == "30.10"
    entry = cache.record_success(
        mall="jd",
        submitted_url="https://item.jd.com/301.html",
        price=12.5,
        source="unit-test",
    )
    assert entry["price"] == "12.5"
    assert entry["payload"] == {}
    with pytest.raises(ValueError):
        cache.record_success(
            mall="jd",
            submitted_url="https://item.jd.com/302.html",
            price=["not", "a", "price"],
            source="unit-test",
        )


def test_comment_count_rejects_bool(cache):
    with pytest.raises(ValueError):
        cache.record_success(
            mall="jd",
            submitted_url="https://item.jd.com/304.html",
            comment_count=True,
            source="unit-test",
        )
    with pytest.raises(ValueError):
        cache.record_success(
            mall="jd",
            submitted_url="https://item.jd.com/305.html",
            comment_count=False,
            source="unit-test",
        )


def test_payload_roundtrip(cache):
    entry = cache.record_success(
        mall="jd",
        submitted_url="https://item.jd.com/303.html",
        source="unit-test",
        payload={"comment": 3, "title": "示例标题"},
    )
    assert entry["payload"] == {"comment": 3, "title": "示例标题"}
    hit = cache.lookup_current("jd", "https://item.jd.com/303.html")
    assert hit is not None
    assert hit["entry"]["payload"] == {"comment": 3, "title": "示例标题"}


def test_attach_document_reference_requires_resolved_identity(cache):
    assert (
        cache.attach_document_reference(
            "DOC-X", mall="jd", url="https://item.jd.com/unknown.html"
        )
        is None
    )
    assert cache.document_references("DOC-X") == []
