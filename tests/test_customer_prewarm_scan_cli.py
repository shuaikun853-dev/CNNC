"""customer_prewarm_scan CLI 门禁(Issue #9 Phase 3)。

覆盖:seeded runtime root 上的 --skip-detail 全扫描、dry-run 沙箱隔离
(生产路径零写入、生产共享缓存只读快照进 sandbox、当日 daily internet 结果
真实写入 sandbox 缓存)、--cache-path 在 dry-run 语义为只读 SOURCE、
--backfill-history 历史回填、customer_prewarm.lock 冲突时
SKIPPED_ALREADY_RUNNING、--json-output 父目录自动创建、argparse 硬配置错误
退出码 2,以及两个 installer/runner .ps1 的纯文本契约(从不执行脚本、
从不注册计划任务)。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.customer_prewarm_discovery import (
    process_document,
    snapshot_cache_readonly,
)
from app.services.customer_prewarm_store import CACHE_STATUS_CACHE_HIT, CustomerPrewarmStore
from app.services.daily_audit_store import (
    DEFAULT_POOL_ID,
    DEFAULT_TENANT_ID,
    DailyAuditStore,
    ProcessLock,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts import customer_prewarm_scan as cli
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

SCRIPTS = Path(cli.__file__).resolve().parent
BUSINESS_DATE = "2026-09-07"

CLI_MAPPINGS = [
    ZhaoshangMallMapping(
        mall_name="得力商城",
        source_id="S1",
        mall_key="nbdeli",
        domains=("b2b.nbdeli.com", "nbdeli.com", "www.nbdeli.com", "nbdeli.cn"),
        vendor_keywords=("nbdeli",),
    )
]


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


# ---- (b) dry-run:生产路径零写入,生产缓存只读快照,sandbox 内真实当日同步 -----


def _file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _cache_row_counts(path) -> dict[str, int]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("crawl_results", "crawl_urls", "crawl_identities")
        }
    finally:
        connection.close()


def _seed_production_runtime_root(runtime_root: Path, url_today: str, *, audit_items=()) -> Path:
    """生产式 runtime root:历史共享缓存 + 当日 state db(SUCCESS OK 行),无 prewarm store。"""
    prod_cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    prod_cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/940.html",
        real_sku_id="S9",
        sku_status="CONFIRMED",
        price="5.5",
        source="zhaoshang-history",
    )
    _seed_state_db(
        runtime_root,
        BUSINESS_DATE,
        crawl_rows=[_ok_crawl_row("nbdeli", "S8", url_today)],
        audit_items=audit_items,
    )
    return runtime_root / "shared_crawl_cache.sqlite3"


def test_dry_run_snapshots_source_cache_and_shows_daily_reuse(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    url_today = "https://b2b.nbdeli.com/goods/941.html"
    prod_cache_path = _seed_production_runtime_root(
        runtime_root, url_today, audit_items=[{"documentId": "DOC-10", "fourCategoryId": "CAT-10"}]
    )
    prod_hash_before = _file_sha256(prod_cache_path)
    prod_rows_before = _cache_row_counts(prod_cache_path)
    state_path = runtime_root / BUSINESS_DATE / "state.sqlite3"
    state_hash_before = _file_sha256(state_path)
    json_output = tmp_path / "dry-summary.json"
    exit_code = _run_scan(runtime_root, "--dry-run", "--skip-detail", json_output=json_output)
    assert exit_code == 0
    # 生产零写入门禁:缓存与 state db hash + 行数完全不变
    assert _file_sha256(prod_cache_path) == prod_hash_before
    assert _cache_row_counts(prod_cache_path) == prod_rows_before
    assert _file_sha256(state_path) == state_hash_before
    assert not (runtime_root / "customer_prewarm.sqlite3").exists()
    assert not (runtime_root / "customer_prewarm.lock").exists()
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["dry_run"] is True
    assert summary["cache_source"] == str(prod_cache_path.resolve())
    assert summary["cache_snapshot"] == {"copied": True, "crawl_results": 1}
    assert "would_record" not in summary
    assert summary["intake_count"] == 1
    assert summary["ok_synced"] == 1  # 当日 daily internet 行真实写入(sandbox)
    # sandbox 缓存同时含历史行(快照复制)与当日 daily-internet 行(本轮真实写入)
    sandbox_cache = SharedCrawlCache(runtime_root / "prewarm_dry_run" / "shared_crawl_cache.sqlite3")
    assert sandbox_cache.lookup_current("nbdeli", "https://b2b.nbdeli.com/goods/940.html") is not None
    today_hit = sandbox_cache.lookup_current("nbdeli", url_today)
    assert today_hit is not None
    assert today_hit["entry"]["source"] == "daily-internet"
    assert today_hit["entry"]["price"] == "6.6"
    assert today_hit["entry"]["payload"]["business_date"] == BUSINESS_DATE
    # sandbox prewarm store 落位,生产 store 未创建
    sandbox_store_path = runtime_root / "prewarm_dry_run" / "customer_prewarm.sqlite3"
    assert sandbox_store_path.is_file()
    pending = CustomerPrewarmStore(sandbox_store_path).pending_documents(10)
    assert [row["document_id"] for row in pending] == ["DOC-10"]


def test_dry_run_customer_hit_from_sandbox_cache(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    url_today = "https://b2b.nbdeli.com/goods/942.html"
    _seed_production_runtime_root(
        runtime_root, url_today, audit_items=[{"documentId": "DOC-11", "fourCategoryId": "CAT-11"}]
    )
    exit_code = _run_scan(runtime_root, "--dry-run", "--skip-detail")
    assert exit_code == 0
    sandbox_store = CustomerPrewarmStore(runtime_root / "prewarm_dry_run" / "customer_prewarm.sqlite3")
    sandbox_cache = SharedCrawlCache(runtime_root / "prewarm_dry_run" / "shared_crawl_cache.sqlite3")
    document = sandbox_store.pending_documents(10)[0]
    assert document["document_id"] == "DOC-11"

    def detail_fetch(document_id, four_category_id):
        payload = {"data": {"referenceInfos": [{"referenceLink": url_today}]}}
        return {"ok": True, "payload": payload, "error": None}

    result = process_document(
        sandbox_store,
        sandbox_cache,
        document,
        mappings=CLI_MAPPINGS,
        detail_fetch=detail_fetch,
    )
    assert result["detail_ok"] is True
    assert result["hits"] == 1 and result["misses"] == 0
    candidates = sandbox_store.candidates(CACHE_STATUS_CACHE_HIT)
    assert len(candidates) == 1
    assert candidates[0]["match_level"] == "EXACT"
    hit = sandbox_cache.lookup_current("nbdeli", url_today)
    assert hit is not None
    assert hit["match_level"] == "EXACT"
    assert hit["entry"]["source"] == "daily-internet"


def test_snapshot_cache_readonly_never_mutates_source(tmp_path):
    source = tmp_path / "prod" / "shared_crawl_cache.sqlite3"
    prod_cache = SharedCrawlCache(source)
    prod_cache.record_success(
        mall="nbdeli",
        submitted_url="https://b2b.nbdeli.com/goods/943.html",
        source="zhaoshang-history",
        payload={"k": "v"},
    )
    before_hash = _file_sha256(source)
    before_rows = _cache_row_counts(source)
    before_files = {entry.name for entry in source.parent.iterdir()}
    dest = tmp_path / "sandbox" / "shared_crawl_cache.sqlite3"
    result = snapshot_cache_readonly(source, dest)
    assert result == {"copied": True, "crawl_results": 1}
    # 主文件零写入:hash 与行数不变
    assert _file_sha256(source) == before_hash
    assert _cache_row_counts(source) == before_rows
    # 源目录不新增除 SQLite 只读访问瞬态伴生(-shm/-wal)之外的任何文件
    new_files = {entry.name for entry in source.parent.iterdir()} - before_files
    assert all(name.endswith(("-shm", "-wal")) for name in new_files)
    # dest 行数 == source 行数
    assert _cache_row_counts(dest) == before_rows
    # 缺失源:dest 不被创建、不建目录
    missing = snapshot_cache_readonly(
        tmp_path / "nope.sqlite3", tmp_path / "sandbox2" / "cache.sqlite3"
    )
    assert missing == {"copied": False, "reason": "missing"}
    assert not (tmp_path / "sandbox2").exists()


def test_dry_run_cache_path_is_source_not_target(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    url_today = "https://b2b.nbdeli.com/goods/944.html"
    _seed_production_runtime_root(runtime_root, url_today)
    # 显式 --cache-path 指向另一份"生产式"缓存(只读 SOURCE)
    external = tmp_path / "elsewhere" / "prod-like.sqlite3"
    external_cache = SharedCrawlCache(external)
    external_cache.record_success(
        mall="nbdeli",
        submitted_url=url_today,
        source="zhaoshang-history",
    )
    external_hash = _file_sha256(external)
    json_output = tmp_path / "dry2-summary.json"
    exit_code = _run_scan(
        runtime_root,
        "--dry-run",
        "--skip-detail",
        "--cache-path",
        str(external),
        json_output=json_output,
    )
    assert exit_code == 0
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["cache_source"] == str(external.resolve())
    assert summary["cache_snapshot"] == {"copied": True, "crawl_results": 1}
    assert _file_sha256(external) == external_hash
    sandbox_cache = SharedCrawlCache(runtime_root / "prewarm_dry_run" / "shared_crawl_cache.sqlite3")
    assert sandbox_cache.lookup_current("nbdeli", url_today) is not None
    # --cache-path 等于 sandbox 写目标自身 → 拒绝快照自覆盖,exit 2
    sandbox_target = runtime_root / "prewarm_dry_run" / "shared_crawl_cache.sqlite3"
    exit_code = _run_scan(
        runtime_root, "--dry-run", "--skip-detail", "--cache-path", str(sandbox_target)
    )
    assert exit_code == 2


def test_scan_cli_dry_run_without_source_cache_records_into_sandbox(tmp_path):
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
    # 生产路径文件一律不存在(缺失的源缓存也绝不被创建)
    assert not (runtime_root / "customer_prewarm.sqlite3").exists()
    assert not (runtime_root / "customer_prewarm.lock").exists()
    assert not (runtime_root / "shared_crawl_cache.sqlite3").exists()
    # 写入全部落在 sandbox
    assert (sandbox_root / "customer_prewarm.sqlite3").is_file()
    assert (sandbox_root / "shared_crawl_cache.sqlite3").is_file()
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["dry_run"] is True
    assert summary["cache_snapshot"] == {"copied": False, "reason": "missing"}
    assert "would_record" not in summary
    assert summary["intake_count"] == 1
    assert summary["ok_synced"] == 1  # 当日行真实写入 sandbox 缓存
    pending = CustomerPrewarmStore(sandbox_root / "customer_prewarm.sqlite3").pending_documents(10)
    assert [row["document_id"] for row in pending] == ["DOC-10"]
    cache = SharedCrawlCache(sandbox_root / "shared_crawl_cache.sqlite3")
    assert cache.lookup_current("nbdeli", url) is not None


# ---- (b2) --backfill-history:只回填历史 state 库,不跑扫描相位 -------------------------


def test_scan_cli_backfill_history(tmp_path):
    runtime_root = tmp_path / "runtime"
    d1, d2 = "2026-09-06", "2026-09-05"
    url_a = "https://b2b.nbdeli.com/goods/945.html"
    url_b = "https://b2b.nbdeli.com/goods/946.html"
    _seed_state_db(runtime_root, d2, crawl_rows=[_ok_crawl_row("nbdeli", "S5", url_a, task_name="task-bf-a")])
    _seed_state_db(runtime_root, d1, crawl_rows=[_ok_crawl_row("nbdeli", "S4", url_b, task_name="task-bf-b")])
    # BUSINESS_DATE(2026-09-07)当天没有 state db → 3 天窗口缺一天
    json_output = tmp_path / "backfill-summary.json"
    exit_code = _run_scan(
        runtime_root, "--backfill-history", "3", "--skip-detail", json_output=json_output
    )
    assert exit_code == 0
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["mode"] == "backfill-history"
    assert summary["backfill"] is True
    assert summary["backfill_dates"] == [d2, d1, BUSINESS_DATE]
    assert summary["dates_scanned"] == 2
    assert summary["dates_missing"] == [BUSINESS_DATE]
    assert summary["ok_synced"] == 2
    assert summary["per_date"][d2]["ok_synced"] == 1
    assert summary["per_date"][d1]["ok_synced"] == 1
    assert summary["errors"] == []
    assert "documents_processed" not in summary  # 扫描相位未执行
    # 缓存落在生产式路径 runtime_root/shared_crawl_cache.sqlite3
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    assert cache.lookup_current("nbdeli", url_a) is not None
    assert cache.lookup_current("nbdeli", url_b) is not None
    assert cache.history("nbdeli", url_a)[0]["payload"]["business_date"] == d2


def test_scan_cli_backfill_history_range_guard(tmp_path, capsys):
    runtime_root = tmp_path / "runtime"
    for bad in ("0", "91"):
        exit_code = _run_scan(runtime_root, "--backfill-history", bad)
        assert exit_code == 2
    out = capsys.readouterr().out
    assert "--backfill-history" in out


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
