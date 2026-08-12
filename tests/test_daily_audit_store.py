"""Offline contract tests for the incremental audit checkpoint store."""

from __future__ import annotations

import sqlite3

import pytest

from app.services.daily_audit_store import (
    DailyAuditStore,
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
    assert {"daily_runs", "snapshots", "snapshot_members", "audit_items", "stage_work", "crawl_work", "remote_jobs"} == store.table_names()


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
    final = store.mark_finalized(lease, "C:/out.xlsx", "manifest-a")
    assert store.mark_finalized(lease, "C:/out.xlsx", "manifest-a")["id"] == final["id"]
    with pytest.raises(FrozenSnapshotConflict):
        store.mark_finalized(lease, "C:/other.xlsx", "manifest-b")


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
