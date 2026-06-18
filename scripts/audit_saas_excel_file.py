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


def _parse_json_segments(text: str) -> list[Any]:
    """解析"多段 JSON 首尾相连"的文本。

    SaaS / 客户后台导出的 JSON 在分页保存时常常被首尾拼在同一个文件里，
    例如：
        {"rspCode": "0000", "rows": [...100 行...]}
        {"rspCode": "0000", "rows": [...100 行...]}
        {"rspCode": "0000", "rows": [...100 行...]}

    标准 json.load 只读第一段，会报 "Extra data"。
    本函数把每一段单独解析返回。
    """
    decoder = json.JSONDecoder()
    segments: list[Any] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t\r\n\ufeff":
            index += 1
        if index >= length:
            break
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


def _format_cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, WorkflowAction):
        return value.value
    if isinstance(value, list):
        rendered: list[str] = []
        for item in value:
            if isinstance(item, WorkflowAction):
                rendered.append(item.value)
            else:
                rendered.append(str(item))
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
        width = min(max(len(header) * 2 + 4, 12), 60)
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
        "PENDING_CRAWL",
        "FAILED",
        "CRAWLER_PROCESS_FAILED",
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
    sheet.append(final_headers)
    for column_index, header in enumerate(final_headers, start=1):
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
    opener: Any | None = None,
    sleep_func: Any | None = None,
    progress_func: Any | None = None,
) -> dict[str, Any]:
    mode = _select_input_mode(input_xlsx, input_saas_list_json, input_saas_json, input_customer_json)

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
    write_audit_workbook(output_xlsx, headers, rows, enriched, audit_response)
    summary_counts = _build_summary_counts(audit_response, enriched)
    return {
        "summary_counts": summary_counts,
        "audit_response": audit_response,
        "enriched": enriched,
        "headers": headers,
        "rows": rows,
        "mode": mode,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
        progress_func=_cli_progress,
    )
    print_summary(result["summary_counts"], args.output_xlsx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
