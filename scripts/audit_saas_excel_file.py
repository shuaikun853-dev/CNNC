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
    "customerPrice",
    "customerPriceStatus",
    "customerPriceSource",
    "customerPriceWarning",
    "customerPriceEvidence",
    "json_price",
    "page_price",
    "title_match_result",
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
]

HIGHLIGHTED_RESULT_COLUMNS = {
    "customerPrice",
    "customerPriceStatus",
    "primary_action",
    "price_vs_customer_check",
}

HEADER_FONT = Font(bold=True)
HEADER_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
HIGHLIGHT_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")


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
) -> list[dict[str, Any]]:
    """对每一行计算客户价信息，返回追加列字典列表。"""
    enriched: list[dict[str, Any]] = []
    for row in rows:
        existing_customer_price = parse_decimal(row.get("customerPrice"))
        if is_valid_reference_price(existing_customer_price):
            enriched.append({
                "customerPrice": existing_customer_price,
                "customerPriceStatus": "EXISTS",
                "customerPriceSource": "EXCEL_INPUT",
                "customerPriceWarning": "",
                "customerPriceEvidence": f"Excel 已提供客户价={existing_customer_price}",
            })
            continue

        if curl_template is None:
            enriched.append({
                "customerPrice": None,
                "customerPriceStatus": "NOT_PROVIDED",
                "customerPriceSource": "",
                "customerPriceWarning": "未提供 --curl-file 且 Excel 未填写 customerPrice",
                "customerPriceEvidence": "",
            })
            continue

        document_id = normalize_cell_str(row.get(document_id_field))
        four_category_id = normalize_cell_str(row.get(category_id_field))
        if not document_id or not four_category_id:
            enriched.append({
                "customerPrice": None,
                "customerPriceStatus": "DETAIL_REQUEST_SKIPPED",
                "customerPriceSource": "",
                "customerPriceWarning": "缺少 documentId 或 fourCategoryId，跳过详情请求",
                "customerPriceEvidence": "",
            })
            continue

        response = call_detail_api(curl_template, document_id, four_category_id, opener=opener)
        if not response.get("ok"):
            enriched.append({
                "customerPrice": None,
                "customerPriceStatus": "DETAIL_REQUEST_FAILED",
                "customerPriceSource": "",
                "customerPriceWarning": "详情接口请求失败，请检查 cURL 登录态是否过期",
                "customerPriceEvidence": "",
            })
            continue
        result = extract_customer_price_from_detail(response.get("payload"))
        enriched.append(result)
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


def run_audit(records: list[SaasSameProductApprovalItem]) -> Any:
    return audit_saas_approval_records(
        records=records,
        use_model=False,
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
    return {
        "总记录数": int(summary.get("total_records", len(record_results))),
        "建议通过数量": int(summary.get("approve_suggestion_count", 0)),
        "人工核查数量": int(summary.get("manual_check_count", 0)),
        "建议驳回数量": int(summary.get("reject_suggestion_count", 0)),
        "客户价高于30%数量": int(summary.get("higher_than_customer_price_30_count", 0)),
        "客户价缺失数量": customer_price_missing,
        "客户价请求失败数量": customer_price_failed,
        "价格不一致数量": int(summary.get("price_mismatch_count", 0)),
        "输出时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _set_column_widths(sheet: Any, headers: list[str]) -> None:
    for index, header in enumerate(headers, start=1):
        letter = get_column_letter(index)
        width = min(max(len(header) * 2 + 4, 12), 60)
        if header in {"reason", "evidence", "customerPriceEvidence", "extLink", "queryStr", "skuName", "customerPriceWarning"}:
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
        # 长度不一致时回退到逐行最小并集，避免抛异常
        while len(record_results) < len(rows):
            record_results.append(None)

    for row_index, (row, info, audit_result) in enumerate(zip(rows, enriched, record_results), start=2):
        merged: dict[str, Any] = {}
        for header in base_headers:
            merged[header] = row.get(header, "")
        merged["customerPrice"] = info.get("customerPrice")
        merged["customerPriceStatus"] = info.get("customerPriceStatus", "")
        merged["customerPriceSource"] = info.get("customerPriceSource", "")
        merged["customerPriceWarning"] = info.get("customerPriceWarning", "")
        merged["customerPriceEvidence"] = info.get("customerPriceEvidence", "")
        if audit_result is not None:
            merged["json_price"] = audit_result.json_price
            merged["page_price"] = audit_result.page_price
            merged["title_match_result"] = audit_result.title_match_result
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
                "page_price",
                "title_match_result",
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
        for column_index, header in enumerate(final_headers, start=1):
            cell = sheet.cell(row=row_index, column=column_index)
            cell.value = _format_cell_value(merged.get(header, ""))
            if header in HIGHLIGHTED_RESULT_COLUMNS:
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
    parser.add_argument("--input-xlsx", required=True, type=Path)
    parser.add_argument("--output-xlsx", required=True, type=Path)
    parser.add_argument("--curl-file", type=Path, default=None)
    parser.add_argument("--document-id-field", default=DEFAULT_DOCUMENT_ID_FIELD)
    parser.add_argument("--category-id-field", default=DEFAULT_CATEGORY_ID_FIELD)
    return parser


def print_summary(summary_counts: dict[str, Any], output_path: Path) -> None:
    for key, value in summary_counts.items():
        print(f"{key}: {value}")
    print(f"输出文件路径: {output_path}")


def run_main(
    input_xlsx: Path,
    output_xlsx: Path,
    curl_file: Path | None,
    document_id_field: str,
    category_id_field: str,
    opener: Any | None = None,
) -> dict[str, Any]:
    headers, rows = read_excel_rows(input_xlsx)
    if not rows:
        raise ValueError(f"输入 Excel 没有数据行：{input_xlsx}")
    curl_template = load_curl_file(curl_file) if curl_file is not None else None
    enriched = enrich_rows_with_customer_price(
        rows,
        curl_template,
        document_id_field,
        category_id_field,
        opener=opener,
    )
    records = build_records(rows, enriched)
    audit_response = run_audit(records)
    write_audit_workbook(output_xlsx, headers, rows, enriched, audit_response)
    summary_counts = _build_summary_counts(audit_response, enriched)
    return {
        "summary_counts": summary_counts,
        "audit_response": audit_response,
        "enriched": enriched,
        "headers": headers,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_main(
        input_xlsx=args.input_xlsx,
        output_xlsx=args.output_xlsx,
        curl_file=args.curl_file,
        document_id_field=args.document_id_field,
        category_id_field=args.category_id_field,
    )
    print_summary(result["summary_counts"], args.output_xlsx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
