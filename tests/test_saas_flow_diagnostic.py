"""STEP_1 Dashboard SaaS Flow Diagnostic 测试。

契约:docs/superpowers/specs/2026-08-27-step1-saas-flow-diagnostic.md。
全部用 fake connection / mock cursor / tmp SQLite,不连接任何真实数据库,
不执行任何生产 SQL。字段语义来源:docs/sql_ops KB(FRONTEND/LIVE-SQL/SCHEMA)。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import saas_flow_diagnostic as diag


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    """只接受 registry 内预定义 SQL;按查询名返回 canned 行。"""

    def __init__(self, canned: dict, executed: list) -> None:
        self._canned = canned
        self._executed = executed
        self._rows: list = []

    def execute(self, sql: str, params: dict | None = None) -> None:
        normalized = " ".join(sql.split())
        matches = [name for name, text in diag.DIAG_SQL_REGISTRY.items()
                   if " ".join(text.split()) == normalized]
        assert matches, "attempted to execute SQL outside the predefined registry"
        name = matches[0]
        if name in self._canned and callable(self._canned[name]):
            self._rows = self._canned[name](dict(params or {}))
        else:
            self._rows = list(self._canned.get(name, []))
        self._executed.append((name, dict(params or {})))

    def fetchall(self) -> list:
        return list(self._rows)

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, canned: dict, executed: list) -> None:
        self._canned = canned
        self._executed = executed
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._canned, self._executed)

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


_DEFAULT_CONFIG = object()  # sentinel:区分“未传 config”与“显式传 None(未配置)”


def make_service(canned: dict, *, config=_DEFAULT_CONFIG, clock=None, runtime_root=None,
                 grace_seconds=1800, ttl=60.0, fresh_min_interval=30.0, now_fn=None,
                 live_fetcher=None):
    clock = clock or FakeClock()
    executed: list = []
    conn = FakeConnection(canned, executed)
    calls = {"connect": 0}

    def factory(cfg):
        calls["connect"] += 1
        return conn

    if config is _DEFAULT_CONFIG:
        config = diag.DiagDBConfig(
            host="db.example.internal", port=3306, user="ro", password="secret-not-committed")
    service = diag.SaasFlowDiagnosticService(
        config=config,
        connection_factory=factory,
        monotonic=clock,
        runtime_root=runtime_root,
        grace_seconds=grace_seconds,
        cache_ttl_seconds=ttl,
        fresh_read_min_interval_seconds=fresh_min_interval,
        now_fn=now_fn,
        live_counts_fetcher=live_fetcher,
    )
    return service, executed, calls


NOW_2026_08_27_10 = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# A. SQL 安全
# ---------------------------------------------------------------------------

def test_registry_statements_are_readonly_single_select():
    for name, sql in diag.DIAG_SQL_REGISTRY.items():
        problems = diag.validate_readonly_statement(sql)
        assert not problems, f"{name}: {problems}"


def test_validate_readonly_statement_flags_writes_and_multistatement():
    for bad in (
        "UPDATE saas_compare_price.commodity_list_table_188605 SET crawl_status=2",
        "DELETE FROM x WHERE id=1",
        "INSERT INTO x VALUES (1)",
        "SELECT 1; DELETE FROM x",
        "SELECT 1 INTO OUTFILE '/tmp/x'",
        "CREATE TABLE t (id INT)",
        "DROP TABLE t",
        "TRUNCATE TABLE t",
        "ALTER TABLE t ADD c INT",
        "REPLACE INTO t VALUES (1)",
    ):
        assert diag.validate_readonly_statement(bad), bad


def test_validate_readonly_statement_accepts_plain_select_and_cte():
    assert diag.validate_readonly_statement("SELECT COUNT(*) FROM t WHERE id = %(id)s") == []
    assert diag.validate_readonly_statement(
        "WITH a AS (SELECT 1 AS n) SELECT n FROM a") == []


def test_fixed_scope_constants():
    assert diag.TENANT_ID == "188605"
    assert diag.POOL_ID == "222"


def test_registry_never_joins_task_and_search_tables():
    """性能硬约束:任何语句不得同时引用 task_table 与 search_table(一对多 cross-product)。"""
    for name, sql in diag.DIAG_SQL_REGISTRY.items():
        low = sql.lower()
        assert not ("task_table" in low and "search_table" in low), name


def test_registry_search_aggregates_are_grouped():
    for name, sql in diag.DIAG_SQL_REGISTRY.items():
        if "search_table" in sql.lower():
            assert "group by" in sql.lower(), f"{name} 引用 search_table 必须 GROUP BY 聚合"


def test_queries_always_use_fixed_pool_and_day_window():
    service, executed, _ = make_service({
        "pool_sync_standard_counts": [("0", 5)],
        "open_crawl_status_counts": [(1, 0, 0, 0, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] in ("ok", "partial")
    for name, params in executed:
        assert params.get("pool_id") == "222", (name, params)
        assert params.get("day_start") == "2026-08-27 00:00:00"
        assert params.get("day_end") == "2026-08-28 00:00:00"


# ---------------------------------------------------------------------------
# B. DB adapter / 配置 / 降级 / 缓存 / 限流
# ---------------------------------------------------------------------------

def test_diag_db_config_from_env_requires_all_variables():
    full = {"CNNC_DIAG_DB_HOST": "h", "CNNC_DIAG_DB_PORT": "3306",
            "CNNC_DIAG_DB_USER": "u", "CNNC_DIAG_DB_PASSWORD": "p"}
    cfg = diag.DiagDBConfig.from_env(full)
    assert cfg is not None and cfg.host == "h" and cfg.port == 3306
    assert diag.DiagDBConfig.from_env({k: v for k, v in full.items() if k != "CNNC_DIAG_DB_HOST"}) is None
    assert diag.DiagDBConfig.from_env({}) is None


def test_diag_db_config_reads_ignored_env_file(tmp_path):
    env_file = tmp_path / "CNNC_DIAG_DB.env"
    env_file.write_text(
        "# comment\nCNNC_DIAG_DB_HOST=file-host\nCNNC_DIAG_DB_PORT=3307\n"
        "CNNC_DIAG_DB_USER=\"file-user\"\nCNNC_DIAG_DB_PASSWORD='file-pass'\n",
        encoding="utf-8")
    cfg = diag.resolve_diag_db_config(environ={}, env_file=env_file)
    assert cfg is not None and cfg.host == "file-host" and cfg.port == 3307
    assert cfg.user == "file-user" and cfg.password == "file-pass"


def test_diag_db_config_process_env_wins_over_file(tmp_path):
    env_file = tmp_path / "CNNC_DIAG_DB.env"
    env_file.write_text("CNNC_DIAG_DB_HOST=file-host\nCNNC_DIAG_DB_USER=fu\n"
                        "CNNC_DIAG_DB_PASSWORD=fp\nCNNC_DIAG_DB_PORT=3306\n", encoding="utf-8")
    cfg = diag.resolve_diag_db_config(
        environ={"CNNC_DIAG_DB_HOST": "proc-host", "CNNC_DIAG_DB_PORT": "3306",
                 "CNNC_DIAG_DB_USER": "pu", "CNNC_DIAG_DB_PASSWORD": "pp"},
        env_file=env_file)
    assert cfg.host == "proc-host"


def test_unconfigured_env_returns_unconfigured_payload_without_db_calls():
    service, executed, _ = make_service({"pool_sync_standard_counts": [("0", 1)]}, config=None)
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "unconfigured"
    assert payload["configured"] is False
    assert payload["stages"] is None
    assert executed == []
    # 增长部分仍可评估(本地持久证据),但当日无 runtime 时标记不可用
    assert payload["growth"]["available"] is False


def test_connect_failure_degrades_to_unavailable_without_credentials():
    def broken_factory(cfg):
        raise ConnectionError("connect refused")

    service = diag.SaasFlowDiagnosticService(
        config=diag.DiagDBConfig(host="db.example.internal", port=3306, user="ro", password="secret-not-committed"),
        connection_factory=broken_factory, monotonic=FakeClock())
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "unavailable"
    text = json.dumps(payload, ensure_ascii=False)
    assert "secret-not-committed" not in text and "db.example.internal" not in text
    assert "ro" != payload.get("error", {}).get("type", "")


def test_single_query_failure_yields_partial_not_total_outage():
    def spider_raises(params):
        raise RuntimeError("spider query failed")

    service, executed, _ = make_service({
        "pool_sync_standard_counts": [("0", 3)],
        "open_crawl_status_counts": [(3, 0, 1, 2, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
        "crawl1_spider_task_rows": spider_raises,
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "partial"
    assert "crawl1_spider_task_rows" in payload["degraded_queries"]
    # spider 失败 → 商品级证据不足,不得冒充"未发现推送记录"
    assert payload["stages"]["crawler_branch"]["EVIDENCE_INCOMPLETE"] == 1
    assert payload["stages"]["crawler_branch"]["PUSH_RECORD_NOT_FOUND"] == 0
    # 其他查询的分类照常返回
    assert payload["stages"]["crawl_status_1"] == 1
    assert payload["stages"]["crawl_status_2"] == 2


def test_all_core_queries_fail_is_unavailable():
    def boom(params):
        raise RuntimeError("db down")

    service, _, _ = make_service({
        "pool_sync_standard_counts": boom,
        "open_crawl_status_counts": boom,
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "unavailable"


def test_cache_hit_within_ttl():
    service, _, calls = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 0, 0, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [],
    })
    first = service.get_diagnostic("2026-08-27")
    assert first["cached"] is False
    second = service.get_diagnostic("2026-08-27")
    assert second["cached"] is True
    assert calls["connect"] == 1


def test_cache_expiry_triggers_fresh_read():
    clock = FakeClock()
    service, _, calls = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 0, 0, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [],
    }, clock=clock)
    service.get_diagnostic("2026-08-27")
    clock.advance(61)
    service.get_diagnostic("2026-08-27")
    assert calls["connect"] == 2


def test_forced_refresh_is_rate_limited():
    clock = FakeClock()
    service, _, calls = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 0, 0, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [],
    }, clock=clock)
    service.get_diagnostic("2026-08-27", force_refresh=True)
    blocked = service.get_diagnostic("2026-08-27", force_refresh=True)
    assert blocked["rate_limited"] is True
    assert calls["connect"] == 1
    clock.advance(31)
    again = service.get_diagnostic("2026-08-27", force_refresh=True)
    assert again["rate_limited"] is not True
    assert calls["connect"] == 2


def test_payload_contains_fixed_scope_and_no_credentials():
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 0, 0, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["scope"] == {"tenant_id": "188605", "commodity_pool_id": "222"}
    text = json.dumps(payload, ensure_ascii=False)
    assert "secret-not-committed" not in text


# ---------------------------------------------------------------------------
# C. crawl_status 路由
# ---------------------------------------------------------------------------

FULL_CANNED = {
    "pool_sync_standard_counts": [("0", 4)],
    # 单行:(routing_open_total, no_commodity_list, crawl_status_1, crawl_status_2, crawl_status_other)
    "open_crawl_status_counts": [(4, 0, 1, 3, 0)],
    "crawl1_commodity_rows": [(11, "C1")],
    "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
    # R2:task → batch(188605_701) → spider 推送记录(is_call=0) → callback 在途
    "crawl1_spider_task_rows": [("188605_701", "E1", 0)],
    "crawl1_callback_rows": [("E1", "8888", "active")],
    # 列:commodity_list_id, search_rows, pool_created_at, model_pending,
    #     has_internet_same, internet_pending_approval, same_approved,
    #     approval_non_same_exists, has_reference_same, has_internet_candidate,
    #     reference_pending_approval, has_model_verdict
    "crawl2_search_flags": [
        (21, 0, "2026-08-27 09:00:00", 0, 0, 0, 0, 0, 0, 0, 0, 0),
        (22, 5, "2026-08-27 08:00:00", 0, 1, 1, 0, 0, 0, 0, 0, 0),
        (23, 5, "2026-08-27 08:00:00", 0, 0, 0, 0, 0, 0, 1, 0, 0),
    ],
}


def test_crawl_status_routes_into_separate_branches():
    service, _, _ = make_service(dict(FULL_CANNED))
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert stages["crawl_status_1"] == 1
    assert stages["crawl_status_2"] == 3
    # crawl_status=1 商品只进入 crawler branch
    assert sum(stages["crawler_branch"].values()) == 1
    # crawl_status=2 商品只进入 downstream branch
    assert sum(stages["downstream_branch"].values()) == 3
    assert stages["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1
    assert stages["downstream_branch"]["CALLBACK_DONE_NO_SEARCH"] == 1
    assert stages["downstream_branch"]["INTERNET_SAME_PENDING_APPROVAL"] == 1
    assert stages["downstream_branch"]["INTERNET_CANDIDATES_ALL_NON_SAME"] == 1
    # 分桶必须闭合:crawl1 + crawl2 = 分支桶合计
    assert stages["crawl_status_1"] + stages["crawl_status_2"] == \
        sum(stages["crawler_branch"].values()) + sum(stages["downstream_branch"].values())


def test_open_commodities_without_commodity_list_are_reported():
    """R3:no_commodity_list 只来自 routing query 自身的 LEFT JOIN 报告;
    pool 与 routing 两个 SELECT 的计数差只能标记 pool_vs_routing drift,不得推导 residual。"""
    canned = dict(FULL_CANNED)
    canned["pool_sync_standard_counts"] = [("0", 6)]   # pool 时点:6 个未收口
    canned["open_crawl_status_counts"] = [(4, 0, 1, 3, 0)]  # routing 时点:4 个,全部在采集列表
    service, _, _ = make_service(canned)
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert stages["not_closed"] == 6
    assert stages["routing_open_total"] == 4
    assert stages["no_commodity_list"] == 0            # 绝不从 6-4 推出 2
    assert stages["snapshot_consistent"] is False
    assert "pool_vs_routing" in stages["snapshot_drift"]
    assert payload["status"] == "partial"


# ---------------------------------------------------------------------------
# D. crawler branch 分类(R2 callback 证据链,crawl_status=1)
# ---------------------------------------------------------------------------

def item_record(**kw):
    """classify_crawler_callback 的标准输入;默认给出一条健康在途证据。"""
    base = {
        "task_count": 1,
        "task_failed": False,
        "malformed": False,
        "batches": {"B1"},
        "spider_failed": False,
        "spider_rows": [{"batch_num": "B1", "exe_number": "E1", "is_call": 0}],
        "callback_failed": False,
        "callback_rows": [{"execution_number": "E1", "push_state": "8888",
                           "callback_source": "active"}],
        "exes_truncated": False,
    }
    base.update(kw)
    return base


def test_classify_task_not_created():
    assert diag.classify_crawler_callback(
        item_record(task_count=0, batches=set(), spider_rows=[],
                    callback_rows=[])) == "TASK_NOT_CREATED"


def test_classify_task_query_failure_is_evidence_incomplete():
    assert diag.classify_crawler_callback(item_record(task_failed=True)) == \
        "EVIDENCE_INCOMPLETE"


def test_classify_malformed_task_id_is_evidence_incomplete():
    assert diag.classify_crawler_callback(item_record(malformed=True)) == \
        "EVIDENCE_INCOMPLETE"


def test_classify_spider_failure_is_evidence_incomplete():
    assert diag.classify_crawler_callback(item_record(spider_failed=True)) == \
        "EVIDENCE_INCOMPLETE"


def test_classify_callback_failure_is_evidence_incomplete():
    assert diag.classify_crawler_callback(item_record(callback_failed=True)) == \
        "EVIDENCE_INCOMPLETE"


def test_classify_no_push_record():
    assert diag.classify_crawler_callback(
        item_record(spider_rows=[], callback_rows=[])) == "PUSH_RECORD_NOT_FOUND"


def test_classify_partial_batch_coverage_is_evidence_incomplete():
    # 两个 expected batch,只有一个有 spider 记录 → 不得冒充"未发现记录"
    record = item_record(
        batches={"B1", "B2"},
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 0}])
    assert diag.classify_crawler_callback(record) == "EVIDENCE_INCOMPLETE"


def test_classify_missing_exe_number_is_evidence_incomplete():
    assert diag.classify_crawler_callback(
        item_record(spider_rows=[{"batch_num": "B1", "exe_number": "", "is_call": 0}],
                    callback_rows=[])) == "EVIDENCE_INCOMPLETE"


def test_classify_all_active_8888_in_flight():
    assert diag.classify_crawler_callback(item_record()) == "CALLBACK_IN_FLIGHT"


def test_classify_all_completed_0000_done_saas_pending():
    record = item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 1}],
        callback_rows=[{"execution_number": "E1", "push_state": "0000",
                        "callback_source": "completed"}])
    assert diag.classify_crawler_callback(record) == "CALLBACK_DONE_SAAS_PENDING"


def test_classify_mixed_success_pending_partial():
    record = item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 1},
                     {"batch_num": "B1", "exe_number": "E2", "is_call": 0}],
        callback_rows=[{"execution_number": "E1", "push_state": "0000",
                        "callback_source": "completed"},
                       {"execution_number": "E2", "push_state": "8888",
                        "callback_source": "active"}])
    assert diag.classify_crawler_callback(record) == "CALLBACK_PARTIAL"


def test_classify_push_confirmed_no_callback_record():
    assert diag.classify_crawler_callback(item_record(callback_rows=[])) == \
        "PUSH_CONFIRMED_NO_CALLBACK_RECORD"


def test_classify_success_plus_no_callback_record_partial():
    record = item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 1},
                     {"batch_num": "B1", "exe_number": "E2", "is_call": 0}],
        callback_rows=[{"execution_number": "E1", "push_state": "0000",
                        "callback_source": "completed"}])
    assert diag.classify_crawler_callback(record) == "CALLBACK_PARTIAL"


def test_classify_pending_plus_no_callback_record_is_in_flight():
    """§8 锁定语义:存在真实在途回调 + 部分 execution 尚无 callback 行 → IN_FLIGHT。"""
    record = item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 0},
                     {"batch_num": "B1", "exe_number": "E2", "is_call": 0}],
        callback_rows=[{"execution_number": "E1", "push_state": "8888",
                        "callback_source": "active"}])
    assert diag.classify_crawler_callback(record) == "CALLBACK_IN_FLIGHT"


def test_classify_active_8888_with_is_call_1_is_evidence_incomplete():
    record = item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 1}])
    assert diag.classify_crawler_callback(record) == "EVIDENCE_INCOMPLETE"


def test_classify_completed_0000_with_is_call_0_is_evidence_incomplete():
    record = item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 0}],
        callback_rows=[{"execution_number": "E1", "push_state": "0000",
                        "callback_source": "completed"}])
    assert diag.classify_crawler_callback(record) == "EVIDENCE_INCOMPLETE"


def test_classify_unknown_push_state_is_evidence_incomplete_never_failed():
    for bad in ("9999", "0001", "", None):
        record = item_record(
            callback_rows=[{"execution_number": "E1", "push_state": bad,
                            "callback_source": "active"}])
        assert diag.classify_crawler_callback(record) == "EVIDENCE_INCOMPLETE", bad
        # completed 侧同样不得解释成失败
        record2 = item_record(
            spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 1}],
            callback_rows=[{"execution_number": "E1", "push_state": bad,
                            "callback_source": "completed"}])
        assert diag.classify_crawler_callback(record2) == "EVIDENCE_INCOMPLETE", bad


def test_classify_execution_in_both_tables_is_evidence_incomplete():
    record = item_record(
        callback_rows=[{"execution_number": "E1", "push_state": "8888",
                        "callback_source": "active"},
                       {"execution_number": "E1", "push_state": "0000",
                        "callback_source": "completed"}])
    assert diag.classify_crawler_callback(record) == "EVIDENCE_INCOMPLETE"


def test_classify_conflicting_push_states_same_source_is_evidence_incomplete():
    record = item_record(
        callback_rows=[{"execution_number": "E1", "push_state": "8888",
                        "callback_source": "active"},
                       {"execution_number": "E1", "push_state": "7777",
                        "callback_source": "active"}])
    assert diag.classify_crawler_callback(record) == "EVIDENCE_INCOMPLETE"


def test_classify_no_callback_row_with_is_call_1_is_evidence_incomplete():
    record = item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 1}],
        callback_rows=[])
    assert diag.classify_crawler_callback(record) == "EVIDENCE_INCOMPLETE"


def test_classify_duplicate_callback_rows_do_not_change_classification():
    doubled = item_record(callback_rows=[
        {"execution_number": "E1", "push_state": "8888", "callback_source": "active"},
        {"execution_number": "E1", "push_state": "8888", "callback_source": "active"}])
    assert diag.classify_crawler_callback(doubled) == "CALLBACK_IN_FLIGHT"


def test_classify_exes_truncated_is_evidence_incomplete():
    assert diag.classify_crawler_callback(item_record(exes_truncated=True)) == \
        "EVIDENCE_INCOMPLETE"


# ---- R2 Review(2026-08-28)P1-1:is_call 缺值/未知值 ≠ 业务 0 ----

def test_r2_review_p1_1_is_call_missing_or_unknown_values_are_evidence_incomplete():
    """原实现 `int(r.get("is_call") or 0)`:None/"" 经 `or 0` 被当成未回调(0),
    可误分类为 CALLBACK_IN_FLIGHT / PUSH_CONFIRMED_NO_CALLBACK_RECORD。
    fail-safe 契约:关键证据缺失 ≠ 业务状态 0。"""
    for bad in (None, "", "abc", 2, "2", -1, True, False):
        record = item_record(spider_rows=[{"batch_num": "B1", "exe_number": "E1",
                                           "is_call": bad}])
        assert diag.classify_crawler_callback(record) == "EVIDENCE_INCOMPLETE", repr(bad)
    # 与 push_state 组合也绝不借缺值兜底成"在途/无记录"
    for bad in (None, "", "2"):
        record = item_record(callback_rows=[], spider_rows=[
            {"batch_num": "B1", "exe_number": "E1", "is_call": bad}])
        assert diag.classify_crawler_callback(record) == "EVIDENCE_INCOMPLETE", repr(bad)


def test_r2_review_p1_1_is_call_explicit_zero_one_accepted():
    """只有明确的 0/"0"/1/"1"(MySQL 实际 int/varchar 表征)按业务值解释。"""
    assert diag.classify_crawler_callback(item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 0}])) == \
        "CALLBACK_IN_FLIGHT"
    assert diag.classify_crawler_callback(item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": "0"}],
        callback_rows=[])) == "PUSH_CONFIRMED_NO_CALLBACK_RECORD"
    done = [{"execution_number": "E1", "push_state": "0000", "callback_source": "completed"}]
    assert diag.classify_crawler_callback(item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": 1}],
        callback_rows=done)) == "CALLBACK_DONE_SAAS_PENDING"
    assert diag.classify_crawler_callback(item_record(
        spider_rows=[{"batch_num": "B1", "exe_number": "E1", "is_call": "1"}],
        callback_rows=done)) == "CALLBACK_DONE_SAAS_PENDING"


# ---- R2 Review P1-2:callback 查询失败的影响范围收窄到依赖 callback 的商品 ----

def test_r2_review_p1_2_callback_failure_does_not_pollute_upstream_states():
    """callback 是下游证据。上游不依赖 callback 的确定状态
    (PUSH_RECORD_NOT_FOUND / TASK_NOT_CREATED)不受全局 callback 失败污染。"""
    assert diag.classify_crawler_callback(
        item_record(spider_rows=[], callback_rows=[], callback_failed=True)) == \
        "PUSH_RECORD_NOT_FOUND"
    assert diag.classify_crawler_callback(
        item_record(task_count=0, batches=set(), spider_rows=[], callback_failed=True)) == \
        "TASK_NOT_CREATED"
    # 存在完整 spider push 证据的商品仍按原契约降级(下游证据缺失)
    assert diag.classify_crawler_callback(item_record(callback_failed=True)) == \
        "EVIDENCE_INCOMPLETE"


def test_r2_review_p1_2_callback_failure_scope_item_level_integration():
    """集成锁定(_build_stages 级):全局 crawl1_callback_rows 查询失败只降级
    依赖 callback 证据的 ITEM;无 spider 推送记录的 ITEM 保持
    PUSH_RECORD_NOT_FOUND;payload partial、degraded 含该查询、分支闭合。"""
    def callback_raises(params):
        raise RuntimeError("callback query failed")

    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 2)],
        "open_crawl_status_counts": [(2, 0, 2, 0, 0)],
        # ITEM_A:task 存在、spider 查询成功、无推送记录(不产生 execution)
        # ITEM_B:task + 推送记录 + execution_number
        "crawl1_commodity_rows": [(11, "CA"), (12, "CB")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1), (12, "SAAS-188605_702_1", 1)],
        "crawl1_spider_task_rows": [("188605_702", "E2", 0)],
        "crawl1_callback_rows": callback_raises,
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "partial"
    assert "crawl1_callback_rows" in payload["degraded_queries"]
    crawler = payload["stages"]["crawler_branch"]
    assert crawler["PUSH_RECORD_NOT_FOUND"] == 1   # ITEM_A 上游确定状态
    assert crawler["EVIDENCE_INCOMPLETE"] == 1     # ITEM_B 依赖 callback → 降级
    assert sum(crawler.values()) == payload["stages"]["crawl_status_1"] == 2


# ---- R2 Review P2:all_batches 独立硬上限 + item 级截断 fail-safe ----

def test_r2_review_p2_batches_truncated_is_evidence_incomplete():
    """batch 未完整进入有界 spider 查询的商品:即使查到的部分无任何记录,
    也只能证据不足——缺失可能是截断造成,不得冒充 PUSH_RECORD_NOT_FOUND。"""
    assert diag.classify_crawler_callback(
        item_record(batches_truncated=True)) == "EVIDENCE_INCOMPLETE"
    assert diag.classify_crawler_callback(
        item_record(batches_truncated=True, spider_rows=[], callback_rows=[])) == \
        "EVIDENCE_INCOMPLETE"


def test_r2_review_p2_batches_within_cap_classify_normally():
    """A/F:batch 总数在上限内时行为与无上限时完全一致,参数全为 str。"""
    service, executed, _ = make_service({
        "pool_sync_standard_counts": [("0", 2)],
        "open_crawl_status_counts": [(2, 0, 2, 0, 0)],
        "crawl1_commodity_rows": [(11, "CA"), (12, "CB")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1), (12, "SAAS-188605_702_1", 1)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0), ("188605_702", "E2", 0)],
        "crawl1_callback_rows": [("E1", "8888", "active"), ("E2", "8888", "active")],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    spider_calls = [params for name, params in executed
                    if name == "crawl1_spider_task_rows"]
    assert len(spider_calls) == 1 and len(spider_calls[0]["batch_nums"]) == 2
    assert all(isinstance(v, str) for v in spider_calls[0]["batch_nums"])
    assert stages["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 2
    assert sum(stages["crawler_branch"].values()) == stages["crawl_status_1"] == 2


def test_r2_review_p2_batch_cap_bounds_params_and_item_level_fail_safe():
    """B/C/D/E:商品 B 拥有 MAX+1 个 batch → 超出部分被有界查询截断。
    SQL IN 参数长度 ≤ 上限;受截断 ITEM → 证据不足(不冒充 PUSH_RECORD_NOT_FOUND);
    未受截断 ITEM 照常分类(在途 / 未发现记录);crawler branch 闭合。"""
    extra_batches = ["188605_B%06d" % j for j in range(diag.MAX_CRAWL1_BATCHES + 1)]
    task_rows = [(11, "SAAS-188605_A_1", 1), (13, "SAAS-188605_AA_1", 1)]
    # B 的 task_id:SAAS-188605_<batch 后缀>_1 → batch = 188605_Bxxxxxx
    task_rows += [(12, "SAAS-188605_%s_1" % b[len("188605_"):], 1)
                  for b in extra_batches]
    service, executed, _ = make_service({
        "pool_sync_standard_counts": [("0", 3)],
        "open_crawl_status_counts": [(3, 0, 3, 0, 0)],
        "crawl1_commodity_rows": [(11, "CA"), (12, "CB"), (13, "CC")],
        "crawl1_task_rows": task_rows,
        # 只返回 A 的推送记录:B 截断证据不足;C 无记录 → PUSH_RECORD_NOT_FOUND
        "crawl1_spider_task_rows": [("188605_A", "EA", 0)],
        "crawl1_callback_rows": [("EA", "8888", "active")],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    spider_calls = [params for name, params in executed
                    if name == "crawl1_spider_task_rows"]
    assert len(spider_calls) == 1
    bounded = spider_calls[0]["batch_nums"]
    assert len(bounded) <= diag.MAX_CRAWL1_BATCHES            # B:参数有界
    assert all(isinstance(v, str) for v in bounded)           # F:全为 str
    crawler = stages["crawler_branch"]
    assert crawler["CALLBACK_IN_FLIGHT"] == 1                 # D:A 未受截断
    assert crawler["PUSH_RECORD_NOT_FOUND"] == 1              # D:C 未受截断
    assert crawler["EVIDENCE_INCOMPLETE"] == 1                # C:B 受截断
    assert sum(crawler.values()) == stages["crawl_status_1"] == 3  # E:闭合


# ---------------------------------------------------------------------------
# E. downstream 分类(crawl_status=2)
# ---------------------------------------------------------------------------

def flags(**kw):
    base = {"search_rows": 5, "pool_created_at": "2026-08-27 08:00:00",
            "model_pending": 0, "has_internet_same": 0, "internet_pending_approval": 0,
            "same_approved": 0, "has_reference_same": 0, "has_internet_candidate": 0,
            "reference_pending_approval": 0, "has_model_verdict": 0,
            "approval_non_same_exists": 0}
    base.update(kw)
    return base


def test_callback_done_no_search_with_grace_window():
    fresh = flags(search_rows=0, pool_created_at="2026-08-27 09:59:00")
    old = flags(search_rows=0, pool_created_at="2026-08-27 06:00:00")
    now = datetime(2026, 8, 27, 2, 0, tzinfo=UTC).astimezone(diag.SHANGHAI).replace(tzinfo=None)  # 上海 10:00
    assert diag.classify_downstream(fresh, now=now, grace_seconds=1800)["state"] == "CALLBACK_DONE_NO_SEARCH"
    assert diag.classify_downstream(fresh, now=now, grace_seconds=1800)["within_grace"] is True
    assert diag.classify_downstream(old, now=now, grace_seconds=1800)["within_grace"] is False


def test_model_pending():
    assert diag.classify_downstream(flags(model_pending=1))["state"] == "MODEL_PENDING"


def test_internet_same_pending_approval():
    record = flags(internet_pending_approval=1, has_internet_same=1)
    assert diag.classify_downstream(record)["state"] == "INTERNET_SAME_PENDING_APPROVAL"


def test_approval_or_final_closure_pending():
    record = flags(same_approved=1, has_internet_same=1)
    assert diag.classify_downstream(record)["state"] == "APPROVAL_OR_FINAL_CLOSURE_PENDING"


def test_internet_candidates_all_non_same_takes_priority_over_reference_same():
    # 生产 8/27 案例:75 个商品 = 参考链接同款 + 互联网候选全部判非同款
    record = flags(has_reference_same=1, has_internet_candidate=1)
    assert diag.classify_downstream(record)["state"] == "INTERNET_CANDIDATES_ALL_NON_SAME"


def test_reference_same_only_when_no_internet_candidate():
    # 生产 8/27 案例:14 个商品 = 参考链接同款,且无互联网候选证据
    record = flags(has_reference_same=1, has_internet_candidate=0)
    assert diag.classify_downstream(record)["state"] == "REFERENCE_SAME_ONLY"


def test_model_done_no_same():
    record = flags(has_model_verdict=1)
    assert diag.classify_downstream(record)["state"] == "MODEL_DONE_NO_SAME"


def test_no_internet_candidate():
    record = flags(search_rows=3, has_model_verdict=0, has_internet_candidate=0)
    assert diag.classify_downstream(record)["state"] == "NO_INTERNET_CANDIDATE"


def test_downstream_evidence_incomplete():
    record = flags(search_rows=0, model_pending=0)  # 由调用方处理 grace;纯函数兜底
    assert diag.classify_downstream(record)["state"] == "CALLBACK_DONE_NO_SEARCH"


def test_pending_approval_beats_model_pending():
    record = flags(internet_pending_approval=1, has_internet_same=1, model_pending=1)
    assert diag.classify_downstream(record)["state"] == "INTERNET_SAME_PENDING_APPROVAL"


# ---------------------------------------------------------------------------
# F. 18:00 前增长停滞
# ---------------------------------------------------------------------------

def obs(minute_offsets, customer, saas):
    base = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)
    return [{"observed_at": (base + timedelta(minutes=m)).isoformat(),
             "customer_count": c, "saas_count": s}
            for m, c, s in zip(minute_offsets, customer, saas)]


BEFORE_CUTOFF = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)  # 上海 14:00


def test_growth_normal_when_saas_grows():
    series = obs([0, 15], [100, 110], [50, 55])
    result = diag.evaluate_growth(series, business_date="2026-08-27", now=BEFORE_CUTOFF)
    assert result["state"] == "NORMAL" and result["evaluated"] is True
    assert result["customer_delta"] == 10 and result["saas_delta"] == 5


def test_growth_lag_watch_on_single_flat_interval():
    series = obs([0, 15], [100, 110], [50, 50])
    result = diag.evaluate_growth(series, business_date="2026-08-27", now=BEFORE_CUTOFF)
    assert result["state"] == "LAG_WATCH"
    assert result["consecutive_lag_intervals"] == 1


def test_growth_lag_persisting_on_two_consecutive_flat_intervals():
    series = obs([0, 15, 30], [100, 110, 120], [50, 50, 50])
    result = diag.evaluate_growth(series, business_date="2026-08-27", now=BEFORE_CUTOFF)
    assert result["state"] == "LAG_PERSISTING"
    assert result["consecutive_lag_intervals"] == 2


def test_growth_recovery_returns_normal():
    series = obs([0, 15, 30], [100, 110, 110], [50, 50, 58])
    result = diag.evaluate_growth(series, business_date="2026-08-27", now=BEFORE_CUTOFF)
    assert result["state"] == "NORMAL"


def test_growth_flat_customer_is_not_lag():
    series = obs([0, 15], [100, 100], [50, 50])
    result = diag.evaluate_growth(series, business_date="2026-08-27", now=BEFORE_CUTOFF)
    assert result["state"] == "NORMAL"


def test_growth_not_evaluated_after_18():
    after = datetime(2026, 8, 27, 11, 0, tzinfo=UTC)  # 上海 19:00
    series = obs([0, 15], [100, 110], [50, 50])
    result = diag.evaluate_growth(series, business_date="2026-08-27", now=after)
    assert result["evaluated"] is False and result["state"] == "NORMAL"


def test_growth_with_less_than_two_observations():
    result = diag.evaluate_growth(obs([0], [100], [50]), business_date="2026-08-27",
                                  now=BEFORE_CUTOFF)
    assert result["state"] == "NORMAL" and result["observation_count"] == 1


def test_growth_series_reader_uses_real_store_schema(tmp_path):
    from app.services.daily_audit_store import DailyAuditStore
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    con = sqlite3.connect(str(tmp_path / "state.sqlite3"))
    try:
        con.execute(
            "INSERT INTO daily_runs(business_date, tenant_id, pool_id, state, created_at, updated_at)"
            " VALUES ('2026-08-27', 188605, 222, 'OPEN', '2026-08-27T01:00:00+00:00', '2026-08-27T01:00:00+00:00')")
        run_id = con.execute("SELECT id FROM daily_runs WHERE business_date='2026-08-27'").fetchone()[0]
        for i, (at, sc, cc) in enumerate([
                ("2026-08-27T01:00:00+00:00", 50, 100),
                ("2026-08-27T01:15:00+00:00", 50, 110),
                ("2026-08-27T01:30:00+00:00", 50, 120)]):
            con.execute(
                "INSERT INTO snapshots(run_id, ordered_hash, observed_at, row_count, headers_json,"
                " evidence_json, created_at) VALUES (?, ?, ?, 0, '[]', ?, ?)",
                (run_id, f"h{i}", at,
                 json.dumps({"saas_count": sc, "customer_count": cc}), at))
        con.commit()
    finally:
        con.close()
    result = diag.read_observation_series(tmp_path / "state.sqlite3", "2026-08-27")
    series = result["observations"]
    assert result["read_error"] is None and result["available"] is True
    assert [(o["customer_count"], o["saas_count"]) for o in series] == [(100, 50), (110, 50), (120, 50)]
    growth = diag.evaluate_growth(series, business_date="2026-08-27", now=BEFORE_CUTOFF)
    assert growth["state"] == "LAG_PERSISTING"


def test_growth_series_reader_skips_rows_without_counts(tmp_path):
    from app.services.daily_audit_store import DailyAuditStore
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    con = sqlite3.connect(str(tmp_path / "state.sqlite3"))
    try:
        con.execute(
            "INSERT INTO daily_runs(business_date, tenant_id, pool_id, state, created_at, updated_at)"
            " VALUES ('2026-08-27', 188605, 222, 'OPEN', 'x', 'x')")
        run_id = con.execute("SELECT id FROM daily_runs WHERE business_date='2026-08-27'").fetchone()[0]
        con.execute(
            "INSERT INTO snapshots(run_id, ordered_hash, observed_at, row_count, headers_json,"
            " evidence_json, created_at) VALUES (?, 'h0', '2026-08-27T01:00:00+00:00', 0, '[]', '{}', 'x')",
            (run_id,))
        con.execute(
            "INSERT INTO snapshots(run_id, ordered_hash, observed_at, row_count, headers_json,"
            " evidence_json, created_at) VALUES (?, 'h1', '2026-08-27T01:15:00+00:00', 0, '[]', ?, 'x')",
            (run_id, json.dumps({"saas_count": 5, "customer_count": 8})))
        con.commit()
    finally:
        con.close()
    result = diag.read_observation_series(tmp_path / "state.sqlite3", "2026-08-27")
    series = result["observations"]
    assert len(series) == 1 and series[0]["customer_count"] == 8


def test_service_growth_uses_local_state_not_business_api(tmp_path):
    day_dir = tmp_path / "2026-08-27"
    day_dir.mkdir()
    from app.services.daily_audit_store import DailyAuditStore
    DailyAuditStore(day_dir / "state.sqlite3")
    con = sqlite3.connect(str(day_dir / "state.sqlite3"))
    try:
        con.execute(
            "INSERT INTO daily_runs(business_date, tenant_id, pool_id, state, created_at, updated_at)"
            " VALUES ('2026-08-27', 188605, 222, 'OPEN', 'x', 'x')")
        run_id = con.execute("SELECT id FROM daily_runs WHERE business_date='2026-08-27'").fetchone()[0]
        for i, (at, sc, cc) in enumerate([
                ("2026-08-27T01:00:00+00:00", 50, 100),
                ("2026-08-27T01:15:00+00:00", 50, 110)]):
            con.execute(
                "INSERT INTO snapshots(run_id, ordered_hash, observed_at, row_count, headers_json,"
                " evidence_json, created_at) VALUES (?, ?, ?, 0, '[]', ?, ?)",
                (run_id, f"h{i}", at,
                 json.dumps({"saas_count": sc, "customer_count": cc}), at))
        con.commit()
    finally:
        con.close()
    service, executed, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 0, 0, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [],
    }, runtime_root=tmp_path, now_fn=lambda: BEFORE_CUTOFF)
    payload = service.get_diagnostic("2026-08-27")
    assert payload["growth"]["available"] is True
    assert payload["growth"]["state"] == "LAG_WATCH"
    # DB 查询参数不含任何业务 API 相关内容(growth 只读本地 SQLite)
    for name, params in executed:
        assert set(params) <= {"pool_id", "day_start", "day_end", "commodity_list_ids", "batch_nums"}


# ---------------------------------------------------------------------------
# G. Dashboard API 隔离
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    return TestClient(app)


def _clear_service(monkeypatch):
    monkeypatch.delenv("CNNC_DIAG_DB_HOST", raising=False)
    monkeypatch.delenv("CNNC_DIAG_DB_PORT", raising=False)
    monkeypatch.delenv("CNNC_DIAG_DB_USER", raising=False)
    monkeypatch.delenv("CNNC_DIAG_DB_PASSWORD", raising=False)
    import app.services.saas_flow_diagnostic as mod
    monkeypatch.setattr(mod, "_SERVICE_SINGLETON", None)


def test_saas_flow_endpoint_400_on_bad_date(client, monkeypatch):
    _clear_service(monkeypatch)
    r = client.get("/dashboard/api/saas-flow?date=2026/08/27")
    assert r.status_code == 400


def test_saas_flow_endpoint_rejects_scope_and_sql_params(client, monkeypatch):
    _clear_service(monkeypatch)
    for forbidden in ("tenant_id", "pool_id", "schema", "table", "sql", "query", "raw_sql", "force_sql"):
        r = client.get(f"/dashboard/api/saas-flow?date=2026-08-27&{forbidden}=x")
        assert r.status_code == 400, forbidden


def test_saas_flow_endpoint_unconfigured_returns_200_degraded(client, monkeypatch):
    _clear_service(monkeypatch)
    r = client.get("/dashboard/api/saas-flow?date=2026-08-27")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unconfigured"
    assert body["scope"] == {"tenant_id": "188605", "commodity_pool_id": "222"}


def test_saas_flow_endpoint_is_get_only(client, monkeypatch):
    _clear_service(monkeypatch)
    for method in ("post", "put", "delete", "patch"):
        r = getattr(client, method)("/dashboard/api/saas-flow")
        assert r.status_code == 405, method


def test_summary_unaffected_when_diagnostic_raises(client, monkeypatch):
    from app import dashboard_api

    def boom(**kw):
        raise RuntimeError("diagnostic exploded")

    monkeypatch.setattr(dashboard_api, "get_saas_flow_diagnostic", boom)
    payload = {"generated_at": "t", "fingerprint": "x" * 64, "overall": {"word": "正在准备数据"}}
    monkeypatch.setattr(dashboard_api, "build_dashboard_summary", lambda **kw: dict(payload))
    r = client.get("/dashboard/api/saas-flow")
    assert r.status_code == 503
    r2 = client.get("/dashboard/api/summary")
    assert r2.status_code == 200 and r2.json()["overall"]["word"] == "正在准备数据"


def test_summary_unaffected_when_db_diagnostic_degrades(client, monkeypatch):
    _clear_service(monkeypatch)
    r = client.get("/dashboard/api/saas-flow?date=2026-08-27")
    assert r.status_code == 200  # DB 未配置 → 面板降级,不是 5xx
    r2 = client.get("/dashboard/api/summary")
    assert r2.status_code == 200  # 主看板正常


def test_refresh_param_is_cache_flag_only(client, monkeypatch):
    _clear_service(monkeypatch)
    r = client.get("/dashboard/api/saas-flow?date=2026-08-27&refresh=1")
    assert r.status_code == 200
    r = client.get("/dashboard/api/saas-flow?date=2026-08-27&refresh=not-a-flag-value-anything")
    assert r.status_code == 400


# ===========================================================================
# Review Fix Round 1(findings P1-1..P2-2 + sync_state=2 边界)
# ===========================================================================

ROOT = Path(__file__).resolve().parents[1]


# ---- P1-1:两个分支的 EVIDENCE_INCOMPLETE 独立计数,互不污染 ----

def test_p1_1_crawler_incomplete_does_not_leak_into_downstream():
    canned = dict(FULL_CANNED)
    # callback 出现未验证 push_state(有推送、有回调行,但状态不可解释)→ 证据不足
    canned["crawl1_callback_rows"] = [("E1", "7777", "active")]
    service, _, _ = make_service(canned)
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert stages["crawler_branch"]["EVIDENCE_INCOMPLETE"] == 1
    assert stages["downstream_branch"]["EVIDENCE_INCOMPLETE"] == 0
    # 两个分支分别闭合
    assert sum(stages["crawler_branch"].values()) == stages["crawl_status_1"]
    assert sum(stages["downstream_branch"].values()) == stages["crawl_status_2"]


def test_p1_1_downstream_incomplete_does_not_leak_into_crawler():
    canned = dict(FULL_CANNED)
    # 路由层称 crawl2=5,但 flags 查询只返回 3 行 → 2 个溢出落 downstream 证据不足
    canned["open_crawl_status_counts"] = [(6, 0, 1, 5, 0)]
    service, _, _ = make_service(canned)
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert stages["downstream_branch"]["EVIDENCE_INCOMPLETE"] == 2
    assert stages["crawler_branch"]["EVIDENCE_INCOMPLETE"] == 0
    assert sum(stages["downstream_branch"].values()) == stages["crawl_status_2"]


# ---- P1-2:spider 推送查询失败 = 证据不足,不是"未发现推送记录" ----

def test_p1_2_spider_query_failure_is_evidence_incomplete_not_push_not_found():
    def spider_raises(params):
        raise RuntimeError("spider timeout")

    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 2)],
        "open_crawl_status_counts": [(2, 0, 2, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1"), (12, "C2")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1), (12, "SAAS-188605_702_1", 1)],
        "crawl1_spider_task_rows": spider_raises,
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "partial"
    assert "crawl1_spider_task_rows" in payload["degraded_queries"]
    crawler = payload["stages"]["crawler_branch"]
    assert crawler["EVIDENCE_INCOMPLETE"] == 2
    assert crawler["PUSH_RECORD_NOT_FOUND"] == 0


# ---- P1-3:核心查询失败 ≠ 业务 0 ----

def test_p1_3_case_a_open_crawl_status_failure_is_not_business_zero():
    def crawl_status_raises(params):
        raise RuntimeError("crawl status timeout")

    service, executed, _ = make_service({
        "pool_sync_standard_counts": [("0", 4)],
        "open_crawl_status_counts": crawl_status_raises,
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl2_search_flags": [(21, 0, "2026-08-27 09:00:00", 0, 0, 0, 0, 0, 0, 0, 0, 0)],
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "partial"
    stages = payload["stages"]
    # 查询失败不得表达为业务 0
    assert stages["crawl_status_1"] is None
    assert stages["crawl_status_2"] is None
    assert stages["crawl_status_other"] is None
    assert stages["crawl_status_available"] is False
    # pool 计数仍可用且保留原语义名
    assert stages["pool_counts_available"] is True
    assert stages["pool_scope_total"] == 4
    assert stages["not_closed"] == 4
    # 不误算 no_commodity_list,不输出矛盾 branch total
    assert stages["no_commodity_list"] is None
    assert stages["crawler_branch"] is None
    assert stages["downstream_branch"] is None
    assert stages["dominant_stage"] is None
    # 查询依赖:路由层失败后不再执行分支子查询
    executed_names = {name for name, _ in executed}
    assert "crawl1_commodity_rows" not in executed_names
    assert "crawl1_task_rows" not in executed_names
    assert "crawl2_search_flags" not in executed_names
    assert "open_crawl_status_counts" in payload["degraded_queries"]


def test_p1_3_case_b_pool_failure_is_not_business_zero():
    def pool_raises(params):
        raise RuntimeError("pool timeout")

    service, _, _ = make_service({
        "pool_sync_standard_counts": pool_raises,
        "open_crawl_status_counts": [(3, 0, 1, 2, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0)],
        "crawl1_callback_rows": [("E1", "8888", "active")],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "partial"
    stages = payload["stages"]
    # 不能声称 customer/pool 总数为 0
    assert stages["pool_scope_total"] is None
    assert stages["not_closed"] is None
    assert stages["closed_standard"] is None
    assert stages["closed_no_same"] is None
    assert stages["pool_counts_available"] is False
    # 路由层成功:routing 计数与 no_commodity_list 来自 routing query 自身,可用
    assert stages["crawl_status_available"] is True
    assert stages["crawl_status_1"] == 1
    assert stages["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1
    assert stages["routing_open_total"] == 3
    assert stages["no_commodity_list"] == 0
    assert "pool_sync_standard_counts" in payload["degraded_queries"]


# ---- P1-5:SaaS Flow 折叠摘要默认可见(不在 .more-block 内) ----

def test_p1_5_saasflow_summary_visible_without_expanding_more():
    html = (ROOT / "app" / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
    sf_index = html.find('id="saasflow"')
    more_index = html.find('class="more-block"')
    assert sf_index != -1 and more_index != -1
    assert sf_index < more_index, "#saasflow 必须移出默认 display:none 的 .more-block"
    import re as _re
    details = _re.search(r"<details[^>]*saasflow[^>]*>", html)
    assert details is not None
    assert "open" not in details.group(0), "默认折叠:details 不得带 open 属性"


# ---- P1-6:SQL 池计数不得冒充权威客户计数 ----

def test_p1_6_pool_count_not_named_customer_total():
    service, _, _ = make_service(dict(FULL_CANNED))
    payload = service.get_diagnostic("2026-08-27")
    text = json.dumps(payload, ensure_ascii=False)
    assert "customer_total" not in text
    assert payload["stages"]["pool_scope_total"] == 4
    js = (ROOT / "app" / "static" / "dashboard" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "app" / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "customer_total" not in js
    assert "今日客户商品" not in js and "今日客户商品" not in html
    assert "pool_scope_total" in js and "当日商品池记录" in js


# ---- P1-7:并发强制刷新 single-flight ----

def test_p1_7_concurrent_forced_refreshes_single_flight():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    build_count = {"n": 0}
    lock = threading.Lock()

    def slow_factory(cfg):
        with lock:
            build_count["n"] += 1
        time.sleep(0.05)
        return FakeConnection({
            "pool_sync_standard_counts": [("0", 1)],
            "open_crawl_status_counts": [(1, 0, 0, 0, 0)],
            "crawl1_commodity_rows": [],
            "crawl2_search_flags": [],
        }, [])

    service = diag.SaasFlowDiagnosticService(
        config=diag.DiagDBConfig(host="db.example.internal", port=3306,
                                 user="ro", password="secret-not-committed"),
        connection_factory=slow_factory,
        fresh_read_min_interval_seconds=30.0,
        cache_ttl_seconds=60.0,
    )
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(
            lambda _: service.get_diagnostic("2026-08-27", force_refresh=True),
            range(10)))
    assert build_count["n"] == 1, f"并发强刷必须 single-flight,实际 build {build_count['n']} 次"
    assert len(results) == 10 and all(isinstance(r, dict) and r.get("status") == "ok"
                                      for r in results)
    assert sum(1 for r in results if r.get("rate_limited")) == 9


# ---- P2-1:强刷下限 clamp 到 30 秒 ----

def test_p2_1_fresh_interval_env_clamps_to_30(monkeypatch):
    monkeypatch.setenv("CNNC_DIAG_FRESH_MIN_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("CNNC_DIAG_CACHE_SECONDS", "10")
    monkeypatch.delenv("CNNC_DIAG_DB_HOST", raising=False)
    monkeypatch.delenv("CNNC_DIAG_DB_USER", raising=False)
    monkeypatch.delenv("CNNC_DIAG_DB_PASSWORD", raising=False)
    monkeypatch.setattr(diag, "_SERVICE_SINGLETON", None)
    diag.get_saas_flow_diagnostic("2026-08-27")
    service = diag._SERVICE_SINGLETON
    assert service._fresh_min_interval == 30.0
    assert service._cache_ttl_seconds == 60.0


# ---- P2-2:evaluation_reason ----

def test_p2_2_evaluation_reason_values():
    no_obs = diag.evaluate_growth([], business_date="2026-08-27", now=BEFORE_CUTOFF)
    assert no_obs["evaluation_reason"] == "NO_OBSERVATIONS"
    one = diag.evaluate_growth(obs([0], [100], [50]), business_date="2026-08-27",
                               now=BEFORE_CUTOFF)
    assert one["evaluation_reason"] == "INSUFFICIENT_OBSERVATIONS"
    ok = diag.evaluate_growth(obs([0, 15], [100, 110], [50, 55]),
                              business_date="2026-08-27", now=BEFORE_CUTOFF)
    assert ok["evaluation_reason"] == "EVALUATED"
    after = diag.evaluate_growth(obs([0, 15], [100, 110], [50, 50]),
                                 business_date="2026-08-27",
                                 now=datetime(2026, 8, 27, 11, 0, tzinfo=UTC))
    assert after["evaluation_reason"] == "AFTER_CUTOFF"


def test_p2_2_frontend_distinguishes_reasons():
    js = (ROOT / "app" / "static" / "dashboard" / "app.js").read_text(encoding="utf-8")
    assert "NO_OBSERVATIONS" in js and "INSUFFICIENT_OBSERVATIONS" in js
    assert "暂无权威观察" in js and "观察次数不足" in js
    # R2 P2-1:读取失败 ≠ 无观察,前端有专用文案
    assert "OBSERVATION_READ_FAILED" in js and "权威观察读取失败" in js
    # R2 P1-1:snapshot drift 有用户可读解释,不冒充业务异常
    assert "查询期间数据变化" in js


# ---- §12(R1):sync_state=2 已审批非同款的显式分类 ----
# R2 P1-3:flag 更名 approval_non_same_exists,语义覆盖 model=0/1/2/3 + sync_state=2

def test_sync_state_2_internet_same_rejected_is_approval_closure_pending():
    # 模型判互联网同款(model=1),人工审批为非同款(sync_state=2),未最终收口
    record = flags(has_internet_same=1, has_internet_candidate=1, approval_non_same_exists=1)
    assert diag.classify_downstream(record)["state"] == "APPROVAL_OR_FINAL_CLOSURE_PENDING"


def test_sync_state_2_reference_same_rejected_is_approval_closure_pending():
    # 参考链接同款(model=3)人工审批为非同款(sync_state=2)
    record = flags(has_reference_same=1, approval_non_same_exists=1)
    assert diag.classify_downstream(record)["state"] == "APPROVAL_OR_FINAL_CLOSURE_PENDING"


def test_sync_state_2_pending_approval_still_dominates_rejected():
    # 同商品同时存在待审批与已驳回:待审批是当前可操作阶段
    record = flags(has_internet_same=1, internet_pending_approval=1, approval_non_same_exists=1)
    assert diag.classify_downstream(record)["state"] == "INTERNET_SAME_PENDING_APPROVAL"


# ===========================================================================
# Review Fix Round 2(P1-1 snapshot drift / P1-2 batch 解析 / P1-3 sync=2 覆盖
# / P2-1 观察读取失败 / P2-2 混合 task-central 失败)
# ===========================================================================


# ---- P1-1:多 SELECT snapshot drift ----

def test_r2_p1_1_a_crawl1_detail_exceeds_route_is_drift_not_contradiction():
    """路由快照 crawl1=3,detail 查询返回 5 行(查询期间 1→2 迁移):
    不得发布 sum(crawler_branch)>3 的矛盾数字;分支标记不可用 + drift 显式可见。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 3)],
        "open_crawl_status_counts": [(3, 0, 3, 0, 0)],
        "crawl1_commodity_rows": [(i, f"C{i}") for i in range(1, 6)],  # 5 行 > 3
        "crawl1_task_rows": [],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert payload["status"] == "partial"
    assert stages["snapshot_consistent"] is False
    assert "crawler_branch" in stages["snapshot_drift"]
    assert stages["crawler_branch"] is None          # 本轮不发布确定性分类
    if stages["downstream_branch"] is not None:      # 未漂移分支不发布矛盾数字
        assert sum(stages["downstream_branch"].values()) <= stages["crawl_status_2"]


def test_r2_p1_1_b_crawl2_detail_exceeds_route_is_drift_not_contradiction():
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 3)],
        "open_crawl_status_counts": [(3, 0, 0, 3, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [
            (i, 5, "2026-08-27 08:00:00", 0, 0, 0, 0, 0, 0, 0, 0, 0) for i in range(1, 6)
        ],  # 5 行 > 3
    })
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert payload["status"] == "partial"
    assert stages["snapshot_consistent"] is False
    assert "downstream_branch" in stages["snapshot_drift"]
    assert stages["downstream_branch"] is None
    if stages["crawler_branch"] is not None:
        assert sum(stages["crawler_branch"].values()) <= stages["crawl_status_1"]


def test_r2_p1_1_c_route_gt_detail_still_overflow_evidence_incomplete():
    """route=5, detail=3:正向溢出(截断/查询窗口)仍按 EVIDENCE_INCOMPLETE 补齐,不冒充业务态。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 5)],
        "open_crawl_status_counts": [(5, 0, 0, 5, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [
            (21, 0, "2026-08-27 09:00:00", 0, 0, 0, 0, 0, 0, 0, 0, 0),
            (22, 5, "2026-08-27 08:00:00", 0, 1, 1, 0, 0, 0, 0, 0, 0),
            (23, 5, "2026-08-27 08:00:00", 0, 0, 0, 0, 0, 0, 1, 0, 0),
        ],
    })
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert stages["snapshot_consistent"] is True and stages["snapshot_drift"] == []
    assert stages["downstream_branch"]["EVIDENCE_INCOMPLETE"] == 2
    assert sum(stages["downstream_branch"].values()) == stages["crawl_status_2"] == 5


def test_r2_p1_1_d_consistent_input_closes_both_branches():
    service, _, _ = make_service(dict(FULL_CANNED))
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert payload["status"] == "ok"
    assert stages["snapshot_consistent"] is True
    assert sum(stages["crawler_branch"].values()) == stages["crawl_status_1"] == 1
    assert sum(stages["downstream_branch"].values()) == stages["crawl_status_2"] == 3
    # 两分支 EVIDENCE_INCOMPLETE 依然独立
    assert stages["crawler_branch"]["EVIDENCE_INCOMPLETE"] == 0
    assert stages["downstream_branch"]["EVIDENCE_INCOMPLETE"] == 0


# ---- P1-2:task_id / batch 严格解析 ----

def test_r2_p1_2_malformed_task_ids_never_produce_batches():
    for bad in ("BROKEN", "FOO_123", "SAAS-", "SAAS-999999_123_1", "", None,
                "SAAS-188605_279774", "SAAS-188605__1"):
        assert diag._batch_from_task_id(bad) is None, bad
    assert diag._batch_from_task_id("SAAS-188605_704827316150657024_279774") == \
        "188605_704827316150657024"


def test_r2_p1_2_mixed_valid_and_malformed_tasks_is_evidence_incomplete():
    """2 个合法 task + 1 个 malformed task → 证据集合不完整,不得进入业务分类。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 1, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [
            (11, "SAAS-188605_701_1", 1),
            (11, "SAAS-188605_702_1", 1),
            (11, "FOO_123", 1),
        ],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0), ("188605_702", "E2", 0)],
        "crawl1_callback_rows": [],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    crawler = payload["stages"]["crawler_branch"]
    assert crawler["EVIDENCE_INCOMPLETE"] == 1
    assert crawler["PUSH_RECORD_NOT_FOUND"] == 0
    assert crawler["PUSH_CONFIRMED_NO_CALLBACK_RECORD"] == 0


def test_r2_p1_2_valid_tasks_with_no_spider_records_is_push_not_found():
    """全部 task 可解析、spider 查询成功且确实无行 → 未发现推送记录(非证据不足)。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 1, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
        "crawl1_spider_task_rows": [],  # 查询成功,确实没有推送记录
        "crawl1_callback_rows": [],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    crawler = payload["stages"]["crawler_branch"]
    assert crawler["PUSH_RECORD_NOT_FOUND"] == 1
    assert crawler["EVIDENCE_INCOMPLETE"] == 0


# ---- P1-3:sync_state=2 覆盖全部 model_audit_result ----

def test_r2_p1_3_model0_sync2_is_approval_closure_pending():
    # 互联网候选(model=0)人工审批为非同款
    record = flags(has_internet_candidate=1, approval_non_same_exists=1)
    assert diag.classify_downstream(record)["state"] == "APPROVAL_OR_FINAL_CLOSURE_PENDING"


def test_r2_p1_3_model2_sync2_is_approval_closure_pending():
    # 参考链接候选(model=2)人工审批为非同款
    record = flags(has_model_verdict=1, approval_non_same_exists=1)
    assert diag.classify_downstream(record)["state"] == "APPROVAL_OR_FINAL_CLOSURE_PENDING"


def test_r2_p1_3_model1_and_model3_sync2_stable():
    assert diag.classify_downstream(
        flags(has_internet_same=1, has_internet_candidate=1,
              approval_non_same_exists=1))["state"] == "APPROVAL_OR_FINAL_CLOSURE_PENDING"
    assert diag.classify_downstream(
        flags(has_reference_same=1, approval_non_same_exists=1))["state"] == \
        "APPROVAL_OR_FINAL_CLOSURE_PENDING"


# ---- P2-1:SQLite 观察读取失败 ≠ NO_OBSERVATIONS ----

def test_r2_p2_1_missing_state_file_is_no_observations(tmp_path):
    result = diag.read_observation_series(tmp_path / "absent.sqlite3", "2026-08-27")
    assert result["available"] is False and result["read_error"] is None
    assert result["observations"] == []
    service, _, _ = make_service({"pool_sync_standard_counts": [("0", 1)],
                                  "open_crawl_status_counts": [(1, 0, 0, 0, 0)],
                                  "crawl1_commodity_rows": [],
                                  "crawl2_search_flags": []},
                                 runtime_root=tmp_path, now_fn=lambda: BEFORE_CUTOFF)
    growth = service.get_diagnostic("2026-08-27")["growth"]
    assert growth["evaluation_reason"] == "NO_OBSERVATIONS"


def test_r2_p2_1_empty_store_is_no_observations(tmp_path):
    from app.services.daily_audit_store import DailyAuditStore
    day_dir = tmp_path / "2026-08-27"
    day_dir.mkdir()
    DailyAuditStore(day_dir / "state.sqlite3")
    result = diag.read_observation_series(day_dir / "state.sqlite3", "2026-08-27")
    assert result["available"] is True and result["read_error"] is None
    assert result["observations"] == []


def test_r2_p2_1_corrupted_sqlite_is_read_failed(tmp_path):
    day_dir = tmp_path / "2026-08-27"
    day_dir.mkdir()
    (day_dir / "state.sqlite3").write_bytes(b"this is not a sqlite database at all")
    service, _, _ = make_service({"pool_sync_standard_counts": [("0", 1)],
                                  "open_crawl_status_counts": [(1, 0, 0, 0, 0)],
                                  "crawl1_commodity_rows": [],
                                  "crawl2_search_flags": []},
                                 runtime_root=tmp_path, now_fn=lambda: BEFORE_CUTOFF)
    payload = service.get_diagnostic("2026-08-27")
    growth = payload["growth"]
    assert growth["evaluation_reason"] == "OBSERVATION_READ_FAILED"
    assert growth["evaluated"] is False and growth["available"] is False
    # 只暴露安全错误类型,不泄漏路径/SQL/堆栈
    text = json.dumps(payload, ensure_ascii=False)
    assert str(tmp_path) not in text and "sqlite3" not in growth.get("read_error", "")


def test_r2_p2_1_missing_schema_is_read_failed(tmp_path):
    day_dir = tmp_path / "2026-08-27"
    day_dir.mkdir()
    con = sqlite3.connect(str(day_dir / "state.sqlite3"))  # 合法 DB 但没有 snapshots 表
    con.execute("CREATE TABLE unrelated (id INTEGER)")
    con.commit()
    con.close()
    result = diag.read_observation_series(day_dir / "state.sqlite3", "2026-08-27")
    assert result["available"] is False
    assert result["read_error"]  # 安全错误类型,非空
    assert result["observations"] == []


# ---- P2-2:spider 失败不得覆盖已确定的 TASK_NOT_CREATED ----

def test_r2_p2_2_spider_failure_keeps_task_not_created_for_taskless_commodity():
    """A 有 task(推送记录查询整体失败)→ EVIDENCE_INCOMPLETE;
    B 无 task → TASK_NOT_CREATED(该判断不依赖推送查询)。"""
    def spider_raises(params):
        raise RuntimeError("spider timeout")

    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 2)],
        "open_crawl_status_counts": [(2, 0, 2, 0, 0)],
        "crawl1_commodity_rows": [(11, "CA"), (12, "CB")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],  # 仅 A 有 task
        "crawl1_spider_task_rows": spider_raises,
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    crawler = payload["stages"]["crawler_branch"]
    assert crawler["EVIDENCE_INCOMPLETE"] == 1   # A:有 task,推送证据不可用
    assert crawler["TASK_NOT_CREATED"] == 1      # B:无 task,结论确定
    assert payload["status"] == "partial"
    assert "crawl1_spider_task_rows" in payload["degraded_queries"]


# ---- §8 invariants 汇总 ----

def test_r2_invariants_summary():
    """§8:query failure≠0 / malformed≠task未进分控 / SQLite 失败≠无观察 / 分支独立闭合 /
    drift 不输出矛盾数字 —— 由上方各专项测试覆盖;此处固化 payload 字段契约。"""
    service, _, _ = make_service(dict(FULL_CANNED))
    stages = service.get_diagnostic("2026-08-27")["stages"]
    for key in ("pool_counts_available", "crawl_status_available", "snapshot_consistent",
                "snapshot_drift", "pool_scope_total", "routing_open_total",
                "crawler_branch", "downstream_branch"):
        assert key in stages, key


# ===========================================================================
# Review Fix Round 3 Closeout(P1 routing 单语句闭合 / P2 drift 辅助字段与
# notice 三态 / 真实日历日期校验)
# ===========================================================================


# ---- P1:routing 层单语句闭合 + pool vs routing drift ----

def test_r3_p1_a_pool_lt_routing_marks_drift_not_residual():
    """pool 时点 not_closed=3,routing 时点 open=5(期间新增 2 条):
    不得伪造 no_commodity_list=2;显式 pool_vs_routing drift。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 3)],
        "open_crawl_status_counts": [(5, 0, 2, 3, 0)],
        "crawl1_commodity_rows": [(11, "C1"), (12, "C2")],
        "crawl1_task_rows": [],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert stages["snapshot_consistent"] is False
    assert "pool_vs_routing" in stages["snapshot_drift"]
    assert stages["no_commodity_list"] == 0
    assert stages["not_closed"] == 3 and stages["routing_open_total"] == 5
    assert payload["status"] == "partial"
    # routing 自身闭合:2+3+0+0=5
    assert stages["crawl_status_1"] + stages["crawl_status_2"] + \
        stages["crawl_status_other"] + stages["no_commodity_list"] == stages["routing_open_total"]
    # branch 仍可发布(自身 detail 未漂移)
    assert stages["crawler_branch"] is not None


def test_r3_p1_b_pool_gt_routing_marks_drift_not_residual():
    """pool 时点 not_closed=5,routing 时点 open=3(期间 2 条完成收口):
    不得从 5-3 推出 no_commodity_list=2。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 5)],
        "open_crawl_status_counts": [(3, 0, 1, 2, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert stages["no_commodity_list"] == 0
    assert stages["snapshot_consistent"] is False
    assert "pool_vs_routing" in stages["snapshot_drift"]
    assert payload["status"] == "partial"


def test_r3_p1_c_routing_statement_closes_internally():
    """routing query 单语句内:no_cl + c1 + c2 + other == routing_open_total。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 5)],
        "open_crawl_status_counts": [(5, 1, 1, 2, 1)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["routing_open_total"] == 5
    assert (stages["no_commodity_list"], stages["crawl_status_1"],
            stages["crawl_status_2"], stages["crawl_status_other"]) == (1, 1, 2, 1)


def test_r3_p1_d_no_commodity_list_only_from_routing_join():
    """只有 routing query 的 LEFT JOIN 报告 no_cl=2 时才允许显示未进入采集列表=2。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 4)],
        "open_crawl_status_counts": [(4, 2, 1, 1, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["no_commodity_list"] == 2
    assert stages["snapshot_consistent"] is True  # pool(4) == routing(4)


def test_r3_p1_e_consistent_when_pool_matches_routing_and_details_close():
    service, _, _ = make_service(dict(FULL_CANNED))
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert stages["snapshot_consistent"] is True
    assert stages["snapshot_drift"] == []
    assert payload["status"] == "ok"
    assert sum(stages["crawler_branch"].values()) == stages["crawl_status_1"]
    assert sum(stages["downstream_branch"].values()) == stages["crawl_status_2"]


# ---- P2:branch drift 时辅助字段 unavailable ----

def test_r3_p2_drift_branch_aux_fields_are_none_not_zero():
    # downstream drift(5 行 > routing 3):downstream 派生字段全部 None,不得冒充 0
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 3)],
        "open_crawl_status_counts": [(3, 0, 0, 3, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [
            (i, 5, "2026-08-27 08:00:00", 0, 0, 0, 0, 0, 0, 0, 0, 0) for i in range(1, 6)
        ],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["downstream_branch"] is None
    assert stages["reference_pending_approval_total"] is None
    assert stages["callback_done_no_search_within_grace"] is None

    # crawler drift:routing c1=3 但 detail 返回 5 行 → crawler 派生 detail None
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 3)],
        "open_crawl_status_counts": [(3, 0, 3, 0, 0)],
        "crawl1_commodity_rows": [(i, f"C{i}") for i in range(1, 6)],
        "crawl1_task_rows": [],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"] is None
    assert stages["central_status_detail"] is None


def test_r3_p2_normal_branch_real_zero_stays_zero():
    """正常 branch:真实 0 仍是 0;central 诊断已退役,detail 恒 None 不冒充空列表。"""
    service, _, _ = make_service(dict(FULL_CANNED), now_fn=lambda: BEFORE_CUTOFF)
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"] is not None
    assert stages["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1
    # R2:不再查询 s_task_info;保留 key 兼容消费者,恒 None
    assert stages["central_status_detail"] is None
    assert stages["reference_pending_approval_total"] == 0
    assert stages["callback_done_no_search_within_grace"] == 0  # 09:59 入池 vs 14:00 检查,超出宽限


# ---- Non-blocking:真实日历日期校验 ----

def test_r3_date_validation_rejects_impossible_calendar_dates(client, monkeypatch):
    _clear_service(monkeypatch)
    for bad in ("2026-02-31", "2026-02-29", "2026-13-01", "2026-00-10"):
        r = client.get(f"/dashboard/api/saas-flow?date={bad}")
        assert r.status_code == 400, bad
        assert r.json()["detail"]["error_code"] == "BAD_BUSINESS_DATE", bad
    # 合法闰年接受(未配置 → 200 降级,而非 400/503)
    r = client.get("/dashboard/api/saas-flow?date=2028-02-29")
    assert r.status_code == 200
    # summary 同一校验:非法日历日期 400,不得进入 503
    r = client.get("/dashboard/api/summary?date=2026-02-31")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "BAD_BUSINESS_DATE"


# ===========================================================================
# Cardinality Closeout(路由单位 = 唯一 MY_COMMODITY_ID,JOIN 行 ≠ 商品数)
#
# 以下测试用 tmp :memory: SQLite 执行 DIAG_SQL_REGISTRY 内的真实 SQL
# (不连任何真实数据库):schema 名模拟 MySQL 的库名前缀,参数 %(name)s 转换为
# SQLite 命名参数,IN 元组展开为字面量(仅测试代码,值全部来自测试自身)。
# ===========================================================================

def _sql_literal(value) -> str:
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


class SqliteRegistryCursor:
    """只执行 registry 内 SQL;%(name)s → :name,IN 元组 → 字面量元组。"""

    def __init__(self, connection) -> None:
        self._connection = connection
        self._rows: list = []

    def execute(self, sql: str, params: dict | None = None) -> None:
        normalized = " ".join(sql.split())
        matches = [name for name, text in diag.DIAG_SQL_REGISTRY.items()
                   if " ".join(text.split()) == normalized]
        assert matches, "attempted to execute SQL outside the predefined registry"
        converted = sql
        bound = dict(params or {})
        for key, value in list(bound.items()):
            if isinstance(value, (tuple, list)):
                literal = "(" + ", ".join(_sql_literal(v) for v in value) + ")"
                converted = converted.replace(f"%({key})s", literal)
                bound.pop(key)
        converted = re.sub(r"%\((\w+)\)s", r":\1", converted)
        self._rows = self._connection.execute(converted, bound).fetchall()

    def fetchall(self) -> list:
        return list(self._rows)

    def close(self) -> None:
        pass


def _sqlite_backend(pool_rows, cl_rows, task_rows=(), spider_rows=(), callback_rows=(),
                    search_rows=()):
    import sqlite3 as _sqlite3

    con = _sqlite3.connect(":memory:")
    con.execute("ATTACH ':memory:' AS saas_onecode_price")
    con.execute("ATTACH ':memory:' AS saas_compare_price")
    con.execute("ATTACH ':memory:' AS shopping_spider")
    con.executescript("""
        CREATE TABLE saas_onecode_price.commodity_pool_188605 (
            COMMODITY_POOL_ID TEXT, IS_MONITOR INTEGER, IS_SYNC_STANDARD INTEGER,
            CREATED_TIME TEXT, MY_COMMODITY_ID TEXT);
        CREATE TABLE saas_compare_price.commodity_list_table_188605 (
            id INTEGER PRIMARY KEY, commodity_id TEXT, crawl_status INTEGER);
        CREATE TABLE saas_compare_price.task_table_188605 (
            reference_id INTEGER, task_id TEXT, task_status TEXT, table_name TEXT);
        CREATE TABLE shopping_spider.spider_task_record (
            id INTEGER PRIMARY KEY, mall TEXT, exe_number TEXT, batch_num TEXT,
            sku_ids TEXT, is_call INTEGER, create_time TEXT, backup_time TEXT);
        CREATE TABLE shopping_spider.spider_sync_log (
            pid INTEGER PRIMARY KEY, execution_number TEXT, push_state TEXT);
        CREATE TABLE shopping_spider.spider_sync_log_complated (
            pid INTEGER PRIMARY KEY, execution_number TEXT, push_state TEXT);
        CREATE TABLE saas_compare_price.search_table_188605 (
            id INTEGER PRIMARY KEY, commodity_list_id INTEGER,
            audit_result TEXT, model_audit_result TEXT, sync_state INTEGER);
    """)
    con.executemany(
        "INSERT INTO saas_onecode_price.commodity_pool_188605 VALUES (?,?,?,?,?)", pool_rows)
    con.executemany(
        "INSERT INTO saas_compare_price.commodity_list_table_188605 VALUES (?,?,?)", cl_rows)
    con.executemany(
        "INSERT INTO saas_compare_price.task_table_188605 VALUES (?,?,?,?)",
        [(*row, "188605") for row in task_rows])
    con.executemany(
        "INSERT INTO shopping_spider.spider_task_record VALUES (?,?,?,?,?,?,?,?)",
        [(i + 1, "M%d" % i, exe, batch, "", call, "2026-08-27 08:00:00", None)
         for i, (batch, exe, call) in enumerate(spider_rows)])
    active = [(i + 1, exe, push) for i, (exe, push, src) in enumerate(callback_rows)
              if src == "active"]
    completed = [(i + 1, exe, push) for i, (exe, push, src) in enumerate(callback_rows)
                 if src == "completed"]
    con.executemany(
        "INSERT INTO shopping_spider.spider_sync_log VALUES (?,?,?)", active)
    con.executemany(
        "INSERT INTO shopping_spider.spider_sync_log_complated VALUES (?,?,?)", completed)
    con.executemany(
        "INSERT INTO saas_compare_price.search_table_188605 VALUES (?,?,?,?,?)", search_rows)
    con.commit()
    return con


class SqliteRegistryConnection:
    def __init__(self, pool_rows, cl_rows, task_rows=(), spider_rows=(), callback_rows=(),
                 search_rows=()):
        self._con = _sqlite_backend(pool_rows, cl_rows, task_rows, spider_rows,
                                    callback_rows, search_rows)

    def cursor(self) -> SqliteRegistryCursor:
        return SqliteRegistryCursor(self._con)

    def close(self) -> None:
        self._con.close()


def pool_row(cid, *, created="2026-08-27 08:00:00", pool="222", monitor=1, sync=0):
    return (pool, monitor, sync, created, cid)


def cl_row(cl_id, cid, status):
    return (cl_id, cid, status)


def make_sqlite_service(pool_rows, cl_rows, task_rows=(), spider_rows=(), callback_rows=(),
                        search_rows=(), **kwargs):
    conn = SqliteRegistryConnection(pool_rows, cl_rows, task_rows, spider_rows,
                                    callback_rows, search_rows)
    return diag.SaasFlowDiagnosticService(
        config=diag.DiagDBConfig(host="db.example.internal", port=3306,
                                 user="ro", password="secret-not-committed"),
        connection_factory=lambda cfg: conn,
        monotonic=kwargs.pop("clock", None) or FakeClock(),
        now_fn=kwargs.pop("now_fn", None),
        **kwargs)


# ---- Test A: duplicate pool rows 只计 1 个商品 ----

def test_card_a_duplicate_pool_rows_count_one_commodity():
    service = make_sqlite_service(
        pool_rows=[pool_row("C1"), pool_row("C1", created="2026-08-27 09:00:00")],
        cl_rows=[])
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["routing_open_total"] == 1
    assert stages["no_commodity_list"] == 1


# ---- Test B: duplicate pool + 单条 crawl1 cl,branch 只分类一次 ----

def test_card_b_duplicate_pool_single_crawl1_classifies_once():
    service = make_sqlite_service(
        pool_rows=[pool_row("C1"), pool_row("C1", created="2026-08-27 09:00:00")],
        cl_rows=[cl_row(11, "C1", 1)],
        task_rows=[(11, "SAAS-188605_701_1", "01")],
        spider_rows=[("188605_701", "E1", 0)],
        callback_rows=[("E1", "8888", "active")])
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["routing_open_total"] == 1
    assert stages["crawl_status_1"] == 1
    assert stages["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1
    assert sum(stages["crawler_branch"].values()) == stages["crawl_status_1"] == 1


# ---- Test C: 同商品两条同状态 commodity_list → evidence incomplete ----

def test_card_c_duplicate_commodity_list_same_status_is_incomplete():
    service = make_sqlite_service(
        pool_rows=[pool_row("C2")],
        cl_rows=[cl_row(101, "C2", 2), cl_row(102, "C2", 2)])
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["routing_open_total"] == 1
    assert stages["routing_evidence_incomplete"] == 1
    assert stages["crawl_status_2"] == 0
    assert stages["downstream_branch"] is not None
    assert sum(stages["downstream_branch"].values()) == 0


# ---- Test D: 同商品冲突状态(1 条 crawl1 + 1 条 crawl2)→ incomplete ----

def test_card_d_conflicting_commodity_list_statuses_is_incomplete():
    service = make_sqlite_service(
        pool_rows=[pool_row("C3")],
        cl_rows=[cl_row(111, "C3", 1), cl_row(112, "C3", 2)])
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["routing_evidence_incomplete"] == 1
    assert stages["crawl_status_1"] == 0
    assert stages["crawl_status_2"] == 0
    assert sum(stages["crawler_branch"].values()) == 0
    assert sum(stages["downstream_branch"].values()) == 0


# ---- Test E: 无 commodity_list → NO_COMMODITY_LIST ----

def test_card_e_no_commodity_list_bucket():
    service = make_sqlite_service(pool_rows=[pool_row("C4")], cl_rows=[])
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["routing_open_total"] == 1
    assert stages["no_commodity_list"] == 1


# ---- Test F: 混合 5 商品完整闭合 ----

def test_card_f_full_routing_closure_five_commodities():
    service = make_sqlite_service(
        pool_rows=[pool_row("C1"), pool_row("C2"), pool_row("C3"),
                   pool_row("C4"), pool_row("C5")],
        cl_rows=[cl_row(21, "C2", 1), cl_row(31, "C3", 2),
                 cl_row(41, "C4", 9), cl_row(51, "C5", 1), cl_row(52, "C5", 2)],
        now_fn=lambda: BEFORE_CUTOFF)
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["routing_open_total"] == 5
    assert stages["no_commodity_list"] == 1
    assert stages["crawl_status_1"] == 1
    assert stages["crawl_status_2"] == 1
    assert stages["crawl_status_other"] == 1
    assert stages["routing_evidence_incomplete"] == 1
    # 闭合不变量:no_cl + c1 + c2 + other + incomplete == routing_open_total
    assert (stages["no_commodity_list"] + stages["crawl_status_1"] + stages["crawl_status_2"]
            + stages["crawl_status_other"] + stages["routing_evidence_incomplete"]) == 5
    # branch 粒度与 routing 商品粒度一致:ambiguous 商品不进入正常分支
    assert sum(stages["crawler_branch"].values()) == stages["crawl_status_1"]
    assert sum(stages["downstream_branch"].values()) == stages["crawl_status_2"]
    assert stages["downstream_branch"]["CALLBACK_DONE_NO_SEARCH"] == 1


# ---- Test G: duplicate pool 不重复 cl.id / branch 分类 ----

def test_card_g_crawl1_duplicate_pool_protection():
    service = make_sqlite_service(
        pool_rows=[pool_row("C1"), pool_row("C1", created="2026-08-27 09:00:00")],
        cl_rows=[cl_row(11, "C1", 1)])
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"]["TASK_NOT_CREATED"] == 1
    assert sum(stages["crawler_branch"].values()) == 1


# ---- Test H: ambiguous 商品不得进入正常 crawler/downstream cohort ----

def test_card_h_ambiguous_commodity_excluded_from_normal_branches():
    service = make_sqlite_service(
        pool_rows=[pool_row("C3")],
        cl_rows=[cl_row(111, "C3", 1), cl_row(112, "C3", 2)],
        task_rows=[(111, "SAAS-188605_701_1", "01")],
        spider_rows=[("188605_701", "E1", 0)],
        callback_rows=[("E1", "8888", "active")])
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["routing_evidence_incomplete"] == 1
    assert sum(stages["crawler_branch"].values()) == 0
    assert sum(stages["downstream_branch"].values()) == 0


# ---- Test I: crawl2 查询失败 → 辅助字段 None,不冒充 0 ----

def test_card_i_crawl2_query_failure_auxiliary_none():
    def crawl2_raises(params):
        raise RuntimeError("crawl2 timeout")

    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 3)],
        "open_crawl_status_counts": [(3, 0, 0, 3, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": crawl2_raises,
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    # route 数量仍用 EVIDENCE_INCOMPLETE 表达
    assert stages["downstream_branch"]["EVIDENCE_INCOMPLETE"] == 3
    # 查询失败 != confirmed zero
    assert stages["reference_pending_approval_total"] is None
    assert stages["callback_done_no_search_within_grace"] is None


# ---- Test J(R2): callback 查询失败 → 商品证据不足,detail 恒 None ----

def test_card_j_callback_query_failure_item_evidence_incomplete():
    def callback_raises(params):
        raise RuntimeError("callback query failed")

    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 1, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0)],
        "crawl1_callback_rows": callback_raises,
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    stages = payload["stages"]
    assert "crawl1_callback_rows" in payload["degraded_queries"]
    assert stages["crawler_branch"]["EVIDENCE_INCOMPLETE"] == 1
    # R2:central 诊断退役,永不冒充"查过 central 且无任务"
    assert stages["central_status_detail"] is None


# ---- Test K(R2): 推送有记录、回调两表均无行、is_call=0 → 已推送暂无回调记录 ----

def test_card_k_push_confirmed_no_callback_record():
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 1, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0)],
        "crawl1_callback_rows": [],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"]["PUSH_CONFIRMED_NO_CALLBACK_RECORD"] == 1
    assert stages["central_status_detail"] is None


# ===========================================================================
# Performance Fix Round 1(2026-08-28)
#   Fix A:crawl1_task_rows 参数类型修正——reference_id 为 varchar(255),传 int
#         会让 MySQL 对列做数值转换、索引失效(EXPLAIN key=idx_table_name/
#         key=NULL,rows≈59万~118万);字符串绑定后 key=idx_reference_id、
#         rows≈95、8~9s → 0.06s(生产只读实测,行集一致)。SQL 文本不变,
#         不用 FORCE INDEX(hint 方案已被生产 EXPLAIN 证伪:仍全表扫)。
#   Fix B:连接故障隔离——2006/2013 杀死连接后废弃并重建,后续 query 不被
#         死连接连带 degraded;失败 query 本身绝不重试。
# ===========================================================================

CRAWL1_CHAIN_CANNED = {
    "pool_sync_standard_counts": [("0", 1)],
    "open_crawl_status_counts": [(1, 0, 1, 0, 0)],
    "crawl1_commodity_rows": [(11, "C1")],
    "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
}


class _FakeMySQLError(Exception):
    """形似 pymysql OperationalError:args = (errno, message)。"""


def _make_multi_conn_service(conn_canned_list, factory_side_effect=None):
    """factory 按序返回给定 FakeConnection;可注入第 N 次调用抛异常。"""
    conns = [FakeConnection(canned, []) for canned in conn_canned_list]
    remaining = list(conns)
    calls = {"connect": 0}

    def factory(cfg):
        calls["connect"] += 1
        if factory_side_effect is not None:
            error = factory_side_effect(calls["connect"])
            if error is not None:
                raise error
        if remaining:
            return remaining.pop(0)
        raise AssertionError("unexpected extra connection factory call")

    service = diag.SaasFlowDiagnosticService(
        config=diag.DiagDBConfig(host="db.example.internal", port=3306,
                                 user="ro", password="secret-not-committed"),
        connection_factory=factory,
        monotonic=FakeClock(),
    )
    return service, conns, calls


# ---- Fix A 契约 ----

def test_r1_fix_a_task_sql_contract_unchanged():
    sql = diag.DIAG_SQL_REGISTRY["crawl1_task_rows"]
    # 生产 EXPLAIN 证伪 FORCE INDEX(hint 下仍 type=ALL 全表扫),不得引入
    assert "FORCE INDEX" not in sql and "USE INDEX" not in sql
    # WHERE 条件逐字保留
    assert "t.table_name = '188605'" in sql
    assert "t.reference_id IN %(commodity_list_ids)s" in sql
    assert diag.QUERY_COLUMNS["crawl1_task_rows"] == (
        "commodity_list_id", "task_id", "task_status")
    assert diag.validate_readonly_statement(sql) == []


def test_r1_fix_a_task_params_bound_as_native_column_type_strings():
    """reference_id 是 varchar(255):int 绑定会让 MySQL 弃用 idx_reference_id
    (生产 EXPLAIN rows≈59 万);必须以字符串绑定(实测 rows≈95)。"""
    canned = dict(CRAWL1_CHAIN_CANNED)
    canned["crawl1_spider_task_rows"] = [("188605_701", "E1", 0)]
    canned["crawl1_callback_rows"] = [("E1", "8888", "active")]
    canned["crawl2_search_flags"] = []
    service, executed, _ = make_service(canned)
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "ok"
    bound = [params["commodity_list_ids"] for name, params in executed
             if name == "crawl1_task_rows"]
    assert bound and all(isinstance(v, str) for v in bound[0])


# ---- Fix B:连接故障隔离 ----

def test_r1_fix_b_is_connection_killing_error_classification():
    assert diag._is_connection_killing_error(_FakeMySQLError(2013, "lost")) is True
    assert diag._is_connection_killing_error(_FakeMySQLError(2006, "gone")) is True
    # 普通 SQL 错误绝不能当连接死亡(否则会疯狂重连)
    for errno in (1142, 1146, 1054, 1064, 1044):
        assert diag._is_connection_killing_error(_FakeMySQLError(errno, "x")) is False
    assert diag._is_connection_killing_error(RuntimeError("boom")) is False
    assert diag._is_connection_killing_error(_FakeMySQLError("2013", "str-errno")) is False


def test_r1_fix_b_2013_discards_connection_next_query_succeeds():
    """spider 查询 2013 杀死连接 → 后续独立查询(crawl2)必须通过新连接正常执行,
    不再被连带 degraded;spider 查询本身只执行一次(绝不重试;
    spider 失败后无 exe 可查,callback 依设计不再执行)。"""
    conn1_canned = dict(CRAWL1_CHAIN_CANNED)
    spider_attempts = {"n": 0}

    def spider_2013(params):
        spider_attempts["n"] += 1
        raise _FakeMySQLError(2013, "Lost connection to MySQL server during query")

    conn1_canned["crawl1_spider_task_rows"] = spider_2013
    conn2_canned = {"crawl2_search_flags": [
        (21, 0, "2026-08-27 09:00:00", 0, 0, 0, 0, 0, 0, 0, 0, 0)]}
    service, conns, calls = _make_multi_conn_service([conn1_canned, conn2_canned])
    payload = service.get_diagnostic("2026-08-27")
    assert payload["degraded_queries"] == ["crawl1_spider_task_rows"]
    assert payload["status"] == "partial"
    assert calls["connect"] == 2
    # 死连接与替换连接都 best-effort close,无泄漏
    assert conns[0].closed is True and conns[1].closed is True
    # spider 只尝试过一次(绝不重试);crawl2 在新连接上成功
    assert spider_attempts["n"] == 1
    assert "crawl1_spider_task_rows" not in [name for name, _ in conns[1]._executed]
    assert "crawl2_search_flags" in [name for name, _ in conns[1]._executed]
    # routing 证据保留,spider 失败语义不变(证据不足,非业务 0)
    stages = payload["stages"]
    assert stages["crawl_status_1"] == 1
    assert stages["crawler_branch"]["EVIDENCE_INCOMPLETE"] == 1


def test_r2_fix_b_task_2013_next_queries_run_on_new_connection():
    """task 查询 2013 → 依赖 task 数据的 spider/callback 不再执行(无参可查),
    独立的 crawl2 经新连接继续执行。"""
    conn1_canned = dict(CRAWL1_CHAIN_CANNED)

    def task_2013(params):
        raise _FakeMySQLError(2013, "lost")

    conn1_canned["crawl1_task_rows"] = task_2013
    conn2_canned = {"crawl2_search_flags": []}
    service, conns, calls = _make_multi_conn_service([conn1_canned, conn2_canned])
    payload = service.get_diagnostic("2026-08-27")
    assert payload["degraded_queries"] == ["crawl1_task_rows"]
    assert calls["connect"] == 2
    conn2_names = [name for name, _ in conns[1]._executed]
    assert "crawl2_search_flags" in conn2_names
    # task 失败 → 无 batch 可查,spider/callback 依设计跳过
    assert "crawl1_spider_task_rows" not in conn2_names
    assert "crawl1_callback_rows" not in conn2_names


def test_r1_fix_b_2006_also_triggers_reconnect():
    conn1_canned = dict(CRAWL1_CHAIN_CANNED)

    def task_2006(params):
        raise _FakeMySQLError(2006, "MySQL server has gone away")

    conn1_canned["crawl1_task_rows"] = task_2006
    conn2_canned = {"crawl1_spider_task_rows": [], "crawl1_callback_rows": [],
                    "crawl2_search_flags": []}
    service, conns, calls = _make_multi_conn_service([conn1_canned, conn2_canned])
    payload = service.get_diagnostic("2026-08-27")
    assert payload["degraded_queries"] == ["crawl1_task_rows"]
    assert calls["connect"] == 2
    assert conns[0].closed is True and conns[1].closed is True
    # task 失败 → 商品级分类不可用,但 crawl2 仍正常执行
    assert payload["stages"]["crawler_branch"]["EVIDENCE_INCOMPLETE"] == 1
    assert "crawl2_search_flags" not in payload["degraded_queries"]


def test_r1_fix_b_ordinary_sql_error_does_not_reconnect():
    """1142 等 SQL 错误不是连接死亡:不得触发换连接/重建。"""
    conn1_canned = dict(CRAWL1_CHAIN_CANNED)

    def spider_1142(params):
        raise _FakeMySQLError(1142, "SELECT command denied")

    conn1_canned["crawl1_spider_task_rows"] = spider_1142
    # routing 层 crawl2=0:crawl2 查询成功但返回空(避免 detail>route 假 drift)
    conn1_canned["crawl2_search_flags"] = []
    service, conns, calls = _make_multi_conn_service([conn1_canned])
    payload = service.get_diagnostic("2026-08-27")
    assert payload["degraded_queries"] == ["crawl1_spider_task_rows"]
    assert calls["connect"] == 1  # 没有重建连接
    # crawl2 在同一连接上继续执行成功(不在 degraded)
    assert "crawl2_search_flags" in [name for name, _ in conns[0]._executed]
    assert payload["stages"]["downstream_branch"] is not None
    assert sum(payload["stages"]["downstream_branch"].values()) == 0


def test_r1_fix_b_replacement_failure_degrades_safely():
    """spider 2013 后重建连接也失败:已有证据保留,后续诊断降级,
    不 500、不伪造业务 0、不泄漏凭据。"""
    conn1_canned = dict(CRAWL1_CHAIN_CANNED)

    def spider_2013(params):
        raise _FakeMySQLError(2013, "Lost connection to MySQL server during query")

    conn1_canned["crawl1_spider_task_rows"] = spider_2013

    def second_call_fails(call_number):
        return ConnectionError("reconnect refused") if call_number == 2 else None

    service, conns, calls = _make_multi_conn_service(
        [conn1_canned], factory_side_effect=second_call_fails)
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "partial"  # 安全降级,不是 5xx
    # spider 失败 → 无 exe 可查(callback 不执行);crawl2 因替换失败连带降级
    assert payload["degraded_queries"] == ["crawl1_spider_task_rows", "crawl2_search_flags"]
    # 已取得的 routing 证据不丢失
    assert payload["stages"]["routing_open_total"] == 1
    assert payload["stages"]["crawl_status_1"] == 1
    # crawl2 未被伪造为零计数:分支为 EVIDENCE_INCOMPLETE 表达(无 crawl2 数据)
    text = json.dumps(payload, ensure_ascii=False)
    assert "secret-not-committed" not in text
    assert calls["connect"] == 2
    assert conns[0].closed is True


def test_r1_fix_b_dead_connection_close_failure_is_tolerated():
    """死连接 close() 抛异常也必须被容忍,替换连接照常创建。"""

    class CloseExplodes(FakeConnection):
        def close(self):
            self.closed = True
            raise RuntimeError("close failed")

    conn1_canned = dict(CRAWL1_CHAIN_CANNED)

    def spider_2013(params):
        raise _FakeMySQLError(2013, "lost")

    conn1_canned["crawl1_spider_task_rows"] = spider_2013
    conn1 = CloseExplodes(conn1_canned, [])
    conn2 = FakeConnection({"crawl1_callback_rows": [], "crawl2_search_flags": []}, [])
    calls = {"connect": 0}
    conns = [conn1, conn2]

    def factory(cfg):
        calls["connect"] += 1
        return conns.pop(0)

    service = diag.SaasFlowDiagnosticService(
        config=diag.DiagDBConfig(host="db.example.internal", port=3306,
                                 user="ro", password="secret-not-committed"),
        connection_factory=factory,
        monotonic=FakeClock(),
    )
    payload = service.get_diagnostic("2026-08-27")
    assert payload["degraded_queries"] == ["crawl1_spider_task_rows"]
    assert calls["connect"] == 2
    assert conn2.closed is True  # 替换连接被正确关闭

# ===========================================================================
# Round 2 Central Bypass(2026-08-28): crawl1 主链 = task → batch → spider
# 推送 → execution_number → callback 证据;s_task_info 不再执行,central 状态退役。
# ===========================================================================

# ---- §17 SQL 安全:主链不引用 central;有界 IN 是唯一过滤形态 ----

def test_r2_registry_primary_path_never_references_central():
    for name, sql in diag.DIAG_SQL_REGISTRY.items():
        assert "s_task_info" not in sql, name
        assert "central_control" not in sql, name
    assert "crawl1_central_rows" not in diag.DIAG_SQL_REGISTRY
    assert "crawl1_central_rows" not in diag.QUERY_COLUMNS


def test_r2_spider_and_callback_sql_shape():
    spider = diag.DIAG_SQL_REGISTRY["crawl1_spider_task_rows"]
    callback = diag.DIAG_SQL_REGISTRY["crawl1_callback_rows"]
    for sql in (spider, callback):
        low = sql.lower()
        assert "select *" not in low
        assert "like" not in low and "regexp" not in low
        assert "force index" not in low and "use index" not in low
        assert diag.validate_readonly_statement(sql) == []
    assert "r.batch_num IN %(batch_nums)s" in spider
    assert "execution_number" not in spider  # spider 只按 batch 查
    assert callback.count("%(execution_numbers)s") == 2  # active+completed 各一次
    # callback SQL 不与 task/search 表同句
    assert "task_table" not in callback and "search_table" not in callback


# ---- §18 参数类型回归:batch_nums / execution_numbers 全为 string ----

def test_r2_batch_and_execution_params_bound_as_strings():
    service, executed, _ = make_service({
        "pool_sync_standard_counts": [("0", 2)],
        "open_crawl_status_counts": [(2, 0, 2, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1"), (12, "C2")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1), (12, "SAAS-188605_702_1", 1)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0), ("188605_702", "E2", 1)],
        "crawl1_callback_rows": [("E1", "8888", "active"), ("E2", "0000", "completed")],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "ok"
    for name, params in executed:
        if name == "crawl1_spider_task_rows":
            assert params["batch_nums"] and all(isinstance(v, str) for v in params["batch_nums"])
        if name == "crawl1_callback_rows":
            assert params["execution_numbers"] and all(
                isinstance(v, str) for v in params["execution_numbers"])


# ---- §16 场景:多 batch / 多 exec / 聚合 / 去重 / overflow ----

def test_r2_item_with_two_batches_full_coverage():
    """同一商品两个 task → 两个 batch;两 batch 均有推送记录,
    exec 一在途一完成 → CALLBACK_PARTIAL(多 batch 全覆盖)。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 1, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1), (11, "SAAS-188605_702_1", 1)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0), ("188605_702", "E2", 1)],
        "crawl1_callback_rows": [("E1", "8888", "active"), ("E2", "0000", "completed")],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"]["CALLBACK_PARTIAL"] == 1
    assert sum(stages["crawler_branch"].values()) == stages["crawl_status_1"] == 1


def test_r2_shared_batch_deduplication():
    """18 个 task 共享同一 batch:batch 去重后仍是一个推送单位,
    商品只分类一次(不因 task 行数放大)。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 1, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_%d" % i, 1) for i in range(18)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0)],
        "crawl1_callback_rows": [("E1", "8888", "active")],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1
    assert sum(stages["crawler_branch"].values()) == 1


def test_r2_multiple_executions_aggregate():
    """单 batch 多 execution:全 success → CALLBACK_DONE_SAAS_PENDING。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 1, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 1), ("188605_701", "E2", 1)],
        "crawl1_callback_rows": [("E1", "0000", "completed"), ("E2", "0000", "completed")],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"]["CALLBACK_DONE_SAAS_PENDING"] == 1


def test_r2_duplicate_callback_rows_do_not_inflate_items():
    """同一 execution 的重复 callback 行不得放大商品计数(set 归一)。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 1, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0)],
        "crawl1_callback_rows": [("E1", "8888", "active"), ("E1", "8888", "active")],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1
    assert sum(stages["crawler_branch"].values()) == 1


def test_r2_overflow_counts_evidence_incomplete():
    """routing 称 crawl1=3 但 detail 只有 1 行(截断/窗口)→ 溢出计入证据不足。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 3)],
        "open_crawl_status_counts": [(3, 0, 3, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0)],
        "crawl1_callback_rows": [("E1", "8888", "active")],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1
    assert stages["crawler_branch"]["EVIDENCE_INCOMPLETE"] == 2
    assert sum(stages["crawler_branch"].values()) == stages["crawl_status_1"] == 3


def test_r2_taskless_item_and_full_chain_item_close_together():
    """A 无 task → TASK_NOT_CREATED;B 全链完成 → DONE_SAAS_PENDING;分支闭合。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 2)],
        "open_crawl_status_counts": [(2, 0, 2, 0, 0)],
        "crawl1_commodity_rows": [(11, "CA"), (12, "CB")],
        "crawl1_task_rows": [(12, "SAAS-188605_702_1", 1)],
        "crawl1_spider_task_rows": [("188605_702", "E2", 1)],
        "crawl1_callback_rows": [("E2", "0000", "completed")],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"]["TASK_NOT_CREATED"] == 1
    assert stages["crawler_branch"]["CALLBACK_DONE_SAAS_PENDING"] == 1
    assert sum(stages["crawler_branch"].values()) == stages["crawl_status_1"] == 2


def test_r2_sqlite_end_to_end_completed_chain():
    """SQLite 全链(task→batch→spider→completed callback)端到端。"""
    service = make_sqlite_service(
        pool_rows=[pool_row("C9")],
        cl_rows=[cl_row(91, "C9", 1)],
        task_rows=[(91, "SAAS-188605_709_1", "01")],
        spider_rows=[("188605_709", "X9", 1)],
        callback_rows=[("X9", "0000", "completed")])
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"]["CALLBACK_DONE_SAAS_PENDING"] == 1
    assert sum(stages["crawler_branch"].values()) == stages["crawl_status_1"] == 1


def test_r2_sqlite_partial_batch_coverage():
    """SQLite:两个 batch 只有一个有 spider 记录 → EVIDENCE_INCOMPLETE。"""
    service = make_sqlite_service(
        pool_rows=[pool_row("C8")],
        cl_rows=[cl_row(81, "C8", 1)],
        task_rows=[(81, "SAAS-188605_701_1", "01"), (81, "SAAS-188605_702_1", "01")],
        spider_rows=[("188605_701", "X1", 0)],
        callback_rows=[("X1", "8888", "active")])
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"]["EVIDENCE_INCOMPLETE"] == 1


# ---- §15 契约:dominant 参与新状态,EVIDENCE_INCOMPLETE 仍不参与 ----

def test_r2_dominant_uses_new_crawler_states():
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 2)],
        "open_crawl_status_counts": [(2, 0, 2, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1"), (12, "C2")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1), (12, "SAAS-188605_702_1", 1)],
        "crawl1_spider_task_rows": [("188605_701", "E1", 0), ("188605_702", "E2", 0)],
        "crawl1_callback_rows": [("E1", "8888", "active"), ("E2", "8888", "active")],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["dominant_stage"]["key"] == "CALLBACK_IN_FLIGHT"


# ===========================================================================
# Round 3(2026-08-28)
#   BUG A:顶部实时数冒用历史 observation snapshot(growth.customer_count);
#         新增 live_counts(count-only 独立只读链),增长序列原样保留。
#   BUG B:商品池数量视觉闭合(closed_total / pool_state_other 后端派生)。
# ===========================================================================

def _live_payload(customer=66, saas=0):
    return {"available": True, "customer_count": customer, "saas_count": saas,
            "observed_at": "2026-08-28T06:30:00+00:00"}


def test_r3_live_counts_included_on_fresh_build(tmp_path):
    """A5:live fetcher 的 count 进入 payload.live_counts;DB stages 不受影响;
    历史增长序列独立(live 可用而本地无观察时 growth 明确 NO_OBSERVATIONS)。"""
    service, _, _ = make_service(dict(FULL_CANNED), runtime_root=tmp_path,
                                 now_fn=lambda: BEFORE_CUTOFF,
                                 live_fetcher=lambda: _live_payload())
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "ok"
    assert payload["live_counts"]["available"] is True
    assert payload["live_counts"]["customer_count"] == 66
    assert payload["live_counts"]["saas_count"] == 0
    assert payload["live_counts"]["observed_at"]
    # 历史增长序列原样保留且与实时数分离:本地无观察 ≠ 客户 0/66
    assert payload["growth"]["available"] is False
    assert payload["growth"]["evaluation_reason"] == "NO_OBSERVATIONS"
    assert payload["stages"]["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1


def test_r3_live_counts_cached_response_does_not_refetch():
    """A7:缓存命中(≤60s TTL)不重复打 live API;缓存里保留上次 live 读数。"""
    calls = []

    def fetcher():
        calls.append(1)
        return _live_payload()

    clock = FakeClock()
    service, _, _ = make_service(dict(FULL_CANNED), clock=clock, live_fetcher=fetcher)
    first = service.get_diagnostic("2026-08-27")
    clock.advance(30.0)  # TTL 内
    second = service.get_diagnostic("2026-08-27")
    assert first["cached"] is False and second["cached"] is True
    assert len(calls) == 1
    assert second["live_counts"]["customer_count"] == 66


def test_r3_live_counts_failure_degrades_not_zero():
    """A6(service 级):live 失败 → available=False、计数 None(绝不冒充 0),
    stages 照常;错误只含安全异常类型名。"""
    def boom():
        raise RuntimeError("network down")

    service, _, _ = make_service(dict(FULL_CANNED), live_fetcher=boom)
    payload = service.get_diagnostic("2026-08-27")
    live = payload["live_counts"]
    assert live["available"] is False
    assert live["customer_count"] is None and live["saas_count"] is None
    assert live["observed_at"] is None
    assert live["error"] == {"type": "RuntimeError"}
    assert payload["status"] == "ok"
    assert payload["stages"]["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1


def test_r3_live_counts_without_fetcher_unavailable_not_fake_zero():
    """未注入 live fetcher → 明确 unavailable,不是客户 0/SaaS 0。"""
    service, _, _ = make_service(dict(FULL_CANNED))
    live = service.get_diagnostic("2026-08-27")["live_counts"]
    assert live["available"] is False
    assert live["customer_count"] is None and live["saas_count"] is None


def test_r3_live_counts_attempted_even_when_db_unconfigured():
    """DB 未配置时 live_counts 仍独立可得(两条链互不拖累)。"""
    service, _, _ = make_service({}, config=None, live_fetcher=lambda: _live_payload())
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "unconfigured"
    assert payload["live_counts"]["available"] is True
    assert payload["live_counts"]["customer_count"] == 66


def test_r3_default_live_fetcher_unavailable_when_curl_config_missing(tmp_path):
    """A6(默认 fetcher):生产 ignored curl 配置缺失 → available=False 快速返回。"""
    result = diag.default_live_counts_fetcher(data_root=tmp_path)
    assert result["available"] is False
    assert result["customer_count"] is None and result["saas_count"] is None
    assert result["error"]["type"] == "CONFIG_MISSING"


def test_r3_endpoint_live_counts_and_failure_scope(client, monkeypatch, tmp_path):
    """A6(endpoint 级):live 失败 → 200 + live_counts.available=False + stages 正常。"""
    import app.services.saas_flow_diagnostic as mod

    def boom():
        raise RuntimeError("live endpoint down")

    service, _, _ = make_service(dict(FULL_CANNED), runtime_root=tmp_path,
                                 live_fetcher=boom)
    monkeypatch.setattr(mod, "_SERVICE_SINGLETON", service)
    r = client.get("/dashboard/api/saas-flow?date=2026-08-27")
    assert r.status_code == 200
    body = r.json()
    assert body["live_counts"]["available"] is False
    assert body["live_counts"]["customer_count"] is None
    assert body["stages"]["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1
    assert body["growth"]["available"] is False  # 历史序列仍在且未被实时数污染


def test_r3_endpoint_live_counts_served_on_success(client, monkeypatch):
    import app.services.saas_flow_diagnostic as mod

    service, _, _ = make_service(dict(FULL_CANNED), live_fetcher=lambda: _live_payload(66, 0))
    monkeypatch.setattr(mod, "_SERVICE_SINGLETON", service)
    r = client.get("/dashboard/api/saas-flow?date=2026-08-27")
    assert r.status_code == 200
    live = r.json()["live_counts"]
    assert live["available"] is True and live["customer_count"] == 66 and live["saas_count"] == 0


# ---- R3 BUG B:pool closure 派生字段 ----

def _pool_canned(sync_rows):
    return {
        "pool_sync_standard_counts": sync_rows,
        "open_crawl_status_counts": [(50, 1, 0, 49, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [(21, 0, "2026-08-27 09:00:00", 0, 0, 0, 0, 0, 0, 0, 0, 0)] * 49,
    }


def test_r3_pool_closure_fields_derived():
    """B1:71=50 未收口 + 15 closed_standard + 6 closed_no_same → closed_total=21,other=0。"""
    service, _, _ = make_service(_pool_canned([("0", 50), ("1", 15), ("2", 6)]))
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["pool_scope_total"] == 71
    assert stages["not_closed"] == 50
    assert stages["closed_total"] == 21
    assert stages["pool_state_other"] == 0


def test_r3_unknown_pool_state_not_folded_into_closed():
    """B5:未知 IS_SYNC_STANDARD 值单独进 pool_state_other,不得并入已最终收口。"""
    service, _, _ = make_service(
        _pool_canned([("0", 50), ("1", 15), ("2", 6), ("3", 2)]))
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["pool_scope_total"] == 73
    assert stages["closed_total"] == 21
    assert stages["pool_state_other"] == 2


def test_r3_pool_failure_closure_fields_none_not_zero():
    """pool 查询失败 → 派生字段 None,不冒充 0。"""
    def pool_raises(params):
        raise RuntimeError("pool timeout")

    service, _, _ = make_service({
        "pool_sync_standard_counts": pool_raises,
        "open_crawl_status_counts": [(50, 1, 0, 49, 0)],
        "crawl1_commodity_rows": [],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["closed_total"] is None
    assert stages["pool_state_other"] is None
    assert stages["pool_counts_available"] is False


# ---- R3 GitHub Review P1:live 链认证失败只降级 live_counts ----

def test_r3_p1e_live_counts_auth_error_degrades_only_live_preserves_stages(tmp_path):
    """P1-E(service 级):AuthError(401/认证失败且禁止刷新)→ live unavailable
    (错误只含类型名),DB stages/growth 原样,status 不因 live 失败变 unavailable。"""
    from scripts.auth_manager import AuthError

    def auth_rejected():
        raise AuthError("SaaS 认证失败(HTTP 401);本次调用禁止自动刷新 Token")

    service, _, _ = make_service(dict(FULL_CANNED), runtime_root=tmp_path,
                                 now_fn=lambda: BEFORE_CUTOFF,
                                 live_fetcher=auth_rejected)
    payload = service.get_diagnostic("2026-08-27")
    live = payload["live_counts"]
    assert live["available"] is False
    assert live["customer_count"] is None and live["saas_count"] is None
    assert live["observed_at"] is None
    assert live["error"] == {"type": "AuthError"}
    assert payload["status"] == "ok"
    assert payload["stages"]["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1
    assert payload["growth"]["available"] is False  # 历史观察序列独立、未被污染


def test_r3_p1e_endpoint_auth_failure_live_down_http_200(client, monkeypatch, tmp_path):
    """P1-E(endpoint 级):live 认证失败 → /dashboard/api/saas-flow 仍 200,
    live_counts 降级,stages/growth 照常。"""
    from scripts.auth_manager import AuthError
    import app.services.saas_flow_diagnostic as mod

    def auth_rejected():
        raise AuthError("SaaS 认证失败(响应报文);本次调用禁止自动刷新 Token")

    service, _, _ = make_service(dict(FULL_CANNED), runtime_root=tmp_path,
                                 live_fetcher=auth_rejected)
    monkeypatch.setattr(mod, "_SERVICE_SINGLETON", service)
    r = client.get("/dashboard/api/saas-flow?date=2026-08-27")
    assert r.status_code == 200
    body = r.json()
    assert body["live_counts"]["available"] is False
    assert body["live_counts"]["error"] == {"type": "AuthError"}
    assert body["status"] == "ok"
    assert body["stages"]["crawler_branch"]["CALLBACK_IN_FLIGHT"] == 1
    assert body["growth"]["available"] is False
    # P1-F:整包响应绝不携带任何 token 片段
    assert "live-r" not in r.text and "live-a" not in r.text
