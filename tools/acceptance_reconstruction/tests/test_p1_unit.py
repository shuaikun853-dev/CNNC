# -*- coding: utf-8 -*-
"""p1_extract 纯合成夹具单测（无网络、无真实大文件）。

用 openpyxl 在 pytest tmp_path 造 mini xlsx / csv / parquet，覆盖每个抽取器的
列映射、归一化、行数与 comments 优先级/四态逻辑。
"""
import csv as csvmod
import os

import pytest
import p1_extract as px
from p1_common import (
    column_index,
    connect_duckdb,
    norm_cell,
    norm_key,
    norm_price,
    sha256_file,
    write_parquet,
    xlsx_header_rows,
)

LIBS = r"E:\.A中核运维\_cnnc_comment_index\_libs"

# ---------------------------------------------------------------------------
# 夹具构建
# ---------------------------------------------------------------------------


def make_cfg(tmp_path):
    d = str(tmp_path)
    work = os.path.join(d, "work")
    return {
        "workdir": work,
        "temp_dir": os.path.join(work, "temp"),
        "final_authority": os.path.join(d, "fa_mini.xlsx"),
        "delivery_0825_parquet": os.path.join(d, "delpq_mini.parquet"),
        "p25_pool": os.path.join(d, "p25_mini.xlsx"),
        "s3_flags": os.path.join(d, "s3_mini.xlsx"),
        "drop80": os.path.join(d, "drop80_mini.xlsx"),
        "drop_new": os.path.join(d, "dropnew_mini.xlsx"),
        "dify_recheck": os.path.join(d, "dify_mini.xlsx"),
        "zs_result": os.path.join(d, "zs_mini.xlsx"),
        "recall_csv": os.path.join(d, "reccsv_mini.csv"),
        "comment_index_parquet": os.path.join(d, "cvidx_mini.parquet"),
        "delivery_coverage_parquet": os.path.join(d, "cvdel_mini.parquet"),
        "power_match_parquet": os.path.join(d, "cvpow_mini.parquet"),
        "libs_dir": LIBS,
    }


def make_xlsx(path, sheets):
    """sheets: {sheet_name: [header_row, data_rows...]}"""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(list(r))
    wb.save(path)


def read_parquet_rows(cfg, name):
    con = connect_duckdb(cfg)
    try:
        p = os.path.join(cfg["temp_dir"], name).replace("'", "''")
        return con.execute(f"SELECT * FROM read_parquet('{p}')").fetchall()
    finally:
        con.close()


def parquet_cols(cfg, name):
    con = connect_duckdb(cfg)
    try:
        p = os.path.join(cfg["temp_dir"], name).replace("'", "''")
        return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p}')").fetchall()]
    finally:
        con.close()


# 11 个 FINAL 数据行: (code, name, sku, mall, title, url, price, index_val, method)
FINAL_ROWS = [
    # row2: 空 sku → NO_KEY（mall 独占, 保持 (code,mall) 组内 ≤1 sku）
    ("100000011587", "罗马仕", "", "tmall", "标题1", "https://a/1", "0.69", "", "不计算"),
    # row3: 整行空白 → 跳过
    None,
    # row4: ZS 命中 POSITIVE
    ("100000200244", "格之格", 12409122618.0, "suning", "标题2", "https://a/2", 199, 150.5,
     "幂次归一化（评论数加权平均价）计算指导价"),
    # row5: ZS 命中但评论空 → EMPTY_SRC（ZS 优先于 RECCSV）
    ("100000011587", "罗马仕", "1102080295EA", "jd", "标题3", "https://a/3", "92", "", "不计算"),
    # row6: RECCSV 命中 POSITIVE
    ("100000019999", "杰思特", "SKU-X", "ehsy", "标题4", "https://a/4", 10, 10.0, "平均价计算指导价"),
    # row7: CVDEL ZERO
    ("100000017777", "品5", "CVDEL-1", "zjmi", "标题5", "https://a/5", 21.95, 20.0, "不计算"),
    # row8: CVDEL FOUND_NO_VALUE → EMPTY_SRC
    ("100000016666", "品6", "CVDEL-2", "zjmi", "标题6", "https://a/6", 22, 20.0, "不计算"),
    # row9: CVPOW COMMENT_MISSING → EMPTY_SRC（优先于 CVIDX 的 42）
    ("100000015555", "品7", "CVPOW-1", "jd", "标题7", "https://a/7", 24, 20.0, "不计算"),
    # row10: CVIDX POSITIVE 42（CVPOW 对该键透明 NO_RECALL_MATCH）
    ("100000014444", "品8", "CVIDX-1", "colipu", "标题8", "https://a/8", 26, 20.0, "不计算"),
    # row11: CVIDX 命中但评论空 → EMPTY_SRC
    ("100000013333", "品9", "CVIDX-2", "colipu", "标题9", "https://a/9", 28, 20.0, "不计算"),
    # row12: 全层未命中（CVDEL 有透明 NOT_FOUND 行）→ NOT_FOUND
    ("100000012222", "品10", "NF-1", "jd", "标题10", "https://a/10", 30, 20.0, "不计算"),
    # row13: 全层完全不存在 → NOT_FOUND
    ("100000011111", "品11", "CK-1", "comix", "标题11", "https://a/11", 32, 20.0, "不计算"),
]
N_FINAL = sum(1 for r in FINAL_ROWS if r is not None)


def build_final_env(tmp_path, cfg, delpq_sku_override=None):
    """mini FA（主表+4问题sheet）+ mini DELPQ parquet。返回 stats。"""
    main_rows = [tuple(px.FA_HEADERS.values())]
    for r in FINAL_ROWS:
        main_rows.append(r if r is not None else (None,) * 9)
    problem_sheets = {
        "01_4449无数据": [
            ("标准商品编码", "标准商品名称", "原问题"),
            ("100000011587", "罗马仕", "无数据"),
        ],
        "02_5.3%存疑同品": [
            ("原问题分类", "标准商品编码", "本次SKUID", "本次处理结果"),
            ("存疑同品", "100000011587", "1102080295EA", "当前已剔除"),
            ("存疑同品", "100000019999", "SKU-X", "保留"),
        ],
        "03_非同品原因": [
            ("原问题分类", "标准商品编码", "非同品原因分类"),
            ("非同品", "100000017777", "规格不同"),
        ],
        "04_34952存疑价格": [
            ("原问题分类", "标准商品编码", "本次价格处理结论"),
            ("存疑价格", "100000016666", "偏离超80%"),
        ],
    }
    sheets = {px.MAIN_SHEET: main_rows, "四项问题处理总结": [("标题", None, None)]}
    sheets.update(problem_sheets)
    make_xlsx(cfg["final_authority"], sheets)

    delpq_rows = []
    for r in FINAL_ROWS:
        if r is None:
            continue
        code, _name, sku, mall = r[0], r[1], norm_key(r[2]), r[3]
        sku = norm_key(sku)
        if delpq_sku_override and code == delpq_sku_override[0]:
            sku = delpq_sku_override[1]
        if sku == "":
            # 真实 delivery_0825.parquet 空 sku 为 SQL NULL（回归: 须 COALESCE 后比较）
            delpq_rows.append((code, mall, None, r[8]))
        else:
            delpq_rows.append((code, mall, sku, r[8]))
    write_parquet(
        {"code": [r[0] for r in delpq_rows], "mall": [r[1] for r in delpq_rows],
         "sku": [r[2] for r in delpq_rows], "method": [r[3] for r in delpq_rows]},
        {"code": "string", "mall": "string", "sku": "string", "method": "string"},
        cfg["delivery_0825_parquet"],
    )


@pytest.fixture()
def final_env(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    build_final_env(tmp_path, cfg)
    monkeypatch.setattr(px, "EXPECTED_MAIN_ROWS", N_FINAL)
    monkeypatch.setattr(px, "EXPECTED_DISTINCT_CODE", 10)
    monkeypatch.setattr(px, "EXPECTED_EMPTY_SKU", 1)
    return cfg


# ---------------------------------------------------------------------------
# p1_common 单元
# ---------------------------------------------------------------------------


def test_norm_cell():
    assert norm_cell(None) == ""
    assert norm_cell(21038747.0) == "21038747"  # float 整数去 .0
    assert norm_cell(12.5) == "12.5"
    assert norm_cell(100) == "100"
    assert norm_cell("  ab ") == "ab"
    assert norm_cell("") == ""


def test_norm_key_alias():
    assert norm_key(21038747.0) == norm_cell("21038747")


def test_norm_price():
    assert norm_price("95.19") == 95.19
    assert norm_price(None) is None
    assert norm_price("") is None
    assert norm_price(6750) == 6750.0
    assert norm_price("0.69") == 0.69
    assert norm_price("1,234.5") == 1234.5
    assert norm_price("abc") is None


def test_sha256_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"abc")
    assert sha256_file(str(p)) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_xlsx_header_rows_skips_blank_leading_row(tmp_path):
    p = str(tmp_path / "h.xlsx")
    make_xlsx(p, {"S": [(None, None), ("a", "b"), (1, 2), (3, 4)]})
    header, rows = xlsx_header_rows(p, "S")
    assert header == ["a", "b"]
    data = list(rows)
    assert [rn for rn, _ in data] == [3, 4]
    assert [r for _, r in data] == [(1, 2), (3, 4)]


def test_column_index_missing_raises():
    with pytest.raises(ValueError):
        column_index(["a", "b"], {"x": "c"})


# ---------------------------------------------------------------------------
# 1. final_main + 交叉校验
# ---------------------------------------------------------------------------


def test_extract_final_mini(final_env):
    cfg = final_env
    stats = {}
    sec = px.run_final(cfg, stats)
    assert sec["rows"] == N_FINAL
    assert sec["skipped_blank_rows"] == 1
    assert sec["cross_check"] == "PASS"
    rows = read_parquet_rows(cfg, "final_main.parquet")
    assert len(rows) == N_FINAL
    by_code = {r[0]: r for r in rows}
    # 列序: code,name,sku,mall,title,url,price_raw,index_val,method,price,src_row
    r = by_code["100000200244"]
    assert r[2] == "12409122618"  # float sku 去了 .0
    assert r[6] == "199" and abs(r[9] - 199.0) < 1e-9  # price_raw + price double
    assert r[10] == 4  # excel 行号（表头1 + 空行3）
    r = by_code["100000011587"]
    empty = [x for x in rows if x[0] == "100000011587" and x[2] == ""]
    assert len(empty) == 1
    assert empty[0][6] == "0.69" and abs(empty[0][9] - 0.69) < 1e-9
    assert parquet_cols(cfg, "final_main.parquet") == [
        "code", "name", "sku", "mall", "title", "url", "price_raw", "index_val",
        "method", "price", "src_row"]


def test_extract_final_crosscheck_fail(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    build_final_env(tmp_path, cfg, delpq_sku_override=("100000200244", "WRONG-SKU"))
    monkeypatch.setattr(px, "EXPECTED_MAIN_ROWS", N_FINAL)
    monkeypatch.setattr(px, "EXPECTED_DISTINCT_CODE", 10)
    monkeypatch.setattr(px, "EXPECTED_EMPTY_SKU", 1)
    with pytest.raises(SystemExit) as e:
        px.run_final(cfg, {})
    assert e.value.code == 2


# ---------------------------------------------------------------------------
# 2. final_problems
# ---------------------------------------------------------------------------


def test_extract_problems_mini(final_env):
    cfg = final_env
    stats = {}
    sec = px.run_problems(cfg, stats)
    assert sec["rows"] == 5
    assert sec["per_sheet"]["01_4449无数据"]["data_rows"] == 1
    assert sec["per_sheet"]["02_5.3%存疑同品"]["data_rows"] == 2
    assert sec["per_sheet"]["03_非同品原因"]["data_rows"] == 1
    assert sec["per_sheet"]["04_34952存疑价格"]["data_rows"] == 1
    rows = read_parquet_rows(cfg, "final_problems.parquet")
    cols = parquet_cols(cfg, "final_problems.parquet")
    assert cols[:2] == ["sheet", "row_no"]
    for c in ["标准商品编码", "原问题", "本次SKUID", "本次处理结果", "非同品原因分类", "本次价格处理结论"]:
        assert c in cols
    r01 = [r for r in rows if r[0] == "01_4449无数据"][0]
    d = dict(zip(cols, r01))
    assert d["标准商品编码"] == "100000011587"
    assert d["本次SKUID"] == ""  # 缺列补 ''
    r04 = [r for r in rows if r[0] == "04_34952存疑价格"][0]
    d = dict(zip(cols, r04))
    assert d["本次价格处理结论"] == "偏离超80%"
    assert d["原问题"] == ""


# ---------------------------------------------------------------------------
# 3. p25
# ---------------------------------------------------------------------------


def test_extract_p25_mini(final_env):
    cfg = final_env
    px.run_final(cfg, {})
    make_xlsx(cfg["p25_pool"], {"合并数据": [
        tuple(px.P25_HEADERS.values()),
        ("100000200244", "标品A", "suning", "12409122618", "匹配标题A", "https://p1", 95.19, 99.0, "正常"),
        ("100000099999", "标品X", "xx", "SKK-X", "匹配标题X", "https://p2", None, 12.5, "超价"),
        ("100000011587", "标品B", "jd", "1102080295EA", "匹配标题B", "https://p3", None, None, ""),
    ]})
    stats = {}
    sec = px.run_p25(cfg, stats)
    assert sec["rows"] == 3
    assert sec["ckey_overlap_with_final"] == 2
    assert sec["ckeys_not_in_final"] == 1
    assert sec["price_used_null"] == 1
    rows = read_parquet_rows(cfg, "p25.parquet")
    d = {(r[0], r[2], r[3]): r for r in rows}  # code, mall, sku
    # 列序: code,std_name,mall,sku,title,url,anomaly,price_buy,price_mkt,price_used,src_row
    assert abs(d[("100000200244", "suning", "12409122618")][9] - 95.19) < 1e-9  # 采购价优先
    assert abs(d[("100000099999", "xx", "SKK-X")][9] - 12.5) < 1e-9  # 无采购价→市场价
    assert d[("100000011587", "jd", "1102080295EA")][9] is None


# ---------------------------------------------------------------------------
# 4. s3flags
# ---------------------------------------------------------------------------


def test_extract_s3_mini(final_env):
    cfg = final_env
    px.run_final(cfg, {})
    make_xlsx(cfg["p25_pool"], {"合并数据": [tuple(px.P25_HEADERS.values())]})  # 空 p25（仅表头）
    px.run_p25(cfg, {})
    make_xlsx(cfg["s3_flags"], {"Sheet1": [
        tuple(px.S3_HEADERS.values()),
        ("100000200244", 12409122618.0, "suning", "标题", "https://s", 2453.4, "是", "否", "否", "否", "否", "是", "是", "5"),
        ("100000055555", "S1", "zjmi", "t", "https://u", None, "是", "是", "否", "否", "是", "是", "是", "0"),
        ("100000055556", "S2", "zjmi", "t", "https://u", None, "否", "否", "否", "否", "否", "否", "否", None),
    ]})
    stats = {}
    sec = px.run_s3(cfg, stats)
    assert sec["rows"] == 3
    assert sec["jz_yes"] == 2
    assert sec["ywdm_yes"] == 1
    assert sec["filtered_yes"] == 1
    assert sec["ckey_overlap_with_final"] == 1
    assert sec["ckey_overlap_with_p25"] == 0
    assert sec["unique_keys_vs_final_p25"] == 2
    assert sec["unique_keys_jz_yes"] == 1
    assert sec["unique_keys_ywdm_yes"] == 1


# ---------------------------------------------------------------------------
# 5. drops
# ---------------------------------------------------------------------------


def test_extract_drops_mini(final_env):
    cfg = final_env
    make_xlsx(cfg["drop80"], {"Sheet1": [
        ("表内行号", "标准商品编码", "标准商品名称", "互联网商城编码", "互联网商品名称",
         "互联网商品价格", "基准价格", "价格偏离率", "价格基准来源", "同品结论", "包规结论",
         "价格核查结论", "价格问题说明"),
        ("1196", "100000000931", "孩视宝", "zjmi", "名2", 6750, 3077.89, 1.193,
         "同标品可比报价中位数", "同品", "一致", "价格离群", "报价高于基准"),
    ]})
    make_xlsx(cfg["drop_new"], {"Sheet1": [
        ("uid", "标准商品编码", "标品标题", "互联网标题", "SKUID", "供应商编码", "店铺名称", "价格", "剔除原因"),
        ("100000010435100289696192", "100000010435", "得力", "得力SK109", "100289696192", "jd", None, "0.69", "占位价<=1元"),
        ("999999999999100287661793", "100000058488", "联塑", "联塑直通", "100287661793", "jd", None, "0.7", "占位价<=1元"),
    ]})
    stats = {}
    sec = px.run_drops(cfg, stats)
    assert sec["rows_total"] == 3
    assert sec["drop80_rows"] == 1
    assert sec["dropnew_rows"] == 2
    assert sec["dropnew_uid12_code_mismatch"] == 1  # 第二行 uid 前 12 位 ≠ 标准商品编码
    rows = read_parquet_rows(cfg, "drops.parquet")
    d80 = [r for r in rows if r[-1] == "DROP80"][0]
    d = dict(zip(parquet_cols(cfg, "drops.parquet"), d80))
    assert d["code"] == "100000000931" and d["mall"] == "zjmi"
    assert d["sku"] == "" and d["reason"] == ""  # DROP80 无 SKU 列
    assert abs(d["price"] - 6750.0) < 1e-9 and abs(d["基准价格"] - 3077.89) < 1e-9
    dn = {r[0]: dict(zip(parquet_cols(cfg, "drops.parquet"), r)) for r in rows if r[-1] == "DROPNEW"}
    assert dn["100000010435"]["sku"] == "100289696192"
    assert dn["100000010435"]["mall"] == "jd"
    assert abs(dn["100000010435"]["price"] - 0.69) < 1e-9
    assert dn["100000010435"]["reason"] == "占位价<=1元"


# ---------------------------------------------------------------------------
# 6. dify
# ---------------------------------------------------------------------------


def test_extract_dify_mini(final_env):
    cfg = final_env
    px.run_final(cfg, {})  # dify 统计需要 final_main.parquet 对照 code 集合
    make_xlsx(cfg["dify_recheck"], {"Sheet0": [
        tuple(px.DIFY_HEADERS.values()),
        ("1" + "100000011587", "标题1", "标题2", "是"),     # 1位前缀, ∈FINAL
        ("2" + "100000011587", "标题1", "标题3", "否"),     # 同码不同前缀
        ("92" + "100000013333", "t", "t", "是"),            # 2位前缀（真实0908格式）, ∈FINAL
        ("1234" + "100000038308", "t", "t", "不是"),         # 4位前缀, ∉FINAL
        ("BAD", "t", "t", "是"),                            # 坏标识
    ]})
    stats = {}
    sec = px.run_dify(cfg, stats)
    assert sec["rows"] == 5
    assert sec["unparsed_uid"] == 1
    assert sec["distinct_codes"] == 3
    assert sec["distinct_codes_in_final"] == 2  # 100000011587/100000013333 ∈FINAL
    assert sec["result_distribution"] == {"是": 3, "否": 1, "不是": 1}
    rows = read_parquet_rows(cfg, "dify.parquet")
    codes = {r[0] for r in rows}
    assert codes == {"100000011587", "100000013333", "100000038308", ""}  # 可变长前缀已去, 坏标识→''


# ---------------------------------------------------------------------------
# 7. comments 优先级 + 四态
# ---------------------------------------------------------------------------


def build_comment_layers(cfg):
    make_xlsx(cfg["zs_result"], {"合并数据": [
        tuple(px.ZS_HEADERS.values()),
        (12409122618.0, 12409122618.0, "suning", "5", "https://zs/1", "batch1"),
        ("1102080295EA", "1102080295EA", "jd", None, "https://zs/2", "batch1"),
    ]})
    with open(cfg["recall_csv"], "w", encoding="utf-8-sig", newline="") as f:
        w = csvmod.writer(f)
        w.writerow(["标准商品编码", "交付商城", "交付SKUID", "评论数", "来源文件", "来源行号", "来源时间"])
        w.writerow(["100000011587", "jd", "1102080295EA", "3", "old.csv", "10", "2026-08-01 00:00:00"])
        w.writerow(["100000011587", "jd", "1102080295EA", "9", "new.csv", "20", "2026-08-02 00:00:00"])
        w.writerow(["100000019999", "ehsy", "SKU-X", "7", "f.csv", "5", "2026-08-03 00:00:00"])
        w.writerow(["999999999999", "jd", "EXTRA", "1", "x.csv", "1", "2026-08-01 00:00:00"])  # ∉FINAL 应被剪枝
    write_parquet(
        {"标准商品编码": ["100000015555", "100000014444"],
         "SKUID": ["CVPOW-1", "CVIDX-1"],
         "商城编码": ["jd", "colipu"],
         "推荐评论数": [None, None],
         "评论匹配状态": ["COMMENT_MISSING", "NO_RECALL_MATCH"],
         "评论来源文件": ["pow.csv", "pow.csv"],
         "来源行号": [88, 89]},
        {"标准商品编码": "string", "SKUID": "string", "商城编码": "string", "推荐评论数": "int64",
         "评论匹配状态": "string", "评论来源文件": "string", "来源行号": "int64"},
        cfg["power_match_parquet"],
    )
    write_parquet(
        {"标准商品编码": ["100000017777", "100000016666", "100000012222"],
         "SKUID": ["CVDEL-1", "CVDEL-2", "NF-1"],
         "商城编码": ["zjmi", "zjmi", "jd"],
         "推荐评论数": [0, None, None],
         "评论匹配状态": ["ZERO", "FOUND_NO_VALUE", "NOT_FOUND"],
         "来源文件": ["del.csv", "del.csv", "del.csv"],
         "来源行号": [11, 12, 13]},
        {"标准商品编码": "string", "SKUID": "string", "商城编码": "string", "推荐评论数": "int64",
         "评论匹配状态": "string", "来源文件": "string", "来源行号": "int64"},
        cfg["delivery_coverage_parquet"],
    )
    write_parquet(
        {"标准商品编码": ["100000014444", "100000013333", "100000015555"],
         "SKUID": ["CVIDX-1", "CVIDX-2", "CVPOW-1"],
         "商城编码": ["colipu", "colipu", "jd"],
         "评论数": [42, None, 42],
         "来源文件": ["idx.csv", "idx.csv", "idx.csv"],
         "来源行号": [21, 22, 23],
         "来源mtime": ["2026-08-01 00:00:00", "2026-08-01 00:00:00", "2026-07-01 00:00:00"]},
        {"标准商品编码": "string", "SKUID": "string", "商城编码": "string", "评论数": "int64",
         "来源文件": "string", "来源行号": "int64", "来源mtime": "string"},
        cfg["comment_index_parquet"],
    )


def test_comments_priority_and_states(final_env):
    cfg = final_env
    px.run_final(cfg, {})
    build_comment_layers(cfg)
    stats = {}
    sec = px.run_comments(cfg, stats)
    assert sec["rows"] == N_FINAL
    assert sec["state_counts"] == {
        "POSITIVE": 3, "ZERO": 1, "EMPTY_SRC": 4, "NOT_FOUND": 2, "NO_KEY": 1}
    assert sec["not_found_total"] == 3  # NOT_FOUND 2 + NO_KEY 1
    assert sec["layer_hits"] == {"ZS": 2, "RECCSV": 1, "CVPOW": 1, "CVDEL": 2,
                                 "CVIDX": 2, "NONE": 3}
    con = connect_duckdb(cfg)
    try:
        p = os.path.join(cfg["temp_dir"], "comments_final.parquet").replace("'", "''")
        rows = con.execute(f"SELECT * FROM read_parquet('{p}')").fetchall()
    finally:
        con.close()
    d = {(r[0], r[2], r[1]): r for r in rows}  # code, mall, sku
    # 列序: code,sku,mall,state,comment_value,comment_src_layer,comment_src_file,comment_src_row
    r = d[("100000200244", "suning", "12409122618")]
    assert r[3] == "POSITIVE" and abs(r[4] - 5.0) < 1e-9 and r[5] == "ZS"
    r = d[("100000011587", "jd", "1102080295EA")]
    assert r[3] == "EMPTY_SRC" and r[4] is None and r[5] == "ZS"  # ZS 空值压过 RECCSV 9
    r = d[("100000019999", "ehsy", "SKU-X")]
    assert r[3] == "POSITIVE" and abs(r[4] - 7.0) < 1e-9 and r[5] == "RECCSV"
    assert r[6] == "f.csv" and r[7] == "5"  # 来源文件/行号透传
    r = d[("100000017777", "zjmi", "CVDEL-1")]
    assert r[3] == "ZERO" and r[4] == 0.0 and r[5] == "CVDEL"
    r = d[("100000016666", "zjmi", "CVDEL-2")]
    assert r[3] == "EMPTY_SRC" and r[5] == "CVDEL"  # FOUND_NO_VALUE 命中
    r = d[("100000015555", "jd", "CVPOW-1")]
    assert r[3] == "EMPTY_SRC" and r[5] == "CVPOW"  # COMMENT_MISSING 压过 CVIDX 42
    r = d[("100000014444", "colipu", "CVIDX-1")]
    assert r[3] == "POSITIVE" and abs(r[4] - 42.0) < 1e-9 and r[5] == "CVIDX"  # NO_RECALL_MATCH 透明
    r = d[("100000013333", "colipu", "CVIDX-2")]
    assert r[3] == "EMPTY_SRC" and r[5] == "CVIDX"
    r = d[("100000012222", "jd", "NF-1")]
    assert r[3] == "NOT_FOUND" and r[5] == "NONE"  # CVDEL NOT_FOUND 透明
    r = d[("100000011111", "comix", "CK-1")]
    assert r[3] == "NOT_FOUND"
    r = d[("100000011587", "tmall", "")]
    assert r[3] == "NO_KEY" and r[5] == "NONE"  # 空 SKU 不查层
    assert len(rows) == N_FINAL
