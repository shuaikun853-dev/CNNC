# -*- coding: utf-8 -*-
"""Offline tests for the read-only CNNC detail smoke check verdict logic."""
from __future__ import annotations

from scripts.cnnc_detail_smoke import FAILURE, classify


def _parsed(status: str, warning: str = "") -> dict:
    return {"customerPriceStatus": status, "customerPriceWarning": warning, "customerPrice": None}


def test_transport_error_is_failure():
    verdict, detail = classify({"ok": False, "payload": None, "error": "HTTP 401"}, _parsed("NOT_FOUND"))
    assert verdict == FAILURE
    assert "transport_error=HTTP 401" in detail


def test_business_error_envelope_is_failure():
    response = {"ok": True, "payload": {"success": False, "rspCode": "8888", "rspDesc": "潜在标准商品数据详情异常", "data": ""}}
    verdict, detail = classify(response, _parsed("DETAIL_REQUEST_FAILED", "详情接口业务失败：rspCode=8888"))
    assert verdict == FAILURE
    assert "business_failure" in detail
    assert "8888" in detail


def test_success_with_found_price_is_healthy():
    response = {"ok": True, "payload": {"success": True, "rspCode": "0000", "data": {"referencePrice": "1"}}}
    verdict, _ = classify(response, _parsed("FOUND_SINGLE_REFERENCE"))
    assert verdict == "HEALTHY"


def test_success_without_references_is_healthy_not_found():
    response = {"ok": True, "payload": {"success": True, "rspCode": "0000", "data": {}}}
    verdict, detail = classify(response, _parsed("NOT_FOUND"))
    assert verdict == "HEALTHY"
    assert "parser_status=NOT_FOUND" in detail


def test_non_dict_payload_is_failure():
    verdict, _ = classify({"ok": True, "payload": []}, _parsed("DETAIL_PARSE_FAILED"))
    assert verdict == FAILURE
