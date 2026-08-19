from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_recovery import (
    RecoveryRejected,
    WAIT_EXPORT_MANUAL_DIAGNOSTIC,
    run_remote_wait_export_recovery,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
)


BUSINESS_DATE = "2026-08-19"
TASK_ID = "1246803"
TASK_NUMBER = "RW-20260819160830"
IDENTITY = f"lxwl|{TASK_ID}|{TASK_NUMBER}"


def _row(index: int, *, sku: str | None = None, url: str | None = None) -> dict[str, object]:
    return {
        "vCommodityId": f"v-{index}",
        "skuId": sku or f"sku-{index}",
        "queryStr": f"query-{index}",
        "skuName": f"name-{index}",
        "extLink": url or f"https://shop.example/{index}",
        "__crawl_required": True,
    }


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
        sheet.append([*row, f"{TASK_NUMBER}-exec-{index}", task_name, mall_key])
    workbook.save(path)


class _Seed:
    def __init__(self, tmp_path: Path):
        self.run_dir = tmp_path / "runtime" / BUSINESS_DATE
        self.state_path = self.run_dir / "state.sqlite3"
        self.lock_path = self.run_dir / "incremental_price_audit.lock"
        self.batch_state = self.run_dir / "BATCH_STATE.md"
        self.batch_state.parent.mkdir(parents=True)
        self.batch_state.write_text("# state\n", encoding="utf-8")
        self.runner_calls: list[list[str]] = []
        self.prepared_holder: dict[str, object] = {}
        self.adapter = CnncLegacyZhaoshangAdapter(
            mappings=[],
            db_env_path=None,
            runner=self._record_runner,
        )

    def _record_runner(self, args, timeout_seconds, _environment):
        self.runner_calls.append(list(args))
        return self.runner(args, timeout_seconds, _environment)

    def runner(self, args, timeout_seconds, _environment):  # overridden per test
        raise AssertionError("runner must be provided by the test")

    def build_requests(self, count: int = 3):
        return tuple(
            CrawlRequest(
                mall_key="lxwl",
                mall_source_id="34",
                mall_name="领先未来商城",
                sku_id=f"sku-{index}",
                url=f"https://shop.example/{index}",
                backend_version="legacy-v1",
            )
            for index in range(count)
        )

    def seed(self, rows, requests, *, manual_error: str = WAIT_EXPORT_MANUAL_DIAGNOSTIC, freeze: bool = True, crawl_source_specs=None):
        prepared = self.adapter.prepare_job(
            BUSINESS_DATE,
            requests,
            self.run_dir / "input.xlsx",
            self.run_dir / "result.xlsx",
            "task-target",
            self.batch_state,
        )
        self.prepared_holder["prepared"] = prepared
        events = (
            {
                "action": "CREATED",
                "mall": "lxwl",
                "task_name": prepared.task_name,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
                "expected_subtask_count": len(requests),
            },
            {
                "action": "ENABLED",
                "mall": "lxwl",
                "task_name": prepared.task_name,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
            },
        )
        if manual_error is not None:
            events = events + (
                {
                    "action": "PRIORITY_UPDATED",
                    "mall": "lxwl",
                    "task_name": prepared.task_name,
                    "task_id": TASK_ID,
                    "task_number": TASK_NUMBER,
                    "affected_rows": len(requests),
                },
            )
        with self.batch_state.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(f"- `{json.dumps(event)}`\n")

        store = DailyAuditStore(self.state_path)
        lease = store.acquire_lease(BUSINESS_DATE, "seed", 60)
        ingested = store.ingest_snapshot(lease, rows)
        snapshot_id = int(ingested["snapshot"]["id"])
        if freeze:
            store.freeze_snapshot(lease, snapshot_id)
        payload = {
            "requests": [request.__dict__ for request in requests],
            "batch_state": str(self.batch_state),
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
        store.reconcile_remote_job(
            lease,
            int(job["id"]),
            remote_identity=IDENTITY,
            state="SUBMITTED",
        )
        if crawl_source_specs is None:
            crawl_source_specs = {}
            for index, request in enumerate(requests):
                crawl_source_specs.setdefault((request.sku_id, request.url), []).append(f"v-{index}")
        for (sku_id, url), source_ids in crawl_source_specs.items():
            for source_id in source_ids:
                crawl = store.ensure_crawl_work(
                    lease,
                    "lxwl",
                    sku_id,
                    url,
                    "legacy-v1",
                    url,
                    source_id=source_id,
                )
                store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
        if manual_error is not None:
            store.mark_remote_manual(lease, int(job["id"]), manual_error)
        store.release_lease(lease)
        return store, job, snapshot_id

    def recover(self, store, job, **overrides):
        kwargs = dict(
            state_path=self.state_path,
            lock_path=self.lock_path,
            business_date=BUSINESS_DATE,
            remote_job_id=int(job["id"]),
            remote_identity=IDENTITY,
            expected_error_text=WAIT_EXPORT_MANUAL_DIAGNOSTIC,
            adapter=self.adapter,
            timeout_seconds=60.0,
        )
        kwargs.update(overrides)
        return run_remote_wait_export_recovery(**kwargs)


def _exporting_runner(seed: _Seed, rows, *, event_task_number: str = TASK_NUMBER):
    def runner(args, timeout_seconds, _environment):
        assert "--wait-export-only" in args
        assert "--create-only" not in args
        prepared = seed.prepared_holder["prepared"]
        _write_result(
            prepared.result_path,
            rows,
            task_name=prepared.task_name,
            mall_key=prepared.mall_key,
        )
        event = {
            "action": "EXPORTED",
            "mall": "lxwl",
            "task_name": prepared.task_name,
            "task_id": TASK_ID,
            "task_number": event_task_number,
            "output_path": str(prepared.result_path),
        }
        with seed.batch_state.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event)}`\n")

        class Result:
            returncode = 0

        return Result()

    return runner


def test_wait_export_recovery_completes_manual_job_from_original_remote_task(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(
        seed,
        [
            ["code-0", "sku-0", "https://shop.example/0", 12.3],
            ["code-1", "sku-1", "https://shop.example/1", 0],
            ["code-2", "sku-2", "https://shop.example/2", 7.7],
        ],
    )
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    result = seed.recover(store, job)

    assert result["outcome"] == "REMOTE_WAIT_EXPORT_RECOVERY_COMPLETED"
    assert result["expected"] == 3
    assert result["validated"] == 3
    assert result["remote_calls"] == 1
    assert result["add_calls"] == 0
    assert result["task_number"] == TASK_NUMBER
    assert len(seed.runner_calls) == 1
    assert seed.prepared_holder["prepared"].result_path.is_file()
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert [crawl["state"] for crawl in crawls] == ["SUCCESS", "SUCCESS", "SUCCESS"]
    assert store.get_remote_job(int(job["id"]))["export_path"] == str(
        seed.prepared_holder["prepared"].result_path
    )


def test_wait_export_recovery_rejects_wrong_remote_job_id(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="does not exist in this run"):
        seed.recover(store, job, remote_job_id=int(job["id"]) + 100)
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


def test_wait_export_recovery_rejects_wrong_remote_identity(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="identity does not exactly match"):
        seed.recover(store, job, remote_identity="lxwl|999|RW-OTHER")
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
    assert seed.runner_calls == []


def test_wait_export_recovery_rejects_wrong_expected_error_text(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="exact legacy wait-export manual diagnostic"):
        seed.recover(store, job, expected_error_text="some unrelated manual error")
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
    assert seed.runner_calls == []


def test_wait_export_recovery_rejects_other_manual_errors(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        manual_error="harvest limit reached",
    )

    with pytest.raises(RecoveryRejected, match="manual error is not the wait-export diagnostic"):
        seed.recover(store, job)
    assert store.get_remote_job(int(job["id"]))["error_text"] == "harvest limit reached"
    assert seed.runner_calls == []


def test_wait_export_recovery_rejects_non_manual_state(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        manual_error=None,
    )

    with pytest.raises(RecoveryRejected, match="is not MANUAL_REQUIRED: SUBMITTED"):
        seed.recover(store, job)
    assert store.get_remote_job(int(job["id"]))["state"] == "SUBMITTED"
    assert seed.runner_calls == []


def test_wait_export_recovery_rejects_unfrozen_run(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        freeze=False,
    )

    with pytest.raises(RecoveryRejected, match="frozen daily run"):
        seed.recover(store, job)
    assert seed.runner_calls == []


def test_wait_export_recovery_rejects_payload_hash_mismatch(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)
    lease = store.acquire_lease(BUSINESS_DATE, "tamper", 60)
    connection = store._transaction(immediate=True)
    with connection as cur:
        cur.execute(
            "UPDATE remote_jobs SET payload_hash=? WHERE id=?",
            ("0" * 64, int(job["id"])),
        )
    store.release_lease(lease)

    with pytest.raises(RecoveryRejected, match="evidence is inconsistent"):
        seed.recover(store, job)
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
    assert seed.runner_calls == []


def test_wait_export_recovery_rejects_crawl_outside_payload(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)
    lease = store.acquire_lease(BUSINESS_DATE, "extra-crawl", 60)
    extra = store.ensure_crawl_work(
        lease,
        "lxwl",
        "sku-foreign",
        "https://shop.example/foreign",
        "legacy-v1",
        "https://shop.example/foreign",
        source_id="v-0",
    )
    store.link_crawl_work_to_remote_job(lease, int(extra["id"]), int(job["id"]))
    store.release_lease(lease)

    with pytest.raises(RecoveryRejected, match="outside the immutable payload"):
        seed.recover(store, job)
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
    assert seed.runner_calls == []


def test_wait_export_recovery_rejects_payload_identity_missing_from_crawls(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        crawl_source_specs={
            ("sku-0", "https://shop.example/0"): ["v-0"],
            ("sku-1", "https://shop.example/1"): ["v-1"],
        },
    )

    with pytest.raises(RecoveryRejected, match="missing from linked crawl work"):
        seed.recover(store, job)
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
    assert seed.runner_calls == []


def test_wait_export_recovery_supports_source_fanout(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(
        seed,
        [["code-0", "sku-0", "https://shop.example/0", 5.5]],
    )
    requests = (
        CrawlRequest(
            mall_key="lxwl",
            mall_source_id="34",
            mall_name="领先未来商城",
            sku_id="sku-0",
            url="https://shop.example/0",
            backend_version="legacy-v1",
        ),
    )
    rows = [_row(0, sku="sku-0", url="https://shop.example/0"), _row(9, sku="sku-0", url="https://shop.example/0")]
    store, job, _snapshot = seed.seed(
        rows,
        requests,
        crawl_source_specs={("sku-0", "https://shop.example/0"): ["v-0", "v-9"]},
    )

    result = seed.recover(store, job)

    assert result["outcome"] == "REMOTE_WAIT_EXPORT_RECOVERY_COMPLETED"
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert len(crawls) == 2
    assert [crawl["state"] for crawl in crawls] == ["SUCCESS", "SUCCESS"]
    assert result["add_calls"] == 0


def test_wait_export_recovery_keeps_job_recoverable_when_remote_still_running(tmp_path):
    seed = _Seed(tmp_path)

    def still_running(args, timeout_seconds, _environment):
        assert "--wait-export-only" in args
        assert "--create-only" not in args
        raise TimeoutError("legacy wait-export-only is still running")

    seed.runner = still_running
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    result = seed.recover(store, job)

    assert result["outcome"] == "REMOTE_WAIT_EXPORT_RECOVERY_RUNNING"
    assert result["add_calls"] == 0
    assert store.get_remote_job(int(job["id"]))["state"] == "RUNNING"
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert all(crawl["state"] == "RUNNING" for crawl in crawls)
    assert not seed.prepared_holder["prepared"].result_path.exists()


def test_wait_export_recovery_marks_manual_on_export_identity_mismatch(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(
        seed,
        [
            ["code-0", "sku-0", "https://shop.example/0", 1.1],
            ["code-1", "sku-1", "https://shop.example/1", 2.2],
            ["code-2", "sku-2", "https://shop.example/2", 3.3],
        ],
        event_task_number="RW-FOREIGN",
    )
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    result = seed.recover(store, job)

    assert result["outcome"] == "REMOTE_WAIT_EXPORT_RECOVERY_MANUAL"
    assert result["error"] == "exact EXPORTED evidence does not match durable remote identity"
    assert result["add_calls"] == 0
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert all(crawl["state"] != "SUCCESS" for crawl in crawls)


def test_wait_export_recovery_rejects_duplicate_execution_after_completion(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(
        seed,
        [["code-0", "sku-0", "https://shop.example/0", 12.3]],
    )
    requests = (seed.build_requests(1)[0],)
    store, job, _snapshot = seed.seed([_row(0)], requests)
    first = seed.recover(store, job)
    assert first["outcome"] == "REMOTE_WAIT_EXPORT_RECOVERY_COMPLETED"

    with pytest.raises(RecoveryRejected, match="is not MANUAL_REQUIRED: COMPLETED"):
        seed.recover(store, job)
    assert len(seed.runner_calls) == 1
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"


def test_wait_export_recovery_rejects_existing_local_result_workbook(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(1)
    store, job, _snapshot = seed.seed([_row(0)], requests)
    _write_result(
        seed.run_dir / "result.xlsx",
        [["code-0", "sku-0", "https://shop.example/0", 12.3]],
        task_name="task-target",
        mall_key="lxwl",
    )

    with pytest.raises(RecoveryRejected, match="must not overwrite an existing result workbook"):
        seed.recover(store, job)
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


def test_wait_export_recovery_requires_accepted_priority_evidence(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(3)
    # Seed without PRIORITY_UPDATED evidence by appending the events manually.
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        manual_error=None,
    )
    lease = store.acquire_lease(BUSINESS_DATE, "manualize", 60)
    store.mark_remote_manual(lease, int(job["id"]), WAIT_EXPORT_MANUAL_DIAGNOSTIC)
    store.release_lease(lease)

    with pytest.raises(RecoveryRejected, match="exact accepted priority evidence is required"):
        seed.recover(store, job)
    assert seed.runner_calls == []
