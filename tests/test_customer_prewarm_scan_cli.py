"""customer_prewarm_scan CLI 门禁(Issue #9 Phase 3)。

覆盖:seeded runtime root 上的 --skip-detail 全扫描、dry-run 沙箱隔离
(生产路径零写入、共享缓存只读)、customer_prewarm.lock 冲突时
SKIPPED_ALREADY_RUNNING、--json-output 父目录自动创建、argparse 硬配置错误
退出码 2,以及两个 installer/runner .ps1 的纯文本契约(从不执行脚本、
从不注册计划任务)。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.daily_audit_store import (
    DEFAULT_POOL_ID,
    DEFAULT_TENANT_ID,
    DailyAuditStore,
    ProcessLock,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts import customer_prewarm_scan as cli

SCRIPTS = Path(cli.__file__).resolve().parent
BUSINESS_DATE = "2026-09-07"


def _ok_crawl_row(mall_key, sku_id, exact_url, task_name="task-x"):
    return {
        "mall_key": mall_key,
        "sku_id": sku_id,
        "exact_url": exact_url,
        "task_name": task_name,
        "result": {"page_price_status": "OK", "page_price_value": "6.6"},
    }


def _seed_state_db(runtime_root, business_date, *, crawl_rows=(), audit_items=()):
    """在 <runtime_root>/<business_date>/state.sqlite3 造真实 per-day 状态库。

    schema 由 DailyAuditStore 建立(幂等),行用原始 SQL 填充(与
    tests/test_customer_prewarm_daily_sync.make_state_db 同一做法,但路径
    固定在 scanner 读取的位置)。
    """
    path = runtime_root / business_date / "state.sqlite3"
    DailyAuditStore(path)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """INSERT INTO daily_runs (business_date, tenant_id, pool_id, state,
                                       created_at, updated_at)
               VALUES (?, ?, ?, 'OPEN', ?, ?)""",
            (business_date, DEFAULT_TENANT_ID, DEFAULT_POOL_ID, now, now),
        )
        run_id = int(cursor.lastrowid)
        for index, row in enumerate(crawl_rows, start=1):
            task_name = row.get("task_name") or ""
            job_id = None
            if task_name:
                cursor = connection.execute(
                    """INSERT INTO remote_jobs (run_id, logical_key, task_name,
                                                payload_hash, mall_key, state,
                                                created_at, updated_at)
                       VALUES (?, ?, ?, 'payload-hash', ?, 'SUCCEEDED', ?, ?)""",
                    (run_id, f"logical-{index}", task_name, row.get("mall_key") or "", now, now),
                )
                job_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO crawl_work (run_id, mall_key, source_id, sku_id,
                                           exact_url, backend_version, fingerprint,
                                           state, remote_job_id, result_json, updated_at)
                   VALUES (?, ?, '', ?, ?, 'v1', ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    row.get("mall_key") or "",
                    row.get("sku_id") or "",
                    row.get("exact_url") or "",
                    f"fp-{index}",
                    row.get("state", "SUCCESS"),
                    job_id,
                    json.dumps(row["result"], ensure_ascii=False),
                    row.get("updated_at", now),
                ),
            )
        for index, row in enumerate(audit_items, start=1):
            connection.execute(
                """INSERT INTO audit_items
                   (run_id, v_commodity_id, sku_id, source_fingerprint,
                    customer_fingerprint, row_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    str(row.get("documentId", f"VC-{index}")),
                    str(row.get("skuId", f"SKU-{index}")),
                    f"src-fp-{index}",
                    "",
                    json.dumps(row, ensure_ascii=False),
                    now,
                ),
            )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return path


def _run_scan(runtime_root, *extra, json_output=None):
    argv = [
        "--business-date",
        BUSINESS_DATE,
        "--runtime-root",
        str(runtime_root),
        *extra,
    ]
    if json_output is not None:
        argv += ["--json-output", str(json_output)]
    return cli.main(argv)


# ---- (a) seeded runtime root + --skip-detail ---------------------------------------


def test_scan_cli_seeded_runtime_root_skip_detail(tmp_path):
    runtime_root = tmp_path / "runtime"
    url = "https://b2b.nbdeli.com/goods/900.html"
    _seed_state_db(
        runtime_root,
        BUSINESS_DATE,
        crawl_rows=[_ok_crawl_row("nbdeli", "S1", url)],
        audit_items=[{"documentId": "DOC-9", "fourCategoryId": "CAT-9"}],
    )
    json_output = tmp_path / "summary.json"
    exit_code = _run_scan(runtime_root, "--skip-detail", json_output=json_output)
    assert exit_code == 0
    assert json_output.is_file()
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert "intake_count" in summary
    assert "documents_processed" in summary
    assert summary["intake_count"] == 1
    assert summary["documents_processed"] == 0  # skip-detail: no attempts consumed
    assert summary["ok_synced"] == 1  # Phase A mirrored the OK crawl row
    prewarm_store_path = runtime_root / "customer_prewarm.sqlite3"
    assert prewarm_store_path.is_file()
    pending = CustomerPrewarmStore(prewarm_store_path).pending_documents(10)
    assert [row["document_id"] for row in pending] == ["DOC-9"]
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    assert cache.lookup_current("nbdeli", url) is not None


# ---- (b) dry-run:生产路径零写入,共享缓存只读 -----------------------------------------


def test_scan_cli_dry_run_isolates_production_paths(tmp_path):
    runtime_root = tmp_path / "runtime"
    url = "https://b2b.nbdeli.com/goods/901.html"
    _seed_state_db(
        runtime_root,
        BUSINESS_DATE,
        crawl_rows=[_ok_crawl_row("nbdeli", "S2", url)],
        audit_items=[{"documentId": "DOC-10", "fourCategoryId": "CAT-10"}],
    )
    json_output = tmp_path / "dry-summary.json"
    exit_code = _run_scan(runtime_root, "--dry-run", "--skip-detail", json_output=json_output)
    assert exit_code == 0
    sandbox_root = runtime_root / "prewarm_dry_run"
    # 生产路径文件一律不存在
    assert not (runtime_root / "customer_prewarm.sqlite3").exists()
    assert not (runtime_root / "customer_prewarm.lock").exists()
    assert not (runtime_root / "shared_crawl_cache.sqlite3").exists()
    # 写入全部落在 sandbox
    assert (sandbox_root / "customer_prewarm.sqlite3").is_file()
    assert (sandbox_root / "shared_crawl_cache.sqlite3").is_file()
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["dry_run"] is True
    assert summary["would_record"] == 1  # 只计数,不写共享缓存
    assert summary["intake_count"] == 1
    pending = CustomerPrewarmStore(sandbox_root / "customer_prewarm.sqlite3").pending_documents(10)
    assert [row["document_id"] for row in pending] == ["DOC-10"]
    # sandbox 共享缓存是新库:lookup 证明 record_success 从未被调用
    cache = SharedCrawlCache(sandbox_root / "shared_crawl_cache.sqlite3")
    assert cache.lookup_current("nbdeli", url) is None


# ---- (c) 锁冲突:SKIPPED_ALREADY_RUNNING 且 exit 0 -----------------------------------


def test_scan_cli_lock_conflict_skips(tmp_path, capsys):
    runtime_root = tmp_path / "runtime"
    lock_path = Path(runtime_root).resolve() / "customer_prewarm.lock"
    lock = ProcessLock(lock_path)
    lock.acquire()
    try:
        exit_code = _run_scan(runtime_root, "--skip-detail")
        assert exit_code == 0
        assert "SKIPPED_ALREADY_RUNNING" in capsys.readouterr().out
        # 冲突即退出:没有创建 prewarm store / cache / sandbox
        assert not (runtime_root / "customer_prewarm.sqlite3").exists()
        assert not (runtime_root / "shared_crawl_cache.sqlite3").exists()
    finally:
        lock.release()


# ---- (d) --json-output 缺失父目录自动创建 --------------------------------------------


def test_scan_cli_json_output_creates_missing_parent_dirs(tmp_path):
    runtime_root = tmp_path / "runtime"
    json_output = tmp_path / "nested" / "deeper" / "summary.json"
    exit_code = _run_scan(runtime_root, "--skip-detail", json_output=json_output)
    assert exit_code == 0  # 缺 state db 是被容忍的:state_db=missing
    assert json_output.is_file()
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["state_db"] == "missing"


# ---- (e) 硬配置错误 exit 2 -----------------------------------------------------------


def test_scan_cli_invalid_argument_fails_closed(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--budget-seconds", "not-a-number"])
    assert excinfo.value.code == 2
    assert "invalid" in capsys.readouterr().err.lower()


def test_scan_cli_sandbox_without_dry_run_is_config_error(tmp_path, capsys):
    exit_code = _run_scan(tmp_path / "runtime", "--sandbox-root", str(tmp_path / "sandbox"))
    assert exit_code == 2
    assert "--sandbox-root" in capsys.readouterr().out


# ---- installer / runner 纯文本契约(从不执行) -----------------------------------------


def test_installer_and_runner_text_contract():
    register = (SCRIPTS / "register_customer_prewarm_scan_tasks.ps1").read_text(encoding="utf-8")
    runner = (SCRIPTS / "run_customer_prewarm_scan_scheduled.ps1").read_text(encoding="utf-8")
    # 数组参数按行分行书写(与既有 register 脚本一致),拼接后校验实际参数序列。
    flattened = " ".join(register.split()).replace('", "', " ")

    assert "CNNC-CustomerPrewarm-Scan" in register
    assert "IgnoreNew" in register
    assert "WindowStyle Hidden" in flattened
    assert "New-TimeSpan -Minutes 1" in register  # 1 分钟重复触发
    assert "-Apply" in register
    assert "if (-not $Apply)" in register  # 默认只预览,不注册
    assert "REGISTRATION IS NOT PERFORMED BY ISSUE #9" in register  # 人工授权注释

    assert "customer_prewarm_scan.py" in runner
    assert "scheduler_logs" in runner
    assert "--budget-seconds" in runner

    # 项目约定:.ps1 保持 ASCII(英文注释)
    register.encode("ascii")
    runner.encode("ascii")
