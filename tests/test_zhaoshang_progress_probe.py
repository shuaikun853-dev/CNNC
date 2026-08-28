"""Tests for the read-only Zhaoshang progress probe service (no real network).

Everything runs through an injected fake adapter and counting resolver; the
production adapter path is covered separately in
tests/test_legacy_adapter_progress_stop.py.
"""

from __future__ import annotations

import threading

from app.services.legacy_zhaoshang_adapter import RemoteTaskIdentityConflictError
from app.services.zhaoshang_progress_probe import (
    REFRESH_RATE_LIMIT_SECONDS,
    ZhaoshangProgressService,
    get_progress_service,
)

TASK_NAME = "cnnc_current_opt_领先未来商城_61284593895349"


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class _FakeAdapter:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[tuple[object, str, str, float]] = []

    def query_task_progress_for_job(self, prepared, task_id, task_number, timeout_seconds):
        self.calls.append((prepared, task_id, task_number, timeout_seconds))
        return self._handler(task_id)


class _CountingResolver:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def __call__(self, job):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ("prepared", job["id"])


def _progress(total, pending, running, failed, waiting_db, finished):
    return {
        "total": total,
        "pending": pending,
        "running": running,
        "failed": failed,
        "waiting_db": waiting_db,
        "finished": finished,
    }


def _result(progress, *, batch_number: str = "batch-1"):
    return {
        "task_id": "1269504",
        "task_number": "RW-20260825150224",
        "batch_number": batch_number,
        "progress": dict(progress) if progress is not None else None,
        "rsp_desc": "总任务量：18条，待执行：18条，执行中：0条，失败：0条，待入库：0条，执行完成：0条",
        "enabled": True,
        "remote_calls": 2,
    }


def _job(job_id, *, task_name: str | None = None, identity: str | None = None):
    return {
        "id": job_id,
        # Unique per job unless a test explicitly passes the same name: two
        # durable jobs sharing one task_name mean a batch-level conflict.
        "task_name": task_name or f"{TASK_NAME}_{job_id}",
        "remote_identity": identity or f"lxwl|{job_id}|RW-{job_id}",
        "state": "RUNNING",
    }


def _service(adapter, clock, *, background_refresh: bool = False, resolver=None):
    return ZhaoshangProgressService(
        lambda: adapter,
        prepared_resolver=resolver if resolver is not None else _CountingResolver(),
        active_ttl_seconds=15.0,
        terminal_ttl_seconds=300.0,
        query_timeout_seconds=10.0,
        clock=clock,
        background_refresh=background_refresh,
    )


_ACTIVE_0_OF_18 = _result(_progress(18, 18, 0, 0, 0, 0))
_ACTIVE_12_OF_18 = _result(_progress(18, 6, 0, 0, 0, 12))
_TERMINAL_18_OF_18 = _result(_progress(18, 0, 0, 0, 0, 18))
_TERMINAL_17_FAILED_1 = _result(_progress(18, 0, 0, 1, 0, 17))


def test_snapshot_terminal_cache_hit_skips_requery() -> None:
    adapter = _FakeAdapter(lambda task_id: _TERMINAL_18_OF_18)
    clock = _FakeClock()
    resolver = _CountingResolver()
    service = _service(adapter, clock, resolver=resolver)

    first = service.snapshot([_job(1)])
    second = service.snapshot([_job(1)])

    assert first["1"]["available"] is True
    assert first["1"]["classification"] == "REMOTE_TERMINAL"
    assert second["1"]["classification"] == "REMOTE_TERMINAL"
    assert second["1"]["stale"] is False
    assert len(adapter.calls) == 1
    assert resolver.calls == 1


def test_snapshot_active_short_ttl_expires_and_requeries() -> None:
    adapter = _FakeAdapter(lambda task_id: _ACTIVE_0_OF_18)
    clock = _FakeClock()
    service = _service(adapter, clock)

    first = service.snapshot([_job(1)])
    assert len(adapter.calls) == 1
    assert first["1"]["classification"] == "REMOTE_ACTIVE"

    clock.advance(16.0)  # beyond the 15s active TTL
    second = service.snapshot([_job(1)])

    assert len(adapter.calls) == 2
    assert second["1"]["classification"] == "REMOTE_ACTIVE"
    assert second["1"]["stale"] is False


def test_snapshot_terminal_long_ttl_does_not_requery() -> None:
    adapter = _FakeAdapter(lambda task_id: _TERMINAL_18_OF_18)
    clock = _FakeClock()
    service = _service(adapter, clock)

    service.snapshot([_job(1)])
    clock.advance(200.0)  # still inside the 300s terminal TTL
    service.snapshot([_job(1)])
    assert len(adapter.calls) == 1

    clock.advance(101.0)  # 301s total: now expired
    service.snapshot([_job(1)])
    assert len(adapter.calls) == 2


def test_single_flight_concurrent_refresh_queries_once() -> None:
    adapter = _FakeAdapter(lambda task_id: _ACTIVE_0_OF_18)
    clock = _FakeClock()
    service = _service(adapter, clock)
    job = _job(1)

    started = threading.Event()
    release = threading.Event()
    original = adapter.query_task_progress_for_job

    def slow_query(prepared, task_id, task_number, timeout_seconds):
        started.set()
        assert release.wait(timeout=10.0)
        return original(prepared, task_id, task_number, timeout_seconds)

    adapter.query_task_progress_for_job = slow_query

    holder = {}

    def _first():
        holder["first"] = service.refresh(job)

    thread = threading.Thread(target=_first)
    thread.start()
    # The in-flight query is now live (the future exists), so a concurrent
    # refresh must join it instead of dispatching a second query.
    assert started.wait(timeout=10.0)

    second_holder = {}

    def _second():
        second_holder["status"] = service.refresh(job, max_wait_seconds=10.0)

    joiner = threading.Thread(target=_second)
    joiner.start()
    release.set()
    thread.join(timeout=10.0)
    joiner.join(timeout=10.0)

    assert len(adapter.calls) == 1
    assert holder["first"]["available"] is True
    assert second_holder["status"]["available"] is True
    assert "rate_limited" not in second_holder["status"]


def test_duplicate_task_name_marks_batch_conflict() -> None:
    adapter = _FakeAdapter(lambda task_id: _ACTIVE_0_OF_18)
    clock = _FakeClock()
    service = _service(adapter, clock)

    jobs = [
        _job(1, task_name=TASK_NAME),
        _job(2, task_name=TASK_NAME, identity="lxwl|2|RW-2"),
    ]  # same task_name on purpose
    status = service.snapshot(jobs)

    assert status["1"]["conflict"] is True
    assert status["1"]["available"] is False
    assert status["1"]["error_code"] == "TASK_BATCH_CONFLICT"
    assert status["2"]["conflict"] is True
    assert status["2"]["error_code"] == "TASK_BATCH_CONFLICT"
    assert adapter.calls == []


def test_one_failing_job_does_not_affect_others() -> None:
    def handler(task_id):
        if task_id == "2":
            raise RuntimeError("remote transport exploded")
        return _ACTIVE_0_OF_18

    adapter = _FakeAdapter(handler)
    clock = _FakeClock()
    service = _service(adapter, clock)

    status = service.snapshot([_job(1), _job(2)])

    assert status["1"]["available"] is True
    assert status["1"]["classification"] == "REMOTE_ACTIVE"
    assert status["2"]["available"] is False
    assert status["2"]["error_code"] == "PROBE_FAILED"
    assert status["2"]["observed_at"] is None


def test_bad_remote_identity_is_invalid() -> None:
    adapter = _FakeAdapter(lambda task_id: _ACTIVE_0_OF_18)
    clock = _FakeClock()
    service = _service(adapter, clock)

    status = service.snapshot([_job(1, identity="not-a-valid-identity")])

    assert status["1"]["available"] is False
    assert status["1"]["error_code"] == "INVALID_REMOTE_IDENTITY"
    assert adapter.calls == []


def test_unprovable_progress_classifies_unknown() -> None:
    cases = [
        # Missing fields: the six-counter signature is incomplete.
        {"total": 18, "pending": 1},
        # total=0 is structurally inadmissible.
        _progress(0, 0, 0, 0, 0, 0),
        # finished + failed > total contradicts the counters.
        _progress(5, 0, 0, 1, 0, 5),
    ]
    adapter = _FakeAdapter(lambda task_id, _cases=cases: _result(_cases[int(task_id) - 1]))
    clock = _FakeClock()
    service = _service(adapter, clock)

    status = service.snapshot([_job(1), _job(2), _job(3)])

    for key in ("1", "2", "3"):
        assert status[key]["available"] is True
        assert status[key]["classification"] == "REMOTE_UNKNOWN"
        assert status[key]["stale"] is False


def test_never_queried_job_is_unavailable_without_observation() -> None:
    def handler(task_id):
        raise RuntimeError("always fails")

    adapter = _FakeAdapter(handler)
    clock = _FakeClock()
    service = _service(adapter, clock)

    status = service.snapshot([_job(1)])

    assert status["1"]["available"] is False
    assert status["1"]["observed_at"] is None
    assert status["1"]["classification"] is None
    assert status["1"]["progress"] is None
    assert status["1"]["error_code"] == "PROBE_FAILED"


def test_refresh_rate_limited_within_window() -> None:
    adapter = _FakeAdapter(lambda task_id: _ACTIVE_0_OF_18)
    clock = _FakeClock()
    service = _service(adapter, clock)
    job = _job(1)

    first = service.refresh(job)
    assert first["available"] is True
    assert "rate_limited" not in first
    assert len(adapter.calls) == 1

    limited = service.refresh(job)
    assert limited.get("rate_limited") is True
    assert len(adapter.calls) == 1  # no re-query inside the window

    clock.advance(REFRESH_RATE_LIMIT_SECONDS)
    again = service.refresh(job)
    assert "rate_limited" not in again
    assert len(adapter.calls) == 2


def test_classifier_semantics_via_service() -> None:
    by_task = {
        "1": _ACTIVE_0_OF_18,
        "2": _ACTIVE_12_OF_18,
        "3": _TERMINAL_18_OF_18,
        "4": _TERMINAL_17_FAILED_1,
    }
    adapter = _FakeAdapter(lambda task_id: by_task[task_id])
    clock = _FakeClock()
    service = _service(adapter, clock)

    status = service.snapshot([_job(1), _job(2), _job(3), _job(4)])

    assert status["1"]["classification"] == "REMOTE_ACTIVE"
    assert status["2"]["classification"] == "REMOTE_ACTIVE"
    assert status["3"]["classification"] == "REMOTE_TERMINAL"
    assert status["4"]["classification"] == "REMOTE_TERMINAL"
    assert status["1"]["progress"]["pending"] == 18
    assert status["3"]["progress"]["finished"] == 18


def test_snapshot_never_raises_when_resolver_fails() -> None:
    adapter = _FakeAdapter(lambda task_id: _ACTIVE_0_OF_18)
    clock = _FakeClock()
    service = _service(adapter, clock, resolver=_CountingResolver(ValueError("job gone")))

    status = service.snapshot([_job(1), _job(2)])

    assert status["1"]["available"] is False
    assert status["1"]["error_code"] == "PREPARED_JOB_UNAVAILABLE"
    assert status["2"]["error_code"] == "PREPARED_JOB_UNAVAILABLE"


def test_missing_resolver_reports_prepared_job_unavailable() -> None:
    adapter = _FakeAdapter(lambda task_id: _ACTIVE_0_OF_18)
    clock = _FakeClock()
    service = ZhaoshangProgressService(lambda: adapter, clock=clock, background_refresh=False)

    status = service.snapshot([_job(1)])

    assert status["1"]["available"] is False
    assert status["1"]["error_code"] == "PREPARED_JOB_UNAVAILABLE"
    assert adapter.calls == []


def test_identity_conflict_from_adapter_marks_conflict() -> None:
    def handler(task_id):
        raise RemoteTaskIdentityConflictError("live task identity does not match")

    adapter = _FakeAdapter(handler)
    clock = _FakeClock()
    service = _service(adapter, clock)

    status = service.snapshot([_job(1)])

    assert status["1"]["available"] is False
    assert status["1"]["conflict"] is True
    assert status["1"]["error_code"] == "IDENTITY_CONFLICT"


def test_failed_requery_keeps_old_observation_as_stale() -> None:
    responses = [_ACTIVE_0_OF_18, RuntimeError("second look failed")]
    adapter = _FakeAdapter(
        lambda task_id: (
            responses[1] if len(adapter.calls) > 1 else responses[0]
        )
    )
    clock = _FakeClock()
    service = _service(adapter, clock)

    first = service.snapshot([_job(1)])
    assert first["1"]["available"] is True
    assert first["1"]["error_code"] is None

    clock.advance(30.0)  # force the active TTL to expire
    second = service.snapshot([_job(1)])

    assert len(adapter.calls) == 2
    assert second["1"]["available"] is True  # old observation retained
    assert second["1"]["stale"] is True
    assert second["1"]["error_code"] == "PROBE_FAILED"
    assert second["1"]["classification"] == "REMOTE_ACTIVE"


def test_get_progress_service_returns_singleton() -> None:
    assert get_progress_service() is get_progress_service()
