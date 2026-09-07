"""Characterization of the shared existing-remote-job harvest seam.

Both public entries -- the recovery channel
(:func:`app.services.recovery.harvest.run_existing_remote_harvest`) and the
manual dashboard channel
(:func:`app.services.dashboard_manual_ops.run_manual_reexport`) -- end in the
same ``_existing_remote_harvest_core``.  These tests pin the shared invariants
(exact run linkage, exact identity, durable POST evidence, immutable payload,
evidence consistency, crawl fan-out/terminal gates, admissible states,
ProcessLock + lease) on one durable shape run through BOTH channels, plus the
deliberate channel-policy differences (EXPORTED admission, MANUAL requeue
whitelist, live-proof fallback, snapshot scope).  No production behavior is
changed here; every assertion pins what IS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.daily_audit_store import DailyAuditStore, LeaseUnavailable, ProcessLock
from app.services.dashboard_manual_ops import run_manual_reexport
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    REMOTE_NOT_MATERIALIZED_LEGACY_DIAGNOSTIC,
)
from app.services.recovery.common import RecoveryRejected
from app.services.recovery.harvest import run_existing_remote_harvest
from app.services.recovery.wait_export import WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC


BUSINESS_DATE = "2026-09-07"
TASK_ID = "1246907"
TASK_NUMBER = "RW-20260907091000"
IDENTITY = f"lxwl|{TASK_ID}|{TASK_NUMBER}"
FOREIGN_IDENTITY = "lxwl|999|RW-FOREIGN"
# Strict durable production shape of the priority-unconfirmed MANUAL family
# (see tests/test_live_proof_fallback.py and harvest.is_priority_unconfirmed_manual_family).
PRIORITY_PENDING_RESULT_JSON = '{"state": "PRIORITY_PENDING", "priority_evidence": null}'


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


def _priced_rows(count: int):
    return [
        [f"code-{index}", f"sku-{index}", f"https://shop.example/{index}", 12.3]
        for index in range(count)
    ]


class _Seed:
    """Durable fixture shared by both channels (mirrors test_manual_reexport)."""

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
        manual_error: str | None = None,
        freeze: bool = True,
        snapshot: bool = True,
        reconcile_state: str = "SUBMITTED",
        priority_updated: bool = True,
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
        events = [
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
        ]
        if priority_updated:
            events.append(
                {
                    "action": "PRIORITY_UPDATED",
                    "mall": "lxwl",
                    "task_name": prepared.task_name,
                    "task_id": TASK_ID,
                    "task_number": TASK_NUMBER,
                    "affected_rows": len(requests),
                }
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
            state=reconcile_state,
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

    def harvest(self, job, **overrides):
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
        return run_existing_remote_harvest(**kwargs)

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


def _invalid_export_runner(seed: _Seed, rows):
    """Write a provenance-invalid workbook (wrong task name) and fail.

    The child failed AND left a workbook that does not validate: deterministic
    evidence, so the adapter fails closed to HarvestStatus.MANUAL with the
    "did not produce an export" diagnostic instead of retrying.
    """

    def runner(args, timeout_seconds, _environment):
        assert "--wait-export-only" in args
        prepared = seed.prepared_holder["prepared"]
        _write_result(
            prepared.result_path,
            rows,
            task_name="wrong-task",
            mall_key=prepared.mall_key,
        )

        class Result:
            returncode = 1

        return Result()

    return runner


def _tamper(store: DailyAuditStore, statements):
    lease = store.acquire_lease(BUSINESS_DATE, "tamper", 60)
    connection = store._transaction(immediate=True)
    with connection as cursor:
        for sql, params in statements:
            cursor.execute(sql, params)
    store.release_lease(lease)


def _seeded(tmp_path, *, runner_kind="exporting", **seed_kwargs):
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(3)
    requests = seed.build_requests(3)
    rows = [_row(index) for index in range(3)]
    if runner_kind == "invalid":
        seed.runner = _invalid_export_runner(seed, _priced_rows(3))
    else:
        seed.runner = _exporting_runner(seed, _priced_rows(3))
    store, job, snapshot_id = seed.seed(rows, requests, **seed_kwargs)
    return seed, store, job, snapshot_id


# ---------------------------------------------------------------------------
# Shared-invariant parity: one durable shape, both channels, rejection with
# zero remote calls and zero durable mutation.
# ---------------------------------------------------------------------------


def test_seam_rejects_unknown_remote_job_id_identically(tmp_path):
    seed, store, job, _snapshot = _seeded(tmp_path)
    before = store.get_remote_job(int(job["id"]))
    crawls_before = store.crawl_work_for_job(int(job["id"]))

    with pytest.raises(RecoveryRejected, match="does not exist in this run"):
        seed.harvest(job, remote_job_id=999)
    manual = seed.reexport(job, remote_job_id=999)

    assert manual["outcome"] == "REEXPORT_REJECTED"
    assert manual["error_code"] == "IDENTITY_MISMATCH"
    assert seed.runner_calls == []
    assert seed.probe_calls == []
    assert store.get_remote_job(int(job["id"])) == before
    assert store.crawl_work_for_job(int(job["id"])) == crawls_before


def test_seam_rejects_foreign_remote_identity_identically(tmp_path):
    seed, store, job, _snapshot = _seeded(tmp_path)
    before = store.get_remote_job(int(job["id"]))
    crawls_before = store.crawl_work_for_job(int(job["id"]))

    with pytest.raises(RecoveryRejected, match="does not exactly match"):
        seed.harvest(job, remote_identity=FOREIGN_IDENTITY)
    manual = seed.reexport(job, remote_identity=FOREIGN_IDENTITY)

    assert manual["outcome"] == "REEXPORT_REJECTED"
    assert manual["error_code"] == "IDENTITY_MISMATCH"
    assert "does not exactly match" in manual["reason"]
    assert seed.runner_calls == []
    assert seed.probe_calls == []
    assert store.get_remote_job(int(job["id"])) == before
    assert store.crawl_work_for_job(int(job["id"])) == crawls_before


def test_seam_rejects_missing_post_evidence_identically(tmp_path):
    seed, store, job, _snapshot = _seeded(tmp_path)
    _tamper(
        store,
        [("UPDATE remote_jobs SET post_attempted=0 WHERE id=?", (int(job["id"]),))],
    )
    before = store.get_remote_job(int(job["id"]))
    crawls_before = store.crawl_work_for_job(int(job["id"]))

    with pytest.raises(RecoveryRejected, match="requires durable POST evidence"):
        seed.harvest(job)
    manual = seed.reexport(job)

    assert manual["outcome"] == "REEXPORT_REJECTED"
    assert manual["error_code"] == "INVALID_REMOTE_STATE"
    assert "requires durable POST evidence" in manual["reason"]
    assert "manual re-export" in manual["reason"]
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"])) == before
    assert store.crawl_work_for_job(int(job["id"])) == crawls_before


def test_seam_rejects_unreconstructable_payload_identically(tmp_path):
    seed, store, job, _snapshot = _seeded(tmp_path)
    _tamper(
        store,
        [("UPDATE remote_jobs SET payload_json='{\"requests\":' WHERE id=?", (int(job["id"]),))],
    )
    before = store.get_remote_job(int(job["id"]))
    crawls_before = store.crawl_work_for_job(int(job["id"]))

    with pytest.raises(RecoveryRejected, match="cannot be reconstructed"):
        seed.harvest(job)
    manual = seed.reexport(job)

    # The manual preflight rebuilds the prepared job from the durable payload
    # before probing; an unreconstructable payload classifies as UNKNOWN and
    # the channel fails closed with REMOTE_PROGRESS_UNKNOWN (zero writes).
    assert manual["outcome"] == "REEXPORT_REJECTED"
    assert manual["error_code"] == "REMOTE_PROGRESS_UNKNOWN"
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"])) == before
    assert store.crawl_work_for_job(int(job["id"])) == crawls_before


def test_seam_rejects_inconsistent_evidence_identically(tmp_path):
    seed, store, job, _snapshot = _seeded(tmp_path)
    _tamper(
        store,
        [("UPDATE remote_jobs SET logical_key=? WHERE id=?", ("0" * 64, int(job["id"])))],
    )
    before = store.get_remote_job(int(job["id"]))
    crawls_before = store.crawl_work_for_job(int(job["id"]))

    with pytest.raises(RecoveryRejected, match="evidence is inconsistent"):
        seed.harvest(job)
    manual = seed.reexport(job)

    assert manual["outcome"] == "REEXPORT_REJECTED"
    assert manual["error_code"] == "INVALID_REMOTE_STATE"
    assert "evidence is inconsistent" in manual["reason"]
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"])) == before
    assert store.crawl_work_for_job(int(job["id"])) == crawls_before


def test_seam_rejects_crawl_outside_immutable_payload_identically(tmp_path):
    seed, store, job, _snapshot = _seeded(tmp_path)
    crawl = store.crawl_work_for_job(int(job["id"]))[0]
    _tamper(
        store,
        [(
            "UPDATE crawl_work SET exact_url=? WHERE id=?",
            ("https://foreign.example/999", int(crawl["id"])),
        )],
    )
    before = store.get_remote_job(int(job["id"]))
    crawls_before = store.crawl_work_for_job(int(job["id"]))

    with pytest.raises(RecoveryRejected, match="outside the immutable payload") as caught:
        seed.harvest(job)
    assert "existing harvest" in str(caught.value)
    manual = seed.reexport(job)

    assert manual["outcome"] == "REEXPORT_REJECTED"
    assert "outside the immutable payload" in manual["reason"]
    assert "manual re-export" in manual["reason"]
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"])) == before
    assert store.crawl_work_for_job(int(job["id"])) == crawls_before


def test_seam_rejects_terminal_crawl_overwrite_identically(tmp_path):
    seed, store, job, _snapshot = _seeded(tmp_path)
    crawl = store.crawl_work_for_job(int(job["id"]))[0]
    _tamper(
        store,
        [("UPDATE crawl_work SET state='NO_RESULT' WHERE id=?", (int(crawl["id"]),))],
    )
    before = store.get_remote_job(int(job["id"]))
    crawls_before = store.crawl_work_for_job(int(job["id"]))

    with pytest.raises(
        RecoveryRejected, match="cannot overwrite terminal crawl evidence"
    ) as caught:
        seed.harvest(job)
    assert "existing harvest" in str(caught.value)
    manual = seed.reexport(job)

    assert manual["outcome"] == "REEXPORT_REJECTED"
    assert "cannot overwrite terminal crawl evidence" in manual["reason"]
    assert "manual re-export" in manual["reason"]
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"])) == before
    assert store.crawl_work_for_job(int(job["id"])) == crawls_before


def test_seam_rejects_superseded_job_identically(tmp_path):
    seed, store, job, _snapshot = _seeded(tmp_path)
    _tamper(
        store,
        [("UPDATE remote_jobs SET state='SUPERSEDED' WHERE id=?", (int(job["id"]),))],
    )
    before = store.get_remote_job(int(job["id"]))
    crawls_before = store.crawl_work_for_job(int(job["id"]))

    with pytest.raises(RecoveryRejected, match="not harvestable: SUPERSEDED"):
        seed.harvest(job)
    manual = seed.reexport(job)

    assert manual["outcome"] == "REEXPORT_REJECTED"
    assert manual["error_code"] == "INVALID_REMOTE_STATE"
    assert "not harvestable: SUPERSEDED" in manual["reason"]
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"])) == before
    assert store.crawl_work_for_job(int(job["id"])) == crawls_before


def test_seam_rejects_completed_job_identically(tmp_path):
    seed, store, job, _snapshot = _seeded(tmp_path, reconcile_state="COMPLETED")
    before = store.get_remote_job(int(job["id"]))
    crawls_before = store.crawl_work_for_job(int(job["id"]))

    with pytest.raises(RecoveryRejected, match="not harvestable: COMPLETED"):
        seed.harvest(job)
    manual = seed.reexport(job)

    assert manual["outcome"] == "REEXPORT_REJECTED"
    assert manual["error_code"] == "INVALID_REMOTE_STATE"
    assert "not harvestable: COMPLETED" in manual["reason"]
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"])) == before
    assert store.crawl_work_for_job(int(job["id"])) == crawls_before


def test_seam_lock_conflict_is_raised_and_reported_per_channel(tmp_path):
    seed, store, job, _snapshot = _seeded(tmp_path)

    holder = ProcessLock(seed.lock_path)
    holder.acquire()
    try:
        with pytest.raises(LeaseUnavailable):
            seed.harvest(job)
        manual = seed.reexport(job)
    finally:
        holder.release()

    assert manual["outcome"] == "REEXPORT_REJECTED"
    assert manual["error_code"] == "LOCK_CONFLICT"
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "SUBMITTED"


def test_seam_snapshot_scope_difference_on_unfrozen_run(tmp_path):
    seed, store, job, snapshot_id = _seeded(tmp_path, freeze=False)
    assert snapshot_id is not None
    assert store.get_run(BUSINESS_DATE)["frozen_snapshot_id"] is None

    # Recovery requires the frozen daily run; the rejection is zero-mutation.
    with pytest.raises(RecoveryRejected, match="requires the frozen daily run"):
        seed.harvest(job)
    assert seed.runner_calls == []

    # The manual channel admits the unfrozen latest snapshot and completes.
    manual = seed.reexport(job)

    assert manual["outcome"] == "REEXPORT_COMPLETED"
    assert manual["result_count"] == 3
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"


def test_seam_invalid_workbook_fails_closed_to_manual_on_both_channels(tmp_path):
    manual_seed, manual_store, manual_job, _snapshot = _seeded(
        tmp_path / "manual", runner_kind="invalid"
    )

    manual = manual_seed.reexport(manual_job)

    assert manual["outcome"] == "REEXPORT_MANUAL"
    assert manual["create_calls"] == 0
    row = manual_store.get_remote_job(int(manual_job["id"]))
    assert row["state"] == "MANUAL_REQUIRED"
    assert "export" in str(row["error_text"])
    assert len(manual_seed.runner_calls) == 1

    recovery_seed, recovery_store, recovery_job, _snapshot = _seeded(
        tmp_path / "recovery", runner_kind="invalid"
    )

    recovery = recovery_seed.harvest(recovery_job)

    assert recovery["outcome"] == "REMOTE_EXISTING_HARVEST_MANUAL"
    assert recovery["add_calls"] == 0
    row = recovery_store.get_remote_job(int(recovery_job["id"]))
    assert row["state"] == "MANUAL_REQUIRED"
    assert "export" in str(row["error_text"])
    assert len(recovery_seed.runner_calls) == 1


def test_seam_happy_path_parity_on_fresh_identical_seeds(tmp_path):
    recovery_seed, recovery_store, recovery_job, _snapshot = _seeded(tmp_path / "recovery")

    recovery = recovery_seed.harvest(recovery_job)

    assert recovery["outcome"] == "REMOTE_EXISTING_HARVEST_COMPLETED"
    assert recovery["validated"] == 3
    assert recovery["remote_calls"] == 1
    assert recovery["add_calls"] == 0
    row = recovery_store.get_remote_job(int(recovery_job["id"]))
    assert int(row["harvest_attempts"]) == 1
    assert row["state"] == "COMPLETED"
    crawls = recovery_store.crawl_work_for_job(int(recovery_job["id"]))
    assert all(crawl["state"] == "SUCCESS" for crawl in crawls)
    assert len(recovery_seed.runner_calls) == 1
    assert "--wait-export-only" in recovery_seed.runner_calls[0]
    assert not any("--create-only" in args for args in recovery_seed.runner_calls)
    assert not any("/add" in " ".join(args) for args in recovery_seed.runner_calls)

    manual_seed, manual_store, manual_job, _snapshot = _seeded(tmp_path / "manual")

    manual = manual_seed.reexport(manual_job)

    assert manual["outcome"] == "REEXPORT_COMPLETED"
    assert manual["result_count"] == 3
    assert manual["create_calls"] == 0
    assert manual_store.get_remote_job(int(manual_job["id"]))["state"] == "COMPLETED"


# ---------------------------------------------------------------------------
# Channel-policy differences.
# ---------------------------------------------------------------------------


def test_seam_exported_state_admitted_only_by_manual_channel(tmp_path):
    manual_seed, manual_store, manual_job, _snapshot = _seeded(
        tmp_path / "manual", reconcile_state="EXPORTED"
    )

    manual = manual_seed.reexport(manual_job)

    assert manual["outcome"] == "REEXPORT_COMPLETED"
    assert manual["result_count"] == 3
    assert manual["create_calls"] == 0
    assert manual_store.get_remote_job(int(manual_job["id"]))["state"] == "COMPLETED"

    recovery_seed, _store, recovery_job, _snapshot = _seeded(
        tmp_path / "recovery", reconcile_state="EXPORTED"
    )

    with pytest.raises(RecoveryRejected, match="not harvestable: EXPORTED"):
        recovery_seed.harvest(recovery_job)
    assert recovery_seed.runner_calls == []


def test_seam_whitelisted_manual_requeue_applies_only_to_manual_channel(tmp_path):
    manual_seed, manual_store, manual_job, _snapshot = _seeded(
        tmp_path / "manual", manual_error=WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC
    )

    manual = manual_seed.reexport(manual_job)

    assert manual["outcome"] == "REEXPORT_COMPLETED"
    row = manual_store.get_remote_job(int(manual_job["id"]))
    assert row["state"] == "COMPLETED"
    assert row["error_text"] is None

    recovery_seed, _store, recovery_job, _snapshot = _seeded(
        tmp_path / "recovery", manual_error=WAIT_EXPORT_STILL_RUNNING_DIAGNOSTIC
    )

    with pytest.raises(RecoveryRejected, match="not harvestable: MANUAL_REQUIRED"):
        recovery_seed.harvest(recovery_job)
    assert recovery_seed.runner_calls == []


def test_seam_priority_unconfirmed_family_needs_the_manual_live_proof(tmp_path):
    def seed_family(base):
        seed, store, job, _snapshot = _seeded(base, priority_updated=False)
        _tamper(
            store,
            [
                ("UPDATE remote_jobs SET state='MANUAL_REQUIRED' WHERE id=?", (int(job["id"]),)),
                (
                    "UPDATE remote_jobs SET error_text=? WHERE id=?",
                    (REMOTE_NOT_MATERIALIZED_LEGACY_DIAGNOSTIC, int(job["id"])),
                ),
                (
                    "UPDATE remote_jobs SET result_json=? WHERE id=?",
                    (PRIORITY_PENDING_RESULT_JSON, int(job["id"])),
                ),
            ],
        )
        return seed, store, job

    # Recovery has no live-proof fallback: the strict family stays rejected.
    recovery_seed, _store, recovery_job = seed_family(tmp_path / "recovery")
    with pytest.raises(RecoveryRejected, match="not harvestable: MANUAL_REQUIRED"):
        recovery_seed.harvest(recovery_job)
    assert recovery_seed.runner_calls == []

    # The manual channel's proven terminal probe admits the family.
    manual_seed, manual_store, manual_job = seed_family(tmp_path / "manual")
    manual = manual_seed.reexport(manual_job)

    assert manual["outcome"] == "REEXPORT_COMPLETED"
    assert manual["result_count"] == 3
    assert manual["create_calls"] == 0
    assert manual_store.get_remote_job(int(manual_job["id"]))["state"] == "COMPLETED"
