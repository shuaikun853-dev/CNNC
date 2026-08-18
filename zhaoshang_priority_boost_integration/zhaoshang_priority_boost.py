"""Safely raise the current Zhaoshang crawler task to highest priority."""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, Sequence


SELECT_TARGET_ROWS_SQL = """
SELECT
    task_id,
    task_num,
    mall,
    create_time,
    task_status,
    weight_type,
    is_valid
FROM central_control_sub_zsz.s_task_info
FORCE INDEX (idx_opt_distinctmall)
WHERE mall = ?
  AND is_valid = '1'
  AND task_num = ?
  AND create_time >= ?
  AND create_time < ?
"""

_TASK_NAME_RE = re.compile(r"^cnnc_current_opt_.+_(?P<marker>\d{14,})$")
_MALL_CODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class DbConnection(Protocol):
    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        ...

    def commit(self) -> None:
        ...

    def rollback(self) -> None:
        ...


class PriorityUpdateZeroRowsError(RuntimeError):
    """The current task could not be safely updated or verified."""


class PriorityScopeSafetyError(RuntimeError):
    """Rows returned by the database escaped the requested task identity."""


@dataclass(frozen=True)
class PriorityTarget:
    task_name: str
    task_number: str
    mall_code: str
    marker_time: datetime
    window_start: datetime
    window_end: datetime

    def lookup_params(self) -> tuple[Any, ...]:
        return (
            self.mall_code,
            self.task_number,
            self.window_start,
            self.window_end,
        )


def build_priority_target(
    *,
    task_name: str,
    task_number: str,
    mall_code: str,
    window_before_minutes: int = 2,
    window_after_minutes: int = 15,
) -> PriorityTarget:
    """Build a composite task identity and its create-time safety window."""

    task_name = str(task_name or "").strip()
    task_number = str(task_number or "").strip()
    mall_code = str(mall_code or "").strip()
    if not task_name:
        raise ValueError("task_name 不能为空")
    if not task_number:
        raise ValueError("task_number 不能为空")
    if not mall_code or not _MALL_CODE_RE.fullmatch(mall_code):
        raise ValueError("mall_code 缺失或格式无效，禁止 task_number-only fallback")
    if window_before_minutes < 0 or window_after_minutes <= 0:
        raise ValueError("priority create_time window 配置无效")

    match = _TASK_NAME_RE.fullmatch(task_name)
    if match is None:
        raise ValueError(
            "task_name marker 必须是末尾至少 14 位纯数字时间: " f"{task_name}"
        )
    marker = match.group("marker")
    try:
        marker_time = datetime.strptime(marker[:14], "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise ValueError(f"task_name marker 不是有效时间: {marker[:14]}") from exc

    return PriorityTarget(
        task_name=task_name,
        task_number=task_number,
        mall_code=mall_code,
        marker_time=marker_time,
        window_start=marker_time - timedelta(minutes=window_before_minutes),
        window_end=marker_time + timedelta(minutes=window_after_minutes),
    )


def normalize_mysql_paramstyle(sql: str) -> str:
    """Convert canonical question-mark placeholders for PyMySQL/mysqlclient."""

    return sql.replace("?", "%s")


def _fetchall_as_dicts(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [dict(row) for row in rows]
    names = [desc[0] for desc in cursor.description]
    return [dict(zip(names, row)) for row in rows]


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip().replace("T", " "))
        except ValueError as exc:
            raise PriorityScopeSafetyError(
                f"create_time outside window: invalid value {value!r}"
            ) from exc
    raise PriorityScopeSafetyError(
        f"create_time outside window: unsupported value {value!r}"
    )


def _is_highest_priority(value: Any) -> bool:
    return str(value).strip() == "-1"


def _validate_identity_rows(
    rows: Sequence[dict[str, Any]],
    target: PriorityTarget,
    *,
    require_valid: bool,
) -> list[dict[str, Any]]:
    validated = [dict(row) for row in rows]
    if not validated:
        raise PriorityUpdateZeroRowsError("no matching rows")

    task_ids = [row.get("task_id") for row in validated]
    if any(task_id in (None, "") for task_id in task_ids):
        raise PriorityScopeSafetyError("missing task_id")
    if len(set(task_ids)) != len(task_ids):
        raise PriorityScopeSafetyError("duplicate task_id in lookup result")

    for row in validated:
        if str(row.get("task_num") or "").strip() != target.task_number:
            raise PriorityScopeSafetyError("task_number mismatch")
        if str(row.get("mall") or "").strip() != target.mall_code:
            raise PriorityScopeSafetyError("mall mismatch")
        create_time = _as_datetime(row.get("create_time"))
        if not target.window_start <= create_time < target.window_end:
            raise PriorityScopeSafetyError("create_time outside window")
        if require_valid:
            if str(row.get("is_valid") or "").strip() != "1":
                raise PriorityScopeSafetyError("is_valid mismatch")
    return validated


def _chunks(values: Sequence[Any], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _id_select_sql(count: int) -> str:
    placeholders = ", ".join("?" for _ in range(count))
    return f"""
SELECT
    task_id,
    task_num,
    mall,
    create_time,
    task_status,
    weight_type,
    is_valid
FROM central_control_sub_zsz.s_task_info
WHERE task_id IN ({placeholders})
FOR UPDATE
"""


def _id_update_sql(count: int) -> str:
    placeholders = ", ".join("?" for _ in range(count))
    return f"""
UPDATE central_control_sub_zsz.s_task_info
SET weight_type = '-1'
WHERE task_id IN ({placeholders})
  AND is_valid = '1'
  AND weight_type <> '-1'
"""


def _id_readback_sql(count: int) -> str:
    placeholders = ", ".join("?" for _ in range(count))
    return f"""
SELECT
    task_id,
    task_num,
    mall,
    create_time,
    task_status,
    weight_type,
    is_valid
FROM central_control_sub_zsz.s_task_info
WHERE task_id IN ({placeholders})
"""


def _distribution(rows: Sequence[dict[str, Any]], field: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field) or "").strip()
        result[key] = result.get(key, 0) + 1
    return result


def _target_dict(target: PriorityTarget) -> dict[str, Any]:
    result = asdict(target)
    for key in ("marker_time", "window_start", "window_end"):
        result[key] = result[key].isoformat(sep=" ")
    return result


def boost_zhaoshang_task_priority(
    conn: DbConnection,
    target: PriorityTarget,
    *,
    expected_subtask_count: int,
    logger: Any = print,
    max_select_retries: int = 60,
    retry_sleep_seconds: float = 2.0,
    update_batch_size: int = 1000,
) -> dict[str, Any]:
    """Locate the current mall's subtasks, then update only their primary keys."""

    if not isinstance(target, PriorityTarget):
        raise TypeError("target 必须是 PriorityTarget")
    if (
        isinstance(expected_subtask_count, bool)
        or not isinstance(expected_subtask_count, int)
        or expected_subtask_count < 1
    ):
        raise ValueError("expected_subtask_count 必须是正整数")
    if max_select_retries < 1:
        raise ValueError("max_select_retries 必须至少为 1")
    if retry_sleep_seconds < 0:
        raise ValueError("retry_sleep_seconds 不能为负数")
    if not 1 <= update_batch_size <= 1000:
        raise ValueError("update_batch_size 必须在 1..1000")

    logger(f"[priority] task_name={target.task_name}")
    logger(f"[priority] task_number={target.task_number}")
    logger(f"[priority] mall_code={target.mall_code}")
    logger(f"[priority] marker_time={target.marker_time.isoformat(sep=' ')}")
    logger(f"[priority] expected_subtask_count={expected_subtask_count}")

    try:
        with conn.cursor() as cursor:
            before_rows: list[dict[str, Any]] = []
            lookup_sql = normalize_mysql_paramstyle(SELECT_TARGET_ROWS_SQL)
            for attempt in range(1, max_select_retries + 1):
                logger(f"[priority] lookup attempt={attempt}/{max_select_retries}")
                # Refresh REPEATABLE READ so delayed scheduler inserts become visible.
                conn.rollback()
                cursor.execute(lookup_sql, target.lookup_params())
                before_rows = _fetchall_as_dicts(cursor)
                logger(f"[priority] matched task_ids count={len(before_rows)}")
                if before_rows:
                    before_rows = _validate_identity_rows(
                        before_rows,
                        target,
                        require_valid=True,
                    )
                if len(before_rows) > expected_subtask_count:
                    raise PriorityScopeSafetyError(
                        "matched subtask count exceeds expected set: "
                        f"matched={len(before_rows)} expected={expected_subtask_count}"
                    )
                if len(before_rows) == expected_subtask_count:
                    break
                if attempt < max_select_retries:
                    time.sleep(retry_sleep_seconds)

            matched_count = len(before_rows)
            if matched_count == 0:
                logger("[priority] failure=no matching rows")
                raise PriorityUpdateZeroRowsError(
                    "no matching rows for mall/task_number/create_time window"
                )
            if matched_count < expected_subtask_count:
                raise PriorityUpdateZeroRowsError(
                    "incomplete subtask rows: "
                    f"matched={matched_count} expected={expected_subtask_count}"
                )

            matched_ids = [row["task_id"] for row in before_rows]

            # Use a fresh primary-key current read for the write transaction. Locking
            # the broad mall index scan would needlessly lock unrelated mall rows.
            conn.rollback()
            locked_rows: list[dict[str, Any]] = []
            for task_id_chunk in _chunks(matched_ids, update_batch_size):
                cursor.execute(
                    normalize_mysql_paramstyle(_id_select_sql(len(task_id_chunk))),
                    tuple(task_id_chunk),
                )
                locked_rows.extend(_fetchall_as_dicts(cursor))
            before_rows = _validate_identity_rows(
                locked_rows,
                target,
                require_valid=True,
            )
            if len(before_rows) != expected_subtask_count:
                raise PriorityUpdateZeroRowsError(
                    "target set changed before priority lock: "
                    f"matched={len(before_rows)} expected={expected_subtask_count}"
                )
            if {row["task_id"] for row in before_rows} != set(matched_ids):
                raise PriorityScopeSafetyError("task_id lock verification mismatch")
            status_before = _distribution(before_rows, "task_status")
            valid_before = _distribution(before_rows, "is_valid")
            weight_before = _distribution(before_rows, "weight_type")
            logger(f"[priority] task_status before={status_before}")
            logger(f"[priority] is_valid before={valid_before}")
            logger(f"[priority] weight_type before={weight_before}")
            already_highest = [
                row for row in before_rows if _is_highest_priority(row.get("weight_type"))
            ]
            update_ids = [
                row["task_id"]
                for row in before_rows
                if not _is_highest_priority(row.get("weight_type"))
            ]
            logger(f"[priority] already -1 count={len(already_highest)}")
            logger(f"[priority] update candidates count={len(update_ids)}")

            if not update_ids:
                conn.rollback()
                logger("[priority] affected_rows=0")
                logger("[priority] state=already_highest_priority")
                return {
                    "target": _target_dict(target),
                    "expected_subtask_count": expected_subtask_count,
                    "matched_subtask_ids": matched_ids,
                    "already_highest_count": len(already_highest),
                    "update_candidates_count": 0,
                    "affected_rows": 0,
                    "task_status_distribution_before": status_before,
                    "task_status_distribution_after": status_before,
                    "is_valid_distribution_before": valid_before,
                    "is_valid_distribution_after": valid_before,
                    "weight_type_distribution_before": weight_before,
                    "weight_type_distribution_after": weight_before,
                    "priority_state": "already_highest_priority",
                }

            affected_rows = 0
            for task_id_chunk in _chunks(update_ids, update_batch_size):
                cursor.execute(
                    normalize_mysql_paramstyle(_id_update_sql(len(task_id_chunk))),
                    tuple(task_id_chunk),
                )
                affected_rows += int(getattr(cursor, "rowcount", 0) or 0)
            logger(f"[priority] affected_rows={affected_rows}")
            if affected_rows != len(update_ids):
                raise PriorityUpdateZeroRowsError(
                    "priority update affected unexpected row count: "
                    f"affected={affected_rows} candidates={len(update_ids)}"
                )

            transaction_rows: list[dict[str, Any]] = []
            for task_id_chunk in _chunks(matched_ids, update_batch_size):
                cursor.execute(
                    normalize_mysql_paramstyle(_id_select_sql(len(task_id_chunk))),
                    tuple(task_id_chunk),
                )
                transaction_rows.extend(_fetchall_as_dicts(cursor))
            transaction_rows = _validate_identity_rows(
                transaction_rows,
                target,
                require_valid=True,
            )
            if {row["task_id"] for row in transaction_rows} != set(matched_ids):
                raise PriorityScopeSafetyError("task_id verification mismatch")
            not_highest = [
                row for row in transaction_rows
                if not _is_highest_priority(row.get("weight_type"))
            ]
            if not_highest:
                raise PriorityUpdateZeroRowsError(
                    "priority verification failed: exact target rows are not -1"
                )
            conn.commit()

            verified_rows: list[dict[str, Any]] = []
            for task_id_chunk in _chunks(matched_ids, update_batch_size):
                cursor.execute(
                    normalize_mysql_paramstyle(_id_readback_sql(len(task_id_chunk))),
                    tuple(task_id_chunk),
                )
                verified_rows.extend(_fetchall_as_dicts(cursor))
            verified_rows = _validate_identity_rows(
                verified_rows,
                target,
                require_valid=True,
            )
            if (
                len(verified_rows) != expected_subtask_count
                or {row["task_id"] for row in verified_rows} != set(matched_ids)
            ):
                raise PriorityScopeSafetyError("post-commit task_id verification mismatch")
            if any(
                not _is_highest_priority(row.get("weight_type"))
                for row in verified_rows
            ):
                raise PriorityUpdateZeroRowsError(
                    "post-commit priority verification failed: exact target rows are not -1"
                )
            status_after = _distribution(verified_rows, "task_status")
            valid_after = _distribution(verified_rows, "is_valid")
            if status_after != status_before:
                raise PriorityScopeSafetyError("task_status changed during priority update")
            if valid_after != valid_before:
                raise PriorityScopeSafetyError("is_valid changed during priority update")
            conn.rollback()
    except Exception as exc:
        logger(f"[priority] failure={exc}")
        conn.rollback()
        raise

    logger("[priority] state=updated")
    weight_after = _distribution(verified_rows, "weight_type")
    logger(f"[priority] weight_type after={weight_after}")
    return {
        "target": _target_dict(target),
        "expected_subtask_count": expected_subtask_count,
        "matched_subtask_ids": matched_ids,
        "already_highest_count": len(already_highest),
        "update_candidates_count": len(update_ids),
        "affected_rows": affected_rows,
        "task_status_distribution_before": status_before,
        "task_status_distribution_after": status_after,
        "is_valid_distribution_before": valid_before,
        "is_valid_distribution_after": valid_after,
        "weight_type_distribution_before": weight_before,
        "weight_type_distribution_after": weight_after,
        "priority_state": "updated",
    }


def connect_from_env() -> DbConnection:
    """Create the narrowly scoped MySQL connection used by the external tool."""

    import pymysql

    return pymysql.connect(
        host=os.environ["ZHAOSHANG_DB_HOST"],
        port=int(os.environ.get("ZHAOSHANG_DB_PORT", "3306")),
        user=os.environ["ZHAOSHANG_DB_USER"],
        password=os.environ["ZHAOSHANG_DB_PASSWORD"],
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


def boost_zhaoshang_task_priority_after_create(
    *,
    task_name: str,
    task_number: str,
    mall_code: str,
    expected_subtask_count: int,
    conn: DbConnection | None = None,
    logger: Any = print,
) -> dict[str, Any]:
    """Boost one exact task identity immediately after its successful enable."""

    target = build_priority_target(
        task_name=task_name,
        task_number=task_number,
        mall_code=mall_code,
    )
    own_conn = conn is None
    if conn is None:
        conn = connect_from_env()
    try:
        return boost_zhaoshang_task_priority(
            conn,
            target,
            expected_subtask_count=expected_subtask_count,
            logger=logger,
        )
    finally:
        if own_conn and hasattr(conn, "close"):
            conn.close()
