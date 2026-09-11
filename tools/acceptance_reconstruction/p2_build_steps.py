# -*- coding: utf-8 -*-
"""p2_build_steps.py — F2 重建层主脚本（验收回溯重建）。

规格: reports/SPEC_ACCEPTANCE_RECONSTRUCTION.md §4/§5/§7/§14.1/§28 D（冻结版契约）。

用法:
    python.exe -X utf8 p2_build_steps.py --config <config.local.json> [--stage all|1|2|3|4|5]

输入（F1 产物, 只读）: temp\\{final_main,final_problems,p25,s3flags,drops,dify,comments_final}.parquet
输出: temp\\step{1..5}.parquet + temp\\p2_lineage_stage{n}.parquet + temp\\p2_stats_stage{n}.json
     最终: lineage\\lineage.parquet + reports\\stage_stats.json（stage5 完成后合并）

硬红线: 输入只读; 只写 workdir; duckdb 只读查询 + 向 temp\\ 写 parquet;
       temp 总量 >3GB → DISK_SAFETY_STOP(exit 3); 断言失败 → 打印样本并 exit 2;
       未在 SPEC/F1事实/ALGO包 中定义的取值 → 打印并停止（不造数据）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import p2_rules as R  # noqa: E402
from p1_common import (  # noqa: E402
    connect_duckdb,
    ensure_libs,
    fmt_bytes,
    guard_disk_limit,
    load_config,
)

# ---------------------------------------------------------------------------
# 冻结事实（F1 实测, inventory/p1_stats.json; 用于断言）
# ---------------------------------------------------------------------------
EXPECTED_FINAL_ROWS = 418291
EXPECTED_P25_UNIQUE = 88712          # p25 distinct 键 ∉ FINAL（键口径, F1 已按 DISTINCT 计算）
# 注意: F1 的 s3 "unique_keys_vs_final_p25=62141" 实为原始行口径（sr 未 DISTINCT）。
# 按 TASK"注意按键去重"要求, S3 独有键数以运行时按键去重推导为准（真实数据=37,967）。
EXPECTED_S3_UNIQUE_ROWS_LABEL = 62141
EXPECTED_EMPTY_SKU_FINAL = 359

BASE_COLS = ("code, name, sku, mall, title, url, price, price_raw, index_val, method, "
             "is_final, same_verdict, verdict_source, record_origin, "
             "src_file, src_sheet, src_row, sources")

STAGE_NAMES = {1: "step1", 2: "step2", 3: "step3", 4: "step4", 5: "step5"}


def _t0() -> float:
    return time.time()


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _q(path: str) -> str:
    return path.replace("'", "''")


def _t(cfg: dict, name: str) -> str:
    return os.path.join(cfg["temp_dir"], name)


def _wd(cfg: dict, *parts: str) -> str:
    return os.path.join(cfg["workdir"], *parts)


def _require(path: str, what: str) -> None:
    if not os.path.isfile(path):
        print(f"MISSING_INPUT: {what} 不存在: {path}（先运行其前置阶段）", flush=True)
        sys.exit(2)


def _reg(con, name: str, cols: dict, types: dict) -> None:
    """把 Python 列数据注册为 duckdb 表（pyarrow）。"""
    import pyarrow as pa

    alias = {"str": pa.string(), "bool": pa.bool_(), "f64": pa.float64(), "i64": pa.int64()}
    con.register(name, pa.table({c: pa.array(cols[c], type=alias[t]) for c, t in types.items()}))


def _assert(con, asserts: list, name: str, actual, expected, *,
            hard: bool = True, sample_sql: str | None = None) -> bool:
    ok = actual == expected
    asserts.append({"name": name, "actual": actual, "expected": expected, "pass": ok})
    _log(f"  ASSERT {'PASS' if ok else 'FAIL'}: {name} actual={actual} expected={expected}")
    if not ok:
        if sample_sql:
            try:
                for row in con.execute(sample_sql).fetchall()[:20]:
                    print(f"    样本: {row}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"    样本查询失败: {e}", flush=True)
        if hard:
            print(f"ASSERT_HARD_FAIL: {name} → exit 2", flush=True)
            sys.exit(2)
    return ok


def _funnel(con, table: str) -> dict:
    """标品数/商城数/唯一SKU数（商城与SKU按非空口径, 口径写入 stats meta）。"""
    rows, codes, malls, skus = con.execute(
        f"""SELECT count(*), count(DISTINCT code),
                   count(DISTINCT CASE WHEN mall<>'' THEN mall END),
                   count(DISTINCT CASE WHEN sku<>'' THEN sku END)
            FROM {table}"""
    ).fetchone()
    return {"rows": rows, "codes": codes, "malls": malls, "unique_skus": skus}


def _comment_source_sql(alias: str = "s") -> str:
    return (f"CASE WHEN {alias}.record_origin='S3_UNIQUE' THEN 's3flags.parquet' "
            f"WHEN {alias}.is_final THEN COALESCE(cm.comment_src_layer, '') ELSE '' END")


def _snap_sql(alias: str = "s") -> str:
    return (f"CASE WHEN {alias}.is_final THEN 'RECONSTRUCTED_FINAL_INTERSECTION' ELSE '' END")


# ---------------------------------------------------------------------------
# 公共输入表
# ---------------------------------------------------------------------------

def _table_fm(con, cfg: dict) -> str:
    fm = _t(cfg, "final_main.parquet")
    _require(fm, "F1 final_main.parquet")
    con.execute(
        f"""CREATE OR REPLACE TABLE fm AS
            SELECT code, name, sku, mall, title, url, price, price_raw, index_val, method,
                   CAST(src_row AS VARCHAR) AS src_row
            FROM read_parquet('{_q(fm)}')"""
    )
    return "fm"


def _table_cm(con, cfg: dict) -> str:
    cm = _t(cfg, "comments_final.parquet")
    _require(cm, "F1 comments_final.parquet")
    con.execute(
        f"""CREATE OR REPLACE TABLE cm AS
            SELECT code, sku, mall, state, comment_value, comment_src_layer,
                   comment_src_file, comment_src_row
            FROM read_parquet('{_q(cm)}')"""
    )
    return "cm"


def _table_s3(con, cfg: dict) -> None:
    """s3flags 按键去重: 标志取 bool_or(任一行=是); 展示字段取"非空评论优先, 再按行号"的行。"""
    s3 = _t(cfg, "s3flags.parquet")
    _require(s3, "F1 s3flags.parquet")
    con.execute(
        f"""CREATE OR REPLACE TABLE s3_all AS
            SELECT code, sku, mall, title, url, price, comment,
                   CAST(src_row AS VARCHAR) AS src_row,
                   COALESCE(jz,'')='是' AS jz_yes, COALESCE(ywdm,'')='是' AS ywdm_yes
            FROM read_parquet('{_q(s3)}')"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE s3_flags AS
           SELECT code, mall, sku, bool_or(jz_yes) AS jz_yes, bool_or(ywdm_yes) AS ywdm_yes
           FROM s3_all GROUP BY code, mall, sku"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE s3_disp AS
           SELECT code, mall, sku, title, url, price, comment, src_row FROM (
               SELECT code, mall, sku, title, url, price, comment, src_row,
                      row_number() OVER (PARTITION BY code, mall, sku
                                         ORDER BY (COALESCE(comment,'')<>'') DESC,
                                                  TRY_CAST(src_row AS BIGINT) ASC) AS rn
               FROM s3_all)
           WHERE rn = 1"""
    )


def _table_code_name(con) -> None:
    con.execute(
        """CREATE OR REPLACE TABLE code_name AS
           SELECT code, min(name) AS name, count(DISTINCT name) AS name_n
           FROM fm GROUP BY code"""
    )


# ---------------------------------------------------------------------------
# Step1 同款校验结果
# ---------------------------------------------------------------------------

def build_step1(con, cfg: dict, WR) -> dict:
    t0 = _t0()
    asserts: list = []
    _table_fm(con, cfg)
    _table_cm(con, cfg)
    _table_s3(con, cfg)
    _table_code_name(con)
    n_name_dup = con.execute(
        "SELECT count(*) FROM code_name WHERE name_n > 1").fetchone()[0]
    if n_name_dup:
        _log(f"注意: {n_name_dup} 个 code 对应多个标准商品名称（Step1 取 min(name), 计数上报）")

    # ---- P25（键唯一性断言, F1: 464,075 行 = 464,075 键）----
    p25 = _t(cfg, "p25.parquet")
    _require(p25, "F1 p25.parquet")
    con.execute(
        f"""CREATE OR REPLACE TABLE p25 AS
            SELECT code, sku, mall, title, url, price_used, anomaly, CAST(src_row AS VARCHAR) AS src_row
            FROM read_parquet('{_q(p25)}')"""
    )
    n_p25, k_p25 = con.execute(
        "SELECT count(*), count(DISTINCT code||'|'||mall||'|'||sku) FROM p25").fetchone()
    _assert(con, asserts, "p25 键唯一(行数=键数)", n_p25, k_p25)

    # S3 独有键数（按键去重口径, 运行时推导）与 F1 原始行口径（对照记录）
    s3u_keys_derived = con.execute(
        """SELECT count(*) FROM s3_disp s
           WHERE NOT EXISTS (SELECT 1 FROM fm f
                             WHERE f.code=s.code AND f.mall=s.mall AND f.sku=s.sku)
             AND NOT EXISTS (SELECT 1 FROM p25 p
                             WHERE p.code=s.code AND p.mall=s.mall AND p.sku=s.sku)"""
    ).fetchone()[0]
    s3u_rows_f1_scope = con.execute(
        """SELECT count(*) FROM s3_all s
           WHERE NOT EXISTS (SELECT 1 FROM fm f
                             WHERE f.code=s.code AND f.mall=s.mall AND f.sku=s.sku)
             AND NOT EXISTS (SELECT 1 FROM p25 p
                             WHERE p.code=s.code AND p.mall=s.mall AND p.sku=s.sku)"""
    ).fetchone()[0]
    _log(f"S3 独有: 按键去重 {s3u_keys_derived} 键 (F1 原始行口径 {s3u_rows_f1_scope}, "
         f"冻结标签 {EXPECTED_S3_UNIQUE_ROWS_LABEL})")
    _assert(con, asserts, "S3 独有原始行数 = F1 冻结标签", s3u_rows_f1_scope,
            EXPECTED_S3_UNIQUE_ROWS_LABEL, hard=False)

    # ---- final_problems 判定列映射（Python 纯函数决定, 未知值 → 停止）----
    prob = _t(cfg, "final_problems.parquet")
    _require(prob, "F1 final_problems.parquet")
    con.execute(
        f"""CREATE OR REPLACE TABLE prob AS
            SELECT sheet, row_no,
                   "标准商品编码" AS code, "标准商品名称" AS name,
                   "本次处理结果" AS cur_result,
                   "上次SKUID" AS last_sku, "上次商城编码" AS last_mall,
                   "上次互联网商品名称" AS last_title, "上次互联网链接" AS last_url,
                   TRY_CAST(NULLIF(replace(trim(COALESCE("上次互联网价格",'')), ',', ''), '') AS DOUBLE) AS last_price
            FROM read_parquet('{_q(prob)}')"""
    )
    pairs = con.execute("SELECT DISTINCT sheet, cur_result FROM prob ORDER BY 1, 2").fetchall()
    cls_rows, unknown = [], []
    for sheet, res in pairs:
        try:
            origin, vs = R.classify_problem_row(sheet, res)
            # 仅进入 Step1 并集的 origin 参与键冲突合并优先级; 其余 prio=0（不落 s1_parts）
            prio = R.merge_priority(origin) if origin in R.MERGE_PRIO else 0
            cls_rows.append((sheet, res, origin, vs, prio))
        except R.UnknownRuleValueError as e:
            unknown.append(str(e))
    if unknown:
        print("UNKNOWN_RULE_VALUE_STOP: 问题表判定列出现未定义取值, 不造规则, 等待澄清:", flush=True)
        for u in unknown:
            print(f"  - {u}", flush=True)
        sys.exit(2)
    con.execute("CREATE OR REPLACE TABLE classes(sheet VARCHAR, cur_result VARCHAR, "
                "origin VARCHAR, verdict_source VARCHAR, prio INTEGER)")
    con.executemany("INSERT INTO classes VALUES (?, ?, ?, ?, ?)", cls_rows)
    _log(f"问题表判定列映射: {len(pairs)} 个 (sheet, 本次处理结果) 组合全部可归类")
    for sheet, res, origin, _vs, _p in cls_rows:
        _log(f"  {sheet} | {res!r} → {origin}")

    con.execute(
        """CREATE OR REPLACE TABLE prob_cls AS
           SELECT p.*, c.origin, c.verdict_source, c.prio
           FROM prob p JOIN classes c ON c.sheet = p.sheet AND c.cur_result = p.cur_result"""
    )

    # ---- DIFY 负例定位（title2 精确匹配同 code 的 FINAL/P25/S3 互联网标题）----
    dify = _t(cfg, "dify.parquet")
    _require(dify, "F1 dify.parquet")
    con.execute(
        f"""CREATE OR REPLACE TABLE dify AS
            SELECT code, title1, title2, result, uid FROM read_parquet('{_q(dify)}')"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE cand AS
           SELECT code, title, mall, sku, code||'|'||mall||'|'||sku AS k, 'FINAL' AS s
             FROM fm WHERE title<>''
           UNION ALL
           SELECT code, title, mall, sku, code||'|'||mall||'|'||sku, 'P25'
             FROM p25 WHERE title<>''
           UNION ALL
           SELECT code, title, mall, sku, code||'|'||mall||'|'||sku, 'S3'
             FROM s3_disp WHERE title<>''"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE dify_neg AS
           SELECT uid, code, title2 FROM dify WHERE result='不是' AND code<>''"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE dify_loc AS
           SELECT n.uid, n.code, n.title2,
                  count(DISTINCT c.k) AS n_keys,
                  min(c.mall) AS mall, min(c.sku) AS sku,
                  COALESCE(string_agg(DISTINCT c.s, ','), '') AS srcs
           FROM dify_neg n LEFT JOIN cand c
             ON c.code = n.code AND c.title = n.title2
           GROUP BY n.uid, n.code, n.title2"""
    )
    located, amb_no, amb_multi = con.execute(
        """SELECT COALESCE(sum(CASE WHEN n_keys=1 THEN 1 ELSE 0 END), 0),
                  COALESCE(sum(CASE WHEN n_keys=0 THEN 1 ELSE 0 END), 0),
                  COALESCE(sum(CASE WHEN n_keys>1 THEN 1 ELSE 0 END), 0)
           FROM dify_loc""").fetchone()
    dify_stats = {
        "neg_total": con.execute("SELECT count(*) FROM dify_neg").fetchone()[0],
        "located": located,
        "ambiguous_no_match": amb_no,
        "ambiguous_multi_match": amb_multi,
        "positive_ignored": con.execute(
            "SELECT count(*) FROM dify WHERE result='是'").fetchone()[0],
    }
    _log(f"dify 负例定位: 定位 {located} / 无匹配 {amb_no} / 多匹配 {amb_multi} "
         f"(正例忽略 {dify_stats['positive_ignored']})")

    # ---- Step1 候选并集 ----
    con.execute(
        """CREATE OR REPLACE TABLE s1_parts(
              code VARCHAR, name VARCHAR, sku VARCHAR, mall VARCHAR, title VARCHAR, url VARCHAR,
              price DOUBLE, price_raw VARCHAR, index_val VARCHAR, method VARCHAR,
              same_verdict VARCHAR, verdict_source VARCHAR, record_origin VARCHAR,
              src_file VARCHAR, src_sheet VARCHAR, src_row VARCHAR,
              prio INTEGER, source_ref VARCHAR, is_final BOOLEAN)"""
    )
    # FINAL 全部行（同款=是）
    con.execute(
        f"""INSERT INTO s1_parts
            SELECT code, name, sku, mall, title, url, price, price_raw, index_val, method,
                   '是', '{R.VS_FINAL}', '{R.ORIGIN_FINAL}',
                   'final_main.parquet', '最终处理结果', src_row,
                   {R.MERGE_PRIO[R.ORIGIN_FINAL]}, 'final_main.parquet#row=' || src_row, TRUE
            FROM fm"""
    )
    # P25 独有键（同款=是, 平台匹配; 标准名称从 final_main 按 code 带出）
    con.execute(
        f"""INSERT INTO s1_parts
            SELECT p.code, COALESCE(cn.name, ''), p.sku, p.mall, p.title, p.url, p.price_used,
                   '', '', '', '是', '{R.VS_P25}', '{R.ORIGIN_P25}',
                   'p25.parquet', '合并数据', p.src_row,
                   {R.MERGE_PRIO[R.ORIGIN_P25]}, 'p25.parquet#row=' || p.src_row, FALSE
            FROM p25 p LEFT JOIN code_name cn ON cn.code = p.code
            WHERE NOT EXISTS (SELECT 1 FROM fm f
                              WHERE f.code=p.code AND f.mall=p.mall AND f.sku=p.sku)"""
    )
    # S3 独有键（对 FINAL∪P25; 同款=是, 第二轮模型; 评论=S3.comment 记 lineage）
    con.execute(
        f"""INSERT INTO s1_parts
            SELECT s.code, COALESCE(cn.name, ''), s.sku, s.mall, s.title, s.url, s.price,
                   '', '', '', '是', '{R.VS_S3}', '{R.ORIGIN_S3}',
                   's3flags.parquet', 'Sheet1', s.src_row,
                   {R.MERGE_PRIO[R.ORIGIN_S3]}, 's3flags.parquet#row=' || s.src_row, FALSE
            FROM s3_disp s LEFT JOIN code_name cn ON cn.code = s.code
            WHERE NOT EXISTS (SELECT 1 FROM fm f
                              WHERE f.code=s.code AND f.mall=s.mall AND f.sku=s.sku)
              AND NOT EXISTS (SELECT 1 FROM p25 p
                              WHERE p.code=s.code AND p.mall=s.mall AND p.sku=s.sku)"""
    )
    # 02/03 负例候选（候选键=code+上次商城+上次SKUID; 上次字段非空才入表, 否则计数上报）
    n_neg_invalid = con.execute(
        f"""SELECT count(*) FROM prob_cls
            WHERE origin IN ('{R.ORIGIN_NEG_02}', '{R.ORIGIN_NEG_03}')
              AND (COALESCE(last_sku,'')='' OR COALESCE(last_mall,'')='')"""
    ).fetchone()[0]
    con.execute(
        f"""INSERT INTO s1_parts
            SELECT p.code, p.name, p.last_sku, p.last_mall, COALESCE(p.last_title,''),
                   COALESCE(p.last_url,''), p.last_price, '', '', '',
                   '否', p.verdict_source, p.origin,
                   'final_problems.parquet', p.sheet, CAST(p.row_no AS VARCHAR),
                   p.prio, 'final_problems.parquet#' || p.sheet || '#row=' || CAST(p.row_no AS VARCHAR),
                   FALSE
            FROM prob_cls p
            WHERE p.origin IN ('{R.ORIGIN_NEG_02}', '{R.ORIGIN_NEG_03}')
              AND COALESCE(p.last_sku,'')<>'' AND COALESCE(p.last_mall,'')<>''"""
    )
    # DIFY 定位到的负例（键=定位候选; 优先级最低档, 仅并 sources/计数）
    con.execute(
        f"""INSERT INTO s1_parts
            SELECT l.code, '', l.sku, l.mall, l.title2, '', NULL, '', '', '',
                   '否', '{R.VS_DIFY}', '{R.ORIGIN_DIFY}',
                   'dify.parquet', 'Sheet0', l.uid,
                   {R.MERGE_PRIO[R.ORIGIN_DIFY]}, 'dify.parquet#uid=' || l.uid, FALSE
            FROM dify_loc l WHERE l.n_keys = 1"""
    )

    # ---- 按 ckey 合并（优先级 + source_ref 字典序平局, 确定性）----
    con.execute(
        """CREATE OR REPLACE TABLE s1 AS
           SELECT code, name, sku, mall, title, url, price, price_raw, index_val, method,
                  same_verdict, verdict_source, record_origin,
                  src_file, src_sheet, src_row, is_final, sources
           FROM (
               SELECT p.*,
                      row_number() OVER (PARTITION BY code, mall, sku
                                         ORDER BY prio DESC, source_ref ASC) AS rn,
                      string_agg(source_ref, '|' ORDER BY prio DESC, source_ref ASC)
                          OVER (PARTITION BY code, mall, sku) AS sources
               FROM s1_parts p)
           WHERE rn = 1"""
    )

    # ---- 统计与断言 ----
    total, yes, no_ = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE same_verdict='是'), "
        "count(*) FILTER (WHERE same_verdict='否') FROM s1").fetchone()
    n_final = con.execute("SELECT count(*) FROM s1 WHERE is_final").fetchone()[0]
    n_rows, n_keys = con.execute(
        "SELECT count(*), count(DISTINCT code||'|'||mall||'|'||sku) FROM s1").fetchone()
    p25_parts = con.execute(
        "SELECT count(DISTINCT code||'|'||mall||'|'||sku) FROM s1_parts "
        "WHERE record_origin='P25_UNIQUE'").fetchone()[0]
    s3_parts = con.execute(
        "SELECT count(DISTINCT code||'|'||mall||'|'||sku) FROM s1_parts "
        "WHERE record_origin='S3_UNIQUE'").fetchone()[0]

    # ---- 冲突统计（主 Agent 20260911 裁定口径）----
    # 1) 负判(02/03/DIFY, 判定时间晚于池快照)覆盖 P25/S3 池正向键的键数, 按负判 origin 细分
    neg_origin_list = ",".join(f"'{o}'" for o in R.NEG_ORIGINS)
    takeover_rows = con.execute(
        f"""SELECT s.record_origin,
                   count(DISTINCT CASE WHEN q25.code IS NOT NULL
                                       THEN s.code||'|'||s.mall||'|'||s.sku END) AS took_p25,
                   count(DISTINCT CASE WHEN qs3.code IS NOT NULL
                                       THEN s.code||'|'||s.mall||'|'||s.sku END) AS took_s3
            FROM s1 s
            LEFT JOIN (SELECT code, mall, sku FROM s1_parts WHERE record_origin='P25_UNIQUE') q25
              ON q25.code=s.code AND q25.mall=s.mall AND q25.sku=s.sku
            LEFT JOIN (SELECT code, mall, sku FROM s1_parts WHERE record_origin='S3_UNIQUE') qs3
              ON qs3.code=s.code AND qs3.mall=s.mall AND qs3.sku=s.sku
            WHERE s.record_origin IN ({neg_origin_list})
            GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    neg_takeover_by_origin = {o: {"p25": p, "s3": s} for o, p, s in takeover_rows}
    neg_took_p25 = sum(v["p25"] for v in neg_takeover_by_origin.values())
    neg_took_s3 = sum(v["s3"] for v in neg_takeover_by_origin.values())
    # 2) 被FINAL压制的负判（键级; 02/03候选已在F1侧验证 0 个在 FINAL, DIFY 定位键可能撞 FINAL）
    neg_lost_final = con.execute(
        """SELECT count(*) FROM (
               SELECT DISTINCT p.code, p.mall, p.sku
               FROM s1_parts p
               WHERE p.same_verdict='否'
                 AND EXISTS (SELECT 1 FROM s1_parts q WHERE q.code=p.code AND q.mall=p.mall
                             AND q.sku=p.sku AND q.is_final))"""
    ).fetchone()[0]
    if neg_lost_final:
        print(f"NEG_LOST_TO_FINAL 键数={neg_lost_final}, 样本(≤10):", flush=True)
        for r in con.execute(
            """SELECT p.code, p.mall, p.sku, min(p.record_origin) AS neg_origin
               FROM s1_parts p
               WHERE p.same_verdict='否'
                 AND EXISTS (SELECT 1 FROM s1_parts q WHERE q.code=p.code AND q.mall=p.mall
                             AND q.sku=p.sku AND q.is_final)
               GROUP BY p.code, p.mall, p.sku LIMIT 10"""
        ).fetchall():
            print(f"    {r}", flush=True)
    # 3) 同层同键重复负判 parts 去重计数（仅并 sources, 不算压制）
    neg_parts_total, neg_parts_keys = con.execute(
        f"""SELECT count(*), count(DISTINCT code||'|'||mall||'|'||sku)
            FROM s1_parts WHERE same_verdict='否'"""
    ).fetchone()
    neg_dup_parts = neg_parts_total - neg_parts_keys
    # 4) 多源合并行数
    multi_source_rows = con.execute(
        "SELECT count(*) FROM s1 WHERE sources LIKE '%|%'").fetchone()[0]

    _assert(con, asserts, "step1 键唯一(行数=键数)", n_rows, n_keys)
    _assert(con, asserts, "step1 FINAL 行数", n_final, EXPECTED_FINAL_ROWS)
    _assert(con, asserts, "step1 P25 独有键部件数 = F1 冻结值", p25_parts, EXPECTED_P25_UNIQUE)
    _assert(con, asserts, "step1 S3 独有键部件数 = 运行时推导值", s3_parts, s3u_keys_derived)
    _assert(con, asserts, "step1 P25独有键数(合并后 = 冻结值 − 负判覆盖)",
            p25_keys_merged := con.execute(
                "SELECT count(DISTINCT code||'|'||mall||'|'||sku) FROM s1 "
                "WHERE record_origin='P25_UNIQUE'").fetchone()[0],
            EXPECTED_P25_UNIQUE - neg_took_p25)
    _assert(con, asserts, "step1 S3独有键数(合并后 = 推导值 − 负判覆盖)",
            s3_keys_merged := con.execute(
                "SELECT count(DISTINCT code||'|'||mall||'|'||sku) FROM s1 "
                "WHERE record_origin='S3_UNIQUE'").fetchone()[0],
            s3u_keys_derived - neg_took_s3)
    _assert(con, asserts, "step1 是-行数 = FINAL + P25独有 + S3独有(去重) − 负判覆盖",
            yes,
            EXPECTED_FINAL_ROWS + EXPECTED_P25_UNIQUE + s3u_keys_derived
            - neg_took_p25 - neg_took_s3)
    _assert(con, asserts, "step1 总行数 = 是+否", total, yes + no_)

    vs_counts = dict(con.execute(
        "SELECT verdict_source, count(*) FROM s1 GROUP BY 1 ORDER BY 2 DESC").fetchall())
    ro_counts = dict(con.execute(
        "SELECT record_origin, count(*) FROM s1 GROUP BY 1 ORDER BY 2 DESC").fetchall())

    funnel = _funnel(con, "s1")
    funnel.update({
        "rows_in_prev": None, "filtered": None, "rows_out": total,
        "yes": yes, "no": no_,
        "verdict_source_counts": vs_counts,
        "record_origin_counts": ro_counts,
        "negative_candidates_invalid_skipped": n_neg_invalid,
        "codes_with_multiple_names": n_name_dup,
        "s3_unique_keys_dedup": s3u_keys_derived,
        "s3_unique_rows_f1_label": EXPECTED_S3_UNIQUE_ROWS_LABEL,
        "s3_unique_rows_f1_scope": s3u_rows_f1_scope,
        "neg_takeover_by_origin": neg_takeover_by_origin,
        "neg_took_p25_keys": neg_took_p25,
        "neg_took_s3_keys": neg_took_s3,
        "neg_lost_to_final_keys": neg_lost_final,
        "neg_duplicate_parts_merged": neg_dup_parts,
        "dify": dify_stats,
        "multi_source_rows": multi_source_rows,
    })
    conflicts = {
        "AMBIGUOUS_LINKAGE": amb_no + amb_multi,
        "AMBIGUOUS_LINKAGE_NO_MATCH": amb_no,
        "AMBIGUOUS_LINKAGE_MULTI_MATCH": amb_multi,
        "NEGATIVE_CANDIDATE_LOST_MERGE": neg_lost_final,
    }
    _log(f"step1: 总 {total} = 是 {yes} + 否 {no_}; 标品 {funnel['codes']}, "
         f"商城(非空) {funnel['malls']}, 唯一SKU(非空) {funnel['unique_skus']}")
    _log(f"step1 verdict_source: {vs_counts}")
    _log(f"step1 record_origin: {ro_counts}")
    _log(f"step1 合并: 多源行 {multi_source_rows}; 负判覆盖池正向(按origin) {neg_takeover_by_origin} "
         f"(P25共 {neg_took_p25}, S3共 {neg_took_s3}); 被FINAL压制的负判键 {neg_lost_final}; "
         f"同层重复负判parts去重 {neg_dup_parts}")

    # ---- 写 step1.parquet + lineage stage1 ----
    step1 = _t(cfg, "step1.parquet")
    con.execute(
        f"""COPY (SELECT {BASE_COLS} FROM s1 ORDER BY code, mall, sku)
            TO '{_q(step1)}' (FORMAT PARQUET)"""
    )
    con.execute(
        f"""COPY (
            SELECT 1 AS stage, s.code AS standard_code, s.mall AS shop_code, s.sku AS sku_id,
                   s.url AS url, s.src_file AS source_file, s.src_sheet AS source_sheet,
                   s.src_row AS source_row, s.record_origin AS record_origin,
                   s.verdict_source AS verdict_source, {_snap_sql('s')} AS snapshot_source,
                   'KEEP' AS next_stage_action, '' AS internal_reason,
                   {_comment_source_sql('s')} AS comment_source,
                   '' AS stage_drop_reason, '' AS conflict_type, s.sources AS sources
            FROM s1 s LEFT JOIN cm ON cm.code=s.code AND cm.mall=s.mall AND cm.sku=s.sku
            UNION ALL
            SELECT 1, l.code, COALESCE(l.mall,''), COALESCE(l.sku,''), '', 'dify.parquet',
                   'Sheet0', l.uid, '{R.ORIGIN_DIFY}', '{R.VS_DIFY}', '', 'NOT_ENTERED',
                   'AMBIGUOUS_LINKAGE_' || CASE WHEN l.n_keys=0 THEN 'NO_MATCH' ELSE 'MULTI_MATCH' END,
                   '', '', 'AMBIGUOUS_LINKAGE',
                   'dify.parquet#uid=' || l.uid ||
                        CASE WHEN l.srcs<>'' THEN '|located_candidates_in=' || l.srcs ELSE '' END
            FROM dify_loc l WHERE l.n_keys <> 1
        ) TO '{_q(_t(cfg, "p2_lineage_stage1.parquet"))}' (FORMAT PARQUET)"""
    )

    stats = {
        "funnel": {"step1": funnel},
        "conflicts": conflicts,
        "asserts": asserts,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    _write_stage_stats(cfg, 1, stats)
    guard_disk_limit(cfg["temp_dir"])
    _log(f"step1 done → {step1} ({fmt_bytes(os.path.getsize(step1))}), 耗时 {stats['elapsed_sec']}s")
    return stats


# ---------------------------------------------------------------------------
# Step2 去极值后结果
# ---------------------------------------------------------------------------

def build_step2(con, cfg: dict, WR) -> dict:
    t0 = _t0()
    asserts: list = []
    step1 = _t(cfg, "step1.parquet")
    _require(step1, "step1.parquet")
    _table_cm(con, cfg)
    _table_s3(con, cfg)
    drops = _t(cfg, "drops.parquet")
    _require(drops, "F1 drops.parquet")
    p25 = _t(cfg, "p25.parquet")
    _require(p25, "F1 p25.parquet")
    prob = _t(cfg, "final_problems.parquet")
    _require(prob, "F1 final_problems.parquet")

    con.execute(f"CREATE OR REPLACE TABLE s1t AS SELECT * FROM read_parquet('{_q(step1)}')")
    con.execute(
        """CREATE OR REPLACE TABLE yes AS
           SELECT * FROM s1t WHERE same_verdict='是'"""
    )
    yes_count = con.execute("SELECT count(*) FROM yes").fetchone()[0]

    # 问题表判定（复用纯函数; Step2 只需 04 剔除候选）
    con.execute(
        f"""CREATE OR REPLACE TABLE prob2 AS
            SELECT sheet, row_no, "标准商品编码" AS code,
                   "上次SKUID" AS last_sku, "上次商城编码" AS last_mall,
                   "本次处理结果" AS cur_result
            FROM read_parquet('{_q(prob)}')"""
    )
    pairs = con.execute("SELECT DISTINCT sheet, cur_result FROM prob2 ORDER BY 1, 2").fetchall()
    cls_rows = []
    for sheet, res in pairs:
        origin, _vs = R.classify_problem_row(sheet, res)
        cls_rows.append((sheet, res, origin))
    con.execute("CREATE OR REPLACE TABLE classes2(sheet VARCHAR, cur_result VARCHAR, origin VARCHAR)")
    con.executemany("INSERT INTO classes2 VALUES (?, ?, ?)", cls_rows)

    # R-b: DROP80(code+mall+price±0.01)
    con.execute(
        f"""CREATE OR REPLACE TABLE rb80 AS
            SELECT DISTINCT code, mall, price FROM read_parquet('{_q(drops)}')
            WHERE source='DROP80' AND price IS NOT NULL"""
    )
    # R-c: DROPNEW ckey + 真实原因
    con.execute(
        f"""CREATE OR REPLACE TABLE rc_reason AS
            SELECT code, mall, sku, min(reason) AS reason
            FROM read_parquet('{_q(drops)}') WHERE source='DROPNEW' GROUP BY 1, 2, 3"""
    )

    # 命中表（含证据引用; 命中 FINAL 行 → 保留并记 FINAL_ROW_RULE_CONFLICT）
    con.execute(
        """CREATE OR REPLACE TABLE h_a AS
           SELECT f.code, f.mall, f.sku,
                  string_agg('s3flags.parquet#row=' || r.src_row,
                             '|' ORDER BY TRY_CAST(r.src_row AS BIGINT)) AS ev
           FROM s3_flags f
           JOIN s3_all r ON r.code=f.code AND r.mall=f.mall AND r.sku=f.sku AND r.jz_yes
           WHERE f.jz_yes GROUP BY 1, 2, 3"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE h_b AS
           SELECT y.code, y.mall, y.sku,
                  string_agg('drops.parquet#DROP80#' || d.code || '/' || d.mall
                             || '/price=' || CAST(d.price AS VARCHAR), '|' ORDER BY d.price) AS ev
           FROM yes y JOIN rb80 d
             ON d.code=y.code AND d.mall=y.mall AND abs(d.price - y.price) <= 0.010000001
           GROUP BY 1, 2, 3"""
    )
    con.execute(
        f"""CREATE OR REPLACE TABLE h_c AS
           SELECT k.code, k.mall, k.sku,
                  'drops.parquet#DROPNEW#' || k.code || '/' || k.mall || '/' || k.sku AS ev
           FROM (SELECT DISTINCT code, mall, sku FROM read_parquet('{_q(drops)}')
                 WHERE source='DROPNEW') k"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE h_d AS
           SELECT p.code, p.last_mall AS mall, p.last_sku AS sku,
                  string_agg('final_problems.parquet#' || p.sheet || '#row=' || CAST(p.row_no AS VARCHAR),
                             '|' ORDER BY p.row_no) AS ev
           FROM prob2 p JOIN classes2 c ON c.sheet=p.sheet AND c.cur_result=p.cur_result
           WHERE c.origin='PRICE_DROP_04'
             AND COALESCE(p.last_sku,'')<>'' AND COALESCE(p.last_mall,'')<>''
           GROUP BY 1, 2, 3"""
    )
    con.execute(
        f"""CREATE OR REPLACE TABLE h_e AS
           SELECT code, mall, sku, 'p25.parquet#anomaly=' || min(anomaly) AS ev
           FROM read_parquet('{_q(p25)}')
           WHERE anomaly LIKE '异常%'
           GROUP BY 1, 2, 3"""
    )

    con.execute(
        f"""CREATE OR REPLACE TABLE ev AS
           SELECT y.code, y.mall, y.sku, y.is_final, y.record_origin, y.verdict_source,
                  y.url, y.src_file, y.src_sheet, y.src_row, y.sources,
                  (a.code IS NOT NULL) AS hit_a, (b.code IS NOT NULL) AS hit_b,
                  (c.code IS NOT NULL) AS hit_c, (d.code IS NOT NULL) AS hit_d,
                  (e.code IS NOT NULL) AS hit_e,
                  COALESCE(a.ev, '') AS ev_a, COALESCE(b.ev, '') AS ev_b,
                  COALESCE(c.ev, '') AS ev_c, COALESCE(d.ev, '') AS ev_d,
                  COALESCE(e.ev, '') AS ev_e,
                  COALESCE(rr.reason, '') AS dropnew_reason,
                  {_comment_source_sql('y')} AS comment_source
           FROM yes y
           LEFT JOIN h_a a ON a.code=y.code AND a.mall=y.mall AND a.sku=y.sku
           LEFT JOIN h_b b ON b.code=y.code AND b.mall=y.mall AND b.sku=y.sku
           LEFT JOIN h_c c ON c.code=y.code AND c.mall=y.mall AND c.sku=y.sku
           LEFT JOIN h_d d ON d.code=y.code AND d.mall=y.mall AND d.sku=y.sku
           LEFT JOIN h_e e ON e.code=y.code AND e.mall=y.mall AND e.sku=y.sku
           LEFT JOIN rc_reason rr ON rr.code=y.code AND rr.mall=y.mall AND rr.sku=y.sku
           LEFT JOIN cm ON cm.code=y.code AND cm.mall=y.mall AND cm.sku=y.sku"""
    )

    # Python 纯函数裁决: 首个命中 + FINAL 保护
    hit_rows = con.execute(
        """SELECT code, mall, sku, is_final, hit_a, hit_b, hit_c, hit_d, hit_e, dropnew_reason,
                  ev_a, ev_b, ev_c, ev_d, ev_e
           FROM ev WHERE hit_a OR hit_b OR hit_c OR hit_d OR hit_e"""
    ).fetchall()
    drop_keys, drop_hits, conflict_hits = [], [], []
    rule_drop_counts = {r: 0 for r in R.STEP2_RULE_ORDER}
    rule_conflict_counts = {r: 0 for r in R.STEP2_RULE_ORDER}
    for (code, mall, sku, is_final, ha, hb, hc, hd, he, dn_reason,
         ea, eb, ec, ed, ee) in hit_rows:
        res = R.evaluate_step2_hits(ha, hb, hc, hd, he, dn_reason)
        ev_map = {"R-a": ea, "R-b": eb, "R-c": ec, "R-d": ed, "R-e": ee}
        for rule, reason in res["hits"]:
            first = (rule == res["first_rule"])
            ev = ev_map[rule]
            if is_final:
                rule_conflict_counts[rule] += 1
                conflict_hits.append((code, mall, sku, rule, reason, ev))
            else:
                if first:
                    rule_drop_counts[rule] += 1
                    drop_keys.append((code, mall, sku))
                drop_hits.append((code, mall, sku, rule, reason, first, ev))
    n_dropped = len(drop_keys)
    n_conflict_events = len(conflict_hits)
    _log(f"step2: 命中行 {len(hit_rows)}; 剔除候选 {n_dropped} (按规则 {rule_drop_counts}); "
         f"FINAL_ROW_RULE_CONFLICT 事件 {n_conflict_events} (按规则 {rule_conflict_counts})")

    _reg(con, "drop_keys",
         {"code": [d[0] for d in drop_keys], "mall": [d[1] for d in drop_keys],
          "sku": [d[2] for d in drop_keys]},
         {"code": "str", "mall": "str", "sku": "str"})
    _reg(con, "drop_hits",
         {"code": [d[0] for d in drop_hits], "mall": [d[1] for d in drop_hits],
          "sku": [d[2] for d in drop_hits], "rule": [d[3] for d in drop_hits],
          "reason": [d[4] for d in drop_hits], "is_first": [d[5] for d in drop_hits],
          "evidence": [d[6] for d in drop_hits]},
         {"code": "str", "mall": "str", "sku": "str", "rule": "str", "reason": "str",
          "is_first": "bool", "evidence": "str"})
    _reg(con, "conflict_hits",
         {"code": [c[0] for c in conflict_hits], "mall": [c[1] for c in conflict_hits],
          "sku": [c[2] for c in conflict_hits], "rule": [c[3] for c in conflict_hits],
          "reason": [c[4] for c in conflict_hits], "evidence": [c[5] for c in conflict_hits]},
         {"code": "str", "mall": "str", "sku": "str", "rule": "str", "reason": "str",
          "evidence": "str"})

    con.execute(
        """CREATE OR REPLACE TABLE s2 AS
           SELECT y.* FROM yes y
           WHERE NOT EXISTS (SELECT 1 FROM drop_keys d
                             WHERE d.code=y.code AND d.mall=y.mall AND d.sku=y.sku)"""
    )

    neg_excluded = con.execute(
        "SELECT count(*) FROM s1t WHERE same_verdict='否'").fetchone()[0]
    s1_total = con.execute("SELECT count(*) FROM s1t").fetchone()[0]
    n_final_kept = con.execute("SELECT count(*) FROM s2 WHERE is_final").fetchone()[0]
    _assert(con, asserts, "step2 FINAL 行全部保留", n_final_kept, EXPECTED_FINAL_ROWS)
    _assert(con, asserts, "step2 无 FINAL 键被剔",
            con.execute(
                """SELECT count(*) FROM drop_keys d
                   WHERE EXISTS (SELECT 1 FROM s1t s WHERE s.is_final
                                 AND s.code=d.code AND s.mall=d.mall AND s.sku=d.sku)"""
            ).fetchone()[0], 0)
    _assert(con, asserts, "step2 输出 = 是-集 - 剔除",
            con.execute("SELECT count(*) FROM s2").fetchone()[0], yes_count - n_dropped)

    funnel2 = _funnel(con, "s2")
    funnel2.update({
        "rows_in_prev": s1_total,
        "filtered": neg_excluded + n_dropped,
        "rows_out": funnel2["rows"],
        "filtered_breakdown": {
            "negative_verdict_excluded": neg_excluded,
            **{f"{r}_dropped": rule_drop_counts[r] for r in R.STEP2_RULE_ORDER},
        },
    })
    conflicts2 = {
        "FINAL_ROW_RULE_CONFLICT": n_conflict_events,
        "final_row_rule_conflicts_by_rule": {r: rule_conflict_counts[r] for r in R.STEP2_RULE_ORDER},
        **{f"{r}_dropped": rule_drop_counts[r] for r in R.STEP2_RULE_ORDER},
    }
    _log(f"step2: 入 {s1_total} (是 {yes_count} / 否 {neg_excluded}) → 剔 {funnel2['filtered']} "
         f"(否排除 {neg_excluded} + 规则剔 {n_dropped}) → 剩 {funnel2['rows_out']}; "
         f"标品 {funnel2['codes']}, 商城 {funnel2['malls']}, 唯一SKU {funnel2['unique_skus']}")

    step2 = _t(cfg, "step2.parquet")
    con.execute(
        f"""COPY (SELECT {BASE_COLS} FROM s2 ORDER BY code, mall, sku)
            TO '{_q(step2)}' (FORMAT PARQUET)"""
    )

    # lineage stage2: 否排除 / 规则剔除(含叠加) / FINAL冲突 / 幸存 KEEP
    con.execute(
        f"""COPY (
            SELECT 2 AS stage, s.code AS standard_code, s.mall AS shop_code, s.sku AS sku_id,
                   s.url AS url, s.src_file AS source_file, s.src_sheet AS source_sheet,
                   s.src_row AS source_row, s.record_origin AS record_origin,
                   s.verdict_source AS verdict_source, {_snap_sql('s')} AS snapshot_source,
                   'DROP' AS next_stage_action, 'SAME_VERDICT_NO' AS internal_reason,
                   {_comment_source_sql('s')} AS comment_source,
                   'NEGATIVE_VERDICT' AS stage_drop_reason, '' AS conflict_type,
                   s.sources AS sources
            FROM s1t s LEFT JOIN cm ON cm.code=s.code AND cm.mall=s.mall AND cm.sku=s.sku
            WHERE s.same_verdict='否'
            UNION ALL
            SELECT 2, e.code, e.mall, e.sku, e.url, e.src_file, e.src_sheet, e.src_row,
                   e.record_origin, e.verdict_source,
                   CASE WHEN e.is_final THEN 'RECONSTRUCTED_FINAL_INTERSECTION' ELSE '' END,
                   'DROP', 'STEP2_RULE_DROP', e.comment_source, d.reason, '',
                   e.sources || '|' || d.evidence
            FROM ev e JOIN drop_hits d ON d.code=e.code AND d.mall=e.mall AND d.sku=e.sku
                       AND d.is_first
            UNION ALL
            SELECT 2, e.code, e.mall, e.sku, e.url, e.src_file, e.src_sheet, e.src_row,
                   e.record_origin, e.verdict_source,
                   CASE WHEN e.is_final THEN 'RECONSTRUCTED_FINAL_INTERSECTION' ELSE '' END,
                   'DROP', 'STEP2_RULE_DROP_OVERLAP', e.comment_source, d.reason, '',
                   e.sources || '|' || d.evidence
            FROM ev e JOIN drop_hits d ON d.code=e.code AND d.mall=e.mall AND d.sku=e.sku
                       AND NOT d.is_first
            UNION ALL
            SELECT 2, c.code, c.mall, c.sku, e.url, e.src_file, e.src_sheet, e.src_row,
                   e.record_origin, e.verdict_source, 'RECONSTRUCTED_FINAL_INTERSECTION',
                   'KEEP', 'FINAL_AUTHORITY_PROTECTED', e.comment_source, c.reason,
                   'FINAL_ROW_RULE_CONFLICT', e.sources || '|' || c.evidence
            FROM conflict_hits c JOIN ev e ON e.code=c.code AND e.mall=c.mall AND e.sku=c.sku
            UNION ALL
            SELECT 2, s.code, s.mall, s.sku, s.url, s.src_file, s.src_sheet, s.src_row,
                   s.record_origin, s.verdict_source, {_snap_sql('s')} AS snapshot_source,
                   'KEEP', '', {_comment_source_sql('s')} AS comment_source,
                   '', '', s.sources
            FROM s2 s LEFT JOIN cm ON cm.code=s.code AND cm.mall=s.mall AND cm.sku=s.sku
        ) TO '{_q(_t(cfg, "p2_lineage_stage2.parquet"))}' (FORMAT PARQUET)"""
    )

    stats = {
        "funnel": {"step2": funnel2},
        "conflicts": conflicts2,
        "asserts": asserts,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    _write_stage_stats(cfg, 2, stats)
    guard_disk_limit(cfg["temp_dir"])
    _log(f"step2 done → {step2} ({fmt_bytes(os.path.getsize(step2))}), 耗时 {stats['elapsed_sec']}s")
    return stats


# ---------------------------------------------------------------------------
# Step3 一物多码去重后结果
# ---------------------------------------------------------------------------

def build_step3(con, cfg: dict, WR) -> dict:
    t0 = _t0()
    asserts: list = []
    step2 = _t(cfg, "step2.parquet")
    _require(step2, "step2.parquet")
    _table_cm(con, cfg)
    _table_s3(con, cfg)

    con.execute(f"CREATE OR REPLACE TABLE s2t AS SELECT * FROM read_parquet('{_q(step2)}')")

    # 评论口径: S3.comment → comments_final → NULL（Step3 组内理论 winner 用）
    s3_comment = {
        (c, m, s): com
        for c, m, s, com in con.execute("SELECT code, mall, sku, comment FROM s3_disp").fetchall()
    }
    cm_value = {
        (c, m, s): v
        for c, m, s, v in con.execute("SELECT code, mall, sku, comment_value FROM cm").fetchall()
    }
    # 非归一化评论值计数（如 '100+'/'1万+'/'已售0' → 按缺失回退, 不发明换算规则）
    n_s3_comment_unnormalized = sum(
        0 if WR.is_normalized_comment(v) else 1 for v in s3_comment.values())

    n_rows_in = con.execute("SELECT count(*) FROM s2t").fetchone()[0]
    state = {"groups": 0, "single": 0, "final_groups": 0, "invalid_price": 0, "no_theory": 0}
    winners, losers, theory_conflicts = [], [], []

    def _flush(g: list, gkey) -> None:
        code, mall = gkey
        state["groups"] += 1
        if len(g) == 1:
            state["single"] += 1
        final_rows = [r for r in g if r[4]]
        if len(final_rows) > 1:
            print(f"ASSERT_HARD_FAIL: (code,mall)=({code},{mall}) 组内出现 "
                  f"{len(final_rows)} 个 FINAL 键（FINAL 自身应 ≤1/商城）", flush=True)
            sys.exit(2)
        theory_rows = [r for r in g if WR.valid_price(r[3])]
        state["invalid_price"] += len(g) - len(theory_rows)
        theory_sku = None
        if theory_rows:
            cand = [{"sku": r[2], "mall": r[1], "price": r[3],
                     "comment": WR.comment_fallback(s3_comment.get((code, mall, r[2])),
                                                    cm_value.get((code, mall, r[2])))}
                    for r in theory_rows]
            theory_sku = theory_rows[WR.pick_winner_index(cand)][2]
        if final_rows:
            state["final_groups"] += 1
            win = final_rows[0]
            conflict = bool(theory_sku is not None and theory_sku != win[2])
            if conflict:
                theory_conflicts.append((code, mall, win[2], theory_sku))
            winners.append((code, mall, win[2], theory_sku or "", True, conflict))
            for r in g:
                if not r[4]:
                    losers.append((code, mall, r[2]))
        elif theory_sku is not None:
            winners.append((code, mall, theory_sku, theory_sku, False, False))
            for r in g:
                if r[2] != theory_sku:
                    losers.append((code, mall, r[2]))
        else:
            # 全组价格无效（无理论依据）: 保留首见行并计数上报, 不造规则
            state["no_theory"] += 1
            winners.append((code, mall, g[0][2], "", False, False))
            for r in g[1:]:
                losers.append((code, mall, r[2]))

    cur = con.execute(
        "SELECT code, mall, sku, price, is_final FROM s2t ORDER BY code, mall, sku")
    prev_key, group = None, []
    while True:
        batch = cur.fetchmany(50000)
        if not batch:
            break
        for row in batch:
            key = (row[0], row[1])
            if prev_key is not None and key != prev_key:
                _flush(group, prev_key)
                group = []
            prev_key = key
            group.append(row)
    if group:
        _flush(group, prev_key)

    groups = state["groups"]
    n_theory_conflicts = len(theory_conflicts)
    _log(f"step3: 组 {groups} (单行组 {state['single']}, 含FINAL组 {state['final_groups']}); "
         f"winner {len(winners)}, 剔 YWDM_LOSER {len(losers)}; "
         f"WINNER_RULE_CONFLICT {n_theory_conflicts}; "
         f"价格无效行 {state['invalid_price']}, 无理论回退组 {state['no_theory']}, "
         f"S3 非归一化评论键 {n_s3_comment_unnormalized}")

    _reg(con, "w_keys",
         {"code": [w[0] for w in winners], "mall": [w[1] for w in winners],
          "sku": [w[2] for w in winners], "theory_sku": [w[3] for w in winners],
          "has_final": [w[4] for w in winners], "is_conflict": [w[5] for w in winners]},
         {"code": "str", "mall": "str", "sku": "str", "theory_sku": "str",
          "has_final": "bool", "is_conflict": "bool"})
    _reg(con, "l_keys",
         {"code": [x[0] for x in losers], "mall": [x[1] for x in losers],
          "sku": [x[2] for x in losers]},
         {"code": "str", "mall": "str", "sku": "str"})

    con.execute(
        """CREATE OR REPLACE TABLE s3 AS
           SELECT s.* FROM s2t s
           JOIN w_keys w ON w.code=s.code AND w.mall=s.mall AND w.sku=s.sku"""
    )
    _assert(con, asserts, "step3 (code,mall) 组内 distinct sku 最大值 = 1",
            con.execute(
                """SELECT max(n) FROM (SELECT code, mall, count(DISTINCT sku) AS n
                                       FROM s3 GROUP BY code, mall)"""
            ).fetchone()[0], 1)
    _assert(con, asserts, "step3 行数 = 组数",
            con.execute("SELECT count(*) FROM s3").fetchone()[0], groups)
    _assert(con, asserts, "step3 FINAL 行全部保留",
            con.execute("SELECT count(*) FROM s3 WHERE is_final").fetchone()[0],
            EXPECTED_FINAL_ROWS)

    funnel3 = _funnel(con, "s3")
    funnel3.update({
        "rows_in_prev": n_rows_in,
        "filtered": len(losers),
        "rows_out": funnel3["rows"],
        "filtered_breakdown": {"YWDM_LOSER": len(losers)},
        "groups": groups,
        "single_row_groups": state["single"],
        "final_covered_groups": state["final_groups"],
        "no_theory_fallback_groups": state["no_theory"],
        "invalid_price_rows": state["invalid_price"],
        "s3_comment_unnormalized_keys": n_s3_comment_unnormalized,
        "s3_comment_unnormalized_note": "S3.comment 原始展示串（如 '100+'/'1万+'/'已售0'）"
                                        "无既定归一化规则 → 理论 winner 评论按缺失回退",
        "note_min_num": "本重建按 组>1 即去重执行（gate 要求全表 (code,mall) ≤1 SKU）; "
                        "min_num/multiple 不重跑方差（spec §8.3/§23 明令禁止）",
    })
    conflicts3 = {
        "WINNER_RULE_CONFLICT": n_theory_conflicts,
        "YWDM_LOSER": len(losers),
    }

    step3 = _t(cfg, "step3.parquet")
    con.execute(
        f"""COPY (SELECT {BASE_COLS} FROM s3 ORDER BY code, mall, sku)
            TO '{_q(step3)}' (FORMAT PARQUET)"""
    )

    # lineage stage3
    con.execute(
        f"""COPY (
            SELECT 3 AS stage, s.code AS standard_code, s.mall AS shop_code, s.sku AS sku_id,
                   s.url AS url, s.src_file AS source_file, s.src_sheet AS source_sheet,
                   s.src_row AS source_row, s.record_origin AS record_origin,
                   s.verdict_source AS verdict_source, {_snap_sql('s')} AS snapshot_source,
                   'KEEP' AS next_stage_action,
                   CASE WHEN w.is_conflict THEN 'FINAL_OVERRIDE_WINNER' ELSE '' END AS internal_reason,
                   {_comment_source_sql('s')} AS comment_source, '' AS stage_drop_reason,
                   CASE WHEN w.is_conflict THEN 'WINNER_RULE_CONFLICT' ELSE '' END AS conflict_type,
                   s.sources || CASE WHEN w.is_conflict
                                     THEN '|theoretical_winner=sku:' || w.theory_sku ELSE '' END
                       AS sources
            FROM s3 s
            JOIN w_keys w ON w.code=s.code AND w.mall=s.mall AND w.sku=s.sku
            LEFT JOIN cm ON cm.code=s.code AND cm.mall=s.mall AND cm.sku=s.sku
            UNION ALL
            SELECT 3, s.code, s.mall, s.sku, s.url, s.src_file, s.src_sheet, s.src_row,
                   s.record_origin, s.verdict_source, {_snap_sql('s')} AS snapshot_source,
                   'DROP', 'YWDM_LOSER', {_comment_source_sql('s')} AS comment_source,
                   'YWDM_LOSER', '', s.sources
            FROM s2t s
            JOIN l_keys l ON l.code=s.code AND l.mall=s.mall AND l.sku=s.sku
            LEFT JOIN cm ON cm.code=s.code AND cm.mall=s.mall AND cm.sku=s.sku
        ) TO '{_q(_t(cfg, "p2_lineage_stage3.parquet"))}' (FORMAT PARQUET)"""
    )

    stats = {
        "funnel": {"step3": funnel3},
        "conflicts": conflicts3,
        "asserts": asserts,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    _write_stage_stats(cfg, 3, stats)
    guard_disk_limit(cfg["temp_dir"])
    _log(f"step3 done → {step3} ({fmt_bytes(os.path.getsize(step3))}), 耗时 {stats['elapsed_sec']}s")
    return stats


# ---------------------------------------------------------------------------
# Step4 人工审核通过结果
# ---------------------------------------------------------------------------

def build_step4(con, cfg: dict, WR) -> dict:
    t0 = _t0()
    asserts: list = []
    step3 = _t(cfg, "step3.parquet")
    _require(step3, "step3.parquet")
    _table_fm(con, cfg)
    _table_cm(con, cfg)

    n_cm, k_cm = con.execute(
        "SELECT count(*), count(DISTINCT code||'|'||mall||'|'||sku) FROM cm").fetchone()
    _assert(con, asserts, "comments_final 键唯一且行数=FINAL 行数", (n_cm, k_cm),
            (EXPECTED_FINAL_ROWS, EXPECTED_FINAL_ROWS))

    con.execute(f"CREATE OR REPLACE TABLE s3t AS SELECT * FROM read_parquet('{_q(step3)}')")

    # 硬断言: FINAL ⊆ Step3（缺口即构造失败 → 样本 + exit 2）
    gap = con.execute(
        """SELECT f.code, f.mall, f.sku FROM fm f
           WHERE NOT EXISTS (SELECT 1 FROM s3t s
                             WHERE s.code=f.code AND s.mall=f.mall AND s.sku=f.sku)"""
    ).fetchall()
    _assert(con, asserts, "FINAL ⊆ Step3 (FINAL_ONLY=0)", len(gap), 0, hard=False)
    if gap:
        print("FINAL_ONLY 缺口样本(≤20):", gap[:20], flush=True)
        sys.exit(2)

    con.execute(
        """CREATE OR REPLACE TABLE s4 AS
           SELECT s.*, cm.comment_value, cm.state AS comment_state, cm.comment_src_layer,
                  '通过' AS audit_result,
                  'RECONSTRUCTED_FINAL_INTERSECTION' AS snapshot_source
           FROM s3t s JOIN cm ON cm.code=s.code AND cm.mall=s.mall AND cm.sku=s.sku
           WHERE s.is_final"""
    )
    n4 = con.execute("SELECT count(*) FROM s4").fetchone()[0]
    _assert(con, asserts, "step4 行数 = FINAL 行数", n4, EXPECTED_FINAL_ROWS)
    _assert(con, asserts, "step4 空 SKU 行数 = FINAL 空 SKU 行数",
            con.execute("SELECT count(*) FROM s4 WHERE sku=''").fetchone()[0],
            EXPECTED_EMPTY_SKU_FINAL)
    fm_only4 = con.execute(
        """SELECT count(*) FROM fm f WHERE NOT EXISTS
           (SELECT 1 FROM s4 WHERE s4.code=f.code AND s4.mall=f.mall AND s4.sku=f.sku)"""
    ).fetchone()[0]
    s4_only = con.execute(
        """SELECT count(*) FROM s4 s WHERE NOT EXISTS
           (SELECT 1 FROM fm f WHERE f.code=s.code AND f.mall=s.mall AND f.sku=s.sku)"""
    ).fetchone()[0]
    _assert(con, asserts, "step4 与 FINAL 键集合双向一致", (fm_only4, s4_only), (0, 0))

    survivors = con.execute(
        "SELECT count(*) FROM s3t WHERE NOT is_final").fetchone()[0]
    state_counts = dict(con.execute(
        "SELECT COALESCE(comment_state,''), count(*) FROM s4 GROUP BY 1").fetchall())
    filled = con.execute("SELECT count(*) FROM s4 WHERE comment_value IS NOT NULL").fetchone()[0]
    comments4 = {
        "POSITIVE": state_counts.get("POSITIVE", 0),
        "ZERO": state_counts.get("ZERO", 0),
        "EMPTY_SRC": state_counts.get("EMPTY_SRC", 0),
        "NOT_FOUND": state_counts.get("NOT_FOUND", 0),
        "NO_KEY": state_counts.get("NO_KEY", 0),
        "not_found_total": state_counts.get("NOT_FOUND", 0) + state_counts.get("NO_KEY", 0),
        "comment_filled": filled,
        "coverage": round(filled / n4, 6) if n4 else 0.0,
    }

    funnel4 = _funnel(con, "s4")
    funnel4.update({
        "rows_in_prev": con.execute("SELECT count(*) FROM s3t").fetchone()[0],
        "filtered": survivors,
        "rows_out": funnel4["rows"],
        "filtered_breakdown": {"MANUAL_REVIEW_NOT_PASSED": survivors},
        "audit_result": "通过",
        "snapshot_source": "RECONSTRUCTED_FINAL_INTERSECTION",
    })
    conflicts4 = {"MANUAL_REVIEW_NOT_PASSED": survivors}
    _log(f"step4: 入 {funnel4['rows_in_prev']} → 非 FINAL 幸存剔除 {survivors} → 剩 {n4}; "
         f"评论四态 {comments4}")

    step4 = _t(cfg, "step4.parquet")
    con.execute(
        f"""COPY (SELECT {BASE_COLS},
                     comment_value, comment_state, comment_src_layer,
                     audit_result, snapshot_source
              FROM s4 ORDER BY code, mall, sku)
            TO '{_q(step4)}' (FORMAT PARQUET)"""
    )

    # lineage stage4
    con.execute(
        f"""COPY (
            SELECT 4 AS stage, s.code AS standard_code, s.mall AS shop_code, s.sku AS sku_id,
                   s.url AS url, s.src_file AS source_file, s.src_sheet AS source_sheet,
                   s.src_row AS source_row, s.record_origin AS record_origin,
                   s.verdict_source AS verdict_source,
                   'RECONSTRUCTED_FINAL_INTERSECTION' AS snapshot_source,
                   'KEEP' AS next_stage_action, '' AS internal_reason,
                   COALESCE(cm.comment_src_layer, '') AS comment_source,
                   '' AS stage_drop_reason, '' AS conflict_type, s.sources AS sources
            FROM s4 s LEFT JOIN cm ON cm.code=s.code AND cm.mall=s.mall AND cm.sku=s.sku
            UNION ALL
            SELECT 4, s.code, s.mall, s.sku, s.url, s.src_file, s.src_sheet, s.src_row,
                   s.record_origin, s.verdict_source, '',
                   'DROP', 'MANUAL_REVIEW_NOT_PASSED', {_comment_source_sql('s')} AS comment_source,
                   'MANUAL_REVIEW_NOT_PASSED', '', s.sources
            FROM s3t s LEFT JOIN cm ON cm.code=s.code AND cm.mall=s.mall AND cm.sku=s.sku
            WHERE NOT s.is_final
        ) TO '{_q(_t(cfg, "p2_lineage_stage4.parquet"))}' (FORMAT PARQUET)"""
    )

    stats = {
        "funnel": {"step4": funnel4},
        "conflicts": conflicts4,
        "comments": {"step4": comments4},
        "asserts": asserts,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    _write_stage_stats(cfg, 4, stats)
    guard_disk_limit(cfg["temp_dir"])
    _log(f"step4 done → {step4} ({fmt_bytes(os.path.getsize(step4))}), 耗时 {stats['elapsed_sec']}s")
    return stats


# ---------------------------------------------------------------------------
# Step5 价格指数计算结果
# ---------------------------------------------------------------------------

def build_step5(con, cfg: dict, WR) -> dict:
    t0 = _t0()
    asserts: list = []
    step4 = _t(cfg, "step4.parquet")
    _require(step4, "step4.parquet")
    _table_fm(con, cfg)
    _table_cm(con, cfg)
    con.execute(f"CREATE OR REPLACE TABLE s4t AS SELECT * FROM read_parquet('{_q(step4)}')")

    con.execute(
        """CREATE OR REPLACE TABLE s5 AS
           SELECT f.code, f.name, f.sku, f.mall, f.title, f.url, f.price, f.price_raw,
                  f.index_val, f.method,
                  TRUE AS is_final, '是' AS same_verdict, 'FINAL_AUTHORITY' AS verdict_source,
                  'FINAL_MAIN' AS record_origin,
                  'final_main.parquet' AS src_file, '最终处理结果' AS src_sheet,
                  CAST(f.src_row AS VARCHAR) AS src_row,
                  'final_main.parquet#row=' || CAST(f.src_row AS VARCHAR) AS sources,
                  cm.comment_value, cm.state AS comment_state, cm.comment_src_layer,
                  'RECONSTRUCTED_FINAL_INTERSECTION' AS snapshot_source
           FROM fm f JOIN cm ON cm.code=f.code AND cm.mall=f.mall AND cm.sku=f.sku"""
    )
    n5 = con.execute("SELECT count(*) FROM s5").fetchone()[0]
    fm_only5 = con.execute(
        """SELECT count(*) FROM fm f WHERE NOT EXISTS
           (SELECT 1 FROM s5 WHERE s5.code=f.code AND s5.mall=f.mall AND s5.sku=f.sku)"""
    ).fetchone()[0]
    s5_only = con.execute(
        """SELECT count(*) FROM s5 s WHERE NOT EXISTS
           (SELECT 1 FROM fm f WHERE f.code=s.code AND f.mall=s.mall AND f.sku=s.sku)"""
    ).fetchone()[0]
    _assert(con, asserts, "step5 vs FINAL 键集合双向 0 差", (fm_only5, s5_only), (0, 0))
    _assert(con, asserts, "step5 行数 = FINAL 行数", n5, EXPECTED_FINAL_ROWS)
    mismatch = con.execute(
        """SELECT count(*) FROM fm f JOIN s5 s
             ON s.code=f.code AND s.mall=f.mall AND s.sku=f.sku
           WHERE f.price IS DISTINCT FROM s.price
              OR f.price_raw IS DISTINCT FROM s.price_raw
              OR f.index_val IS DISTINCT FROM s.index_val
              OR f.method IS DISTINCT FROM s.method"""
    ).fetchone()[0]
    _assert(con, asserts, "step5 价格/指数/方式逐行原样 (差异=0)", mismatch, 0)
    s4_vs_s5 = con.execute(
        """SELECT (SELECT count(*) FROM s4t a WHERE NOT EXISTS
                     (SELECT 1 FROM s5 s WHERE s.code=a.code AND s.mall=a.mall AND s.sku=a.sku)),
                  (SELECT count(*) FROM s5 s WHERE NOT EXISTS
                     (SELECT 1 FROM s4t a WHERE a.code=s.code AND a.mall=s.mall AND a.sku=s.sku))"""
    ).fetchone()
    _assert(con, asserts, "step4 与 step5 键集合一致", s4_vs_s5, (0, 0))

    state_counts = dict(con.execute(
        "SELECT COALESCE(comment_state,''), count(*) FROM s5 GROUP BY 1").fetchall())
    filled = con.execute("SELECT count(*) FROM s5 WHERE comment_value IS NOT NULL").fetchone()[0]
    comments5 = {
        "POSITIVE": state_counts.get("POSITIVE", 0),
        "ZERO": state_counts.get("ZERO", 0),
        "EMPTY_SRC": state_counts.get("EMPTY_SRC", 0),
        "NOT_FOUND": state_counts.get("NOT_FOUND", 0),
        "NO_KEY": state_counts.get("NO_KEY", 0),
        "not_found_total": state_counts.get("NOT_FOUND", 0) + state_counts.get("NO_KEY", 0),
        "comment_filled": filled,
        "coverage": round(filled / n5, 6) if n5 else 0.0,
    }

    funnel5 = _funnel(con, "s5")
    funnel5.update({
        "rows_in_prev": con.execute("SELECT count(*) FROM s4t").fetchone()[0],
        "filtered": 0,
        "rows_out": funnel5["rows"],
        "filtered_breakdown": {},
        "snapshot_source": "RECONSTRUCTED_FINAL_INTERSECTION",
    })
    _log(f"step5: FINAL 全行 {n5} + 评论数 + 指数/方式原样; 评论四态 {comments5}")

    step5 = _t(cfg, "step5.parquet")
    con.execute(
        f"""COPY (SELECT {BASE_COLS},
                     comment_value, comment_state, comment_src_layer, snapshot_source
              FROM s5 ORDER BY code, mall, sku)
            TO '{_q(step5)}' (FORMAT PARQUET)"""
    )

    con.execute(
        f"""COPY (
            SELECT 5 AS stage, s.code AS standard_code, s.mall AS shop_code, s.sku AS sku_id,
                   s.url AS url, s.src_file AS source_file, s.src_sheet AS source_sheet,
                   s.src_row AS source_row, s.record_origin AS record_origin,
                   s.verdict_source AS verdict_source, s.snapshot_source AS snapshot_source,
                   'KEEP' AS next_stage_action, '' AS internal_reason,
                   COALESCE(cm.comment_src_layer, '') AS comment_source,
                   '' AS stage_drop_reason, '' AS conflict_type, s.sources AS sources
            FROM s5 s LEFT JOIN cm ON cm.code=s.code AND cm.mall=s.mall AND cm.sku=s.sku
        ) TO '{_q(_t(cfg, "p2_lineage_stage5.parquet"))}' (FORMAT PARQUET)"""
    )

    stats = {
        "funnel": {"step5": funnel5},
        "conflicts": {},
        "comments": {"step5": comments5},
        "asserts": asserts,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    _write_stage_stats(cfg, 5, stats)
    guard_disk_limit(cfg["temp_dir"])
    _log(f"step5 done → {step5} ({fmt_bytes(os.path.getsize(step5))}), 耗时 {stats['elapsed_sec']}s")
    return stats


# ---------------------------------------------------------------------------
# 阶段统计 / lineage 汇总
# ---------------------------------------------------------------------------

def _write_stage_stats(cfg: dict, stage: int, stats: dict) -> None:
    path = _t(cfg, f"p2_stats_stage{stage}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def _merge_dicts_sum(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict):
            if not isinstance(dst.get(k), dict):
                dst[k] = {}
            _merge_dicts_sum(dst[k], v)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            dst[k] = dst.get(k, 0) + v
        else:
            dst[k] = v


def finalize(cfg: dict, con, stages_ran: list) -> None:
    # lineage 合并
    lin_parts = [f"p2_lineage_stage{n}.parquet" for n in stages_ran
                 if os.path.isfile(_t(cfg, f"p2_lineage_stage{n}.parquet"))]
    lin_out = _wd(cfg, "lineage", "lineage.parquet")
    if lin_parts:
        union = " UNION ALL ".join(
            f"SELECT * FROM read_parquet('{_q(_t(cfg, p))}')" for p in lin_parts)
        os.makedirs(os.path.dirname(lin_out), exist_ok=True)
        con.execute(f"COPY ({union}) TO '{_q(lin_out)}' (FORMAT PARQUET)")
        n = con.execute(f"SELECT count(*) FROM read_parquet('{_q(lin_out)}')").fetchone()[0]
        _log(f"lineage → {lin_out} ({n} 行, {fmt_bytes(os.path.getsize(lin_out))})")

    # stats 合并
    merged = {
        "run_meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.version.split()[0],
            "script": os.path.abspath(__file__),
            "workdir": cfg["workdir"],
            "stages": list(stages_ran),
            "funnel_metric_note": "标品数=distinct code; 商城数/唯一SKU数按非空口径",
        },
        "funnel": {},
        "conflicts": {},
        "comments": {},
        "asserts": [],
        "notes": {
            "step2_rules": "R-a 极值标志 / R-b 偏离超80(code+mall+price±0.01) / R-c DROPNEW真实原因 / "
                           "R-d 04表存疑价格剔除 / R-e 平台价格异常; FINAL 键永不剔除, 命中记 "
                           "FINAL_ROW_RULE_CONFLICT; 叠加取首个命中, 全部命中记 lineage",
            "variance_rerun": "不对方差公式全量重跑（spec §8.3/§23 禁止; 80%阈值为历史临时规则, "
                              "min_num/multiple 仅解释）",
        },
    }
    for n in stages_ran:
        p = _t(cfg, f"p2_stats_stage{n}.json")
        if not os.path.isfile(p):
            continue
        with open(p, "r", encoding="utf-8") as f:
            s = json.load(f)
        merged["funnel"].update(s.get("funnel", {}))
        _merge_dicts_sum(merged["conflicts"], s.get("conflicts", {}))
        merged["comments"].update(s.get("comments", {}))
        merged["asserts"].extend([{**a, "stage": n} for a in s.get("asserts", [])])
    merged["asserts_all_pass"] = all(a.get("pass") for a in merged["asserts"])

    outputs = {}
    for i in range(1, 6):
        p = _t(cfg, f"step{i}.parquet")
        if os.path.isfile(p):
            outputs[f"step{i}.parquet"] = {"path": p, "size": os.path.getsize(p)}
    if os.path.isfile(lin_out):
        outputs["lineage.parquet"] = {"path": lin_out, "size": os.path.getsize(lin_out)}
    merged["outputs"] = outputs

    out = _wd(cfg, "reports", "stage_stats.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    _log(f"stage_stats → {out}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="F2 重建层（验收回溯重建）")
    ap.add_argument("--config", required=True, help="config.local.json 路径")
    ap.add_argument("--stage", default="all",
                    choices=["all", "1", "2", "3", "4", "5"], help="运行阶段")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    ensure_libs(cfg["libs_dir"])
    os.makedirs(cfg["temp_dir"], exist_ok=True)
    for sub in ("lineage", "reports"):
        os.makedirs(_wd(cfg, sub), exist_ok=True)

    algo = R.load_algo(cfg["algo_src"])
    WR = R.WinnerRules(algo)
    _log(f"ALGO 包已加载: {cfg['algo_src']}")

    con = connect_duckdb(cfg)
    stages = [1, 2, 3, 4, 5] if args.stage == "all" else [int(args.stage)]
    builders = {1: build_step1, 2: build_step2, 3: build_step3, 4: build_step4, 5: build_step5}
    for n in stages:
        _log(f"=== F2 step {n} ({STAGE_NAMES[n]}) ===")
        builders[n](con, cfg, WR)
    finalize(cfg, con, stages)
    con.close()
    print("P2_BUILD_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
