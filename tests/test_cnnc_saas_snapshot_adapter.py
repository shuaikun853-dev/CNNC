import json
from urllib import error

import pytest

from app.services.cnnc_saas_snapshot_adapter import (
    CnncSaasSnapshotAdapter,
    SnapshotAuthExpiredError,
    SnapshotConsistencyDrift,
    SnapshotContractError,
    SnapshotFetchExhausted,
    SnapshotFetchPolicy,
    SnapshotResponseCodeError,
    snapshot_to_json_payloads,
)


class _Headers:
    def get_content_charset(self):
        return "utf-8"


class _Response:
    headers = _Headers()

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Opener:
    def __init__(self, events):
        self.events = list(events)
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return _Response(event)


def _config(url, body):
    return {
        "url": url,
        "method": "POST",
        "headers": [],
        "cookie": "",
        "data": json.dumps(body),
    }


def _saas_config(**overrides):
    return _config(
        "https://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryAll",
        {"tenantId": "188605", "commodityPoolId": "222", **overrides},
    )


def _customer_config(**overrides):
    return _config(
        "https://one.cnncecp.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query",
        {"sassCheckStatuss": ["0"], **overrides},
    )


def _adapter(events, **kwargs):
    opener = _Opener(events)
    return CnncSaasSnapshotAdapter(
        _saas_config(),
        _customer_config(),
        opener=opener,
        monotonic=kwargs.pop("monotonic", lambda: 0.0),
        sleep=kwargs.pop("sleep", lambda _seconds: None),
        **kwargs,
    ), opener


def _ok(rows, count):
    return {"rspCode": "0000", "count": count, "rows": rows}


def test_scope_mismatch_fails_before_network():
    opener = _Opener([])
    adapter = CnncSaasSnapshotAdapter(
        _saas_config(tenantId="other"),
        _customer_config(),
        opener=opener,
    )

    with pytest.raises(ValueError, match="tenant"):
        adapter.fetch()

    assert opener.requests == []


@pytest.mark.parametrize(
    "saas_url, customer_url",
    [
        (
            "https://wrong.example/improxy/saas/iio2/sameAudit/queryAll",
            "https://one.cnncecp.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query",
        ),
        (
            "https://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryAll",
            "https://wrong.example/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query",
        ),
    ],
)
def test_untrusted_curl_host_fails_before_network(saas_url, customer_url):
    opener = _Opener([])
    adapter = CnncSaasSnapshotAdapter(
        _config(saas_url, {"tenantId": "188605", "commodityPoolId": "222"}),
        _config(customer_url, {"sassCheckStatuss": ["0"]}),
        opener=opener,
    )

    with pytest.raises(ValueError, match="host"):
        adapter.fetch()

    assert opener.requests == []


@pytest.mark.parametrize(
    "saas_url, customer_url",
    [
        (
            "https://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryAll",
            "https://one.cnncecp.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/queryAll",
        ),
        (
            "https://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryAll",
            "https://www.cnncmall.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/queryDetail",
        ),
        (
            "https://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryDetail",
            "https://one.cnncecp.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query",
        ),
    ],
)
def test_wrong_path_on_trusted_host_fails_before_network(saas_url, customer_url):
    opener = _Opener([])
    adapter = CnncSaasSnapshotAdapter(
        _config(saas_url, {"tenantId": "188605", "commodityPoolId": "222"}),
        _config(customer_url, {"sassCheckStatuss": ["0"]}),
        opener=opener,
    )

    with pytest.raises(ValueError, match="path"):
        adapter.fetch()

    assert opener.requests == []


@pytest.mark.parametrize(
    "customer_url",
    [
        "https://www.cnncmall.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query",
        "https://one.cnncecp.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query",
    ],
)
def test_trusted_customer_hosts_pass_scope_and_fetch(customer_url):
    opener = _Opener(
        [
            _ok([{"vCommodityId": "a"}], 1),
            _ok([{"documentId": "c1"}], 1),
        ]
    )
    adapter = CnncSaasSnapshotAdapter(
        _saas_config(),
        _config(customer_url, {"sassCheckStatuss": ["0"]}),
        opener=opener,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    snapshot = adapter.fetch()

    assert snapshot.saas_count == 1
    assert snapshot.customer_count == 1
    assert [json.loads(request.data.decode("utf-8"))["page"] for request in opener.requests] == [1, 1]


def test_fetches_exact_multipage_rows_and_preserves_duplicate_order():
    saas_rows = [{"vCommodityId": "a"}, {"vCommodityId": "a"}, {"vCommodityId": "b"}]
    customer_rows = [{"documentId": "c1"}, {"documentId": "c2"}]
    adapter, opener = _adapter(
        [
            _ok(saas_rows[:2], 3),
            _ok(saas_rows[2:], 3),
            _ok(customer_rows, 2),
        ],
        policy=SnapshotFetchPolicy(page_size=2),
    )

    snapshot = adapter.fetch(source_label="test-snapshot")

    assert list(snapshot.saas_rows) == saas_rows
    assert list(snapshot.customer_rows) == customer_rows
    assert snapshot.saas_count == 3
    assert snapshot.customer_count == 2
    assert [(page.side, page.page, page.row_count, page.count) for page in snapshot.page_evidence] == [
        ("saas", 1, 2, 3),
        ("saas", 2, 1, 3),
        ("customer", 1, 2, 2),
    ]
    assert snapshot.source_label == "test-snapshot"
    assert [json.loads(request.data.decode("utf-8"))["page"] for request in opener.requests] == [1, 2, 1]


def test_saas_uses_auth_manager_read_only_request_path():
    class _AuthManager:
        def __init__(self):
            self.calls = []

        def request_json(self, system, request_factory, **kwargs):
            request = request_factory()
            self.calls.append((system, json.loads(request.data.decode("utf-8")), kwargs))
            return _ok([], 0)

    auth_manager = _AuthManager()
    adapter, opener = _adapter([_ok([], 0)], auth_manager=auth_manager)

    snapshot = adapter.fetch()

    assert snapshot.saas_count == 0
    assert auth_manager.calls[0][0] == "saas"
    assert auth_manager.calls[0][2]["read_only"] is True
    assert auth_manager.calls[0][2]["opener"] is opener
    assert len(opener.requests) == 1  # Customer remains cURL-authenticated/direct.


def test_auth_refresh_path_cannot_return_success_after_discovery_deadline():
    clock = [0.0]

    class _SlowAuthManager:
        def request_json(self, *_args, **_kwargs):
            clock[0] = 2.0
            return _ok([], 0)

    adapter, opener = _adapter(
        [],
        auth_manager=_SlowAuthManager(),
        monotonic=lambda: clock[0],
        policy=SnapshotFetchPolicy(max_elapsed_seconds=1),
    )

    with pytest.raises(SnapshotFetchExhausted) as caught:
        adapter.fetch()

    assert caught.value.side == "saas"
    assert caught.value.elapsed_seconds == pytest.approx(2.0)
    assert opener.requests == []


def test_safe_transport_failure_retries_then_succeeds():
    sleeps = []
    adapter, opener = _adapter(
        [
            error.URLError("temporary"),
            _ok([{"vCommodityId": "a"}], 1),
            _ok([], 0),
        ],
        sleep=sleeps.append,
    )

    snapshot = adapter.fetch()

    assert snapshot.saas_count == 1
    assert len(opener.requests) == 3
    assert sleeps == [1.0]


def test_timeout_retries_a_second_request_when_discovery_deadline_remains():
    adapter, opener = _adapter(
        [
            TimeoutError("first request timed out"),
            _ok([{"vCommodityId": "a"}], 1),
            _ok([], 0),
        ]
    )

    snapshot = adapter.fetch(max_elapsed_seconds=180)

    assert snapshot.saas_count == 1
    assert len(opener.requests) == 3
    assert opener.timeouts == [60, 60, 60]


def test_three_timeouts_exhaust_existing_finite_retry_policy():
    adapter, opener = _adapter(
        [TimeoutError("one"), TimeoutError("two"), TimeoutError("three")]
    )

    with pytest.raises(SnapshotFetchExhausted) as caught:
        adapter.fetch(max_elapsed_seconds=180)

    assert caught.value.attempts == 3
    assert isinstance(caught.value.last_cause, TimeoutError)
    assert len(opener.requests) == 3


def test_finite_attempt_exhaustion_has_page_context():
    adapter, _opener = _adapter(
        [error.URLError("one"), error.URLError("two")],
        policy=SnapshotFetchPolicy(max_attempts_per_page=2),
    )

    with pytest.raises(SnapshotFetchExhausted) as caught:
        adapter.fetch()

    exc = caught.value
    assert (exc.side, exc.page, exc.attempts) == ("saas", 1, 2)
    assert isinstance(exc.last_cause, error.URLError)
    assert exc.__cause__ is None


def test_elapsed_budget_clamps_request_timeout_and_exhausts():
    ticks = iter((0.0, 179.75, 181.0))
    adapter, opener = _adapter(
        [error.URLError("slow")],
        policy=SnapshotFetchPolicy(request_timeout_seconds=60, max_elapsed_seconds=180),
        monotonic=lambda: next(ticks),
    )

    with pytest.raises(SnapshotFetchExhausted) as caught:
        adapter.fetch()

    assert opener.timeouts == [pytest.approx(0.25)]
    assert caught.value.elapsed_seconds == pytest.approx(181.0)
    assert caught.value.attempts == 1


@pytest.mark.parametrize(
    "events, error_text",
    [
        ([_ok([{"vCommodityId": "a"}], 2), _ok([{"vCommodityId": "b"}], 3)], "count changed"),
        ([_ok([], 1)], "empty page"),
    ],
)
def test_count_changes_and_early_empty_pages_fail(events, error_text):
    # Drift now voids the round and retries from page 1; the conditions must
    # still end the fetch with a typed failure once every round drifts.
    adapter, _opener = _adapter(
        events * 3,
        policy=SnapshotFetchPolicy(page_size=1),
        monotonic=lambda: 0.0,
    )

    with pytest.raises(SnapshotConsistencyDrift, match=error_text):
        adapter.fetch()


def test_serialization_contract_preserves_order_and_duplicates():
    adapter, _opener = _adapter(
        [
            _ok([{"vCommodityId": "a"}, {"vCommodityId": "a"}], 2),
            _ok([{"documentId": "c"}], 1),
        ]
    )

    snapshot = adapter.fetch()
    saas, customer = snapshot_to_json_payloads(snapshot)

    assert saas == {"rspCode": "0000", "count": 2, "rows": [{"vCommodityId": "a"}, {"vCommodityId": "a"}], "success": True}
    assert customer == {"rspCode": "0000", "count": 1, "rows": [{"documentId": "c"}], "success": True}


def test_auth_expired_rsp_code_raises_typed_error():
    auth_expired = {"rspCode": "3333", "rspDesc": "登录已过期,请重新登录", "success": False, "data": ""}
    adapter, opener = _adapter([auth_expired, auth_expired])

    with pytest.raises(SnapshotAuthExpiredError) as caught:
        adapter.fetch()

    assert caught.value.side == "saas"
    assert caught.value.rsp_code == "3333"
    assert str(caught.value) == "SnapshotAuthExpiredError(side=saas,code=3333)"
    assert "登录已过期" not in str(caught.value)
    assert len(opener.requests) == 1  # Typed source errors never retry.


def test_non_auth_bad_rsp_code_raises_response_code_error():
    bad_code = {"rspCode": "5001", "rspDesc": "业务处理失败", "success": False, "data": ""}
    adapter, opener = _adapter([bad_code, bad_code])

    with pytest.raises(SnapshotResponseCodeError) as caught:
        adapter.fetch()

    assert caught.value.side == "saas"
    assert caught.value.rsp_code == "5001"
    assert str(caught.value) == "SnapshotResponseCodeError(side=saas,code=5001)"
    assert len(opener.requests) == 1  # Typed source errors never retry.


def test_rows_contract_violation_raises_contract_error():
    adapter, _opener = _adapter([{"rspCode": "0000", "count": 1, "rows": "nope"}])

    with pytest.raises(SnapshotContractError) as caught:
        adapter.fetch()

    assert caught.value.side == "saas"
    assert caught.value.reason == "ROWS_NOT_OBJECTS"
    assert str(caught.value) == "SnapshotContractError(side=saas,reason=ROWS_NOT_OBJECTS)"


def test_count_contract_violation_raises_contract_error():
    adapter, _opener = _adapter([{"rspCode": "0000", "count": "abc", "rows": []}])

    with pytest.raises(SnapshotContractError) as caught:
        adapter.fetch()

    assert caught.value.side == "saas"
    assert caught.value.reason == "COUNT_INVALID"
