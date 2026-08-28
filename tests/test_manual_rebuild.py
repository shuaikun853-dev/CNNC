"""Offline contract tests for the dashboard manual-rebuild protocol."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from app.services.daily_audit_store import DailyAuditStore
from app.services.dashboard_manual_ops import (
    _replacement_task_name,
    read_operator_actions,
    run_manual_rebuild,
    run_manual_rebuild_command,
)
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    LegacyRestartOutcome,
    LegacySubmitOutcome,
    RestartStatus,
    SubmitStatus,
)


BUSINESS_DATE = "2026-08-28"
MALL_KEY = "lxwl"
MALL_NAME = "领先未来商城"
OLD_TASK_DIGITS = "17300000000001"
OLD_TASK_NAME = f"cnnc_current_opt_{MALL_NAME}_{OLD_TASK_DIGITS}"
TASK_ID = "1246803"
TASK_NUMBER = "RW-20260828160830"
IDENTITY = f"{MALL_KEY}|{TASK_ID}|{TASK_NUMBER}"
NEW_TASK_ID = "8899001"
NEW_TASK_NUMBER = "RW-20260828REBUILD"
NEW_IDENTITY = f"{MALL_KEY}|{NEW_TASK_ID}|{NEW_TASK_NUMBER}"
TERMINAL_CRAWL_STATES = {"SUCCESS", "NO_RESULT", "TERMINAL_FAILED"}


def _sql(state_path: Path, query: str, *params) -> None:
    connection = sqlite3.connect(str(state_path), timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


class _RebuildSeed:
    """Durable fixture mirroring the manual-reexport seed, rebuild shaped."""

    def __init__(self, base: Path):
        self.run_dir = Path(base) / "runtime" / BUSINESS_DATE
        self.state_path = self.run_dir / "state.sqlite3"
        self.lock_path = self.run_dir / "incremental_price_audit.lock"
        self.batch_state = self.run_dir / "BATCH_STATE.md"
        self.batch_state.parent.mkdir(parents=True)
        self.batch_state.write_text("# state\n", encoding="utf-8")
        self.job_dir = self.run_dir / "zhaoshang" / MALL_KEY / OLD_TASK_DIGITS
        self.remote_progress: dict[str, int] | None = {}
        self.stop_outcome = LegacyRestartOutcome(
            RestartStatus.STOPPED,
            disable_confirmed=True,
            observed_enabled=False,
            diagnostic="exact task disabled with confirmed evidence",
        )
        self.stop_calls: list[dict[str, object]] = []
        self.stop_disable_writes = 0
        self.probe_calls: list[tuple[str, str]] = []
        self.runner_calls: list[list[str]] = []
        self.submit_create_old_states: list[str | None] = []
        self.runner_with_priority = False
        self.adapter = CnncLegacyZhaoshangAdapter(
            mappings=[],
            db_env_path=None,
            runner=self._runner,
            progress_probe=self._progress_probe,
            stop_runner=self._stop_runner,
        )
        self.old_job_id: int | None = None

    def _progress_probe(self, _job, task_id, task_number, _timeout_seconds):
        self.probe_calls.append((str(task_id), str(task_number)))
        return {
            "task_id": str(task_id),
            "task_number": str(task_number),
            "progress": dict(self.remote_progress) if self.remote_progress else None,
            "rsp_desc": "",
            "remote_calls": 2,
        }

    def _stop_runner(self, _job, task_id, _task_number, _timeout, *, resume_phase=None):
        old_state = None
        if self.old_job_id is not None:
            store = DailyAuditStore(self.state_path)
            row = store.get_remote_job(self.old_job_id)
            old_state = str(row["state"]) if row is not None else None
        self.stop_calls.append({"task_id": str(task_id), "old_state_at_stop": old_state})
        if self.stop_outcome.status is RestartStatus.STOPPED:
            self.stop_disable_writes += 1
        return self.stop_outcome

    def _runner(self, args, _timeout_seconds, _environment):
        self.runner_calls.append(list(args))
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
        ]
        if self.runner_with_priority:
            events.append(
                {
                    "action": "PRIORITY_UPDATED",
                    "mall": MALL_KEY,
                    "task_name": task_name,
                    "task_id": NEW_TASK_ID,
                    "task_number": NEW_TASK_NUMBER,
                    "affected_rows": 1,
                }
            )
        with self.batch_state.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(f"- `{json.dumps(event)}`\n")
        old_state = None
        if self.old_job_id is not None:
            store = DailyAuditStore(self.state_path)
            row = store.get_remote_job(self.old_job_id)
            old_state = str(row["state"]) if row is not None else None
        self.submit_create_old_states.append(old_state)

        class Result:
            returncode = 0

        return Result()

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
        count: int = 18,
        *,
        success_crawls: tuple[int, ...] = (),
        job_state: str | None = None,
        post_attempted: int | None = None,
        error_text: str | None = None,
    ):
        requests = self.build_requests(count)
        prepared = self.adapter.prepare_job(
            BUSINESS_DATE,
            requests,
            self.job_dir / "input.xlsx",
            self.job_dir / "result.xlsx",
            OLD_TASK_NAME,
            self.batch_state,
        )
        for event in (
            {
                "action": "CREATED",
                "mall": MALL_KEY,
                "task_name": OLD_TASK_NAME,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
                "expected_subtask_count": count,
            },
            {
                "action": "ENABLED",
                "mall": MALL_KEY,
                "task_name": OLD_TASK_NAME,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
            },
            {
                "action": "PRIORITY_UPDATED",
                "mall": MALL_KEY,
                "task_name": OLD_TASK_NAME,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
                "affected_rows": count,
            },
        ):
            with self.batch_state.open("a", encoding="utf-8") as stream:
                stream.write(f"- `{json.dumps(event)}`\n")

        store = DailyAuditStore(self.state_path)
        lease = store.acquire_lease(BUSINESS_DATE, "seed", 60)
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
        crawls = []
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
            crawls.append(crawl)
        for index in success_crawls:
            store.complete_crawl_work(
                lease,
                int(crawls[index]["id"]),
                {"page_price_value": "9.5", "page_price_status": "OK", "page_price_source": "招商局"},
            )
        store.release_lease(lease)
        if error_text is not None:
            _sql(
                self.state_path,
                "UPDATE remote_jobs SET error_text=? WHERE id=?",
                error_text,
                int(job["id"]),
            )
        if job_state is not None:
            _sql(
                self.state_path,
                "UPDATE remote_jobs SET state=? WHERE id=?",
                job_state,
                int(job["id"]),
            )
        if post_attempted is not None:
            _sql(
                self.state_path,
                "UPDATE remote_jobs SET post_attempted=? WHERE id=?",
                post_attempted,
                int(job["id"]),
            )
        self.old_job_id = int(job["id"])
        return store, job, crawls

    def rebuild(self, job, **overrides):
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
        return run_manual_rebuild(**kwargs)


def _snapshot(seed: _RebuildSeed, job_id: int):
    store = DailyAuditStore(seed.state_path)
    job = store.get_remote_job(job_id)
    return (
        dict(job or {}),
        [dict(row) for row in store.crawl_work_for_job(job_id)],
        list(store.list_remote_replacements(int(job["run_id"]))),
    )


def test_rebuild_terminal_job_dispatches_fresh_named_replacement(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, crawls = seed.seed(18, error_text="stalled evidence text")

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_DISPATCHED"
    new_task_name = result["new_task_name"]
    assert new_task_name != OLD_TASK_NAME
    assert re.fullmatch(r"cnnc_current_opt_领先未来商城_\d{14}", new_task_name)
    assert re.fullmatch(r"^cnnc_current_opt_[^_]+_\d{14}$", new_task_name)
    assert result["new_batch_number"] == new_task_name
    assert result["migrated_crawl_count"] == 18
    assert result["skipped_terminal_crawl_count"] == 0
    assert result["stop_phase"] == "TERMINAL_BEFORE_REBUILD"
    assert result["precheck_classification"] == "REMOTE_TERMINAL"

    old = store.get_remote_job(int(job["id"]))
    assert old["state"] == "SUPERSEDED"
    assert old["remote_identity"] == IDENTITY
    assert old["error_text"] == "stalled evidence text"
    assert old["task_name"] == OLD_TASK_NAME

    replacement = store.get_replacement_for_job(int(job["id"]))
    assert replacement["old_batch_number"] == OLD_TASK_NAME
    assert replacement["new_batch_number"] == new_task_name
    assert replacement["status"] == "ACTIVE"

    new_job = store.get_remote_job(int(result["new_remote_job_id"]))
    assert new_job["task_name"] == new_task_name
    assert new_job["state"] == "PRIORITY_PENDING"
    assert int(new_job["post_attempted"]) == 1
    assert new_job["remote_identity"] == NEW_IDENTITY
    assert new_job["mall_key"] == MALL_KEY

    new_crawls = store.crawl_work_for_job(int(result["new_remote_job_id"]))
    assert len(new_crawls) == 18
    assert all(crawl["state"] == "PENDING" for crawl in new_crawls)
    assert store.crawl_work_for_job(int(job["id"])) == []
    assert {int(crawl["id"]) for crawl in new_crawls} == {
        int(crawl["id"]) for crawl in crawls
    }

    assert len(seed.runner_calls) == 1
    assert "--create-only" in seed.runner_calls[0]
    assert seed.stop_calls == []
    assert seed.probe_calls == [(TASK_ID, TASK_NUMBER)]


def test_rebuild_active_job_stops_before_supersede_then_dispatches(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    seed.runner_with_priority = True
    store, job, _crawls = seed.seed(18)

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_DISPATCHED"
    assert result["stop_phase"] == "STOP_CONFIRMED"
    assert result["state"] == "SUBMITTED"
    assert result["new_remote_identity"] == NEW_IDENTITY
    assert len(seed.stop_calls) == 1
    assert seed.stop_calls[0]["old_state_at_stop"] == "SUBMITTED"
    assert len(seed.runner_calls) == 1
    assert seed.submit_create_old_states == ["SUPERSEDED"]
    new_job = store.get_remote_job(int(result["new_remote_job_id"]))
    assert new_job["state"] == "SUBMITTED"
    replacement = store.get_replacement_for_job(int(job["id"]))
    evidence = json.loads(replacement["stop_evidence_json"])
    assert evidence["phase"] == "STOP_CONFIRMED"
    assert evidence["disable_confirmed"] is True
    assert evidence["precheck_classification"] == "REMOTE_ACTIVE"


def test_rebuild_disable_unconfirmed_rejects_with_zero_durable_writes(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    seed.stop_outcome = LegacyRestartOutcome(
        RestartStatus.DISABLE_UNCONFIRMED,
        observed_enabled=True,
        diagnostic="disable did not take effect on the exact task",
    )
    store, job, _crawls = seed.seed(18)
    before = _snapshot(seed, int(job["id"]))

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "OLD_TASK_STOP_UNCONFIRMED"
    assert len(seed.stop_calls) == 1
    assert seed.runner_calls == []
    assert _snapshot(seed, int(job["id"])) == before


def test_rebuild_disable_unknown_requires_reconciled_disabled_state(tmp_path):
    # Unknown disable with no provable live state rejects with zero writes.
    seed = _RebuildSeed(tmp_path / "case-a")
    seed.remote_progress = seed.active_progress(18)
    seed.stop_outcome = LegacyRestartOutcome(
        RestartStatus.DISABLE_UNKNOWN,
        observed_enabled=None,
        diagnostic="disable request outcome unknown; reconcile failed",
    )
    store, job, _crawls = seed.seed(18)
    before = _snapshot(seed, int(job["id"]))

    rejected = seed.rebuild(job)

    assert rejected["outcome"] == "REBUILD_REJECTED"
    assert rejected["error_code"] == "OLD_TASK_STOP_UNCONFIRMED"
    assert _snapshot(seed, int(job["id"])) == before
    assert seed.runner_calls == []

    # The re-observed disabled task reconciles to a confirmed stop.
    seed_b = _RebuildSeed(tmp_path / "case-b")
    seed_b.remote_progress = seed_b.active_progress(18)
    seed_b.stop_outcome = LegacyRestartOutcome(
        RestartStatus.DISABLE_UNKNOWN,
        observed_enabled=False,
        diagnostic="disable request outcome unknown; re-observed live state",
    )
    store_b, job_b, _crawls_b = seed_b.seed(18)

    result = seed_b.rebuild(job_b)

    assert result["outcome"] == "REBUILD_DISPATCHED"
    assert result["stop_phase"] == "STOP_CONFIRMED"
    replacement = store_b.get_replacement_for_job(int(job_b["id"]))
    evidence = json.loads(replacement["stop_evidence_json"])
    assert evidence["reconciled"] is True
    assert evidence["disable_confirmed"] is False


def test_rebuild_externally_disabled_migrates_without_any_disable_write(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    seed.stop_outcome = LegacyRestartOutcome(
        RestartStatus.EXTERNALLY_DISABLED,
        observed_enabled=False,
        diagnostic="exact task is already disabled without durable stop evidence",
    )
    store, job, _crawls = seed.seed(18)

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_DISPATCHED"
    assert result["stop_phase"] == "ALREADY_DISABLED"
    assert len(seed.stop_calls) == 1
    assert seed.stop_disable_writes == 0
    assert store.get_remote_job(int(job["id"]))["state"] == "SUPERSEDED"
    assert len(store.crawl_work_for_job(int(result["new_remote_job_id"]))) == 18


def test_rebuild_unknown_progress_rejects_without_stop_or_writes(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = None
    store, job, _crawls = seed.seed(18)
    before = _snapshot(seed, int(job["id"]))

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "REMOTE_PROGRESS_UNKNOWN"
    assert result["progress_classification"] == "REMOTE_UNKNOWN"
    assert seed.probe_calls == [(TASK_ID, TASK_NUMBER)]
    assert seed.stop_calls == []
    assert seed.runner_calls == []
    assert _snapshot(seed, int(job["id"])) == before


@pytest.mark.parametrize(
    "state,post_attempted",
    [
        ("SUBMIT_INTENT", 0),
        ("SUBMIT_UNKNOWN", 1),
        ("PRIORITY_PENDING", 1),
        ("COMPLETED", 1),
        ("SUPERSEDED", 1),
    ],
)
def test_rebuild_rejects_disallowed_source_states(tmp_path, state, post_attempted):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18, job_state=state, post_attempted=post_attempted)

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_NOT_ALLOWED"
    assert result["state"] == state
    assert result["post_attempted"] == post_attempted
    assert seed.probe_calls == []
    assert seed.stop_calls == []
    assert seed.runner_calls == []
    assert store.list_remote_replacements(int(job["run_id"])) == []


def test_rebuild_second_click_is_idempotent_with_any_action_id(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)

    first = seed.rebuild(job)
    assert first["outcome"] == "REBUILD_DISPATCHED"
    probes_after_first = len(seed.probe_calls)
    runners_after_first = len(seed.runner_calls)

    second = seed.rebuild(job, action_id="rebuild-replay-0002")

    assert second["outcome"] == "REBUILD_ALREADY_REPLACED"
    assert second["new_remote_job_id"] == first["new_remote_job_id"]
    assert second["replacement"]["id"] == store.get_replacement_for_job(int(job["id"]))["id"]
    assert len(seed.probe_calls) == probes_after_first
    assert len(seed.runner_calls) == runners_after_first
    assert seed.stop_calls == []


def test_rebuild_same_action_id_replay_returns_same_replacement(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)

    first = seed.rebuild(job, action_id="rebuild-once-0001")
    second = seed.rebuild(job, action_id="rebuild-once-0001")

    assert first["outcome"] == "REBUILD_DISPATCHED"
    assert second["outcome"] == "REBUILD_ALREADY_REPLACED"
    assert second["action_id"] == "rebuild-once-0001"
    assert second["replacement"]["id"] == first["replacement_id"]
    assert second["new_remote_job_id"] == first["new_remote_job_id"]
    assert len(seed.runner_calls) == 1


def test_rebuild_migrates_only_unfinished_crawls(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(3)
    store, job, crawls = seed.seed(3, success_crawls=(1,))

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_DISPATCHED"
    assert result["migrated_crawl_count"] == 2
    assert result["skipped_terminal_crawl_count"] == 1
    old_crawls = store.crawl_work_for_job(int(job["id"]))
    assert [crawl["state"] for crawl in old_crawls] == ["SUCCESS"]
    assert int(old_crawls[0]["id"]) == int(crawls[1]["id"])
    new_crawls = store.crawl_work_for_job(int(result["new_remote_job_id"]))
    assert len(new_crawls) == 2
    assert all(crawl["state"] == "PENDING" for crawl in new_crawls)


def test_rebuild_with_only_terminal_crawls_is_not_allowed(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(2)
    store, job, _crawls = seed.seed(2, success_crawls=(0, 1))
    before = _snapshot(seed, int(job["id"]))

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_NOT_ALLOWED"
    assert result["detail"] == "no eligible unfinished crawl work"
    assert seed.probe_calls == []
    assert seed.stop_calls == []
    assert _snapshot(seed, int(job["id"])) == before


def test_rebuild_rejects_eligible_crawl_outside_the_immutable_payload(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, crawls = seed.seed(18)
    _sql(
        seed.state_path,
        "UPDATE crawl_work SET exact_url=? WHERE id=?",
        "https://shop.example/tampered",
        int(crawls[4]["id"]),
    )
    before = _snapshot(seed, int(job["id"]))

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "INVALID_REMOTE_STATE"
    assert seed.probe_calls == []
    assert seed.stop_calls == []
    assert seed.runner_calls == []
    assert _snapshot(seed, int(job["id"])) == before


def test_rebuild_rejects_identity_mismatch_without_writes(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)
    before = _snapshot(seed, int(job["id"]))

    result = seed.rebuild(job, remote_identity=f"{MALL_KEY}|999|RW-FOREIGN")

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "IDENTITY_MISMATCH"
    assert seed.probe_calls == []
    assert seed.stop_calls == []
    assert seed.runner_calls == []
    assert _snapshot(seed, int(job["id"])) == before


def test_rebuild_submit_not_attempted_leaves_tick_resumable_intent(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)
    submit_calls: list[str] = []

    def fake_submit(_job, *, timeout_seconds):
        submit_calls.append(f"{timeout_seconds}")
        return LegacySubmitOutcome(
            SubmitStatus.NOT_ATTEMPTED, diagnostic="template is missing"
        )

    seed.adapter.submit = fake_submit

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_QUEUED"
    assert submit_calls == ["60.0"]
    new_job = store.get_remote_job(int(result["new_remote_job_id"]))
    assert new_job["state"] == "SUBMIT_INTENT"
    assert int(new_job["post_attempted"]) == 0
    assert new_job["remote_identity"] in (None, "")
    new_crawls = store.crawl_work_for_job(int(result["new_remote_job_id"]))
    assert len(new_crawls) == 18
    lease = store.acquire_lease(BUSINESS_DATE, "tick-resume", 60)
    try:
        resumable = store.list_remote_jobs(lease, states={"SUBMIT_INTENT"})
        assert [int(row["id"]) for row in resumable] == [int(result["new_remote_job_id"])]
    finally:
        store.release_lease(lease)


def test_rebuild_submit_unknown_marks_durable_unknown_for_reconcile(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)

    def fake_submit(_job, *, timeout_seconds):
        return LegacySubmitOutcome(
            SubmitStatus.SUBMIT_UNKNOWN, diagnostic="legacy create-only completion is unknown"
        )

    seed.adapter.submit = fake_submit

    result = seed.rebuild(job)

    assert result["outcome"] == "REBUILD_SUBMIT_UNKNOWN"
    new_job = store.get_remote_job(int(result["new_remote_job_id"]))
    assert new_job["state"] == "SUBMIT_UNKNOWN"
    assert int(new_job["post_attempted"]) == 1
    assert "unknown" in str(new_job["error_text"])
    assert store.get_remote_job(int(job["id"]))["state"] == "SUPERSEDED"


def test_rebuild_task_name_collision_with_reserved_name_fails_closed(tmp_path):
    action_id = "collide0001"
    reserved_name = _replacement_task_name(OLD_TASK_NAME, action_id, MALL_NAME)
    assert reserved_name != OLD_TASK_NAME
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)
    lease = store.acquire_lease(BUSINESS_DATE, "reserve", 60)
    try:
        store.prepare_remote_intent(
            lease,
            f"{MALL_KEY}:collider",
            reserved_name,
            "collider-hash",
            mall_key=MALL_KEY,
            input_path=str(seed.job_dir / "collider-input.xlsx"),
            result_path=str(seed.job_dir / "collider-result.xlsx"),
            payload_json={"requests": [{"sku_id": "SKU-X"}]},
        )
    finally:
        store.release_lease(lease)

    result = seed.rebuild(job, action_id=action_id)

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "TASK_NAME_CONFLICT"
    assert result["new_task_name"] == reserved_name
    assert seed.runner_calls == []
    assert store.get_remote_job(int(job["id"]))["state"] == "SUBMITTED"
    assert store.list_remote_replacements(int(job["run_id"])) == []


def test_rebuild_records_suffixed_logical_key_over_new_payload(tmp_path):
    action_id = "logicalkey0001"
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)

    result = seed.rebuild(job, action_id=action_id)

    new_job = store.get_remote_job(int(result["new_remote_job_id"]))
    suffix = f"#rebuild#{action_id[:8]}"
    assert new_job["logical_key"].endswith(suffix)
    recomputed = seed.adapter.prepare_job(
        BUSINESS_DATE,
        seed.build_requests(18),
        seed.job_dir / "probe-input.xlsx",
        seed.job_dir / "probe-result.xlsx",
        result["new_task_name"],
        seed.batch_state,
    )
    assert new_job["logical_key"] == f"{recomputed.logical_job_key}{suffix}"
    payload = json.loads(new_job["payload_json"])
    assert len(payload["requests"]) == 18
    assert payload["batch_state"] == str(seed.batch_state)


def test_rebuild_records_one_dashboard_operator_action(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    _store, job, _crawls = seed.seed(18)

    result = seed.rebuild(job, action_id="rebuild-action-001")

    actions = read_operator_actions(seed.run_dir)
    rebuild_actions = [row for row in actions if row["action_type"] == "rebuild"]
    assert len(rebuild_actions) == 1
    assert rebuild_actions[0]["action_id"] == "rebuild-action-001"
    assert rebuild_actions[0]["outcome"] == "REBUILD_DISPATCHED"
    assert rebuild_actions[0]["old_task_name"] == OLD_TASK_NAME
    assert rebuild_actions[0]["new_task_name"] == result["new_task_name"]
    assert rebuild_actions[0]["remote_calls"] == 2


def test_rebuild_command_dispatches_via_runtime_root(tmp_path):
    seed = _RebuildSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)

    result = run_manual_rebuild_command(
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path / "runtime",
        remote_job_id=int(job["id"]),
        remote_identity=IDENTITY,
        timeout_seconds=60.0,
        adapter=seed.adapter,
    )

    assert result["outcome"] == "REBUILD_DISPATCHED"
    assert result["business_date"] == BUSINESS_DATE
    assert store.get_remote_job(int(job["id"]))["state"] == "SUPERSEDED"
    actions = read_operator_actions(seed.run_dir)
    assert [row["action_type"] for row in actions] == ["rebuild"]
    assert actions[0]["action_id"] == result["action_id"]


@pytest.mark.parametrize(
    "outcome,expected_exit",
    [
        ("REBUILD_DISPATCHED", 0),
        ("REBUILD_QUEUED", 0),
        ("REBUILD_SUBMIT_UNKNOWN", 0),
        ("REBUILD_ALREADY_REPLACED", 0),
        ("REBUILD_REJECTED", 2),
        ("REBUILD_NOT_ALLOWED", 2),
    ],
)
def test_rebuild_cli_exit_codes_follow_the_durable_outcome(
    tmp_path, monkeypatch, outcome, expected_exit
):
    import scripts.incremental_price_audit as cli

    monkeypatch.setattr(
        cli,
        "run_manual_rebuild_command",
        lambda **_kwargs: {"outcome": outcome, "business_date": BUSINESS_DATE},
    )
    exit_code = cli.main(
        [
            "rebuild-remote-task",
            "--business-date",
            BUSINESS_DATE,
            "--runtime-root",
            str(tmp_path),
            "--remote-job-id",
            "7",
            "--remote-identity",
            IDENTITY,
        ]
    )
    assert exit_code == expected_exit
