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


def test_autostart_action_arguments_semantic(tmp_path):
    """Final Quote Fix(2026-08-27):Preview 的 Arguments 必须含真实双引号、无字面 \",
    且该构造可被 powershell.exe 命令行真实执行(无副作用 python 探针);
    不注册 Scheduler、不执行 -Apply、不占 8765、不碰生产 runtime。"""
    import json
    import shutil
    import subprocess

    if not shutil.which("powershell"):
        pytest.skip("powershell 不可用")

    def decode_child(b):
        # PowerShell 5.1 管道按控制台代码页输出(本机 GBK),ConvertTo-Json 不转义非 ASCII;
        # 依次尝试 utf-8 / gbk,最终容错替换,保证任何主机都可解析出 JSON。
        for enc in ("utf-8", "gbk"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode(errors="replace")

    reg_path = ROOT / "scripts" / "register_dashboard_autostart.ps1"
    reg = reg_path.read_text(encoding="utf-8")
    # WorkingDirectory 已由 New-ScheduledTaskAction 显式设置,动作载荷不得再重复 Set-Location
    assert "Set-Location" not in reg
    # Principal 显式化:当前用户 Interactive(本地 help 无默认值描述,不以未证语义注册)
    assert "New-ScheduledTaskPrincipal" in reg
    # Restart 语义按微软公开口径表述,不再断言未经实机验证的退出码细分
    assert "非零退出码" not in reg and "退出码 0" not in reg
    runbook = (ROOT / "docs" / "runbooks" / "STEP_1_DASHBOARD.md").read_text(encoding="utf-8")
    assert "判定为失败时" in runbook
    assert "干净退出" not in runbook            # 被撤回的"退出码 0 绝不重启"断言不得残留

    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(reg_path)],
        capture_output=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = decode_child(proc.stdout).splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == "{")
    end = next(i for i, l in enumerate(lines) if l.strip() == "}")
    preview = json.loads("\n".join(lines[start:end + 1]))
    args = preview["Arguments"]
    expected = ('-NoProfile -WindowStyle Hidden -Command '
                '"& \'E:\\Python314\\python.exe\' -m uvicorn app.main:app '
                '--host 127.0.0.1 --port 8765"')
    assert args == expected, args
    assert '"' in args                      # 真实双引号
    assert '\\"' not in args                # 不得含字面 反斜杠+引号
    assert preview["WorkingDirectory"] == str(ROOT)
    assert "Principal" in preview

    # 可执行性探针:与生产同构的 -Command "…" 载荷(无副作用 python 脚本),
    # 经 cmd 透传给 powershell.exe 完整命令行解析执行(等价任务启动形态)。
    probe_py = tmp_path / "dash_probe.py"
    probe_py.write_text("print('DASH_PROBE_OK')", encoding="ascii")
    cmdline = ('powershell.exe -NoProfile -WindowStyle Hidden -Command '
               '"& \'E:\\Python314\\python.exe\' \'%s\'"' % probe_py.as_posix())
    out = subprocess.run(cmdline, capture_output=True, timeout=120, shell=True)
    stdout_text = decode_child(out.stdout)
    assert out.returncode == 0, stdout_text + decode_child(out.stderr)
    assert "DASH_PROBE_OK" in stdout_text


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


# ---- Hotfix review fix(2026-08-27):Scheduler probe 故障隔离的 API 契约 ----

def test_rf_probe_fault_containment_api_stays_200(client, monkeypatch):
    """Scheduler probe 无论返回什么坏形状/坏值,只能降级 scheduler 块,
    /dashboard/api/summary 必须保持 200。测试边界:走真实 build_dashboard_summary
    (本 worktree 无业务日 runtime → MISSING 真实分支),仅 monkeypatch 探针本身,
    每轮重置 30 秒缓存;不 mock build_dashboard_summary 整体。"""
    import app.services.dashboard_projection as dp
    malformed = [None, [], ["unexpected"], "unexpected", 123, True,
                 {"last_result": {}}, {"last_result": float("inf")},
                 {"state": "Running", "last_result": 2147946720,
                  "last_run": "08/27/2026 18:25:25", "next_run": "08/27/2026 18:30:30"}]
    for bad in malformed:
        dp._scheduler_cache.update({"at": 0.0, "value": None})
        monkeypatch.setattr(dp, "_run_scheduler_probe", lambda bad=bad: bad)
        r = client.get("/dashboard/api/summary")
        assert r.status_code == 200, (bad, r.status_code)
        body = r.json()
        assert body["overall"]["word"]            # summary 主体正常产出
        assert "scheduler" in body                # scheduler 块结构仍在
        sched = body["scheduler"]
        assert isinstance(sched.get("available"), bool)
