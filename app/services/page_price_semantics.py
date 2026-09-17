"""Shared business-row page-price semantics for finalize and read-only status."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

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
    if not value.is_finite():
        return False
    return (status == "OK" and value > 0) or (status == "PRICE_IS_ZERO" and value == 0)


def is_local_fallback_success(crawl: Mapping[str, Any]) -> bool:
    """Return whether a crawl row has a valid, terminal local-fallback price."""
    if str(crawl.get("state") or "") != "SUCCESS":
        return False
    raw_result = crawl.get("result_json")
    if isinstance(raw_result, Mapping):
        result = raw_result
    else:
        try:
            result = json.loads(str(raw_result or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    return (
        isinstance(result, Mapping)
        and str(result.get("page_price_source") or "").startswith("本地兜底:")
        and valid_page_price_result(result)
    )


def remote_job_is_closed_by_local_fallback(crawls: Sequence[Mapping[str, Any]]) -> bool:
    """Close stale remote state only when every link has a valid price and at
    least one exact link was completed by local fallback.
    """
    if not crawls or not any(is_local_fallback_success(crawl) for crawl in crawls):
        return False
    for crawl in crawls:
        if str(crawl.get("state") or "") != "SUCCESS":
            return False
        raw_result = crawl.get("result_json")
        if isinstance(raw_result, Mapping):
            result = raw_result
        else:
            try:
                result = json.loads(str(raw_result or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
        if not isinstance(result, Mapping) or not valid_page_price_result(result):
            return False
    return True
