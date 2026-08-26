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
    s = _status()
    out = compute_overall_status(s, recovering=1)
    assert out["word"] == "需要人工处理" and out["color"] == "red"
    assert "自动恢复已用尽" in out["explanation"]
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
    s = _status(final={**_status()["final"], "blocker_summary": {"remote_pending": 3, "remote_intent": 1, "remote_manual": 2}})
    out = compute_overall_status(s)
    assert "招商局" in out["final_text"] and "下一次 closeout" in out["final_text"]


def test_explanation_sentence_boundaries_no_reversal_reading():
    s = _status(remote={**_status()["remote"], "recovery_exhausted": 0, "unknown": 0})
    s["crawl"]["unknown"] = 0
    out = compute_overall_status(s)
    assert "无需人工处理。" in out["explanation"]
    assert not any(ch.isdigit() for ch in out["explanation"].split("无需人工处理")[-1][:1])
    out_red = compute_overall_status(_status())
    assert "自动恢复已用尽。" in out_red["explanation"]


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


def test_recovery_timeline_phase_and_observations(tmp_path):
    jobs = project_remote_jobs(_make_db(tmp_path), "2026-08-26")
    tl = project_recovery_timeline(jobs[0])
    assert tl["phase"] == "自动恢复中"
    assert tl["observations"][0]["signature"][5] == 45
    assert tl["restart"]["phase"] == "ENABLE_CONFIRMED"
