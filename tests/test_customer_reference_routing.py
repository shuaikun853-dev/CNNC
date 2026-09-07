"""classify_reference_url routing classification tests (Issue #10 Part A).

顺序规则:STAPLES(stbchina.cn)先于映射 → MANUAL_SKIP(detail.tmall.com)→
映射 ZHAOSHANG(复用 route_url 的 jslink 重写)→ UNMAPPED;坏输入永不抛出。
"""

from __future__ import annotations

import dataclasses

import pytest

from app.services.customer_reference_routing import (
    OUTCOME_MANUAL_SKIP,
    OUTCOME_STAPLES,
    OUTCOME_UNMAPPED,
    OUTCOME_ZHAOSHANG,
    ReferenceRouting,
    classify_reference_url,
)
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping


def _mapping(mall_key, *domains):
    return ZhaoshangMallMapping(
        mall_name=mall_key.upper(),
        source_id=f"src-{mall_key}",
        mall_key=mall_key,
        domains=tuple(domains),
        vendor_keywords=(mall_key,),
    )


MAPPINGS = [
    _mapping("jd", "item.jd.com"),
    _mapping("nbdeli", "b2b.nbdeli.com", "nbdeli.cn", "nbdeli.com"),
    _mapping("comix", "qx.com"),
]


# ---- Staples(先于映射)---------------------------------------------------------


def test_staples_identified_even_with_empty_mappings():
    for url in (
        "https://stbchina.cn/p/1",
        "https://www.stbchina.cn/p/1",
        "https://a.b.stbchina.cn/p/1",
    ):
        routing = classify_reference_url(url, [])
        assert routing.outcome == OUTCOME_STAPLES, url
        assert routing.mall_key == ""
        assert routing.host.endswith("stbchina.cn")
        assert routing.lookup_url == url


def test_staples_check_precedes_mapping_resolution():
    # 即使映射表声称拥有 stbchina 域名,Staples 规则仍然优先(顺序规则)
    staples_claim = [_mapping("comix", "stbchina.cn")]
    routing = classify_reference_url("https://www.stbchina.cn/p/1", staples_claim)
    assert routing.outcome == OUTCOME_STAPLES
    assert routing.mall_key == ""


# ---- MANUAL_SKIP(tmall detail)--------------------------------------------------


def test_tmall_detail_host_is_manual_skip():
    for url, host in (
        ("https://detail.tmall.com/item.htm?id=1", "detail.tmall.com"),
        ("https://a.detail.tmall.com/item.htm?id=1", "a.detail.tmall.com"),
    ):
        routing = classify_reference_url(url, MAPPINGS)
        assert routing.outcome == OUTCOME_MANUAL_SKIP, url
        assert routing.mall_key == ""
        assert routing.host == host


# ---- ZHAOSHANG(映射,含 jslink 重写与别名)---------------------------------------


def test_zhaoshang_jd_mapping():
    routing = classify_reference_url("https://item.jd.com/100012043978.html", MAPPINGS)
    assert routing.outcome == OUTCOME_ZHAOSHANG
    assert routing.mall_key == "jd"
    assert routing.host == "item.jd.com"


def test_zhaoshang_comix_qx_alias():
    routing = classify_reference_url("https://www.qx.com/product-1.html", MAPPINGS)
    assert routing.outcome == OUTCOME_ZHAOSHANG
    assert routing.mall_key == "comix"


def test_zhaoshang_jslink_rewrite_keeps_lookup_url():
    routing = classify_reference_url("https://www.jslink.com/pcweb/detail?sku=1", MAPPINGS)
    assert routing.outcome == OUTCOME_ZHAOSHANG
    assert routing.mall_key == "nbdeli"
    assert routing.lookup_url == "https://b2b.nbdeli.com/pcweb/detail?sku=1"
    assert routing.host == "b2b.nbdeli.com"


# ---- UNMAPPED 与坏输入 -----------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "not a url", "   "])
def test_malformed_or_empty_input_never_raises(bad):
    routing = classify_reference_url(bad, MAPPINGS)
    assert routing.outcome == OUTCOME_UNMAPPED
    assert routing.mall_key == ""
    assert routing.host == ""


def test_unmapped_host_and_malformed_ipv6_stay_unmapped():
    unmapped = classify_reference_url("https://unknown.example/item/9", MAPPINGS)
    assert unmapped.outcome == OUTCOME_UNMAPPED
    assert unmapped.host == "unknown.example"
    assert classify_reference_url("http://[::1/open", MAPPINGS).outcome == OUTCOME_UNMAPPED


# ---- 常量与 frozen 数据类 ----------------------------------------------------------


def test_outcome_constants():
    assert OUTCOME_ZHAOSHANG == "ZHAOSHANG"
    assert OUTCOME_STAPLES == "STAPLES"
    assert OUTCOME_MANUAL_SKIP == "MANUAL_SKIP"
    assert OUTCOME_UNMAPPED == "UNMAPPED"


def test_reference_routing_is_frozen():
    routing = classify_reference_url("https://item.jd.com/1", MAPPINGS)
    assert isinstance(routing, ReferenceRouting)
    with pytest.raises(dataclasses.FrozenInstanceError):
        routing.outcome = OUTCOME_UNMAPPED  # type: ignore[misc]
