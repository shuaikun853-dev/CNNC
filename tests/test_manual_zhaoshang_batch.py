from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from app.services.legacy_zhaoshang_adapter import CrawlRequest
from app.workflows.incremental_price_audit import CrawlMember, CrawlPlan


def _request(
    *,
    mall: str = "jd",
    sku: str = "100123",
    url: str = "https://item.jd.com/100123.html",
) -> CrawlRequest:
    return CrawlRequest(
        mall_key=mall,
        mall_source_id=f"source-{mall}",
        mall_name=mall,
        sku_id=sku,
        url=url,
        backend_version="legacy-v1",
    )


def test_supported_manual_request_is_batch_first_and_never_calls_legacy():
    from app.services.manual_zhaoshang_batch import (
        ManualBatchRequest,
        execute_batch_first,
    )

    legacy_calls = []
    batch_calls = []
    item = ManualBatchRequest("step1:row-1", _request())

    result = execute_batch_first(
        (item,),
        batch_runner=lambda rows: batch_calls.append(rows) or {"step1:row-1": {"state": "SUCCESS"}},
        legacy_runner=lambda rows: legacy_calls.append(rows) or {},
    )

    assert len(batch_calls) == 1
    assert legacy_calls == []
    assert batch_calls[0][0].request.backend_version == "batch-url-v1"
    assert result.batch_results["step1:row-1"]["state"] == "SUCCESS"


def test_unsupported_url_is_an_explicit_legacy_fallback():
    from app.services.manual_zhaoshang_batch import (
        ManualBatchRequest,
        execute_batch_first,
    )

    item = ManualBatchRequest(
        "step3:row-1",
        _request(
            mall="mymro",
            sku="98S6010",
            url="https://www.mymro.cn/not-proven/98S6010",
        ),
    )
    batch_calls = []
    legacy_calls = []

    result = execute_batch_first(
        (item,),
        batch_runner=lambda rows: batch_calls.append(rows) or {},
        legacy_runner=lambda rows: legacy_calls.append(rows) or {"legacy": True},
    )

    assert batch_calls == []
    assert len(legacy_calls) == 1
    assert legacy_calls[0][0].request.backend_version == "legacy-v1"
    assert result.legacy_result == {"legacy": True}


def test_missing_product_code_is_an_explicit_legacy_fallback():
    from app.services.manual_zhaoshang_batch import (
        ManualBatchRequest,
        route_manual_request,
    )

    item = ManualBatchRequest(
        "step3:row-without-code",
        _request(sku="", url="https://item.jd.com/detail"),
    )

    routed = route_manual_request(item)

    assert routed.request.backend_version == "legacy-v1"


def test_manual_request_uses_submitted_url_code_for_authoritative_malls():
    from app.services.manual_zhaoshang_batch import authoritative_product_code

    assert (
        authoritative_product_code(
            "mymro", "https://www.mymro.cn/u-98S6010.html", "changed-sku"
        )
        == "98S6010"
    )
    assert (
        authoritative_product_code(
            "ofs",
            "https://www.officemate.cn/gallery.html?scontent=n,6258398",
            "changed-sku",
        )
        == "6258398"
    )
    assert (
        authoritative_product_code(
            "jd", "https://item.jd.com/100123.html", "100123"
        )
        == "100123"
    )


def test_batch_result_normalization_preserves_zero_title_and_comment():
    from app.services.manual_zhaoshang_batch import normalized_result_from_crawl

    result = normalized_result_from_crawl(
        _request(),
        {
            "page_price_status": "PRICE_IS_ZERO",
            "page_price_value": 0,
            "page_title": "已下架",
            "comment_count": 17,
            "page_sku_id": "100123",
            "returned_url": "https://item.jd.com/100123.html?from=batch",
        },
    )

    assert result["page_price_status"] == "PRICE_IS_ZERO"
    assert result["page_price_value"] == 0
    assert result["title"] == "已下架"
    assert result["comment"] == "17"
    assert result["product_code"] == "100123"
    assert result["returned_url"].endswith("from=batch")


def test_identity_conflict_is_not_normalized_as_success():
    from app.services.manual_zhaoshang_batch import normalized_result_from_crawl

    result = normalized_result_from_crawl(
        _request(),
        {"manual": True, "error": "IDENTITY_CONFLICT: duplicate executeNumber result"},
    )

    assert result["page_price_status"] == "IDENTITY_CONFLICT"
    assert result["page_price_value"] is None


def test_durable_runner_does_not_repeat_post_after_unknown_submit(tmp_path):
    from app.services.manual_zhaoshang_batch import (
        ManualBatchRequest,
        run_batch_zhaoshang_requests,
    )

    request = replace(_request(), backend_version="batch-url-v1")
    item = ManualBatchRequest("step1:row-1", request)
    submit_calls = []

    def plan(items, _timeout):
        row = items[0]["row"]
        member = CrawlMember(
            request.mall_key,
            row["source_id"],
            request.sku_id,
            request.url,
            request.backend_version,
            row,
        )
        return [
            CrawlPlan(
                request.mall_key,
                "logical-1",
                "task-1",
                "payload-1",
                payload_json={"contract_version": "test-batch"},
                members=(member,),
            )
        ]

    def submit(_job, _crawls, _timeout):
        submit_calls.append("POST")
        raise TimeoutError("response lost after POST")

    def harvest(jobs, _timeout):
        return {
            int(job["id"]): {
                "state": "SUBMIT_UNKNOWN",
                "reconcile_status": "NOT_FOUND",
                "reconcile_diagnostic": "inconclusive",
            }
            for job in jobs
        }

    kwargs = {
        "business_date": "2026-09-22",
        "purpose": "legacy-step1-manual",
        "state_path": tmp_path / "state.sqlite3",
        "callbacks": (plan, lambda value, _members, _timeout: value, submit, harvest),
        "max_harvest_passes": 1,
        "runtime_budget_seconds": 5,
    }

    run_batch_zhaoshang_requests((item,), **kwargs)
    run_batch_zhaoshang_requests((item,), **kwargs)

    assert submit_calls == ["POST"]


def test_durable_runner_takes_over_crashed_lease_under_process_lock(tmp_path):
    from app.services.daily_audit_store import DailyAuditStore
    from app.services.manual_zhaoshang_batch import (
        ManualBatchRequest,
        run_batch_zhaoshang_requests,
    )

    state_path = tmp_path / "state.sqlite3"
    store = DailyAuditStore(state_path)
    store.acquire_lease("2026-09-22", "crashed-owner", 3600)
    item = ManualBatchRequest(
        "step1:row-1", replace(_request(), backend_version="batch-url-v1")
    )

    result = run_batch_zhaoshang_requests(
        (item,),
        business_date="2026-09-22",
        purpose="legacy-step1-manual",
        state_path=state_path,
        callbacks=(lambda _items, _timeout: [], lambda value, *_args: value, lambda *_args: {}, lambda *_args: {}),
        max_harvest_passes=0,
        runtime_budget_seconds=5,
    )

    assert result == {}


def test_production_callbacks_use_explicit_step3_db_env_path(tmp_path, monkeypatch):
    from app.services import batch_zhaoshang_priority as priority_module
    from app.services import incremental_audit_runtime as runtime_module
    from app.services import manual_zhaoshang_batch as manual_module

    custom_env = tmp_path / "custom-zhaoshang.env"
    seen = {}

    def runtime_components(*, env_path, **_kwargs):
        seen["result_env"] = env_path
        return object(), object()

    def bridge(*_args, priority_runner, **_kwargs):
        priority_runner(
            SimpleNamespace(execute_numbers=("exec-1",), mall_key="jd"), 1.0
        )
        return tuple(lambda *_args, **_kwargs: None for _index in range(4))

    monkeypatch.setattr(runtime_module, "_batch_sql_runtime_components", runtime_components)
    monkeypatch.setattr(runtime_module, "batch_zhaoshang_bridge", bridge)
    monkeypatch.setattr(
        priority_module,
        "connect_batch_priority_from_env_file",
        lambda path, **_kwargs: seen.setdefault("priority_env", path) or object(),
    )
    monkeypatch.setattr(
        priority_module,
        "ensure_batch_priority",
        lambda factory, _target, timeout_seconds: factory(timeout_seconds),
    )

    manual_module._production_callbacks(
        business_date="2026-09-22",
        purpose="step3-no-push",
        state_path=tmp_path / "state.sqlite3",
        db_env_path=custom_env,
    )

    assert seen == {"result_env": custom_env, "priority_env": custom_env}
