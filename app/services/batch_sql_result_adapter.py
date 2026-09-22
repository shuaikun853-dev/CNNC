"""Fail-closed SQL result lookup for exact Zhaoshang batch identities."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
import math
import re
import time
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from app.services.page_price_semantics import valid_page_price_result
from app.services.zhaoshang_url_identity import strict_batch_result_url_match


MAX_QUERY_EXECUTE_NUMBERS = 100
ALLOWED_MALLS = frozenset(
    {
        "colipu",
        "comix",
        "ehsy",
        "gongpin",
        "guangbo",
        "haoxiny",
        "jd",
        "lxwl",
        "lxwlmall",
        "nbdeli",
        "qstbg",
        "suning",
        "tima",
        "xfs",
        "xhgj",
        "ycw",
        "zjmi",
        "zkh",
    }
)


class DbConnection(Protocol):
    def cursor(self) -> Any:
        ...

    def rollback(self) -> None:
        ...

    def close(self) -> None:
        ...


class BatchResultStatus(str, Enum):
    FOUND = "FOUND"
    NOT_YET_VISIBLE = "NOT_YET_VISIBLE"
    TERMINAL_NO_RESULT = "TERMINAL_NO_RESULT"
    CRAWLER_FAILED = "CRAWLER_FAILED"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    INVALID_RESULT = "INVALID_RESULT"


@dataclass(frozen=True)
class BatchResultEvidence:
    task_status: str
    query_progress: str | None = None
    terminal_no_result_proven: bool = False

    def __post_init__(self) -> None:
        task_status = str(self.task_status or "").strip()
        if task_status not in {"00", "01", "02", "03"}:
            raise ValueError("task_status must be one of 00, 01, 02, or 03")
        object.__setattr__(self, "task_status", task_status)


@dataclass(frozen=True)
class BatchResultTarget:
    execute_number: str
    expected_sku: str
    expected_url: str
    evidence: BatchResultEvidence

    def __post_init__(self) -> None:
        execute_number = str(self.execute_number or "").strip()
        expected_sku = str(self.expected_sku or "").strip()
        expected_url = str(self.expected_url or "").strip()
        if not execute_number:
            raise ValueError("execute_number is required")
        if not expected_sku:
            raise ValueError("expected_sku is required")
        if not expected_url:
            raise ValueError("expected_url is required")
        object.__setattr__(self, "execute_number", execute_number)
        object.__setattr__(self, "expected_sku", expected_sku)
        object.__setattr__(self, "expected_url", expected_url)


@dataclass(frozen=True)
class BatchSqlResult:
    execute_number: str
    status: BatchResultStatus
    diagnostic: str = ""
    goods_no: Any = None
    sku_id: Any = None
    title: Any = None
    price: Any = None
    comment: Any = None
    url: Any = None
    is_self: Any = None
    spu_id: Any = None
    spu_name: Any = None


class BatchSqlResultQueryError(RuntimeError):
    """A read failed without changing the durable remote identity."""

    def __init__(self, message: str, *, execute_numbers: Sequence[str]) -> None:
        super().__init__(message)
        self.execute_numbers = tuple(execute_numbers)


ConnectionFactory = Callable[[], DbConnection]
TimedConnectionFactory = Callable[[float], DbConnection]


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _placeholders(count: int) -> str:
    return ", ".join("%s" for _ in range(count))


def _result_sql(mall: str, count: int) -> str:
    if mall not in ALLOWED_MALLS:
        raise ValueError("mall is not allowlisted")
    return f"""
SELECT
    m.exe_number AS execute_number,
    m.goods_no AS price_goods_no,
    m.sku_id AS price_sku_id,
    j.goods_no,
    j.sku_id,
    j.title,
    j.price,
    j.`comment` AS comment,
    j.url,
    j.is_self,
    j.spu_id,
    j.spu_name
FROM shopping_spider.{mall}_price m
JOIN shopping_spider.{mall}_goods j
  ON j.sku_id = m.sku_id
WHERE m.exe_number IN ({_placeholders(count)})
"""


_NBDELI_NUMERIC_PCS = re.compile(r"^(?P<sku>[0-9]+)PCS$")


def _canonical_sku(mall: str, value: Any) -> str:
    text = str(value or "").strip()
    if mall == "nbdeli":
        match = _NBDELI_NUMERIC_PCS.fullmatch(text)
        if match is not None:
            return match.group("sku")
    return text


def _urls_match(mall: str, left: Any, right: Any) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if left_text == right_text:
        return bool(left_text)
    if strict_batch_result_url_match(mall, left_text, right_text):
        return True
    if mall != "nbdeli" or not left_text or not right_text:
        return False
    try:
        left_parts = urlsplit(left_text)
        right_parts = urlsplit(right_text)
    except ValueError:
        return False
    if (
        left_parts.scheme != right_parts.scheme
        or left_parts.netloc != right_parts.netloc
        or left_parts.path != right_parts.path
        or left_parts.fragment != right_parts.fragment
    ):
        return False
    left_query = left_parts.query.split("&")
    right_query = right_parts.query.split("&")
    if len(left_query) != len(right_query):
        return False
    for left_item, right_item in zip(left_query, right_query, strict=True):
        if left_item == right_item:
            continue
        left_key, left_sep, left_value = left_item.partition("=")
        right_key, right_sep, right_value = right_item.partition("=")
        if (
            left_sep != "="
            or right_sep != "="
            or left_key != "sku"
            or right_key != "sku"
            or _canonical_sku(mall, left_value)
            != _canonical_sku(mall, right_value)
        ):
            return False
    return True


def _matches_identity(mall: str, row: Mapping[str, Any], target: BatchResultTarget) -> bool:
    sku_values = {
        _canonical_sku(mall, row.get("price_goods_no")),
        _canonical_sku(mall, row.get("price_sku_id")),
        _canonical_sku(mall, row.get("goods_no")),
        _canonical_sku(mall, row.get("sku_id")),
    }
    return _canonical_sku(mall, target.expected_sku) in sku_values and _urls_match(
        mall, row.get("url"), target.expected_url
    )


def _result_price(value: Any) -> Decimal | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        price = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not price.is_finite() or price < 0:
        return None
    return price


def _invalid_result_reason(row: Mapping[str, Any]) -> str | None:
    price = _result_price(row.get("price"))
    if price is None:
        return "SQL result price is missing, non-numeric, non-finite, or negative"
    if price > 0 and not str(row.get("title") or "").strip():
        return "positive SQL result price requires a nonblank title"
    return None


def batch_sql_result_to_crawl_result(
    mall: str, result: BatchSqlResult
) -> Mapping[str, Any]:
    """Convert one provenance-valid SQL row into shared page-price semantics."""

    mall = str(mall or "").strip()
    if mall not in ALLOWED_MALLS:
        raise ValueError("mall is not allowlisted")
    if result.status is not BatchResultStatus.FOUND:
        raise ValueError("only FOUND batch SQL results can become crawl results")
    price = _result_price(result.price)
    if price is None:
        raise ValueError("FOUND batch SQL result has an invalid price")
    if price == 0:
        status = "PRICE_IS_ZERO"
        title = "已下架"
    else:
        status = "OK"
        title = str(result.title or "").strip()
        if not title:
            raise ValueError("FOUND positive batch SQL result has no title")
    crawl_result = {
        "page_price_status": status,
        "page_price_value": price,
        "page_title": title,
        "comment_count": result.comment,
        "page_price_source": f"batch_sql:{mall}",
    }
    if not valid_page_price_result(crawl_result):
        raise ValueError("converted batch SQL result violates page-price semantics")
    return crawl_result


def _missing_result(target: BatchResultTarget) -> BatchSqlResult:
    task_status = target.evidence.task_status
    if task_status in {"00", "01"}:
        status = BatchResultStatus.NOT_YET_VISIBLE
        diagnostic = "crawler task remains active and no exact SQL result is visible"
    elif task_status == "03":
        status = BatchResultStatus.CRAWLER_FAILED
        diagnostic = "crawler task failed before an exact SQL result was found"
    elif target.evidence.terminal_no_result_proven:
        status = BatchResultStatus.TERMINAL_NO_RESULT
        diagnostic = "terminal no-result evidence was supplied"
    else:
        status = BatchResultStatus.EVIDENCE_INCOMPLETE
        diagnostic = "task_status 02 with zero rows requires spider-log or equivalent terminal evidence"
    return BatchSqlResult(execute_number=target.execute_number, status=status, diagnostic=diagnostic)


class BatchSqlResultAdapter:
    """Query only the requested allowlisted mall using bounded exact identities."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        timed_connection_factory: TimedConnectionFactory | None = None,
        chunk_size: int = MAX_QUERY_EXECUTE_NUMBERS,
        max_concurrency: int = 3,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if chunk_size < 1 or chunk_size > MAX_QUERY_EXECUTE_NUMBERS:
            raise ValueError("chunk_size must be between 1 and 100")
        if max_concurrency < 1 or max_concurrency > 3:
            raise ValueError("max_concurrency must be between 1 and 3")
        self._connection_factory = connection_factory
        self._timed_connection_factory = timed_connection_factory
        self._chunk_size = chunk_size
        self._max_concurrency = max_concurrency
        self._monotonic = monotonic

    def fetch_batch_results(
        self,
        mall: str,
        targets: Sequence[BatchResultTarget],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[BatchSqlResult, ...]:
        mall = str(mall or "").strip()
        if mall not in ALLOWED_MALLS:
            raise ValueError("mall is not allowlisted")
        requested = tuple(targets)
        if not requested:
            raise ValueError("at least one target is required")
        execute_numbers = tuple(target.execute_number for target in requested)
        if len(set(execute_numbers)) != len(execute_numbers):
            raise ValueError("execute_numbers must be unique")

        deadline = (
            self._monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        )
        connection: DbConnection | None = None

        def prepare_database_operation() -> float | None:
            if deadline is None:
                return None
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("batch SQL result deadline is exhausted")
            if connection is not None:
                socket_timeout = max(1, int(math.ceil(remaining)))
                for attribute in ("_read_timeout", "_write_timeout"):
                    if hasattr(connection, attribute):
                        setattr(connection, attribute, socket_timeout)
            return remaining

        if timeout_seconds is not None and timeout_seconds <= 0:
            raise TimeoutError("batch SQL result deadline is exhausted")
        connection = (
            self._timed_connection_factory(prepare_database_operation())
            if timeout_seconds is not None
            and self._timed_connection_factory is not None
            else self._connection_factory()
        )
        rows: list[Mapping[str, Any]] = []
        try:
            with connection.cursor() as cursor:
                for group in _chunks(execute_numbers, self._chunk_size):
                    prepare_database_operation()
                    cursor.execute(_result_sql(mall, len(group)), tuple(group))
                    rows.extend(dict(row) for row in cursor.fetchall())
            prepare_database_operation()
            connection.rollback()
        except Exception as exc:
            try:
                if deadline is None or deadline - self._monotonic() > 0:
                    prepare_database_operation()
                    connection.rollback()
            finally:
                raise BatchSqlResultQueryError(
                    f"batch SQL result lookup failed: {type(exc).__name__}",
                    execute_numbers=execute_numbers,
                ) from exc
        finally:
            connection.close()

        rows_by_execute: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            execute_number = str(row.get("execute_number") or "").strip()
            rows_by_execute.setdefault(execute_number, []).append(row)

        results: list[BatchSqlResult] = []
        for target in requested:
            candidates = rows_by_execute.get(target.execute_number, [])
            if not candidates:
                results.append(_missing_result(target))
                continue
            if len(candidates) != 1:
                results.append(
                    BatchSqlResult(
                        execute_number=target.execute_number,
                        status=BatchResultStatus.IDENTITY_CONFLICT,
                        diagnostic=(
                            "exact SQL result query returned "
                            f"{len(candidates)} candidate rows"
                        ),
                    )
                )
                continue
            matches = [row for row in candidates if _matches_identity(mall, row, target)]
            if len(matches) != 1:
                results.append(
                    BatchSqlResult(
                        execute_number=target.execute_number,
                        status=BatchResultStatus.IDENTITY_CONFLICT,
                        diagnostic=(
                            f"exact SQL result identity matched {len(matches)} of {len(candidates)} candidate rows"
                        ),
                    )
                )
                continue
            row = matches[0]
            invalid_reason = _invalid_result_reason(row)
            if invalid_reason is not None:
                results.append(
                    BatchSqlResult(
                        execute_number=target.execute_number,
                        status=BatchResultStatus.INVALID_RESULT,
                        diagnostic=invalid_reason,
                    )
                )
                continue
            results.append(
                BatchSqlResult(
                    execute_number=target.execute_number,
                    status=BatchResultStatus.FOUND,
                    goods_no=row.get("goods_no"),
                    sku_id=row.get("sku_id"),
                    title=row.get("title"),
                    price=row.get("price"),
                    comment=row.get("comment"),
                    url=row.get("url"),
                    is_self=row.get("is_self"),
                    spu_id=row.get("spu_id"),
                    spu_name=row.get("spu_name"),
                )
            )
        return tuple(results)

    def fetch_multi_mall_results(
        self,
        targets_by_mall: Mapping[str, Sequence[BatchResultTarget]],
    ) -> dict[str, tuple[BatchSqlResult, ...]]:
        requested = {str(mall or "").strip(): tuple(targets) for mall, targets in targets_by_mall.items()}
        if not requested:
            return {}
        for mall in requested:
            if mall not in ALLOWED_MALLS:
                raise ValueError("mall is not allowlisted")
        workers = min(self._max_concurrency, len(requested))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                mall: executor.submit(self.fetch_batch_results, mall, targets)
                for mall, targets in requested.items()
            }
            return {mall: future.result() for mall, future in futures.items()}
