from decimal import Decimal
from typing import Any

from app.schemas.workflow import (
    SameProductCandidate,
    SameProductRecord,
    WorkflowAction,
    WorkflowAnalyzeResponse,
    WorkflowRecordResult,
)

WAIT_CRAWLER_STATUSES = frozenset({"同款匹配中", "采集匹配中"})
VALIDATE_FAILED_STATUSES = frozenset({"校验失败", "校验失败数据", "VALIDATION_FAILED"})

PRICE_DIFF_THRESHOLD = Decimal("0.30")
HIGH_PRICE_RATIO = Decimal("1.30")
MIN_VALID_SAME_COUNT = 2
MIN_VALID_MALL_COUNT = 2


def _normalize_mall(mall_name: str | None) -> str:
    if mall_name is None:
        return ""
    return mall_name.strip()


def _has_valid_price(c: SameProductCandidate) -> bool:
    if c.crawl_price is not None and c.crawl_price > 0:
        return True
    if c.page_actual_price is not None and c.page_actual_price > 0:
        return True
    return False


def _is_valid_candidate(c: SameProductCandidate) -> bool:
    if c.is_same_product is not True:
        return False
    if not c.is_link_valid:
        return False
    if not c.is_product_on_shelf:
        return False
    if not _normalize_mall(c.mall_name):
        return False
    if not _has_valid_price(c):
        return False
    return True


def _is_confirmed_same_product_with_invalid_price(c: SameProductCandidate) -> bool:
    if c.is_same_product is not True:
        return False
    if not c.is_link_valid:
        return False
    if not c.is_product_on_shelf:
        return False
    if not _normalize_mall(c.mall_name):
        return False
    if _has_valid_price(c):
        return False
    return True


def _candidate_link_or_shelf_invalid(c: SameProductCandidate) -> bool:
    return (not c.is_link_valid) or (not c.is_product_on_shelf)


# === 业务决策（保留 R3 决策） ===
# 客户倾向保留尽可能低且合理价格，因此 R3 暂不以"低于参考价 × 0.7"作为 MANUAL_CHECK 条件。
# 仅在 max_price > reference_price * 1.3 时触发 R09_REF_PRICE_TOO_HIGH。


def _derive_metrics(candidates: list[SameProductCandidate]) -> dict:
    valid = [c for c in candidates if _is_valid_candidate(c)]
    valid_same_count = len(valid)

    valid_malls: set[str] = set()
    for c in valid:
        mall = _normalize_mall(c.mall_name)
        if mall:
            valid_malls.add(mall)
    valid_mall_count = len(valid_malls)

    prices: list[Decimal] = []
    for c in valid:
        if c.crawl_price is not None and c.crawl_price > 0:
            prices.append(c.crawl_price)
        elif c.page_actual_price is not None and c.page_actual_price > 0:
            prices.append(c.page_actual_price)

    min_price = min(prices) if prices else None
    max_price = max(prices) if prices else None

    if min_price is not None and min_price > 0 and max_price is not None:
        price_diff_pct = (max_price - min_price) / min_price
    else:
        price_diff_pct = None

    return {
        "valid_same_count": valid_same_count,
        "valid_mall_count": valid_mall_count,
        "min_price": min_price,
        "max_price": max_price,
        "price_diff_pct": price_diff_pct,
    }


def _collect_link_issues(candidates: list[SameProductCandidate]) -> list[str]:
    issues: list[str] = []
    for i, c in enumerate(candidates):
        if not c.is_link_valid:
            issues.append(f"candidate[{i}] mall={c.mall_name} link_invalid")
        elif not c.is_product_on_shelf:
            issues.append(f"candidate[{i}] mall={c.mall_name} off_shelf")
    return issues


def _collect_invalid_price_evidence(candidates: list[SameProductCandidate]) -> list[str]:
    issues: list[str] = []
    for i, c in enumerate(candidates):
        if _is_confirmed_same_product_with_invalid_price(c):
            issues.append(
                f"candidate[{i}] mall={c.mall_name} crawl_price={c.crawl_price} "
                f"page_actual_price={c.page_actual_price} 价格缺失或<=0"
            )
    return issues


def _collect_empty_mall_evidence(candidates: list[SameProductCandidate]) -> list[str]:
    issues: list[str] = []
    for i, c in enumerate(candidates):
        if c.is_same_product is True and not _normalize_mall(c.mall_name):
            issues.append(f"candidate[{i}] mall_name 为空，不计入有效同款")
    return issues


def _collect_price_inconsistency(candidates: list[SameProductCandidate]) -> list[str]:
    issues: list[str] = []
    for i, c in enumerate(candidates):
        if c.crawl_price is None or c.page_actual_price is None:
            continue
        if c.crawl_price != c.page_actual_price:
            issues.append(
                f"candidate[{i}] mall={c.mall_name} crawl={c.crawl_price} "
                f"page={c.page_actual_price}"
            )
    return issues


def _result(
    *,
    code: str,
    primary: WorkflowAction,
    suggested: list[WorkflowAction],
    reason: str,
    rule_id: str,
    evidence: list[str],
    pushable: bool = False,
    ready_to_push: bool = False,
) -> WorkflowRecordResult:
    return WorkflowRecordResult(
        standard_commodity_code=code,
        primary_action=primary,
        suggested_actions=suggested,
        action=primary,
        reason=reason,
        pushable=pushable,
        ready_to_push=ready_to_push,
        needs_human_approval=True,
        execution_allowed=False,
        executed=False,
        rule_id=rule_id,
        evidence=evidence,
    )


def evaluate_record(record: SameProductRecord) -> WorkflowRecordResult:
    code = record.standard_commodity_code
    candidates = record.candidates
    status = (record.current_status or "").strip()
    m = _derive_metrics(candidates)
    valid_same_count = m["valid_same_count"]
    valid_mall_count = m["valid_mall_count"]
    min_price = m["min_price"]
    max_price = m["max_price"]
    price_diff_pct = m["price_diff_pct"]

    base_evidence = [
        f"valid_same_count={valid_same_count}",
        f"valid_mall_count={valid_mall_count}",
        f"min_price={min_price}",
        f"max_price={max_price}",
        f"price_diff_pct={price_diff_pct}",
        f"current_status={status or '(empty)'}",
    ]

    empty_mall_evidence = _collect_empty_mall_evidence(candidates)

    # R01: 同款采集回流中
    if status in WAIT_CRAWLER_STATUSES:
        return _result(
            code=code,
            primary=WorkflowAction.WAIT_CRAWLER,
            suggested=[WorkflowAction.WAIT_CRAWLER],
            reason=f"同款仍在采集回流中（current_status={status}），不能直接推送",
            rule_id="R01_WAIT_CRAWLER",
            evidence=base_evidence,
        )

    # R12: 校验失败 -> 必须排在 APPROVE 之前，避免 happy path 覆盖
    if status in VALIDATE_FAILED_STATUSES:
        return _result(
            code=code,
            primary=WorkflowAction.MANUAL_CHECK,
            suggested=[WorkflowAction.MANUAL_CHECK],
            reason="当前状态为校验失败，需人工核查失败原因，不能直接审批通过或推送",
            rule_id="R12_VALIDATE_FAILED",
            evidence=base_evidence + [f"当前状态={status}"],
        )

    # R02: 候选为空
    if not candidates:
        return _result(
            code=code,
            primary=WorkflowAction.SUPPLEMENT_SAME_PRODUCT,
            suggested=[WorkflowAction.SUPPLEMENT_SAME_PRODUCT],
            reason="candidates 为空，无同款数据，无法生成价格指数，建议补同款",
            rule_id="R02_NO_CANDIDATES",
            evidence=base_evidence,
        )

    # R06A: 全部候选链接失效或商品下架（必须在 R03 之前）
    all_link_or_shelf_invalid = all(_candidate_link_or_shelf_invalid(c) for c in candidates)
    if all_link_or_shelf_invalid:
        return _result(
            code=code,
            primary=WorkflowAction.REJECT,
            suggested=[WorkflowAction.REJECT],
            reason="所有候选链接均失效或商品下架，建议人工驳回",
            rule_id="R06A_ALL_LINK_INVALID_OR_OFFSHELF",
            evidence=base_evidence + ["所有候选链接失效或下架"] + _collect_link_issues(candidates),
        )

    # R06B: 部分候选链接失效或商品下架（必须在 APPROVE 之前；不输出 REJECT）
    has_link_or_shelf_invalid = any(_candidate_link_or_shelf_invalid(c) for c in candidates)
    if has_link_or_shelf_invalid:
        return _result(
            code=code,
            primary=WorkflowAction.MANUAL_CHECK,
            suggested=[WorkflowAction.MANUAL_CHECK],
            reason="存在部分候选链接失效或下架，需人工剔除异常链接后再判断是否审批",
            rule_id="R06B_PARTIAL_LINK_INVALID_OR_OFFSHELF",
            evidence=base_evidence + ["存在部分候选链接失效或下架"] + _collect_link_issues(candidates),
        )

    # R08: 已确认同款（链接/上架/商城均有效）但价格缺失或 <=0（可达版本）
    invalid_price_evidence = _collect_invalid_price_evidence(candidates)
    if invalid_price_evidence:
        return _result(
            code=code,
            primary=WorkflowAction.MANUAL_CHECK,
            suggested=[WorkflowAction.MANUAL_CHECK],
            reason="存在已确认同款但价格缺失或价格无效，需人工核查价格采集结果",
            rule_id="R08_PRICE_INVALID",
            evidence=base_evidence + ["存在已确认同款但价格缺失或无效"] + invalid_price_evidence,
        )

    # R03: 有效同款=0
    if valid_same_count == 0:
        return _result(
            code=code,
            primary=WorkflowAction.SUPPLEMENT_SAME_PRODUCT,
            suggested=[WorkflowAction.SUPPLEMENT_SAME_PRODUCT],
            reason="有效同款数=0，无法生成价格指数，建议补同款",
            rule_id="R03_VALID_COUNT_ZERO",
            evidence=base_evidence + empty_mall_evidence,
        )

    # R04: 有效同款=1
    if valid_same_count == 1:
        return _result(
            code=code,
            primary=WorkflowAction.SUPPLEMENT_SAME_PRODUCT,
            suggested=[WorkflowAction.SUPPLEMENT_SAME_PRODUCT, WorkflowAction.DO_NOT_PUSH],
            reason="仅 1 条有效同款，无法生成价格指数（要求 >=2 条不同商城），建议补同款",
            rule_id="R04_VALID_COUNT_ONE",
            evidence=base_evidence + empty_mall_evidence,
        )

    # R05: 有效同款>=2 但有效商城<2（同商城重复）
    if valid_mall_count < MIN_VALID_MALL_COUNT:
        return _result(
            code=code,
            primary=WorkflowAction.MANUAL_CHECK,
            suggested=[WorkflowAction.MANUAL_CHECK],
            reason=f"有效同款数={valid_same_count} 但有效商城数={valid_mall_count}，存在一物多码风险",
            rule_id="R05_SAME_MALL_DUPLICATE",
            evidence=base_evidence,
        )

    # R07: crawl_price 与 page_actual_price 不一致
    price_issues = _collect_price_inconsistency(candidates)
    if price_issues:
        return _result(
            code=code,
            primary=WorkflowAction.MANUAL_CHECK,
            suggested=[WorkflowAction.MANUAL_CHECK],
            reason="存在 crawl_price 与 page_actual_price 不一致的同款，需人工核查",
            rule_id="R07_PRICE_INCONSISTENT",
            evidence=base_evidence + price_issues,
        )

    # R09: 仅判最高价高于参考价 1.3 倍（保留 R3 业务决策：不判低于参考价）
    ref_price = record.reference_price
    if ref_price is not None and ref_price > 0 and max_price is not None:
        if max_price > ref_price * HIGH_PRICE_RATIO:
            return _result(
                code=code,
                primary=WorkflowAction.MANUAL_CHECK,
                suggested=[WorkflowAction.MANUAL_CHECK],
                reason=f"max_price={max_price} > reference_price({ref_price}) x 1.3，价格显著偏高",
                rule_id="R09_REF_PRICE_TOO_HIGH",
                evidence=base_evidence,
            )

    # R10: 价差 > 30%
    if price_diff_pct is not None and price_diff_pct > PRICE_DIFF_THRESHOLD:
        return _result(
            code=code,
            primary=WorkflowAction.MANUAL_CHECK,
            suggested=[WorkflowAction.MANUAL_CHECK, WorkflowAction.FEEDBACK_CUSTOMER],
            reason=f"价差 price_diff_pct={price_diff_pct} > 30%，需人工核查",
            rule_id="R10_PRICE_DIFF_TOO_LARGE",
            evidence=base_evidence,
        )

    # R13: 全部校验通过
    if (
        valid_same_count >= MIN_VALID_SAME_COUNT
        and valid_mall_count >= MIN_VALID_MALL_COUNT
        and min_price is not None
        and max_price is not None
    ):
        approve_evidence = [
            f"有效同款数={valid_same_count}",
            f"有效商城数={valid_mall_count}",
            f"最低价={min_price}",
            f"最高价={max_price}",
            f"价差比例={price_diff_pct}",
            "采集价与页面价一致",
            "链接均有效且商品上架",
        ]
        return _result(
            code=code,
            primary=WorkflowAction.APPROVE,
            suggested=[WorkflowAction.APPROVE],
            reason="校验通过：有效同款>=2、有效商城>=2、链接有效、价格一致、价差<=30%、未显著高于参考价；建议审批通过并推送",
            rule_id="R13_APPROVE",
            evidence=approve_evidence,
            pushable=True,
            ready_to_push=True,
        )

    # R99: 兜底
    return _result(
        code=code,
        primary=WorkflowAction.MANUAL_CHECK,
        suggested=[WorkflowAction.MANUAL_CHECK],
        reason="未匹配任何明确规则，需人工核查",
        rule_id="R99_MANUAL_CHECK",
        evidence=base_evidence,
    )


def analyze_records(records: list[Any]) -> WorkflowAnalyzeResponse:
    results: list[WorkflowRecordResult] = []
    rule_counter: dict[str, int] = {}
    for rec in records:
        if isinstance(rec, SameProductRecord):
            r = evaluate_record(rec)
        else:
            parsed = SameProductRecord.model_validate(rec)
            r = evaluate_record(parsed)
        results.append(r)
        rid = r.rule_id or "UNKNOWN"
        rule_counter[rid] = rule_counter.get(rid, 0) + 1

    summary: dict[str, int | str | float | bool] = {
        "status": "OK",
        "total": len(results),
        "execution_allowed": False,
        "executed": False,
    }
    for rid, cnt in rule_counter.items():
        summary[f"rule_count.{rid}"] = cnt

    return WorkflowAnalyzeResponse(
        workflow_name="work_01_same_product_approval",
        total_records=len(results),
        summary=summary,
        record_results=results,
        execution_allowed=False,
        executed=False,
    )
