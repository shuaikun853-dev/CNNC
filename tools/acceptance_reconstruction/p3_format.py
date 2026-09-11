# -*- coding: utf-8 -*-
"""p3_format.py — F3 导出层 xlsx 格式化纯函数（验收回溯重建）。

对应规格: reports/SPEC_ACCEPTANCE_RECONSTRUCTION.md §4(客户列)/§6(导出层)/§7（冻结版契约）。

职责（全部为可单测的纯/近纯函数）:
- 客户中文列名与列规格（SPEC §4 冻结, 5 个导出文件, 禁止增删列）
- sheet 拆分计划（单 sheet 数据行 > SHEET_MAX_DATA_ROWS → xxx_1/_2, 表头一致）
- openpyxl write_only 流式写出（首行冻结 A2 / autofilter 全列 / 编码与URL列 '@' /
  价格与指数 '0.00' / 评论数整数或空 / 指数空串写空）
- 客户文件敏感词扫描器（大小写不敏感; "去极值"不在词表 → 流程名允许）
- 价格指数复算 Decimal 精确函数（幂次 p=1 CEIL 2位 / 平均价 HALF_UP 2位 / 缺失评论按 0）

口径来源: 幂次 CEIL 2位、平均价 HALF_UP 2位 为本项目 0909 重算实证口径（写死, 报告注明）。
硬红线: 不添加任何内部列/内部 Sheet; 输出路径由调用方决定。
"""
from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: Excel 单 sheet 数据行上限 = 1,048,576 总行 - 1 表头行
SHEET_MAX_DATA_ROWS = 1_048_575

FMT_TEXT = "@"
FMT_NUM2 = "0.00"

# 客户中文列名（SPEC §4 冻结）
H_CODE = "标准商品编码"
H_NAME = "标准商品名称"
H_SKU = "互联网商品编码（SKUID）"
H_MALL = "互联网商城编码"
H_TITLE = "互联网商品名称"
H_URL = "互联网链接"
H_PRICE = "互联网商品价格"
H_SAME = "同款判断"
H_COMMENT = "评论数"
H_AUDIT = "人工审核结果"
H_INDEX = "本次处理后的价格指数"
H_METHOD = "价格指数计算方式"

# 列规格: (中文表头, parquet字段, 单元格类型)
# 单元格类型: text_at=强制文本'@' / text=文本 / price=数值'0.00' / int=整数或空 / index=数值'0.00'或空
CUSTOMER_COLUMN_SPECS: dict[str, list[tuple[str, str, str]]] = {
    "01": [
        (H_CODE, "code", "text_at"),
        (H_NAME, "name", "text"),
        (H_SKU, "sku", "text_at"),
        (H_MALL, "mall", "text"),
        (H_TITLE, "title", "text"),
        (H_URL, "url", "text_at"),
        (H_PRICE, "price", "price"),
        (H_SAME, "same_verdict", "text"),
    ],
    "02": [
        (H_CODE, "code", "text_at"),
        (H_NAME, "name", "text"),
        (H_SKU, "sku", "text_at"),
        (H_MALL, "mall", "text"),
        (H_TITLE, "title", "text"),
        (H_URL, "url", "text_at"),
        (H_PRICE, "price", "price"),
    ],
    "03": [
        (H_CODE, "code", "text_at"),
        (H_NAME, "name", "text"),
        (H_SKU, "sku", "text_at"),
        (H_MALL, "mall", "text"),
        (H_TITLE, "title", "text"),
        (H_URL, "url", "text_at"),
        (H_PRICE, "price", "price"),
    ],
    "04": [
        (H_CODE, "code", "text_at"),
        (H_NAME, "name", "text"),
        (H_SKU, "sku", "text_at"),
        (H_MALL, "mall", "text"),
        (H_TITLE, "title", "text"),
        (H_URL, "url", "text_at"),
        (H_PRICE, "price", "price"),
        (H_COMMENT, "comment_value", "int"),
        (H_AUDIT, "audit_result", "text"),
    ],
    "05": [
        (H_CODE, "code", "text_at"),
        (H_NAME, "name", "text"),
        (H_SKU, "sku", "text_at"),
        (H_MALL, "mall", "text"),
        (H_TITLE, "title", "text"),
        (H_URL, "url", "text_at"),
        (H_PRICE, "price", "price"),
        (H_COMMENT, "comment_value", "int"),
        (H_INDEX, "index_val", "index"),
        (H_METHOD, "method", "text"),
    ],
}


def customer_headers(step: str) -> list[str]:
    """客户文件中文表头（SPEC §4 冻结）。"""
    return [h for h, _f, _k in CUSTOMER_COLUMN_SPECS[step]]


def customer_fields(step: str) -> list[str]:
    """客户文件对应的 parquet 字段（SELECT 顺序=表头顺序）。"""
    return [f for _h, f, _k in CUSTOMER_COLUMN_SPECS[step]]


def customer_kinds(step: str) -> list[str]:
    """客户文件列的单元格类型序列。"""
    return [k for _h, _f, k in CUSTOMER_COLUMN_SPECS[step]]


# ---------------------------------------------------------------------------
# sheet 拆分
# ---------------------------------------------------------------------------

def split_sheet_names(base: str, total_rows: int, max_rows: int = SHEET_MAX_DATA_ROWS) -> list[tuple[str, int]]:
    """拆分计划: 返回 [(sheet名, 数据行数)]。

    total_rows <= max_rows → [(base, total_rows)];
    否则拆为 base_1/base_2/...（表头一致）。
    """
    if total_rows <= 0:
        return [(base, 0)]
    if total_rows <= max_rows:
        return [(base, total_rows)]
    plan: list[tuple[str, int]] = []
    left = total_rows
    idx = 1
    while left > 0:
        n = min(left, max_rows)
        plan.append((f"{base}_{idx}", n))
        left -= n
        idx += 1
    return plan


# ---------------------------------------------------------------------------
# 敏感词扫描（验证门禁 6）
# ---------------------------------------------------------------------------

#: 客户文件禁词（sheet名/表头/全数据文本; 大小写不敏感; "去极值"不在词表→允许）
SENSITIVE_WORDS: tuple[str, ...] = (
    "基准价", "基准价格", "参考基准", "内部基准", "偏离基准",
    "baseline", "reference baseline", "内部参考值", "referencePrice",
    "内部审计", "RECONSTRUCTED", "HISTORICAL", "FINAL_AUTHORITY",
    "source_file", "source_sheet", "source_row", "snapshot_source",
    "verdict_source", "WINNER_RULE_CONFLICT", "FINAL_ROW_RULE_CONFLICT",
    "MISSING_SOURCE_DATA",
)


def build_sensitive_pattern(words: tuple[str, ...] | list[str] | None = None) -> re.Pattern:
    """编译敏感词正则（大小写不敏感, 词按长度降序保证优先命中长词）。"""
    ws = sorted(words if words is not None else SENSITIVE_WORDS, key=len, reverse=True)
    return re.compile("|".join(re.escape(w) for w in ws), re.IGNORECASE)


def find_sensitive_hit(pattern: re.Pattern, text: object) -> str | None:
    """返回命中的文本片段（按原文大小写）; 未命中返回 None。非字符串按 str() 处理。"""
    if text is None:
        return None
    m = pattern.search(str(text))
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# 单元格值转换（纯函数, 返回 (写入值, number_format 或 None)）
# ---------------------------------------------------------------------------

def convert_cell(kind: str, value: object) -> tuple[object, str | None]:
    """按列类型转换。返回 (value, number_format); value=None 表示写空单元格。"""
    if kind == "text_at":
        s = _to_text(value)
        return (s, FMT_TEXT) if s is not None else (None, None)
    if kind == "text":
        return _to_text(value), None
    if kind == "price":
        f = _to_float(value)
        return (f, FMT_NUM2) if f is not None else (None, None)
    if kind == "int":
        return _to_int(value), None
    if kind == "index":
        v = _to_index(value)
        if v is None:
            return None, None
        if isinstance(v, float):
            return v, FMT_NUM2
        return v, FMT_TEXT  # 非数值字符串兜底（防丢数据）
    raise ValueError(f"未知列类型: {kind}")


def _to_text(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s != "" else None


def _to_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        return int(round(value))
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def _to_index(value: object) -> float | str | None:
    """指数: 空串→None（写空）; 数值→float; 非数值→原文本。"""
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    f = _to_float(s)
    return f if f is not None else s


# ---------------------------------------------------------------------------
# xlsx 流式写出（openpyxl write_only）
# ---------------------------------------------------------------------------

def write_xlsx(path: str, sheets: list[dict], max_rows: int = SHEET_MAX_DATA_ROWS,
               on_progress=None) -> list[dict]:
    """流式写出 xlsx（openpyxl write_only）。

    sheets: [{"name": 基础sheet名, "headers": [...], "kinds": [...], "rows": 行迭代器(元组)}]
    - 表头首行; freeze A2; autofilter 全列(表头+数据); 行超 max_rows 自动拆 name_1/_2。
    - 每行元组长度必须 == len(headers); 按 kinds 转换单元格。
    - on_progress(sheet名, 已写行数) 回调可选。
    返回: [{"name": 实际sheet名, "rows": 数据行数}, ...]
    """
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.utils import get_column_letter

    wb = Workbook(write_only=True)
    written: list[dict] = []

    for spec in sheets:
        base = spec["name"]
        headers: list[str] = list(spec["headers"])
        kinds: list[str] = list(spec["kinds"])
        rows = spec["rows"]
        ncols = len(headers)
        if len(kinds) != ncols:
            raise ValueError(f"sheet {base}: headers({ncols}) 与 kinds({len(kinds)}) 数量不一致")

        ws = wb.create_sheet(title=base)
        ws.freeze_panes = "A2"
        ws.append(headers)
        cur_name = base
        n = 0
        overflow = False

        def _close_sheet(sheet, name: str, nrows: int) -> None:
            sheet.auto_filter.ref = f"A1:{get_column_letter(ncols)}{nrows + 1}"
            written.append({"name": name, "rows": nrows})

        for row in rows:
            if n >= max_rows:  # 拆分: 当前 sheet 更名 base_1, 新建 base_2（表头一致）
                if not overflow:
                    ws.title = f"{base}_1"
                    cur_name = f"{base}_1"
                    overflow = True
                _close_sheet(ws, cur_name, n)
                cur_name = f"{base}_{len(written) + 1}"
                ws = wb.create_sheet(title=cur_name)
                ws.freeze_panes = "A2"
                ws.append(headers)
                n = 0
            cells = []
            for i, kind in enumerate(kinds):
                v = row[i] if i < len(row) else None
                val, fmt = convert_cell(kind, v)
                if val is None:
                    cells.append(None)
                elif fmt is None:
                    cells.append(val)
                else:
                    c = WriteOnlyCell(ws, val)
                    c.number_format = fmt
                    cells.append(c)
            ws.append(cells)
            n += 1
            if on_progress and n % 100_000 == 0:
                on_progress(cur_name, n)
        _close_sheet(ws, cur_name, n)

    wb.save(path)
    return written


# ---------------------------------------------------------------------------
# 价格指数复算（Decimal 精确; 口径=0909 重算实证, 写死）
# ---------------------------------------------------------------------------

#: 评论缺失四态（含 NO_KEY）→ 权重按 0
MISSING_COMMENT_STATES = frozenset({"EMPTY_SRC", "NOT_FOUND", "NO_KEY"})

_TWO = Decimal("0.01")


def dec_of(value: object) -> Decimal:
    """float/int/str → Decimal（经 str 保证二进制浮点不失真, 如 24.0→'24.0'）。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(str(value).strip())


def comment_weight(comment_value: object, comment_state: object) -> Decimal:
    """评论权重 = 评论数 + 1; 缺失（缺失态/None/不可解析）按 0 → 权重 1。"""
    if comment_state in MISSING_COMMENT_STATES or comment_value is None:
        return Decimal(1)  # 评论数按 0
    try:
        return dec_of(comment_value) + Decimal(1)
    except (InvalidOperation, ValueError):
        return Decimal(1)


def quant_ceil2(d: Decimal) -> Decimal:
    """幂次口径: 向上取整 2 位（0909 重算实证, 写死）。"""
    return d.quantize(_TWO, rounding=ROUND_CEILING)


def quant_halfup2(d: Decimal) -> Decimal:
    """平均价口径: 四舍五入 HALF_UP 2 位（0909 重算实证, 写死）。"""
    return d.quantize(_TWO, rounding=ROUND_HALF_UP)


def partition_valid_price(rows) -> tuple[list, int]:
    """拆分 (有效价格行, 无效价格行数)。有效=price 可解析为 Decimal 的行。"""
    ok, bad = [], 0
    for r in rows:
        try:
            if r[0] is None:
                bad += 1
                continue
            dec_of(r[0])
        except (InvalidOperation, ValueError):
            bad += 1
            continue
        ok.append(r)
    return ok, bad


def recalc_power_index(rows) -> Decimal | None:
    """幂次 p=1 复算: Σ(price×(评论+1)) / Σ(评论+1), CEIL 2位。rows: (price, comment_value, comment_state)。

    价格不可解析的行跳过; 无有效行 → None（不造数据）。
    """
    ok, _bad = partition_valid_price([(r[0], r[1], r[2]) for r in rows])
    num = Decimal(0)
    den = Decimal(0)
    for price, cv, cs in ok:
        w = comment_weight(cv, cs)
        num += dec_of(price) * w
        den += w
    if den == 0:
        return None
    return quant_ceil2(num / den)


def recalc_avg_price(rows) -> Decimal | None:
    """平均价复算: 算术平均, HALF_UP 2位。rows 同上（评论列忽略）。"""
    ok, _bad = partition_valid_price([(r[0], r[1], r[2]) for r in rows])
    if not ok:
        return None
    total = sum((dec_of(r[0]) for r in ok), Decimal(0))
    return quant_halfup2(total / Decimal(len(ok)))


def index_val_equal(computed: Decimal, index_val: object) -> bool:
    """复算值与指数原值按数值相等比较（'22.98'/'22.980' 均可）; 空串/非法→False。"""
    if index_val is None:
        return False
    s = str(index_val).strip()
    if s == "":
        return False
    try:
        return dec_of(s) == computed
    except (InvalidOperation, ValueError):
        return False
