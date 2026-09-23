from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.services.batch_zhaoshang_priority import (
    ALLOWED_BATCH_SUB_SCHEMAS,
    BatchMaterializationProbe,
    BatchMaterializationStatus,
    BatchMaterializationTarget,
    BatchPrioritySafetyError,
    BatchPriorityStatus,
    BatchPriorityAttemptStatus,
    BatchPriorityTransientError,
    boost_batch_priority_by_execute_numbers,
    ensure_batch_priority,
    probe_batch_materialization,
)


def _row(
    index: int,
    *,
    exec_num: str | None = None,
    task_id: int | None = None,
    mall: str = "jd",
    task_status: str = "00",
    weight_type: str = "2",
    is_valid: str = "1",
) -> dict[str, Any]:
    return {
        "task_id": index if task_id is None else task_id,
        "exec_num": exec_num or f"exec-{index}",
        "mall": mall,
        "task_status": task_status,
        "queue_status": "0",
        "weight_type": weight_type,
        "is_valid": is_valid,
        "create_time": "2026-08-13 09:08:50",
        "enqueue_time": None,
    }


@dataclass
class _Step:
    rows: list[dict[str, Any]] | None = None
    rowcount: int = 0
    error: BaseException | None = None
    advance_seconds: float = 0.0


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection
        self.rows: list[dict[str, Any]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.connection.calls.append((sql, tuple(params)))
        steps = (
            self.connection.transaction_probe_steps
            if "WHERE exec_num IN" in sql and "FOR UPDATE" in sql
            else self.connection.steps
        )
        if not steps:
            raise AssertionError("unexpected SQL execution")
        step = steps.pop(0)
        if step.error is not None:
            raise step.error
        self.rows = list(step.rows or [])
        self.rowcount = step.rowcount
        if self.connection.clock is not None:
            self.connection.clock.value += step.advance_seconds

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)


class _Connection:
    def __init__(
        self,
        *steps: _Step,
        clock: "_Clock | None" = None,
        transaction_probe_steps: tuple[_Step, ...] | None = None,
        rollback_advance_seconds: float = 0.0,
    ) -> None:
        self.steps = list(steps)
        self.clock = clock
        first_rows = list(steps[0].rows or []) if steps else []
        self.transaction_probe_steps = list(
            transaction_probe_steps
            if transaction_probe_steps is not None
            else (_Step(rows=first_rows), _Step(rows=[]))
        )
        self.rollback_advance_seconds = rollback_advance_seconds
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.clock is not None:
            self.clock.value += self.rollback_advance_seconds

    def close(self) -> None:
        self.closed += 1


def _target(count: int) -> BatchMaterializationTarget:
    return BatchMaterializationTarget(
        execute_numbers=tuple(f"exec-{index}" for index in range(1, count + 1)),
        expected_mall="jd",
    )


def _materialized_probe(*rows: dict[str, Any]) -> BatchMaterializationProbe:
    return BatchMaterializationProbe(
        status=BatchMaterializationStatus.MATERIALIZED,
        schema=ALLOWED_BATCH_SUB_SCHEMAS[0],
        rows=tuple(rows),
        expected_count=len(rows),
        found_count=len(rows),
    )


def test_probe_reports_not_materialized_for_zero_of_expected_rows() -> None:
    connection = _Connection(_Step(rows=[]), _Step(rows=[]))

    result = probe_batch_materialization(connection, _target(3))

    assert result.status is BatchMaterializationStatus.NOT_MATERIALIZED
    assert result.found_count == 0
    assert all("exec_num IN" in sql for sql, _params in connection.calls)


def test_probe_reports_partial_without_updating_the_visible_subset() -> None:
    rows = [_row(index) for index in range(1, 9)]
    connection = _Connection(_Step(rows=rows), _Step(rows=[]))

    result = probe_batch_materialization(connection, _target(13))

    assert result.status is BatchMaterializationStatus.PARTIALLY_MATERIALIZED
    assert result.schema == "central_control_sub"
    assert result.found_count == 8
    assert len(connection.calls) == 2


def test_probe_reports_materialized_only_for_the_exact_single_schema_set() -> None:
    rows = [_row(index) for index in range(1, 14)]
    connection = _Connection(_Step(rows=rows), _Step(rows=[]))

    result = probe_batch_materialization(connection, _target(13))

    assert result.status is BatchMaterializationStatus.MATERIALIZED
    assert result.schema == "central_control_sub"
    assert {row["exec_num"] for row in result.rows} == set(_target(13).execute_numbers)


def test_probe_treats_failed_task_status_as_materialized_not_conflict() -> None:
    failed = _row(1, task_status="03")
    connection = _Connection(_Step(rows=[failed]), _Step(rows=[]))

    result = probe_batch_materialization(connection, _target(1))

    assert result.status is BatchMaterializationStatus.MATERIALIZED
    assert result.rows[0]["task_status"] == "03"


@pytest.mark.parametrize(
    "rows, diagnostic",
    [
        ([_row(1), _row(2, exec_num="exec-1")], "duplicate exec_num"),
        ([_row(1, task_id=7), _row(2, task_id=7)], "duplicate task_id"),
        ([_row(1, mall="suning")], "mall mismatch"),
        ([_row(1, is_valid="0")], "is_valid mismatch"),
    ],
)
def test_probe_fails_closed_on_identity_conflicts(rows, diagnostic) -> None:
    connection = _Connection(_Step(rows=rows), _Step(rows=[]))

    result = probe_batch_materialization(connection, _target(len(rows)))

    assert result.status is BatchMaterializationStatus.IDENTITY_CONFLICT
    assert diagnostic in result.diagnostic


def test_probe_fails_closed_when_target_rows_span_allowlisted_schemas() -> None:
    connection = _Connection(_Step(rows=[_row(1)]), _Step(rows=[_row(2)]))

    result = probe_batch_materialization(connection, _target(2))

    assert result.status is BatchMaterializationStatus.SCHEMA_CONFLICT
    assert result.schema is None


def test_probe_queries_large_targets_in_bounded_chunks() -> None:
    target = BatchMaterializationTarget(
        execute_numbers=tuple(f"exec-{index}" for index in range(1, 6)),
        expected_mall="jd",
    )
    connection = _Connection(
        _Step(rows=[_row(1), _row(2)]),
        _Step(rows=[_row(3), _row(4)]),
        _Step(rows=[_row(5)]),
        _Step(rows=[]),
        _Step(rows=[]),
        _Step(rows=[]),
    )

    result = probe_batch_materialization(connection, target, query_batch_size=2)

    assert result.status is BatchMaterializationStatus.MATERIALIZED
    assert [len(params) for _sql, params in connection.calls] == [2, 2, 1, 2, 2, 1]


def test_probe_does_not_allow_callers_to_narrow_the_schema_allowlist() -> None:
    connection = _Connection()

    with pytest.raises(TypeError):
        probe_batch_materialization(  # type: ignore[call-arg]
            connection,
            _target(1),
            schemas=(ALLOWED_BATCH_SUB_SCHEMAS[0],),
        )

    assert connection.calls == []


def test_probe_classifies_sql_timeout_as_retryable() -> None:
    connection = _Connection(_Step(error=TimeoutError("database timed out")))

    with pytest.raises(BatchPriorityTransientError, match="TimeoutError"):
        probe_batch_materialization(connection, _target(1))


def test_probe_does_not_misclassify_unknown_database_errors_as_retryable() -> None:
    class ProgrammingFailure(RuntimeError):
        pass

    connection = _Connection(_Step(error=ProgrammingFailure("bad SQL")))

    with pytest.raises(BatchPrioritySafetyError, match="ProgrammingFailure"):
        probe_batch_materialization(connection, _target(1))


def test_priority_updates_all_exact_nonhighest_primary_keys() -> None:
    before = [_row(1), _row(2)]
    after = [_row(1, weight_type="-1"), _row(2, weight_type="-1")]
    connection = _Connection(
        _Step(rows=before),
        _Step(rowcount=2),
        _Step(rows=after),
        _Step(rows=after),
    )

    result = boost_batch_priority_by_execute_numbers(
        connection, _target(2), _materialized_probe(*before)
    )

    assert result.status is BatchPriorityStatus.CONFIRMED
    assert result.priority_updated == 2
    assert result.already_highest == 0
    assert result.already_completed == 0
    assert result.affected_rows == 2
    update_sql, update_params = next(
        call for call in connection.calls if call[0].lstrip().startswith("UPDATE")
    )
    assert "WHERE task_id IN" in update_sql
    assert "task_status" not in update_sql
    assert "is_valid = '1'" in update_sql
    assert update_params == (1, 2)
    assert connection.commits == 1


def test_priority_is_idempotent_for_already_highest_rows() -> None:
    before = [_row(1, weight_type="-1"), _row(2)]
    after = [_row(1, weight_type="-1"), _row(2, weight_type="-1")]
    connection = _Connection(
        _Step(rows=before),
        _Step(rowcount=1),
        _Step(rows=after),
        _Step(rows=after),
    )

    result = boost_batch_priority_by_execute_numbers(
        connection, _target(2), _materialized_probe(*before)
    )

    assert result.priority_updated == 1
    assert result.already_highest == 1
    assert next(
        params
        for sql, params in connection.calls
        if sql.lstrip().startswith("UPDATE")
    ) == (2,)


def test_priority_allows_rows_that_complete_before_the_update() -> None:
    before = [_row(1), _row(2, task_status="02")]
    after = [_row(1, weight_type="-1"), _row(2, task_status="02", weight_type="-1")]
    connection = _Connection(
        _Step(rows=before),
        _Step(rowcount=2),
        _Step(rows=after),
        _Step(rows=after),
    )

    result = boost_batch_priority_by_execute_numbers(
        connection, _target(2), _materialized_probe(*before)
    )

    assert result.priority_updated == 1
    assert result.already_completed == 1
    assert next(
        params
        for sql, params in connection.calls
        if sql.lstrip().startswith("UPDATE")
    ) == (1, 2)


def test_priority_updates_row_already_failed_before_lock() -> None:
    failed = [_row(1, task_status="03")]
    updated = [_row(1, task_status="03", weight_type="-1")]
    connection = _Connection(
        _Step(rows=failed),
        _Step(rowcount=1),
        _Step(rows=updated),
        _Step(rows=updated),
    )

    result = boost_batch_priority_by_execute_numbers(
        connection, _target(1), _materialized_probe(*failed)
    )

    assert result.already_failed == 1
    assert result.priority_updated == 0
    assert result.already_highest == 0
    assert result.already_completed == 0
    assert result.affected_rows == 1
    assert next(params for sql, params in connection.calls if sql.lstrip().startswith("UPDATE")) == (1,)


def test_priority_updates_active_row_that_fails_under_transaction_lock() -> None:
    probe_row = _row(1, task_status="00")
    failed = [_row(1, task_status="03")]
    updated = [_row(1, task_status="03", weight_type="-1")]
    connection = _Connection(
        _Step(rows=failed),
        _Step(rowcount=1),
        _Step(rows=updated),
        _Step(rows=updated),
        transaction_probe_steps=(_Step(rows=failed), _Step(rows=[])),
    )

    result = boost_batch_priority_by_execute_numbers(
        connection, _target(1), _materialized_probe(probe_row)
    )

    assert result.already_failed == 1
    assert result.affected_rows == 1
    assert next(params for sql, params in connection.calls if sql.lstrip().startswith("UPDATE")) == (1,)


def test_priority_readback_accepts_active_row_that_fails_after_commit() -> None:
    before = [_row(1, task_status="00")]
    updated = [_row(1, task_status="00", weight_type="-1")]
    failed = [_row(1, task_status="03", weight_type="-1")]
    connection = _Connection(
        _Step(rows=before),
        _Step(rowcount=1),
        _Step(rows=updated),
        _Step(rows=failed),
    )

    result = boost_batch_priority_by_execute_numbers(
        connection, _target(1), _materialized_probe(*before)
    )

    assert result.affected_rows == 1
    assert result.priority_updated == 0
    assert result.already_failed == 1
    assert (
        result.priority_updated
        + result.already_highest
        + result.already_completed
        + result.already_failed
        == result.expected_count
    )


def test_priority_classification_keeps_completed_rows_out_of_already_highest() -> None:
    completed = [_row(1, task_status="02", weight_type="-1")]
    connection = _Connection(
        _Step(rows=completed),
        _Step(rows=completed),
        _Step(rows=completed),
    )

    result = boost_batch_priority_by_execute_numbers(
        connection, _target(1), _materialized_probe(*completed)
    )

    assert result.priority_updated == 0
    assert result.already_highest == 0
    assert result.already_completed == 1


def test_priority_classification_is_exclusive_when_highest_row_completes_after_commit() -> None:
    before = [_row(1, weight_type="-1")]
    transaction = [_row(1, weight_type="-1")]
    completed = [_row(1, task_status="02", weight_type="-1")]
    connection = _Connection(
        _Step(rows=before),
        _Step(rows=transaction),
        _Step(rows=completed),
    )

    result = boost_batch_priority_by_execute_numbers(
        connection, _target(1), _materialized_probe(*before)
    )

    assert result.priority_updated == 0
    assert result.already_highest == 0
    assert result.already_completed == 1
    assert (
        result.priority_updated + result.already_highest + result.already_completed
        == result.expected_count
    )


def test_priority_rejects_zero_affected_rows_after_nonhighest_precheck() -> None:
    before = [_row(1)]
    after = [_row(1, weight_type="-1")]
    connection = _Connection(
        _Step(rows=before),
        _Step(rowcount=0),
        _Step(rows=after),
        _Step(rows=after),
    )

    with pytest.raises(BatchPrioritySafetyError, match="priority update affected count mismatch"):
        boost_batch_priority_by_execute_numbers(
            connection, _target(1), _materialized_probe(*before)
        )
    assert connection.commits == 0
    assert connection.rollbacks >= 1


def test_priority_rejects_short_update_even_if_readback_is_highest() -> None:
    before = [_row(1), _row(2)]
    after = [_row(1, weight_type="-1"), _row(2, weight_type="-1")]
    connection = _Connection(
        _Step(rows=before),
        _Step(rowcount=1),
        _Step(rows=after),
        _Step(rows=after),
    )

    with pytest.raises(BatchPrioritySafetyError, match="priority update affected count mismatch"):
        boost_batch_priority_by_execute_numbers(
            connection, _target(2), _materialized_probe(*before)
        )
    assert connection.commits == 0


def test_priority_rejects_short_update_when_completed_row_is_not_highest() -> None:
    before = [_row(1), _row(2)]
    after = [_row(1, weight_type="-1"), _row(2, task_status="02")]
    connection = _Connection(
        _Step(rows=before),
        _Step(rowcount=1),
        _Step(rows=after),
        _Step(rows=after),
    )

    with pytest.raises(BatchPrioritySafetyError, match="priority update affected count mismatch"):
        boost_batch_priority_by_execute_numbers(
            connection, _target(2), _materialized_probe(*before)
        )
    assert connection.commits == 0


def test_priority_fails_closed_on_readback_identity_drift() -> None:
    before = [_row(1)]
    drifted = [_row(1, exec_num="wrong", weight_type="-1")]
    connection = _Connection(
        _Step(rows=before),
        _Step(rowcount=1),
        _Step(rows=drifted),
    )

    with pytest.raises(BatchPrioritySafetyError, match="exec_num identity drift"):
        boost_batch_priority_by_execute_numbers(
            connection, _target(1), _materialized_probe(*before)
        )

    assert connection.commits == 0
    assert connection.rollbacks >= 1


def test_priority_rechecks_both_schemas_inside_the_write_transaction() -> None:
    before = [_row(1)]
    connection = _Connection(
        _Step(rows=before),
        _Step(rowcount=1),
        _Step(rows=[_row(1, weight_type="-1")]),
        _Step(rows=[_row(1, weight_type="-1")]),
        transaction_probe_steps=(
            _Step(rows=before),
            _Step(rows=[_row(1)]),
        ),
    )

    with pytest.raises(BatchPrioritySafetyError, match="multiple allowlisted schemas"):
        boost_batch_priority_by_execute_numbers(
            connection, _target(1), _materialized_probe(*before)
        )

    assert not any(sql.lstrip().startswith("UPDATE") for sql, _params in connection.calls)


def test_priority_does_not_start_a_second_rollback_after_deadline_expires() -> None:
    clock = _Clock()
    before = [_row(1)]
    connection = _Connection(
        _Step(rows=before),
        clock=clock,
        rollback_advance_seconds=2,
    )

    with pytest.raises(BatchPriorityTransientError, match="deadline exhausted"):
        boost_batch_priority_by_execute_numbers(
            connection,
            _target(1),
            _materialized_probe(*before),
            deadline=1,
            monotonic=clock.monotonic,
        )

    assert connection.rollbacks == 1
    assert connection.closed == 1


def test_priority_fails_closed_when_active_readback_is_not_highest() -> None:
    before = [_row(1)]
    connection = _Connection(
        _Step(rows=before),
        _Step(rowcount=1),
        _Step(rows=before),
    )

    with pytest.raises(BatchPrioritySafetyError, match="row is not highest priority"):
        boost_batch_priority_by_execute_numbers(
            connection, _target(1), _materialized_probe(*before)
        )


def test_priority_rejects_nonmaterialized_probe_without_sql() -> None:
    connection = _Connection()
    probe = BatchMaterializationProbe(
        BatchMaterializationStatus.PARTIALLY_MATERIALIZED,
        "central_control_sub",
        (_row(1),),
        2,
        1,
    )

    with pytest.raises(BatchPrioritySafetyError, match="not exactly materialized"):
        boost_batch_priority_by_execute_numbers(connection, _target(2), probe)

    assert connection.calls == []


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def test_ensure_priority_fast_polls_partial_materialization_then_confirms() -> None:
    clock = _Clock()
    partial = _Connection(_Step(rows=[_row(1)]), _Step(rows=[]))
    before = [_row(1), _row(2)]
    after = [_row(1, weight_type="-1"), _row(2, weight_type="-1")]
    complete = _Connection(
        _Step(rows=before),
        _Step(rows=[]),
        _Step(rows=before),
        _Step(rowcount=2),
        _Step(rows=after),
        _Step(rows=after),
    )
    connections = [partial, complete]

    result = ensure_batch_priority(
        lambda _timeout: connections.pop(0),
        _target(2),
        timeout_seconds=5,
        poll_interval_seconds=1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result.status is BatchPriorityAttemptStatus.CONFIRMED
    assert result.priority is not None
    assert result.priority.priority_updated == 2
    assert clock.sleeps == [1]
    assert partial.closed == complete.closed == 1


def test_ensure_priority_returns_pending_at_the_bounded_deadline() -> None:
    clock = _Clock()
    connections = [
        _Connection(_Step(rows=[]), _Step(rows=[])),
        _Connection(_Step(rows=[]), _Step(rows=[])),
    ]

    result = ensure_batch_priority(
        lambda _timeout: connections.pop(0),
        _target(1),
        timeout_seconds=1,
        poll_interval_seconds=1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result.status is BatchPriorityAttemptStatus.PENDING
    assert result.materialization is BatchMaterializationStatus.NOT_MATERIALIZED
    assert clock.sleeps == [1]


def test_ensure_priority_stops_database_work_when_sql_consumes_deadline() -> None:
    clock = _Clock()
    connection = _Connection(
        _Step(rows=[_row(1)], advance_seconds=2),
        _Step(rows=[]),
        clock=clock,
    )

    result = ensure_batch_priority(
        lambda _timeout: connection,
        _target(1),
        timeout_seconds=1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result.status is BatchPriorityAttemptStatus.PENDING
    assert result.diagnostic == "priority callback deadline exhausted"
    assert len(connection.calls) == 1


def test_ensure_priority_treats_permanent_connection_factory_failure_as_conflict() -> None:
    clock = _Clock()

    def broken_factory(_timeout: float) -> _Connection:
        raise ValueError("missing database configuration")

    result = ensure_batch_priority(
        broken_factory,
        _target(1),
        timeout_seconds=5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result.status is BatchPriorityAttemptStatus.CONFLICT
    assert result.diagnostic == "ValueError"
    assert clock.sleeps == []


def test_ensure_priority_retries_transient_connection_factory_failure() -> None:
    clock = _Clock()
    attempts = 0

    def timed_out_factory(_timeout: float) -> _Connection:
        nonlocal attempts
        attempts += 1
        raise TimeoutError("connect timed out")

    result = ensure_batch_priority(
        timed_out_factory,
        _target(1),
        timeout_seconds=1,
        poll_interval_seconds=1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result.status is BatchPriorityAttemptStatus.PENDING
    assert result.diagnostic == "TimeoutError"
    assert attempts == 1
    assert clock.sleeps == [1]


def test_ensure_priority_returns_conflict_without_update() -> None:
    clock = _Clock()
    conflict = _Connection(_Step(rows=[_row(1)]), _Step(rows=[_row(1)]))

    result = ensure_batch_priority(
        lambda _timeout: conflict,
        _target(1),
        timeout_seconds=5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result.status is BatchPriorityAttemptStatus.CONFLICT
    assert result.materialization is BatchMaterializationStatus.SCHEMA_CONFLICT
    assert len(conflict.calls) == 2
