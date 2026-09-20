# app/services/dashboard_projection.py
"""Read-only projections for the STEP_1 dashboard. Zero writes, zero network."""
from __future__ import annotations

from typing import Mapping

from app.services.customer_price_semantics import DETAIL_MAX_ATTEMPTS_DEFAULT


def _cp(detail: Mapping, key: str) -> int:
    """Defensive read of a customer_price_* detail counter (0 when absent)."""
    try:
        return max(0, int(detail.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


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
    # 客户价门禁(2026-09-03,R2-B 同步 fail-closed;2026-09-11 单据缺席改跳过态):
    # detail 业务计数取代旧"Detail 有终态失败"黄条。RETRYING=自动重试中(黄,无需人工);
    # MISSING/FAILED/INVALID=重试预算耗尽(红,阻塞自动审批与最终出表);
    # MISSING_PROVEN=缺详情身份/未提供价(R2-B 起红条阻塞,与"客户价获取失败"并列);
    # CUSTOMER_ABSENT_SKIPPED(含 legacy LOOKUP_FAILED)=客户侧权威查询成功且单据
    # 缺席=本轮无需处理(黄"关注",不阻塞出表,无需人工)。
    cp_retrying = _cp(d, "customer_price_retrying")
    cp_missing = _cp(d, "customer_price_missing")
    cp_failed = _cp(d, "customer_price_failed")
    cp_invalid = _cp(d, "customer_price_invalid")
    cp_absent = _cp(d, "customer_price_absent_proven")
    cp_skipped = _cp(d, "customer_absent_skipped")
    if cp_retrying:
        items.append({"level": "yellow", "tag": "自动重试", "title": "客户价获取正在自动重试",
            "problem": f"{cp_retrying} 条商品客户价获取进入业务失败,已进入 durable 自动重试。",
            "impact": "只影响对应行的客户价获取,不影响其他链路;重试预算耗尽前无需人工。",
            "system_action": f"按每行至多 {DETAIL_MAX_ATTEMPTS_DEFAULT} 次的重试预算自动重试。",
            "suggestion": "无需操作;重试耗尽后本页会升级为红色“客户价获取失败”。"})
    if cp_missing or cp_failed or cp_invalid:
        reasons = []
        if cp_missing:
            reasons.append(f"{cp_missing} 条客户导出/详情中未找到该商品(NOT_FOUND 缺失)")
        if cp_failed:
            reasons.append(f"{cp_failed} 条详情请求或解析失败")
        if cp_invalid:
            reasons.append(f"{cp_invalid} 条取得无效客户价")
        items.append({"level": "red", "tag": "需人工", "title": "客户价获取失败",
            "problem": "部分商品客户价重试预算耗尽仍未取得:" + "、".join(reasons) + "。",
            "impact": "对应数据缺失价格审核基准;系统已禁止自动审批与最终出表。",
            "system_action": "已停止自动重试,保留失败证据。",
            "suggestion": "在客户价明细中查看失败行;处理上游问题后可人工 requeue 重试。"})
    if cp_absent:
        items.append({"level": "red", "tag": "需人工", "title": "客户价确定性缺失",
            "problem": "缺少详情身份或未提供客户价,客户价确定性缺席,自动重试无意义。",
            "impact": "该数据缺少价格审核基准;当前阻塞最终出表。",
            "system_action": "不再自动重试;证据已保留在客户价明细。",
            "suggestion": "核对行内身份字段与输入价格;如需放行需业务显式决策。"})
    if cp_skipped:
        items.append({"level": "yellow", "tag": "关注", "title": "客户侧已无数据跳过(不阻塞)",
            "problem": f"{cp_skipped} 条商品经客户侧权威查询确认本轮无此单据,按规则记为无需处理。",
            "impact": "确定性单据缺席,属跳过态,不阻塞最终出表。",
            "system_action": "已跳过,不重试;证据已保留在客户价明细。",
            "suggestion": "无需操作;如怀疑上游客户导出遗漏,可人工核对(可选)。"})
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


# closeout writer 谓词(单一权威):production writer 锁被 closeout/finalize 命令
# 持有时才算"最终收口执行中"。tick/harvest-only/drain-only 持锁不得让最终收口
# 显示进行中;锁 active 但 command 缺失(如无 owner 的 force-finalize)按
# "不能证明是收口"保守判 False。finalize 与 closeout 同为收尾出版链
# (closeout 锁内完成冻结→观察→finalize),独立 finalize 命令是同一链条尾部。
_CLOSEOUT_WRITER_COMMANDS = frozenset({"closeout", "finalize", "force-finalize"})


def _closeout_writer_active(s: Mapping) -> bool:
    writer = s.get("production_writer") or {}
    return bool(writer.get("active")) and str(writer.get("command") or "") in _CLOSEOUT_WRITER_COMMANDS


def _stages(s: Mapping, finalized: bool, recovery_exhausted_invalid: int = 0) -> list[dict]:
    d, m, r, c = s["detail"], s["model"], s["remote"], s["crawl"]
    h = s.get("remote_health", {})
    legal_exhausted = max(0, r["recovery_exhausted"] - recovery_exhausted_invalid)
    prep_pending = d["pending"] + d["running"] + m["pending"] + m["running"]
    # planning_pending 是"尚未轮到规划"的正常暂态,只计入未完成,不进 zs_bad(需人工)。
    zs_uncleared = any(r[k] for k in ("intent", "pending", "running", "exported", "unknown", "manual")) \
        or any(c[k] for k in ("pending", "running", "unplanned", "unknown", "manual")) \
        or bool(c.get("planning_pending", 0))
    zs_bad = bool(r["manual"] or recovery_exhausted_invalid or c["manual"] or c["unplanned"])
    zs_yellow = bool(r["unknown"] or c["unknown"] or h.get("stall_detected"))
    uniq = s["counts"]["unique_items"]
    stages = []
    # 客户价门禁(2026-09-03,R2-B 同步 fail-closed;2026-09-11 单据缺席改跳过态):
    # missing/failed/invalid = 重试预算耗尽的人工阻塞;absent_proven(缺详情身份/
    # 未提供价,MISSING_PROVEN)同样阻塞正式出表(round-2 裁定)。
    # customer_absent_skipped(权威查询成功且单据缺席)是非阻塞跳过态,不进入本判定。
    cp_blocking = _cp(d, "customer_price_missing") + _cp(d, "customer_price_failed") \
        + _cp(d, "customer_price_invalid") + _cp(d, "customer_price_absent_proven") > 0
    prep_bad = m["terminal"] or cp_blocking
    stages.append({"key": "prep", "name": "数据准备",
        "state": "bad" if prep_bad else ("cur" if prep_pending else "done"),
        "state_word": "⚠ 异常" if prep_bad else ("● 进行中" if prep_pending else "✓ 完成"),
        "num": f"{m['completed']} / {uniq}" if uniq else "—",
        "sub": [("SaaS 发现", f"{s['counts']['discovered']} 行(新 {s['counts']['new']} / 变 {s['counts']['changed']})"),
                ("Detail", f"{d['completed']}/{uniq}"),
                ("模型校验", f"{m['completed']}/{uniq}"),
                # 客户价业务口径(门禁可见性):ready/required;无必需求价的行显示占位。
                ("客户价", f"{_cp(d, 'customer_price_ready')}/{_cp(d, 'customer_price_required')}")
                if _cp(d, "customer_price_required") else ("客户价", "—")]})
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
    # closeout 阶段纳入 production writer 权威:手动 closeout 已真实持有 OS 级
    # writer 锁(status.production_writer.active)而数据尚未冻结时,旧逻辑只看
    # finalized/frozen,必然把"进行中"显示成"○ 等待"。优先级:
    # finalized > 收口执行中(closeout/finalize writer 锁,单一权威谓词,覆盖手动/自动)
    # > frozen > wait。tick/harvest-only/drain-only 持锁不算收口进行中。
    writer_active = _closeout_writer_active(s)
    if finalized:
        closeout_state, closeout_word = "done", "✓ 完成"
        freeze_text = "已完成"
    elif s["final"]["frozen"] or writer_active:
        closeout_state, closeout_word = "cur", "● 进行中"
        if s["final"]["frozen"]:
            freeze_text = "已冻结，正在收口" if writer_active else "已冻结"
        else:
            freeze_text = "收口任务已启动，正在准备"
    else:
        closeout_state, closeout_word = "wait", "○ 等待"
        freeze_text = "未开始(等待 18:00)"
    stages.append({"key": "closeout", "name": "最终收口",
        "state": closeout_state, "state_word": closeout_word,
        "num": "18:00 口径", "sub": [("数据冻结", freeze_text)]})
    stages.append({"key": "publish", "name": "最终出表", "state": "done" if finalized else "wait",
        "state_word": "✓ 完成" if finalized else "○ 等待",
        "num": "已生成" if finalized else "阻塞清零后自动",
        # 阶段卡只显示文件名;完整路径仍在 payload.status.final.output_path。
        "sub": [("输出文件", Path(s["final"]["output_path"]).name if s["final"]["output_path"] else "未生成")]})
    return stages


def _final_text(s: Mapping, closeout_running: bool = False) -> tuple[str, str]:
    f = s["final"]
    if f["finalized"]:
        state = "forced" if f["forced"] else "ok"
        text = "今日最终 Excel 已生成" + ("(曾强制收尾,存在被接受的缺价)" if f["forced"] else "")
        if f["output_path"]:
            text += ";文件存在校验:" + ("通过" if f["output_exists"] else "失败,请检查")
        return text + "。", state
    b = f.get("blocker_summary") or {}
    missing_prefix = ""
    if "missing_page_price_rows" in f:
        missing_prefix = (
            f"未爬到价格:{int(f.get('missing_page_price_rows') or 0)} 条"
            f"(强制收口上限 {int(f.get('force_closeout_limit') or 20)} 条)。"
        )
    mapping = [("remote_pending", "个招商局任务仍在执行/待回收"), ("remote_intent", "个任务已排队待提交"),
               ("remote_unknown", "个任务提交结果待确认"), ("remote_manual", "项招商局任务需人工"),
               ("model_pending", "条模型处理未完成"), ("model_terminal", "条模型终态失败"),
               ("detail_terminal", "条客户价获取失败(重试耗尽)"),
               ("customer_price_absent_proven", "条客户价确定性缺失(无详情身份/未提供价)"),
               ("customer_price_invalid", "条客户价证据无效(价格与状态矛盾)"),
               ("detail_pending", "条 Detail 未完成"),
               ("crawl_pending", "个底层采集工作项未完成(非缺价商品数)"),
               ("crawl_unplanned", "条必爬行未规划"), ("crawl_planning_pending", "条必爬行待规划"),
               ("crawl_unknown", "条采集提交待确认"), ("crawl_manual", "条采集需人工")]
    parts = [f"{b[key]} {phrase}" for key, phrase in mapping if b.get(key, 0) > 0]
    if closeout_running:
        # closeout 正在执行时不得宣称"下一次 closeout 自动生成"(自相矛盾);
        # 无阻塞时不重复"正在执行"措辞,一句话说明将自动出表。
        if parts:
            return missing_prefix + "还不能生成最终 Excel:" + ";".join(parts) + "。本轮 closeout 正在执行。", "pending"
        return missing_prefix + "还不能生成最终 Excel:当前 closeout 正在执行,完成后将自动生成最终 Excel。", "pending"
    if not parts:
        parts = ["全部阻塞已清零,等待下一次 closeout 自动出表"]
    return missing_prefix + "还不能生成最终 Excel:" + ";".join(parts) + "。阻塞清零后系统将在下一次 closeout 自动生成。", "pending"


def compute_overall_status(status: Mapping, *, recovering: int = 0,
                           recovery_exhausted_invalid: int = 0) -> dict:
    if status.get("state") == "MISSING":
        return {"word": "今日无数据", "color": "gray",
                "explanation": "今天的审计尚未开始(工作日 09:00 后由计划任务自动启动;周末不运行)。",
                "attention": [], "stages": [], "zs_summary": {}, "final_text": "尚无最终表。", "final_state": "pending"}
    d, m, r, c, f = status["detail"], status["model"], status["remote"], status["crawl"], status["final"]
    prep_pending = d["pending"] + d["running"] + m["pending"] + m["running"]
    # P1-1:合法 RECOVERY_EXHAUSTED 不再计入 manual;proof 不合法的才按真实阻塞升级红。
    # 客户价门禁(2026-09-03,R2-B 同步 fail-closed;2026-09-11 单据缺席改跳过态):
    # detail terminal = 客户价重试预算耗尽(MISSING/FAILED/INVALID),是真实人工阻塞
    # ——attention 已红"需人工",hero word 必须同源一致。missing/failed/invalid
    # 直接入判(SUCCESS+price<=0 的 INVALID 阶段态是 SUCCESS、terminal=0,但
    # finalize ready 门与 NON_TOLERABLE 同样阻塞);absent_proven(缺详情身份/
    # 未提供价,MISSING_PROVEN)R2-B 起同为阻塞。customer_absent_skipped(权威查询
    # 成功且单据缺席)是非阻塞跳过态,不入判。d["terminal"] 保留为防御(旧/不可
    # 解码 error_text 的 TERMINAL 行按 FAILED 覆盖)。
    manual_needed = any((r["manual"], c["manual"], c["unplanned"], m["terminal"], d["terminal"])) \
        or any(_cp(d, k) for k in ("customer_price_missing", "customer_price_failed",
                                   "customer_price_invalid", "customer_price_absent_proven")) \
        or recovery_exhausted_invalid > 0
    zs_uncleared = any(r[k] for k in ("intent", "pending", "running", "exported")) or bool(
        c["pending"] + c["running"] + c.get("planning_pending", 0)
    )
    # Review Fix Round 2:等待收口门直接采用生产 live blocker truth——
    # final.computed_pending(增量状态权威)不含 detail.terminal(终态证据,普通 finalize
    # 放行),不在投影层维护第二套 blocker 判定;discovered>0 排除首个快照落地前的
    # 每日开市空窗,避免把"发现还没开始"误报成"已全部完成"。
    closeout_running = _closeout_writer_active(status)   # 单一权威,word 与 final_text 共用
    if f["finalized"]:
        word, color = "今日结果已生成", "green"
    elif manual_needed:
        word, color = "需要人工处理", "red"
    elif f["frozen"] or closeout_running:
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
    final_text, final_state = _final_text(status, closeout_running)
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
        # closeout writer 已持锁但尚未冻结时不得声称"已冻结"(矛盾)。
        if s["final"]["frozen"]:
            parts.append("数据已按 18:00 口径冻结,系统正在收尾并等待阻塞清零。")
        else:
            parts.append("最终收口正在执行,数据冻结与阻塞清零进行中。")
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
from app.services.remote_diagnostics import sanitize_remote_diagnostic
# 生产语义权威:finalize 对 RECOVERY_EXHAUSTED durable proof 的接受判定唯一来源
# (app/workflows/incremental_price_audit_finalize.py::_valid_recovery_exhausted_evidence)。
# Dashboard 只读复用同一判定,不在投影层重写第二套业务规则。
from app.workflows.incremental_price_audit_finalize import _valid_recovery_exhausted_evidence

_STATE_CN = {
    "SUBMIT_INTENT": ("等待提交", "wait"), "PRIORITY_PENDING": ("等待提优", "wait"),
    "SUBMITTED": ("等待执行", "wait"), "RUNNING": ("执行中", "run"), "EXPORTED": ("等待回收", "wait"),
    "COMPLETED": ("已完成", "ok"), "SUBMIT_UNKNOWN": ("提交结果待确认", "warn"),
    "MANUAL_REQUIRED": ("需要人工检查", "err"), "RECOVERY_EXHAUSTED": ("自动恢复已用尽", "err"),
    "SUPERSEDED": ("已被人工重建替代", "warn"),
}

_SUBMIT_RECONCILE_DEAD_THRESHOLD = 4


def _remote_job_diagnostic(row: Mapping, evidence: Mapping) -> dict:
    state = str(row.get("state") or "")
    reason = sanitize_remote_diagnostic(row.get("error_text"))
    reconcile = evidence.get("submit_reconcile")
    reconcile = reconcile if isinstance(reconcile, Mapping) else {}
    last_reconcile = sanitize_remote_diagnostic(reconcile.get("last_diagnostic"))
    attempts = int(row.get("recovery_attempts") or 0)
    if state == "SUBMIT_UNKNOWN":
        return {
            "stage": "远端提交",
            "classification": "提交结果待确认",
            "reason": reason or "原始提交诊断不可恢复",
            "updated_at": row.get("updated_at"),
            "reconcile_attempts": attempts,
            "reconcile_threshold": _SUBMIT_RECONCILE_DEAD_THRESHOLD,
            "last_reconcile_status": sanitize_remote_diagnostic(reconcile.get("status"), max_length=80),
            "last_reconcile_diagnostic": last_reconcile or "尚无自动对账诊断",
            "last_reconcile_at": reconcile.get("last_reconcile_at"),
            "next_action": (
                "继续按 task_name + mall 做 exact reconcile；只有达到安全 dead threshold "
                "且能证明任务不存在后，redispatch 才能接管。"
            ),
            "note": "远端创建请求已进入可能发送边界，但尚未取得 exact taskId/taskNumber。",
        }
    priority_scope = "PriorityScopeSafetyError" in reason
    if state == "MANUAL_REQUIRED":
        return {
            "stage": "提优确认" if priority_scope else "远端任务处理",
            "classification": "PriorityScopeSafetyError" if priority_scope else "需要人工检查",
            "reason": reason or "具体诊断不可恢复",
            "updated_at": row.get("updated_at"),
            "reconcile_attempts": attempts,
            "reconcile_threshold": _SUBMIT_RECONCILE_DEAD_THRESHOLD,
            "last_reconcile_status": "",
            "last_reconcile_diagnostic": "",
            "last_reconcile_at": None,
            "next_action": (
                "系统将仅在 exact identity、terminal 和完整成功进度均被严格证明后自动回收结果；"
                "否则保持人工检查。"
            ),
            "note": (
                "该异常来自 priority 确认阶段，不代表远端爬虫失败。"
                if priority_scope else "保持 fail-closed，等待新的严格证据或人工处理。"
            ),
        }
    if reason:
        return {
            "stage": "远端任务处理",
            "classification": state or "远端异常",
            "reason": reason,
            "updated_at": row.get("updated_at"),
            "reconcile_attempts": attempts,
            "reconcile_threshold": _SUBMIT_RECONCILE_DEAD_THRESHOLD,
            "last_reconcile_status": "",
            "last_reconcile_diagnostic": "",
            "last_reconcile_at": None,
            "next_action": "按当前状态机继续处理。",
            "note": "",
        }
    return {}


def _parse_ts(value) -> datetime | None:
    try:
        m = datetime.fromisoformat(str(value or ""))
        return m if m.tzinfo else m.replace(tzinfo=UTC)
    except ValueError:
        return None


def _replacement_summary(con, job_id: int) -> dict | None:
    """Read-only lookup of the manual rebuild replacement that retired a job.

    Older state databases may predate the replacements table; any read
    failure just skips the annotation (the job itself still projects).
    """
    try:
        row = con.execute(
            """SELECT r.new_remote_job_id AS replacement_job_id, r.new_batch_number,
                      r.status, r.reason, r.created_at,
                      j.task_name AS new_task_name
               FROM remote_job_replacements r
               LEFT JOIN remote_jobs j ON j.id = r.new_remote_job_id
               WHERE r.old_remote_job_id=? ORDER BY r.id LIMIT 1""",
            (job_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "replacement_job_id": int(row["replacement_job_id"]),
        "new_task_name": str(row["new_task_name"] or ""),
        "new_batch_number": str(row["new_batch_number"] or ""),
        "status": str(row["status"] or ""),
        "reason": str(row["reason"] or ""),
        "created_at": row["created_at"],
    }


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
            row_dict = dict(row)
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
                "error_text": sanitize_remote_diagnostic(row["error_text"]),
                "diagnostic": _remote_job_diagnostic(row_dict, evidence),
                "remote_identity": row["remote_identity"],
                "recovery_evidence_json": row["recovery_evidence_json"],
                "superseded_by": _replacement_summary(con, int(row["id"])),
            })
        return jobs
    finally:
        con.close()


def _load_remote_jobs_for_progress(state_path: Path, business_date: str) -> list[dict]:
    """Load full durable remote-job rows for server-side progress probing only.

    This is intentionally separate from the browser-facing projection so
    payload_json and local file paths stay internal while the progress resolver
    can reconstruct the exact PreparedLegacyJob before a read-only remote query.
    """
    uri = f"file:{Path(state_path).resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        run = con.execute(
            "SELECT id FROM daily_runs WHERE business_date=?",
            (business_date,),
        ).fetchone()
        if run is None:
            return []
        rows = con.execute(
            "SELECT * FROM remote_jobs WHERE run_id=? ORDER BY id",
            (int(run["id"]),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def _has_restart_evidence(job: Mapping) -> bool:
    """任务是否携带 restart 协议证据(任一非空 phase,含零写撤退 DECLINED 等)。"""
    restart = decode_recovery_evidence(job.get("recovery_evidence_json")).get("restart")
    return isinstance(restart, dict) and str(restart.get("phase") or "") != ""


# ---- 客户价行级只读投影(plan §8):零写、零网络、全部文本脱敏 ----------------

from app.services.customer_price_semantics import (
    customer_price_business_state as _cp_state,
)
from app.services.customer_price_semantics import (
    sanitize_customer_price_text as _cp_sanitize,
)

# 行级分类键 → 过滤语义。R2-B 起 unacquired=尚未取得可用客户价,确定性缺席
# (MISSING_PROVEN)同属未获取;2026-09-11 起 CUSTOMER_ABSENT_SKIPPED(权威查询
# 成功且单据缺席)是非阻塞跳过态,同样只排除 READY。terminal=重试预算耗尽、
# 需要人工的终态。
_CP_UNFINISHED_KEYS = ("PENDING", "RETRYING", "MISSING", "FAILED", "INVALID")
_CP_TERMINAL_KEYS = frozenset({"MISSING", "FAILED", "INVALID"})
_CUSTOMER_PRICE_FILTERS = ("all", "unacquired", "retrying", "terminal")
# semantics 业务键 → 汇总桶(单一来源:customer_price_business_state)。
# "CUSTOMER_NOT_FOUND"/"LOOKUP_FAILED" 是防御性别名:分类器对这两个 status 恒返
# 回 key=CUSTOMER_ABSENT_SKIPPED,这两个字符串不会作为 key 出现(legacy 证据兼容)。
_STATE_BUCKET = {
    "READY": "ready", "PENDING": "pending", "RETRYING": "retrying",
    "MISSING": "missing", "MISSING_PROVEN": "absent_proven",
    "FAILED": "failed", "INVALID": "invalid",
    "CUSTOMER_ABSENT_SKIPPED": "absent_skipped",
    "CUSTOMER_NOT_FOUND": "absent_skipped", "LOOKUP_FAILED": "absent_skipped",
}


def empty_customer_price_projection(business_date: str, filter: str = "all") -> dict:
    """Shape-stable empty result (no state file / no run / no snapshot yet)."""
    return {
        "business_date": business_date,
        "filter": filter,
        "total_required": 0,
        "counts": {"ready": 0, "pending": 0, "retrying": 0, "missing": 0,
                   "failed": 0, "invalid": 0, "absent_proven": 0, "absent_skipped": 0},
        "rows": [],
        "truncated": 0,
        "limit": 0,
        "max_attempts": DETAIL_MAX_ATTEMPTS_DEFAULT,
    }


def project_customer_price(
    state_path: Path, business_date: str, *, filter: str = "all", limit: int = 500
) -> dict:
    """Read-only row-level customer-price projection for one business date.

    Mirrors ``project_remote_jobs``: ``mode=ro`` SQLite, no writes, no network.
    Rows come from the frozen snapshot when the run has one, otherwise the
    latest snapshot; members without a DETAIL stage row project as PENDING.
    Every text field passes ``sanitize_customer_price_text`` before leaving the
    process — no header/cookie/token material can reach the UI.
    """
    if filter not in _CUSTOMER_PRICE_FILTERS:
        filter = "all"  # projection 层防御;API 层负责 400
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 500
    uri = f"file:{Path(state_path).resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True); con.row_factory = sqlite3.Row
    try:
        run = con.execute(
            "SELECT id, frozen_snapshot_id FROM daily_runs WHERE business_date=?",
            (business_date,),
        ).fetchone()
        if run is None:
            return empty_customer_price_projection(business_date, filter)
        if run["frozen_snapshot_id"] is not None:
            snapshot_id = int(run["frozen_snapshot_id"])
        else:
            latest = con.execute(
                """SELECT id FROM snapshots WHERE run_id=?
                   ORDER BY observed_at DESC, id DESC LIMIT 1""",
                (int(run["id"]),),
            ).fetchone()
            if latest is None:
                return empty_customer_price_projection(business_date, filter)
            snapshot_id = int(latest["id"])
        rows: list[dict] = []
        counts = {"ready": 0, "pending": 0, "retrying": 0, "missing": 0,
                  "failed": 0, "invalid": 0, "absent_proven": 0, "absent_skipped": 0}
        for row in con.execute(
            """SELECT m.row_ordinal, i.row_json, i.v_commodity_id, i.sku_id,
                      w.state AS detail_state, w.attempts, w.retryable,
                      w.error_text, w.result_json, w.updated_at
               FROM snapshot_members m
               JOIN audit_items i ON i.id=m.audit_item_id
               LEFT JOIN stage_work w ON w.audit_item_id=i.id AND w.stage='DETAIL'
               WHERE m.snapshot_id=? ORDER BY m.row_ordinal""",
            (snapshot_id,),
        ):
            try:
                source = _json.loads(str(row["row_json"] or "{}"))
            except (TypeError, ValueError):   # JSONDecodeError ⊂ ValueError
                source = {}
            if not isinstance(source, dict):
                source = {}
            try:
                decoded = _json.loads(str(row["result_json"] or ""))
            except (TypeError, ValueError):   # JSONDecodeError ⊂ ValueError
                decoded = None
            result = decoded if isinstance(decoded, dict) else None
            state = _cp_state(row["detail_state"], result, row["error_text"])
            bucket = _STATE_BUCKET.get(state.key)
            if bucket is not None:
                counts[bucket] += 1
            rows.append({
                "document_id": _cp_sanitize(
                    source.get("vCommodityId") or source.get("documentId")
                    or row["v_commodity_id"]),
                "title": _cp_sanitize(
                    source.get("queryStr") or source.get("skuName") or ""),
                "sku_id": _cp_sanitize(source.get("skuId") or row["sku_id"]),
                "saas_price": (
                    _cp_sanitize(source.get("price"))
                    if source.get("price") is not None else None),
                "customer_price": (
                    _cp_sanitize(result.get("customerPrice"))
                    if result is not None and result.get("customerPrice") is not None
                    else None),
                "customer_price_status": _cp_sanitize(state.status),
                "customer_price_source": _cp_sanitize(
                    result.get("customerPriceSource")
                    if isinstance(result, Mapping) else ""),
                "state": state.key,
                "detail_state": _cp_sanitize(row["detail_state"]),
                "attempts": int(row["attempts"] or 0),
                "max_attempts": DETAIL_MAX_ATTEMPTS_DEFAULT,
                "retryable": bool(row["retryable"]),
                "error_text": _cp_sanitize(row["error_text"]),
                "updated_at": _cp_sanitize(row["updated_at"]),
            })
    finally:
        con.close()
    if filter == "unacquired":
        # R2-B:确定性缺席(MISSING_PROVEN)同属未获取;2026-09-11:单据缺席跳过态
        # (CUSTOMER_ABSENT_SKIPPED)同样只被"all/unacquired"看到——只排除 READY。
        selected = [r for r in rows if r["state"] != "READY"]
    elif filter == "retrying":
        selected = [r for r in rows if r["state"] == "RETRYING"]
    elif filter == "terminal":
        selected = [r for r in rows if r["state"] in _CP_TERMINAL_KEYS]
    else:
        selected = rows
    # 未完成在前(获取中→待重试→缺失→失败→无效),证据缺席次之,单据缺席跳过
    # 再次,已获取最后。
    order = {**{key: i for i, key in enumerate(_CP_UNFINISHED_KEYS)},
             "MISSING_PROVEN": len(_CP_UNFINISHED_KEYS),
             "CUSTOMER_ABSENT_SKIPPED": len(_CP_UNFINISHED_KEYS) + 1,
             "READY": len(_CP_UNFINISHED_KEYS) + 2}
    selected.sort(key=lambda r: order.get(r["state"], len(order)))
    truncated = max(0, len(selected) - limit) if limit else 0
    return {
        "business_date": business_date,
        "filter": filter,
        "total_required": len(rows),
        "counts": counts,
        "rows": selected[:limit] if limit else [],
        "truncated": truncated,
        "limit": limit,
        "max_attempts": DETAIL_MAX_ATTEMPTS_DEFAULT,
    }


_CP_SUMMARY_KEYS = (
    ("customer_price_required", "required"),
    ("customer_price_ready", "ready"),
    ("customer_price_pending", "pending"),
    ("customer_price_retrying", "retrying"),
    ("customer_price_missing", "missing"),
    ("customer_price_failed", "failed"),
    ("customer_price_invalid", "invalid"),
    ("customer_price_absent_proven", "absent_proven"),
    ("customer_absent_skipped", "absent_skipped"),
)


def customer_price_summary_block(detail: Mapping) -> dict:
    """Lightweight customer-price summary for build_dashboard_summary.

    Counts only (reuse of ``status.detail.customer_price_*``); no row-level
    data so the summary payload cannot balloon with the commodity pool.
    ``ratio`` reuses the status layer's rounded ``customer_price_ratio`` when
    present (single authority) and is only recomputed defensively.
    """
    block = {short: _cp(detail, key) for key, short in _CP_SUMMARY_KEYS}
    ratio = detail.get("customer_price_ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
        ratio = block["ready"] / block["required"] if block["required"] else 0
    block["ratio"] = ratio
    return block


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
    # Review fix:探针可能输出"合法 JSON 但非 dict 顶层"([] / 字符串 / 数字 / bool),
    # json.loads 不报错而 data.get 会 AttributeError 逃逸。Scheduler 只是附属只读观测:
    # 任何坏形状一律按探针失败降级 unavailable,绝不拖垮 summary。
    if not isinstance(data, Mapping):
        data = None
    if data is None:
        result = {"available": False}
    else:
        # Hotfix 2026-08-27:LastTaskResult 为 UInt32(如触发重叠被拒 0x800710E0=
        # 2147946720,超 Int32);PS 侧强转异常时该字段还可能序列化成非数值。
        # 探针形状永远不许把 summary 打成 503——非数值按未知结果码(-1)降级;
        # OverflowError 对应 json.loads 接受的 Infinity→int() 溢出。
        try:
            code = int(data.get("last_result", -1))
        except (TypeError, ValueError, OverflowError):
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


_PROBE_DISABLED_STATUS = {
    "available": False,
    "error_code": "PROBE_DISABLED",
    "classification": None,
    "progress": None,
    "observed_at": None,
    "stale": False,
    "conflict": False,
}


def build_dashboard_summary(
    business_date: str, runtime_root=DEFAULT_RUNTIME_ROOT, *, progress_service=None
) -> dict:
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
    # Read-only live progress overlay: any probe failure degrades to the
    # per-job unavailable shape; it can never fail the summary itself.
    progress_map: dict = {}
    if progress_service is not None:
        try:
            progress_map = progress_service.snapshot(
                _load_remote_jobs_for_progress(paths.state_path, business_date)
            ) or {}
        except Exception:
            progress_map = {}
    payload = {
        "generated_at": _datetime.now(_UTC).isoformat(timespec="seconds"),
        "overall": compute_overall_status(status, recovering=recovering,
                                          recovery_exhausted_invalid=recovery_exhausted_invalid),
        # 客户价轻量汇总(counts+ratio,无行级数据);fingerprint 自动覆盖。
        "customer_price": customer_price_summary_block(status.get("detail") or {}),
        "jobs": [{**{k: v for k, v in j.items() if k != "recovery_evidence_json"},
                  "live_progress": dict(progress_map.get(str(j["id"])) or _PROBE_DISABLED_STATUS)}
                 for j in jobs],
        "timelines": timelines,
        "recent_days": project_recent_days(runtime_root, n=14),
        "scheduler": project_scheduler_status(),
        "status": status,
        "local_fallback": dict((status.get("metrics") or {}).get("local_fallback") or {}),
    }
    payload["fingerprint"] = fingerprint_payload(payload)
    return payload
