"""本地 SaaS 待审批 Excel 审查脚本。

读取 Excel -> （可选）调用中核详情接口补客户最高价 -> 调用现有审查逻辑 -> 输出 Excel。

安全约束（与 audit_saas_json_file.py 对齐）：
- 不自动登录，不绕验证码
- 不调用审批/驳回接口
- 不写数据库，不执行 SQL
- 不启用 Playwright，不启用 OCR
- 不爬商品页面
- 不持久化任何 cookie/token
- 不打印 cookie/token/auth-token/access-token/y1AmuISe
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.schemas.workflow import SaasSameProductApprovalItem, WorkflowAction
from app.workflows.work_01_saas_approval_json_audit import audit_saas_approval_records


SENSITIVE_HEADER_NAMES = {
    "cookie",
    "authorization",
    "auth-token",
    "access-token",
    "x-auth-token",
    "y1amuise",
}

DEFAULT_DOCUMENT_ID_FIELD = "documentId"
DEFAULT_CATEGORY_ID_FIELD = "fourCategoryId"

INPUT_BASE_FIELDS = [
    "documentId",
    "fourCategoryId",
    "vCommodityId",
    "queryStr",
    "sassVCommodityName",
    "skuId",
    "skuName",
    "vendorName",
    "shopName",
    "price",
    "extLink",
    "customerPrice",
]

RESULT_APPENDED_COLUMNS = [
    "fourCategoryId",
    "fourCategoryName",
    "customer_lookup_status",
    "list_reference_price",
    "customerPrice",
    "customerPriceStatus",
    "customerPriceSource",
    "customerPriceWarning",
    "customerPriceEvidence",
    "json_price",
    "title_match_result",
    "title_match_confidence",
    "title_match_reason",
    "sku_link_check",
    "price_check",
    "price_vs_customer_check",
    "primary_action",
    "suggested_actions",
    "reason",
    "evidence",
    "needs_human_approval",
    "execution_allowed",
    "executed",
    "page_price_value",
    "page_price_status",
    "page_price_source",
    "page_vs_customer_check",
    "over_30pct",
]

HIGHLIGHTED_RESULT_COLUMNS = {
    "customerPrice",
    "customerPriceStatus",
    "primary_action",
    "price_vs_customer_check",
    "page_price_value",
    "over_30pct",
}

# 英文字段名 → 中文显示名（仅影响 Excel 表头显示，不影响内部逻辑）
COLUMN_DISPLAY_NAMES = {
    # 原始 SaaS 列
    "vCommodityId": "SaaS商品ID",
    "documentId": "文档ID",
    "queryStr": "客户标题",
    "sassVCommodityName": "SaaS商品名称",
    "skuId": "SKU编号",
    "skuName": "互联网标题",
    "vendorName": "供应商",
    "shopName": "店铺名称",
    "price": "SaaS价格",
    "pageActualPrice": "页面实际价格",
    "extLink": "互联网链接",
    "commentNum": "评论数",
    "createTime": "创建时间",
    "updateTime": "更新时间",
    "modelAuditResult": "模型审核结果",
    "isMatch": "是否匹配",
    "customerPrice": "客户价",
    # 客户列
    "fourCategoryId": "四级类目ID",
    "fourCategoryName": "四级类目名称",
    "customer_lookup_status": "客户数据匹配状态",
    "list_reference_price": "客户列表参考价",
    # 客户价详情
    "customerPriceStatus": "客户价状态",
    "customerPriceSource": "客户价来源",
    "customerPriceWarning": "客户价警告",
    "customerPriceEvidence": "客户价证据",
    # 审查结果
    "json_price": "JSON价格",
    "title_match_result": "标题匹配结果",
    "title_match_confidence": "标题匹配置信度",
    "title_match_reason": "标题匹配原因",
    "sku_link_check": "SKU链接检查",
    "price_check": "价格检查",
    "price_vs_customer_check": "价格对比客户价",
    "primary_action": "建议操作",
    "suggested_actions": "建议操作列表",
    "reason": "原因",
    "evidence": "证据",
    "needs_human_approval": "需人工审批",
    "execution_allowed": "允许执行",
    "executed": "已执行",
    # 爬虫
    "page_price_value": "爬取页面价格",
    "page_price_status": "页面价格状态",
    "page_price_source": "页面价格来源",
    # 30% 高价
    "over_30pct": "超过客户价30%",
    # SAAS_LIST 特有
    "commodityName": "商品名称",
    "referenceLink": "参考链接",
    "categoryId": "类目ID",
    "categoryName": "类目名称",
    "materialCode": "物料编码",
    "materialDesc": "物料描述",
    "quasiStandardCommodityCode": "准标准商品编码",
}


def _display_header(name: str) -> str:
    """英文字段名 → 中文显示名；未映射的保持原文。"""
    return COLUMN_DISPLAY_NAMES.get(name, name)


# 枚举值 → 中文显示（仅影响 Excel 显示）
VALUE_DISPLAY_NAMES = {
    "APPROVE": "建议通过",
    "REJECT": "建议驳回",
    "MANUAL_CHECK": "人工核查",
    "WAIT_CRAWLER": "等待爬取",
    "SUPPLEMENT_SAME_PRODUCT": "补充同款",
    "DO_NOT_PUSH": "不推送",
    "FEEDBACK_CUSTOMER": "反馈客户",
    "READY_TO_PUSH": "准备推送",
    "IMPORT_CUSTOMER_LINKS": "导入客户链接",
    "UNBIND_WRONG_RELATIONS": "解绑错误关系",
    "RECALCULATE_INDEX": "重算指数",
    "PUSH_INDEX": "推送指数",
    "ASK_CUSTOMER_RECHECK": "请客户复核",
    "MANUAL_VERIFY": "人工验证",
    "FORWARD_TO_WORK01": "转发到Work01",
    "NO_ACTION": "无操作",
    "WAIT_COLLECTION": "等待采集",
    "RETRY_PUSH": "重试推送",
    "CONFIRM": "确认",
    "ALERT": "告警",
    "EXEMPTION": "豁免",
    "QUERY": "查询",
    "EXPORT": "导出",
    "FOUND": "已找到",
    "FOUND_SINGLE_REFERENCE": "仅找到单个参考价",
    "FOUND_CACHED": "已找到(缓存)",
    "EXISTS": "已存在",
    "NOT_PROVIDED": "未提供",
    "NOT_FOUND": "未找到",
    "LOOKUP_FAILED": "匹配失败",
    "DETAIL_REQUEST_FAILED": "详情请求失败",
    "DETAIL_REQUEST_SKIPPED": "详情请求跳过",
    "DETAIL_PARSE_FAILED": "详情解析失败",
    "MATCHED": "已匹配",
    "MATCHED_NO_CATEGORY": "已匹配但无类目",
    "SAME": "同款",
    "NOT_SAME": "非同款",
    "MANUAL_REVIEW": "人工复核",
    "MODEL_UNAVAILABLE": "模型不可用",
    "MATCH": "一致",
    "MISMATCH": "不一致",
    "UNKNOWN": "未知",
    "JSON_PRICE_MISSING": "JSON价格为空",
    "WAIT_PAGE_PRICE_CHECK": "待页面价格核验",
    "HIGHER_THAN_CUSTOMER_PRICE_30": "超过客户价30%",
    "WITHIN_THRESHOLD": "在阈值内",
    "CUSTOMER_PRICE_MISSING": "客户价缺失",
    "CUSTOMER_PRICE_INVALID": "客户价无效",
    "CANDIDATE_PRICE_MISSING": "候选价格缺失",
    "CANDIDATE_PRICE_INVALID": "候选价格无效",
    "PAGE_PRICE_MISSING": "页面价格缺失",
    "PENDING_CRAWL": "待爬取",
    "OK": "爬取成功",
    "FAILED": "爬取失败",
    "PRICE_MISSING": "价格不可见",
    "SKIPPED_JD": "京东跳过",
    "SKIPPED_NO_LINK": "无链接跳过",
    "SKIPPED_MODEL_NOT_SAME": "模型判非同款跳过",
    "SKIPPED": "跳过",
    "CRAWLER_PROCESS_FAILED": "爬虫进程失败",
    "TRUE": "是",
    "FALSE": "否",
}


def _display_value(value: Any) -> Any:
    """枚举值 → 中文显示。"""
    if isinstance(value, str) and value in VALUE_DISPLAY_NAMES:
        return VALUE_DISPLAY_NAMES[value]
    return value


HEADER_FONT = Font(bold=True)
HEADER_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
HIGHLIGHT_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
OVER_30_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")

JD_HOST_KEYWORDS = ("item.jd.com", "i-item.jd.com", "jd.com")

THIRTY_PERCENT_RATIO = Decimal("1.30")


# ---------------------------------------------------------------------------
# Excel 输入
# ---------------------------------------------------------------------------


def read_excel_rows(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """读取 xlsx 第一个 Sheet，返回 (列名顺序, 行字典列表)。"""
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError(f"只支持 xlsx 文件：{path}")
    workbook = load_workbook(filename=str(path), data_only=True, read_only=True)
    try:
        sheet = workbook.active
        if sheet is None:
            return [], []
        iterator = sheet.iter_rows(values_only=True)
        try:
            header_row = next(iterator)
        except StopIteration:
            return [], []
        headers: list[str] = []
        for index, value in enumerate(header_row):
            if value is None or str(value).strip() == "":
                headers.append(f"__col_{index}")
            else:
                headers.append(str(value).strip())
        rows: list[dict[str, Any]] = []
        for raw in iterator:
            if raw is None:
                continue
            if all(cell is None or (isinstance(cell, str) and cell.strip() == "") for cell in raw):
                continue
            row: dict[str, Any] = {}
            for header, cell in zip(headers, raw):
                row[header] = cell
            rows.append(row)
        return headers, rows
    finally:
        workbook.close()


def normalize_cell_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        text = format(value, "f").rstrip("0").rstrip(".")
        return text or "0"
    return str(value).strip()


def parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def is_valid_reference_price(value: Decimal | None) -> bool:
    return value is not None and value > 0


# ---------------------------------------------------------------------------
# SaaS 列表 JSON 输入
# ---------------------------------------------------------------------------


SAAS_LIST_HEADERS = [
    "documentId",
    "fourCategoryId",
    "fourCategoryName",
    "vCommodityId",
    "queryStr",
    "sassVCommodityName",
    "skuId",
    "skuName",
    "vendorName",
    "shopName",
    "price",
    "extLink",
    "commodityName",
    "list_reference_price",
    "referenceLink",
    "categoryId",
    "categoryName",
    "materialCode",
    "materialDesc",
    "quasiStandardCommodityCode",
]


def _saas_list_payload_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise ValueError("saas-list-json 必须是数组或包含 rows 的对象")
    for key in ("rows", "records", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            inner_rows = value.get("rows") if isinstance(value.get("rows"), list) else None
            if inner_rows is not None:
                return [item for item in inner_rows if isinstance(item, dict)]
    raise ValueError("saas-list-json 中未找到 rows 字段")


def _normalize_saas_list_row(raw: dict[str, Any]) -> dict[str, Any]:
    """把列表接口返回的原始 row 转成 audit 流程能识别的 row。"""
    document_id = normalize_cell_str(raw.get("documentId"))
    four_category_id = normalize_cell_str(raw.get("fourCategoryId"))
    four_category_name = normalize_cell_str(raw.get("fourCategoryName"))
    commodity_name = normalize_cell_str(raw.get("commodityName"))
    sass_v_name = normalize_cell_str(raw.get("sassVCommodityName"))
    reference_link = normalize_cell_str(raw.get("referenceLink"))
    list_reference_price = parse_decimal(raw.get("referencePrice"))
    query_str = sass_v_name or commodity_name
    sku_id = normalize_cell_str(raw.get("referenceId"))
    return {
        "documentId": document_id,
        "fourCategoryId": four_category_id,
        "fourCategoryName": four_category_name,
        "vCommodityId": document_id,
        "queryStr": query_str,
        "sassVCommodityName": sass_v_name,
        "skuId": sku_id,
        "skuName": commodity_name,
        "vendorName": "",
        "shopName": "",
        "price": list_reference_price,
        "extLink": reference_link,
        "commodityName": commodity_name,
        "list_reference_price": list_reference_price,
        "referenceLink": reference_link,
        "categoryId": normalize_cell_str(raw.get("categoryId")),
        "categoryName": normalize_cell_str(raw.get("categoryName")),
        "materialCode": normalize_cell_str(raw.get("materialCode")),
        "materialDesc": normalize_cell_str(raw.get("materialDesc")),
        "quasiStandardCommodityCode": normalize_cell_str(raw.get("quasiStandardCommodityCode")),
    }


def _heal_missing_closing_brace(text: str) -> str:
    """修复导出工具常见的缺最外层右花括号的多段 JSON。

    场景：上一段 JSON 末尾 `"success": true` 后直接换行接下一段 `{`，
    缺一个 `}` 让 json.decoder 误把后续内容当成同一对象的延续。
    """
    # 中间段："success": true|false 后紧跟换行 + 空白 + {  →  在两者之间补一个 }
    text = re.sub(
        r'("success"\s*:\s*(?:true|false))(\s*\n\s*)(\{)',
        r"\1}\2\3",
        text,
    )
    return text


def _parse_json_segments(text: str) -> list[Any]:
    """解析"多段 JSON 首尾相连"的文本。

    SaaS / 客户后台导出的 JSON 在分页保存时常常被首尾拼在同一个文件里，
    例如：
        {"rspCode": "0000", "rows": [...100 行...]}
        {"rspCode": "0000", "rows": [...100 行...]}
        {"rspCode": "0000", "rows": [...100 行...]}

    标准 json.load 只读第一段，会报 "Extra data"。
    本函数把每一段单独解析返回；同时容错部分导出工具漏写最外层 `}` 的情况。
    """
    text = _heal_missing_closing_brace(text)
    decoder = json.JSONDecoder()
    segments: list[Any] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t\r\n\ufeff":
            index += 1
        if index >= length:
            break
        # 兼容尾部孤立的 } / , 等冗余字符（导出工具偶尔多写一个右括号）
        if text[index] in "}],":
            index += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败（位置 {index}）：{exc}") from exc
        segments.append(obj)
        index = end
    return segments


def _load_json_file_segments(path: Path) -> list[Any]:
    if path.suffix.lower() != ".json":
        raise ValueError(f"输入必须是 JSON 文件：{path}")
    text = path.read_text(encoding="utf-8")
    return _parse_json_segments(text)


def _collect_rows_from_segments(segments: list[Any]) -> list[dict[str, Any]]:
    """把多段 payload 里的 rows 全部合并成一个列表。"""
    all_rows: list[dict[str, Any]] = []
    for segment in segments:
        all_rows.extend(_saas_list_payload_rows(segment))
    return all_rows


def read_saas_list_json(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """读取 SaaS 列表接口 response JSON（支持多段拼接），返回 (列名顺序, 行字典列表)。"""
    segments = _load_json_file_segments(path)
    raw_rows = _collect_rows_from_segments(segments)
    rows = [_normalize_saas_list_row(raw) for raw in raw_rows]
    return list(SAAS_LIST_HEADERS), rows


# ---------------------------------------------------------------------------
# SaaS pending JSON + 客户 JSON 联合输入
# ---------------------------------------------------------------------------


SAAS_PENDING_HEADERS = [
    "vCommodityId",
    "documentId",
    "queryStr",
    "sassVCommodityName",
    "skuId",
    "skuName",
    "vendorName",
    "shopName",
    "price",
    "pageActualPrice",
    "extLink",
    "commentNum",
    "createTime",
    "updateTime",
    "modelAuditResult",
    "isMatch",
]


def _normalize_saas_pending_row(raw: dict[str, Any]) -> dict[str, Any]:
    """SaaS 同款审批页面导出的一行 → 内部 row。

    主键：vCommodityId（与中核客户后台的 documentId 对应）。
    queryStr   = 客户标题
    skuName    = 互联网标题
    extLink    = 互联网链接（要爬的）
    """
    v_commodity_id = normalize_cell_str(raw.get("vCommodityId"))
    document_id = normalize_cell_str(raw.get("documentId"))
    query_str = normalize_cell_str(raw.get("queryStr"))
    if not query_str:
        query_str = normalize_cell_str(raw.get("sassVCommodityName"))
    sku_name = normalize_cell_str(raw.get("skuName"))
    if not sku_name:
        sku_name = normalize_cell_str(raw.get("commodityName"))
    ext_link = normalize_cell_str(raw.get("extLink"))
    if not ext_link:
        ext_link = normalize_cell_str(raw.get("referenceLink"))
    return {
        "vCommodityId": v_commodity_id,
        "documentId": document_id,
        "queryStr": query_str,
        "sassVCommodityName": normalize_cell_str(raw.get("sassVCommodityName")),
        "skuId": normalize_cell_str(raw.get("skuId")),
        "skuName": sku_name,
        "vendorName": normalize_cell_str(raw.get("vendorName")),
        "shopName": normalize_cell_str(raw.get("shopName")),
        "price": parse_decimal(raw.get("price")),
        "pageActualPrice": parse_decimal(raw.get("pageActualPrice")),
        "extLink": ext_link,
        "commentNum": normalize_cell_str(raw.get("commentNum")),
        "createTime": normalize_cell_str(raw.get("createTime")),
        "updateTime": normalize_cell_str(raw.get("updateTime")),
        "modelAuditResult": normalize_cell_str(raw.get("modelAuditResult")),
        "isMatch": normalize_cell_str(raw.get("isMatch")),
        # customerPrice 通常由 detail 接口反查得到；若 saas 输入直接提供（测试/手填场景）也透传
        "customerPrice": parse_decimal(raw.get("customerPrice")),
    }


def read_saas_pending_json(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """读取 SaaS 同款审批页面导出 JSON（支持多段拼接），返回 (列名顺序, 行字典列表)。"""
    segments = _load_json_file_segments(path)
    raw_rows = _collect_rows_from_segments(segments)
    rows = [_normalize_saas_pending_row(raw) for raw in raw_rows]
    return list(SAAS_PENDING_HEADERS), rows


def build_customer_index(path: Path) -> dict[str, dict[str, Any]]:
    """读取中核客户导出 JSON（支持多段拼接），返回 documentId -> 客户行 的映射。

    每条值至少包含：
      fourCategoryId
      fourCategoryName
      list_reference_price
    """
    segments = _load_json_file_segments(path)
    raw_rows = _collect_rows_from_segments(segments)
    index: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        document_id = normalize_cell_str(raw.get("documentId"))
        if not document_id:
            continue
        index[document_id] = {
            "fourCategoryId": normalize_cell_str(raw.get("fourCategoryId")),
            "fourCategoryName": normalize_cell_str(raw.get("fourCategoryName")),
            "list_reference_price": parse_decimal(raw.get("referencePrice")),
            "commodityName": normalize_cell_str(raw.get("commodityName")),
            "referenceLink": normalize_cell_str(raw.get("referenceLink")),
        }
    return index


def join_saas_with_customer(
    saas_rows: list[dict[str, Any]],
    customer_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """给 SaaS rows 回填 fourCategoryId/Name/list_reference_price/customer_lookup_status。

    Join key：saas.vCommodityId == customer.documentId
    回填后：
      row["documentId"] 取 customer.documentId（命中时）
      row["fourCategoryId"]、["fourCategoryName"]
      row["list_reference_price"]
      row["customer_lookup_status"]：MATCHED / NOT_FOUND
    """
    joined: list[dict[str, Any]] = []
    for row in saas_rows:
        merged = dict(row)
        join_key = normalize_cell_str(row.get("vCommodityId"))
        customer_row = customer_index.get(join_key) if join_key else None
        if customer_row is None:
            if not merged.get("documentId"):
                merged["documentId"] = ""
            merged["fourCategoryId"] = ""
            merged["fourCategoryName"] = ""
            merged["list_reference_price"] = None
            merged["customer_lookup_status"] = "NOT_FOUND"
        else:
            # 命中：documentId 用 customer 端的（语义上等于 saas.vCommodityId）
            merged["documentId"] = join_key
            merged["fourCategoryId"] = customer_row.get("fourCategoryId", "")
            merged["fourCategoryName"] = customer_row.get("fourCategoryName", "")
            merged["list_reference_price"] = customer_row.get("list_reference_price")
            merged["customer_lookup_status"] = (
                "MATCHED" if customer_row.get("fourCategoryId") else "MATCHED_NO_CATEGORY"
            )
        joined.append(merged)
    return joined


# ---------------------------------------------------------------------------
# 京东 / 链接判定
# ---------------------------------------------------------------------------


def is_jd_link(url: str) -> bool:
    """判断是否为京东链接（按 AGENT_GUIDE.md 规则需跳过）。"""
    text = (url or "").strip().lower()
    if not text:
        return False
    for keyword in JD_HOST_KEYWORDS:
        if keyword in text:
            return True
    return False



# ---------------------------------------------------------------------------
# Row -> SaasSameProductApprovalItem 适配
# ---------------------------------------------------------------------------


def build_record_payload(row: dict[str, Any]) -> dict[str, Any]:
    """将 Excel 行映射到 SaasSameProductApprovalItem 接受的字段。"""
    query_str = normalize_cell_str(row.get("queryStr"))
    if not query_str:
        query_str = normalize_cell_str(row.get("sassVCommodityName"))
    payload: dict[str, Any] = {
        "vCommodityId": normalize_cell_str(row.get("vCommodityId")),
        "queryStr": query_str,
        "skuId": normalize_cell_str(row.get("skuId")),
        "skuName": normalize_cell_str(row.get("skuName")),
        "vendorName": normalize_cell_str(row.get("vendorName")),
        "shopName": normalize_cell_str(row.get("shopName")),
        "extLink": normalize_cell_str(row.get("extLink")),
    }
    price = parse_decimal(row.get("price"))
    if price is not None:
        payload["price"] = price
    page_actual = parse_decimal(row.get("pageActualPrice"))
    if page_actual is not None:
        payload["pageActualPrice"] = page_actual
    customer_price = parse_decimal(row.get("customerPrice"))
    if customer_price is not None:
        payload["customerPrice"] = customer_price
    return payload


# ---------------------------------------------------------------------------
# 客户价提取
# ---------------------------------------------------------------------------


def extract_customer_price_from_detail(detail_payload: Any) -> dict[str, Any]:
    """根据详情返回提取客户价。"""
    if not isinstance(detail_payload, dict):
        return {
            "customerPrice": None,
            "customerPriceStatus": "DETAIL_PARSE_FAILED",
            "customerPriceSource": "",
            "customerPriceWarning": "详情接口返回不是 JSON 对象，无法解析参考价",
            "customerPriceEvidence": "",
        }

    data = detail_payload.get("data") if isinstance(detail_payload.get("data"), dict) else None
    rows: list[Any] = []
    if isinstance(detail_payload.get("rows"), list):
        rows = detail_payload["rows"]
    elif isinstance(data, dict) and isinstance(data.get("rows"), list):
        rows = data["rows"]

    reference_infos: list[dict[str, Any]] = []
    if isinstance(data, dict) and isinstance(data.get("referenceInfos"), list):
        reference_infos = [info for info in data["referenceInfos"] if isinstance(info, dict)]
    if not reference_infos:
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("referenceInfos"), list):
                reference_infos.extend(info for info in row["referenceInfos"] if isinstance(info, dict))

    valid_prices: list[Decimal] = []
    for info in reference_infos:
        price = parse_decimal(info.get("referencePrice"))
        if is_valid_reference_price(price):
            valid_prices.append(price)

    if len(valid_prices) >= 2:
        max_price = max(valid_prices)
        evidence_parts = [f"参考价{index}={price}" for index, price in enumerate(valid_prices, start=1)]
        evidence_parts.append(f"客户最高价={max_price}")
        return {
            "customerPrice": max_price,
            "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "",
            "customerPriceEvidence": "；".join(evidence_parts),
        }

    if len(valid_prices) == 1:
        only_price = valid_prices[0]
        return {
            "customerPrice": only_price,
            "customerPriceStatus": "FOUND_SINGLE_REFERENCE",
            "customerPriceSource": "DETAIL_SINGLE_REFERENCE",
            "customerPriceWarning": "当前详情结果只提取到1个参考价，请确认是否为完整客户价",
            "customerPriceEvidence": f"参考价1={only_price}；客户最高价={only_price}",
        }

    fallback_price: Decimal | None = None
    if isinstance(data, dict):
        candidate = parse_decimal(data.get("referencePrice"))
        if is_valid_reference_price(candidate):
            fallback_price = candidate
    if fallback_price is None:
        for row in rows:
            if isinstance(row, dict):
                candidate = parse_decimal(row.get("referencePrice"))
                if is_valid_reference_price(candidate):
                    fallback_price = candidate
                    break

    if fallback_price is not None:
        return {
            "customerPrice": fallback_price,
            "customerPriceStatus": "FOUND_SINGLE_REFERENCE",
            "customerPriceSource": "DETAIL_SINGLE_REFERENCE",
            "customerPriceWarning": "当前详情结果只提取到1个参考价，请确认是否为完整客户价",
            "customerPriceEvidence": f"参考价1={fallback_price}；客户最高价={fallback_price}",
        }

    return {
        "customerPrice": None,
        "customerPriceStatus": "NOT_FOUND",
        "customerPriceSource": "",
        "customerPriceWarning": "详情接口未返回有效参考价",
        "customerPriceEvidence": "",
    }


# ---------------------------------------------------------------------------
# cURL 解析与详情接口请求
# ---------------------------------------------------------------------------


_CARET_QUOTE = re.compile(r'\^"')
_CARET_BACKSLASH_QUOTE = re.compile(r'\^\\\^"')
_CARET_BACKSLASH = re.compile(r'\^\\')


def _strip_windows_caret(text: str) -> str:
    """去除 Windows cmd 风格的 ^ 续行与 ^" 转义，简化 cURL 解析。"""
    text = text.replace("^\r\n", " ").replace("^\n", " ").replace("^\r", " ")
    text = _CARET_BACKSLASH_QUOTE.sub('"', text)
    text = _CARET_QUOTE.sub('"', text)
    text = _CARET_BACKSLASH.sub("", text)
    text = text.replace('\\"', '"')
    return text


def _join_continuation_lines(text: str) -> str:
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    return text


def parse_curl(curl_text: str) -> dict[str, Any]:
    """从 cURL 文本中解析出 url、headers、cookie、data 模板。"""
    cleaned = _strip_windows_caret(curl_text)
    cleaned = _join_continuation_lines(cleaned)
    cleaned = cleaned.strip()
    if cleaned.lower().startswith("curl "):
        cleaned = cleaned[5:]
    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError as exc:
        raise ValueError(f"cURL 内容无法解析：{exc}") from exc

    url = ""
    headers: list[tuple[str, str]] = []
    cookie = ""
    data: str = ""
    method: str = ""

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-H", "--header") and index + 1 < len(tokens):
            value = tokens[index + 1]
            if ":" in value:
                name, _, raw_value = value.partition(":")
                headers.append((name.strip(), raw_value.strip()))
            index += 2
            continue
        if token in ("-b", "--cookie") and index + 1 < len(tokens):
            cookie = tokens[index + 1]
            index += 2
            continue
        if token in ("--data-raw", "--data", "--data-binary", "-d") and index + 1 < len(tokens):
            data = tokens[index + 1]
            index += 2
            continue
        if token in ("-X", "--request") and index + 1 < len(tokens):
            method = tokens[index + 1]
            index += 2
            continue
        if token in ("--compressed", "-L", "--location", "-k", "--insecure", "-s", "--silent", "-v", "--verbose"):
            index += 1
            continue
        if token.startswith("-"):
            # 未知开关，跳过下一个值（保守一致地处理）
            index += 2 if (index + 1 < len(tokens) and not tokens[index + 1].startswith("-")) else 1
            continue
        if not url:
            url = token
        index += 1

    if not url:
        raise ValueError("cURL 中未找到目标 URL")
    if not method:
        method = "POST" if data else "GET"
    return {
        "url": url,
        "headers": headers,
        "cookie": cookie,
        "data": data,
        "method": method.upper(),
    }


def load_curl_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return parse_curl(text)


def build_detail_request_body(template: str, document_id: str, four_category_id: str) -> bytes:
    """根据 cURL 模板构造单行详情接口请求体。"""
    template = (template or "").strip()
    if template:
        try:
            payload = json.loads(template)
            if isinstance(payload, dict):
                payload["documentId"] = document_id
                payload["fourCategoryId"] = four_category_id
                return json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except json.JSONDecodeError:
            pass
    payload = {"documentId": document_id, "fourCategoryId": four_category_id}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _safe_header_for_log(name: str) -> bool:
    return name.lower() not in SENSITIVE_HEADER_NAMES


def call_detail_api(
    curl_template: dict[str, Any],
    document_id: str,
    four_category_id: str,
    timeout: float = 15.0,
    opener: Any | None = None,
) -> dict[str, Any]:
    """调用中核详情接口。返回结构 {ok, payload, error}。

    - opener 仅用于测试注入；默认使用 urllib.request.urlopen。
    - 不打印任何敏感请求头/Cookie 内容。
    """
    body = build_detail_request_body(curl_template.get("data", ""), document_id, four_category_id)
    request = urllib.request.Request(
        url=curl_template["url"],
        data=body,
        method=curl_template.get("method", "POST") or "POST",
    )
    has_content_type = False
    for name, value in curl_template.get("headers", []):
        request.add_header(name, value)
        if name.lower() == "content-type":
            has_content_type = True
    if not has_content_type:
        request.add_header("Content-Type", "application/json")
    cookie = curl_template.get("cookie") or ""
    if cookie:
        request.add_header("Cookie", cookie)

    try:
        if opener is None:
            response = urllib.request.urlopen(request, timeout=timeout)
        else:
            response = opener(request, timeout=timeout)
        with response:
            raw = response.read()
        text = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return {"ok": False, "payload": None, "error": f"详情接口响应不是 JSON：{exc}"}
        return {"ok": True, "payload": payload, "error": ""}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "payload": None, "error": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "payload": None, "error": f"URL 错误：{exc.reason}"}
    except TimeoutError:
        return {"ok": False, "payload": None, "error": "详情接口超时"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "payload": None, "error": f"详情接口异常：{type(exc).__name__}"}


# ---------------------------------------------------------------------------
# 客户价补全主流程
# ---------------------------------------------------------------------------


def enrich_rows_with_customer_price(
    rows: list[dict[str, Any]],
    curl_template: dict[str, Any] | None,
    document_id_field: str,
    category_id_field: str,
    opener: Any | None = None,
    request_interval: float = 0.0,
    sleep_func: Any | None = None,
    progress_func: Any | None = None,
) -> list[dict[str, Any]]:
    """对每一行计算客户价信息，返回追加列字典列表。

    request_interval：每次成功/失败请求后等待秒数，避免被风控。
    sleep_func：测试可注入。
    progress_func(index, total, document_id, status)：测试或 CLI 可观察。
    """
    enriched: list[dict[str, Any]] = []
    sleep = sleep_func if sleep_func is not None else time.sleep
    total = len(rows)
    request_count = 0
    detail_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        existing_customer_price = parse_decimal(row.get("customerPrice"))
        if is_valid_reference_price(existing_customer_price):
            enriched.append({
                "customerPrice": existing_customer_price,
                "customerPriceStatus": "EXISTS",
                "customerPriceSource": "EXCEL_INPUT",
                "customerPriceWarning": "",
                "customerPriceEvidence": f"已提供客户价={existing_customer_price}",
            })
            if progress_func is not None:
                progress_func(index, total, normalize_cell_str(row.get(document_id_field)), "EXISTS")
            continue

        document_id = normalize_cell_str(row.get(document_id_field))
        four_category_id = normalize_cell_str(row.get(category_id_field))
        lookup_status = normalize_cell_str(row.get("customer_lookup_status"))
        if lookup_status == "NOT_FOUND":
            enriched.append({
                "customerPrice": None,
                "customerPriceStatus": "LOOKUP_FAILED",
                "customerPriceSource": "",
                "customerPriceWarning": "客户导出 JSON 中未找到该 documentId，已跳过详情请求",
                "customerPriceEvidence": "",
            })
            if progress_func is not None:
                progress_func(index, total, document_id, "LOOKUP_FAILED")
            continue

        if curl_template is None:
            enriched.append({
                "customerPrice": None,
                "customerPriceStatus": "NOT_PROVIDED",
                "customerPriceSource": "",
                "customerPriceWarning": "未提供 --curl-file 且输入未填写 customerPrice",
                "customerPriceEvidence": "",
            })
            if progress_func is not None:
                progress_func(index, total, document_id, "NOT_PROVIDED")
            continue

        if not document_id or not four_category_id:
            enriched.append({
                "customerPrice": None,
                "customerPriceStatus": "DETAIL_REQUEST_SKIPPED",
                "customerPriceSource": "",
                "customerPriceWarning": "缺少 documentId 或 fourCategoryId，跳过详情请求",
                "customerPriceEvidence": "",
            })
            if progress_func is not None:
                progress_func(index, total, document_id, "DETAIL_REQUEST_SKIPPED")
            continue

        cache_key = (document_id, four_category_id)
        cached = detail_cache.get(cache_key)
        if cached is not None:
            cached_copy = dict(cached)
            evidence = cached_copy.get("customerPriceEvidence", "")
            if evidence and "（detail 缓存命中）" not in evidence:
                cached_copy["customerPriceEvidence"] = evidence + "（detail 缓存命中）"
            enriched.append(cached_copy)
            if progress_func is not None:
                progress_func(index, total, document_id, cached_copy.get("customerPriceStatus", "") + "_CACHED")
            continue

        if request_count > 0 and request_interval > 0:
            sleep(request_interval)
        request_count += 1
        response = call_detail_api(curl_template, document_id, four_category_id, opener=opener)
        if not response.get("ok"):
            failed_info = {
                "customerPrice": None,
                "customerPriceStatus": "DETAIL_REQUEST_FAILED",
                "customerPriceSource": "",
                "customerPriceWarning": "详情接口请求失败，请检查 cURL 登录态是否过期",
                "customerPriceEvidence": "",
            }
            detail_cache[cache_key] = failed_info
            enriched.append(failed_info)
            if progress_func is not None:
                progress_func(index, total, document_id, "DETAIL_REQUEST_FAILED")
            continue
        result = extract_customer_price_from_detail(response.get("payload"))
        detail_cache[cache_key] = result
        enriched.append(result)
        if progress_func is not None:
            progress_func(index, total, document_id, result.get("customerPriceStatus", ""))
    return enriched


# ---------------------------------------------------------------------------
# 调用现有审查逻辑
# ---------------------------------------------------------------------------


def build_records(
    rows: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
) -> list[SaasSameProductApprovalItem]:
    records: list[SaasSameProductApprovalItem] = []
    for row, info in zip(rows, enriched):
        merged = dict(row)
        if info.get("customerPrice") is not None:
            merged["customerPrice"] = info["customerPrice"]
        payload = build_record_payload(merged)
        records.append(SaasSameProductApprovalItem(**payload))
    return records


def run_audit(
    records: list[SaasSameProductApprovalItem],
    use_model: bool = False,
) -> Any:
    return audit_saas_approval_records(
        records=records,
        use_model=use_model,
        use_link_audit=False,
        crawl_results=[],
        use_crawl_result=False,
    )


# ---------------------------------------------------------------------------
# Excel 输出
# ---------------------------------------------------------------------------


# 值翻译：英文枚举值 → 中文显示（仅影响 Excel 显示，不影响内部逻辑）
VALUE_DISPLAY_NAMES = {
    # WorkflowAction
    "APPROVE": "建议通过",
    "REJECT": "建议驳回",
    "MANUAL_CHECK": "人工核查",
    "WAIT_CRAWLER": "等待爬取",
    "SUPPLEMENT_SAME_PRODUCT": "补充同款",
    "DO_NOT_PUSH": "不推送",
    "FEEDBACK_CUSTOMER": "反馈客户",
    "READY_TO_PUSH": "准备推送",
    "IMPORT_CUSTOMER_LINKS": "导入客户链接",
    "UNBIND_WRONG_RELATIONS": "解绑错误关系",
    "RECALCULATE_INDEX": "重新计算指数",
    "PUSH_INDEX": "推送指数",
    "ASK_CUSTOMER_RECHECK": "请客户复核",
    "MANUAL_VERIFY": "人工核验",
    "FORWARD_TO_WORK01": "转交Work01",
    "NO_ACTION": "无操作",
    "WAIT_COLLECTION": "等待采集",
    "RETRY_PUSH": "重试推送",
    "CONFIRM": "确认",
    "ALERT": "告警",
    "EXEMPTION": "豁免",
    "QUERY": "查询",
    "EXPORT": "导出",
    # title_match_result
    "SAME": "同款",
    "NOT_SAME": "非同款",
    "MANUAL_REVIEW": "人工复核",
    "MODEL_UNAVAILABLE": "模型不可用",
    # customerPriceStatus
    "FOUND": "已找到",
    "FOUND_SINGLE_REFERENCE": "仅找到单个参考价",
    "FOUND_CACHED": "已找到(缓存)",
    "EXISTS": "已存在",
    "NOT_PROVIDED": "未提供",
    "NOT_FOUND": "未找到",
    "LOOKUP_FAILED": "匹配失败",
    "DETAIL_REQUEST_FAILED": "详情请求失败",
    "DETAIL_REQUEST_SKIPPED": "详情请求跳过",
    "DETAIL_PARSE_FAILED": "详情解析失败",
    "DETAIL_SINGLE_REFERENCE": "详情单参考价",
    "DETAIL_REFERENCE_INFOS": "详情多参考价",
    "EXCEL_INPUT": "Excel输入",
    # customer_lookup_status
    "MATCHED": "已匹配",
    "MATCHED_NO_CATEGORY": "已匹配(无类目)",
    # sku_link_check
    "MATCH": "一致",
    "MISMATCH": "不一致",
    "UNKNOWN": "未知",
    # price_check
    "JSON_PRICE_MISSING": "JSON价格为空",
    "WAIT_PAGE_PRICE_CHECK": "待页面价格核验",
    # price_vs_customer_check
    "WITHIN_THRESHOLD": "阈值内",
    "HIGHER_THAN_CUSTOMER_PRICE_30": "超过客户价30%",
    "CUSTOMER_PRICE_MISSING": "客户价缺失",
    "CUSTOMER_PRICE_INVALID": "客户价无效",
    "CANDIDATE_PRICE_MISSING": "候选价格缺失",
    "CANDIDATE_PRICE_INVALID": "候选价格无效",
    "PAGE_PRICE_MISSING": "页面价格缺失",
    # page_price_status
    "PENDING_CRAWL": "待爬取",
    "SKIPPED_JD": "跳过(京东)",
    "SKIPPED_NO_LINK": "跳过(无链接)",
    "SKIPPED_MODEL_NOT_SAME": "跳过(模型判非同款)",
    "SKIPPED": "跳过",
    "OK": "成功",
    "FAILED": "失败",
    "CRAWLER_PROCESS_FAILED": "爬虫进程失败",
    "PRICE_MISSING": "价格不可见",
    # over_30pct
    "TRUE": "是",
    "FALSE": "否",
}


def _translate_value(value: Any) -> Any:
    """把英文枚举值翻译成中文显示。"""
    if isinstance(value, str):
        return VALUE_DISPLAY_NAMES.get(value, value)
    return value


def _format_cell_value(value: Any) -> Any:
    if value is None:
        return ""
    value = _display_value(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, WorkflowAction):
        return _display_value(value.value)
    if isinstance(value, list):
        rendered: list[str] = []
        for item in value:
            if isinstance(item, WorkflowAction):
                rendered.append(_display_value(item.value))
            else:
                rendered.append(str(_display_value(item)))
        return "；".join(rendered)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return value


def _build_summary_counts(audit_response: Any, enriched: list[dict[str, Any]]) -> dict[str, Any]:
    summary = audit_response.summary or {}
    record_results = audit_response.record_results or []
    customer_price_missing = sum(
        1 for info in enriched
        if info.get("customerPriceStatus") in {"NOT_PROVIDED", "DETAIL_REQUEST_SKIPPED", "NOT_FOUND", "DETAIL_PARSE_FAILED"}
    )
    customer_price_failed = sum(
        1 for info in enriched if info.get("customerPriceStatus") == "DETAIL_REQUEST_FAILED"
    )
    customer_price_found = sum(
        1 for info in enriched if info.get("customerPriceStatus") == "FOUND"
    )
    customer_price_single = sum(
        1 for info in enriched if info.get("customerPriceStatus") == "FOUND_SINGLE_REFERENCE"
    )
    return {
        "总记录数": int(summary.get("total_records", len(record_results))),
        "建议通过数量": int(summary.get("approve_suggestion_count", 0)),
        "人工核查数量": int(summary.get("manual_check_count", 0)),
        "建议驳回数量": int(summary.get("reject_suggestion_count", 0)),
        "客户价高于30%数量": int(summary.get("higher_than_customer_price_30_count", 0)),
        "客户价FOUND数量": customer_price_found,
        "客户价SINGLE数量": customer_price_single,
        "客户价缺失数量": customer_price_missing,
        "客户价请求失败数量": customer_price_failed,
        "价格不一致数量": int(summary.get("price_mismatch_count", 0)),
        "输出时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _set_column_widths(sheet: Any, headers: list[str]) -> None:
    for index, header in enumerate(headers, start=1):
        letter = get_column_letter(index)
        display = _display_header(header)
        width = min(max(len(display) * 2 + 4, 12), 60)
        if header in {"reason", "evidence", "customerPriceEvidence", "extLink", "queryStr", "skuName", "customerPriceWarning", "title_match_reason"}:
            width = 50
        sheet.column_dimensions[letter].width = width


def _write_summary_sheet(workbook: Workbook, summary_counts: dict[str, Any]) -> None:
    sheet = workbook.active
    sheet.title = "summary"
    headers = list(summary_counts.keys())
    sheet.append(headers)
    sheet.append([_format_cell_value(summary_counts[key]) for key in headers])
    for column_index, _ in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=column_index)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"
    _set_column_widths(sheet, headers)


def _collect_title_reason(audit_result):
    if audit_result is None:
        return ""
    chunks = []
    for item in audit_result.evidence or []:
        text = str(item)
        if text.startswith("title_model.reason="):
            chunks.append(text[len("title_model.reason="):])
        elif text.startswith("title_model.risk="):
            chunks.append("risk=" + text[len("title_model.risk="):])
    return ";".join(chunks)


def _compute_page_price_status(row, audit_result):
    # 如果已被 apply_crawl_results_to_rows 写入最终结果，直接用
    if "__page_price_status" in row:
        return {
            "page_price_value": row.get("__page_price_value"),
            "page_price_status": row.get("__page_price_status", ""),
            "page_price_source": row.get("__page_price_source", ""),
        }
    ext_link = normalize_cell_str(row.get("extLink"))
    if not ext_link:
        return {
            "page_price_value": None,
            "page_price_status": "SKIPPED_NO_LINK",
            "page_price_source": "",
        }
    if is_jd_link(ext_link):
        return {
            "page_price_value": None,
            "page_price_status": "SKIPPED_JD",
            "page_price_source": "JD",
        }
    if audit_result is not None and audit_result.title_match_result == "NOT_SAME":
        return {
            "page_price_value": None,
            "page_price_status": "SKIPPED_MODEL_NOT_SAME",
            "page_price_source": "",
        }
    return {
        "page_price_value": None,
        "page_price_status": "PENDING_CRAWL",
        "page_price_source": "",
    }


def compute_over_30_check(customer_price, page_price, page_price_status):
    pending_statuses = {
        "SKIPPED_JD",
        "SKIPPED_NO_LINK",
        "SKIPPED_MODEL_NOT_SAME",
        "SKIPPED",
        "PENDING_CRAWL",
        "FAILED",
        "CRAWLER_PROCESS_FAILED",
        "PRICE_MISSING",
    }
    if page_price_status in pending_statuses:
        return ("PAGE_PRICE_MISSING", "")
    if page_price is None or page_price <= 0:
        return ("PAGE_PRICE_MISSING", "")
    if customer_price is None or customer_price <= 0:
        return ("CUSTOMER_PRICE_MISSING", "")
    threshold = customer_price * THIRTY_PERCENT_RATIO
    if page_price > threshold:
        return ("HIGHER_THAN_CUSTOMER_PRICE_30", "TRUE")
    return ("WITHIN_THRESHOLD", "FALSE")


def _write_record_results_sheet(
    workbook: Workbook,
    headers: list[str],
    rows: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
    audit_response: Any,
) -> None:
    sheet = workbook.create_sheet("record_results")
    base_headers = [header for header in headers if header not in RESULT_APPENDED_COLUMNS]
    final_headers = base_headers + RESULT_APPENDED_COLUMNS
    display_headers = [_display_header(h) for h in final_headers]
    sheet.append(display_headers)
    for column_index, header in enumerate(display_headers, start=1):
        cell = sheet.cell(row=1, column=column_index)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN

    record_results = list(audit_response.record_results or [])
    if len(record_results) != len(rows):
        while len(record_results) < len(rows):
            record_results.append(None)

    for row_index, (row, info, audit_result) in enumerate(zip(rows, enriched, record_results), start=2):
        merged: dict[str, Any] = {}
        for header in base_headers:
            merged[header] = row.get(header, "")

        merged["fourCategoryId"] = row.get("fourCategoryId", "")
        merged["fourCategoryName"] = row.get("fourCategoryName", "")
        merged["customer_lookup_status"] = row.get("customer_lookup_status", "")
        merged["list_reference_price"] = row.get("list_reference_price")

        merged["customerPrice"] = info.get("customerPrice")
        merged["customerPriceStatus"] = info.get("customerPriceStatus", "")
        merged["customerPriceSource"] = info.get("customerPriceSource", "")
        merged["customerPriceWarning"] = info.get("customerPriceWarning", "")
        merged["customerPriceEvidence"] = info.get("customerPriceEvidence", "")

        if audit_result is not None:
            merged["json_price"] = audit_result.json_price
            merged["title_match_result"] = audit_result.title_match_result
            merged["title_match_confidence"] = audit_result.title_match_confidence
            merged["title_match_reason"] = _collect_title_reason(audit_result)
            merged["sku_link_check"] = audit_result.sku_link_check
            merged["price_check"] = audit_result.price_check
            merged["price_vs_customer_check"] = audit_result.price_vs_customer_check
            merged["primary_action"] = audit_result.primary_action
            merged["suggested_actions"] = audit_result.suggested_actions
            merged["reason"] = audit_result.reason
            merged["evidence"] = audit_result.evidence
            merged["needs_human_approval"] = audit_result.needs_human_approval
            merged["execution_allowed"] = audit_result.execution_allowed
            merged["executed"] = audit_result.executed
        else:
            for column in (
                "json_price",
                "title_match_result",
                "title_match_confidence",
                "title_match_reason",
                "sku_link_check",
                "price_check",
                "price_vs_customer_check",
                "primary_action",
                "suggested_actions",
                "reason",
                "evidence",
                "needs_human_approval",
                "execution_allowed",
                "executed",
            ):
                merged[column] = ""

        page_status_info = _compute_page_price_status(row, audit_result)
        merged["page_price_value"] = page_status_info["page_price_value"]
        merged["page_price_status"] = page_status_info["page_price_status"]
        merged["page_price_source"] = page_status_info["page_price_source"]

        page_vs_check, over_30 = compute_over_30_check(
            customer_price=info.get("customerPrice"),
            page_price=page_status_info["page_price_value"],
            page_price_status=page_status_info["page_price_status"],
        )
        merged["page_vs_customer_check"] = page_vs_check
        merged["over_30pct"] = over_30

        row_is_over = over_30 == "TRUE"
        for column_index, header in enumerate(final_headers, start=1):
            cell = sheet.cell(row=row_index, column=column_index)
            cell.value = _format_cell_value(merged.get(header, ""))
            if row_is_over:
                cell.fill = OVER_30_FILL
            elif header in HIGHLIGHTED_RESULT_COLUMNS:
                cell.fill = HIGHLIGHT_FILL

    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(final_headers))}{max(len(rows) + 1, 1)}"
    _set_column_widths(sheet, final_headers)


def write_audit_workbook(
    output_path: Path,
    headers: list[str],
    rows: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
    audit_response: Any,
) -> None:
    if output_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError(f"输出文件必须是 xlsx：{output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    summary_counts = _build_summary_counts(audit_response, enriched)
    _write_summary_sheet(workbook, summary_counts)
    _write_record_results_sheet(workbook, headers, rows, enriched, audit_response)
    workbook.save(str(output_path))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地 SaaS 待审批 Excel 审查脚本")
    parser.add_argument("--input-xlsx", type=Path, default=None)
    parser.add_argument("--input-saas-list-json", type=Path, default=None,
                        help="单文件模式：客户列表接口 response JSON，自动取 documentId+fourCategoryId")
    parser.add_argument("--input-saas-json", type=Path, default=None,
                        help="双文件模式：SaaS 同款审批页面导出的 JSON（含客户标题/互联网标题/extLink）")
    parser.add_argument("--input-customer-json", type=Path, default=None,
                        help="双文件模式：中核客户导出的 JSON（用 documentId 反查 fourCategoryId）")
    parser.add_argument("--output-xlsx", required=True, type=Path)
    parser.add_argument("--curl-file", type=Path, default=None)
    parser.add_argument("--document-id-field", default=DEFAULT_DOCUMENT_ID_FIELD)
    parser.add_argument("--category-id-field", default=DEFAULT_CATEGORY_ID_FIELD)
    parser.add_argument("--request-interval", type=float, default=0.5,
                        help="详情接口每条之间等待秒数，默认 0.5")
    parser.add_argument("--use-model", action="store_true",
                        help="启用 SaaS 标题模型校验（需要 TITLE_MATCH_MODEL_API_KEY/BASE_URL 环境变量）")
    parser.add_argument("--enable-crawler", action="store_true",
                        help="启用多商城爬虫；不启用则 page_price_status=PENDING_CRAWL 占位")
    parser.add_argument("--crawler-script", type=Path, default=DEFAULT_CRAWLER_SCRIPT,
                        help="multi_mall_crawler/main.py 路径")
    parser.add_argument("--crawler-headless", action="store_true",
                        help="爬虫无头模式（生产推荐打开）")
    parser.add_argument("--crawler-concurrency", type=int, default=3)
    parser.add_argument("--crawler-per-domain-delay", type=float, default=1.5)
    parser.add_argument("--crawler-timeout-ms", type=int, default=18000)
    parser.add_argument("--skip-dry-run", action="store_true",
                        help="跳过 dry-run 直接正式爬（默认会先 dry-run）")
    parser.add_argument("--excluded-output-dir", type=Path,
                        default=ROOT / "爬取模板",
                        help="爬取模板输出根目录（自动追加 YYYY-MM-DD 子目录）")
    parser.add_argument("--no-excluded-export", action="store_true",
                        help="跳过京东/震坤行爬取模板的导出")
    return parser


def print_summary(summary_counts: dict[str, Any], output_path: Path) -> None:
    for key, value in summary_counts.items():
        print(f"{key}: {value}")
    print(f"输出文件路径: {output_path}")


def _cli_progress(index: int, total: int, document_id: str, status: str) -> None:
    print(f"[{index}/{total}] documentId={document_id} status={status}")


def _select_input_mode(input_xlsx, input_saas_list_json, input_saas_json, input_customer_json):
    if input_customer_json is not None and input_saas_json is None:
        raise ValueError("--input-customer-json 必须配合 --input-saas-json 使用")
    flags = [
        ("input_xlsx", input_xlsx),
        ("input_saas_list_json", input_saas_list_json),
        ("input_saas_json", input_saas_json),
    ]
    active = [name for name, value in flags if value is not None]
    if not active:
        raise ValueError("必须指定 --input-xlsx 或 --input-saas-list-json 或 --input-saas-json")
    if len(active) > 1:
        raise ValueError("--input-xlsx / --input-saas-list-json / --input-saas-json 互斥，三选一")
    if input_saas_json is not None and input_customer_json is None:
        raise ValueError("--input-saas-json 必须配合 --input-customer-json 使用")
    return active[0]


def run_main(
    output_xlsx: Path,
    input_xlsx: Path | None = None,
    input_saas_list_json: Path | None = None,
    input_saas_json: Path | None = None,
    input_customer_json: Path | None = None,
    curl_file: Path | None = None,
    document_id_field: str = DEFAULT_DOCUMENT_ID_FIELD,
    category_id_field: str = DEFAULT_CATEGORY_ID_FIELD,
    request_interval: float = 0.0,
    use_model: bool = False,
    enable_crawler: bool = False,
    crawler_script: Path | None = None,
    crawler_headless: bool = True,
    crawler_concurrency: int = 3,
    crawler_per_domain_delay: float = 1.5,
    crawler_timeout_ms: int = 18000,
    skip_dry_run: bool = False,
    excluded_output_dir: Path | None = None,
    excluded_export_disabled: bool = False,
    crawler_runner: Any = None,
    opener: Any | None = None,
    sleep_func: Any | None = None,
    progress_func: Any | None = None,
) -> dict[str, Any]:
    mode = _select_input_mode(input_xlsx, input_saas_list_json, input_saas_json, input_customer_json)

    # 双 JSON 模式 + 未禁用 → 先生成京东/震坤行爬取模板（公司/人工系统处理）
    # AUDIT_SUPPRESS_EXCLUDED_EXPORT=1 可全局禁用（用于 pytest 等测试环境，避免污染真实目录）
    import os as _os
    env_suppress = _os.environ.get("AUDIT_SUPPRESS_EXCLUDED_EXPORT", "").strip() == "1"
    excluded_export_summary: dict[str, Any] = {"enabled": False}
    if (
        mode == "input_saas_json"
        and not excluded_export_disabled
        and not env_suppress
        and input_saas_json is not None
    ):
        out_root = excluded_output_dir if excluded_output_dir is not None else (ROOT / "爬取模板")
        try:
            # 延迟 import 防止 run_main 不需要这条路径时也加载 openpyxl 二次
            from scripts.export_excluded_malls import export_excluded_templates
            print(f"[excluded-export] 输出根目录: {out_root}")
            excluded_export_summary = export_excluded_templates(
                input_saas_json=input_saas_json,
                output_root=out_root,
            )
            excluded_export_summary["enabled"] = True
            for name, info in excluded_export_summary.items():
                if name == "_meta" or name == "enabled":
                    continue
                if isinstance(info, dict) and "matched" in info:
                    print(
                        f"[excluded-export] {name}: 命中 {info['matched']} 行，"
                        f"去重后 {info['deduped']} 行 → {info['path']}"
                    )
        except Exception as exc:  # noqa: BLE001
            # 模板导出失败不阻塞主审计
            print(f"[excluded-export] 失败：{type(exc).__name__}: {exc}")
            excluded_export_summary = {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}

    if mode == "input_saas_json":
        headers, rows = read_saas_pending_json(input_saas_json)
        customer_index = build_customer_index(input_customer_json)
        rows = join_saas_with_customer(rows, customer_index)
        source_path = input_saas_json
    elif mode == "input_saas_list_json":
        headers, rows = read_saas_list_json(input_saas_list_json)
        source_path = input_saas_list_json
    else:
        headers, rows = read_excel_rows(input_xlsx)
        source_path = input_xlsx

    if not rows:
        raise ValueError(f"输入没有数据行：{source_path}")

    curl_template = load_curl_file(curl_file) if curl_file is not None else None
    enriched = enrich_rows_with_customer_price(
        rows,
        curl_template,
        document_id_field,
        category_id_field,
        opener=opener,
        request_interval=request_interval,
        sleep_func=sleep_func,
        progress_func=progress_func,
    )
    records = build_records(rows, enriched)
    audit_response = run_audit(records, use_model=use_model)

    crawler_summary: dict[str, Any] = {"enabled": enable_crawler}
    if enable_crawler:
        urls_to_crawl = collect_urls_for_crawl(rows, enriched, audit_response)
        crawler_summary["urls_total"] = len(urls_to_crawl)
        if not urls_to_crawl:
            print("[crawler] 没有需要爬取的链接（PENDING_CRAWL=0），跳过爬虫")
            apply_crawl_results_to_rows(rows, enriched, audit_response, crawl_results_by_url={}, crawler_process_failed=False)
        else:
            script_path = crawler_script if crawler_script is not None else DEFAULT_CRAWLER_SCRIPT
            runtime_dir = output_xlsx.parent if output_xlsx.parent != Path("") else Path("./runtime")
            runtime_dir.mkdir(parents=True, exist_ok=True)
            input_xlsx_path = runtime_dir / CRAWL_INPUT_FILENAME
            dry_run_xlsx_path = runtime_dir / CRAWL_DRY_RUN_FILENAME
            output_xlsx_path = runtime_dir / CRAWL_OUTPUT_FILENAME

            print(f"[crawler] 待爬 URL 数（去重后）：{len(urls_to_crawl)}")
            print(f"[crawler] 输入 xlsx：{input_xlsx_path}")
            write_crawler_input_xlsx(urls_to_crawl, input_xlsx_path)

            crawler_process_failed = False

            if not skip_dry_run:
                print(f"[crawler] dry-run 验证链接识别...")
                dry_result = run_crawler_subprocess(
                    crawler_script=script_path,
                    input_xlsx=input_xlsx_path,
                    output_xlsx=dry_run_xlsx_path,
                    dry_run=True,
                    headless=crawler_headless,
                    concurrency=crawler_concurrency,
                    per_domain_delay=crawler_per_domain_delay,
                    timeout_ms=crawler_timeout_ms,
                    limit=len(urls_to_crawl),
                    runner=crawler_runner,
                )
                crawler_summary["dry_run"] = dry_result
                if not dry_result["ok"]:
                    print(f"[crawler] dry-run 失败：{dry_result['error']}")
                    crawler_process_failed = True

            if not crawler_process_failed:
                print(f"[crawler] 正式爬取...")
                run_result = run_crawler_subprocess(
                    crawler_script=script_path,
                    input_xlsx=input_xlsx_path,
                    output_xlsx=output_xlsx_path,
                    dry_run=False,
                    headless=crawler_headless,
                    concurrency=crawler_concurrency,
                    per_domain_delay=crawler_per_domain_delay,
                    timeout_ms=crawler_timeout_ms,
                    limit=len(urls_to_crawl),
                    runner=crawler_runner,
                )
                crawler_summary["run"] = run_result
                if not run_result["ok"]:
                    print(f"[crawler] 正式爬取失败：{run_result['error']}")
                    crawler_process_failed = True

            crawl_results_by_url: dict[str, dict[str, Any]] = {}
            if not crawler_process_failed:
                try:
                    crawl_results_by_url = read_crawler_output(output_xlsx_path)
                    print(f"[crawler] 读回 {len(crawl_results_by_url)} 条结果")
                except Exception as exc:  # noqa: BLE001
                    print(f"[crawler] 读取爬虫输出失败：{type(exc).__name__}: {exc}")
                    crawler_process_failed = True

            apply_crawl_results_to_rows(
                rows,
                enriched,
                audit_response,
                crawl_results_by_url=crawl_results_by_url,
                crawler_process_failed=crawler_process_failed,
            )
            crawler_summary["crawler_process_failed"] = crawler_process_failed
            crawler_summary["urls_returned"] = len(crawl_results_by_url)

    write_audit_workbook(output_xlsx, headers, rows, enriched, audit_response)
    summary_counts = _build_summary_counts(audit_response, enriched)
    return {
        "summary_counts": summary_counts,
        "audit_response": audit_response,
        "enriched": enriched,
        "headers": headers,
        "rows": rows,
        "mode": mode,
        "crawler": crawler_summary,
        "excluded_export": excluded_export_summary,
    }


def _load_env_file(path: Path) -> dict[str, str]:
    """读取一个简单 KEY=VALUE 风格 .env 文件，注入到 os.environ。

    安全：
      - 不打印任何 VALUE
      - 文件不存在不报错（跳过）
      - 已存在的环境变量不会被覆盖（防止误覆盖系统级配置）
      - 只识别 ALLOWED_KEYS 内的键，无关键值忽略
    返回：{key: "SET" / "EXISTING" / "EMPTY"} 用于状态打印
    """
    import os as _os
    allowed_keys = {
        "TITLE_MATCH_MODEL_API_KEY",
        "TITLE_MATCH_MODEL_BASE_URL",
        "TITLE_MATCH_MODEL_NAME",
    }
    status: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return status
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return status
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key not in allowed_keys:
            continue
        if not value:
            status[key] = "EMPTY"
            continue
        if _os.environ.get(key):
            status[key] = "EXISTING"
            continue
        _os.environ[key] = value
        status[key] = "SET"
    return status


def _print_model_env_status() -> None:
    """打印模型环境变量状态，KEY 只显示是否存在，不显示值。"""
    import os as _os
    has_key = bool(_os.environ.get("TITLE_MATCH_MODEL_API_KEY", "").strip())
    base_url = _os.environ.get("TITLE_MATCH_MODEL_BASE_URL", "").strip()
    model_name = _os.environ.get("TITLE_MATCH_MODEL_NAME", "").strip()
    print(f"[model] TITLE_MATCH_MODEL_API_KEY: {'SET' if has_key else 'NOT_SET'}")
    print(f"[model] TITLE_MATCH_MODEL_BASE_URL: {base_url or 'NOT_SET'}")
    print(f"[model] TITLE_MATCH_MODEL_NAME: {model_name or 'NOT_SET'}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 先加载本地 .env（不打印 KEY 值）
    env_file = ROOT / "data" / "model.env"
    _load_env_file(env_file)
    if args.use_model:
        _print_model_env_status()
    result = run_main(
        input_xlsx=args.input_xlsx,
        input_saas_list_json=args.input_saas_list_json,
        input_saas_json=args.input_saas_json,
        input_customer_json=args.input_customer_json,
        output_xlsx=args.output_xlsx,
        curl_file=args.curl_file,
        document_id_field=args.document_id_field,
        category_id_field=args.category_id_field,
        request_interval=args.request_interval,
        use_model=args.use_model,
        enable_crawler=args.enable_crawler,
        crawler_script=args.crawler_script,
        crawler_headless=args.crawler_headless,
        crawler_concurrency=args.crawler_concurrency,
        crawler_per_domain_delay=args.crawler_per_domain_delay,
        crawler_timeout_ms=args.crawler_timeout_ms,
        skip_dry_run=args.skip_dry_run,
        excluded_output_dir=args.excluded_output_dir,
        excluded_export_disabled=args.no_excluded_export,
        progress_func=_cli_progress,
    )
    print_summary(result["summary_counts"], args.output_xlsx)
    return 0


# ---------------------------------------------------------------------------
# 多商城爬虫子进程接入（按 multi_mall_crawler/AGENT_GUIDE.md）
# ---------------------------------------------------------------------------


import subprocess as _subprocess  # noqa: E402

DEFAULT_CRAWLER_SCRIPT = Path(r"E:\.A2026\多商城爬取\multi_mall_crawler\main.py")
CRAWL_INPUT_FILENAME = "_crawl_input.xlsx"
CRAWL_DRY_RUN_FILENAME = "_crawl_dry_run.xlsx"
CRAWL_OUTPUT_FILENAME = "_crawl_output.xlsx"

CRAWLER_OUTPUT_URL_HEADER = "原始链接"
CRAWLER_OUTPUT_PRICE_HEADER = "商品价格"
CRAWLER_OUTPUT_MALL_HEADER = "识别商城"
CRAWLER_OUTPUT_STATUS_HEADER = "采集状态"
CRAWLER_OUTPUT_REASON_HEADER = "失败原因"


def _normalize_url(url: Any) -> str:
    if url is None:
        return ""
    return str(url).strip()


def collect_urls_for_crawl(
    rows: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
    audit_response: Any,
) -> list[str]:
    """收集 page_price_status == PENDING_CRAWL 的所有 URL，按原顺序去重。"""
    record_results = list(audit_response.record_results or []) if audit_response is not None else []
    while len(record_results) < len(rows):
        record_results.append(None)

    seen: set[str] = set()
    urls: list[str] = []
    for row, info, audit_result in zip(rows, enriched, record_results):
        pre = _compute_page_price_status(row, audit_result)
        if pre["page_price_status"] != "PENDING_CRAWL":
            continue
        url = _normalize_url(row.get("extLink"))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def write_crawler_input_xlsx(urls: list[str], path: Path) -> None:
    """写爬虫输入 xlsx，单列 URL（表头 'URL'）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "links"
    sheet.append(["URL"])
    for url in urls:
        sheet.append([url])
    workbook.save(str(path))


def _resolve_crawler_python_executable() -> str:
    return sys.executable or "python"


def _build_crawler_argv(
    crawler_script: Path,
    input_xlsx: Path,
    output_xlsx: Path,
    *,
    dry_run: bool,
    headless: bool,
    concurrency: int,
    per_domain_delay: float,
    timeout_ms: int,
    limit: int,
) -> list[str]:
    # 爬虫子进程 cwd = 爬虫脚本目录，所有输入/输出路径必须用绝对路径
    abs_input = str(input_xlsx.resolve())
    abs_output = str(output_xlsx.resolve())
    argv = [
        _resolve_crawler_python_executable(),
        str(crawler_script.resolve()),
        "--input", abs_input,
        "--output", abs_output,
        "--limit", str(max(limit, 1)),
        "--concurrency", str(concurrency),
        "--per-domain-delay", str(per_domain_delay),
        "--timeout", str(timeout_ms),
    ]
    if dry_run:
        argv.append("--dry-run")
    if headless:
        argv.append("--headless")
    return argv


def run_crawler_subprocess(
    crawler_script: Path,
    input_xlsx: Path,
    output_xlsx: Path,
    *,
    dry_run: bool = False,
    headless: bool = True,
    concurrency: int = 3,
    per_domain_delay: float = 1.5,
    timeout_ms: int = 18000,
    limit: int = 100,
    runner: Any = None,
) -> dict[str, Any]:
    """子进程调爬虫 main.py。

    runner: 测试可注入，签名 (argv, cwd) -> int（exit_code）。
    返回 {ok, exit_code, error}
    """
    if not crawler_script.exists():
        return {"ok": False, "exit_code": -1, "error": f"爬虫脚本不存在：{crawler_script}"}

    argv = _build_crawler_argv(
        crawler_script,
        input_xlsx,
        output_xlsx,
        dry_run=dry_run,
        headless=headless,
        concurrency=concurrency,
        per_domain_delay=per_domain_delay,
        timeout_ms=timeout_ms,
        limit=limit,
    )
    cwd = str(crawler_script.parent)

    if runner is not None:
        try:
            exit_code = int(runner(argv, cwd))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "exit_code": -1, "error": f"爬虫 runner 异常：{type(exc).__name__}"}
        return {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "error": "" if exit_code == 0 else f"爬虫退出码 {exit_code}",
        }

    try:
        proc = _subprocess.run(argv, cwd=cwd, check=False)
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "error": "" if proc.returncode == 0 else f"爬虫退出码 {proc.returncode}",
        }
    except FileNotFoundError as exc:
        return {"ok": False, "exit_code": -1, "error": f"无法启动爬虫：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": -1, "error": f"爬虫子进程异常：{type(exc).__name__}"}


def read_crawler_output(path: Path) -> dict[str, dict[str, Any]]:
    """读爬虫输出 xlsx，返回 url -> {page_price_value,page_price_status,page_price_source}"""
    if not path.exists():
        raise FileNotFoundError(f"爬虫输出 xlsx 不存在：{path}")
    workbook = load_workbook(filename=str(path), data_only=True, read_only=True)
    try:
        sheet = workbook.active
        if sheet is None:
            return {}
        iterator = sheet.iter_rows(values_only=True)
        try:
            header_row = next(iterator)
        except StopIteration:
            return {}
        headers = [str(value).strip() if value is not None else "" for value in header_row]

        def _idx(name: str) -> int:
            try:
                return headers.index(name)
            except ValueError:
                return -1

        url_idx = _idx(CRAWLER_OUTPUT_URL_HEADER)
        if url_idx < 0:
            return {}
        price_idx = _idx(CRAWLER_OUTPUT_PRICE_HEADER)
        mall_idx = _idx(CRAWLER_OUTPUT_MALL_HEADER)
        status_idx = _idx(CRAWLER_OUTPUT_STATUS_HEADER)
        reason_idx = _idx(CRAWLER_OUTPUT_REASON_HEADER)

        results: dict[str, dict[str, Any]] = {}
        for raw in iterator:
            if raw is None:
                continue
            url = ""
            if 0 <= url_idx < len(raw) and raw[url_idx] is not None:
                url = str(raw[url_idx]).strip()
            if not url:
                continue
            price_value = None
            if 0 <= price_idx < len(raw):
                price_value = parse_decimal(raw[price_idx])
            status_text = ""
            if 0 <= status_idx < len(raw) and raw[status_idx] is not None:
                status_text = str(raw[status_idx]).strip()
            reason_text = ""
            if 0 <= reason_idx < len(raw) and raw[reason_idx] is not None:
                reason_text = str(raw[reason_idx]).strip()
            mall_text = ""
            if 0 <= mall_idx < len(raw) and raw[mall_idx] is not None:
                mall_text = str(raw[mall_idx]).strip()

            if status_text == "成功":
                results[url] = {
                    "page_price_value": price_value,
                    "page_price_status": "OK" if price_value is not None and price_value > 0 else "PRICE_MISSING",
                    "page_price_source": mall_text or "",
                }
            elif status_text == "跳过":
                results[url] = {
                    "page_price_value": None,
                    "page_price_status": "SKIPPED",
                    "page_price_source": reason_text or mall_text or "",
                }
            else:
                results[url] = {
                    "page_price_value": None,
                    "page_price_status": "FAILED",
                    "page_price_source": reason_text or status_text or "",
                }
        return results
    finally:
        workbook.close()


def apply_crawl_results_to_rows(
    rows: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
    audit_response: Any,
    crawl_results_by_url: dict[str, dict[str, Any]],
    crawler_process_failed: bool = False,
) -> None:
    """把爬虫结果（或失败原因）写入每行的 __page_price_* 字段。

    _compute_page_price_status 会优先读这些字段，从而让 _write_record_results_sheet 直接使用最终结果。
    """
    record_results = list(audit_response.record_results or []) if audit_response is not None else []
    while len(record_results) < len(rows):
        record_results.append(None)

    for row, info, audit_result in zip(rows, enriched, record_results):
        pre = _compute_page_price_status(row, audit_result)
        page_status = pre["page_price_status"]
        page_value = pre["page_price_value"]
        page_source = pre["page_price_source"]

        if page_status == "PENDING_CRAWL":
            if crawler_process_failed:
                page_status = "CRAWLER_PROCESS_FAILED"
                page_value = None
                page_source = "爬虫子进程失败"
            else:
                url = _normalize_url(row.get("extLink"))
                hit = crawl_results_by_url.get(url) if url else None
                if hit is None:
                    page_status = "FAILED"
                    page_value = None
                    page_source = "爬虫未返回该链接"
                else:
                    page_status = hit.get("page_price_status", "FAILED")
                    page_value = hit.get("page_price_value")
                    page_source = hit.get("page_price_source", "")

        row["__page_price_value"] = page_value
        row["__page_price_status"] = page_status
        row["__page_price_source"] = page_source


if __name__ == "__main__":
    raise SystemExit(main())
