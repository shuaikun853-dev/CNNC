from app.agent.planner import TABLE_MAP, STEP_TEMPLATES

INTERPRETATION_HINTS = {
    "weekly_check": (
        "巡检结果关注：1) 商品启用率是否低于 95%；2) 采集状态异常占比是否超过 5%；"
        "3) 任务失败率是否超过 2%。任何指标超出阈值，进入对应场景细查。"
    ),
    "single_item_check": (
        "单商品排查关注：1) STATUS=1 表示启用；2) monitor 表 STATUS=0 为采集中，=2 为异常；"
        "3) 价格为 0 或 NULL 需确认爬虫是否回调。"
    ),
    "crawler_check": (
        "爬虫排查关注：1) 采集中超过 24h 基本可判定为未回调；2) 失败率超过 10% 需关注；"
        "3) 确认目标站点是否变更需人工判断。"
    ),
    "relation_check": (
        "同款排查关注：1) 无同款绑定率过高说明匹配流程问题；2) 重复绑定导致价格比对混乱；"
        "3) 标品下架会级联影响同款关系。"
    ),
    "task_check": (
        "任务排查关注：1) STATE=3 为失败，=4 为取消，=0 为待执行；2) 积压超过 2h 需关注；"
        "3) ERROR_MSG 包含具体错误原因。注意：本系统不自动重推。"
    ),
    "knowledge_qa": (
        "知识问答结果仅供参考，不执行任何操作。如涉及 SQL，请手动在只读副本上执行验证。"
    ),
}


def generate_result_guide(scenario: str, sql_templates: list[dict]) -> dict:
    hints = []
    for tpl in sql_templates:
        hints.append({
            "sql_name": tpl.get("name", ""),
            "result_hint": tpl.get("result_hint", ""),
        })

    return {
        "interpretation": INTERPRETATION_HINTS.get(scenario, "暂无解读提示"),
        "per_sql_hints": hints,
    }
