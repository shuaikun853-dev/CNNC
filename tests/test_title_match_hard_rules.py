"""Regression contracts for the SAME hard-rule double-insurance.

The model prompt stays the primary judge; these deterministic rules only
downgrade a model SAME to NOT_SAME when historically validated evidence
proves the titles cannot be the same product.  Every 等价表达 case below
must stay veto-free (不得误杀).
"""

from __future__ import annotations

import json

import pytest

from app.services.title_match_hard_rules import evaluate_same_hard_rules
from app.services.title_match_model_adapter import (
    TitleMatchModelRequest,
    call_title_match_model,
    call_title_match_model_batch,
)


# ---------------------------------------------------------------------------
# R1 — model-code single-sided / conflicting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("query_str", "sku_name"),
    [
        # 惠普 AR-EP65 硒鼓 原装
        ("惠普 AR-EP65 硒鼓 原装", "惠普 EP-25 硒鼓 原装"),
        # Explicit model present, candidate title never expresses it.
        ("世达 SATA 91403 两用批头", "世达 螺丝刀两用批头组套"),
        # Capacity explicitly on one side only.
        ("爱国者 U盘 128G 黑色", "爱国者 U盘 黑色"),
        ("得力 订书机 DS-320 中号", "得力 订书机 大号"),
        # Prefix collisions: a longer *different* model code that merely
        # contains the query's code as a substring is NOT the same model.
        ("球阀 DN20 黄铜", "球阀 DN200 黄铜"),
        ("得力 订书机 DS-320", "得力 订书机 DS-3200"),
        ("硒鼓 EP65 适配", "硒鼓 EP650 适配"),
        ("世达 批头 91403", "世达 批头 914030"),
    ],
    ids=(
        "ar-ep65-vs-ep25",
        "model-missing",
        "capacity-missing",
        "another-model-missing",
        "dn20-vs-dn200",
        "ds320-vs-ds3200",
        "ep65-vs-ep650",
        "91403-vs-914030",
    ),
)
def test_model_code_single_side_or_conflict_vetoes_same(query_str, sku_name):
    reason = evaluate_same_hard_rules(query_str, sku_name)
    assert reason is not None and reason.startswith("R1:"), reason


@pytest.mark.parametrize(
    ("query_str", "sku_name"),
    [
        # Brand prefix / connector variants are equivalent spellings.
        ("世达 SATA-91403 批头", "世达 91403 两用批头"),
        ("世达 91403 批头", "SATA 世达91403批头"),
        # Storage suffix equivalence.
        ("闪迪 G50-2TB 移动固态硬盘", "闪迪 G50-2T 移动固态硬盘"),
        ("移动硬盘 1TB USB3.0", "移动硬盘 1T USB3.0"),
        # Metric unit equivalence: 500mm = 50cm = 0.5m.
        ("钢直尺 500mm 不锈钢", "钢板尺 50cm 不锈钢"),
        ("钢直尺 500mm 不锈钢", "钢直尺 0.5m 不锈钢"),
        # Pure-letter words (sensor/marketing names) never anchor a veto.
        ("罗技 G502 HERO 有线鼠标", "罗技 G502 有线鼠标"),
        # Same code, different case and separators.
        ("KONICA MINOLTA bizhub 225i 复印机", "柯尼卡美能达 bizhub225i 复印机"),
        # Short codes (A4, 4K) are too weak to veto.
        ("复印纸 A4 70g 500张/包", "复印纸 A4 70g 每包500张"),
    ],
    ids=(
        "brand-prefix-equiv",
        "brand-prefix-equiv-reverse",
        "storage-tb-t-equiv",
        "tb-t-equiv",
        "mm-cm-equiv",
        "mm-m-equiv",
        "pure-letter-not-strong",
        "case-separator-equiv",
        "short-code-weak",
    ),
)
def test_equivalent_model_spellings_do_not_veto(query_str, sku_name):
    assert evaluate_same_hard_rules(query_str, sku_name) is None


# ---------------------------------------------------------------------------
# R2 — multi-pack single-sided / conflicting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("query_str", "sku_name"),
    [
        # 1000枚/盒 vs candidate without any multi-pack expression.
        ("标签贴纸 1000枚/盒 白色", "标签贴纸 白色 打印贴"),
        # Pack level present on one side only (包装层级不得被销售单位豁免).
        ("笔记本内页 100张/包", "笔记本内页 100张"),
        # Conflicting counts.
        ("标签贴纸 1000枚/盒", "标签贴纸 500枚/盒"),
        ("砂纸 50片/盒", "砂纸 80片/箱"),
        # Multi-pack declared only by the candidate.
        ("标签贴纸 白色", "标签贴纸 白色 800片/箱"),
    ],
    ids=(
        "pack-missing",
        "pack-level-missing",
        "pack-count-conflict",
        "pack-count-conflict-units",
        "sku-side-single",
    ),
)
def test_multipack_single_side_or_conflict_vetoes_same(query_str, sku_name):
    reason = evaluate_same_hard_rules(query_str, sku_name)
    assert reason is not None and reason.startswith("R2:"), reason


@pytest.mark.parametrize(
    ("query_str", "sku_name"),
    [
        # Equivalent pack expressions with the same count.
        ("标签贴纸 1000枚/盒 白色", "标签贴纸 1000张/盒 白色"),
        ("笔记本内页 100张/包", "笔记本内页 100张装"),
        ("打印纸 A4 500张/包", "打印纸 A4 每包500张"),
        # 4支/套 vs 四色套装4支: pack word + same piece count is equivalent.
        ("墨盒 四色套装 4支/套", "墨盒 黑色+彩色组合 四色套装4支"),
        # Single-piece sale units never constitute pack evidence.
        ("得力签字笔 0.5mm 黑色 单支装", "得力签字笔 0.5mm 黑色 1支"),
        ("剪刀 不锈钢 全钢 把", "剪刀 不锈钢 全钢 单把 1把"),
        # Neither side declares a pack.
        ("透明胶带 宽45mm", "透明胶带 45mm 宽"),
    ],
    ids=(
        "same-count-diff-piece-word",
        "denominator-vs-装",
        "denominator-vs-每",
        "set-word-equivalence",
        "single-piece-sale-unit",
        "single-piece-sale-unit-2",
        "no-pack-either-side",
    ),
)
def test_equivalent_pack_expressions_do_not_veto(query_str, sku_name):
    assert evaluate_same_hard_rules(query_str, sku_name) is None


def test_empty_inputs_never_veto():
    assert evaluate_same_hard_rules("", "x") is None
    assert evaluate_same_hard_rules("x", "") is None
    assert evaluate_same_hard_rules("  ", "  ") is None


# ---------------------------------------------------------------------------
# adapter integration: veto composes with the model verdict
# ---------------------------------------------------------------------------

def _same_response(index: int = 0) -> str:
    return json.dumps({
        "results": [
            {"index": index, "match_result": "SAME", "model_match": "exact", "confidence": 0.95}
        ]
    })


def _configure_env(monkeypatch):
    monkeypatch.setenv("TITLE_MATCH_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TITLE_MATCH_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("TITLE_MATCH_MODEL_NAME", "test-model")


def test_model_same_is_downgraded_by_hard_rule(monkeypatch):
    import app.services.title_match_model_adapter as adapter

    _configure_env(monkeypatch)
    monkeypatch.setattr(
        adapter, "_post_model_payload",
        lambda _payload, timeout_seconds=None: json.dumps(
            {"match_result": "SAME", "model_match": "exact", "confidence": 0.95}
        ),
    )
    result = call_title_match_model(
        TitleMatchModelRequest(
            query_str="惠普 AR-EP65 硒鼓 原装",
            sku_name="惠普 EP-25 硒鼓 原装",
        )
    )
    assert result.match_result == "NOT_SAME"
    assert "硬规则否决(R1:" in result.reason
    assert any("hard-rule veto" in point for point in result.risk_points)


def test_model_not_same_and_manual_review_are_untouched(monkeypatch):
    import app.services.title_match_model_adapter as adapter

    _configure_env(monkeypatch)
    responses = iter((
        json.dumps({"match_result": "NOT_SAME", "model_match": "mismatch", "confidence": 0.9}),
        json.dumps({"match_result": "MANUAL_REVIEW", "model_match": "partial", "confidence": 0.5}),
    ))
    monkeypatch.setattr(
        adapter, "_post_model_payload",
        lambda _payload, timeout_seconds=None: next(responses),
    )
    first = call_title_match_model(
        TitleMatchModelRequest(query_str="惠普 AR-EP65 硒鼓", sku_name="惠普 EP-25 硒鼓")
    )
    second = call_title_match_model(
        TitleMatchModelRequest(query_str="惠普 AR-EP65 硒鼓", sku_name="惠普 EP-25 硒鼓")
    )
    assert first.match_result == "NOT_SAME"
    assert "硬规则否决" not in first.reason
    assert second.match_result == "MANUAL_REVIEW"
    assert "硬规则否决" not in second.reason


def test_batch_same_results_are_vetoed_per_row(monkeypatch):
    import app.services.title_match_model_adapter as adapter

    _configure_env(monkeypatch)
    monkeypatch.setattr(
        adapter, "_post_model_payload",
        lambda _payload, timeout_seconds=None: json.dumps({
            "results": [
                {"index": 0, "match_result": "SAME", "model_match": "exact", "confidence": 0.95},
                {"index": 1, "match_result": "SAME", "model_match": "exact", "confidence": 0.95},
            ]
        }),
    )
    results = call_title_match_model_batch([
        TitleMatchModelRequest(query_str="惠普 AR-EP65 硒鼓", sku_name="惠普 EP-25 硒鼓"),
        TitleMatchModelRequest(query_str="世达 SATA-91403 批头", sku_name="世达 91403 两用批头"),
    ])
    assert results[0].match_result == "NOT_SAME"
    assert "硬规则否决(R1:" in results[0].reason
    assert results[1].match_result == "SAME"


def test_prompt_version_invalidates_with_tightened_rules():
    """TITLE_MATCH_PROMPT_VERSION is content-addressed over the real prompt."""
    import app.services.title_match_model_adapter as adapter

    assert adapter.TITLE_MATCH_PROMPT_VERSION.startswith("sha256:")
    # The single-sided clause and the conditional key-field requirement are
    # part of the hashed template, so any later edit flips the version
    # automatically.
    assert "单边缺失" in adapter._PROMPT_TEMPLATE
    # P2-1 contract: the key-field SAME requirement is conditional on what
    # query_str actually expresses — the old unconditional "双侧都有明确表达"
    # phrasing (which read as "every product must carry capacity/power
    # fields on both sides") must not come back.
    assert "凡 query_str 已明确表达的关键标识字段" in adapter._PROMPT_TEMPLATE
    assert "不要求 sku_name 凭空补齐" in adapter._PROMPT_TEMPLATE
    assert "不得仅因商品本身不存在功率/容量等字段而输出 MANUAL_REVIEW" in adapter._PROMPT_TEMPLATE
    assert "双侧都有明确表达" not in adapter._PROMPT_TEMPLATE


# ---------------------------------------------------------------------------
# P2-2: single/batch post-validation parity
# ---------------------------------------------------------------------------

_SAME_JSON = json.dumps(
    {"match_result": "SAME", "model_match": "exact", "confidence": 0.95}
)
_SAME_BATCH_JSON = json.dumps(
    {"results": [{"index": 0, "match_result": "SAME", "model_match": "exact", "confidence": 0.95}]}
)


def _both_paths(monkeypatch, query_str, sku_name, model_json):
    """Run one input through the single and the batch call with the same
    model verdict JSON and return both final results."""
    import app.services.title_match_model_adapter as adapter

    _configure_env(monkeypatch)

    def fake_post(payload, timeout_seconds=None):
        content = payload["messages"][0]["content"]
        if "逐条判断待判断数据" in content:
            return _SAME_BATCH_JSON
        return model_json

    monkeypatch.setattr(adapter, "_post_model_payload", fake_post)
    single = call_title_match_model(
        TitleMatchModelRequest(query_str=query_str, sku_name=sku_name)
    )
    batch = call_title_match_model_batch(
        [TitleMatchModelRequest(query_str=query_str, sku_name=sku_name)]
    )
    return single, batch[0]


@pytest.mark.parametrize(
    ("query_str", "sku_name", "expected"),
    [
        # 适用机型 on a consumable is normal description (prompt rule 7):
        # the adapter-level blanket risk-word downgrade for 适用 is gone on
        # BOTH paths, so a correct model SAME survives.  (The work_01 final
        # approval gate keeps its own conservative 适用 handling — that layer
        # applies equally to every verdict and is out of scope here.)
        ("惠普 88A 硒鼓 原装", "惠普88A硒鼓 适用HP M126 打印机", "SAME"),
        # Substitute wording without a confirmed target model fails closed
        # on BOTH paths.
        ("订书机 中号 办公", "通用型订书机 中号 办公", "MANUAL_REVIEW"),
        # Prompt rule 7 escape, deterministic form: the substitute word is
        # present but the query's strong model code is provably in the title.
        ("世达 91403 两用批头", "91403 通用批头 两用", "SAME"),
    ],
    ids=("适用机型-not-an-adapter-risk", "通用-no-model-confirmed", "通用-model-confirmed"),
)
def test_single_and_batch_paths_agree_on_post_validation(monkeypatch, query_str, sku_name, expected):
    single, batched = _both_paths(monkeypatch, query_str, sku_name, _SAME_JSON)
    assert single.match_result == expected
    assert batched.match_result == expected
    # The visible business fields agree too, not just the verdict code.
    assert single.reason == batched.reason
    assert single.risk_points == batched.risk_points


def test_veto_keeps_model_self_assessment_as_evidence(monkeypatch):
    """P2-3: the veto changes only match_result/reason/risk_points.

    Downstream business decisions consume only match_result (confidence/
    reason/risk_points surface as audit evidence; model_match/spec_match/
    brand_match have no consumer).  The retained model self-assessment
    documents what the model claimed before the override, and the appended
    reason/risk marker keeps the evidence trail self-explanatory — so the
    veto must not silently rewrite those retained fields.
    """
    single, batched = _both_paths(
        monkeypatch,
        "惠普 AR-EP65 硒鼓 原装",
        "惠普 EP-25 硒鼓 原装",
        _SAME_JSON,
    )
    for result in (single, batched):
        assert result.match_result == "NOT_SAME"
        # Model self-assessment retained verbatim as evidence of what the
        # model claimed before the deterministic override.
        assert result.model_match == "exact"
        assert result.spec_match is None or result.spec_match is not False
        assert result.confidence == 0.95
        # The override itself is explicit in the human-readable fields.
        assert "硬规则否决(R1:" in result.reason
        assert any("hard-rule veto" in point for point in result.risk_points)
