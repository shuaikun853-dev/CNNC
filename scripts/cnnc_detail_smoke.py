# -*- coding: utf-8 -*-
"""Read-only CNNC detail smoke check.

Verifies BEFORE the daily incremental price-audit flow that the customer-price
detail endpoint works end to end: one real request built from the production
curl template, parsed by the production parser.

Verdicts:
  HEALTHY                                - success=true, rspCode=0000 and the
                                           parser classified the payload as
                                           FOUND / FOUND_SINGLE_REFERENCE /
                                           NOT_FOUND (auth fine; NOT_FOUND
                                           only means this document carries no
                                           reference prices).
  CNNC_DETAIL_AUTH_OR_BUSINESS_FAILURE   - transport error, success=false,
                                           rspCode!=0000, or the parser
                                           classified DETAIL_REQUEST_FAILED /
                                           DETAIL_PARSE_FAILED.

Strictly read-only: touches no SQLite state, performs no SaaS writes.
Never prints cookies/tokens/auth header values.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from app.services.saas_audit_core import extract_customer_price_from_detail
    from scripts.audit_saas_excel_file import call_detail_api
    from scripts.curl_utils import load_curl_command
except ModuleNotFoundError:  # direct invocation from scripts/
    from audit_saas_excel_file import call_detail_api  # type: ignore
    from curl_utils import load_curl_command  # type: ignore
    from app.services.saas_audit_core import extract_customer_price_from_detail  # type: ignore

DEFAULT_TEMPLATE = ROOT / "data" / "detail_request.curl"
FAILURE = "CNNC_DETAIL_AUTH_OR_BUSINESS_FAILURE"
HEALTHY_PARSER_STATUSES = {"FOUND", "FOUND_SINGLE_REFERENCE", "NOT_FOUND"}


def classify(response: dict[str, Any], parsed: dict[str, Any]) -> tuple[str, str]:
    """Pure verdict for one detail response plus its parser result."""
    if not response.get("ok"):
        return FAILURE, f"transport_error={response.get('error') or 'unknown'}"
    payload = response.get("payload")
    if not isinstance(payload, dict):
        return FAILURE, "payload_not_json_object"
    if payload.get("success") is not True or str(payload.get("rspCode")) != "0000":
        return FAILURE, f"business_failure rspCode={payload.get('rspCode')} rspDesc={payload.get('rspDesc')}"
    status = str(parsed.get("customerPriceStatus"))
    if status not in HEALTHY_PARSER_STATUSES:
        return FAILURE, f"parser_status={status} warning={parsed.get('customerPriceWarning')}"
    return "HEALTHY", f"parser_status={status} price={parsed.get('customerPrice')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--document-id", default="", help="defaults to the documentId inside the template body")
    parser.add_argument("--category-id", default="", help="defaults to the fourCategoryId inside the template body")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    if not args.template.is_file():
        print(f"{FAILURE}: template missing: {args.template}")
        return 2
    template = load_curl_command(args.template)
    try:
        body = json.loads(template.get("data") or "{}")
    except json.JSONDecodeError:
        body = {}
    document_id = args.document_id or str(body.get("documentId") or "")
    category_id = args.category_id or str(body.get("fourCategoryId") or "")
    if not document_id or not category_id:
        print(f"{FAILURE}: no documentId/fourCategoryId available for the probe")
        return 2

    response = call_detail_api(dict(template), document_id, category_id, timeout=args.timeout)
    parsed = extract_customer_price_from_detail(response.get("payload"))
    verdict, detail = classify(response, parsed)
    print(f"{verdict}: documentId={document_id} fourCategoryId={category_id} {detail}")
    return 0 if verdict == "HEALTHY" else 1


if __name__ == "__main__":
    sys.exit(main())
