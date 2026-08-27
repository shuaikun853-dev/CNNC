# app/services/dashboard_projection.py
"""Read-only projections for the STEP_1 dashboard. Zero writes, zero network."""
from __future__ import annotations

from typing import Mapping


def _attention_items(s: Mapping, recovery_exhausted_invalid: int = 0) -> list[dict]:
    r, c, m, h = s["remote"], s["crawl"], s["model"], s.get("remote_health", {})
    d = s["detail"]
    items: list[dict] = []
    # P1-1:带合法 durable proof 的 RECOVERY_EXHAUSTED 是安全收口(关联 crawl 已转
    # NO_RESULT,ordinary finalize 放行),不是"需要人工处理";补采只是可选人工操作。
    if r["recovery_exhausted"] - recovery_exhausted_invalid > 0:
        items.append({"level": "yellow", "tag": "关注", "title": "自动恢复已用尽",
            "problem": "招商局任务停滞,唯一一次自动重启(停用→启用)后仍无进展,系统已停止再次自动重启。",
            "impact": "关联商品已按“无采集结果”安全收口(价格留空,不伪造数据),不阻塞最终出表。",
            "system_action": "已按规则收口,不再自动重试;证据链保留在任务详情。",
            "suggestion": "接受缺价则无需操作;如需补采属可选人工操作,按人工恢复手册处理。"})
    # P1-1:proof 不合法的 RECOVERY_EXHAUSTED 会被 finalize 判为 remote_invalid
    # (NON_TOLERABLE),必须按真实阻塞显示红色人工。
    if recovery_exhausted_invalid:
        items.append({"level": "red", "tag": "需人工", "title": "自动恢复已用尽但证据不完整",
            "problem": "存在恢复用尽任务缺少完整 durable 恢复证据(确认停用/启用/恢复时间)。",
            "impact": "证据校验未通过,该状态当前阻塞最终出表。",
            "system_action": "系统按无效终态处理,不会自动出表。",
            "suggestion": "核对任务详情中的恢复证据,按人工恢复手册处理。"})
    if r["manual"]:
        items.append({"level": "red", "tag": "需人工", "title": "招商局任务需要人工检查",
            "problem": "任务进入 MANUAL_REQUIRED(身份冲突/写未确认/探测失败等)。",
            "impact": "该任务及其商品在人工处理前不会进入最终表。",
            "system_action": "系统已停止该任务的一切自动动作,等待人工。",
            "suggestion": "按人工恢复手册对应子命令处理;不要删除 durable 状态。"})
    if c["manual"]:
        # P1-4:采集 manual/terminal 在 finalize 中计入 crawl_terminal(ready 门 +
        # NON_TOLERABLE_BLOCKERS),普通与强制出表都被阻塞,绝不能说"价格留空进入最终表"。
        items.append({"level": "red", "tag": "需人工", "title": "采集行需要人工处理",
            "problem": "部分商品采集进入人工/终态失败。",
            "impact": "对应商品采集异常当前阻塞最终出表,需要先处理该采集问题。",
            "system_action": "已停止自动重试。", "suggestion": "查看任务详情中的错误证据后人工处理。"})
    if c["unplanned"]:
        items.append({"level": "red", "tag": "需人工", "title": "存在未规划采集的必爬行",
            "problem": "快照要求爬取的行没有对应 crawl 计划。", "impact": "finalize 被禁止。",
            "system_action": "不会在未规划情况下出表。", "suggestion": "运行 doctor 核对,授权后再处理。"})
    if m["terminal"]:
        items.append({"level": "red", "tag": "需人工", "title": "模型校验终态失败",
            "problem": "模型重试预算耗尽后仍失败。", "impact": "对应商品缺模型结论。",
            "system_action": "不再自动重试。", "suggestion": "可按手册 requeue-model-transient/recover-model 处理。"})
    # Review Fix Round 2:detail.terminal 是终态证据,不在 computed_final_pending 内、
    # 不阻塞普通 finalize(行以 _detail_failure() 客户价留空+失败证据进入最终表),
    # 因此是非阻塞黄色关注——既不误报"正在准备数据",也不默默消失。
    if d["terminal"]:
        items.append({"level": "yellow", "tag": "关注", "title": "Detail 有终态失败",
            "problem": "部分商品 Detail 请求已重试耗尽并进入终态失败。",
            "impact": "不阻塞今日最终出表;对应商品客户价留空并保留失败证据,按现有可用信息继续收口。",
            "system_action": "该 Detail 已停止自动重试,失败证据保留在 durable 状态中。",
            "suggestion": "一般无需操作;如需补齐详情可在技术详情中查看对应失败记录。"})
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
    items.sort(key=lambda a: a["level"] != "red")  # 红色优先展示(首屏截取前 3 条)
    return items


def _zs_summary(s: Mapping, recovering: int, recovery_exhausted_invalid: int = 0) -> dict:
    r = s["remote"]
    return {"executing": r["running"], "awaiting_harvest": r["awaiting_harvest"],
            "awaiting_priority": r["awaiting_priority"], "recovering": recovering,
            "manual": r["manual"] + recovery_exhausted_invalid,
            "recovery_exhausted": max(0, r["recovery_exhausted"] - recovery_exhausted_invalid),
            "completed": r["completed"]}


def _stages(s: Mapping, finalized: bool, recovery_exhausted_invalid: int = 0) -> list[dict]:
    d, m, r, c = s["detail"], s["model"], s["remote"], s["crawl"]
    h = s.get("remote_health", {})
    legal_exhausted = max(0, r["recovery_exhausted"] - recovery_exhausted_invalid)
    prep_pending = d["pending"] + d["running"] + m["pending"] + m["running"]
    zs_uncleared = any(r[k] for k in ("intent", "pending", "running", "exported", "unknown", "manual")) \
        or any(c[k] for k in ("pending", "running", "unplanned", "unknown", "manual"))
    zs_bad = bool(r["manual"] or recovery_exhausted_invalid or c["manual"] or c["unplanned"])
    zs_yellow = bool(r["unknown"] or c["unknown"] or h.get("stall_detected"))
    uniq = s["counts"]["unique_items"]
    stages = []
    stages.append({"key": "prep", "name": "数据准备",
        "state": "bad" if m["terminal"] else ("cur" if prep_pending else "done"),
        "state_word": "⚠ 异常" if m["terminal"] else ("● 进行中" if prep_pending else "✓ 完成"),
        "num": f"{m['completed']} / {uniq}" if uniq else "—",
        "sub": [("SaaS 发现", f"{s['counts']['discovered']} 行(新 {s['counts']['new']} / 变 {s['counts']['changed']})"),
                ("Detail", f"{d['completed']}/{uniq}"),
                ("模型校验", f"{m['completed']}/{uniq}")]})
    if finalized:
        zs_state, zs_word = "done", "✓ 完成"
    elif zs_bad:
        zs_state, zs_word = "bad", "⚠ 需人工"
    elif zs_yellow:
        zs_state, zs_word = "cur", "● 进行中(关注)"
    elif zs_uncleared:
        zs_state, zs_word = "cur", "● 进行中"
    elif r["completed"] or legal_exhausted:
        zs_state, zs_word = "done", "✓ 完成"
    else:
        zs_state, zs_word = "wait", "○ 等待"
    stages.append({"key": "zhaoshang", "name": "招商局处理", "state": zs_state, "state_word": zs_word,
        "num": f"执行中 {r['running']} · 需人工 {r['manual']}",
        "sub": [("等待提交", str(c["waiting_submit"])), ("等待提优", str(r["awaiting_priority"])),
                ("执行中(含自动恢复)", str(r["running"])), ("等待回收", str(r["awaiting_harvest"])),
                ("已完成", str(r["completed"])), ("需人工", str(r["manual"])),
                ("自动恢复已用尽", str(r["recovery_exhausted"]))]})
    stages.append({"key": "closeout", "name": "最终收口",
        "state": "done" if finalized else ("cur" if s["final"]["frozen"] else "wait"),
        "state_word": "✓ 完成" if finalized else ("● 进行中" if s["final"]["frozen"] else "○ 等待"),
        "num": "18:00 口径", "sub": [("数据冻结", "已冻结" if s["final"]["frozen"] else "未开始(等待 18:00)")]})
    stages.append({"key": "publish", "name": "最终出表", "state": "done" if finalized else "wait",
        "state_word": "✓ 完成" if finalized else "○ 等待",
        "num": "已生成" if finalized else "阻塞清零后自动",
        "sub": [("输出文件", s["final"]["output_path"] or "未生成")]})
    return stages


def _final_text(s: Mapping) -> tuple[str, str]:
    f = s["final"]
    if f["finalized"]:
        state = "forced" if f["forced"] else "ok"
        text = "今日最终 Excel 已生成" + ("(曾强制收尾,存在被接受的缺价)" if f["forced"] else "")
        if f["output_path"]:
            text += ";文件存在校验:" + ("通过" if f["output_exists"] else "失败,请检查")
        return text + "。", state
    b = f.get("blocker_summary") or {}
    mapping = [("remote_pending", "个招商局任务仍在执行/待回收"), ("remote_intent", "个任务已排队待提交"),
               ("remote_unknown", "个任务提交结果待确认"), ("remote_manual", "项招商局任务需人工"),
               ("model_pending", "条模型处理未完成"), ("model_terminal", "条模型终态失败"),
               ("detail_pending", "条 Detail 未完成"), ("crawl_pending", "条采集未完成"),
               ("crawl_unplanned", "条必爬行未规划"), ("crawl_unknown", "条采集提交待确认"), ("crawl_manual", "条采集需人工")]
    parts = [f"{b[key]} {phrase}" for key, phrase in mapping if b.get(key, 0) > 0]
    if not parts:
        parts = ["全部阻塞已清零,等待下一次 closeout 自动出表"]
    return "还不能生成最终 Excel:" + ";".join(parts) + "。阻塞清零后系统将在下一次 closeout 自动生成。", "pending"


def compute_overall_status(status: Mapping, *, recovering: int = 0,
                           recovery_exhausted_invalid: int = 0) -> dict:
    if status.get("state") == "MISSING":
        return {"word": "今日无数据", "color": "gray",
                "explanation": "今天的审计尚未开始(工作日 09:00 后由计划任务自动启动;周末不运行)。",
                "attention": [], "stages": [], "zs_summary": {}, "final_text": "尚无最终表。", "final_state": "pending"}
    d, m, r, c, f = status["detail"], status["model"], status["remote"], status["crawl"], status["final"]
    prep_pending = d["pending"] + d["running"] + m["pending"] + m["running"]
    # P1-1:合法 RECOVERY_EXHAUSTED 不再计入 manual;proof 不合法的才按真实阻塞升级红。
    manual_needed = any((r["manual"], c["manual"], c["unplanned"], m["terminal"])) \
        or recovery_exhausted_invalid > 0
    zs_uncleared = any(r[k] for k in ("intent", "pending", "running", "exported")) or bool(c["pending"] + c["running"])
    # Review Fix Round 2:等待收口门直接采用生产 live blocker truth——
    # final.computed_pending(增量状态权威)不含 detail.terminal(终态证据,普通 finalize
    # 放行),不在投影层维护第二套 blocker 判定;discovered>0 排除首个快照落地前的
    # 每日开市空窗,避免把"发现还没开始"误报成"已全部完成"。
    if f["finalized"]:
        word, color = "今日结果已生成", "green"
    elif manual_needed:
        word, color = "需要人工处理", "red"
    elif f["frozen"]:
        word, color = "正在最终收口", "blue"
    elif status["counts"]["discovered"] > 0 and f["computed_pending"] == 0:
        word, color = "等待最终收口", "blue"
    elif prep_pending == 0 and r["awaiting_harvest"] > 0 and not r["pending"] and not r["running"]:
        word, color = "正在回收招商局结果", "blue"
    elif prep_pending == 0 and (zs_uncleared or r["unknown"]):
        word, color = "正在等待招商局", "blue"
    else:
        word, color = "正在准备数据", "blue"
    attention = _attention_items(status, recovery_exhausted_invalid)
    explanation = _explanation(status, attention, recovering, word)
    final_text, final_state = _final_text(status)
    return {"word": word, "color": color, "explanation": explanation, "attention": attention,
            "stages": _stages(status, f["finalized"], recovery_exhausted_invalid),
            "zs_summary": _zs_summary(status, recovering, recovery_exhausted_invalid),
            "final_text": final_text, "final_state": final_state}


def _explanation(s: Mapping, attention: list, recovering: int, word: str) -> str:
    counts, d, m, r = s["counts"], s["detail"], s["model"], s["remote"]
    parts: list[str] = []
    # 解释句铁律:存在任何"需要关注"项时逐项提及(红色优先排序,取前 3),
    # 禁止出现"无异常/无需人工处理"表述。
    if attention:
        parts.append(";".join(a["title"] for a in attention[:3]) + "。")
    elif word == "需要人工处理":
        parts.append("存在需要人工处理的问题。")
    else:
        parts.append("当前无需人工处理。")
    if word in ("正在等待招商局", "正在回收招商局结果"):
        seg = f"{counts['discovered']} 条数据的 Detail 与模型处理已全部完成,{r['running']} 个招商局远端任务执行中"
        if recovering:
            seg += f",其中 {recovering} 个正在自动恢复"
        parts.append(seg + "。")
    elif word == "正在准备数据":
        parts.append(f"已发现 {counts['discovered']} 行,Detail {d['completed']}/{counts['unique_items']},模型 {m['completed']}/{counts['unique_items']}。")
    elif word == "等待最终收口":
        parts.append("今日数据处理和招商局任务已全部完成,等待 18:00 口径冻结并进入最终收口。")
    elif word == "正在最终收口":
        parts.append("数据已按 18:00 口径冻结,系统正在收尾并等待阻塞清零。")
    elif word == "今日结果已生成":
        parts.append("今日流程已全部完成。")
    if len(attention) > 3:
        parts.append(f"需要关注共 {len(attention)} 项。")
    return "".join(parts)


import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.services.remote_running_recovery import (
    PENDING_RESTART_PHASES, RUNNING_PROBE_MIN_HARVEST_ATTEMPTS, RUNNING_PROBE_MIN_WALL_SECONDS,
    decode_recovery_evidence,
)
# 生产语义权威:finalize 对 RECOVERY_EXHAUSTED durable proof 的接受判定唯一来源
# (app/workflows/incremental_price_audit_finalize.py::_valid_recovery_exhausted_evidence)。
# Dashboard 只读复用同一判定,不在投影层重写第二套业务规则。
from app.workflows.incremental_price_audit_finalize import _valid_recovery_exhausted_evidence

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
            cn, pill = _STATE_CN.get(str(row["state"]), (str(row["state"]), "warn"))
            recovery_attempts = int(row["recovery_attempts"] or 0)
            exhausted_valid = None
            if str(row["state"]) == "RECOVERY_EXHAUSTED":
                # 与 finalize 完全一致的 durable proof 判定:合法=安全收口(黄),
                # 不合法=finalize 将判 remote_invalid 的真实阻塞(红)。
                exhausted_valid = _valid_recovery_exhausted_evidence(dict(row))
                if exhausted_valid:
                    pill = "warn"
            subtasks = None
            for o in evidence.get("observations") or []:
                sig = o.get("signature")
                if isinstance(sig, list) and len(sig) == 6:
                    subtasks = {"finished": int(sig[5]), "total": int(sig[0])}
            jobs.append({
                "id": int(row["id"]), "mall_key": str(row["mall_key"] or ""),
                "task_name": str(row["task_name"] or ""), "state": str(row["state"]),
                "state_cn": (cn + " · 自动恢复中") if recovering else cn,
                "pill": "run" if recovering else pill, "subtasks": subtasks,
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "harvest_attempts": int(row["harvest_attempts"] or 0), "recovering": recovering,
                "recovery_attempts": recovery_attempts,
                "post_attempted": int(row["post_attempted"] or 0),
                "recovery_exhausted_valid": exhausted_valid,
                "crawl_total": counts.get(int(row["id"]), (0, 0))[0],
                "crawl_success": counts.get(int(row["id"]), (0, 0))[1],
                "error_text": row["error_text"], "remote_identity": row["remote_identity"],
                "recovery_evidence_json": row["recovery_evidence_json"],
            })
        return jobs
    finally:
        con.close()


def _has_restart_evidence(job: Mapping) -> bool:
    """任务是否携带 restart 协议证据(任一非空 phase,含零写撤退 DECLINED 等)。"""
    restart = decode_recovery_evidence(job.get("recovery_evidence_json")).get("restart")
    return isinstance(restart, dict) and str(restart.get("phase") or "") != ""


def project_recovery_timeline(job_row: Mapping) -> dict:
    evidence = decode_recovery_evidence(job_row.get("recovery_evidence_json"))
    state = str(job_row.get("state") or "")
    created = _parse_ts(job_row.get("created_at"))
    wall = (datetime.now(UTC) - created).total_seconds() if created else 0.0
    attempts = int(job_row.get("harvest_attempts") or 0)
    restart = evidence.get("restart")
    phase_restart = str(restart.get("phase")) if isinstance(restart, dict) else ""
    if state == "RECOVERY_EXHAUSTED":
        phase = "恢复已用尽"
    elif state == "MANUAL_REQUIRED":
        phase = "需要人工处理"
    elif state == "RUNNING" and phase_restart in PENDING_RESTART_PHASES:
        phase = "已证明停滞(恢复进行中)"
    elif state == "RUNNING" and phase_restart == "ENABLE_CONFIRMED":
        phase = "自动恢复中"
    elif state == "RUNNING" and attempts >= RUNNING_PROBE_MIN_HARVEST_ATTEMPTS \
            and wall >= RUNNING_PROBE_MIN_WALL_SECONDS:
        phase = "观察中(≠异常)"
    elif phase_restart == "ENABLE_CONFIRMED":
        phase = "恢复成功"
    else:
        phase = "正常采集"
    return {"phase": phase, "observations": evidence.get("observations") or [],
            "restart": restart, "probe_health": evidence.get("probe_health"),
            "recovery_attempts": int(job_row.get("recovery_attempts") or 0),
            "harvest_attempts": int(job_row.get("harvest_attempts") or 0)}


import json as _json
import subprocess as _subprocess
from datetime import date as _date, timedelta as _timedelta
from time import monotonic as _time_monotonic

_RUNNING_CODES = {267009, 0x41301}


def project_recent_days(runtime_root, n: int = 14, *, today: str | None = None) -> list[dict]:
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


_SCHEDULER_CACHE_SECONDS = 30.0
_scheduler_cache: dict = {"at": 0.0, "value": None}


def _run_scheduler_probe() -> dict | None:
    """Read-only PowerShell Get-ScheduledTask/Get-ScheduledTaskInfo JSON probe."""
    cmd = ["powershell", "-NoProfile", "-Command",
           "$t = Get-ScheduledTask -TaskName 'CNNC-PriceAudit-Incremental' -ErrorAction SilentlyContinue; "
           "if (-not $t) { Write-Output '{}' ; exit 0 }; "
           "$i = Get-ScheduledTaskInfo -TaskName $t.TaskName -ErrorAction SilentlyContinue; "
           "[pscustomobject]@{ state = [string]$t.State; last_run = [string]$i.LastRunTime; "
           "last_result = if ($i) { [int64]$i.LastTaskResult } else { -1 }; next_run = [string]$i.NextRunTime } | ConvertTo-Json -Compress"]
    try:
        proc = _subprocess.run(cmd, capture_output=True, timeout=10, check=False)
    except (OSError, _subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        data = _json.loads(proc.stdout.decode("utf-8-sig", errors="replace") or "{}")
    except (ValueError, TypeError):
        return None
    return data or None


def project_scheduler_status() -> dict:
    now = _time_monotonic()
    if _scheduler_cache["value"] is not None and now - _scheduler_cache["at"] < _SCHEDULER_CACHE_SECONDS:
        return _scheduler_cache["value"]
    data = None
    try:
        data = _run_scheduler_probe()
    except Exception:
        data = None
    if data is None:
        result = {"available": False}
    else:
        # Hotfix 2026-08-27:LastTaskResult 为 UInt32(如触发重叠被拒 0x800710E0=
        # 2147946720,超 Int32);PS 侧强转异常时该字段还可能序列化成非数值。
        # 探针形状永远不许把 summary 打成 503——非数值按未知结果码(-1)降级。
        try:
            code = int(data.get("last_result", -1))
        except (TypeError, ValueError):
            code = -1
        result = {"available": True,
                  "running": str(data.get("state", "")).lower() == "running" or code in _RUNNING_CODES,
                  "last_run": str(data.get("last_run") or "") or None,
                  "last_result": str(code),
                  "last_result_cn": _translate_task_result(code),
                  "next_run": str(data.get("next_run") or "") or None}
    _scheduler_cache.update({"at": now, "value": result})
    return result


import hashlib as _hashlib
from datetime import UTC as _UTC, datetime as _datetime

from app.services.incremental_audit_runtime import DEFAULT_RUNTIME_ROOT, runtime_paths
from app.services.incremental_audit_status import collect_runtime_status


def fingerprint_payload(payload: Mapping) -> str:
    body = {k: v for k, v in payload.items() if k not in ("generated_at", "fingerprint")}
    return _hashlib.sha256(
        _json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def build_dashboard_summary(business_date: str, runtime_root=DEFAULT_RUNTIME_ROOT) -> dict:
    paths = runtime_paths(business_date, runtime_root)
    status = collect_runtime_status(business_date=business_date, runtime_root=runtime_root)
    jobs = project_remote_jobs(paths.state_path, business_date) if paths.state_path.is_file() else []
    recovering = sum(1 for j in jobs if j["recovering"])
    # P1-1:按 finalize 权威判定区分合法/不合法 RECOVERY_EXHAUSTED,只让不合法的升级红。
    recovery_exhausted_invalid = sum(
        1 for j in jobs
        if j["state"] == "RECOVERY_EXHAUSTED" and j["recovery_exhausted_valid"] is False)
    timelines: dict = {}
    for j in jobs:
        # P1-3:恢复成功后转 COMPLETED(pill=ok)的任务也必须保留 recovery timeline,
        # 否则丢失"自动恢复是否发生过"的关键观测证据。
        if j["recovering"] or j["pill"] in ("err", "warn") \
                or j["recovery_attempts"] > 0 or _has_restart_evidence(j):
            timelines[str(j["id"])] = project_recovery_timeline(j)
    payload = {
        "generated_at": _datetime.now(_UTC).isoformat(timespec="seconds"),
        "overall": compute_overall_status(status, recovering=recovering,
                                          recovery_exhausted_invalid=recovery_exhausted_invalid),
        "jobs": [{k: v for k, v in j.items() if k != "recovery_evidence_json"} for j in jobs],
        "timelines": timelines,
        "recent_days": project_recent_days(runtime_root, n=14),
        "scheduler": project_scheduler_status(),
        "status": status,
    }
    payload["fingerprint"] = fingerprint_payload(payload)
    return payload
