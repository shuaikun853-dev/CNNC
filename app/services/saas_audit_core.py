"""Pure, reusable business rules for the SaaS price-audit workflow.

The legacy CLI re-exports these helpers.  Keep this module free of HTTP,
subprocess, Excel writing and durable-state concerns so ticks and the legacy
one-shot runner make the same decisions.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.schemas.workflow import SaasSameProductApprovalItem


THIRTY_PERCENT_RATIO = Decimal("1.30")
JD_HOST_KEYWORDS = ("item.jd.com", "i-item.jd.com", "jd.com")
SAAS_LIST_HEADERS = [
    "documentId", "fourCategoryId", "fourCategoryName", "vCommodityId", "queryStr",
    "sassVCommodityName", "skuId", "skuName", "vendorName", "shopName", "price",
    "extLink", "commodityName", "list_reference_price", "referenceLink", "categoryId",
    "categoryName", "materialCode", "materialDesc", "quasiStandardCommodityCode",
]
SAAS_PENDING_HEADERS = [
    "vCommodityId", "documentId", "queryStr", "sassVCommodityName", "skuId", "skuName",
    "vendorName", "shopName", "price", "pageActualPrice", "extLink", "commentNum",
    "createTime", "updateTime", "modelAuditResult", "isMatch",
]


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
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip()) if str(value).strip() else None
    except (InvalidOperation, ValueError):
        return None


def is_valid_reference_price(value: Decimal | None) -> bool:
    return value is not None and value > 0


def _saas_list_payload_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise ValueError("saas-list-json 必须是数组或包含 rows 的对象")
    for key in ("rows", "records", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict) and isinstance(value.get("rows"), list):
            return [item for item in value["rows"] if isinstance(item, dict)]
    raise ValueError("saas-list-json 中未找到 rows 字段")


def _normalize_saas_list_row(raw: dict[str, Any]) -> dict[str, Any]:
    document_id = normalize_cell_str(raw.get("documentId"))
    commodity_name = normalize_cell_str(raw.get("commodityName"))
    sass_v_name = normalize_cell_str(raw.get("sassVCommodityName"))
    reference_link = normalize_cell_str(raw.get("referenceLink"))
    list_reference_price = parse_decimal(raw.get("referencePrice"))
    return {
        "documentId": document_id,
        "fourCategoryId": normalize_cell_str(raw.get("fourCategoryId")),
        "fourCategoryName": normalize_cell_str(raw.get("fourCategoryName")),
        "vCommodityId": document_id,
        "queryStr": sass_v_name or commodity_name,
        "sassVCommodityName": sass_v_name,
        "skuId": normalize_cell_str(raw.get("referenceId")),
        "skuName": commodity_name,
        "vendorName": "", "shopName": "", "price": list_reference_price,
        "extLink": reference_link, "commodityName": commodity_name,
        "list_reference_price": list_reference_price, "referenceLink": reference_link,
        "categoryId": normalize_cell_str(raw.get("categoryId")),
        "categoryName": normalize_cell_str(raw.get("categoryName")),
        "materialCode": normalize_cell_str(raw.get("materialCode")),
        "materialDesc": normalize_cell_str(raw.get("materialDesc")),
        "quasiStandardCommodityCode": normalize_cell_str(raw.get("quasiStandardCommodityCode")),
    }


def _heal_missing_closing_brace(text: str) -> str:
    text = re.sub(r'("success"\s*:\s*(?:true|false))(\s*\n\s*)(\{)', r"\1}\2\3", text)
    return re.sub(r'(\})(\s*\n\s*)(\{\s*\n\s*"rspCode")', r'\1\n    ]\n}\2\3', text)


def _parse_json_segments(text: str) -> list[Any]:
    text = _heal_missing_closing_brace(text)
    decoder = json.JSONDecoder()
    segments: list[Any] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n\ufeff":
            index += 1
        if index >= len(text):
            break
        if text[index] not in "{[":
            candidates = [pos for pos in (text.find("{", index), text.find("[", index)) if pos >= 0]
            if not candidates:
                break
            index = min(candidates)
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON parse failed at {index}: {exc}") from exc
        segments.append(value)
    return segments


def _load_json_file_segments(path: Path) -> list[Any]:
    if path.suffix.lower() != ".json":
        raise ValueError(f"输入必须是 JSON 文件：{path}")
    return _parse_json_segments(path.read_text(encoding="utf-8"))


def _collect_rows_from_segments(segments: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for segment in segments:
        rows.extend(_saas_list_payload_rows(segment))
    return rows


def read_saas_list_json(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    return list(SAAS_LIST_HEADERS), [_normalize_saas_list_row(row) for row in _collect_rows_from_segments(_load_json_file_segments(path))]


def _normalize_saas_pending_row(raw: dict[str, Any]) -> dict[str, Any]:
    query = normalize_cell_str(raw.get("queryStr")) or normalize_cell_str(raw.get("sassVCommodityName"))
    sku_name = normalize_cell_str(raw.get("skuName")) or normalize_cell_str(raw.get("commodityName"))
    ext_link = normalize_cell_str(raw.get("extLink")) or normalize_cell_str(raw.get("referenceLink"))
    return {
        "vCommodityId": normalize_cell_str(raw.get("vCommodityId")),
        "documentId": normalize_cell_str(raw.get("documentId")), "queryStr": query,
        "sassVCommodityName": normalize_cell_str(raw.get("sassVCommodityName")),
        "skuId": normalize_cell_str(raw.get("skuId")), "skuName": sku_name,
        "vendorName": normalize_cell_str(raw.get("vendorName")),
        "shopName": normalize_cell_str(raw.get("shopName")), "price": parse_decimal(raw.get("price")),
        "pageActualPrice": parse_decimal(raw.get("pageActualPrice")), "extLink": ext_link,
        "commentNum": normalize_cell_str(raw.get("commentNum")),
        "createTime": normalize_cell_str(raw.get("createTime")),
        "updateTime": normalize_cell_str(raw.get("updateTime")),
        "modelAuditResult": normalize_cell_str(raw.get("modelAuditResult")),
        "isMatch": normalize_cell_str(raw.get("isMatch")), "customerPrice": parse_decimal(raw.get("customerPrice")),
    }


# Public canonical entrypoint for incremental discovery. Keep the original
# private spelling for legacy script compatibility.
def normalize_saas_pending_row(raw: dict[str, Any]) -> dict[str, Any]:
    return _normalize_saas_pending_row(raw)


def read_saas_pending_json(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    return list(SAAS_PENDING_HEADERS), [_normalize_saas_pending_row(row) for row in _collect_rows_from_segments(_load_json_file_segments(path))]


def build_customer_index(path: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for raw in _collect_rows_from_segments(_load_json_file_segments(path)):
        document_id = normalize_cell_str(raw.get("documentId"))
        if document_id:
            index[document_id] = customer_row(raw)
    return index


def customer_row(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "fourCategoryId": normalize_cell_str(raw.get("fourCategoryId")),
        "fourCategoryName": normalize_cell_str(raw.get("fourCategoryName")),
        "list_reference_price": parse_decimal(raw.get("referencePrice")),
        "commodityName": normalize_cell_str(raw.get("commodityName")),
        "referenceLink": normalize_cell_str(raw.get("referenceLink")),
    }


def build_strict_customer_index(path: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for raw in _collect_rows_from_segments(_load_json_file_segments(path)):
        document_id = normalize_cell_str(raw.get("documentId"))
        if not document_id:
            continue
        if document_id in index:
            raise ValueError(f"duplicate customer documentId: {document_id}")
        index[document_id] = customer_row(raw)
    return index


def join_saas_with_customer(saas_rows: list[dict[str, Any]], customer_index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    joined: list[dict[str, Any]] = []
    for row in saas_rows:
        merged = dict(row)
        key = normalize_cell_str(row.get("vCommodityId"))
        customer = customer_index.get(key) if key else None
        if customer is None:
            merged.update({"documentId": merged.get("documentId") or "", "fourCategoryId": "", "fourCategoryName": "", "list_reference_price": None, "customer_lookup_status": "NOT_FOUND"})
        else:
            merged.update({"documentId": key, "fourCategoryId": customer.get("fourCategoryId", ""), "fourCategoryName": customer.get("fourCategoryName", ""), "list_reference_price": customer.get("list_reference_price"), "customer_lookup_status": "MATCHED" if customer.get("fourCategoryId") else "MATCHED_NO_CATEGORY"})
        joined.append(merged)
    return joined


def is_jd_link(url: str) -> bool:
    text = (url or "").strip().lower()
    return bool(text) and any(keyword in text for keyword in JD_HOST_KEYWORDS)


def build_record_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "vCommodityId": normalize_cell_str(row.get("vCommodityId")),
        "queryStr": normalize_cell_str(row.get("queryStr")) or normalize_cell_str(row.get("sassVCommodityName")),
        "skuId": normalize_cell_str(row.get("skuId")), "skuName": normalize_cell_str(row.get("skuName")),
        "modelAuditResult": normalize_cell_str(row.get("modelAuditResult")),
        "isMatch": normalize_cell_str(row.get("isMatch")),
        "vendorName": normalize_cell_str(row.get("vendorName")), "shopName": normalize_cell_str(row.get("shopName")),
        "extLink": normalize_cell_str(row.get("extLink")),
    }
    for source, target in (("price", "price"), ("pageActualPrice", "pageActualPrice"), ("customerPrice", "customerPrice")):
        value = parse_decimal(row.get(source))
        if value is not None:
            payload[target] = value
    return payload


def extract_customer_price_from_detail(detail_payload: Any) -> dict[str, Any]:
    failed = {"customerPrice": None, "customerPriceStatus": "DETAIL_PARSE_FAILED", "customerPriceSource": "", "customerPriceWarning": "详情接口返回不是 JSON 对象，无法解析参考价", "customerPriceEvidence": ""}
    if not isinstance(detail_payload, dict):
        return failed
    rsp_code = normalize_cell_str(detail_payload.get("rspCode"))
    if detail_payload.get("success") is False or (rsp_code and rsp_code != "0000"):
        rsp_desc = normalize_cell_str(detail_payload.get("rspDesc"))
        return {"customerPrice": None, "customerPriceStatus": "DETAIL_REQUEST_FAILED", "customerPriceSource": "", "customerPriceWarning": f"详情接口业务失败：rspCode={rsp_code or 'UNKNOWN'}；rspDesc={rsp_desc}", "customerPriceEvidence": ""}
    data = detail_payload.get("data") if isinstance(detail_payload.get("data"), dict) else None
    rows = detail_payload.get("rows") if isinstance(detail_payload.get("rows"), list) else (data.get("rows", []) if isinstance(data, dict) and isinstance(data.get("rows"), list) else [])
    infos = [item for item in (data.get("referenceInfos", []) if isinstance(data, dict) else []) if isinstance(item, dict)]
    if not infos:
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("referenceInfos"), list):
                infos.extend(item for item in row["referenceInfos"] if isinstance(item, dict))
    prices = [price for price in (parse_decimal(item.get("referencePrice")) for item in infos) if is_valid_reference_price(price)]
    if not prices and isinstance(data, dict):
        price = parse_decimal(data.get("referencePrice"))
        if is_valid_reference_price(price):
            prices = [price]
    if not prices:
        for row in rows:
            price = parse_decimal(row.get("referencePrice")) if isinstance(row, dict) else None
            if is_valid_reference_price(price):
                prices = [price]
                break
    if not prices:
        return {"customerPrice": None, "customerPriceStatus": "NOT_FOUND", "customerPriceSource": "", "customerPriceWarning": "详情接口未返回有效参考价", "customerPriceEvidence": ""}
    price = max(prices)
    single = len(prices) == 1
    evidence = [f"参考价{index}={value}" for index, value in enumerate(prices, start=1)]
    evidence.append(f"客户最高价={price}")
    return {"customerPrice": price, "customerPriceStatus": "FOUND_SINGLE_REFERENCE" if single else "FOUND", "customerPriceSource": "DETAIL_SINGLE_REFERENCE" if single else "DETAIL_REFERENCE_INFOS", "customerPriceWarning": "当前详情结果只提取到1个参考价，请确认是否为完整客户价" if single else "", "customerPriceEvidence": "；".join(evidence)}


def build_records(rows: list[dict[str, Any]], enriched: list[dict[str, Any]]) -> list[SaasSameProductApprovalItem]:
    records: list[SaasSameProductApprovalItem] = []
    for row, info in zip(rows, enriched):
        merged = dict(row)
        if info.get("customerPrice") is not None:
            merged["customerPrice"] = info["customerPrice"]
        records.append(SaasSameProductApprovalItem(**build_record_payload(merged)))
    return records


def compute_over_30_check(customer_price: Decimal | None, page_price: Decimal | None, page_price_status: str) -> tuple[str, str]:
    if page_price_status in {"SKIPPED_JD", "SKIPPED_ZHAOSHANG", "SKIPPED_NO_LINK", "SKIPPED_MODEL_NOT_SAME", "SKIPPED", "PENDING_CRAWL", "FAILED", "CRAWLER_PROCESS_FAILED", "PRICE_MISSING", "NO_RESULT"}:
        return "PAGE_PRICE_MISSING", ""
    if page_price is None or page_price < 0:
        return "PAGE_PRICE_MISSING", ""
    if page_price == 0:
        return "PAGE_PRICE_ZERO", ""
    if not is_valid_reference_price(customer_price):
        return "CUSTOMER_PRICE_MISSING", ""
    return ("HIGHER_THAN_CUSTOMER_PRICE_30", "TRUE") if page_price > customer_price * THIRTY_PERCENT_RATIO else ("WITHIN_THRESHOLD", "FALSE")


def compute_saas_price_vs_customer(customer_price: Decimal | None, saas_price: Decimal | None) -> tuple[str, str]:
    if not is_valid_reference_price(saas_price):
        return "SAAS_PRICE_MISSING", "SaaS price missing"
    if not is_valid_reference_price(customer_price):
        return "CUSTOMER_PRICE_MISSING", "customer price missing"
    if saas_price > customer_price * THIRTY_PERCENT_RATIO:
        return "SAAS_HIGHER_THAN_CUSTOMER_30", f"SaaS={saas_price}; customer={customer_price}"
    return "SAAS_WITHIN_CUSTOMER_THRESHOLD", f"SaaS={saas_price}; customer={customer_price}"


def compute_price_changed(saas_price: Decimal | None, page_price: Decimal | None, page_price_status: str, tolerance_ratio: Decimal = Decimal("0.01")) -> tuple[str, str]:
    if page_price_status in {"SKIPPED_JD", "SKIPPED_ZHAOSHANG", "SKIPPED_NO_LINK", "SKIPPED_MODEL_NOT_SAME", "SKIPPED", "PENDING_CRAWL", "FAILED", "CRAWLER_PROCESS_FAILED", "PRICE_MISSING", "NO_RESULT"} or page_price is None or not is_valid_reference_price(saas_price):
        return "PRICE_UNAVAILABLE", "page or SaaS price unavailable"
    if abs(page_price - saas_price) / saas_price > tolerance_ratio:
        return "CHANGED", f"SaaS={saas_price}; page={page_price}"
    return "UNCHANGED", f"SaaS={saas_price}; page={page_price}"


def compute_final_decision(saas_vs_customer_status: str, price_changed_status: str, over_30pct_value: str) -> str:
    flags = sum((saas_vs_customer_status == "SAAS_HIGHER_THAN_CUSTOMER_30", price_changed_status == "CHANGED", over_30pct_value == "TRUE"))
    if flags >= 2:
        return "REJECT_BOTH"
    if saas_vs_customer_status == "SAAS_HIGHER_THAN_CUSTOMER_30" or over_30pct_value == "TRUE":
        return "REJECT_SAAS_HIGH"
    return "REJECT_PRICE_CHANGED" if price_changed_status == "CHANGED" else "PASS"


def relationship_identity(row: dict[str, Any]) -> tuple[str, str]:
    document = normalize_cell_str(row.get("vCommodityId"))
    sku = normalize_cell_str(row.get("skuId"))
    if not document or not sku:
        raise ValueError("vCommodityId and skuId are required for relationship identity")
    return document, sku


def crawl_request_identity(row: dict[str, Any], mall_key: str, backend_version: str) -> tuple[str, str, str, str, str]:
    source_id, sku = relationship_identity(row)
    mall = normalize_cell_str(mall_key)
    if not mall:
        raise ValueError("mall_key is required for crawl identity")
    return mall, source_id, sku, normalize_cell_str(row.get("extLink")), normalize_cell_str(backend_version)


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return normalize_cell_str(value) if isinstance(value, str) else value


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def source_fingerprint(row: dict[str, Any]) -> str:
    normalized = _normalize_saas_pending_row(row)
    # Crawl eligibility is a consumed business input, not merely runtime
    # decoration. Including the resolved policy prevents an audit item from
    # retaining stale eligibility when the mall mapping changes during a day.
    normalized["crawlPolicy"] = {
        "required": bool(row.get("__crawl_required", False)),
        "mallKey": normalize_cell_str(row.get("__crawl_mall_key")),
        "mallSourceId": normalize_cell_str(row.get("__crawl_mall_source_id")),
        "backendVersion": normalize_cell_str(row.get("__crawl_backend_version")),
    }
    return _fingerprint(normalized)


def customer_fingerprint(row: dict[str, Any]) -> str:
    normalized = customer_row({**row, "referencePrice": row.get("referencePrice", row.get("list_reference_price"))})
    return _fingerprint({"fourCategoryId": normalized["fourCategoryId"], "fourCategoryName": normalized["fourCategoryName"], "referencePrice": normalized["list_reference_price"], "commodityName": normalized["commodityName"], "referenceLink": normalized["referenceLink"]})


def snapshot_fingerprint(source_fingerprints: list[str]) -> str:
    return _fingerprint(list(source_fingerprints))


def model_fingerprint(row: dict[str, Any], model: str, prompt_version: str, schema_version: str) -> str:
    return _fingerprint({"title": [normalize_cell_str(row.get(key)) for key in ("queryStr", "skuName", "modelAuditResult", "isMatch")], "model": normalize_cell_str(model), "prompt_version": normalize_cell_str(prompt_version), "schema_version": normalize_cell_str(schema_version)})


def detail_fingerprint(row: dict[str, Any], business_date: str, schema_version: str = "detail-v1") -> str:
    """Fingerprint exactly the customer/detail inputs consumed by the Detail stage."""
    document, _sku = relationship_identity(row)
    return _fingerprint({
        "documentId": document,
        "fourCategoryId": normalize_cell_str(row.get("fourCategoryId")),
        "customer": customer_fingerprint(row),
        "business_date": normalize_cell_str(business_date),
        "schema_version": normalize_cell_str(schema_version),
    })


def crawl_fingerprint(row: dict[str, Any], mall_key: str, business_date: str, backend_version: str) -> str:
    return _fingerprint({
        "identity": crawl_request_identity(row, mall_key, backend_version),
        "business_date": normalize_cell_str(business_date),
        "backend_version": normalize_cell_str(backend_version),
    })
