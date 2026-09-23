import ast
from pathlib import Path

from app.services.legacy_zhaoshang_adapter import CrawlRequest
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "process_monitor_no_push_zhaoshang.py"


def _item():
    from app.services.manual_zhaoshang_batch import ManualBatchRequest

    return ManualBatchRequest(
        "step3:row-1",
        CrawlRequest(
            mall_key="jd",
            mall_source_id="3",
            mall_name="京东",
            sku_id="100123",
            url="https://item.jd.com/100123.html",
            backend_version="batch-url-v1",
        ),
    )


def test_step3_batch_result_maps_by_submitted_url_and_preserves_real_fields():
    from app.services.manual_zhaoshang_batch import batch_records_from_results

    item = _item()
    records, failures = batch_records_from_results(
        (item,),
        {
            item.source_id: {
                "state": "SUCCESS",
                "result": {
                    "page_price_status": "OK",
                    "page_price_value": 88.5,
                    "page_title": "真实标题",
                    "comment_count": 321,
                    "page_sku_id": "REAL-100123",
                    "returned_url": "https://item.jd.com/100123.html?batch=1",
                },
            }
        },
    )

    assert failures == []
    assert records[item.request.url]["price"] == 88.5
    assert records[item.request.url]["title"] == "真实标题"
    assert records[item.request.url]["comment"] == "321"
    assert records[item.request.url]["code"] == "REAL-100123"
    assert records[item.request.url]["returned_url"].endswith("batch=1")


def test_step3_batch_zero_price_is_preserved_as_delisted():
    from app.services.manual_zhaoshang_batch import batch_records_from_results

    item = _item()
    records, failures = batch_records_from_results(
        (item,),
        {
            item.source_id: {
                "state": "SUCCESS",
                "result": {
                    "page_price_status": "PRICE_IS_ZERO",
                    "page_price_value": 0,
                    "page_title": "已下架",
                    "comment_count": 0,
                    "page_sku_id": "100123",
                    "returned_url": item.request.url,
                },
            }
        },
    )

    assert failures == []
    assert records[item.request.url]["price"] == 0
    assert records[item.request.url]["status"] == "PRICE_IS_ZERO"
    assert records[item.request.url]["title"] == "已下架"
    assert records[item.request.url]["comment"] == "0"


def test_step3_identity_conflict_is_fail_closed():
    from app.services.manual_zhaoshang_batch import batch_records_from_results

    item = _item()
    records, failures = batch_records_from_results(
        (item,),
        {
            item.source_id: {
                "state": "MANUAL_REQUIRED",
                "error": "IDENTITY_CONFLICT: multiple SQL rows",
            }
        },
    )

    assert records == {}
    assert failures == [
        {
            "source_id": item.source_id,
            "mall": "jd",
            "url": item.request.url,
            "reason": "IDENTITY_CONFLICT: multiple SQL rows",
        }
    ]


def test_step3_partition_routes_supported_batch_unsupported_legacy_and_staples_local():
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter
    from app.services.manual_zhaoshang_batch import route_step3_manual_requests

    mappings = [
        ZhaoshangMallMapping("京东", "3", "jd", ("item.jd.com",), ("jd",)),
        ZhaoshangMallMapping("固安捷", "20", "mymro", ("mymro.cn",), ("mymro",)),
        ZhaoshangMallMapping("广博", "21", "guangbo", ("shop.guangbo.cn",), ("guangbo",)),
    ]
    urls = (
        "https://item.jd.com/100123.html",
        "https://www.mymro.cn/not-proven/98S6010",
        "https://shop.guangbo.cn/product/GB-1",
        "https://www.stbchina.cn/product?itemId=1",
    )

    routed = route_step3_manual_requests(
        urls,
        adapter=CnncLegacyZhaoshangAdapter(mappings=mappings),
        sku_resolver=lambda url: {
            urls[0]: "100123",
            urls[1]: "98S6010",
            urls[2]: "GB-1",
            urls[3]: "1",
        }[url],
        legacy_mall_resolver=lambda _url: None,
        mall_names={},
    )

    assert [item.request.mall_key for item in routed.batch_items] == ["jd", "guangbo"]
    assert [item.request.mall_key for item in routed.legacy_items] == ["mymro"]
    assert routed.non_zhaoshang_urls == (urls[3],)


def test_step3_batch_result_filters_existing_bad_title_rule():
    from app.services.manual_zhaoshang_batch import batch_records_from_results

    item = _item()
    records, failures = batch_records_from_results(
        (item,),
        {
            item.source_id: {
                "state": "SUCCESS",
                "result": {
                    "page_price_status": "OK",
                    "page_price_value": 88.5,
                    "page_title": "验证码",
                    "comment_count": 12,
                    "page_sku_id": "100123",
                    "returned_url": item.request.url,
                },
            }
        },
        title_is_invalid=lambda title: title == "验证码",
    )

    assert failures == []
    assert records[item.request.url]["title"] == ""
    assert records[item.request.url]["comment"] == "12"


def test_step3_jslink_batch_result_maps_back_to_original_business_url():
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter
    from app.services.manual_zhaoshang_batch import (
        batch_records_from_results,
        route_step3_manual_requests,
    )

    original = "https://www.jslink.com/pcweb/detail?sku=122262592"
    mappings = [
        ZhaoshangMallMapping("集实采", "30", "jslink", ("jslink.com",), ("jslink",)),
        ZhaoshangMallMapping("得力", "7", "nbdeli", ("nbdeli.com",), ("nbdeli",)),
    ]
    routed = route_step3_manual_requests(
        (original,),
        adapter=CnncLegacyZhaoshangAdapter(mappings=mappings),
        sku_resolver=lambda _url: "122262592",
        legacy_mall_resolver=lambda _url: None,
        mall_names={},
    )
    item = routed.batch_items[0]

    records, failures = batch_records_from_results(
        routed.batch_items,
        {
            item.source_id: {
                "state": "SUCCESS",
                "result": {
                    "page_price_status": "OK",
                    "page_price_value": 10,
                    "page_title": "真实标题",
                    "page_sku_id": "122262592",
                    "returned_url": item.request.url,
                },
            }
        },
        source_urls=routed.source_urls,
    )

    assert failures == []
    assert original in records
    assert "b2b.nbdeli.com" in records[original]["returned_url"]


def test_step3_production_script_is_wired_to_shared_batch_runner():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "run_batch_zhaoshang_requests" in calls
    assert "batch_records_from_results" in calls
