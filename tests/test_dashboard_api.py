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
    assert "If-None-Match" in js                      # 304 条件请求头接线
    # 不可见暂停/回前台补刷不再用字符串存在性冒充行为测试:
    # 真实行为由 tests/js/run_dashboard_js_tests.js(node)驱动 createPollController 验证。
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


# ---- Autostart Reliability Fix(2026-08-27):长期可用性 settings ----

def test_autostart_register_uses_task_scheduler_cmdlets_with_full_settings():
    reg = (ROOT / "scripts" / "register_dashboard_autostart.ps1").read_text(encoding="utf-8")
    # 显式控制 settings 必须走 ScheduledTasks 模块,而不是 schtasks /Create
    assert "Register-ScheduledTask" in reg
    assert "New-ScheduledTaskAction" in reg and "New-ScheduledTaskTrigger" in reg
    assert "New-ScheduledTaskSettingsSet" in reg
    assert "schtasks /Create" not in reg
    # 可靠性 settings(schtasks 默认 PT72H 时限 + 电池双禁 + 无重启,全部显式纠正)
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in reg       # 无时限(PT0S)
    assert "-AllowStartIfOnBatteries" in reg
    assert "-DontStopIfGoingOnBatteries" in reg
    assert "-MultipleInstances IgnoreNew" in reg
    assert "-RestartCount 3" in reg                              # 异常退出自动拉起 3 次
    assert "-RestartInterval (New-TimeSpan -Minutes 1)" in reg


def test_autostart_register_preview_gate_precedes_any_write():
    reg = (ROOT / "scripts" / "register_dashboard_autostart.ps1").read_text(encoding="utf-8")
    gate = reg.find("if (-not $Apply)")
    write = reg.find("Register-ScheduledTask")
    assert gate != -1 and write != -1
    assert gate < write, "默认 Preview 必须先于任何 Scheduler 写操作 exit"


def test_autostart_register_preview_reports_full_target_config():
    reg = (ROOT / "scripts" / "register_dashboard_autostart.ps1").read_text(encoding="utf-8")
    for field in ("TaskName", "Executable", "Arguments", "WorkingDirectory", "Trigger",
                  "ExecutionTimeLimit", "AllowStartIfOnBatteries", "DontStopIfGoingOnBatteries",
                  "MultipleInstances", "RestartCount", "RestartInterval", "Apply"):
        assert f'"{field}"' in reg or f"'{field}'" in reg, f"Preview 必须打印 {field}"


def test_dashboard_runbook_exists():
    text = (ROOT / "docs" / "runbooks" / "STEP_1_DASHBOARD.md").read_text(encoding="utf-8")
    assert "只读" in text and "127.0.0.1" in text and "8765" in text
    assert "-Apply" in text


# ---- Independent Review Fix Round 1(2026-08-27):P1-5 / P2-1 / P2-2 ----

def test_rf_p1_5_first_layer_labels_are_chinese(client):
    html = client.get("/dashboard").text
    js = client.get("/dashboard/static/app.js").text
    for text in (html, js):
        assert "Business date" not in text, "第一层不得出现英文标签 Business date"
        assert "Scheduled task" not in text, "第一层不得出现英文标签 Scheduled task"
    assert "业务日期" in html and "计划任务" in html          # HTML 初始文本
    assert "业务日期" in js and "计划任务" in js              # JS renderMeta 动态文案


def test_rf_p2_1_live_text_tracks_last_successful_poll(client):
    js = client.get("/dashboard/static/app.js").text
    assert "最后同步" in js                                    # 实时角标基于成功轮询(200/304)


def test_rf_p2_2_poll_pause_behavior_via_node():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node 未安装,跳过前端轮控行为测试")
    script = ROOT / "tests" / "js" / "run_dashboard_js_tests.js"
    proc = subprocess.run([node, str(script)], capture_output=True, text=True,
                          timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL PASS" in proc.stdout
