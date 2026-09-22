import json
from pathlib import Path

import pytest

from app.services import batch_zhaoshang_adapter as batch_module
from app.services.batch_zhaoshang_adapter import (
    BatchProgressStatus,
    BatchReconcileStatus,
    BatchSubmitStatus,
    CnncBatchZhaoshangAdapter,
    parse_progress,
)
from app.services.legacy_zhaoshang_adapter import CrawlRequest


FIXTURES = Path(__file__).parent / "fixtures" / "zhaoshang_batch"


def _request(index: int, *, mall: str = "jd") -> CrawlRequest:
    return CrawlRequest(
        mall_key=mall,
        mall_source_id={"jd": "3", "zkh": "18"}[mall],
        mall_name={"jd": "京东", "zkh": "震坤行商城"}[mall],
        sku_id=f"sku-{index:03d}",
        url=f"https://{mall}.example/item/{index}",
        backend_version="batch-v1",
    )


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_prepare_groups_by_mall_slices_at_100_and_is_order_independent():
    requests = [_request(index) for index in range(205)] + [_request(index, mall="zkh") for index in range(3)]

    first = CnncBatchZhaoshangAdapter.prepare_jobs("2026-08-12", requests)
    second = CnncBatchZhaoshangAdapter.prepare_jobs("2026-08-12", reversed(requests))

    assert first == second
    assert [job.expected_count for job in first.jobs] == [100, 100, 5, 3]
    assert len({job.logical_job_key for job in first.jobs}) == 4
    assert len({job.batch_number for job in first.jobs}) == 4
    execute_numbers = [value for job in first.jobs for value in job.execute_numbers]
    assert len(execute_numbers) == len(set(execute_numbers)) == 208
    assert all(value.startswith("mrwang-") and len(value) == 23 for value in execute_numbers)


def test_prepare_builds_documented_url_payload_without_priority_guessing():
    job = CnncBatchZhaoshangAdapter.prepare_jobs("2026-08-12", [_request(1)]).jobs[0]

    assert job.task_name == job.batch_number
    assert job.payload["crawlMode"] == "3"
    assert job.payload["commodityPoolId"] == "777"
    assert job.payload["tenantId"] == "000000"
    assert job.payload["taskWeight"] == "0"
    assert "endEt" not in job.payload and "startEt" not in job.payload and "taskInterval" not in job.payload
    assert job.payload["reptileList"] == [{
        "mallLogo": "jd",
        "executeNumber": job.execute_numbers[0],
        "url": "https://jd.example/item/1",
        "productCode": "sku-001",
    }]


def test_prepare_deduplicates_exact_identity_and_rejects_blank_url():
    request = _request(1)
    plan = CnncBatchZhaoshangAdapter.prepare_jobs("2026-08-12", [request, request])
    assert plan.jobs[0].expected_count == 1

    with pytest.raises(ValueError, match="URL"):
        CnncBatchZhaoshangAdapter.prepare_jobs(
            "2026-08-12",
            [CrawlRequest("jd", "3", "京东", "sku", "", "batch-v1")],
        )


@pytest.mark.parametrize(
    "url",
    ["hostless-value", "//www.qstbg.com/gd/detail.do?goods_id=1"],
)
def test_prepare_rejects_non_absolute_http_urls(url):
    request = CrawlRequest(
        mall_key="qstbg",
        mall_source_id="28",
        mall_name="齐心商城",
        sku_id="sku-1",
        url=url,
        backend_version="batch-url-v1",
    )

    with pytest.raises(ValueError, match="absolute HTTP"):
        CnncBatchZhaoshangAdapter.prepare_jobs("2026-09-21", [request])


def test_manual_entrypoint_purposes_are_durable_and_restore_exactly():
    requests = [_request(1)]

    for purpose in ("legacy-step1-manual", "step3-no-push"):
        job = CnncBatchZhaoshangAdapter.prepare_jobs(
            "2026-09-22", requests, purpose=purpose
        ).jobs[0]
        restored = CnncBatchZhaoshangAdapter.restore_job(job.durable_payload())

        assert restored.purpose == purpose
        assert restored.logical_job_key == job.logical_job_key


def test_source_scoped_requests_keep_one_execute_number_per_crawl_work():
    request = _request(1)

    plan = CnncBatchZhaoshangAdapter.prepare_jobs(
        "2026-08-12",
        [request, request],
        source_ids=["document-a", "document-b"],
    )

    assert len(plan.jobs) == 1
    job = plan.jobs[0]
    assert job.requests == (request, request)
    assert job.source_ids == ("document-a", "document-b")
    assert len(job.execute_numbers) == 2
    assert len(set(job.execute_numbers)) == 2
    restored = CnncBatchZhaoshangAdapter.restore_job(job.durable_payload())
    assert restored.source_ids == job.source_ids
    assert restored.execute_numbers == job.execute_numbers


def test_durable_envelope_round_trips_and_rejects_tampering():
    original = CnncBatchZhaoshangAdapter.prepare_jobs("2026-08-12", [_request(1), _request(2)]).jobs[0]

    restored = CnncBatchZhaoshangAdapter.restore_job(
        original.durable_payload(),
        expected_logical_job_key=original.logical_job_key,
        expected_payload_sha256=original.payload_sha256,
    )

    assert restored == original
    tampered = json.loads(json.dumps(original.durable_payload()))
    tampered["post_body"]["reptileList"][0]["productCode"] = "different"
    with pytest.raises(ValueError, match="verification"):
        CnncBatchZhaoshangAdapter.restore_job(tampered)
    tampered_weight = json.loads(json.dumps(original.durable_payload()))
    tampered_weight["post_body"]["taskWeight"] = "-1"
    with pytest.raises(ValueError, match="verification"):
        CnncBatchZhaoshangAdapter.restore_job(tampered_weight)


def test_customer_prewarm_purpose_is_explicit_and_identity_bound_in_durable_payload():
    normal = CnncBatchZhaoshangAdapter.prepare_jobs(
        "2026-08-12", [_request(1)]
    ).jobs[0]
    prewarm = CnncBatchZhaoshangAdapter.prepare_jobs(
        "2026-08-12", [_request(1)], purpose="customer-prewarm"
    ).jobs[0]

    assert normal.purpose == "price-audit"
    assert normal.durable_payload()["purpose"] == "price-audit"
    assert prewarm.purpose == "customer-prewarm"
    assert prewarm.durable_payload()["purpose"] == "customer-prewarm"
    assert prewarm.logical_job_key != normal.logical_job_key
    restored = CnncBatchZhaoshangAdapter.restore_job(prewarm.durable_payload())
    assert restored.purpose == "customer-prewarm"

    tampered = json.loads(json.dumps(prewarm.durable_payload()))
    tampered["purpose"] = "price-audit"
    with pytest.raises(ValueError, match="verification"):
        CnncBatchZhaoshangAdapter.restore_job(tampered)


def test_submit_is_write_disabled_by_default():
    calls = []
    adapter = CnncBatchZhaoshangAdapter(post_json=lambda *_args: calls.append(1))
    job = adapter.prepare_jobs("2026-08-12", [_request(1)]).jobs[0]

    outcome = adapter.submit(job, timeout_seconds=10)

    assert outcome.status is BatchSubmitStatus.NOT_ATTEMPTED
    assert calls == []


def test_default_http_client_bypasses_proxy_and_sends_no_auth_headers(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return _fixture("create_success.json")

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["post"] = kwargs
            return Response()

    monkeypatch.setattr(batch_module.httpx, "Client", Client)

    status, _response = CnncBatchZhaoshangAdapter._default_post_json(
        "http://batch.invalid", {"safe": True}, 10
    )

    assert status == 200
    assert captured["client"]["trust_env"] is False
    assert captured["client"]["follow_redirects"] is False
    assert captured["post"]["headers"] == {"Content-Type": "application/json"}


def test_submit_accepts_only_documented_success_shape():
    calls = []

    def post(_url, body, timeout):
        calls.append((body, timeout))
        return 200, _fixture("create_success.json")

    adapter = CnncBatchZhaoshangAdapter(write_enabled=True, post_json=post)
    job = adapter.prepare_jobs("2026-08-12", [_request(1), _request(2)]).jobs[0]
    outcome = adapter.submit(job, timeout_seconds=10)

    assert outcome.status is BatchSubmitStatus.SUBMITTED
    assert outcome.remote_identity is not None
    assert outcome.remote_identity.batch_number == job.batch_number
    assert len(calls) == 1


def test_timeout_after_server_success_is_reconciled_without_a_second_post():
    post_calls = []
    progress_calls = []

    def post(*_args):
        post_calls.append(1)
        raise TimeoutError("response lost after server commit")

    def get(_url, params, _timeout):
        progress_calls.append(params)
        return 200, _fixture("progress_completed.json")

    adapter = CnncBatchZhaoshangAdapter(write_enabled=True, post_json=post, get_json=get)
    job = adapter.prepare_jobs("2026-08-12", [_request(1), _request(2)]).jobs[0]

    first = adapter.submit(job, timeout_seconds=10)
    repeated = adapter.submit(job, timeout_seconds=10)
    reconciled = adapter.reconcile_unknown(job, timeout_seconds=10)

    assert first.status is repeated.status is BatchSubmitStatus.SUBMIT_UNKNOWN
    assert reconciled.status is BatchReconcileStatus.FOUND
    assert post_calls == [1]
    assert progress_calls == [{"batchNumber": job.batch_number}]


def test_reconcile_fails_closed_on_count_or_identity_collision():
    job = CnncBatchZhaoshangAdapter.prepare_jobs("2026-08-12", [_request(1), _request(2)]).jobs[0]
    adapter = CnncBatchZhaoshangAdapter(
        get_json=lambda *_args: (200, {
            "rspCode": "0000",
            "success": True,
            "data": {"batchNumber": job.batch_number, "mallLogo": "jd", "totalCount": 3},
        })
    )

    outcome = adapter.reconcile_unknown(job, timeout_seconds=10)

    assert outcome.status is BatchReconcileStatus.MANUAL
    assert "total" in outcome.diagnostic


def test_reconcile_rejects_conflicting_optional_remote_execute_number_evidence():
    job = CnncBatchZhaoshangAdapter.prepare_jobs(
        "2026-08-12", [_request(1), _request(2)]
    ).jobs[0]
    adapter = CnncBatchZhaoshangAdapter(
        get_json=lambda *_args: (
            200,
            {
                "rspCode": "0000",
                "success": True,
                "data": {
                    "batchNumber": job.batch_number,
                    "mallLogo": "jd",
                    "executeNumbers": ["wrong-one", "wrong-two"],
                    "totalCount": 2,
                },
            },
        )
    )

    outcome = adapter.reconcile_unknown(job, timeout_seconds=10)

    assert outcome.status is BatchReconcileStatus.MANUAL
    assert "identity" in outcome.diagnostic


def test_parse_progress_supports_documented_rspdesc_and_structured_shape():
    documented = parse_progress(_fixture("progress_completed.json"))
    structured = parse_progress({
        "rspCode": "0000",
        "data": {"totalCount": 5, "completedCount": 2, "pendingCount": 1, "runningCount": 2, "failedCount": 0, "pendingStoreCount": 0},
    })

    assert documented.status is BatchProgressStatus.COMPLETED
    assert (documented.total, documented.completed) == (2, 2)
    assert structured.status is BatchProgressStatus.RUNNING
    assert (structured.total, structured.completed, structured.running) == (5, 2, 2)


@pytest.mark.parametrize(
    "response, expected",
    [
        ({"rspCode": "0000"}, BatchSubmitStatus.SUBMIT_UNKNOWN),
        ({"rspCode": "0000", "success": False}, BatchSubmitStatus.SUBMIT_UNKNOWN),
        ({"rspCode": "6666", "success": False}, BatchSubmitStatus.REJECTED),
    ],
)
def test_submit_does_not_overclaim_malformed_or_rejected_responses(response, expected):
    adapter = CnncBatchZhaoshangAdapter(write_enabled=True, post_json=lambda *_args: (200, response))
    job = adapter.prepare_jobs("2026-08-12", [_request(1)]).jobs[0]

    assert adapter.submit(job, timeout_seconds=10).status is expected


@pytest.mark.parametrize(
    "status_code, expected",
    [
        (400, BatchSubmitStatus.REJECTED),
        (401, BatchSubmitStatus.SUBMIT_UNKNOWN),
        (429, BatchSubmitStatus.SUBMIT_UNKNOWN),
        (503, BatchSubmitStatus.SUBMIT_UNKNOWN),
    ],
)
def test_submit_http_failure_classification_is_fail_closed(status_code, expected):
    adapter = CnncBatchZhaoshangAdapter(
        write_enabled=True,
        post_json=lambda *_args: (status_code, {"rspCode": str(status_code), "success": False}),
    )
    job = adapter.prepare_jobs("2026-08-12", [_request(1)]).jobs[0]

    assert adapter.submit(job, timeout_seconds=10).status is expected
