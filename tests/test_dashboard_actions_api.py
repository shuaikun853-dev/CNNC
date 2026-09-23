"""Operator-console API contract tests:guard、受控 POST、action 查询与进度集成。

全部 monkeypatch 注入(fake runner / fake durable job / fake progress),
不启动真实子进程、不触网、不写生产 runtime。
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.dashboard_actions import DashboardActionError
from app.services.dashboard_manual_ops import record_operator_action

BUSINESS_DATE = "2026-08-28"
IDENTITY = "JD|8899|1024"


@pytest.fixture()
def client():
    return TestClient(app)


def _token(client) -> str:
    r = client.get("/dashboard/api/operator-context")
    assert r.status_code == 200
    body = r.json()
    assert body["actions_enabled"] is True
    assert body["business_date"]
    assert r.headers["Cache-Control"] == "no-store"
    return body["action_token"]


def _auth(client, **extra) -> dict:
    return {"X-Dashboard-Action-Token": _token(client), **extra}


class _FakeRunner:
    def __init__(self):
        self.starts: list[dict] = []
        self.in_flight = False
        self.handle = SimpleNamespace(
            action_id="act-1", action_type="closeout", target=f"{BUSINESS_DATE}:-",
            business_date=BUSINESS_DATE, started_at="2026-08-28T10:00:00+00:00",
            status="RUNNING", outcome=None, pid=111)
        self.get_result = None
        self.durable_result = None

    def start(self, **kwargs):
        self.starts.append(kwargs)
        if self.in_flight:
            raise DashboardActionError(
                "action already in flight", code="ACTION_IN_FLIGHT",
                existing_action_id="prior-act")
        return self.handle

    def get(self, action_id):
        return self.get_result

    def list_recent(self, limit=20):
        return [self.handle] if self.get_result is not None else []

    def durable_status(self, action_id, run_dir):
        return self.durable_result


def _job_row(updated_at="2026-08-28T09:00:00", identity=IDENTITY):
    return {
        "id": 7, "task_name": "cnnc_current_opt_JD_0001",
        "state": "RUNNING", "remote_identity": identity,
        "updated_at": updated_at, "created_at": "2026-08-28T08:00:00",
    }


# ---- 1. GET 层只读语义保持 ----

def test_summary_still_rejects_writes(client):
    for method in ("post", "put", "delete", "patch"):
        r = getattr(client, method)("/dashboard/api/summary")
        assert r.status_code == 405, method


# ---- 2/3. guard:token / Origin / Host ----

def test_post_without_or_with_wrong_token_403(client):
    r = client.post("/dashboard/api/actions/closeout", json={})
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "INVALID_ACTION_TOKEN"
    r = client.post("/dashboard/api/actions/closeout", json={},
                    headers={"X-Dashboard-Action-Token": "wrong"})
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "INVALID_ACTION_TOKEN"


def test_post_with_foreign_origin_403(client):
    r = client.post("/dashboard/api/actions/closeout", json={},
                    headers=_auth(client, Origin="http://evil.com"))
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "FOREIGN_ORIGIN"


def test_post_with_foreign_host_403(client):
    r = client.post("/dashboard/api/actions/closeout", json={},
                    headers=_auth(client, Host="evil.com"))
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "NON_LOCAL_HOST"


def test_post_with_same_origin_passes_guard(client, monkeypatch):
    fake = _FakeRunner()
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: fake)
    r = client.post("/dashboard/api/actions/closeout", json={"business_date": BUSINESS_DATE},
                    headers=_auth(client, Origin="http://testserver"))
    assert r.status_code == 202, r.text


# ---- 4. closeout ----

def test_closeout_accepted_and_in_flight(client, monkeypatch):
    fake = _FakeRunner()
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: fake)
    headers = _auth(client)
    r = client.post("/dashboard/api/actions/closeout",
                    json={"business_date": BUSINESS_DATE}, headers=headers)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["action_id"] == "act-1"
    assert body["status"] == "RUNNING"
    assert body["poll"] == "/dashboard/api/actions/act-1"
    assert fake.starts[0]["action_type"] == "closeout"
    assert fake.starts[0]["business_date"] == BUSINESS_DATE

    fake.in_flight = True
    r = client.post("/dashboard/api/actions/closeout",
                    json={"business_date": BUSINESS_DATE}, headers=headers)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error_code"] == "ACTION_IN_FLIGHT"
    assert detail["existing_action_id"] == "prior-act"


def test_closeout_bad_business_date_400(client, monkeypatch):
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: _FakeRunner())
    r = client.post("/dashboard/api/actions/closeout",
                    json={"business_date": "2026-02-31"}, headers=_auth(client))
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "BAD_BUSINESS_DATE"


def test_force_closeout_requires_confirm_and_fresh_bounded_count(client, monkeypatch):
    fake = _FakeRunner()
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: fake)
    monkeypatch.setattr("app.dashboard_api.collect_runtime_status", lambda **kwargs: {
        "final": {
            "frozen": True,
            "finalized": False,
            "missing_page_price_rows": 7,
            "force_closeout_limit": 30,
        }
    })
    headers = _auth(client)

    r = client.post("/dashboard/api/actions/force-closeout", json={
        "business_date": BUSINESS_DATE,
        "confirm": False,
        "expected_missing_page_price_rows": 7,
    }, headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "CONFIRM_REQUIRED"

    r = client.post("/dashboard/api/actions/force-closeout", json={
        "business_date": BUSINESS_DATE,
        "confirm": True,
        "expected_missing_page_price_rows": 6,
    }, headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "STALE_MISSING_PRICE_COUNT"

    r = client.post("/dashboard/api/actions/force-closeout", json={
        "business_date": BUSINESS_DATE,
        "confirm": True,
        "expected_missing_page_price_rows": 7,
    }, headers=headers)
    assert r.status_code == 202, r.text
    assert fake.starts[-1]["action_type"] == "force-closeout"
    assert fake.starts[-1]["max_incomplete"] == 30
    assert fake.starts[-1]["expected_missing_page_price_rows"] == 7


def test_force_closeout_rejects_more_than_thirty_missing_rows(client, monkeypatch):
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: _FakeRunner())
    monkeypatch.setattr("app.dashboard_api.collect_runtime_status", lambda **kwargs: {
        "final": {
            "frozen": True,
            "finalized": False,
            "missing_page_price_rows": 31,
            "force_closeout_limit": 30,
        }
    })
    r = client.post("/dashboard/api/actions/force-closeout", json={
        "business_date": BUSINESS_DATE,
        "confirm": True,
        "expected_missing_page_price_rows": 31,
    }, headers=_auth(client))
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "MISSING_PRICE_LIMIT_EXCEEDED"


# ---- 5. re-export ----

def test_reexport_branches(client, monkeypatch):
    fake = _FakeRunner()
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: fake)
    monkeypatch.setattr("app.dashboard_api._load_remote_job", lambda jid, bd: None)
    headers = _auth(client)
    r = client.post("/dashboard/api/jobs/7/re-export",
                    json={"business_date": BUSINESS_DATE}, headers=headers)
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "JOB_NOT_FOUND"

    monkeypatch.setattr("app.dashboard_api._load_remote_job",
                        lambda jid, bd: _job_row())
    r = client.post("/dashboard/api/jobs/7/re-export",
                    json={"business_date": BUSINESS_DATE,
                          "expected_updated_at": "2026-08-28T08:00:00"}, headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "STALE_ACTION"

    r = client.post("/dashboard/api/jobs/7/re-export",
                    json={"business_date": BUSINESS_DATE,
                          "expected_updated_at": "2026-08-28T09:00:00",
                          "timeout_seconds": 120.0}, headers=headers)
    assert r.status_code == 202, r.text
    started = fake.starts[-1]
    assert started["action_type"] == "re-export"
    assert started["remote_job_id"] == 7
    # identity 只来自 durable state,绝不来自请求体
    assert started["remote_identity"] == IDENTITY
    assert started["timeout_seconds"] == 120.0


def test_reexport_rejects_body_identity_fields(client, monkeypatch):
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: _FakeRunner())
    r = client.post("/dashboard/api/jobs/7/re-export",
                    json={"business_date": BUSINESS_DATE, "remote_identity": "EVIL|1|1"},
                    headers=_auth(client))
    assert r.status_code == 422  # extra="forbid":严格 schema


def test_reexport_job_without_identity_409(client, monkeypatch):
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: _FakeRunner())
    monkeypatch.setattr("app.dashboard_api._load_remote_job",
                        lambda jid, bd: _job_row(identity=""))
    r = client.post("/dashboard/api/jobs/7/re-export",
                    json={"business_date": BUSINESS_DATE}, headers=_auth(client))
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "INVALID_REMOTE_STATE"


# ---- 6. rebuild ----

def test_rebuild_requires_confirm_then_accepts(client, monkeypatch):
    fake = _FakeRunner()
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: fake)
    monkeypatch.setattr("app.dashboard_api._load_remote_job",
                        lambda jid, bd: _job_row())
    headers = _auth(client)
    r = client.post("/dashboard/api/jobs/7/rebuild",
                    json={"business_date": BUSINESS_DATE}, headers=headers)
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "REBUILD_CONFIRM_REQUIRED"

    r = client.post("/dashboard/api/jobs/7/rebuild",
                    json={"business_date": BUSINESS_DATE, "confirm": True}, headers=headers)
    assert r.status_code == 202, r.text
    assert fake.starts[-1]["action_type"] == "rebuild"
    assert fake.starts[-1]["remote_identity"] == IDENTITY


# ---- 7. refresh-progress ----

def test_refresh_progress_passthrough_and_rate_limit(client, monkeypatch):
    monkeypatch.setattr("app.dashboard_api._load_remote_job",
                        lambda jid, bd: _job_row())
    captured: list[tuple] = []

    def fake_refresh(state_path, job, **kwargs):
        captured.append((state_path, job))
        return {"job_id": 7, "task_name": job["task_name"], "available": True,
                "classification": "ACTIVE", "progress": {"total": 10, "finished": 4},
                "observed_at": "t", "stale": False, "conflict": False,
                "error_code": None, "rate_limited": False}

    monkeypatch.setattr("app.dashboard_api.refresh_job_progress", fake_refresh)
    r = client.post("/dashboard/api/jobs/7/refresh-progress",
                    json={"business_date": BUSINESS_DATE}, headers=_auth(client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == 7 and body["task_name"] == _job_row()["task_name"]
    assert body["available"] is True and body["rate_limited"] is False
    assert captured[0][1]["remote_identity"] == IDENTITY

    def fake_rate_limited(state_path, job, **kwargs):
        return {"job_id": 7, "task_name": "n", "available": True,
                "classification": "ACTIVE", "progress": None, "observed_at": "t",
                "stale": False, "conflict": False, "error_code": None,
                "rate_limited": True}

    monkeypatch.setattr("app.dashboard_api.refresh_job_progress", fake_rate_limited)
    r = client.post("/dashboard/api/jobs/7/refresh-progress",
                    json={"business_date": BUSINESS_DATE}, headers=_auth(client))
    assert r.status_code == 200
    assert r.json()["rate_limited"] is True


def test_refresh_progress_bad_identity_409(client, monkeypatch):
    monkeypatch.setattr("app.dashboard_api._load_remote_job",
                        lambda jid, bd: _job_row(identity="bad-identity"))
    monkeypatch.setattr("app.dashboard_api.refresh_job_progress",
                        lambda *a, **k: pytest.fail("must not refresh a broken identity"))
    r = client.post("/dashboard/api/jobs/7/refresh-progress",
                    json={"business_date": BUSINESS_DATE}, headers=_auth(client))
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "INVALID_REMOTE_STATE"


# ---- 8. action 状态端点 ----

def test_action_status_running_then_404(client, monkeypatch):
    fake = _FakeRunner()
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: fake)
    fake.get_result = fake.handle
    r = client.get("/dashboard/api/actions/act-1")
    assert r.status_code == 200
    body = r.json()
    assert body["action_id"] == "act-1" and body["status"] == "RUNNING"
    assert body["action_type"] == "closeout"

    fake.get_result = None  # 内存无 → durable 也无 → 404
    r = client.get("/dashboard/api/actions/act-1")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "ACTION_NOT_FOUND"


def test_action_status_falls_back_to_durable(client, monkeypatch, tmp_path):
    fake = _FakeRunner()
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: fake)
    fake.get_result = None
    fake.durable_result = {"action_id": "act-9", "action_type": "closeout",
                           "status": "SUCCEEDED", "outcome": {"result_code": "FINALIZED"},
                           "started_at": "2026-08-28T10:00:00+00:00"}
    r = client.get("/dashboard/api/actions/act-9")
    assert r.status_code == 200
    assert r.json()["status"] == "SUCCEEDED"


# ---- 11. actions 列表(今日 run_dir 的 durable 记录) ----

def test_actions_list_reports_running_and_recent(client, monkeypatch, tmp_path):
    fake = _FakeRunner()
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: fake)
    monkeypatch.setattr("app.dashboard_api.runtime_paths",
                        lambda bd, root=None: SimpleNamespace(run_dir=tmp_path))
    record_operator_action(tmp_path, action_type="closeout", action_id="a1",
                           payload={"event": "started", "target": "x:-"})
    fake.get_result = fake.handle  # 让 list_recent 产出 RUNNING 摘要
    r = client.get("/dashboard/api/actions")
    assert r.status_code == 200
    body = r.json()
    assert body["running"][0]["action_id"] == "act-1"
    assert body["recent"][0]["action_id"] == "a1"
    assert body["recent"][0]["action_type"] == "closeout"


def test_actions_list_uses_requested_business_date(client, monkeypatch, tmp_path):
    fake = _FakeRunner()
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: fake)
    seen = []
    monkeypatch.setattr(
        "app.dashboard_api.runtime_paths",
        lambda bd, root=None: seen.append(bd) or SimpleNamespace(run_dir=tmp_path),
    )
    r = client.get(f"/dashboard/api/actions?date={BUSINESS_DATE}")
    assert r.status_code == 200
    assert r.json()["business_date"] == BUSINESS_DATE
    assert seen == [BUSINESS_DATE]


def test_actions_list_degrades_when_read_fails(client, monkeypatch, tmp_path):
    monkeypatch.setattr("app.dashboard_api.get_action_runner", lambda: _FakeRunner())
    monkeypatch.setattr("app.dashboard_api.runtime_paths",
                        lambda bd, root=None: SimpleNamespace(run_dir=tmp_path))

    def boom(*a, **k):
        raise OSError("read failed")

    monkeypatch.setattr("app.dashboard_api.read_operator_actions", boom)
    # Review Fix Round 2:顶层新增 additive business_date(服务端权威日期,固定以便精确断言)
    monkeypatch.setattr("app.dashboard_api._today", lambda: "2026-09-02")
    r = client.get("/dashboard/api/actions")
    assert r.status_code == 200
    assert r.json() == {"running": [], "recent": [], "business_date": "2026-09-02"}


# ---- 9. summary 进度集成(投影层) ----

def _fake_runtime(monkeypatch, tmp_path, jobs):
    from app.services import dashboard_projection as dp
    state = tmp_path / "state.sqlite3"
    state.write_bytes(b"")
    monkeypatch.setattr(dp, "runtime_paths", lambda bd, root=None: SimpleNamespace(
        state_path=state, run_dir=tmp_path))
    monkeypatch.setattr(dp, "project_remote_jobs", lambda sp, bd: [dict(j) for j in jobs])
    monkeypatch.setattr(
        dp, "_load_remote_jobs_for_progress", lambda sp, bd: [dict(j) for j in jobs]
    )
    monkeypatch.setattr(dp, "collect_runtime_status", lambda **kw: {"state": "MISSING"})
    monkeypatch.setattr(dp, "project_recent_days", lambda root, n=14, **kw: [])
    monkeypatch.setattr(dp, "project_scheduler_status", lambda: {"available": False})


_JOB = {"id": 7, "mall_key": "JD", "task_name": "cnnc_current_opt_JD_0001",
        "state": "RUNNING", "remote_identity": IDENTITY, "pill": "run",
        "recovering": False, "recovery_attempts": 0, "recovery_exhausted_valid": None,
        "superseded_by": None}


def test_summary_live_progress_passthrough(monkeypatch, tmp_path):
    from app.services.dashboard_projection import build_dashboard_summary
    _fake_runtime(monkeypatch, tmp_path, [_JOB])
    live = {"available": True, "classification": "ACTIVE",
            "progress": {"total": 10, "finished": 4}, "observed_at": "t",
            "stale": False, "conflict": False, "error_code": None}
    payload = build_dashboard_summary(BUSINESS_DATE, tmp_path,
                                      progress_service=SimpleNamespace(snapshot=lambda jobs: {"7": live}))
    assert payload["jobs"][0]["live_progress"] == live



def test_summary_live_progress_reconstructs_from_durable_job_without_leaking_internal_fields(monkeypatch, tmp_path):
    from app.services import dashboard_projection as dp
    from app.services.daily_audit_store import DailyAuditStore
    from app.services.dashboard_manual_ops import reconstruct_prepared_job
    from app.services.zhaoshang_progress_probe import ZhaoshangProgressService

    state = tmp_path / "state.sqlite3"
    DailyAuditStore(state)
    batch_state = tmp_path / "BATCH_STATE.md"
    batch_state.write_text("# test batch\n", encoding="utf-8")
    input_path = tmp_path / "input.xlsx"
    result_path = tmp_path / "result.xlsx"
    payload = {
        "batch_state": str(batch_state),
        "requests": [{
            "mall_key": "zjmi",
            "mall_source_id": "40",
            "mall_name": "浙江物产商城",
            "sku_id": "A100586863",
            "url": "https://www.zjmi-mall.com/pages/goods/goods-detail/index?id=A100586863",
            "backend_version": "legacy-v1",
        }],
    }
    now = "2026-09-20T03:03:42+00:00"
    with sqlite3.connect(state) as connection:
        run_id = connection.execute(
            """INSERT INTO daily_runs(
                   business_date, tenant_id, pool_id, state, metrics_json, created_at, updated_at
               ) VALUES (?, ?, ?, 'OPEN', '{}', ?, ?)""",
            ("2026-09-20", 188605, 222, now, now),
        ).lastrowid
        connection.execute(
            """INSERT INTO remote_jobs(
                   run_id, logical_key, task_name, payload_hash, mall_key,
                   input_path, result_path, payload_json, state, post_attempted,
                   remote_identity, harvest_attempts, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SUBMITTED', 1, ?, 1, ?, ?)""",
            (
                run_id,
                "logical-zjmi-1",
                "cnnc_current_opt_浙江物产商城_78045444081648",
                "payload-zjmi-1",
                "zjmi",
                str(input_path),
                str(result_path),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                "zjmi|1397571|RW-20260920110352",
                now,
                now,
            ),
        )

    monkeypatch.setattr(
        dp,
        "runtime_paths",
        lambda bd, root=None: SimpleNamespace(state_path=state, run_dir=tmp_path),
    )
    monkeypatch.setattr(dp, "collect_runtime_status", lambda **kw: {"state": "MISSING"})
    monkeypatch.setattr(dp, "project_recent_days", lambda root, n=14, **kw: [])
    monkeypatch.setattr(dp, "project_scheduler_status", lambda: {"available": False})

    class _Adapter:
        def query_task_progress_for_job(self, prepared, task_id, task_number, timeout_seconds):
            assert prepared.mall_key == "zjmi"
            assert len(prepared.requests) == 1
            assert task_id == "1397571"
            assert task_number == "RW-20260920110352"
            return {
                "batch_number": prepared.task_name,
                "progress": {
                    "total": 1,
                    "pending": 0,
                    "running": 0,
                    "failed": 0,
                    "waiting_db": 0,
                    "finished": 1,
                },
                "rsp_desc": "",
            }

    service = ZhaoshangProgressService(
        adapter_factory=lambda: _Adapter(),
        prepared_resolver=lambda job: reconstruct_prepared_job(state, job),
        background_refresh=False,
    )
    payload_out = dp.build_dashboard_summary(
        "2026-09-20", tmp_path, progress_service=service
    )

    live = payload_out["jobs"][0]["live_progress"]
    assert live["available"] is True
    assert live["classification"] == "REMOTE_TERMINAL"
    assert live["progress"]["finished"] == 1
    assert "payload_json" not in payload_out["jobs"][0]
    assert "input_path" not in payload_out["jobs"][0]
    assert "result_path" not in payload_out["jobs"][0]


def test_summary_progress_disabled_default(monkeypatch, tmp_path):
    from app.services.dashboard_projection import build_dashboard_summary
    _fake_runtime(monkeypatch, tmp_path, [_JOB])
    payload = build_dashboard_summary(BUSINESS_DATE, tmp_path, progress_service=None)
    progress = payload["jobs"][0]["live_progress"]
    assert progress["available"] is False
    assert progress["error_code"] == "PROBE_DISABLED"
    assert progress["classification"] is None and progress["progress"] is None


def test_summary_survives_progress_service_failure(monkeypatch, tmp_path):
    from app.services.dashboard_projection import build_dashboard_summary
    _fake_runtime(monkeypatch, tmp_path, [_JOB])

    class _Boom:
        def snapshot(self, jobs):
            raise RuntimeError("probe exploded")

    payload = build_dashboard_summary(BUSINESS_DATE, tmp_path, progress_service=_Boom())
    assert payload["jobs"][0]["live_progress"]["error_code"] == "PROBE_DISABLED"


# ---- 10. publish 阶段卡只显示文件名 ----

def test_publish_stage_card_shows_basename_only():
    from app.services.dashboard_projection import compute_overall_status
    status = {
        "state": "OPEN", "remote_health": {},
        "counts": {"discovered": 10, "unique_items": 10, "new": 0, "changed": 0},
        "detail": {"pending": 0, "running": 0, "completed": 10, "terminal": 0},
        "model": {"pending": 0, "running": 0, "completed": 10, "terminal": 0},
        "crawl": {"pending": 0, "running": 0, "completed": 10, "no_result": 0,
                  "unplanned": 0, "unknown": 0, "manual": 0, "waiting_submit": 0,
                  "awaiting_priority": 0, "awaiting_remote": 0},
        "remote": {"intent": 0, "pending": 0, "running": 0, "exported": 0,
                   "completed": 10, "unknown": 0, "manual": 0, "awaiting_priority": 0,
                   "awaiting_harvest": 0, "recovery_exhausted": 0},
        "final": {"pending": 0, "computed_pending": 0, "frozen": True,
                  "finalized": True, "forced": False,
                  "output_path": r"E:\x\y\saas_audit_final_20260828.xlsx",
                  "output_exists": True, "manifest_recorded": True,
                  "blocker_summary": {}},
    }
    stages = compute_overall_status(status)["stages"]
    publish = next(s for s in stages if s["key"] == "publish")
    label, value = publish["sub"][0]
    assert label == "输出文件"
    assert value == "saas_audit_final_20260828.xlsx"
    assert "E:" not in value and "\\" not in value and "/" not in value


def test_publish_stage_card_missing_output():
    from app.services.dashboard_projection import compute_overall_status
    status = {
        "state": "OPEN", "remote_health": {},
        "counts": {"discovered": 10, "unique_items": 10, "new": 0, "changed": 0},
        "detail": {"pending": 0, "running": 0, "completed": 10, "terminal": 0},
        "model": {"pending": 0, "running": 0, "completed": 10, "terminal": 0},
        "crawl": {"pending": 0, "running": 0, "completed": 10, "no_result": 0,
                  "unplanned": 0, "unknown": 0, "manual": 0, "waiting_submit": 0,
                  "awaiting_priority": 0, "awaiting_remote": 0},
        "remote": {"intent": 0, "pending": 0, "running": 0, "exported": 0,
                   "completed": 10, "unknown": 0, "manual": 0, "awaiting_priority": 0,
                   "awaiting_harvest": 0, "recovery_exhausted": 0},
        "final": {"pending": 0, "computed_pending": 0, "frozen": False,
                  "finalized": False, "forced": False, "output_path": None,
                  "output_exists": False, "manifest_recorded": False,
                  "blocker_summary": {}},
    }
    publish = next(s for s in compute_overall_status(status)["stages"]
                   if s["key"] == "publish")
    assert publish["sub"][0][1] == "未生成"
