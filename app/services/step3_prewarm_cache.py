"""STEP3 consumer for already-crawled customer reference links.

This module is intentionally side-effect free apart from mutating the supplied
ImportRow-like objects. The shared crawl cache itself is read only to callers.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

REQUIRED_COMMENT_MALLS = frozenset({"jd", "suning"})


def _positive_price(value: Any) -> float | None:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return float(parsed)


def _comment_value(entry: dict[str, Any]) -> str:
    value = entry.get("comment_count")
    if value is not None and str(value).strip() != "":
        return str(value).strip()
    payload = entry.get("payload") or {}
    result = payload.get("result") if isinstance(payload, dict) else {}
    raw = result.get("comment_count_raw") if isinstance(result, dict) else None
    text = str(raw or "").strip()
    if text and text not in {"未识别", "N/A", "NA", "None", "null"}:
        return text
    return ""


def missing_required_comment(
    row: Any,
    *,
    mall_resolver: Callable[[str], str | None],
    required_comment_malls: frozenset[str] = REQUIRED_COMMENT_MALLS,
) -> bool:
    """True when an otherwise priced JD/Suning row still lacks comment evidence."""
    url = str(getattr(row, "URL", "") or "").strip()
    mall = str(mall_resolver(url) or "").strip()
    if mall not in required_comment_malls:
        return False
    if _positive_price(getattr(row, "价格", None)) is None:
        return False
    return not str(getattr(row, "销量_评论数", "") or "").strip()


def apply_cached_customer_results(
    rows: Iterable[Any],
    *,
    shared_cache: Any,
    mall_resolver: Callable[[str], str | None],
    required_comment_malls: frozenset[str] = REQUIRED_COMMENT_MALLS,
) -> dict[str, int]:
    summary = {
        "scanned": 0,
        "cache_miss": 0,
        "applied": 0,
        "incomplete": 0,
        "missing_required_comment": 0,
        "channel_mismatch": 0,
        "unconfirmed_sku": 0,
    }
    for row in rows:
        summary["scanned"] += 1
        url = str(getattr(row, "URL", "") or "").strip()
        mall = str(mall_resolver(url) or "").strip()
        if not url or not mall:
            summary["cache_miss"] += 1
            continue
        row_channel = str(getattr(row, "供应商编码", "") or "").strip()
        if row_channel and row_channel != mall:
            summary["channel_mismatch"] += 1
            continue
        hit = shared_cache.lookup_current(mall, url)
        if not hit:
            summary["cache_miss"] += 1
            continue
        entry = hit.get("entry") or {}
        sku = str(entry.get("real_sku_id") or "").strip()
        if str(entry.get("sku_status") or "").strip() != "CONFIRMED":
            summary["unconfirmed_sku"] += 1
            continue
        title = str(entry.get("title") or "").strip()
        price = _positive_price(entry.get("price"))
        if not sku or not title or price is None:
            summary["incomplete"] += 1
            continue
        comment = _comment_value(entry)
        if mall in required_comment_malls and not comment:
            summary["missing_required_comment"] += 1
            continue

        row.SKUID = sku
        row.互联网标题 = title
        row.价格 = price
        row.销量_评论数 = comment
        if hasattr(row, "供应商编码") and not row_channel:
            row.供应商编码 = mall
        if hasattr(row, "爬取状态"):
            row.爬取状态 = "成功"
        if hasattr(row, "爬取失败原因"):
            row.爬取失败原因 = ""
        summary["applied"] += 1
    return summary
