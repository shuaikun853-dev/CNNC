"""Transient wait-export failures must self-heal on the exact remote identity.

wait-export already owns an exact remote task identity, so a transient
process failure (startup OSError, one non-zero exit) must keep the job
RUNNING and retry only wait/status/export against the same identity — never
create, /add, ENABLE or a fresh priority of an unknown task.  Deterministic
failures (missing external tool, missing required file, invalid existing
workbook) stay MANUAL immediately.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_runtime import legacy_zhaoshang_bridge
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    HarvestStatus,
    LegacyPreSendFailure,
)
from app.workflows.incremental_price_audit import TickConfig, TickDependencies, run_incremental_tick

from tests.test_legacy_zhaoshang_adapter import (
    _adapter,
    _prepare,
    _write_result,
)

TASK_ID = "1246803"
TASK_NUMBER = "RW-20260819160830"
IDENTITY = f"lxwl|{TASK_ID}|{TASK_NUMBER}"
BUSINESS_DATE = "2026-08-19"


# ---------------------------------------------------------------- adapter level


def test_wait_export_oserror_first_then_export_success(tmp_path):
    job = _prepare(tmp_path)
    calls = []

    def runner(args, _timeout, _environment):
        calls.append(list(args))
        if len(calls) == 1:
            raise OSError("transient pipe failure")
        _write_result(job.result_path, [["sku-1", "https://item.jd.com/a", "12.5"]])
        return subprocess.CompletedProcess(args, returncode=0)

    first = _adapter(runner=runner).harvest(job, timeout_seconds=15)
    second = _adapter(runner=runner).harvest(job, timeout_seconds=15)

    assert first.status is HarvestStatus.RUNNING
    assert second.status is HarvestStatus.EXPORTED
    assert second.result_map["https://item.jd.com/a"]["page_price_value"] == 12.5
    assert all("--wait-export-only" in argv and "--create-only" not in argv for argv in calls)
    assert len(calls) == 2


def test_wait_export_child_nonzero_first_then_export_success(tmp_path):
    job = _prepare(tmp_path)
    calls = []

    def runner(args, _timeout, _environment):
        calls.append(list(args))
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, returncode=1)
        _write_result(job.result_path, [["sku-1", "https://item.jd.com/a", "12.5"]])
        return subprocess.CompletedProcess(args, returncode=0)

    first = _adapter(runner=runner).harvest(job, timeout_seconds=15)
    second = _adapter(runner=runner).harvest(job, timeout_seconds=15)

    assert first.status is HarvestStatus.RUNNING
    assert second.status is HarvestStatus.EXPORTED
    assert all("--wait-export-only" in argv and "--create-only" not in argv for argv in calls)


def test_wait_export_missing_external_tool_stays_manual(tmp_path):
    job = _prepare(tmp_path)

    def runner(_args, _timeout, _environment):
        raise LegacyPreSendFailure("legacy Zhaoshang external tool is unavailable")

    outcome = _adapter(runner=runner).harvest(job, timeout_seconds=15)

    assert outcome.status is HarvestStatus.MANUAL


def test_wait_export_missing_required_file_stays_manual(tmp_path):
    job = _prepare(tmp_path)

    def runner(_args, _timeout, _environment):
        raise FileNotFoundError("db env file is gone")

    outcome = _adapter(runner=runner).harvest(job, timeout_seconds=15)

    assert outcome.status is HarvestStatus.MANUAL


def test_wait_export_invalid_existing_workbook_stays_manual(tmp_path):
    job = _prepare(tmp_path)

    def runner(args, _timeout, _environment):
        # The child fails AND leaves an invalid workbook behind: a fresh
        # re-run cannot fix a deterministic validation failure.
        _write_result(job.result_path, [["wrong-sku", "https://item.jd.com/a", "12.5"]])
        return subprocess.CompletedProcess(args, returncode=1)

    outcome = _adapter(runner=runner).harvest(job, timeout_seconds=15)

    assert outcome.status is HarvestStatus.MANUAL


def test_wait_export_zero_budget_stays_retryable(tmp_path):
    outcome = _adapter().harvest(_prepare(tmp_path), timeout_seconds=0)

    assert outcome.status is HarvestStatus.RUNNING


# ------------------------------------------------------------ workflow level


class _E2ESeed:
    def __init__(self, tmp_path: Path):
        self.run_dir = tmp_path / "runtime" / BUSINESS_DATE
        self.state_path = self.run_dir / "state.sqlite3"
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
        self.rows = [
            {
                "vCommodityId": f"v-{index}",
                "skuId": f"sku-{index}",
                "queryStr": f"query-{index}",
                "skuName": f"name-{index}",
                "extLink": f"https://shop.example/{index}",
                "__crawl_required": True,
            }
            for index in range(2)
        ]
        self.requests = tuple(
            CrawlRequest(
                mall_key="lxwl",
                mall_source_id="34",
                mall_name="领先未来商城",
                sku_id=f"sku-{index}",
                url=f"https://shop.example/{index}",
                backend_version="legacy-v1",
            )
            for index in range(2)
        )

    def _record_runner(self, args, timeout_seconds, environment):
        self.runner_calls.append(list(args))
        return self.runner(args, timeout_seconds, environment)

    def runner(self, args, timeout_seconds, _environment):  # overridden per test
        raise AssertionError("runner must be provided by the test")

    def seed_running_job(self):
        prepared = self.adapter.prepare_job(
            BUSINESS_DATE,
            self.requests,
            self.run_dir / "input.xlsx",
            self.run_dir / "result.xlsx",
            "task-target",
            self.batch_state,
        )
        self.prepared_holder["prepared"] = prepared
        for event in (
            {
                "action": "CREATED",
                "mall": "lxwl",
                "task_name": prepared.task_name,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
                "expected_subtask_count": len(self.requests),
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
                "affected_rows": len(self.requests),
            },
        ):
            with self.batch_state.open("a", encoding="utf-8") as stream:
                stream.write(f"- `{json.dumps(event, ensure_ascii=False)}`\n")

        store = DailyAuditStore(self.state_path)
        lease = store.acquire_lease(BUSINESS_DATE, "seed", 60)
        ingested = store.ingest_snapshot(lease, self.rows)
        store.freeze_snapshot(lease, int(ingested["snapshot"]["id"]))
        payload = {
            "requests": [request.__dict__ for request in self.requests],
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
            lease, int(job["id"]), remote_identity=IDENTITY, state="RUNNING"
        )
        for index, request in enumerate(self.requests):
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
        store.release_lease(lease)
        return store, job


def _exporting_runner(seed: _E2ESeed):
    def runner(args, _timeout, _environment):
        assert "--wait-export-only" in args
        assert "--create-only" not in args
        prepared = seed.prepared_holder["prepared"]
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["商品编码", "商品最小品类单元id", "原始链接", "商品价格", "执行编号", "批次号", "商城标识"])
        for index, request in enumerate(prepared.requests):
            sheet.append(
                [
                    request.sku_id,
                    "",
                    request.url,
                    "12.5",
                    f"{TASK_NUMBER}-exec-{index}",
                    prepared.task_name,
                    "lxwl",
                ]
            )
        workbook.save(prepared.result_path)
        event = {
            "action": "EXPORTED",
            "mall": "lxwl",
            "task_name": prepared.task_name,
            "task_id": TASK_ID,
            "task_number": TASK_NUMBER,
            "output_path": str(prepared.result_path),
        }
        with seed.batch_state.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event, ensure_ascii=False)}`\n")

        class Result:
            returncode = 0

        return Result()

    return runner


def _tick_with_bridge(store, tmp_path, seed, tick_index: int):
    _plan, _finalize, submit, harvest = legacy_zhaoshang_bridge(
        seed.adapter,
        paths=type(
            "Paths",
            (),
            {"run_dir": seed.run_dir},
        )(),
        business_date=BUSINESS_DATE,
    )
    deps = TickDependencies(
        discover=lambda _timeout: pytest.fail("frozen run must not discover"),
        detail=lambda _row, _timeout: {"customerPrice": "10"},
        model=lambda _row, _timeout: {"match_result": "SAME"},
        harvest=harvest,
        submit_crawl=lambda *_args: pytest.fail(
            "wait-export retry must never dispatch a new remote create"
        ),
    )
    config = TickConfig(
        business_date=BUSINESS_DATE,
        runtime_budget_seconds=60,
        heavy_work_reserve_seconds=5,
        lease_ttl_seconds=60,
    )
    return run_incremental_tick(
        store, ProcessLock(tmp_path / f"tick-{tick_index}.lock"), config, deps
    )


def test_transient_wait_export_failure_self_heals_across_ticks(tmp_path):
    seed = _E2ESeed(tmp_path)
    store, job = seed.seed_running_job()

    def flaky_runner(args, timeout_seconds, environment):
        if len(seed.runner_calls) == 1:
            raise OSError("transient startup failure")
        return _exporting_runner(seed)(args, timeout_seconds, environment)

    seed.runner = flaky_runner

    first = _tick_with_bridge(store, tmp_path, seed, 1)
    after_first = store.get_remote_job(int(job["id"]))
    inspect = store.acquire_lease(BUSINESS_DATE, "inspect-1", 30)
    try:
        intents_after_first = store.list_remote_jobs(inspect, states={"SUBMIT_INTENT"})
    finally:
        store.release_lease(inspect)
    assert after_first["state"] == "RUNNING"
    assert after_first["remote_identity"] == IDENTITY
    assert first.outcome != "MANUAL_REVIEW_REQUIRED"
    assert intents_after_first == []
    assert all("--create-only" not in argv for argv in seed.runner_calls)

    second = _tick_with_bridge(store, tmp_path, seed, 2)
    after_second = store.get_remote_job(int(job["id"]))

    assert second.outcome == "COMPLETED"
    assert after_second["state"] == "COMPLETED"
    assert after_second["remote_identity"] == IDENTITY
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert all(crawl["state"] == "SUCCESS" for crawl in crawls)
    assert all("--create-only" not in argv for argv in seed.runner_calls)
    assert len(seed.runner_calls) == 2
