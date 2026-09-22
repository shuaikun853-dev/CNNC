"""Read-only Zhaoshang task progress layer for the STEP_1 dashboard.

纯只读观测:唯一远端调用是 adapter.query_task_progress_for_job(内部为 exact
lookup + identity 校验 + queryTaskProgress)。绝不创建/停止/导出任务,绝不写
SQLite。缓存与后台刷新线程只服务展示;任何失败都降级为 unavailable,绝不抛出
到 summary。
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Sequence

from app.services.remote_execution_policy import (
    RemoteExecutionState,
    canonical_progress_signature,
    classify_remote_execution_state,
)

# Per-job refresh rate limit: an explicit refresh of the same job within this
# window returns the current cache entry instead of dispatching another query.
REFRESH_RATE_LIMIT_SECONDS = 10.0


def _text(value: object) -> str:
    return str(value or "").strip()


def _default_adapter_factory() -> Any:
    # Deferred import: the adapter module is referenced only here so this
    # module never enters the adapter's import graph at import time.
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter

    return CnncLegacyZhaoshangAdapter()


def _unavailable_status(error_code: str | None, *, conflict: bool = False) -> dict:
    return {
        "available": False,
        "classification": None,
        "progress": None,
        "observed_at": None,
        "stale": False,
        "conflict": conflict,
        "error_code": error_code,
    }


@dataclass(frozen=True)
class ProgressObservation:
    """One immutable read-only observation of an exact remote task."""

    batch_number: str
    classification: str
    progress: dict[str, int] | None
    observed_at: str
    rsp_desc: str = ""


@dataclass
class _CacheEntry:
    observation: ProgressObservation | None = None
    observed_monotonic: float | None = None
    error_code: str | None = None
    conflict: bool = False
    last_refresh_request: float | None = None
    in_flight: Future | None = None


class _ProbeError(Exception):
    """Internal probe failure carrying its dashboard error code."""

    def __init__(self, code: str, *, conflict: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.conflict = conflict


class ZhaoshangProgressService:
    """Read-only progress cache serving dashboard snapshots and refreshes.

    Every remote read flows through ``adapter.query_task_progress_for_job``
    (exact lookup + identity verification + progress query, no writes).  The
    cache is per-job, TTL-driven (short for ACTIVE/UNKNOWN, long for
    TERMINAL), and single-flight per job.  ``snapshot`` never waits on the
    network in background mode and never raises; ``refresh`` forces one query
    subject to a per-job rate limit.
    """

    def __init__(
        self,
        adapter_factory: Callable[[], Any] = _default_adapter_factory,
        *,
        prepared_resolver: Callable[[Mapping[str, Any]], Any] | None = None,
        active_ttl_seconds: float = 15.0,
        terminal_ttl_seconds: float = 300.0,
        query_timeout_seconds: float = 10.0,
        max_concurrency: int = 2,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        background_refresh: bool = True,
    ) -> None:
        self._adapter_factory = adapter_factory
        self._prepared_resolver = prepared_resolver
        self._active_ttl_seconds = float(active_ttl_seconds)
        self._terminal_ttl_seconds = float(terminal_ttl_seconds)
        self._query_timeout_seconds = float(query_timeout_seconds)
        self._max_concurrency = max(1, int(max_concurrency))
        self._clock = clock
        self._now = now
        self._background_refresh = bool(background_refresh)
        self._lock = threading.RLock()
        self._entries: dict[str, _CacheEntry] = {}
        self._adapter: Any = None
        self._executor: ThreadPoolExecutor | None = None
        self._pending_queries = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(self, jobs: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
        """Build {job_id: status_dict} for the durable remote job rows.

        Never waits on the network (background mode), never raises: every
        failure degrades to an unavailable entry so one broken job can never
        take the dashboard summary down.
        """
        try:
            job_list = [job if isinstance(job, Mapping) else {} for job in jobs]
        except Exception:
            return {}
        name_counts: Counter[str] = Counter()
        for job in job_list:
            name = _text(job.get("task_name"))
            if name:
                name_counts[name] += 1
        results: dict[str, dict] = {}
        for job in job_list:
            key = str(job.get("id"))
            try:
                results[key] = self._snapshot_one(job, name_counts)
            except Exception:
                results[key] = _unavailable_status("PROBE_FAILED")
        return results

    def refresh(
        self, job: Mapping[str, Any], *, max_wait_seconds: float = 10.0
    ) -> dict:
        """Force one query for a single job, bypassing the TTL.

        Subject to a per-job rate limit (REFRESH_RATE_LIMIT_SECONDS): a repeat
        refresh inside the window returns the current cache entry with
        ``rate_limited=True`` and dispatches nothing.  A concurrent in-flight
        query is joined (never duplicated) for up to ``max_wait_seconds``.
        """
        try:
            job_dict = job if isinstance(job, Mapping) else {}
            job_id = str(job_dict.get("id"))
            with self._lock:
                entry = self._entries.setdefault(job_id, _CacheEntry())
                busy = entry.in_flight is not None and not entry.in_flight.done()
                if not busy:
                    moment = self._clock()
                    if (
                        entry.last_refresh_request is not None
                        and moment - entry.last_refresh_request
                        < REFRESH_RATE_LIMIT_SECONDS
                    ):
                        status = dict(self._status_from_entry(entry))
                        status["rate_limited"] = True
                        return status
                    entry.last_refresh_request = moment
            future, owns_inline = self._start_query(job_id, job_dict)
            if owns_inline:
                self._execute_query(job_id, job_dict, future)
            else:
                try:
                    future.result(timeout=max(0.0, float(max_wait_seconds)))
                except Exception:
                    # Timeout or lost executor: return the current entry.
                    pass
            with self._lock:
                entry = self._entries.setdefault(job_id, entry)
                return self._status_from_entry(entry)
        except Exception:
            return _unavailable_status("PROBE_FAILED")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _snapshot_one(self, job: Mapping[str, Any], name_counts: Mapping[str, int]) -> dict:
        identity = _text(job.get("remote_identity"))
        parts = identity.split("|") if identity else []
        if len(parts) != 3 or not all(part.strip() for part in parts):
            return _unavailable_status("INVALID_REMOTE_IDENTITY")
        name = _text(job.get("task_name"))
        if name and name_counts.get(name, 0) > 1:
            # Two durable jobs sharing one task_name make the batch-level
            # progress answer ambiguous for both; never query either.
            return _unavailable_status("TASK_BATCH_CONFLICT", conflict=True)
        job_id = str(job.get("id"))
        with self._lock:
            entry = self._entries.setdefault(job_id, _CacheEntry())
            stale = self._is_stale(entry)
            busy = entry.in_flight is not None and not entry.in_flight.done()
            # Background capacity is checked best-effort here; the strict
            # single-flight join happens inside _start_query, so a rare race
            # can overshoot by at most the colliding snapshot callers.
            capacity = (
                (not self._background_refresh)
                or self._pending_queries < self._max_concurrency
            )
            trigger = stale and not busy and capacity
        if trigger:
            future, owns_inline = self._start_query(job_id, job)
            if owns_inline:
                # Synchronous mode: the snapshot itself runs the query inline.
                self._execute_query(job_id, job, future)
        with self._lock:
            entry = self._entries.setdefault(job_id, _CacheEntry())
            return self._status_from_entry(entry)

    def _start_query(
        self, job_id: str, job: Mapping[str, Any]
    ) -> tuple[Future, bool]:
        """Create or join this job's single-flight query.

        Returns the in-flight future and whether the caller owns the
        synchronous execution duty (non-background mode).  Background mode
        dispatches to the shared executor; every caller merely observes.
        """
        with self._lock:
            entry = self._entries.setdefault(job_id, _CacheEntry())
            if entry.in_flight is not None and not entry.in_flight.done():
                return entry.in_flight, False
            future: Future = Future()
            entry.in_flight = future
            future.add_done_callback(lambda _f: self._clear_in_flight(job_id, future))
            if not self._background_refresh:
                return future, True
            self._pending_queries += 1

            def _work() -> None:
                try:
                    self._execute_query(job_id, job, future)
                finally:
                    with self._lock:
                        self._pending_queries -= 1

            self._get_executor().submit(_work)
            return future, False

    def _execute_query(
        self, job_id: str, job: Mapping[str, Any], future: Future
    ) -> None:
        try:
            observation = self._query_once(job)
        except _ProbeError as exc:
            self._record_result(
                job_id, None, error_code=exc.code, conflict=exc.conflict
            )
        except Exception:
            self._record_result(job_id, None, error_code="PROBE_FAILED", conflict=False)
        else:
            self._record_result(job_id, observation, error_code=None, conflict=False)
        if not future.done():
            future.set_result(True)

    def _query_once(self, job: Mapping[str, Any]) -> ProgressObservation:
        resolver = self._prepared_resolver
        if resolver is None:
            raise _ProbeError("PREPARED_JOB_UNAVAILABLE")
        try:
            prepared = resolver(job)
        except Exception as exc:
            raise _ProbeError("PREPARED_JOB_UNAVAILABLE") from exc
        identity = _text(job.get("remote_identity"))
        parts = identity.split("|") if identity else []
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise _ProbeError("INVALID_REMOTE_IDENTITY")
        _mall, task_id, task_number = (part.strip() for part in parts)
        adapter = self._borrow_adapter()
        try:
            result = adapter.query_task_progress_for_job(
                prepared, task_id, task_number, self._query_timeout_seconds
            )
        except Exception as exc:
            # Drop the cached adapter so the next attempt rebuilds it.
            self._adapter = None
            from app.services.legacy_zhaoshang_adapter import (
                RemoteTaskIdentityConflictError,
            )

            if isinstance(exc, RemoteTaskIdentityConflictError):
                raise _ProbeError("IDENTITY_CONFLICT", conflict=True) from exc
            raise _ProbeError("PROBE_FAILED") from exc
        if not isinstance(result, Mapping):
            self._adapter = None
            raise _ProbeError("PROBE_FAILED")
        progress = result.get("progress")
        if not isinstance(progress, Mapping):
            progress = None
        if progress is None:
            classification = RemoteExecutionState.UNKNOWN.value
        elif canonical_progress_signature(progress) is None:
            classification = RemoteExecutionState.UNKNOWN.value
        else:
            classification = classify_remote_execution_state(progress, "").value
        return ProgressObservation(
            batch_number=_text(result.get("batch_number")),
            classification=classification,
            progress=dict(progress) if progress is not None else None,
            observed_at=self._now().isoformat(),
            rsp_desc=_text(result.get("rsp_desc")),
        )

    def _borrow_adapter(self) -> Any:
        if self._adapter is None:
            try:
                self._adapter = self._adapter_factory()
            except Exception as exc:
                raise _ProbeError("PROBE_FAILED") from exc
        return self._adapter

    def _record_result(
        self,
        job_id: str,
        observation: ProgressObservation | None,
        *,
        error_code: str | None,
        conflict: bool,
    ) -> None:
        with self._lock:
            entry = self._entries.setdefault(job_id, _CacheEntry())
            if observation is not None:
                entry.observation = observation
                entry.observed_monotonic = self._clock()
                entry.error_code = None
                entry.conflict = False
            else:
                # Keep the last observation (if any); only the error state
                # moves, so a failed refresh leaves the old data stale.
                entry.error_code = error_code
                if conflict:
                    entry.conflict = True

    def _clear_in_flight(self, job_id: str, future: Future) -> None:
        with self._lock:
            entry = self._entries.get(job_id)
            if entry is not None and entry.in_flight is future:
                entry.in_flight = None

    def _get_executor(self) -> ThreadPoolExecutor:
        # Reuses one process-lifetime pool; created lazily on first submit.
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_concurrency,
                    thread_name_prefix="zhaoshang-progress",
                )
            return self._executor

    def _is_stale(self, entry: _CacheEntry) -> bool:
        if entry.observation is None or entry.observed_monotonic is None:
            return True
        ttl = (
            self._terminal_ttl_seconds
            if entry.observation.classification == RemoteExecutionState.TERMINAL.value
            else self._active_ttl_seconds
        )
        return (self._clock() - entry.observed_monotonic) > ttl

    def _status_from_entry(self, entry: _CacheEntry) -> dict:
        observation = entry.observation
        if observation is None:
            return {
                "available": False,
                "classification": None,
                "progress": None,
                "observed_at": None,
                "stale": False,
                "conflict": entry.conflict,
                "error_code": entry.error_code,
            }
        return {
            "available": True,
            "classification": observation.classification,
            "progress": dict(observation.progress) if observation.progress else None,
            "observed_at": observation.observed_at,
            "stale": self._is_stale(entry),
            "conflict": entry.conflict,
            "error_code": entry.error_code,
        }


_PROGRESS_SERVICE: ZhaoshangProgressService | None = None
_PROGRESS_SERVICE_LOCK = threading.Lock()


def get_progress_service() -> ZhaoshangProgressService:
    """Process-lifetime singleton for dashboard injection; tests build their own."""
    global _PROGRESS_SERVICE
    with _PROGRESS_SERVICE_LOCK:
        if _PROGRESS_SERVICE is None:
            _PROGRESS_SERVICE = ZhaoshangProgressService()
        return _PROGRESS_SERVICE
