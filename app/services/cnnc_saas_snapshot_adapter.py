"""Read-only authoritative SaaS and CNNC snapshot discovery for incremental audit ticks."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Mapping
from urllib import error, request
from urllib.parse import urlparse

from scripts.auth_manager import AuthManager
from scripts.curl_utils import apply_curl_headers, internal_direct_opener, load_curl_command


TENANT_ID = "188605"
COMMODITY_POOL_ID = "222"
SAAS_QUERY_ALL_PATH = "/improxy/saas/iio2/sameAudit/queryAll"
SAAS_HOST = "saasportal.iio.com"
CNNC_TRUSTED_HOSTS = frozenset({"www.cnncmall.com", "one.cnncecp.com"})
CNNC_QUERY_PATH = "/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query"


@dataclass(frozen=True)
class SnapshotFetchPolicy:
    page_size: int = 200
    request_timeout_seconds: float = 60.0
    max_attempts_per_page: int = 3
    max_elapsed_seconds: float = 180.0
    retry_sleep_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.page_size, int) or not 1 <= self.page_size <= 200:
            raise ValueError("page_size must be an integer between 1 and 200")
        if not isinstance(self.max_attempts_per_page, int) or self.max_attempts_per_page <= 0:
            raise ValueError("max_attempts_per_page must be a positive integer")
        for field_name in (
            "request_timeout_seconds",
            "max_elapsed_seconds",
            "retry_sleep_seconds",
        ):
            value = float(getattr(self, field_name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field_name} must be positive and finite")


@dataclass(frozen=True)
class SnapshotPageEvidence:
    side: str
    page: int
    row_count: int
    count: int


@dataclass(frozen=True)
class AuthoritativeSnapshot:
    observed_at_utc: datetime
    saas_rows: tuple[dict[str, Any], ...]
    customer_rows: tuple[dict[str, Any], ...]
    saas_count: int
    customer_count: int
    page_evidence: tuple[SnapshotPageEvidence, ...]
    source_label: str | None = None


class SnapshotFetchExhausted(RuntimeError):
    def __init__(
        self,
        side: str,
        page: int,
        attempts: int,
        elapsed_seconds: float,
        last_cause: BaseException | None,
    ) -> None:
        self.side = side
        self.page = page
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.last_cause = last_cause
        cause_name = type(last_cause).__name__ if last_cause is not None else "deadline"
        super().__init__(
            f"{side} page {page} discovery exhausted after {attempts} attempts "
            f"in {elapsed_seconds:.3f}s: {cause_name}"
        )


class SnapshotConsistencyDrift(RuntimeError):
    """Pagination observed the authoritative source moving mid-fetch.

    A drifting round (count change, empty page, overrun, underrun) proves the
    rows collected so far belong to no single source state, so the entire
    round is voided and pagination restarts from page 1 under the original
    absolute deadline.
    """

    def __init__(self, side: str, reason: str) -> None:
        self.side = side
        self.reason = reason
        super().__init__(f"{side} snapshot consistency drift: {reason}")


SNAPSHOT_DRIFT_MAX_ROUNDS = 3


class _RetryableSnapshotResponse(RuntimeError):
    pass


class CnncSaasSnapshotAdapter:
    def __init__(
        self,
        saas_curl: Mapping[str, Any] | str | Path,
        customer_curl: Mapping[str, Any] | str | Path,
        *,
        policy: SnapshotFetchPolicy | None = None,
        auth_manager: AuthManager | None = None,
        opener: Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._saas_config = self._load_config(saas_curl)
        self._customer_config = self._load_config(customer_curl)
        self.policy = policy or SnapshotFetchPolicy()
        self.auth_manager = auth_manager
        self._opener = opener or internal_direct_opener()
        self._monotonic = monotonic
        self._sleep = sleep

    @staticmethod
    def _load_config(config: Mapping[str, Any] | str | Path) -> dict[str, Any]:
        if isinstance(config, (str, Path)):
            return dict(load_curl_command(Path(config)))
        return dict(config)

    @staticmethod
    def _request_body(config: Mapping[str, Any], side: str) -> dict[str, Any]:
        raw_body = config.get("data", config.get("body", ""))
        try:
            body = json.loads(str(raw_body or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{side} cURL request body is not valid JSON") from exc
        if not isinstance(body, dict):
            raise ValueError(f"{side} cURL request body must be a JSON object")
        return body

    @staticmethod
    def _validate_endpoint(
        config: Mapping[str, Any],
        side: str,
        expected_host: str | Collection[str],
        expected_path: str,
    ) -> None:
        parsed = urlparse(str(config.get("url") or ""))
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError(f"{side} cURL scheme must be http or https")
        if isinstance(expected_host, str):
            trusted_hosts = {expected_host.lower()}
        else:
            trusted_hosts = {host.lower() for host in expected_host}
        if (parsed.hostname or "").lower() not in trusted_hosts:
            raise ValueError(f"{side} cURL host is not trusted")
        if parsed.path != expected_path:
            raise ValueError(f"{side} cURL path is not the authoritative query endpoint")
        if str(config.get("method") or "POST").upper() != "POST":
            raise ValueError(f"{side} cURL method must be POST")

    def _validate_scope(self) -> tuple[dict[str, Any], dict[str, Any]]:
        saas_body = self._request_body(self._saas_config, "saas")
        customer_body = self._request_body(self._customer_config, "customer")
        self._validate_endpoint(self._saas_config, "saas", SAAS_HOST, SAAS_QUERY_ALL_PATH)
        self._validate_endpoint(self._customer_config, "customer", CNNC_TRUSTED_HOSTS, CNNC_QUERY_PATH)
        if str(saas_body.get("tenantId")) != TENANT_ID:
            raise ValueError("saas tenant scope must be 188605")
        if str(saas_body.get("commodityPoolId")) != COMMODITY_POOL_ID:
            raise ValueError("saas commodity pool scope must be 222")
        if customer_body.get("sassCheckStatuss") != ["0"]:
            raise ValueError("customer cURL must retain sassCheckStatuss == ['0']")
        return saas_body, customer_body

    @staticmethod
    def _rows_from_response(payload: Mapping[str, Any], side: str) -> list[dict[str, Any]]:
        rows: Any = payload.get("rows")
        if rows is None:
            nested = payload.get("data")
            if isinstance(nested, Mapping):
                rows = nested.get("rows", nested.get("records"))
            else:
                rows = nested
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise RuntimeError(f"{side} response rows must be a list of objects")
        return rows

    @staticmethod
    def _count_from_response(payload: Mapping[str, Any], side: str) -> int:
        value: Any = payload.get("count")
        if value is None and isinstance(payload.get("data"), Mapping):
            nested = payload["data"]
            value = nested.get("count", nested.get("total"))
        if isinstance(value, bool):
            raise RuntimeError(f"{side} response count is invalid")
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{side} response count is invalid") from exc
        if count < 0:
            raise RuntimeError(f"{side} response count is invalid")
        return count

    @staticmethod
    def _is_retryable_response(payload: Mapping[str, Any]) -> bool:
        code = str(payload.get("rspCode") or "")
        return bool(payload.get("retryable")) or code in {
            "408",
            "429",
            "500",
            "502",
            "503",
            "504",
            "TEMPORARY_ERROR",
            "RETRYABLE",
        }

    @staticmethod
    def _decode_response(response: Any) -> dict[str, Any]:
        headers = getattr(response, "headers", None)
        charset = headers.get_content_charset() if headers is not None else None
        payload = json.loads(response.read().decode(charset or "utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("response must be a JSON object")
        return payload

    def _request_factory(
        self,
        config: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> Callable[[], request.Request]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        method = str(config.get("method") or "POST").upper()

        def factory() -> request.Request:
            outbound = request.Request(str(config.get("url") or ""), data=body, method=method)
            apply_curl_headers(
                outbound,
                config.get("headers") or [],
                str(config.get("cookie") or ""),
            )
            return outbound

        return factory

    @staticmethod
    def _is_retryable_exception(exc: BaseException) -> bool:
        if isinstance(exc, error.HTTPError):
            return exc.code in {408, 429} or 500 <= exc.code <= 599
        return isinstance(exc, (error.URLError, TimeoutError, OSError, json.JSONDecodeError, _RetryableSnapshotResponse))

    def _request_page(
        self,
        side: str,
        page: int,
        config: Mapping[str, Any],
        payload: Mapping[str, Any],
        deadline: float,
        started_at: float,
    ) -> dict[str, Any]:
        attempts = 0
        last_cause: BaseException | None = None
        factory = self._request_factory(config, payload)
        while attempts < self.policy.max_attempts_per_page:
            now = self._monotonic()
            remaining_seconds = deadline - now
            if remaining_seconds <= 0:
                raise SnapshotFetchExhausted(
                    side, page, attempts, max(now - started_at, 0.0), last_cause
                )
            attempts += 1
            timeout = min(self.policy.request_timeout_seconds, remaining_seconds)
            try:
                if side == "saas" and self.auth_manager is not None:
                    response_payload = self.auth_manager.request_json(
                        "saas",
                        factory,
                        read_only=True,
                        opener=self._opener,
                        timeout=timeout,
                    )
                else:
                    with self._opener.open(factory(), timeout=timeout) as response:
                        response_payload = self._decode_response(response)
                now = self._monotonic()
                if now > deadline:
                    raise SnapshotFetchExhausted(
                        side, page, attempts, max(now - started_at, 0.0), TimeoutError("discovery deadline exceeded")
                    )
                if not isinstance(response_payload, dict):
                    raise RuntimeError(f"{side} response must be a JSON object")
                if str(response_payload.get("rspCode")) != "0000":
                    if self._is_retryable_response(response_payload):
                        raise _RetryableSnapshotResponse(f"{side} retryable response")
                    raise RuntimeError(f"{side} response rspCode is not 0000")
                return response_payload
            except Exception as exc:
                if not self._is_retryable_exception(exc):
                    raise
                last_cause = exc

            now = self._monotonic()
            remaining_seconds = deadline - now
            if attempts >= self.policy.max_attempts_per_page or remaining_seconds <= 0:
                raise SnapshotFetchExhausted(
                    side, page, attempts, max(now - started_at, 0.0), last_cause
                ) from None
            self._sleep(min(self.policy.retry_sleep_seconds, remaining_seconds))

        raise SnapshotFetchExhausted(
            side,
            page,
            attempts,
            max(self._monotonic() - started_at, 0.0),
            last_cause,
        )

    def _fetch_side(
        self,
        side: str,
        config: Mapping[str, Any],
        base_body: Mapping[str, Any],
        deadline: float,
        started_at: float,
    ) -> tuple[tuple[dict[str, Any], ...], int, tuple[SnapshotPageEvidence, ...]]:
        for attempt in range(1, SNAPSHOT_DRIFT_MAX_ROUNDS + 1):
            try:
                return self._fetch_side_round(side, config, base_body, deadline, started_at)
            except SnapshotConsistencyDrift:
                # A drifted round is void: nothing it collected may leak into
                # an authoritative snapshot.  Restart from page 1 while every
                # round still shares the same absolute deadline.
                if attempt >= SNAPSHOT_DRIFT_MAX_ROUNDS or self._monotonic() >= deadline:
                    raise
        raise AssertionError("unreachable drift retry loop")

    def _fetch_side_round(
        self,
        side: str,
        config: Mapping[str, Any],
        base_body: Mapping[str, Any],
        deadline: float,
        started_at: float,
    ) -> tuple[tuple[dict[str, Any], ...], int, tuple[SnapshotPageEvidence, ...]]:
        rows: list[dict[str, Any]] = []
        evidence: list[SnapshotPageEvidence] = []
        expected_count: int | None = None
        page = 1
        while expected_count is None or len(rows) < expected_count:
            payload = {**base_body, "page": page, "limit": self.policy.page_size}
            response_payload = self._request_page(
                side, page, config, payload, deadline, started_at
            )
            page_rows = self._rows_from_response(response_payload, side)
            count = self._count_from_response(response_payload, side)
            if expected_count is None:
                expected_count = count
            elif count != expected_count:
                raise SnapshotConsistencyDrift(side, "count changed during pagination")
            if not page_rows and len(rows) < count:
                raise SnapshotConsistencyDrift(side, "empty page before count")
            if len(rows) + len(page_rows) > count:
                raise SnapshotConsistencyDrift(side, "pagination overrun")
            rows.extend(page_rows)
            evidence.append(SnapshotPageEvidence(side, page, len(page_rows), count))
            if len(rows) == count:
                break
            page += 1
        if expected_count is None or len(rows) != expected_count:
            raise SnapshotConsistencyDrift(side, "pagination underrun")
        return tuple(rows), expected_count, tuple(evidence)

    def fetch(
        self,
        *,
        source_label: str | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> AuthoritativeSnapshot:
        if max_elapsed_seconds is not None:
            try:
                max_elapsed_seconds = float(max_elapsed_seconds)
            except (TypeError, ValueError) as exc:
                raise ValueError("max_elapsed_seconds must be positive and finite") from exc
            if not math.isfinite(max_elapsed_seconds) or max_elapsed_seconds <= 0:
                raise ValueError("max_elapsed_seconds must be positive and finite")
        saas_body, customer_body = self._validate_scope()
        started_at = self._monotonic()
        deadline = started_at + min(self.policy.max_elapsed_seconds, max_elapsed_seconds or self.policy.max_elapsed_seconds)
        saas_rows, saas_count, saas_pages = self._fetch_side(
            "saas", self._saas_config, saas_body, deadline, started_at
        )
        customer_rows, customer_count, customer_pages = self._fetch_side(
            "customer", self._customer_config, customer_body, deadline, started_at
        )
        return AuthoritativeSnapshot(
            observed_at_utc=datetime.now(timezone.utc),
            saas_rows=saas_rows,
            customer_rows=customer_rows,
            saas_count=saas_count,
            customer_count=customer_count,
            page_evidence=(*saas_pages, *customer_pages),
            source_label=source_label,
        )


def snapshot_to_json_payloads(snapshot: AuthoritativeSnapshot) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return legacy-compatible JSON payloads without changing row order or multiplicity."""

    return (
        {
            "rspCode": "0000",
            "count": snapshot.saas_count,
            "rows": list(snapshot.saas_rows),
            "success": True,
        },
        {
            "rspCode": "0000",
            "count": snapshot.customer_count,
            "rows": list(snapshot.customer_rows),
            "success": True,
        },
    )
