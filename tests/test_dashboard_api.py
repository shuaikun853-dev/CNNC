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
    etag = r.headers["ETag"]
    r2 = client.get("/dashboard/api/summary", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_no_write_endpoints(client):
    for method in ("post", "put", "delete", "patch"):
        r = getattr(client, method)("/dashboard/api/summary")
        assert r.status_code == 405
