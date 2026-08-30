"""Crash-window contract tests for the durable manual-rebuild protocol (P1-2).

Every test kills one rebuild click at an exact write-ahead boundary
(KeyboardInterrupt = BaseException, uncatchable by the protocol's own
``except Exception`` handlers) and then proves the resumed click can never
repeat a remote write that may already have landed: the INTENT row names the
replacement before any remote write, STOP_SENT forces a read-only reconcile
instead of a second disable, and a burned dispatch claim fails safe onto the
tick reconcile instead of a second create.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.services.daily_audit_store import DailyAuditStore, RemoteIntentConflict
from app.services.dashboard_manual_ops import (
    _replacement_task_name,
    run_manual_rebuild,
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

ACTION_A1 = "crash-a-intent-0001"
ACTION_A2 = "crash-a-click-0002"
ACTION_B1 = "crash-b-stop-0001"
ACTION_B2 = "crash-b-click-0002"
ACTION_B3 = "crash-b2-stop-001"
ACTION_B4 = "crash-b2-click-02"
ACTION_C1 = "crash-c-stop-0001"
ACTION_D1 = "crash-d-send-0001"
ACTION_D2 = "crash-d-click-0002"
ACTION_E1 = "crash-e-unknown-01"
ACTION_E2 = "crash-e-click-002"
ACTION_F1 = "crash-f-intent-001"
ACTION_F2 = "crash-f-click-002"
ACTION_PWF_A = "pwf-a-0001"
ACTION_PWF_B = "pwf-b-0002"
ACTION_PWF_C = "pwf-c-0003"
ACTION_PWF_D = "pwf-d-0004"
ACTION_PWF_E = "pwf-e-0005"
ACTION_PWF_F = "pwf-f-0006"
ACTION_TNC_A = "tnc-a-0001"
ACTION_TNC_B = "tnc-b-0002"
ACTION_TNC_C = "tnc-c-0003"
ACTION_TNC_E1 = "tnc-e-0001"
ACTION_TNC_E2 = "tnc-e-0002"


def _sql(state_path: Path, query: str, *params) -> None:
    connection = sqlite3.connect(str(state_path), timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(query, params)
        connection.commit()
    finally:
        connection.close()


class _CrashSeed:
    """Durable fixture mirroring the manual-rebuild seed, crash shaped."""

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
        job_state: str | None = None,
        post_attempted: int | None = None,
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
        store.release_lease(lease)
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


# ---------------------------------------------------------------------------
# Crash A: death during the progress precheck (INTENT durable, zero writes).
# ---------------------------------------------------------------------------


def test_crash_a_precheck_death_keeps_intent_and_resumes_same_action(tmp_path):
    seed = _CrashSeed(tmp_path)
    store, job, _crawls = seed.seed(18)

    def exploding_lookup(_prepared, _task_id, _task_number, _timeout):
        raise KeyboardInterrupt("crash during precheck")

    with pytest.raises(KeyboardInterrupt):
        seed.rebuild(job, action_id=ACTION_A1, progress_lookup=exploding_lookup)

    action = store.get_rebuild_action(ACTION_A1)
    assert action is not None
    assert action["phase"] == "INTENT"
    assert seed.stop_calls == []  # zero remote writes: stop never fired
    assert seed.runner_calls == []
    persisted_name = action["replacement_task_name"]

    seed.remote_progress = seed.terminal_progress(18)
    result = seed.rebuild(job, action_id=ACTION_A2)

    assert result["outcome"] == "REBUILD_DISPATCHED"
    assert result["rebuild_action_id"] == ACTION_A1  # resume owns the protocol
    assert result["new_task_name"] == persisted_name  # never re-derived
    latest = store.get_latest_rebuild_action_for_job(int(job["id"]))
    assert latest["action_id"] == ACTION_A1
    assert latest["phase"] == "DISPATCHED"
    assert store.get_rebuild_action(ACTION_A2) is None  # exactly one row


# ---------------------------------------------------------------------------
# Crash B: death after the write-ahead STOP_SENT (the disable may have left).
# ---------------------------------------------------------------------------


def test_crash_b_stop_sent_reconciles_already_disabled_without_second_stop(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    seed.runner_with_priority = True
    store, job, _crawls = seed.seed(18)

    def exploding_stop(_prepared, task_id, _task_number, _timeout):
        seed.stop_calls.append({"task_id": str(task_id), "crash": True})
        raise KeyboardInterrupt("crash around the disable write")

    with pytest.raises(KeyboardInterrupt):
        seed.rebuild(job, action_id=ACTION_B1, stop_callable=exploding_stop)

    action = store.get_rebuild_action(ACTION_B1)
    assert action["phase"] == "STOP_SENT"
    assert len(seed.stop_calls) == 1
    assert store.get_replacement_for_job(int(job["id"])) is None
    persisted_name = action["replacement_task_name"]

    def reconciled_lookup(_prepared, _task_id, _task_number, _timeout):
        return {"enabled": False, "progress": dict(seed.active_progress(18))}

    result = seed.rebuild(job, action_id=ACTION_B2, progress_lookup=reconciled_lookup)

    assert result["outcome"] == "REBUILD_DISPATCHED"
    assert result["rebuild_action_id"] == ACTION_B1
    assert result["new_task_name"] == persisted_name
    assert len(seed.stop_calls) == 1  # read-only reconcile, no second disable
    replacement = store.get_replacement_for_job(int(job["id"]))
    evidence = json.loads(replacement["stop_evidence_json"])
    assert evidence["phase"] == "ALREADY_DISABLED"
    assert evidence["reconciled"] is True
    assert evidence["observed_enabled"] is False
    latest = store.get_latest_rebuild_action_for_job(int(job["id"]))
    assert latest["phase"] == "DISPATCHED"


def test_crash_b_stop_sent_with_live_task_refuses_a_second_stop(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    store, job, _crawls = seed.seed(18)

    def exploding_stop(_prepared, task_id, _task_number, _timeout):
        seed.stop_calls.append({"task_id": str(task_id), "crash": True})
        raise KeyboardInterrupt("crash around the disable write")

    with pytest.raises(KeyboardInterrupt):
        seed.rebuild(job, action_id=ACTION_B3, stop_callable=exploding_stop)
    assert store.get_rebuild_action(ACTION_B3)["phase"] == "STOP_SENT"

    def live_lookup(_prepared, _task_id, _task_number, _timeout):
        return {"enabled": True, "progress": dict(seed.active_progress(18))}

    result = seed.rebuild(job, action_id=ACTION_B4, progress_lookup=live_lookup)

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "OLD_TASK_STOP_UNCONFIRMED"
    assert "refusing a second stop" in result["reason"]
    assert len(seed.stop_calls) == 1  # never a second disable
    assert store.get_rebuild_action(ACTION_B3)["phase"] == "STOP_SENT"
    assert store.get_replacement_for_job(int(job["id"])) is None
    assert store.get_remote_job(int(job["id"]))["state"] == "SUBMITTED"
    assert seed.runner_calls == []


# ---------------------------------------------------------------------------
# Crash C: death after STOP_CONFIRMED was durable (no precheck, no stop).
# ---------------------------------------------------------------------------


def test_crash_c_stop_confirmed_resumes_without_precheck_or_stop(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)
    persisted_name = _replacement_task_name(OLD_TASK_NAME, ACTION_C1, MALL_NAME)

    lease = store.acquire_lease(BUSINESS_DATE, "crash-c-seed", 300.0)
    try:
        store.create_rebuild_action(
            lease,
            action_id=ACTION_C1,
            old_remote_job_id=int(job["id"]),
            remote_identity=IDENTITY,
            replacement_task_name=persisted_name,
        )
        store.update_rebuild_action_phase(
            lease,
            action_id=ACTION_C1,
            phase="STOP_CONFIRMED",
            stop_evidence={
                "phase": "STOP_CONFIRMED",
                "disable_confirmed": True,
                "observed_enabled": False,
            },
        )
    finally:
        store.release_lease(lease)

    result = seed.rebuild(job, action_id="crash-c-click-0002")

    assert result["outcome"] == "REBUILD_DISPATCHED"
    assert result["stop_phase"] == "STOP_CONFIRMED"
    assert result["precheck_classification"] == "REMOTE_ACTIVE"
    assert result["new_task_name"] == persisted_name
    assert result["rebuild_action_id"] == ACTION_C1
    assert seed.stop_calls == []  # the disable already proved durable
    assert seed.probe_calls == []  # no fresh precheck either
    latest = store.get_latest_rebuild_action_for_job(int(job["id"]))
    assert latest["phase"] == "DISPATCHED"


# ---------------------------------------------------------------------------
# Crash D: death after CREATE_SENT, before the create outcome was recorded.
# ---------------------------------------------------------------------------


def test_crash_d_submit_death_never_repeats_the_create(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)
    submit_calls: list[str] = []
    real_submit = seed.adapter.submit

    def exploding_submit(prepared, *, timeout_seconds):
        submit_calls.append(str(prepared.task_name))
        raise KeyboardInterrupt("crash before the create outcome landed")

    seed.adapter.submit = exploding_submit

    with pytest.raises(KeyboardInterrupt):
        seed.rebuild(job, action_id=ACTION_D1)

    assert len(submit_calls) == 1
    action = store.get_rebuild_action(ACTION_D1)
    assert action["phase"] == "CREATE_SENT"  # SUPERSEDED -> CREATE_SENT chain
    replacement = store.get_replacement_for_job(int(job["id"]))
    assert replacement is not None
    new_job = store.get_remote_job(int(replacement["new_remote_job_id"]))
    assert new_job["state"] == "SUBMIT_INTENT"
    assert int(new_job["post_attempted"]) == 1

    # Restore a fully working create channel: the resumed click must not use
    # it at all -- a second submit attempt would push the count to 2.
    seed.adapter.submit = real_submit

    result = seed.rebuild(job, action_id=ACTION_D2)

    assert result["outcome"] == "REBUILD_SUBMIT_UNKNOWN"
    assert result["rebuild_resumed"] is True
    assert result["rebuild_action_id"] == ACTION_D1
    assert len(submit_calls) == 1  # never a second create attempt
    new_job = store.get_remote_job(int(replacement["new_remote_job_id"]))
    assert new_job["state"] == "SUBMIT_UNKNOWN"
    assert store.get_remote_job(int(job["id"]))["state"] == "SUPERSEDED"


# ---------------------------------------------------------------------------
# Crash E: SUBMIT_UNKNOWN outcome is durable; replay is informational.
# ---------------------------------------------------------------------------


def test_crash_e_submit_unknown_then_replay_is_informational(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)
    submit_calls: list[str] = []

    def unknown_submit(_prepared, *, timeout_seconds):
        submit_calls.append(str(timeout_seconds))
        return LegacySubmitOutcome(
            SubmitStatus.SUBMIT_UNKNOWN,
            diagnostic="legacy create-only completion is unknown",
        )

    seed.adapter.submit = unknown_submit

    first = seed.rebuild(job, action_id=ACTION_E1)

    assert first["outcome"] == "REBUILD_SUBMIT_UNKNOWN"
    assert store.get_rebuild_action(ACTION_E1)["phase"] == "SUBMIT_UNKNOWN"
    replacement = store.get_replacement_for_job(int(job["id"]))
    new_job = store.get_remote_job(int(replacement["new_remote_job_id"]))
    assert new_job["state"] == "SUBMIT_UNKNOWN"
    assert int(new_job["post_attempted"]) == 1

    second = seed.rebuild(job, action_id=ACTION_E2)

    assert second["outcome"] == "REBUILD_ALREADY_REPLACED"
    assert "rebuild_resumed" not in second  # informational replay: current shape
    assert second["new_remote_job_id"] == int(replacement["new_remote_job_id"])
    assert len(submit_calls) == 1


# ---------------------------------------------------------------------------
# Guard: an active INTENT action rejects a competing action id (§27-9).
# ---------------------------------------------------------------------------


def test_active_intent_action_blocks_a_competing_action_id(tmp_path, monkeypatch):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    store, job, _crawls = seed.seed(18)

    def exploding_lookup(_prepared, _task_id, _task_number, _timeout):
        raise KeyboardInterrupt("crash during precheck")

    with pytest.raises(KeyboardInterrupt):
        seed.rebuild(job, action_id=ACTION_F1, progress_lookup=exploding_lookup)
    assert store.get_rebuild_action(ACTION_F1)["phase"] == "INTENT"

    # Simulate the concurrent race: the pre-create read misses the row the
    # way a parallel click's insert would land between the read and the
    # transactional create check.
    original = DailyAuditStore.get_latest_rebuild_action_for_job
    reads = {"n": 0}

    def racing_read(self, old_job_id):
        reads["n"] += 1
        if reads["n"] == 1:
            return None
        return original(self, old_job_id)

    monkeypatch.setattr(DailyAuditStore, "get_latest_rebuild_action_for_job", racing_read)

    result = seed.rebuild(job, action_id=ACTION_F2)

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "REBUILD_ACTION_ACTIVE"
    assert result["existing_action_id"] == ACTION_F1
    assert store.get_rebuild_action(ACTION_F2) is None  # still exactly one row
    assert seed.stop_calls == []
    assert seed.runner_calls == []


def test_failed_action_never_blocks_a_fresh_protocol(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = None  # precheck UNKNOWN -> protocol FAILED
    store, job, _crawls = seed.seed(18)

    first = seed.rebuild(job, action_id=ACTION_F1)

    assert first["outcome"] == "REBUILD_REJECTED"
    assert first["error_code"] == "REMOTE_PROGRESS_UNKNOWN"
    failed = store.get_rebuild_action(ACTION_F1)
    assert failed["phase"] == "FAILED"
    assert failed["error_text"] == "REMOTE_PROGRESS_UNKNOWN"
    assert seed.stop_calls == []

    seed.remote_progress = seed.terminal_progress(18)
    second = seed.rebuild(job, action_id=ACTION_F2)

    assert second["outcome"] == "REBUILD_DISPATCHED"
    assert second["rebuild_action_id"] == ACTION_F2
    assert store.get_rebuild_action(ACTION_F1)["phase"] == "FAILED"
    assert store.get_rebuild_action(ACTION_F2)["phase"] == "DISPATCHED"
    latest = store.get_latest_rebuild_action_for_job(int(job["id"]))
    assert latest["action_id"] == ACTION_F2


# ---------------------------------------------------------------------------
# Store-level protocol guards (create / merge / supersede atomics).
# ---------------------------------------------------------------------------


def test_store_rebuild_action_protocol_guards(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(3)
    store, job, crawls = seed.seed(3)
    old_id = int(job["id"])
    replacement_name = "cnnc_current_opt_领先未来商城_22233344455566"

    lease = store.acquire_lease(BUSINESS_DATE, "protocol", 300.0)
    try:
        row = store.create_rebuild_action(
            lease,
            action_id="proto-1",
            old_remote_job_id=old_id,
            remote_identity=IDENTITY,
            replacement_task_name=replacement_name,
        )
        assert row["phase"] == "INTENT"
        assert json.loads(row["stop_evidence_json"]) == {}
        assert row["new_remote_job_id"] is None

        with pytest.raises(
            RemoteIntentConflict, match="an active rebuild action already exists"
        ):
            store.create_rebuild_action(
                lease,
                action_id="proto-2",
                old_remote_job_id=old_id,
                remote_identity=IDENTITY,
                replacement_task_name=replacement_name,
            )
        with pytest.raises(ValueError, match="three"):
            store.create_rebuild_action(
                lease,
                action_id="proto-3",
                old_remote_job_id=old_id,
                remote_identity="mall|only-two",
                replacement_task_name=replacement_name,
            )
        with pytest.raises(ValueError, match="leased daily run"):
            store.create_rebuild_action(
                lease,
                action_id="proto-4",
                old_remote_job_id=999999,
                remote_identity=IDENTITY,
                replacement_task_name=replacement_name,
            )

        # UNIQUE(action_id) fires only once no active protocol remains for
        # the id: fail a scratch protocol first, then re-create its id.
        scratch_job = store.prepare_remote_intent(
            lease,
            f"legacy:{MALL_KEY}:proto-scratch",
            "cnnc_current_opt_领先未来商城_99988877766655",
            "scratch-hash",
            mall_key=MALL_KEY,
            input_path="C:\\x\\s-input.xlsx",
            result_path="C:\\x\\s-result.xlsx",
            payload_json={"requests": [], "batch_state": "C:\\x\\BATCH_STATE.md"},
        )
        store.create_rebuild_action(
            lease,
            action_id="proto-scratch",
            old_remote_job_id=int(scratch_job["id"]),
            remote_identity="jd|900|RW-900",
            replacement_task_name=replacement_name,
        )
        store.update_rebuild_action_phase(lease, action_id="proto-scratch", phase="FAILED")
        with pytest.raises(RemoteIntentConflict, match="rebuild action id already exists"):
            store.create_rebuild_action(
                lease,
                action_id="proto-scratch",
                old_remote_job_id=int(scratch_job["id"]),
                remote_identity="jd|900|RW-900",
                replacement_task_name=replacement_name,
            )

        store.update_rebuild_action_phase(
            lease, action_id="proto-1", phase="STOP_SENT", stop_evidence={"a": 1}
        )
        store.update_rebuild_action_phase(
            lease,
            action_id="proto-1",
            phase="STOP_CONFIRMED",
            stop_evidence={"b": 2, "a": 9},
        )
        merged = json.loads(store.get_rebuild_action("proto-1")["stop_evidence_json"])
        assert merged == {"a": 9, "b": 2}  # merge never replaces prior evidence
        assert store.get_rebuild_action("proto-1")["phase"] == "STOP_CONFIRMED"

        eligible = [int(crawl["id"]) for crawl in crawls]
        superseded = store.supersede_remote_job_to_replacement(
            lease,
            old_job_id=old_id,
            replacement_logical_key=f"legacy:{MALL_KEY}:proto-new",
            replacement_task_name=replacement_name,
            replacement_payload_hash="proto-hash",
            replacement_input_path="C:\\x\\input.xlsx",
            replacement_result_path="C:\\x\\result.xlsx",
            replacement_payload_json={"requests": [], "batch_state": "C:\\x\\BATCH_STATE.md"},
            mall_key=MALL_KEY,
            action_id="proto-1",
            reason="crash-recovery store smoke",
            old_batch_number=OLD_TASK_NAME,
            new_batch_number=replacement_name,
            stop_evidence={"phase": "STOP_CONFIRMED"},
            eligible_crawl_ids=eligible,
            rebuild_action_id="proto-1",
        )
        new_job_id = int(superseded["new_job"]["id"])
        action = store.get_rebuild_action("proto-1")
        assert action["phase"] == "SUPERSEDED"  # advanced in the same transaction
        assert int(action["new_remote_job_id"]) == new_job_id

        with pytest.raises(
            RemoteIntentConflict, match="remote job already has a replacement"
        ):
            store.create_rebuild_action(
                lease,
                action_id="proto-5",
                old_remote_job_id=old_id,
                remote_identity=IDENTITY,
                replacement_task_name=replacement_name,
            )

        rollback_name = "cnnc_current_opt_领先未来商城_66655544433322"
        with pytest.raises(
            RemoteIntentConflict,
            match="rebuild action does not match the superseded job",
        ):
            store.supersede_remote_job_to_replacement(
                lease,
                old_job_id=new_job_id,  # action proto-1 belongs to the OLD job
                replacement_logical_key=f"legacy:{MALL_KEY}:proto-rollback",
                replacement_task_name=rollback_name,
                replacement_payload_hash="rollback-hash",
                replacement_input_path="C:\\x\\input2.xlsx",
                replacement_result_path="C:\\x\\result2.xlsx",
                replacement_payload_json={"requests": [], "batch_state": "C:\\x\\BATCH_STATE.md"},
                mall_key=MALL_KEY,
                action_id="proto-mismatch",  # fresh lineage id; only the
                # write-ahead binding is stale, so the mismatch guard fires
                reason="mismatch probe",
                old_batch_number=replacement_name,
                new_batch_number=rollback_name,
                stop_evidence={"phase": "STOP_CONFIRMED"},
                eligible_crawl_ids=eligible,
                rebuild_action_id="proto-1",
            )
        # The mismatched supersede rolled back completely: no stray job row.
        connection = sqlite3.connect(str(seed.state_path), timeout=5)
        try:
            stray = connection.execute(
                "SELECT COUNT(*) FROM remote_jobs WHERE task_name=?",
                (rollback_name,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert stray == 0
    finally:
        store.release_lease(lease)


# ---------------------------------------------------------------------------
# P2-A: PRE_WRITE_FAILED is a proven zero-write stop boundary -- the
# write-ahead STOP_SENT is withdrawn back to INTENT, never reconcile-locked.
# ---------------------------------------------------------------------------


def test_pre_write_failed_reverts_stop_sent_to_intent(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    store, job, _crawls = seed.seed(18)
    seed.stop_outcome = LegacyRestartOutcome(
        RestartStatus.PRE_WRITE_FAILED,
        diagnostic="auth refresh deadline exhausted",
    )

    first = seed.rebuild(job, action_id=ACTION_PWF_A)

    assert first["outcome"] == "REBUILD_REJECTED"
    assert first["error_code"] == "STOP_PRE_WRITE_FAILED"
    assert first["stop_status"] == "PRE_WRITE_FAILED"
    action = store.get_rebuild_action(ACTION_PWF_A)
    assert action is not None
    assert action["phase"] == "INTENT"  # write-ahead STOP_SENT withdrawn
    assert "pre-write" in str(action["error_text"])
    assert "auth refresh deadline exhausted" in str(action["error_text"])
    assert len(seed.stop_calls) == 1  # the stop attempt fired but wrote nothing
    assert store.get_replacement_for_job(int(job["id"])) is None
    assert seed.runner_calls == []  # no replacement was created


def test_pre_write_failed_then_click_retries_fresh_stop(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    store, job, _crawls = seed.seed(18)
    seed.stop_outcome = LegacyRestartOutcome(
        RestartStatus.PRE_WRITE_FAILED,
        diagnostic="auth refresh deadline exhausted",
    )
    first = seed.rebuild(job, action_id=ACTION_PWF_A)
    assert first["error_code"] == "STOP_PRE_WRITE_FAILED"
    persisted_name = str(store.get_rebuild_action(ACTION_PWF_A)["replacement_task_name"])

    # A later click with a recovered auth channel resumes the same action
    # from INTENT: fresh precheck, fresh stop, same deterministic name.
    seed.stop_outcome = LegacyRestartOutcome(
        RestartStatus.STOPPED,
        disable_confirmed=True,
        observed_enabled=False,
        diagnostic="exact task disabled with confirmed evidence",
    )
    second = seed.rebuild(job, action_id=ACTION_PWF_B)

    assert second["outcome"] == "REBUILD_DISPATCHED"
    assert second["rebuild_action_id"] == ACTION_PWF_A  # resume owns the protocol
    assert second["new_task_name"] == persisted_name
    assert len(seed.stop_calls) == 2  # one fresh stop, zero double-stop risk
    assert store.get_rebuild_action(ACTION_PWF_A)["phase"] == "DISPATCHED"
    assert store.get_rebuild_action(ACTION_PWF_B) is None  # exactly one row


def test_disable_unknown_keeps_stop_sent_reconcile_only(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    store, job, _crawls = seed.seed(18)
    seed.stop_outcome = LegacyRestartOutcome(
        RestartStatus.DISABLE_UNKNOWN,
        observed_enabled=True,
        diagnostic="disable request outcome unknown",
    )

    first = seed.rebuild(job, action_id=ACTION_PWF_C)

    assert first["outcome"] == "REBUILD_REJECTED"
    assert first["error_code"] == "OLD_TASK_STOP_UNCONFIRMED"
    assert store.get_rebuild_action(ACTION_PWF_C)["phase"] == "STOP_SENT"

    def live_lookup(_prepared, _task_id, _task_number, _timeout):
        return {"enabled": True, "progress": dict(seed.active_progress(18))}

    second = seed.rebuild(job, action_id=ACTION_PWF_D, progress_lookup=live_lookup)

    assert second["outcome"] == "REBUILD_REJECTED"
    assert second["error_code"] == "OLD_TASK_STOP_UNCONFIRMED"
    assert len(seed.stop_calls) == 1  # reconcile-only: never a second disable
    assert store.get_rebuild_action(ACTION_PWF_C)["phase"] == "STOP_SENT"
    assert store.get_replacement_for_job(int(job["id"])) is None


def test_stop_timeout_escape_keeps_stop_sent(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    store, job, _crawls = seed.seed(18)

    def timeout_stop(_prepared, task_id, _task_number, _timeout):
        seed.stop_calls.append({"task_id": str(task_id), "timeout": True})
        raise TimeoutError("deadline")

    with pytest.raises(TimeoutError):
        seed.rebuild(job, action_id=ACTION_PWF_E, stop_callable=timeout_stop)

    assert store.get_rebuild_action(ACTION_PWF_E)["phase"] == "STOP_SENT"

    def live_lookup(_prepared, _task_id, _task_number, _timeout):
        return {"enabled": True, "progress": dict(seed.active_progress(18))}

    result = seed.rebuild(job, action_id=ACTION_PWF_F, progress_lookup=live_lookup)

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "OLD_TASK_STOP_UNCONFIRMED"
    assert len(seed.stop_calls) == 1  # the escape stays reconcile-only
    assert store.get_rebuild_action(ACTION_PWF_E)["phase"] == "STOP_SENT"


# ---------------------------------------------------------------------------
# P2-B: a replacement task-name conflict must not dead-end a stop-confirmed
# action; an ambiguous durable phase is still never released.
# ---------------------------------------------------------------------------


def _seed_name_conflict(tmp_path):
    """Seed one old job plus a remote_jobs row reserving the tnc-a name."""
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    store, job, _crawls = seed.seed(18)
    reserved = _replacement_task_name(OLD_TASK_NAME, ACTION_TNC_A, MALL_NAME)
    lease = store.acquire_lease(BUSINESS_DATE, "tnc-reserve", 300.0)
    try:
        store.prepare_remote_intent(
            lease,
            f"legacy:{MALL_KEY}:reserved-{ACTION_TNC_A}",
            reserved,
            "reserved-hash",
            mall_key=MALL_KEY,
            input_path="C:\\x\\r-input.xlsx",
            result_path="C:\\x\\r-result.xlsx",
            payload_json={"requests": [], "batch_state": "C:\\x\\BATCH_STATE.md"},
        )
    finally:
        store.release_lease(lease)
    return seed, store, job, reserved


def test_task_name_conflict_fails_action_when_stop_confirmed(tmp_path):
    seed, store, job, reserved = _seed_name_conflict(tmp_path)

    result = seed.rebuild(job, action_id=ACTION_TNC_A)

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "TASK_NAME_CONFLICT"
    assert result["action_failed"] is True
    action = store.get_rebuild_action(ACTION_TNC_A)
    assert action["phase"] == "FAILED"  # released: stop was durably confirmed
    assert "replacement task name conflict" in str(action["error_text"])
    assert store.get_replacement_for_job(int(job["id"])) is None
    assert len(seed.stop_calls) == 1  # the stop confirmed before the conflict
    assert seed.runner_calls == []


def test_task_name_conflict_then_new_action_rebuilds(tmp_path):
    seed, store, job, reserved = _seed_name_conflict(tmp_path)
    first = seed.rebuild(job, action_id=ACTION_TNC_A)
    assert first["error_code"] == "TASK_NAME_CONFLICT"
    assert seed.stop_disable_writes == 1  # the one confirmed disable

    # The released action lets a fresh action id derive a brand-new name; the
    # exact task is already disabled, so the second protocol is zero-write.
    seed.stop_outcome = LegacyRestartOutcome(
        RestartStatus.EXTERNALLY_DISABLED,
        observed_enabled=False,
        diagnostic="exact task is already disabled elsewhere",
    )
    second = seed.rebuild(job, action_id=ACTION_TNC_B)

    assert second["outcome"] == "REBUILD_DISPATCHED"
    assert second["new_task_name"] != reserved
    assert seed.stop_disable_writes == 1  # EXTERNALLY_DISABLED writes nothing
    assert store.get_rebuild_action(ACTION_TNC_B)["phase"] == "DISPATCHED"
    assert store.get_rebuild_action(ACTION_TNC_A)["phase"] == "FAILED"
    assert store.get_replacement_for_job(int(job["id"])) is not None


def test_failed_conflict_action_same_id_click_starts_fresh(tmp_path):
    seed, store, job, _reserved = _seed_name_conflict(tmp_path)
    first = seed.rebuild(job, action_id=ACTION_TNC_A)
    assert first["error_code"] == "TASK_NAME_CONFLICT"

    retry = seed.rebuild(job, action_id=ACTION_TNC_A)  # same id click

    # The FAILED row never trips the active guard: the replayed id is caught
    # by the id-level uniqueness instead, with zero further remote writes.
    assert retry["outcome"] == "REBUILD_REJECTED"
    assert retry["error_code"] != "REBUILD_ACTION_ACTIVE"
    assert "already exists" in str(retry["reason"])
    assert store.get_rebuild_action(ACTION_TNC_A)["phase"] == "FAILED"
    assert len(seed.stop_calls) == 1


def test_task_name_conflict_at_stop_sent_never_releases(tmp_path, monkeypatch):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.active_progress(18)
    store, job, _crawls = seed.seed(18)

    original_update = DailyAuditStore.update_rebuild_action_phase

    def suppressing_update(
        self, token, *, action_id, phase, stop_evidence=None,
        new_remote_job_id=None, error_text=None,
    ):
        if phase == "STOP_CONFIRMED":
            return None  # the confirm never lands: durable stays STOP_SENT
        return original_update(
            self, token, action_id=action_id, phase=phase,
            stop_evidence=stop_evidence, new_remote_job_id=new_remote_job_id,
            error_text=error_text,
        )

    def conflicting_supersede(self, *args, **kwargs):
        raise RemoteIntentConflict("replacement task name is already reserved")

    monkeypatch.setattr(
        DailyAuditStore, "update_rebuild_action_phase", suppressing_update)
    monkeypatch.setattr(
        DailyAuditStore, "supersede_remote_job_to_replacement", conflicting_supersede)

    result = seed.rebuild(job, action_id=ACTION_TNC_C)

    assert result["outcome"] == "REBUILD_REJECTED"
    assert result["error_code"] == "TASK_NAME_CONFLICT"
    assert result["action_failed"] is False  # ambiguous phase: fail closed
    action = store.get_rebuild_action(ACTION_TNC_C)
    assert action["phase"] == "STOP_SENT"  # not FAILED, not released
    assert len(seed.stop_calls) == 1


def test_conflict_with_existing_replacement_still_already_replaced(tmp_path):
    seed = _CrashSeed(tmp_path)
    seed.remote_progress = seed.terminal_progress(18)
    store, job, _crawls = seed.seed(18)

    first = seed.rebuild(job, action_id=ACTION_TNC_E1)

    assert first["outcome"] == "REBUILD_DISPATCHED"

    second = seed.rebuild(job, action_id=ACTION_TNC_E2)

    assert second["outcome"] == "REBUILD_ALREADY_REPLACED"  # idempotent replay
    assert second["new_remote_job_id"] == first["new_remote_job_id"]
    assert store.get_replacement_for_job(int(job["id"])) is not None
