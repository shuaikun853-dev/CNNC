import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal

from app.schemas.workflow import (
    InternalCrawlResult,
    SaasSameProductApprovalAuditResponse,
    SaasSameProductApprovalAuditResult,
    SaasSameProductApprovalItem,
    WorkflowAction,
)
from app.services.link_audit_worker import (
    LinkAuditRequest,
    LinkAuditResult,
    check_price_consistency,
    check_sku_link_consistency,
    audit_link,
)
from app.services.title_match_model_adapter import (
    TitleMatchModelRequest,
    TitleMatchModelResult,
    call_title_match_model_batch,
    call_title_match_model,
)


RISK_WORDS = ("替代款", "通用", "适用", "升级款")
REVIEW_TERMS = RISK_WORDS
HIGHER_THAN_CUSTOMER_PRICE_RATIO = Decimal("1.30")
SPEC_RE = re.compile(r"\d+\s*(?:件套|件|套)")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]*")


def normalize_mall_name(mall_name: str | None) -> str:
    name = (mall_name or "").strip()
    if not name:
        return ""
    aliases = {
        "jd": "京东",
        "京东": "京东",
        "京东商城": "京东",
        "tmall": "天猫",
        "天猫": "天猫",
        "天猫商城": "天猫",
        "taobao": "淘宝",
        "淘宝": "淘宝",
        "suning": "苏宁",
        "苏宁": "苏宁",
        "苏宁易购": "苏宁",
    }
    return aliases.get(name.lower(), aliases.get(name, name))


def get_page_actual_price(item: SaasSameProductApprovalItem) -> Decimal | None:
    if item.pageActualPrice is not None:
        return item.pageActualPrice
    return item.page_actual_price


def get_customer_price(record: SaasSameProductApprovalItem) -> Decimal | None:
    if record.customerPrice is not None:
        return record.customerPrice
    if record.customer_price is not None:
        return record.customer_price
    if record.referencePrice is not None:
        return record.referencePrice
    return record.reference_price


def get_candidate_price(record: SaasSameProductApprovalItem, page_actual_price: Decimal | None) -> Decimal | None:
    if page_actual_price is not None:
        return page_actual_price
    return record.price


def _normalize_key(value: str | None) -> str:
    return (value or "").strip()


def _parse_crawl_time(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.replace("Z", ""), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_crawl_result(results: list[InternalCrawlResult]) -> InternalCrawlResult | None:
    if not results:
        return None
    latest_index = 0
    latest_time = _parse_crawl_time(results[0].crawl_time)
    for index, result in enumerate(results[1:], start=1):
        parsed_time = _parse_crawl_time(result.crawl_time)
        if parsed_time is None and latest_time is None:
            latest_index = index
            continue
        if parsed_time is not None and (latest_time is None or parsed_time >= latest_time):
            latest_time = parsed_time
            latest_index = index
    return results[latest_index]


def match_crawl_result(
    record: SaasSameProductApprovalItem,
    crawl_results: list[InternalCrawlResult],
) -> InternalCrawlResult | None:
    ext_link = _normalize_key(record.extLink)
    sku_id = _normalize_key(record.skuId)
    vendor_name = normalize_mall_name(record.vendorName)
    if ext_link:
        matched = [result for result in crawl_results if _normalize_key(result.extLink) == ext_link]
        found = _latest_crawl_result(matched)
        if found is not None:
            return found
    if sku_id:
        matched = [result for result in crawl_results if _normalize_key(result.skuId) == sku_id]
        found = _latest_crawl_result(matched)
        if found is not None:
            return found
    if vendor_name and sku_id:
        matched = [
            result for result in crawl_results
            if normalize_mall_name(result.vendorName) == vendor_name and _normalize_key(result.skuId) == sku_id
        ]
        return _latest_crawl_result(matched)
    return None


def match_crawl_result_with_evidence(
    record: SaasSameProductApprovalItem,
    crawl_results: list[InternalCrawlResult],
) -> tuple[InternalCrawlResult | None, list[str]]:
    result = match_crawl_result(record, crawl_results)
    if result is None:
        return None, ["缺少内部爬取结果，需人工核验页面价格"]
    if _normalize_key(result.extLink) and _normalize_key(result.extLink) == _normalize_key(record.extLink):
        return result, ["命中内部爬取结果：extLink"]
    if _normalize_key(result.skuId) and _normalize_key(result.skuId) == _normalize_key(record.skuId):
        if normalize_mall_name(result.vendorName) == normalize_mall_name(record.vendorName):
            return result, ["命中内部爬取结果：vendorName+skuId"]
        return result, ["命中内部爬取结果：skuId"]
    return result, ["命中内部爬取结果：vendorName+skuId"]


def sanitize_screenshot_path(screenshot_path: str) -> tuple[str, list[str]]:
    path = (screenshot_path or "").strip()
    if not path:
        return "", []
    lower = path.lower()
    if (
        ".." in path
        or path.startswith("/")
        or path.startswith("\\")
        or re.match(r"^[a-zA-Z]:", path)
        or lower.startswith(("file://", "http://", "https://"))
    ):
        return "", ["截图路径疑似不安全，已忽略"]
    return path, [f"内部爬取截图路径={path}"]


def _is_crawl_status(crawl_result: InternalCrawlResult | None, *statuses: str) -> bool:
    if crawl_result is None:
        return False
    values = {crawl_result.crawl_status.upper(), crawl_result.page_status.upper()}
    return any(status in values for status in statuses)


def _crawl_link_result(
    item: SaasSameProductApprovalItem,
    crawl_result: InternalCrawlResult | None,
    crawl_evidence: list[str],
) -> LinkAuditResult:
    sku_check, sku_evidence = check_sku_link_consistency(item.skuId, item.extLink)
    if crawl_result is None:
        return LinkAuditResult(
            reachable=None,
            off_shelf=None,
            sku_link_check=sku_check,
            page_price=None,
            price_check="WAIT_PAGE_PRICE_CHECK",
            evidence=[*sku_evidence, *crawl_evidence],
        )
    screenshot_path, screenshot_evidence = sanitize_screenshot_path(crawl_result.screenshot_path)
    status = crawl_result.crawl_status.upper() or crawl_result.page_status.upper()
    page_status = crawl_result.page_status.upper()
    evidence = [
        *sku_evidence,
        *crawl_evidence,
        f"内部爬取状态={crawl_result.crawl_status or crawl_result.page_status or 'UNKNOWN'}",
        *screenshot_evidence,
    ]
    if crawl_result.error_code:
        evidence.append(f"内部爬取错误码={crawl_result.error_code}")
    if crawl_result.error_message:
        evidence.append(f"内部爬取错误信息={crawl_result.error_message}")
    if crawl_result.page_title:
        evidence.append(f"内部爬取页面标题={crawl_result.page_title}")
    if crawl_result.raw_price_candidates:
        evidence.append(f"内部爬取价格候选={','.join(crawl_result.raw_price_candidates)}")
    if _is_crawl_status(crawl_result, "OFF_SHELF"):
        price_check, price_evidence = check_price_consistency(item.price, crawl_result.page_price)
        return LinkAuditResult(
            reachable=True,
            off_shelf=True,
            sku_link_check=sku_check,
            page_title=crawl_result.page_title,
            page_price=crawl_result.page_price,
            price_check=price_check,
            screenshot_path=screenshot_path,
            error_code=crawl_result.error_code,
            page_status=page_status,
            crawl_status=status,
            error_message=crawl_result.error_message,
            price_source=crawl_result.price_source,
            raw_price_candidates=crawl_result.raw_price_candidates,
            evidence=[*evidence, *price_evidence, "内部爬取结果显示商品疑似下架"],
        )
    if _is_crawl_status(crawl_result, "SOLD_OUT", "PRICE_MISSING", "CAPTCHA", "ACCESS_DENIED", "TIMEOUT", "FAILED", "UNKNOWN"):
        price_check, price_evidence = check_price_consistency(item.price, crawl_result.page_price)
        if _is_crawl_status(crawl_result, "PRICE_MISSING"):
            price_check = "WAIT_PAGE_PRICE_CHECK"
        return LinkAuditResult(
            reachable=None,
            off_shelf=None,
            sku_link_check=sku_check,
            page_title=crawl_result.page_title,
            page_price=crawl_result.page_price,
            price_check=price_check,
            screenshot_path=screenshot_path,
            error_code=crawl_result.error_code,
            page_status=page_status,
            crawl_status=status,
            error_message=crawl_result.error_message,
            price_source=crawl_result.price_source,
            raw_price_candidates=crawl_result.raw_price_candidates,
            evidence=[*evidence, *price_evidence, "内部爬取结果需人工核验"],
        )
    price_check, price_evidence = check_price_consistency(item.price, crawl_result.page_price)
    return LinkAuditResult(
        reachable=True,
        off_shelf=False,
        sku_link_check=sku_check,
        page_title=crawl_result.page_title,
        page_price=crawl_result.page_price,
        price_check=price_check,
        screenshot_path=screenshot_path,
        error_code=crawl_result.error_code,
        page_status=page_status or "NORMAL",
        crawl_status=status or "SUCCESS",
        error_message=crawl_result.error_message,
        price_source=crawl_result.price_source,
        raw_price_candidates=crawl_result.raw_price_candidates,
        evidence=[*evidence, *price_evidence],
    )


def check_candidate_price_against_customer_price(
    candidate_price: Decimal | None,
    customer_price: Decimal | None,
) -> tuple[str, list[str]]:
    if customer_price is None:
        return "CUSTOMER_PRICE_MISSING", ["客户价格为空，无法执行 30% 高价过滤"]
    if customer_price <= 0:
        return "CUSTOMER_PRICE_INVALID", ["客户价格无效，无法执行30%高价过滤"]
    if candidate_price is None:
        return "CANDIDATE_PRICE_MISSING", ["候选价格为空，无法执行 30% 高价过滤"]
    if candidate_price <= 0:
        return "CANDIDATE_PRICE_INVALID", ["候选价格无效，无法执行30%高价过滤"]
    threshold = customer_price * HIGHER_THAN_CUSTOMER_PRICE_RATIO
    if candidate_price > threshold:
        return "HIGHER_THAN_CUSTOMER_PRICE_30", [
            f"客户价格={customer_price}",
            f"候选价格={candidate_price}",
            f"高价阈值=客户价格*1.3={threshold}",
            "候选价格高于客户价格30%以上",
        ]
    return "WITHIN_THRESHOLD", [
        f"客户价格={customer_price}",
        f"候选价格={candidate_price}",
        f"高价阈值=客户价格*1.3={threshold}",
        "候选价格未超过客户价格30%",
    ]


def detect_title_risk_words(query_str: str, sku_name: str) -> list[str]:
    text = f"{query_str or ''} {sku_name or ''}"
    return [word for word in RISK_WORDS if word in text]


def _extract_models(text: str) -> list[str]:
    models: list[str] = []
    for raw in MODEL_RE.findall(text):
        token = raw.strip("-_/")
        if not token:
            continue
        has_digit = any(ch.isdigit() for ch in token)
        has_alpha = any(ch.isalpha() for ch in token)
        if has_digit and (has_alpha or len(token) >= 4):
            models.append(token.upper())
    models.sort(key=lambda value: (any(ch.isalpha() for ch in value), len(value)), reverse=True)
    return models


def _extract_specs(text: str) -> set[str]:
    return {re.sub(r"\s+", "", value) for value in SPEC_RE.findall(text)}


def audit_title_similarity(
    query_str: str,
    sku_name: str,
    model_audit_result: str,
    is_match: str,
) -> tuple[str, list[str]]:
    query = (query_str or "").strip()
    sku = (sku_name or "").strip()
    if not query or not sku:
        return "MANUAL_REVIEW", ["缺少标准商品描述或 SKU 标题"]

    evidence: list[str] = []
    if model_audit_result == "1" and is_match == "1":
        evidence.append("SaaS模型判断为同款")

    query_models = _extract_models(query)
    sku_upper = sku.upper()
    matched_models = [model for model in query_models if model in sku_upper]
    has_review_term = any(term in sku for term in REVIEW_TERMS)

    if matched_models:
        evidence.append(f"核心型号命中: {matched_models[0]}")
    elif query_models:
        evidence.append("核心型号未完全命中，需人工确认")
    else:
        evidence.append("未识别到明确核心型号，需人工确认")

    query_specs = _extract_specs(query)
    sku_specs = _extract_specs(sku)
    if query_specs and query_specs.intersection(sku_specs):
        evidence.append("规格/件数疑似一致")

    if has_review_term:
        evidence.append("标题含替代款/升级款/通用/适用，需人工确认是否为原型号同款")
        return "MANUAL_REVIEW", evidence

    if matched_models:
        return "SAME", evidence
    if query_models:
        return "MANUAL_REVIEW", evidence
    return "MANUAL_REVIEW", evidence


def _title_result_from_model_or_rule(
    item: SaasSameProductApprovalItem,
    use_model: bool,
) -> tuple[str, float, list[str]]:
    rule_result, rule_evidence = audit_title_similarity(
        item.queryStr,
        item.skuName,
        item.modelAuditResult,
        item.isMatch,
    )
    if not use_model:
        return rule_result, 0.0, rule_evidence

    model_result = call_title_match_model(
        TitleMatchModelRequest(
            query_str=item.queryStr,
            sku_name=item.skuName,
            model_audit_result=item.modelAuditResult,
            is_match=item.isMatch,
        )
    )
    evidence = [
        f"title_model.match_result={model_result.match_result}",
        f"title_model.confidence={model_result.confidence}",
    ]
    if model_result.reason:
        evidence.append(f"title_model.reason={model_result.reason}")
    evidence.extend([f"title_model.risk={risk}" for risk in model_result.risk_points])
    evidence.extend(rule_evidence)

    if model_result.match_result == "MODEL_UNAVAILABLE":
        return "MODEL_UNAVAILABLE", model_result.confidence, evidence
    return model_result.match_result, model_result.confidence, evidence


def _title_model_cache_key(item: SaasSameProductApprovalItem) -> tuple[str, str, str, str]:
    return (
        item.queryStr or "",
        item.skuName or "",
        item.modelAuditResult or "",
        item.isMatch or "",
    )


def _title_model_concurrency(default: int = 8) -> int:
    raw = os.environ.get("TITLE_MATCH_MODEL_CONCURRENCY", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def _title_model_batch_size(default: int = 30) -> int:
    raw = os.environ.get("TITLE_MATCH_MODEL_BATCH_SIZE", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def _title_model_batch_concurrency(default: int = 1) -> int:
    raw = os.environ.get("TITLE_MATCH_MODEL_BATCH_CONCURRENCY", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def _call_title_model_for_item(item: SaasSameProductApprovalItem) -> TitleMatchModelResult:
    return call_title_match_model(
        TitleMatchModelRequest(
            query_str=item.queryStr,
            sku_name=item.skuName,
            model_audit_result=item.modelAuditResult,
            is_match=item.isMatch,
        )
    )


def _precompute_title_model_results(
    records: list[SaasSameProductApprovalItem],
) -> dict[tuple[str, str, str, str], TitleMatchModelResult]:
    unique_records: dict[tuple[str, str, str, str], SaasSameProductApprovalItem] = {}
    for record in records:
        unique_records.setdefault(_title_model_cache_key(record), record)
    if not unique_records:
        return {}

    batch_size = _title_model_batch_size()
    if batch_size > 1:
        items = list(unique_records.items())
        results: dict[tuple[str, str, str, str], TitleMatchModelResult] = {}
        total_batches = (len(items) + batch_size - 1) // batch_size
        batch_concurrency = min(_title_model_batch_concurrency(), total_batches)
        print(
            f"[model] 标题模型批量调用: rows={len(records)} unique={len(unique_records)} "
            f"batch_size={batch_size} batches={total_batches} batch_concurrency={batch_concurrency}",
            flush=True,
        )
        batches = []
        for batch_number in range(total_batches):
            start = batch_number * batch_size
            chunk = items[start:start + batch_size]
            requests = [
                TitleMatchModelRequest(
                    query_str=record.queryStr,
                    sku_name=record.skuName,
                    model_audit_result=record.modelAuditResult,
                    is_match=record.isMatch,
                )
                for _key, record in chunk
            ]
            batches.append((batch_number, chunk, requests))

        def run_batch(batch_info):
            batch_number, chunk, requests = batch_info
            batch_results = call_title_match_model_batch(requests)
            return batch_number, chunk, batch_results

        completed = 0
        if batch_concurrency <= 1:
            for batch_info in batches:
                batch_number, chunk, batch_results = run_batch(batch_info)
                for index, (key, _record) in enumerate(chunk):
                    results[key] = batch_results[index]
                completed += 1
                print(f"[model] 批量调用进度: {completed}/{total_batches}", flush=True)
        else:
            with ThreadPoolExecutor(max_workers=batch_concurrency) as executor:
                future_to_batch = {
                    executor.submit(run_batch, batch_info): batch_info[0]
                    for batch_info in batches
                }
                for future in as_completed(future_to_batch):
                    try:
                        _batch_number, chunk, batch_results = future.result()
                    except Exception as exc:  # noqa: BLE001
                        batch_number = future_to_batch[future]
                        chunk = batches[batch_number][1]
                        batch_results = {
                            index: TitleMatchModelResult(
                                match_result="MANUAL_REVIEW",
                                confidence=0.0,
                                model_match="unknown",
                                reason=f"标题模型批量并发调用失败，已降级人工核查: {type(exc).__name__}",
                            )
                            for index in range(len(chunk))
                        }
                    for index, (key, _record) in enumerate(chunk):
                        results[key] = batch_results[index]
                    completed += 1
                    print(f"[model] 批量调用进度: {completed}/{total_batches}", flush=True)
        return results

    concurrency = min(_title_model_concurrency(), len(unique_records))
    print(f"[model] 标题模型逐条调用: rows={len(records)} unique={len(unique_records)} concurrency={concurrency}", flush=True)
    results: dict[tuple[str, str, str, str], TitleMatchModelResult] = {}
    if concurrency <= 1:
        return {
            key: _call_title_model_for_item(record)
            for key, record in unique_records.items()
        }

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_key = {
            executor.submit(_call_title_model_for_item, record): key
            for key, record in unique_records.items()
        }
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[key] = TitleMatchModelResult(
                    match_result="MANUAL_REVIEW",
                    confidence=0.0,
                    model_match="unknown",
                    reason=f"标题模型并发调用失败，已降级人工核查: {type(exc).__name__}",
                )
    return results


def _decide_action(
    title_result: str,
    sku_check: str,
    price_check: str,
    price_vs_customer_check: str,
    reachable: bool | None,
    off_shelf: bool | None,
) -> tuple[WorkflowAction, list[WorkflowAction]]:
    if price_vs_customer_check == "HIGHER_THAN_CUSTOMER_PRICE_30":
        return WorkflowAction.REJECT, [WorkflowAction.REJECT, WorkflowAction.MANUAL_CHECK]
    if title_result == "NOT_SAME" or sku_check == "MISMATCH" or price_check == "MISMATCH" or reachable is False or off_shelf is True:
        return WorkflowAction.REJECT, [WorkflowAction.REJECT, WorkflowAction.MANUAL_CHECK]
    if (
        title_result == "SAME"
        and sku_check in {"MATCH", "UNKNOWN"}
        and price_check == "MATCH"
        and reachable is not False
        and off_shelf is not True
    ):
        return WorkflowAction.APPROVE, [WorkflowAction.APPROVE]
    return WorkflowAction.MANUAL_CHECK, [WorkflowAction.MANUAL_CHECK]


def _reason(
    title_result: str,
    sku_check: str,
    price_check: str,
    price_vs_customer_check: str,
    reachable: bool | None,
    off_shelf: bool | None,
    evidence: list[str],
) -> str:
    if price_vs_customer_check == "HIGHER_THAN_CUSTOMER_PRICE_30":
        return "候选价格高于客户价格30%以上，不建议作为同款审批数据，建议人工驳回前复核"
    if title_result == "MANUAL_REVIEW" and any("替代款" in item for item in evidence):
        return "型号命中，但标题含“替代款”，需人工确认是否为原型号同款"
    if title_result == "NOT_SAME":
        return "标题模型判断非同款，建议人工驳回前复核"
    if reachable is False:
        return "候选链接不可访问，建议人工驳回前复核"
    if off_shelf is True:
        return "候选商品疑似下架，建议人工驳回前复核"
    if sku_check == "MISMATCH":
        return "SKU 与链接不一致，建议人工驳回前复核"
    if price_check == "MISMATCH":
        return "SaaS JSON 价格与页面实际价格不一致，建议人工驳回前复核"
    if price_check == "WAIT_PAGE_PRICE_CHECK":
        return "缺少页面实际价格，待页面价格核验"
    if title_result == "SAME" and price_check == "MATCH":
        return "标题模型判断同款且价格一致，建议人工审批通过"
    return "需人工核查同款关系、SKU 链接和价格一致性"


def audit_saas_approval_item(
    item: SaasSameProductApprovalItem,
    use_model: bool = False,
    use_link_audit: bool = True,
    title_model_result: TitleMatchModelResult | None = None,
    crawl_results: list[InternalCrawlResult] | None = None,
    use_crawl_result: bool = False,
) -> SaasSameProductApprovalAuditResult:
    if title_model_result is not None:
        title_result = title_model_result.match_result
        title_confidence = title_model_result.confidence
        title_evidence = [
            f"title_model.match_result={title_model_result.match_result}",
            f"title_model.confidence={title_model_result.confidence}",
        ]
        if title_model_result.reason:
            title_evidence.append(f"title_model.reason={title_model_result.reason}")
        title_evidence.extend([f"title_model.risk={risk}" for risk in title_model_result.risk_points])
    else:
        title_result, title_confidence, title_evidence = _title_result_from_model_or_rule(item, use_model)

    page_price = get_page_actual_price(item)
    crawl_result: InternalCrawlResult | None = None
    crawl_evidence: list[str] = []
    if use_crawl_result:
        crawl_result, crawl_evidence = match_crawl_result_with_evidence(item, crawl_results or [])
        link_result = _crawl_link_result(item, crawl_result, crawl_evidence)
    elif use_link_audit:
        link_result = audit_link(LinkAuditRequest(
            ext_link=item.extLink,
            sku_id=item.skuId,
            vendor_name=item.vendorName,
            shop_name=item.shopName,
            json_price=item.price,
            page_actual_price=page_price,
        ))
    else:
        sku_check, sku_evidence = check_sku_link_consistency(item.skuId, item.extLink)
        price_check, price_evidence = check_price_consistency(item.price, page_price)
        link_result = LinkAuditResult(
            sku_link_check=sku_check,
            page_price=page_price,
            price_check=price_check,
            evidence=[*sku_evidence, *price_evidence],
        )

    customer_price = get_customer_price(item)
    candidate_price = get_candidate_price(item, link_result.page_price)
    price_vs_customer_check, customer_price_evidence = check_candidate_price_against_customer_price(candidate_price, customer_price)
    evidence = [
        f"vendor={normalize_mall_name(item.vendorName) or normalize_mall_name(item.shopName)}",
        *title_evidence,
        *link_result.evidence,
        *customer_price_evidence,
    ]
    action, suggested = _decide_action(
        title_result,
        link_result.sku_link_check,
        link_result.price_check,
        price_vs_customer_check,
        link_result.reachable,
        link_result.off_shelf,
    )
    if use_crawl_result and crawl_result is None:
        action = WorkflowAction.MANUAL_CHECK
        suggested = [WorkflowAction.MANUAL_CHECK]
    if use_crawl_result and crawl_result is not None and _is_crawl_status(
        crawl_result,
        "SOLD_OUT",
        "PRICE_MISSING",
        "CAPTCHA",
        "ACCESS_DENIED",
        "TIMEOUT",
        "FAILED",
        "UNKNOWN",
    ):
        if action == WorkflowAction.APPROVE:
            action = WorkflowAction.MANUAL_CHECK
            suggested = [WorkflowAction.MANUAL_CHECK]
    if use_crawl_result and crawl_result is not None and link_result.price_source.upper() == "OCR" and action == WorkflowAction.APPROVE:
        action = WorkflowAction.MANUAL_CHECK
        suggested = [WorkflowAction.MANUAL_CHECK]
        evidence.append("价格仅来自 OCR，不能作为自动通过依据，需人工复核")
    risk_words = detect_title_risk_words(item.queryStr, item.skuName)
    if risk_words:
        evidence.append(f"命中标题风险词：{','.join(risk_words)}")
        if action == WorkflowAction.APPROVE:
            action = WorkflowAction.MANUAL_CHECK
            suggested = [WorkflowAction.MANUAL_CHECK]
    reason = _reason(title_result, link_result.sku_link_check, link_result.price_check, price_vs_customer_check, link_result.reachable, link_result.off_shelf, evidence)
    if risk_words and action == WorkflowAction.MANUAL_CHECK:
        reason = "标题包含替代款/通用/适用/升级款等风险词，需人工确认是否为原型号同款"
    return SaasSameProductApprovalAuditResult(
        vCommodityId=item.vCommodityId,
        skuId=item.skuId,
        extLink=item.extLink,
        queryStr=item.queryStr,
        skuName=item.skuName,
        vendorName=item.vendorName,
        shopName=item.shopName,
        json_price=item.price,
        page_actual_price=link_result.page_price,
        customer_price=customer_price,
        price_vs_customer_check=price_vs_customer_check,
        crawl_status=link_result.crawl_status,
        page_status=link_result.page_status,
        page_title=link_result.page_title,
        page_price=link_result.page_price,
        crawl_error_code=link_result.error_code,
        crawl_error_message=link_result.error_message,
        screenshot_path=link_result.screenshot_path,
        crawl_matched=crawl_result is not None,
        title_match_result=title_result,
        title_match_confidence=title_confidence,
        sku_link_check=link_result.sku_link_check,
        price_check=link_result.price_check,
        reachable=link_result.reachable,
        off_shelf=link_result.off_shelf,
        primary_action=action,
        suggested_actions=suggested,
        reason=reason,
        evidence=evidence,
        needs_human_approval=True,
        execution_allowed=False,
        executed=False,
    )


def enforce_saas_audit_recommendation_only(
    response: SaasSameProductApprovalAuditResponse,
) -> SaasSameProductApprovalAuditResponse:
    safe_results: list[SaasSameProductApprovalAuditResult] = []
    for result in response.record_results:
        safe_results.append(result.model_copy(update={
            "needs_human_approval": True,
            "execution_allowed": False,
            "executed": False,
        }))
    return response.model_copy(update={
        "record_results": safe_results,
        "execution_allowed": False,
        "executed": False,
    })


def audit_saas_approval_records(
    records: list[SaasSameProductApprovalItem],
    use_model: bool = True,
    use_link_audit: bool = True,
    crawl_results: list[InternalCrawlResult] | None = None,
    use_crawl_result: bool = True,
) -> SaasSameProductApprovalAuditResponse:
    title_model_results = _precompute_title_model_results(records) if use_model else {}
    results = [
        audit_saas_approval_item(
            record,
            use_model=False if use_model else use_model,
            use_link_audit=use_link_audit,
            title_model_result=title_model_results.get(_title_model_cache_key(record)),
            crawl_results=crawl_results,
            use_crawl_result=use_crawl_result,
        )
        for record in records
    ]
    groups: dict[str, list[SaasSameProductApprovalAuditResult]] = {}
    for result in results:
        groups.setdefault(result.vCommodityId, []).append(result)

    summary: dict[str, int | str | float | bool] = {
        "total_records": len(results),
        "total_v_commodities": len(groups),
        "approve_suggestion_count": len([r for r in results if r.primary_action == WorkflowAction.APPROVE]),
        "manual_check_count": len([r for r in results if r.primary_action == WorkflowAction.MANUAL_CHECK]),
        "reject_suggestion_count": len([r for r in results if r.primary_action == WorkflowAction.REJECT]),
        "wait_page_price_check_count": len([r for r in results if r.price_check == "WAIT_PAGE_PRICE_CHECK"]),
        "price_mismatch_count": len([r for r in results if r.price_check == "MISMATCH"]),
        "sku_mismatch_count": len([r for r in results if r.sku_link_check == "MISMATCH"]),
        "higher_than_customer_price_30_count": len([r for r in results if r.price_vs_customer_check == "HIGHER_THAN_CUSTOMER_PRICE_30"]),
        "customer_price_missing_count": len([r for r in results if r.price_vs_customer_check == "CUSTOMER_PRICE_MISSING"]),
        "crawl_result_matched_count": len([r for r in results if r.crawl_matched]),
        "crawl_result_missing_count": len([r for r in results if not r.crawl_matched]),
        "crawl_off_shelf_count": len([r for r in results if r.crawl_status == "OFF_SHELF" or r.page_status == "OFF_SHELF"]),
        "crawl_sold_out_count": len([r for r in results if r.crawl_status == "SOLD_OUT" or r.page_status == "SOLD_OUT"]),
        "crawl_price_missing_count": len([r for r in results if r.crawl_status == "PRICE_MISSING" or r.price_check == "WAIT_PAGE_PRICE_CHECK"]),
        "crawl_failed_count": len([r for r in results if r.crawl_status in {"FAILED", "TIMEOUT", "CAPTCHA", "ACCESS_DENIED"}]),
        "crawl_price_mismatch_count": len([r for r in results if r.price_check == "MISMATCH" and r.crawl_matched]),
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
