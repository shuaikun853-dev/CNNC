"""2026-09-04 hotfix regression tests: live identity proof fallback.

Production incident: job 30 (zjmi|1323015|RW-20260904150200) sat in
MANUAL_REQUIRED with error_text=PriorityUpdateZeroRowsError and a null
priority_evidence while the remote task itself was intact (a manual web
export returned priced rows).  Both the re-export and the rebuild gate on
historical accepted priority evidence and had no live-admission channel, so
both failed closed with INVALID_REMOTE_STATE.

These tests pin the new narrow live-proof fallback: only the exact
priority-unconfirmed family, only a proven read-only probe of the exact
remote identity, zero create/prioritize/stop calls on the re-export path,
and the rebuild protocol unchanged once the gate is passed.  Everything runs
against a fake adapter (counting seams) and a temporary SQLite store; no
real remote and no production database is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.daily_audit_store import DailyAuditStore
from app.services.dashboard_manual_ops import (
    read_operator_actions,
    run_manual_rebuild,
    run_manual_reexport,
    run_manual_reexport_command,
)
from app.services.dashboard_projection import project_remote_jobs
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    RemoteTaskIdentityConflictError,
)

pytestmark = pytest.mark.critical_safety


BUSINESS_DATE = "2026-08-28"
MALL_KEY = "lxwl"
MALL_NAME = "领先未来商城"
TASK_ID = "1246803"
TASK_NUMBER = "RW-20260828160830"
IDENTITY = f"{MALL_KEY}|{TASK_ID}|{TASK_NUMBER}"
OLD_TASK_DIGITS = "17300000000001"
OLD_TASK_NAME = f"cnnc_current_opt_{MALL_NAME}_{OLD_TASK_DIGITS}"
NEW_TASK_ID = "8899001"
NEW_TASK_NUMBER = "RW-20260828REBUILD"
ZERO_ROWS_ERROR = "PriorityUpdateZeroRowsError"
# Review round 2: the strict durable production shape of the priority-
# unconfirmed family (job 30).  Anything looser (NULL/empty/malformed/partial
# result_json) stays outside the live-proof fallback.
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


def _priced_rows(count: int):
    return [
        [f"code-{index}", f"sku-{index}", f"https://shop.example/{index}", 12.3]
        for index in range(count)
    ]


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
    """Durable fixture shared by the re-export and rebuild fallback tests.

    Mirrors the seeds of tests/test_manual_reexport.py and
    tests/test_manual_rebuild.py plus the pieces the hotfix needs: an
    optional PRIORITY_UPDATED event (omitted to reproduce the
    priority-unconfirmed family), and counting/failing seams for prioritize,
    stop and create so every test proves zero unintended remote writes.
    """

    def __init__(self, tmp_path: Path, *, rebuild: bool = False):
        self.rebuild = rebuild
        self.run_dir = tmp_path / "runtime" / BUSINESS_DATE
        self.state_path = self.run_dir / "state.sqlite3"
        self.lock_path = self.run_dir / "incremental_price_audit.lock"
        self.batch_state = self.run_dir / "BATCH_STATE.md"
        self.batch_state.parent.mkdir(parents=True)
        self.batch_state.write_text("# state\n", encoding="utf-8")
        self.job_dir = self.run_dir / "zhaoshang" / MALL_KEY / OLD_TASK_DIGITS
        self.runner_calls: list[list[str]] = []
        self.probe_calls: list[tuple[str, str]] = []
        self.prioritize_calls: list[tuple[str, str]] = []
        self.stop_calls: list[dict[str, object]] = []
        self.remote_progress: dict[str, int] | None = {}
        self.probe_error: Exception | None = None
        self.prepared_holder: dict[str, object] = {}
        self.old_job_id: int | None = None
        self.adapter = CnncLegacyZhaoshangAdapter(
            mappings=[],
            db_env_path=None,
            runner=self._record_runner,
            progress_probe=self._progress_probe,
            priority_runner=self._priority_runner,
            stop_runner=self._stop_runner,
        )

    def _record_runner(self, args, timeout_seconds, _environment):
        self.runner_calls.append(list(args))
        return self.runner(args, timeout_seconds, _environment)

    def runner(self, args, timeout_seconds, _environment):
        raise AssertionError("runner must be provided by the test")

    def _progress_probe(self, _job, task_id, task_number, _timeout_seconds):
        self.probe_calls.append((str(task_id), str(task_number)))
        if self.probe_error is not None:
            raise self.probe_error
        return {
            "task_id": str(task_id),
            "task_number": str(task_number),
            "progress": dict(self.remote_progress) if self.remote_progress else None,
            "rsp_desc": "",
            "remote_calls": 2,
        }

    def _priority_runner(self, _job, task_id, task_number, _timeout_seconds):
        self.prioritize_calls.append((str(task_id), str(task_number)))
        raise AssertionError("no prioritize call may ever leave the re-export path")

    def _stop_runner(self, _job, task_id, _task_number, _timeout, *, resume_phase=None):
        old_state = None
        if self.old_job_id is not None:
            store = DailyAuditStore(self.state_path)
            row = store.get_remote_job(self.old_job_id)
            old_state = str(row["state"]) if row is not None else None
        self.stop_calls.append(
            {"task_id": str(task_id), "old_state_at_stop": old_state}
        )
        return self.stop_outcome()

    def stop_outcome(self):
        from app.services.legacy_zhaoshang_adapter import LegacyRestartOutcome, RestartStatus

        return LegacyRestartOutcome(
            RestartStatus.STOPPED,
            disable_confirmed=True,
            observed_enabled=False,
            diagnostic="exact task disabled with confirmed evidence",
        )

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
                mall_key=MALL_KEY,
                mall_source_id="34",
                mall_name=MALL_NAME,
                sku_id=f"sku-{index}",
                url=f"https://shop.example/{index}",
                backend_version="legacy-v1",
            )
            for index in range(count)
        )

    def seed(
        self,
        count: int,
        *,
        priority_updated: bool = True,
        job_state: str | None = None,
        error_text: str | None = None,
        result_json: str | None = None,
    ):
        """Seed one durable job.

        ``result_json`` pins an exact durable result shape; the default leaves
        result_json NULL (the accepted-evidence shape -- the strict
        priority-unconfirmed fixtures pass the exact
        PRIORITY_PENDING_RESULT_JSON production shape explicitly).
        """
        import sqlite3

        task_name = OLD_TASK_NAME if self.rebuild else "task-target"
        job_parent = self.job_dir if self.rebuild else self.run_dir
        requests = self.build_requests(count)
        prepared = self.adapter.prepare_job(
            BUSINESS_DATE,
            requests,
            job_parent / "input.xlsx",
            job_parent / "result.xlsx",
            task_name,
            self.batch_state,
        )
        self.prepared_holder["prepared"] = prepared
        events = [
            {
                "action": "CREATED",
                "mall": MALL_KEY,
                "task_name": task_name,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
                "expected_subtask_count": count,
            },
            {
                "action": "ENABLED",
                "mall": MALL_KEY,
                "task_name": task_name,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
            },
        ]
        if priority_updated:
            events.append(
                {
                    "action": "PRIORITY_UPDATED",
                    "mall": MALL_KEY,
                    "task_name": task_name,
                    "task_id": TASK_ID,
                    "task_number": TASK_NUMBER,
                    "affected_rows": count,
                }
            )
        with self.batch_state.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(f"- `{json.dumps(event)}`\n")

        store = DailyAuditStore(self.state_path)
        lease = store.acquire_lease(BUSINESS_DATE, "seed", 60)
        ingested = store.ingest_snapshot(
            lease, [_row(index) for index in range(count)]
        )
        store.freeze_snapshot(lease, int(ingested["snapshot"]["id"]))
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
                MALL_KEY,
                request.sku_id,
                request.url,
                "legacy-v1",
                request.url,
                source_id=f"v-{index}",
            )
            store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
        store.release_lease(lease)
        if error_text is not None or job_state is not None or result_json is not None:
            connection = sqlite3.connect(str(self.state_path), timeout=5)
            try:
                if error_text is not None:
                    connection.execute(
                        "UPDATE remote_jobs SET error_text=? WHERE id=?",
                        (error_text, int(job["id"])),
                    )
                if job_state is not None:
                    connection.execute(
                        "UPDATE remote_jobs SET state=? WHERE id=?",
                        (job_state, int(job["id"])),
                    )
                if result_json is not None:
                    connection.execute(
                        "UPDATE remote_jobs SET result_json=? WHERE id=?",
                        (result_json, int(job["id"])),
                    )
                connection.commit()
            finally:
                connection.close()
        self.old_job_id = int(job["id"])
        return store, job

    def exporting_runner(self, rows):
        seed = self

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
                "mall": MALL_KEY,
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

    def creating_runner(self):
        seed = self

        def runner(args, _timeout_seconds, _environment):
            # NB: _record_runner already appended the args; only assert here.
            assert "--create-only" in args
            assert "--wait-export-only" not in args
            task_name = args[args.index("--task-name") + 1]
            events = [
                {
                    "action": "CREATED",
                    "mall": MALL_KEY,
                    "task_name": task_name,
                    "task_id": NEW_TASK_ID,
                    "task_number": NEW_TASK_NUMBER,
                },
                {
                    "action": "ENABLED",
                    "mall": MALL_KEY,
                    "task_name": task_name,
                    "task_id": NEW_TASK_ID,
                    "task_number": NEW_TASK_NUMBER,
                },
                {
                    "action": "PRIORITY_UPDATED",
                    "mall": MALL_KEY,
                    "task_name": task_name,
                    "task_id": NEW_TASK_ID,
                    "task_number": NEW_TASK_NUMBER,
                    "affected_rows": 1,
                },
            ]
            with seed.batch_state.open("a", encoding="utf-8") as stream:
                for event in events:
                    stream.write(f"- `{json.dumps(event)}`\n")

            class Result:
                returncode = 0

            return Result()

        return runner


# ---------------------------------------------------------------------------
# REEXPORT: live proof fallback on the manual re-export path.
# ---------------------------------------------------------------------------


def _seeded_reexport(
    tmp_path,
    *,
    error_text,
    remote_progress,
    result_json: str | None = PRIORITY_PENDING_RESULT_JSON,
):
    seed = _Seed(tmp_path)
    seed.remote_progress = remote_progress
    requests = seed.build_requests(3)
    store, job = seed.seed(
        3,
        priority_updated=False,
        job_state="MANUAL_REQUIRED",
        error_text=error_text,
        result_json=result_json,
    )
    return seed, store, job


def test_reexport_1_live_proof_terminal_admits_priority_unconfirmed_manual(tmp_path):
    """REEXPORT-1: missing historical evidence + live TERMINAL -> export runs."""
    seed, store, job = _seeded_reexport(
        tmp_path, error_text=ZERO_ROWS_ERROR, remote_progress=None
    )
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = seed.exporting_runner(_priced_rows(3))
    prioritize_calls_before = len(seed.prioritize_calls)
    stop_calls_before = len(seed.stop_calls)

    result = run_manual_reexport(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REEXPORT_COMPLETED"
    assert "error_code" not in result
    assert result["result_count"] == 3
    assert result["create_calls"] == 0
    assert result["progress_classification"] == "REMOTE_TERMINAL"
    # harvest (wait-export) dispatched exactly once; no create/prioritize/stop.
    assert len(seed.runner_calls) == 1
    assert "--wait-export-only" in seed.runner_calls[0]
    assert not any("--create-only" in args for args in seed.runner_calls)
    assert not any("/add" in " ".join(args) for args in seed.runner_calls)
    assert len(seed.prioritize_calls) == prioritize_calls_before
    assert len(seed.stop_calls) == stop_calls_before
    assert seed.probe_calls == [(TASK_ID, TASK_NUMBER)]
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"
    crawls = store.crawl_work_for_job(int(job["id"]))
    assert all(crawl["state"] == "SUCCESS" for crawl in crawls)


def test_reexport_2_live_probe_unknown_still_fails_closed(tmp_path):
    """REEXPORT-2: probe UNKNOWN -> no export, no prioritize, fail closed."""
    seed, store, job = _seeded_reexport(
        tmp_path, error_text=ZERO_ROWS_ERROR, remote_progress=None
    )
    seed.runner = seed.exporting_runner(_priced_rows(3))
    before = store.get_remote_job(int(job["id"]))

    result = run_manual_reexport(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "REMOTE_PROGRESS_UNKNOWN"
    assert seed.runner_calls == []
    assert seed.prioritize_calls == []
    assert seed.stop_calls == []
    assert store.get_remote_job(int(job["id"])) == before


def test_reexport_3_probe_identity_conflict_fails_closed(tmp_path):
    """REEXPORT-3: RemoteTaskIdentityConflictError -> no export, fail closed."""
    seed, store, job = _seeded_reexport(
        tmp_path, error_text=ZERO_ROWS_ERROR, remote_progress=None
    )
    seed.probe_error = RemoteTaskIdentityConflictError(
        "live task identity does not match the durable remote identity"
    )
    seed.runner = seed.exporting_runner(_priced_rows(3))
    before = store.get_remote_job(int(job["id"]))

    result = run_manual_reexport(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "REMOTE_PROGRESS_UNKNOWN"
    assert seed.runner_calls == []
    assert seed.prioritize_calls == []
    assert seed.stop_calls == []
    assert store.get_remote_job(int(job["id"])) == before


def test_reexport_4_accepted_evidence_path_is_unchanged(tmp_path):
    """REEXPORT-4: accepted historical evidence keeps the exact old behavior."""
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = seed.exporting_runner(_priced_rows(3))
    requests = seed.build_requests(3)
    store, job = seed.seed(3, priority_updated=True)

    result = run_manual_reexport(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REEXPORT_COMPLETED"
    assert result["result_count"] == 3
    assert result["create_calls"] == 0
    assert result["progress_classification"] == "REMOTE_TERMINAL"
    # Remote call accounting (OBS): preflight probe reported 2, export 1.
    assert result["preflight_remote_calls"] == 2
    assert result["export_remote_calls"] == 1
    assert result["remote_calls_total"] == 3
    assert result["remote_calls"] == 3
    assert len(seed.runner_calls) == 1
    assert store.get_remote_job(int(job["id"]))["state"] == "COMPLETED"


def test_reexport_5_other_manual_errors_get_no_fallback(tmp_path):
    """REEXPORT-5: a different MANUAL diagnostic stays rejected, no export."""
    seed, store, job = _seeded_reexport(
        tmp_path, error_text="some unrelated manual error", remote_progress=None
    )
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = seed.exporting_runner(_priced_rows(3))

    result = run_manual_reexport(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "INVALID_REMOTE_STATE"
    assert "not harvestable: MANUAL_REQUIRED" in result["reason"]
    assert seed.runner_calls == []
    assert seed.prioritize_calls == []
    assert seed.stop_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


# ---------------------------------------------------------------------------
# REEXPORT-6 (review round 2): the live proof alone never admits a job whose
# durable state is not the strict priority-unconfirmed MANUAL family.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("durable_state", ["SUBMITTED", "RUNNING", "EXPORTED"])
def test_reexport_6_live_proof_never_admits_non_manual_durable_state(
    tmp_path, durable_state
):
    """A proven TERMINAL probe cannot rescue SUBMITTED/RUNNING/EXPORTED jobs
    whose historical priority evidence is missing: still rejected, zero
    export, durable state untouched -- corrupted evidence is never silently
    repaired by the fallback."""
    seed = _Seed(tmp_path)
    seed.remote_progress = seed.terminal_progress(3)  # a live probe would admit
    seed.runner = seed.exporting_runner(_priced_rows(3))
    store, job = seed.seed(3, priority_updated=False, job_state=durable_state)

    result = run_manual_reexport(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert result["error_code"] == "INVALID_REMOTE_STATE"
    assert "exact accepted priority evidence is required" in result["reason"]
    # The preflight probe ran (and proved TERMINAL), but the export never
    # dispatched: zero export/prioritize/stop calls, zero durable change.
    assert seed.probe_calls == [(TASK_ID, TASK_NUMBER)]
    assert result["export_remote_calls"] == 0
    assert result["remote_calls_total"] == result["preflight_remote_calls"]
    assert seed.runner_calls == []
    assert seed.prioritize_calls == []
    assert seed.stop_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == durable_state


# ---------------------------------------------------------------------------
# REBUILD: live proof fallback on the manual rebuild path.
# ---------------------------------------------------------------------------


def _seeded_rebuild(
    tmp_path,
    *,
    remote_progress,
    error_text=ZERO_ROWS_ERROR,
    result_json: str | None = PRIORITY_PENDING_RESULT_JSON,
):
    seed = _Seed(tmp_path, rebuild=True)
    seed.remote_progress = remote_progress
    requests = seed.build_requests(3)
    store, job = seed.seed(
        3,
        priority_updated=False,
        job_state="MANUAL_REQUIRED",
        error_text=error_text,
        result_json=result_json,
    )
    return seed, store, job


def test_rebuild_1_live_proof_active_runs_writeahead_protocol(tmp_path):
    """REBUILD-1: priority-unconfirmed family + live ACTIVE -> full protocol."""
    seed, store, job = _seeded_rebuild(tmp_path, remote_progress=None)
    seed.remote_progress = seed.active_progress(3)
    seed.runner = seed.creating_runner()

    result = run_manual_rebuild(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        action_id="liveproof-rebuild-1",
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REBUILD_DISPATCHED"
    assert result["new_task_name"] != OLD_TASK_NAME
    assert result["stop_phase"] == "STOP_CONFIRMED"
    assert result["migrated_crawl_count"] == 3
    # Write-ahead protocol walked through: the durable action row ends in
    # DISPATCHED and the stop evidence carries the confirmed disable.
    action = store.get_rebuild_action("liveproof-rebuild-1")
    assert action is not None
    assert action["phase"] == "DISPATCHED"
    replacement = store.get_replacement_for_job(int(job["id"]))
    evidence = json.loads(replacement["stop_evidence_json"])
    assert evidence["phase"] == "STOP_CONFIRMED"
    assert evidence["disable_confirmed"] is True
    assert evidence["precheck_classification"] == "REMOTE_ACTIVE"
    # Old job retired, replacement created exactly once, crawls migrated.
    old = store.get_remote_job(int(job["id"]))
    assert old["state"] == "SUPERSEDED"
    assert old["remote_identity"] == IDENTITY
    new_job = store.get_remote_job(int(result["new_remote_job_id"]))
    assert new_job["task_name"] == result["new_task_name"]
    assert new_job["state"] == "SUBMITTED"
    new_crawls = store.crawl_work_for_job(int(result["new_remote_job_id"]))
    assert len(new_crawls) == 3
    assert all(crawl["state"] == "SUBMITTED" for crawl in new_crawls)
    # Remote writes: exactly one create, exactly one confirmed stop.
    assert len(seed.runner_calls) == 1
    assert "--create-only" in seed.runner_calls[0]
    assert len(seed.stop_calls) == 1
    assert seed.stop_calls[0]["old_state_at_stop"] == "MANUAL_REQUIRED"
    # Two read-only probes: the live-proof preflight plus the stop precheck.
    assert len(seed.probe_calls) == 2
    # Remote call accounting mirrors probes(2) + stop(1) + create(1).
    assert result["remote_calls"] == 4


def test_rebuild_2_live_proof_terminal_skips_disable(tmp_path):
    """REBUILD-2: live TERMINAL -> no disable, TERMINAL_PROOF, replacement."""
    seed, store, job = _seeded_rebuild(tmp_path, remote_progress=None)
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = seed.creating_runner()

    result = run_manual_rebuild(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        action_id="liveproof-rebuild-2",
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REBUILD_DISPATCHED"
    assert result["stop_phase"] == "TERMINAL_BEFORE_REBUILD"
    assert result["live_proof_classification"] == "REMOTE_TERMINAL"
    assert seed.stop_calls == []  # never disables a provably terminal task
    assert len(seed.runner_calls) == 1  # exactly one create
    assert store.get_remote_job(int(job["id"]))["state"] == "SUPERSEDED"
    assert store.get_replacement_for_job(int(job["id"])) is not None
    new_crawls = store.crawl_work_for_job(int(result["new_remote_job_id"]))
    assert len(new_crawls) == 3


def _assert_rebuild_live_proof_fail_closed(seed, store, job, result):
    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "INVALID_REMOTE_STATE"
    assert result["rejection_stage"] == "live_proof"
    assert "live identity proof" in result["reason"]
    assert result["progress_classification"] == "REMOTE_UNKNOWN"
    # Zero durable protocol rows and zero remote writes of any kind.
    assert store.get_latest_rebuild_action_for_job(int(job["id"])) is None
    assert store.get_replacement_for_job(int(job["id"])) is None
    assert seed.stop_calls == []
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


def test_rebuild_3_unknown_live_probe_fails_closed(tmp_path):
    """REBUILD-3a: UNKNOWN live probe -> no replacement/stop/create."""
    seed, store, job = _seeded_rebuild(tmp_path, remote_progress=None)
    seed.runner = seed.creating_runner()

    result = run_manual_rebuild(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        action_id="liveproof-rebuild-3a",
        timeout_seconds=60.0,
    )

    _assert_rebuild_live_proof_fail_closed(seed, store, job, result)


def test_rebuild_3_identity_conflict_fails_closed(tmp_path):
    """REBUILD-3b: probe identity conflict fails closed like UNKNOWN."""
    seed, store, job = _seeded_rebuild(tmp_path, remote_progress=None)
    seed.probe_error = RemoteTaskIdentityConflictError(
        "live task identity does not match the durable remote identity"
    )
    seed.runner = seed.creating_runner()

    result = run_manual_rebuild(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        action_id="liveproof-rebuild-3b",
        timeout_seconds=60.0,
    )

    _assert_rebuild_live_proof_fail_closed(seed, store, job, result)


def test_rebuild_non_family_shape_keeps_original_gate(tmp_path):
    """Outside the priority-unconfirmed family the original rejection stands
    and no probe call is spent at all."""
    seed, store, job = _seeded_rebuild(
        tmp_path, remote_progress=None, error_text="some unrelated manual error"
    )
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = seed.creating_runner()

    result = run_manual_rebuild(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        action_id="liveproof-rebuild-5",
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "INVALID_REMOTE_STATE"
    assert result["rejection_stage"] == "priority_evidence_gate"
    assert (
        "exact durable priority evidence does not match the remote identity"
        in result["reason"]
    )
    assert seed.probe_calls == []  # no live probe for a non-family shape
    assert seed.stop_calls == []
    assert seed.runner_calls == []
    assert store.get_latest_rebuild_action_for_job(int(job["id"])) is None


# ---------------------------------------------------------------------------
# REBUILD-4 (review round 2): only the strict PRIORITY_PENDING/null-evidence
# result_json joins the fallback; every corrupt shape keeps the original gate.
# ---------------------------------------------------------------------------


_REBUILD_CORRUPT_RESULT_JSON = [
    pytest.param(None, id="null-result-json"),
    pytest.param("", id="empty-result-json"),
    pytest.param("not-json{{", id="malformed-result-json"),
    pytest.param("{}", id="empty-object"),
    pytest.param('{"priority_evidence": null}', id="missing-state"),
    pytest.param(
        '{"state": "SUBMITTED", "priority_evidence": null}', id="wrong-state"
    ),
    pytest.param(
        '{"state": "PRIORITY_PENDING", "priority_evidence": "updated"}',
        id="evidence-not-null",
    ),
]


@pytest.mark.parametrize("corrupt_result_json", _REBUILD_CORRUPT_RESULT_JSON)
def test_rebuild_4_corrupt_result_json_fails_priority_gate(
    tmp_path, corrupt_result_json
):
    """NULL/empty/malformed/partial result_json is not "null evidence": the
    rebuild keeps the original priority_evidence_gate rejection with zero
    stop/create/replacement and no live probe spent."""
    seed, store, job = _seeded_rebuild(
        tmp_path, remote_progress=None, result_json=corrupt_result_json
    )
    seed.remote_progress = seed.terminal_progress(3)  # a live probe would admit
    seed.runner = seed.creating_runner()

    result = run_manual_rebuild(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        action_id="liveproof-rebuild-4",
        timeout_seconds=60.0,
    )

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "INVALID_REMOTE_STATE"
    assert result["rejection_stage"] == "priority_evidence_gate"
    assert (
        "exact durable priority evidence does not match the remote identity"
        in result["reason"]
    )
    # No live probe spent, zero remote writes, zero durable protocol rows.
    assert seed.probe_calls == []
    assert seed.stop_calls == []
    assert seed.runner_calls == []
    assert store.get_latest_rebuild_action_for_job(int(job["id"])) is None
    assert store.get_replacement_for_job(int(job["id"])) is None
    assert store.get_remote_job(int(job["id"]))["state"] == "MANUAL_REQUIRED"


# ---------------------------------------------------------------------------
# Shared strict family helpers (review round 2): unit pinning for the exact
# shape the harvest core and the dashboard rebuild gate both enforce.
# ---------------------------------------------------------------------------


def test_strict_priority_unconfirmed_helpers_unit():
    """The shared helpers accept exactly the strict shape and nothing else."""
    from app.services.recovery.harvest import (
        is_priority_unconfirmed_manual_family,
        is_priority_unconfirmed_result_shape,
    )

    strict = '{"state": "PRIORITY_PENDING", "priority_evidence": null}'
    assert is_priority_unconfirmed_result_shape(strict)
    assert is_priority_unconfirmed_result_shape(
        {"state": "PRIORITY_PENDING", "priority_evidence": None}
    )
    for bad in (
        None,
        "",
        "not-json{{",
        "{}",
        "[]",
        '"PRIORITY_PENDING"',
        '{"state": "PRIORITY_PENDING"}',
        '{"state": "SUBMITTED", "priority_evidence": null}',
        '{"state": "PRIORITY_PENDING", "priority_evidence": "updated"}',
    ):
        assert not is_priority_unconfirmed_result_shape(bad), bad

    def _family_job(**overrides):
        job = {
            "state": "MANUAL_REQUIRED",
            "post_attempted": 1,
            "error_text": ZERO_ROWS_ERROR,
            "result_json": strict,
        }
        job.update(overrides)
        return job

    assert is_priority_unconfirmed_manual_family(_family_job())
    assert not is_priority_unconfirmed_manual_family(
        _family_job(state="SUBMITTED")
    )
    assert not is_priority_unconfirmed_manual_family(
        _family_job(post_attempted=0)
    )
    assert not is_priority_unconfirmed_manual_family(
        _family_job(error_text="some unrelated manual error")
    )
    assert not is_priority_unconfirmed_manual_family(
        _family_job(result_json=None)
    )
    assert not is_priority_unconfirmed_manual_family(
        _family_job(result_json='{"state": "PRIORITY_PENDING"}')
    )
    assert not is_priority_unconfirmed_manual_family(
        _family_job(
            result_json='{"state": "PRIORITY_PENDING", "priority_evidence": "updated"}'
        )
    )


# ---------------------------------------------------------------------------
# OBS-1: re-export action record accounts for the preflight remote calls.
# ---------------------------------------------------------------------------


def test_obs_1_reexport_record_accounts_for_preflight_calls(tmp_path):
    """OBS-1: probe ran once, export never ran -> the jsonl record shows the
    preflight calls and a non-misleading remote_calls total."""
    seed, store, job = _seeded_reexport(
        tmp_path, error_text=ZERO_ROWS_ERROR, remote_progress=None
    )
    seed.runner = seed.exporting_runner(_priced_rows(3))

    result = run_manual_reexport_command(
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path / "runtime",
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        timeout_seconds=60.0,
        adapter=seed.adapter,
    )

    assert result["outcome"] == "REEXPORT_REJECTED"
    assert seed.probe_calls == [(TASK_ID, TASK_NUMBER)]  # exactly one preflight
    assert seed.runner_calls == []  # export never dispatched
    actions = read_operator_actions(seed.run_dir)
    assert len(actions) == 1
    record = actions[0]
    assert record["action_type"] == "manual_reexport"
    assert record["outcome"] == "REEXPORT_REJECTED"
    assert record["preflight_remote_calls"] >= 1
    assert record["export_remote_calls"] == 0
    assert record["remote_calls_total"] == (
        record["preflight_remote_calls"] + record["export_remote_calls"]
    )
    assert record["remote_calls"] == record["remote_calls_total"]
    assert record["remote_calls"] != 0  # no more misleading zero total
    assert record["progress_classification"] == "REMOTE_UNKNOWN"
    assert record["create_calls"] == 0


def test_obs_1b_rebuild_record_carries_stage_and_breakdown(tmp_path):
    """Rebuild records carry rejection_stage, classification, breakdown."""
    seed, store, job = _seeded_rebuild(tmp_path, remote_progress=None)
    seed.remote_progress = seed.active_progress(3)
    seed.runner = seed.creating_runner()

    run_manual_rebuild(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        action_id="liveproof-rebuild-obs",
        timeout_seconds=60.0,
    )

    actions = [
        row
        for row in read_operator_actions(seed.run_dir)
        if row["action_type"] == "rebuild"
    ]
    assert len(actions) == 1
    record = actions[0]
    assert record["outcome"] == "REBUILD_DISPATCHED"
    # Review round 2: adapter operation counters (probe/stop/create dispatches),
    # renamed from *_remote_calls -- these are not HTTP call counts.
    assert record["probe_operations"] == 2
    assert record["stop_operations"] == 1
    assert record["create_operations"] == 1
    assert record["remote_calls"] == 4
    assert record["progress_classification"] == "REMOTE_ACTIVE"
    assert record["live_proof_classification"] == "REMOTE_ACTIVE"

    # The rejection side of the same protocol records the stage too.
    seed_b, store_b, job_b = _seeded_rebuild(tmp_path / "b", remote_progress=None)
    seed_b.runner = seed_b.creating_runner()
    rejected = run_manual_rebuild(
        state_path=seed_b.state_path,
        lock_path=seed_b.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job_b["id"]),
        remote_identity=IDENTITY,
        adapter=seed_b.adapter,
        action_id="liveproof-rebuild-obs-rej",
        timeout_seconds=60.0,
    )
    assert rejected["outcome"] == "REBUILD_REJECTED"
    records = [
        row
        for row in read_operator_actions(seed_b.run_dir)
        if row["action_type"] == "rebuild"
    ]
    assert records[0]["rejection_stage"] == "live_proof"
    assert records[0]["progress_classification"] == "REMOTE_UNKNOWN"
    assert records[0]["stop_operations"] == 0
    assert records[0]["create_operations"] == 0


# ---------------------------------------------------------------------------
# UI-1 (backend contract): rebuild answer + projection feed the auto-switch.
# ---------------------------------------------------------------------------


def test_ui_1_rebuild_response_and_projection_expose_the_replacement(tmp_path):
    """The rebuild success response carries new_remote_job_id/new_task_name
    and the projection summary contains the replacement so the frontend can
    switch the drawer to the new job."""
    seed, store, job = _seeded_rebuild(tmp_path, remote_progress=None)
    seed.remote_progress = seed.terminal_progress(3)
    seed.runner = seed.creating_runner()

    result = run_manual_rebuild(
        state_path=seed.state_path,
        lock_path=seed.lock_path,
        business_date=BUSINESS_DATE,
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        adapter=seed.adapter,
        action_id="liveproof-rebuild-ui",
        timeout_seconds=60.0,
    )

    # Contract the frontend depends on for the auto-switch:
    assert result["new_remote_job_id"]
    assert result["new_task_name"] == result["new_batch_number"]
    assert result["new_task_name"] != OLD_TASK_NAME

    projected = project_remote_jobs(seed.state_path, BUSINESS_DATE)
    by_id = {int(row["id"]): row for row in projected}
    old_row = by_id[int(job["id"])]
    assert old_row["state"] == "SUPERSEDED"
    assert old_row["superseded_by"] is not None
    assert old_row["superseded_by"]["new_task_name"] == result["new_task_name"]
    new_row = by_id[int(result["new_remote_job_id"])]
    assert new_row["task_name"] == result["new_task_name"]
    assert new_row["remote_identity"] == f"{MALL_KEY}|{NEW_TASK_ID}|{NEW_TASK_NUMBER}"
    assert new_row["state"] in {"SUBMITTED", "PRIORITY_PENDING"}
