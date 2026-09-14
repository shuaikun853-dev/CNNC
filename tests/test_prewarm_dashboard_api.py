"""Issue #13 Slice C-API:预爬页路由 + 只读投影 API + 受控手动重爬接线测试。

与 test_dashboard_api.py 同 harness:TestClient(app) + monkeypatch
``app.dashboard_api`` 模块属性注入替身(投影函数 / 手动重爬 runner)。
零写用例用 tmp_path 下真实最小三库(与 test_prewarm_dashboard_projection.py
同 fixture 模式),经包装器把真实投影函数指到 tmp runtime,SHA256 前后
一致证明 GET 零写、零新建文件。
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    CustomerPrewarmStore,
)
from app.services.prewarm_dashboard_projection import (
    build_prewarm_dashboard_summary as real_build_prewarm_summary,
    get_prewarm_link_detail as real_get_prewarm_detail,
    list_prewarm_links as real_list_prewarm_links,
)
from app.services.prewarm_manual_recrawl import ManualRecrawlError
from app.services.shared_crawl_cache import SharedCrawlCache
from app.services.staples_prewarm import StaplesPrewarmStore

BUSINESS_DATE = "2026-09-08"
STAPLES_URL = "https://www.stbchina.cn/item.html?itemId=1001358701037447"
SUCCEEDED_AT = "2026-09-08T04:00:00+00:00"


@pytest.fixture()
def client():
    return TestClient(app)


def _operator_token(client: TestClient) -> str:
    return client.get("/dashboard/api/operator-context").json()["action_token"]


class _FakeRunner:
    """start()/get() 替身:记录 start 调用,可注入返回值或异常。"""

    def __init__(self, handle=None, error=None, get_result=None):
        self.handle = handle or {"action_id": "act-1", "status": "RUNNING",
                                 "route": "staples-local", "mall": "stbchina"}
        self.error = error
        self.get_result = get_result
        self.calls: list[dict] = []

    def start(self, business_date, *, candidate_id,
              expected_original_url=None, expected_status=None):
        self.calls.append({
            "business_date": business_date, "candidate_id": candidate_id,
            "expected_original_url": expected_original_url,
            "expected_status": expected_status,
        })
        if self.error is not None:
            raise self.error
        return dict(self.handle)

    def get(self, action_id):
        return dict(self.get_result) if self.get_result is not None else None


# ======================================================================
# 页面:GET /dashboard/prewarm
# ======================================================================

def test_prewarm_page_served_with_versioned_assets(client):
    r = client.get("/dashboard/prewarm")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-cache"
    assert "客户链接预爬" in r.text
    assert r.text.count("?v=") == 3
    import re
    for name in ("shell.css", "prewarm.css", "prewarm.js"):
        assert re.search(rf"/dashboard/static/{re.escape(name)}\?v=\d+", r.text), name
    disk = client.get("/dashboard/static/prewarm.html").text
    assert "?v=" not in disk  # 磁盘文件保持纯静态契约,版本由服务端注入


# ======================================================================
# summary:GET /api/prewarm/summary
# ======================================================================

def test_summary_200_passthrough(client, monkeypatch):
    from app import dashboard_api
    seen = {}
    payload = {"business_date": BUSINESS_DATE, "generated_at": "t",
               "available": {"prewarm_store": True, "shared_cache": True,
                             "state_db": True},
               "degraded": False, "degraded_reasons": [], "kpi": {"candidate_count": 3}}

    def fake(business_date):
        seen["business_date"] = business_date
        return dict(payload)

    monkeypatch.setattr(dashboard_api, "build_prewarm_dashboard_summary", fake)
    r = client.get(f"/dashboard/api/prewarm/summary?date={BUSINESS_DATE}")
    assert r.status_code == 200
    assert seen["business_date"] == BUSINESS_DATE
    assert r.json() == payload
    assert r.headers["Cache-Control"] == "no-store"


def test_summary_degraded_payload_is_normal_200(client, monkeypatch):
    from app import dashboard_api
    payload = {"business_date": BUSINESS_DATE, "generated_at": "t",
               "available": {"prewarm_store": False, "shared_cache": False,
                             "state_db": False},
               "degraded": True, "degraded_reasons": ["prewarm_db_missing"],
               "kpi": {}}
    monkeypatch.setattr(
        dashboard_api, "build_prewarm_dashboard_summary", lambda bd: dict(payload))
    r = client.get(f"/dashboard/api/prewarm/summary?date={BUSINESS_DATE}")
    assert r.status_code == 200
    assert r.json()["degraded"] is True


def test_summary_unknown_param_400(client):
    r = client.get("/dashboard/api/prewarm/summary?foo=1")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "UNKNOWN_QUERY_PARAM"


def test_summary_bad_date_400(client):
    for bad in ("2026-02-30", "2026/08/26", "not-a-date"):
        r = client.get(f"/dashboard/api/prewarm/summary?date={bad}")
        assert r.status_code == 400, bad
        assert r.json()["detail"]["error_code"] == "BAD_BUSINESS_DATE"


def test_summary_unexpected_exception_503(client, monkeypatch):
    from app import dashboard_api

    def boom(business_date):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(dashboard_api, "build_prewarm_dashboard_summary", boom)
    r = client.get(f"/dashboard/api/prewarm/summary?date={BUSINESS_DATE}")
    assert r.status_code == 503
    assert r.json()["detail"]["error_code"] == "PREWARM_SUMMARY_READ_FAILED"


# ======================================================================
# links:GET /api/prewarm/links
# ======================================================================

def test_links_200_filters_passthrough(client, monkeypatch):
    from app import dashboard_api
    seen = {}
    payload = {"links": [], "pagination": {"page": 3, "page_size": 25,
                                           "total": 0, "total_pages": 0},
               "filters_applied": {}, "generated_at": "t"}

    def fake(business_date, *, page=1, page_size=50, filters=None):
        seen.update({"business_date": business_date, "page": page,
                     "page_size": page_size, "filters": dict(filters or {})})
        return dict(payload)

    monkeypatch.setattr(dashboard_api, "list_prewarm_links", fake)
    r = client.get(
        f"/dashboard/api/prewarm/links?date={BUSINESS_DATE}"
        "&page=3&page_size=25&mall=jd&unverified_sku=1")
    assert r.status_code == 200
    assert seen == {"business_date": BUSINESS_DATE, "page": 3, "page_size": 25,
                    "filters": {"mall": "jd", "unverified_sku": True}}
    assert r.json() == payload
    assert r.headers["Cache-Control"] == "no-store"


def test_links_bool_flag_true_word_and_string_filters(client, monkeypatch):
    from app import dashboard_api
    seen = {}
    monkeypatch.setattr(
        dashboard_api, "list_prewarm_links",
        lambda bd, *, page=1, page_size=50, filters=None:
            seen.update({"business_date": bd, "page": page,
                         "page_size": page_size, "filters": dict(filters or {})})
            or {"links": []})
    r = client.get(
        f"/dashboard/api/prewarm/links?date={BUSINESS_DATE}"
        "&failure=TRUE&missing_title=1&status=CACHE_HIT&source=staples-local")
    assert r.status_code == 200
    assert seen["filters"] == {"failure": True, "missing_title": True,
                               "status": "CACHE_HIT", "source": "staples-local"}


def test_links_defaults_when_no_params(client, monkeypatch):
    from app import dashboard_api
    seen = {}

    def fake(business_date, *, page=1, page_size=50, filters=None):
        seen.update({"business_date": business_date, "page": page,
                     "page_size": page_size, "filters": dict(filters or {})})
        return {"links": []}

    monkeypatch.setattr(dashboard_api, "list_prewarm_links", fake)
    monkeypatch.setattr(dashboard_api, "_today", lambda: BUSINESS_DATE)
    r = client.get("/dashboard/api/prewarm/links")
    assert r.status_code == 200
    assert seen == {"business_date": BUSINESS_DATE, "page": 1, "page_size": 50,
                    "filters": {}}


def test_links_param_guards_400(client):
    cases = [
        ("page=0", "BAD_PAGE_PARAM"),
        ("page=-2", "BAD_PAGE_PARAM"),
        ("page=abc", "BAD_PAGE_PARAM"),
        ("page_size=0", "BAD_PAGE_SIZE_PARAM"),
        ("page_size=201", "BAD_PAGE_SIZE_PARAM"),
        ("page_size=abc", "BAD_PAGE_SIZE_PARAM"),
        ("missing_title=maybe", "BAD_FILTER_PARAM"),
        ("unverified_sku=0", "BAD_FILTER_PARAM"),
        ("unresolved_sku=false", "BAD_FILTER_PARAM"),
        ("failure=yes", "BAD_FILTER_PARAM"),
        ("cache_miss=", "BAD_FILTER_PARAM"),
        ("foo=1", "UNKNOWN_QUERY_PARAM"),
    ]
    for query, expected_code in cases:
        r = client.get(f"/dashboard/api/prewarm/links?{query}")
        assert r.status_code == 400, query
        assert r.json()["detail"]["error_code"] == expected_code, query


def test_links_bad_date_400(client):
    r = client.get("/dashboard/api/prewarm/links?date=2026-02-30")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "BAD_BUSINESS_DATE"


def test_links_unexpected_exception_503(client, monkeypatch):
    from app import dashboard_api

    def boom(business_date, *, page=1, page_size=50, filters=None):
        raise RuntimeError("locked")

    monkeypatch.setattr(dashboard_api, "list_prewarm_links", boom)
    r = client.get("/dashboard/api/prewarm/links")
    assert r.status_code == 503
    assert r.json()["detail"]["error_code"] == "PREWARM_LINKS_READ_FAILED"


# ======================================================================
# detail:GET /api/prewarm/links/{candidate_id}
# ======================================================================

def test_detail_200_passthrough(client, monkeypatch):
    from app import dashboard_api
    seen = {}
    payload = {"candidate_id": 42, "original_url": STAPLES_URL,
               "history": [], "remote_job": None, "staples_job": None}

    def fake(business_date, candidate_id):
        seen.update({"business_date": business_date, "candidate_id": candidate_id})
        return dict(payload)

    monkeypatch.setattr(dashboard_api, "get_prewarm_link_detail", fake)
    r = client.get(f"/dashboard/api/prewarm/links/42?date={BUSINESS_DATE}")
    assert r.status_code == 200
    assert seen == {"business_date": BUSINESS_DATE, "candidate_id": 42}
    assert r.json() == payload
    assert r.headers["Cache-Control"] == "no-store"


def test_detail_none_404(client, monkeypatch):
    from app import dashboard_api
    monkeypatch.setattr(dashboard_api, "get_prewarm_link_detail", lambda bd, cid: None)
    r = client.get(f"/dashboard/api/prewarm/links/999?date={BUSINESS_DATE}")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "PREWARM_LINK_NOT_FOUND"


def test_detail_param_and_date_guards(client):
    r = client.get("/dashboard/api/prewarm/links/42?foo=1")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "UNKNOWN_QUERY_PARAM"
    r = client.get("/dashboard/api/prewarm/links/42?date=2026-13-01")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "BAD_BUSINESS_DATE"


def test_detail_unexpected_exception_503(client, monkeypatch):
    from app import dashboard_api

    def boom(business_date, candidate_id):
        raise RuntimeError("locked")

    monkeypatch.setattr(dashboard_api, "get_prewarm_link_detail", boom)
    r = client.get(f"/dashboard/api/prewarm/links/42?date={BUSINESS_DATE}")
    assert r.status_code == 503


# ======================================================================
# recrawl POST:守卫先行 + 错误映射
# ======================================================================

_RECRAWL_URL = "/dashboard/api/prewarm/links/42/recrawl"
_RECRAWL_BODY = {
    "business_date": BUSINESS_DATE,
    "expected_original_url": STAPLES_URL,
    "expected_status": "CACHE_HIT",
}


def _install_runner(monkeypatch, runner):
    from app import dashboard_api
    monkeypatch.setattr(
        dashboard_api, "get_manual_recrawl_runner", lambda: runner)


def test_recrawl_guard_precedes_any_runner_work(client, monkeypatch):
    """守卫必须先于 runner.start:四种 403 场景下 start 调用数恒为 0。"""
    runner = _FakeRunner()
    _install_runner(monkeypatch, runner)
    token = _operator_token(client)

    # 无令牌
    r = client.post(_RECRAWL_URL, json=_RECRAWL_BODY)
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "INVALID_ACTION_TOKEN"
    assert runner.calls == []
    # 错误令牌
    r = client.post(_RECRAWL_URL, json=_RECRAWL_BODY,
                    headers={"X-Dashboard-Action-Token": "wrong"})
    assert r.status_code == 403
    assert runner.calls == []
    # 外部 Origin + 正确令牌
    r = client.post(_RECRAWL_URL, json=_RECRAWL_BODY, headers={
        "X-Dashboard-Action-Token": token,
        "Origin": "https://evil.example.com"})
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "FOREIGN_ORIGIN"
    assert runner.calls == []
    # 非回环 Host + 正确令牌
    r = client.post(_RECRAWL_URL, json=_RECRAWL_BODY, headers={
        "X-Dashboard-Action-Token": token, "host": "evil.com"})
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "NON_LOCAL_HOST"
    assert runner.calls == []


def test_recrawl_happy_202_with_poll_path(client, monkeypatch):
    runner = _FakeRunner(handle={"action_id": "act-9", "status": "RUNNING",
                                 "route": "staples-local", "mall": "stbchina"})
    _install_runner(monkeypatch, runner)
    token = _operator_token(client)
    r = client.post(_RECRAWL_URL, json=_RECRAWL_BODY,
                    headers={"X-Dashboard-Action-Token": token})
    assert r.status_code == 202
    assert r.headers["Cache-Control"] == "no-store"
    body = r.json()
    assert body["action_id"] == "act-9"
    assert body["status"] == "RUNNING"
    assert body["route"] == "staples-local"
    assert body["mall"] == "stbchina"
    assert body["poll"] == "/dashboard/api/prewarm/recrawl-actions/act-9"
    assert runner.calls == [{
        "business_date": BUSINESS_DATE, "candidate_id": 42,
        "expected_original_url": STAPLES_URL, "expected_status": "CACHE_HIT",
    }]


def test_recrawl_default_date_and_optional_expectations(client, monkeypatch):
    from app import dashboard_api
    runner = _FakeRunner()
    _install_runner(monkeypatch, runner)
    monkeypatch.setattr(dashboard_api, "_today", lambda: BUSINESS_DATE)
    token = _operator_token(client)
    r = client.post(_RECRAWL_URL, json={},
                    headers={"X-Dashboard-Action-Token": token})
    assert r.status_code == 202
    assert runner.calls[0]["business_date"] == BUSINESS_DATE
    assert runner.calls[0]["expected_original_url"] is None
    assert runner.calls[0]["expected_status"] is None


def test_recrawl_body_guards(client, monkeypatch):
    runner = _FakeRunner()
    _install_runner(monkeypatch, runner)
    token = _operator_token(client)
    # body business_date 非法 → 400(且 runner 未被调用)
    r = client.post(_RECRAWL_URL, json={**_RECRAWL_BODY, "business_date": "2026-02-30"},
                    headers={"X-Dashboard-Action-Token": token})
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "BAD_BUSINESS_DATE"
    assert runner.calls == []
    # extra 字段 → pydantic forbid 422
    r = client.post(_RECRAWL_URL, json={**_RECRAWL_BODY, "unexpected": 1},
                    headers={"X-Dashboard-Action-Token": token})
    assert r.status_code == 422
    assert runner.calls == []


def test_recrawl_error_mapping(client, monkeypatch):
    cases = [
        ("CANDIDATE_NOT_FOUND", 404, None),
        ("STALE_PREWARM_LINK", 409, None),
        ("ACTION_IN_FLIGHT", 409, "prev-action"),
        ("RECAL_NOT_SUPPORTED", 400, None),
        ("STATE_DB_MISSING", 409, None),
    ]
    for code, expected_status, existing in cases:
        runner = _FakeRunner(error=ManualRecrawlError(
            code, "boom", existing_action_id=existing))
        _install_runner(monkeypatch, runner)
        client_ = TestClient(app)
        token = _operator_token(client_)
        r = client_.post(_RECRAWL_URL, json=_RECRAWL_BODY,
                         headers={"X-Dashboard-Action-Token": token})
        assert r.status_code == expected_status, code
        detail = r.json()["detail"]
        assert detail["error_code"] == code, code
        if code == "ACTION_IN_FLIGHT":
            assert detail["existing_action_id"] == "prev-action"


def test_recrawl_unknown_code_and_non_domain_error_500(client, monkeypatch):
    """未知 ManualRecrawlError code → 500 JSON 映射;非本域异常(RuntimeError)
    不被路由吞掉,按 FastAPI/uvicorn 语义交给服务器错误中间件(与既有
    action POST 的 _start_action_or_http_error 同约定,只有本域错误被映射)。"""
    from app import dashboard_api
    token = _operator_token(client)

    runner = _FakeRunner(error=ManualRecrawlError("SOMETHING_ELSE", "boom"))
    _install_runner(monkeypatch, runner)
    r = client.post(_RECRAWL_URL, json=_RECRAWL_BODY,
                    headers={"X-Dashboard-Action-Token": token})
    assert r.status_code == 500
    assert r.json()["detail"]["error_code"] == "SOMETHING_ELSE"

    runner2 = _FakeRunner(error=RuntimeError("not a ManualRecrawlError"))
    _install_runner(monkeypatch, runner2)
    raw_client = TestClient(app, raise_server_exceptions=False)
    r2 = raw_client.post(_RECRAWL_URL, json=_RECRAWL_BODY,
                         headers={"X-Dashboard-Action-Token": token})
    assert r2.status_code == 500


# ======================================================================
# poll:GET /api/prewarm/recrawl-actions/{action_id}
# ======================================================================

def test_poll_200_passthrough(client, monkeypatch):
    handle = {"action_id": "act-1", "candidate_id": 42, "route": "zhaoshang",
              "mall": "comix", "original_url": "https://qx.com/a.html",
              "status": "SUCCEEDED", "outcome": {"dispatch": {"outcome": "CREATED"}},
              "started_at": "t", "finished_at": "t2"}
    runner = _FakeRunner(get_result=handle)
    _install_runner(monkeypatch, runner)
    r = client.get("/dashboard/api/prewarm/recrawl-actions/act-1")
    assert r.status_code == 200
    assert r.json() == handle
    assert r.headers["Cache-Control"] == "no-store"


def test_poll_unknown_404(client, monkeypatch):
    runner = _FakeRunner(get_result=None)
    _install_runner(monkeypatch, runner)
    r = client.get("/dashboard/api/prewarm/recrawl-actions/missing")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "ACTION_NOT_FOUND"


def test_poll_rejects_any_query_param(client, monkeypatch):
    runner = _FakeRunner(get_result={"action_id": "act-1"})
    _install_runner(monkeypatch, runner)
    r = client.get("/dashboard/api/prewarm/recrawl-actions/act-1?date=1")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "UNKNOWN_QUERY_PARAM"


# ======================================================================
# GET 零写:真实三库 + 真实投影函数(仅 runtime_root 指向 tmp)
# ======================================================================

_REMOTE_JOBS_DDL = """
CREATE TABLE remote_jobs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    logical_key TEXT NOT NULL,
    task_name TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    mall_key TEXT,
    input_path TEXT,
    result_path TEXT,
    payload_json TEXT,
    state TEXT NOT NULL DEFAULT 'SUBMIT_INTENT',
    post_attempted INTEGER NOT NULL DEFAULT 0,
    remote_identity TEXT,
    export_path TEXT,
    result_json TEXT,
    error_text TEXT,
    harvest_attempts INTEGER NOT NULL DEFAULT 0,
    harvest_elapsed_seconds REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    recovery_evidence_json TEXT
)
"""


def _build_tmp_runtime(runtime_root):
    """最小真实三库:prewarm store + shared cache + <date>/state.sqlite3。"""
    db_path = runtime_root / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(db_path)
    shared = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    StaplesPrewarmStore(db_path)  # sidecar 表同文件
    row = shared.record_success(
        "comix", "https://qx.com/item-a.html", source="daily-internet",
        succeeded_at=SUCCEEDED_AT, sku_status="CONFIRMED",
        real_sku_id="SKU-A", title="Title A", price="12.5")
    upserted = prewarm.upsert_link("D1", "https://qx.com/item-a.html", mall="comix")
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_CACHE_HIT,
        match_level="EXACT", resolved_identity_id=int(row["identity_id"]))
    day_dir = runtime_root / BUSINESS_DATE
    day_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(day_dir / "state.sqlite3"))
    try:
        connection.execute(_REMOTE_JOBS_DDL)
        connection.execute(
            "INSERT INTO remote_jobs (id, run_id, logical_key, task_name,"
            " payload_hash, mall_key, state, created_at, updated_at)"
            " VALUES (1, 1, 'logical-1', 'cnnc_current_opt_mallx_00000000000001',"
            " 'hash', 'comix', 'RUNNING', ?, ?)", (SUCCEEDED_AT, SUCCEEDED_AT))
        connection.commit()
    finally:
        connection.close()
    return candidate_id


def _snapshot(runtime_root):
    """runtime_root 下全部文件的相对路径 → SHA256(零写断言边界)。"""
    snapshot = {}
    for path in sorted(runtime_root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(runtime_root))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return snapshot


def test_get_routes_zero_write_against_real_projection(client, monkeypatch, tmp_path):
    from app import dashboard_api
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    _build_tmp_runtime(runtime_root)
    before = _snapshot(runtime_root)
    assert before, "fixture 必须产出真实文件"

    def wrapped_summary(business_date):
        return real_build_prewarm_summary(
            business_date, runtime_root=str(runtime_root))

    def wrapped_list(business_date, *, page=1, page_size=50, filters=None):
        return real_list_prewarm_links(
            business_date, page=page, page_size=page_size,
            filters=filters, runtime_root=str(runtime_root))

    def wrapped_detail(business_date, candidate_id):
        return real_get_prewarm_detail(
            business_date, candidate_id, runtime_root=str(runtime_root))

    monkeypatch.setattr(
        dashboard_api, "build_prewarm_dashboard_summary", wrapped_summary)
    monkeypatch.setattr(dashboard_api, "list_prewarm_links", wrapped_list)
    monkeypatch.setattr(dashboard_api, "get_prewarm_link_detail", wrapped_detail)

    r_summary = client.get(f"/dashboard/api/prewarm/summary?date={BUSINESS_DATE}")
    r_links = client.get(
        f"/dashboard/api/prewarm/links?date={BUSINESS_DATE}&mall=comix")
    r_detail = client.get(f"/dashboard/api/prewarm/links/1?date={BUSINESS_DATE}")
    assert r_summary.status_code == 200
    assert r_links.status_code == 200
    assert r_detail.status_code == 200
    # 真实投影:summary KPI 非零、links 命中候选、detail 可见
    assert r_summary.json()["kpi"]["candidate_count"] == 1
    assert len(r_links.json()["links"]) == 1
    assert r_detail.json()["candidate_id"] == 1

    after = _snapshot(runtime_root)
    # 零写铁律:三个库文件字节级不变(无 checkpoint/无新行/无 schema 变化)。
    for name in ("customer_prewarm.sqlite3", "shared_crawl_cache.sqlite3",
                 f"{BUSINESS_DATE}\\state.sqlite3"):
        assert after[name] == before[name], name
    # mode=ro 读 WAL 库时 SQLite 会按读语义创建 -shm/-wal 读辅助文件
    # (不是模块写入;生产 runtime 中 writer 常驻本就存在)。除这两个库的
    # 读辅助文件外,零新建、零删除、其余文件字节级不变。
    allowed_new = {
        "customer_prewarm.sqlite3-shm", "customer_prewarm.sqlite3-wal",
        "shared_crawl_cache.sqlite3-shm", "shared_crawl_cache.sqlite3-wal",
    }
    new_files = set(after) - set(before)
    gone_files = set(before) - set(after)
    assert new_files <= allowed_new, new_files
    assert not gone_files, gone_files
