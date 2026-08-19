from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_recovery import (
    EXPORT_NO_PRICE_CRAWL_REASON,
    RecoveryRejected,
    WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC,
    run_remote_export_no_price_resolution,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    ExportNoPriceProbe,
)


BUSINESS_DATE = "2026-08-19"
TASK_ID = "1246803"
TASK_NUMBER = "RW-20260819160830"
IDENTITY = f"lxwl|{TASK_ID}|{TASK_NUMBER}"
FINISHED_STATUS = "本次任务执行完毕"
NO_PRICE_RSP_DESC = "未检索到商品价格信息"


def _row(index: int, *, source: str | None = None, sku: str | None = None, url: str | None = None) -> dict[str, object]:
    return {
        "vCommodityId": source or f"v-{index}",
        "skuId": sku or f"sku-{index}",
        "queryStr": f"query-{index}",
        "skuName": f"name-{index}",
        "extLink": url or f"https://shop.example/{index}",
        "__crawl_required": True,
    }


def _finished_probe(*, count: int = 3, **overrides) -> ExportNoPriceProbe:
    values = dict(
        task_status_rsp_desc=FINISHED_STATUS,
        progress={
            "total": count,
            "pending": 0,
            "running": 0,
            "failed": 0,
            "waiting_db": 0,
            "finished": count,
        },
        export_rsp_desc=NO_PRICE_RSP_DESC,
        export_count=0,
        remote_calls=4,
    )
    values.update(overrides)
    if "progress" in overrides:
        progress = dict(values["progress"])
        values["progress"] = progress
    return ExportNoPriceProbe(**values)


class _Seed:
    def __init__(self, tmp_path: Path):
        self.run_dir = tmp_path / "runtime" / BUSINESS_DATE
        self.state_path = self.run_dir / "state.sqlite3"
        self.lock_path = self.run_dir / "incremental_price_audit.lock"
        self.batch_state = self.run_dir / "BATCH_STATE.md"
        self.batch_state.parent.mkdir(parents=True)
        self.batch_state.write_text("# state\n", encoding="utf-8")
        self.runner_calls: list[list[str]] = []
        self.probe_calls: list[tuple[str, str]] = []
        self.probe_result: ExportNoPriceProbe | None = None
        self.probe_error: BaseException | None = None
        self.prepared_holder: dict[str, object] = {}
        self.adapter = CnncLegacyZhaoshangAdapter(
            mappings=[],
            db_env_path=None,
            runner=self._record_runner,
            export_probe=self._record_probe,
        )

    def _record_runner(self, args, timeout_seconds, _environment):
        self.runner_calls.append(list(args))
        raise AssertionError("the export no-price resolution must not run the legacy subprocess")

    def _record_probe(self, job, task_id, task_number, timeout_seconds):
        self.probe_calls.append((str(task_id), str(task_number)))
        assert str(task_id) == TASK_ID
        assert str(task_number) == TASK_NUMBER
        if self.probe_error is not None:
            raise self.probe_error
        assert self.probe_result is not None
        return self.probe_result

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

    def seed(
        self,
        rows,
        requests,
        *,
        manual_error: str | None = WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC,
        freeze: bool = True,
        crawl_state: str = "RUNNING",
        crawl_source_specs=None,
    ):
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
        if crawl_state != "PENDING":
            store.advance_crawl_work_for_job(lease, int(job["id"]), crawl_state)
        if manual_error is not None:
            store.mark_remote_manual(lease, int(job["id"]), manual_error)
        store.release_lease(lease)
        return store, job, snapshot_id

    def seed_extra_no_result_crawls(self, store, count: int):
        lease = store.acquire_lease(BUSINESS_DATE, "extras", 60)
        for index in range(count):
            store.ensure_crawl_work(
                lease,
                "lxwl",
                f"extra-sku-{index}",
                f"https://shop.example/extra/{index}",
                "legacy-v1",
                f"https://shop.example/extra/{index}",
                source_id=f"extra-{index}",
            )
        store.release_lease(lease)
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                "UPDATE crawl_work SET state='NO_RESULT' WHERE sku_id LIKE 'extra-sku-%'"
            )
            connection.commit()
        finally:
            connection.close()

    def resolve(self, store, job, **overrides):
        kwargs = dict(
            state_path=self.state_path,
            lock_path=self.lock_path,
            business_date=BUSINESS_DATE,
            remote_job_id=int(job["id"]),
            remote_identity=IDENTITY,
            expected_error_text=WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC,
            adapter=self.adapter,
            timeout_seconds=60.0,
        )
        kwargs.update(overrides)
        return run_remote_export_no_price_resolution(**kwargs)


def test_export_no_price_resolution_completes_job54_shape(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe(count=3)
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    result = seed.resolve(store, job)

    assert result["outcome"] == "REMOTE_EXPORT_NO_PRICE_RESOLVED"
    assert result["add_calls"] == 0
    assert result["remote_calls"] == 4
    assert result["resolved"] == 3
    assert result["resolved_crawl_work_ids"] == sorted(
        int(crawl["id"]) for crawl in store.crawl_work_for_job(int(job["id"]))
    )
    assert result["no_result_before"] == 0
    assert result["no_result_after"] == 3
    assert result["no_result_limit"] == 10
    assert result["export_rsp_desc"] == NO_PRICE_RSP_DESC
    assert result["export_count"] == 0
    assert result["task_status_rsp_desc"] == FINISHED_STATUS
    assert seed.probe_calls == [(TASK_ID, TASK_NUMBER)]
    assert seed.runner_calls == []

    final_job = store.get_remote_job(int(job["id"]))
    assert final_job["state"] == "COMPLETED"
    assert final_job["error_text"] is None
    job_evidence = json.loads(str(final_job["result_json"]))
    assert job_evidence["evidence_type"] == "EXPORT_NO_PRICE_TERMINAL"
    assert job_evidence["export_rspDesc"] == NO_PRICE_RSP_DESC
    assert job_evidence["export_count"] == 0
    assert job_evidence["no_result_before"] == 0

    crawls = store.crawl_work_for_job(int(job["id"]))
    assert len(crawls) == 3
    for crawl in crawls:
        assert crawl["state"] == "NO_RESULT"
        evidence = json.loads(str(crawl["result_json"]))
        assert evidence["evidence_type"] == "EXPORT_NO_PRICE_TERMINAL"
        assert evidence["page_price_status"] == "NO_RESULT"
        assert evidence["page_price_value"] is None
        assert evidence["export_rspDesc"] == NO_PRICE_RSP_DESC
        assert evidence["export_count"] == 0
        assert crawl["error_text"] == EXPORT_NO_PRICE_CRAWL_REASON


def test_export_no_price_resolution_supports_source_fanout_shape(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe(count=3)
    requests = seed.build_requests(3)
    rows = [
        _row(0, source="702251207576932352", sku="sku-0", url="https://shop.example/0"),
        _row(9, source="702251571579604992", sku="sku-1", url="https://shop.example/1"),
        _row(8, source="702251571579604992", sku="sku-2", url="https://shop.example/2"),
    ]
    store, job, _snapshot = seed.seed(
        rows,
        requests,
        crawl_source_specs={
            ("sku-0", "https://shop.example/0"): ["702251207576932352"],
            ("sku-1", "https://shop.example/1"): ["702251571579604992"],
            ("sku-2", "https://shop.example/2"): ["702251571579604992"],
        },
    )

    result = seed.resolve(store, job)

    assert result["outcome"] == "REMOTE_EXPORT_NO_PRICE_RESOLVED"
    assert result["resolved"] == 3
    assert sorted(result["resolved_source_ids"]) == sorted(
        {"702251207576932352", "702251571579604992"}
    )


def test_export_no_price_resolution_rejects_task_still_running(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe(task_status_rsp_desc="任务执行中")
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="task status does not confirm"):
        seed.resolve(store, job)
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
    assert all(crawl["state"] == "RUNNING" for crawl in store.crawl_work_for_job(int(job["id"])))


def test_export_no_price_resolution_rejects_incomplete_progress(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe(
        progress={"total": 3, "pending": 1, "running": 0, "failed": 0, "waiting_db": 0, "finished": 2},
    )
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="progress is not fully completed"):
        seed.resolve(store, job)
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


def test_export_no_price_resolution_rejects_failed_progress(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe(
        progress={"total": 3, "pending": 0, "running": 0, "failed": 1, "waiting_db": 0, "finished": 2},
    )
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="progress is not fully completed"):
        seed.resolve(store, job)
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


def test_export_no_price_resolution_rejects_probe_transient_error(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_error = TimeoutError("export endpoint timeout")
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="probe did not confirm terminal evidence"):
        seed.resolve(store, job)
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
    assert all(crawl["state"] == "RUNNING" for crawl in store.crawl_work_for_job(int(job["id"])))


def test_export_no_price_resolution_rejects_positive_export_count(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe(export_count=3)
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="use the harvest path"):
        seed.resolve(store, job)
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


def test_export_no_price_resolution_rejects_wrong_export_rsp_desc(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe(export_rsp_desc="导出失败")
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="not the exact no-price semantic"):
        seed.resolve(store, job)
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


def test_export_no_price_resolution_rejects_existing_local_result_workbook(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)
    Path(str(store.get_remote_job(int(job["id"]))["result_path"])).write_bytes(b"PK-fake")

    with pytest.raises(RecoveryRejected, match="result workbook already exists"):
        seed.resolve(store, job)
    assert seed.probe_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


def test_export_no_price_resolution_rejects_identity_mismatch(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="remote identity does not exactly match"):
        seed.resolve(store, job, remote_identity="lxwl|999|RW-OTHER")
    assert seed.probe_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


def test_export_no_price_resolution_rejects_payload_hash_mismatch(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)
    lease = store.acquire_lease(BUSINESS_DATE, "tamper", 60)
    with store._transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE remote_jobs SET payload_hash=? WHERE id=?",
            ("0" * 64, int(job["id"])),
        )
    store.release_lease(lease)

    with pytest.raises(RecoveryRejected, match="durable legacy job evidence is inconsistent"):
        seed.resolve(store, job)
    assert seed.probe_calls == []


def test_export_no_price_resolution_rejects_crawl_outside_payload(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
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
        seed.resolve(store, job)
    assert seed.probe_calls == []


def test_export_no_price_resolution_rejects_source_outside_frozen_snapshot(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        crawl_source_specs={
            ("sku-0", "https://shop.example/0"): ["v-0"],
            ("sku-1", "https://shop.example/1"): ["v-1"],
            ("sku-2", "https://shop.example/2"): ["v-unknown"],
        },
    )

    with pytest.raises(RecoveryRejected, match="outside the frozen snapshot"):
        seed.resolve(store, job)
    assert seed.probe_calls == []


def test_export_no_price_resolution_rejects_when_threshold_exceeded(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)
    seed.seed_extra_no_result_crawls(store, 8)

    with pytest.raises(RecoveryRejected, match="no-result limit"):
        seed.resolve(store, job)
    assert seed.probe_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
    assert all(crawl["state"] == "RUNNING" for crawl in store.crawl_work_for_job(int(job["id"])))


def test_export_no_price_resolution_allows_threshold_exact_boundary(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)
    seed.seed_extra_no_result_crawls(store, 7)

    result = seed.resolve(store, job)

    assert result["outcome"] == "REMOTE_EXPORT_NO_PRICE_RESOLVED"
    assert result["no_result_before"] == 7
    assert result["no_result_after"] == 10
    assert result["no_result_limit"] == 10


def test_export_no_price_resolution_idempotent_rerun(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    first = seed.resolve(store, job)
    assert first["outcome"] == "REMOTE_EXPORT_NO_PRICE_RESOLVED"
    assert len(seed.probe_calls) == 1

    second = seed.resolve(store, job)

    assert second["outcome"] == "REMOTE_EXPORT_NO_PRICE_ALREADY_RESOLVED"
    assert second["resolved"] == 0
    assert second["remote_calls"] == 0
    assert second["add_calls"] == 0
    assert second["no_result_after"] == 3
    assert len(seed.probe_calls) == 1
    final_job = store.get_remote_job(int(job["id"]))
    assert final_job["state"] == "COMPLETED"


def test_export_no_price_resolution_rejects_wrong_expected_error_text(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    with pytest.raises(RecoveryRejected, match="exact legacy wait-export manual diagnostic"):
        seed.resolve(store, job, expected_error_text="some unrelated manual error")
    assert seed.probe_calls == []


def test_export_no_price_resolution_rejects_other_manual_errors(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        manual_error="harvest limit reached",
    )

    with pytest.raises(RecoveryRejected, match="not the wait-export diagnostic"):
        seed.resolve(store, job)
    assert seed.probe_calls == []
    assert store.get_remote_job(int(job["id"]))["error_text"] == "harvest limit reached"


def test_export_no_price_resolution_rejects_non_manual_state(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        manual_error=None,
    )

    with pytest.raises(RecoveryRejected, match="is not MANUAL_REQUIRED: SUBMITTED"):
        seed.resolve(store, job)
    assert seed.probe_calls == []


def test_export_no_price_resolution_rejects_unfrozen_run(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        freeze=False,
    )

    with pytest.raises(RecoveryRejected, match="requires the frozen daily run"):
        seed.resolve(store, job)
    assert seed.probe_calls == []


def test_export_no_price_resolution_rejects_terminal_crawl_evidence(tmp_path):
    seed = _Seed(tmp_path)
    seed.probe_result = _finished_probe()
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)
    lease = store.acquire_lease(BUSINESS_DATE, "terminalize", 60)
    crawls = store.crawl_work_for_job(int(job["id"]))
    store.complete_crawl_work(
        lease,
        int(crawls[0]["id"]),
        {"page_price_value": "9.5", "page_price_status": "OK", "page_price_source": "招商局"},
    )
    store.release_lease(lease)

    with pytest.raises(RecoveryRejected, match="cannot overwrite terminal crawl evidence"):
        seed.resolve(store, job)
    assert seed.probe_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
