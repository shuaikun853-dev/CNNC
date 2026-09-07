import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_HELPER = (
    ROOT / "zhaoshang_priority_boost_integration" / "zhaoshang_priority_boost.py"
)
EXTERNAL_HELPER = Path(
    r"E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\zhaoshang_priority_boost.py"
)

if not EXTERNAL_HELPER.is_file():
    pytest.skip(
        "zhaoshang external priority helper unavailable (PRODUCTION_MACHINE_ONLY)",
        allow_module_level=True,
    )


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=[INTEGRATION_HELPER, EXTERNAL_HELPER], ids=["integration", "production"])
def priority_module(request):
    path = request.param
    assert path.is_file()
    return _load_module(path, f"priority_{request.node.name}_{request.param.stem}")


def row(
    task_id,
    *,
    task_number="RW-20260811181227",
    mall="guangbo",
    create_time=datetime(2026, 8, 11, 18, 12, 15),
    task_status="00",
    weight_type=2,
    is_valid="1",
):
    return {
        "task_id": task_id,
        "task_num": task_number,
        "mall": mall,
        "create_time": create_time,
        "task_status": task_status,
        "weight_type": weight_type,
        "is_valid": is_valid,
    }


class MemoryCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.connection.executions.append((normalized, tuple(params)))
        self.rowcount = 0

        if normalized.upper().startswith("UPDATE"):
            self.connection.update_calls += 1
            ids = set(params)
            requires_active = "task_status IN ('00', '01')" in normalized
            for item in self.connection.rows:
                if (
                    item["task_id"] in ids
                    and (not requires_active or item["task_status"] in ("00", "01"))
                    and str(item["is_valid"]) == "1"
                    and str(item["weight_type"]) != "-1"
                ):
                    item["weight_type"] = -1
                    self.rowcount += 1
                    self.connection.updated_ids.append(item["task_id"])
            return

        if "FORCE INDEX (idx_opt_distinctmall)" in normalized:
            self.connection.lookup_calls += 1
            if self.connection.snapshots:
                snapshot_index = min(
                    self.connection.lookup_calls - 1,
                    len(self.connection.snapshots) - 1,
                )
                self.connection.rows = [dict(item) for item in self.connection.snapshots[snapshot_index]]

            if self.connection.forced_lookup_rows is not None:
                self.rows = [dict(item) for item in self.connection.forced_lookup_rows]
                return

            mall, task_number, window_start, window_end = params
            requires_active = "task_status IN ('00', '01')" in normalized
            self.rows = [
                dict(item)
                for item in self.connection.rows
                if item["mall"] == mall
                and item["task_num"] == task_number
                and (not requires_active or item["task_status"] in ("00", "01"))
                and str(item["is_valid"]) == "1"
                and window_start <= item["create_time"] < window_end
            ]
            return

        if normalized.upper().startswith("SELECT") and "task_id IN" in normalized:
            if (
                "FOR UPDATE" in normalized.upper()
                and self.connection.pk_lock_rows is not None
                and not self.connection.pk_lock_applied
            ):
                self.connection.rows = [
                    dict(item) for item in self.connection.pk_lock_rows
                ]
                self.connection.pk_lock_applied = True
            ids = set(params)
            self.rows = [dict(item) for item in self.connection.rows if item["task_id"] in ids]
            return

        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchall(self):
        return self.rows


class MemoryConnection:
    def __init__(
        self,
        rows=(),
        *,
        snapshots=None,
        forced_lookup_rows=None,
        pk_lock_rows=None,
    ):
        self.rows = [dict(item) for item in rows]
        self.snapshots = snapshots
        self.forced_lookup_rows = forced_lookup_rows
        self.pk_lock_rows = pk_lock_rows
        self.pk_lock_applied = False
        self.executions = []
        self.updated_ids = []
        self.lookup_calls = 0
        self.update_calls = 0
        self.cursor_calls = 0
        self.rollback_count = 0
        self.committed = False

    def cursor(self):
        self.cursor_calls += 1
        return MemoryCursor(self)

    def rollback(self):
        self.rollback_count += 1

    def commit(self):
        self.committed = True


def make_target(module, *, task_number="RW-20260811181227", mall_code="guangbo"):
    return module.build_priority_target(
        task_name="cnnc_current_opt_广博商城_20260811181149361499",
        task_number=task_number,
        mall_code=mall_code,
    )


def boost(module, connection, target=None, **kwargs):
    return module.boost_zhaoshang_task_priority(
        connection,
        target or make_target(module),
        logger=lambda _message: None,
        max_select_retries=kwargs.pop("max_select_retries", 1),
        retry_sleep_seconds=0,
        expected_subtask_count=kwargs.pop("expected_subtask_count", 1),
        **kwargs,
    )


@pytest.mark.parametrize("task_status", ["00", "01", "02"])
def test_all_execution_states_are_updated_to_highest_priority(
    priority_module, task_status
):
    connection = MemoryConnection([row(101, task_status=task_status, weight_type=0)])

    result = boost(priority_module, connection)

    assert result["priority_state"] == "updated"
    assert result["matched_subtask_ids"] == [101]
    assert result["affected_rows"] == 1
    assert connection.updated_ids == [101]
    assert connection.rows[0]["task_status"] == task_status
    assert connection.rows[0]["weight_type"] == -1
    assert connection.committed is True


def test_shared_task_number_across_malls_updates_only_current_mall(priority_module):
    connection = MemoryConnection(
        [
            row(201, mall="nbdeli"),
            row(202, mall="lxwlmall"),
            row(203, mall="guangbo"),
        ]
    )

    result = boost(priority_module, connection)

    assert result["matched_subtask_ids"] == [203]
    assert connection.updated_ids == [203]
    assert [item["weight_type"] for item in connection.rows] == [2, 2, -1]

    lookup_sql, lookup_params = next(
        execution
        for execution in connection.executions
        if "FORCE INDEX" in execution[0]
    )
    assert "FORCE INDEX (idx_opt_distinctmall)" in lookup_sql
    assert "mall = %s" in lookup_sql
    assert "task_num = %s" in lookup_sql
    assert "create_time >= %s" in lookup_sql
    assert "create_time < %s" in lookup_sql
    assert lookup_params[0:2] == ("guangbo", "RW-20260811181227")

    update_sql, update_params = next(
        execution
        for execution in connection.executions
        if execution[0].upper().startswith("UPDATE")
    )
    assert "task_id IN (%s)" in update_sql
    assert "task_num" not in update_sql
    assert "batch_num" not in update_sql
    assert "task_status IN ('00', '01')" not in update_sql
    assert "is_valid = '1'" in update_sql
    assert "weight_type <> '-1'" in update_sql
    assert update_params == (203,)

    lookup_selects = [
        sql for sql, _params in connection.executions if "FORCE INDEX" in sql
    ]
    pk_selects = [
        sql
        for sql, _params in connection.executions
        if sql.upper().startswith("SELECT") and "task_id IN" in sql
    ]
    assert all("task_status IN ('00', '01')" not in sql for sql in lookup_selects)
    assert lookup_selects and all("FOR UPDATE" not in sql for sql in lookup_selects)
    assert pk_selects and any("FOR UPDATE" in sql for sql in pk_selects)


def test_polling_refreshes_snapshot_until_subtasks_are_inserted(priority_module):
    connection = MemoryConnection(snapshots=[[], [row(301)]])

    result = boost(priority_module, connection, max_select_retries=2)

    assert result["affected_rows"] == 1
    assert connection.lookup_calls == 2
    assert connection.rollback_count >= 2


def test_late_materialized_subtask_needs_a_stretched_window_and_keeps_identity_exact(priority_module):
    """A subtask materialized after marker+15min stays recoverable on re-checks.

    The exact identity (task_num + mall) is unchanged; only the upper bound of
    the create_time window stretches, mirroring what the adapter computes for
    every priority re-check, probe and zero-rows recovery.
    """
    from dataclasses import replace as _replace

    late_row = row(401, create_time=datetime(2026, 8, 11, 18, 52, 0))
    foreign_earlier_row = row(
        402, create_time=datetime(2026, 8, 11, 18, 5, 0), mall="guangbo"
    )
    target = make_target(priority_module)

    with pytest.raises(priority_module.PriorityUpdateZeroRowsError, match="no matching rows"):
        boost(priority_module, MemoryConnection([late_row, foreign_earlier_row]), target)

    stretched = _replace(
        target, window_end=datetime(2026, 8, 11, 19, 0, 0)
    )
    connection = MemoryConnection([late_row, foreign_earlier_row])
    result = boost(priority_module, connection, stretched)

    assert result["priority_state"] == "updated"
    assert result["matched_subtask_ids"] == [401]
    assert connection.updated_ids == [401]
    assert connection.rows[1]["weight_type"] == 2


def test_polling_waits_for_complete_expected_subtask_set(priority_module):
    partial = [row(311)]
    complete = [row(311), row(312)]
    connection = MemoryConnection(snapshots=[partial, complete, complete])

    result = boost(
        priority_module,
        connection,
        max_select_retries=3,
        expected_subtask_count=2,
    )

    assert result["matched_subtask_ids"] == [311, 312]
    assert connection.updated_ids == [311, 312]


def test_persistently_partial_subtask_set_never_reaches_update(priority_module):
    connection = MemoryConnection(snapshots=[[row(321)], [row(321)], [row(321)]])

    with pytest.raises(priority_module.PriorityUpdateZeroRowsError, match="incomplete"):
        boost(
            priority_module,
            connection,
            max_select_retries=3,
            expected_subtask_count=2,
        )

    assert connection.update_calls == 0


def test_more_rows_than_local_subtasks_fails_closed(priority_module):
    connection = MemoryConnection([row(331), row(332)])

    with pytest.raises(priority_module.PriorityScopeSafetyError, match="exceeds"):
        boost(priority_module, connection, expected_subtask_count=1)

    assert connection.update_calls == 0


def test_already_highest_priority_is_idempotent_without_update(priority_module):
    connection = MemoryConnection([row(401, weight_type=-1)])

    result = boost(priority_module, connection)

    assert result["priority_state"] == "already_highest_priority"
    assert result["affected_rows"] == 0
    assert result["already_highest_count"] == 1
    assert connection.update_calls == 0
    assert connection.committed is False


def test_primary_key_current_read_observes_concurrent_idempotent_update(priority_module):
    connection = MemoryConnection(
        [row(411, weight_type=2)],
        pk_lock_rows=[row(411, weight_type=-1)],
    )

    result = boost(priority_module, connection)

    assert result["priority_state"] == "already_highest_priority"
    assert result["affected_rows"] == 0
    assert connection.update_calls == 0
    assert connection.pk_lock_applied is True


def test_primary_key_current_read_handles_task_completing_before_lock(priority_module):
    connection = MemoryConnection(
        [row(421, task_status="00", weight_type=2)],
        pk_lock_rows=[row(421, task_status="02", weight_type=2)],
    )

    result = boost(priority_module, connection)

    assert result["priority_state"] == "updated"
    assert result["affected_rows"] == 1
    assert connection.update_calls == 1
    assert connection.rows[0]["task_status"] == "02"
    assert connection.rows[0]["weight_type"] == -1
    assert connection.pk_lock_applied is True


def test_primary_key_current_read_updates_mixed_execution_states(priority_module):
    connection = MemoryConnection(
        [row(431), row(432), row(433)],
        pk_lock_rows=[
            row(431, task_status="00"),
            row(432, task_status="01"),
            row(433, task_status="02"),
        ],
    )

    result = boost(priority_module, connection, expected_subtask_count=3)

    assert result["affected_rows"] == 3
    assert connection.updated_ids == [431, 432, 433]
    assert result["task_status_distribution_before"] == {"00": 1, "01": 1, "02": 1}
    assert result["task_status_distribution_after"] == {"00": 1, "01": 1, "02": 1}
    assert [item["task_status"] for item in connection.rows] == ["00", "01", "02"]
    assert {item["weight_type"] for item in connection.rows} == {-1}


def test_partial_highest_priority_updates_only_remaining_exact_rows(priority_module):
    connection = MemoryConnection(
        [
            row(441, task_status="00", weight_type=-1),
            row(442, task_status="01", weight_type=0),
            row(443, task_status="02", weight_type=0),
        ]
    )

    result = boost(priority_module, connection, expected_subtask_count=3)

    assert result["already_highest_count"] == 1
    assert result["update_candidates_count"] == 2
    assert result["affected_rows"] == 2
    assert connection.updated_ids == [442, 443]
    assert {item["weight_type"] for item in connection.rows} == {-1}


def test_historical_same_task_number_outside_marker_window_is_not_updated(priority_module):
    connection = MemoryConnection(
        [row(501, create_time=datetime(2026, 8, 10, 18, 12, 15))]
    )

    with pytest.raises(priority_module.PriorityUpdateZeroRowsError, match="no matching rows"):
        boost(priority_module, connection)

    assert connection.update_calls == 0
    assert connection.updated_ids == []


def test_missing_mall_mapping_fails_before_opening_cursor(priority_module):
    connection = MemoryConnection([row(601)])

    with pytest.raises(ValueError, match="mall_code"):
        priority_module.boost_zhaoshang_task_priority_after_create(
            task_name="cnnc_current_opt_广博商城_20260811181149361499",
            task_number="RW-20260811181227",
            mall_code="",
            expected_subtask_count=1,
            conn=connection,
            logger=lambda _message: None,
        )

    assert connection.cursor_calls == 0
    assert connection.update_calls == 0


def test_mixed_mall_lookup_fails_closed_without_update(priority_module):
    connection = MemoryConnection(
        forced_lookup_rows=[row(701), row(702, mall="lxwlmall")]
    )

    with pytest.raises(priority_module.PriorityScopeSafetyError, match="mall mismatch"):
        boost(priority_module, connection)

    assert connection.update_calls == 0


@pytest.mark.parametrize(
    ("unsafe_row", "message"),
    [
        (row(711, task_number="RW-other"), "task_number mismatch"),
        (
            row(712, create_time=datetime(2026, 8, 10, 18, 12, 15)),
            "create_time outside window",
        ),
        (row(714, is_valid="0"), "is_valid mismatch"),
    ],
)
def test_lookup_safety_mismatch_never_reaches_update(
    priority_module, unsafe_row, message
):
    connection = MemoryConnection(forced_lookup_rows=[unsafe_row])

    with pytest.raises(priority_module.PriorityScopeSafetyError, match=message):
        boost(priority_module, connection)

    assert connection.update_calls == 0


def test_marker_uses_first_fourteen_digits_and_configured_window(priority_module):
    target = make_target(priority_module)

    assert target.marker_time == datetime(2026, 8, 11, 18, 11, 49)
    assert target.window_start == datetime(2026, 8, 11, 18, 9, 49)
    assert target.window_end == datetime(2026, 8, 11, 18, 26, 49)


def test_invalid_task_marker_fails_closed(priority_module):
    with pytest.raises(ValueError, match="marker"):
        priority_module.build_priority_target(
            task_name="cnnc_current_opt_广博商城_181149",
            task_number="RW-20260811181227",
            mall_code="guangbo",
        )


def test_updates_are_parameterized_and_chunked_by_primary_key(priority_module):
    connection = MemoryConnection([row(task_id) for task_id in range(801, 806)])

    result = boost(
        priority_module,
        connection,
        update_batch_size=2,
        expected_subtask_count=5,
    )

    assert result["affected_rows"] == 5
    assert connection.updated_ids == [801, 802, 803, 804, 805]
    update_calls = [
        execution
        for execution in connection.executions
        if execution[0].upper().startswith("UPDATE")
    ]
    assert [params for _sql, params in update_calls] == [
        (801, 802),
        (803, 804),
        (805,),
    ]
    assert all("task_id IN" in sql for sql, _params in update_calls)


def test_priority_logs_composite_identity_and_lookup_counts(priority_module):
    connection = MemoryConnection([row(901)])
    messages = []

    priority_module.boost_zhaoshang_task_priority(
        connection,
        make_target(priority_module),
        logger=messages.append,
        max_select_retries=1,
        retry_sleep_seconds=0,
        expected_subtask_count=1,
    )

    output = "\n".join(messages)
    for expected in (
        "[priority] task_name=",
        "[priority] task_number=",
        "[priority] mall_code=guangbo",
        "[priority] marker_time=",
        "[priority] expected_subtask_count=1",
        "[priority] lookup attempt=1/1",
        "[priority] matched task_ids count=1",
        "[priority] already -1 count=0",
        "[priority] update candidates count=1",
        "[priority] affected_rows=1",
        "[priority] state=updated",
    ):
        assert expected in output
