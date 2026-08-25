from app.agent.router import route_question, SCENARIO_RULES

STEP_TEMPLATES = {
    "weekly_check": [
        "1. 确认巡检时间范围（最近一周）",
        "2. 统计 commodity_pool 商品启用/下架总量",
        "3. 统计 monitor_commodity_price STATUS 分布",
        "4. 统计 task_execute 最近一周 STATE 分布",
        "5. 汇总异常比例，生成巡检报告",
    ],
    "single_item_check": [
        "1. 获取商品 ID，查 commodity_pool 基本信息",
        "2. 查 monitor_commodity_price 最近更新记录",
        "3. 查 monitor_update_price_record 价格更新历史",
        "4. 确认爬虫状态是否正常回调",
        "5. 如价格源正常但未更新，检查 update_price 记录",
    ],
    "crawler_check": [
        "1. 统计长时间 STATUS=0（采集中）的记录数",
        "2. 查看采集失败率趋势",
        "3. 确认目标站点是否变更（需人工判断）",
        "4. 检查爬虫回调接口日志",
        "5. 如为批量问题，联系爬虫团队",
    ],
    "relation_check": [
        "1. 统计无同款绑定（standard_commodity_relation 缺失）的商品数",
        "2. 检查是否存在同款重复绑定",
        "3. 确认标品（standard_commodity）状态是否正常",
        "4. 如为新增商品无同款，确认标品匹配流程",
        "5. 如为同款下架，确认是否需要重新匹配",
    ],
    "task_check": [
        "1. 查最近 N 小时 task_execute 失败任务",
        "2. 查积压中（STATE=0）的任务数",
        "3. 分析 ERROR_MSG 常见错误类型",
        "4. 确认消费端是否正常",
        "5. 如需重推，仅记录建议，不自动执行",
    ],
    "knowledge_qa": [
        "1. 根据问题关键词，在知识库中查找相关段落",
        "2. 整理知识库中的相关说明",
        "3. 如涉及表结构，附上表说明",
        "4. 如涉及 SQL，提供只读查询模板",
        "5. 明确标注此为知识问答，不执行任何操作",
    ],
}

TABLE_MAP = {
    "weekly_check": ["commodity_pool_188605", "monitor_commodity_price_188605", "onecode_base3.task_execute"],
    "single_item_check": ["commodity_pool_188605", "monitor_commodity_price_188605", "monitor_update_price_record"],
    "crawler_check": ["monitor_commodity_price_188605", "monitor_commodity_price_last_change_188605"],
    "relation_check": ["standard_commodity_relation_188605", "standard_commodity_188605", "commodity_pool_188605"],
    "task_check": ["onecode_base3.task_execute"],
    "knowledge_qa": [],
}


def build_diagnosis(question: str) -> dict:
    route_result = route_question(question)
    scenario = route_result.scenario

    steps = STEP_TEMPLATES.get(scenario, STEP_TEMPLATES["knowledge_qa"])
    tables = TABLE_MAP.get(scenario, [])

    return {
        "scenario": scenario,
        "confidence": route_result.confidence,
        "reason": route_result.reason,
        "diagnosis_steps": steps,
        "related_tables": tables,
    }
