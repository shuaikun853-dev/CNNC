from decimal import Decimal

import pytest

from app.schemas.workflow import (
    SameProductCandidate,
    SameProductRecord,
    WorkflowAction,
    WorkflowScope,
)
from app.workflows.work_01_same_product_approval import (
    analyze_records,
    evaluate_record,
)


CNNC_SCOPE = WorkflowScope(project="CNNC", tenant_id=188605, commodity_pool_id=222)


def _cand(
    *,
    mall: str = "JD",
    crawl: str | None = "100",
    page: str | None = "100",
    is_same: bool | None = True,
    link_valid: bool = True,
    on_shelf: bool = True,
) -> SameProductCandidate:
    return SameProductCandidate(
        link=f"https://example.com/{mall}",
        mall_name=mall,
        crawl_price=Decimal(crawl) if crawl is not None else None,
        page_actual_price=Decimal(page) if page is not None else None,
        is_same_product=is_same,
        is_link_valid=link_valid,
        is_product_on_shelf=on_shelf,
    )


def _rec(
    *,
    code: str = "C001",
    status: str = "",
    ref: str | None = None,
    candidates: list[SameProductCandidate] | None = None,
) -> SameProductRecord:
    return SameProductRecord(
        scope=CNNC_SCOPE,
        standard_commodity_code=code,
        current_status=status,
        reference_price=Decimal(ref) if ref is not None else None,
        candidates=candidates or [],
    )


def test_r01_wait_crawler_when_status_matching():
    r = _rec(status="同款匹配中", candidates=[_cand(mall="JD"), _cand(mall="TM")])
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.WAIT_CRAWLER
    assert res.rule_id == "R01_WAIT_CRAWLER"
    assert res.execution_allowed is False
    assert res.executed is False
    assert res.needs_human_approval is True


def test_r02_no_candidates_supplement():
    r = _rec(candidates=[])
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.SUPPLEMENT_SAME_PRODUCT
    assert res.rule_id == "R02_NO_CANDIDATES"


def test_r03_valid_count_zero_all_none():
    r = _rec(candidates=[_cand(mall="JD", is_same=None), _cand(mall="TM", is_same=None)])
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.SUPPLEMENT_SAME_PRODUCT
    assert res.rule_id == "R03_VALID_COUNT_ZERO"


def test_r04_valid_count_one():
    r = _rec(
        candidates=[
            _cand(mall="JD", is_same=True),
            _cand(mall="TM", is_same=False),
        ]
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.SUPPLEMENT_SAME_PRODUCT
    assert res.rule_id == "R04_VALID_COUNT_ONE"
    assert WorkflowAction.DO_NOT_PUSH in res.suggested_actions


def test_r05_same_mall_duplicate():
    r = _rec(
        candidates=[
            _cand(mall="JD", crawl="100", page="100"),
            _cand(mall="JD", crawl="105", page="105"),
        ]
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.MANUAL_CHECK
    assert res.rule_id == "R05_SAME_MALL_DUPLICATE"


def test_r06b_partial_invalid_link_manual_check():
    r = _rec(
        candidates=[
            _cand(mall="JD", crawl="100", page="100"),
            _cand(mall="TM", crawl="105", page="105"),
            _cand(mall="PDD", link_valid=False),
        ]
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.MANUAL_CHECK
    assert res.rule_id == "R06B_PARTIAL_LINK_INVALID_OR_OFFSHELF"
    assert res.ready_to_push is False


def test_r06b_partial_off_shelf_manual_check():
    r = _rec(
        candidates=[
            _cand(mall="JD", crawl="100", page="100"),
            _cand(mall="TM", crawl="105", page="105"),
            _cand(mall="PDD", on_shelf=False),
        ]
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.MANUAL_CHECK
    assert res.rule_id == "R06B_PARTIAL_LINK_INVALID_OR_OFFSHELF"
    assert "off_shelf" in " ".join(res.evidence)



def test_r07_price_inconsistent_manual_check():
    r = _rec(
        candidates=[
            _cand(mall="JD", crawl="100", page="100"),
            _cand(mall="TM", crawl="105", page="120"),
        ]
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.MANUAL_CHECK
    assert res.rule_id == "R07_PRICE_INCONSISTENT"


def test_r09_ref_price_too_high_only_high_side():
    r = _rec(
        ref="100",
        candidates=[
            _cand(mall="JD", crawl="140", page="140"),
            _cand(mall="TM", crawl="150", page="150"),
        ],
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.MANUAL_CHECK
    assert res.rule_id == "R09_REF_PRICE_TOO_HIGH"


def test_low_price_does_not_trigger_ref_rule_per_user_decision():
    r = _rec(
        ref="100",
        candidates=[
            _cand(mall="JD", crawl="50", page="50"),
            _cand(mall="TM", crawl="55", page="55"),
        ],
    )
    res = evaluate_record(r)
    assert res.rule_id != "R09_REF_PRICE_TOO_HIGH"


def test_r10_price_diff_too_large():
    r = _rec(
        candidates=[
            _cand(mall="JD", crawl="100", page="100"),
            _cand(mall="TM", crawl="200", page="200"),
        ]
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.MANUAL_CHECK
    assert res.rule_id == "R10_PRICE_DIFF_TOO_LARGE"


def test_r13_approve_happy_path():
    r = _rec(
        ref="100",
        candidates=[
            _cand(mall="JD", crawl="100", page="100"),
            _cand(mall="TM", crawl="105", page="105"),
        ],
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.APPROVE
    assert res.rule_id == "R13_APPROVE"
    assert res.pushable is True
    assert res.ready_to_push is True
    assert res.execution_allowed is False
    assert res.executed is False
    assert res.needs_human_approval is True


def test_none_is_same_not_counted_as_valid():
    r = _rec(
        candidates=[
            _cand(mall="JD", is_same=True),
            _cand(mall="TM", is_same=None),
            _cand(mall="PDD", is_same=None),
        ]
    )
    res = evaluate_record(r)
    assert res.rule_id == "R04_VALID_COUNT_ONE"


def test_analyze_records_summary_and_rule_counts():
    r1 = _rec(code="A", candidates=[])
    r2 = _rec(
        code="B",
        ref="100",
        candidates=[
            _cand(mall="JD", crawl="100", page="100"),
            _cand(mall="TM", crawl="105", page="105"),
        ],
    )
    resp = analyze_records([r1, r2])
    assert resp.workflow_name == "work_01_same_product_approval"
    assert resp.total_records == 2
    assert resp.execution_allowed is False
    assert resp.executed is False
    assert resp.summary["rule_count.R02_NO_CANDIDATES"] == 1
    assert resp.summary["rule_count.R13_APPROVE"] == 1


def test_analyze_records_accepts_dict_input():
    payload = [
        _rec(
            code="X",
            candidates=[
                _cand(mall="JD", crawl="100", page="100"),
                _cand(mall="TM", crawl="105", page="105"),
            ],
        ).model_dump()
    ]
    resp = analyze_records(payload)
    assert resp.total_records == 1
    assert resp.record_results[0].primary_action == WorkflowAction.APPROVE


# ===== P1-1: mall_name 空白不计入有效同款 =====
def test_p1_1_empty_mall_not_counted_as_valid():
    r = _rec(
        candidates=[
            _cand(mall="", crawl="100", page="100"),
            _cand(mall="京东", crawl="105", page="105"),
        ]
    )
    res = evaluate_record(r)
    assert res.rule_id == "R04_VALID_COUNT_ONE"
    assert res.primary_action == WorkflowAction.SUPPLEMENT_SAME_PRODUCT
    assert WorkflowAction.DO_NOT_PUSH in res.suggested_actions


def test_p1_1_whitespace_mall_not_counted_as_valid():
    r = _rec(
        candidates=[
            _cand(mall="   ", crawl="100", page="100"),
            _cand(mall="京东", crawl="105", page="105"),
        ]
    )
    res = evaluate_record(r)
    assert res.rule_id == "R04_VALID_COUNT_ONE"
    assert "mall_name 为空" in " ".join(res.evidence)


# ===== P1-2: R12_VALIDATE_FAILED =====
def test_p1_2_validate_failed_blocks_approve():
    r = _rec(
        status="校验失败",
        ref="100",
        candidates=[
            _cand(mall="京东", crawl="100", page="100"),
            _cand(mall="天猫", crawl="105", page="105"),
        ],
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.MANUAL_CHECK
    assert res.rule_id == "R12_VALIDATE_FAILED"
    assert res.ready_to_push is False
    assert res.execution_allowed is False
    assert res.executed is False


def test_p1_2_validate_failed_aliases():
    for status in ["校验失败数据", "VALIDATION_FAILED"]:
        r = _rec(
            status=status,
            candidates=[
                _cand(mall="京东", crawl="100", page="100"),
                _cand(mall="天猫", crawl="105", page="105"),
            ],
        )
        res = evaluate_record(r)
        assert res.rule_id == "R12_VALIDATE_FAILED", f"status={status}"


# ===== P1-3: R08_PRICE_INVALID（可达版本） =====
def test_p1_3_price_invalid_zero_crawl_none_page():
    r = _rec(
        candidates=[
            _cand(mall="京东", crawl="0", page=None),
            _cand(mall="天猫", crawl="105", page="105"),
        ]
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.MANUAL_CHECK
    assert res.rule_id == "R08_PRICE_INVALID"
    assert res.ready_to_push is False


def test_p1_3_price_invalid_both_none():
    r = _rec(
        candidates=[
            _cand(mall="京东", crawl=None, page=None),
            _cand(mall="天猫", crawl="105", page="105"),
        ]
    )
    res = evaluate_record(r)
    assert res.rule_id == "R08_PRICE_INVALID"


# ===== P1-4: R06A 全部失效 / R06B 部分失效 =====
def test_p1_4_r06a_all_link_invalid_reject():
    r = _rec(
        candidates=[
            _cand(mall="京东", link_valid=False),
            _cand(mall="天猫", on_shelf=False),
        ]
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.REJECT
    assert res.rule_id == "R06A_ALL_LINK_INVALID_OR_OFFSHELF"
    assert "所有候选链接失效或下架" in " ".join(res.evidence)


def test_p1_4_r06b_two_valid_one_invalid_manual_check():
    r = _rec(
        candidates=[
            _cand(mall="京东", crawl="100", page="100"),
            _cand(mall="天猫", crawl="105", page="105"),
            _cand(mall="拼多多", link_valid=False),
        ]
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.MANUAL_CHECK
    assert res.rule_id == "R06B_PARTIAL_LINK_INVALID_OR_OFFSHELF"
    assert res.ready_to_push is False


# ===== 保留业务决策：低于参考价但合理 =====
def test_low_price_but_reasonable_should_approve():
    # 客户倾向保留尽可能低且合理价格，因此 R3 暂不以"低于参考价 × 0.7"作为 MANUAL_CHECK 条件
    r = _rec(
        ref="100",
        candidates=[
            _cand(mall="京东", crawl="50", page="50"),
            _cand(mall="天猫", crawl="55", page="55"),
        ],
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.APPROVE
    assert res.rule_id == "R13_APPROVE"
    assert res.ready_to_push is True


# ===== APPROVE evidence 完整性 =====
def test_approve_evidence_includes_all_required_fields():
    r = _rec(
        ref="100",
        candidates=[
            _cand(mall="京东", crawl="100", page="100"),
            _cand(mall="天猫", crawl="105", page="105"),
        ],
    )
    res = evaluate_record(r)
    assert res.primary_action == WorkflowAction.APPROVE
    ev = " ".join(res.evidence)
    assert "有效同款数=" in ev
    assert "有效商城数=" in ev
    assert "最低价=" in ev
    assert "最高价=" in ev
    assert "价差比例=" in ev
    assert "采集价与页面价一致" in ev
    assert "链接均有效且商品上架" in ev


# ===== 全局不变量：所有响应 execution_allowed=false / executed=false =====
def test_all_outputs_recommendation_only():
    cases = [
        _rec(status="同款匹配中", candidates=[_cand(mall="京东"), _cand(mall="天猫")]),
        _rec(status="校验失败", candidates=[_cand(mall="京东"), _cand(mall="天猫")]),
        _rec(candidates=[]),
        _rec(candidates=[_cand(mall="京东", link_valid=False), _cand(mall="天猫", on_shelf=False)]),
        _rec(candidates=[_cand(mall="京东", crawl="100", page="100"), _cand(mall="天猫", crawl="105", page="105"), _cand(mall="拼多多", link_valid=False)]),
        _rec(candidates=[_cand(mall="京东", crawl="0", page=None), _cand(mall="天猫", crawl="105", page="105")]),
        _rec(candidates=[_cand(mall="京东", crawl="100", page="100"), _cand(mall="京东", crawl="105", page="105")]),
        _rec(candidates=[_cand(mall="京东", crawl="100", page="120"), _cand(mall="天猫", crawl="105", page="105")]),
        _rec(ref="100", candidates=[_cand(mall="京东", crawl="200", page="200"), _cand(mall="天猫", crawl="210", page="210")]),
        _rec(candidates=[_cand(mall="京东", crawl="100", page="100"), _cand(mall="天猫", crawl="200", page="200")]),
        _rec(ref="100", candidates=[_cand(mall="京东", crawl="100", page="100"), _cand(mall="天猫", crawl="105", page="105")]),
    ]
    for r in cases:
        res = evaluate_record(r)
        assert res.execution_allowed is False, f"{res.rule_id} execution_allowed should be False"
        assert res.executed is False, f"{res.rule_id} executed should be False"
        assert res.needs_human_approval is True, f"{res.rule_id} needs_human_approval should be True"


# ===== R99 兜底（理论上不可达，但保留兜底覆盖） =====
def test_r99_fallback_unreachable_under_normal_inputs():
    r = _rec(
        ref="100",
        candidates=[
            _cand(mall="京东", crawl="100", page="100"),
            _cand(mall="天猫", crawl="105", page="105"),
        ],
    )
    res = evaluate_record(r)
    assert res.rule_id != "R99_MANUAL_CHECK"
