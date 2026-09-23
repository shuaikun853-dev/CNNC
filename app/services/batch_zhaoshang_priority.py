"""Exact materialization and priority boundary for Zhaoshang batch slices."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

try:
    from pymysql.constants import CR as PyMySQLClientError
    from pymysql.constants import ER as PyMySQLServerError
    from pymysql.err import InterfaceError as PyMySQLInterfaceError
    from pymysql.err import OperationalError as PyMySQLOperationalError
except ImportError:
    _PYMYSQL_INTERFACE_ERRORS: tuple[type[BaseException], ...] = ()
    _PYMYSQL_OPERATIONAL_ERRORS: tuple[type[BaseException], ...] = ()
    _RETRYABLE_OPERATIONAL_CODES: frozenset[int] = frozenset()
else:
    _PYMYSQL_INTERFACE_ERRORS = (PyMySQLInterfaceError,)
    _PYMYSQL_OPERATIONAL_ERRORS = (PyMySQLOperationalError,)
    _RETRYABLE_OPERATIONAL_CODES = frozenset(
        {
            PyMySQLServerError.CON_COUNT_ERROR,
            PyMySQLServerError.SERVER_SHUTDOWN,
            PyMySQLServerError.TOO_MANY_USER_CONNECTIONS,
            PyMySQLServerError.LOCK_WAIT_TIMEOUT,
            PyMySQLServerError.LOCK_TABLE_FULL,
            PyMySQLServerError.LOCK_DEADLOCK,
            PyMySQLClientError.CR_CONNECTION_ERROR,
            PyMySQLClientError.CR_CONN_HOST_ERROR,
            PyMySQLClientError.CR_IPSOCK_ERROR,
            PyMySQLClientError.CR_SERVER_GONE_ERROR,
            PyMySQLClientError.CR_SERVER_LOST,
            PyMySQLClientError.CR_SERVER_LOST_EXTENDED,
        }
    )


ALLOWED_BATCH_SUB_SCHEMAS = (
    "central_control_sub",
    "central_control_sub_zsz",
)
MAX_EXEC_NUMBERS = 100


class DbConnection(Protocol):
    def cursor(self) -> Any:
        ...

    def commit(self) -> None:
        ...

    def rollback(self) -> None:
        ...


class BatchPriorityTransientError(RuntimeError):
    """A bounded database operation may safely be retried on a later tick."""


class BatchPrioritySafetyError(RuntimeError):
    """Exact batch identity or priority readback could not be proven."""


class BatchMaterializationStatus(str, Enum):
    NOT_MATERIALIZED = "NOT_MATERIALIZED"
    PARTIALLY_MATERIALIZED = "PARTIALLY_MATERIALIZED"
    MATERIALIZED = "MATERIALIZED"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    SCHEMA_CONFLICT = "SCHEMA_CONFLICT"


class BatchPriorityStatus(str, Enum):
    CONFIRMED = "CONFIRMED"


class BatchPriorityAttemptStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class BatchMaterializationTarget:
    execute_numbers: tuple[str, ...]
    expected_mall: str

    def __post_init__(self) -> None:
        execute_numbers = tuple(str(value or "").strip() for value in self.execute_numbers)
        expected_mall = str(self.expected_mall or "").strip()
        if not expected_mall:
            raise ValueError("expected_mall is required")
        if not execute_numbers or len(execute_numbers) > MAX_EXEC_NUMBERS:
            raise ValueError("execute_numbers must contain between 1 and 100 values")
        if any(not value for value in execute_numbers):
            raise ValueError("execute_numbers cannot contain blank values")
        if len(set(execute_numbers)) != len(execute_numbers):
            raise ValueError("execute_numbers must be unique")
        object.__setattr__(self, "execute_numbers", execute_numbers)
        object.__setattr__(self, "expected_mall", expected_mall)

    @property
    def expected_count(self) -> int:
        return len(self.execute_numbers)


@dataclass(frozen=True)
class BatchMaterializationProbe:
    status: BatchMaterializationStatus
    schema: str | None
    rows: tuple[Mapping[str, Any], ...]
    expected_count: int
    found_count: int
    diagnostic: str = ""


@dataclass(frozen=True)
class BatchPriorityOutcome:
    status: BatchPriorityStatus
    schema: str
    expected_count: int
    affected_rows: int
    priority_updated: int
    already_highest: int
    already_completed: int
    task_ids: tuple[Any, ...]
    already_failed: int = 0


@dataclass(frozen=True)
class BatchPriorityAttempt:
    status: BatchPriorityAttemptStatus
    materialization: BatchMaterializationStatus | None
    priority: BatchPriorityOutcome | None = None
    diagnostic: str = ""


def _chunks(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _placeholders(count: int) -> str:
    return ", ".join("%s" for _ in range(count))


def _exec_select_sql(schema: str, count: int, *, for_update: bool = False) -> str:
    if schema not in ALLOWED_BATCH_SUB_SCHEMAS:
        raise BatchPrioritySafetyError("schema is not allowlisted")
    suffix = "\nFOR UPDATE" if for_update else ""
    return f"""
SELECT
    task_id,
    exec_num,
    mall,
    task_status,
    queue_status,
    weight_type,
    is_valid,
    create_time,
    enqueue_time
FROM {schema}.s_task_info
WHERE exec_num IN ({_placeholders(count)}){suffix}
"""


def _task_select_sql(schema: str, count: int, *, for_update: bool) -> str:
    if schema not in ALLOWED_BATCH_SUB_SCHEMAS:
        raise BatchPrioritySafetyError("schema is not allowlisted")
    suffix = "\nFOR UPDATE" if for_update else ""
    return f"""
SELECT
    task_id,
    exec_num,
    mall,
    task_status,
    queue_status,
    weight_type,
    is_valid,
    create_time,
    enqueue_time
FROM {schema}.s_task_info
WHERE task_id IN ({_placeholders(count)}){suffix}
"""


def _task_update_sql(schema: str, count: int) -> str:
    if schema not in ALLOWED_BATCH_SUB_SCHEMAS:
        raise BatchPrioritySafetyError("schema is not allowlisted")
    return f"""
UPDATE {schema}.s_task_info
SET weight_type = '-1'
WHERE task_id IN ({_placeholders(count)})
  AND is_valid = '1'
  AND weight_type <> '-1'
"""


def _fetchall(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if not isinstance(rows[0], Mapping):
        raise BatchPrioritySafetyError("database cursor did not return mapping rows")
    return [dict(row) for row in rows]


def _transient_diagnostic(exc: BaseException) -> str | None:
    if isinstance(exc, TimeoutError):
        return "TimeoutError"
    if isinstance(exc, ConnectionError):
        return "ConnectionError"
    if _PYMYSQL_INTERFACE_ERRORS and isinstance(exc, _PYMYSQL_INTERFACE_ERRORS):
        return "InterfaceError"
    if _PYMYSQL_OPERATIONAL_ERRORS and isinstance(exc, _PYMYSQL_OPERATIONAL_ERRORS):
        code = exc.args[0] if exc.args else None
        if type(code) is int and code in _RETRYABLE_OPERATIONAL_CODES:
            return f"OperationalError[{code}]"
    return None


def _raise_database_error(exc: BaseException) -> None:
    diagnostic = _transient_diagnostic(exc)
    if diagnostic is not None:
        raise BatchPriorityTransientError(diagnostic) from exc
    raise BatchPrioritySafetyError(type(exc).__name__) from exc


def _prepare_db_operation(
    conn: DbConnection,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    """Fence every potentially blocking DB operation by one absolute deadline."""

    if deadline is None:
        return
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise BatchPriorityTransientError("priority callback deadline exhausted")
    socket_timeout = max(1, int(math.ceil(remaining)))
    for attribute in ("_read_timeout", "_write_timeout"):
        if hasattr(conn, attribute):
            setattr(conn, attribute, socket_timeout)


def _execute(
    cursor: Any,
    conn: DbConnection,
    sql: str,
    params: tuple[Any, ...],
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    _prepare_db_operation(conn, deadline, monotonic)
    cursor.execute(sql, params)


def _close_quietly(conn: DbConnection) -> None:
    if hasattr(conn, "close"):
        try:
            conn.close()
        except Exception:
            pass


def _bounded_rollback(
    conn: DbConnection,
    deadline: float | None,
    monotonic: Callable[[], float],
    *,
    raise_if_expired: bool,
) -> None:
    try:
        _prepare_db_operation(conn, deadline, monotonic)
    except BatchPriorityTransientError:
        _close_quietly(conn)
        if raise_if_expired:
            raise
        return
    conn.rollback()


def _rollback_after_failure(
    conn: DbConnection,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    try:
        _bounded_rollback(
            conn,
            deadline,
            monotonic,
            raise_if_expired=False,
        )
    except Exception:
        _close_quietly(conn)


def _select_by_execute_numbers(
    cursor: Any,
    conn: DbConnection,
    target: BatchMaterializationTarget,
    *,
    batch_size: int,
    for_update: bool,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> dict[str, list[dict[str, Any]]]:
    rows_by_schema: dict[str, list[dict[str, Any]]] = {}
    for schema in ALLOWED_BATCH_SUB_SCHEMAS:
        schema_rows: list[dict[str, Any]] = []
        for chunk in _chunks(target.execute_numbers, batch_size):
            _execute(
                cursor,
                conn,
                _exec_select_sql(schema, len(chunk), for_update=for_update),
                tuple(chunk),
                deadline=deadline,
                monotonic=monotonic,
            )
            schema_rows.extend(_fetchall(cursor))
        if schema_rows:
            rows_by_schema[schema] = schema_rows
    return rows_by_schema


def _identity_error(
    rows: Sequence[Mapping[str, Any]],
    target: BatchMaterializationTarget,
    *,
    require_exact_execs: bool,
    expected_task_ids: set[Any] | None = None,
) -> str | None:
    exec_numbers = [str(row.get("exec_num") or "").strip() for row in rows]
    task_ids = [row.get("task_id") for row in rows]
    if any(not value for value in exec_numbers):
        return "missing exec_num"
    if len(set(exec_numbers)) != len(exec_numbers):
        return "duplicate exec_num"
    if any(value in (None, "") for value in task_ids):
        return "missing task_id"
    if len(set(task_ids)) != len(task_ids):
        return "duplicate task_id"
    if any(str(row.get("mall") or "").strip() != target.expected_mall for row in rows):
        return "mall mismatch"
    if any(str(row.get("is_valid") or "").strip() != "1" for row in rows):
        return "is_valid mismatch"
    actual_execs = set(exec_numbers)
    expected_execs = set(target.execute_numbers)
    if require_exact_execs and actual_execs != expected_execs:
        return "exec_num identity drift"
    if not actual_execs <= expected_execs:
        return "unexpected exec_num"
    if expected_task_ids is not None and set(task_ids) != expected_task_ids:
        return "task_id identity drift"
    return None


def probe_batch_materialization(
    conn: DbConnection,
    target: BatchMaterializationTarget,
    *,
    query_batch_size: int = MAX_EXEC_NUMBERS,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> BatchMaterializationProbe:
    """Locate one exact batch slice without schema discovery or broad scans."""

    if not 1 <= query_batch_size <= MAX_EXEC_NUMBERS:
        raise ValueError("query_batch_size must be between 1 and 100")
    try:
        with conn.cursor() as cursor:
            rows_by_schema = _select_by_execute_numbers(
                cursor,
                conn,
                target,
                batch_size=query_batch_size,
                for_update=False,
                deadline=deadline,
                monotonic=monotonic,
            )
    except BatchPriorityTransientError:
        raise
    except BatchPrioritySafetyError:
        raise
    except Exception as exc:
        _raise_database_error(exc)

    if not rows_by_schema:
        return BatchMaterializationProbe(
            BatchMaterializationStatus.NOT_MATERIALIZED,
            None,
            (),
            target.expected_count,
            0,
        )
    if len(rows_by_schema) != 1:
        return BatchMaterializationProbe(
            BatchMaterializationStatus.SCHEMA_CONFLICT,
            None,
            tuple(row for rows in rows_by_schema.values() for row in rows),
            target.expected_count,
            sum(len(rows) for rows in rows_by_schema.values()),
            "target rows exist in multiple allowlisted schemas",
        )

    schema, rows = next(iter(rows_by_schema.items()))
    diagnostic = _identity_error(rows, target, require_exact_execs=False)
    if diagnostic:
        return BatchMaterializationProbe(
            BatchMaterializationStatus.IDENTITY_CONFLICT,
            schema,
            tuple(rows),
            target.expected_count,
            len(rows),
            diagnostic,
        )
    if len(rows) < target.expected_count:
        return BatchMaterializationProbe(
            BatchMaterializationStatus.PARTIALLY_MATERIALIZED,
            schema,
            tuple(rows),
            target.expected_count,
            len(rows),
        )
    if len(rows) != target.expected_count or set(
        str(row["exec_num"]).strip() for row in rows
    ) != set(target.execute_numbers):
        return BatchMaterializationProbe(
            BatchMaterializationStatus.IDENTITY_CONFLICT,
            schema,
            tuple(rows),
            target.expected_count,
            len(rows),
            "materialized exec_num set is not exact",
        )
    return BatchMaterializationProbe(
        BatchMaterializationStatus.MATERIALIZED,
        schema,
        tuple(rows),
        target.expected_count,
        len(rows),
    )


def _select_by_task_ids(
    cursor: Any,
    conn: DbConnection,
    schema: str,
    task_ids: Sequence[Any],
    *,
    for_update: bool,
    batch_size: int,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in _chunks(task_ids, batch_size):
        _execute(
            cursor,
            conn,
            _task_select_sql(schema, len(chunk), for_update=for_update),
            tuple(chunk),
            deadline=deadline,
            monotonic=monotonic,
        )
        rows.extend(_fetchall(cursor))
    return rows


def _verify_priority_rows(
    rows: Sequence[Mapping[str, Any]],
    target: BatchMaterializationTarget,
    task_ids: set[Any],
) -> None:
    diagnostic = _identity_error(
        rows,
        target,
        require_exact_execs=True,
        expected_task_ids=task_ids,
    )
    if diagnostic:
        raise BatchPrioritySafetyError(diagnostic)
    if len(rows) != target.expected_count:
        raise BatchPrioritySafetyError("task row count drift")
    for row in rows:
        status = str(row.get("task_status") or "").strip()
        if status not in {"00", "01", "02", "03"}:
            raise BatchPrioritySafetyError("unexpected task_status")
        if str(row.get("weight_type") or "").strip() != "-1":
            raise BatchPrioritySafetyError("row is not highest priority")


def boost_batch_priority_by_execute_numbers(
    conn: DbConnection,
    target: BatchMaterializationTarget,
    probe: BatchMaterializationProbe,
    *,
    update_batch_size: int = MAX_EXEC_NUMBERS,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> BatchPriorityOutcome:
    """Freeze the exact materialized PK set, update nonhighest rows, and read back."""

    if probe.status is not BatchMaterializationStatus.MATERIALIZED:
        raise BatchPrioritySafetyError("batch slice is not exactly materialized")
    if probe.schema not in ALLOWED_BATCH_SUB_SCHEMAS:
        raise BatchPrioritySafetyError("materialized schema is not allowlisted")
    if not 1 <= update_batch_size <= MAX_EXEC_NUMBERS:
        raise ValueError("update_batch_size must be between 1 and 100")
    diagnostic = _identity_error(probe.rows, target, require_exact_execs=True)
    if diagnostic or len(probe.rows) != target.expected_count:
        raise BatchPrioritySafetyError(diagnostic or "materialized row count is not exact")

    schema = probe.schema
    frozen_task_ids = tuple(row["task_id"] for row in probe.rows)
    frozen_task_id_set = set(frozen_task_ids)
    try:
        with conn.cursor() as cursor:
            _bounded_rollback(
                conn,
                deadline,
                monotonic,
                raise_if_expired=True,
            )
            transaction_probe = _select_by_execute_numbers(
                cursor,
                conn,
                target,
                batch_size=update_batch_size,
                for_update=True,
                deadline=deadline,
                monotonic=monotonic,
            )
            if len(transaction_probe) > 1:
                raise BatchPrioritySafetyError(
                    "target rows exist in multiple allowlisted schemas"
                )
            if set(transaction_probe) != {schema}:
                raise BatchPrioritySafetyError("materialized schema drift")
            locked_exec_rows = transaction_probe[schema]
            diagnostic = _identity_error(
                locked_exec_rows,
                target,
                require_exact_execs=True,
                expected_task_ids=frozen_task_id_set,
            )
            if diagnostic or len(locked_exec_rows) != target.expected_count:
                raise BatchPrioritySafetyError(
                    diagnostic or "transaction materialization row count drift"
                )
            before = _select_by_task_ids(
                cursor,
                conn,
                schema,
                frozen_task_ids,
                for_update=True,
                batch_size=update_batch_size,
                deadline=deadline,
                monotonic=monotonic,
            )
            diagnostic = _identity_error(
                before,
                target,
                require_exact_execs=True,
                expected_task_ids=frozen_task_id_set,
            )
            if diagnostic or len(before) != target.expected_count:
                raise BatchPrioritySafetyError(diagnostic or "locked row count drift")
            for row in before:
                status = str(row.get("task_status") or "").strip()
                if status not in {"00", "01", "02", "03"}:
                    raise BatchPrioritySafetyError("unexpected task_status")

            update_ids = tuple(
                row["task_id"]
                for row in before
                if str(row.get("weight_type") or "").strip() != "-1"
            )
            affected_rows = 0
            for chunk in _chunks(update_ids, update_batch_size):
                _execute(
                    cursor,
                    conn,
                    _task_update_sql(schema, len(chunk)),
                    tuple(chunk),
                    deadline=deadline,
                    monotonic=monotonic,
                )
                affected_rows += int(getattr(cursor, "rowcount", 0) or 0)
            if affected_rows > len(update_ids):
                raise BatchPrioritySafetyError("priority update affected too many rows")

            transaction_rows = _select_by_task_ids(
                cursor,
                conn,
                schema,
                frozen_task_ids,
                for_update=True,
                batch_size=update_batch_size,
                deadline=deadline,
                monotonic=monotonic,
            )
            _verify_priority_rows(transaction_rows, target, frozen_task_id_set)
            _prepare_db_operation(conn, deadline, monotonic)
            conn.commit()

            verified_rows = _select_by_task_ids(
                cursor,
                conn,
                schema,
                frozen_task_ids,
                for_update=False,
                batch_size=update_batch_size,
                deadline=deadline,
                monotonic=monotonic,
            )
            _verify_priority_rows(verified_rows, target, frozen_task_id_set)
            _bounded_rollback(
                conn,
                deadline,
                monotonic,
                raise_if_expired=False,
            )
    except BatchPriorityTransientError:
        _rollback_after_failure(conn, deadline, monotonic)
        raise
    except BatchPrioritySafetyError:
        _rollback_after_failure(conn, deadline, monotonic)
        raise
    except Exception as exc:
        _rollback_after_failure(conn, deadline, monotonic)
        _raise_database_error(exc)

    final_by_id = {row["task_id"]: row for row in verified_rows}
    update_id_set = set(update_ids)
    priority_updated = 0
    already_highest = 0
    already_completed = 0
    already_failed = 0
    for task_id in frozen_task_ids:
        final = final_by_id[task_id]
        final_status = str(final.get("task_status") or "").strip()
        if final_status == "02":
            already_completed += 1
        elif final_status == "03":
            already_failed += 1
        elif task_id in update_id_set:
            priority_updated += 1
        else:
            already_highest += 1
    if (
        priority_updated
        + already_highest
        + already_completed
        + already_failed
        != target.expected_count
    ):
        raise BatchPrioritySafetyError("priority outcome classification is not exact")
    return BatchPriorityOutcome(
        status=BatchPriorityStatus.CONFIRMED,
        schema=schema,
        expected_count=target.expected_count,
        affected_rows=affected_rows,
        priority_updated=priority_updated,
        already_highest=already_highest,
        already_completed=already_completed,
        task_ids=frozen_task_ids,
        already_failed=already_failed,
    )


def connect_batch_priority_from_env_file(
    path: Path,
    *,
    timeout_seconds: float,
) -> DbConnection:
    """Open the existing controlled DB configuration without logging values."""

    from pymysql import connect
    from pymysql.cursors import DictCursor

    from scripts.monitor_stb_fallback import load_zhaoshang_db_env

    values = load_zhaoshang_db_env(path)
    timeout = max(1, int(math.ceil(timeout_seconds)))
    return connect(
        host=values["ZHAOSHANG_DB_HOST"],
        port=int(values["ZHAOSHANG_DB_PORT"]),
        user=values["ZHAOSHANG_DB_USER"],
        password=values["ZHAOSHANG_DB_PASSWORD"],
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
        connect_timeout=min(10, timeout),
        read_timeout=timeout,
        write_timeout=timeout,
    )


def ensure_batch_priority(
    connection_factory: Callable[[float], DbConnection],
    target: BatchMaterializationTarget,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> BatchPriorityAttempt:
    """Poll within one callback budget, then defer safely to a later tick."""

    if timeout_seconds <= 0:
        return BatchPriorityAttempt(
            BatchPriorityAttemptStatus.PENDING,
            None,
            diagnostic="priority callback budget is exhausted",
        )
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    deadline = monotonic() + timeout_seconds
    last_materialization: BatchMaterializationStatus | None = None
    last_diagnostic = ""

    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return BatchPriorityAttempt(
                BatchPriorityAttemptStatus.PENDING,
                last_materialization,
                diagnostic=last_diagnostic or "batch rows are not fully materialized",
            )
        conn: DbConnection | None = None
        try:
            conn = connection_factory(remaining)
            probe = probe_batch_materialization(
                conn,
                target,
                deadline=deadline,
                monotonic=monotonic,
            )
            last_materialization = probe.status
            last_diagnostic = probe.diagnostic
            if probe.status in {
                BatchMaterializationStatus.IDENTITY_CONFLICT,
                BatchMaterializationStatus.SCHEMA_CONFLICT,
            }:
                return BatchPriorityAttempt(
                    BatchPriorityAttemptStatus.CONFLICT,
                    probe.status,
                    diagnostic=probe.diagnostic,
                )
            if probe.status is BatchMaterializationStatus.MATERIALIZED:
                priority = boost_batch_priority_by_execute_numbers(
                    conn,
                    target,
                    probe,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                return BatchPriorityAttempt(
                    BatchPriorityAttemptStatus.CONFIRMED,
                    probe.status,
                    priority=priority,
                )
        except BatchPrioritySafetyError as exc:
            return BatchPriorityAttempt(
                BatchPriorityAttemptStatus.CONFLICT,
                last_materialization,
                diagnostic=str(exc),
            )
        except BatchPriorityTransientError as exc:
            last_diagnostic = str(exc)
        except Exception as exc:
            transient = _transient_diagnostic(exc)
            if transient is not None:
                last_diagnostic = transient
            else:
                return BatchPriorityAttempt(
                    BatchPriorityAttemptStatus.CONFLICT,
                    last_materialization,
                    diagnostic=type(exc).__name__,
                )
        finally:
            if conn is not None and hasattr(conn, "close"):
                conn.close()

        remaining = deadline - monotonic()
        if remaining <= 0:
            continue
        sleep(min(poll_interval_seconds, remaining))
