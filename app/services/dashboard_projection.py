# app/services/dashboard_projection.py
"""Read-only projections for the STEP_1 dashboard. Zero writes, zero network."""
from __future__ import annotations

from typing import Mapping


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
    elif zs_uncleared or r["completed"]:
        zs_state, zs_word = "cur", "● 进行中"
    else:
        zs_state, zs_word = "wait", "○ 等待"
    stages.append({"key": "zhaoshang", "name": "招商局处理", "state": zs_state, "state_word": zs_word,
        "num": f"执行中 {r['running']} · 需人工 {r['manual'] + r['recovery_exhausted']}",
        "sub": [("等待提交", str(c["waiting_submit"])), ("等待提优", str(r["awaiting_priority"])),
                ("执行中(含自动恢复)", str(r["running"])), ("等待回收", str(r["awaiting_harvest"])),
                ("已完成", str(r["completed"])), ("需人工/恢复用尽", str(r["manual"] + r["recovery_exhausted"]))]})
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


def compute_overall_status(status: Mapping, *, recovering: int = 0) -> dict:
    if status.get("state") == "MISSING":
        return {"word": "今日无数据", "color": "gray",
                "explanation": "今天的审计尚未开始(工作日 09:00 后由计划任务自动启动;周末不运行)。",
                "attention": [], "stages": [], "zs_summary": {}, "final_text": "尚无最终表。", "final_state": "pending"}
    d, m, r, c, f = status["detail"], status["model"], status["remote"], status["crawl"], status["final"]
    prep_pending = d["pending"] + d["running"] + m["pending"] + m["running"]
    manual_needed = any((r["manual"], r["recovery_exhausted"], c["manual"], c["unplanned"], m["terminal"]))
    zs_uncleared = any(r[k] for k in ("intent", "pending", "running", "exported")) or bool(c["pending"] + c["running"])
    if f["finalized"]:
        word, color = "今日结果已生成", "green"
    elif manual_needed:
        word, color = "需要人工处理", "red"
    elif f["frozen"]:
        word, color = "正在最终收口", "blue"
    elif prep_pending == 0 and r["awaiting_harvest"] > 0 and not r["pending"] and not r["running"]:
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


def _explanation(s: Mapping, attention: list, recovering: int, word: str) -> str:
    counts, d, m, r = s["counts"], s["detail"], s["model"], s["remote"]
    parts: list[str] = []
    if word == "需要人工处理":
        tops = ";".join(a["title"] for a in attention if a["level"] == "red") or "存在需要人工处理的问题"
        parts.append(tops)
    else:
        parts.append("当前无需人工处理" if not attention else ";".join(a["title"] for a in attention[:3]))
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
