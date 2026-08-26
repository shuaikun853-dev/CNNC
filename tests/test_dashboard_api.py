from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


def test_dashboard_page_served(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "中核 STEP_1" in r.text
    assert "DESIGN PROTOTYPE" not in r.text  # 生产页无原型横幅


def test_summary_400_bad_date(client):
    r = client.get("/dashboard/api/summary?date=2026/08/26")
    assert r.status_code == 400
    assert "YYYY-MM-DD" in r.json()["detail"]["message"]


def test_summary_503_on_read_failure(client, monkeypatch):
    import sqlite3
    from app import dashboard_api

    def boom(**kw):
        raise sqlite3.DatabaseError("locked")

    monkeypatch.setattr(dashboard_api, "build_dashboard_summary", boom)
    r = client.get("/dashboard/api/summary")
    assert r.status_code == 503
    assert "状态读取失败" in r.json()["detail"]["message"]


def test_summary_200_etag_and_304(client, monkeypatch):
    from app import dashboard_api
    payload = {"generated_at": "t", "fingerprint": "x" * 64, "overall": {"word": "正在准备数据"}}
    monkeypatch.setattr(dashboard_api, "build_dashboard_summary", lambda **kw: dict(payload))
    r = client.get("/dashboard/api/summary")
    assert r.status_code == 200 and r.json()["overall"]["word"] == "正在准备数据"
    assert r.headers["Cache-Control"] == "no-cache"
    etag = r.headers["ETag"]
    r2 = client.get("/dashboard/api/summary", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.headers["Cache-Control"] == "no-cache"


def test_no_write_endpoints(client):
    for method in ("post", "put", "delete", "patch"):
        r = getattr(client, method)("/dashboard/api/summary")
        assert r.status_code == 405


def test_static_assets_served(client):
    for name in ("index.html", "app.css", "app.js"):
        r = client.get(f"/dashboard/static/{name}")
        assert r.status_code == 200, name
    css = client.get("/dashboard/static/app.css").text
    js = client.get("/dashboard/static/app.js").text
    html = client.get("/dashboard").text
    assert "--ok" in css and "--err" in css          # 五色令牌
    assert "visibilitychange" in js                   # 不可见暂停
    assert "If-None-Match" in js                      # 304 支持
    assert "cdn" not in css.lower() and "http://" not in css and "https://" not in css  # 零外链
    for hook in ("hero", "attn", "stages", "zs", "final", "more", "jobs-table", "drawer", "tech", "conn-lost", "live", "refresh", "jobs-toggle"):
        assert hook in html, hook


ROOT = Path(__file__).resolve().parents[1]


def test_autostart_scripts_exist_with_apply_gate():
    reg = (ROOT / "scripts" / "register_dashboard_autostart.ps1").read_text(encoding="utf-8")
    un = (ROOT / "scripts" / "unregister_dashboard_autostart.ps1").read_text(encoding="utf-8")
    for text in (reg, un):
        assert "CNNC-Dashboard-ReadOnly" in text
        assert "CNNC-PriceAudit-Incremental" not in text        # 不触碰现有审计任务(字面名禁止)
        assert "127.0.0.1" in text and "8765" in text
    assert "-Apply" in reg and "预览" in reg
    assert "-Apply" in un and "预览" in un


def test_dashboard_runbook_exists():
    text = (ROOT / "docs" / "runbooks" / "STEP_1_DASHBOARD.md").read_text(encoding="utf-8")
    assert "只读" in text and "127.0.0.1" in text and "8765" in text
    assert "-Apply" in text
