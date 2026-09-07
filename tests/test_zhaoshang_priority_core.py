from __future__ import annotations

from datetime import datetime

import pytest

from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    EXTERNAL_TOOL_PATH,
)


_PRIORITY_TOOL = EXTERNAL_TOOL_PATH.with_name("zhaoshang_priority_boost.py")

if not _PRIORITY_TOOL.is_file():
    pytest.skip(
        "zhaoshang external priority tool unavailable (PRODUCTION_MACHINE_ONLY)",
        allow_module_level=True,
    )

priority = CnncLegacyZhaoshangAdapter._load_module(
    _PRIORITY_TOOL,
    "test_zhaoshang_priority_core_module",
)


def _row(weight_type: object, *, task_status: str = "00") -> dict[str, object]:
    return {
        "task_id": 9001,
        "task_num": "RW-20260812165901",
        "mall": "jd",
        "create_time": datetime(2026, 8, 12, 16, 59, 1),
        "task_status": task_status,
        "weight_type": weight_type,
        "is_valid": "1",
    }


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.current = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        normalized = " ".join(str(sql).split())
        self.connection.calls.append((normalized, tuple(params)))
        self.rowcount = 0
        if normalized.startswith("SELECT") and "FORCE INDEX" in normalized:
            self.current = [dict(row) for row in self.connection.rows.values()]
        elif normalized.startswith("SELECT") and "WHERE task_id IN" in normalized:
            self.current = [dict(self.connection.rows[value]) for value in params]
        elif normalized.startswith("UPDATE"):
            changed = 0
            for value in params:
                row = self.connection.rows[value]
                if str(row["weight_type"]) != "-1":
                    changed += 1
                    if self.connection.apply_update:
                        row["weight_type"] = "-1"
            self.rowcount = changed
            self.current = []
        else:
            raise AssertionError(normalized)

    def fetchall(self):
        return list(self.current)


class _Connection:
    def __init__(self, row, *, apply_update=True):
        self.rows = {row["task_id"]: dict(row)}
        self.apply_update = apply_update
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _target():
    return priority.build_priority_target(
        task_name="cnnc_current_opt_京东_20260812165901",
        task_number="RW-20260812165901",
        mall_code="jd",
    )


def test_priority_core_updates_exact_primary_keys_and_verifies_minus_one():
    connection = _Connection(_row(0))

    result = priority.boost_zhaoshang_task_priority(
        connection,
        _target(),
        expected_subtask_count=1,
        max_select_retries=1,
        retry_sleep_seconds=0,
        logger=lambda *_args: None,
    )

    updates = [(sql, params) for sql, params in connection.calls if sql.startswith("UPDATE")]
    assert result["priority_state"] == "updated"
    assert result["affected_rows"] == 1
    assert connection.rows[9001]["weight_type"] == "-1"
    assert updates == [(updates[0][0], (9001,))]
    assert "SET weight_type = '-1'" in updates[0][0]
    assert connection.commits == 1


def test_priority_core_updates_completed_row_without_changing_execution_state():
    connection = _Connection(_row(0, task_status="02"))

    result = priority.boost_zhaoshang_task_priority(
        connection,
        _target(),
        expected_subtask_count=1,
        max_select_retries=1,
        retry_sleep_seconds=0,
        logger=lambda *_args: None,
    )

    assert result["priority_state"] == "updated"
    assert result["affected_rows"] == 1
    assert connection.rows[9001]["weight_type"] == "-1"
    assert connection.rows[9001]["task_status"] == "02"


def test_priority_core_reports_already_highest_without_update():
    connection = _Connection(_row("-1"))

    result = priority.boost_zhaoshang_task_priority(
        connection,
        _target(),
        expected_subtask_count=1,
        max_select_retries=1,
        retry_sleep_seconds=0,
        logger=lambda *_args: None,
    )

    assert result["priority_state"] == "already_highest_priority"
    assert result["already_highest_count"] == 1
    assert not any(sql.startswith("UPDATE") for sql, _params in connection.calls)
    assert connection.commits == 0


def test_priority_core_never_accepts_update_without_readback_confirmation():
    connection = _Connection(_row(0), apply_update=False)

    with pytest.raises(priority.PriorityUpdateZeroRowsError, match="not -1"):
        priority.boost_zhaoshang_task_priority(
            connection,
            _target(),
            expected_subtask_count=1,
            max_select_retries=1,
            retry_sleep_seconds=0,
            logger=lambda *_args: None,
        )

    assert connection.commits == 0
    assert connection.rollbacks >= 1
