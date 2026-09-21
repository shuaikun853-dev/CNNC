from types import SimpleNamespace

from app.services.crawl_url_identity import SKU_STATUS_CONFIRMED
from app.services.shared_crawl_cache import SharedCrawlCache
from app.services.step3_prewarm_cache import apply_cached_customer_results


def _row(url: str, mall: str):
    return SimpleNamespace(
        URL=url,
        SKUID="",
        互联网标题="",
        价格="",
        销量_评论数="",
        供应商编码=mall,
        供应商名称={"jd": "京东商城", "suning": "苏宁商城", "nbdeli": "得力商城"}[mall],
        爬取状态="待爬取",
        爬取失败原因="",
    )


def test_step3_uses_complete_jd_customer_prewarm_result(tmp_path):
    url = "https://item.jd.com/100311804305.html"
    cache = SharedCrawlCache(tmp_path / "cache.sqlite3")
    cache.record_success(
        mall="jd", submitted_url=url, real_sku_id="100311804305",
        sku_status=SKU_STATUS_CONFIRMED, title="亿联 MeetingEye 500",
        price="63420.5", comment_count=321, source="daily-internet",
        payload={"result": {"comment_count_raw": "321"}},
    )
    row = _row(url, "jd")
    summary = apply_cached_customer_results(
        [row], shared_cache=cache, mall_resolver=lambda _url: "jd"
    )

    assert summary["applied"] == 1
    assert row.SKUID == "100311804305"
    assert row.互联网标题 == "亿联 MeetingEye 500"
    assert row.价格 == 63420.5
    assert row.销量_评论数 == "321"
    assert row.供应商编码 == "jd"
    assert row.供应商名称 == "京东商城"
    assert row.爬取状态 == "成功"


def test_step3_rejects_channel_mismatch_before_cache_reuse(tmp_path):
    url = "https://item.jd.com/100311804305.html"
    cache = SharedCrawlCache(tmp_path / "cache.sqlite3")
    cache.record_success(
        mall="jd", submitted_url=url, real_sku_id="100311804305",
        sku_status=SKU_STATUS_CONFIRMED, title="亿联 MeetingEye 500",
        price="63420.5", comment_count=321, source="daily-internet",
    )
    row = _row(url, "jd")
    row.供应商编码 = "suning"

    summary = apply_cached_customer_results(
        [row], shared_cache=cache, mall_resolver=lambda _url: "jd"
    )

    assert summary["applied"] == 0
    assert summary["channel_mismatch"] == 1
    assert row.SKUID == ""
    assert row.价格 == ""
    assert row.爬取状态 == "待爬取"


def test_step3_does_not_consume_jd_cache_hit_without_comment(tmp_path):
    url = "https://item.jd.com/100311804305.html"
    cache = SharedCrawlCache(tmp_path / "cache.sqlite3")
    cache.record_success(
        mall="jd", submitted_url=url, real_sku_id="100311804305",
        sku_status=SKU_STATUS_CONFIRMED, title="亿联 MeetingEye 500",
        price="63420.5", source="daily-internet",
    )
    row = _row(url, "jd")
    summary = apply_cached_customer_results(
        [row], shared_cache=cache, mall_resolver=lambda _url: "jd"
    )

    assert summary["applied"] == 0
    assert summary["missing_required_comment"] == 1
    assert row.价格 == ""
    assert row.爬取状态 == "待爬取"


def test_step3_does_not_consume_upstream_only_sku_as_crawled_sku(tmp_path):
    url = "https://item.jd.com/100311804305.html"
    cache = SharedCrawlCache(tmp_path / "cache.sqlite3")
    cache.record_success(
        mall="jd", submitted_url=url, real_sku_id="100311804305",
        sku_status="UPSTREAM_PROVIDED", title="亿联 MeetingEye 500",
        price="63420.5", comment_count=321, source="daily-internet",
    )
    row = _row(url, "jd")

    summary = apply_cached_customer_results(
        [row], shared_cache=cache, mall_resolver=lambda _url: "jd"
    )

    assert summary["applied"] == 0
    assert summary["unconfirmed_sku"] == 1
    assert row.SKUID == ""
    assert row.爬取状态 == "待爬取"


def test_step3_suning_accepts_raw_comment_when_integer_count_is_unavailable(tmp_path):
    url = "https://product.suning.com/0000000000/123456789.html"
    cache = SharedCrawlCache(tmp_path / "cache.sqlite3")
    cache.record_success(
        mall="suning", submitted_url=url, real_sku_id="123456789",
        sku_status=SKU_STATUS_CONFIRMED, title="苏宁商品",
        price="199.0", comment_count=None, source="daily-internet",
        payload={"result": {"comment_count_raw": "1万+"}},
    )
    row = _row(url, "suning")

    summary = apply_cached_customer_results(
        [row], shared_cache=cache, mall_resolver=lambda _url: "suning"
    )

    assert summary["applied"] == 1
    assert row.SKUID == "123456789"
    assert row.互联网标题 == "苏宁商品"
    assert row.价格 == 199.0
    assert row.销量_评论数 == "1万+"


def test_jd_comment_requirement_blocks_only_otherwise_priced_result():
    from app.services.step3_prewarm_cache import missing_required_comment

    jd = _row("https://item.jd.com/100311804305.html", "jd")
    jd.价格 = 10
    assert missing_required_comment(jd, mall_resolver=lambda _url: "jd") is True
    jd.销量_评论数 = "1万+"
    assert missing_required_comment(jd, mall_resolver=lambda _url: "jd") is False
    jd.销量_评论数 = ""
    jd.价格 = ""
    assert missing_required_comment(jd, mall_resolver=lambda _url: "jd") is False

    deli = _row("https://b2b.nbdeli.com/pcweb/detail?sku=1", "nbdeli")
    deli.价格 = 10
    assert missing_required_comment(deli, mall_resolver=lambda _url: "nbdeli") is False


def test_step3_allows_non_jd_suning_without_comment(tmp_path):
    url = "https://b2b.nbdeli.com/pcweb/detail?sku=122950172"
    cache = SharedCrawlCache(tmp_path / "cache.sqlite3")
    cache.record_success(
        mall="nbdeli", submitted_url=url, real_sku_id="122950172",
        sku_status=SKU_STATUS_CONFIRMED, title="妙炫 丝杆",
        price="726.57", source="daily-internet",
    )
    row = _row(url, "nbdeli")

    summary = apply_cached_customer_results(
        [row], shared_cache=cache, mall_resolver=lambda _url: "nbdeli"
    )

    assert summary["applied"] == 1
    assert row.SKUID == "122950172"
    assert row.互联网标题 == "妙炫 丝杆"
    assert row.价格 == 726.57
    assert row.销量_评论数 == ""
