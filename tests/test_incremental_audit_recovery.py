from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_recovery import (
    RecoveryRejected,
    release_model_recovery,
    resolve_exported_no_result,
    run_existing_remote_harvest,
    run_existing_remote_priority_recovery,
    run_local_remote_harvest_recovery,
    run_model_recovery,
    requeue_model_transient_failures,
)
from app.services.saas_audit_core import model_fingerprint
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
)


BUSINESS_DATE = "2026-08-11"
MODEL_NAME = "title-match"
PROMPT_VERSION = "prompt-v1"
SCHEMA_VERSION = "schema-v1"


def _row(index: int) -> dict[str, object]:
    return {
        "vCommodityId": f"v-{index}",
        "skuId": f"sku-{index}",
        "queryStr": f"query-{index}",
        "skuName": f"name-{index}",
        "extLink": f"https://shop.example/{index}",
        "__crawl_required": True,
    }


def _seed_model_work(tmp_path):
    state_path = tmp_path / "runtime" / BUSINESS_DATE / "state.sqlite3"
    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed", 60)
    rows = [_row(index) for index in range(3)]
    ingested = store.ingest_snapshot(lease, rows)
    snapshot_id = int(ingested["snapshot"]["id"])
    fingerprints = [
        model_fingerprint(row, MODEL_NAME, PROMPT_VERSION, SCHEMA_VERSION)
        for row in rows
    ]
    works = []
    for index, item in enumerate(store.ordered_snapshot_items(snapshot_id)):
        work = store.ensure_stage_work(
            lease,
            int(item["id"]),
            "MODEL",
            fingerprints[0] if index == 2 else fingerprints[index],
        )
        store.claim_stage_work(
            lease, int(work["id"]), max_attempts=2, max_elapsed_seconds=300
        )
        works.append(work)
    store.fail_stage_work(
        lease,
        int(works[0]["id"]),
        elapsed_seconds=60,
        error="ModelCallRetryExhaustedError(last_cause=TimeoutError)",
        retryable=False,
        max_attempts=2,
        max_elapsed_seconds=300,
    )
    store.fail_stage_work(
        lease,
        int(works[1]["id"]),
        elapsed_seconds=60,
        error="ModelCallRetryExhaustedError(last_cause=TimeoutError)",
        retryable=False,
        max_attempts=2,
        max_elapsed_seconds=300,
    )
    original_success = {"match_result": "SAME", "confidence": 1}
    store.complete_stage_work(lease, int(works[2]["id"]), original_success)
    store.freeze_snapshot(lease, snapshot_id)
    store.release_lease(lease)
    return store, state_path, works, original_success


def test_model_recovery_runs_only_exact_terminal_model_work(tmp_path):
    store, state_path, works, original_success = _seed_model_work(tmp_path)
    recovered_ids = [int(works[0]["id"]), int(works[1]["id"])]
    model_calls = []

    def model_batch(rows, timeout):
        model_calls.append((list(rows), timeout))
        return {
            index: {"match_result": "SAME", "confidence": 0.9}
            for index, _row_value in enumerate(rows)
        }

    result = run_model_recovery(
        state_path=state_path,
        lock_path=state_path.with_name("incremental_price_audit.lock"),
        business_date=BUSINESS_DATE,
        stage_work_ids=recovered_ids,
        model_batch=model_batch,
        model_name=MODEL_NAME,
        model_prompt_version=PROMPT_VERSION,
        model_schema_version=SCHEMA_VERSION,
        timeout_seconds=60,
    )

    assert result["outcome"] == "MODEL_RECOVERY_PENDING_REVIEW"
    assert result["stage_work_ids"] == recovered_ids
    assert [entry["stage_work_id"] for entry in result["recovered_results"]] == recovered_ids
    assert all(
        entry["result"] == {"match_result": "SAME", "confidence": 0.9}
        for entry in result["recovered_results"]
    )
    assert len(model_calls) == 1
    assert len(model_calls[0][0]) == 2
    assert all(
        store.get_stage_work(work_id)["state"] == "RECOVERED_PENDING_REVIEW"
        for work_id in recovered_ids
    )
    untouched = store.get_stage_work(int(works[2]["id"]))
    assert untouched["state"] == "SUCCESS"
    assert json.loads(untouched["result_json"]) == original_success
    lease = store.acquire_lease(BUSINESS_DATE, "inspect", 60)
    assert store.reuse_successful_stage_work(lease, recovered_ids[0]) is None
    assert store.get_stage_work(recovered_ids[0])["state"] == "RECOVERED_PENDING_REVIEW"
    assert store.list_crawl_work(lease) == []
    assert store.list_remote_jobs(lease) == []
    store.release_lease(lease)

    with pytest.raises(RecoveryRejected, match="not TERMINAL_FAILED"):
        run_model_recovery(
            state_path=state_path,
            lock_path=state_path.with_name("incremental_price_audit.lock"),
            business_date=BUSINESS_DATE,
            stage_work_ids=recovered_ids,
            model_batch=lambda *_args: pytest.fail("successful recovery must not rerun"),
            model_name=MODEL_NAME,
            model_prompt_version=PROMPT_VERSION,
            model_schema_version=SCHEMA_VERSION,
            timeout_seconds=60,
        )


def test_model_recovery_requires_exact_explicit_release_after_review(tmp_path):
    store, state_path, works, original_success = _seed_model_work(tmp_path)
    recovered_ids = [int(works[0]["id"]), int(works[1]["id"])]
    run_model_recovery(
        state_path=state_path,
        lock_path=state_path.with_name("incremental_price_audit.lock"),
        business_date=BUSINESS_DATE,
        stage_work_ids=recovered_ids,
        model_batch=lambda rows, _timeout: {
            index: {"match_result": "SAME", "confidence": 0.9}
            for index, _row_value in enumerate(rows)
        },
        model_name=MODEL_NAME,
        model_prompt_version=PROMPT_VERSION,
        model_schema_version=SCHEMA_VERSION,
        timeout_seconds=60,
    )

    result = release_model_recovery(
        state_path=state_path,
        lock_path=state_path.with_name("incremental_price_audit.lock"),
        business_date=BUSINESS_DATE,
        stage_work_ids=recovered_ids,
    )

    assert result["outcome"] == "MODEL_RECOVERY_RELEASED"
    assert result["model_calls"] == 0
    assert result["remote_calls"] == 0
    assert all(store.get_stage_work(work_id)["state"] == "SUCCESS" for work_id in recovered_ids)
    untouched = store.get_stage_work(int(works[2]["id"]))
    assert json.loads(untouched["result_json"]) == original_success
    with pytest.raises(RecoveryRejected, match="pending review"):
        release_model_recovery(
            state_path=state_path,
            lock_path=state_path.with_name("incremental_price_audit.lock"),
            business_date=BUSINESS_DATE,
            stage_work_ids=recovered_ids,
        )


def test_model_recovery_rejects_success_missing_and_non_model_ids_without_mutation(tmp_path):
    store, state_path, works, original_success = _seed_model_work(tmp_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-detail", 60)
    item = store.get_audit_item(int(works[0]["audit_item_id"]))
    detail = store.ensure_stage_work(lease, int(item["id"]), "DETAIL", "detail-fingerprint")
    store.claim_stage_work(lease, int(detail["id"]), max_attempts=1, max_elapsed_seconds=60)
    store.fail_stage_work(
        lease,
        int(detail["id"]),
        elapsed_seconds=1,
        error="detail terminal",
        retryable=False,
        max_attempts=1,
        max_elapsed_seconds=60,
    )
    store.release_lease(lease)
    terminal_before = store.get_stage_work(int(works[0]["id"])).copy()

    for invalid_id, error in (
        (int(works[2]["id"]), "not TERMINAL_FAILED"),
        (999999, "does not exist"),
        (int(detail["id"]), "is not MODEL"),
    ):
        with pytest.raises(RecoveryRejected, match=error):
            run_model_recovery(
                state_path=state_path,
                lock_path=state_path.with_name("incremental_price_audit.lock"),
                business_date=BUSINESS_DATE,
                stage_work_ids=[int(works[0]["id"]), invalid_id],
                model_batch=lambda *_args: pytest.fail("invalid recovery must not call model"),
                model_name=MODEL_NAME,
                model_prompt_version=PROMPT_VERSION,
                model_schema_version=SCHEMA_VERSION,
                timeout_seconds=60,
            )
        assert store.get_stage_work(int(works[0]["id"])) == terminal_before
    assert json.loads(store.get_stage_work(int(works[2]["id"]))["result_json"]) == original_success


def test_model_recovery_timeout_returns_selected_stages_to_terminal_state(tmp_path):
    store, state_path, works, _original_success = _seed_model_work(tmp_path)
    selected = [int(works[0]["id"]), int(works[1]["id"])]

    with pytest.raises(TimeoutError):
        run_model_recovery(
            state_path=state_path,
            lock_path=state_path.with_name("incremental_price_audit.lock"),
            business_date=BUSINESS_DATE,
            stage_work_ids=selected,
            model_batch=lambda *_args: (_ for _ in ()).throw(TimeoutError()),
            model_name=MODEL_NAME,
            model_prompt_version=PROMPT_VERSION,
            model_schema_version=SCHEMA_VERSION,
            timeout_seconds=60,
        )

    recovered = [store.get_stage_work(work_id) for work_id in selected]
    assert all(work["state"] == "TERMINAL_FAILED" for work in recovered)
    assert all(work["attempts"] == 1 for work in recovered)
    assert all(work["error_text"] == "TimeoutError" for work in recovered)


def test_model_recovery_preserves_all_585_success_results(tmp_path):
    state_path = tmp_path / "runtime" / BUSINESS_DATE / "state.sqlite3"
    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-scale", 600)
    ingested = store.ingest_snapshot(lease, [_row(index) for index in range(595)])
    snapshot_id = int(ingested["snapshot"]["id"])
    terminal_ids = []
    success_before = {}
    for index, item in enumerate(store.ordered_snapshot_items(snapshot_id)):
        work = store.ensure_stage_work(
            lease,
            int(item["id"]),
            "MODEL",
            model_fingerprint(item["row"], MODEL_NAME, PROMPT_VERSION, SCHEMA_VERSION),
        )
        store.claim_stage_work(
            lease, int(work["id"]), max_attempts=1, max_elapsed_seconds=300
        )
        if index < 10:
            store.fail_stage_work(
                lease,
                int(work["id"]),
                elapsed_seconds=60,
                error="ModelCallRetryExhaustedError(last_cause=TimeoutError)",
                retryable=False,
                max_attempts=1,
                max_elapsed_seconds=300,
            )
            terminal_ids.append(int(work["id"]))
        else:
            result = {"match_result": "SAME", "ordinal": index}
            store.complete_stage_work(lease, int(work["id"]), result)
            success_before[int(work["id"])] = store.get_stage_work(int(work["id"])).copy()
    store.freeze_snapshot(lease, snapshot_id)
    store.release_lease(lease)

    run_model_recovery(
        state_path=state_path,
        lock_path=state_path.with_name("incremental_price_audit.lock"),
        business_date=BUSINESS_DATE,
        stage_work_ids=terminal_ids,
        model_batch=lambda rows, _timeout: {
            index: {"match_result": "SAME", "recovered": True}
            for index, _row_value in enumerate(rows)
        },
        model_name=MODEL_NAME,
        model_prompt_version=PROMPT_VERSION,
        model_schema_version=SCHEMA_VERSION,
        timeout_seconds=60,
    )

    assert len(success_before) == 585
    assert {
        work_id: store.get_stage_work(work_id) for work_id in success_before
    } == success_before
    assert all(
        store.get_stage_work(work_id)["state"] == "RECOVERED_PENDING_REVIEW"
        for work_id in terminal_ids
    )


def test_transient_requeue_changes_exactly_54_terminals_and_preserves_541_successes(tmp_path):
    state_path = tmp_path / "runtime" / BUSINESS_DATE / "state.sqlite3"
    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-transient", 600)
    rows = [_row(index) for index in range(595)]
    snapshot_id = int(store.ingest_snapshot(lease, rows)["snapshot"]["id"])
    terminal_ids: list[int] = []
    success_before: dict[int, dict[str, object]] = {}
    error_text = "ModelCallRetryExhaustedError(attempts=1,last_cause=TimeoutError)"
    for index, item in enumerate(store.ordered_snapshot_items(snapshot_id)):
        work = store.ensure_stage_work(
            lease,
            int(item["id"]),
            "MODEL",
            model_fingerprint(item["row"], MODEL_NAME, PROMPT_VERSION, SCHEMA_VERSION),
        )
        store.claim_stage_work(
            lease, int(work["id"]), max_attempts=2, max_elapsed_seconds=300
        )
        if index < 54:
            store.fail_stage_work(
                lease,
                int(work["id"]),
                elapsed_seconds=60,
                error=error_text,
                retryable=False,
                max_attempts=2,
                max_elapsed_seconds=300,
            )
            terminal_ids.append(int(work["id"]))
        else:
            store.complete_stage_work(
                lease, int(work["id"]), {"match_result": "SAME", "ordinal": index}
            )
            success_before[int(work["id"])] = store.get_stage_work(int(work["id"])).copy()
    store.release_lease(lease)

    result = requeue_model_transient_failures(
        state_path=state_path,
        lock_path=state_path.with_name("incremental_price_audit.lock"),
        business_date=BUSINESS_DATE,
        expected_count=54,
        expected_attempts=1,
    )

    assert result == {
        "outcome": "MODEL_TRANSIENT_REQUEUED",
        "business_date": BUSINESS_DATE,
        "expected_count": 54,
        "expected_attempts": 1,
        "error_type": "ModelCallRetryExhaustedError",
        "last_cause": "TimeoutError",
        "recovered": 54,
        "skipped": 0,
        "failed": 0,
        "stage_work_ids": terminal_ids,
        "model_calls": 0,
        "remote_writes": 0,
    }
    recovered = [store.get_stage_work(work_id) for work_id in terminal_ids]
    assert all(work["state"] == "RETRYABLE_FAILED" for work in recovered)
    assert all(work["retryable"] == 1 for work in recovered)
    assert all(work["attempts"] == 1 for work in recovered)
    assert all(work["elapsed_seconds"] == 60 for work in recovered)
    assert all(work["error_text"] == error_text for work in recovered)
    assert len(success_before) == 541
    assert {
        work_id: store.get_stage_work(work_id) for work_id in success_before
    } == success_before
    inspect = store.acquire_lease(BUSINESS_DATE, "inspect-transient", 60)
    assert store.list_crawl_work(inspect) == []
    assert store.list_remote_jobs(inspect) == []
    store.release_lease(inspect)

    with pytest.raises(RecoveryRejected, match="exactly 54"):
        requeue_model_transient_failures(
            state_path=state_path,
            lock_path=state_path.with_name("incremental_price_audit.lock"),
            business_date=BUSINESS_DATE,
            expected_count=54,
            expected_attempts=1,
        )


def test_transient_requeue_fails_closed_when_any_terminal_has_other_evidence(tmp_path):
    state_path = tmp_path / "runtime" / BUSINESS_DATE / "state.sqlite3"
    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-mixed", 60)
    snapshot_id = int(store.ingest_snapshot(lease, [_row(1), _row(2)])["snapshot"]["id"])
    work_ids = []
    for index, item in enumerate(store.ordered_snapshot_items(snapshot_id)):
        work = store.ensure_stage_work(
            lease,
            int(item["id"]),
            "MODEL",
            model_fingerprint(item["row"], MODEL_NAME, PROMPT_VERSION, SCHEMA_VERSION),
        )
        store.claim_stage_work(
            lease, int(work["id"]), max_attempts=2, max_elapsed_seconds=300
        )
        store.fail_stage_work(
            lease,
            int(work["id"]),
            elapsed_seconds=60,
            error=(
                "ModelCallRetryExhaustedError(attempts=1,last_cause=TimeoutError)"
                if index == 0
                else "ModelCallRetryExhaustedError(attempts=1,last_cause=HTTPError,status=400)"
            ),
            retryable=False,
            max_attempts=2,
            max_elapsed_seconds=300,
        )
        work_ids.append(int(work["id"]))
    store.release_lease(lease)

    with pytest.raises(RecoveryRejected, match="does not exactly match"):
        requeue_model_transient_failures(
            state_path=state_path,
            lock_path=state_path.with_name("incremental_price_audit.lock"),
            business_date=BUSINESS_DATE,
            expected_count=2,
            expected_attempts=1,
        )

    assert all(store.get_stage_work(work_id)["state"] == "TERMINAL_FAILED" for work_id in work_ids)


def _write_result(path: Path, rows, *, task_name: str, mall_key: str):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "商品编码",
        "商品最小品类单元id",
        "原始链接",
        "商品价格",
        "执行编号",
        "批次号",
        "商城标识",
    ])
    for index, row in enumerate(rows):
        sheet.append([*row, f"task-number-execution-{index}", task_name, mall_key])
    workbook.save(path)


def test_remote_manual_recovery_reuses_existing_export_and_backfills_only_one_job(tmp_path):
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    state_path = run_dir / "state.sqlite3"
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.parent.mkdir(parents=True)
    batch_state.write_text("# state\n", encoding="utf-8")
    runner_calls = []
    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        runner=lambda *_args: runner_calls.append(1),
    )
    requests = tuple(
        CrawlRequest(
            mall_key="mall",
            mall_source_id="source",
            mall_name="Mall",
            sku_id=f"sku-{index}",
            url=f"https://shop.example/{index}",
            backend_version="legacy-v1",
        )
        for index in range(10)
    )
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        requests,
        run_dir / "input.xlsx",
        run_dir / "result.xlsx",
        "task-target",
        batch_state,
    )
    _write_result(
        prepared.result_path,
        [
            [f"wrong-{index}", f"sku-{index}", f"https://shop.example/{index}", index + 1]
            for index in range(10)
        ],
        task_name=prepared.task_name,
        mall_key=prepared.mall_key,
    )
    events = (
        {"action": "CREATED", "mall": "mall", "task_name": "task-target", "task_id": "task-id", "task_number": "task-number", "expected_subtask_count": 10},
        {"action": "ENABLED", "mall": "mall", "task_name": "task-target", "task_id": "task-id", "task_number": "task-number"},
        {"action": "EXPORTED", "mall": "mall", "task_name": "task-target", "task_id": "task-id", "task_number": "task-number", "output_path": str(prepared.result_path)},
    )
    with batch_state.open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(f"- `{json.dumps(event)}`\n")

    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-remote", 60)
    ingested = store.ingest_snapshot(lease, [_row(index) for index in range(10)])
    store.freeze_snapshot(lease, int(ingested["snapshot"]["id"]))
    payload = {
        "requests": [request.__dict__ for request in requests],
        "batch_state": str(batch_state),
    }
    target = store.prepare_remote_intent(
        lease,
        prepared.logical_job_key,
        prepared.task_name,
        prepared.payload_sha256,
        mall_key=prepared.mall_key,
        input_path=str(prepared.template_path),
        result_path=str(prepared.result_path),
        payload_json=payload,
    )
    store.claim_remote_submission(lease, int(target["id"]))
    identity = "mall|task-id|task-number"
    store.reconcile_remote_job(
        lease, int(target["id"]), remote_identity=identity, state="SUBMITTED"
    )
    store.mark_remote_manual(lease, int(target["id"]), "export validation failed")
    for index, request in enumerate(requests):
        crawl = store.ensure_crawl_work(
            lease,
            request.mall_key,
            request.sku_id,
            request.url,
            request.backend_version,
            f"crawl-{index}",
            source_id=f"v-{index}",
        )
        store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(target["id"]))
    other_ids = []
    for index in range(26):
        other = store.prepare_remote_intent(
            lease,
            f"other-{index}",
            f"task-other-{index}",
            f"payload-{index}",
        )
        other_ids.append(int(other["id"]))
    before = {job_id: store.get_remote_job(job_id) for job_id in other_ids}
    store.release_lease(lease)

    result = run_local_remote_harvest_recovery(
        state_path=state_path,
        lock_path=run_dir / "incremental_price_audit.lock",
        business_date=BUSINESS_DATE,
        remote_job_id=int(target["id"]),
        remote_identity=identity,
        adapter=adapter,
    )

    assert result["outcome"] == "REMOTE_HARVEST_RECOVERY_COMPLETED"
    assert result["crawl_completed"] == 10
    assert result["matched"] == 10
    assert result["foreign_filtered"] == 0
    assert result["remote_calls"] == 0
    assert runner_calls == []
    assert store.get_remote_job(int(target["id"]))["state"] == "COMPLETED"
    crawls = store.crawl_work_for_job(int(target["id"]))
    assert len(crawls) == 10
    assert all(crawl["state"] == "SUCCESS" for crawl in crawls)
    assert all(json.loads(crawl["result_json"])["page_price_status"] == "OK" for crawl in crawls)
    assert {job_id: store.get_remote_job(job_id) for job_id in other_ids} == before


def test_no_result_recovery_uses_exact_completed_export_without_borrowing_sibling_price(
    tmp_path,
):
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    state_path = run_dir / "state.sqlite3"
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.parent.mkdir(parents=True)
    batch_state.write_text("# state\n", encoding="utf-8")
    adapter = CnncLegacyZhaoshangAdapter(mappings=[], db_env_path=None)
    shared_url = "https://shop.example/item?sku=base"
    requests = (
        CrawlRequest("mall", "source", "Mall", "basePCS", shared_url, "legacy-v1"),
        CrawlRequest("mall", "source", "Mall", "base", shared_url, "legacy-v1"),
    )
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        requests,
        run_dir / "input.xlsx",
        run_dir / "result.xlsx",
        "task-target",
        batch_state,
    )
    _write_result(
        prepared.result_path,
        [["basePCS", "base", shared_url, 12.3]],
        task_name=prepared.task_name,
        mall_key=prepared.mall_key,
    )
    events = (
        {
            "action": "CREATED",
            "mall": "mall",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
            "expected_subtask_count": 2,
        },
        {
            "action": "ENABLED",
            "mall": "mall",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
        },
        {
            "action": "EXPORTED",
            "mall": "mall",
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
    lease = store.acquire_lease(BUSINESS_DATE, "seed-no-result", 60)
    ingested = store.ingest_snapshot(lease, [_row(1)])
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
    identity = "mall|task-id|task-number"
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity=identity,
        state="COMPLETED",
        export_path=str(prepared.result_path),
        result={shared_url: {"page_price_value": 12.3}},
    )
    sibling = store.ensure_crawl_work(
        lease,
        "mall",
        "basePCS",
        shared_url,
        "legacy-v1",
        "sibling-fingerprint",
        source_id="v-1",
    )
    target = store.ensure_crawl_work(
        lease,
        "mall",
        "base",
        shared_url,
        "legacy-v1",
        "target-fingerprint",
        source_id="v-1",
    )
    for crawl in (sibling, target):
        store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    sibling_result = {"page_price_status": "OK", "page_price_value": 12.3}
    store.complete_crawl_work(lease, int(sibling["id"]), sibling_result)
    error = "legacy export missing safe skuId / exact URL result"
    store.fail_crawl_work(lease, int(target["id"]), error, retryable=False)
    store.release_lease(lease)

    result = resolve_exported_no_result(
        state_path=state_path,
        lock_path=run_dir / "incremental_price_audit.lock",
        business_date=BUSINESS_DATE,
        crawl_work_id=int(target["id"]),
        remote_job_id=int(job["id"]),
        remote_identity=identity,
        adapter=adapter,
    )

    assert result["outcome"] == "CRAWL_NO_RESULT_RESOLVED"
    assert result["remote_calls"] == 0
    assert store.get_crawl_work(int(target["id"]))["state"] == "NO_RESULT"
    target_evidence = json.loads(store.get_crawl_work(int(target["id"]))["result_json"])
    assert target_evidence["evidence_type"] == "EXPORTED_NO_MATCHING_ROW"
    assert target_evidence["page_price_value"] is None
    assert json.loads(store.get_crawl_work(int(sibling["id"]))["result_json"]) == sibling_result


def test_existing_remote_harvest_persists_partial_export_as_success_and_no_result(
    tmp_path,
):
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    state_path = run_dir / "state.sqlite3"
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.parent.mkdir(parents=True)
    batch_state.write_text("# state\n", encoding="utf-8")
    requests = tuple(
        CrawlRequest(
            "mall",
            "source",
            "Mall",
            f"sku-{index}",
            f"https://shop.example/{index}",
            "legacy-v1",
        )
        for index in range(3)
    )
    runner_calls = []
    prepared_holder = {}

    def wait_export(args, timeout_seconds, _environment):
        runner_calls.append((list(args), timeout_seconds))
        assert "--wait-export-only" in args
        assert "--create-only" not in args
        assert "/add" not in " ".join(args)
        prepared = prepared_holder["prepared"]
        _write_result(
            prepared.result_path,
            [
                ["wrong-0", "sku-0", "https://shop.example/0", 12.3],
                ["wrong-1", "sku-1", "https://shop.example/1", 0],
            ],
            task_name=prepared.task_name,
            mall_key=prepared.mall_key,
        )
        event = {
            "action": "EXPORTED",
            "mall": "mall",
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
        "task-target",
        batch_state,
    )
    prepared_holder["prepared"] = prepared
    events = (
        {
            "action": "CREATED",
            "mall": "mall",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
            "expected_subtask_count": 3,
        },
        {
            "action": "ENABLED",
            "mall": "mall",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
        },
        {
            "action": "PRIORITY_UPDATED",
            "mall": "mall",
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
    lease = store.acquire_lease(BUSINESS_DATE, "seed-harvest", 60)
    ingested = store.ingest_snapshot(lease, [_row(index) for index in range(3)])
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
    identity = "mall|task-id|task-number"
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity=identity,
        state="SUBMITTED",
    )
    for index, request in enumerate(requests):
        crawl = store.ensure_crawl_work(
            lease,
            request.mall_key,
            request.sku_id,
            request.url,
            request.backend_version,
            f"crawl-{index}",
            source_id=f"v-{index}",
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
        timeout_seconds=60,
    )

    assert result["outcome"] == "REMOTE_EXISTING_HARVEST_COMPLETED"
    assert result["expected"] == 3
    assert result["remote_calls"] == 1
    assert result["add_calls"] == 0
    assert len(runner_calls) == 1
    assert result["no_result"] == 1
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert [crawl["state"] for crawl in crawls] == ["SUCCESS", "SUCCESS", "NO_RESULT"]
    assert json.loads(crawls[-1]["result_json"])["page_price_value"] is None


def test_priority_recovery_updates_only_one_existing_exact_remote_job(tmp_path):
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    state_path = run_dir / "state.sqlite3"
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.parent.mkdir(parents=True)
    batch_state.write_text("# state\n", encoding="utf-8")
    priority_calls = []

    def prioritize(job, task_id, task_number, _timeout):
        priority_calls.append((job.task_name, task_id, task_number, len(job.requests)))
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
        runner=lambda *_args: pytest.fail("priority recovery must never call /add"),
    )
    requests = tuple(
        CrawlRequest(
            "mall",
            "source",
            "Mall",
            f"sku-{index}",
            f"https://shop.example/{index}",
            "legacy-v1",
        )
        for index in range(3)
    )
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        requests,
        run_dir / "input.xlsx",
        run_dir / "result.xlsx",
        "task-target",
        batch_state,
    )
    events = (
        {
            "action": "CREATED",
            "mall": "mall",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
            "expected_subtask_count": 3,
        },
        {
            "action": "ENABLED",
            "mall": "mall",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
        },
        {
            "action": "PRIORITY_UNCONFIRMED",
            "mall": "mall",
            "task_name": prepared.task_name,
            "task_id": "task-id",
            "task_number": "task-number",
            "reason": "TimeoutError",
        },
    )
    with batch_state.open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(f"- `{json.dumps(event)}`\n")

    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-priority", 60)
    ingested = store.ingest_snapshot(lease, [_row(index) for index in range(3)])
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
    identity = "mall|task-id|task-number"
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity=identity,
        state="PRIORITY_PENDING",
    )
    for index, request in enumerate(requests):
        crawl = store.ensure_crawl_work(
            lease,
            request.mall_key,
            request.sku_id,
            request.url,
            request.backend_version,
            f"crawl-{index}",
            source_id=f"v-{index}",
        )
        store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    store.release_lease(lease)

    result = run_existing_remote_priority_recovery(
        state_path=state_path,
        lock_path=run_dir / "incremental_price_audit.lock",
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=identity,
        adapter=adapter,
        timeout_seconds=60,
    )

    assert result == {
        "outcome": "REMOTE_PRIORITY_RECOVERY_COMPLETED",
        "business_date": BUSINESS_DATE,
        "remote_job_id": int(job["id"]),
        "remote_identity": identity,
        "expected": 3,
        "matched": 3,
        "weight_type_minus_one": 3,
        "weight_type_zero": 0,
        "weight_type_other": 0,
        "priority_calls": 1,
        "add_calls": 0,
    }
    assert priority_calls == [(prepared.task_name, "task-id", "task-number", 3)]
    assert store.get_remote_job(int(job["id"]))["state"] == "SUBMITTED"
    assert {crawl["state"] for crawl in store.crawl_work_for_job(int(job["id"]))} == {
        "SUBMITTED"
    }


def _seed_manual_priority_job_17(
    tmp_path,
    *,
    result_state="PRIORITY_PENDING",
    error_text="InterfaceError",
    mark_manual=True,
    freeze_run=True,
):
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    state_path = run_dir / "state.sqlite3"
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.parent.mkdir(parents=True)
    batch_state.write_text("# state\n", encoding="utf-8")
    priority_calls = []

    def prioritize(job, task_id, task_number, _timeout):
        priority_calls.append((job.task_name, task_id, task_number))
        event = {
            "action": "PRIORITY_UPDATED",
            "mall": job.mall_key,
            "task_name": job.task_name,
            "task_id": task_id,
            "task_number": task_number,
            "affected_rows": 1,
        }
        with job.batch_state_path.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event)}`\n")

    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        priority_runner=prioritize,
        runner=lambda *_args: pytest.fail("priority recovery must never call /add"),
    )
    request = CrawlRequest(
        "jd",
        "source",
        "京东",
        "sku-17",
        "https://item.jd.com/sku-17",
        "legacy-v1",
    )
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        (request,),
        run_dir / "input.xlsx",
        run_dir / "result.xlsx",
        "cnnc_current_opt_京东_28089831959826",
        batch_state,
    )
    for action in ("CREATED", "ENABLED", "PRIORITY_UNCONFIRMED"):
        event = {
            "action": action,
            "mall": "jd",
            "task_name": prepared.task_name,
            "task_id": "1242978",
            "task_number": "RW-20260818110643",
        }
        if action == "CREATED":
            event["expected_subtask_count"] = 1
        if action == "PRIORITY_UNCONFIRMED":
            event["reason"] = "InterfaceError"
        with batch_state.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event)}`\n")

    store = DailyAuditStore(state_path)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-priority-17", 60)
    ingested = store.ingest_snapshot(lease, [_row(17)])
    if freeze_run:
        store.freeze_snapshot(lease, int(ingested["snapshot"]["id"]))
    for index in range(16):
        store.prepare_remote_intent(
            lease,
            f"filler-logical-{index}",
            f"filler-task-{index}",
            f"filler-payload-{index}",
        )
    payload = {
        "requests": [request.__dict__],
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
    assert int(job["id"]) == 17
    store.claim_remote_submission(lease, 17)
    identity = "jd|1242978|RW-20260818110643"
    store.reconcile_remote_job(
        lease,
        17,
        remote_identity=identity,
        state="PRIORITY_PENDING",
        result={"state": result_state, "remote_identity": identity},
    )
    crawl = store.ensure_crawl_work(
        lease,
        request.mall_key,
        request.sku_id,
        request.url,
        request.backend_version,
        "crawl-17",
        source_id="v-17",
    )
    store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), 17)
    if mark_manual:
        store.mark_remote_manual(lease, 17, error_text)
    store.release_lease(lease)
    return state_path, run_dir, store, adapter, priority_calls, identity


def test_manual_priority_recovery_compare_and_check_reuses_exact_job_17(tmp_path):
    state_path, run_dir, store, adapter, priority_calls, identity = (
        _seed_manual_priority_job_17(tmp_path)
    )

    result = run_existing_remote_priority_recovery(
        state_path=state_path,
        lock_path=run_dir / "incremental_price_audit.lock",
        business_date=BUSINESS_DATE,
        remote_job_id=17,
        remote_identity=identity,
        expected_current_state="MANUAL_REQUIRED",
        expected_result_state="PRIORITY_PENDING",
        expected_error_text="InterfaceError",
        adapter=adapter,
        timeout_seconds=60,
    )

    recovered = store.get_remote_job(17)
    assert result["remote_job_id"] == 17
    assert result["remote_identity"] == identity
    assert result["add_calls"] == 0
    assert recovered["id"] == 17
    assert recovered["state"] == "SUBMITTED"
    assert recovered["remote_identity"] == identity
    assert priority_calls == [
        ("cnnc_current_opt_京东_28089831959826", "1242978", "RW-20260818110643")
    ]


def test_manual_priority_recovery_keeps_frozen_run_gate(tmp_path):
    state_path, run_dir, store, adapter, priority_calls, identity = (
        _seed_manual_priority_job_17(tmp_path, freeze_run=False)
    )

    with pytest.raises(RecoveryRejected, match="frozen daily run"):
        run_existing_remote_priority_recovery(
            state_path=state_path,
            lock_path=run_dir / "incremental_price_audit.lock",
            business_date=BUSINESS_DATE,
            remote_job_id=17,
            remote_identity=identity,
            expected_current_state="MANUAL_REQUIRED",
            expected_result_state="PRIORITY_PENDING",
            expected_error_text="InterfaceError",
            adapter=adapter,
            timeout_seconds=60,
        )

    assert store.get_remote_job(17)["state"] == "MANUAL_REQUIRED"
    assert priority_calls == []


@pytest.mark.parametrize(
    (
        "result_state",
        "error_text",
        "mark_manual",
        "requested_job_id",
        "requested_identity",
    ),
    [
        ("SUBMITTED", "InterfaceError", True, 17, None),
        ("PRIORITY_PENDING", "priority identity conflict", True, 17, None),
        ("PRIORITY_PENDING", "InterfaceError", False, 17, None),
        ("PRIORITY_PENDING", "InterfaceError", True, 18, None),
        (
            "PRIORITY_PENDING",
            "InterfaceError",
            True,
            17,
            "jd|different-id|RW-20260818110643",
        ),
    ],
)
def test_manual_priority_recovery_rejects_any_compare_and_check_mismatch(
    tmp_path,
    result_state,
    error_text,
    mark_manual,
    requested_job_id,
    requested_identity,
):
    state_path, run_dir, store, adapter, priority_calls, identity = (
        _seed_manual_priority_job_17(
            tmp_path,
            result_state=result_state,
            error_text=error_text,
            mark_manual=mark_manual,
        )
    )

    with pytest.raises(RecoveryRejected):
        run_existing_remote_priority_recovery(
            state_path=state_path,
            lock_path=run_dir / "incremental_price_audit.lock",
            business_date=BUSINESS_DATE,
            remote_job_id=requested_job_id,
            remote_identity=requested_identity or identity,
            expected_current_state="MANUAL_REQUIRED",
            expected_result_state="PRIORITY_PENDING",
            expected_error_text="InterfaceError",
            adapter=adapter,
            timeout_seconds=60,
        )

    assert store.get_remote_job(17)["state"] == (
        "MANUAL_REQUIRED" if mark_manual else "PRIORITY_PENDING"
    )
    assert priority_calls == []
