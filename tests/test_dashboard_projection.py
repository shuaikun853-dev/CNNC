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
    s = _status(final={**_status()["final"], "blocker_summary": {"remote_pending": 3, "remote_priority_pending": 1, "remote_manual": 2}})
    out = compute_overall_status(s)
    assert "招商局" in out["final_text"] and "下一次 closeout" in out["final_text"]
