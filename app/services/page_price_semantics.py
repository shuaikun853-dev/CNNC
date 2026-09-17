"""Shared business-row page-price semantics for finalize and read-only status."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

FORCE_CLOSEOUT_MISSING_PRICE_LIMIT = 20


def crawl_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("vCommodityId") or row.get("documentId") or "").strip(),
        str(row.get("skuId") or "").strip(),
        str(row.get("extLink") or "").strip(),
    )


def valid_page_price_result(result: Mapping[str, Any]) -> bool:
    status = str(result.get("page_price_status") or "").strip()
    try:
        value = Decimal(str(result.get("page_price_value")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (status == "OK" and value > 0) or (status == "PRICE_IS_ZERO" and value == 0)
