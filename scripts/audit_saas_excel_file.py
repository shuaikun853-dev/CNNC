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
import base64
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
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
from app.services import saas_audit_core as _audit_core
from app.services import zhaoshang_result_adapter as _zhaoshang_results
from app.workflows.work_01_saas_approval_json_audit import audit_saas_approval_records
from scripts.auth_manager import AuthManager, SYSTEM_CONFIG
from scripts.curl_utils import apply_curl_headers, internal_direct_opener, load_curl_command, parse_curl_command
from scripts.zhaoshang_mall_mapping import is_zhaoshang_domain

# Legacy public names intentionally remain import-compatible while their
# implementations live in the canonical pure core.
normalize_cell_str = _audit_core.normalize_cell_str
parse_decimal = _audit_core.parse_decimal
is_valid_reference_price = _audit_core.is_valid_reference_price
_saas_list_payload_rows = _audit_core._saas_list_payload_rows
_normalize_saas_list_row = _audit_core._normalize_saas_list_row
_heal_missing_closing_brace = _audit_core._heal_missing_closing_brace
_parse_json_segments = _audit_core._parse_json_segments
_load_json_file_segments = _audit_core._load_json_file_segments
_collect_rows_from_segments = _audit_core._collect_rows_from_segments
read_saas_list_json = _audit_core.read_saas_list_json
_normalize_saas_pending_row = _audit_core._normalize_saas_pending_row
read_saas_pending_json = _audit_core.read_saas_pending_json
build_customer_index = _audit_core.build_customer_index
join_saas_with_customer = _audit_core.join_saas_with_customer
is_jd_link = _audit_core.is_jd_link
build_record_payload = _audit_core.build_record_payload
extract_customer_price_from_detail = _audit_core.extract_customer_price_from_detail
build_records = _audit_core.build_records
compute_over_30_check = _audit_core.compute_over_30_check
compute_saas_price_vs_customer = _audit_core.compute_saas_price_vs_customer
compute_price_changed = _audit_core.compute_price_changed
compute_final_decision = _audit_core.compute_final_decision


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
DEFAULT_ZHAOSHANG_ADD_CURL_FILE = ROOT / "data" / "latest_zhaoshang_add.curl"
DEFAULT_ZHAOSHANG_EXPORT_CURL_FILE = ROOT / "data" / "latest_zhaoshang_exportTask.curl"


def _get_header_case_insensitive(headers: dict[str, Any], name: str) -> str:
    target = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def load_zhaoshang_auth_env_from_curl(
    path: Path,
    *,
    auth_manager: AuthManager | None = None,
) -> bool:
    """Refresh state-backed Zhaoshang auth; cURL is only a bootstrap template."""
    if auth_manager is None:
        sources = {
            name: tuple(config["curl_sources"])
            for name, config in SYSTEM_CONFIG.items()
        }
        sources["zhaoshang"] = (path,)
        auth_manager = AuthManager(curl_sources=sources)
    os.environ.update(auth_manager.export_environment("zhaoshang", refresh=True))
    return True


def _zhaoshang_curl_token_is_expired(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        parsed = parse_curl_command(path.read_text(encoding="utf-8-sig"))
        blade_auth = _get_header_case_insensitive(parsed.get("headers_dict") or {}, "Blade-Auth")
        token = blade_auth.split()[-1]
        parts = token.split(".")
        if len(parts) != 3:
            return False
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        expires_at = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return isinstance(expires_at, (int, float)) and expires_at <= time.time()
    except Exception:
        return False


def load_zhaoshang_auth_env(
    *, auth_manager: AuthManager | None = None
) -> Path:
    """Refresh before any Zhaoshang write and export only the latest state pair."""
    manager = auth_manager or AuthManager()
    os.environ.update(manager.export_environment("zhaoshang", refresh=True))
    return manager.state_path


def load_zhaoshang_auth_env_from_export_curl(
    path: Path = DEFAULT_ZHAOSHANG_EXPORT_CURL_FILE,
    *,
    auth_manager: AuthManager | None = None,
) -> bool:
    return load_zhaoshang_auth_env_from_curl(path, auth_manager=auth_manager)


def _elapsed_seconds(start: float) -> str:
    return f"{time.perf_counter() - start:.2f}s"


def _log_step(component: str, message: str, *, start: float | None = None) -> None:
    elapsed = f" elapsed={_elapsed_seconds(start)}" if start is not None else ""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{component}] {message}{elapsed}", flush=True)

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
    "saas_price_vs_customer",
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
    "price_changed",
    "final_decision",
]

HIGHLIGHTED_RESULT_COLUMNS = {
    "customerPrice",
    "customerPriceStatus",
    "primary_action",
    "price_vs_customer_check",
    "saas_price_vs_customer",
    "page_price_value",
    "over_30pct",
    "price_changed",
    "final_decision",
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
    "saas_price_vs_customer": "SaaS价对比客户价",
    "price_changed": "价格变动",
    "final_decision": "最终决策",
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










# ---------------------------------------------------------------------------
# 京东 / 链接判定
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Row -> SaasSameProductApprovalItem 适配
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# 客户价提取
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# cURL 解析与详情接口请求
# ---------------------------------------------------------------------------


def parse_curl(curl_text: str) -> dict[str, Any]:
    """从 cURL 文本中解析出 url、headers、cookie、data 模板。"""
    return parse_curl_command(curl_text)


def load_curl_file(path: Path) -> dict[str, Any]:
    return load_curl_command(path)


def build_detail_request_body(template: str, document_id: str, four_category_id: str) -> bytes:
    """根据 cURL 模板构造单行详情接口请求体。"""
    template = (template or "").strip()
    if template:
        try:
            payload = json.loads(template)
            if isinstance(payload, dict):
                if "vCommodityId" in payload and "documentId" not in payload:
                    payload["vCommodityId"] = document_id
                else:
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
    """调用中核详情接口。返回结构 {ok, payload, error, http_status}。

    - http_status 仅在 HTTPError 分支携带真实状态码，其余分支为 None。
    - opener 仅用于测试注入；默认使用 urllib.request.urlopen。
    - 不打印任何敏感请求头/Cookie 内容。
    """
    body = build_detail_request_body(curl_template.get("data", ""), document_id, four_category_id)
    request = urllib.request.Request(
        url=curl_template["url"],
        data=body,
        method=curl_template.get("method", "POST") or "POST",
    )
    has_content_type = any(name.lower() == "content-type" for name, _ in curl_template.get("headers", []))
    apply_curl_headers(request, curl_template.get("headers", []), curl_template.get("cookie") or "")
    if not has_content_type:
        request.headers["Content-Type"] = "application/json"

    try:
        if opener is None:
            response = internal_direct_opener().open(request, timeout=timeout)
        else:
            response = opener(request, timeout=timeout)
        with response:
            raw = response.read()
        text = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return {"ok": False, "payload": None, "http_status": None, "error": f"详情接口响应不是 JSON：{exc}"}
        return {"ok": True, "payload": payload, "http_status": None, "error": ""}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "payload": None, "http_status": exc.code, "error": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "payload": None, "http_status": None, "error": f"URL 错误：{exc.reason}"}
    except TimeoutError:
        return {"ok": False, "payload": None, "http_status": None, "error": "详情接口超时"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "payload": None, "http_status": None, "error": f"详情接口异常：{type(exc).__name__}"}


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


def _run_batch_model_audit(
    records: list[SaasSameProductApprovalItem],
    max_workers: int = 5,
    batch_size: int = 20,
) -> Any:
    """批量模型校验（使用 call_title_match_model_batch 一次请求多条）。

    策略：
    1. 先按 (queryStr, skuName) 去重，减少调用量
    2. 按 batch_size 分批，每批一次 API 请求
    3. 多批之间用 ThreadPoolExecutor 并发
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.services.title_match_model_adapter import (
        TitleMatchModelRequest,
        TitleMatchModelResult,
        call_title_match_model_batch,
    )
    from app.workflows.work_01_saas_approval_json_audit import (
        audit_saas_approval_item,
        enforce_saas_audit_recommendation_only,
        SaasSameProductApprovalAuditResponse,
        WorkflowAction,
    )

    print(f"[model] 批量校验 {len(records)} 条，batch_size={batch_size}")

    # 去重：同一 (queryStr, skuName) 只调一次
    unique_pairs: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rec in records:
        key = (rec.queryStr or "", rec.skuName or "")
        if key in seen:
            continue
        seen.add(key)
        unique_pairs.append((rec.queryStr or "", rec.skuName or "", rec.modelAuditResult or "", rec.isMatch or ""))

    print(f"[model] 去重后 {len(unique_pairs)} 个唯一 (queryStr, skuName) 对")

    # 分批
    batches: list[list[TitleMatchModelRequest]] = []
    for i in range(0, len(unique_pairs), batch_size):
        chunk = unique_pairs[i:i + batch_size]
        batches.append([
            TitleMatchModelRequest(query_str=q, sku_name=s, model_audit_result=m, is_match=im)
            for q, s, m, im in chunk
        ])

    print(f"[model] 分成 {len(batches)} 批，每批最多 {batch_size} 条，{min(max_workers, len(batches))} 并发")

    # 并发调批量接口
    pair_to_result: dict[tuple[str, str], TitleMatchModelResult] = {}

    def _call_batch(batch_idx: int, batch: list[TitleMatchModelRequest]):
        results = call_title_match_model_batch(batch)
        return batch_idx, batch, results

    with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as pool:
        futures = {pool.submit(_call_batch, i, b): i for i, b in enumerate(batches)}
        done_count = 0
        for future in as_completed(futures):
            batch_idx, batch, results = future.result()
            for idx, req in enumerate(batch):
                key = (req.query_str, req.sku_name)
                if idx not in results:
                    raise RuntimeError(f"批量模型未返回该条结果 batch={batch_idx} index={idx}")
                pair_to_result[key] = results[idx]
            done_count += 1
            print(f"[model] 批次 {done_count}/{len(batches)} 完成")

    print(f"[model] 全部完成，{len(pair_to_result)} 个结果")

    # 用模型结果跑 audit
    results_list: list = []
    for rec in records:
        key = (rec.queryStr or "", rec.skuName or "")
        model_result = pair_to_result.get(key)
        result = audit_saas_approval_item(
            rec,
            use_model=False,
            use_link_audit=False,
            title_model_result=model_result,
            crawl_results=[],
            use_crawl_result=False,
        )
        results_list.append(result)

    # 构建 summary
    groups: dict[str, list] = {}
    for result in results_list:
        groups.setdefault(result.vCommodityId, []).append(result)

    summary: dict[str, int | str | float | bool] = {
        "total_records": len(results_list),
        "total_v_commodities": len(groups),
        "approve_suggestion_count": len([r for r in results_list if r.primary_action == WorkflowAction.APPROVE]),
        "manual_check_count": len([r for r in results_list if r.primary_action == WorkflowAction.MANUAL_CHECK]),
        "reject_suggestion_count": len([r for r in results if r.primary_action == WorkflowAction.REJECT]),
        "wait_page_price_check_count": len([r for r in results if r.price_check == "WAIT_PAGE_PRICE_CHECK"]),
        "price_mismatch_count": len([r for r in results if r.price_check == "MISMATCH"]),
        "sku_mismatch_count": len([r for r in results if r.sku_link_check == "MISMATCH"]),
        "higher_than_customer_price_30_count": len([r for r in results if r.price_vs_customer_check == "HIGHER_THAN_CUSTOMER_PRICE_30"]),
        "customer_price_missing_count": len([r for r in results if r.price_vs_customer_check == "CUSTOMER_PRICE_MISSING"]),
        "crawl_result_matched_count": len([r for r in results if r.crawl_matched]),
        "crawl_result_missing_count": len([r for r in results if not r.crawl_matched]),
        "crawl_off_shelf_count": 0,
        "crawl_sold_out_count": 0,
        "crawl_price_missing_count": 0,
        "crawl_failed_count": 0,
        "crawl_price_mismatch_count": 0,
        "model_unavailable_count": len([r for r in results if r.title_match_result == "MODEL_UNAVAILABLE"]),
    }
    for v_commodity_id, group_results in groups.items():
        prefix = f"group.{v_commodity_id}"
        summary[f"{prefix}.candidate_count"] = len(group_results)
        summary[f"{prefix}.approve_suggestion_count"] = len([r for r in group_results if r.primary_action == WorkflowAction.APPROVE])
        summary[f"{prefix}.manual_check_count"] = len([r for r in group_results if r.primary_action == WorkflowAction.MANUAL_CHECK])
        summary[f"{prefix}.reject_suggestion_count"] = len([r for r in group_results if r.primary_action == WorkflowAction.REJECT])
        summary[f"{prefix}.valid_mall_count"] = len({normalize_mall_name(r.shopName or r.vendorName) for r in group_results if normalize_mall_name(r.shopName or r.vendorName)})

    return enforce_saas_audit_recommendation_only(
        SaasSameProductApprovalAuditResponse(
            total_records=len(results),
            summary=summary,
            record_results=results,
            execution_allowed=False,
            executed=False,
        )
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
    "SKIPPED_ZHAOSHANG": "招商局处理",
    "SKIPPED_NO_LINK": "跳过(无链接)",
    "SKIPPED_MODEL_NOT_SAME": "跳过(模型判非同款)",
    "SKIPPED": "跳过",
    "OK": "成功",
    "FAILED": "失败",
    "CRAWLER_PROCESS_FAILED": "爬虫进程失败",
    "PRICE_MISSING": "价格不可见",
    "NO_RESULT": "无采集结果",
    "PRICE_IS_ZERO": "价格为0",
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


def _is_zhaoshang_mall(url: str) -> bool:
    """检查 URL 是否属于招商局任务系统商城（不走 multi_mall_crawler）。"""
    return is_zhaoshang_domain(url)


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
    if _is_zhaoshang_mall(ext_link):
        return {
            "page_price_value": None,
            "page_price_status": "SKIPPED_ZHAOSHANG",
            "page_price_source": "招商局",
        }
    # 注意：模型 NOT_SAME 也要爬取价格（业务要求）
    # 不再跳过爬虫，让 over_30pct + saas_price_vs_customer + price_changed 一起兜底判断
    return {
        "page_price_value": None,
        "page_price_status": "PENDING_CRAWL",
        "page_price_source": "",
    }










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

        # SaaS 价 vs 客户价预检（爬虫前就能算）
        saas_price_for_check = row.get("price")
        if not isinstance(saas_price_for_check, Decimal):
            saas_price_for_check = parse_decimal(saas_price_for_check)
        saas_vs_cust_status, _saas_vs_cust_evidence = compute_saas_price_vs_customer(
            customer_price=info.get("customerPrice"),
            saas_price=saas_price_for_check,
        )
        merged["saas_price_vs_customer"] = saas_vs_cust_status

        # 价格变动（SaaS json 里的价 vs 爬虫拿到的页面价）
        price_changed_status, _pc_evidence = compute_price_changed(
            saas_price=saas_price_for_check,
            page_price=page_status_info["page_price_value"],
            page_price_status=page_status_info["page_price_status"],
        )
        merged["price_changed"] = price_changed_status

        # 综合最终决策
        merged["final_decision"] = compute_final_decision(
            saas_vs_customer_status=saas_vs_cust_status,
            price_changed_status=price_changed_status,
            over_30pct_value=over_30,
        )

        row_is_over = (
            over_30 == "TRUE"
            or saas_vs_cust_status == "SAAS_HIGHER_THAN_CUSTOMER_30"
            or price_changed_status == "CHANGED"
            or page_status_info["page_price_status"] == "PRICE_IS_ZERO"
        )
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
    parser.add_argument("--zhaoshang-task", action="store_true",
                        help="启用招商局自动建任务（用京东.xlsx + 震坤行.xlsx）")
    parser.add_argument("--zhaoshang-tool-path", type=Path, default=DEFAULT_ZHAOSHANG_TOOL_PATH,
                        help="招商局 crawl_task_tool.py 路径")
    parser.add_argument("--zhaoshang-task-prefix", default="中核价格爬取",
                        help="招商局任务名前缀，默认 '中核价格爬取'")
    parser.add_argument("--zhaoshang-wait-timeout", type=int, default=1200,
                        help="等待招商局任务超时秒数，默认 600 (10分钟)")
    parser.add_argument("--zhaoshang-progress-interval", type=int, default=30,
                        help="招商局进度查询间隔秒数，默认 30")
    parser.add_argument("--zhaoshang-concurrency", type=int, default=6,
                        help="招商局商城任务并发数，默认 6")
    parser.add_argument(
        "--auto-stbchina-fallback",
        action="store_true",
        help="仅在用户明确授权后，于招商局阶段成功结束后运行史泰博专用脚本",
    )
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
    zhaoshang_task_enabled: bool = False,
    zhaoshang_tool_path: Path | None = None,
    zhaoshang_task_prefix: str = "中核价格更新",
    zhaoshang_wait_timeout: int = 1200,
    zhaoshang_progress_interval: int = 30,
    zhaoshang_concurrency: int = 6,
    auto_stbchina_fallback: bool = False,
    crawler_runner: Any = None,
    opener: Any | None = None,
    sleep_func: Any | None = None,
    progress_func: Any | None = None,
) -> dict[str, Any]:
    mode = _select_input_mode(input_xlsx, input_saas_list_json, input_saas_json, input_customer_json)
    if auto_stbchina_fallback and (
        not zhaoshang_task_enabled or mode != "input_saas_json"
    ):
        raise ValueError(
            "--auto-stbchina-fallback 必须在双 JSON 的招商局阶段启用后使用"
        )

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

    zhaoshang_summary: dict[str, Any] = {"enabled": False}
    zhaoshang_executor: ThreadPoolExecutor | None = None
    zhaoshang_future = None
    if zhaoshang_task_enabled and mode == "input_saas_json":
        auth_curl_path = load_zhaoshang_auth_env()
        if auth_curl_path:
            _log_step("zhaoshang", f"loaded auth from {auth_curl_path}")
        zhaoshang_start = time.perf_counter()
        _log_step(
            "zhaoshang",
            f"submit async tasks concurrency={max(1, zhaoshang_concurrency)}",
        )
        zhaoshang_executor = ThreadPoolExecutor(max_workers=1)
        zhaoshang_future = zhaoshang_executor.submit(
            run_zhaoshang_tasks,
            excluded_export_summary=excluded_export_summary,
            task_name_prefix=zhaoshang_task_prefix,
            tool_path=zhaoshang_tool_path,
            wait_timeout_seconds=zhaoshang_wait_timeout,
            progress_interval_seconds=zhaoshang_progress_interval,
            skip_if_exists=True,
            max_workers=zhaoshang_concurrency,
            batch_state_path=output_xlsx.parent / "BATCH_STATE.md",
        )

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
    detail_start = time.perf_counter()
    _log_step("detail", f"start rows={len(rows)}")
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
    _log_step("detail", f"completed rows={len(enriched)}", start=detail_start)
    records = build_records(rows, enriched)
    model_start = time.perf_counter()
    _log_step("model", f"start rows={len(records)} use_model={use_model}")
    audit_response = run_audit(records, use_model=use_model)
    _log_step("model", "completed", start=model_start)

    crawler_summary: dict[str, Any] = {"enabled": enable_crawler}
    if enable_crawler:
        crawler_stage_start = time.perf_counter()
        urls_to_crawl = collect_urls_for_crawl(rows, enriched, audit_response)
        crawler_summary["urls_total"] = len(urls_to_crawl)
        if not urls_to_crawl:
            _log_step("crawler", "no pending URLs, skipped")
            apply_crawl_results_to_rows(rows, enriched, audit_response, crawl_results_by_url={}, crawler_process_failed=False)
        else:
            script_path = crawler_script if crawler_script is not None else DEFAULT_CRAWLER_SCRIPT
            runtime_dir = output_xlsx.parent if output_xlsx.parent != Path("") else Path("./runtime")
            runtime_dir.mkdir(parents=True, exist_ok=True)
            input_xlsx_path = runtime_dir / CRAWL_INPUT_FILENAME
            dry_run_xlsx_path = runtime_dir / CRAWL_DRY_RUN_FILENAME
            output_xlsx_path = runtime_dir / CRAWL_OUTPUT_FILENAME

            _log_step("crawler", f"pending_urls={len(urls_to_crawl)} input={input_xlsx_path}")
            write_crawler_input_xlsx(urls_to_crawl, input_xlsx_path)

            crawler_process_failed = False

            if not skip_dry_run:
                dry_start = time.perf_counter()
                _log_step("crawler", "dry-run start")
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
                _log_step("crawler", f"dry-run completed ok={dry_result['ok']}", start=dry_start)
                crawler_summary["dry_run"] = dry_result
                if not dry_result["ok"]:
                    print(f"[crawler] dry-run 失败：{dry_result['error']}")
                    crawler_process_failed = True

            if not crawler_process_failed:
                run_start = time.perf_counter()
                _log_step("crawler", "run start")
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
                _log_step("crawler", f"run completed ok={run_result['ok']}", start=run_start)
                crawler_summary["run"] = run_result
                if not run_result["ok"]:
                    print(f"[crawler] 正式爬取失败：{run_result['error']}")
                    crawler_process_failed = True

            crawl_results_by_url: dict[str, dict[str, Any]] = {}
            if not crawler_process_failed:
                try:
                    crawl_results_by_url = read_crawler_output(output_xlsx_path)
                    _log_step("crawler", f"loaded results={len(crawl_results_by_url)}")
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
        _log_step("crawler", "stage completed", start=crawler_stage_start)

    stbchina_summary: dict[str, Any] = {"enabled": auto_stbchina_fallback}

    # 招商局任务在模板导出后已异步启动；这里等待结果并回填页面价。
    if zhaoshang_future is not None:
        _log_step("zhaoshang", "waiting async tasks")
        try:
            zhaoshang_results = zhaoshang_future.result()
        finally:
            if zhaoshang_executor is not None:
                zhaoshang_executor.shutdown(wait=True)
        zhaoshang_meta = zhaoshang_results.get("_meta", {}) if isinstance(zhaoshang_results, dict) else {}
        task_elapsed = zhaoshang_meta.get("elapsed_seconds")
        task_elapsed_text = f"{task_elapsed:.2f}s" if isinstance(task_elapsed, (int, float)) else "unknown"
        _log_step(
            "zhaoshang",
            f"async tasks joined task_elapsed={task_elapsed_text} overlapped_since_submit={_elapsed_seconds(zhaoshang_start)}",
        )
        zhaoshang_summary = {
            "enabled": True,
            "results": zhaoshang_results,
        }
        failed_malls: list[str] = []
        if not isinstance(zhaoshang_results, dict):
            failed_malls.append("结果格式异常")
        else:
            if zhaoshang_results.get("_error"):
                failed_malls.append(f"初始化失败: {zhaoshang_results['_error']}")
            for mall_name, info in zhaoshang_results.items():
                if str(mall_name).startswith("_"):
                    continue
                if not isinstance(info, dict) or not info.get("ok"):
                    error = info.get("error", "未知错误") if isinstance(info, dict) else "结果格式异常"
                    failed_malls.append(f"{mall_name}: {error}")
                elif not info.get("skipped"):
                    try:
                        _validate_zhaoshang_priority_evidence(info)
                    except Exception as exc:  # noqa: BLE001
                        failed_malls.append(f"{mall_name}: {exc}")
                        continue
                    if not info.get("result_path"):
                        failed_malls.append(f"{mall_name}: 未取得导出结果")
        if failed_malls:
            details = "; ".join(_safe_console_text(item, 300) for item in failed_malls)
            raise RuntimeError(f"招商局阶段未完成，停止生成最终 Excel: {details}")
        backfill_start = time.perf_counter()
        backfilled = apply_zhaoshang_results_to_rows(rows, enriched, audit_response, zhaoshang_results)
        zhaoshang_summary["backfilled"] = backfilled
        _log_step("zhaoshang", f"backfilled {backfilled} rows", start=backfill_start)
        if backfilled > 0:
            print(f"[zhaoshang] 回填 {backfilled} 条京东/震坤行价格")

    if auto_stbchina_fallback:
        stbchina_urls = collect_stbchina_urls(rows, missing_only=True)
        stbchina_summary["urls_total"] = len(stbchina_urls)
        if stbchina_urls:
            stbchina_start = time.perf_counter()
            _log_step("stbchina", f"start urls={len(stbchina_urls)}")
            runtime_dir = output_xlsx.parent if output_xlsx.parent != Path("") else Path("./runtime")
            stbchina_results = run_stbchina_crawler(stbchina_urls, runtime_dir)
            stb_backfilled = apply_stbchina_results_to_rows(rows, stbchina_results)
            stbchina_summary["backfilled"] = stb_backfilled
            _log_step("stbchina", f"completed backfilled={stb_backfilled}", start=stbchina_start)
        else:
            stbchina_summary["backfilled"] = 0

    write_start = time.perf_counter()
    write_audit_workbook(output_xlsx, headers, rows, enriched, audit_response)
    _log_step("output", f"workbook written: {output_xlsx}", start=write_start)
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
        "zhaoshang": zhaoshang_summary,
        "stbchina": stbchina_summary,
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
        zhaoshang_task_enabled=args.zhaoshang_task,
        zhaoshang_tool_path=args.zhaoshang_tool_path,
        zhaoshang_task_prefix=args.zhaoshang_task_prefix,
        zhaoshang_wait_timeout=args.zhaoshang_wait_timeout,
        zhaoshang_progress_interval=args.zhaoshang_progress_interval,
        zhaoshang_concurrency=args.zhaoshang_concurrency,
        auto_stbchina_fallback=args.auto_stbchina_fallback,
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
                    "page_price_status": "OK" if price_value is not None else "PRICE_MISSING",
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


def _is_stbchina_link(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return "stbchina.cn" in host


def collect_stbchina_urls(
    rows: list[dict[str, Any]],
    *,
    missing_only: bool = False,
) -> list[str]:
    """收集所有 stbchina.cn 链接（去重）。"""
    seen: set[str] = set()
    urls: list[str] = []
    for row in rows:
        url = normalize_cell_str(row.get("extLink"))
        if not url or not _is_stbchina_link(url) or url in seen:
            continue
        if missing_only and row.get("__page_price_status") in {"OK", "PRICE_IS_ZERO"}:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def run_stbchina_crawler(urls: list[str], runtime_dir: Path) -> dict[str, dict[str, Any]]:
    """调用史泰博专用脚本爬取价格。

    返回 {url: {page_price_value, page_price_status, page_price_source}}
    """
    if not urls:
        return {}

    stbchina_script = Path(r"E:\.A2026\多商城爬取\multi_mall_crawler\scripts\stbchina_fast_crawler.py")
    if not stbchina_script.exists():
        print(f"[stbchina] 专用脚本不存在: {stbchina_script}")
        return {}

    # 写输入文件
    input_txt = runtime_dir / "_stbchina_urls.txt"
    input_txt.parent.mkdir(parents=True, exist_ok=True)
    input_txt.write_text("\n".join(urls) + "\n", encoding="utf-8")

    output_xlsx = runtime_dir / "_stbchina_result.xlsx"

    cmd = [
        sys.executable,
        str(stbchina_script),
        "--input", str(input_txt),
        "--output", str(output_xlsx),
    ]

    print(f"[stbchina] 待爬 {len(urls)} 条，调用专用脚本...")
    try:
        proc = _subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                               encoding="utf-8", errors="replace",
                               env={**_os.environ, "PYTHONIOENCODING": "utf-8"})
        if proc.returncode != 0:
            print(f"[stbchina] 脚本失败 (exit={proc.returncode})")
            if proc.stderr:
                for line in proc.stderr.strip().splitlines()[-3:]:
                    print(f"  [stbchina stderr] {line}")
            return {}
        if proc.stdout:
            for line in proc.stdout.strip().splitlines()[-3:]:
                print(f"  [stbchina stdout] {line}")
    except Exception as exc:
        print(f"[stbchina] 异常: {type(exc).__name__}: {exc}")
        return {}

    # 读结果
    if not output_xlsx.exists():
        print(f"[stbchina] 结果文件不存在: {output_xlsx}")
        return {}

    results: dict[str, dict[str, Any]] = {}
    try:
        wb = load_workbook(filename=str(output_xlsx), data_only=True, read_only=True)
        ws = wb.active
        iterator = ws.iter_rows(values_only=True)
        try:
            header_row = next(iterator)
        except StopIteration:
            wb.close()
            return {}
        headers = [str(v).strip() if v is not None else "" for v in header_row]

        def _find(name):
            return headers.index(name) if name in headers else -1

        url_idx = _find("商品链接")
        price_idx = _find("商品价格")
        status_idx = _find("采集状态")
        reason_idx = _find("失败原因")

        if url_idx < 0:
            wb.close()
            return {}

        for raw in iterator:
            if raw is None:
                continue
            url = str(raw[url_idx] or "").strip() if url_idx < len(raw) else ""
            if not url:
                continue
            price = parse_decimal(raw[price_idx]) if price_idx >= 0 and price_idx < len(raw) else None
            status_text = str(raw[status_idx] or "").strip() if status_idx >= 0 and status_idx < len(raw) else ""
            reason_text = str(raw[reason_idx] or "").strip() if reason_idx >= 0 and reason_idx < len(raw) else ""

            if price is not None and price >= 0:
                results[url] = {
                    "page_price_value": price,
                    "page_price_status": "OK" if price > 0 else "PRICE_IS_ZERO",
                    "page_price_source": "史泰博专用",
                }
            else:
                results[url] = {
                    "page_price_value": None,
                    "page_price_status": "FAILED",
                    "page_price_source": reason_text or status_text or "",
                }
        wb.close()
    except Exception as exc:
        print(f"[stbchina] 读取结果失败: {type(exc).__name__}: {exc}")
        return {}

    print(f"[stbchina] 读回 {len(results)} 条结果")
    return results


def apply_stbchina_results_to_rows(
    rows: list[dict[str, Any]],
    stbchina_results: dict[str, dict[str, Any]],
) -> int:
    """把史泰博爬取结果回填到 row 的 __page_price_* 字段。"""
    backfilled = 0
    for row in rows:
        url = normalize_cell_str(row.get("extLink"))
        if not url or not _is_stbchina_link(url):
            continue
        hit = stbchina_results.get(url)
        if hit is None:
            continue
        row["__page_price_value"] = hit["page_price_value"]
        row["__page_price_status"] = hit["page_price_status"]
        row["__page_price_source"] = hit["page_price_source"]
        backfilled += 1
    return backfilled


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




# ---------------------------------------------------------------------------
# 招商局自动建任务集成（E:\.A2026\招商局自动建任务）
# 业务：京东、震坤行不走 multi_mall_crawler，改投递到招商局采集任务系统
# ---------------------------------------------------------------------------


DEFAULT_ZHAOSHANG_TOOL_PATH = Path(r"E:\.A2026\招商局自动建任务\agent\tools\crawl_task_tool.py")

# 9 个商城全部走招商局任务系统（不再走 multi_mall_crawler）
# 中文名 → 招商局 mall_key 映射（动态从 excluded_export_summary 读取 mall_key）


def _load_zhaoshang_run_crawl_task(tool_path: Path):
    tool_path = Path(tool_path)
    if not tool_path.exists():
        raise FileNotFoundError(f"招商局工具不存在: {tool_path}")

    project_root = tool_path.parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    spec = importlib.util.spec_from_file_location("zhaoshang_crawl_task_tool", str(tool_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载招商局工具: {tool_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run_crawl_task = getattr(module, "run_crawl_task", None)
    if run_crawl_task is None:
        raise AttributeError(f"招商局工具缺少 run_crawl_task: {tool_path}")
    return run_crawl_task


def _safe_console_text(value: Any, limit: int = 300) -> str:
    text = str(value)
    if len(text) > limit:
        text = text[:limit]
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace")


def _zhaoshang_failure_detail(exc: Exception) -> str:
    text = str(exc)
    text = re.sub(r"(?im)^(cookie|blade-auth|authorization|auth-token|access-token)\s*:\s*.*$", r"\1: [REDACTED]", text)
    text = re.sub(r"(?i)(x-access-token|refreshToken|auth-token|access-token)=([^;\s]+)", r"\1=[REDACTED]", text)
    return text


_ZHAOSHANG_PRIORITY_SUCCESS_STATES = {
    "updated",
    "already_highest_priority",
    "task_completed_unconfirmed",
}


def _validate_zhaoshang_priority_evidence(result: Any) -> str:
    if not isinstance(result, dict):
        raise RuntimeError("招商局工具未返回结构化结果")
    priority_state = str(result.get("priority_state") or "").strip()
    if priority_state not in _ZHAOSHANG_PRIORITY_SUCCESS_STATES:
        raise RuntimeError(
            f"招商局任务缺少可接受的提优状态证据: {priority_state or '<missing>'}"
        )
    if priority_state == "updated" and int(result.get("affected_rows") or 0) <= 0:
        raise RuntimeError("招商局任务标记 updated 但缺少 affected_rows>0 证据")
    if (
        priority_state == "already_highest_priority"
        and int(result.get("already_highest_count") or 0) <= 0
    ):
        raise RuntimeError("招商局任务标记 already_highest 但缺少已验证行数")
    return priority_state


def backfill_zhaoshang_results_into_workbook(
    source_path: Path,
    output_path: Path,
    zhaoshang_summary: dict[str, Any],
) -> dict[str, int]:
    """Merge late Zhaoshang exports and recompute all dependent price decisions."""
    url_to_result: dict[str, dict[str, Any]] = {}
    code_candidates: dict[str, list[dict[str, Any]]] = {}
    for mall_cn, info in (zhaoshang_summary or {}).items():
        if str(mall_cn).startswith("_") or not isinstance(info, dict) or not info.get("ok"):
            continue
        result_path = info.get("result_path")
        if not result_path:
            continue
        mapped = read_zhaoshang_result_xlsx(
            Path(result_path),
            template_path=info.get("template_path"),
        )
        url_to_result.update(mapped)
        for hit in mapped.values():
            product_code = normalize_cell_str(hit.get("product_code"))
            if product_code:
                code_candidates.setdefault(product_code, []).append(hit)

    unique_code_to_result = {
        product_code: hits[0]
        for product_code, hits in code_candidates.items()
        if len(hits) == 1
    }

    workbook = load_workbook(source_path)
    try:
        sheet = workbook["record_results"]
        header_index = {
            str(cell.value or "").strip(): index
            for index, cell in enumerate(sheet[1], start=1)
        }

        def column(field: str) -> int:
            display = _display_header(field)
            if display not in header_index:
                raise ValueError(f"Excel missing column: {field}")
            return header_index[display]

        url_col = column("extLink")
        sku_col = header_index.get(_display_header("skuId"))
        saas_price_col = column("price")
        customer_price_col = column("customerPrice")
        saas_vs_customer_col = column("saas_price_vs_customer")
        page_price_col = column("page_price_value")
        page_status_col = column("page_price_status")
        page_source_col = column("page_price_source")
        page_customer_col = column("page_vs_customer_check")
        over_30_col = column("over_30pct")
        price_changed_col = column("price_changed")
        final_decision_col = column("final_decision")
        highlighted_cols = {
            header_index[_display_header(field)]
            for field in HIGHLIGHTED_RESULT_COLUMNS
            if _display_header(field) in header_index
        }

        backfilled = 0
        for row_index in range(2, sheet.max_row + 1):
            source_url = _normalize_url(sheet.cell(row_index, url_col).value)
            hit = url_to_result.get(source_url)
            if hit is None and sku_col is not None:
                product_code = normalize_cell_str(sheet.cell(row_index, sku_col).value)
                hit = unique_code_to_result.get(product_code)
            if hit is not None:
                page_price = hit.get("page_price_value")
                page_status = hit.get("page_price_status") or "FAILED"
                if page_status == "PRICE_ZERO":
                    page_status = "PRICE_IS_ZERO"
                sheet.cell(row_index, page_price_col).value = _format_cell_value(page_price)
                sheet.cell(row_index, page_status_col).value = page_status
                sheet.cell(row_index, page_source_col).value = hit.get("page_price_source", "")
                backfilled += 1
            else:
                page_price = parse_decimal(sheet.cell(row_index, page_price_col).value)
                page_status = str(sheet.cell(row_index, page_status_col).value or "")

            page_vs_customer, over_30 = compute_over_30_check(
                parse_decimal(sheet.cell(row_index, customer_price_col).value),
                page_price,
                page_status,
            )
            price_changed, _ = compute_price_changed(
                parse_decimal(sheet.cell(row_index, saas_price_col).value),
                page_price,
                page_status,
            )
            final_decision = compute_final_decision(
                str(sheet.cell(row_index, saas_vs_customer_col).value or ""),
                price_changed,
                over_30,
            )
            sheet.cell(row_index, page_customer_col).value = page_vs_customer
            sheet.cell(row_index, over_30_col).value = over_30
            sheet.cell(row_index, price_changed_col).value = price_changed
            sheet.cell(row_index, final_decision_col).value = final_decision

            row_is_abnormal = (
                over_30 == "TRUE"
                or str(sheet.cell(row_index, saas_vs_customer_col).value or "") == "SAAS_HIGHER_THAN_CUSTOMER_30"
                or price_changed == "CHANGED"
                or page_status == "PRICE_IS_ZERO"
            )
            for cell in sheet[row_index]:
                cell.fill = OVER_30_FILL if row_is_abnormal else PatternFill(fill_type=None)
            if not row_is_abnormal:
                for col_index in highlighted_cols:
                    sheet.cell(row_index, col_index).fill = HIGHLIGHT_FILL

        decisions = [str(sheet.cell(row, final_decision_col).value or "") for row in range(2, sheet.max_row + 1)]
        if "summary" in workbook:
            summary = workbook["summary"]
            summary.cell(2, 2).value = decisions.count("PASS")
            summary.cell(2, 3).value = sum(value in {"MANUAL_REVIEW", "PRICE_UNAVAILABLE"} for value in decisions)
            summary.cell(2, 4).value = sum(value.startswith("REJECT") for value in decisions)
            summary.cell(2, 5).value = sum(
                str(sheet.cell(row, over_30_col).value or "") == "TRUE"
                for row in range(2, sheet.max_row + 1)
            )
        workbook.save(output_path)
        return {
            "backfilled": backfilled,
            "mapped_urls": len(url_to_result),
            "mapped_unique_codes": len(unique_code_to_result),
        }
    finally:
        workbook.close()


def read_zhaoshang_result_xlsx(path, template_path: Path | str | None = None):
    """Legacy read-only facade for the canonical result adapter."""
    return _zhaoshang_results.read_zhaoshang_result_xlsx(path, template_path=template_path)


def apply_zhaoshang_results_to_rows(rows, enriched, audit_response, zhaoshang_summary):
    """Backfill Zhaoshang prices onto source rows by URL."""
    url_to_result = {}
    code_candidates = {}
    if isinstance(zhaoshang_summary, dict):
        for mall_cn, info in zhaoshang_summary.items():
            if str(mall_cn).startswith("_"):
                continue
            if not isinstance(info, dict) or not info.get("ok") or info.get("skipped"):
                continue
            result_path = info.get("result_path")
            if not result_path:
                continue
            mapped = read_zhaoshang_result_xlsx(
                Path(result_path),
                template_path=info.get("template_path"),
            )
            url_to_result.update(mapped)
            for hit in mapped.values():
                product_code = normalize_cell_str(hit.get("product_code"))
                if product_code:
                    code_candidates.setdefault(product_code, []).append(hit)
            print(f"[zhaoshang] {_safe_console_text(mall_cn, 80)}: backfill mapped {len(mapped)} rows")

    if not url_to_result and not code_candidates:
        return 0

    unique_code_to_result = {
        product_code: hits[0]
        for product_code, hits in code_candidates.items()
        if len(hits) == 1
    }

    backfilled = 0
    for row in rows:
        url = _normalize_url(row.get("extLink"))
        hit = url_to_result.get(url)
        if hit is None:
            hit = unique_code_to_result.get(normalize_cell_str(row.get("skuId")))
        if hit is None:
            continue
        row["__page_price_value"] = hit["page_price_value"]
        row["__page_price_status"] = hit["page_price_status"]
        row["__page_price_source"] = hit["page_price_source"]
        backfilled += 1
    return backfilled


def run_zhaoshang_tasks(
    excluded_export_summary,
    task_name_prefix="中核价格爬取",
    tool_path=None,
    wait_timeout_seconds=600,
    progress_interval_seconds=30,
    skip_if_exists=True,
    max_workers=6,
    batch_state_path=None,
):
    """Run one Zhaoshang task per mall, concurrently."""
    group_start = time.perf_counter()
    tool_path = tool_path or DEFAULT_ZHAOSHANG_TOOL_PATH
    try:
        run_crawl_task = _load_zhaoshang_run_crawl_task(tool_path)
    except Exception as exc:
        _log_step("zhaoshang", f"load tool failed {type(exc).__name__}: {_safe_console_text(exc)}")
        return {"_error": f"{type(exc).__name__}: {exc}"}

    results: dict[str, dict[str, Any]] = {}
    if not isinstance(excluded_export_summary, dict):
        return results

    jobs: list[tuple[str, dict[str, Any], str, Path]] = []
    for mall_cn, info in excluded_export_summary.items():
        if str(mall_cn).startswith("_") or mall_cn == "enabled":
            continue
        if not isinstance(info, dict) or info.get("deduped", 0) == 0:
            results[mall_cn] = {"ok": True, "skipped": True, "reason": "无可用数据行"}
            continue

        mall_key = info.get("mall_key") or mall_cn
        excel_path = info.get("path")
        if excel_path is None or not Path(excel_path).exists():
            results[mall_cn] = {"ok": False, "error": f"模板文件不存在: {excel_path}"}
            continue

        jobs.append((mall_cn, info, mall_key, Path(excel_path)))

    if not jobs:
        return results

    worker_count = max(1, min(int(max_workers or 1), len(jobs)))
    _log_step("zhaoshang", f"run mall tasks total={len(jobs)} concurrency={worker_count}")

    def run_one(job: tuple[str, dict[str, Any], str, Path]) -> tuple[str, dict[str, Any]]:
        mall_cn, info, mall_key, excel_path = job
        task_marker = datetime.now().strftime("%Y%m%d%H%M%S%f")
        task_name = f"cnnc_current_opt_{mall_cn}_{task_marker}"
        task_start = time.perf_counter()
        try:
            _log_step(
                "zhaoshang",
                f"{_safe_console_text(mall_cn, 80)} submit task_name={_safe_console_text(task_name, 160)} mall_key={mall_key} rows={info.get('deduped', 0)}",
            )
            result = run_crawl_task(
                task_name=task_name,
                mall_key=mall_key,
                excel_path=str(excel_path),
                wait_timeout_seconds=wait_timeout_seconds,
                progress_interval_seconds=progress_interval_seconds,
                skip_if_exists=skip_if_exists,
                batch_state_path=batch_state_path,
            )
            _validate_zhaoshang_priority_evidence(result)
            _log_step(
                "zhaoshang",
                f"{_safe_console_text(mall_cn, 80)} completed result={_safe_console_text(result.get('result_path'), 200)}",
                start=task_start,
            )
            return mall_cn, {
                "ok": True,
                "task_name": task_name,
                "mall_key": mall_key,
                "source_id": info.get("source_id", ""),
                "rows": info.get("deduped", 0),
                "template_path": str(excel_path),
                "result_path": result.get("result_path"),
                "main_task_id": result.get("main_task_id"),
                "task_number": result.get("task_number"),
                "priority_state": result.get("priority_state"),
                "affected_rows": result.get("affected_rows"),
                "already_highest_count": result.get("already_highest_count"),
            }
        except Exception as exc:
            detail = _zhaoshang_failure_detail(exc)
            error = f"{type(exc).__name__}: {_safe_console_text(detail)}"
            _log_step("zhaoshang", f"{_safe_console_text(mall_cn, 80)} failed {error}", start=task_start)
            print(
                f"[zhaoshang][failure-detail] mall={_safe_console_text(mall_cn, 80)}\n"
                f"{_safe_console_text(detail, 1_000_000)}",
                flush=True,
            )
            return mall_cn, {
                "ok": False,
                "task_name": task_name,
                "mall_key": mall_key,
                "source_id": info.get("source_id", ""),
                "error": error,
                "failure_detail": detail,
            }

    if worker_count <= 1:
        for job in jobs:
            mall_cn, result = run_one(job)
            results[mall_cn] = result
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_to_mall = {pool.submit(run_one, job): job[0] for job in jobs}
            for future in as_completed(future_to_mall):
                mall_cn = future_to_mall[future]
                try:
                    result_mall, result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result_mall = mall_cn
                    result = {"ok": False, "error": f"{type(exc).__name__}: {_safe_console_text(exc)}"}
                results[result_mall] = result

    results["_meta"] = {
        "elapsed_seconds": round(time.perf_counter() - group_start, 2),
        "concurrency": worker_count,
        "task_count": len(jobs),
    }
    return results


if __name__ == "__main__":
    raise SystemExit(main())
