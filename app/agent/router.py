import re
from dataclasses import dataclass

OUT_OF_SCOPE_MESSAGE = (
    "当前 Agent 只支持中核 SaaS 运维，tenant_id=188605，"
    "commodity_pool_id=222，不处理其他租户或非中核专项问题。"
)

CNNC_POSITIVE_SIGNALS = ["中核", "188605"]

NON_CNNC_PROJECT_NAMES = ["宏伟", "南水北调", "华盛"]

_SIX_DIGIT_SUFFIX_RE = re.compile(r"_(\d{6})")

_SIX_DIGIT_STANDALONE_RE = re.compile(r"(?:tenant_id\s*=?\s*'?|租户\s*'?)(\d{6})('?)?", re.IGNORECASE)

_EXPLICIT_TENANT_RE = re.compile(
    r"(?:tenant_id|TENANT_ID)\s*=?\s*'?(\d{6})'?", re.IGNORECASE
)

_ANY_SIX_DIGIT_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

_TABLE_SUFFIX_RE = re.compile(r"[a-zA-Z_]+_(\d{6})")


@dataclass
class ScopeCheckResult:
    in_scope: bool
    error_code: str
    message: str


def _has_explicit_non_cnnc_tenant(question: str) -> bool:
    for m in _EXPLICIT_TENANT_RE.finditer(question):
        tid = m.group(1)
        if tid != "188605":
            return True
    for m in _SIX_DIGIT_STANDALONE_RE.finditer(question):
        tid = m.group(1)
        if tid != "188605":
            return True
    return False


def _has_non_cnnc_six_digit(question: str) -> bool:
    for m in _ANY_SIX_DIGIT_RE.finditer(question):
        num = m.group(1)
        if num != "188605":
            return True
    return False


def _has_non_cnnc_table_suffix(question: str) -> bool:
    for m in _TABLE_SUFFIX_RE.finditer(question):
        suffix = m.group(1)
        if suffix != "188605":
            return True
    return False


def _has_non_cnnc_project_name(question: str) -> bool:
    for name in NON_CNNC_PROJECT_NAMES:
        if name in question:
            return True
    return False


def check_scope(question: str) -> ScopeCheckResult:
    if not question or not question.strip():
        return ScopeCheckResult(True, "", "")

    if _has_non_cnnc_project_name(question):
        return ScopeCheckResult(False, "OUT_OF_SCOPE", OUT_OF_SCOPE_MESSAGE)

    if _has_explicit_non_cnnc_tenant(question):
        return ScopeCheckResult(False, "OUT_OF_SCOPE", OUT_OF_SCOPE_MESSAGE)

    if _has_non_cnnc_six_digit(question):
        return ScopeCheckResult(False, "OUT_OF_SCOPE", OUT_OF_SCOPE_MESSAGE)

    if _has_non_cnnc_table_suffix(question):
        return ScopeCheckResult(False, "OUT_OF_SCOPE", OUT_OF_SCOPE_MESSAGE)

    return ScopeCheckResult(True, "", "")


SCENARIO_RULES = {
    "weekly_check": {
        "keywords": [
            "每周巡检", "周报", "巡检", "统计", "跑批", "周统计",
            "批量状态", "整体情况", "全局统计", "周数据",
        ],
        "label": "每周巡检与统计",
    },
    "single_item_check": {
        "keywords": [
            "没价格", "价格不更新", "商品异常", "单商品", "某商品",
            "价格不对", "价格缺失", "商品下架", "价格不变",
            "monitor_commodity_price", "commodity_pool",
        ],
        "label": "单商品异常排查",
    },
    "crawler_check": {
        "keywords": [
            "采集中", "爬虫", "未回调", "爬取失败", "采集失败",
            "STATUS异常", "回调", "爬虫卡住", "长时间采集中",
            "爬取超时", "采集超时",
        ],
        "label": "爬虫/采集链路排查",
    },
    "relation_check": {
        "keywords": [
            "无同款", "同款不足", "同款下架", "同款重复", "同款",
            "标品关系", "standard_commodity_relation", "同款绑定",
            "同款缺失",
        ],
        "label": "同款关系排查",
    },
    "task_check": {
        "keywords": [
            "推送失败", "没收到", "任务积压", "task_execute", "推送异常",
            "客户没收到", "推送不成功", "任务失败", "task 失败",
            "重推", "消息未达",
        ],
        "label": "推送/任务链路排查",
    },
    "knowledge_qa": {
        "keywords": [],
        "label": "运维知识问答",
    },
}


@dataclass
class RouteResult:
    scenario: str
    confidence: float
    reason: str


def route_question(question: str) -> RouteResult:
    if not question or not question.strip():
        return RouteResult("knowledge_qa", 0.1, "问题为空，无法识别")

    q = question.lower()
    best_scenario = "knowledge_qa"
    best_score = 0
    best_reason = "未匹配到特定场景关键词，归入知识问答"

    for scenario, rule in SCENARIO_RULES.items():
        if scenario == "knowledge_qa":
            continue
        hits = [kw for kw in rule["keywords"] if kw.lower() in q]
        score = len(hits) / max(len(rule["keywords"]), 1)
        if hits and score >= best_score:
            best_score = score
            best_scenario = scenario
            best_reason = f"命中关键词: {', '.join(hits)}"

    confidence = min(0.5 + best_score * 0.5, 1.0) if best_scenario != "knowledge_qa" else max(0.3, best_score)

    return RouteResult(
        scenario=best_scenario,
        confidence=round(confidence, 2),
        reason=best_reason,
    )
