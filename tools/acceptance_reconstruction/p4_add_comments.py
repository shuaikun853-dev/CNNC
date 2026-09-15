# -*- coding: utf-8 -*-
"""p4_add_comments.py — F4 客户补充列层: 在 final_audit 01/02/03/05 的「互联网商品价格」后插入「评论数」列。

背景: 用户明确要求在 output_customer_final_audit 的 01/02/03/05 加评论数列
（覆盖 SPEC §4 "01 不放评论数" 的建议; 04 已含评论数, 不动; output_customer 原五件不动）。

数据来源（禁止无来源填数, 禁止使用 s3flags 非数字展示串如 '1万+'）:
  a) temp\\comments_final.parquet（418,291 FINAL 键: code,sku,mall,state,comment_value,…）
     — 05 主表全部行 与 01/02/03 的 is_final 行; state∈{POSITIVE,ZERO}→写 comment_value(整数);
       EMPTY_SRC/NOT_FOUND/NO_KEY→写空。
  b) 额外候选行（step parquet 中 is_final=False 的行, 含 01 的 P25/S3 独有键与否行）:
     先查 config.comment_index_parquet 精确键 (code,sku,mall)（评论数非空才算命中）;
     再查 zs_result.xlsx 的 (商城标识,商品编码)→评论 列（文本数字转 int, 非数字跳过）;
     两层都 miss→写空。

步骤:
  a. 备份 final_audit\\05 → temp\\05_backup_preComment.xlsx（已存在则跳过并打印）。
  b. 构建候选评论 map（duckdb 读 comment_index + openpyxl 读 zs_result）。
  c. 01/02/03: 用 p3_format.write_xlsx 流式重导出到 final_audit（列=原列集价格后插评论数）。
  d. 05: openpyxl 常规模式 load → insert_cols(8) → 表头复制G1样式 → 逐行写评论值 →
     列宽12/autofilter 扩展 → 保存覆盖; 内存不足(MemoryError/RSS>上限)→回退 write_only 重建
     6 sheet（主表加列, 其余 sheet 值+基础格式复制）。
  e. 验证（全 PASS 才 exit 0）:
     - 01/02/03 行数 545,163/497,997/478,677; 表头=旧+评论数@第8列; 敏感词 0;
       全量逐行与 step parquet+评论 map 一致（含 5 行抽样打印）。
     - 05: 6 sheet 名不变; 主表 418,291 行×10列 第8列='评论数'; 填充=POSITIVE+ZERO=160,310;
       其余 sheet 行列数与备份一致; 全 sheet 逐单元格值与备份流式比对一致。
     - FINAL_AUTHORITY 三件套 sha256 不变; workdir 总量 < 5GB。

硬红线: 只写 final_audit\\01/02/03/05 + temp\\05_backup_preComment.xlsx + 本脚本/测试;
不动 output_customer、04、历史源、FINAL 三件套; 禁 git。
"""
from __future__ import annotations

import argparse
import gc
import os
import shutil
import sys
import tempfile
import time
from copy import copy
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from p1_common import (  # noqa: E402
    connect_duckdb,
    dir_size,
    fmt_bytes,
    load_config,
    norm_cell,
    sha256_file,
)
from p3_format import (  # noqa: E402
    build_sensitive_pattern,
    convert_cell,
    customer_fields,
    customer_headers,
    customer_kinds,
    find_sensitive_hit,
    write_xlsx,
)

# ---------------------------------------------------------------------------
# 冻结常量
# ---------------------------------------------------------------------------

FINAL_AUDIT_DIR = "output_customer_final_audit"
BACKUP_05_NAME = "05_backup_preComment.xlsx"
COMMENTS_PARQUET = "comments_final.parquet"
FILE_05 = "05_价格指数计算结果数据.xlsx"
SHEET_05_MAIN = "最终处理结果"

#: 「评论数」插入位置 = 互联网商品价格（第7列）之后 → 新列第8列（1-based）
INSERT_AT_1BASED = 8
COMMENT_COL_0BASED = INSERT_AT_1BASED - 1
NEW_COL_WIDTH = 12.0

#: 01/02/03 行数冻结（与源 step parquet 一致）
STEP_DEFS = {
    "01": {"file": "01_同款校验结果数据.xlsx", "parquet": "step1.parquet",
           "sheet": "同款校验结果数据", "rows": 545163},
    "02": {"file": "02_去极值后结果数据.xlsx", "parquet": "step2.parquet",
           "sheet": "去极值后结果数据", "rows": 497997},
    "03": {"file": "03_一物多码去重后结果数据.xlsx", "parquet": "step3.parquet",
           "sheet": "一物多码去重后结果数据", "rows": 478677},
}

#: 05 六个 sheet 行/列冻结（行=含表头 max_row; 列=主表插列后 10）
SHEET_05_EXPECT_ROWS = {
    SHEET_05_MAIN: 418292,
    "四项问题处理总结": 24,
    "01_4449无数据": 12118,
    "02_5.3%存疑同品": 22701,
    "03_非同品原因": 3112,
    "04_34952存疑价格": 34953,
}
SHEET_05_EXPECT_COLS = {
    SHEET_05_MAIN: 10,
    "四项问题处理总结": 6,
    "01_4449无数据": 14,
    "02_5.3%存疑同品": 24,
    "03_非同品原因": 25,
    "04_34952存疑价格": 24,
}

#: 05 主表评论期望填充 = POSITIVE + ZERO = 160,310
EXPECTED_05_FILLED = 160310

#: FINAL_AUTHORITY 三件套 sha256 前缀（冻结锚点）
FINAL_SHA_PREFIX = {
    "final_authority": "24cdcd90",
    "final_authority_equiv": "91ad3471",
    "final_authority_orig": "24cdcd90",
}

WORKDIR_LIMIT_BYTES = 5 * 1024 ** 3   # workdir 总量上限 5GB
SCRATCH_DIR = ".tmp_xlsx_p4"          # openpyxl 临时目录重定向（禁写 C 盘）


class MemoryGateExceeded(Exception):
    """进程 RSS 超过上限（触发 05 回退重建路径）。"""


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _q(path: str) -> str:
    return path.replace("'", "''")


# ---------------------------------------------------------------------------
# 纯函数: 列规格 / 评论值解析
# ---------------------------------------------------------------------------

def headers_with_comment(step: str) -> list[str]:
    """原表头在互联网商品价格(第7列)后插入 评论数。"""
    orig = customer_headers(step)
    if orig[6] != "互联网商品价格":
        raise ValueError(f"step {step} 第7列不是互联网商品价格: {orig[6]!r}")
    return orig[:7] + ["评论数"] + orig[7:]


def kinds_with_comment(step: str) -> list[str]:
    """与 headers_with_comment 对齐的单元格类型（评论数=整数或空）。"""
    orig = customer_kinds(step)
    return orig[:7] + ["int"] + orig[7:]


def zs_comment_to_int(v) -> int | None:
    """zs_result 评论列值 → int 或 None（无法安全解析→None=跳过）。

    数字直接用; 文本数字('0'/'12')转 int; 非数字('500+'/'1万+'/空)→None。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        return int(round(v))
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return int(round(float(s)))
    except (ValueError, OverflowError):
        return None


def resolve_comment(is_final, code, sku, mall, final_map, cvidx_map, zs_map):
    """单行评论值解析。返回 (value:int|None, src:'FINAL'|'CVIDX'|'ZS'|'MISS')。

    - is_final 行: 只用 comments_final（a 链）; POSITIVE/ZERO→整数值;
      EMPTY_SRC/NOT_FOUND/NO_KEY→空; 不回退候选链。
    - 非 is_final 行（候选行/否行）: CVIDX → ZS → 空。
    """
    key = (norm_cell(code), norm_cell(sku), norm_cell(mall))
    if is_final:
        ent = final_map.get(key)
        if ent and ent[0] in ("POSITIVE", "ZERO") and ent[1] is not None:
            return int(round(ent[1])), "FINAL"
        return None, "MISS"
    v = cvidx_map.get(key)
    if v is not None:
        return v, "CVIDX"
    zv = zs_map.get((key[2], key[0]))  # zs 键=(商城标识, 商品编码)
    if zv is not None:
        return zv, "ZS"
    return None, "MISS"


def assemble_row(row: tuple, n_fields: int, comment) -> tuple:
    """parquet 行（fields… + is_final）→ 输出行元组（评论数插在第8列, 0-based 7）。"""
    return tuple(row[:7]) + (comment,) + tuple(row[7:n_fields])


# ---------------------------------------------------------------------------
# 评论 map 构建
# ---------------------------------------------------------------------------

def build_final_comment_map(con, parquet_path: str) -> dict:
    """comments_final.parquet → {(code,sku,mall): (state, comment_value|None)}。"""
    if not os.path.isfile(parquet_path):
        print(f"MISSING_INPUT: {parquet_path}", flush=True)
        sys.exit(2)
    rows = con.execute(
        f"SELECT code, sku, mall, state, comment_value FROM '{_q(parquet_path)}'"
    ).fetchall()
    out = {}
    for code, sku, mall, state, val in rows:
        out[(norm_cell(code), norm_cell(sku), norm_cell(mall))] = (state, val)
    return out


def fetch_candidate_keys(con, step_parquets: list[str]) -> list[tuple]:
    """所有 step 中 is_final=False 的 distinct (code,sku,mall)（含 01 否行/P25/S3 独有键）。"""
    union = " UNION ".join(
        f"SELECT DISTINCT code, sku, mall FROM '{_q(p)}' WHERE NOT is_final"
        for p in step_parquets)
    return [(r[0], r[1], r[2]) for r in con.execute(union).fetchall()]


def build_cvidx_map(con, cand_keys: list[tuple], ci_parquet: str) -> dict:
    """comment_index 精确键 (code,sku,mall) → 评论数 int; 评论数为 NULL 的键不算命中。"""
    if not cand_keys:
        return {}
    import pyarrow as pa

    tbl = pa.table({
        "code": [k[0] for k in cand_keys],
        "sku": [k[1] for k in cand_keys],
        "mall": [k[2] for k in cand_keys],
    })
    con.register("p4_cand_keys", tbl)
    try:
        rows = con.execute(
            f"SELECT k.code, k.sku, k.mall, ci.评论数 "
            f"FROM p4_cand_keys k JOIN '{_q(ci_parquet)}' ci "
            f"ON k.code = ci.标准商品编码 AND k.sku = ci.SKUID AND k.mall = ci.商城编码 "
            f"WHERE ci.评论数 IS NOT NULL"
        ).fetchall()
    finally:
        con.unregister("p4_cand_keys")
    return {(r[0], r[1], r[2]): int(round(r[3])) for r in rows}


def build_zs_map(zs_xlsx: str) -> dict:
    """zs_result.xlsx → {(商城标识, 商品编码): 评论int}; 非数字/空评论不入 map。

    小表全载（openpyxl read_only 流式扫一遍）。
    """
    from openpyxl import load_workbook

    wb = load_workbook(zs_xlsx, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [norm_cell(h) for h in next(it)]
        i_code = header.index("商品编码")
        i_mall = header.index("商城标识")
        i_cmt = header.index("评论")
        out = {}
        for row in it:
            code = norm_cell(row[i_code] if i_code < len(row) else None)
            mall = norm_cell(row[i_mall] if i_mall < len(row) else None)
            v = zs_comment_to_int(row[i_cmt] if i_cmt < len(row) else None)
            if code and mall and v is not None:
                out[(mall, code)] = v
        return out
    finally:
        wb.close()


def build_candidate_maps(con, cfg: dict, temp_dir: str) -> tuple[dict, dict, int]:
    """候选两层 map: (cvidx_map, zs_map, 候选键数)。"""
    step_parquets = [os.path.join(temp_dir, STEP_DEFS[s]["parquet"]) for s in ("01", "02", "03")]
    for p in step_parquets:
        if not os.path.isfile(p):
            print(f"MISSING_INPUT: {p}", flush=True)
            sys.exit(2)
    cand_keys = fetch_candidate_keys(con, step_parquets)
    cvidx_map = build_cvidx_map(con, cand_keys, cfg["comment_index_parquet"])
    zs_map = build_zs_map(cfg["zs_result"])
    return cvidx_map, zs_map, len(cand_keys)


# ---------------------------------------------------------------------------
# 01/02/03 重导出（p3_format 流式写出器）
# ---------------------------------------------------------------------------

def export_step_with_comments(con, step: str, temp_dir: str, out_path: str,
                              final_map: dict, cvidx_map: dict, zs_map: dict) -> dict:
    """重导出单个客户文件到 out_path（评论数插在第8列）。返回 {填充统计, sheets}。"""
    d = STEP_DEFS[step]
    parquet = os.path.join(temp_dir, d["parquet"])
    fields = customer_fields(step)
    n_fields = len(fields)
    sql = f"SELECT {', '.join(fields)}, is_final FROM '{_q(parquet)}'"
    stats = {"FINAL": 0, "CVIDX": 0, "ZS": 0, "MISS": 0}

    def _rows():
        cur = con.execute(sql)
        while True:
            batch = cur.fetchmany(50_000)
            if not batch:
                break
            for row in batch:
                v, src = resolve_comment(row[n_fields], row[0], row[2], row[3],
                                         final_map, cvidx_map, zs_map)
                stats[src] += 1
                yield assemble_row(row, n_fields, v)

    sheets = [{"name": d["sheet"], "headers": headers_with_comment(step),
               "kinds": kinds_with_comment(step), "rows": _rows()}]
    written = write_xlsx(out_path, sheets)
    return {"stats": stats, "written": written}


# ---------------------------------------------------------------------------
# 05 处理
# ---------------------------------------------------------------------------

def backup_file(src: str, dst: str) -> bool:
    """备份（唯一副本）; 已存在则跳过。返回是否新建。"""
    if os.path.isfile(dst):
        _log(f"备份已存在, 跳过: {dst} ({os.path.getsize(dst)}B)")
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    _log(f"已备份: {src} → {dst} ({os.path.getsize(dst)}B)")
    return True


def get_rss_bytes():
    """当前进程 WorkingSet（Windows）; 不可用时返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return None
        return int(pmc.WorkingSetSize)
    except Exception:
        return None


def _rss_gate(limit_bytes, where: str) -> None:
    if limit_bytes is None:
        return
    rss = get_rss_bytes()
    if rss is not None and rss > limit_bytes:
        raise MemoryGateExceeded(f"RSS={fmt_bytes(rss)} > {fmt_bytes(limit_bytes)} @ {where}")


def shift_column_dimensions(ws, insert_at: int, new_width: float) -> None:
    """insert_cols 不会移动 column_dimensions → 手动右移 >= insert_at 的列宽, 新列设宽。"""
    from openpyxl.utils import column_index_from_string, get_column_letter

    moved = []
    for letter in list(ws.column_dimensions.keys()):
        idx = column_index_from_string(letter)
        if idx >= insert_at:
            moved.append((idx, ws.column_dimensions[letter]))
            del ws.column_dimensions[letter]
    for idx, dim in sorted(moved, key=lambda t: -t[0]):
        nl = get_column_letter(idx + 1)
        nd = copy(dim)
        nd.min = nd.max = idx + 1
        nd.index = nl
        ws.column_dimensions[nl] = nd
    nd = ws.column_dimensions[get_column_letter(insert_at)]
    nd.width = new_width  # customWidth 由 width 非 None 自动派生


def apply_comment_column_normal(ws, final_map: dict, rss_limit=None, progress=None) -> dict:
    """常规模式在 ws（05 主表, 9列）插入评论数列并填值。返回 {filled, empty, rows}。"""
    from openpyxl.utils import get_column_letter

    ws.insert_cols(INSERT_AT_1BASED)
    shift_column_dimensions(ws, INSERT_AT_1BASED, NEW_COL_WIDTH)

    # 表头: 复制第7列表头单元格样式（_style 含字体/填充/对齐/边框/格式）
    src = ws.cell(1, 7)
    dst = ws.cell(1, INSERT_AT_1BASED)
    dst.value = "评论数"
    dst._style = copy(src._style)

    # autofilter 扩展到新列（Excel 插列行为）
    if ws.auto_filter.ref:
        ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"

    filled = empty = 0
    max_row = ws.max_row
    for r in range(2, max_row + 1):
        ent = final_map.get((norm_cell(ws.cell(r, 1).value),
                             norm_cell(ws.cell(r, 3).value),
                             norm_cell(ws.cell(r, 4).value)))
        if ent and ent[0] in ("POSITIVE", "ZERO") and ent[1] is not None:
            ws.cell(r, INSERT_AT_1BASED).value = int(round(ent[1]))
            filled += 1
        else:
            empty += 1
        if progress and r % 50_000 == 0:
            progress(r, filled, empty)
            _rss_gate(rss_limit, f"05 写值 row={r}")
    _rss_gate(rss_limit, "05 写值完成")
    return {"filled": filled, "empty": empty, "rows": max_row - 1}


def process_05_normal(src_path: str, out_tmp: str, final_map: dict,
                      rss_limit=None) -> dict:
    """常规模式: load → 插列 → 填值 → 另存 out_tmp。"""
    from openpyxl import load_workbook

    _rss_gate(rss_limit, "05 加载前")
    wb = load_workbook(src_path, read_only=False, data_only=False)  # 保留公式
    try:
        if SHEET_05_MAIN not in wb.sheetnames:
            raise ValueError(f"05 缺少主表 sheet: {SHEET_05_MAIN}")
        ws = wb[SHEET_05_MAIN]
        if ws.max_column >= INSERT_AT_1BASED and \
                str(ws.cell(1, INSERT_AT_1BASED).value).strip() == "评论数":
            raise ValueError("05 主表第8列已是评论数（疑似重复运行）")
        stats = apply_comment_column_normal(
            ws, final_map, rss_limit=rss_limit,
            progress=lambda r, f, e: _log(f"  05 写值 {r}: filled={f} empty={e}"))
        _rss_gate(rss_limit, "05 保存前")
        wb.save(out_tmp)
        return stats
    finally:
        wb.close()


def rebuild_05_write_only(src_path: str, out_path: str, final_map: dict) -> dict:
    """回退方案: write_only 重建 6 sheet（主表加评论数列+基础格式; 其余 sheet 值+基础格式复制）。

    基础格式 = 表头(微软雅黑11粗体/深蓝底/白字/居中) + freeze + autofilter + 主表列类型格式
    （p3_format.convert_cell）。注意: 不复制逐单元格富样式/合并单元格（仅完整兜底用）。
    """
    from openpyxl import Workbook, load_workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb_in = load_workbook(src_path, read_only=True, data_only=False)
    wb_out = Workbook(write_only=True)
    try:
        stats = {"filled": 0, "empty": 0, "rows": 0}
        header_font = Font(name="微软雅黑", size=11, bold=True, color="FFFFFFFF")
        header_fill = PatternFill(fill_type="solid", fgColor="FF1F4E78")
        header_align = Alignment(horizontal="center")

        for name in wb_in.sheetnames:
            ws_in = wb_in[name]
            ws_out = wb_out.create_sheet(title=name)
            is_main = (name == SHEET_05_MAIN)
            in_max_row = ws_in.max_row or 0
            in_max_col = ws_in.max_column or 0
            out_ncols = in_max_col + 1 if is_main else in_max_col
            kinds = customer_kinds("05") if is_main else None  # 10 列, 评论数在 index 7
            freeze = getattr(ws_in, "freeze_panes", None)
            if freeze is None and not is_main:
                freeze = "A2"
            if freeze:
                ws_out.freeze_panes = freeze
            ws_out.auto_filter.ref = f"A1:{get_column_letter(out_ncols)}{in_max_row}"

            header_written = False
            nrows = 0
            for row in ws_in.iter_rows(values_only=True):
                vals = list(row)
                if not header_written:
                    if is_main:
                        vals = vals[:7] + ["评论数"] + vals[7:]
                    cells = []
                    for v in vals:
                        c = WriteOnlyCell(ws_out, v)
                        c.font = copy(header_font)
                        c.fill = copy(header_fill)
                        c.alignment = copy(header_align)
                        cells.append(c)
                    ws_out.append(cells)
                    header_written = True
                    continue
                nrows += 1
                if is_main:
                    ent = final_map.get((norm_cell(vals[0]), norm_cell(vals[2]),
                                         norm_cell(vals[3])))
                    if ent and ent[0] in ("POSITIVE", "ZERO") and ent[1] is not None:
                        cv = int(round(ent[1]))
                        stats["filled"] += 1
                    else:
                        cv = None
                        stats["empty"] += 1
                    out_vals = vals[:7] + [cv] + vals[7:9]
                    cells = []
                    for i, kind in enumerate(kinds):
                        v = out_vals[i] if i < len(out_vals) else None
                        val, fmt = convert_cell(kind, v)
                        if val is None:
                            cells.append(None)
                        elif fmt is None:
                            cells.append(val)
                        else:
                            c = WriteOnlyCell(ws_out, val)
                            c.number_format = fmt
                            cells.append(c)
                    ws_out.append(cells)
                else:
                    ws_out.append(list(vals))
            if is_main:
                stats["rows"] = nrows
        wb_out.save(out_path)
        return stats
    finally:
        wb_out.close()
        # write_only wb_out 无需 close（save 后即弃）


def _05_already_done(out_final: str) -> bool:
    """05 主表第8列已是「评论数」→ 重复运行保护。"""
    from openpyxl import load_workbook

    wb = load_workbook(out_final, read_only=True, data_only=True)
    try:
        if SHEET_05_MAIN not in wb.sheetnames:
            return False
        ws = wb[SHEET_05_MAIN]
        row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        return bool(row) and len(row) >= INSERT_AT_1BASED and \
            str(row[COMMENT_COL_0BASED]).strip() == "评论数"
    finally:
        wb.close()


def process_05(cfg: dict, final_map: dict, backup_path: str, out_final: str,
               tmp_dir: str, rss_limit) -> tuple[str, dict]:
    """05 主流程: 常规模式优先, 内存不足回退 write_only 重建。返回 (模式, stats)。"""
    out_tmp = os.path.join(tmp_dir, "p4_05_new.xlsx")
    try:
        stats = process_05_normal(out_final, out_tmp, final_map, rss_limit=rss_limit)
        mode = "normal"
    except (MemoryError, MemoryGateExceeded) as e:
        _log(f"WARN: 05 常规模式失败（{type(e).__name__}: {e}）→ 回退 write_only 重建")
        gc.collect()
        if os.path.isfile(out_tmp):
            os.remove(out_tmp)
        stats = rebuild_05_write_only(backup_path, out_tmp, final_map)
        mode = "write_only_rebuild"
    os.replace(out_tmp, out_final)
    return mode, stats


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------

def _cells_eq(a, b) -> bool:
    """单元格值相等（NaN==NaN; 其余 ==; None==None）。"""
    if isinstance(a, float) and isinstance(b, float):
        if a != a and b != b:
            return True
        return a == b
    if isinstance(a, float) and a != a:
        return b is None or (isinstance(b, str) and b.strip().lower() == "nan")
    if isinstance(b, float) and b != b:
        return a is None or (isinstance(a, str) and a.strip().lower() == "nan")
    return a == b


def verify_steps(cfg: dict, con, final_map: dict, cvidx_map: dict, zs_map: dict) -> list[dict]:
    """01/02/03: 行数/表头/敏感词/全量逐行值（含评论 vs map）+ 5 行抽样。"""
    from openpyxl import load_workbook

    fa_dir = os.path.join(cfg["workdir"], FINAL_AUDIT_DIR)
    pattern = build_sensitive_pattern()
    sample_idx = {0, 99_999, 249_999, 399_999}
    checks = []
    for step in ("01", "02", "03"):
        d = STEP_DEFS[step]
        path = os.path.join(fa_dir, d["file"])
        parquet = os.path.join(cfg["temp_dir"], d["parquet"])
        fields = customer_fields(step)
        n_fields = len(fields)
        kinds = kinds_with_comment(step)
        exp_headers = headers_with_comment(step)
        sql = f"SELECT {', '.join(fields)}, is_final FROM '{_q(parquet)}'"
        src_rows = con.execute(f"SELECT COUNT(*) FROM '{_q(parquet)}'").fetchone()[0]

        wb = load_workbook(path, read_only=True, data_only=True)
        n_data = 0
        mismatches = []
        hits = []
        samples = []
        try:
            if wb.sheetnames != [d["sheet"]]:
                mismatches.append(f"sheet名={wb.sheetnames}")
            ws = wb.worksheets[0]
            if find_sensitive_hit(pattern, ws.title):
                hits.append(f"sheet名:{ws.title}")
            # 流式并行比对（单次迭代: 表头 + 数据行）
            xiter = ws.iter_rows(values_only=True)
            header = next(xiter, None)
            if list(header or []) != exp_headers:
                mismatches.append(f"表头={list(header or [])}")
            else:
                for h in header:
                    w = find_sensitive_hit(pattern, h)
                    if w:
                        hits.append(f"表头:{h}")

            def _piter():
                cur = con.execute(sql)
                while True:
                    batch = cur.fetchmany(50_000)
                    if not batch:
                        break
                    yield from batch

            piter = _piter()
            for xrow in xiter:
                prow = next(piter, None)
                if prow is None:
                    mismatches.append(f"行{n_data + 1}: xlsx 多出行")
                    break
                n_data += 1
                expected = list(prow[:7])
                v, src = resolve_comment(prow[n_fields], prow[0], prow[2], prow[3],
                                         final_map, cvidx_map, zs_map)
                expected.insert(7, v)
                expected += list(prow[7:n_fields])
                for i, (xv, ev) in enumerate(zip(xrow, expected)):
                    ev_c = convert_cell(kinds[i], ev)[0]
                    if not _cells_eq(xv, ev_c):
                        if len(mismatches) < 10:
                            mismatches.append(f"行{n_data}列{i + 1}: xlsx={xv!r} 期望={ev_c!r}")
                for xv in xrow:
                    w = find_sensitive_hit(pattern, xv)
                    if w:
                        hits.append(f"行{n_data}:{w}")
                if n_data - 1 in sample_idx or n_data == src_rows:
                    # read_only 行尾空单元格会被裁剪 → 评论数列可能越界
                    cmt = xrow[7] if len(xrow) > 7 else None
                    samples.append((n_data, xrow[0], xrow[2], xrow[3], cmt))
            if next(piter, None) is not None:
                mismatches.append("parquet 多出行")
        finally:
            wb.close()

        ok = (n_data == d["rows"] == src_rows and not mismatches and not hits)
        checks.append({"name": f"{d['file']} 行数/表头/逐行值/敏感词",
                       "pass": ok,
                       "detail": (f"行数={n_data}(期望{d['rows']}, 源{src_rows}) "
                                  f"mismatch={len(mismatches)} 敏感词={len(hits)}; "
                                  f"{mismatches[:3] if mismatches else ''}"),
                       "samples": samples})
    return checks


def _val_eq(a, b) -> bool:
    """值相等; None 与 '' 视为等价。

    说明: openpyxl 载入→保存往返会把「空字符串单元格」规范化为「空单元格」
    （05 备份主表 F 列有 7 处 ''）, 内容语义不变, 比对按等价处理。
    """
    if _cells_eq(a, b):
        return True
    if a is None and isinstance(b, str) and b == "":
        return True
    if b is None and isinstance(a, str) and a == "":
        return True
    return False


def verify_05(cfg: dict, backup_path: str, final_map: dict) -> list[dict]:
    """05: sheet 名/行列数/表头/填充数/全 sheet 与备份逐单元格流式比对。"""
    from openpyxl import load_workbook

    out_final = os.path.join(cfg["workdir"], FINAL_AUDIT_DIR, FILE_05)
    checks = []
    wb_new = load_workbook(out_final, read_only=True, data_only=False)
    wb_old = load_workbook(backup_path, read_only=True, data_only=False)
    try:
        # 1) sheet 名不变 + 行列数
        names_ok = wb_new.sheetnames == wb_old.sheetnames == list(SHEET_05_EXPECT_ROWS)
        rows_cols_detail = []
        dims_ok = names_ok
        for name in wb_new.sheetnames:
            rn, cn = wb_new[name].max_row, wb_new[name].max_column
            er, ec = SHEET_05_EXPECT_ROWS.get(name), SHEET_05_EXPECT_COLS.get(name)
            ro, co = wb_old[name].max_row, wb_old[name].max_column
            ok = (rn == er and cn == ec)
            dims_ok = dims_ok and ok
            rows_cols_detail.append(f"{name}:{rn}x{cn}(期望{er}x{ec};备份{ro}x{co})")

        # 2) 逐单元格流式比对（主表含评论数列核验）
        filled = empty = 0
        n_main = 0
        main_header = None
        mism = []
        filled_ok = None
        for name in wb_new.sheetnames:
            ws_new, ws_old = wb_new[name], wb_old[name]
            it_new = ws_new.iter_rows(values_only=True)
            it_old = ws_old.iter_rows(values_only=True)
            is_main = (name == SHEET_05_MAIN)
            if is_main:
                main_header = next(it_new, None)
                old_header = next(it_old, None)
                exp_header = list(old_header or [])[:7] + ["评论数"] + list(old_header or [])[7:]
                if list(main_header or []) != exp_header:
                    mism.append("主表表头不符")
            for rnew, rold in zip(it_new, it_old):
                lnew, lold = list(rnew), list(rold)
                if is_main:
                    n_main += 1
                    cv = lnew[7] if len(lnew) > 7 else None
                    if cv is None:
                        empty += 1
                    else:
                        filled += 1
                    expect_new = lold[:7] + [cv] + lold[7:9]
                    # 评论值必须与 map 一致
                    ent = final_map.get((norm_cell(lold[0]), norm_cell(lold[2]),
                                         norm_cell(lold[3])))
                    exp_cv = int(round(ent[1])) if ent and ent[0] in ("POSITIVE", "ZERO") \
                        and ent[1] is not None else None
                    if not (cv is None and exp_cv is None) and cv != exp_cv:
                        if len(mism) < 10:
                            mism.append(f"主表行{n_main + 1} 评论={cv!r} 期望={exp_cv!r}")
                else:
                    expect_new = lold
                if len(lnew) != len(expect_new) or not all(
                        _val_eq(a, b) for a, b in zip(lnew, expect_new)):
                    if len(mism) < 10:
                        mism.append(f"{name} 行值不符(首异行见前)")
        filled_ok = (filled == EXPECTED_05_FILLED)
        if n_main != 418291:
            mism.append(f"主表数据行={n_main}")

        checks.append({"name": "05 sheet名/行列数与冻结值一致", "pass": names_ok and dims_ok,
                       "detail": "; ".join(rows_cols_detail)})
        checks.append({"name": "05 主表 418291行×10列 第8列=评论数",
                       "pass": bool(main_header) and len(main_header) == 10
                       and main_header[7] == "评论数" and n_main == 418291,
                       "detail": f"表头第8列={main_header[7] if main_header else None}, 数据行={n_main}"})
        checks.append({"name": "05 评论填充=160310(POSITIVE+ZERO)",
                       "pass": bool(filled_ok), "detail": f"filled={filled} empty={empty}"})
        checks.append({"name": "05 全 sheet 逐单元格值与备份一致(含价格指数/方式列; ''≡空单元格)",
                       "pass": not mism, "detail": f"mismatch={len(mism)}; {mism[:3]}"})
    finally:
        wb_new.close()
        wb_old.close()
    return checks


def verify_final_sha(cfg: dict) -> dict:
    """FINAL 三件套 sha256 与 p1_stats.inputs 及冻结前缀一致。"""
    stats_path = os.path.join(cfg["workdir"], "inventory", "p1_stats.json")
    with open(stats_path, "r", encoding="utf-8") as f:
        p1 = json_load(stats_path)
    items = []
    ok = True
    for key, prefix in FINAL_SHA_PREFIX.items():
        actual = sha256_file(cfg[key])
        expect = (p1.get("inputs", {}).get(key, {}) or {}).get("sha256", "")
        same = (actual == expect) and actual.startswith(prefix)
        ok = ok and same
        items.append(f"{key}={actual[:16]}{'OK' if same else 'MISMATCH'}")
    return {"name": "FINAL_AUTHORITY 三件套 sha256 不变", "pass": ok,
            "detail": "; ".join(items)}


def json_load(path: str) -> dict:
    import json
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def verify_disk(cfg: dict) -> dict:
    """workdir 总量 < 5GB 并打印。"""
    total = dir_size(cfg["workdir"])
    ok = total <= WORKDIR_LIMIT_BYTES
    return {"name": "workdir 总量 < 5GB", "pass": ok,
            "detail": f"total={fmt_bytes(total)} ({total}B), 上限={fmt_bytes(WORKDIR_LIMIT_BYTES)}"}


# ---------------------------------------------------------------------------
# 临时目录重定向（禁写 C 盘）
# ---------------------------------------------------------------------------

def setup_scratch_tmp(cfg: dict) -> str:
    scratch = os.path.join(cfg["workdir"], SCRATCH_DIR)
    os.makedirs(scratch, exist_ok=True)
    os.environ["TMP"] = scratch
    os.environ["TEMP"] = scratch
    tempfile.tempdir = scratch
    return scratch


def cleanup_scratch(scratch: str) -> None:
    if os.path.isdir(scratch):
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="F4 客户补充列层: final_audit 01/02/03/05 加评论数列")
    ap.add_argument("--config", required=True)
    ap.add_argument("--part", default="all", choices=["all", "verify"],
                    help="all=备份+重导出+05插列+验证; verify=仅验证（产物已在盘）")
    ap.add_argument("--rss-limit-gb", type=float, default=8.0,
                    help="05 常规模式内存上限（GB）, 超限回退 write_only 重建")
    args = ap.parse_args()

    cfg = load_config(args.config)
    scratch = setup_scratch_tmp(cfg)
    rss_limit = int(args.rss_limit_gb * 1024 ** 3)
    fa_dir = os.path.join(cfg["workdir"], FINAL_AUDIT_DIR)
    out_05 = os.path.join(fa_dir, FILE_05)
    backup_05 = os.path.join(cfg["temp_dir"], BACKUP_05_NAME)
    t0 = time.time()
    _log(f"F4 启动: workdir={cfg['workdir']} rss_limit={fmt_bytes(rss_limit)}")

    if not os.path.isfile(out_05):
        print(f"MISSING_INPUT: {out_05}", flush=True)
        return 2

    con = connect_duckdb(cfg)
    try:
        if args.part == "all":
            # a. 备份 05（唯一副本, 已存在则跳过）
            backup_file(out_05, backup_05)
        else:
            _log("verify-only: 跳过备份/重导出/05 修改, 仅验证")

        # b. 评论 map
        _log("构建 FINAL 评论 map (comments_final.parquet)…")
        final_map = build_final_comment_map(
            con, os.path.join(cfg["temp_dir"], COMMENTS_PARQUET))
        _log(f"  FINAL map: {len(final_map)} 键")
        _log("构建候选评论 map (comment_index + zs_result)…")
        cvidx_map, zs_map, n_cand = build_candidate_maps(con, cfg, cfg["temp_dir"])
        _log(f"  候选键={n_cand}, CVIDX 命中={len(cvidx_map)}, ZS 可用={len(zs_map)}")

        if args.part == "all":
            # c. 01/02/03 重导出（覆盖 final_audit）
            for step in ("01", "02", "03"):
                d = STEP_DEFS[step]
                tmp_path = os.path.join(cfg["temp_dir"], f"p4_tmp_{step}.xlsx")
                _log(f"重导出客户{step}: {d['file']}（插入评论数列）")
                res = export_step_with_comments(con, step, cfg["temp_dir"], tmp_path,
                                                final_map, cvidx_map, zs_map)
                st = res["stats"]
                total = sum(st.values())
                os.replace(tmp_path, os.path.join(fa_dir, d["file"]))
                _log(f"  完成: 行数={total} 填充={st['FINAL'] + st['CVIDX'] + st['ZS']} "
                     f"空={st['MISS']} 来源: FINAL链={st['FINAL']} CVIDX={st['CVIDX']} ZS={st['ZS']}")

            # d. 05 插列（常规模式优先, 回退 write_only 重建; 重复运行保护）
            if _05_already_done(out_05):
                _log("05 主表第8列已是评论数 → 跳过修改, 直接进入验证")
                mode, st05 = "skipped_already_done", {"filled": -1, "empty": -1, "rows": -1}
            else:
                _log("处理 05: 插入评论数列…")
                mode, st05 = process_05(cfg, final_map, backup_05, out_05,
                                        cfg["temp_dir"], rss_limit)
                _log(f"  完成（模式={mode}）: 填充={st05['filled']} 空={st05['empty']} "
                     f"数据行={st05['rows']}")

        # e. 验证
        _log("验证 01/02/03（全量逐行 + 敏感词扫描）…")
        checks = verify_steps(cfg, con, final_map, cvidx_map, zs_map)
        for c in checks:
            for s in c.get("samples", [])[:5]:
                _log(f"  抽样{step_label(c['name'])} 行{s[0]}: code={s[1]} sku={s[2]} "
                     f"mall={s[3]} 评论数={s[4]!r}")
        _log("验证 05（与备份流式比对）…")
        checks += verify_05(cfg, backup_05, final_map)
        _log("验证 FINAL 三件套 sha256 与磁盘…")
        checks.append(verify_final_sha(cfg))
        checks.append(verify_disk(cfg))

        _log("=== P4 VALIDATION CHECKS ===")
        all_pass = True
        for c in checks:
            all_pass = all_pass and c["pass"]
            _log(f"{'PASS' if c['pass'] else 'FAIL'}: {c['name']} | {c['detail']}")
        _log(f"产物清单:")
        for step in ("01", "02", "03"):
            p = os.path.join(fa_dir, STEP_DEFS[step]["file"])
            _log(f"  {p} {os.path.getsize(p)}B sha256={sha256_file(p)[:16]}")
        _log(f"  {out_05} {os.path.getsize(out_05)}B sha256={sha256_file(out_05)[:16]}")
        _log(f"  备份: {backup_05} {os.path.getsize(backup_05)}B "
             f"sha256={sha256_file(backup_05)[:16]}")
        if all_pass:
            _log("RESULT: ALL P4 CHECKS PASS")
            return 0
        print("P4_CHECK_FAIL: 存在未通过验证项 → exit 2", flush=True)
        return 2
    finally:
        con.close()
        cleanup_scratch(scratch)
        _log(f"F4 结束: 耗时 {time.time() - t0:.1f}s")


def step_label(check_name: str) -> str:
    return check_name.split("_")[0]


if __name__ == "__main__":
    sys.exit(main())
