"""Offline contract tests for the manual-rebuild durable supersede flow."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.services.daily_audit_store import DailyAuditStore, RemoteIntentConflict


@pytest.fixture
def store(tmp_path):
    return DailyAuditStore(tmp_path / "daily-audit.sqlite3")


def _lease(store, date="2026-08-11", owner="rebuild-operator"):
    store.ensure_run(date)
    return store.acquire_lease(date, owner, ttl_seconds=30)


def _submitted_job(store, lease, *, logical_key="mall-a:natural", task_name="task-a"):
    job = store.prepare_remote_intent(
        lease,
        logical_key,
        task_name,
        "payload-a",
        mall_key="mall-a",
        input_path="run/zhaoshang/mall-a/input.xlsx",
        result_path="run/zhaoshang/mall-a/result.xlsx",
        payload_json={"requests": [{"sku_id": "SKU-1"}]},
    )
    store.claim_remote_submission(lease, int(job["id"]))
    store.reconcile_remote_job(
        lease,
        int(job["id"]),
        remote_identity=f"mall-a|task-id-{task_name}|task-number",
        state="SUBMITTED",
    )
    return job


def _decorate_stall_evidence(store, job_id, *, error="stalled in RUNNING after confirmed restart", evidence):
    """Attach durable diagnostics without changing the SUBMITTED state."""
    connection = sqlite3.connect(str(store.path), timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(
            "UPDATE remote_jobs SET error_text=?, recovery_evidence_json=? WHERE id=?",
            (error, json.dumps(evidence, ensure_ascii=False, sort_keys=True), job_id),
        )
        connection.commit()
    finally:
        connection.close()


def _crawl(store, lease, ordinal, *, state=None):
    work = store.ensure_crawl_work(
        lease,
        "mall-a",
        f"SKU-{ordinal}",
        f"https://shop.example/p/{ordinal}",
        "v1",
        f"fp-{ordinal}",
        source_id=f"V-{ordinal}",
    )
    if state is not None:
        connection = sqlite3.connect(str(store.path), timeout=5)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("UPDATE crawl_work SET state=? WHERE id=?", (state, work["id"]))
            connection.commit()
        finally:
            connection.close()
        work = store.get_crawl_work(int(work["id"]))
    return work


def _supersede(store, lease, old_job_id, eligible_crawl_ids, **overrides):
    payload = {
        "old_job_id": int(old_job_id),
        "replacement_logical_key": "mall-a:natural#rebuild#abcd1234",
        "replacement_task_name": "task-a-rebuild-1",
        "replacement_payload_hash": "payload-rebuild",
        "replacement_input_path": "run/zhaoshang/mall-a/rebuild-input.xlsx",
        "replacement_result_path": "run/zhaoshang/mall-a/rebuild-result.xlsx",
        "replacement_payload_json": {"requests": [{"sku_id": "SKU-1"}]},
        "mall_key": "mall-a",
        "action_id": "rebuild-20260828-0001",
        "reason": "RUNNING stall after confirmed restart",
        "old_batch_number": "RW-100",
        "new_batch_number": "RW-101",
        "stop_evidence": {"stop": "confirmed", "restarts": 1},
        "eligible_crawl_ids": eligible_crawl_ids,
    }
    payload.update(overrides)
    return store.supersede_remote_job_to_replacement(lease, **payload)


def test_supersede_migrates_eligible_crawls_and_preserves_old_evidence(store):
    lease = _lease(store)
    job = _submitted_job(store, lease)
    _decorate_stall_evidence(
        store, int(job["id"]), evidence={"phase": "restart-confirmed", "restarts": 1}
    )
    pending = _crawl(store, lease, 1)
    manual = _crawl(store, lease, 2, state="MANUAL_REQUIRED")
    success = _crawl(store, lease, 3)
    store.link_crawl_work_batch_to_remote_job(
        lease, [int(pending["id"]), int(manual["id"]), int(success["id"])], int(job["id"])
    )
    store.complete_crawl_work(
        lease,
        int(success["id"]),
        {"page_price_value": "9.5", "page_price_status": "OK", "page_price_source": "招商局"},
    )

    result = _supersede(store, lease, int(job["id"]), [int(pending["id"]), int(manual["id"])])

    new_job_id = int(result["new_job"]["id"])
    assert result["new_job"]["state"] == "SUBMIT_INTENT"
    assert result["new_job"]["post_attempted"] is False
    assert result["new_job"]["task_name"] == "task-a-rebuild-1"
    assert result["new_job"]["mall_key"] == "mall-a"
    assert json.loads(result["new_job"]["payload_json"]) == {"requests": [{"sku_id": "SKU-1"}]}
    assert result["migrated_crawl_count"] == 2

    old = store.get_remote_job(int(job["id"]))
    assert old["state"] == "SUPERSEDED"
    assert old["error_text"] == "stalled in RUNNING after confirmed restart"
    assert json.loads(old["recovery_evidence_json"]) == {"phase": "restart-confirmed", "restarts": 1}
    assert old["remote_identity"] == "mall-a|task-id-task-a|task-number"

    untouched = store.get_crawl_work(int(success["id"]))
    assert untouched["state"] == "SUCCESS"
    assert int(untouched["remote_job_id"]) == int(job["id"])
    assert json.loads(untouched["result_json"])["page_price_value"] == "9.5"

    for migrated_id in (int(pending["id"]), int(manual["id"])):
        row = store.get_crawl_work(migrated_id)
        assert row["state"] == "PENDING"
        assert int(row["remote_job_id"]) == new_job_id
        assert row["error_text"] is None
        assert row["result_json"] is None

    replacement = store.get_replacement_for_job(int(job["id"]))
    assert replacement["old_remote_job_id"] == int(job["id"])
    assert replacement["new_remote_job_id"] == new_job_id
    assert replacement["rebuild_action_id"] == "rebuild-20260828-0001"
    assert replacement["reason"] == "RUNNING stall after confirmed restart"
    assert replacement["status"] == "ACTIVE"
    assert replacement["old_batch_number"] == "RW-100"
    assert replacement["new_batch_number"] == "RW-101"
    assert json.loads(replacement["stop_evidence_json"]) == {"stop": "confirmed", "restarts": 1}
    store.release_lease(lease)


def test_supersede_twice_fails_closed_without_a_second_replacement(store):
    lease = _lease(store)
    job = _submitted_job(store, lease)
    crawl = _crawl(store, lease, 1)
    store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    _supersede(store, lease, int(job["id"]), [int(crawl["id"])])

    with pytest.raises(RemoteIntentConflict, match="already has a replacement"):
        _supersede(
            store,
            lease,
            int(job["id"]),
            [],
            replacement_task_name="task-a-rebuild-2",
            action_id="rebuild-20260828-0002",
            replacement_logical_key="mall-a:natural#rebuild#ffff0000",
        )

    replacements = store.list_remote_replacements(lease.run_id)
    assert len(replacements) == 1
    assert {row["rebuild_action_id"] for row in replacements} == {"rebuild-20260828-0001"}
    store.release_lease(lease)


def test_replacement_task_name_collision_including_the_old_job_itself_fails(store):
    lease = _lease(store)
    job = _submitted_job(store, lease)
    crawl = _crawl(store, lease, 1)
    store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))

    with pytest.raises(RemoteIntentConflict, match="task name is already reserved"):
        _supersede(store, lease, int(job["id"]), [int(crawl["id"])], replacement_task_name="task-a")

    other = store.prepare_remote_intent(lease, "mall-a:other", "task-b", "payload-b", mall_key="mall-a")
    assert other["id"] is not None
    with pytest.raises(RemoteIntentConflict, match="task name is already reserved"):
        _supersede(store, lease, int(job["id"]), [int(crawl["id"])], replacement_task_name="task-b")

    assert store.list_remote_replacements(lease.run_id) == []
    assert store.get_remote_job(int(job["id"]))["state"] == "SUBMITTED"
    store.release_lease(lease)


def test_terminal_crawl_in_eligible_set_rolls_the_whole_migration_back(store):
    lease = _lease(store)
    job = _submitted_job(store, lease)
    pending = _crawl(store, lease, 1)
    success = _crawl(store, lease, 2)
    store.link_crawl_work_batch_to_remote_job(
        lease, [int(pending["id"]), int(success["id"])], int(job["id"])
    )
    store.complete_crawl_work(
        lease,
        int(success["id"]),
        {"page_price_value": "8.0", "page_price_status": "OK", "page_price_source": "招商局"},
    )

    with pytest.raises(RemoteIntentConflict, match="terminal crawl evidence cannot be migrated"):
        _supersede(store, lease, int(job["id"]), [int(pending["id"]), int(success["id"])])

    jobs = store.list_remote_jobs(lease)
    assert len(jobs) == 1
    assert store.list_remote_replacements(lease.run_id) == []
    assert store.get_remote_job(int(job["id"]))["state"] == "SUBMITTED"
    assert int(store.get_crawl_work(int(pending["id"]))["remote_job_id"]) == int(job["id"])
    assert store.get_crawl_work(int(pending["id"]))["state"] == "PENDING"
    store.release_lease(lease)


def test_crawl_bound_to_another_remote_job_cannot_be_migrated(store):
    lease = _lease(store)
    job = _submitted_job(store, lease)
    other_job = _submitted_job(
        store, lease, logical_key="mall-a:other", task_name="task-b"
    )
    foreign = _crawl(store, lease, 1)
    own = _crawl(store, lease, 2)
    store.link_crawl_work_to_remote_job(lease, int(foreign["id"]), int(other_job["id"]))
    store.link_crawl_work_to_remote_job(lease, int(own["id"]), int(job["id"]))

    with pytest.raises(RemoteIntentConflict, match="not bound to the superseded remote job"):
        _supersede(store, lease, int(job["id"]), [int(foreign["id"]), int(own["id"])])

    assert store.list_remote_replacements(lease.run_id) == []
    assert int(store.get_crawl_work(int(foreign["id"]))["remote_job_id"]) == int(other_job["id"])
    assert store.get_remote_job(int(job["id"]))["state"] == "SUBMITTED"
    store.release_lease(lease)


def test_empty_eligible_set_is_rejected(store):
    lease = _lease(store)
    job = _submitted_job(store, lease)

    with pytest.raises(ValueError, match="no eligible crawl work to migrate"):
        _supersede(store, lease, int(job["id"]), [])

    assert store.list_remote_replacements(lease.run_id) == []
    assert store.get_remote_job(int(job["id"]))["state"] == "SUBMITTED"
    store.release_lease(lease)


def test_dirty_superseded_state_without_replacement_row_still_fails_closed(store):
    lease = _lease(store)
    job = _submitted_job(store, lease)
    crawl = _crawl(store, lease, 1)
    store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    connection = sqlite3.connect(str(store.path), timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("UPDATE remote_jobs SET state='SUPERSEDED' WHERE id=?", (int(job["id"]),))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RemoteIntentConflict, match="already has a replacement"):
        _supersede(store, lease, int(job["id"]), [int(crawl["id"])])

    assert store.list_remote_replacements(lease.run_id) == []
    store.release_lease(lease)


def test_replacement_readers_return_the_recorded_lineage(store):
    lease = _lease(store)
    job = _submitted_job(store, lease)
    crawl = _crawl(store, lease, 1)
    store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
    result = _supersede(store, lease, int(job["id"]), [int(crawl["id"])])
    new_job_id = int(result["new_job"]["id"])

    assert store.get_replacement_for_job(int(job["id"]))["id"] == result["replacement"]["id"]
    assert store.get_replacement_of_job(new_job_id)["id"] == result["replacement"]["id"]
    assert store.get_replacement_for_job(new_job_id) is None
    assert store.get_replacement_of_job(int(job["id"])) is None
    listed = store.list_remote_replacements(lease.run_id)
    assert [row["id"] for row in listed] == [result["replacement"]["id"]]
    assert listed[0]["old_remote_job_id"] == int(job["id"])
    assert listed[0]["new_remote_job_id"] == new_job_id
    store.release_lease(lease)
