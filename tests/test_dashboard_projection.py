# tests/test_dashboard_projection.py
from app.services.dashboard_projection import compute_overall_status


def _status(**over):
    base = {
        "state": "OPEN", "remote_health": {"stall_detected": False},
        "counts": {"discovered": 151, "unique_items": 151, "new": 12, "changed": 8, "processed": 151},
        "detail": {"pending": 0, "running": 0, "completed": 151, "terminal": 0, "attempts": 151},
        "model": {"pending": 0, "running": 0, "completed": 151, "terminal": 0, "attempts": 151},
        "crawl": {"pending": 3, "running": 40, "completed": 45, "no_result": 30, "unplanned": 0,
                  "unknown": 4, "manual": 0, "waiting_submit": 0, "awaiting_priority": 1,
                  "awaiting_remote": 2, "blocked_manual": 0, "retryable": 0, "unclassified": 0},
        "remote": {"intent": 0, "pending": 3, "running": 3, "exported": 2, "completed": 45,
                   "unknown": 1, "manual": 0, "awaiting_priority": 1, "awaiting_harvest": 2,
                   "recovery_exhausted": 1},
        "final": {"pending": 7, "computed_pending": 7, "frozen": False, "finalized": False,
                  "forced": False, "output_path": None, "output_exists": False,
                  "manifest_recorded": False, "blocker_summary": {}},
        "metrics": {},
    }
    base.update(over)
    return base


def test_r1_manual_wins_and_explanation_mentions_every_attention():
    # Review Fix Round 1 后语义:MANUAL_REQUIRED 才是红色主状态;合法恢复用尽走黄。
    s = _status(remote={**_status()["remote"], "manual": 1})
    out = compute_overall_status(s, recovering=1)
    assert out["word"] == "需要人工处理" and out["color"] == "red"
    assert "招商局任务需要人工检查" in out["explanation"]
    assert "自动恢复已用尽" in out["explanation"]  # 关注项逐项提及(红优先,取前 3)
    assert "无需人工处理" not in out["explanation"]
    assert [a["level"] for a in out["attention"]][0] == "red"
    assert any(a["level"] == "yellow" for a in out["attention"])  # SUBMIT_UNKNOWN→黄


def test_r7_preparing_data_when_detail_pending():
    s = _status(detail={"pending": 10, "running": 5, "completed": 136, "terminal": 0, "attempts": 0},
                remote={"intent": 0, "pending": 0, "running": 0, "exported": 0, "completed": 0,
                        "unknown": 0, "manual": 0, "awaiting_priority": 0, "awaiting_harvest": 0,
                        "recovery_exhausted": 0},
                crawl={"pending": 0, "running": 0, "completed": 0, "no_result": 0, "unplanned": 0,
                       "unknown": 0, "manual": 0, "waiting_submit": 0, "awaiting_priority": 0,
                       "awaiting_remote": 0, "blocked_manual": 0, "retryable": 0, "unclassified": 0})
    out = compute_overall_status(s)
    assert out["word"] == "正在准备数据" and out["color"] == "blue"
    assert out["stages"][0]["state"] == "cur"


def test_r6_waiting_zhaoshang_and_recovering_mentioned():
    s = _status(remote={**_status()["remote"], "recovery_exhausted": 0, "unknown": 0})
    s["crawl"]["unknown"] = 0
    out = compute_overall_status(s, recovering=1)
    assert out["word"] == "正在等待招商局" and out["color"] == "blue"
    assert "1 个正在自动恢复" in out["explanation"]


def test_r5_harvest_when_only_awaiting_harvest():
    s = _status()
    s["remote"].update({"recovery_exhausted": 0, "unknown": 0, "pending": 0, "awaiting_priority": 0, "running": 0})
    s["crawl"].update({"unknown": 0, "awaiting_priority": 0, "awaiting_remote": 2})
    out = compute_overall_status(s)
    assert out["word"] == "正在回收招商局结果"


def test_r4_closeout_when_frozen():
    s = _status()
    s["remote"].update({"recovery_exhausted": 0, "unknown": 0})
    s["crawl"]["unknown"] = 0
    s["final"]["frozen"] = True
    out = compute_overall_status(s)
    assert out["word"] == "正在最终收口"
    assert out["stages"][2]["state"] == "cur"


def test_r3_finalized_green_and_forced_flag():
    s = _status(final={"pending": 0, "computed_pending": 0, "frozen": True, "finalized": True,
                       "forced": True, "output_path": "x.xlsx", "output_exists": True,
                       "manifest_recorded": True, "blocker_summary": {}})
    out = compute_overall_status(s)
    assert out["word"] == "今日结果已生成" and out["color"] == "green"
    assert out["final_state"] == "forced"
    assert all(st["state"] == "done" for st in out["stages"])


def test_r2_missing_gray():
    out = compute_overall_status({"state": "MISSING"})
    assert out["word"] == "今日无数据" and out["color"] == "gray"


def test_yellow_breaker_attention_not_word_change():
    s = _status(remote={**_status()["remote"], "recovery_exhausted": 0},
                remote_health={"stall_detected": True, "recent_stall_jobs": 3})
    s["crawl"]["unknown"] = 0
    out = compute_overall_status(s)
    assert out["word"] == "正在等待招商局"  # 黄不改词
    assert any("暂停新建任务" in a["title"] + a["system_action"] for a in out["attention"])


def test_healthy_no_attention_single_line():
    s = _status(remote={**_status()["remote"], "recovery_exhausted": 0, "unknown": 0})
    s["crawl"]["unknown"] = 0
    out = compute_overall_status(s)
    assert out["attention"] == []
    assert "无需人工处理" in out["explanation"]


def test_final_text_natural_language_blockers():
    s = _status(final={**_status()["final"], "missing_page_price_rows": 7,
                       "force_closeout_limit": 20,
                       "blocker_summary": {"remote_pending": 3, "remote_intent": 1,
                                           "remote_manual": 2, "crawl_pending": 57}})
    out = compute_overall_status(s)
    assert "招商局" in out["final_text"] and "下一次 closeout" in out["final_text"]
    assert "未爬到价格:7 条" in out["final_text"]
    assert "57 个底层采集工作项" in out["final_text"]


def test_explanation_sentence_boundaries_no_reversal_reading():
    s = _status(remote={**_status()["remote"], "recovery_exhausted": 0, "unknown": 0})
    s["crawl"]["unknown"] = 0
    out = compute_overall_status(s)
    assert "无需人工处理。" in out["explanation"]
    assert not any(ch.isdigit() for ch in out["explanation"].split("无需人工处理")[-1][:1])
    out_red = compute_overall_status(_status(remote={**_status()["remote"], "manual": 1}))
    assert "招商局任务需要人工检查" in out_red["explanation"]


def test_final_text_surfaces_remote_intent_only_blocker():
    s = _status(final={**_status()["final"], "blocker_summary": {"remote_intent": 1}})
    assert "1 个任务已排队待提交" in compute_overall_status(s)["final_text"]


import json
import sqlite3
from pathlib import Path

from app.services.dashboard_projection import project_remote_jobs, project_recovery_timeline


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.sqlite3"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE daily_runs(id INTEGER PRIMARY KEY, business_date TEXT, state TEXT DEFAULT 'OPEN',
        frozen_snapshot_id INTEGER, final_output_path TEXT, final_manifest_hash TEXT,
        metrics_json TEXT DEFAULT '{}', lease_owner TEXT, lease_expires_at TEXT,
        created_at TEXT, updated_at TEXT);
      CREATE TABLE remote_jobs(id INTEGER PRIMARY KEY, run_id INTEGER, logical_key TEXT, task_name TEXT,
        payload_hash TEXT, mall_key TEXT, input_path TEXT, result_path TEXT, payload_json TEXT,
        state TEXT, post_attempted INTEGER, remote_identity TEXT, export_path TEXT, result_json TEXT,
        error_text TEXT, harvest_attempts INTEGER DEFAULT 0, harvest_elapsed_seconds REAL DEFAULT 0,
        recovery_attempts INTEGER DEFAULT 0, recovery_evidence_json TEXT, created_at TEXT, updated_at TEXT);
      CREATE TABLE crawl_work(id INTEGER PRIMARY KEY, run_id INTEGER, mall_key TEXT, source_id TEXT,
        sku_id TEXT, exact_url TEXT, backend_version TEXT, fingerprint TEXT, state TEXT,
        remote_job_id INTEGER, result_json TEXT, error_text TEXT, updated_at TEXT);
      INSERT INTO daily_runs(id, business_date) VALUES (1, '2026-08-26');
      INSERT INTO remote_jobs(id, run_id, logical_key, task_name, payload_hash, mall_key, state,
        remote_identity, harvest_attempts, recovery_attempts, recovery_evidence_json, created_at, updated_at)
        VALUES (12, 1, 'k12', 'cnnc_current_opt_jd_1015', 'h', '京东商城', 'RUNNING', 'jd|88231|T1', 9, 1,
        '{"version":1,"observations":[{"observed_at":"2026-08-26T02:58:00+00:00","progress":{"total":120,"pending":75,"running":0,"failed":0,"waiting_db":0,"finished":45},"signature":[120,75,0,0,0,45],"export_had_result":false,"export_count":0,"export_rsp_desc":"未检索到商品价格信息","task_status_rsp_desc":""}],"restart":{"phase":"ENABLE_CONFIRMED","recovery_at":"2026-08-26T02:53:00+00:00"},"probe_health":null}',
        '2026-08-26T01:15:00+00:00', '2026-08-26T02:58:00+00:00');
      INSERT INTO crawl_work(run_id, mall_key, source_id, sku_id, exact_url, backend_version, fingerprint, state, remote_job_id)
        VALUES (1,'京东商城','a','s','u','legacy','f','SUCCESS',12),
               (1,'京东商城','b','s2','u2','legacy','f','PENDING',12);
    """)
    con.commit(); con.close()
    return db


def test_project_remote_jobs_recovers_and_subtasks(tmp_path):
    jobs = project_remote_jobs(_make_db(tmp_path), "2026-08-26")
    assert len(jobs) == 1
    j = jobs[0]
    assert j["mall_key"] == "京东商城" and j["state"] == "RUNNING"
    assert j["state_cn"] == "执行中 · 自动恢复中" and j["recovering"] is True
    assert j["subtasks"] == {"finished": 45, "total": 120}
    assert j["crawl_total"] == 2 and j["crawl_success"] == 1


def test_project_remote_jobs_exposes_sanitized_submit_diagnostic(tmp_path):
    db = _make_db(tmp_path)
    con = sqlite3.connect(db)
    evidence = {
        "version": 1,
        "observations": [],
        "restart": None,
        "probe_health": None,
        "submit_reconcile": {
            "status": "NOT_FOUND",
            "last_diagnostic": "lookup Cookie=session-secret still inconclusive",
            "last_reconcile_at": "2026-08-26T03:10:00+00:00",
            "recovery_attempts": 1,
        },
    }
    con.execute(
        "UPDATE remote_jobs SET state='SUBMIT_UNKNOWN', remote_identity=NULL, "
        "error_text=?, recovery_attempts=1, recovery_evidence_json=? WHERE id=12",
        (
            "timeout Authorization: Bearer TOP_SECRET at E:\\private\\payload.xlsx",
            json.dumps(evidence),
        ),
    )
    con.commit()
    con.close()

    job = project_remote_jobs(db, "2026-08-26")[0]
    diagnostic = job["diagnostic"]
    assert diagnostic["stage"] == "远端提交"
    assert diagnostic["classification"] == "提交结果待确认"
    assert diagnostic["reason"].startswith("timeout Authorization=[REDACTED]")
    browser_fields = json.dumps(
        {"error_text": job["error_text"], "diagnostic": diagnostic}, ensure_ascii=False
    )
    assert "TOP_SECRET" not in browser_fields
    assert "session-secret" not in browser_fields
    assert "E:\\private" not in browser_fields
    assert diagnostic["reconcile_attempts"] == 1
    assert diagnostic["reconcile_threshold"] == 4
    assert diagnostic["last_reconcile_diagnostic"] == "lookup Cookie=[REDACTED] still inconclusive"
    assert "exact reconcile" in diagnostic["next_action"]


def test_project_remote_jobs_is_honest_when_submit_diagnostic_was_lost(tmp_path):
    db = _make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE remote_jobs SET state='SUBMIT_UNKNOWN', remote_identity=NULL, error_text=NULL, "
        "recovery_evidence_json=NULL WHERE id=12"
    )
    con.commit()
    con.close()

    diagnostic = project_remote_jobs(db, "2026-08-26")[0]["diagnostic"]
    assert diagnostic["reason"] == "原始提交诊断不可恢复"
    assert diagnostic["last_reconcile_diagnostic"] == "尚无自动对账诊断"


def test_project_remote_jobs_classifies_priority_scope_manual(tmp_path):
    db = _make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE remote_jobs SET state='MANUAL_REQUIRED', error_text=? WHERE id=12",
        ("PriorityScopeSafetyError: task_status changed during priority update",),
    )
    con.commit()
    con.close()

    diagnostic = project_remote_jobs(db, "2026-08-26")[0]["diagnostic"]
    assert diagnostic["stage"] == "提优确认"
    assert diagnostic["classification"] == "PriorityScopeSafetyError"
    assert "task_status changed" in diagnostic["reason"]
    assert "不代表远端爬虫失败" in diagnostic["note"]


def test_project_remote_jobs_classifies_collapsed_priority_manual_without_inventing_reason(tmp_path):
    db = _make_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE remote_jobs SET state='MANUAL_REQUIRED', post_attempted=1, "
        "result_json=NULL, error_text=? WHERE id=12",
        ("highest priority was not confirmed",),
    )
    con.commit()
    con.close()

    diagnostic = project_remote_jobs(db, "2026-08-26")[0]["diagnostic"]
    assert diagnostic["stage"] == "提优确认"
    assert diagnostic["classification"] == "优先级未确认"
    assert diagnostic["reason"] == "highest priority was not confirmed"
    assert "PriorityScopeSafetyError" not in json.dumps(diagnostic, ensure_ascii=False)


def test_recovery_timeline_phase_and_observations(tmp_path):
    jobs = project_remote_jobs(_make_db(tmp_path), "2026-08-26")
    tl = project_recovery_timeline(jobs[0])
    assert tl["phase"] == "自动恢复中"
    assert tl["observations"][0]["signature"][5] == 45
    assert tl["restart"]["phase"] == "ENABLE_CONFIRMED"


def test_recovery_timeline_stalled_pending_restart(tmp_path):
    import sqlite3 as s3
    db = _make_db(tmp_path)
    con = s3.connect(db)
    row = con.execute("SELECT recovery_evidence_json FROM remote_jobs WHERE id=12").fetchone()
    ev = json.loads(row[0]); ev["restart"]["phase"] = "ENABLE_SENT"
    con.execute("UPDATE remote_jobs SET recovery_evidence_json=? WHERE id=12", (json.dumps(ev, ensure_ascii=False),))
    con.commit(); con.close()
    jobs = project_remote_jobs(db, "2026-08-26")
    assert project_recovery_timeline(jobs[0])["phase"] == "已证明停滞(恢复进行中)"


def test_recovery_timeline_recovered_success(tmp_path):
    import sqlite3 as s3
    db = _make_db(tmp_path)
    con = s3.connect(db)
    con.execute("UPDATE remote_jobs SET state='COMPLETED' WHERE id=12")
    con.commit(); con.close()
    jobs = project_remote_jobs(db, "2026-08-26")
    tl = project_recovery_timeline(jobs[0])
    assert tl["phase"] == "恢复成功" and jobs[0]["recovering"] is False


def test_recovery_timeline_plain_completed_is_normal(tmp_path):
    import sqlite3 as s3
    db = _make_db(tmp_path)
    con = s3.connect(db)
    con.execute("UPDATE remote_jobs SET state='COMPLETED', recovery_evidence_json=NULL WHERE id=12")
    con.commit(); con.close()
    jobs = project_remote_jobs(db, "2026-08-26")
    assert project_recovery_timeline(jobs[0])["phase"] == "正常采集"


from app.services.dashboard_projection import project_recent_days, _translate_task_result


def test_recent_days_from_daily_status_json(tmp_path):
    import json
    (tmp_path / "2026-08-25").mkdir()
    (tmp_path / "2026-08-25" / "daily_status.json").write_text(json.dumps({
        "state": "FINALIZED", "final": {"finalized": True, "forced": False, "output_exists": True},
        "crawl": {"no_result": 0}}), encoding="utf-8")
    (tmp_path / "2026-08-24").mkdir()
    (tmp_path / "2026-08-24" / "daily_status.json").write_text(json.dumps({
        "state": "OPEN", "final": {"finalized": False, "forced": False}}), encoding="utf-8")
    days = project_recent_days(tmp_path, n=3, today="2026-08-26")
    assert {"date": "2026-08-25", "class": "ok"} in days
    assert {"date": "2026-08-24", "class": "err"} in days
    assert {"date": "2026-08-26", "class": "none"} in days


def test_translate_task_result_known_codes():
    assert "正在运行" in _translate_task_result(267009)
    assert _translate_task_result(0).startswith("上次运行成功")
    assert "未知" in _translate_task_result(-1)


def test_scheduler_status_degrades(monkeypatch):
    import app.services.dashboard_projection as dp
    dp._scheduler_cache.update({"at": 0.0, "value": None})
    monkeypatch.setattr(dp, "_run_scheduler_probe", lambda: None)
    out = dp.project_scheduler_status()
    assert out == {"available": False}


def test_scheduler_status_degrades_on_timeout(monkeypatch):
    import subprocess as sp
    import app.services.dashboard_projection as dp

    def boom():
        raise sp.TimeoutExpired(cmd=["schtasks"], timeout=10)

    dp._scheduler_cache.update({"at": 0.0, "value": None})
    monkeypatch.setattr(dp, "_run_scheduler_probe", boom)
    assert dp.project_scheduler_status() == {"available": False}


def test_scheduler_status_from_powershell_probe(monkeypatch):
    import app.services.dashboard_projection as dp
    dp._scheduler_cache.update({"at": 0.0, "value": None})
    monkeypatch.setattr(dp, "_run_scheduler_probe", lambda: {
        "state": "Running", "last_run": "2026/8/26 9:15:00", "last_result": 267009,
        "next_run": "2026/8/26 9:30:00"})
    out = dp.project_scheduler_status()
    assert out["available"] is True and out["running"] is True
    assert "正在运行" in out["last_result_cn"]


# ---- Hotfix(2026-08-27 晚):scheduler 探针 UInt32 结果码溢出 ----

def test_scheduler_probe_uint32_result_code_translates_not_crashes(monkeypatch):
    """LastTaskResult 是 UInt32:0x800710E0(触发重叠被拒)=2147946720 > Int32.MaxValue。
    探针/翻译必须按数值正常处理,未知码给中文"未知"文案。"""
    import app.services.dashboard_projection as dp
    dp._scheduler_cache.update({"at": 0.0, "value": None})
    monkeypatch.setattr(dp, "_run_scheduler_probe", lambda: {
        "state": "Running", "last_run": "08/27/2026 18:25:25",
        "last_result": 2147946720, "next_run": "08/27/2026 18:30:30"})
    out = dp.project_scheduler_status()
    assert out["available"] is True and out["running"] is True
    assert out["last_result"] == "2147946720"
    assert "未知" in out["last_result_cn"]


def test_scheduler_probe_non_numeric_last_result_degrades_not_503(monkeypatch):
    """PS 侧强转失败时该字段可能序列化成对象/字符串:必须降级为未知结果码,
    绝不能让 TypeError 把整个 summary 接口打成 503(2026-08-27 18:2x 实况)。"""
    import app.services.dashboard_projection as dp
    for bad in ({}, {"error": "cast"}, "not-a-number", None, float("inf")):
        dp._scheduler_cache.update({"at": 0.0, "value": None})
        monkeypatch.setattr(dp, "_run_scheduler_probe", lambda bad=bad: {
            "state": "Ready", "last_run": "08/27/2026 18:25:25",
            "last_result": bad, "next_run": "08/27/2026 18:30:30"})
        out = dp.project_scheduler_status()
        assert out["available"] is True
        assert out["last_result"] == "-1"
        assert "未知" in out["last_result_cn"]


def test_rf_probe_malformed_top_level_shape_degrades_unavailable(monkeypatch):
    """Review fix:探针可能返回"合法 JSON 但非 dict 顶层"([]/字符串/数字/bool),
    json.loads 不报错而 data.get 会 AttributeError 逃逸把 summary 打成 503。
    任何坏形状只能降级 unavailable,不得抛异常。"""
    import app.services.dashboard_projection as dp
    for bad in (None, [], ["unexpected"], "unexpected", 123, True):
        dp._scheduler_cache.update({"at": 0.0, "value": None})
        monkeypatch.setattr(dp, "_run_scheduler_probe", lambda bad=bad: bad)
        assert dp.project_scheduler_status() == {"available": False}


def test_rf_probe_bad_last_result_keeps_running_state_truth(monkeypatch):
    """bad last_result 降级不得覆盖 PowerShell state=Running 的真实判定。"""
    import app.services.dashboard_projection as dp
    dp._scheduler_cache.update({"at": 0.0, "value": None})
    monkeypatch.setattr(dp, "_run_scheduler_probe", lambda: {
        "state": "Running", "last_run": "08/27/2026 18:25:25",
        "last_result": {}, "next_run": "08/27/2026 18:30:30"})
    out = dp.project_scheduler_status()
    assert out["available"] is True and out["running"] is True
    assert out["last_result"] == "-1"


def test_scheduler_probe_source_no_int32_cast():
    import inspect
    import app.services.dashboard_projection as dp
    src = inspect.getsource(dp._run_scheduler_probe)
    assert "[int64]" in src, "探针必须用 [int64] 承载 UInt32 结果码"
    assert "[int]" not in src.replace("[int64]", ""), "不得残留 Int32 强转(会溢出)"


from app.services.dashboard_projection import build_dashboard_summary, fingerprint_payload


def test_build_summary_end_to_end(tmp_path, monkeypatch):
    (tmp_path / "day").mkdir()
    db = _make_db(tmp_path / "day")
    from app.services import dashboard_projection as dp
    monkeypatch.setattr(dp, "collect_runtime_status", lambda **kw: _status())
    monkeypatch.setattr(dp, "runtime_paths", lambda bd, root=None: type("P", (), {
        "state_path": db, "run_dir": db.parent})())
    monkeypatch.setattr(dp, "project_scheduler_status", lambda: {"available": False})
    monkeypatch.setattr(dp, "project_recent_days", lambda root, n=14, **kw: [])
    out = build_dashboard_summary("2026-08-26", tmp_path)
    # Review Fix Round 1 后语义:合法恢复用尽不再把整日推红;该日仍有 pending → 等待招商局。
    assert out["overall"]["word"] == "正在等待招商局" and out["overall"]["color"] == "blue"
    assert any(j["mall_key"] == "京东商城" for j in out["jobs"])
    assert out["timelines"] and "12" in out["timelines"]
    assert all("recovery_evidence_json" not in j for j in out["jobs"])
    assert next(j for j in out["jobs"] if j["id"] == 12)["recovery_attempts"] == 1
    assert len(out["fingerprint"]) == 64
    again = build_dashboard_summary("2026-08-26", tmp_path)
    assert again["fingerprint"] == out["fingerprint"]  # 同数据同指纹


def test_fingerprint_ignores_generated_at():
    a = {"generated_at": "t1", "fingerprint": "", "data": {"x": 1}}
    b = {"generated_at": "t2", "fingerprint": "", "data": {"x": 1}}
    assert fingerprint_payload(a) == fingerprint_payload(b)


def test_final_text_ok_and_missing_output_branches():
    ok = _status(final={"pending": 0, "computed_pending": 0, "frozen": True, "finalized": True,
                        "forced": False, "output_path": "x.xlsx", "output_exists": True,
                        "manifest_recorded": True, "blocker_summary": {}})
    out_ok = compute_overall_status(ok)
    assert out_ok["final_state"] == "ok" and "已生成" in out_ok["final_text"] and "通过" in out_ok["final_text"]
    missing = _status(final={"pending": 0, "computed_pending": 0, "frozen": True, "finalized": True,
                             "forced": False, "output_path": "x.xlsx", "output_exists": False,
                             "manifest_recorded": False, "blocker_summary": {}})
    out_missing = compute_overall_status(missing)
    assert "失败" in out_missing["final_text"]


# ---- Independent Review Fix Round 1(2026-08-27):P1-1 / P1-2 / P1-3 / P1-4 ----

def _cleared(**over):
    """前置工作全部完成、招商局/采集全部清零、未冻结未出表(等待 18:00 收口)。"""
    s = _status(
        crawl={"pending": 0, "running": 0, "completed": 45, "no_result": 30, "unplanned": 0,
               "unknown": 0, "manual": 0, "waiting_submit": 0, "awaiting_priority": 0,
               "awaiting_remote": 0, "blocked_manual": 0, "retryable": 0, "unclassified": 0},
        remote={"intent": 0, "pending": 0, "running": 0, "exported": 0, "completed": 45,
                "unknown": 0, "manual": 0, "awaiting_priority": 0, "awaiting_harvest": 0,
                "recovery_exhausted": 0},
        final={"pending": 0, "computed_pending": 0, "frozen": False, "finalized": False,
               "forced": False, "output_path": None, "output_exists": False,
               "manifest_recorded": False, "blocker_summary": {}},
    )
    return s


def test_rf1_legal_recovery_exhausted_is_not_manual():
    s = _status()  # 唯一"异常"计数是 remote.recovery_exhausted=1(合法 durable proof)
    out = compute_overall_status(s)
    assert out["word"] != "需要人工处理" and out["color"] != "red"
    assert not any(a["level"] == "red" and "恢复已用尽" in a["title"] for a in out["attention"])
    assert any(a["level"] == "yellow" and a["title"] == "自动恢复已用尽" for a in out["attention"])
    assert out["zs_summary"]["manual"] == 0
    assert out["zs_summary"]["recovery_exhausted"] == 1
    assert out["stages"][1]["state"] != "bad"


def test_rf1_invalid_recovery_exhausted_proof_is_red():
    s = _status()
    out = compute_overall_status(s, recovery_exhausted_invalid=1)
    assert out["word"] == "需要人工处理" and out["color"] == "red"
    assert any(a["level"] == "red" and "证据不完整" in a["title"] for a in out["attention"])
    assert out["zs_summary"]["manual"] == 1
    assert out["stages"][1]["state"] == "bad"


def test_rf1_manual_required_still_red():
    s = _status(remote={**_status()["remote"], "recovery_exhausted": 0, "manual": 2})
    out = compute_overall_status(s)
    assert out["word"] == "需要人工处理" and out["color"] == "red"
    assert any(a["title"] == "招商局任务需要人工检查" and a["level"] == "red" for a in out["attention"])
    assert [a["level"] for a in out["attention"]][0] == "red"


def test_rf1_legal_exhausted_with_cleared_day_waits_closeout():
    s = _cleared()
    s["remote"]["recovery_exhausted"] = 1
    out = compute_overall_status(s)
    assert out["word"] == "等待最终收口" and out["color"] == "blue"
    assert out["zs_summary"]["manual"] == 0 and out["zs_summary"]["recovery_exhausted"] == 1
    assert out["stages"][1]["state"] == "done"


def test_rf2_wait_final_closeout_when_all_cleared_before_freeze():
    s = _cleared()
    out = compute_overall_status(s)
    assert out["word"] == "等待最终收口"
    assert out["word"] != "正在准备数据"
    assert "18:00" in out["explanation"]
    assert out["stages"][1]["state"] == "done"
    assert out["stages"][2]["state"] == "wait"


def test_rf4_crawl_manual_impact_says_finalize_blocked():
    s = _status(remote={**_status()["remote"], "recovery_exhausted": 0})
    s["crawl"]["manual"] = 2
    out = compute_overall_status(s)
    assert out["word"] == "需要人工处理"
    item = next(a for a in out["attention"] if a["title"] == "采集行需要人工处理")
    assert "阻塞" in item["impact"] and "最终出表" in item["impact"]
    assert "留空进入最终表" not in item["impact"]


_PROVEN_RESTART = {
    "phase": "ENABLE_CONFIRMED", "recovery_at": "2026-08-26T02:53:00+00:00",
    "disable_evidence": {"confirmed_disabled": True},
    "enable_evidence": {"confirmed_enabled": True},
}


def _job_db(tmp_path, *, state, post_attempted=0, restart_patch=None):
    day = tmp_path / "day"
    day.mkdir(parents=True, exist_ok=True)
    db = _make_db(day)
    con = sqlite3.connect(db)
    if restart_patch is not None:
        row = con.execute("SELECT recovery_evidence_json FROM remote_jobs WHERE id=12").fetchone()
        ev = json.loads(row[0])
        ev["restart"] = restart_patch
        con.execute("UPDATE remote_jobs SET recovery_evidence_json=? WHERE id=12",
                    (json.dumps(ev, ensure_ascii=False),))
    con.execute("UPDATE remote_jobs SET state=?, post_attempted=? WHERE id=12", (state, post_attempted))
    con.commit()
    con.close()
    return db


def test_rf3_completed_recovered_job_keeps_timeline_at_summary_level(tmp_path, monkeypatch):
    db = _job_db(tmp_path, state="COMPLETED")  # fixture 自带 recovery_attempts=1 + ENABLE_CONFIRMED
    from app.services import dashboard_projection as dp
    monkeypatch.setattr(dp, "collect_runtime_status", lambda **kw: _cleared())
    monkeypatch.setattr(dp, "runtime_paths", lambda bd, root=None: type("P", (), {
        "state_path": db, "run_dir": db.parent})())
    monkeypatch.setattr(dp, "project_scheduler_status", lambda: {"available": False})
    monkeypatch.setattr(dp, "project_recent_days", lambda root, n=14, **kw: [])
    out = dp.build_dashboard_summary("2026-08-26", tmp_path)
    tl = out["timelines"].get("12")
    assert tl is not None, "COMPLETED 且已恢复的任务必须在 summary API 中保留 recovery timeline"
    assert tl["phase"] == "恢复成功"
    assert tl["recovery_attempts"] == 1
    job = next(j for j in out["jobs"] if j["id"] == 12)
    assert job["recovery_attempts"] == 1 and job["pill"] == "ok"


def test_rf1_project_remote_jobs_marks_exhausted_proof_validity(tmp_path):
    from app.workflows.incremental_price_audit_finalize import _valid_recovery_exhausted_evidence
    valid_db = _job_db(tmp_path / "v", state="RECOVERY_EXHAUSTED", post_attempted=1,
                       restart_patch=_PROVEN_RESTART)
    jobs = project_remote_jobs(valid_db, "2026-08-26")
    assert jobs[0]["recovery_exhausted_valid"] is True
    assert jobs[0]["pill"] == "warn"
    assert _valid_recovery_exhausted_evidence(jobs[0]) is True  # 与 finalize 权威判定一致
    invalid_db = _job_db(tmp_path / "i", state="RECOVERY_EXHAUSTED", post_attempted=1)
    jobs2 = project_remote_jobs(invalid_db, "2026-08-26")
    assert jobs2[0]["recovery_exhausted_valid"] is False
    assert jobs2[0]["pill"] == "err"
    assert _valid_recovery_exhausted_evidence(jobs2[0]) is False


def test_rf1_summary_overall_reflects_exhausted_proof_validity(tmp_path, monkeypatch):
    from app.services import dashboard_projection as dp

    def build(db):
        st = _cleared()
        st["remote"]["recovery_exhausted"] = 1
        monkeypatch.setattr(dp, "collect_runtime_status", lambda **kw: st)
        monkeypatch.setattr(dp, "runtime_paths", lambda bd, root=None: type("P", (), {
            "state_path": db, "run_dir": db.parent})())
        monkeypatch.setattr(dp, "project_scheduler_status", lambda: {"available": False})
        monkeypatch.setattr(dp, "project_recent_days", lambda root, n=14, **kw: [])
        return dp.build_dashboard_summary("2026-08-26", tmp_path)

    out_valid = build(_job_db(tmp_path / "v", state="RECOVERY_EXHAUSTED", post_attempted=1,
                              restart_patch=_PROVEN_RESTART))
    assert out_valid["overall"]["word"] == "等待最终收口"
    assert out_valid["overall"]["color"] != "red"
    out_invalid = build(_job_db(tmp_path / "i", state="RECOVERY_EXHAUSTED", post_attempted=1))
    assert out_invalid["overall"]["word"] == "需要人工处理"
    assert out_invalid["overall"]["color"] == "red"


# ---- Review Fix Round 2(2026-08-27):detail.terminal 不阻塞等待最终收口 ----

def _detail_terminal_day():
    s = _cleared()
    s["detail"] = {"pending": 0, "running": 0, "completed": 150, "terminal": 1, "attempts": 151,
                   "customer_price_required": 151, "customer_price_ready": 150,
                   "customer_price_pending": 0, "customer_price_retrying": 0,
                   "customer_price_missing": 1, "customer_price_failed": 0,
                   "customer_price_invalid": 0, "customer_price_absent_proven": 0,
                   "customer_absent_skipped": 0}
    return s


def test_detail_terminal_blocks_as_manual_after_customer_price_gates():
    """Flipped with the customer-price gates(2026-09-03 一致性修复):detail
    terminal = 客户价重试预算耗尽,是真实人工阻塞——hero word 与红色 attention
    同源,不再出现"红色需人工 + 蓝色等待收口"的自相矛盾。"""
    out = compute_overall_status(_detail_terminal_day())
    assert out["word"] == "需要人工处理"
    assert out["color"] == "red"
    assert out["stages"][0]["state"] == "bad"          # prep 卡同源显示异常
    assert out["stages"][0]["state_word"] == "⚠ 异常"


def test_customer_price_gates_red_attention_replaces_old_detail_terminal_yellow():
    """Flipped with the customer-price gates(2026-09-03):重试预算耗尽的客户价
    失败是真实阻塞,旧"Detail 有终态失败/不阻塞出表/客户价留空继续收口"黄条
    与新 finalize 门禁矛盾,被红色"客户价获取失败"取代。"""
    out = compute_overall_status(_detail_terminal_day())
    item = next(a for a in out["attention"] if a["title"] == "客户价获取失败")
    assert item["level"] == "red" and item["tag"] == "需人工"
    assert "重试预算耗尽" in item["problem"]
    assert "禁止自动审批与最终出表" in item["impact"]
    assert not any(a["title"] == "Detail 有终态失败" for a in out["attention"])
    assert out["word"] == "需要人工处理" and out["color"] == "red"
    assert "无需人工处理" not in out["explanation"]  # 解释句铁律
    assert "客户价获取失败" in out["explanation"]


def test_customer_price_failed_prep_card_bad_and_word_manual_consistent():
    """客户价 failed/invalid(m["terminal"]=0、d["terminal"]=1 同形)场景:
    prep 卡按 cp_blocking 显示异常,word 升为需要人工处理——三处同源一致。"""
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=149, customer_price_failed=2,
                             customer_price_invalid=1, terminal=3)
    out = compute_overall_status(s)
    assert out["word"] == "需要人工处理" and out["color"] == "red"
    assert out["stages"][0]["state"] == "bad"
    assert any(a["level"] == "red" and a["title"] == "客户价获取失败"
               for a in out["attention"])


def test_invalid_only_zero_price_success_still_turns_day_red():
    """INVALID-only(d.terminal==0)也推红:SUCCESS+price<=0 的无效客户价阶段态
    是 SUCCESS(detail.terminal=0),但 finalize ready 门与 NON_TOLERABLE 同样
    阻塞——hero word / prep 卡 / 红色 attention 必须与 finalize 同源一致。"""
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=150, customer_price_invalid=1)
    assert s["detail"]["terminal"] == 0
    out = compute_overall_status(s)
    assert out["word"] == "需要人工处理" and out["color"] == "red"
    assert out["stages"][0]["state"] == "bad"
    assert any(a["level"] == "red" and a["title"] == "客户价获取失败"
               for a in out["attention"])
    assert "禁止自动审批与最终出表" in next(
        a for a in out["attention"] if a["title"] == "客户价获取失败")["impact"]


def test_absent_proven_only_turns_day_red_and_blocks():
    """MISSING_PROVEN(缺详情身份/未提供价,非单据缺席)保持 R2-B 阻塞语义:
    hero word 推红、prep 卡显示异常、attention 独立红色"客户价确定性缺失"。"""
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=148, customer_price_absent_proven=3)
    out = compute_overall_status(s)
    assert out["word"] == "需要人工处理" and out["color"] == "red"
    assert out["stages"][0]["state"] == "bad"
    assert any(a["level"] == "red" and a["tag"] == "需人工"
               and a["title"] == "客户价确定性缺失" for a in out["attention"])
    # 旧黄条(证据齐全缺席/不阻塞出表)必须彻底消失。
    assert not any(a["title"] == "部分商品客户价缺失(证据齐全)" for a in out["attention"])


def test_customer_absent_skipped_only_is_yellow_nonblocking_and_day_not_red():
    """2026-09-11 规则变更:customer_absent_skipped(客户侧权威查询成功且单据
    缺席)是跳过态——仅黄色"关注"条目,不推红 hero word、不判 prep 异常、
    computed_pending 为零时日状态保持"等待最终收口"。"""
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=148, customer_absent_skipped=3)
    out = compute_overall_status(s)
    assert out["word"] != "需要人工处理" and out["color"] != "red"
    assert out["stages"][0]["state"] != "bad"
    item = next(a for a in out["attention"] if a["title"] == "客户侧已无数据跳过(不阻塞)")
    assert item["level"] == "yellow" and item["tag"] == "关注"
    assert "3 条商品" in item["problem"] and "无需处理" in item["problem"]
    assert "不阻塞最终出表" in item["impact"]
    assert "不重试" in item["system_action"]
    assert "无需操作" in item["suggestion"]
    assert not any(a["level"] == "red" for a in out["attention"])


def test_rf2_finalized_day_stays_green_with_customer_price_failure_history():
    """已出表日客户价失败历史不消失:红色条目如实保留(强制收口的缺价可见),
    但 finalized 主状态不因历史失败翻红。"""
    s = _detail_terminal_day()
    s["final"] = {"pending": 0, "computed_pending": 0, "frozen": True, "finalized": True,
                  "forced": False, "output_path": "x.xlsx", "output_exists": True,
                  "manifest_recorded": True, "blocker_summary": {}}
    out = compute_overall_status(s)
    assert out["word"] == "今日结果已生成" and out["color"] == "green"
    item = next(a for a in out["attention"] if a["title"] == "客户价获取失败")
    assert item["level"] == "red"  # 失败历史不降级、不消失
    assert not any(a["title"] == "Detail 有终态失败" for a in out["attention"])


def test_rf2_empty_run_before_first_snapshot_shows_preparing_not_closeout():
    s = _cleared()
    s["counts"] = {"discovered": 0, "unique_items": 0, "new": 0, "changed": 0, "processed": 0}
    s["detail"] = {"pending": 0, "running": 0, "completed": 0, "terminal": 0, "attempts": 0}
    s["model"] = {"pending": 0, "running": 0, "completed": 0, "terminal": 0, "attempts": 0}
    out = compute_overall_status(s)
    assert out["word"] == "正在准备数据"  # 首个快照落地前的空窗不得宣称"已全部完成"


# ---- Review Fix Round 2(2026-09-02):P2-1 closeout writer 谓词同源一致性 ----
# Hero(word)/阶段卡(stages)/final_text/explanation 四处必须同源于单一权威谓词
# _closeout_writer_active(closeout/finalize 持锁才算收口进行中)。

def _closeout_writer_day(command: str = "closeout", writer: dict | None = None):
    """清零日(discovered>0、computed_pending=0、未冻结、无阻塞)+ production writer。"""
    s = _cleared()
    s["production_writer"] = writer if writer is not None \
        else {"active": True, "command": command}
    return s


def test_r2_closeout_writer_running_aligns_word_stage_final_text_explanation():
    s = _closeout_writer_day()
    out = compute_overall_status(s)
    assert out["word"] == "正在最终收口" and out["color"] == "blue"
    assert out["stages"][2]["state"] == "cur"
    assert out["stages"][2]["sub"] == [("数据冻结", "收口任务已启动，正在准备")]
    # closeout 正在执行时 final_text 不得宣称"等待下一次 closeout"
    assert "正在执行" in out["final_text"]
    assert "等待下一次 closeout" not in out["final_text"]
    # 未冻结时 explanation 不得声称"已按 18:00 口径冻结"
    assert "已按 18:00 口径冻结" not in out["explanation"]
    assert "正在执行" in out["explanation"]


def test_r2_tick_writer_lock_keeps_waiting_closeout_baseline():
    s = _closeout_writer_day(command="tick")
    out = compute_overall_status(s)
    assert out["word"] == "等待最终收口"  # 基线不变:tick 持锁不算收口进行中
    assert out["stages"][2]["state"] == "wait"
    assert "等待下一次 closeout" in out["final_text"]
    assert "已按 18:00 口径冻结" not in out["explanation"]


def test_r2_frozen_without_writer_keeps_frozen_explanation():
    s = _closeout_writer_day()
    s["production_writer"] = {"active": False, "command": None}   # 锁未持有
    s["final"]["frozen"] = True
    out = compute_overall_status(s)
    assert out["word"] == "正在最终收口"
    # 既有语义保持:真已冻结才说"已按 18:00 口径冻结"
    assert "已按 18:00 口径冻结" in out["explanation"]
    assert out["stages"][2]["sub"] == [("数据冻结", "已冻结")]


# ---- 客户价可观测性(2026-09-03,plan §8):prep 卡 / attention 三态 / final_text ----

def _cp_detail(**over):
    d = {"pending": 0, "running": 0, "completed": 151, "terminal": 0, "attempts": 151,
         "customer_price_required": 151, "customer_price_ready": 151,
         "customer_price_pending": 0, "customer_price_retrying": 0,
         "customer_price_missing": 0, "customer_price_failed": 0,
         "customer_price_invalid": 0, "customer_price_absent_proven": 0,
         "customer_absent_skipped": 0, "customer_price_ratio": 1.0}
    d.update(over)
    return d


def test_prep_stage_sub_includes_customer_price_progress_row():
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=150)
    out = compute_overall_status(s)
    prep = out["stages"][0]
    assert ("客户价", "150/151") in prep["sub"]
    assert ("Detail", "151/151") in prep["sub"]      # 既有技术行保留


def test_prep_stage_customer_price_placeholder_when_nothing_required():
    s = _cleared()   # detail 无 customer_price_* 键 → required 0
    out = compute_overall_status(s)
    assert ("客户价", "—") in out["stages"][0]["sub"]


def test_attention_customer_price_retrying_is_yellow_auto_retry():
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=149, customer_price_retrying=2)
    out = compute_overall_status(s)
    item = next(a for a in out["attention"] if a["title"] == "客户价获取正在自动重试")
    assert item["level"] == "yellow" and item["tag"] == "自动重试"
    assert "2 条商品" in item["problem"]
    assert "不影响其他链路" in item["impact"] and "无需人工" in item["impact"]
    assert "3 次" in item["system_action"]           # 重试预算(=3)可见
    assert "无需操作" in item["suggestion"]
    assert not any(a["level"] == "red" for a in out["attention"])


def test_attention_customer_price_exhausted_failure_is_red_manual():
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=148, customer_price_missing=1,
                             customer_price_failed=2, customer_price_invalid=1)
    out = compute_overall_status(s)
    item = next(a for a in out["attention"] if a["title"] == "客户价获取失败")
    assert item["level"] == "red" and item["tag"] == "需人工"
    assert "重试预算耗尽仍未取得" in item["problem"]
    # 正文区分三种失败来源(NOT_FOUND 缺失 / 请求失败 / 无效价)
    assert "NOT_FOUND 缺失" in item["problem"] and "详情请求或解析失败" in item["problem"] \
        and "无效客户价" in item["problem"]
    assert "禁止自动审批与最终出表" in item["impact"]
    assert "已停止自动重试" in item["system_action"]
    assert "客户价明细" in item["suggestion"] and "requeue" in item["suggestion"]


def test_attention_absent_proven_is_red_blocking_and_distinct_from_exhausted():
    """MISSING_PROVEN(缺详情身份/未提供价)是独立红条(tag 需人工),与"客户价
    获取失败"(retry 耗尽)并列;单据缺席(customer_absent_skipped)则不再入此条。"""
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=148, customer_price_absent_proven=3)
    out = compute_overall_status(s)
    item = next(a for a in out["attention"] if a["title"] == "客户价确定性缺失")
    assert item["level"] == "red" and item["tag"] == "需人工"
    assert "确定性缺席" in item["problem"] and "自动重试无意义" in item["problem"]
    assert "缺少价格审核基准" in item["impact"] and "阻塞最终出表" in item["impact"]
    assert "不再自动重试" in item["system_action"] and "客户价明细" in item["system_action"]
    # 仅 absent_proven 时不得出现"客户价获取失败"(那是 retry 耗尽条目)。
    assert not any(a["title"] == "客户价获取失败" for a in out["attention"])


def test_absent_proven_and_exhausted_failures_are_two_parallel_red_items():
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=147, customer_price_missing=1,
                             customer_price_failed=2, customer_price_absent_proven=3)
    out = compute_overall_status(s)
    titles = [a["title"] for a in out["attention"] if a["level"] == "red"]
    assert "客户价获取失败" in titles and "客户价确定性缺失" in titles


def test_customer_absent_skipped_is_a_separate_nonblocking_attention_item():
    """customer_absent_skipped 与红色阻塞条目并列展示,但保持黄色非阻塞。"""
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=147, customer_price_failed=1,
                             customer_absent_skipped=3)
    out = compute_overall_status(s)
    skipped = next(a for a in out["attention"] if a["title"] == "客户侧已无数据跳过(不阻塞)")
    assert skipped["level"] == "yellow" and skipped["tag"] == "关注"
    assert any(a["level"] == "red" and a["title"] == "客户价获取失败"
               for a in out["attention"])


def test_absent_proven_lowers_customer_price_ratio_and_blocks_prep():
    """获取率仍是 ready/required(absent_proven 降低获取率):318/322 → 99%。"""
    s = _cleared()
    s["detail"] = _cp_detail(customer_price_ready=318, customer_price_required=322,
                             customer_price_completed=322, customer_price_absent_proven=4,
                             customer_price_ratio=round(318 / 322, 2))
    out = compute_overall_status(s)
    prep = out["stages"][0]
    assert prep["state"] == "bad" and prep["state_word"] == "⚠ 异常"
    assert ("客户价", "318/322") in prep["sub"]
    assert out["word"] == "需要人工处理" and out["color"] == "red"


def test_final_text_surfaces_absent_proven_blocker():
    s = _cleared()
    s["final"] = {**_cleared()["final"],
                  "blocker_summary": {"customer_price_absent_proven": 5}}
    out = compute_overall_status(s)
    assert "5 条客户价确定性缺失(无详情身份/未提供价)" in out["final_text"]


def test_final_text_never_surfaces_customer_absent_skipped_as_blocker():
    """customer_absent_skipped 是非阻塞跳过态,绝不进入"还不能生成最终 Excel"
    的阻塞清单。"""
    s = _cleared()
    s["final"] = {**_cleared()["final"],
                  "blocker_summary": {"customer_absent_skipped": 5}}
    out = compute_overall_status(s)
    assert "5 条" not in out["final_text"] or "跳过" not in out["final_text"]
    assert "全部阻塞已清零" in out["final_text"]


def test_final_text_surfaces_customer_price_exhausted_blocker():
    s = _cleared()
    s["final"] = {**_cleared()["final"],
                  "blocker_summary": {"detail_terminal": 2, "customer_price_missing": 2}}
    out = compute_overall_status(s)
    assert "2 条客户价获取失败(重试耗尽)" in out["final_text"]


def test_final_text_surfaces_customer_price_invalid_only_blocker():
    """INVALID-only 天不得显示"全部阻塞已清零":证据无效(价格与状态矛盾)
    同样阻塞最终出表,必须逐条出现。"""
    s = _cleared()
    s["final"] = {**_cleared()["final"],
                  "blocker_summary": {"customer_price_invalid": 3}}
    out = compute_overall_status(s)
    assert "3 条客户价证据无效(价格与状态矛盾)" in out["final_text"]
    assert "全部阻塞已清零" not in out["final_text"]


def test_build_summary_customer_price_block_and_fingerprint_reacts(tmp_path, monkeypatch):
    """summary 携带客户价轻量汇总(counts+ratio,无行级数据);ETag 指纹随其变化。"""
    from app.services import dashboard_projection as dp
    (tmp_path / "day").mkdir()
    db = _make_db(tmp_path / "day")
    state = _cleared()
    state["detail"] = _cp_detail()

    def build():
        monkeypatch.setattr(dp, "collect_runtime_status", lambda **kw: state)
        monkeypatch.setattr(dp, "runtime_paths", lambda bd, root=None: type("P", (), {
            "state_path": db, "run_dir": db.parent})())
        monkeypatch.setattr(dp, "project_scheduler_status", lambda: {"available": False})
        monkeypatch.setattr(dp, "project_recent_days", lambda root, n=14, **kw: [])
        return dp.build_dashboard_summary("2026-08-26", tmp_path)

    out = build()
    cp = out["customer_price"]
    assert cp == {"required": 151, "ready": 151, "pending": 0, "retrying": 0,
                  "missing": 0, "failed": 0, "invalid": 0, "absent_proven": 0,
                  "absent_skipped": 0, "ratio": 1.0}
    assert out["overall"]["stages"][0]["sub"][-1] == ("客户价", "151/151")
    fingerprint_before = out["fingerprint"]
    state["detail"]["customer_price_failed"] = 3   # 重试耗尽的行进入业务失败
    state["detail"]["customer_price_ready"] = 148
    state["detail"]["customer_price_ratio"] = round(148 / 151, 2)   # status 层权威值
    again = build()
    assert again["customer_price"]["failed"] == 3
    assert again["customer_price"]["ratio"] == round(148 / 151, 2) < 1.0
    assert again["fingerprint"] != fingerprint_before   # 指纹必须跟着变(无 304 陈旧)
