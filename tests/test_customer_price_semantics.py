"""Rule-table contracts for the customer-price semantics module (plan §3)."""

from __future__ import annotations

from decimal import Decimal

from app.services.customer_price_semantics import (
    CUSTOMER_PRICE_FOUND_STATUSES,
    CUSTOMER_PRICE_PROVEN_ABSENT_STATUSES,
    CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES,
    DETAIL_MAX_ATTEMPTS_DEFAULT,
    DetailBusinessRetryRequired,
    DetailRequestError,
    customer_price_business_state,
    customer_price_required,
    decode_business_error,
    encode_business_error,
    is_retryable_detail_http_status,
    sanitize_customer_price_text,
)


def _success_result(price=None, status="FOUND", **extra):
    return {
        "customerPrice": price,
        "customerPriceStatus": status,
        "customerPriceSource": "DETAIL_REFERENCE_INFOS",
        "customerPriceWarning": "",
        "customerPriceEvidence": "参考价1=10",
        **extra,
    }


# ---- Status families -------------------------------------------------------


def test_status_families_match_the_detail_worker_vocabulary():
    assert CUSTOMER_PRICE_FOUND_STATUSES == {"FOUND", "FOUND_SINGLE_REFERENCE", "EXISTS"}
    assert CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES == {
        "NOT_FOUND",
        "DETAIL_REQUEST_FAILED",
        "DETAIL_PARSE_FAILED",
    }
    assert CUSTOMER_PRICE_PROVEN_ABSENT_STATUSES == {
        "LOOKUP_FAILED",
        "DETAIL_REQUEST_SKIPPED",
        "NOT_PROVIDED",
    }
    assert DETAIL_MAX_ATTEMPTS_DEFAULT == 3


# ---- Classifier rule table -------------------------------------------------


def test_pending_states_block():
    for state in (None, "", "PENDING", "CLAIMED", "SOME_FUTURE_STATE"):
        classified = customer_price_business_state(state, None, "")
        assert classified.key == "PENDING"
        assert classified.blocking is True
        assert classified.wait_class == "WAIT_WORK"


def test_retryable_failed_is_retrying_and_blocks():
    classified = customer_price_business_state(
        "RETRYABLE_FAILED",
        None,
        encode_business_error("NOT_FOUND", "详情接口未返回有效参考价"),
    )
    assert classified.key == "RETRYING"
    assert classified.status == "NOT_FOUND"
    assert classified.blocking is True
    assert classified.wait_class == "WAIT_RETRY"


def test_terminal_not_found_prefix_is_missing():
    classified = customer_price_business_state(
        "TERMINAL_FAILED",
        None,
        encode_business_error("NOT_FOUND", "详情接口未返回有效参考价"),
    )
    assert classified.key == "MISSING"
    assert classified.status == "NOT_FOUND"
    assert classified.blocking is True
    assert classified.wait_class == "MANUAL_REVIEW"


def test_terminal_request_failed_prefix_is_failed():
    classified = customer_price_business_state(
        "TERMINAL_FAILED",
        None,
        encode_business_error("DETAIL_REQUEST_FAILED", "rspCode=500"),
    )
    assert classified.key == "FAILED"
    assert classified.status == "DETAIL_REQUEST_FAILED"
    assert classified.blocking is True


def test_terminal_without_prefix_is_failed():
    classified = customer_price_business_state("TERMINAL_FAILED", None, "TimeoutError")
    assert classified.key == "FAILED"
    assert classified.status == ""
    assert classified.blocking is True


def test_terminal_parse_failed_prefix_is_failed():
    classified = customer_price_business_state(
        "TERMINAL_FAILED",
        None,
        encode_business_error("DETAIL_PARSE_FAILED", "详情接口返回不是 JSON 对象"),
    )
    assert classified.key == "FAILED"
    assert classified.status == "DETAIL_PARSE_FAILED"


def test_success_with_positive_price_and_found_status_is_ready():
    """Round-2 fail-closed rule: READY requires a positive price AND a
    FOUND-family status.  A positive price alone is never success."""
    for status in sorted(CUSTOMER_PRICE_FOUND_STATUSES):
        classified = customer_price_business_state("SUCCESS", _success_result("10", status), "")
        assert classified.key == "READY"
        assert classified.blocking is False
        assert classified.wait_class == "NONE"


def test_positive_price_with_failure_status_is_invalid_contradiction():
    """Contradictory evidence (price=100 + failed status) is never READY."""
    for status in sorted(CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES | CUSTOMER_PRICE_PROVEN_ABSENT_STATUSES):
        classified = customer_price_business_state("SUCCESS", _success_result("100", status), "")
        assert classified.key == "INVALID", status
        assert classified.blocking is True
        assert classified.wait_class == "MANUAL_REVIEW"


def test_positive_price_with_unknown_or_missing_status_is_invalid():
    for status in ("", "SOME_FUTURE_STATUS"):
        classified = customer_price_business_state("SUCCESS", _success_result("100", status), "")
        assert classified.key == "INVALID", status
        assert classified.blocking is True


def test_success_accepts_decimal_and_numeric_string_prices():
    assert customer_price_business_state("SUCCESS", _success_result(Decimal("3.5")), "").key == "READY"
    assert customer_price_business_state("SUCCESS", _success_result(" 12.50 "), "").key == "READY"
    assert customer_price_business_state("SUCCESS", _success_result(10), "").key == "READY"


def test_success_lookup_failed_is_missing_proven_and_blocks_finalization():
    """Round-2 ruling: deterministic absence blocks formal finalization too."""
    classified = customer_price_business_state(
        "SUCCESS",
        _success_result(None, "LOOKUP_FAILED", customerPriceWarning="customer export did not contain this documentId"),
        "",
    )
    assert classified.key == "MISSING_PROVEN"
    assert classified.status == "LOOKUP_FAILED"
    assert classified.blocking is True
    assert classified.wait_class == "MANUAL_REVIEW"


def test_success_request_skipped_and_not_provided_are_missing_proven_and_blocking():
    for status in ("DETAIL_REQUEST_SKIPPED", "NOT_PROVIDED"):
        classified = customer_price_business_state("SUCCESS", _success_result(None, status), "")
        assert classified.key == "MISSING_PROVEN"
        assert classified.blocking is True
        assert classified.wait_class == "MANUAL_REVIEW"


def test_success_with_legacy_retryable_status_is_invalid_success_and_blocks():
    for status in sorted(CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES):
        classified = customer_price_business_state("SUCCESS", _success_result(None, status), "")
        assert classified.key == "FAILED"
        assert classified.blocking is True
        assert classified.status == status


def test_success_with_zero_unparseable_or_missing_price_is_invalid():
    zero = customer_price_business_state("SUCCESS", _success_result("0", "FOUND"), "")
    assert zero.key == "INVALID"
    assert zero.blocking is True
    negative = customer_price_business_state("SUCCESS", _success_result("-1", "FOUND"), "")
    assert negative.key == "INVALID"
    unparseable = customer_price_business_state("SUCCESS", _success_result("贵", "FOUND"), "")
    assert unparseable.key == "INVALID"
    missing_key = customer_price_business_state("SUCCESS", {"customerPriceStatus": "FOUND"}, "")
    assert missing_key.key == "INVALID"
    broken_json = customer_price_business_state("SUCCESS", None, "")
    assert broken_json.key == "INVALID"


# ---- encode / decode roundtrip ---------------------------------------------


def test_encode_decode_roundtrip():
    text = encode_business_error("NOT_FOUND", "详情接口未返回有效参考价")
    assert text == "customerPriceStatus=NOT_FOUND；详情接口未返回有效参考价"
    assert decode_business_error(text) == ("NOT_FOUND", "详情接口未返回有效参考价")


def test_decode_without_prefix_returns_empty_status():
    assert decode_business_error("TimeoutError") == ("", "")
    assert decode_business_error(None) == ("", "")
    assert decode_business_error("customerPriceStatus=NOT_FOUND") == ("NOT_FOUND", "")
    assert decode_business_error("customerPriceStatus=NOT_FOUND；") == ("NOT_FOUND", "")


def test_detail_business_retry_required_carries_result_and_encoded_str():
    result = _success_result(None, "NOT_FOUND", customerPriceWarning="详情接口未返回有效参考价")
    exc = DetailBusinessRetryRequired(result)
    assert exc.result["customerPriceStatus"] == "NOT_FOUND"
    assert str(exc) == encode_business_error("NOT_FOUND", "详情接口未返回有效参考价")
    assert decode_business_error(str(exc))[0] == "NOT_FOUND"


# ---- typed detail transport failure (round 2, P1-4) ------------------------


def test_detail_request_error_renders_http_status_or_message():
    with_status = DetailRequestError(http_status=500, message="HTTP 500", retryable=True)
    assert with_status.http_status == 500
    assert with_status.retryable is True
    assert str(with_status) == "detail request failed: HTTP 500"
    without_status = DetailRequestError(http_status=None, message="详情接口超时", retryable=True)
    assert without_status.http_status is None
    assert str(without_status) == "detail request failed: 详情接口超时"
    assert isinstance(without_status, RuntimeError)


def test_retryable_detail_http_status_classification():
    # No status (transport/timeout/decode): transient by definition.
    assert is_retryable_detail_http_status(None) is True
    # Classic transient statuses.
    assert is_retryable_detail_http_status(408) is True
    assert is_retryable_detail_http_status(429) is True
    for code in (500, 502, 503, 504, 599):
        assert is_retryable_detail_http_status(code) is True, code
    # CNNC gateway auth/WAF rotation window: retryable within the bounded budget.
    assert is_retryable_detail_http_status(403) is True
    assert is_retryable_detail_http_status(412) is True
    # Deterministic request errors never retry.
    for code in (400, 401, 404, 405, 418, 422):
        assert is_retryable_detail_http_status(code) is False, code


# ---- sanitize --------------------------------------------------------------


def test_sanitize_strips_authorization_cookie_token_tgc_and_long_hex():
    text = (
        "Authorization: Bearer abc123def456; Cookie: TGC=ST-123456-fedcba9876543210; "
        "access_token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c; "
        "token=deadbeefdeadbeef; ok=参考价10"
    )
    cleaned = sanitize_customer_price_text(text)
    assert "abc123def456" not in cleaned
    assert "ST-123456-fedcba9876543210" not in cleaned
    assert "eyJzdWIiOiIxMjM0NTY3ODkwIn0" not in cleaned
    assert "deadbeefdeadbeef" not in cleaned
    assert "参考价10" in cleaned
    assert "[REDACTED]" in cleaned


def test_sanitize_keeps_business_text_unchanged():
    text = "详情接口业务失败：rspCode=0001；rspDesc=无权限查看该商品"
    assert sanitize_customer_price_text(text) == text
    assert sanitize_customer_price_text(None) == ""
    assert sanitize_customer_price_text("") == ""


# ---- required predicate ----------------------------------------------------


def test_customer_price_required_is_true_for_every_row():
    assert customer_price_required({}) is True
    assert customer_price_required({"customerPrice": None}) is True
    assert customer_price_required(None) is True
