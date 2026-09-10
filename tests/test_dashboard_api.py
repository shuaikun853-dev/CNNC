from pathlib import Path
import json

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
    for hook in ("hero", "attn", "stages", "zs", "final", "more", "jobs-table", "drawer", "tech", "conn-lost", "live", "refresh", "jobs-toggle", "prod-identity", "pi-chips"):
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


# ---- 本机受控操作台 UI(2026-08-28):前端静态契约 ----

def test_operator_console_static_contract(client):
    """操作台前端契约:布局防御 CSS + HTML hook + 操作令牌头;二次确认不用
    window.confirm(浏览器原生 confirm),POST 渲染不依赖原生弹窗。"""
    css = client.get("/dashboard/static/app.css").text
    html = client.get("/dashboard").text
    js = client.get("/dashboard/static/app.js").text
    # 长路径/长标识折行防御(1100px 主容器与 860px 断点不溢出的 CSS 保证)
    assert "overflow-wrap:anywhere" in css
    assert "word-break" in css
    # 操作台关键 hook(既有 hook 由 test_static_assets_served 覆盖,不在此重复)
    for hook in ("closeout-btn", "final-output", "copy-path", "rebuild-confirm", "jobs-actions"):
        assert hook in html, hook
    # 受控写必须携带进程内操作令牌头
    assert "X-Dashboard-Action-Token" in js
    # 不得使用浏览器原生 confirm/alert 做二次确认/提示
    assert "window.confirm" not in js and "window.alert" not in js


# ---- Dashboard Auth/Timezone/Closeout 修复(2026-09-02):G1-G5 ----

def test_dashboard_index_cache_busting(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-cache"
    import re
    html = r.text
    assert re.search(r"/dashboard/static/app\.js\?v=\d+", html)
    assert re.search(r"/dashboard/static/app\.css\?v=\d+", html)
    assert html.count("?v=") == 2
    disk = (ROOT / "app" / "static" / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "?v=" not in disk
    for name in ("app.js", "app.css"):
        assert client.get(f"/dashboard/static/{name}").status_code == 200
    assert "cdn" not in html.lower()
    assert 'src="/dashboard/static/app.js?v=' in html
    assert 'href="/dashboard/static/app.css?v=' in html


def test_dashboard_index_version_tracks_mtime(monkeypatch):
    """真实行为断言:版本源变化 → 注入 URL 变化(monkeypatch _asset_version)。"""
    import re

    from app import dashboard_api

    versions = iter(["1000000000001", "2000000000002"])

    def fake_version(path):
        if path.name == "app.js":
            return next(versions)
        return str(path.stat().st_mtime_ns)

    monkeypatch.setattr(dashboard_api, "_asset_version", fake_version)
    html1 = dashboard_api._dashboard_index_html()
    html2 = dashboard_api._dashboard_index_html()
    v1 = re.search(r"/dashboard/static/app\.js\?v=(\d+)", html1).group(1)
    v2 = re.search(r"/dashboard/static/app\.js\?v=(\d+)", html2).group(1)
    assert v1 == "1000000000001" and v2 == "2000000000002" and v1 != v2


def test_actions_running_entries_carry_business_date(client, monkeypatch):
    """/api/actions 的 running 条目必须带 business_date(前端 closeoutRunning
    权威判定的首选匹配字段;ActionHandle 本就有该字段,纯 additive)。"""
    from app import dashboard_api

    class _Handle:
        action_id = "a-1"
        action_type = "closeout"
        status = "RUNNING"
        target = "2026-09-02:-"
        started_at = "2026-09-02T10:00:00+00:00"
        business_date = "2026-09-02"

    class _Runner:
        def list_recent(self, limit):
            return [_Handle()]

        def get(self, action_id):
            return None

        def durable_status(self, action_id, run_dir):
            return None

    monkeypatch.setattr(dashboard_api, "get_action_runner", lambda: _Runner())
    # R2 additive:顶层 business_date(服务端权威),前端 bootstrap 失败时的匹配兜底。
    monkeypatch.setattr(dashboard_api, "_today", lambda: "2026-09-02")
    r = client.get("/dashboard/api/actions")
    assert r.status_code == 200
    running = r.json()["running"]
    assert running, "构造的 RUNNING closeout 必须出现在 running 列表"
    assert running[0]["business_date"] == "2026-09-02"
    assert running[0]["target"] == "2026-09-02:-"
    assert running[0]["action_type"] == "closeout" and running[0]["status"] == "RUNNING"
    assert r.json()["business_date"] == "2026-09-02"


def _stages_status_fixture(*, frozen: bool = False, finalized: bool = False,
                           writer_command: str | None = None) -> dict:
    """_stages 所需的最小 status mapping(字段名对齐 incremental_audit_status
    的 _empty_status 真实形状,只保留 _stages 访问的键)。
    R2:writer_command=None → 锁未持有(active=False 且 command=None);
    非空字符串 → 锁被该命令持有(active=True, command=writer_command)。
    run_runtime_command 的 owner.command 实际取值:tick/finalize/drain-only/
    harvest-only/closeout。"""
    return {
        "counts": {"discovered": 0, "unique_items": 0, "new": 0, "changed": 0},
        "detail": {"pending": 0, "running": 0, "completed": 0, "terminal": 0},
        "model": {"pending": 0, "running": 0, "completed": 0, "terminal": 0},
        "crawl": {"pending": 0, "running": 0, "completed": 0, "no_result": 0,
                  "unplanned": 0, "planning_pending": 0, "unknown": 0, "manual": 0,
                  "waiting_submit": 0},
        "remote": {"intent": 0, "pending": 0, "running": 0, "exported": 0,
                   "completed": 0, "unknown": 0, "manual": 0,
                   "awaiting_priority": 0, "awaiting_harvest": 0,
                   "recovery_exhausted": 0, "superseded": 0},
        "remote_health": {},
        "final": {"frozen": frozen, "finalized": finalized, "forced": False,
                  "output_path": None},
        "production_writer": {"active": writer_command is not None,
                              "command": writer_command},
    }


def _closeout_stage(stages):
    return next(s for s in stages if s["key"] == "closeout")


def test_projection_closeout_writer_active_unfrozen_shows_running():
    """Bug3 核心:人工 closeout RUNNING(writer 锁真实持有)而 frozen=false
    不得再显示"○ 等待",必须显示"● 进行中/收口任务已启动,正在准备"。"""
    from app.services.dashboard_projection import _stages
    stage = _closeout_stage(_stages(
        _stages_status_fixture(writer_command="closeout", frozen=False, finalized=False), False))
    assert stage["state"] == "cur"
    assert stage["state_word"] == "● 进行中"
    assert stage["sub"] == [("数据冻结", "收口任务已启动，正在准备")]


def test_projection_closeout_writer_active_frozen():
    from app.services.dashboard_projection import _stages
    stage = _closeout_stage(_stages(
        _stages_status_fixture(writer_command="closeout", frozen=True, finalized=False), False))
    assert stage["state"] == "cur"
    assert stage["sub"] == [("数据冻结", "已冻结，正在收口")]


def test_projection_closeout_frozen_without_writer():
    from app.services.dashboard_projection import _stages
    stage = _closeout_stage(_stages(
        _stages_status_fixture(frozen=True, finalized=False), False))
    assert stage["state"] == "cur"
    assert stage["state_word"] == "● 进行中"
    assert stage["sub"] == [("数据冻结", "已冻结")]


def test_projection_closeout_all_false_waits():
    from app.services.dashboard_projection import _stages
    stage = _closeout_stage(_stages(
        _stages_status_fixture(frozen=False, finalized=False), False))
    assert stage["state"] == "wait"
    assert stage["state_word"] == "○ 等待"
    assert stage["sub"] == [("数据冻结", "未开始(等待 18:00)")]


def test_projection_closeout_finalized_done():
    from app.services.dashboard_projection import _stages
    stage = _closeout_stage(_stages(
        _stages_status_fixture(frozen=True, finalized=True), True))
    assert stage["state"] == "done"
    assert stage["state_word"] == "✓ 完成"
    assert stage["sub"] == [("数据冻结", "已完成")]


def test_projection_closeout_missing_writer_key_is_defensive():
    """旧 payload/外部构造缺 production_writer 键时按 False 处理,绝不 KeyError。"""
    from app.services.dashboard_projection import _stages
    status = _stages_status_fixture(frozen=False, finalized=False)
    status.pop("production_writer")
    stage = _closeout_stage(_stages(status, False))
    assert stage["state"] == "wait"


# ---- Review Fix Round 2(2026-09-02):P1-1 closeout writer 谓词回归 ----
# 单一权威谓词 _closeout_writer_active:仅 closeout/finalize 持锁算收口进行中;
# tick/harvest-only/drain-only 持锁、锁 active 但 command 缺失、active=False
# 一律保守判"等待"。

def test_rf2_r2_tick_writer_lock_still_waits():
    from app.services.dashboard_projection import _stages
    stage = _closeout_stage(_stages(
        _stages_status_fixture(writer_command="tick"), False))
    assert stage["state"] == "wait"
    assert stage["state_word"] == "○ 等待"
    assert stage["sub"] == [("数据冻结", "未开始(等待 18:00)")]


def test_rf2_r2_harvest_only_writer_lock_still_waits():
    from app.services.dashboard_projection import _stages
    stage = _closeout_stage(_stages(
        _stages_status_fixture(writer_command="harvest-only"), False))
    assert stage["state"] == "wait"
    assert stage["sub"] == [("数据冻结", "未开始(等待 18:00)")]


def test_rf2_r2_drain_only_writer_lock_still_waits():
    from app.services.dashboard_projection import _stages
    stage = _closeout_stage(_stages(
        _stages_status_fixture(writer_command="drain-only"), False))
    assert stage["state"] == "wait"
    assert stage["sub"] == [("数据冻结", "未开始(等待 18:00)")]


def test_rf2_r2_finalize_writer_counts_as_closeout_running():
    from app.services.dashboard_projection import _stages
    stage = _closeout_stage(_stages(
        _stages_status_fixture(writer_command="finalize"), False))
    assert stage["state"] == "cur"
    assert stage["state_word"] == "● 进行中"
    assert stage["sub"] == [("数据冻结", "收口任务已启动，正在准备")]


def test_rf2_r2_inactive_lock_with_closeout_command_waits():
    """active=False + command="closeout":锁未真实持有,不得判定收口进行中。"""
    from app.services.dashboard_projection import _stages
    status = _stages_status_fixture()
    status["production_writer"] = {"active": False, "command": "closeout"}
    stage = _closeout_stage(_stages(status, False))
    assert stage["state"] == "wait"
    assert stage["sub"] == [("数据冻结", "未开始(等待 18:00)")]


def test_rf2_r2_active_lock_without_command_key_waits():
    """锁 active 但 metadata 缺 command 键(如无 owner 的 force-finalize):
    "不能证明是收口"保守判等待。"""
    from app.services.dashboard_projection import _stages
    status = _stages_status_fixture()
    status["production_writer"] = {"active": True}
    stage = _closeout_stage(_stages(status, False))
    assert stage["state"] == "wait"


def test_fingerprint_reflects_production_writer_activity():
    """writer_active 参与 ETag:活动状态变化必须产出新指纹(无 304 陈旧)。"""
    from app.services.dashboard_projection import fingerprint_payload
    base = {"status": {"production_writer": {"active": False}}, "overall": {}}
    busy = {"status": {"production_writer": {"active": True}}, "overall": {}}
    assert fingerprint_payload(base) != fingerprint_payload(busy)


# ---- 客户价行级只读接口(2026-09-03,plan §8):GET 铁律与参数白名单 ----

def test_customer_price_param_guards(client):
    r = client.get("/dashboard/api/customer-price?tenant=1")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "UNKNOWN_QUERY_PARAM"
    r = client.get("/dashboard/api/customer-price?filter=;drop")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "BAD_FILTER_PARAM"
    r = client.get("/dashboard/api/customer-price?date=2026-02-30")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "BAD_BUSINESS_DATE"
    r = client.get("/dashboard/api/customer-price?date=2026/08/26")
    assert r.status_code == 400


def test_customer_price_write_methods_not_allowed(client):
    for method in ("post", "put", "delete", "patch"):
        r = getattr(client, method)("/dashboard/api/customer-price")
        assert r.status_code == 405


def test_customer_price_empty_200_when_no_state_file(client, monkeypatch, tmp_path):
    from app import dashboard_api
    monkeypatch.setattr(dashboard_api, "runtime_paths", lambda bd: type("P", (), {
        "state_path": tmp_path / "absent.sqlite3"})())
    r = client.get("/dashboard/api/customer-price?date=2026-08-11")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    body = r.json()
    assert body == {
        "business_date": "2026-08-11", "filter": "all", "total_required": 0,
        "counts": {"ready": 0, "pending": 0, "retrying": 0, "missing": 0,
                   "failed": 0, "invalid": 0, "absent_proven": 0},
        "rows": [], "truncated": 0, "limit": 0, "max_attempts": 3}


def test_customer_price_projects_readonly_payload(client, monkeypatch, tmp_path):
    from app import dashboard_api
    state = tmp_path / "state.sqlite3"
    state.write_bytes(b"sqlite")   # 只需存在;投影被 monkeypatch,不发真 SQL
    payload = {
        "business_date": "2026-08-11", "filter": "unacquired", "total_required": 2,
        "counts": {"ready": 0, "pending": 1, "retrying": 1, "missing": 0,
                   "failed": 0, "invalid": 0, "absent_proven": 0},
        "rows": [{"document_id": "v1", "state": "RETRYING", "attempts": 1,
                  "max_attempts": 3, "error_text": "customerPriceStatus=NOT_FOUND"}],
        "truncated": 0, "limit": 500, "max_attempts": 3}
    seen = {}

    def fake_project(state_path, business_date, *, filter="all"):
        seen["args"] = (str(state_path), business_date, filter)
        return payload

    monkeypatch.setattr(dashboard_api, "runtime_paths", lambda bd: type("P", (), {
        "state_path": state})())
    monkeypatch.setattr(dashboard_api, "project_customer_price", fake_project)
    r = client.get("/dashboard/api/customer-price?date=2026-08-11&filter=unacquired")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    assert r.json() == payload
    assert seen["args"] == (str(state), "2026-08-11", "unacquired")


def test_customer_price_frontend_hooks_and_no_independent_poll(client):
    """前端契约:客户价区块/抽屉/过滤按钮存在;明细仅在打开/切换过滤时拉取
    (fetchCpRows = 定义 + 打开触发 + 过滤触发,别无他处→无独立轮询链)。"""
    html = client.get("/dashboard").text
    js = client.get("/dashboard/static/app.js").text
    for hook in ("cp-card", "cp-counts", "cp-open", "cp-drawer", "cp-filters", "cp-table"):
        assert hook in html, hook
    assert "/dashboard/api/customer-price" in js
    assert "renderCustomerPrice" in js and "cpStatusCn" in js
    assert js.count("fetchCpRows") == 3


def test_production_writer_lock_blocks_second_process(tmp_path):
    """DashboardActionRunner._inflight 丢失(进程重启)后,底层 per-day
    production writer ProcessLock(OS 级 msvcrt/fcntl)仍是唯一单 writer fence。"""
    from app.services.daily_audit_store import LeaseUnavailable, ProcessLock
    lock_path = tmp_path / "production.lock"
    first = ProcessLock(lock_path).acquire(owner={"pid": 111})
    try:
        with pytest.raises(LeaseUnavailable):
            ProcessLock(lock_path).acquire(owner={"pid": 222})
    finally:
        first.release()
    # 释放后可重新获取
    second = ProcessLock(lock_path).acquire(owner={"pid": 333})
    second.release()


# ======================================================================
# CNNC 认证路由(2026-09-10):健康态不拒绝重新登录 + 手动恢复工件透传
# ======================================================================

def _fake_status_provider(monkeypatch):
    """status 路由在调用时才 import CnncAuthProvider;替换类对象即可隔离
    真实 data/ 读取,让 status 恒为 CNNC_AUTH_OK(健康)。"""
    from scripts import cnnc_auth_core as core

    class _FakeStatusProvider:
        def get_status(self):
            return {"status": "CNNC_AUTH_OK", "login_required": False}

    monkeypatch.setattr(core, "CnncAuthProvider", _FakeStatusProvider)


def test_cnnc_auth_login_healthy_state_starts_detached_recovery(client, monkeypatch):
    """认证健康(CNNC_AUTH_OK)时 POST /api/cnnc-auth/login 必须 202 started 且
    拉起恢复子进程(mock 掉):锁定"健康态不拒绝重新登录"。login 路由无健康门,
    只有 operator guard / 60s 节流 / detached 拉起——这是有意契约。"""
    from app import dashboard_api
    launched = []

    def fake_popen(args, **kwargs):
        launched.append(list(args))

    monkeypatch.setattr(dashboard_api.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(dashboard_api, "_cnnc_login_launched_at", None)  # 隔离节流单例
    r = client.post("/dashboard/api/cnnc-auth/login", headers={
        "X-Dashboard-Action-Token": dashboard_api._ACTION_TOKEN})
    assert r.status_code == 202
    assert r.json()["started"] is True
    assert len(launched) == 1  # 子进程确实被拉起(mock 断言)
    assert "--login-and-recover" in launched[0]


def test_cnnc_auth_login_requires_operator_token(client, monkeypatch):
    """回归:重登入口仍受 operator guard 保护(无令牌 403),健康态不绕过门禁。"""
    from app import dashboard_api
    monkeypatch.setattr(dashboard_api, "_cnnc_login_launched_at", None)
    r = client.post("/dashboard/api/cnnc-auth/login")
    assert r.status_code == 403


def test_cnnc_auth_status_merges_manual_recovery_artifact(client, monkeypatch, tmp_path):
    """status 路由合并结果工件:last_manual_recovery 原样透传工件内容;
    缺文件/坏 JSON 容错不加字段(只读、绝不影响主 payload)。"""
    from app import dashboard_api
    _fake_status_provider(monkeypatch)
    artifact = tmp_path / "cnnc_login_recovery_result.json"
    artifact.write_text(json.dumps({
        "completed_at": "2026-09-10T06:30:00+00:00",
        "outcome": "RECOVERY_OK",
        "curl_expected": 3,
        "curl_synced": 3,
        "curl_failed_files": [],
        "at_redacted": "AT-2026***",
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(dashboard_api, "_CNNC_RECOVERY_RESULT_PATH", artifact)

    r = client.get("/dashboard/api/cnnc-auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "CNNC_AUTH_OK"
    assert body["last_manual_recovery"]["outcome"] == "RECOVERY_OK"
    assert body["last_manual_recovery"]["curl_synced"] == 3
    assert body["last_manual_recovery"]["curl_failed_files"] == []

    # 工件缺失:响应正常且无该字段
    monkeypatch.setattr(
        dashboard_api, "_CNNC_RECOVERY_RESULT_PATH", tmp_path / "missing.json")
    r2 = client.get("/dashboard/api/cnnc-auth/status")
    assert r2.status_code == 200
    assert "last_manual_recovery" not in r2.json()


def test_cnnc_auth_status_bad_json_artifact_is_ignored(client, monkeypatch, tmp_path):
    from app import dashboard_api
    _fake_status_provider(monkeypatch)
    artifact = tmp_path / "bad.json"
    artifact.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(dashboard_api, "_CNNC_RECOVERY_RESULT_PATH", artifact)

    r = client.get("/dashboard/api/cnnc-auth/status")
    assert r.status_code == 200
    assert "last_manual_recovery" not in r.json()


# ======================================================================
# 跨进程 single-flight 锁 409（2026-09-10 Blocker 3）：真实 --login-and-recover
# 进程仍持 OS 锁时，登录端点必须 409 拒绝（优先于 60 秒节流），绝不叠加拉起
# 第二个恢复进程。端点在调用时才 import LOGIN_RECOVERY_LOCK_PATH，测试
# monkeypatch scripts.cnnc_auth_refresh 的模块属性即可注入 tmp 锁路径，
# 绝不触碰真实 data/。
# ======================================================================

def _inject_login_lock_path(monkeypatch, lock_path):
    from scripts import cnnc_auth_refresh
    monkeypatch.setattr(
        cnnc_auth_refresh, "LOGIN_RECOVERY_LOCK_PATH", lock_path)


def _fake_login_popen(monkeypatch):
    from app import dashboard_api
    launched = []

    def fake_popen(args, **kwargs):
        launched.append(list(args))

    monkeypatch.setattr(dashboard_api.subprocess, "Popen", fake_popen)
    return launched


def test_cnnc_auth_login_lock_held_returns_409_before_throttle(
        client, monkeypatch, tmp_path):
    """Case A：外部持有锁 + 节流时间戳设为 61 秒前（绕过节流）→ 409
    CNNC_LOGIN_ALREADY_RUNNING、零 Popen；证明锁检查优先于节流。"""
    import time

    from app import dashboard_api
    from app.services.daily_audit_store import ProcessLock

    lock_path = tmp_path / "login_recovery.lock"
    _inject_login_lock_path(monkeypatch, lock_path)
    holder = ProcessLock(lock_path).acquire(
        owner={"pid": 12345, "command": "login-and-recover"})
    try:
        monkeypatch.setattr(dashboard_api, "_cnnc_login_launched_at",
                            time.monotonic() - 61.0)  # 节流已过期
        launched = _fake_login_popen(monkeypatch)
        r = client.post("/dashboard/api/cnnc-auth/login", headers={
            "X-Dashboard-Action-Token": dashboard_api._ACTION_TOKEN})
    finally:
        holder.release()
    assert r.status_code == 409
    body = r.json()
    assert body["error_code"] == "CNNC_LOGIN_ALREADY_RUNNING"
    assert body["status_name"] == "CNNC_LOGIN_ALREADY_RUNNING"
    assert "重新登录流程已经在进行中" in body["error"]
    assert launched == []  # 绝不拉起第二个恢复进程


def test_cnnc_auth_login_lock_free_starts_detached_recovery(
        client, monkeypatch, tmp_path):
    """Case B：锁未被持有 → 202 started、Popen 恰好一次（既有契约不破）。"""
    from app import dashboard_api

    _inject_login_lock_path(monkeypatch, tmp_path / "login_recovery.lock")
    monkeypatch.setattr(dashboard_api, "_cnnc_login_launched_at", None)
    launched = _fake_login_popen(monkeypatch)
    r = client.post("/dashboard/api/cnnc-auth/login", headers={
        "X-Dashboard-Action-Token": dashboard_api._ACTION_TOKEN})
    assert r.status_code == 202
    assert r.json()["started"] is True
    assert len(launched) == 1
    assert "--login-and-recover" in launched[0]


def test_cnnc_auth_login_cross_process_lock_holder_blocks(
        client, monkeypatch, tmp_path):
    """Case C（真跨进程关键例）：独立子进程持有 OS 锁（msvcrt/fcntl 按句柄
    排他，非同进程重入）→ 409、零 Popen；结束安全终止子进程（进程退出锁
    自动释放，不留残留）。"""
    import subprocess
    import sys
    import time

    from app import dashboard_api
    from app.services.daily_audit_store import ProcessLock

    lock_path = tmp_path / "login_recovery.lock"
    _inject_login_lock_path(monkeypatch, lock_path)
    root = Path(__file__).resolve().parents[1]
    holder_src = tmp_path / "lock_holder.py"
    holder_src.write_text(
        "import sys, time\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from app.services.daily_audit_store import ProcessLock\n"
        "lock = ProcessLock(sys.argv[1]).acquire("
        "owner={'pid': 'child-holder', 'command': 'test'})\n"
        "print('HELD', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, str(holder_src), str(lock_path), str(root)],
        cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if ProcessLock.inspect(lock_path)["active"]:
                break
            if child.poll() is not None:
                raise AssertionError(
                    "lock holder 提前退出: "
                    + child.stderr.read().decode("utf-8", "replace"))
            time.sleep(0.05)
        assert ProcessLock.inspect(lock_path)["active"] is True, (
            "子进程未成功持有锁")
        monkeypatch.setattr(dashboard_api, "_cnnc_login_launched_at", None)
        launched = _fake_login_popen(monkeypatch)
        r = client.post("/dashboard/api/cnnc-auth/login", headers={
            "X-Dashboard-Action-Token": dashboard_api._ACTION_TOKEN})
        assert r.status_code == 409
        assert r.json()["error_code"] == "CNNC_LOGIN_ALREADY_RUNNING"
        assert launched == []
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
    # 子进程退出后 OS 锁自动释放（探活即可验证，无需子进程显式清理）
    assert ProcessLock.inspect(lock_path)["active"] is False


def test_cnnc_auth_login_stale_lock_files_still_start(client, monkeypatch, tmp_path):
    """Case D：锁文件与 owner.json 都存在但无人持有（stale 残留）→
    inspect active=False（区分“文件存在”vs“锁被持有”）→ 202 started。"""
    from app import dashboard_api

    lock_path = tmp_path / "login_recovery.lock"
    _inject_login_lock_path(monkeypatch, lock_path)
    lock_path.write_bytes(b"0")
    lock_path.with_name(lock_path.name + ".owner.json").write_text(
        json.dumps({"pid": 999999, "command": "login-and-recover",
                    "started_at": "2026-09-09T00:00:00+00:00"}),
        encoding="utf-8")
    monkeypatch.setattr(dashboard_api, "_cnnc_login_launched_at", None)
    launched = _fake_login_popen(monkeypatch)
    r = client.post("/dashboard/api/cnnc-auth/login", headers={
        "X-Dashboard-Action-Token": dashboard_api._ACTION_TOKEN})
    assert r.status_code == 202
    assert len(launched) == 1
