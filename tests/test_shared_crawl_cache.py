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
    SKU_STATUS_UPSTREAM_PROVIDED,
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


def _table_row_count(cache: SharedCrawlCache, table: str) -> int:
    """Direct sqlite3 row count of one table; bypasses the public API on purpose."""
    connection = sqlite3.connect(str(cache.path))
    try:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        connection.close()


def _identity_product_identity(cache: SharedCrawlCache, identity_id: int) -> str | None:
    """Direct sqlite3 read of one crawl_identities.product_identity."""
    connection = sqlite3.connect(str(cache.path))
    try:
        row = connection.execute(
            "SELECT product_identity FROM crawl_identities WHERE id=?", (identity_id,)
        ).fetchone()
        return None if row is None else row[0]
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


def test_ofs_product_identity_hostname_policy():
    # Hostname allow policy: ofs.cn, www.ofs.cn, and any real subdomain
    # *.ofs.cn.  Anything else (evilofs.cn, ofs.cn.evil.com, example.com)
    # must be a conservative MISS.
    assert ofs_product_identity("https://www.ofs.cn/product-1.html") == "1"
    assert ofs_product_identity("https://ofs.cn/product-1.html") == "1"
    assert ofs_product_identity("https://foo.ofs.cn/product-1.html") == "1"
    assert ofs_product_identity("https://evilofs.cn/product-1.html") is None
    assert ofs_product_identity("https://ofs.cn.evil.com/product-1.html") is None
    assert ofs_product_identity("https://example.com/product-1.html") is None


def test_resolve_mall_product_identity_is_mall_scoped():
    assert resolve_mall_product_identity("ofs", "https://www.ofs.cn/product-1.html") == "1"
    # jd has no registered parser: conservative MISS even for an ofs URL.
    assert resolve_mall_product_identity("jd", "https://www.ofs.cn/product-1.html") is None
    assert resolve_mall_product_identity("", "https://www.ofs.cn/product-1.html") is None
    assert resolve_mall_product_identity("ofs", "") is None
    assert resolve_mall_product_identity("ofs", "   ") is None
    # Issue #11: stbchina joins ofs as the second registered parser.
    assert set(MALL_IDENTITY_PARSERS) == {"ofs", "stbchina"}


def test_placeholder_and_status_constants():
    assert is_placeholder_code("zhpq-000001")
    assert is_placeholder_code("  ZHPQ-7 ")
    assert not is_placeholder_code("123456")
    assert not is_placeholder_code("")
    assert is_placeholder_code("xx-1", prefixes=("xx-",))
    assert SKU_STATUSES == frozenset(
        {"CONFIRMED", "URL_PARSED_UNVERIFIED", "UNRESOLVED", "UPSTREAM_PROVIDED"}
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
        "2026-08-20T10:00:00.000000+00:00",
        "2026-08-25T10:00:00.000000+00:00",
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
    # A true dual-identity conflict: URL A and URL B already own two distinct
    # NULL identities (jd has no parser); a record whose chain spans both must
    # fail closed instead of merging the two existing identity groups.
    url_a = "https://item.jd.com/a.html"
    url_b = "https://item.jd.com/b.html"
    first_a = cache.record_success(mall="jd", submitted_url=url_a, source="unit-test")
    first_b = cache.record_success(mall="jd", submitted_url=url_b, source="unit-test")
    assert first_a["identity_id"] != first_b["identity_id"]
    with pytest.raises(AliasConflictError):
        cache.record_success(
            mall="jd",
            submitted_url=url_a,
            original_url=url_a,
            returned_url=url_b,
            source="unit-test",
        )
    # The whole transaction rolled back: both identities keep their original
    # single success, and no new alias rows were left behind.
    hit_a = cache.lookup_current("jd", url_a)
    hit_b = cache.lookup_current("jd", url_b)
    assert hit_a is not None
    assert hit_b is not None
    assert hit_a["entry"]["id"] == first_a["id"]
    assert hit_b["entry"]["id"] == first_b["id"]
    assert hit_a["identity_id"] != hit_b["identity_id"]
    assert hit_a["history_count"] == 1
    assert hit_b["history_count"] == 1
    assert _table_row_count(cache, "crawl_urls") == 2


def test_case10a_dirty_url_merges_into_known_returned_identity(cache):
    # A brand-new dirty submitted URL whose returned/canonical URL is already
    # known must merge into that identity, not conflict (this also covers the
    # simplest unknown-submitted + known-returned variant).
    dirty_a = "https://item.jd.com/dirty-a.html"
    dirty_c = "https://item.jd.com/dirty-c.html"
    canonical_b = "https://item.jd.com/canonical-b.html"
    cache.record_success(
        mall="jd",
        submitted_url=dirty_a,
        returned_url=canonical_b,
        price="first",
        source="unit-test",
    )
    cache.record_success(
        mall="jd",
        submitted_url=dirty_c,
        returned_url=canonical_b,
        price="second",
        source="unit-test",
    )
    hit_a = cache.lookup_current("jd", dirty_a)
    hit_b = cache.lookup_current("jd", canonical_b)
    hit_c = cache.lookup_current("jd", dirty_c)
    assert hit_a is not None
    assert hit_b is not None
    assert hit_c is not None
    identity_id = hit_a["identity_id"]
    assert hit_b["identity_id"] == identity_id
    assert hit_c["identity_id"] == identity_id
    assert hit_a["history_count"] == 2
    assert hit_b["history_count"] == 2
    assert hit_c["history_count"] == 2
    assert hit_c["entry"]["price"] == "second"
    assert hit_c["entry"]["is_current"] is True


# ---- Round 3: parser evidence set (whole-chain parsing, NULL promotion) ------


def test_case_r3_1_known_product_not_polluted_by_returned_conflict(cache):
    url_111 = "https://ofs.cn/product-111.html"
    url_222 = "https://ofs.cn/product-222.html"
    first = cache.record_success(mall="ofs", submitted_url=url_111, source="unit-test")
    with pytest.raises(AliasConflictError):
        cache.record_success(
            mall="ofs",
            submitted_url=url_111,
            original_url=url_111,
            returned_url=url_222,
            source="unit-test",
        )
    # The chain itself parses to two product identities: fail-closed with no
    # residue, and the known 111 identity stays untouched.
    assert _table_row_count(cache, "crawl_urls") == 1
    assert _table_row_count(cache, "crawl_results") == 1
    connection = sqlite3.connect(str(cache.path))
    try:
        row_222 = connection.execute(
            "SELECT COUNT(*) FROM crawl_urls WHERE url=?", (url_222,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert row_222 == 0
    hit = cache.lookup_current("ofs", url_111)
    assert hit is not None
    assert hit["entry"]["id"] == first["id"]
    assert hit["history_count"] == 1


def test_case_r3_2_two_fresh_parser_identities_conflict(cache):
    # Rule A: even when neither parsed identity exists yet, one chain whose
    # URLs parse to two different product identities is self-contradictory.
    with pytest.raises(AliasConflictError):
        cache.record_success(
            mall="ofs",
            submitted_url="https://ofs.cn/product-111.html",
            original_url="https://ofs.cn/product-111.html",
            returned_url="https://ofs.cn/product-222.html",
            source="unit-test",
        )
    assert _table_row_count(cache, "crawl_identities") == 0
    assert _table_row_count(cache, "crawl_urls") == 0
    assert _table_row_count(cache, "crawl_results") == 0


def test_case_r3_3_null_identity_promotes_to_parsed_identity(cache):
    dirty = "https://ofs.cn/detail?id=999"
    product = "https://ofs.cn/product-999.html"
    cache.record_success(mall="ofs", submitted_url=dirty, source="unit-test")
    cache.record_success(
        mall="ofs",
        submitted_url=dirty,
        returned_url=product,
        price="second",
        source="unit-test",
    )
    hit_dirty = cache.lookup_current("ofs", dirty)
    assert hit_dirty is not None
    identity_id = hit_dirty["identity_id"]
    # The formerly NULL identity learned its parsed product identity.
    assert _identity_product_identity(cache, identity_id) == "999"
    assert hit_dirty["history_count"] == 2
    hit_product = cache.lookup_current("ofs", product)
    assert hit_product is not None
    assert hit_product["identity_id"] == identity_id
    # Level 3 now resolves unregistered aliases of the learned identity.
    unregistered = "https://ofs.cn/foo/product-999.html"
    hit_level3 = cache.lookup_current("ofs", unregistered)
    assert hit_level3 is not None
    assert hit_level3["match_level"] == "MALL_PARSER"
    assert hit_level3["identity_id"] == identity_id


def test_case_r3_4_promotion_blocked_when_identity_taken(cache):
    dirty = "https://ofs.cn/detail?id=999"
    product = "https://ofs.cn/product-999.html"
    cache.record_success(mall="ofs", submitted_url=dirty, source="unit-test")
    cache.record_success(mall="ofs", submitted_url=product, source="unit-test")
    hit_dirty = cache.lookup_current("ofs", dirty)
    hit_product = cache.lookup_current("ofs", product)
    assert hit_dirty is not None
    assert hit_product is not None
    identity_null = hit_dirty["identity_id"]
    identity_parsed = hit_product["identity_id"]
    assert identity_null != identity_parsed
    # The chain reaches both identities (dirty alias -> I1, parsed 999 -> I2):
    # promotion must not merge or overwrite either side.
    with pytest.raises(AliasConflictError):
        cache.record_success(
            mall="ofs",
            submitted_url=dirty,
            original_url=dirty,
            returned_url=product,
            source="unit-test",
        )
    assert _identity_product_identity(cache, identity_null) is None
    assert _identity_product_identity(cache, identity_parsed) == "999"
    assert _table_row_count(cache, "crawl_identities") == 2
    assert _table_row_count(cache, "crawl_urls") == 2
    assert _table_row_count(cache, "crawl_results") == 2


def test_case_r3_5_same_parsed_identity_multiple_urls_ok(cache):
    entry = cache.record_success(
        mall="ofs",
        submitted_url="https://ofs.cn/product-999.html",
        original_url="https://ofs.cn/foo/product-999.html",
        source="unit-test",
    )
    hit_original = cache.lookup_current("ofs", "https://ofs.cn/foo/product-999.html")
    hit_submitted = cache.lookup_current("ofs", "https://ofs.cn/product-999.html")
    assert hit_original is not None
    assert hit_submitted is not None
    assert hit_original["identity_id"] == entry["identity_id"]
    assert hit_submitted["identity_id"] == entry["identity_id"]
    assert hit_original["history_count"] == 1


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


def test_microsecond_precision_decides_current(cache):
    url = "https://item.jd.com/600.html"
    cache.record_success(
        mall="jd",
        submitted_url=url,
        price="NEWER",
        source="unit-test",
        succeeded_at="2026-09-04T10:00:00.900000+00:00",
    )
    cache.record_success(
        mall="jd",
        submitted_url=url,
        price="OLDER",
        source="unit-test",
        succeeded_at="2026-09-04T10:00:00.100000+00:00",
    )
    history = cache.history("jd", url)
    assert len(history) == 2
    current = cache.lookup_current("jd", url)
    assert current is not None
    assert current["entry"]["price"] == "NEWER"
    assert _current_row_count(cache) == 1


def test_identical_timestamp_id_tiebreak(cache):
    url = "https://item.jd.com/601.html"
    cache.record_success(
        mall="jd",
        submitted_url=url,
        price="FIRST",
        source="unit-test",
        succeeded_at="2026-09-04T11:00:00.000000+00:00",
    )
    cache.record_success(
        mall="jd",
        submitted_url=url,
        price="SECOND",
        source="unit-test",
        succeeded_at="2026-09-04T11:00:00.000000+00:00",
    )
    current = cache.lookup_current("jd", url)
    assert current is not None
    assert current["entry"]["price"] == "SECOND"
    assert _current_row_count(cache) == 1


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
    assert entry["succeeded_at"] == "2026-08-20T10:00:00.000000+00:00"


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


# ---- Issue #7 closure: fourth SKU evidence state (UPSTREAM_PROVIDED) ----------


def test_upstream_provided_requires_sku(cache):
    """UPSTREAM_PROVIDED carries an upstream SKU: empty real_sku_id is rejected."""
    with pytest.raises(ValueError, match="UPSTREAM_PROVIDED"):
        cache.record_success(
            mall="jd",
            submitted_url=JD_URL,
            real_sku_id=None,
            sku_status=SKU_STATUS_UPSTREAM_PROVIDED,
            source="unit-test",
        )


def test_upstream_provided_stores_sku(cache):
    """UPSTREAM_PROVIDED stores the SKU verbatim, never auto-promotes."""
    url = "https://b2b.nbdeli.com/goods/971.html"
    upstream = cache.record_success(
        mall="nbdeli",
        submitted_url=url,
        real_sku_id="SKU-9",
        sku_status=SKU_STATUS_UPSTREAM_PROVIDED,
        source="unit-test",
        succeeded_at="2026-09-01T00:00:00+00:00",
    )
    assert upstream["sku_status"] == "UPSTREAM_PROVIDED"
    assert upstream["real_sku_id"] == "SKU-9"
    hit = cache.lookup_current("nbdeli", url)
    assert hit is not None
    assert hit["entry"]["sku_status"] == "UPSTREAM_PROVIDED"
    assert hit["entry"]["real_sku_id"] == "SKU-9"
    # A later URL_PARSED record keeps its own state: UPSTREAM_PROVIDED never
    # promotes it (symmetric to the existing URL_PARSED never-promotes rule).
    later = cache.record_success(
        mall="nbdeli",
        submitted_url=url,
        real_sku_id="SKU-9B",
        sku_status=SKU_STATUS_URL_PARSED_UNVERIFIED,
        source="unit-test",
        succeeded_at="2026-09-02T00:00:00+00:00",
    )
    assert later["sku_status"] == SKU_STATUS_URL_PARSED_UNVERIFIED
    assert lookup_status(cache, "nbdeli", url) == SKU_STATUS_URL_PARSED_UNVERIFIED
    assert [
        row["sku_status"] for row in cache.history("nbdeli", url)
    ] == ["UPSTREAM_PROVIDED", SKU_STATUS_URL_PARSED_UNVERIFIED]
    # And a URL_PARSED row followed by a later UPSTREAM_PROVIDED row keeps
    # UPSTREAM_PROVIDED - no state promotes another in either direction.
    third = cache.record_success(
        mall="nbdeli",
        submitted_url=url,
        real_sku_id="SKU-9C",
        sku_status=SKU_STATUS_UPSTREAM_PROVIDED,
        source="unit-test",
        succeeded_at="2026-09-03T00:00:00+00:00",
    )
    assert third["sku_status"] == SKU_STATUS_UPSTREAM_PROVIDED
    assert lookup_status(cache, "nbdeli", url) == SKU_STATUS_UPSTREAM_PROVIDED


def lookup_status(cache: SharedCrawlCache, mall: str, url: str) -> str | None:
    hit = cache.lookup_current(mall, url)
    return None if hit is None else hit["entry"]["sku_status"]


def test_placeholder_defense_covers_upstream_provided(cache):
    """#7 placeholder / crawl_id-echo defenses apply to UPSTREAM_PROVIDED too."""
    with pytest.raises(ValueError, match="placeholder"):
        cache.record_success(
            mall="jd",
            submitted_url=JD_URL,
            real_sku_id="zhpq-77",
            sku_status=SKU_STATUS_UPSTREAM_PROVIDED,
            source="unit-test",
        )
    with pytest.raises(ValueError, match="equals crawl_id"):
        cache.record_success(
            mall="jd",
            submitted_url=JD_URL,
            crawl_id="task-echo-1",
            real_sku_id="task-echo-1",
            sku_status=SKU_STATUS_UPSTREAM_PROVIDED,
            source="unit-test",
        )


def test_get_result_reads_history_and_current(cache):
    """get_result reads any row by id - current or superseded history - and
    legacy three-state rows remain readable verbatim."""
    url = "https://example.com/p/item"
    first = cache.record_success(
        mall="m1",
        submitted_url=url,
        price="1",
        source="unit-test",
        succeeded_at="2026-09-01T00:00:00+00:00",
    )
    second = cache.record_success(
        mall="m1",
        submitted_url=url,
        price="2",
        source="unit-test",
        succeeded_at="2026-09-02T00:00:00+00:00",
    )
    got_old = cache.get_result(int(first["id"]))
    assert got_old is not None
    assert got_old["id"] == first["id"]
    assert got_old["is_current"] is False
    assert got_old["price"] == "1"
    got_current = cache.get_result(int(second["id"]))
    assert got_current is not None
    assert got_current["is_current"] is True
    assert got_current["payload"] == {}
    assert cache.get_result(10**9) is None
    # Every legacy status row stays readable through get_result.
    legacy_rows = [
        cache.record_success(
            mall="m2",
            submitted_url="https://example.com/p/confirmed",
            real_sku_id="C-1",
            sku_status=SKU_STATUS_CONFIRMED,
            source="unit-test",
        ),
        cache.record_success(
            mall="m3",
            submitted_url="https://example.com/p/url-parsed",
            real_sku_id="P-1",
            sku_status=SKU_STATUS_URL_PARSED_UNVERIFIED,
            source="unit-test",
        ),
        cache.record_success(
            mall="m4",
            submitted_url="https://example.com/p/unresolved",
            sku_status=SKU_STATUS_UNRESOLVED,
            source="unit-test",
        ),
    ]
    for row in legacy_rows:
        got = cache.get_result(int(row["id"]))
        assert got is not None
        assert got["sku_status"] == row["sku_status"]


# ---- read_only mode (Issue #10 Review Round 2, Finding 1) ----------------------


def test_read_only_cache_reads_existing_file(cache_path):
    writer = SharedCrawlCache(cache_path)
    recorded = writer.record_success(mall="comix", submitted_url=JD_URL, source="t", price="1.0")
    writer.attach_document_reference("DOC-1", mall="comix", url=JD_URL, source="t")

    read_only = SharedCrawlCache.open_read_only(cache_path)
    assert read_only.path == cache_path
    hit = read_only.lookup_current("comix", JD_URL)
    assert hit is not None and hit["match_level"] == "EXACT"
    assert read_only.history("comix", JD_URL)[0]["id"] == recorded["id"]
    assert read_only.get_result(recorded["id"])["price"] == "1.0"
    assert [row["document_id"] for row in read_only.document_references("DOC-1")] == ["DOC-1"]
    assert read_only.table_names() >= {
        "crawl_identities",
        "crawl_urls",
        "crawl_results",
        "document_references",
    }


def test_read_only_cache_find_id_by_source_event_key_works(cache_path):
    writer = SharedCrawlCache(cache_path)
    writer.record_success(
        mall="comix",
        submitted_url=JD_URL,
        source="zhaoshang-history",
        price="1.0",
        payload={"source_event_key": "evt-1"},
    )
    read_only = SharedCrawlCache.open_read_only(cache_path)
    assert read_only.find_id_by_source_event_key("zhaoshang-history", "evt-1") is not None


def test_read_only_cache_missing_file_raises_without_creating_anything(tmp_path):
    target = tmp_path / "not-yet" / "cache.sqlite3"
    with pytest.raises(FileNotFoundError):
        SharedCrawlCache.open_read_only(target)
    # A missing cache file is a diagnosis, never a fresh empty store.
    assert not target.exists()
    assert not target.parent.exists()


def test_read_only_cache_write_apis_raise_runtime_error(cache_path):
    SharedCrawlCache(cache_path).record_success(
        mall="comix", submitted_url=JD_URL, source="t", price="1.0"
    )
    read_only = SharedCrawlCache.open_read_only(cache_path)

    with pytest.raises(RuntimeError):
        read_only.record_success(mall="comix", submitted_url=JD_URL, source="t", price="2.0")
    with pytest.raises(RuntimeError):
        read_only.attach_document_reference("DOC-1", mall="comix", url=JD_URL, source="t")
    # Read paths keep working after every rejected write.
    assert read_only.lookup_current("comix", JD_URL) is not None
