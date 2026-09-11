"""Single authoritative semantics for customer-price correctness gates.

Pure module: no HTTP, no database, no filesystem, no clock.  Every consumer
(runtime detail worker, tick workflow, durable store requeue, finalize gates,
status projection, dashboard) classifies customer-price evidence exclusively
through this module so the business meaning cannot drift between callers.

Three evidence families exist (mirror of ``extract_customer_price_from_detail``
and the detail worker's deterministic skips):

- FOUND: the detail API returned a usable reference price (or the row already
  carried one in the source export).
- RETRYABLE: ``NOT_FOUND`` / ``DETAIL_REQUEST_FAILED`` / ``DETAIL_PARSE_FAILED``
  — the outcome is not deterministic, so the row enters durable retry and, once
  the attempt budget is exhausted, blocks finalization as MISSING (NOT_FOUND)
  or FAILED (request/parse).
- ABSENT_SKIPPED: ``CUSTOMER_NOT_FOUND`` (legacy rows: ``LOOKUP_FAILED``) — the
  round's authoritative customer snapshot query succeeded and was complete, and
  the documentId is provably not in the customer export.  There is nothing to
  process this round: the row is a non-blocking skip
  (CUSTOMER_ABSENT_SKIPPED).  Evidence is preserved, no retry happens, and the
  row never blocks finalization (2026-09-11 business authorization).  All
  technical failures (auth/network/HTTP/pagination/snapshot drift) keep the
  whole tick failing and can never produce this state.
- PROVEN_ABSENT: ``DETAIL_REQUEST_SKIPPED`` / ``NOT_PROVIDED`` — the row has no
  detail identity or no price was provided, which is a different situation from
  document absence.  Retrying is meaningless; the enriched evidence still flows
  into review output, and the row still blocks formal finalization as
  MISSING_PROVEN (round-2 fail-closed ruling: no valid price means the
  price-review baseline is unresolved) and never auto-APPROVE.

Fail-closed invariant: any SUCCESS evidence without a positive price AND a
FOUND-family status is never READY — contradictory evidence (positive price
plus a failure/absence/unknown status) classifies as INVALID.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

CUSTOMER_PRICE_FOUND_STATUSES = frozenset({"FOUND", "FOUND_SINGLE_REFERENCE", "EXISTS"})
CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES = frozenset(
    {"NOT_FOUND", "DETAIL_REQUEST_FAILED", "DETAIL_PARSE_FAILED"}
)
# CUSTOMER_NOT_FOUND is the "authoritative customer snapshot round complete and
# the documentId is absent" status produced by the runtime detail worker.
# LOOKUP_FAILED is kept for legacy durable evidence compatibility: the
# 2026-09-11 audit proved its only producer was the same deterministic join
# path, so historical LOOKUP_FAILED rows project into the same non-blocking
# skip state without rewriting state databases.
CUSTOMER_PRICE_ABSENT_SKIPPED_STATUSES = frozenset({"CUSTOMER_NOT_FOUND", "LOOKUP_FAILED"})
# Identity missing / price not provided: a different situation from document
# absence — the row still lacks its price-review baseline and blocks as
# MISSING_PROVEN.
CUSTOMER_PRICE_PROVEN_ABSENT_STATUSES = frozenset(
    {"DETAIL_REQUEST_SKIPPED", "NOT_PROVIDED"}
)

# Matches TickConfig.detail_max_attempts; duplicated here only so pure
# read-only consumers (status/dashboard) can render the budget without
# importing the workflow module.
DETAIL_MAX_ATTEMPTS_DEFAULT = 3

# Structured, decodable, sanitized prefix persisted into stage_work.error_text
# for durable detail business failures.
BUSINESS_ERROR_PREFIX = "customerPriceStatus="
BUSINESS_ERROR_SEPARATOR = "；"


class DetailBusinessRetryRequired(RuntimeError):
    """A detail callback produced a retryable business failure, not a success.

    ``result`` carries the complete five-field customer-price evidence dict the
    worker would otherwise have returned; the durable store keeps it out of
    ``result_json`` (a business failure must never look like a successful
    completion) and persists the structured ``str()`` prefix into
    ``error_text`` instead.
    """

    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)
        status = _status_text(self.result)
        warning = sanitize_customer_price_text(str(self.result.get("customerPriceWarning") or ""))
        super().__init__(encode_business_error(status, warning))


class DetailRequestError(RuntimeError):
    """A detail HTTP transport-level failure with typed retry semantics.

    ``call_detail_api`` reports ``{ok, payload, http_status, error}``; the
    worker converts a not-ok response into this exception so the durable retry
    classifier can decide from the real HTTP status instead of a bare
    ``RuntimeError``.  ``http_status`` is ``None`` for non-HTTP failures
    (transport/timeout/decode); ``retryable`` mirrors
    :func:`is_retryable_detail_http_status`.
    """

    def __init__(self, *, http_status: int | None, message: object, retryable: bool) -> None:
        self.http_status = http_status
        self.message = str(message or "")
        self.retryable = retryable
        if http_status is not None:
            super().__init__(f"detail request failed: HTTP {http_status}")
        else:
            super().__init__(f"detail request failed: {self.message or 'transport error'}")


def is_retryable_detail_http_status(status: int | None) -> bool:
    """HTTP-semantic retry decision for one detail request failure.

    - ``None`` (transport/timeout/decode failure) is treated as transient.
    - 408/429 and all 5xx are classic transient/retry-after signals.
    - 403/412: in production the CNNC gateway answers 403/412 during its
      auth/WAF rotation window and the request succeeds again within the
      bounded retry budget, so they stay retryable here.
    - Every other 4xx (400/404/405/...) is a deterministic request error;
      retrying cannot change the answer.
    """
    if status is None:
        return True
    if not isinstance(status, int) or isinstance(status, bool):
        return True
    if status in {408, 429, 403, 412}:
        return True
    if status >= 500:
        return True
    return not 400 <= status < 500


def encode_business_error(status: str, warning: str) -> str:
    """Render the structured error prefix persisted into ``error_text``."""
    return f"{BUSINESS_ERROR_PREFIX}{status}{BUSINESS_ERROR_SEPARATOR}{warning or ''}"


def decode_business_error(text: object) -> tuple[str, str]:
    """Parse :func:`encode_business_error` output; ``("", "")`` when unprefixed."""
    value = str(text or "").strip()
    if not value.startswith(BUSINESS_ERROR_PREFIX):
        return "", ""
    body = value[len(BUSINESS_ERROR_PREFIX):]
    if BUSINESS_ERROR_SEPARATOR in body:
        status, warning = body.split(BUSINESS_ERROR_SEPARATOR, 1)
    else:
        status, warning = body, ""
    return status.strip(), warning.strip()


_SENSITIVE_KEY_RE = re.compile(
    r"(?i)\b(authorization|auth|cookie|set-cookie|token|csrf|x-csrf-token|tgc|tgt|"
    r"jsessionid|phpsessid|api[_-]?key|api[_-]?secret|secret|password|passwd|pwd)\b"
    r"(\s*[:=]\s*)([^\s;，,；]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+([^\s;，,；]+)")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")

REDACTED = "[REDACTED]"


def sanitize_customer_price_text(text: object) -> str:
    """Strip credential material before any customer-price text is persisted."""
    value = str(text or "")
    # JWT/bearer/long-hex first, so a token following "Authorization:" is not
    # partially left behind by the key=value redaction.
    value = _JWT_RE.sub(REDACTED, value)
    value = _BEARER_RE.sub(lambda match: f"{match.group(1)} {REDACTED}", value)
    value = _LONG_HEX_RE.sub(REDACTED, value)
    value = _SENSITIVE_KEY_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", value)
    return value


@dataclass(frozen=True)
class CustomerPriceBusinessState:
    """Business classification of one row's customer-price evidence.

    ``key`` is one of READY / PENDING / RETRYING / MISSING / MISSING_PROVEN /
    FAILED / INVALID / CUSTOMER_ABSENT_SKIPPED.  ``blocking`` marks whether the
    state must block ordinary finalize (only READY and CUSTOMER_ABSENT_SKIPPED
    never block; round-2 ruling made MISSING_PROVEN blocking because a required
    row whose price-review baseline is genuinely unresolved must not ship a
    formal workbook, while a proven documentId absence is "nothing to process
    this round" per the 2026-09-11 authorization).  ``wait_class`` tells a
    consumer how the row is expected to move on its own: ``WAIT_WORK`` /
    ``WAIT_RETRY`` resolve automatically, ``MANUAL_REVIEW`` rows need a human,
    and ``NONE`` needs nothing.
    """

    key: str
    status: str
    detail_state: str
    blocking: bool
    wait_class: str


READY = "READY"
PENDING = "PENDING"
RETRYING = "RETRYING"
MISSING = "MISSING"
MISSING_PROVEN = "MISSING_PROVEN"
FAILED = "FAILED"
INVALID = "INVALID"
CUSTOMER_ABSENT_SKIPPED = "CUSTOMER_ABSENT_SKIPPED"


def _status_text(result: Mapping[str, Any] | None) -> str:
    if not isinstance(result, Mapping):
        return ""
    return str(result.get("customerPriceStatus") or "").strip().upper()


def _parse_price(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return price if price.is_finite() else None


def _classify_success(result: Mapping[str, Any] | None) -> CustomerPriceBusinessState:
    """Fail-closed classification of a SUCCESS detail row (rule table authoritative).

    A positive price alone is never success: READY requires BOTH a positive
    price AND a FOUND-family status.  Any positive price combined with a
    failure/absence/unknown status is contradictory evidence -> INVALID.  An
    ABSENT_SKIPPED status with no valid price is CUSTOMER_ABSENT_SKIPPED (a
    non-blocking skip: authoritative query succeeded, documentId absent —
    2026-09-11 authorization).  A proven-absent status (no detail identity /
    price not provided) with no valid price is MISSING_PROVEN (blocking, round
    2); a retryable business failure with no valid price is the legacy
    invalid-success -> FAILED.  Everything else is INVALID.
    """
    price = _parse_price(result.get("customerPrice")) if isinstance(result, Mapping) else None
    status = _status_text(result)
    if price is not None and price > 0:
        if status in CUSTOMER_PRICE_FOUND_STATUSES:
            return CustomerPriceBusinessState(READY, status, "SUCCESS", False, "NONE")
        # Contradictory evidence (positive price + failed/absent/unknown
        # status) must never look like a usable price.
        return CustomerPriceBusinessState(INVALID, status, "SUCCESS", True, "MANUAL_REVIEW")
    if status in CUSTOMER_PRICE_ABSENT_SKIPPED_STATUSES:
        # Authoritative customer query succeeded and the documentId is absent:
        # nothing to process this round — a non-blocking skip, not a failure.
        return CustomerPriceBusinessState(CUSTOMER_ABSENT_SKIPPED, status, "SUCCESS", False, "NONE")
    if status in CUSTOMER_PRICE_PROVEN_ABSENT_STATUSES:
        # Deterministic absence: retrying is meaningless, but the row still
        # lacks its price-review baseline, so it blocks formal finalization
        # (round-2 fail-closed ruling) with the enriched evidence preserved.
        return CustomerPriceBusinessState(MISSING_PROVEN, status, "SUCCESS", True, "MANUAL_REVIEW")
    if status in CUSTOMER_PRICE_RETRYABLE_DETAIL_STATUSES:
        # Legacy durable state: a retryable business failure was checkpointed
        # as SUCCESS.  It is evidence of failure, never of a usable price.
        return CustomerPriceBusinessState(FAILED, status, "SUCCESS", True, "MANUAL_REVIEW")
    return CustomerPriceBusinessState(INVALID, status, "SUCCESS", True, "MANUAL_REVIEW")


def customer_price_business_state(
    detail_state: object,
    result: Mapping[str, Any] | None,
    error_text: object,
) -> CustomerPriceBusinessState:
    """Classify one row's customer-price evidence from its durable stage state.

    ``detail_state`` is the stage_work state (``None``/empty when the row has no
    DETAIL stage work yet), ``result`` the decoded ``result_json`` (or ``None``)
    and ``error_text`` the persisted failure text.  The rule table (authoritative,
    mirrored by tests/test_customer_price_semantics.py):

    ================ ============================== ======================= ========
    detail state     evidence                       key                     blocking
    ================ ============================== ======================= ========
    PENDING/CLAIMED  -                              PENDING                 yes
    RETRYABLE_FAILED -                              RETRYING                yes
    TERMINAL_FAILED  error prefix NOT_FOUND         MISSING                 yes
    TERMINAL_FAILED  REQUEST/PARSE prefix or none   FAILED                  yes
    SUCCESS          price>0 AND FOUND-family       READY                   no
    SUCCESS          price>0 AND other status       INVALID                 yes
    SUCCESS          no price AND ABSENT_SKIPPED    CUSTOMER_ABSENT_SKIPPED no
    SUCCESS          no price AND PROVEN_ABSENT     MISSING_PROVEN          yes
    SUCCESS          no price AND RETRYABLE (legacy) FAILED                 yes
    SUCCESS          price invalid / unknown status INVALID                 yes
    (no stage row)   -                              PENDING                 yes
    ================ ============================== ======================= ========
    """
    state = str(detail_state or "").strip().upper()
    if state == "SUCCESS":
        return _classify_success(result)
    if state == "TERMINAL_FAILED":
        status, _warning = decode_business_error(error_text)
        if status == "NOT_FOUND":
            return CustomerPriceBusinessState(MISSING, status, state, True, "MANUAL_REVIEW")
        return CustomerPriceBusinessState(FAILED, status, state, True, "MANUAL_REVIEW")
    if state == "RETRYABLE_FAILED":
        status, _warning = decode_business_error(error_text)
        return CustomerPriceBusinessState(RETRYING, status, state, True, "WAIT_RETRY")
    return CustomerPriceBusinessState(PENDING, "", state, True, "WAIT_WORK")


def customer_price_required(row: Mapping[str, Any] | None) -> bool:
    """Every audited row needs a customer price as its price-review baseline.

    Single source of truth for "does this row require customer-price evidence".
    The pipeline currently audits one commodity pool whose rows all use the
    customer price as the price-review baseline, so the predicate is
    unconditionally True; keep callers on this function (never inline the
    assumption) so a future exemption stays a one-line change here.
    取不到 ≠ 不需要：确定性缺席（MISSING_PROVEN）同样阻塞正式出表，
    除非未来找到明确业务豁免。
    """
    return True
