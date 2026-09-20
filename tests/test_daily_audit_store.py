"""Offline contract tests for the incremental audit checkpoint store."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.services.daily_audit_store import (
    DailyAuditStore,
    FINALIZATION_INTENT_KEY,
    FrozenSnapshotConflict,
    LeaseUnavailable,
    ProcessLock,
    RemoteIntentConflict,
    StaleLease,
)


@pytest.fixture
def store(tmp_path):
    return DailyAuditStore(tmp_path / "daily-audit.sqlite3")


def _lease(store, date="2026-08-11", owner="tick-a"):
    store.ensure_run(date)
    return store.acquire_lease(date, owner, ttl_seconds=30)


def _row(v="V-1", sku="SKU-1", url="https://shop.example/p/1", **extra):
    return {"vCommodityId": v, "skuId": sku, "extLink": url, "price": "12.30", **extra}


def test_schema_has_only_checkpoint_tables_and_dates_are_isolated(store):
    first = store.ensure_run("2026-08-11")
    again = store.ensure_run("2026-08-11")
    second = store.ensure_run("2026-08-12")

    assert first["id"] == again["id"]
    assert first["id"] != second["id"]
    # P1-2 adds the durable write-ahead rebuild protocol table
    # (remote_job_rebuild_actions) next to the checkpoint tables.
    assert {"daily_runs", "snapshots", "snapshot_members", "audit_items", "stage_work", "crawl_work", "local_fallback_work", "remote_jobs", "remote_job_replacements", "remote_job_rebuild_actions"} == store.table_names()


def test_run_scope_and_cutoff_are_canonical_and_conflicts_fail_closed(store):
    run = store.ensure_run("2026-08-11", cutoff_at="2026-08-11T10:00:00+00:00")
    assert run["cutoff_at"] == "2026-08-11T10:00:00+00:00"
    assert store.ensure_run("2026-08-11", cutoff_at="2026-08-11T10:00:00+00:00")["id"] == run["id"]
    with pytest.raises(ValueError, match="cutoff"):
        store.ensure_run("2026-08-11", cutoff_at="2026-08-11T11:00:00+00:00")
    with pytest.raises(ValueError, match="tenant"):
        store.ensure_run("2026-08-11", tenant_id=1)
    with pytest.raises(ValueError, match="business_date"):
        store.ensure_run("11-08-2026")
    store.ensure_run("2026-08-12")
    assert store.ensure_run("2026-08-12", cutoff_at="2026-08-12T10:00:00+00:00")["cutoff_at"] == "2026-08-12T10:00:00+00:00"


def test_process_lock_is_nonblocking_across_instances(tmp_path):
    path = tmp_path / "tick.lock"
    first = ProcessLock(path)
    second = ProcessLock(path)

    first.acquire()
    with pytest.raises(LeaseUnavailable):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_process_lock_owner_metadata_is_auxiliary_and_removed_on_clean_release(tmp_path):
    path = tmp_path / "tick.lock"
    lock = ProcessLock(path).acquire(owner={
        "business_date": "2026-08-11",
        "pid": 123,
        "command": "drain-only",
        "started_at": "2026-08-11T20:30:00+08:00",
        "invocation": "manual",
    })

    status = ProcessLock.inspect(path)
    assert status["active"] is True
    assert status["pid"] == 123
    assert status["command"] == "drain-only"
    lock.release()

    assert ProcessLock.inspect(path)["active"] is False
    assert not lock.metadata_path.exists()


def test_process_lock_is_reentrant_for_one_command_lifecycle(tmp_path):
    lock = ProcessLock(tmp_path / "tick.lock").acquire()
    lock.acquire()
    lock.release()

    with pytest.raises(LeaseUnavailable):
        ProcessLock(lock.path).acquire()
    lock.release()
    ProcessLock(lock.path).acquire().release()


def test_process_lock_releases_after_exception_and_keyboard_interrupt(tmp_path):
    path = tmp_path / "tick.lock"
    with pytest.raises(RuntimeError):
        with ProcessLock(path):
            raise RuntimeError("boom")
    ProcessLock(path).acquire().release()

    with pytest.raises(KeyboardInterrupt):
        with ProcessLock(path):
            raise KeyboardInterrupt
    ProcessLock(path).acquire().release()


def test_stale_owner_metadata_never_blocks_a_new_owner(tmp_path):
    path = tmp_path / "tick.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0")
    metadata = path.with_name(f"{path.name}.owner.json")
    metadata.write_text(json.dumps({"pid": 999999, "command": "stale"}), encoding="utf-8")

    assert ProcessLock.inspect(path)["active"] is False
    lock = ProcessLock(path).acquire(owner={"pid": 321, "command": "tick"})
    assert ProcessLock.inspect(path)["command"] == "tick"
    lock.release()


def test_os_releases_process_lock_after_forced_process_termination(tmp_path):
    path = tmp_path / "tick.lock"
    code = "\n".join((
        "import sys, time",
        "from app.services.daily_audit_store import ProcessLock",
        "lock = ProcessLock(sys.argv[1]).acquire(owner={'pid': 1, 'command': 'child'})",
        "print('READY', flush=True)",
        "time.sleep(60)",
    ))
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        assert ProcessLock.inspect(path)["active"] is True
    finally:
        child.terminate()
        child.wait(timeout=10)

    ProcessLock(path).acquire(owner={"pid": 2, "command": "next"}).release()


def test_lease_overlap_expiry_and_fencing(store):
    first = _lease(store, owner="one")
    with pytest.raises(LeaseUnavailable):
        store.acquire_lease("2026-08-11", "two", ttl_seconds=30)

    # Expiry is deterministic in tests; no sleep loop is needed.
    store.force_lease_expiry_for_test("2026-08-11")
    second = store.acquire_lease("2026-08-11", "two", ttl_seconds=30)
    assert second.generation > first.generation
    with pytest.raises(StaleLease):
        store.ingest_snapshot(first, [_row()])
    store.release_lease(first)  # release of an old token is deliberately harmless
    assert store.get_run("2026-08-11")["lease_owner"] == "two"


def test_lease_renew_and_explicit_locked_takeover(store, tmp_path):
    first = _lease(store, owner="one")
    renewed = store.renew_lease(first, ttl_seconds=30)
    assert renewed.generation == first.generation
    lock = ProcessLock(tmp_path / "process.lock").acquire()
    second = store.acquire_lease(
        "2026-08-11", "two", ttl_seconds=30, allow_unexpired_takeover=True, process_lock=lock
    )
    lock.release()
    assert second.generation > first.generation
    with pytest.raises(StaleLease):
        store.renew_lease(first, ttl_seconds=30)


def test_snapshot_preserves_order_duplicates_and_diffs_changes(store):
    lease = _lease(store)
    first = store.ingest_snapshot(lease, [_row(), _row(), _row("V-2", "SKU-2")])
    assert first["status_counts"] == {"NEW": 3}
    assert first["member_count"] == 3
    assert [member["row_ordinal"] for member in store.snapshot_members(first["snapshot"]["id"])] == [0, 1, 2]
    assert [member["duplicate_ordinal"] for member in store.snapshot_members(first["snapshot"]["id"])] == [0, 1, 0]

    same = store.ingest_snapshot(lease, [_row(), _row(), _row("V-2", "SKU-2")])
    assert same["reused"] is True
    changed = store.ingest_snapshot(lease, [_row(price="13.00"), _row("V-2", "SKU-2"), _row("V-3", "SKU-3")])
    assert changed["status_counts"] == {"CHANGED": 1, "UNCHANGED": 1, "NEW": 1}


def test_legacy_snapshot_hash_constraint_migrates_without_losing_members(tmp_path):
    path = tmp_path / "legacy-snapshots.sqlite"
    original = DailyAuditStore(path)
    lease = _lease(original)
    first = original.ingest_snapshot(
        lease, [_row()], observed_at="2026-08-11T09:59:00+00:00"
    )
    original.release_lease(lease)

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """CREATE TABLE snapshots_legacy (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES daily_runs(id) ON DELETE CASCADE,
                ordered_hash TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                headers_json TEXT NOT NULL DEFAULT '[]',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(run_id, ordered_hash)
            )"""
        )
        connection.execute("INSERT INTO snapshots_legacy SELECT * FROM snapshots")
        connection.execute("DROP TABLE snapshots")
        connection.execute("ALTER TABLE snapshots_legacy RENAME TO snapshots")
        connection.commit()
    finally:
        connection.close()

    migrated = DailyAuditStore(path)
    lease = migrated.acquire_lease("2026-08-11", "after-migration", ttl_seconds=30)
    second = migrated.ingest_snapshot(
        lease, [_row()], observed_at="2026-08-11T10:01:00+00:00"
    )
    snapshots = migrated.list_snapshots("2026-08-11")

    assert [snapshot["observed_at"] for snapshot in snapshots] == [
        "2026-08-11T09:59:00+00:00",
        "2026-08-11T10:01:00+00:00",
    ]
    assert migrated.snapshot_members(int(first["snapshot"]["id"]))[0]["row_ordinal"] == 0
    assert second["reused"] is False


def test_legacy_store_adds_recovery_and_local_fallback_schema_idempotently(tmp_path):
    path = tmp_path / "legacy-local-fallback.sqlite3"
    original = DailyAuditStore(path)
    lease = _lease(original)
    job = original.prepare_remote_intent(
        lease, "legacy-job", "legacy-task", "legacy-payload", mall_key="comix",
    )
    crawl = original.ensure_crawl_work(
        lease, "comix", "SKU-1", "https://comix.example/item", "legacy-v1", "fp-1",
        source_id="DOC-1",
    )
    original.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    original.release_lease(lease)

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE local_fallback_work")
        connection.execute("ALTER TABLE remote_jobs DROP COLUMN recovery_evidence_json")
        connection.execute("ALTER TABLE remote_jobs DROP COLUMN recovery_attempts")

    migrated = DailyAuditStore(path)
    reopened = DailyAuditStore(path)
    with sqlite3.connect(path) as connection:
        remote_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(remote_jobs)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        retained = connection.execute(
            "SELECT logical_key, recovery_attempts FROM remote_jobs WHERE id=?",
            (int(job["id"]),),
        ).fetchone()

    assert migrated.path == reopened.path == path
    assert {"recovery_attempts", "recovery_evidence_json"} <= remote_columns
    assert "local_fallback_work" in tables
    assert "ux_local_fallback_active_url" in indexes
    assert retained == ("legacy-job", 0)


def test_customer_fingerprint_is_part_of_audit_version_and_same_snapshot_is_unchanged(store):
    lease = _lease(store)
    row = _row()
    first = store.ingest_snapshot(
        lease, [row], source_fingerprints=["source-a"], customer_fingerprints=["customer-a"]
    )
    rerun = store.ingest_snapshot(
        lease, [row], source_fingerprints=["source-a"], customer_fingerprints=["customer-a"]
    )
    assert rerun["reused"] is True
    assert rerun["status_counts"] == {"UNCHANGED": 1}
    assert rerun["members"][0]["change_kind"] == "UNCHANGED"
    changed = store.ingest_snapshot(
        lease, [row], source_fingerprints=["source-a"], customer_fingerprints=["customer-b"]
    )
    assert changed["status_counts"] == {"CHANGED": 1}
    assert first["members"][0]["audit_item_id"] != changed["members"][0]["audit_item_id"]


def test_stage_reuses_success_resets_on_fingerprint_and_recovers_claim(store):
    lease = _lease(store)
    item_id = store.ingest_snapshot(lease, [_row()])["members"][0]["audit_item_id"]
    pending = store.ensure_stage_work(lease, item_id, "MODEL", "model-fp-a")
    claimed = store.claim_stage_work(lease, pending["id"], max_attempts=3, max_elapsed_seconds=300)
    assert claimed["attempts"] == 1
    store.complete_stage_work(lease, pending["id"], result={"isMatch": True})
    assert store.ensure_stage_work(lease, item_id, "MODEL", "model-fp-a")["state"] == "SUCCESS"
    reset = store.ensure_stage_work(lease, item_id, "MODEL", "model-fp-b")
    assert reset["state"] == "PENDING"
    claim = store.claim_stage_work(lease, reset["id"], max_attempts=3, max_elapsed_seconds=300)
    assert claim is not None
    assert store.recover_claimed_stage_work(lease) == 1
    assert store.get_stage_work(reset["id"])["state"] == "RETRYABLE_FAILED"
    claim = store.claim_stage_work(lease, reset["id"], max_attempts=3, max_elapsed_seconds=300)
    store.fail_stage_work(lease, reset["id"], elapsed_seconds=10, error="temporary", retryable=True, max_attempts=3, max_elapsed_seconds=300)
    assert store.get_stage_work(reset["id"])["state"] == "RETRYABLE_FAILED"


def test_remote_intent_is_durable_conflict_and_unknown_submit_never_reclaims(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(lease, "mall-A:hash", "daily-20260811-mall-A", "payload-a")
    assert store.prepare_remote_intent(lease, "mall-A:hash", "daily-20260811-mall-A", "payload-a")["id"] == job["id"]
    with pytest.raises(RemoteIntentConflict):
        store.prepare_remote_intent(lease, "mall-A:hash", "daily-20260811-mall-A", "payload-b")
    assert store.claim_remote_submission(lease, job["id"])["post_attempted"] is True
    assert store.claim_remote_submission(lease, job["id"]) is None
    store.mark_submit_unknown(lease, job["id"], "client timed out")
    assert store.get_remote_job(job["id"])["state"] == "SUBMIT_UNKNOWN"
    assert store.claim_remote_submission(lease, job["id"]) is None
    store.reconcile_remote_job(lease, job["id"], remote_identity="remote-99", state="SUBMITTED")
    assert store.get_remote_job(job["id"])["remote_identity"] == "remote-99"


def test_submit_unknown_harvest_without_new_error_preserves_original_diagnostic(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(
        lease,
        "mall-submit-unknown:hash",
        "daily-submit-unknown",
        "payload-submit-unknown",
    )
    store.claim_remote_submission(lease, int(job["id"]))
    store.mark_submit_unknown(
        lease,
        int(job["id"]),
        "prewarm dispatch submit unknown: ORIGINAL_REASON",
    )

    recorded = store.record_harvest(
        lease,
        int(job["id"]),
        elapsed_seconds=1.0,
        max_attempts=4,
        max_elapsed_seconds=300.0,
        error=None,
    )

    assert recorded["state"] == "SUBMIT_UNKNOWN"
    assert recorded["error_text"] == "prewarm dispatch submit unknown: ORIGINAL_REASON"


def test_submit_unknown_found_reconciliation_clears_stale_submit_diagnostic(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(
        lease,
        "mall-submit-found:hash",
        "daily-submit-found",
        "payload-submit-found",
    )
    store.claim_remote_submission(lease, int(job["id"]))
    store.mark_submit_unknown(
        lease,
        int(job["id"]),
        "prewarm dispatch submit unknown: ORIGINAL_REASON",
    )

    reconciled = store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity="mall-submit-found|task-id|task-number",
        state="PRIORITY_PENDING",
    )

    assert reconciled["state"] == "PRIORITY_PENDING"
    assert reconciled["error_text"] is None


def test_confirmed_submit_waits_in_priority_pending_before_remote_progress(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(lease, "mall-A:hash", "daily-20260811-mall-A", "payload-a")
    store.claim_remote_submission(lease, job["id"])
    store.mark_submit_unknown(lease, job["id"], "priority failed after create")

    confirmed = store.reconcile_remote_job(
        lease,
        job["id"],
        remote_identity="mall-A|task-id|task-number",
        state="PRIORITY_PENDING",
    )

    assert confirmed["state"] == "PRIORITY_PENDING"
    assert store.claim_remote_submission(lease, job["id"]) is None
    with pytest.raises(RemoteIntentConflict):
        store.reconcile_remote_job(
            lease,
            job["id"],
            remote_identity="mall-A|task-id|task-number",
            state="COMPLETED",
        )


def test_explicit_manual_priority_requeue_preserves_remote_identity(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(lease, "mall-A:hash", "daily-20260811-mall-A", "payload-a")
    store.claim_remote_submission(lease, job["id"])
    store.mark_submit_unknown(lease, job["id"], "created but priority failed")
    store.reconcile_remote_job(
        lease,
        job["id"],
        remote_identity="mall-A|task-id|task-number",
        state="PRIORITY_PENDING",
    )
    store.mark_remote_manual(lease, job["id"], "invalid_task_name_time_marker")

    with pytest.raises(RemoteIntentConflict):
        store.requeue_manual_priority_job(
            lease,
            job["id"],
            remote_identity="mall-A|other-id|task-number",
        )
    recovered = store.requeue_manual_priority_job(
        lease,
        job["id"],
        remote_identity="mall-A|task-id|task-number",
    )

    assert recovered["state"] == "PRIORITY_PENDING"
    assert recovered["remote_identity"] == "mall-A|task-id|task-number"
    assert recovered["post_attempted"] == 1
    assert recovered["error_text"] is None


def test_explicit_manual_harvest_requeue_preserves_remote_identity(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(
        lease,
        "mall-A:hash",
        "daily-20260811-mall-A",
        "payload-a",
        result_path="C:/result.xlsx",
    )
    store.claim_remote_submission(lease, job["id"])
    store.reconcile_remote_job(
        lease,
        job["id"],
        remote_identity="mall-A|task-id|task-number",
        state="SUBMITTED",
    )
    store.mark_remote_manual(lease, job["id"], "export validation failed")

    with pytest.raises(RemoteIntentConflict):
        store.requeue_manual_harvest_job(
            lease,
            job["id"],
            remote_identity="mall-A|other-id|task-number",
        )
    recovered = store.requeue_manual_harvest_job(
        lease,
        job["id"],
        remote_identity="mall-A|task-id|task-number",
    )

    assert recovered["state"] == "SUBMITTED"
    assert recovered["remote_identity"] == "mall-A|task-id|task-number"
    assert recovered["post_attempted"] == 1
    assert recovered["error_text"] is None


def test_remote_crash_recovery_and_query_apis_are_fenced(store):
    lease = _lease(store)
    item_id = store.ingest_snapshot(lease, [_row()])["members"][0]["audit_item_id"]
    work = store.ensure_stage_work(lease, item_id, "DETAIL", "detail-a")
    job = store.prepare_remote_intent(
        lease, "mall-A:hash", "task-A", "payload-a", mall_key="mall-A", input_path="C:/in.xlsx", payload_json={"a": 1}
    )
    store.claim_remote_submission(lease, job["id"])
    assert store.recover_ambiguous_remote_submissions(lease) == 1
    assert store.get_remote_job(job["id"])["state"] == "SUBMIT_UNKNOWN"
    assert store.list_remote_jobs(lease, states={"SUBMIT_UNKNOWN"})[0]["id"] == job["id"]
    assert store.list_stage_work(lease, stage="DETAIL")[0]["id"] == work["id"]
    crawl = store.ensure_crawl_work(lease, "mall-A", "SKU-1", "https://example/p", "legacy-v1", "crawl-a")
    store.link_crawl_work_to_remote_job(lease, crawl["id"], job["id"])
    assert store.advance_crawl_work_for_job(lease, job["id"], "RUNNING") == 1
    assert store.complete_crawl_work(lease, crawl["id"], {"price": "1"})["state"] == "SUCCESS"
    store.update_daily_metrics(lease, {"today_discovered": 1})
    assert store.get_run("2026-08-11")["metrics_json"] == '{"today_discovered":1}'
    store.force_lease_expiry_for_test("2026-08-11")
    with pytest.raises(StaleLease):
        store.list_stage_work(lease)
    with pytest.raises(StaleLease):
        store.update_daily_metrics(lease, {"today_discovered": 2})


def test_crawl_identity_is_exact_and_many_rows_can_link_one_job(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(lease, "mall-A:hash", "task-A", "payload-a")
    a = store.ensure_crawl_work(lease, "mall-A", "SKU-1", " https://example/p ", "legacy-v1", "crawl-a")
    same = store.ensure_crawl_work(lease, "mall-A", "SKU-1", "https://example/p", "legacy-v1", "crawl-a")
    b = store.ensure_crawl_work(lease, "mall-A", "SKU-2", "https://example/p", "legacy-v1", "crawl-b")
    other_source = store.ensure_crawl_work(
        lease, "mall-A", "SKU-1", "https://example/p", "legacy-v1", "crawl-a", source_id="source-2"
    )
    assert a["id"] == same["id"]
    assert a["id"] != other_source["id"]
    store.link_crawl_work_to_remote_job(lease, a["id"], job["id"])
    store.link_crawl_work_to_remote_job(lease, b["id"], job["id"])
    assert {row["remote_job_id"] for row in store.crawl_work_for_job(job["id"])} == {job["id"]}


def test_new_source_inherits_terminal_exact_backend_pair_without_resubmit(store):
    lease = _lease(store)
    failed = store.ensure_crawl_work(
        lease,
        "mall-A",
        "SKU-1",
        "https://example/p",
        "legacy-v1",
        "crawl-a",
        source_id="source-a",
    )
    store.fail_crawl_work(lease, int(failed["id"]), "export missing exact pair", retryable=False)
    later = store.ensure_crawl_work(
        lease,
        "mall-A",
        "SKU-1",
        "https://example/p",
        "legacy-v1",
        "crawl-b",
        source_id="source-b",
    )

    reused = store.reuse_successful_crawl_work(lease, int(later["id"]))

    assert reused is not None
    assert reused["state"] == "TERMINAL_FAILED"
    assert reused["error_text"] == "export missing exact pair"
    assert reused["remote_job_id"] is None


def test_completed_export_can_resolve_exact_terminal_crawl_as_no_result(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(
        lease,
        "mall-A:hash",
        "daily-20260811-mall-A",
        "payload-a",
        result_path="C:/exports/mall-A-result.xlsx",
    )
    store.claim_remote_submission(lease, int(job["id"]))
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity="mall-A|task-id|task-number",
        state="COMPLETED",
        result={"https://example/other": {"price": "12.30"}},
    )
    crawl = store.ensure_crawl_work(
        lease,
        "mall-A",
        "SKU-1",
        "https://example/p",
        "legacy-v1",
        "crawl-a",
        source_id="V-1",
    )
    store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    original_error = "legacy export missing safe skuId / exact URL result"
    store.fail_crawl_work(lease, int(crawl["id"]), original_error, retryable=False)

    resolved = store.complete_crawl_work_without_result(
        lease,
        int(crawl["id"]),
        reason=original_error,
    )
    repeated = store.complete_crawl_work_without_result(
        lease,
        int(crawl["id"]),
        reason=original_error,
    )

    evidence = json.loads(resolved["result_json"])
    assert resolved["state"] == "NO_RESULT"
    assert resolved["error_text"] == original_error
    assert evidence == {
        "evidence_type": "EXPORTED_NO_MATCHING_ROW",
        "no_result_reason": original_error,
        "page_price_source": "招商局（无采集结果）",
        "page_price_status": "NO_RESULT",
        "page_price_value": None,
        "remote_identity": "mall-A|task-id|task-number",
        "remote_state": "COMPLETED",
        "result_path": "C:/exports/mall-A-result.xlsx",
    }
    assert repeated == resolved


def test_no_result_requires_exact_completed_export_evidence(store):
    lease = _lease(store)
    crawl = store.ensure_crawl_work(
        lease,
        "mall-A",
        "SKU-1",
        "https://example/p",
        "legacy-v1",
        "crawl-a",
        source_id="V-1",
    )
    store.fail_crawl_work(
        lease,
        int(crawl["id"]),
        "legacy export missing safe skuId / exact URL result",
        retryable=False,
    )

    with pytest.raises(RemoteIntentConflict, match="completed export evidence"):
        store.complete_crawl_work_without_result(
            lease,
            int(crawl["id"]),
            reason="legacy export missing safe skuId / exact URL result",
        )


def test_freeze_and_finalization_are_idempotent_and_conflicts_are_rejected(store):
    lease = _lease(store)
    first = store.ingest_snapshot(lease, [_row()])["snapshot"]
    second = store.ingest_snapshot(lease, [_row("V-2", "SKU-2")])["snapshot"]
    assert store.freeze_snapshot(lease, first["id"])["frozen_snapshot_id"] == first["id"]
    assert store.freeze_snapshot(lease, first["id"])["frozen_snapshot_id"] == first["id"]
    with pytest.raises(FrozenSnapshotConflict):
        store.ingest_snapshot(lease, [_row("V-3", "SKU-3")])
    with pytest.raises(FrozenSnapshotConflict):
        store.freeze_snapshot(lease, second["id"])
    prepared = store.prepare_finalization(lease, "C:/out.xlsx")
    assert prepared["state"] == "FINALIZING"
    assert prepared["final_output_path"] == "C:/out.xlsx"
    assert store.prepare_finalization(lease, "C:/out.xlsx")["id"] == prepared["id"]
    with pytest.raises(FrozenSnapshotConflict):
        store.prepare_finalization(lease, "C:/other.xlsx")
    with pytest.raises(FrozenSnapshotConflict, match="commit_finalization"):
        store.mark_finalized(lease, "C:/out.xlsx", "manifest-a")
    assert store.get_run("2026-08-11")["state"] == "FINALIZING"
    final = store.commit_finalization(lease, "C:/out.xlsx", "manifest-a", {
        "finalized_rows": 1,
        "final_missing_page_price_rows": 0,
        "final_frozen_missing_page_price_rows": 0,
        "final_failed_customer_link_rows": 0,
        "final_appended_failed_customer_link_rows": 0,
        "final_page_price_required_rows": 0,
    })
    assert store.mark_finalized(lease, "C:/out.xlsx", "manifest-a")["id"] == final["id"]
    with pytest.raises(FrozenSnapshotConflict):
        store.mark_finalized(lease, "C:/other.xlsx", "manifest-b")


def test_atomic_final_commit_is_idempotent_and_rejects_metric_conflicts(store):
    lease = _lease(store)
    snapshot = store.ingest_snapshot(lease, [_row()])["snapshot"]
    store.freeze_snapshot(lease, snapshot["id"])
    store.prepare_finalization(lease, "C:/out.xlsx")
    metrics = {
        "finalized_rows": 1,
        "final_missing_page_price_rows": 75,
        "final_frozen_missing_page_price_rows": 7,
        "final_failed_customer_link_rows": 68,
        "final_appended_failed_customer_link_rows": 68,
        "final_page_price_required_rows": 7,
    }

    with pytest.raises(ValueError, match="missing required keys"):
        store.commit_finalization(lease, "C:/out.xlsx", "manifest-a", {"finalized_rows": 1})
    with pytest.raises(ValueError, match="force finalization markers"):
        store.commit_finalization(lease, "C:/out.xlsx", "manifest-a", {**metrics, "forced": True})
    assert store.get_run("2026-08-11")["state"] == "FINALIZING"

    first = store.commit_finalization(lease, "C:/out.xlsx", "manifest-a", metrics)
    assert first["state"] == "FINALIZED"
    assert first["final_manifest_hash"] == "manifest-a"
    assert all(json.loads(first["metrics_json"])[key] == value for key, value in metrics.items())
    assert store.commit_finalization(lease, "C:/out.xlsx", "manifest-a", metrics) == first
    with pytest.raises(FrozenSnapshotConflict):
        store.commit_finalization(lease, "C:/out.xlsx", "manifest-b", metrics)
    with pytest.raises(FrozenSnapshotConflict):
        store.commit_finalization(lease, "C:/other.xlsx", "manifest-a", metrics)
    with pytest.raises(FrozenSnapshotConflict):
        store.commit_finalization(
            lease, "C:/out.xlsx", "manifest-a",
            {**metrics, "final_missing_page_price_rows": 7},
        )
    assert json.loads(store.get_run("2026-08-11")["metrics_json"])["final_missing_page_price_rows"] == 75
    with pytest.raises(FrozenSnapshotConflict, match="immutable"):
        store.update_daily_metrics(lease, {"final_missing_page_price_rows": 7})
    with pytest.raises(FrozenSnapshotConflict, match="immutable"):
        store.update_daily_metrics(lease, {"force_finalize": True})
    assert json.loads(store.get_run("2026-08-11")["metrics_json"])["final_missing_page_price_rows"] == 75


def test_prepared_finalization_intent_survives_retry_and_is_consumed_at_commit(store):
    lease = _lease(store)
    snapshot = store.ingest_snapshot(lease, [_row()])["snapshot"]
    store.freeze_snapshot(lease, snapshot["id"])
    metrics = {
        "finalized_rows": 1,
        "final_missing_page_price_rows": 1,
        "final_frozen_missing_page_price_rows": 1,
        "final_failed_customer_link_rows": 0,
        "final_appended_failed_customer_link_rows": 0,
        "final_page_price_required_rows": 1,
    }
    first = store.prepare_finalization(
        lease, "C:/out.xlsx", input_manifest_hash="input-a", metrics=metrics,
    )
    intent = json.loads(first["metrics_json"])[FINALIZATION_INTENT_KEY]
    assert intent == {"input_manifest_hash": "input-a", "metrics": metrics}
    retry = store.prepare_finalization(
        lease, "C:/out.xlsx", input_manifest_hash="input-a",
        metrics={**metrics, "final_missing_page_price_rows": 0},
    )
    assert json.loads(retry["metrics_json"])[FINALIZATION_INTENT_KEY] == intent
    with pytest.raises(FrozenSnapshotConflict, match="prepared publication intent"):
        store.commit_finalization(
            lease, "C:/out.xlsx", "manifest-a",
            {**metrics, "final_missing_page_price_rows": 0},
        )
    assert store.get_run("2026-08-11")["state"] == "FINALIZING"
    final = store.commit_finalization(lease, "C:/out.xlsx", "manifest-a", metrics)
    assert final["state"] == "FINALIZED"
    assert FINALIZATION_INTENT_KEY not in json.loads(final["metrics_json"])


@pytest.mark.parametrize("forced", [False, True])
def test_atomic_final_commit_rolls_back_state_manifest_and_metrics(store, monkeypatch, forced):
    lease = _lease(store)
    snapshot = store.ingest_snapshot(lease, [_row()])["snapshot"]
    store.freeze_snapshot(lease, snapshot["id"])
    store.prepare_finalization(lease, "C:/out.xlsx")
    before = store.get_run("2026-08-11")
    original_transaction = store._transaction
    metrics = {
        "finalized_rows": 1,
        "final_missing_page_price_rows": 1,
        "final_frozen_missing_page_price_rows": 1,
        "final_failed_customer_link_rows": 0,
        "final_appended_failed_customer_link_rows": 0,
        "final_page_price_required_rows": 1,
    }
    if forced:
        metrics.update({
            "forced": True,
            "forced_incomplete_count": 1,
            "forced_incomplete_item_count": 1,
            "remote_incomplete_job_count": 1,
            "blocker_count": 2,
            "force_finalize": True,
            "force_finalize_at": "2026-08-11T12:00:00+00:00",
            "force_finalize_max_incomplete": 1,
            "force_finalize_actual_incomplete": 1,
            "force_finalize_accept_unresolved_crawl": False,
            "missing_page_price_rows": 1,
            "page_price_required_rows": 1,
            "force_finalize_reason": "test",
            "frozen_snapshot_id": snapshot["id"],
            "frozen_row_count": 1,
            "blocker_summary_at_force_finalize": {},
            "force_finalize_incomplete_tasks": [],
        })

    @contextmanager
    def crash_before_commit(*args, **kwargs):
        with original_transaction(*args, **kwargs) as connection:
            yield connection
            pending = connection.execute(
                "SELECT state, final_manifest_hash, metrics_json FROM daily_runs WHERE id=?",
                (lease.run_id,),
            ).fetchone()
            assert pending["state"] == "FINALIZED"
            assert pending["final_manifest_hash"] == "manifest-a"
            assert json.loads(pending["metrics_json"])["final_missing_page_price_rows"] == 1
            if forced:
                assert json.loads(pending["metrics_json"])["force_finalize_actual_incomplete"] == 1
            raise RuntimeError("injected crash inside final transaction")

    monkeypatch.setattr(store, "_transaction", crash_before_commit)
    with pytest.raises(RuntimeError, match="injected crash"):
        store.commit_finalization(lease, "C:/out.xlsx", "manifest-a", metrics)
    after = store.get_run("2026-08-11")
    assert (after["state"], after["final_manifest_hash"], after["metrics_json"]) == (
        before["state"], before["final_manifest_hash"], before["metrics_json"],
    )


def test_each_store_operation_opens_its_own_connection(store, monkeypatch):
    opened: list[sqlite3.Connection] = []
    original = store._connect

    def tracking_connect():
        connection = original()
        opened.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", tracking_connect)
    store.ensure_run("2026-08-11")
    store.get_run("2026-08-11")
    assert len(opened) >= 2
    assert all(connection is not other for index, connection in enumerate(opened) for other in opened[index + 1 :])


def test_remote_reconciliation_is_fenced_monotonic_and_identity_immutable(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(lease, "mall-a:one", "task-a", "payload-a")
    with pytest.raises(RemoteIntentConflict):
        store.reconcile_remote_job(lease, job["id"], remote_identity="remote-a")
    store.claim_remote_submission(lease, job["id"])
    store.mark_submit_unknown(lease, job["id"], "timeout")
    store.reconcile_remote_job(lease, job["id"], remote_identity="remote-a", state="RUNNING")
    with pytest.raises(RemoteIntentConflict):
        store.reconcile_remote_job(lease, job["id"], remote_identity="remote-b", state="RUNNING")
    store.reconcile_remote_job(lease, job["id"], remote_identity="remote-a", state="COMPLETED")
    with pytest.raises(RemoteIntentConflict):
        store.reconcile_remote_job(lease, job["id"], remote_identity="remote-a", state="RUNNING")


def test_remote_identity_is_unique_within_a_daily_run(store):
    lease = _lease(store)
    first = store.prepare_remote_intent(lease, "mall-a:one", "task-a", "payload-a")
    second = store.prepare_remote_intent(lease, "mall-a:two", "task-b", "payload-b")
    for job in (first, second):
        store.claim_remote_submission(lease, job["id"])

    store.reconcile_remote_job(
        lease,
        first["id"],
        remote_identity="mall-a|task-id|task-number",
    )
    with pytest.raises(RemoteIntentConflict, match="remote identity"):
        store.reconcile_remote_job(
            lease,
            second["id"],
            remote_identity="mall-a|task-id|task-number",
        )


def test_remote_identity_may_repeat_in_a_different_daily_run(store):
    first_lease = _lease(store, date="2026-08-11", owner="first")
    second_lease = _lease(store, date="2026-08-12", owner="second")
    identity = "mall-a|task-id|task-number"

    for lease, logical_key, task_name in (
        (first_lease, "mall-a:first-day", "task-first-day"),
        (second_lease, "mall-a:second-day", "task-second-day"),
    ):
        job = store.prepare_remote_intent(lease, logical_key, task_name, logical_key)
        store.claim_remote_submission(lease, job["id"])
        reconciled = store.reconcile_remote_job(
            lease,
            job["id"],
            remote_identity=identity,
        )
        assert reconciled["remote_identity"] == identity


def test_remote_identity_migration_fails_closed_on_legacy_collision(tmp_path):
    path = tmp_path / "legacy-remote-collision.sqlite3"
    store = DailyAuditStore(path)
    lease = _lease(store)
    first = store.prepare_remote_intent(lease, "mall-a:one", "task-a", "payload-a")
    second = store.prepare_remote_intent(lease, "mall-a:two", "task-b", "payload-b")
    for job in (first, second):
        store.claim_remote_submission(lease, job["id"])
    store.reconcile_remote_job(lease, first["id"], remote_identity="remote-a")
    store.reconcile_remote_job(lease, second["id"], remote_identity="remote-b")
    store.release_lease(lease)

    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX IF EXISTS ux_remote_jobs_run_remote_identity")
        connection.execute(
            "UPDATE remote_jobs SET remote_identity='remote-a' WHERE id=?",
            (second["id"],),
        )

    with pytest.raises(RemoteIntentConflict, match="duplicate remote_identity"):
        DailyAuditStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM remote_jobs WHERE remote_identity='remote-a'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='ux_remote_jobs_run_remote_identity'"
        ).fetchone()[0] == 0


def test_crawl_binding_and_harvest_caps_fail_closed(store):
    lease = _lease(store)
    first = store.prepare_remote_intent(lease, "mall-a:one", "task-a", "payload-a")
    second = store.prepare_remote_intent(lease, "mall-a:two", "task-b", "payload-b")
    crawl = store.ensure_crawl_work(lease, "mall-a", "sku", "https://example/a", "v1", "fp")
    assert store.link_crawl_work_to_remote_job(lease, crawl["id"], first["id"])["remote_job_id"] == first["id"]
    assert store.link_crawl_work_to_remote_job(lease, crawl["id"], first["id"])["remote_job_id"] == first["id"]
    with pytest.raises(RemoteIntentConflict):
        store.link_crawl_work_to_remote_job(lease, crawl["id"], second["id"])
    store.claim_remote_submission(lease, first["id"])
    store.reconcile_remote_job(lease, first["id"], remote_identity="remote-a", state="RUNNING")
    assert store.claim_harvest(lease, first["id"], max_attempts=1, max_elapsed_seconds=10)
    capped = store.record_harvest(lease, first["id"], elapsed_seconds=10, max_attempts=1, max_elapsed_seconds=10)
    assert capped["state"] == "MANUAL_REQUIRED"
    assert store.claim_harvest(lease, first["id"], max_attempts=1, max_elapsed_seconds=10) is None


def test_explicit_stage_requeue_resets_attempt_and_elapsed(store):
    lease = _lease(store)
    item_id = store.ingest_snapshot(lease, [_row()])["members"][0]["audit_item_id"]
    work = store.ensure_stage_work(lease, item_id, "DETAIL", "fp")
    store.claim_stage_work(lease, work["id"], max_attempts=1, max_elapsed_seconds=1)
    store.fail_stage_work(lease, work["id"], elapsed_seconds=1, error="limited", retryable=True, max_attempts=1, max_elapsed_seconds=1)
    reset = store.requeue_terminal_stage_work(lease, work["id"])
    assert reset["attempts"] == 0 and reset["elapsed_seconds"] == 0
    assert store.claim_stage_work(lease, work["id"], max_attempts=1, max_elapsed_seconds=1)


def test_release_remote_submission_claim_retreats_only_burned_intent(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(lease, "mall-A:hash", "daily-20260811-mall-A", "payload-a")

    # A fresh intent never claimed releases as an idempotent no-op.
    released = store.release_remote_submission_claim(lease, job["id"], "auth refresh failed")
    assert released["state"] == "SUBMIT_INTENT"
    assert released["post_attempted"] is False
    assert released["error_text"] is None

    assert store.claim_remote_submission(lease, job["id"])["post_attempted"] is True
    released = store.release_remote_submission_claim(lease, job["id"], "auth refresh failed")
    assert released["state"] == "SUBMIT_INTENT"
    assert released["post_attempted"] is False
    assert "auth refresh failed" in released["error_text"]

    # Repeated release after retreat stays idempotent.
    again = store.release_remote_submission_claim(lease, job["id"], "auth refresh failed")
    assert again["post_attempted"] is False

    # The retreated claim can be burned again for a later retry decision.
    assert store.claim_remote_submission(lease, job["id"])["post_attempted"] is True

    # Once the outcome is uncertain the claim may never retreat.
    store.mark_submit_unknown(lease, job["id"], "timeout")
    with pytest.raises(RemoteIntentConflict):
        store.release_remote_submission_claim(lease, job["id"], "late release")


def test_release_remote_submission_claim_refuses_confirmed_remote_identity(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(lease, "mall-B:hash", "daily-20260811-mall-B", "payload-b")
    store.claim_remote_submission(lease, job["id"])
    store.reconcile_remote_job(lease, job["id"], remote_identity="mall|1|RW-1", state="SUBMITTED")

    with pytest.raises(RemoteIntentConflict):
        store.release_remote_submission_claim(lease, job["id"], "unexpected pre-send")

    assert store.get_remote_job(job["id"])["post_attempted"] is True


def test_burned_claim_without_release_stays_fail_closed_as_unknown(store):
    lease = _lease(store)
    job = store.prepare_remote_intent(lease, "mall-C:hash", "daily-20260811-mall-C", "payload-c")
    store.claim_remote_submission(lease, job["id"])

    assert store.recover_ambiguous_remote_submissions(lease) == 1
    swept = store.get_remote_job(job["id"])
    assert swept["state"] == "SUBMIT_UNKNOWN"
    assert swept["post_attempted"] is True

    with pytest.raises(RemoteIntentConflict):
        store.release_remote_submission_claim(lease, job["id"], "after sweep")
