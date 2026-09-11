# -*- coding: utf-8 -*-
"""F2 重建层单元测试（合成 mini parquet 夹具, 不触碰真实数据文件）。

覆盖:
- p2_rules: ALGO winner 语义（评论非空>评论大>longValue 低价>首见）、longValue 截断、
  评论回退链、问题表判定映射（含未知值停止）、Step2 规则顺序/原因、verdict 优先级。
- p2_build_steps: 端到端跑 --stage all（夹具期望值 monkeypatch 注入）,
  断言 Step1 负例映射/键冲突合并/DIFY 定位与 AMBIGUOUS、Step2 各 R 规则触发与 FINAL 保护、
  Step3 组去重与 WINNER_RULE_CONFLICT、Step4 交集与评论四态、Step5 原样、lineage 与 stats。
"""
from __future__ import annotations

import json
import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import p2_build_steps as B
import p2_rules as R

LIBS = r"E:\.A中核运维\_cnnc_comment_index\_libs"
ALGO_SRC = (r"E:\.A中核运维\20260824-0829\周五"
            r"\cnnc_price_algorithms_review_v1\src\cnnc_price_algorithms.py")


def _wr() -> R.WinnerRules:
    return R.WinnerRules(R.load_algo(ALGO_SRC))


# ---------------------------------------------------------------------------
# 纯函数单测
# ---------------------------------------------------------------------------

def test_long_value_truncates_toward_zero():
    wr = _wr()
    assert wr.long_value(10.9) == 10
    assert wr.long_value(0.5) == 0
    assert wr.long_value(99.99) == 99
    assert wr.long_value(3.0) == 3
    assert wr.long_value(0.0) == 0


def test_comment_fallback_chain():
    wr = _wr()
    assert wr.comment_fallback("5", None) == 5
    assert wr.comment_fallback("", 7.0) == 7          # S3 空 → 回退 comments_final
    assert wr.comment_fallback(None, 7.0) == 7
    assert wr.comment_fallback("", None) is None
    assert wr.comment_fallback("0", 9.0) == 0         # 0 是有效评论数, 不回退
    assert wr.comment_fallback(None, 0.0) == 0
    # 非归一化展示串（真实数据存在）→ 按缺失回退, 不发明换算规则
    assert wr.comment_fallback("100+", 7.0) == 7
    assert wr.comment_fallback("1万+", None) is None
    assert wr.is_normalized_comment("100+") is False
    assert wr.is_normalized_comment("12") is True
    assert wr.is_normalized_comment(None) is True


def test_pick_winner_semantics():
    wr = _wr()
    # 评论非空 > 评论空
    rows = [{"sku": "a", "mall": "m", "price": 1.0, "comment": None},
            {"sku": "b", "mall": "m", "price": 9.0, "comment": 1}]
    assert rows[wr.pick_winner_index(rows)]["sku"] == "b"
    # 评论大者胜
    rows = [{"sku": "a", "mall": "m", "price": 1.0, "comment": 5},
            {"sku": "b", "mall": "m", "price": 1.0, "comment": 9}]
    assert rows[wr.pick_winner_index(rows)]["sku"] == "b"
    # longValue 低价胜（10.9→10, 9.9→9）
    rows = [{"sku": "a", "mall": "m", "price": 10.9, "comment": None},
            {"sku": "b", "mall": "m", "price": 9.9, "comment": None}]
    assert rows[wr.pick_winner_index(rows)]["sku"] == "b"
    # longValue 相同 → 首见保留（10.9 与 10.1 均截断为 10, 严格小于才替换）
    rows = [{"sku": "a", "mall": "m", "price": 10.9, "comment": None},
            {"sku": "b", "mall": "m", "price": 10.1, "comment": None}]
    assert rows[wr.pick_winner_index(rows)]["sku"] == "a"
    # 空组报错
    with pytest.raises(ValueError):
        wr.pick_winner_index([])


def test_classify_problem_row_mapping():
    assert R.classify_problem_row("02_5.3%存疑同品", "当前已剔除") == \
        (R.ORIGIN_NEG_02, R.VS_NEG_02)
    assert R.classify_problem_row("02_5.3%存疑同品", "当前保留且纳入价格指数")[0] == \
        R.ORIGIN_KEEP_CONFIRMED
    assert R.classify_problem_row("02_5.3%存疑同品", "当前保留但价格指数为空")[0] == \
        R.ORIGIN_KEEP_CONFIRMED
    assert R.classify_problem_row("03_非同品原因", "当前已剔除") == \
        (R.ORIGIN_NEG_03, R.VS_NEG_03)
    assert R.classify_problem_row("03_非同品原因", "当前保留且纳入价格指数")[0] == \
        R.ORIGIN_KEEP_CONFIRMED
    assert R.classify_problem_row("04_34952存疑价格", "当前已剔除") == \
        (R.ORIGIN_PRICE_DROP_04, None)            # 04 剔除不算"否"
    assert R.classify_problem_row("04_34952存疑价格", "当前保留且纳入价格指数")[0] == \
        R.ORIGIN_KEEP_CONFIRMED
    assert R.classify_problem_row("01_4449无数据", "仍无有效同款") == \
        (R.ORIGIN_IGNORE_01, None)


def test_classify_unknown_value_raises():
    with pytest.raises(R.UnknownRuleValueError):
        R.classify_problem_row("02_5.3%存疑同品", "怪异结果")
    with pytest.raises(R.UnknownRuleValueError):
        R.classify_problem_row("99_未知表", "当前已剔除")


def test_evaluate_step2_hits_order_and_reason():
    res = R.evaluate_step2_hits(True, True, False, False, False)
    assert res["any"] and res["first_rule"] == "R-a"
    assert [r for r, _ in res["hits"]] == ["R-a", "R-b"]
    res = R.evaluate_step2_hits(False, False, True, False, False, "占位价<=1元")
    assert res["first_reason"] == "HISTORICAL_占位价<=1元"
    res = R.evaluate_step2_hits(False, True, False, True, False)
    assert res["first_rule"] == "R-b" and res["first_reason"] == "HISTORICAL_偏离超80"
    assert [r for r, _ in res["hits"]] == ["R-b", "R-d"]   # 全部命中记 lineage
    res = R.evaluate_step2_hits(False, False, False, False, False)
    assert not res["any"] and res["first_rule"] is None
    with pytest.raises(R.UnknownRuleValueError):
        R.evaluate_step2_hits(False, False, True, False, False, "")


def test_verdict_rank_and_merge_priority():
    assert R.verdict_rank(R.VS_FINAL) == 3
    assert R.verdict_rank(R.VS_P25) == 2
    assert R.verdict_rank(R.VS_NEG_03) == 2
    assert R.verdict_rank(R.VS_NEG_02) == 2
    assert R.verdict_rank(R.VS_DIFY) == 2
    with pytest.raises(R.UnknownRuleValueError):
        R.verdict_rank("未知来源")
    # 主 Agent 裁定层级: FINAL > 03非同品 ≈ 02复核剔除 ≈ DIFY > P25 > S3
    assert R.merge_priority(R.ORIGIN_FINAL) > R.merge_priority(R.ORIGIN_NEG_03) \
        > R.merge_priority(R.ORIGIN_NEG_02) > R.merge_priority(R.ORIGIN_DIFY) \
        > R.merge_priority(R.ORIGIN_P25) > R.merge_priority(R.ORIGIN_S3)


# ---------------------------------------------------------------------------
# 端到端夹具（mini parquet）
# ---------------------------------------------------------------------------

def _w(tmp: str, name: str, cols: dict) -> str:
    path = os.path.join(tmp, name)
    pq.write_table(pa.table(cols), path)
    return path


@pytest.fixture()
def fixture_dir(tmp_path):
    tmp = str(tmp_path)
    temp = os.path.join(tmp, "temp")
    os.makedirs(temp, exist_ok=True)

    def t(name, cols):
        return _w(temp, name, cols)

    t("final_main.parquet", {
        "code": ["c1", "c2", "c3", "c4", "c5"],
        "name": ["N1", "N2", "N3", "N4", "N5"],
        "sku": ["s1", "s2", "", "s4", "s5"],
        "mall": ["m1", "m1", "", "m4", "m5"],
        "title": ["T_c1m1s1", "T_c2m1s2", "T_c3", "T_c4m4s4", "T_c5m5s5"],
        "url": ["U1", "U2", "U3", "U4", "U5"],
        "price_raw": ["10.0", "20.0", "5.0", "8.0", "12.0"],
        "index_val": ["10.00", "20.00", "", "8.00", "12.00"],
        "method": ["平均价计算指导价", "平均价计算指导价", "不计算",
                   "平均价计算指导价", "平均价计算指导价"],
        "price": [10.0, 20.0, 5.0, 8.0, 12.0],
        "src_row": [2, 3, 4, 5, 6],
    })
    t("p25.parquet", {
        "code": ["c1", "c1", "c1", "c6", "c9", "c4", "c7"],
        "sku": ["p1", "p2", "p3", "p6", "s9", "q1", "s7x"],
        "mall": ["m2", "m4", "m4", "m6", "m9", "m4", "m7x"],
        "title": ["T_p1", "T_p2", "T_p3", "T_p6", "T_s9", "T_q1", "T_s7x"],
        "url": ["UP1", "UP2", "UP3", "UP6", "US9", "UQ1", "U7x"],
        "price_buy": [11.0, 7.0, 9.0, 30.0, 3.0, 1.0, 2.0],
        "price_mkt": [None] * 7,
        "price_used": [11.0, 7.0, 9.0, 30.0, 3.0, 1.0, 2.0],
        "anomaly": ["正常", "正常", "正常", "异常类型2", "正常", "正常", "正常"],
        "src_row": [2, 3, 4, 5, 6, 7, 8],
    })
    t("s3flags.parquet", {
        "code": ["c1", "c1", "c4", "c7", "c8", "c1", "c4"],
        "sku": ["s3", "s3", "s4", "s7", "s8", "p1", "q1"],
        "mall": ["m3", "m3", "m4", "m7", "m8", "m2", "m4"],
        "title": ["T_s3", "T_s3", "T_c4m4s4", "T_s7", "T_s8", "T_p1", "T_q1"],
        "url": ["US3", "US3", "U4", "US7", "US8", "UP1", "UQ1"],
        "price": [9.0, 9.0, 8.0, 6.0, 1.0, 11.0, 1.0],
        "jz": ["否", "否", "是", "是", "否", "否", "否"],
        "ywdm": ["否", "否", "否", "否", "否", "否", "否"],
        "jd": ["否"] * 7, "attr": ["否"] * 7, "filtered": ["否"] * 7, "match": ["是"] * 7,
        "verdict2": ["是"] * 7,
        "comment": ["5", "", "", "2", "", "", "99"],
        "src_row": [2, 3, 4, 5, 6, 7, 8],
    })
    t("drops.parquet", {
        "code": ["c1", "c1", "c8"],
        "mall": ["m2", "m1", "m8"],
        "基准价格": [None, None, None],
        "价格偏离率": [None, None, None],
        "价格基准来源": ["", "", ""],
        "价格核查结论": ["价格离群", "价格离群", ""],
        "价格问题说明": ["", "", ""],
        "sku": ["", "", "s8"],
        "reason": ["", "", "占位价<=1元"],
        "source": ["DROP80", "DROP80", "DROPNEW"],
        "price": [11.0, 10.0, 1.0],
    })
    t("final_problems.parquet", {
        "sheet": ["02_5.3%存疑同品", "02_5.3%存疑同品", "02_5.3%存疑同品",
                  "03_非同品原因", "03_非同品原因",
                  "04_34952存疑价格", "04_34952存疑价格", "01_4449无数据"],
        "row_no": [2, 3, 4, 2, 3, 2, 3, 2],
        "标准商品编码": ["c5", "c6", "c1", "c7", "c7", "c9", "c9", "c1"],
        "标准商品名称": ["N5", "N6", "N1", "N7", "N7", "N9", "N9", "N1"],
        "本次处理结果": ["当前已剔除", "当前已剔除", "当前保留且纳入价格指数",
                       "当前已剔除", "当前保留且纳入价格指数",
                       "当前已剔除", "当前保留但价格指数为空", "已补到>=2商城并已计算指数"],
        "是否纳入价格指数": ["否", "否", "是", "否", "是", "否", "否", "是"],
        "上次SKUID": ["s5", "sx", "s1", "s7x", "s7", "s9", "s9", "s1"],
        "上次商城编码": ["m5", "mx", "m1", "m7x", "m7", "m9", "m9", "m1"],
        "上次互联网商品名称": ["T_c5m5s5", "T_sx", "T_c1m1s1", "T_s7x", "T_s7",
                           "T_s9", "T_s9", "T_c1m1s1"],
        "上次互联网链接": ["U5", "Ux", "U1", "U7x", "US7", "US9", "US9", "U1"],
        "上次互联网价格": ["12.0", "1.0", "10.0", "2.0", "6.0", "3.0", "3.0", "10.0"],
    })
    t("dify.parquet", {
        "code": ["c5", "c1", "c6", "c9"],
        "title1": ["T1", "T2", "T3", "T4"],
        "title2": ["T_c5m5s5", "no_such_title", "T_p6", "T_s9"],
        "result": ["不是", "不是", "不是", "是"],
        "uid": ["x1", "x2", "x3", "x4"],
    })
    t("comments_final.parquet", {
        "code": ["c1", "c2", "c3", "c4", "c5"],
        "sku": ["s1", "s2", "", "s4", "s5"],
        "mall": ["m1", "m1", "", "m4", "m5"],
        "state": ["POSITIVE", "ZERO", "NO_KEY", "POSITIVE", "EMPTY_SRC"],
        "comment_value": [3.0, 0.0, None, 10.0, None],
        "comment_src_layer": ["RECCSV", "CVDEL", "NONE", "RECCSV", "CVDEL"],
        "comment_src_file": ["f.csv", "g.csv", "", "f.csv", "g.csv"],
        "comment_src_row": ["9", "1", "", "8", "2"],
    })

    cfg = {
        "workdir": tmp,
        "temp_dir": temp,
        "algo_src": ALGO_SRC,
        "libs_dir": LIBS,
    }
    for k in ("final_authority", "final_authority_equiv", "final_authority_orig", "p25_pool",
              "s3_flags", "s3_raw", "drop80", "drop_new", "dify_recheck", "recall_csv",
              "comment_index_parquet", "delivery_coverage_parquet", "power_match_parquet",
              "delivery_0825_parquet", "zs_result"):
        cfg[k] = os.path.join(tmp, k + ".dummy")
    cfg_path = os.path.join(tmp, "config.local.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    return cfg_path


@pytest.fixture()
def run_all(fixture_dir, monkeypatch):
    """注入夹具期望值后端到端运行 --stage all, 返回夹具 tmp 根目录。

    夹具冻结值: FINAL=5 行; P25 独有键=7 (p1,p2,p3,p6,s9,q1,s7x);
    S3 独有键=3 (s3,s7,s8, 运行时按键去重推导)。
    负判覆盖池正向: 03负判 (c7,m7x,s7x) 压 P25; DIFY x3 (c6,m6,p6) 压 P25 →
    是 = 5 + 7 + 3 − 2 = 13; 否 = (c6,mx,sx)02 + (c7,m7x,s7x)03 + (c6,m6,p6)DIFY = 3。
    """
    monkeypatch.setattr(B, "EXPECTED_FINAL_ROWS", 5)
    monkeypatch.setattr(B, "EXPECTED_P25_UNIQUE", 7)
    monkeypatch.setattr(B, "EXPECTED_EMPTY_SKU_FINAL", 1)
    monkeypatch.setattr(B, "EXPECTED_S3_UNIQUE_ROWS_LABEL", 4)  # 夹具 s3 独有原始行数
    rc = B.main(["--config", fixture_dir, "--stage", "all"])
    assert rc == 0
    return os.path.dirname(fixture_dir)


def _con():
    import sys

    if LIBS not in sys.path:
        sys.path.insert(0, LIBS)
    import duckdb

    return duckdb.connect()


def _q(path: str) -> str:
    return path.replace("'", "''")


def test_step1_union_and_merge(run_all):
    tmp = run_all
    con = _con()
    s1 = con.execute(
        f"""SELECT code, mall, sku, same_verdict, verdict_source, record_origin, sources
            FROM read_parquet('{_q(os.path.join(tmp, "temp", "step1.parquet"))}')
            ORDER BY code, mall, sku"""
    ).fetchall()
    keys = {(r[0], r[1], r[2]) for r in s1}
    assert len(s1) == len(keys) == 16            # 13 是 + 3 否, 键唯一
    yes = [r for r in s1 if r[3] == "是"]
    no = [r for r in s1 if r[3] == "否"]
    assert len(yes) == 13 and len(no) == 3
    # 负例映射: 02/03/DIFY 均为 HISTORICAL 层明确负判
    neg = {(r[0], r[1], r[2]): r[4] for r in no}
    assert neg[("c6", "mx", "sx")] == R.VS_NEG_02
    assert neg[("c7", "m7x", "s7x")] == R.VS_NEG_03
    assert neg[("c6", "m6", "p6")] == R.VS_DIFY
    # FINAL 覆盖 dify 否 与 02 否
    row_c5 = next(r for r in s1 if (r[0], r[1], r[2]) == ("c5", "m5", "s5"))
    assert row_c5[3] == "是" and row_c5[4] == R.VS_FINAL
    assert "final_main.parquet#row=6" in row_c5[6]
    assert "dify.parquet#uid=x1" in row_c5[6]
    assert "final_problems.parquet#" in row_c5[6]
    # DIFY 再校验(0908) 压过 P25 池正向(0813): (c6,m6,p6) 判否, sources 并列两源
    row_p6 = next(r for r in s1 if (r[0], r[1], r[2]) == ("c6", "m6", "p6"))
    assert row_p6[3] == "否" and row_p6[4] == R.VS_DIFY
    assert "dify.parquet#uid=x3" in row_p6[6] and "p25.parquet#row=5" in row_p6[6]
    # 03 明确"非同品"负判 压过 P25 候选池正向, sources 并列两源
    row_03 = next(r for r in s1 if (r[0], r[1], r[2]) == ("c7", "m7x", "s7x"))
    assert row_03[3] == "否" and row_03[4] == R.VS_NEG_03
    assert "p25.parquet#row=8" in row_03[6]
    stats1 = json.load(open(os.path.join(tmp, "temp", "p2_stats_stage1.json"), encoding="utf-8"))
    assert stats1["funnel"]["step1"]["neg_takeover_by_origin"] == {
        "NEG_02_REEVAL": {"p25": 0, "s3": 0},
        "NEG_03_NONSAME": {"p25": 1, "s3": 0},
        "NEG_DIFY": {"p25": 1, "s3": 0},
    }
    assert stats1["funnel"]["step1"]["neg_took_p25_keys"] == 2
    assert stats1["funnel"]["step1"]["neg_took_s3_keys"] == 0
    con.close()


def test_step1_dify_ambiguous_not_entered(run_all):
    tmp = run_all
    con = _con()
    (amb,) = con.execute(
        f"""SELECT count(*) FROM read_parquet('{_q(os.path.join(tmp, "temp", "step1.parquet"))}')
            WHERE code='c1' AND mall='' AND sku='' AND same_verdict='否'"""
    ).fetchone()
    assert amb == 0                               # 未强行入表
    lin1 = os.path.join(tmp, "temp", "p2_lineage_stage1.parquet")
    rows = con.execute(
        f"""SELECT conflict_type, internal_reason, source_row
            FROM read_parquet('{_q(lin1)}') WHERE conflict_type='AMBIGUOUS_LINKAGE'"""
    ).fetchall()
    assert len(rows) == 1 and rows[0][2] == "x2"
    assert rows[0][1] == "AMBIGUOUS_LINKAGE_NO_MATCH"
    con.close()


def test_step2_rules_and_final_protection(run_all):
    tmp = run_all
    con = _con()
    s2 = con.execute(
        f"""SELECT code, mall, sku FROM read_parquet('{_q(os.path.join(tmp, "temp", "step2.parquet"))}')
            ORDER BY code, mall, sku"""
    ).fetchall()
    keys = {(r[0], r[1], r[2]) for r in s2}
    # 13 是 - 4 剔 = 9
    assert len(s2) == 9
    for gone in [("c1", "m2", "p1"),   # R-b DROP80 价格±0.01
                 ("c6", "m6", "p6"),   # Step1 已被 DIFY 负判判否, 不在是-集
                 ("c7", "m7", "s7"),   # R-a 极值标志
                 ("c8", "m8", "s8"),   # R-c DROPNEW 原因
                 ("c9", "m9", "s9")]:  # R-d 04表存疑价格
        assert gone not in keys, gone
    for final_key in [("c1", "m1", "s1"), ("c2", "m1", "s2"), ("c3", "", ""),
                      ("c4", "m4", "s4"), ("c5", "m5", "s5")]:
        assert final_key in keys
    assert ("c1", "m4", "p2") in keys and ("c1", "m4", "p3") in keys \
        and ("c1", "m3", "s3") in keys and ("c4", "m4", "q1") in keys
    # stats: 规则计数与 FINAL 保护
    stats = json.load(open(os.path.join(tmp, "temp", "p2_stats_stage2.json"), encoding="utf-8"))
    assert stats["conflicts"]["R-a_dropped"] == 1
    assert stats["conflicts"]["R-b_dropped"] == 1
    assert stats["conflicts"]["R-c_dropped"] == 1
    assert stats["conflicts"]["R-d_dropped"] == 1
    assert stats["conflicts"]["R-e_dropped"] == 0
    assert stats["conflicts"]["FINAL_ROW_RULE_CONFLICT"] == 2
    assert stats["funnel"]["step2"]["filtered_breakdown"]["negative_verdict_excluded"] == 3
    # lineage: 首个命中 + FINAL 冲突证据
    lin2 = os.path.join(tmp, "temp", "p2_lineage_stage2.parquet")
    drop_rows = con.execute(
        f"""SELECT sku_id, stage_drop_reason, internal_reason
            FROM read_parquet('{_q(lin2)}')
            WHERE next_stage_action='DROP' AND internal_reason='STEP2_RULE_DROP'
            ORDER BY sku_id"""
    ).fetchall()
    reasons = {r[0]: r[1] for r in drop_rows}
    assert reasons == {"p1": "HISTORICAL_偏离超80", "s7": "HISTORICAL_极值标志",
                       "s8": "HISTORICAL_占位价<=1元", "s9": "HISTORICAL_存疑价格剔除"}
    conflicts = con.execute(
        f"""SELECT sku_id, conflict_type, stage_drop_reason
            FROM read_parquet('{_q(lin2)}') WHERE conflict_type='FINAL_ROW_RULE_CONFLICT'
            ORDER BY sku_id"""
    ).fetchall()
    assert [(c[0], c[2]) for c in conflicts] == [("s1", "HISTORICAL_偏离超80"),
                                                 ("s4", "HISTORICAL_极值标志")]
    con.close()


def test_step3_group_dedup_and_winner_conflict(run_all):
    tmp = run_all
    con = _con()
    s3 = con.execute(
        f"""SELECT code, mall, sku FROM read_parquet('{_q(os.path.join(tmp, "temp", "step3.parquet"))}')
            ORDER BY code, mall, sku"""
    ).fetchall()
    keys = {(r[0], r[1], r[2]) for r in s3}
    assert len(s3) == 7 and len(keys) == 7
    # 组去重: (c1,m4) 只剩理论 winner p2 (longValue 7 < 9)
    assert ("c1", "m4", "p2") in keys and ("c1", "m4", "p3") not in keys
    # FINAL 覆盖组: (c4,m4) winner 仍是 FINAL 的 s4
    assert ("c4", "m4", "s4") in keys and ("c4", "m4", "q1") not in keys
    # (code,mall) ≤1
    dup = con.execute(
        f"""SELECT count(*) FROM (
              SELECT code, mall FROM read_parquet('{_q(os.path.join(tmp, "temp", "step3.parquet"))}')
              GROUP BY code, mall HAVING count(DISTINCT sku) > 1)"""
    ).fetchone()[0]
    assert dup == 0
    stats = json.load(open(os.path.join(tmp, "temp", "p2_stats_stage3.json"), encoding="utf-8"))
    assert stats["conflicts"]["WINNER_RULE_CONFLICT"] == 1
    assert stats["conflicts"]["YWDM_LOSER"] == 2
    assert stats["funnel"]["step3"]["groups"] == 7
    lin3 = os.path.join(tmp, "temp", "p2_lineage_stage3.parquet")
    conflict = con.execute(
        f"""SELECT sku_id, internal_reason, sources FROM read_parquet('{_q(lin3)}')
            WHERE conflict_type='WINNER_RULE_CONFLICT'"""
    ).fetchall()
    assert len(conflict) == 1 and conflict[0][0] == "s4"
    assert "theoretical_winner=sku:q1" in conflict[0][2]
    losers = con.execute(
        f"""SELECT sku_id FROM read_parquet('{_q(lin3)}')
            WHERE internal_reason='YWDM_LOSER' ORDER BY sku_id"""
    ).fetchall()
    assert [x[0] for x in losers] == ["p3", "q1"]
    con.close()


def test_step4_intersection_and_comments(run_all):
    tmp = run_all
    con = _con()
    s4 = con.execute(
        f"""SELECT code, mall, sku, comment_value, comment_state, audit_result, snapshot_source
            FROM read_parquet('{_q(os.path.join(tmp, "temp", "step4.parquet"))}')
            ORDER BY code, mall, sku"""
    ).fetchall()
    assert len(s4) == 5
    by_key = {(r[0], r[1], r[2]): r for r in s4}
    assert by_key[("c1", "m1", "s1")][3] == 3.0
    assert by_key[("c2", "m1", "s2")][3] == 0.0 and by_key[("c2", "m1", "s2")][4] == "ZERO"
    assert by_key[("c3", "", "")][3] is None and by_key[("c3", "", "")][4] == "NO_KEY"
    assert by_key[("c5", "m5", "s5")][3] is None and by_key[("c5", "m5", "s5")][4] == "EMPTY_SRC"
    for r in s4:
        assert r[5] == "通过" and r[6] == "RECONSTRUCTED_FINAL_INTERSECTION"
    # Step3 非 FINAL 幸存者被剔并记 lineage
    lin4 = os.path.join(tmp, "temp", "p2_lineage_stage4.parquet")
    dropped = con.execute(
        f"""SELECT sku_id FROM read_parquet('{_q(lin4)}')
            WHERE internal_reason='MANUAL_REVIEW_NOT_PASSED' ORDER BY sku_id"""
    ).fetchall()
    assert [x[0] for x in dropped] == ["p2", "s3"]
    stats = json.load(open(os.path.join(tmp, "temp", "p2_stats_stage4.json"), encoding="utf-8"))
    c4 = stats["comments"]["step4"]
    assert (c4["POSITIVE"], c4["ZERO"], c4["EMPTY_SRC"], c4["NO_KEY"], c4["NOT_FOUND"]) == \
        (2, 1, 1, 1, 0)
    con.close()


def test_step5_verbatim_and_keyset(run_all):
    tmp = run_all
    con = _con()
    s5 = con.execute(
        f"""SELECT code, mall, sku, price, price_raw, index_val, method
            FROM read_parquet('{_q(os.path.join(tmp, "temp", "step5.parquet"))}')
            ORDER BY code, mall, sku"""
    ).fetchall()
    assert len(s5) == 5
    fm = con.execute(
        f"""SELECT code, mall, sku, price, price_raw, index_val, method
            FROM read_parquet('{_q(os.path.join(tmp, "temp", "final_main.parquet"))}')
            ORDER BY code, mall, sku"""
    ).fetchall()
    assert s5 == fm                                # 值原样, 键集合双向一致
    stats = json.load(open(os.path.join(tmp, "reports", "stage_stats.json"), encoding="utf-8"))
    assert stats["asserts_all_pass"] is True
    assert stats["comments"]["step4"] == stats["comments"]["step5"]
    con.close()


def test_lineage_full_chain(run_all):
    tmp = run_all
    con = _con()
    lin = os.path.join(tmp, "lineage", "lineage.parquet")
    counts = dict(con.execute(
        f"""SELECT stage, count(*) FROM read_parquet('{_q(lin)}') GROUP BY 1 ORDER BY 1"""
    ).fetchall())
    assert counts == {1: 17, 2: 18, 3: 9, 4: 7, 5: 5}
    # 每行含 TASK 要求字段非空
    bad = con.execute(
        f"""SELECT count(*) FROM read_parquet('{_q(lin)}')
            WHERE stage IS NULL OR standard_code IS NULL OR next_stage_action IS NULL
               OR record_origin IS NULL OR verdict_source IS NULL"""
    ).fetchone()[0]
    assert bad == 0
    con.close()
