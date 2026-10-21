"""Offline, read-only demonstration of existing price-audit decision rules.

No network calls, database connections, authentication, model calls, or writes.
All sample data is synthetic. Never interpret a result as approval to mutate data.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.services.saas_audit_core import (
    compute_final_decision,
    compute_over_30_check,
    compute_price_changed,
    compute_saas_price_vs_customer,
    parse_decimal,
)

EXAMPLE_INPUT = Path(__file__).resolve().parent / "fixtures" / "sample_prices.json"


def evaluate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute advisory classifications using existing, unchanged core functions."""
    if not isinstance(rows, list) or not rows:
        raise ValueError("items must be a non-empty list")

    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each item must be an object")
        record_id = row.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("each item requires a non-empty string id")
        if record_id in seen:
            raise ValueError("duplicate item id")
        seen.add(record_id)

        customer_price = parse_decimal(row.get("customer_price"))
        saas_price = parse_decimal(row.get("saas_price"))
        page_price = parse_decimal(row.get("page_price"))
        page_status = row.get("page_price_status", "SUCCESS")
        if page_status not in {"SUCCESS", "NO_RESULT"}:
            raise ValueError("page_price_status must be SUCCESS or NO_RESULT")

        # Incomplete evidence must not look like an authorization to approve.
        complete = (
            all(p is not None and p.is_finite() and p > 0
                for p in (customer_price, saas_price, page_price))
            and page_status == "SUCCESS"
        )
        if complete:
            saas_status, _ = compute_saas_price_vs_customer(customer_price, saas_price)
            changed_status, _ = compute_price_changed(saas_price, page_price, page_status)
            threshold_status, threshold_flag = compute_over_30_check(
                customer_price, page_price, page_status
            )
            decision = compute_final_decision(saas_status, changed_status, threshold_flag)
        else:
            saas_status = "NOT_EVALUATED"
            changed_status = "NOT_EVALUATED"
            threshold_status = "NOT_EVALUATED"
            decision = "REVIEW_REQUIRED"

        results.append({
            "id": record_id,
            "decision": decision,
            "advisory_only": True,
            "checks": {
                "saas_vs_customer": saas_status,
                "price_changed": changed_status,
                "page_vs_customer": threshold_status,
            },
        })

    return {
        "mode": "offline_demo",
        "readonly": True,
        "count": len(results),
        "summary": dict(sorted(Counter(r["decision"] for r in results).items())),
        "items": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline synthetic price audit (read-only)")
    parser.add_argument("--input", type=Path, default=EXAMPLE_INPUT,
                        help="local JSON input file; defaults to synthetic fixture")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("input must be a JSON object")
        result = evaluate(payload.get("items"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        parser.exit(2, f"Invalid local input: {type(exc).__name__}: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
