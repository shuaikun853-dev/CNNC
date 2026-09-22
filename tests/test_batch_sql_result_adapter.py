from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.batch_sql_result_adapter import (
    ALLOWED_MALLS,
    BatchResultEvidence,
    BatchResultStatus,
    BatchSqlResult,
    BatchResultTarget,
    BatchSqlResultAdapter,
    BatchSqlResultQueryError,
    batch_sql_result_to_crawl_result,
)
from app.services.page_price_semantics import valid_page_price_result
from app.services import zhaoshang_url_identity


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params):
        params = tuple(params)
        self.connection.calls.append((sql, params))
        response = self.connection.handler(sql, params)
        if isinstance(response, BaseException):
            raise response
        self.rows = response

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.rolled_back = 0
        self.closed = 0

    def cursor(self):
        return _Cursor(self)

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed += 1


class _Factory:
    def __init__(self, handler):
        self.handler = handler
        self.connections = []

    def __call__(self):
        connection = _Connection(self.handler)
        self.connections.append(connection)
        return connection


def test_fetch_uses_the_callers_remaining_timeout_for_the_sql_connection():
    base_factory = _Factory(lambda _sql, _params: [_row()])
    timeouts = []

    def timed_factory(timeout_seconds):
        timeouts.append(timeout_seconds)
        return base_factory()

    adapter = BatchSqlResultAdapter(
        base_factory, timed_connection_factory=timed_factory
    )

    result = adapter.fetch_batch_results(
        "jd", [_target()], timeout_seconds=3.25
    )

    assert result[0].status is BatchResultStatus.FOUND
    assert timeouts == [pytest.approx(3.25, abs=0.01)]


def test_fetch_fails_when_sql_consumes_the_absolute_deadline_without_rollback():
    now = [10.0]

    def handler(_sql, _params):
        now[0] += 4.0
        return [_row()]

    factory = _Factory(handler)
    adapter = BatchSqlResultAdapter(
        factory,
        timed_connection_factory=lambda _timeout: factory(),
        monotonic=lambda: now[0],
    )

    with pytest.raises(BatchSqlResultQueryError, match="TimeoutError"):
        adapter.fetch_batch_results(
            "jd", [_target()], timeout_seconds=3.0
        )

    assert factory.connections[0].rolled_back == 0
    assert factory.connections[0].closed == 1


def _target(
    execute_number="mrwang-0000000000000001",
    *,
    sku="sku-1",
    url="https://example.test/product/1",
    task_status="02",
    terminal_no_result_proven=False,
):
    return BatchResultTarget(
        execute_number=execute_number,
        expected_sku=sku,
        expected_url=url,
        evidence=BatchResultEvidence(
            task_status=task_status,
            query_progress="COMPLETED" if task_status in {"02", "03"} else "RUNNING",
            terminal_no_result_proven=terminal_no_result_proven,
        ),
    )


def _row(execute_number="mrwang-0000000000000001", **overrides):
    row = {
        "execute_number": execute_number,
        "price_goods_no": "sku-1",
        "price_sku_id": "sku-1",
        "goods_no": "sku-1",
        "sku_id": "sku-1",
        "title": "Exact title",
        "price": Decimal("12.30"),
        "comment": "8",
        "url": "https://example.test/product/1",
        "is_self": "1",
        "spu_id": "spu-1",
        "spu_name": "SPU one",
    }
    row.update(overrides)
    return row


PROVEN_BATCH_RESULT_MALLS = {
    "colipu",
    "comix",
    "ehsy",
    "gongpin",
    "guangbo",
    "haoxiny",
    "jd",
    "lxwl",
    "lxwlmall",
    "mymro",
    "nbdeli",
    "ofs",
    "qstbg",
    "suning",
    "tima",
    "xfs",
    "xhgj",
    "ycw",
    "zjmi",
    "zkh",
}


def test_allowlist_contains_only_proven_batch_result_malls():
    assert ALLOWED_MALLS == PROVEN_BATCH_RESULT_MALLS


@pytest.mark.parametrize("mall", sorted(PROVEN_BATCH_RESULT_MALLS))
def test_fetches_exact_identity_from_the_allowlisted_mall(mall):
    expected_url = {
        "mymro": "https://www.mymro.cn/u-98S6010.html",
        "ofs": "https://www.ofs.cn/product-3975210.html",
    }.get(mall, "https://example.test/product/1")
    factory = _Factory(lambda _sql, _params: [_row(url=expected_url)])
    adapter = BatchSqlResultAdapter(factory)

    result = adapter.fetch_batch_results(mall, [_target(url=expected_url)])

    assert result[0].status == BatchResultStatus.FOUND
    assert result[0].execute_number == "mrwang-0000000000000001"
    assert result[0].sku_id == "sku-1"
    assert result[0].price == Decimal("12.30")
    sql, params = factory.connections[0].calls[0]
    assert f"shopping_spider.{mall}_price" in sql
    assert f"shopping_spider.{mall}_goods" in sql
    assert "m.exe_number IN (%s)" in sql
    assert params == ("mrwang-0000000000000001",)
    assert "dc_main_task_info" not in sql
    assert "dc_crawl_options_info" not in sql
    assert "UNION" not in sql.upper()


@pytest.mark.parametrize(
    "mall",
    [
        "dezhi",
        "eascs",
        "epec",
        "synthware",
        "tansoole",
        "xinminghui",
    ],
)
def test_unproven_batch_result_malls_remain_fail_closed(mall):
    with pytest.raises(ValueError, match="mall is not allowlisted"):
        BatchSqlResultAdapter(_Factory(lambda _sql, _params: [])).fetch_batch_results(
            mall, [_target()]
        )


def test_one_hundred_identities_use_one_bounded_query():
    targets = [_target(f"mrwang-{index:016x}") for index in range(100)]
    factory = _Factory(lambda _sql, _params: [])

    BatchSqlResultAdapter(factory).fetch_batch_results("jd", targets)

    assert len(factory.connections[0].calls) == 1
    assert len(factory.connections[0].calls[0][1]) == 100


def test_more_than_one_hundred_identities_are_chunked():
    targets = [_target(f"mrwang-{index:016x}") for index in range(101)]
    factory = _Factory(lambda _sql, _params: [])

    BatchSqlResultAdapter(factory).fetch_batch_results("jd", targets)

    assert [len(params) for _, params in factory.connections[0].calls] == [100, 1]


@pytest.mark.parametrize(
    ("mall", "targets", "message"),
    [
        ("arbitrary", [_target()], "mall is not allowlisted"),
        ("jd", [], "at least one target"),
        ("jd", [_target(), _target()], "execute_numbers must be unique"),
    ],
)
def test_rejects_unsafe_or_ambiguous_query_identity(mall, targets, message):
    with pytest.raises(ValueError, match=message):
        BatchSqlResultAdapter(_Factory(lambda _sql, _params: [])).fetch_batch_results(mall, targets)


def test_rejects_blank_execute_number_before_querying():
    with pytest.raises(ValueError, match="execute_number is required"):
        _target("")


def test_active_zero_row_is_not_yet_visible():
    result = BatchSqlResultAdapter(_Factory(lambda _sql, _params: [])).fetch_batch_results(
        "jd", [_target(task_status="01")]
    )

    assert result[0].status == BatchResultStatus.NOT_YET_VISIBLE


def test_failed_zero_row_is_crawler_failed():
    result = BatchSqlResultAdapter(_Factory(lambda _sql, _params: [])).fetch_batch_results(
        "jd", [_target(task_status="03")]
    )

    assert result[0].status == BatchResultStatus.CRAWLER_FAILED


def test_completed_zero_row_requires_positive_terminal_no_result_evidence():
    adapter = BatchSqlResultAdapter(_Factory(lambda _sql, _params: []))

    unresolved = adapter.fetch_batch_results("jd", [_target(task_status="02")])
    proven = adapter.fetch_batch_results(
        "jd", [_target(task_status="02", terminal_no_result_proven=True)]
    )

    assert unresolved[0].status == BatchResultStatus.EVIDENCE_INCOMPLETE
    assert proven[0].status == BatchResultStatus.TERMINAL_NO_RESULT


@pytest.mark.parametrize(
    "rows",
    [
        [_row(), _row(goods_no="sku-1", sku_id="sku-1")],
        [_row(price_goods_no="wrong", price_sku_id="wrong", goods_no="wrong", sku_id="wrong")],
        [_row(url="https://example.test/product/different")],
    ],
)
def test_duplicate_or_identity_mismatch_is_fail_closed(rows):
    result = BatchSqlResultAdapter(_Factory(lambda _sql, _params: rows)).fetch_batch_results(
        "jd", [_target()]
    )

    assert result[0].status == BatchResultStatus.IDENTITY_CONFLICT
    assert result[0].title is None


def test_one_exact_row_among_multiple_join_candidates_is_still_ambiguous():
    rows = [
        _row(),
        _row(url="https://example.test/product/different"),
    ]

    result = BatchSqlResultAdapter(
        _Factory(lambda _sql, _params: rows)
    ).fetch_batch_results("jd", [_target()])

    assert result[0].status is BatchResultStatus.IDENTITY_CONFLICT
    assert "candidate" in result[0].diagnostic


@pytest.mark.parametrize(
    ("mall", "submitted_url", "expected_code"),
    [
        ("mymro", "https://www.mymro.cn/u-98S6010.html", "98S6010"),
        ("ofs", "https://www.ofs.cn/product-3975210.html", "3975210"),
        (
            "ofs",
            "https://www.officemate.cn/gallery.html?scontent=n,6258398",
            "6258398",
        ),
    ],
)
def test_submitted_url_product_code_uses_only_proven_mymro_and_ofs_formats(
    mall, submitted_url, expected_code
):
    assert zhaoshang_url_identity.submitted_url_product_code(
        mall, submitted_url
    ) == expected_code


@pytest.mark.parametrize(
    ("mall", "submitted_url"),
    [
        ("mymro", "https://evil.example/u-98S6010.html"),
        ("mymro", "https://www.mymro.cn/p/98S6010"),
        ("mymro", "https://www.mymro.cn/u-98S6010.html?ref=1"),
        ("ofs", "https://evil.example/product-3975210.html"),
        ("ofs", "https://www.ofs.cn/p/3975210"),
        ("ofs", "https://www.officemate.cn/gallery.html?scontent=6258398"),
        ("jd", "https://www.ofs.cn/product-3975210.html"),
    ],
)
def test_submitted_url_product_code_rejects_unproven_formats(mall, submitted_url):
    assert zhaoshang_url_identity.submitted_url_product_code(
        mall, submitted_url
    ) is None


def test_mymro_unique_execute_result_accepts_changed_result_identity():
    result = BatchSqlResultAdapter(
        _Factory(
            lambda _sql, _params: [
                _row(
                    price_goods_no="result-B",
                    price_sku_id="result-B",
                    goods_no="result-B",
                    sku_id="result-B",
                    url="https://www.mymro.cn/u-resultC.html",
                )
            ]
        )
    ).fetch_batch_results(
        "mymro",
        [
            _target(
                sku="submitted-A",
                url="https://www.mymro.cn/u-98S6010.html",
            )
        ],
    )[0]

    assert result.status is BatchResultStatus.FOUND
    assert result.sku_id == "result-B"


def test_ofs_unique_execute_result_accepts_changed_result_identity():
    result = BatchSqlResultAdapter(
        _Factory(
            lambda _sql, _params: [
                _row(
                    price_goods_no="6258398",
                    price_sku_id="6258398",
                    goods_no="6258398",
                    sku_id="6258398",
                    url=(
                        "https://www.officemate.cn/gallery.html?"
                        "scontent=n,6258398"
                    ),
                )
            ]
        )
    ).fetch_batch_results(
        "ofs",
        [
            _target(
                sku="submitted-sku",
                url="https://www.ofs.cn/product-3975210.html",
            )
        ],
    )[0]

    assert result.status is BatchResultStatus.FOUND
    assert result.sku_id == "6258398"


@pytest.mark.parametrize(
    ("mall", "url"),
    [
        ("mymro", "https://www.mymro.cn/u-98S6010.html"),
        ("ofs", "https://www.ofs.cn/product-3975210.html"),
    ],
)
def test_submitted_code_authoritative_malls_still_reject_multiple_execute_results(
    mall, url
):
    rows = [_row(), _row(sku_id="second", goods_no="second")]

    result = BatchSqlResultAdapter(
        _Factory(lambda _sql, _params: rows)
    ).fetch_batch_results(mall, [_target(url=url)])[0]

    assert result.status is BatchResultStatus.IDENTITY_CONFLICT
    assert "2 candidate rows" in result.diagnostic


def test_positive_price_and_title_convert_to_valid_normal_crawl_result():
    result = BatchSqlResultAdapter(_Factory(lambda _sql, _params: [_row()])).fetch_batch_results(
        "jd", [_target()]
    )[0]

    crawl_result = batch_sql_result_to_crawl_result("jd", result)

    assert result.status == BatchResultStatus.FOUND
    assert crawl_result == {
        "page_price_status": "OK",
        "page_price_value": Decimal("12.30"),
        "page_title": "Exact title",
        "comment_count": "8",
        "page_price_source": "batch_sql:jd",
    }
    assert valid_page_price_result(crawl_result)


@pytest.mark.parametrize("title", [None, "", "Old title", "已下架"])
def test_zero_price_normalizes_every_title_to_delisted(title):
    result = BatchSqlResultAdapter(
        _Factory(lambda _sql, _params: [_row(price=Decimal("0"), title=title)])
    ).fetch_batch_results("nbdeli", [_target()])[0]

    crawl_result = batch_sql_result_to_crawl_result("nbdeli", result)

    assert result.status == BatchResultStatus.FOUND
    assert crawl_result == {
        "page_price_status": "PRICE_IS_ZERO",
        "page_price_value": Decimal("0"),
        "page_title": "已下架",
        "comment_count": "8",
        "page_price_source": "batch_sql:nbdeli",
    }
    assert valid_page_price_result(crawl_result)


@pytest.mark.parametrize("price", [None, "", "not-a-number", "NaN", "Infinity", "-1"])
def test_invalid_price_is_not_a_success(price):
    result = BatchSqlResultAdapter(
        _Factory(lambda _sql, _params: [_row(price=price)])
    ).fetch_batch_results("jd", [_target()])[0]

    assert result.status == BatchResultStatus.INVALID_RESULT
    with pytest.raises(ValueError, match="FOUND"):
        batch_sql_result_to_crawl_result("jd", result)


@pytest.mark.parametrize("title", [None, "", "   "])
def test_positive_price_requires_a_nonblank_title(title):
    result = BatchSqlResultAdapter(
        _Factory(lambda _sql, _params: [_row(title=title)])
    ).fetch_batch_results("jd", [_target()])[0]

    assert result.status == BatchResultStatus.INVALID_RESULT


def test_nbdeli_proven_numeric_pcs_identity_is_equivalent():
    result = BatchSqlResultAdapter(
        _Factory(
            lambda _sql, _params: [
                _row(
                    price_goods_no="123",
                    price_sku_id="123",
                    goods_no="123",
                    sku_id="123",
                    url="https://b2b.nbdeli.com/pcweb/detail?sku=123",
                )
            ]
        )
    ).fetch_batch_results(
        "nbdeli",
        [
            _target(
                sku="123PCS",
                url="https://b2b.nbdeli.com/pcweb/detail?sku=123PCS",
            )
        ],
    )[0]

    assert result.status == BatchResultStatus.FOUND


@pytest.mark.parametrize(
    ("submitted_url", "result_url"),
    [
        (
            "https://www.qstbg.com/gd/detail.do?goods_id=619178",
            "https://www.qstbg.com/gd/initParameter.do?goods_id=619178&formid=form1#title_detail",
        ),
        (
            "https://www.qstbg.com/gd/initParameter.do?goods_id=619178&formid=form1#title_detail",
            "https://www.qstbg.com/gd/detail.do?goods_id=619178",
        ),
        (
            "https://www.qstbg.com/gd/detail.do?goods_id=619178&count=1",
            "https://www.qstbg.com/gd/initParameter.do?goods_id=619178&formid=form1#title_detail",
        ),
    ],
)
def test_qstbg_proven_batch_url_rewrites_are_equivalent(submitted_url, result_url):
    result = BatchSqlResultAdapter(
        _Factory(lambda _sql, _params: [_row(url=result_url)])
    ).fetch_batch_results(
        "qstbg", [_target(url=submitted_url)]
    )[0]

    assert result.status is BatchResultStatus.FOUND


@pytest.mark.parametrize(
    "result_url",
    [
        "https://www.qstbg.com/gd/initParameter.do?goods_id=other&formid=form1#title_detail",
        "https://qstbg.com/gd/initParameter.do?goods_id=619178&formid=form1#title_detail",
        "https://www.qstbg.com/gd/initParameter.do?goods_id=619178&formid=other#title_detail",
        "https://www.qstbg.com/gd/initParameter.do?goods_id=619178&formid=form1&extra=1#title_detail",
        "https://www.qstbg.com/gd/initParameter.do?goods_id=619178&formid=form1#other",
        "https://www.qstbg.com/gd/detail?goods_id=619178",
    ],
)
def test_qstbg_similar_but_unproven_urls_remain_identity_conflicts(result_url):
    result = BatchSqlResultAdapter(
        _Factory(lambda _sql, _params: [_row(url=result_url)])
    ).fetch_batch_results(
        "qstbg",
        [_target(url="https://www.qstbg.com/gd/detail.do?goods_id=619178&count=1")],
    )[0]

    assert result.status is BatchResultStatus.IDENTITY_CONFLICT


def test_qstbg_does_not_inherit_unrelated_inner_path_rewrite():
    result = BatchSqlResultAdapter(
        _Factory(
            lambda _sql, _params: [
                _row(url="https://www.qstbg.com/catalog/detail?goods_id=619178")
            ]
        )
    ).fetch_batch_results(
        "qstbg",
        [
            _target(
                url="https://www.qstbg.com/catalog/detail/inner?goods_id=619178"
            )
        ],
    )[0]

    assert result.status is BatchResultStatus.IDENTITY_CONFLICT


@pytest.mark.parametrize(
    ("submitted_url", "result_url"),
    [
        (
            "https://www.lxwlmall.com/detail/inner?skuNo=11949135",
            "https://www.lxwlmall.com/detail?skuNo=11949135",
        ),
        (
            "https://www.lxwlmall.com/detail?skuNo=11949135",
            "https://www.lxwlmall.com/detail/inner?skuNo=11949135",
        ),
    ],
)
def test_lxwlmall_proven_inner_path_rewrite_is_equivalent(submitted_url, result_url):
    result = BatchSqlResultAdapter(
        _Factory(lambda _sql, _params: [_row(url=result_url)])
    ).fetch_batch_results(
        "lxwlmall", [_target(url=submitted_url)]
    )[0]

    assert result.status is BatchResultStatus.FOUND


@pytest.mark.parametrize(
    "result_url",
    [
        "https://other.lxwlmall.com/detail?skuNo=11949135",
        "https://www.lxwlmall.com/product?skuNo=11949135",
        "https://www.lxwlmall.com/catalog/detail?skuNo=11949135",
        "https://www.lxwlmall.com/detail?skuNo=other",
        "https://www.lxwlmall.com/detail?skuNo=11949135&extra=1",
        "https://www.lxwlmall.com/detail?skuNo=11949135#fragment",
    ],
)
def test_lxwlmall_similar_but_unproven_urls_remain_identity_conflicts(result_url):
    result = BatchSqlResultAdapter(
        _Factory(lambda _sql, _params: [_row(url=result_url)])
    ).fetch_batch_results(
        "lxwlmall",
        [_target(url="https://www.lxwlmall.com/detail/inner?skuNo=11949135")],
    )[0]

    assert result.status is BatchResultStatus.IDENTITY_CONFLICT


def test_lxwlmall_only_accepts_the_evidenced_root_detail_paths():
    result = BatchSqlResultAdapter(
        _Factory(
            lambda _sql, _params: [
                _row(
                    url="https://www.lxwlmall.com/catalog/detail?skuNo=11949135"
                )
            ]
        )
    ).fetch_batch_results(
        "lxwlmall",
        [
            _target(
                url="https://www.lxwlmall.com/catalog/detail/inner?skuNo=11949135"
            )
        ],
    )[0]

    assert result.status is BatchResultStatus.IDENTITY_CONFLICT


@pytest.mark.parametrize(
    ("mall", "url"),
    [
        ("qstbg", "https://www.qstbg.com/gd/detail.do?goods_id=619178"),
        ("lxwlmall", "https://www.lxwlmall.com/detail/inner?skuNo=11949135"),
    ],
)
def test_new_mall_url_rules_never_override_a_wrong_sku(mall, url):
    result = BatchSqlResultAdapter(
        _Factory(
            lambda _sql, _params: [
                _row(
                    price_goods_no="other",
                    price_sku_id="other",
                    goods_no="other",
                    sku_id="other",
                    url=url,
                )
            ]
        )
    ).fetch_batch_results(mall, [_target(url=url)])[0]

    assert result.status is BatchResultStatus.IDENTITY_CONFLICT


def test_nbdeli_normal_identity_remains_exact():
    result = BatchSqlResultAdapter(
        _Factory(
            lambda _sql, _params: [
                _row(
                    price_goods_no="123",
                    price_sku_id="123",
                    goods_no="123",
                    sku_id="123",
                    url="https://b2b.nbdeli.com/pcweb/detail?sku=123",
                )
            ]
        )
    ).fetch_batch_results(
        "nbdeli", [_target(sku="123", url="https://b2b.nbdeli.com/pcweb/detail?sku=123")]
    )[0]

    assert result.status == BatchResultStatus.FOUND


@pytest.mark.parametrize("mall", ["jd", "suning"])
def test_non_nbdeli_url_canonicalization_does_not_weaken_exact_identity(mall):
    result = BatchSqlResultAdapter(
        _Factory(
            lambda _sql, _params: [
                _row(
                    url="https://example.test/product/1/?b=2&a=1",
                )
            ]
        )
    ).fetch_batch_results(
        mall,
        [
            _target(
                url="https://www.example.test/product/1?a=1&b=2#fragment"
            )
        ],
    )[0]

    assert result.status == BatchResultStatus.IDENTITY_CONFLICT


def test_nbdeli_only_normalizes_numeric_pcs_value_not_other_url_structure():
    result = BatchSqlResultAdapter(
        _Factory(
            lambda _sql, _params: [
                _row(
                    price_goods_no="123",
                    price_sku_id="123",
                    goods_no="123",
                    sku_id="123",
                    url="https://b2b.nbdeli.com/pcweb/detail?other=1&sku=123",
                )
            ]
        )
    ).fetch_batch_results(
        "nbdeli",
        [
            _target(
                sku="123PCS",
                url="https://b2b.nbdeli.com/pcweb/detail?sku=123PCS&other=1",
            )
        ],
    )[0]

    assert result.status == BatchResultStatus.IDENTITY_CONFLICT


@pytest.mark.parametrize(
    ("mall", "expected_sku", "actual_sku"),
    [
        ("nbdeli", "123PCSC", "123"),
        ("nbdeli", "ABCPCS", "ABC"),
        ("jd", "123PCS", "123"),
        ("suning", "123PCS", "123"),
    ],
)
def test_unproven_or_non_nbdeli_pcs_transform_is_identity_conflict(
    mall, expected_sku, actual_sku
):
    result = BatchSqlResultAdapter(
        _Factory(
            lambda _sql, _params: [
                _row(
                    price_goods_no=actual_sku,
                    price_sku_id=actual_sku,
                    goods_no=actual_sku,
                    sku_id=actual_sku,
                    url=f"https://example.test/product/1?sku={actual_sku}",
                )
            ]
        )
    ).fetch_batch_results(
        mall,
        [
            _target(
                sku=expected_sku,
                url=f"https://example.test/product/1?sku={expected_sku}",
            )
        ],
    )[0]

    assert result.status == BatchResultStatus.IDENTITY_CONFLICT


def test_multi_mall_queries_only_requested_malls_with_bounded_concurrency():
    factory = _Factory(lambda _sql, params: [_row(params[0])])
    adapter = BatchSqlResultAdapter(factory, max_concurrency=3)

    results = adapter.fetch_multi_mall_results(
        {
            "jd": [_target("mrwang-0000000000000001")],
            "suning": [_target("mrwang-0000000000000002")],
        }
    )

    assert set(results) == {"jd", "suning"}
    all_sql = "\n".join(sql for connection in factory.connections for sql, _ in connection.calls)
    assert "shopping_spider.jd_price" in all_sql
    assert "shopping_spider.suning_price" in all_sql
    assert "shopping_spider.nbdeli_price" not in all_sql
    assert len(factory.connections) == 2


def test_query_failure_preserves_identity_and_can_be_retried_next_tick():
    attempts = 0

    def handler(_sql, _params):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return RuntimeError("temporary read failure")
        return [_row()]

    factory = _Factory(handler)
    adapter = BatchSqlResultAdapter(factory)
    target = _target()

    with pytest.raises(BatchSqlResultQueryError) as captured:
        adapter.fetch_batch_results("jd", [target])

    assert captured.value.execute_numbers == (target.execute_number,)
    assert adapter.fetch_batch_results("jd", [target])[0].status == BatchResultStatus.FOUND
    assert len(factory.connections) == 2
