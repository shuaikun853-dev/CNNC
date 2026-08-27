# CNNC STEP_1 实时中文只读监控驾驶舱 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 CNNC STEP_1 构建零写入、中文优先、异常优先/渐进披露的实时只读监控 Dashboard(FastAPI `/dashboard` + 原生单页)。

**Architecture:** 复用 `collect_runtime_status()`(SQLite mode=ro)作为唯一状态源;新增 `dashboard_projection.py` 提供 `compute_overall_status` 等纯函数投影与 4 个只读扩展投影;`dashboard_api.py` 以 APIRouter 挂载 `/dashboard` 前缀(单页 + summary JSON,ETag/304);前端为 vendored 原生 HTML/CSS/JS 单页,按 V2 原型移植,条件轮询。

**Tech Stack:** Python 3.14(E:\Python314\python.exe)、FastAPI/starlette(现有依赖,零新增)、pytest、原生 HTML/CSS/JS(零构建零 CDN)。

**Spec:** `docs/superpowers/specs/2026-08-26-step1-realtime-dashboard-design.md`(V2,已获用户批准)

## Global Constraints

- 解释器固定 `E:\Python314\python.exe`;pytest 一律显式 `--basetemp`,禁止裸 pytest(仓库已知坑)。
- 分支 `feat/step1-realtime-dashboard-20260826`,worktree `E:\.A中核运维\cnnc-dashboard-dev`;每个 Task 结束 commit。
- **零业务写入**:SQLite 只允许 `file:...?mode=ro` URI 短连接;禁止 POST/PUT/DELETE 端点;禁止触碰生产 `runtime/`、现有 `CNNC-PriceAudit-Incremental` 计划任务、现有路由 `/health` `/diagnose` `/workflows/*` 的行为。
- **零新增 Python 依赖**(StaticFiles 用 starlette 自带);前端资产全部 vendored 于 `app/static/dashboard/`,禁止任何 CDN/外链,字体用系统中文栈。
- 界面中文优先;英文原值只出现在"技术详情"折叠区。
- 服务绑定 `127.0.0.1:8765`。
- TDD 铁律:每个行为先写失败测试并亲眼看它失败,再最小实现。
- 测试不接触生产 runtime;一律用 `tmp_path` 构造 fixture。

## File Structure

- Create `app/services/dashboard_projection.py` — 全部纯投影:overall status、招商局任务/恢复时间线、近 N 天、scheduler、summary 组装、fingerprint。
- Create `app/dashboard_api.py` — APIRouter(2 个 GET)。
- Create `app/static/dashboard/index.html` / `app.css` / `app.js` — V2 原型移植 + 真实数据绑定。
- Modify `app/main.py` — include router + mount static(仅追加,不改现有)。
- Create `scripts/register_dashboard_autostart.ps1` / `scripts/unregister_dashboard_autostart.ps1` — 独立登录自启(预览/-Apply 门控,不碰现有任务)。
- Create `docs/runbooks/STEP_1_DASHBOARD.md`;Modify `TESTING.md`(新增 dashboard 测试章节)。
- Test `tests/test_dashboard_projection.py`、`tests/test_dashboard_api.py`。

---

### Task 1: compute_overall_status 主状态判定(纯函数)

**Files:**
- Create: `app/services/dashboard_projection.py`
- Test: `tests/test_dashboard_projection.py`

**Interfaces:**
- Consumes: `collect_runtime_status()` 的输出 dict(形状见 `app/services/incremental_audit_status.py`)。
- Produces: `compute_overall_status(status: Mapping, *, recovering: int = 0) -> dict`,返回:
  `{"word": str, "color": "red|blue|green|gray", "explanation": str, "attention": list[dict], "stages": list[dict], "zs_summary": dict, "final_text": str, "final_state": "ok|pending|forced"}`
  attention 项:`{"level": "red|yellow", "tag": str, "title": str, "problem": str, "impact": str, "system_action": str, "suggestion": str}`
  stages 项:`{"key": "prep|zhaoshang|closeout|publish", "name": "数据准备|招商局处理|最终收口|最终出表", "state": "done|cur|wait|bad", "state_word": str, "num": str, "sub": list[tuple[str,str]]}`

- [ ] **Step 1: Write the failing test**

```python
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
    out = compute_overall_status(_status())
    assert "招商局" in out["final_text"] and "下一次 closeout" in out["final_text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py -v --basetemp=runtime\pytest-tmp\dash1`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.dashboard_projection'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/services/dashboard_projection.py
"""Read-only projections for the STEP_1 dashboard. Zero writes, zero network."""
from __future__ import annotations

from typing import Any, Mapping

_STAGES = (
    ("prep", "数据准备"),
    ("zhaoshang", "招商局处理"),
    ("closeout", "最终收口"),
    ("publish", "最终出表"),
)


def _attention_items(s: Mapping) -> list[dict]:
    r, c, m, h = s["remote"], s["crawl"], s["model"], s.get("remote_health", {})
    items: list[dict] = []
    if r["recovery_exhausted"]:
        items.append({"level": "red", "tag": "需人工", "title": "自动恢复已用尽",
            "problem": "招商局任务停滞且唯一一次自动重启(停用→启用)后仍无进展,系统已停止等待。",
            "impact": "关联商品将以“无采集结果”进入最终表(价格留空,不会伪造数据)。",
            "system_action": "已按规则收口,不再自动重试;证据链保留在任务详情。",
            "suggestion": "核对招商局后台该任务;接受缺价则无需操作,否则按人工恢复手册处理。"})
    if r["manual"]:
        items.append({"level": "red", "tag": "需人工", "title": "招商局任务需要人工检查",
            "problem": "任务进入 MANUAL_REQUIRED(身份冲突/写未确认/探测失败等)。",
            "impact": "该任务及其商品在人工处理前不会进入最终表。",
            "system_action": "系统已停止该任务的一切自动动作,等待人工。",
            "suggestion": "按人工恢复手册对应子命令处理;不要删除 durable 状态。"})
    if c["manual"]:
        items.append({"level": "red", "tag": "需人工", "title": "采集行需要人工处理",
            "problem": "部分商品采集进入人工/终态失败。", "impact": "对应商品价格留空进入最终表。",
            "system_action": "已停止自动重试。", "suggestion": "查看任务详情中的错误证据后人工处理。"})
    if c["unplanned"]:
        items.append({"level": "red", "tag": "需人工", "title": "存在未规划采集的必爬行",
            "problem": "快照要求爬取的行没有对应 crawl 计划。", "impact": "finalize 被禁止。",
            "system_action": "不会在未规划情况下出表。", "suggestion": "运行 doctor 核对,授权后再处理。"})
    if m["terminal"]:
        items.append({"level": "red", "tag": "需人工", "title": "模型校验终态失败",
            "problem": "模型重试预算耗尽后仍失败。", "impact": "对应商品缺模型结论。",
            "system_action": "不再自动重试。", "suggestion": "可按手册 requeue-model-transient/recover-model 处理。"})
    if r["unknown"] or c["unknown"]:
        items.append({"level": "yellow", "tag": "关注", "title": "任务提交结果待确认",
            "problem": "提交后结果未知,系统正按精确身份自动对账。",
            "impact": "对应商品暂无采集结果。", "system_action": "不会自动重发任务。",
            "suggestion": "等待自动对账;长时间未决则按手册执行 doctor 核查。"})
    if h.get("stall_detected"):
        items.append({"level": "yellow", "tag": "保护", "title": "远端保护:已暂停新建任务",
            "problem": "多个提优行未物化,断路器打开。", "impact": "暂停新建招商局任务,已有任务继续回收。",
            "system_action": "属保护机制非故障;探测恢复后自动闭合。",
            "suggestion": "无需操作;持续过久再按手册检查远端。"})
    return items


def _zs_summary(s: Mapping, recovering: int) -> dict:
    r = s["remote"]
    return {"executing": r["running"], "awaiting_harvest": r["awaiting_harvest"],
            "awaiting_priority": r["awaiting_priority"], "recovering": recovering,
            "manual": r["manual"] + r["recovery_exhausted"], "completed": r["completed"]}


def _stages(s: Mapping, finalized: bool) -> list[dict]:
    d, m, r, c = s["detail"], s["model"], s["remote"], s["crawl"]
    h = s.get("remote_health", {})
    prep_pending = d["pending"] + d["running"] + m["pending"] + m["running"]
    zs_uncleared = any(r[k] for k in ("intent", "pending", "running", "exported", "unknown", "manual", "recovery_exhausted")) \
        or any(c[k] for k in ("pending", "running", "unplanned", "unknown", "manual"))
    zs_bad = bool(r["manual"] or r["recovery_exhausted"] or c["manual"] or c["unplanned"])
    zs_yellow = bool(r["unknown"] or c["unknown"] or h.get("stall_detected"))
    stages = []
    stages.append({"key": "prep", "name": "数据准备", "state": "bad" if m["terminal"] else ("cur" if prep_pending else "done"),
        "state_word": "⚠ 异常" if m["terminal"] else ("● 进行中" if prep_pending else "✓ 完成"),
        "num": f"{d['completed'] + m['completed'] and (str(m['completed']) + ' / ' + str(s['counts']['unique_items'])) or '—'}",
        "sub": [("SaaS 发现", f"{s['counts']['discovered']} 行(新 {s['counts']['new']} / 变 {s['counts']['changed']})"),
                ("Detail", f"{d['completed']}/{s['counts']['unique_items']}"),
                ("模型校验", f"{m['completed']}/{s['counts']['unique_items']}")]})
    if finalized:
        zs_state, zs_word = "done", "✓ 完成"
    elif zs_bad:
        zs_state, zs_word = "bad", "⚠ 需人工"
    elif zs_yellow:
        zs_state, zs_word = "cur", "● 进行中(关注)"
    elif zs_uncleared or r["completed"]:
        zs_state, zs_word = "cur", "● 进行中"
    else:
        zs_state, zs_word = "wait", "○ 等待"
    stages.append({"key": "zhaoshang", "name": "招商局处理", "state": zs_state, "state_word": zs_word,
        "num": f"执行中 {r['running']} · 需人工 {r['manual'] + r['recovery_exhausted']}",
        "sub": [("等待提交", str(s['crawl']['waiting_submit'])), ("等待提优", str(r['awaiting_priority'])),
                ("执行中(含自动恢复)", str(r['running'])), ("等待回收", str(r['awaiting_harvest'])),
                ("已完成", str(r['completed'])), ("需人工/恢复用尽", str(r['manual'] + r['recovery_exhausted']))]})
    stages.append({"key": "closeout", "name": "最终收口", "state": "done" if finalized else ("cur" if s["final"]["frozen"] else "wait"),
        "state_word": "✓ 完成" if finalized else ("● 进行中" if s["final"]["frozen"] else "○ 等待"),
        "num": "18:00 口径", "sub": [("数据冻结", "已冻结" if s["final"]["frozen"] else "未开始(等待 18:00)")]})
    stages.append({"key": "publish", "name": "最终出表", "state": "done" if finalized else "wait",
        "state_word": "✓ 完成" if finalized else "○ 等待", "num": "阻塞清零后自动" if not finalized else "已生成",
        "sub": [("输出文件", s["final"]["output_path"] or "未生成")]})
    return stages


def _final_text(s: Mapping) -> tuple[str, str]:
    f = s["final"]
    if f["finalized"]:
        state = "forced" if f["forced"] else "ok"
        text = "今日最终 Excel 已生成" + ("(曾强制收尾,存在被接受的缺价)" if f["forced"] else "") + \
            (";文件存在校验:" + ("通过" if f["output_exists"] else "失败,请检查") + "。" if f["output_path"] else "。")
        return text, state
    b = f.get("blocker_summary") or {}
    parts = []
    mapping = [("remote_pending", "个招商局任务仍在执行/待回收"), ("remote_priority_pending", "个任务等待提优"),
               ("remote_unknown", "个任务提交结果待确认"), ("remote_manual", "项招商局任务需人工"),
               ("model_pending", "条模型处理未完成"), ("model_terminal", "条模型终态失败"),
               ("detail_pending", "条 Detail 未完成"), ("crawl_pending", "条采集未完成"),
               ("crawl_unplanned", "条必爬行未规划"), ("crawl_unknown", "条采集提交待确认"), ("crawl_manual", "条采集需人工")]
    for key, phrase in mapping:
        if b.get(key, 0) > 0:
            parts.append(f"{b[key]} {phrase}")
    if not parts:
        parts.append("全部阻塞已清零,等待下一次 closeout 自动出表")
    return "还不能生成最终 Excel:" + ";".join(parts) + "。阻塞清零后系统将在下一次 closeout 自动生成。", "pending"


def compute_overall_status(status: Mapping, *, recovering: int = 0) -> dict:
    if status.get("state") == "MISSING":
        return {"word": "今日无数据", "color": "gray",
                "explanation": "今天的审计尚未开始(工作日 09:00 后由计划任务自动启动;周末不运行)。",
                "attention": [], "stages": [], "zs_summary": {}, "final_text": "尚无最终表。", "final_state": "pending"}
    d, m, r, c, f = status["detail"], status["model"], status["remote"], status["crawl"], status["final"]
    prep_pending = d["pending"] + d["running"] + m["pending"] + m["running"]
    manual_needed = any((r["manual"], r["recovery_exhausted"], c["manual"], c["unplanned"], m["terminal"]))
    zs_uncleared = any(r[k] for k in ("intent", "pending", "running", "exported")) or bool(c["pending"] + c["running"])
    if manual_needed:
        word, color = "需要人工处理", "red"
    elif f["finalized"]:
        word, color = "今日结果已生成", "green"
    elif f["frozen"]:
        word, color = "正在最终收口", "blue"
    elif prep_pending == 0 and r["awaiting_harvest"] > 0:
        word, color = "正在回收招商局结果", "blue"
    elif prep_pending == 0 and (zs_uncleared or r["unknown"]):
        word, color = "正在等待招商局", "blue"
    else:
        word, color = "正在准备数据", "blue"
    attention = _attention_items(status)
    explanation = _explanation(status, attention, recovering, word)
    final_text, final_state = _final_text(status)
    return {"word": word, "color": color, "explanation": explanation, "attention": attention,
            "stages": _stages(status, f["finalized"]), "zs_summary": _zs_summary(status, recovering),
            "final_text": final_text, "final_state": final_state}


def _explanation(s: Mapping, attention: list[dict], recovering: int, word: str) -> str:
    counts, d, m, r = s["counts"], s["detail"], s["model"], s["remote"]
    parts: list[str] = []
    if word == "需要人工处理":
        tops = ";".join(a["title"] for a in attention if a["level"] == "red") or "存在需要人工处理的问题"
        parts.append(tops)
    else:
        parts.append("当前无需人工处理" if not attention else
                     ";".join(a["title"] for a in attention[:3]))
    if word in ("正在等待招商局", "正在回收招商局结果"):
        seg = f"{counts['discovered']} 条数据的 Detail 与模型处理已全部完成,{r['running']} 个招商局远端任务执行中"
        if recovering:
            seg += f",其中 {recovering} 个正在自动恢复"
        parts.append(seg + "。")
    elif word == "正在准备数据":
        parts.append(f"已发现 {counts['discovered']} 行,Detail {d['completed']}/{counts['unique_items']},模型 {m['completed']}/{counts['unique_items']}。")
    elif word == "正在最终收口":
        parts.append("数据已按 18:00 口径冻结,系统正在收尾并等待阻塞清零。")
    elif word == "今日结果已生成":
        parts.append("今日流程已全部完成。")
    if len(attention) > 3:
        parts.append(f"需要关注共 {len(attention)} 项。")
    return "".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py -v --basetemp=runtime\pytest-tmp\dash1`
Expected: PASS(全部用例)

- [ ] **Step 5: Commit**

```bash
git add app/services/dashboard_projection.py tests/test_dashboard_projection.py
git commit -m "feat(dashboard): overall status projection with attention-first rules"
```

---

### Task 2: 招商局任务列表与恢复时间线投影

**Files:**
- Modify: `app/services/dashboard_projection.py`(追加)
- Test: `tests/test_dashboard_projection.py`(追加)

**Interfaces:**
- Consumes: `daily_audit_store` 的 remote_jobs/crawl_work 表结构;`remote_running_recovery.decode_recovery_evidence`。
- Produces:
  `project_remote_jobs(state_path: Path, business_date: str) -> list[dict]`(每项含 `id, mall_key, task_name, state, state_cn, pill("run|ok|wait|warn|err"), subtasks({"finished","total"}|None), created_at, updated_at, harvest_attempts, recovering: bool`);
  `project_recovery_timeline(job_row: Mapping) -> dict`(`{"phase": "正常采集|观察中(≠异常)|已证明停滞|自动恢复中|恢复成功|需要人工处理|恢复已用尽", "observations": list[dict], "restart": dict|None, "probe_health": dict|None}`)。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard_projection.py — 追加
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
      INSERT INTO remote_jobs(id, run_id, task_name, mall_key, state, remote_identity, harvest_attempts,
        recovery_attempts, recovery_evidence_json, created_at, updated_at)
        VALUES (12, 1, 'cnnc_…_1015', '京东商城', 'RUNNING', 'jd|88231|T1', 9, 1,
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py -k remote_jobs -v --basetemp=runtime\pytest-tmp\dash2`
Expected: FAIL — `ImportError: cannot import name 'project_remote_jobs'`

- [ ] **Step 3: Write minimal implementation(追加到 dashboard_projection.py)**

```python
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.services.remote_running_recovery import (
    RUNNING_PROBE_MIN_HARVEST_ATTEMPTS, RUNNING_PROBE_MIN_WALL_SECONDS,
    decode_recovery_evidence,
)

_STATE_CN = {
    "SUBMIT_INTENT": ("等待提交", "wait"), "PRIORITY_PENDING": ("等待提优", "wait"),
    "SUBMITTED": ("等待执行", "wait"), "RUNNING": ("执行中", "run"), "EXPORTED": ("等待回收", "wait"),
    "COMPLETED": ("已完成", "ok"), "SUBMIT_UNKNOWN": ("提交结果待确认", "warn"),
    "MANUAL_REQUIRED": ("需要人工检查", "err"), "RECOVERY_EXHAUSTED": ("自动恢复已用尽", "err"),
}


def _parse_ts(value) -> datetime | None:
    try:
        m = datetime.fromisoformat(str(value or ""))
        return m if m.tzinfo else m.replace(tzinfo=UTC)
    except ValueError:
        return None


def project_remote_jobs(state_path: Path, business_date: str) -> list[dict]:
    uri = f"file:{Path(state_path).resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True); con.row_factory = sqlite3.Row
    try:
        run = con.execute("SELECT id FROM daily_runs WHERE business_date=?", (business_date,)).fetchone()
        if run is None:
            return []
        run_id = int(run["id"])
        counts = {int(r["remote_job_id"] or 0): (int(r["total"]), int(r["success"]))
                  for r in con.execute(
                      """SELECT remote_job_id, COUNT(*) AS total,
                                SUM(CASE WHEN state='SUCCESS' THEN 1 ELSE 0 END) AS success
                         FROM crawl_work WHERE run_id=? AND remote_job_id IS NOT NULL GROUP BY remote_job_id""",
                      (run_id,))}
        jobs = []
        for row in con.execute("SELECT * FROM remote_jobs WHERE run_id=? ORDER BY id", (run_id,)):
            evidence = decode_recovery_evidence(row["recovery_evidence_json"])
            restart = evidence.get("restart")
            recovering = str(row["state"]) == "RUNNING" and isinstance(restart, dict) \
                and str(restart.get("phase")) == "ENABLE_CONFIRMED"
            obs = evidence.get("observations") or []
            subtasks = None
            for o in obs:
                sig = o.get("signature")
                if isinstance(sig, list) and len(sig) == 6:
                    subtasks = {"finished": int(sig[5]), "total": int(sig[0])}
            cn, pill = _STATE_CN.get(str(row["state"]), (str(row["state"]), "warn"))
            jobs.append({
                "id": int(row["id"]), "mall_key": str(row["mall_key"] or ""),
                "task_name": str(row["task_name"]), "state": str(row["state"]),
                "state_cn": (cn + " · 自动恢复中") if recovering else cn,
                "pill": "run" if recovering else pill, "subtasks": subtasks,
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "harvest_attempts": int(row["harvest_attempts"] or 0), "recovering": recovering,
                "crawl_total": counts.get(int(row["id"]), (0, 0))[0],
                "crawl_success": counts.get(int(row["id"]), (0, 0))[1],
                "error_text": row["error_text"], "remote_identity": row["remote_identity"],
                "recovery_evidence_json": row["recovery_evidence_json"],
            })
        return jobs
    finally:
        con.close()


def project_recovery_timeline(job_row: Mapping) -> dict:
    evidence = decode_recovery_evidence(job_row.get("recovery_evidence_json"))
    state = str(job_row.get("state") or "")
    created = _parse_ts(job_row.get("created_at"))
    wall = (datetime.now(UTC) - created).total_seconds() if created else 0.0
    attempts = int(job_row.get("harvest_attempts") or 0)
    restart = evidence.get("restart")
    if state == "RECOVERY_EXHAUSTED":
        phase = "恢复已用尽"
    elif state == "MANUAL_REQUIRED":
        phase = "需要人工处理"
    elif state == "RUNNING" and isinstance(restart, dict) and str(restart.get("phase")) == "ENABLE_CONFIRMED":
        phase = "自动恢复中"
    elif state == "RUNNING" and attempts >= RUNNING_PROBE_MIN_HARVEST_ATTEMPTS \
            and wall >= RUNNING_PROBE_MIN_WALL_SECONDS:
        phase = "观察中(≠异常)"
    else:
        phase = "正常采集"
    return {"phase": phase, "observations": evidence.get("observations") or [],
            "restart": restart, "probe_health": evidence.get("probe_health")}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py -v --basetemp=runtime\pytest-tmp\dash2`
Expected: PASS(Task 1 + Task 2 全部)

- [ ] **Step 5: Commit**

```bash
git add app/services/dashboard_projection.py tests/test_dashboard_projection.py
git commit -m "feat(dashboard): remote job list and recovery timeline projections"
```

---

### Task 3: 近 N 天概览与 Scheduler 只读投影

**Files:**
- Modify: `app/services/dashboard_projection.py`(追加)
- Test: `tests/test_dashboard_projection.py`(追加)

**Interfaces:**
- Produces:
  `project_recent_days(runtime_root: Path, n: int = 14) -> list[dict]`(`{"date": str, "class": "ok|warn|err|none"}`;V1 首页不渲染但接口保留);
  `project_scheduler_status() -> dict`(`{"available": bool, "running": bool|None, "last_run": str|None, "last_result": str|None, "last_result_cn": str|None, "next_run": str|None}`;267009/0x41301→"当前计划任务正在运行")。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard_projection.py — 追加
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
    monkeypatch.setattr(dp, "_run_schtasks_query", lambda: None)
    out = dp.project_scheduler_status()
    assert out == {"available": False}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py -k "recent_days or translate or scheduler" -v --basetemp=runtime\pytest-tmp\dash3`
Expected: FAIL — ImportError

- [ ] **Step 3: Write minimal implementation(追加)**

```python
import json as _json
import subprocess as _subprocess
import xml.etree.ElementTree as _ET
from datetime import date as _date, timedelta as _timedelta

SCHEDULER_TASK_NAME = "CNNC-PriceAudit-Incremental"
_RUNNING_CODES = {267009, 0x41301}


def project_recent_days(runtime_root: Path, n: int = 14, *, today: str | None = None) -> list[dict]:
    root = Path(runtime_root)
    start = _date.fromisoformat(today) if today else _date.today()
    out: list[dict] = []
    for i in range(n):
        day = (start - _timedelta(days=i)).isoformat()
        status_file = root / day / "daily_status.json"
        if not status_file.is_file():
            out.append({"date": day, "class": "none"}); continue
        try:
            data = _json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append({"date": day, "class": "none"}); continue
        final = data.get("final") or {}
        if final.get("finalized"):
            cls = "warn" if final.get("forced") or (data.get("crawl") or {}).get("no_result", 0) else "ok"
        else:
            cls = "err"
        out.append({"date": day, "class": cls})
    out.reverse()
    return out


def _translate_task_result(code) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return f"结果码未知:{code}"
    if code in _RUNNING_CODES:
        return "当前计划任务正在运行"
    if code == 0:
        return "上次运行成功"
    if code == 0x41306:
        return "上次运行被终止"
    return f"上次运行结果码 {code}(未知,详情见技术说明)"


def _run_schtasks_query() -> bytes | None:
    try:
        proc = _subprocess.run(
            ["schtasks", "/query", "/tn", SCHEDULER_TASK_NAME, "/xml"],
            capture_output=True, timeout=10, check=False)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def project_scheduler_status() -> dict:
    raw = _run_schtasks_query()
    if raw is None:
        return {"available": False}
    try:
        root = _ET.fromstring(raw)
        ns = {"t": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {"t": ""}
        def _text(tag: str) -> str | None:
            el = root.find(f".//t:{tag}", ns)
            return el.text.strip() if el is not None and el.text else None
        result = _text("LastTaskResult")
        code = int(result, 16) if result and result.lower().startswith("0x") else (int(result) if result and result.isdigit() else None)
        return {"available": True,
                "running": code in _RUNNING_CODES if code is not None else None,
                "last_run": _text("LastRunTime"),
                "last_result": result,
                "last_result_cn": _translate_task_result(code) if code is not None else None,
                "next_run": _text("NextRunTime")}
    except (_ET.ParseError, ValueError, TypeError):
        return {"available": False}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py -v --basetemp=runtime\pytest-tmp\dash3`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/dashboard_projection.py tests/test_dashboard_projection.py
git commit -m "feat(dashboard): recent-days and read-only scheduler projections"
```

---

### Task 4: summary 组装与 fingerprint

**Files:**
- Modify: `app/services/dashboard_projection.py`(追加)
- Test: `tests/test_dashboard_projection.py`(追加)

**Interfaces:**
- Consumes: `incremental_audit_status.collect_runtime_status`、`incremental_audit_runtime.runtime_paths`、Task 1-3 的函数。
- Produces: `build_dashboard_summary(business_date: str, runtime_root: Path | str) -> dict`,键:`generated_at, fingerprint, overall, jobs, timelines(仅异常/恢复中任务的 project_recovery_timeline), recent_days, scheduler, status(collect_runtime_status 原样,技术详情用)`;`fingerprint_payload(payload: dict) -> str`(对去掉 generated_at/fingerprint 后的部分做 sha256)。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard_projection.py — 追加
from app.services.dashboard_projection import build_dashboard_summary, fingerprint_payload


def test_build_summary_end_to_end(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "day")  # Task 2 的建库工具
    import json as _j
    from app.services import dashboard_projection as dp
    monkeypatch.setattr(dp, "collect_runtime_status", lambda **kw: _status())
    monkeypatch.setattr(dp, "runtime_paths", lambda bd, root=None: type("P", (), {
        "state_path": db, "run_dir": db.parent})())
    monkeypatch.setattr(dp, "project_scheduler_status", lambda: {"available": False})
    monkeypatch.setattr(dp, "project_recent_days", lambda root, n=14, **kw: [])
    out = build_dashboard_summary("2026-08-26", tmp_path)
    assert out["overall"]["word"] == "需要人工处理"
    assert any(j["mall_key"] == "京东商城" for j in out["jobs"])
    assert out["timelines"] and "12" in out["timelines"]
    assert "fingerprint" in out and len(out["fingerprint"]) == 64
    again = build_dashboard_summary("2026-08-26", tmp_path)
    assert again["fingerprint"] == out["fingerprint"]  # 同数据同指纹(generated_at 除外)


def test_fingerprint_ignores_generated_at():
    a = {"generated_at": "t1", "fingerprint": "", "data": {"x": 1}}
    b = {"generated_at": "t2", "fingerprint": "", "data": {"x": 1}}
    assert fingerprint_payload(a) == fingerprint_payload(b)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py -k summary -v --basetemp=runtime\pytest-tmp\dash4`
Expected: FAIL — ImportError

- [ ] **Step 3: Write minimal implementation(追加)**

```python
import hashlib as _hashlib
from datetime import UTC as _UTC, datetime as _datetime

from app.services.incremental_audit_runtime import DEFAULT_RUNTIME_ROOT, runtime_paths
from app.services.incremental_audit_status import collect_runtime_status


def fingerprint_payload(payload: Mapping) -> str:
    body = {k: v for k, v in payload.items() if k not in ("generated_at", "fingerprint")}
    return _hashlib.sha256(
        _json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def build_dashboard_summary(business_date: str, runtime_root: Path | str = DEFAULT_RUNTIME_ROOT) -> dict:
    paths = runtime_paths(business_date, runtime_root)
    status = collect_runtime_status(business_date=business_date, runtime_root=runtime_root)
    jobs = project_remote_jobs(paths.state_path, business_date) if paths.state_path.is_file() else []
    recovering = sum(1 for j in jobs if j["recovering"])
    timelines: dict[str, dict] = {}
    for j in jobs:
        if j["recovering"] or j["pill"] in ("err", "warn"):
            timelines[str(j["id"])] = project_recovery_timeline(j)
    payload = {
        "generated_at": _datetime.now(_UTC).isoformat(timespec="seconds"),
        "overall": compute_overall_status(status, recovering=recovering),
        "jobs": [{k: v for k, v in j.items() if k != "recovery_evidence_json"} for j in jobs],
        "timelines": timelines,
        "recent_days": project_recent_days(runtime_root, n=14),
        "scheduler": project_scheduler_status(),
        "status": status,
    }
    payload["fingerprint"] = fingerprint_payload(payload)
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py -v --basetemp=runtime\pytest-tmp\dash4`
Expected: PASS(全文件)

- [ ] **Step 5: Commit**

```bash
git add app/services/dashboard_projection.py tests/test_dashboard_projection.py
git commit -m "feat(dashboard): summary assembly with content fingerprint"
```

---

### Task 5: FastAPI 路由与挂载

**Files:**
- Create: `app/dashboard_api.py`
- Modify: `app/main.py`(文件末尾追加 include_router 与静态挂载)
- Test: `tests/test_dashboard_api.py`

**Interfaces:**
- Consumes: Task 4 `build_dashboard_summary`;`incremental_audit_runtime.business_date_in_shanghai`。
- Produces: `router`(APIRouter prefix="/dashboard");`GET /dashboard`(FileResponse index.html);`GET /dashboard/api/summary?date=&`(200 JSON+ETag / 304 / 400 / 503)。静态挂载 `/dashboard/static`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard_api.py
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


def test_summary_400_bad_date(client, monkeypatch):
    r = client.get("/dashboard/api/summary?date=2026/08/26")
    assert r.status_code == 400
    assert "YYYY-MM-DD" in r.json()["detail"]["message"]


def test_summary_503_on_read_failure(client, monkeypatch):
    from app.services import dashboard_projection as dp
    def boom(**kw):
        raise dp.__import__("sqlite3").DatabaseError("locked")
    monkeypatch.setattr(dp, "build_dashboard_summary", boom)
    from app import dashboard_api
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_api.py -v --basetemp=runtime\pytest-tmp\dash5`
Expected: FAIL — `ImportError`(app.dashboard_api 不存在;其中 page_served 在静态文件缺失时也应红)

- [ ] **Step 3: Write minimal implementation**

```python
# app/dashboard_api.py
"""Read-only STEP_1 dashboard routes. GET only, zero business writes."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app.services.dashboard_projection import build_dashboard_summary

router = APIRouter(prefix="/dashboard", tags=["step1-dashboard"])
_STATIC_DIR = Path(__file__).resolve().parent / "static" / "dashboard"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _today() -> str:
    from app.services.incremental_audit_runtime import business_date_in_shanghai
    return business_date_in_shanghai()


@router.get("/", include_in_schema=True)
def dashboard_index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@router.get("/api/summary")
def dashboard_summary(request: Request, date: str = Query(default=None)) -> Response:
    business_date = date or _today()
    if not _DATE_RE.match(business_date):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_BUSINESS_DATE", "message": "business_date 必须为 YYYY-MM-DD"})
    try:
        payload = build_dashboard_summary(business_date)
    except Exception as exc:  # 只读投影失败统一降级,不暴露堆栈
        raise HTTPException(status_code=503, detail={
            "error_code": "STATUS_READ_FAILED", "message": "状态读取失败,请稍后重试",
            "type": type(exc).__name__}) from exc
    etag = f'"{payload["fingerprint"]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(payload, headers={"ETag": etag})
```

`app/main.py` 末尾追加(不改任何现有行):

```python
from pathlib import Path as _Path
from starlette.staticfiles import StaticFiles as _StaticFiles
from app.dashboard_api import router as _dashboard_router

app.include_router(_dashboard_router)
app.mount("/dashboard/static",
          _StaticFiles(directory=_Path(__file__).resolve().parent / "static" / "dashboard"),
          name="dashboard-static")
```

同时创建占位 `app/static/dashboard/index.html`(Task 6 完成真实页面,本 Task 先放最小页面使测试可过):

```html
<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8"><title>中核 STEP_1 每日价格审计</title>
<body>中核 STEP_1 每日价格审计驾驶舱(加载中)</body></html>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_api.py -v --basetemp=runtime\pytest-tmp\dash5`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/dashboard_api.py app/main.py app/static/dashboard/index.html tests/test_dashboard_api.py
git commit -m "feat(dashboard): read-only FastAPI router with ETag/304 and error contract"
```

---

### Task 6: 前端单页(V2 原型移植 + 真实数据绑定)

**Files:**
- Create: `app/static/dashboard/index.html`(替换占位)
- Create: `app/static/dashboard/app.css`
- Create: `app/static/dashboard/app.js`
- Test: `tests/test_dashboard_api.py`(追加)

**Interfaces:**
- Consumes: `GET /dashboard/api/summary` 响应结构(Task 4 payload)。
- Produces: 可运行单页;`app.js` 暴露 `window.Dashboards` 命名空间(`start()`、`stop()`,便于测试/调试)。

移植来源(权威视觉规格):`E:\.A中核运维\dashboard-design\prototype\v2\index.html`(已获用户批准的 V2 原型)。移植修改清单(全部执行):
1. 删除:`proto-banner`、`模拟断线` 按钮、`切换演示场景` 按钮与 `sc-good/sc-bad` 双场景 DOM;
2. 删除 `近14天` 相关(若原型残留);技术详情保留英文原值渲染;
3. body 标题改"中核 STEP_1 每日价格审计";`data-` 钩子:`#hero .word/.explain/.meta`、`#attn`、`#stages`、`#zs`、`#final`、`#jobs-table tbody`、`#drawer`、`#tech pre`、`#conn-lost`、`#live`;
4. CSS 原样移植为 `app.css`(五色令牌不变)。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard_api.py — 追加
def test_static_assets_served(client):
    for name in ("index.html", "app.css", "app.js"):
        r = client.get(f"/dashboard/static/{name}")
        assert r.status_code == 200, name
    css = client.get("/dashboard/static/app.css").text
    js = client.get("/dashboard/static/app.js").text
    assert "--ok" in css and "--err" in css          # 五色令牌
    assert "visibilitychange" in js                   # 不可见暂停
    assert "If-None-Match" in js                      # 304 支持
    assert "cdn" not in css.lower() and "http://" not in css  # 零外链
```

- [ ] **Step 2: Run test to verify it fails**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_api.py -k assets -v --basetemp=runtime\pytest-tmp\dash6`
Expected: FAIL(app.js 404 / 断言失败)

- [ ] **Step 3: Write implementation**

`app.js` 核心骨架(渲染函数按 payload 结构逐一填充 HTML,与原型 DOM 同构):

```javascript
// app/static/dashboard/app.js
(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const ACTIVE_MS = 5000, IDLE_MS = 30000;
  let timer = null, etag = null, lastGenerated = null;

  function fmtAgo(iso) {
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    return s < 60 ? `${Math.round(s)} 秒前` : `${Math.round(s / 60)} 分钟前`;
  }
  function setLive(ok, text) {
    $("#live").textContent = text;
    $("#live-dot").className = "dot" + (ok ? "" : " err");
  }
  async function poll() {
    try {
      const headers = {};
      if (etag) headers["If-None-Match"] = etag;
      const res = await fetch("/dashboard/api/summary", { headers });
      if (res.status === 304) { onOk(); return; }
      if (!res.ok) throw new Error(String(res.status));
      etag = res.headers.get("ETag");
      const payload = await res.json();
      render(payload);
      onOk();
    } catch (e) {
      document.body.classList.add("offline");   // 断线横幅 + 旧数据保留
    }
  }
  function onOk() {
    document.body.classList.remove("offline");
    if (lastGenerated) setLive(true, "实时 · 最后更新 " + fmtAgo(lastGenerated));
  }
  function schedule(payload) {
    clearTimeout(timer);
    const st = payload.status || {};
    const finalized = st.final && st.final.finalized;
    const active = (st.production_writer && st.production_writer.active) ||
      (st.final && st.final.computed_pending > 0 && !finalized);
    const interval = finalized ? null : (active ? ACTIVE_MS : IDLE_MS);
    if (interval) timer = setTimeout(() => poll().then(renderLoop), interval);
  }
  function renderLoop() { /* poll() 内已 render;此处仅续排 */ }
  function render(p) {
    lastGenerated = p.generated_at;
    renderHero(p.overall); renderAttention(p.overall.attention);
    renderStages(p.overall.stages); renderZs(p.overall.zs_summary);
    renderFinal(p.overall); renderJobs(p.jobs); window._timelines = p.timelines;
    renderTech(p.status); schedule(p);
  }
  function renderHero(o) {
    const h = $("#hero");
    h.className = "hero " + o.color;
    $("#hero .word").textContent = (o.color === "red" ? "⚠ " : o.color === "blue" ? "● " :
      o.color === "green" ? "✓ " : "") + o.word;
    $("#hero .explain").textContent = o.explanation;
  }
  function renderAttention(items) {
    const box = $("#attn");
    if (!items || !items.length) {
      box.classList.remove("has");
      box.querySelector("ol").innerHTML = "";
      box.querySelector(".empty").style.display = "";
      return;
    }
    box.classList.add("has");
    box.querySelector(".empty").style.display = "none";
    box.querySelector("ol").innerHTML = items.slice(0, 3).map((a) => `
      <li class="${a.level}">
        <div class="t"><span class="tag ${a.level}">${a.tag}</span>${a.title}</div>
        <div class="row"><b>问题</b> ${a.problem}</div>
        <div class="row"><b>影响</b> ${a.impact}</div>
        <div class="row"><b>系统动作</b> ${a.system_action}</div>
        <div class="row"><b>建议</b> ${a.suggestion}</div>
      </li>`).join("") +
      (items.length > 3 ? `<li class="t" style="color:var(--muted)">其余 ${items.length - 3} 项,详见技术详情</li>` : "");
  }
  function renderStages(stages) {
    $("#stages").innerHTML = (stages || []).map((s) => `
      <div class="stage ${s.state}" data-key="${s.key}">
        <div class="k">${s.name}</div>
        <div class="st">${s.state_word}</div><div class="num">${s.num}</div>
        <div class="sub">${(s.sub || []).map(([k, v]) =>
          `<div><span>${k}</span><span>${v}</span></div>`).join("")}</div>
      </div>`).join("");
    document.querySelectorAll("#stages .stage").forEach((el) =>
      el.addEventListener("click", () => el.classList.toggle("open")));
  }
  function renderZs(z) {
    if (!z || !Object.keys(z).length) { $("#zs").style.display = "none"; return; }
    $("#zs").style.display = "";
    const map = [["executing", "执行中", "run"], ["awaiting_harvest", "等待回收", ""],
      ["awaiting_priority", "等待提优", ""], ["recovering", "自动恢复中", "run"],
      ["manual", "需要人工", "err"], ["completed", "已完成", "ok"]];
    $("#zs .counts").innerHTML = map.map(([k, lab, cls]) =>
      `<span><span class="lab">${lab}</span><b class="${cls}">${z[k] ?? 0}</b></span>`).join("");
  }
  function renderFinal(o) {
    const f = $("#final");
    f.className = "final" + (o.final_state === "ok" ? " ok" : "");
    f.querySelector(".why").textContent = o.final_text;
  }
  function renderJobs(jobs) {
    $("#jobs-table tbody").innerHTML = (jobs || []).map((j) => `
      <tr class="row" data-id="${j.id}">
        <td><span class="sw pill-${j.pill}"></span>${j.mall_key}</td>
        <td class="mono">#${j.id} · ${j.task_name.slice(0, 18)}…</td>
        <td><span class="pill ${j.pill}">${j.state === "RUNNING" ? "●" :
          j.pill === "ok" ? "✓" : j.pill === "err" ? "✕" : j.pill === "warn" ? "?" : "○"} ${j.state_cn}</span></td>
        <td class="num">${j.subtasks ? j.subtasks.finished + " / " + j.subtasks.total : "—"}</td>
        <td class="num">${j.harvest_attempts} 次</td>
        <td class="num">${j.updated_at ? fmtAgo(j.updated_at) : "—"}</td>
      </tr>`).join("");
    document.querySelectorAll("#jobs-table tr.row").forEach((tr) =>
      tr.addEventListener("click", () => openDrawer(tr.dataset.id, jobs)));
  }
  function openDrawer(id, jobs) {
    const j = jobs.find((x) => String(x.id) === String(id));
    const tl = (window._timelines || {})[String(id)] || {};
    $("#drawer").classList.add("show");
    $("#drawer .kv").innerHTML = `
      <b>任务编号</b><span class="mono">#${j.id} · ${j.task_name}</span>
      <b>远端标识</b><span class="mono">${j.remote_identity || "—"}</span>
      <b>当前状态</b><span>${j.state_cn}</span>
      <b>采集轮次</b><span>harvest ${j.harvest_attempts} 次</span>
      <b>恢复阶段</b><span>${tl.phase || "—"}</span>
      <b>子任务进度</b><span>${j.subtasks ? j.subtasks.finished + " / " + j.subtasks.total : "—"}</span>`;
    $("#drawer .tl").innerHTML = (tl.observations || []).slice(-8).map((o) => `
      <li><time>${(o.observed_at || "").slice(11, 16)}</time>进度 ${o.signature ? o.signature[5] + "/" + o.signature[0] : "—"} · 导出 ${o.export_count === 0 ? "无结果" : "有结果"}</li>`).join("");
    $("#drawer .note").textContent = "观察与探测不代表异常;仅“需要人工处理/恢复已用尽”需要人工介入。";
  }
  function renderTech(status) {
    $("#tech pre").textContent = JSON.stringify(status, null, 2);
  }
  function start() {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") { poll(); }  // 回前台立即补刷
    });
    $("#refresh").addEventListener("click", () => poll());
    $("#jobs-toggle").addEventListener("click", () => {
      const m = $("#more"); m.classList.toggle("show");
      $("#jobs-toggle").textContent = m.classList.contains("show") ? "收起任务列表 ↑" : "查看全部招商局任务 ↓";
    });
    poll();
  }
  window.Dashboards = { start, stop: () => clearTimeout(timer) };
  document.addEventListener("DOMContentLoaded", start);
})();
```

`index.html`:以 V2 原型为底,按上述修改清单裁剪,DOM 钩子为
`#hero(.word/.explain)`、`#live/#live-dot`、`#attn(.empty/ol)`、`#stages`、`#zs .counts`、`#final .why`、`#more(#jobs-table/#drawer/#tech)`、`#conn-lost`、`#refresh`、`#jobs-toggle`;`<link rel="stylesheet" href="/dashboard/static/app.css">`;`<script src="/dashboard/static/app.js" defer></script>`。`app.css` 从 V2 原型 `<style>` 原样迁移并删除演示专用类(`.proto-banner`、场景切换相关)。

- [ ] **Step 4: Run test to verify it passes**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_api.py -v --basetemp=runtime\pytest-tmp\dash6`
Expected: PASS

- [ ] **Step 5: Manual smoke(生产 runtime 只读)**

```powershell
E:\Python314\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
# 浏览器打开 http://127.0.0.1:8765/dashboard 核对:四问首屏可答、任务表展开、抽屉、断线(Ctrl+停服)横幅
```
Expected: 页面正常,`runtime\` 无任何文件新增/修改(dir /t:w 对比前后)。

- [ ] **Step 6: Commit**

```bash
git add app/static/dashboard
git commit -m "feat(dashboard): V2 single-page frontend with conditional polling and stale UX"
```

---

### Task 7: 登录自启脚本与文档

**Files:**
- Create: `scripts/register_dashboard_autostart.ps1`
- Create: `scripts/unregister_dashboard_autostart.ps1`
- Create: `docs/runbooks/STEP_1_DASHBOARD.md`
- Modify: `TESTING.md`(新增 dashboard 章节)
- Test: `tests/test_dashboard_api.py`(追加)

**Interfaces:**
- Produces: 预览默认、`-Apply` 才注册/删除;任务名 `CNNC-Dashboard-ReadOnly`;动作 = 在仓库根以隐藏窗口启动 `E:\Python314\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765`;绝不引用/修改 `CNNC-PriceAudit-Incremental`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard_api.py — 追加
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_autostart_scripts_exist_with_apply_gate():
    reg = (ROOT / "scripts" / "register_dashboard_autostart.ps1").read_text(encoding="utf-8")
    un = (ROOT / "scripts" / "unregister_dashboard_autostart.ps1").read_text(encoding="utf-8")
    for text in (reg, un):
        assert "CNNC-Dashboard-ReadOnly" in text
        assert "CNNC-PriceAudit-Incremental" not in text        # 不触碰现有任务
        assert "127.0.0.1" in text and "8765" in text
    assert "-Apply" in reg and "Preview" in reg                  # 注册走预览/-Apply 门控
    assert "-Apply" in un


def test_dashboard_runbook_exists():
    text = (ROOT / "docs" / "runbooks" / "STEP_1_DASHBOARD.md").read_text(encoding="utf-8")
    assert "只读" in text and "127.0.0.1" in text and "8765" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_api.py -k autostart_or_runbook -v --basetemp=runtime\pytest-tmp\dash7`(用 `-k "autostart or runbook"`)
Expected: FAIL(FileNotFoundError)

- [ ] **Step 3: Write implementation**

`register_dashboard_autostart.ps1`(要点;完整脚本按仓库现有 register 脚本风格编写):

```powershell
[CmdletBinding()]
param(
  [string] $RepoRoot = "",
  [string] $PythonExe = "E:\Python314\python.exe",
  [int] $Port = 8765,
  [switch] $Apply
)
$ErrorActionPreference = "Stop"
if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
$taskName = "CNNC-Dashboard-ReadOnly"
$action = "powershell.exe -NoProfile -WindowStyle Hidden -Command `"Set-Location '$RepoRoot'; & '$PythonExe' -m uvicorn app.main:app --host 127.0.0.1 --port $Port`""
$preview = [pscustomobject]@{ TaskName = $taskName; Action = $action; Trigger = "登录时(当前用户)"; Apply = [bool]$Apply }
$preview | ConvertTo-Json -Depth 3
if (-not $Apply) { Write-Host "预览模式:未做任何更改。加 -Apply 才注册。" ; exit 0 }
schtasks /Create /TN $taskName /SC ONLOGON /TR $action /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "schtasks create failed: $LASTEXITCODE" }
Write-Host "已注册 $taskName(未触碰 CNNC-PriceAudit-Incremental)。"
```

`unregister_dashboard_autostart.ps1`:同结构,`schtasks /End /TN` + `schtasks /Delete /TN ... /F`,同样 `-Apply` 门控与预览。

`docs/runbooks/STEP_1_DASHBOARD.md`:记录 用途/只读红线(零写入、GET-only)、启动方式(手动 uvicorn 命令)、自启注册/卸载(预览→-Apply)、绑定 127.0.0.1:8765、排障(503=状态读取失败;断线横幅含义;Scheduler 不可用降级)。
`TESTING.md`:新增"Dashboard 测试"一节(两条测试文件、运行命令含 --basetemp、fixture 原则)。

- [ ] **Step 4: Run test to verify it passes**

Run: `E:\Python314\python.exe -m pytest tests\test_dashboard_api.py -k "autostart or runbook" -v --basetemp=runtime\pytest-tmp\dash7`
Expected: PASS

- [ ] **Step 5: Validate PowerShell syntax(真解析器)**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\13013\.zcode\skills\windows-shell-verified\scripts\Test-PowerShellFile.ps1 -Path scripts\register_dashboard_autostart.ps1
```
Expected: 0 parse errors(unregister 同)

- [ ] **Step 6: Commit**

```bash
git add scripts/register_dashboard_autostart.ps1 scripts/unregister_dashboard_autostart.ps1 docs/runbooks/STEP_1_DASHBOARD.md TESTING.md tests/test_dashboard_api.py
git commit -m "feat(dashboard): logon autostart scripts (preview/-Apply gated) and runbook"
```

---

### Task 8: 全量相关套件与交付证据

**Files:**
- 无新文件(验证任务)

**Interfaces:**
- Consumes: 全部前序任务。

- [ ] **Step 1: Fresh full relevant suite**

```powershell
E:\Python314\python.exe -m pytest tests\test_dashboard_projection.py tests\test_dashboard_api.py `
  tests\test_incremental_audit_runtime.py tests\test_incremental_price_audit.py `
  tests\test_incremental_price_audit_finalize.py tests\test_incremental_audit_materializer.py `
  tests\test_incremental_audit_status.py -q --basetemp=runtime\pytest-tmp\dashfull
```
Expected: 全绿(既有套件不受影响)

- [ ] **Step 2: 零写入复核**

启动 uvicorn 浏览 10 分钟(含任务表/抽屉/断线恢复),`git -C E:\.A中核运维\cnnc-dashboard-dev status` 无 runtime 变更;对生产 `runtime\incremental_price_audit` 目录做前后 `dir /s /t:w` 快照对比,无新增/修改。

- [ ] **Step 3: 汇报并停止**

报告:HEAD SHA、diff 统计、测试输出、文件清单、设计证据(spec/原型路径)。**不 merge、不部署、不 push prod、不改现有 Scheduler。** 用户明确要求时才 push dev 分支。

---

## Self-Review 记录(已执行)

1. **Spec coverage**:spec §3 架构→Task 4/5;§4.1 首屏预算→Task 6;§4.2 优先级→Task 1;§4.3/4.4 披露→Task 1/6;§4.5 五色→Task 6;§5 投影→Task 1-4;§6 API→Task 5;§7 测试→各 Task+8;§8 自启→Task 7;§9 验收→Task 6/8。无缺口。
2. **Placeholder scan**:Task 6 前端以"V2 原型移植+ enumerated 修改清单+完整 app.js"给出,原型文件为已批准的权威视觉规格,非 TBD;其余任务均含实码。
3. **Type consistency**:`compute_overall_status/project_remote_jobs/project_recovery_timeline/project_recent_days/project_scheduler_status/build_dashboard_summary/fingerprint_payload` 签名在各 Task 间一致;`_run_schtasks_query` 被 Task 3 测试 monkeypatch,已在实现中作为模块级函数存在。
