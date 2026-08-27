"""STEP_1 Dashboard SaaS Flow Diagnostic 测试。

契约:docs/superpowers/specs/2026-08-27-step1-saas-flow-diagnostic.md。
全部用 fake connection / mock cursor / tmp SQLite,不连接任何真实数据库,
不执行任何生产 SQL。字段语义来源:docs/sql_ops KB(FRONTEND/LIVE-SQL/SCHEMA)。
"""
from __future__ import annotations

import json
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
                 grace_seconds=1800, ttl=60.0, fresh_min_interval=30.0, now_fn=None):
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
    def central_raises(params):
        raise RuntimeError("central query failed")

    service, executed, _ = make_service({
        "pool_sync_standard_counts": [("0", 3)],
        "open_crawl_status_counts": [(3, 0, 1, 2, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
        "crawl1_central_rows": central_raises,
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "partial"
    assert "crawl1_central_rows" in payload["degraded_queries"]
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
    "crawl1_central_rows": [("188605_701", "00", "00", None, None)],
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
    assert stages["crawler_branch"]["CENTRAL_NOT_ENQUEUED"] == 1
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
# D. crawler branch 分类(crawl_status=1)
# ---------------------------------------------------------------------------

def central(*rows):
    return [dict(zip(("task_status", "queue_status", "enqueue_time", "end_time"), row)) for row in rows]


def test_task_not_created():
    assert diag.classify_crawler_branch({"task_count": 0, "central": []}) == "TASK_NOT_CREATED"


def test_task_created_no_central_record():
    record = {"task_count": 3, "central": []}
    assert diag.classify_crawler_branch(record) == "TASK_CREATED_NO_CENTRAL_RECORD"


def test_central_not_enqueued():
    record = {"task_count": 2, "central": central(("00", "00", None, None), ("00", "00", None, None))}
    assert diag.classify_crawler_branch(record) == "CENTRAL_NOT_ENQUEUED"


def test_central_enqueued_waiting():
    record = {"task_count": 2, "central": central(("00", "01", "2026-08-27 08:00:00", None))}
    assert diag.classify_crawler_branch(record) == "CENTRAL_ENQUEUED_WAITING"


def test_crawler_executing():
    record = {"task_count": 2, "central": central(("01", "02", "2026-08-27 08:00:00", None))}
    assert diag.classify_crawler_branch(record) == "CRAWLER_EXECUTING"
    # 02 但 end_time 为空:完成证据不足,仍在执行
    record2 = {"task_count": 2, "central": central(("02", "01", "2026-08-27 08:00:00", None))}
    assert diag.classify_crawler_branch(record2) == "CRAWLER_EXECUTING"


def test_crawler_failed():
    record = {"task_count": 2, "central": central(("03", "02", "2026-08-27 08:00:00", "2026-08-27 08:01:00"))}
    assert diag.classify_crawler_branch(record) == "CRAWLER_FAILED"


def test_callback_lag_requires_all_subtasks_completed_with_end_time():
    # 02/01 + end_time 非空 = 已完成(queue_status 不必是 02)——生产实测修正
    record = {"task_count": 18, "central": central(
        ("02", "01", "2026-08-27 08:00:00", "2026-08-27 08:00:01"),
        ("02", "02", "2026-08-27 08:00:00", "2026-08-27 08:00:02"))}
    assert diag.classify_crawler_branch(record) == "CALLBACK_LAG"
    # 混合:部分完成 + 部分未入队 → 不是 callback lag
    mixed = {"task_count": 18, "central": central(
        ("02", "02", "2026-08-27 08:00:00", "2026-08-27 08:00:02"),
        ("00", "00", None, None))}
    assert diag.classify_crawler_branch(mixed) == "CENTRAL_NOT_ENQUEUED"


def test_crawler_evidence_incomplete_on_unknown_states():
    record = {"task_count": 2, "central": central((None, None, None, None))}
    assert diag.classify_crawler_branch(record) == "EVIDENCE_INCOMPLETE"


def test_start_time_is_never_used_as_execution_proof():
    """分类输入不包含 start_time;证据字段只有 enqueue/task/queue/end。"""
    import inspect
    source = inspect.getsource(diag.classify_crawler_branch)
    assert "start_time" not in source


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
    # central 返回未知状态(有 task、有分控行,但状态不可解释)→ crawler 证据不足
    canned["crawl1_central_rows"] = [("188605_701", None, None, None, None)]
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


# ---- P1-2:central 查询失败 = 证据不足,不是 task 未进分控 ----

def test_p1_2_central_query_failure_is_evidence_incomplete_not_no_central_record():
    def central_raises(params):
        raise RuntimeError("central timeout")

    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 2)],
        "open_crawl_status_counts": [(2, 0, 2, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1"), (12, "C2")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1), (12, "SAAS-188605_702_1", 1)],
        "crawl1_central_rows": central_raises,
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    assert payload["status"] == "partial"
    assert "crawl1_central_rows" in payload["degraded_queries"]
    crawler = payload["stages"]["crawler_branch"]
    assert crawler["EVIDENCE_INCOMPLETE"] == 2
    assert crawler["TASK_CREATED_NO_CENTRAL_RECORD"] == 0


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
        "crawl1_central_rows": [("188605_701", "00", "00", None, None)],
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
    assert stages["crawler_branch"]["CENTRAL_NOT_ENQUEUED"] == 1
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
        "crawl1_central_rows": [],
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
        "crawl1_central_rows": [("188605_701", "00", "00", None, None)],
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    crawler = payload["stages"]["crawler_branch"]
    assert crawler["EVIDENCE_INCOMPLETE"] == 1
    assert crawler["TASK_CREATED_NO_CENTRAL_RECORD"] == 0
    assert crawler["CENTRAL_NOT_ENQUEUED"] == 0


def test_r2_p1_2_valid_tasks_with_empty_central_keeps_no_central_record():
    """全部 task 可解析、central 查询成功且确实无行 → 保留真实业务分类。"""
    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 1)],
        "open_crawl_status_counts": [(1, 0, 1, 0, 0)],
        "crawl1_commodity_rows": [(11, "C1")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],
        "crawl1_central_rows": [],  # 查询成功,确实没有分控行
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    crawler = payload["stages"]["crawler_branch"]
    assert crawler["TASK_CREATED_NO_CENTRAL_RECORD"] == 1
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


# ---- P2-2:central 失败不得覆盖已确定的 TASK_NOT_CREATED ----

def test_r2_p2_2_central_failure_keeps_task_not_created_for_taskless_commodity():
    """A 有 task(batch 查询导致 central 整体失败)→ EVIDENCE_INCOMPLETE;
    B 无 task → TASK_NOT_CREATED(该判断不依赖 central 查询)。"""
    def central_raises(params):
        raise RuntimeError("central timeout")

    service, _, _ = make_service({
        "pool_sync_standard_counts": [("0", 2)],
        "open_crawl_status_counts": [(2, 0, 2, 0, 0)],
        "crawl1_commodity_rows": [(11, "CA"), (12, "CB")],
        "crawl1_task_rows": [(11, "SAAS-188605_701_1", 1)],  # 仅 A 有 task
        "crawl1_central_rows": central_raises,
        "crawl2_search_flags": [],
    })
    payload = service.get_diagnostic("2026-08-27")
    crawler = payload["stages"]["crawler_branch"]
    assert crawler["EVIDENCE_INCOMPLETE"] == 1   # A:有 task,central 失败
    assert crawler["TASK_NOT_CREATED"] == 1      # B:无 task,结论确定
    assert payload["status"] == "partial"
    assert "crawl1_central_rows" in payload["degraded_queries"]


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
        "crawl1_central_rows": [],
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
        "crawl1_central_rows": [],
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
        "crawl1_central_rows": [],
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
        "crawl1_central_rows": [],
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
        "crawl1_central_rows": [],
        "crawl2_search_flags": [],
    })
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"] is None
    assert stages["central_status_detail"] is None


def test_r3_p2_normal_branch_real_zero_stays_zero():
    """正常 branch:真实 0 仍是 0,真实证据记录原样保留。"""
    service, _, _ = make_service(dict(FULL_CANNED), now_fn=lambda: BEFORE_CUTOFF)
    stages = service.get_diagnostic("2026-08-27")["stages"]
    assert stages["crawler_branch"] is not None
    # 查询成功且确有 1 条 00/00 分控记录 → detail 原样保留(真实空列表仍会是 [])
    assert stages["central_status_detail"] == [
        {"batch_num": "188605_701", "task_status": "00", "queue_status": "00", "count": 1}]
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
