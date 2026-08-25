"""Fail-closed source fan-out gate between immutable payloads and linked crawls."""

from __future__ import annotations

import json

import pytest
from openpyxl import Workbook

from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_recovery import (
    RecoveryRejected,
    run_existing_remote_harvest,
    run_existing_remote_priority_recovery,
    run_existing_remote_priority_zero_rows_recovery,
    validate_linked_crawls_against_remote_payload,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
)


BUSINESS_DATE = "2026-08-19"
FROZEN_SOURCES = {"source-a", "source-b", "source-c", "source-d", "source-e"}


def _request(
    mall: str = "ehsy",
    sku: str = "DDL362",
    url: str = "https://www.ehsy.com/product-DDL362",
    backend: str = "legacy-v1",
) -> CrawlRequest:
    return CrawlRequest(mall, "source", "Mall", sku, url, backend)


def _crawl(
    crawl_id: int,
    source: str,
    *,
    mall: str = "ehsy",
    sku: str = "DDL362",
    url: str = "https://www.ehsy.com/product-DDL362",
    backend: str = "legacy-v1",
    remote_job_id: int = 71,
    run_id: int = 1,
) -> dict[str, object]:
    return {
        "id": crawl_id,
        "run_id": run_id,
        "remote_job_id": remote_job_id,
        "mall_key": mall,
        "source_id": source,
        "sku_id": sku,
        "exact_url": url,
        "backend_version": backend,
        "state": "PENDING",
    }


def _validate(requests, crawls, *, frozen_sources=FROZEN_SOURCES, remote_job_id=71, run_id=1):
    return validate_linked_crawls_against_remote_payload(
        operation="fanout-test",
        requests=tuple(requests),
        crawls=tuple(crawls),
        frozen_sources=set(frozen_sources),
        remote_job_id=remote_job_id,
        run_id=run_id,
    )


def test_case1_single_identity_payload_and_link_pass():
    groups = _validate([_request()], [_crawl(400, "source-a")])
    assert set(groups) == {("ehsy", "DDL362", "https://www.ehsy.com/product-DDL362", "legacy-v1")}
    assert [crawl["id"] for crawl in groups[("ehsy", "DDL362", "https://www.ehsy.com/product-DDL362", "legacy-v1")]] == [400]


def test_case2_legal_two_source_fanout_passes():
    groups = _validate(
        [_request()],
        [_crawl(400, "source-a"), _crawl(423, "source-b")],
    )
    fanout = groups[("ehsy", "DDL362", "https://www.ehsy.com/product-DDL362", "legacy-v1")]
    assert [crawl["id"] for crawl in fanout] == [400, 423]


def test_case3_multi_identity_fanout_passes():
    request_a = _request(sku="A", url="https://www.ehsy.com/product-A")
    request_b = _request(sku="B", url="https://www.ehsy.com/product-B")
    crawls = [
        _crawl(400, "source-a", sku="A", url="https://www.ehsy.com/product-A"),
        _crawl(401, "source-b", sku="A", url="https://www.ehsy.com/product-A"),
        _crawl(402, "source-c", sku="A", url="https://www.ehsy.com/product-A"),
        _crawl(403, "source-d", sku="B", url="https://www.ehsy.com/product-B"),
        _crawl(404, "source-e", sku="B", url="https://www.ehsy.com/product-B"),
    ]
    groups = _validate([request_a, request_b], crawls)
    assert {len(rows) for rows in groups.values()} == {3, 2}


def test_case4_missing_payload_identity_fails_closed():
    request_a = _request(sku="A", url="https://www.ehsy.com/product-A")
    request_b = _request(sku="B", url="https://www.ehsy.com/product-B")
    with pytest.raises(RecoveryRejected, match="missing from linked crawl work"):
        _validate([request_a, request_b], [_crawl(400, "source-a", sku="A", url="https://www.ehsy.com/product-A")])


def test_case5_linked_identity_outside_payload_fails_closed():
    with pytest.raises(RecoveryRejected, match="outside the immutable payload"):
        _validate(
            [_request()],
            [
                _crawl(400, "source-a"),
                _crawl(423, "source-b", sku="OTHER123", url="https://www.ehsy.com/product-OTHER123"),
            ],
        )


def test_case6_extra_fanout_source_outside_frozen_snapshot_fails_closed():
    with pytest.raises(RecoveryRejected, match="outside the frozen snapshot"):
        _validate(
            [_request()],
            [_crawl(400, "source-a"), _crawl(423, "source-not-frozen")],
        )


def test_case7_remote_job_linkage_mismatch_fails_closed():
    with pytest.raises(RecoveryRejected, match="not bound to the exact remote job"):
        _validate([_request()], [_crawl(400, "source-a", remote_job_id=999)])


def test_case9_same_source_duplicate_full_identity_fails_closed():
    with pytest.raises(RecoveryRejected, match="full durable crawl identities are duplicated"):
        _validate([_request()], [_crawl(400, "source-a"), _crawl(423, "source-a")])


def test_case12_url_near_miss_identity_fails_closed():
    with pytest.raises(RecoveryRejected, match="outside the immutable payload"):
        _validate(
            [_request(url="https://www.ehsy.com/product-DDL362")],
            [
                _crawl(400, "source-a"),
                _crawl(423, "source-b", url="https://www.ehsy.com/product-DDL362-other"),
            ],
        )


def test_case13_backend_version_mismatch_fails_closed():
    with pytest.raises(RecoveryRejected, match="outside the immutable payload"):
        _validate(
            [_request(backend="legacy-v1")],
            [_crawl(400, "source-a"), _crawl(423, "source-b", backend="legacy-v2")],
        )


def test_run_scope_mismatch_fails_closed():
    with pytest.raises(RecoveryRejected, match="outside the daily run scope"):
        _validate([_request()], [_crawl(400, "source-a", run_id=2)])


def test_missing_source_identity_fails_closed():
    with pytest.raises(RecoveryRejected, match="no durable source identity"):
        _validate([_request()], [_crawl(400, "")])


def test_duplicate_payload_identity_fails_closed():
    with pytest.raises(RecoveryRejected, match="duplicate backend identities"):
        _validate([_request(), _request()], [_crawl(400, "source-a")])


# ---------------------------------------------------------------------------
# Through-flow recovery tests over a real durable store.
# ---------------------------------------------------------------------------


def _snapshot_row(source: str, index: int) -> dict[str, object]:
    return {
        "vCommodityId": source,
        "skuId": f"sku-{index}",
        "queryStr": f"query-{index}",
        "skuName": f"name-{index}",
        "extLink": f"https://shop.example/{index}",
        "__crawl_required": True,
    }


def _seed_remote_job(
    tmp_path,
    *,
    mall: str,
    mall_name: str,
    skus: tuple[str, ...],
    links: list[tuple[int, str]],
    snapshot_sources: tuple[str, ...],
    job_state: str = "MANUAL_REQUIRED",
    error_text: str = "PriorityUpdateZeroRowsError",
):
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    state_path = run_dir / "state.sqlite3"
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.parent.mkdir(parents=True)
    batch_state.write_text("# state\n", encoding="utf-8")
    probe_calls: list[tuple[str, str]] = []

    def prioritize(job, task_id, task_number, _timeout):
        probe_calls.append((task_id, task_number))
        event = {
            "action": "PRIORITY_UPDATED",
            "mall": job.mall_key,
            "task_name": job.task_name,
            "task_id": task_id,
            "task_number": task_number,
            "affected_rows": len(job.requests),
        }
        with job.batch_state_path.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event)}`\n")
        return {"priority_state": "updated", "affected_rows": len(job.requests)}

    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        priority_runner=prioritize,
        runner=lambda *_args: pytest.fail("fan-out recovery must never call /add"),
    )
    requests = tuple(
        CrawlRequest(
            mall,
            "source",
            mall_name,
            sku,
            f"https://www.{mall}.com/product-{sku}",
            "legacy-v1",
        )
        for sku in skus
    )
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        requests,
        run_dir / "input.xlsx",
        run_dir / "result.xlsx",
        f"cnnc_current_opt_{mall_name}_28089831959826",
        batch_state,
    )
    for action in ("CREATED", "ENABLED"):
        event = {
            "action": action,
            "mall": mall,
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
        }
        with batch_state.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event)}`\n")

    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-fanout", 60)
    ingested = store.ingest_snapshot(
        lease, [_snapshot_row(source, index) for index, source in enumerate(snapshot_sources)]
    )
    store.freeze_snapshot(lease, int(ingested["snapshot"]["id"]))
    payload = {
        "requests": [request.__dict__ for request in requests],
        "batch_state": str(batch_state),
    }
    job = store.prepare_remote_intent(
        lease,
        prepared.logical_job_key,
        prepared.task_name,
        prepared.payload_sha256,
        mall_key=prepared.mall_key,
        input_path=str(prepared.template_path),
        result_path=str(prepared.result_path),
        payload_json=payload,
    )
    store.claim_remote_submission(lease, int(job["id"]))
    remote_identity = f"{mall}|task-id|task-number"
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity=remote_identity,
        state="PRIORITY_PENDING",
    )
    for request_index, source_id in links:
        request = requests[request_index]
        crawl = store.ensure_crawl_work(
            lease,
            request.mall_key,
            request.sku_id,
            request.url,
            request.backend_version,
            f"crawl-{request_index}-{source_id}",
            source_id=source_id,
        )
        store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    if job_state == "MANUAL_REQUIRED":
        store.mark_remote_manual(lease, int(job["id"]), error_text)
    store.release_lease(lease)
    return {
        "state_path": state_path,
        "lock_path": run_dir / "incremental_price_audit.lock",
        "adapter": adapter,
        "store": store,
        "job_id": int(job["id"]),
        "remote_identity": remote_identity,
        "probe_calls": probe_calls,
    }


def _recover_zero_rows(seed, **overrides):
    return run_existing_remote_priority_zero_rows_recovery(
        state_path=seed["state_path"],
        lock_path=seed["lock_path"],
        business_date=BUSINESS_DATE,
        remote_job_id=seed["job_id"],
        remote_identity=overrides.pop("remote_identity", seed["remote_identity"]),
        expected_error_text=overrides.pop("expected_error_text", "PriorityUpdateZeroRowsError"),
        adapter=seed["adapter"],
        timeout_seconds=60.0,
    )


def test_case10_job71_shape_zero_rows_recovery_completes_with_ddl362_fanout(tmp_path):
    seed = _seed_remote_job(
        tmp_path,
        mall="ehsy",
        mall_name="ehsy",
        skus=("ARR562", "ARR564", "ARR574", "DDL362", "KPY592"),
        links=[
            (0, "v-0"),
            (1, "v-1"),
            (2, "v-2"),
            (3, "v-3"),
            (3, "v-4"),
            (4, "v-5"),
        ],
        snapshot_sources=("v-0", "v-1", "v-2", "v-3", "v-4", "v-5"),
    )

    result = _recover_zero_rows(seed)

    assert result["outcome"] == "REMOTE_PRIORITY_ZERO_ROWS_RECOVERY_COMPLETED"
    assert result["add_calls"] == 0
    assert result["priority_calls"] == 1
    assert result["expected"] == 5 and result["matched"] == 5
    crawls = seed["store"].crawl_work_for_job(seed["job_id"])
    assert len(crawls) == 6
    assert {crawl["state"] for crawl in crawls} == {"SUBMITTED"}
    ddl362 = [
        crawl
        for crawl in crawls
        if crawl["sku_id"] == "DDL362" and crawl["mall_key"] == "ehsy"
    ]
    assert {crawl["source_id"] for crawl in ddl362} == {"v-3", "v-4"}
    assert {int(crawl["id"]) for crawl in ddl362} == {
        int(crawl["id"]) for crawl in crawls if crawl["source_id"] in {"v-3", "v-4"}
    }
    with pytest.raises(RecoveryRejected):
        _recover_zero_rows(seed)
    assert len(seed["probe_calls"]) == 1


def test_case11_job72_shape_zero_rows_recovery_completes_with_single_request_fanout(tmp_path):
    seed = _seed_remote_job(
        tmp_path,
        mall="tima",
        mall_name="tima",
        skus=("SKU0005676369",),
        links=[(0, "v-0"), (0, "v-1")],
        snapshot_sources=("v-0", "v-1"),
    )

    result = _recover_zero_rows(seed)

    assert result["outcome"] == "REMOTE_PRIORITY_ZERO_ROWS_RECOVERY_COMPLETED"
    assert result["add_calls"] == 0
    assert result["expected"] == 1 and result["matched"] == 1
    crawls = seed["store"].crawl_work_for_job(seed["job_id"])
    assert len(crawls) == 2
    assert {crawl["state"] for crawl in crawls} == {"SUBMITTED"}
    assert {crawl["source_id"] for crawl in crawls} == {"v-0", "v-1"}
    assert seed["store"].get_remote_job(seed["job_id"])["state"] == "SUBMITTED"


def test_case6_flow_extra_fanout_source_outside_frozen_snapshot_stops_recovery(tmp_path):
    seed = _seed_remote_job(
        tmp_path,
        mall="ehsy",
        mall_name="ehsy",
        skus=("DDL362",),
        links=[(0, "v-0"), (0, "v-ghost")],
        snapshot_sources=("v-0",),
    )

    with pytest.raises(RecoveryRejected, match="outside the frozen snapshot"):
        _recover_zero_rows(seed)

    assert seed["probe_calls"] == []
    job = seed["store"].get_remote_job(seed["job_id"])
    assert job["state"] == "MANUAL_REQUIRED"
    assert job["error_text"] == "PriorityUpdateZeroRowsError"
    assert {
        crawl["state"]
        for crawl in seed["store"].crawl_work_for_job(seed["job_id"])
    } == {"PENDING"}


def test_case8_flow_wrong_remote_identity_stops_recovery_before_probe(tmp_path):
    seed = _seed_remote_job(
        tmp_path,
        mall="tima",
        mall_name="tima",
        skus=("SKU0005676369",),
        links=[(0, "v-0"), (0, "v-1")],
        snapshot_sources=("v-0", "v-1"),
    )

    with pytest.raises(RecoveryRejected, match="remote identity"):
        _recover_zero_rows(seed, remote_identity="tima|other-id|task-number")

    assert seed["probe_calls"] == []
    assert seed["store"].get_remote_job(seed["job_id"])["state"] == "MANUAL_REQUIRED"


def test_priority_recovery_job60_path_supports_source_fanout(tmp_path):
    seed = _seed_remote_job(
        tmp_path,
        mall="ehsy",
        mall_name="ehsy",
        skus=("ARR562", "DDL362"),
        links=[(0, "v-0"), (1, "v-1"), (1, "v-2")],
        snapshot_sources=("v-0", "v-1", "v-2"),
        job_state="PRIORITY_PENDING",
    )

    result = run_existing_remote_priority_recovery(
        state_path=seed["state_path"],
        lock_path=seed["lock_path"],
        business_date=BUSINESS_DATE,
        remote_job_id=seed["job_id"],
        remote_identity=seed["remote_identity"],
        adapter=seed["adapter"],
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REMOTE_PRIORITY_RECOVERY_COMPLETED"
    assert result["add_calls"] == 0
    assert result["expected"] == 2 and result["matched"] == 2
    crawls = seed["store"].crawl_work_for_job(seed["job_id"])
    assert len(crawls) == 3
    assert {crawl["state"] for crawl in crawls} == {"SUBMITTED"}
    assert seed["store"].get_remote_job(seed["job_id"])["state"] == "SUBMITTED"


def _write_export_result(path, rows, *, task_name: str, mall_key: str) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "商品编码",
            "商品最小品类单元id",
            "原始链接",
            "商品价格",
            "执行编号",
            "批次号",
            "商城标识",
        ]
    )
    for index, row in enumerate(rows):
        sheet.append([*row, f"task-number-execution-{index}", task_name, mall_key])
    workbook.save(path)


def test_existing_remote_harvest_fans_one_result_out_to_both_frozen_sources(tmp_path):
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    state_path = run_dir / "state.sqlite3"
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.parent.mkdir(parents=True)
    batch_state.write_text("# state\n", encoding="utf-8")
    requests = tuple(
        CrawlRequest(
            "ehsy",
            "source",
            "ehsy",
            f"sku-{index}",
            f"https://www.ehsy.com/product-{index}",
            "legacy-v1",
        )
        for index in range(3)
    )
    runner_calls: list[list[str]] = []
    prepared_holder = {}

    def wait_export(args, timeout_seconds, _environment):
        runner_calls.append(list(args))
        prepared = prepared_holder["prepared"]
        _write_export_result(
            prepared.result_path,
            [
                ["sku-0", "sku-0", "https://www.ehsy.com/product-0", 10],
                ["sku-1", "sku-1", "https://www.ehsy.com/product-1", 11],
                ["sku-2", "sku-2", "https://www.ehsy.com/product-2", 12],
            ],
            task_name=prepared.task_name,
            mall_key=prepared.mall_key,
        )
        event = {
            "action": "EXPORTED",
            "mall": "ehsy",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
            "output_path": str(prepared.result_path),
        }
        with prepared.batch_state_path.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event)}`\n")

        class Result:
            returncode = 0

        return Result()

    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        runner=wait_export,
    )
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        requests,
        run_dir / "input.xlsx",
        run_dir / "result.xlsx",
        "cnnc_current_opt_ehsy_28089831959826",
        batch_state,
    )
    prepared_holder["prepared"] = prepared
    events = (
        {
            "action": "CREATED",
            "mall": "ehsy",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
            "expected_subtask_count": 3,
        },
        {
            "action": "ENABLED",
            "mall": "ehsy",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
        },
        {
            "action": "PRIORITY_UPDATED",
            "mall": "ehsy",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
            "affected_rows": 3,
        },
    )
    with batch_state.open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(f"- `{json.dumps(event)}`\n")

    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-harvest-fanout", 60)
    ingested = store.ingest_snapshot(
        lease,
        [_snapshot_row(source, index) for index, source in enumerate(("v-0", "v-1", "v-1b", "v-2"))],
    )
    store.freeze_snapshot(lease, int(ingested["snapshot"]["id"]))
    payload = {
        "requests": [request.__dict__ for request in requests],
        "batch_state": str(batch_state),
    }
    job = store.prepare_remote_intent(
        lease,
        prepared.logical_job_key,
        prepared.task_name,
        prepared.payload_sha256,
        mall_key=prepared.mall_key,
        input_path=str(prepared.template_path),
        result_path=str(prepared.result_path),
        payload_json=payload,
    )
    store.claim_remote_submission(lease, int(job["id"]))
    identity = "ehsy|task-id|task-number"
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity=identity,
        state="SUBMITTED",
    )
    for request_index, source_id in ((0, "v-0"), (1, "v-1"), (1, "v-1b"), (2, "v-2")):
        request = requests[request_index]
        crawl = store.ensure_crawl_work(
            lease,
            request.mall_key,
            request.sku_id,
            request.url,
            request.backend_version,
            f"crawl-{request_index}-{source_id}",
            source_id=source_id,
        )
        store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    store.release_lease(lease)

    result = run_existing_remote_harvest(
        state_path=state_path,
        lock_path=run_dir / "incremental_price_audit.lock",
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=identity,
        adapter=adapter,
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REMOTE_EXISTING_HARVEST_COMPLETED"
    assert result["expected"] == 3
    assert result["downloaded"] == 3
    assert result["validated"] == 4
    assert result["add_calls"] == 0
    assert len(runner_calls) == 1
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert len(crawls) == 4
    assert {crawl["state"] for crawl in crawls} == {"SUCCESS"}
    fanout = [crawl for crawl in crawls if crawl["sku_id"] == "sku-1"]
    assert {crawl["source_id"] for crawl in fanout} == {"v-1", "v-1b"}
    assert len({crawl["result_json"] for crawl in fanout}) == 1
