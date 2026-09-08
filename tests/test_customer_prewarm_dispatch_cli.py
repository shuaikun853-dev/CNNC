"""customer_prewarm_dispatch CLI 门禁(Issue #10 Part B)。

覆盖:--plan-only 的可证明零写入(prewarm store / shared cache / per-day
state db 三库 SHA256 与目录文件集完全不变、适配器零构造)且 §26 报告字段
计数正确;--backfill-zhaoshang-history 真证据链历史回填(harvest_existing
溯源门 + 绑定 SUCCESS crawl 行双证合一)及其幂等性、partial 标记;
argparse 硬配置错误退出码 2;dispatch 模式 happy path 冒烟
(注入计数 fake adapter,零真实远端调用)。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from openpyxl import Workbook

from app.services.customer_prewarm_dispatch import prewarm_placeholder_sku
from app.services.customer_prewarm_store import (
    CACHE_STATUS_PENDING_PREWARM,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import (
    DEFAULT_POOL_ID,
    DEFAULT_TENANT_ID,
    DailyAuditStore,
)
from app.services import customer_prewarm_dispatch as dispatch_service
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts import customer_prewarm_dispatch as cli
from tests.test_zhaoshang_history_backfill import (
    make_task_name,
    seed_exported_zhaoshang_job,
)

BUSINESS_DATE = "2026-09-07"
PREVIOUS_DATE = "2026-09-06"
BACKEND_VERSION = "legacy-v1"


def _write_mapping_workbook(path: Path) -> Path:
    """最小映射表(comix/jd);域名由 EXTRA_DOMAIN_ALIASES additive 并入。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["商城名称", "sourceId", "商城标识 identification"])
    sheet.append(["齐心商城", "SRC-COMIX", "comix"])
    sheet.append(["京东商城", "SRC-JD", "jd"])
    workbook.save(path)
    return path


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_set(directory) -> set[str]:
    return {entry.name for entry in Path(directory).iterdir()}


def _make_candidate(prewarm: CustomerPrewarmStore, url: str, *, mall: str = "", document: str) -> int:
    upsert = prewarm.upsert_link(document, url, mall=mall, lookup_url=url)
    candidate_id = int(upsert["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    return candidate_id


def _seed_daily_run(state_path: Path, business_date: str) -> int:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    DailyAuditStore(state_path)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    connection = sqlite3.connect(str(state_path), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """INSERT INTO daily_runs (business_date, tenant_id, pool_id, state,
                                       created_at, updated_at)
               VALUES (?, ?, ?, 'OPEN', ?, ?)""",
            (business_date, DEFAULT_TENANT_ID, DEFAULT_POOL_ID, now, now),
        )
        connection.execute("COMMIT")
        return int(cursor.lastrowid)
    finally:
        connection.close()


def _seed_unfinished_bound_job(runtime_root: Path, business_date: str, mall_key: str, url: str) -> Path:
    """未完成绑定任务:crawl 行(SUBMITTED, 绑定) + remote_jobs(SUBMITTED, 有身份)。"""
    state_path = runtime_root / business_date / "state.sqlite3"
    run_id = _seed_daily_run(state_path, business_date)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    connection = sqlite3.connect(str(state_path), isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """INSERT INTO remote_jobs (run_id, logical_key, task_name, payload_hash, mall_key,
                                        state, remote_identity, post_attempted, created_at, updated_at)
               VALUES (?, 'seed-logical', 'cnnc_current_opt_seed_00000000000001', 'seed-hash', ?,
                       'SUBMITTED', ?, 1, ?, ?)""",
            (run_id, mall_key, f"{mall_key}|seed-task-19|seed-number-19", now, now),
        )
        job_id = int(cursor.lastrowid)
        connection.execute(
            """INSERT INTO crawl_work (run_id, mall_key, source_id, sku_id, exact_url,
                                       backend_version, fingerprint, state, remote_job_id, updated_at)
               VALUES (?, ?, 'SRC-COMIX', ?, ?, ?, 'seed-fp', 'SUBMITTED', ?, ?)""",
            (
                run_id,
                mall_key,
                prewarm_placeholder_sku(mall_key, url),
                url,
                BACKEND_VERSION,
                job_id,
                now,
            ),
        )
        connection.execute("COMMIT")
    finally:
        connection.close()
    return state_path


def _run_cli(runtime_root, workbook, *extra, json_output=None):
    argv = [
        "--business-date",
        BUSINESS_DATE,
        "--runtime-root",
        str(runtime_root),
        "--mapping-path",
        str(workbook),
        *extra,
    ]
    if json_output is not None:
        argv += ["--json-output", str(json_output)]
    return cli.main(argv)


# ---- Case 19:--plan-only 零写入 + §26 报告 ------------------------------------------


def test_case19_plan_only_is_provably_zero_write_with_full_report(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    prewarm_store_path = runtime_root / "customer_prewarm.sqlite3"
    cache_path = runtime_root / "shared_crawl_cache.sqlite3"

    prewarm = CustomerPrewarmStore(prewarm_store_path)
    cache = SharedCrawlCache(cache_path)
    state_path = _seed_unfinished_bound_job(runtime_root, BUSINESS_DATE, "comix", "https://qx.com/product-19reuse.html")

    _make_candidate(prewarm, "https://qx.com/product-19hit.html", mall="comix", document="D1")
    cache.record_success(
        mall="comix",
        submitted_url="https://qx.com/product-19hit.html",
        source="daily-internet",
        price="1.9",
    )
    _make_candidate(prewarm, "https://qx.com/product-19reuse.html", mall="comix", document="D2")
    _make_candidate(prewarm, "https://qx.com/product-19new.html", mall="comix", document="D3")
    _make_candidate(prewarm, "https://item.jd.com/100.html", mall="jd", document="D4")
    _make_candidate(prewarm, "https://www.stbchina.cn/item/19.html", document="D5")
    _make_candidate(prewarm, "https://detail.tmall.com/item.htm?id=19", document="D6")

    hashes_before = {
        "prewarm": _sha256(prewarm_store_path),
        "cache": _sha256(cache_path),
        "state": _sha256(state_path),
    }
    files_before = {
        "runtime": _file_set(runtime_root),
        "day": _file_set(runtime_root / BUSINESS_DATE),
    }

    class _NoAdapter:
        def __init__(self, *args, **kwargs):
            raise AssertionError("plan-only must never construct the legacy adapter")

    monkeypatch.setattr(dispatch_service, "CnncLegacyZhaoshangAdapter", _NoAdapter)

    json_output = tmp_path / "plan-summary.json"
    exit_code = _run_cli(runtime_root, _write_mapping_workbook(tmp_path / "mapping.xlsx"), "--plan-only", json_output=json_output)

    assert exit_code == 0
    assert _sha256(prewarm_store_path) == hashes_before["prewarm"]
    assert _sha256(cache_path) == hashes_before["cache"]
    assert _sha256(state_path) == hashes_before["state"]
    # 只读访问 WAL 库可能留下瞬态 -shm/-wal 伴生(与 scanner CLI 门禁同一约定);
    # 主库文件之外不允许出现任何新文件。
    new_runtime_files = _file_set(runtime_root) - files_before["runtime"]
    assert all(name.endswith(("-shm", "-wal")) for name in new_runtime_files)
    new_day_files = _file_set(runtime_root / BUSINESS_DATE) - files_before["day"]
    assert all(name.endswith(("-shm", "-wal")) for name in new_day_files)

    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["outcome"] == "OK"
    assert summary["plan_only"] is True
    assert summary["candidates_total"] == 6
    assert summary["cache_hit_after_recheck"] == 1
    assert summary["staples_skipped"] == 1
    assert summary["manual_skipped"] == 1
    assert summary["mapped_zhaoshang"] == 3
    assert summary["comix_qx_count"] == 2  # 复用 + 新建(命中行已在复核处先行返回)
    assert summary["ofs_officemate_count"] == 0
    assert summary["staples_stbchina_count"] == 1
    assert summary["tmall_manual_skip_count"] == 1
    assert summary["existing_remote_reused_planned"] == 1
    assert summary["new_remote_urls_planned"] == 2
    assert summary["new_remote_jobs_planned"] == 2  # comix + jd 两个商场组
    assert summary["priority_updates_planned"] == 3  # 1 个可 boost 复用 + 2 个新建
    assert summary["by_mall"] == {
        "comix": {"reused": 1, "created_urls": 1},
        "jd": {"reused": 0, "created_urls": 1},
    }
    assert summary["batch_sizes"] == [1, 1]
    # plan-only 绝不落任何候选状态变化。
    assert all(
        row["cache_status"] == CACHE_STATUS_PENDING_PREWARM
        for row in CustomerPrewarmStore(prewarm_store_path).candidates()
    )


# ---- Case 18:--backfill-zhaoshang-history 真证据链回填(幂等)-------------------------
#
# Review Round 2 Finding 2 重写:回填不再 mirror 任意 SUCCESS crawl 行,只
# 记录经真实 harvest_existing 溯源门 + 绑定 SUCCESS crawl 行双证合一的行
# (证据链种子见 tests/test_zhaoshang_history_backfill.py)。


def test_case18_backfill_zhaoshang_history_is_evidence_based_and_idempotent(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    seed_exported_zhaoshang_job(
        runtime_root,
        PREVIOUS_DATE,
        sku_id="SKU-18A",
        url="https://item.jd.com/18a.html",
        task_name=make_task_name(1),
    )
    seed_exported_zhaoshang_job(
        runtime_root,
        BUSINESS_DATE,
        sku_id="SKU-18B",
        url="https://item.jd.com/18b.html",
        task_name=make_task_name(2),
    )

    json_output = tmp_path / "backfill.json"
    exit_code = _run_cli(runtime_root, _write_mapping_workbook(tmp_path / "mapping.xlsx"), "--backfill-zhaoshang-history", "2", json_output=json_output)
    assert exit_code == 0
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["mode"] == "backfill-zhaoshang-history"
    assert summary["backfill_dates"] == [PREVIOUS_DATE, BUSINESS_DATE]
    assert summary["jobs_examined"] == 2
    assert summary["jobs_validated"] == 2
    assert summary["rows_recorded"] == 2
    assert summary["already_ingested_cross_source"] == 0
    assert summary["partial"] is False

    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    hit_a = cache.lookup_current("jd", "https://item.jd.com/18a.html")
    hit_b = cache.lookup_current("jd", "https://item.jd.com/18b.html")
    assert hit_a is not None and hit_b is not None
    assert hit_a["entry"]["source"] == "zhaoshang-history"
    assert hit_b["entry"]["source"] == "zhaoshang-history"
    rows_after_first = _cache_row_count(runtime_root / "shared_crawl_cache.sqlite3")
    assert rows_after_first == 2

    # 幂等:重跑只报 already_ingested_cross_source,缓存行数不变。
    json_output_2 = tmp_path / "backfill-2.json"
    exit_code = _run_cli(runtime_root, _write_mapping_workbook(tmp_path / "mapping.xlsx"), "--backfill-zhaoshang-history", "2", json_output=json_output_2)
    assert exit_code == 0
    summary2 = json.loads(json_output_2.read_text(encoding="utf-8"))
    assert summary2["rows_recorded"] == 0
    assert summary2["already_ingested_cross_source"] == 2
    assert _cache_row_count(runtime_root / "shared_crawl_cache.sqlite3") == rows_after_first


def test_backfill_partial_job_prints_marker_and_exits_zero(tmp_path, capsys):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    seeded = seed_exported_zhaoshang_job(runtime_root, BUSINESS_DATE, task_name=make_task_name(3))
    Path(seeded["result_path"]).unlink()

    json_output = tmp_path / "backfill-partial.json"
    exit_code = _run_cli(runtime_root, _write_mapping_workbook(tmp_path / "mapping.xlsx"), "--backfill-zhaoshang-history", "1", json_output=json_output)

    assert exit_code == 0  # partial 是文档化结果,不是失败
    stdout = capsys.readouterr().out
    assert "HISTORICAL_BACKFILL_PARTIAL" in stdout
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["partial"] is True
    assert summary["jobs_examined"] == 1
    assert summary["jobs_validated"] == 0
    assert len(summary["jobs_partial"]) == 1
    assert summary["rows_recorded"] == 0
    assert _cache_row_count(runtime_root / "shared_crawl_cache.sqlite3") == 0


def _cache_row_count(cache_path: Path) -> int:
    connection = sqlite3.connect(str(cache_path))
    try:
        return int(connection.execute("SELECT COUNT(*) FROM crawl_results").fetchone()[0])
    finally:
        connection.close()


# ---- argparse 硬配置错误:退出码 2 ----------------------------------------------------


def test_arg_validation_backfill_zero_fails_closed(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    exit_code = _run_cli(runtime_root, _write_mapping_workbook(tmp_path / "mapping.xlsx"), "--backfill-zhaoshang-history", "0")
    assert exit_code == 2


def test_arg_validation_budget_zero_fails_closed(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    exit_code = _run_cli(runtime_root, _write_mapping_workbook(tmp_path / "mapping.xlsx"), "--budget-seconds", "0")
    assert exit_code == 2


def test_arg_validation_backfill_with_plan_only_fails_closed(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    exit_code = _run_cli(runtime_root, _write_mapping_workbook(tmp_path / "mapping.xlsx"), "--backfill-zhaoshang-history", "2", "--plan-only")
    assert exit_code == 2


# ---- dispatch 模式 happy path 冒烟(fake adapter,零真实远端调用)----------------------


class _RecordingAdapter:
    instances: list["_RecordingAdapter"] = []
    submit_calls = 0
    prioritize_calls = 0

    def __init__(self, *, mappings=None):
        self.mappings = tuple(mappings or ())
        _RecordingAdapter.instances.append(self)

    def prepare_job(self, business_date, requests, template_path, result_path, task_name, batch_state_path):
        template_path = Path(template_path)
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text("immutable-prewarm", encoding="utf-8")
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(
            {
                "business_date": business_date,
                "requests": sorted(request.identity for request in requests),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        class _Prepared:
            pass

        prepared = _Prepared()
        prepared.business_date = business_date
        prepared.mall_key = requests[0].mall_key
        prepared.mall_source_id = requests[0].mall_source_id
        prepared.mall_name = requests[0].mall_name
        prepared.task_name = task_name
        prepared.template_path = template_path
        prepared.result_path = Path(result_path)
        prepared.batch_state_path = Path(batch_state_path)
        prepared.requests = tuple(requests)
        prepared.logical_job_key = "logical-" + hashlib.sha256(canonical).hexdigest()[:16]
        prepared.payload_sha256 = "payload-" + hashlib.sha256(canonical).hexdigest()[:16]
        return prepared

    def submit(self, _job, *, timeout_seconds):
        _RecordingAdapter.submit_calls += 1
        return _FakeOutcome("SUBMITTED", task_id="remote-cli", task_number="number-cli",
                            priority_evidence="updated")

    def prioritize(self, _job, *, task_id, task_number, timeout_seconds):
        _RecordingAdapter.prioritize_calls += 1
        return _FakeOutcome("PRIORITY_UPDATED", weight_type=-1)


class _FakeOutcome:
    def __init__(self, status, **attrs):
        self.status = status
        self.__dict__.update(attrs)


def test_dispatch_mode_happy_path_smoke(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    _make_candidate(prewarm, "https://qx.com/product-smoke.html", mall="comix", document="D-SMOKE")
    _seed_daily_run(runtime_root / BUSINESS_DATE / "state.sqlite3", BUSINESS_DATE)

    _RecordingAdapter.instances = []
    _RecordingAdapter.submit_calls = 0
    _RecordingAdapter.prioritize_calls = 0
    monkeypatch.setattr(dispatch_service, "CnncLegacyZhaoshangAdapter", _RecordingAdapter)

    json_output = tmp_path / "dispatch-summary.json"
    exit_code = _run_cli(runtime_root, _write_mapping_workbook(tmp_path / "mapping.xlsx"), json_output=json_output)

    assert exit_code == 0
    assert len(_RecordingAdapter.instances) == 1
    assert _RecordingAdapter.submit_calls == 1
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["outcome"] == "OK"
    assert summary["jobs_created"] == 1
    assert summary["plan_only"] is False
    candidate = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3").candidates()[0]
    assert candidate["cache_status"] == "REMOTE_DISPATCHED"
    assert candidate["remote_job_id"] is not None


# ---- Review Round 2 Finding 1:plan-only 零构造 + 三数据源分离 ------------------------


def _snapshot_tree(root: Path) -> set[str]:
    return {str(entry.relative_to(root)) for entry in root.rglob("*")}


def test_case_a_plan_only_missing_prewarm_store_creates_nothing(tmp_path):
    runtime_root = tmp_path / "runtime"
    state_path = runtime_root / BUSINESS_DATE / "state.sqlite3"
    _seed_daily_run(state_path, BUSINESS_DATE)
    before = _snapshot_tree(runtime_root)
    assert "customer_prewarm.sqlite3" not in before

    json_output = tmp_path / "plan-a.json"
    exit_code = _run_cli(runtime_root, _write_mapping_workbook(tmp_path / "mapping.xlsx"), "--plan-only", json_output=json_output)

    assert exit_code == 0
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["outcome"] == "PREWARM_STORE_MISSING"
    assert summary["plan_only"] is True
    # plan-only 不取锁、不建库:除 state 库的瞬态 wal/shm 伴生外,什么都不允许出现。
    new_files = _snapshot_tree(runtime_root) - before
    assert all(name.endswith(("-wal", "-shm")) for name in new_files)
    assert not (runtime_root / "customer_prewarm.sqlite3").exists()
    assert not (runtime_root / "shared_crawl_cache.sqlite3").exists()
    assert state_path.is_file()


def test_case_b_plan_only_reads_sandbox_stores_and_production_state(tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    production = tmp_path / "production"
    sandbox.mkdir()
    production.mkdir()
    prewarm_store_path = sandbox / "customer_prewarm.sqlite3"
    cache_path = sandbox / "shared_crawl_cache.sqlite3"
    prewarm = CustomerPrewarmStore(prewarm_store_path)
    cache = SharedCrawlCache(cache_path)
    _make_candidate(prewarm, "https://qx.com/product-b-reuse.html", mall="comix", document="D1")
    _make_candidate(prewarm, "https://qx.com/product-b-hit.html", mall="comix", document="D2")
    _make_candidate(prewarm, "https://qx.com/product-b-new.html", mall="comix", document="D3")
    _make_candidate(prewarm, "https://www.stbchina.cn/item/b.html", document="D4")
    _make_candidate(prewarm, "https://detail.tmall.com/item.htm?id=b", document="D5")
    cache.record_success(
        mall="comix",
        submitted_url="https://qx.com/product-b-hit.html",
        source="daily-internet",
        price="1.1",
    )
    state_path = _seed_unfinished_bound_job(production, BUSINESS_DATE, "comix", "https://qx.com/product-b-reuse.html")

    hashes_before = {
        "prewarm": _sha256(prewarm_store_path),
        "cache": _sha256(cache_path),
        "state": _sha256(state_path),
    }
    files_before = {"sandbox": _snapshot_tree(sandbox), "production": _snapshot_tree(production)}

    class _NoAdapter:
        def __init__(self, *args, **kwargs):
            raise AssertionError("plan-only must never construct the legacy adapter")

    monkeypatch.setattr(dispatch_service, "CnncLegacyZhaoshangAdapter", _NoAdapter)

    json_output = tmp_path / "plan-b.json"
    exit_code = _run_cli(
        production,
        _write_mapping_workbook(tmp_path / "mapping.xlsx"),
        "--plan-only",
        "--state-runtime-root",
        str(production),
        "--prewarm-store-path",
        str(prewarm_store_path),
        "--shared-cache-path",
        str(cache_path),
        json_output=json_output,
    )

    assert exit_code == 0
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    # 候选/缓存读自 A(sandbox),远端任务探测读自 B(production)。
    assert summary["outcome"] == "OK"
    assert summary["plan_only"] is True
    assert summary["candidates_total"] == 5
    assert summary["cache_hit_after_recheck"] == 1
    assert summary["staples_skipped"] == 1
    assert summary["manual_skipped"] == 1
    assert summary["mapped_zhaoshang"] == 2
    assert summary["existing_remote_reused_planned"] == 1
    assert summary["new_remote_urls_planned"] == 1
    assert summary["new_remote_jobs_planned"] == 1
    assert summary["priority_updates_planned"] == 2  # 可 boost 复用 + 新建
    assert summary["comix_qx_count"] == 2
    assert summary["by_mall"] == {"comix": {"reused": 1, "created_urls": 1}}
    assert summary["batch_sizes"] == [1]
    # A 与 B 的主库字节不变;目录除瞬态 wal/shm 外无新增。
    assert _sha256(prewarm_store_path) == hashes_before["prewarm"]
    assert _sha256(cache_path) == hashes_before["cache"]
    assert _sha256(state_path) == hashes_before["state"]
    assert all(name.endswith(("-wal", "-shm")) for name in _snapshot_tree(sandbox) - files_before["sandbox"])
    assert all(name.endswith(("-wal", "-shm")) for name in _snapshot_tree(production) - files_before["production"])


def test_case_c_plan_only_nonexistent_store_path_reports_missing(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    _seed_daily_run(runtime_root / BUSINESS_DATE / "state.sqlite3", BUSINESS_DATE)
    ghost = tmp_path / "ghost" / "customer_prewarm.sqlite3"

    json_output = tmp_path / "plan-c.json"
    exit_code = _run_cli(
        runtime_root,
        _write_mapping_workbook(tmp_path / "mapping.xlsx"),
        "--plan-only",
        "--prewarm-store-path",
        str(ghost),
        json_output=json_output,
    )

    assert exit_code == 0
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    # 明确的诊断结果,绝不静默返回 candidates_total=0 的空报告。
    assert summary["outcome"] == "PREWARM_STORE_MISSING"
    assert Path(summary["missing_path"]).resolve() == ghost.resolve()
    assert "candidates_total" not in summary
    assert not ghost.exists()
    assert not ghost.parent.exists()


def test_plan_only_missing_shared_cache_reports_cache_outcome(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    _seed_daily_run(runtime_root / BUSINESS_DATE / "state.sqlite3", BUSINESS_DATE)
    CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")

    json_output = tmp_path / "plan-cache-missing.json"
    exit_code = _run_cli(runtime_root, _write_mapping_workbook(tmp_path / "mapping.xlsx"), "--plan-only", json_output=json_output)

    assert exit_code == 0
    summary = json.loads(json_output.read_text(encoding="utf-8"))
    assert summary["outcome"] == "SHARED_CACHE_MISSING"
    assert not (runtime_root / "shared_crawl_cache.sqlite3").exists()
