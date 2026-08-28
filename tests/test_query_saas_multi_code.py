from scripts.query_saas_multi_code import chunked, find_multi_code_malls, parse_document_ids


def test_parse_document_ids_deduplicates_and_preserves_order():
    assert parse_document_ids("单据id 701 701 700000000000000001\n700000000000000002") == [
        "700000000000000001",
        "700000000000000002",
    ]


def test_chunked_limits_monitor_queries_to_one_hundred_ids():
    batches = list(chunked([str(number) for number in range(201)], 100))

    assert [len(batch) for batch in batches] == [100, 100, 1]
    assert batches[0][0] == "0"
    assert batches[-1] == ["200"]


def test_find_multi_code_malls_uses_valid_distinct_sku_link_pairs():
    relations = [
        {"shopCode": "JD", "shopName": "京东", "skuId": "A", "linkUrl": "https://a", "isValid": 1},
        {"shopCode": "jd", "shopName": "京东", "skuId": "A", "linkUrl": "https://a", "isValid": 1},
        {"shopCode": "JD", "shopName": "京东", "skuId": "B", "linkUrl": "https://b", "isValid": 1},
        {"shopCode": "JD", "shopName": "京东", "skuId": "C", "linkUrl": "https://c", "isValid": 0},
    ]

    assert find_multi_code_malls(relations) == [{"mall": "京东", "count": 2}]
