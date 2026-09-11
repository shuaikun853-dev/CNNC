# -*- coding: utf-8 -*-
"""p1_extract.py — F1 抽取层主脚本（验收回溯重建）。

规格: reports/SPEC_ACCEPTANCE_RECONSTRUCTION.md §0/§1/§2/§3/§5/§7（冻结版契约）。

用法:
    python.exe -X utf8 p1_extract.py --config <config.local.json> [--only final|p25|s3|drops|dify|comments|all]

产物（全部写 cfg.temp_dir 与 cfg.workdir\\inventory\\）:
    temp\\final_main.parquet        FA 主表 + DELPQ 交叉校验（行数/ckey 集合差/method 分布）
    temp\\final_problems.parquet    FA 4 个问题 sheet 全行全列（长表）
    temp\\p25.parquet               P25 平台匹配候选池
    temp\\s3flags.parquet           S3FLAGS 补采批标志
    temp\\drops.parquet             DROP80 + DROPNEW
    temp\\dify.parquet              DIFY 同款再校验
    temp\\comments_final.parquet    FINAL 行评论合并（ZS>RECCSV>CVPOW>CVDEL>CVIDX, 四态）
    inventory\\p1_stats.json        全部统计 + 输入文件 sha256/size/mtime

硬红线: 输入只读; 不写 temp\\ 与 inventory\\ 之外任何位置; RECCSV 只 duckdb 直读聚合;
temp 总量 >3GB → DISK_SAFETY_STOP(exit 3); 交叉校验失败 → 打印差异样本并 exit 2。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from p1_common import (  # noqa: E402
    cell,
    column_index,
    connect_duckdb,
    dir_size,
    ensure_libs,
    file_meta,
    fmt_bytes,
    guard_disk_limit,
    load_config,
    norm_cell,
    norm_key,
    norm_price,
    sha256_file,
    write_parquet,
    xlsx_header_rows,
)

# ---------------------------------------------------------------------------
# 冻结事实（SPEC §0, 已由主 Agent 验证, 用于交叉校验断言）
# ---------------------------------------------------------------------------
MAIN_SHEET = "最终处理结果"
PROBLEM_SHEETS = ["01_4449无数据", "02_5.3%存疑同品", "03_非同品原因", "04_34952存疑价格"]
EXPECTED_MAIN_ROWS = 418291
EXPECTED_DISTINCT_CODE = 120047
EXPECTED_EMPTY_SKU = 359
FA_HEADERS = {
    "code": "标准商品编码",
    "name": "标准商品名称",
    "sku": "互联网商品编码（SKUID）",
    "mall": "互联网商城编码",
    "title": "互联网商品名称",
    "url": "互联网链接",
    "price": "互联网商品价格",
    "index_val": "本次处理后的价格指数",
    "method": "价格指数计算方式",
}
P25_HEADERS = {
    "code": "标准商品编码",
    "std_name": "商品标题",
    "mall": "匹配商城编码",
    "sku": "匹配商品编码",
    "title": "匹配商品标题",
    "url": "匹配商品链接",
    "price_buy": "匹配采购价（元）",
    "price_mkt": "匹配市场价（元）",
    "anomaly": "价格异常类型",
}
S3_HEADERS = {
    "code": "标准商品编码",
    "sku": "SKUID",
    "mall": "供应商编码",
    "title": "互联网标题",
    "url": "URL",
    "price": "价格",
    "jz": "是否极值过滤",
    "ywdm": "是否一物多码过滤",
    "jd": "是否京东过滤",
    "attr": "是否属性过滤",
    "filtered": "是否过滤",
    "match": "是否匹配",
    "verdict2": "第二轮模型校验结果",
    "comment": "评论数",
}
DROP80_HEADERS = {
    "code": "标准商品编码",
    "mall": "互联网商城编码",
    "price": "互联网商品价格",
    "基准价格": "基准价格",
    "价格偏离率": "价格偏离率",
    "价格基准来源": "价格基准来源",
    "价格核查结论": "价格核查结论",
    "价格问题说明": "价格问题说明",
}
DROPNEW_HEADERS = {
    "code": "标准商品编码",
    "sku": "SKUID",
    "mall": "供应商编码",
    "price": "价格",
    "reason": "剔除原因",
}
DIFY_HEADERS = {"uid": "唯一标识", "title1": "商品标题1", "title2": "商品标题2", "result": "结果"}
ZS_HEADERS = {
    "sku": "商品编码",
    "unit_id": "商品最小品类单元id",
    "mall": "商城标识",
    "comment": "评论",
    "url": "商品详情地址",
    "batch": "批次号",
}

INPUT_KEYS = [
    "final_authority", "final_authority_equiv", "final_authority_orig",
    "p25_pool", "s3_flags", "s3_raw", "drop80", "drop_new", "dify_recheck",
    "recall_csv", "comment_index_parquet", "delivery_coverage_parquet",
    "power_match_parquet", "delivery_0825_parquet", "zs_result", "algo_src",
]

OUTPUT_FILES = [
    "final_main.parquet", "final_problems.parquet", "p25.parquet",
    "s3flags.parquet", "drops.parquet", "dify.parquet",
    "comments_final.parquet", "reccsv_agg.parquet",
]


def _t0() -> float:
    return time.time()


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _out(cfg: dict, name: str) -> str:
    return os.path.join(cfg["temp_dir"], name)


def _inventory_path(cfg: dict) -> str:
    return os.path.join(cfg["workdir"], "inventory", "p1_stats.json")


def _q(path: str) -> str:
    """SQL 字符串转义（duckdb 单引号）。"""
    return path.replace("'", "''")


# ---------------------------------------------------------------------------
# 输入文件清单（sha256/size/mtime）
# ---------------------------------------------------------------------------

def hash_inputs(cfg: dict) -> dict:
    out = {}
    for key in INPUT_KEYS:
        p = cfg[key]
        if not os.path.exists(p):
            out[key] = {"path": p, "error": "MISSING"}
            continue
        meta = file_meta(p)
        _log(f"  sha256 {key}: {os.path.basename(p)} ({fmt_bytes(meta['size'])})")
        meta["sha256"] = sha256_file(p)
        out[key] = meta
    return out


# ---------------------------------------------------------------------------
# 1. final_main.parquet
# ---------------------------------------------------------------------------

def run_final(cfg: dict, stats: dict) -> dict:
    """FA 主表流式抽取 + DELPQ 交叉校验。任一校验 FAIL → 打印样本 ≤20 条并 exit 2。"""
    t = _t0()
    con = connect_duckdb(cfg)
    path = cfg["final_authority"]
    header, rows = xlsx_header_rows(path, MAIN_SHEET)
    idx = column_index(header, FA_HEADERS)

    fields = ["code", "name", "sku", "mall", "title", "url", "price_raw", "index_val", "method"]
    cols = {f: [] for f in fields}
    cols["price"] = []
    cols["src_row"] = []
    n = 0
    skipped_blank = 0
    for row_no, row in rows:
        vals = {f: norm_cell(cell(row, idx[f])) for f in fields if f != "price_raw"}
        if not any(vals.values()):
            skipped_blank += 1
            continue
        raw_price = norm_cell(cell(row, idx["price"]))
        for f in fields:
            cols[f].append(raw_price if f == "price_raw" else vals[f])
        cols["price"].append(norm_price(raw_price))
        cols["src_row"].append(row_no)
        n += 1

    fm = _out(cfg, "final_main.parquet")
    write_parquet(
        cols,
        {**{f: "string" for f in fields}, "price": "double", "src_row": "int64"},
        fm,
    )
    _log(f"final_main: {n} 数据行 (跳过空行 {skipped_blank}) → {fm}")

    # ---- 交叉校验（SPEC §3.1）----
    checks = {}
    delpq = _q(cfg["delivery_0825_parquet"])
    fm_sql = _q(fm)

    checks["row_count"] = {
        "actual": con.execute(f"SELECT count(*) FROM read_parquet('{fm_sql}')").fetchone()[0],
        "expected": EXPECTED_MAIN_ROWS,
    }
    checks["distinct_code"] = {
        "actual": con.execute(f"SELECT count(DISTINCT code) FROM read_parquet('{fm_sql}')").fetchone()[0],
        "expected": EXPECTED_DISTINCT_CODE,
    }
    checks["empty_sku_rows"] = {
        "actual": con.execute(f"SELECT count(*) FROM read_parquet('{fm_sql}') WHERE sku=''").fetchone()[0],
        "expected": EXPECTED_EMPTY_SKU,
    }
    checks["code_mall_multi_sku_groups"] = {
        "actual": con.execute(
            f"SELECT count(*) FROM (SELECT code, mall FROM read_parquet('{fm_sql}') GROUP BY code, mall HAVING count(DISTINCT sku) > 1)"
        ).fetchone()[0],
        "expected": 0,
    }

    # 注意: DELPQ 空 sku/mall 为 SQL NULL, 本表归一化为 '' → 比较前统一 COALESCE
    a_minus_b = (
        f"(SELECT code, COALESCE(mall,'') AS mall, COALESCE(sku,'') AS sku FROM read_parquet('{fm_sql}') "
        f"EXCEPT SELECT code, COALESCE(mall,'') AS mall, COALESCE(sku,'') AS sku FROM read_parquet('{delpq}'))"
    )
    b_minus_a = (
        f"(SELECT code, COALESCE(mall,'') AS mall, COALESCE(sku,'') AS sku FROM read_parquet('{delpq}') "
        f"EXCEPT SELECT code, COALESCE(mall,'') AS mall, COALESCE(sku,'') AS sku FROM read_parquet('{fm_sql}'))"
    )
    fm_only = con.execute(f"SELECT count(*) FROM {a_minus_b}").fetchone()[0]
    dp_only = con.execute(f"SELECT count(*) FROM {b_minus_a}").fetchone()[0]
    checks["ckey_fm_minus_delpq"] = {"actual": fm_only, "expected": 0}
    checks["ckey_delpq_minus_fm"] = {"actual": dp_only, "expected": 0}

    method_fm = dict(con.execute(
        f"SELECT COALESCE(method,'') AS method, count(*) FROM read_parquet('{fm_sql}') GROUP BY 1").fetchall())
    method_dp = dict(con.execute(
        f"SELECT COALESCE(method,'') AS method, count(*) FROM read_parquet('{delpq}') GROUP BY 1").fetchall())
    checks["method_distribution_equal"] = {"actual": method_fm == method_dp, "expected": True}

    failed = [k for k, v in checks.items() if v["actual"] != v["expected"]]
    if failed:
        print("CROSS_CHECK_FAIL:", failed, flush=True)
        for k in failed:
            print(f"  {k}: actual={checks[k]['actual']} expected={checks[k]['expected']}", flush=True)
        samples = con.execute(f"SELECT * FROM {a_minus_b} LIMIT 20").fetchall()
        if samples:
            print(f"  样本 fm_only(≤20): {samples}", flush=True)
        samples = con.execute(f"SELECT * FROM {b_minus_a} LIMIT 20").fetchall()
        if samples:
            print(f"  样本 delpq_only(≤20): {samples}", flush=True)
        if method_fm != method_dp:
            print(f"  method_fm={method_fm}", flush=True)
            print(f"  method_delpq={method_dp}", flush=True)
        con.close()
        sys.exit(2)

    print("  method 分布:", json.dumps(method_fm, ensure_ascii=False), flush=True)
    print("  交叉校验 PASS: 行数/distinct code/空SKU/(code,mall)组/ckey 集合差双向/method 分布", flush=True)
    con.close()
    sec = {
        "rows": n,
        "skipped_blank_rows": skipped_blank,
        "src_file": path,
        "main_sheet": MAIN_SHEET,
        "method_distribution": method_fm,
        "cross_check_vs_delivery_0825": checks,
        "cross_check": "PASS",
        "elapsed_sec": round(time.time() - t, 1),
    }
    stats["final"] = sec
    return sec


# ---------------------------------------------------------------------------
# 2. final_problems.parquet
# ---------------------------------------------------------------------------

def run_problems(cfg: dict, stats: dict) -> dict:
    """FA 4 个问题 sheet 全行全列 → 长表 (sheet, row_no + 原列名并集, 缺列补'')。"""
    t = _t0()
    path = cfg["final_authority"]
    union_cols: list[str] = []
    per_sheet = {}
    sheet_data = []  # (sheet, [(row_no, {col: val})])
    for sn in PROBLEM_SHEETS:
        header, rows = xlsx_header_rows(path, sn)
        for h in header:
            if h and h not in union_cols:
                union_cols.append(h)
        recs = []
        blank = 0
        for row_no, row in rows:
            vals = {h: norm_cell(cell(row, i)) for i, h in enumerate(header)}
            if not any(vals.values()):
                blank += 1
                continue
            recs.append((row_no, vals))
        per_sheet[sn] = {"header_cols": len(header), "data_rows": len(recs), "blank_rows_skipped": blank}
        sheet_data.append((sn, recs))
        _log(f"problems[{sn}]: {len(recs)} 数据行 (跳过空行 {blank})")

    out_cols = {"sheet": [], "row_no": []}
    for c in union_cols:
        out_cols[c] = []
    total = 0
    for sn, recs in sheet_data:
        for row_no, vals in recs:
            out_cols["sheet"].append(sn)
            out_cols["row_no"].append(row_no)
            for c in union_cols:
                out_cols[c].append(vals.get(c, ""))
            total += 1

    fp = _out(cfg, "final_problems.parquet")
    write_parquet(out_cols, {**{c: "string" for c in union_cols}, "sheet": "string", "row_no": "int64"}, fp)
    # 注: SPEC §0 的行数口径 = 工作表 max_row（含表头行），本表记录纯数据行（每 sheet 少 1）。
    spec_convention = {sn: per_sheet[sn]["data_rows"] + 1 + per_sheet[sn]["blank_rows_skipped"]
                       for sn in PROBLEM_SHEETS}
    sec = {
        "rows": total,
        "columns": union_cols,
        "per_sheet": per_sheet,
        "rows_spec_maxrow_convention": spec_convention,
        "note": "SPEC §0 行数=工作表 max_row(含表头); 本表为纯数据行",
        "src_file": path,
        "elapsed_sec": round(time.time() - t, 1),
    }
    stats["problems"] = sec
    _log(f"final_problems: {total} 行 × {len(union_cols)} 列 → {fp}")
    return sec


# ---------------------------------------------------------------------------
# 3. p25.parquet
# ---------------------------------------------------------------------------

def run_p25(cfg: dict, stats: dict) -> dict:
    t = _t0()
    con = connect_duckdb(cfg)
    path = cfg["p25_pool"]
    header, rows = xlsx_header_rows(path, "合并数据")
    idx = column_index(header, P25_HEADERS)

    fields = ["code", "std_name", "mall", "sku", "title", "url", "anomaly"]
    cols = {f: [] for f in fields}
    cols.update({"price_buy": [], "price_mkt": [], "price_used": [], "src_row": []})
    n = 0
    skipped_blank = 0
    for row_no, row in rows:
        vals = {f: norm_cell(cell(row, idx[f])) for f in fields}
        if not any(vals.values()):
            skipped_blank += 1
            continue
        buy = norm_price(cell(row, idx["price_buy"]))
        mkt = norm_price(cell(row, idx["price_mkt"]))
        for f in fields:
            cols[f].append(vals[f])
        cols["price_buy"].append(buy)
        cols["price_mkt"].append(mkt)
        cols["price_used"].append(buy if buy is not None else mkt)
        cols["src_row"].append(row_no)
        n += 1

    p25 = _out(cfg, "p25.parquet")
    write_parquet(
        cols,
        {**{f: "string" for f in fields}, "price_buy": "double", "price_mkt": "double",
         "price_used": "double", "src_row": "int64"},
        p25,
    )
    _log(f"p25: {n} 数据行 (跳过空行 {skipped_blank}) → {p25}")

    p_sql = _q(p25)
    f_sql = _q(_out(cfg, "final_main.parquet"))
    overlap, p_keys, p_only, price_used_null = con.execute(
        f"""
        WITH f AS (SELECT DISTINCT code, mall, sku FROM read_parquet('{f_sql}')),
             p AS (SELECT DISTINCT code, mall, sku FROM read_parquet('{p_sql}'))
        SELECT
          (SELECT count(*) FROM p JOIN f ON p.code=f.code AND p.mall=f.mall AND p.sku=f.sku),
          (SELECT count(*) FROM p),
          (SELECT count(*) FROM p LEFT JOIN f ON p.code=f.code AND p.mall=f.mall AND p.sku=f.sku WHERE f.code IS NULL),
          (SELECT count(*) FROM read_parquet('{p_sql}') WHERE price_used IS NULL)
        """
    ).fetchone()
    anomaly_top = con.execute(
        f"SELECT anomaly, count(*) c FROM read_parquet('{p_sql}') GROUP BY 1 ORDER BY c DESC LIMIT 10"
    ).fetchall()
    con.close()
    sec = {
        "rows": n,
        "skipped_blank_rows": skipped_blank,
        "expected_rows": 464075,
        "distinct_ckeys": p_keys,
        "ckey_overlap_with_final": overlap,
        "ckeys_not_in_final": p_only,
        "price_used_null": price_used_null,
        "anomaly_top10": [{"anomaly": a, "count": c} for a, c in anomaly_top],
        "src_file": path,
        "elapsed_sec": round(time.time() - t, 1),
    }
    stats["p25"] = sec
    print(f"  anomaly top10: {[(a, c) for a, c in anomaly_top]}", flush=True)
    print(f"  与 final ckey 重叠={overlap}, p25 独有键={p_only}, price_used 空={price_used_null}", flush=True)
    return sec


# ---------------------------------------------------------------------------
# 4. s3flags.parquet
# ---------------------------------------------------------------------------

def run_s3(cfg: dict, stats: dict) -> dict:
    t = _t0()
    con = connect_duckdb(cfg)
    path = cfg["s3_flags"]
    header, rows = xlsx_header_rows(path, "Sheet1")
    idx = column_index(header, S3_HEADERS)

    fields = ["code", "sku", "mall", "title", "url", "jz", "ywdm", "jd", "attr",
              "filtered", "match", "verdict2", "comment"]
    cols = {f: [] for f in fields}
    cols.update({"price": [], "src_row": []})
    n = 0
    skipped_blank = 0
    for row_no, row in rows:
        vals = {f: norm_cell(cell(row, idx[f])) for f in fields}
        if not any(vals.values()):
            skipped_blank += 1
            continue
        for f in fields:
            cols[f].append(vals[f])
        cols["price"].append(norm_price(cell(row, idx["price"])))
        cols["src_row"].append(row_no)
        n += 1

    s3 = _out(cfg, "s3flags.parquet")
    write_parquet(
        cols,
        {**{f: "string" for f in fields}, "price": "double", "src_row": "int64"},
        s3,
    )
    _log(f"s3flags: {n} 数据行 (跳过空行 {skipped_blank}) → {s3}")

    s_sql = _q(s3)
    f_sql = _q(_out(cfg, "final_main.parquet"))
    p_sql = _q(_out(cfg, "p25.parquet"))
    jz_cnt, ywdm_cnt, filtered_cnt = con.execute(
        f"""SELECT
              (SELECT count(*) FROM read_parquet('{s_sql}') WHERE jz='是'),
              (SELECT count(*) FROM read_parquet('{s_sql}') WHERE ywdm='是'),
              (SELECT count(*) FROM read_parquet('{s_sql}') WHERE filtered='是')"""
    ).fetchone()
    ov_f, ov_p, uniq, uniq_jz, uniq_ywdm, s_keys = con.execute(
        f"""
        WITH f AS (SELECT DISTINCT code, mall, sku FROM read_parquet('{f_sql}')),
             p AS (SELECT DISTINCT code, mall, sku FROM read_parquet('{p_sql}')),
             s AS (SELECT DISTINCT code, mall, sku FROM read_parquet('{s_sql}')),
             sr AS (SELECT code, mall, sku, jz, ywdm FROM read_parquet('{s_sql}'))
        SELECT
          (SELECT count(*) FROM s JOIN f ON s.code=f.code AND s.mall=f.mall AND s.sku=f.sku),
          (SELECT count(*) FROM s JOIN p ON s.code=p.code AND s.mall=p.mall AND s.sku=p.sku),
          (SELECT count(*) FROM sr LEFT JOIN f ON sr.code=f.code AND sr.mall=f.mall AND sr.sku=f.sku
                       LEFT JOIN p ON sr.code=p.code AND sr.mall=p.mall AND sr.sku=p.sku
                       WHERE f.code IS NULL AND p.code IS NULL),
          (SELECT count(*) FROM sr LEFT JOIN f ON sr.code=f.code AND sr.mall=f.mall AND sr.sku=f.sku
                       LEFT JOIN p ON sr.code=p.code AND sr.mall=p.mall AND sr.sku=p.sku
                       WHERE f.code IS NULL AND p.code IS NULL AND sr.jz='是'),
          (SELECT count(*) FROM sr LEFT JOIN f ON sr.code=f.code AND sr.mall=f.mall AND sr.sku=f.sku
                       LEFT JOIN p ON sr.code=p.code AND sr.mall=p.mall AND sr.sku=p.sku
                       WHERE f.code IS NULL AND p.code IS NULL AND sr.ywdm='是'),
          (SELECT count(*) FROM s)
        """
    ).fetchone()
    con.close()
    sec = {
        "rows": n,
        "skipped_blank_rows": skipped_blank,
        "expected_rows": 115627,
        "distinct_ckeys": s_keys,
        "jz_yes": jz_cnt,
        "ywdm_yes": ywdm_cnt,
        "filtered_yes": filtered_cnt,
        "ckey_overlap_with_final": ov_f,
        "ckey_overlap_with_p25": ov_p,
        "unique_keys_vs_final_p25": uniq,
        "unique_keys_jz_yes": uniq_jz,
        "unique_keys_ywdm_yes": uniq_ywdm,
        "src_file": path,
        "elapsed_sec": round(time.time() - t, 1),
    }
    stats["s3"] = sec
    print(
        f"  jz=是 {jz_cnt}, ywdm=是 {ywdm_cnt}, filtered=是 {filtered_cnt}; "
        f"重叠 final={ov_f}, p25={ov_p}, 独有键={uniq} (独有中 jz=是 {uniq_jz}, ywdm=是 {uniq_ywdm})",
        flush=True,
    )
    return sec


# ---------------------------------------------------------------------------
# 5. drops.parquet
# ---------------------------------------------------------------------------

DROP80_OUT_FIELDS = ["code", "mall", "price", "基准价格", "价格偏离率", "价格基准来源",
                     "价格核查结论", "价格问题说明"]
DROP_DOUBLE_FIELDS = {"price", "基准价格", "价格偏离率"}


def run_drops(cfg: dict, stats: dict) -> dict:
    t = _t0()
    con = connect_duckdb(cfg)

    # DROP80: 无 SKU 列（SPEC §3.5）, 仅 code+mall+价格+基准; F2 以 code+mall+price 匹配候选
    header, rows = xlsx_header_rows(cfg["drop80"], "Sheet1")
    idx = column_index(header, DROP80_HEADERS)
    cols = {k: [] for k in DROP80_OUT_FIELDS}
    cols.update({"sku": [], "reason": [], "source": []})
    n80 = 0
    blank80 = 0
    for _row_no, row in rows:
        vals = {k: norm_cell(cell(row, i)) for k, i in idx.items()}
        if not any(vals.values()):
            blank80 += 1
            continue
        for k in DROP80_OUT_FIELDS:
            if k in DROP_DOUBLE_FIELDS:
                cols[k].append(norm_price(cell(row, idx[k])))
            else:
                cols[k].append(vals[k])
        cols["sku"].append("")
        cols["reason"].append("")
        cols["source"].append("DROP80")
        n80 += 1

    # DROPNEW: uid 前 12 位=code（与 标准商品编码 列一致性计数）
    header, rows = xlsx_header_rows(cfg["drop_new"], "Sheet1")
    idx = column_index(header, DROPNEW_HEADERS)
    uid_idx = column_index(header, {"uid": "uid"})["uid"]
    nnew = 0
    uid_mismatch = 0
    blank_new = 0
    for _row_no, row in rows:
        code = norm_cell(cell(row, idx["code"]))
        sku = norm_cell(cell(row, idx["sku"]))
        mall = norm_cell(cell(row, idx["mall"]))
        price = norm_price(cell(row, idx["price"]))
        reason = norm_cell(cell(row, idx["reason"]))
        uid = norm_cell(cell(row, uid_idx))
        if not any((code, sku, mall, reason, uid)):
            blank_new += 1
            continue
        if len(uid) > 12 and uid[:12] != code:
            uid_mismatch += 1
        for k in DROP80_OUT_FIELDS:
            if k == "code":
                cols[k].append(code)
            elif k == "mall":
                cols[k].append(mall)
            elif k == "price":
                cols[k].append(price)
            elif k in DROP_DOUBLE_FIELDS:
                cols[k].append(None)
            else:
                cols[k].append("")
        cols["sku"].append(sku)
        cols["reason"].append(reason)
        cols["source"].append("DROPNEW")
        nnew += 1

    drops = _out(cfg, "drops.parquet")
    types = {k: "string" for k in cols}
    for k in DROP_DOUBLE_FIELDS:
        types[k] = "double"
    write_parquet(cols, types, drops)
    _log(f"drops: DROP80 {n80} 行 (空行{blank80}) + DROPNEW {nnew} 行 (空行{blank_new}, uid12≠code {uid_mismatch}) → {drops}")

    d_sql = _q(drops)
    per_source = dict(con.execute(f"SELECT source, count(*) FROM read_parquet('{d_sql}') GROUP BY 1").fetchall())
    distinct_codes = con.execute(f"SELECT count(DISTINCT code) FROM read_parquet('{d_sql}')").fetchone()[0]
    reason_top = con.execute(
        f"SELECT reason, count(*) c FROM read_parquet('{d_sql}') WHERE source='DROPNEW' GROUP BY 1 ORDER BY c DESC LIMIT 10"
    ).fetchall()
    con.close()
    sec = {
        "rows_total": n80 + nnew,
        "drop80_rows": n80,
        "drop80_expected_rows": 3057,
        "dropnew_rows": nnew,
        "dropnew_expected_rows": 4914,
        "dropnew_uid12_code_mismatch": uid_mismatch,
        "distinct_codes": distinct_codes,
        "dropnew_reason_top10": [{"reason": r, "count": c} for r, c in reason_top],
        "drop80_src": cfg["drop80"],
        "dropnew_src": cfg["drop_new"],
        "note": "DROP80 无 SKU 列(SPEC §3.5), F2 以 code+mall+price±0.01 匹配候选",
        "elapsed_sec": round(time.time() - t, 1),
    }
    stats["drops"] = sec
    print(f"  per_source={per_source}, distinct codes={distinct_codes}", flush=True)
    print(f"  DROPNEW 剔除原因 top10: {[(r, c) for r, c in reason_top]}", flush=True)
    return sec


# ---------------------------------------------------------------------------
# 6. dify.parquet
# ---------------------------------------------------------------------------

_DIFY_UID_RE = re.compile(r"^(\d*)(\d{12})$")
# 探针证实（0908 真实数据）: 唯一标识 = 可变长数字前缀(1~4位) + 12位标准商品编码
# 例: 1100000094495→'1'+'100000094495'; 92100000038308→'92'+'100000038308';
#     1234+12位同理。FINAL 120,047 个 code 全部为 12 位数字。


def run_dify(cfg: dict, stats: dict) -> dict:
    """唯一标识 = 可变长数字前缀(1~4位) + 12位标准商品编码, 取后 12 位为 code。"""
    t = _t0()
    con = connect_duckdb(cfg)
    header, rows = xlsx_header_rows(cfg["dify_recheck"], "Sheet0")
    idx = column_index(header, DIFY_HEADERS)

    cols = {"code": [], "title1": [], "title2": [], "result": [], "uid": []}
    n = 0
    unparsed = 0
    blank = 0
    for _row_no, row in rows:
        uid = norm_cell(cell(row, idx["uid"]))
        title1 = norm_cell(cell(row, idx["title1"]))
        title2 = norm_cell(cell(row, idx["title2"]))
        result = norm_cell(cell(row, idx["result"]))
        if not any((uid, title1, title2, result)):
            blank += 1
            continue
        m = _DIFY_UID_RE.match(uid)
        if m:
            code = m.group(2)
        else:
            code = ""
            unparsed += 1
        cols["code"].append(code)
        cols["title1"].append(title1)
        cols["title2"].append(title2)
        cols["result"].append(result)
        cols["uid"].append(uid)
        n += 1

    dify = _out(cfg, "dify.parquet")
    write_parquet(cols, {k: "string" for k in cols}, dify)
    _log(f"dify: {n} 数据行 (空行{blank}, 唯一标识无法解析 {unparsed}) → {dify}")

    d_sql = _q(dify)
    result_dist = dict(con.execute(
        f"SELECT result, count(*) FROM read_parquet('{d_sql}') GROUP BY 1 ORDER BY 2 DESC").fetchall())
    distinct_codes = con.execute(
        f"SELECT count(DISTINCT code) FROM read_parquet('{d_sql}') WHERE code<>''").fetchone()[0]
    sec = {
        "rows": n,
        "blank_rows_skipped": blank,
        "unparsed_uid": unparsed,
        "distinct_codes": distinct_codes,
        "result_distribution": result_dist,
        "src_file": cfg["dify_recheck"],
        "elapsed_sec": round(time.time() - t, 1),
    }
    fm = _out(cfg, "final_main.parquet")
    if os.path.exists(fm):
        codes_in_final = con.execute(
            f"""SELECT count(*) FROM (SELECT DISTINCT code FROM read_parquet('{_q(dify)}') WHERE code<>'')
                WHERE code IN (SELECT DISTINCT code FROM read_parquet('{_q(fm)}'))"""
        ).fetchone()[0]
        sec["distinct_codes_in_final"] = codes_in_final
    con.close()
    stats["dify"] = sec
    print(f"  result 分布: {result_dist}", flush=True)
    return sec


# ---------------------------------------------------------------------------
# 7. comments_final.parquet（SPEC §5 优先级: ZS > RECCSV > CVPOW > CVDEL > CVIDX）
# ---------------------------------------------------------------------------
# 命中语义（按 SPEC §5"取第一个命中"+四态定义）:
#   ZS:     行存在即命中（评论空→EMPTY_SRC）
#   RECCSV: 聚合键存在即命中（评论数空→EMPTY_SRC）
#   CVPOW:  评论匹配状态 != 'NO_RECALL_MATCH' 即命中（推荐评论数空→EMPTY_SRC）
#   CVDEL:  评论匹配状态 != 'NOT_FOUND' 即命中（推荐评论数空, 状态=FOUND_NO_VALUE → EMPTY_SRC）
#   CVIDX:  键存在即命中（评论数空→EMPTY_SRC）
# 空 SKU 行: 不查任何层, state=NO_KEY（计入 NOT_FOUND 的独立子类）

def _zs_layer(cfg: dict) -> dict:
    """ZS xlsx → {(mall, sku): (评论原文, src_file, src_row)}。

    探针证实: ZS 商品编码=平台SKU(非标品编码), 商城标识=商城(多值, 不只 suning);
    商品最小品类单元id 仅作 sku 缺失时回退（SPEC §3.7）。
    """
    header, rows = xlsx_header_rows(cfg["zs_result"], "合并数据")
    idx = column_index(header, ZS_HEADERS)
    src_file = os.path.basename(cfg["zs_result"])
    zs = {}
    collisions = 0
    n = 0
    unit_fallback = 0
    for row_no, row in rows:
        sku = norm_key(cell(row, idx["sku"]))
        if not sku:
            sku = norm_key(cell(row, idx["unit_id"]))
            unit_fallback += 1
        mall = norm_key(cell(row, idx["mall"]))
        if not sku and not mall:
            continue
        n += 1
        key = (mall, sku)
        if key in zs:
            collisions += 1
            # 同 (mall,sku) 多批: 保留已有值者, 否则保留后见者
            if norm_price(zs[key][0]) is not None:
                continue
        zs[key] = (norm_cell(cell(row, idx["comment"])), src_file, str(row_no))
    return {"map": zs, "rows": n, "collisions": collisions,
            "unit_id_fallback": unit_fallback, "src_file": src_file}


def _reccsv_layer(cfg: dict, con) -> dict:
    """RECCSV (2.18GB) duckdb 直读聚合: 按 (标准商品编码,交付商城,交付SKUID) 取 来源时间 最新行的评论数。

    只 duckdb 聚合出小结果（temp\\reccsv_agg.parquet）, 禁止整表载入/复制（SPEC §7）。
    平局确定性: 来源时间 DESC → 来源行号(数值) DESC → 来源文件 DESC。
    """
    csv_path = _q(cfg["recall_csv"])
    fm_sql = _q(_out(cfg, "final_main.parquet"))
    agg_path = _q(_out(cfg, "reccsv_agg.parquet"))
    con.execute(f"CREATE TABLE fk AS SELECT DISTINCT code, mall, sku FROM read_parquet('{fm_sql}')")
    con.execute(
        f"""
        COPY (
            SELECT code, mall, sku, comment_raw, src_file, src_row FROM (
                SELECT f.code, f.mall, f.sku,
                       r.评论数 AS comment_raw,
                       r.来源文件 AS src_file,
                       r.来源行号 AS src_row,
                       row_number() OVER (
                           PARTITION BY f.code, f.mall, f.sku
                           ORDER BY r.来源时间 DESC,
                                    TRY_CAST(r.来源行号 AS BIGINT) DESC NULLS LAST,
                                    r.来源文件 DESC
                       ) AS rn
                FROM read_csv('{csv_path}', header=true, all_varchar=true) r
                JOIN fk f ON r.标准商品编码 = f.code AND r.交付商城 = f.mall AND r.交付SKUID = f.sku
            ) WHERE rn = 1
        ) TO '{agg_path}' (FORMAT PARQUET)
        """
    )
    rows = con.execute(
        f"SELECT code, mall, sku, comment_raw, src_file, src_row FROM read_parquet('{agg_path}')"
    ).fetchall()
    m = {}
    for code, mall, sku, comment_raw, src_file, src_row in rows:
        m[(norm_key(code), norm_key(mall), norm_key(sku))] = (
            norm_cell(comment_raw), norm_cell(src_file), norm_cell(src_row))
    return {"map": m, "rows": len(rows), "agg_path": _out(cfg, "reccsv_agg.parquet")}


def _fk_table_sql(cfg: dict) -> str:
    fm_sql = _q(_out(cfg, "final_main.parquet"))
    return f"(SELECT DISTINCT code, mall, sku FROM read_parquet('{fm_sql}'))"


def run_comments(cfg: dict, stats: dict) -> dict:
    t = _t0()
    fm = _out(cfg, "final_main.parquet")
    if not os.path.exists(fm):
        _log("final_main.parquet 缺失, 先运行 final 抽取")
        run_final(cfg, stats)
    con = connect_duckdb(cfg)
    fm_sql = _q(fm)

    fk_rows = con.execute(
        f"SELECT code, mall, sku, src_row FROM read_parquet('{fm_sql}') ORDER BY src_row"
    ).fetchall()

    zs = _zs_layer(cfg)
    _log(f"comments[ZS]: {zs['rows']} 行 → {len(zs['map'])} 键 (碰撞 {zs['collisions']}, 单元id回退 {zs['unit_id_fallback']})")
    reccsv = _reccsv_layer(cfg, con)
    _log(f"comments[RECCSV]: 聚合 {reccsv['rows']} 键命中 FINAL")
    fk_sql = _fk_table_sql(cfg)

    cvpow_all = con.execute(
        f"""SELECT f.code, f.mall, f.sku, p.推荐评论数, p.评论匹配状态, p.评论来源文件, p.来源行号
            FROM read_parquet('{_q(cfg["power_match_parquet"])}') p
            JOIN {fk_sql} f ON p.标准商品编码=f.code AND p.SKUID=f.sku AND p.商城编码=f.mall"""
    ).fetchall()
    cvpow = {}
    cvpow_transparent = 0
    for code, mall, sku, val, st, sf, sr in cvpow_all:
        if st == "NO_RECALL_MATCH":
            cvpow_transparent += 1
            continue
        cvpow[(norm_key(code), norm_key(mall), norm_key(sku))] = (norm_cell(val), norm_cell(sf), norm_cell(sr))
    _log(f"comments[CVPOW]: 命中 {len(cvpow)} 键 (透明 NO_RECALL_MATCH {cvpow_transparent})")

    cvdel_all = con.execute(
        f"""SELECT f.code, f.mall, f.sku, d.推荐评论数, d.评论匹配状态, d.来源文件, d.来源行号
            FROM read_parquet('{_q(cfg["delivery_coverage_parquet"])}') d
            JOIN {fk_sql} f ON d.标准商品编码=f.code AND d.SKUID=f.sku AND d.商城编码=f.mall"""
    ).fetchall()
    cvdel = {}
    cvdel_transparent = 0
    for code, mall, sku, val, st, sf, sr in cvdel_all:
        if st == "NOT_FOUND":
            cvdel_transparent += 1
            continue
        cvdel[(norm_key(code), norm_key(mall), norm_key(sku))] = (norm_cell(val), norm_cell(sf), norm_cell(sr))
    _log(f"comments[CVDEL]: 命中 {len(cvdel)} 键 (透明 NOT_FOUND {cvdel_transparent})")

    cvidx_all = con.execute(
        f"""SELECT f.code, f.mall, f.sku, c.评论数, c.来源文件, c.来源行号
            FROM read_parquet('{_q(cfg["comment_index_parquet"])}') c
            JOIN {fk_sql} f ON c.标准商品编码=f.code AND c.SKUID=f.sku AND c.商城编码=f.mall
            ORDER BY c.来源mtime"""
    ).fetchall()
    cvidx = {}
    for code, mall, sku, val, sf, sr in cvidx_all:
        cvidx[(norm_key(code), norm_key(mall), norm_key(sku))] = (norm_cell(val), norm_cell(sf), norm_cell(sr))
    _log(f"comments[CVIDX]: 命中 {len(cvidx)} 键")

    zs_file = zs["src_file"]
    out_cols = {k: [] for k in ("code", "sku", "mall", "state", "comment_value",
                                "comment_src_layer", "comment_src_file", "comment_src_row")}
    state_counts = {"POSITIVE": 0, "ZERO": 0, "EMPTY_SRC": 0, "NOT_FOUND": 0, "NO_KEY": 0}
    layer_counts = {"ZS": 0, "RECCSV": 0, "CVPOW": 0, "CVDEL": 0, "CVIDX": 0, "NONE": 0}

    for code, mall, sku, _src_row in fk_rows:
        code, mall, sku = norm_key(code), norm_key(mall), norm_key(sku)
        hit = None
        if sku == "":
            state = "NO_KEY"
        else:
            z = zs["map"].get((mall, sku))
            if z is not None:
                hit = ("ZS", z[0], z[1], z[2])
            else:
                r = reccsv["map"].get((code, mall, sku))
                if r is not None:
                    hit = ("RECCSV", r[0], r[1], r[2])
                else:
                    p = cvpow.get((code, mall, sku))
                    if p is not None:
                        hit = ("CVPOW", p[0], p[1], p[2])
                    else:
                        d = cvdel.get((code, mall, sku))
                        if d is not None:
                            hit = ("CVDEL", d[0], d[1], d[2])
                        else:
                            c = cvidx.get((code, mall, sku))
                            if c is not None:
                                hit = ("CVIDX", c[0], c[1], c[2])
            if hit is None:
                state = "NOT_FOUND"
            else:
                v = norm_price(hit[1])
                if v is None:
                    state = "EMPTY_SRC"
                elif v > 0:
                    state = "POSITIVE"
                else:
                    state = "ZERO"
        out_cols["code"].append(code)
        out_cols["sku"].append(sku)
        out_cols["mall"].append(mall)
        out_cols["state"].append(state)
        out_cols["comment_value"].append(norm_price(hit[1]) if hit else None)
        out_cols["comment_src_layer"].append(hit[0] if hit else "NONE")
        out_cols["comment_src_file"].append(hit[2] if hit else "")
        out_cols["comment_src_row"].append(hit[3] if hit else "")
        state_counts[state] += 1
        layer_counts[hit[0] if hit else "NONE"] += 1

    cm = _out(cfg, "comments_final.parquet")
    write_parquet(
        out_cols,
        {"code": "string", "sku": "string", "mall": "string", "state": "string",
         "comment_value": "double", "comment_src_layer": "string",
         "comment_src_file": "string", "comment_src_row": "string"},
        cm,
    )
    con.close()
    not_found_total = state_counts["NOT_FOUND"] + state_counts["NO_KEY"]
    print(f"  四态分布: POSITIVE={state_counts['POSITIVE']}, ZERO={state_counts['ZERO']}, "
          f"EMPTY_SRC={state_counts['EMPTY_SRC']}, NOT_FOUND={state_counts['NOT_FOUND']} "
          f"(含 NO_KEY 子类 {state_counts['NO_KEY']}), NOT_FOUND 合计={not_found_total}", flush=True)
    print(f"  各层命中: {layer_counts}", flush=True)
    sec = {
        "rows": len(fk_rows),
        "state_counts": state_counts,
        "not_found_total": not_found_total,
        "layer_hits": layer_counts,
        "layers": {
            "ZS": {"rows": zs["rows"], "keys": len(zs["map"]), "collisions": zs["collisions"],
                   "unit_id_fallback": zs["unit_id_fallback"], "src_file": zs_file},
            "RECCSV": {"keys_hit_final": reccsv["rows"], "agg_path": reccsv["agg_path"]},
            "CVPOW": {"keys_hit_final": len(cvpow), "transparent_no_recall_match": cvpow_transparent},
            "CVDEL": {"keys_hit_final": len(cvdel), "transparent_not_found": cvdel_transparent},
            "CVIDX": {"keys_hit_final": len(cvidx)},
        },
        "hit_semantics": "ZS/RECCSV/CVIDX: 键存在即命中; CVPOW: 状态!=NO_RECALL_MATCH; "
                         "CVDEL: 状态!=NOT_FOUND; 命中但值空→EMPTY_SRC",
        "out_path": cm,
        "elapsed_sec": round(time.time() - t, 1),
    }
    stats["comments"] = sec
    return sec


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

STEPS = {
    "final": run_final,
    "problems": run_problems,
    "p25": run_p25,
    "s3": run_s3,
    "drops": run_drops,
    "dify": run_dify,
    "comments": run_comments,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="F1 抽取层（验收回溯重建）")
    ap.add_argument("--config", required=True, help="config.local.json 路径")
    ap.add_argument("--only", default="all",
                    choices=["final", "p25", "s3", "drops", "dify", "comments", "problems", "all"])
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    ensure_libs(cfg["libs_dir"])
    os.makedirs(cfg["temp_dir"], exist_ok=True)
    os.makedirs(os.path.join(cfg["workdir"], "inventory"), exist_ok=True)

    spath = _inventory_path(cfg)
    stats = {}
    if os.path.exists(spath):
        try:
            with open(spath, "r", encoding="utf-8") as f:
                stats = json.load(f)
        except Exception:
            stats = {}
    stats["run_meta"] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "only": args.only,
        "python": sys.version.split()[0],
        "script": os.path.abspath(__file__),
        "workdir": cfg["workdir"],
    }

    order = list(STEPS) if args.only == "all" else [args.only]
    for name in order:
        _log(f"=== step {name} ===")
        STEPS[name](cfg, stats)
        size = guard_disk_limit(cfg["temp_dir"])
        _log(f"=== step {name} done, temp 总量 {fmt_bytes(size)} ===")

    if args.only == "all":
        _log("=== 输入文件 sha256/size/mtime ===")
        stats["inputs"] = hash_inputs(cfg)

    outputs = {}
    for fn in OUTPUT_FILES:
        p = _out(cfg, fn)
        if os.path.exists(p):
            outputs[fn] = {"path": p, "size": os.path.getsize(p)}
    outputs["p1_stats.json"] = {"path": spath}
    stats["outputs"] = outputs
    stats["temp_dir_total_bytes"] = dir_size(cfg["temp_dir"])

    with open(spath, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    _log(f"p1_stats.json → {spath}")
    print("P1_EXTRACT_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
