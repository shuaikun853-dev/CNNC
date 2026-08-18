from __future__ import annotations

import importlib
import json
from email.message import Message
from pathlib import Path

import pytest


def _module():
    try:
        return importlib.import_module("scripts.push_saas_commodity_price")
    except ModuleNotFoundError:
        pytest.fail("scripts.push_saas_commodity_price has not been implemented")


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeOpener:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout: int):
        self.requests.append((request, timeout))
        return _FakeResponse(self.responses.pop(0))


class _FakeAuthManager:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls = []

    def request_json(
        self,
        system,
        request_factory,
        *,
        read_only,
        opener=None,
        timeout=60,
    ):
        request = request_factory()
        self.calls.append((system, read_only, request, opener, timeout))
        return self.responses.pop(0)


def _parsed_query_curl() -> dict:
    body = {
        "tenantId": "188605",
        "commodityPoolId": "222",
        "commodityId": "old",
        "page": 1,
        "limit": 10,
    }
    return {
        "url": "http://saasportal.iio.com/improxy/saas/iio2/commodityMonitorResult/query",
        "method": "POST",
        "cookie": "x-access-token=secret; refreshToken=secret-r",
        "headers": [("Content-Type", "application/json")],
        "body": json.dumps(body),
        "data": json.dumps(body),
    }


def _row(document_id: str, relation_id: str, **overrides) -> dict:
    value = {
        "commodityId": document_id,
        "id": relation_id,
        "myCommodityId": f"mine-{document_id}",
        "isSync": "1",
        "isSyncStandard": "1",
        "syncPriceStatus": "1",
    }
    value.update(overrides)
    return value


def test_load_document_ids_rejects_invalid_and_duplicate_values(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "ids.txt"
    path.write_text("700134888769474560\nnot-an-id\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid document ID"):
        module.load_document_ids(path)

    path.write_text("700134888769474560\n700134888769474560\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate document ID"):
        module.load_document_ids(path)


def test_validate_query_curl_locks_host_path_tenant_and_pool() -> None:
    module = _module()
    parsed = _parsed_query_curl()
    module.validate_query_curl(parsed)
    assert module.push_url_from_query_curl(parsed).endswith(
        "/commodityPoolRelation/pushCommodityPrice"
    )

    outside = _parsed_query_curl()
    outside["body"] = json.dumps(
        {"tenantId": "other", "commodityPoolId": "222"}
    )
    with pytest.raises(ValueError, match="outside the project scope"):
        module.validate_query_curl(outside)


def test_fetch_push_plan_preserves_input_order_and_skips_manual_attempt() -> None:
    module = _module()
    ids = ["700134888769474560", "700100054391083008"]
    opener = _FakeOpener(
        [
            {
                "rspCode": "0000",
                "count": 2,
                "rows": [_row(ids[1], "22"), _row(ids[0], "11")],
            }
        ]
    )

    plan = module.fetch_push_plan(
        _parsed_query_curl(),
        ids,
        already_attempted_relation_ids={"22"},
        opener=opener,
    )

    assert [item.document_id for item in plan] == ids
    assert [item.action for item in plan] == ["PUSH", "SKIP_MANUAL_ATTEMPT"]
    request_body = json.loads(opener.requests[0][0].data)
    assert request_body["commodityId"] == " ".join(ids)
    assert request_body["limit"] == 500


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([_row("700134888769474560", "11")], "missing"),
        (
            [
                _row("700134888769474560", "11"),
                _row("700134888769474560", "12"),
                _row("700100054391083008", "22"),
            ],
            "duplicates",
        ),
    ],
)
def test_fetch_push_plan_requires_one_exact_relation_per_document(rows, message) -> None:
    module = _module()
    ids = ["700134888769474560", "700100054391083008"]
    opener = _FakeOpener([{"rspCode": "0000", "count": len(rows), "rows": rows}])

    with pytest.raises(RuntimeError, match=message):
        module.fetch_push_plan(_parsed_query_curl(), ids, opener=opener)


def test_execution_confirmation_locks_push_count_and_full_plan() -> None:
    module = _module()
    item = module.PushPlanItem(
        document_id="700134888769474560",
        relation_id="11",
        my_commodity_id="mine-1",
        is_sync="1",
        is_sync_standard="1",
        sync_price_status="1",
        action="PUSH",
    )
    digest = module.execution_plan_sha256([item])
    module.validate_execution_confirmation(
        [item], expected_count=1, expected_sha256=digest
    )
    with pytest.raises(ValueError, match="Push count changed"):
        module.validate_execution_confirmation(
            [item], expected_count=2, expected_sha256=digest
        )


def test_execute_push_plan_uses_one_non_retrying_write_per_item_and_stops() -> None:
    module = _module()
    plan = [
        module.PushPlanItem(
            document_id="700134888769474560",
            relation_id="11",
            my_commodity_id="mine-1",
            is_sync="1",
            is_sync_standard="1",
            sync_price_status="1",
            action="PUSH",
        ),
        module.PushPlanItem(
            document_id="700100054391083008",
            relation_id="22",
            my_commodity_id="mine-2",
            is_sync="1",
            is_sync_standard="1",
            sync_price_status="1",
            action="PUSH",
        ),
    ]
    manager = _FakeAuthManager(
        [
            {"rspCode": "0000", "rspDesc": "SUCCESS"},
            {"rspCode": "9999", "rspDesc": "failed"},
        ]
    )
    recorded = []

    with pytest.raises(RuntimeError, match="documentId=700100054391083008"):
        module.execute_push_plan(
            _parsed_query_curl(),
            plan,
            auth_manager=manager,
            on_success=recorded.append,
        )

    assert len(manager.calls) == 2
    assert all(call[0:2] == ("saas", False) for call in manager.calls)
    assert len(recorded) == 1
    first_request = manager.calls[0][2]
    assert first_request.full_url.endswith("/pushCommodityPrice")
    assert json.loads(first_request.data) == {
        "tenantId": "188605",
        "commodityPoolId": "222",
        "id": "11",
    }
