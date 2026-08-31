from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import pytest
from openpyxl import Workbook

from app.services import incremental_audit_recovery as recovery
from app.services.daily_audit_store import DailyAuditStore
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    ExportNoPriceProbe,
)
from app.services.recovery.no_result import (
    EXPORT_NO_PRICE_CRAWL_REASON,
    EXPORT_NO_PRICE_RSP_DESC,
)


BUSINESS_DATE = "2026-08-14"


def _make_export_probe(
    calls: list[tuple[str, str, float]] | None = None,
    exception: Exception | None = None,
    **overrides: Any,
) -> Callable[..., ExportNoPriceProbe]:
    """Injected export probe; defaults satisfy the full live no-price contract."""

    def probe(job, task_id, task_number, timeout_seconds):
        if calls is not None:
            calls.append((task_id, task_number, float(timeout_seconds)))
        if exception is not None:
            raise exception
        fields: dict[str, Any] = {
            "task_status_rsp_desc": "任务状态: 本次任务执行完毕",
            "progress": {
                "total": len(job.requests),
                "finished": len(job.requests),
                "pending": 0,
                "running": 0,
                "failed": 0,
                "waiting_db": 0,
            },
            "export_rsp_desc": EXPORT_NO_PRICE_RSP_DESC,
            "export_count": 0,
            "remote_calls": 3,
        }
        fields.update(overrides)
        return ExportNoPriceProbe(**fields)

    return probe


def _write_partial_result(
    path: Path,
    rows: list[tuple[str, str, str, object]],
    *,
    task_name: str,
    mall_key: str,
    foreign_rows: list[tuple[str, str, str, object]] | None = None,
    execution_numbers: list[str] | None = None,
) -> None:
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
        execution = (
            execution_numbers[index]
            if execution_numbers is not None
            else f"task-number-execution-{index}"
        )
        sheet.append([*row, execution, task_name, mall_key])
    for index, row in enumerate(foreign_rows or []):
        sheet.append([*row, f"foreign-{index}", "foreign-task", mall_key])
    workbook.save(path)


def _seed_partial_job(
    tmp_path: Path,
    requests: tuple[CrawlRequest, ...],
    export_rows: list[tuple[str, str, str, object]],
    *,
    foreign_rows: list[tuple[str, str, str, object]] | None = None,
    duplicate_sources: dict[int, tuple[str, ...]] | None = None,
    execution_numbers: list[str] | None = None,
    export_probe: Callable[..., ExportNoPriceProbe] | None = None,
) -> tuple[DailyAuditStore, CnncLegacyZhaoshangAdapter, int, str, Path]:
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    run_dir.mkdir(parents=True)
    state_path = run_dir / "state.sqlite3"
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.write_text("# state\n", encoding="utf-8")
    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        export_probe=export_probe,
    )
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        requests,
        run_dir / "input.xlsx",
        run_dir / "result.xlsx",
        "task-target",
        batch_state,
    )
    _write_partial_result(
        prepared.result_path,
        export_rows,
        task_name=prepared.task_name,
        mall_key=prepared.mall_key,
        foreign_rows=foreign_rows,
        execution_numbers=execution_numbers,
    )
    events = (
        {
            "action": "CREATED",
            "mall": prepared.mall_key,
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
            "expected_subtask_count": len(requests),
        },
        {
            "action": "ENABLED",
            "mall": prepared.mall_key,
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
        },
        {
            "action": "PRIORITY_ALREADY_HIGHEST",
            "mall": prepared.mall_key,
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
            "already_highest_count": len(requests),
        },
        {
            "action": "EXPORTED",
            "mall": prepared.mall_key,
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
            "output_path": str(prepared.result_path),
        },
    )
    with batch_state.open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(f"- `{json.dumps(event)}`\n")

    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-partial", 60)
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
    remote_identity = "mall|task-id|task-number"
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity=remote_identity,
        state="SUBMITTED",
    )
    store.mark_remote_manual(lease, int(job["id"]), "full export validation failed")
    for index, request in enumerate(requests):
        sources = (f"v-{index}",)
        if duplicate_sources and index in duplicate_sources:
            sources = duplicate_sources[index]
        for source_id in sources:
            crawl = store.ensure_crawl_work(
                lease,
                request.mall_key,
                request.sku_id,
                request.url,
                request.backend_version,
                f"fingerprint-{index}-{source_id}",
                source_id=source_id,
            )
            store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    store.release_lease(lease)
    return store, adapter, int(job["id"]), remote_identity, state_path


def _requests(count: int) -> tuple[CrawlRequest, ...]:
    return tuple(
        CrawlRequest(
            "mall",
            "source",
            "Mall",
            f"sku-{index}",
            f"https://shop.example/{index}",
            "legacy-v1",
        )
        for index in range(count)
    )


def _run(
    state_path: Path,
    adapter: CnncLegacyZhaoshangAdapter,
    job_id: int,
    remote_identity: str,
    timeout_seconds: float | None = None,
):
    kwargs = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
    return recovery.run_partial_exact_harvest_recovery(
        state_path=state_path,
        lock_path=state_path.with_name("incremental_price_audit.lock"),
        business_date=BUSINESS_DATE,
        remote_job_id=job_id,
        remote_identity=remote_identity,
        adapter=adapter,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("expected", "matched", "missing"),
    [(273, 272, 1), (58, 55, 3)],
)
def test_partial_exact_harvest_completes_unique_matches_and_exact_absences(
    tmp_path,
    expected,
    matched,
    missing,
):
    requests = _requests(expected)
    rows = [
        (request.sku_id, request.sku_id, request.url, 0 if index == 0 else index + 1)
        for index, request in enumerate(requests[:matched])
    ]
    probe_calls: list[tuple[str, str, float]] = []
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows, export_probe=_make_export_probe(calls=probe_calls)
    )

    result = _run(state_path, adapter, job_id, identity)

    assert result["crawl_success"] == matched
    assert result["crawl_no_result"] == missing
    assert result["crawl_manual"] == 0
    assert result["remote_state"] == "COMPLETED"
    assert result["remote_calls"] == 3
    assert len(probe_calls) == 1
    assert result["add_calls"] == 0
    crawls = store.crawl_work_for_job(job_id)
    assert [crawl["state"] for crawl in crawls].count("SUCCESS") == matched
    assert [crawl["state"] for crawl in crawls].count("NO_RESULT") == missing
    zero = json.loads(crawls[0]["result_json"])
    assert zero["page_price_status"] == "PRICE_IS_ZERO"
    assert Decimal(str(zero["page_price_value"])) == Decimal("0")


def test_partial_exact_harvest_treats_candidate_columns_as_exported_aliases(tmp_path):
    normal = _requests(31)
    shared_url = "https://shop.example/shared"
    requests = (
        *normal,
        CrawlRequest("mall", "source", "Mall", "base", shared_url, "legacy-v1"),
        CrawlRequest("mall", "source", "Mall", "basePCS", shared_url, "legacy-v1"),
        CrawlRequest("mall", "source", "Mall", "absent", "https://shop.example/absent", "legacy-v1"),
    )
    rows = [
        (request.sku_id, request.sku_id, request.url, index + 1)
        for index, request in enumerate(normal)
    ]
    rows.append(("base", "basePCS", shared_url, 99))
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows, export_probe=_make_export_probe()
    )

    result = _run(state_path, adapter, job_id, identity)

    assert result["crawl_success"] == 33
    assert result["crawl_no_result"] == 1
    assert result["crawl_manual"] == 0
    assert result["remote_state"] == "COMPLETED"
    states = {crawl["sku_id"]: crawl["state"] for crawl in store.crawl_work_for_job(job_id)}
    assert states["base"] == "SUCCESS"
    assert states["basePCS"] == "SUCCESS"
    assert states["absent"] == "NO_RESULT"


def test_partial_exact_absence_does_not_borrow_sibling_sku_price(tmp_path):
    shared_url = "https://shop.example/shared"
    requests = (
        CrawlRequest("mall", "source", "Mall", "basePCS", shared_url, "legacy-v1"),
        CrawlRequest("mall", "source", "Mall", "base", shared_url, "legacy-v1"),
    )
    rows = [("basePCS", "basePCS", shared_url, 12.3)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows, export_probe=_make_export_probe()
    )

    result = _run(state_path, adapter, job_id, identity)

    assert result["crawl_success"] == 1
    assert result["crawl_no_result"] == 1
    crawls = {crawl["sku_id"]: crawl for crawl in store.crawl_work_for_job(job_id)}
    assert Decimal(
        str(json.loads(crawls["basePCS"]["result_json"])["page_price_value"])
    ) == Decimal("12.3")
    absent = json.loads(crawls["base"]["result_json"])
    assert absent["evidence_type"] == "EXPORTED_NO_MATCHING_ROW"
    assert absent["no_result_reason"] == EXPORT_NO_PRICE_CRAWL_REASON
    assert crawls["base"]["error_text"] == EXPORT_NO_PRICE_CRAWL_REASON
    assert absent["page_price_value"] is None


def test_partial_exact_harvest_filters_foreign_provenance_rows(tmp_path):
    requests = _requests(2)
    current = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    foreign = [(requests[1].sku_id, requests[1].sku_id, requests[1].url, 999)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, current, foreign_rows=foreign, export_probe=_make_export_probe()
    )

    result = _run(state_path, adapter, job_id, identity)

    assert result["foreign_filtered"] == 1
    crawls = {crawl["sku_id"]: crawl for crawl in store.crawl_work_for_job(job_id)}
    assert crawls["sku-0"]["state"] == "SUCCESS"
    assert crawls["sku-1"]["state"] == "NO_RESULT"
    assert json.loads(crawls["sku-1"]["result_json"])["page_price_value"] is None


def test_partial_exact_harvest_trusts_exported_sku_after_exact_provenance(tmp_path):
    requests = _requests(1)
    rows = [("exported-sku", requests[0].sku_id, requests[0].url, 12.5)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows
    )

    result = _run(state_path, adapter, job_id, identity)

    assert result["remote_state"] == "COMPLETED"
    crawl = store.crawl_work_for_job(job_id)[0]
    evidence = json.loads(crawl["result_json"])
    assert crawl["state"] == "SUCCESS"
    assert evidence["product_code"] == "exported-sku"
    assert evidence["page_price_value"] == "12.5"


def test_partial_exact_harvest_trusts_rewritten_export_url_after_exact_provenance(tmp_path):
    requests = _requests(1)
    rows = [(requests[0].sku_id, requests[0].sku_id, "https://rewritten.example/item", 8)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows
    )

    result = _run(state_path, adapter, job_id, identity)

    assert result["remote_state"] == "COMPLETED"
    evidence = json.loads(store.crawl_work_for_job(job_id)[0]["result_json"])
    assert evidence["exported_url"] == "https://rewritten.example/item"
    assert evidence["page_price_value"] == "8"


def test_partial_exact_harvest_trusts_all_exported_business_fields_for_one_request(tmp_path):
    requests = _requests(1)
    rows = [("new-product-code", "new-export-sku", requests[0].url, 0)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows
    )

    result = _run(state_path, adapter, job_id, identity)

    assert result["remote_state"] == "COMPLETED"
    evidence = json.loads(store.crawl_work_for_job(job_id)[0]["result_json"])
    assert evidence["product_code"] == "new-product-code"
    assert evidence["exported_url"] == requests[0].url
    assert evidence["page_price_status"] == "PRICE_IS_ZERO"
    assert evidence["page_price_value"] == "0"


def test_partial_exact_harvest_rejects_one_execution_with_conflicting_prices(tmp_path):
    requests = _requests(2)
    rows = [
        (requests[0].sku_id, requests[0].sku_id, requests[0].url, 10),
        (requests[1].sku_id, requests[1].sku_id, requests[1].url, 11),
    ]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        execution_numbers=["task-number-shared", "task-number-shared"],
    )

    with pytest.raises(recovery.RecoveryRejected, match="execution.*conflict"):
        _run(state_path, adapter, job_id, identity)

    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"PENDING"}


def test_partial_exact_harvest_rejects_missing_execution_provenance(tmp_path):
    requests = _requests(1)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows, execution_numbers=[""]
    )

    with pytest.raises(recovery.RecoveryRejected, match="execution provenance"):
        _run(state_path, adapter, job_id, identity)

    assert store.crawl_work_for_job(job_id)[0]["state"] == "PENDING"


def test_partial_exact_harvest_rejects_execution_from_another_remote_task(tmp_path):
    requests = _requests(1)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows, execution_numbers=["other-task-execution-1"]
    )

    with pytest.raises(recovery.RecoveryRejected, match="execution.*remote task"):
        _run(state_path, adapter, job_id, identity)

    assert store.crawl_work_for_job(job_id)[0]["state"] == "PENDING"


def test_partial_exact_harvest_rerun_skips_success_without_new_harvest_attempt(tmp_path):
    requests = _requests(3)
    rows = [
        (request.sku_id, request.sku_id, request.url, index + 1)
        for index, request in enumerate(requests)
    ]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows
    )
    first = _run(state_path, adapter, job_id, identity)
    before_job = store.get_remote_job(job_id)
    before_crawls = store.crawl_work_for_job(job_id)

    second = _run(state_path, adapter, job_id, identity)

    assert first["crawl_success"] == 3
    assert second["written_success"] == 0
    assert second["skipped_terminal"] == 3
    assert store.get_remote_job(job_id)["harvest_attempts"] == before_job["harvest_attempts"]
    assert store.crawl_work_for_job(job_id) == before_crawls


def test_partial_exact_harvest_is_resumable_after_mid_write_crash(tmp_path, monkeypatch):
    requests = _requests(3)
    rows = [
        (request.sku_id, request.sku_id, request.url, index + 1)
        for index, request in enumerate(requests[:2])
    ]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows, export_probe=_make_export_probe()
    )
    original = DailyAuditStore.complete_partial_exact_crawl_work
    calls = 0

    def crash_after_first(self, *args, **kwargs):
        nonlocal calls
        value = original(self, *args, **kwargs)
        calls += 1
        if calls == 1:
            raise RuntimeError("injected crash")
        return value

    with monkeypatch.context() as scoped:
        scoped.setattr(DailyAuditStore, "complete_partial_exact_crawl_work", crash_after_first)
        with pytest.raises(RuntimeError, match="injected crash"):
            _run(state_path, adapter, job_id, identity)

    after_crash = store.crawl_work_for_job(job_id)
    assert [crawl["state"] for crawl in after_crash].count("SUCCESS") == 1
    attempts = store.get_remote_job(job_id)["harvest_attempts"]

    resumed = _run(state_path, adapter, job_id, identity)

    assert resumed["written_success"] == 1
    assert resumed["written_no_result"] == 1
    assert [crawl["state"] for crawl in store.crawl_work_for_job(job_id)] == [
        "SUCCESS",
        "SUCCESS",
        "NO_RESULT",
    ]
    assert store.get_remote_job(job_id)["harvest_attempts"] == attempts


def test_partial_exact_harvest_fans_one_durable_request_out_to_all_local_consumers(tmp_path):
    requests = _requests(2)
    rows = [
        (request.sku_id, request.sku_id, request.url, index + 1)
        for index, request in enumerate(requests)
    ]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        duplicate_sources={1: ("v-1", "v-2", "v-3", "v-4", "v-5")},
    )

    result = _run(state_path, adapter, job_id, identity)

    assert result["crawl_success"] == 6
    assert result["crawl_manual"] == 0
    assert result["remote_state"] == "COMPLETED"
    duplicates = [
        crawl for crawl in store.crawl_work_for_job(job_id) if crawl["sku_id"] == "sku-1"
    ]
    assert {crawl["state"] for crawl in duplicates} == {"SUCCESS"}
    assert len({crawl["source_id"] for crawl in duplicates}) == 5
    assert len({crawl["fingerprint"] for crawl in duplicates}) == 5
    assert len({crawl["result_json"] for crawl in duplicates}) == 1


def test_partial_exact_harvest_does_not_fan_out_to_unbound_same_request(tmp_path):
    requests = _requests(1)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path, requests, rows
    )
    lease = store.acquire_lease(BUSINESS_DATE, "seed-unbound", 60)
    unbound = store.ensure_crawl_work(
        lease,
        requests[0].mall_key,
        requests[0].sku_id,
        requests[0].url,
        requests[0].backend_version,
        "unbound-fingerprint",
        source_id="unbound-source",
    )
    store.release_lease(lease)

    result = _run(state_path, adapter, job_id, identity)

    assert result["crawl_success"] == 1
    assert store.get_crawl_work(int(unbound["id"]))["state"] == "PENDING"
    assert store.get_crawl_work(int(unbound["id"]))["remote_job_id"] is None


def test_partial_exact_absence_rejects_incomplete_progress_without_writes(tmp_path):
    """P1: absent + probe progress not fully completed -> reject, zero writes, retryable."""
    requests = _requests(2)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    probe_calls: list[tuple[str, str, float]] = []
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(
            calls=probe_calls,
            progress={
                "total": 2,
                "finished": 1,
                "pending": 0,
                "running": 1,
                "failed": 0,
                "waiting_db": 0,
            },
        ),
    )

    with pytest.raises(recovery.RecoveryRejected, match="progress is not fully completed"):
        _run(state_path, adapter, job_id, identity)

    assert len(probe_calls) == 1
    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"PENDING"}
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"

    retry_adapter = CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        export_probe=_make_export_probe(),
    )
    retried = _run(state_path, retry_adapter, job_id, identity)

    assert retried["crawl_success"] == 1
    assert retried["crawl_no_result"] == 1
    assert retried["remote_state"] == "COMPLETED"


def test_partial_exact_absence_rejects_task_status_without_finished_marker(tmp_path):
    """P2: absent + probe task status missing the finished marker -> reject."""
    requests = _requests(2)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(task_status_rsp_desc="任务仍在运行"),
    )

    with pytest.raises(
        recovery.RecoveryRejected, match="does not confirm the task finished"
    ):
        _run(state_path, adapter, job_id, identity)

    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"PENDING"}
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"


def test_partial_exact_absence_writes_no_result_only_with_full_probe_contract(tmp_path):
    """P3: absent + fully conforming probe -> NO_RESULT with durable evidence."""
    requests = _requests(3)
    rows = [
        (request.sku_id, request.sku_id, request.url, index + 1)
        for index, request in enumerate(requests[:2])
    ]
    probe_calls: list[tuple[str, str, float]] = []
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(calls=probe_calls, remote_calls=5),
    )

    result = _run(state_path, adapter, job_id, identity, timeout_seconds=123.5)

    assert probe_calls == [("task-id", "task-number", 123.5)]
    assert result["remote_calls"] == 5
    assert result["remote_state"] == "COMPLETED"
    assert result["no_result_before"] == 0
    assert result["no_result_limit"] == 10
    assert result["export_rsp_desc"] == EXPORT_NO_PRICE_RSP_DESC
    assert result["export_count"] == 0
    assert result["task_status_rsp_desc"].endswith("本次任务执行完毕")
    assert result["progress"] == {
        "total": 3,
        "finished": 3,
        "pending": 0,
        "running": 0,
        "failed": 0,
        "waiting_db": 0,
    }
    crawls = {crawl["sku_id"]: crawl for crawl in store.crawl_work_for_job(job_id)}
    assert crawls["sku-0"]["state"] == "SUCCESS"
    assert crawls["sku-1"]["state"] == "SUCCESS"
    assert crawls["sku-2"]["state"] == "NO_RESULT"
    absent_evidence = json.loads(crawls["sku-2"]["result_json"])
    assert absent_evidence["evidence_type"] == "EXPORTED_NO_MATCHING_ROW"
    assert absent_evidence["no_result_reason"] == EXPORT_NO_PRICE_CRAWL_REASON
    assert crawls["sku-2"]["error_text"] == EXPORT_NO_PRICE_CRAWL_REASON


def test_partial_exact_absence_probe_exception_fails_closed(tmp_path):
    """P4: absent + probe raises -> reject before any state write."""
    requests = _requests(2)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(exception=RuntimeError("network down")),
    )

    with pytest.raises(recovery.RecoveryRejected, match="probe failed: network down"):
        _run(state_path, adapter, job_id, identity)

    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"PENDING"}
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"


def test_partial_exact_absence_rerun_is_idempotent_without_new_probe(tmp_path):
    """P5: after a proven completion, rerun probes nothing and writes nothing."""
    requests = _requests(3)
    rows = [
        (request.sku_id, request.sku_id, request.url, index + 1)
        for index, request in enumerate(requests[:2])
    ]
    probe_calls: list[tuple[str, str, float]] = []
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(calls=probe_calls),
    )
    first = _run(state_path, adapter, job_id, identity)
    before_crawls = store.crawl_work_for_job(job_id)
    before_job = store.get_remote_job(job_id)

    second = _run(state_path, adapter, job_id, identity)

    assert first["crawl_no_result"] == 1
    assert len(probe_calls) == 1
    assert second["remote_calls"] == 0
    assert second["written_success"] == 0
    assert second["written_no_result"] == 0
    assert second["written_manual"] == 0
    assert second["skipped_terminal"] == 3
    assert store.crawl_work_for_job(job_id) == before_crawls
    final_job = store.get_remote_job(job_id)
    assert final_job["state"] == before_job["state"] == "COMPLETED"
    assert final_job["error_text"] == before_job["error_text"]
    assert final_job["harvest_attempts"] == before_job["harvest_attempts"]
    assert final_job["export_path"] == before_job["export_path"]


def test_partial_exact_terminal_crawls_survive_rerun_without_probe(tmp_path):
    """P6: SUCCESS/NO_RESULT crawls are never rewritten and never re-probed."""
    requests = _requests(3)
    rows = [
        (request.sku_id, request.sku_id, request.url, index + 1)
        for index, request in enumerate(requests[:2])
    ]
    store, _adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(),
    )
    first = _run(state_path, _adapter, job_id, identity)
    before_crawls = store.crawl_work_for_job(job_id)
    before_job = store.get_remote_job(job_id)
    assert first["remote_state"] == "COMPLETED"

    rerun_adapter = CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        export_probe=_make_export_probe(exception=RuntimeError("must not probe")),
    )
    second = _run(state_path, rerun_adapter, job_id, identity)

    assert second["skipped_terminal"] == 3
    assert second["written_no_result"] == 0
    assert store.crawl_work_for_job(job_id) == before_crawls
    final_job = store.get_remote_job(job_id)
    assert final_job["state"] == before_job["state"] == "COMPLETED"
    assert final_job["error_text"] == before_job["error_text"]
    assert final_job["harvest_attempts"] == before_job["harvest_attempts"]
    assert final_job["export_path"] == before_job["export_path"]


def test_partial_exact_absence_rejects_before_probe_when_no_result_limit_exceeded(tmp_path):
    """LIMIT: no_result_before + pending absent > 10 rejects before any probe call."""
    requests = _requests(11)
    probe_calls: list[tuple[str, str, float]] = []
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        [],
        export_probe=_make_export_probe(calls=probe_calls),
    )

    with pytest.raises(recovery.RecoveryRejected, match="no-result limit"):
        _run(state_path, adapter, job_id, identity)

    assert probe_calls == []
    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"PENDING"}
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"


def test_partial_exact_absence_rejects_non_no_price_export_semantic_without_writes(tmp_path):
    """Case 4b: export response lacking the exact no-price semantic -> reject, zero writes."""
    requests = _requests(2)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(export_rsp_desc="部分成功"),
    )

    with pytest.raises(
        recovery.RecoveryRejected, match="not the exact no-price semantic"
    ):
        _run(state_path, adapter, job_id, identity)

    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"PENDING"}
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"


def test_partial_exact_absence_rejects_progress_total_mismatch_without_writes(tmp_path):
    """Case 6: finished status but progress total differs from expected -> reject."""
    requests = _requests(2)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(
            progress={
                "total": 1,
                "finished": 1,
                "pending": 0,
                "running": 0,
                "failed": 0,
                "waiting_db": 0,
            },
        ),
    )

    with pytest.raises(recovery.RecoveryRejected, match="progress is not fully completed"):
        _run(state_path, adapter, job_id, identity)

    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"PENDING"}
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"


def test_partial_exact_absence_rejects_failed_progress_without_writes(tmp_path):
    """Case 7: progress reports failed subtasks -> reject, zero writes."""
    requests = _requests(2)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(
            progress={
                "total": 2,
                "finished": 1,
                "pending": 0,
                "running": 0,
                "failed": 1,
                "waiting_db": 0,
            },
        ),
    )

    with pytest.raises(recovery.RecoveryRejected, match="progress is not fully completed"):
        _run(state_path, adapter, job_id, identity)

    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"PENDING"}
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"


def test_partial_exact_absence_rejects_nonzero_export_count_without_writes(tmp_path):
    """Case 9: export endpoint still returns rows -> reject, zero writes."""
    requests = _requests(2)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(export_count=3),
    )

    with pytest.raises(recovery.RecoveryRejected, match="contains rows"):
        _run(state_path, adapter, job_id, identity)

    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"PENDING"}
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"


def test_partial_exact_absence_probe_evidence_passes_or_rejects_whole_absent_batch(tmp_path):
    """Case 11: one probe verdict covers every absent crawl in the batch."""
    requests = _requests(3)
    rows = [(requests[0].sku_id, requests[0].sku_id, requests[0].url, 10)]
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(
            progress={
                "total": 3,
                "finished": 1,
                "pending": 2,
                "running": 0,
                "failed": 0,
                "waiting_db": 0,
            },
        ),
    )

    with pytest.raises(recovery.RecoveryRejected, match="progress is not fully completed"):
        _run(state_path, adapter, job_id, identity)

    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"PENDING"}
    assert store.get_remote_job(job_id)["state"] == "MANUAL_REQUIRED"

    retry_adapter = CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        export_probe=_make_export_probe(),
    )
    retried = _run(state_path, retry_adapter, job_id, identity)

    assert retried["crawl_success"] == 1
    assert retried["crawl_no_result"] == 2
    crawls = {crawl["sku_id"]: crawl for crawl in store.crawl_work_for_job(job_id)}
    assert crawls["sku-1"]["state"] == "NO_RESULT"
    assert crawls["sku-2"]["state"] == "NO_RESULT"
    assert retried["remote_state"] == "COMPLETED"


def test_partial_exact_harvest_without_absent_rows_never_probes(tmp_path):
    """Case 12: an all-matching workbook completes the SUCCESS path with no probe call."""
    requests = _requests(2)
    rows = [
        (request.sku_id, request.sku_id, request.url, index + 1)
        for index, request in enumerate(requests)
    ]
    probe_calls: list[tuple[str, str, float]] = []
    store, adapter, job_id, identity, state_path = _seed_partial_job(
        tmp_path,
        requests,
        rows,
        export_probe=_make_export_probe(
            calls=probe_calls,
            exception=RuntimeError("must not probe"),
        ),
    )

    result = _run(state_path, adapter, job_id, identity)

    assert probe_calls == []
    assert result["crawl_success"] == 2
    assert result["crawl_no_result"] == 0
    assert result["remote_calls"] == 0
    assert result["remote_state"] == "COMPLETED"
    assert {crawl["state"] for crawl in store.crawl_work_for_job(job_id)} == {"SUCCESS"}
