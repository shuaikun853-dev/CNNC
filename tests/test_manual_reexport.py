from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.dashboard_manual_ops import (
    REEXPORT_MANUAL_REQUEUE_DIAGNOSTICS,
    read_operator_actions,
    record_operator_action,
    run_manual_reexport,
    run_manual_reexport_command,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
)
from app.services.recovery.wait_export import (
    WAIT_EXPORT_MANUAL_DIAGNOSTIC,
    WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC,
)


BUSINESS_DATE = "2026-08-28"
TASK_ID = "1246803"
TASK_NUMBER = "RW-20260828160830"
IDENTITY = f"lxwl|{TASK_ID}|{TASK_NUMBER}"


def _row(index: int) -> dict[str, object]:
    return {
        "vCommodityId": f"v-{index}",
        "skuId": f"sku-{index}",
        "queryStr": f"query-{index}",
        "skuName": f"name-{index}",
        "extLink": f"https://shop.example/{index}",
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
    """Same durable fixture shape as the wait-export recovery tests."""

    def __init__(self, tmp_path: Path):
        self.run_dir = tmp_path / "runtime" / BUSINESS_DATE
        self.state_path = self.run_dir / "state.sqlite3"
        self.lock_path = self.run_dir / "incremental_price_audit.lock"
        self.batch_state = self.run_dir / "BATCH_STATE.md"
        self.batch_state.parent.mkdir(parents=True)
        self.batch_state.write_text("# state\n", encoding="utf-8")
        self.runner_calls: list[list[str]] = []
        self.probe_calls: list[tuple[str, str]] = []
        self.remote_progress: dict[str, int] | None = {}
        self.prepared_holder: dict[str, object] = {}
        self.adapter = CnncLegacyZhaoshangAdapter(
            mappings=[],
            db_env_path=None,
            runner=self._record_runner,
            progress_probe=self._progress_probe,
        )

    def _record_runner(self, args, timeout_seconds, _environment):
        self.runner_calls.append(list(args))
        return self.runner(args, timeout_seconds, _environment)

    def runner(self, args, timeout_seconds, _environment):  # overridden per test
        raise AssertionError("runner must be provided by the test")

    def _progress_probe(self, _job, task_id, task_number, _timeout_seconds):
        self.probe_calls.append((str(task_id), str(task_number)))
        return {
            "task_id": str(task_id),
            "task_number": str(task_number),
            "progress": dict(self.remote_progress) if self.remote_progress else None,
            "rsp_desc": "",
            "remote_calls": 2,
        }

    def terminal_progress(self, count: int) -> dict[str, int]:
        return {
            "total": count,
            "pending": 0,
            "running": 0,
            "failed": 0,
            "waiting_db": 0,
            "finished": count,
        }

    def active_progress(self, count: int) -> dict[str, int]:
        return {
            "total": count,
            "pending": max(1, count - 1),
            "running": 0,
            "failed": 0,
            "waiting_db": 0,
            "finished": 1 if count > 1 else 0,
        }

    def build_requests(self, count: int = 18):
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
        manual_error: str | None = None,
        freeze: bool = True,
        snapshot: bool = True,
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
        snapshot_id = None
        if snapshot:
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
        for index, request in enumerate(requests):
            crawl = store.ensure_crawl_work(
                lease,
                "lxwl",
                request.sku_id,
                request.url,
                "legacy-v1",
                request.url,
                source_id=f"v-{index}",
            )
            store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
        if manual_error is not None:
            store.mark_remote_manual(lease, int(job["id"]), manual_error)
        store.release_lease(lease)
        return store, job, snapshot_id

    def reexport(self, job, **overrides):
        kwargs = dict(
            state_path=self.state_path,
            lock_path=self.lock_path,
            business_date=BUSINESS_DATE,
            remote_job_id=int(job["id"]),
            remote_identity=IDENTITY,
            adapter=self.adapter,
            timeout_seconds=60.0,
        )
        kwargs.update(overrides)
        return run_manual_reexport(**kwargs)


def _priced_rows(count: int):
    return [
        [f"code-{index}", f"sku-{index}", f"https://shop.example/{index}", 12.3]
        for index in range(count)
    ]


def _exporting_runner(seed: _Seed, rows):
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
            "task_number": TASK_NUMBER,
            "output_path": str(prepared.result_path),
        }
        with seed.batch_state.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event)}`\n")

        class Result:
            returncode = 0

        return Result()

    return runner


def _still_running_runner(seed: _Seed):
    def runner(args, timeout_seconds, _environment):
        assert "--wait-export-only" in args
        assert "--create-only" not in args

        class Result:
            returncode = 0

        return Result()

    return runner


def test_manual_reexport_completes_terminal_job_without_any_create_call(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    seed.runner = _exporting_runner(seed, _priced_rows(18))
    requests = seed.build_requests(18)
    store, job, _snapshot = seed.seed([_row(index) for index in range(18)], requests)

    result = seed.reexport(job)

    assert result["outcome"] == "REEXPORT_COMPLETED"
    assert result["result_count"] == 18
    assert result["validated"] == 18
    assert result["create_calls"] == 0
    assert result["progress_classification"] == "REMOTE_TERMINAL"
    assert len(seed.runner_calls) == 1
    assert "--wait-export-only" in seed.runner_calls[0]
    assert not any("--create-only" in args for args in seed.runner_calls)
    assert not any("/add" in " ".join(args) for args in seed.runner_calls)
    assert seed.probe_calls == [(TASK_ID, TASK_NUMBER)]
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert len(crawls) == 18
    assert all(crawl["state"] == "SUCCESS" for crawl in crawls)


def test_manual_reexport_still_running_then_completes_on_second_click(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    seed.runner = _still_running_runner(seed)
    requests = seed.build_requests(18)
    store, job, _snapshot = seed.seed([_row(index) for index in range(18)], requests)

    first = seed.reexport(job)

    assert first["outcome"] == "REEXPORT_STILL_RUNNING"
    assert first["still_active"] is True
    assert first["progress_classification"] == "REMOTE_ACTIVE"
    assert first["create_calls"] == 0
    assert store.get_remote_job(int(job["id"]))["state"] == "RUNNING"
    assert int(store.get_remote_job(int(job["id"]))["harvest_attempts"]) == 1

    seed.remote_progress = seed.terminal_progress(18)
    seed.runner = _exporting_runner(seed, _priced_rows(18))
    second = seed.reexport(job)

    assert second["outcome"] == "REEXPORT_COMPLETED"
    assert second["result_count"] == 18
    assert int(store.get_remote_job(int(job["id"]))["harvest_attempts"]) == 2
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert all(crawl["state"] == "SUCCESS" for crawl in crawls)


def test_manual_reexport_completed_no_result_when_export_has_zero_rows(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    seed.runner = _exporting_runner(seed, [])
    requests = seed.build_requests(18)
    store, job, _snapshot = seed.seed([_row(index) for index in range(18)], requests)

    result = seed.reexport(job)

    assert result["outcome"] == "REEXPORT_COMPLETED_NO_RESULT"
    assert result["result_count"] == 0
    assert result["validated"] == 0
    assert result["no_result"] == 18
    assert result["create_calls"] == 0
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert all(crawl["state"] == "NO_RESULT" for crawl in crawls)


def test_manual_reexport_unknown_progress_rejects_without_durable_writes(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, _priced_rows(18))
    requests = seed.build_requests(18)
    store, job, _snapshot = seed.seed([_row(index) for index in range(18)], requests)
    before = store.get_remote_job(int(job["id"]))
    crawls_before = store.crawl_work_for_job(int(job["id"]))

    result = seed.reexport(job, progress_lookup=lambda _job: "REMOTE_UNKNOWN")

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "REMOTE_PROGRESS_UNKNOWN"
    assert result["create_calls"] == 0
    assert result["progress_classification"] == "REMOTE_UNKNOWN"
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"])) == before
    assert store.crawl_work_for_job(int(job["id"])) == crawls_before


def test_manual_reexport_default_probe_progress_none_is_unknown(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = None
    seed.runner = _exporting_runner(seed, _priced_rows(18))
    requests = seed.build_requests(18)
    store, job, _snapshot = seed.seed([_row(index) for index in range(18)], requests)
    before = store.get_remote_job(int(job["id"]))

    result = seed.reexport(job)

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "REMOTE_PROGRESS_UNKNOWN"
    assert seed.probe_calls == [(TASK_ID, TASK_NUMBER)]
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"])) == before


def test_manual_reexport_rejects_identity_mismatch_without_writes(tmp_path):
    seed = _Seed(tmp_path)
    seed.runner = _exporting_runner(seed, _priced_rows(18))
    requests = seed.build_requests(18)
    store, job, _snapshot = seed.seed([_row(index) for index in range(18)], requests)
    before = store.get_remote_job(int(job["id"]))

    result = seed.reexport(job, remote_identity="lxwl|999|RW-FOREIGN")

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "IDENTITY_MISMATCH"
    assert seed.runner_calls == []
    assert seed.probe_calls == []
    assert store.get_remote_job(int(job["id"])) == before


def test_manual_reexport_rejects_tampered_logical_key_evidence(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    seed.runner = _exporting_runner(seed, _priced_rows(18))
    requests = seed.build_requests(18)
    store, job, _snapshot = seed.seed([_row(index) for index in range(18)], requests)
    lease = store.acquire_lease(BUSINESS_DATE, "tamper", 60)
    connection = store._transaction(immediate=True)
    with connection as cursor:
        cursor.execute(
            "UPDATE remote_jobs SET logical_key=? WHERE id=?",
            ("0" * 64, int(job["id"])),
        )
    store.release_lease(lease)

    result = seed.reexport(job)

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "INVALID_REMOTE_STATE"
    assert "evidence is inconsistent" in result["reason"]
    assert seed.runner_calls == []


def test_manual_reexport_requeues_manual_wait_export_still_running_diagnostic(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    seed.runner = _exporting_runner(seed, _priced_rows(18))
    requests = seed.build_requests(18)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(18)],
        requests,
        manual_error=WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC,
    )

    result = seed.reexport(job)

    assert result["outcome"] == "REEXPORT_COMPLETED"
    assert result["result_count"] == 18
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"
    assert store.get_remote_job(int(job["id"]))["error_text"] is None


def test_manual_reexport_rejects_manual_job_with_unknown_diagnostic(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    seed.runner = _exporting_runner(seed, _priced_rows(18))
    requests = seed.build_requests(18)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(18)],
        requests,
        manual_error="some unrelated manual error",
    )

    result = seed.reexport(job)

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "INVALID_REMOTE_STATE"
    assert "not harvestable: MANUAL_REQUIRED" in result["reason"]
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"
    assert store.get_remote_job(int(job["id"]))["error_text"] == "some unrelated manual error"


def test_manual_reexport_requeues_manual_no_export_diagnostic(tmp_path):
    from app.services.recovery.no_result import WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC

    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    seed.runner = _exporting_runner(seed, _priced_rows(18))
    requests = seed.build_requests(18)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(18)],
        requests,
        manual_error=WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC,
    )
    assert WAIT_EXPORT_NO_EXPORT_DIAGNOSTIC in REEXPORT_MANUAL_REQUEUE_DIAGNOSTICS
    assert WAIT_EXPORT_MANUAL_DIAGNOSTIC in REEXPORT_MANUAL_REQUEUE_DIAGNOSTICS

    result = seed.reexport(job)

    assert result["outcome"] == "REEXPORT_COMPLETED"
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"


def test_manual_reexport_uses_latest_snapshot_before_freeze(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = _exporting_runner(seed, _priced_rows(3))
    requests = seed.build_requests(3)
    store, job, snapshot_id = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        freeze=False,
    )
    assert snapshot_id is not None
    assert store.get_run(BUSINESS_DATE)["frozen_snapshot_id"] is None

    result = seed.reexport(job)

    assert result["outcome"] == "REEXPORT_COMPLETED"
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"


def test_manual_reexport_rejects_run_without_any_snapshot(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = _exporting_runner(seed, _priced_rows(3))
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed(
        [_row(index) for index in range(3)],
        requests,
        snapshot=False,
    )

    result = seed.reexport(job)

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "NO_SNAPSHOT_SCOPE"
    assert "authoritative snapshot scope" in result["reason"]
    assert seed.runner_calls == []


def test_manual_reexport_reports_lock_conflict(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = _exporting_runner(seed, _priced_rows(3))
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    holder = ProcessLock(seed.lock_path)
    holder.acquire()
    try:
        result = seed.reexport(job)
    finally:
        holder.release()

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "LOCK_CONFLICT"
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "SUBMITTED"


def test_manual_reexport_repeated_clicks_never_cap_flip_to_manual(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.active_progress(3)
    seed.runner = _still_running_runner(seed)
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    for expected_attempts in (1, 2, 3):
        result = seed.reexport(job)
        assert result["outcome"] == "REEXPORT_STILL_RUNNING"
        row = store.get_remote_job(int(job["id"]))
        assert int(row["harvest_attempts"]) == expected_attempts
        assert row["state"] == "RUNNING"

    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = _exporting_runner(seed, _priced_rows(3))
    final = seed.reexport(job)

    assert final["outcome"] == "REEXPORT_COMPLETED"
    assert int(store.get_remote_job(int(job["id"]))["harvest_attempts"]) == 4
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"


def test_manual_reexport_after_completion_rejects_cleanly(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = _exporting_runner(seed, _priced_rows(3))
    requests = seed.build_requests(3)
    store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)
    first = seed.reexport(job)
    assert first["outcome"] == "REEXPORT_COMPLETED"

    second = seed.reexport(job)

    assert second["outcome"] == "REEXPORT_REJECTED"
    assert second["error_code"] == "INVALID_REMOTE_STATE"
    assert "not harvestable: COMPLETED" in second["reason"]
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"


def test_manual_reexport_requires_state_database(tmp_path):
    seed = _Seed(tmp_path)
    with pytest.raises(Exception, match="state database does not exist"):
        run_manual_reexport(
            state_path=tmp_path / "missing.sqlite3",
            lock_path=seed.lock_path,
            business_date=BUSINESS_DATE,
            remote_job_id=1,
            remote_identity=IDENTITY,
            adapter=seed.adapter,
        )


def test_operator_action_jsonl_roundtrip(tmp_path):
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    path = record_operator_action(
        run_dir,
        action_type="manual_reexport",
        action_id="a1",
        payload={"outcome": "REEXPORT_COMPLETED", "remote_calls": 1},
        now=datetime(2026, 8, 28, tzinfo=UTC),
    )
    assert path.name == "dashboard_actions.jsonl"
    record_operator_action(
        run_dir,
        action_type="manual_reexport",
        action_id="a2",
        payload={"outcome": "REEXPORT_STILL_RUNNING"},
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write("not-json\n")

    rows = read_operator_actions(run_dir)
    assert [row["action_id"] for row in rows] == ["a1", "a2"]
    assert rows[0]["recorded_at"].startswith("2026-08-28")
    assert rows[0]["outcome"] == "REEXPORT_COMPLETED"
    assert rows[1]["outcome"] == "REEXPORT_STILL_RUNNING"
    assert read_operator_actions(run_dir, limit=1) == [rows[1]]
    assert read_operator_actions(tmp_path / "empty") == []


def test_manual_reexport_command_records_operator_action(tmp_path):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = _exporting_runner(seed, _priced_rows(3))
    requests = seed.build_requests(3)
    _store, job, _snapshot = seed.seed([_row(index) for index in range(3)], requests)

    result = run_manual_reexport_command(
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path / "runtime",
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        timeout_seconds=60.0,
        adapter=seed.adapter,
    )

    assert result["outcome"] == "REEXPORT_COMPLETED"
    assert result["business_date"] == BUSINESS_DATE
    actions = read_operator_actions(seed.run_dir)
    assert len(actions) == 1
    assert actions[0]["action_type"] == "manual_reexport"
    assert actions[0]["outcome"] == "REEXPORT_COMPLETED"
    assert actions[0]["remote_job_id"] == int(job["id"])
    assert actions[0]["remote_identity"] == IDENTITY
