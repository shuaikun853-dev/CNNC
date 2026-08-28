"""真实旧生产库的原地升级路径测试(44c37de schema → 含 manual-ops 的当前 schema)。

夹具由 44c37de(未含 manual-ops cherry-pick)的 store 代码生成,没有
``remote_job_replacements`` 表。测试把夹具复制到 tmp_path 后用当前代码打开,
验证 ``_initialize`` 迁移:只新增表、不动任何既有行、重复打开幂等,并且升级后
supersede(手工重建换岗)机制与三个 lease-free 读接口立即可用。

夹具缺失时 skip 而非 fail,保证测试在无夹具环境可移植。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.services.daily_audit_store import DailyAuditStore

FIXTURE_PATH = Path(
    r"E:\.A中核运维\cnnc-ops-agent-worktrees\_migration_fixture_dashboard_ops"
    r"\state_old_prod.sqlite3"
)
BUSINESS_DATE = "2026-08-28"
JOB1_LOGICAL_KEY = "legacy:jd:11111111111111"
JOB2_LOGICAL_KEY = "legacy:jd:22222222222222"


@pytest.fixture
def copied_db(tmp_path: Path) -> Path:
    if not FIXTURE_PATH.is_file():
        pytest.skip("migration fixture unavailable")
    target = tmp_path / "state.sqlite3"
    shutil.copyfile(FIXTURE_PATH, target)
    return target


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _snapshot_rows(path: Path) -> tuple[list[dict], list[dict]]:
    connection = _connect(path)
    try:
        jobs = [
            dict(row) for row in connection.execute(
                "SELECT id, run_id, logical_key, task_name, payload_hash, mall_key,"
                " state, post_attempted, remote_identity FROM remote_jobs ORDER BY id"
            )
        ]
        crawls = [
            dict(row) for row in connection.execute(
                "SELECT id, run_id, mall_key, source_id, sku_id, state, remote_job_id"
                " FROM crawl_work ORDER BY id"
            )
        ]
    finally:
        connection.close()
    return jobs, crawls


def _crawl_by_source(path: Path, source_id: str) -> dict:
    connection = _connect(path)
    try:
        row = connection.execute(
            "SELECT id, source_id, sku_id, state, remote_job_id FROM crawl_work"
            " WHERE source_id=? ORDER BY id LIMIT 1",
            (source_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, f"fixture crawl row missing: {source_id}"
    return dict(row)


def _sqlite_tables(path: Path) -> set[str]:
    connection = _connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        connection.close()
    return {row["name"] for row in rows}


def test_upgrade_adds_replacements_table(copied_db: Path):
    # 升级前:旧 schema 确实没有 replacement 表
    assert "remote_job_replacements" not in _sqlite_tables(copied_db)

    store = DailyAuditStore(copied_db)  # 打开即触发 _initialize 迁移
    assert "remote_job_replacements" in store.table_names()


def test_upgrade_preserves_every_existing_row(copied_db: Path):
    jobs_before, crawls_before = _snapshot_rows(copied_db)
    assert len(jobs_before) == 2
    assert len(crawls_before) == 3

    DailyAuditStore(copied_db)  # 只做 schema 迁移,不允许任何行变化

    jobs_after, crawls_after = _snapshot_rows(copied_db)
    assert jobs_after == jobs_before
    assert crawls_after == crawls_before

    by_key = {job["logical_key"]: job for job in jobs_after}
    job1 = by_key[JOB1_LOGICAL_KEY]
    job2 = by_key[JOB2_LOGICAL_KEY]
    assert job1["state"] == "COMPLETED"
    assert job1["remote_identity"] == "jd|100|RW-100"
    assert job1["task_name"] == "cnnc_current_opt_京东_11111111111111"
    assert job1["post_attempted"] == 1
    assert job2["state"] == "RUNNING"
    assert job2["remote_identity"] == "jd|200|RW-200"
    assert job2["task_name"] == "cnnc_current_opt_京东_22222222222222"

    crawls_by_source = {crawl["source_id"]: crawl for crawl in crawls_after}
    assert crawls_by_source["SRC0"]["state"] == "PENDING"
    assert crawls_by_source["SRC0"]["remote_job_id"] == job1["id"]
    assert crawls_by_source["SRC1"]["state"] == "SUCCESS"
    assert crawls_by_source["SRC1"]["remote_job_id"] == job1["id"]
    assert crawls_by_source["SRC2"]["state"] == "RUNNING"
    assert crawls_by_source["SRC2"]["remote_job_id"] == job2["id"]


def test_reopen_after_upgrade_is_idempotent(copied_db: Path):
    DailyAuditStore(copied_db)
    jobs_before, crawls_before = _snapshot_rows(copied_db)

    again = DailyAuditStore(copied_db)  # 第二次构造:不得抛错、不得丢行
    assert "remote_job_replacements" in again.table_names()
    jobs_after, crawls_after = _snapshot_rows(copied_db)
    assert jobs_after == jobs_before
    assert crawls_after == crawls_before
    assert len(jobs_after) == 2 and len(crawls_after) == 3


def test_supersede_and_readers_work_after_upgrade(copied_db: Path):
    store = DailyAuditStore(copied_db)
    lease = store.acquire_lease(BUSINESS_DATE, "migration-smoke", 300.0)
    jobs = {job["logical_key"]: job for job in store.list_remote_jobs(lease)}
    job1_id = int(jobs[JOB1_LOGICAL_KEY]["id"])
    job2_id = int(jobs[JOB2_LOGICAL_KEY]["id"])

    eligible = _crawl_by_source(copied_db, "SRC0")  # PENDING 绑 job1 → 可迁移
    done = _crawl_by_source(copied_db, "SRC1")      # SUCCESS 绑 job1 → 终态证据留下
    assert eligible["state"] == "PENDING" and int(eligible["remote_job_id"]) == job1_id
    assert done["state"] == "SUCCESS" and int(done["remote_job_id"]) == job1_id

    try:
        # store 层 supersede 不检查源 job 状态(协议层才检查);job1 是 COMPLETED
        # 也必须能换岗,这里只验证迁移后的机制可用。
        result = store.supersede_remote_job_to_replacement(
            lease,
            old_job_id=job1_id,
            replacement_logical_key="legacy:jd:11111111111111#rebuild#abcd1234",
            replacement_task_name="cnnc_current_opt_京东_33333333333333",
            replacement_payload_hash="payloadhash1",
            replacement_input_path="C:\\x\\input.xlsx",
            replacement_result_path="C:\\x\\result.xlsx",
            replacement_payload_json={"requests": [], "batch_state": "C:\\x\\BATCH_STATE.md"},
            mall_key="jd",
            action_id="smoke-action-1",
            reason="migration smoke",
            old_batch_number="RW-100",
            new_batch_number="RW-101",
            stop_evidence={"phase": "TERMINAL_BEFORE_REBUILD"},
            eligible_crawl_ids=[int(eligible["id"])],
        )
    finally:
        store.release_lease(lease)

    new_job_id = int(result["new_job"]["id"])
    replacement_id = int(result["replacement"]["id"])

    # old job 转 SUPERSEDED,错误/恢复证据与身份保持不变
    old = store.get_remote_job(job1_id)
    assert old["state"] == "SUPERSEDED"
    assert old["remote_identity"] == "jd|100|RW-100"
    assert old["task_name"] == "cnnc_current_opt_京东_11111111111111"
    assert old["post_attempted"] is True

    # SUCCESS crawl 未列入 eligible → 仍绑 old job,状态不变
    untouched = store.get_crawl_work(int(done["id"]))
    assert untouched["state"] == "SUCCESS"
    assert int(untouched["remote_job_id"]) == job1_id

    # eligible crawl 迁到新 job,重置为 PENDING
    migrated = store.get_crawl_work(int(eligible["id"]))
    assert migrated["state"] == "PENDING"
    assert int(migrated["remote_job_id"]) == new_job_id

    # 新 job 是干净的可提交 intent;job2 完全不受影响
    assert result["new_job"]["state"] == "SUBMIT_INTENT"
    assert result["new_job"]["post_attempted"] is False
    assert result["new_job"]["task_name"] == "cnnc_current_opt_京东_33333333333333"
    assert result["new_job"]["mall_key"] == "jd"
    assert json.loads(result["new_job"]["payload_json"]) == {
        "requests": [], "batch_state": "C:\\x\\BATCH_STATE.md",
    }
    assert result["migrated_crawl_count"] == 1
    assert store.get_remote_job(job2_id)["state"] == "RUNNING"

    # replacement 行完整(lineage / batch 号 / 停机证据)
    replacement = store.get_replacement_for_job(job1_id)
    assert replacement is not None
    assert int(replacement["id"]) == replacement_id
    assert int(replacement["old_remote_job_id"]) == job1_id
    assert int(replacement["new_remote_job_id"]) == new_job_id
    assert replacement["rebuild_action_id"] == "smoke-action-1"
    assert replacement["reason"] == "migration smoke"
    assert replacement["status"] == "ACTIVE"
    assert replacement["old_batch_number"] == "RW-100"
    assert replacement["new_batch_number"] == "RW-101"
    assert json.loads(replacement["stop_evidence_json"]) == {"phase": "TERMINAL_BEFORE_REBUILD"}

    # 三个 lease-free 读接口读回正确
    assert int(store.get_replacement_of_job(new_job_id)["id"]) == replacement_id
    assert store.get_replacement_for_job(new_job_id) is None
    assert store.get_replacement_of_job(job1_id) is None
    listed = store.list_remote_replacements(int(lease.run_id))
    assert [int(row["id"]) for row in listed] == [replacement_id]
    assert listed[0]["rebuild_action_id"] == "smoke-action-1"
