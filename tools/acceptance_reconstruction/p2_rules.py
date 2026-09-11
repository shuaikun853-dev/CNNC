# -*- coding: utf-8 -*-
"""p2_rules.py — F2 重建层规则纯函数（验收回溯重建）。

对应规格: reports/SPEC_ACCEPTANCE_RECONSTRUCTION.md §4/§7（冻结版契约）。

职责（全部纯函数，不落盘、不连库）:
- ALGO 包加载（importlib 从 cfg.algo_src 路径加载, 不重新发明 winner/longValue 语义）
- winner 选择: 复用 ALGO 包 CommodityRow + _better_candidate（评论非空>评论大>longValue 低价>首见）
- final_problems 判定列 → record_origin/verdict_source 映射（依据 20260911 真实探针 distinct 分布）
- Step2 剔除规则 R-a..R-e 的顺序与 reason 组合（首个命中为 stage_drop_reason）
- Step1 键冲突裁决优先级（数值 prio, 主 Agent 20260911 裁定）:
    FINAL_AUTHORITY(100) > HISTORICAL_非同品/03表(70) ≈ HISTORICAL_复核剔除/02表(69)
      ≈ HISTORICAL_DIFY再校验(68) > HISTORICAL_平台匹配/P25(60) > HISTORICAL_第二轮模型/S3(55)

裁决依据（主 Agent 20260911 验收裁定）:
02/03 表是 FINAL_AUTHORITY 自带的人工复核结论（0822-0825）, DIFY 是 0908 模型再校验,
时间均晚于 P25(0813)/S3(0822-15:15前) 候选池快照 → "更晚的明确人工/模型判定 > 更早的平台匹配正向"。
02/03/DIFY 三者同键冲突取先列者(03>02>DIFY)并记 lineage sources, 计数上报。
"""

from __future__ import annotations

import importlib.util
import os
import sys

# ---------------------------------------------------------------------------
# verdict_source / record_origin 常量
# ---------------------------------------------------------------------------

VS_FINAL = "FINAL_AUTHORITY"
VS_P25 = "HISTORICAL_平台匹配"
VS_S3 = "HISTORICAL_第二轮模型"
VS_NEG_02 = "HISTORICAL_复核剔除"
VS_NEG_03 = "HISTORICAL_非同品"
VS_DIFY = "HISTORICAL_DIFY_再校验非同款"

ORIGIN_FINAL = "FINAL_MAIN"
ORIGIN_P25 = "P25_UNIQUE"
ORIGIN_S3 = "S3_UNIQUE"
ORIGIN_NEG_02 = "NEG_02_REEVAL"
ORIGIN_NEG_03 = "NEG_03_NONSAME"
ORIGIN_DIFY = "NEG_DIFY"
ORIGIN_KEEP_CONFIRMED = "KEEP_CONFIRMED"      # 问题表"当前保留"行（已是 FINAL 行, 不另立键）
ORIGIN_PRICE_DROP_04 = "PRICE_DROP_04"        # 04表剔除行 → Step2 R-d 候选（同款成立, 不是"否"）
ORIGIN_IGNORE_01 = "IGNORE_01"                # 01表不在 F2 重建范围

# Step1 键冲突合并优先级（数值大者胜; 平局按 source_ref 字典序, 保证确定性）
MERGE_PRIO = {
    ORIGIN_FINAL: 100,
    ORIGIN_NEG_03: 70,   # 02/03/DIFY 同层: 明确负判, 同键取先列者 03 > 02 > DIFY
    ORIGIN_NEG_02: 69,
    ORIGIN_DIFY: 68,
    ORIGIN_P25: 60,
    ORIGIN_S3: 55,
}

# 进入 Step1 并集的负判 origin（其判定时间晚于池快照 → 压过 P25/S3 池正向）
NEG_ORIGINS = (ORIGIN_NEG_03, ORIGIN_NEG_02, ORIGIN_DIFY)


class UnknownRuleValueError(ValueError):
    """遇到 SPEC/F1事实/ALGO包 之外未定义的取值时抛出（禁造数据, 停止等澄清）。"""


# ---------------------------------------------------------------------------
# ALGO 包加载与 winner 规则
# ---------------------------------------------------------------------------

def load_algo(algo_src: str):
    """从路径加载已确认算法包（31 项官方测试通过的冻结版）。

    注意: 必须先注册 sys.modules 再 exec_module —— Python 3.14 的
    dataclasses 处理 @dataclass 时会按 cls.__module__ 查 sys.modules。
    """
    if not os.path.isfile(algo_src):
        raise FileNotFoundError(f"ALGO 包不存在: {algo_src}")
    spec = importlib.util.spec_from_file_location("cnnc_price_algorithms_f2", algo_src)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 ALGO 包: {algo_src}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class WinnerRules:
    """winner/longValue 语义全部委托 ALGO 包, 不重新实现比较逻辑。"""

    def __init__(self, algo):
        self.algo = algo
        for attr in ("CommodityRow", "java_long_from_price", "_normalized_nullable_comment",
                     "_better_candidate"):
            if not hasattr(algo, attr):
                raise AttributeError(f"ALGO 包缺少预期接口: {attr}")

    def long_value(self, price: float) -> int:
        """Java Double.longValue() 向零截断（ALGO 语义）。要求 price 为有限非负 float。"""
        return int(self.algo.java_long_from_price(price))

    def normalize_comment(self, value):
        """可空评论数归一: None/''/'-' → None; '5'/5/5.0 → 5; 非法 → ValueError。"""
        return self.algo._normalized_nullable_comment(value)

    def comment_fallback(self, s3_comment, comments_final_value):
        """Step3 理论 winner 评论口径: S3.comment → comments_final → NULL。

        S3.comment 为原始字符串（''/None 视为缺失, 继续回退）; comments_final 为 float|None。
        非归一化原始值（如 '100+'、'1万+'、'已售0'）无法按 ALGO 口径解释 → 按缺失处理
        继续回退（不发明换算规则; 出现次数由调用方计数上报）。
        """
        for v in (s3_comment, comments_final_value):
            if v is None:
                continue
            try:
                n = self.normalize_comment(v)
            except ValueError:
                continue
            if n is not None:
                return n
        return None

    def is_normalized_comment(self, value) -> bool:
        """该取值能否按 ALGO 口径解释为可空评论数（用于统计上报）。"""
        if value is None:
            return True
        try:
            self.normalize_comment(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def valid_price(price) -> bool:
        """理论 winner 可比价格: 非 None、有限、非负（ALGO to_decimal_price 要求非负）。"""
        if price is None:
            return False
        f = float(price)
        return f == f and f not in (float("inf"), float("-inf")) and f >= 0.0

    def pick_winner_index(self, rows) -> int:
        """按 ALGO _better_candidate 折叠选出 winner 下标。

        rows: list[dict]，每项含 sku(str), mall(str|None), price(float, 须过 valid_price),
              comment(归一化 int|None)。语义: 评论非空>评论大>longValue 低价>首见
              （平局时 _better_candidate 保留先见者 → 折叠天然实现"首见"）。
        """
        if not rows:
            raise ValueError("pick_winner_index: 空候选组")
        crs = [
            self.algo.CommodityRow(
                sku_id=r["sku"], shop_code=r.get("mall"), price=r["price"],
                comment_num=r.get("comment"),
            )
            for r in rows
        ]
        winner = None
        for cr in crs:
            winner = cr if winner is None else self.algo._better_candidate(winner, cr)
        return crs.index(winner)


# ---------------------------------------------------------------------------
# final_problems 判定列映射（依据 20260911 探针的 distinct 全集）
# ---------------------------------------------------------------------------

def classify_problem_row(sheet: str, cur_result: str) -> tuple[str, str | None]:
    """(sheet, 本次处理结果) → (record_origin, verdict_source)。

    语义归类（TASK 规定）: 含'剔除'→剔除、含'保留'→保留。
      02表: 剔除 → NEG_02_REEVAL / HISTORICAL_复核剔除（Step1 负例, 压池正向）
            保留 → KEEP_CONFIRMED（已是 FINAL 行）
      03表: 剔除 → NEG_03_NONSAME / HISTORICAL_非同品（Step1 负例, 压池正向）
            保留 → KEEP_CONFIRMED
      04表: 剔除 → PRICE_DROP_04（同款成立, Step2 R-d 候选, 不算"否"）
            保留 → KEEP_CONFIRMED
      01表: IGNORE_01（无数据补采表, 不在 F2 重建范围）
    其余任何取值 → UnknownRuleValueError（停止等澄清, 不造规则）。
    """
    r = (cur_result or "").strip()
    if sheet == "01_4449无数据":
        return ORIGIN_IGNORE_01, None
    if sheet == "02_5.3%存疑同品":
        if "剔除" in r:
            return ORIGIN_NEG_02, VS_NEG_02
        if "保留" in r:
            return ORIGIN_KEEP_CONFIRMED, None
        raise UnknownRuleValueError(f"02表 未知的'本次处理结果'取值: {cur_result!r}")
    if sheet == "03_非同品原因":
        if "剔除" in r:
            return ORIGIN_NEG_03, VS_NEG_03
        if "保留" in r:
            return ORIGIN_KEEP_CONFIRMED, None
        raise UnknownRuleValueError(f"03表 未知的'本次处理结果'取值: {cur_result!r}")
    if sheet == "04_34952存疑价格":
        if "剔除" in r:
            return ORIGIN_PRICE_DROP_04, None
        if "保留" in r:
            return ORIGIN_KEEP_CONFIRMED, None
        raise UnknownRuleValueError(f"04表 未知的'本次处理结果'取值: {cur_result!r}")
    raise UnknownRuleValueError(f"未知的问题表 sheet: {sheet!r}")


def merge_priority(origin: str) -> int:
    """record_origin → Step1 键冲突合并优先级（见模块 docstring 说明）。"""
    if origin not in MERGE_PRIO:
        raise UnknownRuleValueError(f"未知 record_origin: {origin!r}")
    return MERGE_PRIO[origin]


def verdict_rank(verdict_source: str) -> int:
    """TASK 优先级: FINAL_AUTHORITY(3) > HISTORICAL*(2) > REEVALUATED*(1)。"""
    v = (verdict_source or "").strip()
    if v == VS_FINAL:
        return 3
    if v.startswith("HISTORICAL"):
        return 2
    if v.startswith("REEVALUATED"):
        return 1
    raise UnknownRuleValueError(f"未知 verdict_source: {verdict_source!r}")


def anomaly_is_abnormal(anomaly: str) -> bool:
    """R-e 口径: anomaly 含'异常'前缀（真实分布: 异常类型1/2/4; '正常'/''为正常）。"""
    return (anomaly or "").strip().startswith("异常")


# ---------------------------------------------------------------------------
# Step2 剔除规则（顺序即优先级; 首个命中为 stage_drop_reason, 全部命中记 lineage）
# ---------------------------------------------------------------------------

STEP2_RULE_ORDER = ("R-a", "R-b", "R-c", "R-d", "R-e")

_STEP2_FIXED_REASON = {
    "R-a": "HISTORICAL_极值标志",
    "R-b": "HISTORICAL_偏离超80",
    "R-d": "HISTORICAL_存疑价格剔除",
    "R-e": "HISTORICAL_平台价格异常",
}


def step2_rule_reason(rule: str, dropnew_reason: str = "") -> str:
    """规则 → stage_drop_reason。R-c = 'HISTORICAL_' + DROPNEW 真实原因（原因必非空）。"""
    if rule in _STEP2_FIXED_REASON:
        return _STEP2_FIXED_REASON[rule]
    if rule == "R-c":
        reason = (dropnew_reason or "").strip()
        if not reason:
            raise UnknownRuleValueError("R-c 命中但 DROPNEW 剔除原因为空（F1 事实: 原因必非空）")
        return "HISTORICAL_" + reason
    raise UnknownRuleValueError(f"未知 Step2 规则: {rule!r}")


def evaluate_step2_hits(hit_a: bool, hit_b: bool, hit_c: bool, hit_d: bool,
                        hit_e: bool, dropnew_reason: str = "") -> dict:
    """输入五规则命中布尔 + DROPNEW 原因 → {any, hits:[(rule,reason)...], first_rule, first_reason}。

    hits 按 R-a > R-b > R-c > R-d > R-e 排序（首个命中为 stage_drop_reason, 全部命中记 lineage）。
    """
    flags = {"R-a": bool(hit_a), "R-b": bool(hit_b), "R-c": bool(hit_c),
             "R-d": bool(hit_d), "R-e": bool(hit_e)}
    hits = []
    for rule in STEP2_RULE_ORDER:
        if flags[rule]:
            hits.append((rule, step2_rule_reason(rule, dropnew_reason)))
    return {
        "any": bool(hits),
        "hits": hits,
        "first_rule": hits[0][0] if hits else None,
        "first_reason": hits[0][1] if hits else None,
    }
