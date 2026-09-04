"""Offline contracts for bounded parallel MODEL batch HTTP calls.

Design target under test (see docs/superpowers/plans/2026-08-28-step1-parallel-model-scheduler-ui.md):
with provider latency L seconds per batch of 10 rows and concurrency C, N rows
complete in ceil(ceil(N/10)/C) * L wall-clock seconds.  For N=500, C=6 and a
realistic L=12s that is 9 waves * 12s = 108s <= 120s.  The tests below scale L
down (real, non-zero sleeps — never duration=0 monkeypatches) and verify the
same mechanics: real concurrency > 1, bounded inflight, serial-equivalent
durable writes, and the scaled wall-clock bound.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import UTC, datetime
from urllib import error

import pytest

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
)


BATCH_LATENCY_SECONDS = 0.25
# Long enough that the first dispatched wave of six is fully executing at
# once (settling/claim pacing is orders of magnitude below it), so the
# observed executing-overlap ceiling is deterministic, not settle-limited.
SATURATION_LATENCY_SECONDS = 1.0
PROVIDER_LATENCY_SECONDS = 12.0
DESIGN_DEADLINE_SECONDS = 120.0


class InflightTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current = 0
        self.max_inflight = 0

    def __enter__(self):
        with self._lock:
            self._current += 1
            self.max_inflight = max(self.max_inflight, self._current)
        return self

    def __exit__(self, *_exc):
        with self._lock:
            self._current -= 1
        return False


def _rows(count: int):
    return [
        {
            "vCommodityId": f"doc-{index}",
            "documentId": f"doc-{index}",
            "skuId": f"sku-{index}",
            "extLink": "",
            "fourCategoryId": "cat",
            "list_reference_price": "10",
            "queryStr": f"标准商品标题第{index}号",
            "skuName": f"候选商品标题第{index}号",
            "__crawl_required": False,
        }
        for index in range(count)
    ]


def _deps_with_tracker(tracker: InflightTracker, latency: float = BATCH_LATENCY_SECONDS, *, fail_query: str | None = None):
    def discover(timeout):
        return DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=_rows(0),
            source_fingerprints=[],
            customer_fingerprints=[],
            evidence={"saas_count": 0, "customer_count": 0},
        )

    def model_batch(batch, timeout):
        with tracker:
            time.sleep(latency)
            if fail_query is not None and any(row["queryStr"] == fail_query for row in batch):
                raise TimeoutError("transient provider failure")
            return {index: {"match_result": "SAME"} for index, _row in enumerate(batch)}

    return TickDependencies(
        discover=discover,
        harvest=lambda jobs, timeout: {},
        # Round-3 contract: healthy detail evidence is READY (price + FOUND).
        detail=lambda row, timeout: {
            "customerPrice": "10", "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS", "customerPriceWarning": "",
            "customerPriceEvidence": "参考价1=10",
        },
        model_batch=model_batch,
        plan_crawl=None,
        submit_crawl=None,
        finalize_crawl_plan=None,
        wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
    )


def _tick_config(*, batch_size: int, concurrency: int, budget: float = 300, reserve: float = 30):
    return TickConfig(
        business_date="2026-08-11",
        runtime_budget_seconds=budget,
        heavy_work_reserve_seconds=reserve,
        lease_ttl_seconds=900,
        external_timeout_seconds=60,
        model_batch_size=batch_size,
        model_max_concurrency=concurrency,
        cutoff_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
    )


def _run_rows(deps, rows, config, tmp_path):
    original_discover = deps.discover

    def discover(timeout):
        state = original_discover(timeout)
        return DiscoveredState(
            observed_at=state.observed_at,
            headers=state.headers,
            rows=rows,
            source_fingerprints=[f"source-{row['vCommodityId']}" for row in rows],
            customer_fingerprints=[f"customer-{row['vCommodityId']}" for row in rows],
            evidence={"saas_count": len(rows), "customer_count": len(rows)},
        )

    deps.discover = discover
    store = DailyAuditStore(tmp_path / "state.sqlite")
    started = time.monotonic()
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)
    elapsed = time.monotonic() - started
    token = store.acquire_lease("2026-08-11", "inspect", 60)
    works = store.list_stage_work(token, stage="MODEL")
    store.release_lease(token)
    return summary, works, elapsed


@pytest.mark.parametrize("row_count", [300, 500])
def test_parallel_model_batches_meet_scaled_wall_clock_target(tmp_path, row_count):
    """Direct MODEL-stage benchmark: real sleeps, real SQLite, real threads.

    Acceptance evidence, all observed (never configured-then-assumed):

    * the executing-overlap ceiling (callbacks simultaneously inside the
      provider call) must reach exactly the configured concurrency — the
      first dispatched wave runs all six batches for the full latency, so
      this is deterministic rather than settle-pacing dependent;
    * the 12s-latency projection uses that observed effective concurrency:
      wall clock = ceil(batches / observed) * 12s + measured fixed overhead.
    """
    from app.workflows.incremental_price_audit import (
        TickBudget,
        _stage_model_batches,
    )

    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run("2026-08-11")
    token = store.acquire_lease("2026-08-11", "benchmark", 300)
    rows = _rows(row_count)
    ingested = store.ingest_snapshot(
        token,
        rows,
        datetime(2026, 8, 11, 10, 1, tzinfo=UTC).isoformat(timespec="seconds"),
        source_fingerprints=[f"source-{row['vCommodityId']}" for row in rows],
        customer_fingerprints=[f"customer-{row['vCommodityId']}" for row in rows],
        headers=["vCommodityId"],
    )
    items = store.ordered_snapshot_items(int(ingested["snapshot"]["id"]))
    tracker = InflightTracker()

    def model_batch(batch, timeout):
        with tracker:
            time.sleep(SATURATION_LATENCY_SECONDS)
            return {index: {"match_result": "SAME"} for index, _row in enumerate(batch)}

    configured_concurrency = 6
    config = _tick_config(batch_size=10, concurrency=configured_concurrency, budget=900, reserve=90)
    budget = TickBudget(time.monotonic(), 900, 90, time.monotonic)
    counters: dict[str, int] = {}

    started = time.monotonic()
    _stage_model_batches(store, token, items, config, model_batch, budget, counters)
    elapsed = time.monotonic() - started
    store.release_lease(token)

    works = store.list_stage_work(store.acquire_lease("2026-08-11", "inspect", 30), stage="MODEL")
    assert len(works) == row_count
    assert all(work["state"] == "SUCCESS" for work in works)

    observed_effective = tracker.max_inflight
    assert observed_effective == configured_concurrency, (
        f"executing overlap {observed_effective} never saturated {configured_concurrency} workers"
    )
    assert observed_effective <= configured_concurrency, "concurrency must stay bounded"

    expected_batches = math.ceil(row_count / 10)
    waves = math.ceil(expected_batches / observed_effective)
    http_floor = waves * SATURATION_LATENCY_SECONDS
    fixed_overhead = elapsed - http_floor
    # The fixed SQLite overhead must stay small and must not scale with the
    # rows' HTTP latency: at the real 12s provider latency the design target
    # is waves * 12s + fixed_overhead <= 120s, projected from the OBSERVED
    # effective concurrency.
    projection = waves * PROVIDER_LATENCY_SECONDS + fixed_overhead
    assert projection <= DESIGN_DEADLINE_SECONDS, (
        f"{row_count} rows: waves={waves} observed={observed_effective} "
        f"fixed_overhead={fixed_overhead:.2f}s projection={projection:.1f}s"
    )
    assert counters["model_max_inflight"] >= observed_effective
    assert counters["model_max_inflight"] <= configured_concurrency
    print(
        f"rows={row_count} batch_size=10 configured_concurrency={configured_concurrency} "
        f"observed_max_inflight={observed_effective} latency={SATURATION_LATENCY_SECONDS}s "
        f"batches={expected_batches} waves={waves} elapsed_seconds={elapsed:.3f} "
        f"fixed_overhead_seconds={fixed_overhead:.3f} "
        f"projection_at_12s={projection:.1f}s"
    )


def test_serial_concurrency_one_is_the_default_and_stays_ordered(tmp_path):
    tracker = InflightTracker()
    deps = _deps_with_tracker(tracker)
    config = _tick_config(batch_size=10, concurrency=1)

    _summary, works, _elapsed = _run_rows(deps, _rows(40), config, tmp_path)

    assert len(works) == 40
    assert all(work["state"] == "SUCCESS" for work in works)
    assert tracker.max_inflight == 1
    assert "model_max_inflight" not in _summary.counters


def test_single_batch_failure_does_not_pollute_other_batches(tmp_path):
    tracker = InflightTracker()
    deps = _deps_with_tracker(tracker, fail_query="标准商品标题第7号")
    config = _tick_config(batch_size=1, concurrency=4)

    summary, works, _elapsed = _run_rows(deps, _rows(8), config, tmp_path)

    by_query = {}
    token = None  # works already carry result/error columns
    for work in works:
        by_query[work["fingerprint"]] = work
    failed = [work for work in works if work["state"] == "RETRYABLE_FAILED"]
    succeeded = [work for work in works if work["state"] == "SUCCESS"]
    assert len(failed) == 1
    assert len(succeeded) == 7
    assert failed[0]["retryable"] == 1
    assert tracker.max_inflight > 1
    assert summary.counters["model_batches"] == 8


def test_budget_exhaustion_stops_dispatch_and_recovers_threads(tmp_path):
    """A fake monotonic clock makes the budget stop deterministic.

    Real SQLite transaction time must not decide how many batches dispatch:
    DETAIL advances the clock 0.05s per row (1.0s total for 20 rows) and each
    MODEL batch callback advances 0.1s; with budget 1.7 / reserve 0.5 only the
    first couple of waves fit before dispatch stops mid-day.
    """
    from dataclasses import dataclass

    @dataclass
    class FakeClock:
        value: float = 0.0

        def monotonic(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = FakeClock()
    baseline_threads = threading.active_count()
    tracker = InflightTracker()

    def model_batch(batch, timeout):
        with tracker:
            clock.advance(0.1)
            time.sleep(0.01)
            return {index: {"match_result": "SAME"} for index, _row in enumerate(batch)}

    rows = _rows(20)
    deps = TickDependencies(
        discover=lambda timeout: DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=rows,
            source_fingerprints=[f"source-{row['vCommodityId']}" for row in rows],
            customer_fingerprints=[f"customer-{row['vCommodityId']}" for row in rows],
            evidence={"saas_count": len(rows), "customer_count": len(rows)},
        ),
        harvest=lambda jobs, timeout: {},
        detail=lambda row, timeout: (clock.advance(0.05) or {
            "customerPrice": "10", "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS", "customerPriceWarning": "",
            "customerPriceEvidence": "参考价1=10",
        }),
        model_batch=model_batch,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
    )
    config = _tick_config(batch_size=1, concurrency=6, budget=1.7, reserve=0.5)

    store = DailyAuditStore(tmp_path / "state.sqlite")
    summary = run_incremental_tick(store, ProcessLock(tmp_path / "tick.lock"), config, deps)

    dispatched = summary.counters["model_batches"]
    assert 0 < dispatched < 20
    assert summary.outcome in {"BUDGET_EXHAUSTED", "PENDING_RETRY"}
    token = store.acquire_lease("2026-08-11", "inspect", 30)
    works = store.list_stage_work(token, stage="MODEL")
    store.release_lease(token)
    settled = [work for work in works if work["state"] in {"SUCCESS", "RETRYABLE_FAILED"}]
    pending = [work for work in works if work["state"] == "PENDING"]
    # Never-dispatched groups stay PENDING and unclaimed; every claimed row
    # was settled by exactly one batch (batch_size=1 here).
    assert len(settled) == dispatched
    assert len(pending) + len(settled) == 20
    # Worker threads are reclaimed; no leaked threads.
    assert threading.active_count() == baseline_threads


def test_concurrent_429_retries_are_bounded_and_jittered(monkeypatch):
    """Adapter-level: N concurrent 429 batches retry with jitter, not lockstep."""
    import app.services.title_match_model_adapter as adapter

    for name in (
        "TITLE_MATCH_MODEL_MAX_ATTEMPTS",
        "TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS",
        "TITLE_MATCH_MODEL_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TITLE_MATCH_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TITLE_MATCH_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("TITLE_MATCH_MODEL_NAME", "test-model")
    monkeypatch.setenv("TITLE_MATCH_MODEL_RETRY_SLEEP", "0.4")

    valid = json.dumps(
        {"results": [{"index": 0, "match_result": "SAME", "model_match": "exact", "confidence": 0.9}]}
    )
    sleep_durations: list[float] = []
    sleep_lock = threading.Lock()
    attempts = {}
    own_threads: set[threading.Thread] = set()
    threads_lock = threading.Lock()

    def fake_sleep(seconds):
        # ``adapter.time`` is the global time module: the patch is visible to
        # every thread in the process, including unrelated leaked background
        # threads from earlier tests.  Count only sleeps issued by the six
        # threads this test created so the ==12 bound stays exact.
        if threading.current_thread() not in own_threads:
            return
        with sleep_lock:
            sleep_durations.append(seconds)

    monkeypatch.setattr(adapter.time, "sleep", fake_sleep)

    def fake_post(payload, timeout_seconds=None):
        thread = threading.current_thread().name
        count = attempts.get(thread, 0) + 1
        attempts[thread] = count
        if count <= 2:
            raise error.HTTPError("https://model.invalid", 429, "rate limited", {}, None)
        return valid

    monkeypatch.setattr(adapter, "_post_model_payload", fake_post)

    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def run_one(name):
        with threads_lock:
            own_threads.add(threading.current_thread())
        try:
            results[name] = adapter.call_title_match_model_batch(
                [adapter.TitleMatchModelRequest(query_str=f"q-{name}", sku_name=f"s-{name}")],
                max_attempts=3,
                max_elapsed_seconds=30,
            )
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors[name] = exc

    threads = [threading.Thread(target=run_one, args=(str(index),)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert not errors
    assert all(result[0].match_result == "SAME" for result in results.values())
    # Exactly two retry sleeps per call: bounded, no storm amplification.
    assert len(sleep_durations) == 12
    # Jitter window [0.75, 1.25] * 0.4s keeps retries off a lockstep cadence.
    assert all(0.3 - 1e-9 <= value <= 0.5 + 1e-9 for value in sleep_durations)
    assert len({round(value, 6) for value in sleep_durations}) >= 2
