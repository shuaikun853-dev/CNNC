"""Shared business-row page-price semantics for finalize and read-only status."""
from __future__ import annotations

import json
from dataclasses import dataclass
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


def business_row_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    """Strict final-workbook identity for one document/link business row."""
    document_id = str(
        row.get("document_id")
        or row.get("documentId")
        or row.get("vCommodityId")
        or ""
    ).strip()
    return document_id, str(row.get("original_url") or row.get("extLink") or "").strip()


def valid_projected_page_price(row: Mapping[str, Any]) -> bool:
    return valid_page_price_result(
        {
            "page_price_status": row.get("__page_price_status"),
            "page_price_value": row.get("__page_price_value"),
        }
    )


@dataclass(frozen=True)
class FinalPagePriceProjection:
    """One authoritative view of the rows a final workbook will contain."""

    frozen_missing_page_price_rows: int
    failed_customer_link_rows: int
    appended_failed_customer_link_rows: int
    overlaid_failed_customer_link_rows: int
    page_price_required_rows: int
    missing_page_price_rows: int
    failed_by_frozen_index: tuple[tuple[int, Mapping[str, Any]], ...]
    appended_failures: tuple[Mapping[str, Any], ...]


def build_final_page_price_projection(
    frozen_rows: Sequence[Mapping[str, Any]],
    failed_rows: Sequence[Mapping[str, Any]],
) -> FinalPagePriceProjection:
    """Deduplicate day-scoped failures against frozen business identities.

    A failed document/link overlays every already-existing frozen row with the
    same strict identity.  Only identities absent from the frozen artifact are
    appended.  The count is then derived from that exact projected row set,
    never by adding two independently calculated counters.
    """
    frozen_by_identity: dict[tuple[str, str], list[int]] = {}
    for index, row in enumerate(frozen_rows):
        identity = business_row_identity(row)
        if all(identity):
            frozen_by_identity.setdefault(identity, []).append(index)

    unique_failures: list[Mapping[str, Any]] = []
    seen_failures: set[tuple[str, str]] = set()
    for failure in failed_rows:
        identity = business_row_identity(failure)
        if not all(identity) or identity in seen_failures:
            continue
        seen_failures.add(identity)
        unique_failures.append(failure)

    failed_by_frozen_index: list[tuple[int, Mapping[str, Any]]] = []
    appended_failures: list[Mapping[str, Any]] = []
    overlaid_identities = 0
    for failure in unique_failures:
        indices = frozen_by_identity.get(business_row_identity(failure), [])
        if indices:
            overlaid_identities += 1
            failed_by_frozen_index.extend((index, failure) for index in indices)
        else:
            appended_failures.append(failure)

    overlaid_indices = {index for index, _failure in failed_by_frozen_index}
    frozen_required = [
        index
        for index, row in enumerate(frozen_rows)
        if bool(row.get("__crawl_required", bool(str(row.get("extLink") or "").strip())))
    ]
    frozen_missing = sum(
        not valid_projected_page_price(frozen_rows[index])
        for index in frozen_required
    )
    projected_frozen_missing = sum(
        index in overlaid_indices or not valid_projected_page_price(frozen_rows[index])
        for index in frozen_required
    )
    appended_count = len(appended_failures)
    return FinalPagePriceProjection(
        frozen_missing_page_price_rows=frozen_missing,
        failed_customer_link_rows=len(unique_failures),
        appended_failed_customer_link_rows=appended_count,
        overlaid_failed_customer_link_rows=overlaid_identities,
        page_price_required_rows=len(frozen_required) + appended_count,
        missing_page_price_rows=projected_frozen_missing + appended_count,
        failed_by_frozen_index=tuple(failed_by_frozen_index),
        appended_failures=tuple(appended_failures),
    )
