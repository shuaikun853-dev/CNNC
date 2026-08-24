"""Closeout-before-18:30 contracts: source stability, closeout pacing, recovery closeout.

Scheduler routing (17:15 tick, 18:00 closeout, IgnoreNew) lives in
``test_incremental_audit_operations.py``; these tests cover the engine-level
behaviour the routing depends on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook

import app.services.incremental_audit_runtime as runtime_module
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_runtime import (
    runtime_paths,
    run_remote_harvest_recovery_command,
    run_runtime_command,
)
from app.services.incremental_audit_status import collect_runtime_status
from app.services.legacy_zhaoshang_adapter import (
    CrawlRequest,
    CnncLegacyZhaoshangAdapter,
)
from app.workflows.incremental_price_audit import (
    CrawlMember,
    CrawlPlan,
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
    source_stability_seconds,
)
from app.workflows.incremental_price_audit_finalize import (
    FinalizeBlocked,
    FinalizeReadiness,
    materialize_frozen_daily_run,
)


BUSINESS_DATE = "2026-08-11"
CUTOFF = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)


class VirtualClock:
    """Monotonic + sleep that advance together so closeout waits are virtual."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


def _row(v: str, sku: str, **extra) -> dict[str, Any]:
    row = {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": "",
        "fourCategoryId": "cat",
        "list_reference_price": "10",
        "queryStr": "title",
        "skuName": "title",
    }
    row.update(extra)
    return row


def _observation(rows, observed_at: datetime) -> DiscoveredState:
    return DiscoveredState(
        observed_at=observed_at,
        headers=["vCommodityId", "skuId", "extLink"],
        rows=list(rows),
        source_fingerprints=[f"source-{row['vCommodityId']}-{row['skuId']}" for row in rows],
        customer_fingerprints=[f"customer-{row['vCommodityId']}" for row in rows],
        evidence={"saas_count": len(rows), "customer_count": 0},
    )


def _config(**overrides) -> TickConfig:
    values = {
        "business_date": BUSINESS_DATE,
        "runtime_budget_seconds": 120,
        "heavy_work_reserve_seconds": 5,
        "lease_ttl_seconds": 90,
        "cutoff_at": CUTOFF,
    }
    values.update(overrides)
    return TickConfig(**values)


def _deps_for(rows, observed_at: datetime) -> TickDependencies:
    return TickDependencies(
        discover=lambda _timeout: _observation(rows, observed_at),
        detail=lambda _row_value, _timeout: {"customerPrice": "10"},
        model_batch=lambda batch, _timeout: {
            index: {"match_result": "SAME", "confidence": 1}
            for index, _row_value in enumerate(batch)
        },
        harvest=lambda jobs, _timeout: {
            int(job["id"]): {"state": "RUNNING", "remote_identity": "x"} for job in jobs
        },
        wall_clock=lambda: datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
    )


def _seed_observation(store: DailyAuditStore, rows, observed_at: datetime) -> None:
    observation = _observation(rows, observed_at)
    lock = ProcessLock(store.path.with_name("seed.lock"))
    lock.acquire()
    token = store.acquire_lease(BUSINESS_DATE, "seed", 60, process_lock=lock)
    try:
        store.ensure_run(BUSINESS_DATE, cutoff_at=CUTOFF.isoformat(timespec="seconds"))
        store.ingest_snapshot(
            token,
            list(observation.rows),
            observation.observed_at.isoformat(timespec="seconds"),
            source_fingerprints=observation.source_fingerprints,
            customer_fingerprints=observation.customer_fingerprints,
            headers=observation.headers,
            evidence=observation.evidence,
        )
    finally:
        store.release_lease(token)
        lock.release()


# ---------------------------------------------------------------------------
# Source stability (P0-3): the freeze gate needs two identical authoritative
# observations spanning the configured window, never customer_count equality.
# ---------------------------------------------------------------------------


def test_two_identical_observations_fifteen_minutes_apart_freeze_immediately(tmp_path):
    # 17:45 (09:45 UTC) and 18:00 (10:00 UTC) observed the same content, so
    # the 18:00 finalize may freeze at once instead of waiting until 18:10.
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    _seed_observation(store, [_row("v1", "s1")], datetime(2026, 8, 11, 9, 45, tzinfo=UTC))

    summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        _config(),
        _deps_for([_row("v1", "s1")], datetime(2026, 8, 11, 10, 0, tzinfo=UTC)),
        finalize=True,
    )

    snapshots = store.list_snapshots(BUSINESS_DATE)
    assert summary.frozen_snapshot_id == snapshots[-1]["id"]
    assert source_stability_seconds(store, BUSINESS_DATE, snapshots[-1]) == 900.0


def test_changed_source_between_observations_blocks_freeze(tmp_path):
    # 17:45 = 380 rows, 18:00 = 395 rows: never freeze, process the growth.
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    _seed_observation(
        store,
        [_row(f"v{index}", f"s{index}") for index in range(3)],
        datetime(2026, 8, 11, 9, 45, tzinfo=UTC),
    )

    summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        _config(),
        _deps_for(
            [_row(f"v{index}", f"s{index}") for index in range(4)],
            datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
        ),
        finalize=True,
    )

    assert summary.outcome == "WAITING_SOURCE_COUNT"
    assert summary.frozen_snapshot_id is None


def test_five_minutes_of_identical_observations_do_not_freeze_at_600s_window(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    lock = ProcessLock(tmp_path / "tick.lock")
    rows = [_row("v1", "s1")]
    run_incremental_tick(store, lock, _config(), _deps_for(rows, datetime(2026, 8, 11, 10, 0, tzinfo=UTC)))
    summary = run_incremental_tick(
        store, lock, _config(), _deps_for(rows, datetime(2026, 8, 11, 10, 5, tzinfo=UTC)), finalize=True
    )

    assert summary.outcome == "WAITING_SOURCE_COUNT"
    assert source_stability_seconds(store, BUSINESS_DATE, store.list_snapshots(BUSINESS_DATE)[-1]) == 300.0


def test_ten_minutes_of_identical_observations_freeze_the_latest_snapshot(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    lock = ProcessLock(tmp_path / "tick.lock")
    rows = [_row("v1", "s1")]
    for hour, minute in ((10, 0), (10, 5)):
        run_incremental_tick(store, lock, _config(), _deps_for(rows, datetime(2026, 8, 11, hour, minute, tzinfo=UTC)))
    summary = run_incremental_tick(
        store, lock, _config(), _deps_for(rows, datetime(2026, 8, 11, 10, 10, tzinfo=UTC)), finalize=True
    )

    snapshots = store.list_snapshots(BUSINESS_DATE)
    assert len(snapshots) == 3
    assert summary.frozen_snapshot_id == snapshots[-1]["id"]


def test_same_row_count_with_changed_hash_does_not_freeze(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    lock = ProcessLock(tmp_path / "tick.lock")
    run_incremental_tick(
        store, lock, _config(),
        _deps_for([_row("v1", "s1"), _row("v2", "s2")], datetime(2026, 8, 11, 9, 50, tzinfo=UTC)),
    )
    summary = run_incremental_tick(
        store, lock, _config(),
        _deps_for([_row("v1", "s1"), _row("v2", "s2-changed")], datetime(2026, 8, 11, 10, 0, tzinfo=UTC)),
        finalize=True,
    )

    assert summary.outcome == "WAITING_SOURCE_COUNT"
    snapshots = store.list_snapshots(BUSINESS_DATE)
    assert snapshots[0]["row_count"] == snapshots[1]["row_count"] == 2
    assert snapshots[0]["ordered_hash"] != snapshots[1]["ordered_hash"]
    assert source_stability_seconds(store, BUSINESS_DATE, snapshots[-1]) == 0.0


def test_new_observation_inside_window_resets_the_stability_clock(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    lock = ProcessLock(tmp_path / "tick.lock")
    rows = [_row("v1", "s1")]
    changed = [_row("v1", "s1"), _row("v2", "s2")]
    run_incremental_tick(store, lock, _config(), _deps_for(rows, datetime(2026, 8, 11, 9, 50, tzinfo=UTC)))
    run_incremental_tick(store, lock, _config(), _deps_for(changed, datetime(2026, 8, 11, 9, 55, tzinfo=UTC)))
    run_incremental_tick(store, lock, _config(), _deps_for(changed, datetime(2026, 8, 11, 10, 3, tzinfo=UTC)))
    latest = store.list_snapshots(BUSINESS_DATE)[-1]
    # 10:03 is only 8 minutes after the 09:55 change; the earlier 09:50 run
    # of different content cannot extend the stability window.
    assert source_stability_seconds(store, BUSINESS_DATE, latest) == 480.0


def test_customer_count_equality_is_never_a_freeze_condition(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    lock = ProcessLock(tmp_path / "tick.lock")
    rows = [_row("v1", "s1"), _row("v2", "s2")]
    # saas_count=2 but customer_count=0 in both observations: production has
    # observed SaaS=395/customer=164; equality must be neither required nor
    # sufficient — stability alone decides.
    run_incremental_tick(store, lock, _config(), _deps_for(rows, datetime(2026, 8, 11, 9, 50, tzinfo=UTC)))
    summary = run_incremental_tick(
        store, lock, _config(), _deps_for(rows, datetime(2026, 8, 11, 10, 0, tzinfo=UTC)), finalize=True
    )

    assert summary.frozen_snapshot_id is not None
    assert summary.counters["customer_count"] == 0
    assert summary.counters["saas_count"] == 2


# ---------------------------------------------------------------------------
# Closeout progression (P0-2): one process drives the day to FINAL.
# ---------------------------------------------------------------------------


class CloseoutHarness:
    def __init__(
        self,
        tmp_path: Path,
        discovers: list[DiscoveredState],
        *,
        enable_submit: bool = False,
    ) -> None:
        self.clock = VirtualClock()
        self.events: list[str] = []
        self.discover_times: list[float] = []
        self.sweep_times: list[float] = []
        self.materialize_calls: list[str] = []
        self.submitted: list[tuple[str, ...]] = []
        self.submit_times: list[float] = []
        self.submitted_job_crawls: dict[int, list[dict[str, Any]]] = {}
        self.gate_checks = 0
        self._discovers = list(discovers)
        self.root = tmp_path / "runtime"

        def discover(_timeout: float) -> DiscoveredState:
            self.discover_times.append(self.clock.monotonic())
            if not self._discovers:
                raise AssertionError("unexpected extra discovery")
            return self._discovers.pop(0)

        def detail(row, _timeout):
            self.events.append(f"detail:{row['vCommodityId']}")
            return {"customerPrice": "10"}

        def model_batch(batch, _timeout):
            for row in batch:
                self.events.append(f"model:{row['vCommodityId']}")
            return {index: {"match_result": "SAME", "confidence": 1} for index, _row in enumerate(batch)}

        def materialize(*args):
            self.materialize_calls.append(args[2])
            return object()

        self.deps = TickDependencies(
            discover=discover,
            harvest=self._harvest,
            detail=detail,
            model_batch=model_batch,
            wall_clock=lambda: datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
        )
        if enable_submit:
            # The full remote-write dependency seam the production closeout
            # gets under --enable-remote-writes: planner, plan finalizer,
            # submitter and the durable stall gate.
            def plan(items, _timeout):
                members = [
                    CrawlMember(
                        mall_key="mall-a",
                        source_id=item["row"]["vCommodityId"],
                        sku_id=item["row"]["skuId"],
                        exact_url=item["row"]["extLink"],
                        backend_version="legacy-v1",
                        row=item["row"],
                    )
                    for item in items
                ]
                return [
                    CrawlPlan(
                        mall_key="mall-a",
                        logical_key="mall-a:batch",
                        task_name="task-mall-a",
                        payload_hash="payload-a",
                        members=members,
                    )
                ]

            def finalize(plan, members, _timeout):
                payload = {"members": [member.sku_id for member in members]}
                suffix = "-".join(payload["members"])
                return CrawlPlan(
                    mall_key=plan.mall_key,
                    logical_key=f"{plan.mall_key}:{suffix}",
                    task_name=f"tick-{suffix}",
                    payload_hash=f"payload-{suffix}",
                    payload_json=payload,
                    members=tuple(members),
                )

            def submit(job, crawls, _timeout):
                self.submit_times.append(self.clock.monotonic())
                self.submitted.append(tuple(crawl["sku_id"] for crawl in crawls))
                self.submitted_job_crawls[int(job["id"])] = [dict(crawl) for crawl in crawls]
                return {
                    "remote_identity": f"mall-a|id-{job['id']}|num-{job['id']}",
                    "state": "SUBMITTED",
                }

            def submit_gate():
                self.gate_checks += 1
                return None

            self.deps.plan_crawl = plan
            self.deps.finalize_crawl_plan = finalize
            self.deps.submit_crawl = submit
            self.deps.submit_gate = submit_gate
        self.materialize = materialize

    def _harvest(self, jobs, _timeout):
        if jobs:
            self.sweep_times.append(self.clock.monotonic())
        return {
            int(job["id"]): {"state": "RUNNING", "remote_identity": str(job.get("remote_identity") or "x")}
            for job in jobs
        }

    def run(self, **kwargs):
        values = {
            "business_date": BUSINESS_DATE,
            "runtime_root": self.root,
            "dependencies": self.deps,
            "materialize": self.materialize,
            "monotonic": self.clock.monotonic,
            "closeout_sleep": self.clock.sleep,
        }
        values.update(kwargs)
        return run_runtime_command("closeout", **values)


def test_closeout_processes_new_saas_rows_in_the_same_process(tmp_path):
    base_rows = [_row(f"v{index}", f"s{index}") for index in range(2)]
    # A distinct query makes the new row's model fingerprint its own, so its
    # MODEL stage cannot silently reuse the earlier rows' result.
    grown_rows = base_rows + [_row("v3", "s3", queryStr="title-3", skuName="name-3")]
    harness = CloseoutHarness(
        tmp_path,
        [
            _observation(base_rows, datetime(2026, 8, 11, 10, 1, tzinfo=UTC)),
            _observation(grown_rows, datetime(2026, 8, 11, 10, 6, tzinfo=UTC)),
            _observation(grown_rows, datetime(2026, 8, 11, 10, 11, tzinfo=UTC)),
            _observation(grown_rows, datetime(2026, 8, 11, 10, 16, tzinfo=UTC)),
        ],
    )

    result = harness.run(runtime_budget_seconds=2000)

    # The 18:05 growth enters Detail and Model inside this closeout instead of
    # waiting for the next scheduler wakeup, and the source freezes once the
    # 10-minute stability window holds (10:06 -> 10:16).
    assert "detail:v3" in harness.events and "model:v3" in harness.events
    assert result.publication is not None
    assert result.tick.frozen_snapshot_id is not None
    assert harness.discover_times == [1000.0, 1300.0, 1600.0, 1900.0]


def test_closeout_submits_and_harvests_late_delta_remote_work_before_freeze(tmp_path):
    """Characterization: the closeout already owns the full late-delta chain.

    18:00 stops CNNC pushes, not SaaS latency.  A row pushed at 17:59 that
    only appears on SaaS at 18:06 must, inside the same closeout process,
    re-query SaaS (observation), flow through Detail, Model and crawl
    planning, pass the submit gate, be safely submitted, keep being
    harvested, and only freeze once the 600-second source-stability window
    holds on a snapshot that includes the late row.
    """
    base_rows = [_row(f"v{index}", f"s{index}") for index in (1, 2)]
    grown_rows = base_rows + [_row("v3", "s3", queryStr="title-3", skuName="name-3")]
    harness = CloseoutHarness(
        tmp_path,
        [
            _observation(base_rows, datetime(2026, 8, 11, 10, 1, tzinfo=UTC)),
            _observation(grown_rows, datetime(2026, 8, 11, 10, 6, tzinfo=UTC)),
            _observation(grown_rows, datetime(2026, 8, 11, 10, 11, tzinfo=UTC)),
            _observation(grown_rows, datetime(2026, 8, 11, 10, 16, tzinfo=UTC)),
        ],
        enable_submit=True,
    )

    def harvest(jobs, _timeout):
        # The priority-probe chain of the legacy bridge folds into the
        # terminal COMPLETED result here; priority policy itself is covered
        # by the tick and adapter suites.
        result = {}
        for job in jobs:
            job_id = int(job["id"])
            crawls = harness.submitted_job_crawls.get(job_id, [])
            result[job_id] = {
                "state": "COMPLETED",
                "remote_identity": job.get("remote_identity")
                or f"mall-a|id-{job_id}|num-{job_id}",
                "crawl_results": {int(crawl["id"]): {"price": "10"} for crawl in crawls},
            }
        return result

    harness.deps.harvest = harvest

    result = harness.run(runtime_budget_seconds=2000)

    # The 18:01 baseline and the 18:06 late row each reach the planner and
    # the submitter inside this closeout; only delta work is submitted.
    assert harness.submitted == [("s1", "s2"), ("s3",)]
    assert harness.gate_checks >= 2  # the gate runs before every dispatch
    assert "detail:v3" in harness.events and "model:v3" in harness.events
    # The late submit happens on the 18:06 observation pass, well before the
    # freeze pass at 18:16.
    assert len(harness.submit_times) == 2
    assert 1300.0 <= harness.submit_times[1] < 1900.0
    # Freeze still waits for the full 600-second stability window
    # (10:06 -> 10:16) and freezes a snapshot that includes the late row.
    assert harness.discover_times == [1000.0, 1300.0, 1600.0, 1900.0]
    assert result.tick.frozen_snapshot_id is not None
    assert result.publication is not None

    paths = runtime_paths(BUSINESS_DATE, harness.root)
    store = DailyAuditStore(paths.state_path)
    run = store.get_run(BUSINESS_DATE)
    frozen_items = store.ordered_snapshot_items(run["frozen_snapshot_id"])
    assert {item["row"]["skuId"] for item in frozen_items} == {"s1", "s2", "s3"}
    assert source_stability_seconds(store, BUSINESS_DATE, {
        "id": run["frozen_snapshot_id"],
        "row_count": len(frozen_items),
        "ordered_hash": next(
            snapshot["ordered_hash"]
            for snapshot in store.list_snapshots(BUSINESS_DATE)
            if snapshot["id"] == run["frozen_snapshot_id"]
        ),
    }) == 600.0
    # Harvest keeps running through the closeout: every submitted remote job
    # completed with exact identity, and no unknown/manual state appeared.
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    token = store.acquire_lease(BUSINESS_DATE, "verify", 60, process_lock=lock)
    try:
        jobs = store.list_remote_jobs(token)
        assert len(jobs) == 2
        assert {job["state"] for job in jobs} == {"COMPLETED"}
        assert all(job["remote_identity"].startswith("mall-a|id-") for job in jobs)
    finally:
        store.release_lease(token)
        lock.release()
    assert result.tick.counters["submit_unknown"] == 0
    assert result.tick.counters["submit_paused"] == 0
    assert result.tick.counters["crawl_submitted"] == 2


def test_closeout_paces_sweeps_independently_of_five_minute_observations(tmp_path):
    _seed_pending_remote_jobs(tmp_path, 2)
    rows = [_row("v1", "s1")]
    harness = CloseoutHarness(
        tmp_path,
        [_observation(rows, datetime(2026, 8, 11, 10, minute, tzinfo=UTC)) for minute in (0, 5, 10, 15)],
    )

    # Observations every 300 virtual seconds, full sweeps only every 400, and
    # a stability window that never completes: the closeout must stop at its
    # wall deadline without freezing, spinning, or observing more often.
    result = harness.run(
        runtime_budget_seconds=1000,
        closeout_observation_interval_seconds=300,
        closeout_remote_sweep_interval_seconds=400,
        source_stability_seconds=10_000,
    )

    assert result.tick.outcome == "CLOSEOUT_DEADLINE"
    assert result.publication is None
    # Observations at 0, 300, 600 virtual seconds; the 900-second observation
    # never starts because only the finalization reserve remains after 880.
    assert harness.discover_times == [1000.0, 1300.0, 1600.0]
    # Sweeps at 0, 400, 800; the 400/800 sweeps are sweep-only passes that
    # skip discovery (it is not due), which is exactly the phase control the
    # orchestrator needs so a remote sweep never forces a SaaS observation.
    assert harness.sweep_times == [1000.0, 1400.0, 1800.0]


def _seed_pending_remote_jobs(tmp_path: Path, count: int) -> tuple[DailyAuditStore, list[int]]:
    paths = runtime_paths(BUSINESS_DATE, tmp_path / "runtime")
    store = DailyAuditStore(paths.state_path)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    token = store.acquire_lease(BUSINESS_DATE, "seed", 120, process_lock=lock)
    try:
        store.ensure_run(BUSINESS_DATE, cutoff_at=CUTOFF.isoformat(timespec="seconds"))
        job_ids: list[int] = []
        for index in range(count):
            crawl = store.ensure_crawl_work(
                token, "mall", f"sku-{index}", f"https://shop.example/{index}",
                "legacy-v1", f"fp-{index}", source_id=f"v-{index}",
            )
            job = store.prepare_remote_intent(
                token, f"key-{index}", f"task-{index}", f"payload-{index}",
                mall_key="mall", input_path="", result_path="", payload_json={},
            )
            store.link_crawl_work_to_remote_job(token, int(crawl["id"]), int(job["id"]))
            store.claim_remote_submission(token, int(job["id"]))
            store.reconcile_remote_job(
                token, int(job["id"]), remote_identity=f"mall|id-{index}|num-{index}", state="SUBMITTED",
            )
            job_ids.append(int(job["id"]))
    finally:
        store.release_lease(token)
        lock.release()
    return store, job_ids


def test_closeout_sweep_covers_all_jobs_in_batches_of_at_most_six(tmp_path):
    _store, job_ids = _seed_pending_remote_jobs(tmp_path, 8)
    seen_batches: list[list[int]] = []
    harness = CloseoutHarness(tmp_path, [_observation([_row("v1", "s1")], datetime(2026, 8, 11, 10, 1, tzinfo=UTC))])

    def harvest(jobs, _timeout):
        if jobs:
            seen_batches.append([int(job["id"]) for job in jobs])
        return {
            int(job["id"]): {"state": "RUNNING", "remote_identity": str(job.get("remote_identity") or "x")}
            for job in jobs
        }

    harness.deps.harvest = harvest

    def blocked(*_args):
        raise FinalizeBlocked(
            "frozen snapshot still has mandatory pending or invalid work",
            FinalizeReadiness(crawl_pending=8, remote_pending=8),
        )

    harness.materialize = blocked
    result = harness.run(
        runtime_budget_seconds=1000,
        source_stability_seconds=0,
        closeout_remote_sweep_interval_seconds=600,
    )

    # The source freezes on pass 1; both sweeps must cover all 8 eligible
    # remote jobs exactly once each, in batches of at most 6.
    assert result.tick.outcome == "CLOSEOUT_DEADLINE"
    assert len(seen_batches) == 4  # two sweeps x batches of 6 + 2
    for sweep in (seen_batches[:2], seen_batches[2:]):
        flat = [job_id for batch in sweep for job_id in batch]
        assert all(len(batch) <= 6 for batch in sweep)
        assert sorted(flat) == sorted(job_ids)
        assert len(flat) == len(set(flat)), "one remote job is checked at most once per sweep"


def test_closeout_deadline_checkpoints_and_the_next_closeout_finishes(tmp_path):
    store, job_ids = _seed_pending_remote_jobs(tmp_path, 3)
    first = CloseoutHarness(tmp_path, [_observation([_row("v1", "s1")], datetime(2026, 8, 11, 10, 1, tzinfo=UTC))])

    def blocked(*_args):
        raise FinalizeBlocked(
            "frozen snapshot still has mandatory pending or invalid work",
            FinalizeReadiness(crawl_pending=3, remote_pending=3),
        )

    first.materialize = blocked
    stopped = first.run(
        runtime_budget_seconds=700,
        source_stability_seconds=0,
        closeout_remote_sweep_interval_seconds=600,
    )

    assert stopped.tick.outcome == "CLOSEOUT_DEADLINE"
    assert stopped.publication is None
    assert store.get_run(BUSINESS_DATE)["state"] != "FINALIZED"
    for job_id in job_ids:
        assert store.get_remote_job(job_id)["state"] in {"SUBMITTED", "RUNNING"}

    second = CloseoutHarness(tmp_path, [])
    second.deps.discover = lambda _timeout: (_ for _ in ()).throw(
        AssertionError("the resumed closeout must reuse the frozen snapshot")
    )

    def finishing_harvest(jobs, _timeout):
        second.sweep_times.append(second.clock.monotonic())
        return {
            int(job["id"]): {
                "state": "COMPLETED",
                "remote_identity": str(job.get("remote_identity") or "x"),
                "export_path": "unused",
                "result": {},
                "crawl_results": {
                    int(crawl["id"]): {
                        "page_price_status": "OK",
                        "page_price_value": 12.5,
                        "page_price_source": "fake",
                    }
                    for crawl in job.get("crawl_work", [])
                },
            }
            for job in jobs
        }

    second.deps.harvest = finishing_harvest
    resumed = second.run(runtime_budget_seconds=700, source_stability_seconds=0)

    assert resumed.publication is not None
    for job_id in job_ids:
        assert store.get_remote_job(job_id)["state"] == "COMPLETED"


def test_closeout_finalizes_in_one_pass_and_exits_immediately(tmp_path):
    harness = CloseoutHarness(tmp_path, [_observation([_row("v1", "s1")], datetime(2026, 8, 11, 10, 1, tzinfo=UTC))])
    clock = harness.clock
    result = harness.run(runtime_budget_seconds=2000, source_stability_seconds=0)

    assert result.publication is not None
    assert len(harness.materialize_calls) == 1
    passes = [entry for entry in result.tick.phase_trace if entry.startswith("pass")]
    assert passes == ["pass1:COMPLETED"]
    # blocker=0 materialized the final inside this closeout; the wall budget
    # must not keep the process alive afterwards.
    assert clock.monotonic() < 1100.0


def test_closeout_is_a_noop_after_the_day_is_finalized(tmp_path):
    harness = CloseoutHarness(tmp_path, [_observation([_row("v1", "s1")], datetime(2026, 8, 11, 10, 1, tzinfo=UTC))])
    harness.run(runtime_budget_seconds=2000, source_stability_seconds=0, materialize=materialize_frozen_daily_run)
    paths = runtime_paths(BUSINESS_DATE, harness.root)
    assert DailyAuditStore(paths.state_path).get_run(BUSINESS_DATE)["state"] == "FINALIZED"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an already-finalized day must not touch dependencies")

    harness.deps.discover = forbidden
    harness.deps.harvest = forbidden
    again = harness.run(runtime_budget_seconds=2000)

    assert again.tick.outcome == "ALREADY_FINALIZED"
    assert again.publication is None
    assert harness.discover_times == [1000.0]


def test_safety_closeout_after_finalization_never_rewrites_final_xlsx(tmp_path):
    """Safety Case C: the 20:35 safety closeout is a pure idempotent noop.

    A day already FINALIZED before 20:00 exits through the existing
    ALREADY_FINALIZED semantics before any SaaS request, remote call or
    materialization, so the one-shot late trigger can never rewrite the
    published final.xlsx.
    """
    rows = [_row("v1", "s1")]
    harness = CloseoutHarness(tmp_path, [_observation(rows, datetime(2026, 8, 11, 10, 1, tzinfo=UTC))])
    harness.run(
        runtime_budget_seconds=2000,
        source_stability_seconds=0,
        materialize=materialize_frozen_daily_run,
    )
    paths = runtime_paths(BUSINESS_DATE, harness.root)
    final_path = Path(paths.final_output_path)
    original_bytes = final_path.read_bytes()
    assert DailyAuditStore(paths.state_path).get_run(BUSINESS_DATE)["state"] == "FINALIZED"

    def forbidden_materialize(*_args, **_kwargs):
        raise AssertionError("the safety closeout must not rewrite final.xlsx")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an already-finalized day must not touch dependencies")

    harness.materialize = forbidden_materialize
    harness.deps.discover = forbidden
    harness.deps.harvest = forbidden

    again = harness.run(runtime_budget_seconds=2000)

    assert again.tick.outcome == "ALREADY_FINALIZED"
    assert again.publication is None
    assert final_path.read_bytes() == original_bytes
    assert DailyAuditStore(paths.state_path).get_run(BUSINESS_DATE)["state"] == "FINALIZED"


# ---------------------------------------------------------------------------
# Recovery closeout (§16): a recovery that clears the last blocker finalizes
# in the same command; RUNNING remotes never fake a final.
# ---------------------------------------------------------------------------


def _seed_recoverable_manual_job(tmp_path: Path, *, rows_count: int = 2):
    run_dir = tmp_path / "runtime" / BUSINESS_DATE
    state_path = run_dir / "state.sqlite3"
    run_dir.mkdir(parents=True)
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.write_text("# state\n", encoding="utf-8")
    adapter = CnncLegacyZhaoshangAdapter(
        mappings=[],
        db_env_path=None,
        runner=lambda *_args: None,
    )
    requests = tuple(
        CrawlRequest(
            mall_key="mall", mall_source_id="source", mall_name="Mall",
            sku_id=f"sku-{index}", url=f"https://shop.example/{index}",
            backend_version="legacy-v1",
        )
        for index in range(rows_count)
    )
    prepared = adapter.prepare_job(
        BUSINESS_DATE, requests, run_dir / "input.xlsx", run_dir / "result.xlsx",
        "task-target", batch_state,
    )
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["商品编码", "商品最小品类单元id", "原始链接", "商品价格", "执行编号", "批次号", "商城标识"])
    for index, request in enumerate(requests):
        sheet.append([
            request.sku_id, request.sku_id, request.url, index + 1,
            f"task-number-execution-{index}", prepared.task_name, prepared.mall_key,
        ])
    workbook.save(prepared.result_path)
    events = (
        {"action": "CREATED", "mall": "mall", "task_name": "task-target", "task_id": "task-id", "task_number": "task-number", "expected_subtask_count": rows_count},
        {"action": "ENABLED", "mall": "mall", "task_name": "task-target", "task_id": "task-id", "task_number": "task-number"},
        {"action": "EXPORTED", "mall": "mall", "task_name": "task-target", "task_id": "task-id", "task_number": "task-number", "output_path": str(prepared.result_path)},
    )
    with batch_state.open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(f"- `{json.dumps(event)}`\n")

    store = DailyAuditStore(state_path)
    lock = ProcessLock(run_dir / "incremental_price_audit.lock")
    lock.acquire()
    token = store.acquire_lease(BUSINESS_DATE, "seed", 120, process_lock=lock)
    try:
        rows = []
        for index in range(rows_count):
            row = _row(
                f"v-{index}", f"sku-{index}",
                extLink=f"https://shop.example/{index}",
                __crawl_required=True,
                __crawl_mall_key="mall",
                __crawl_mall_source_id="source",
                __crawl_backend_version="legacy-v1",
            )
            rows.append(row)
        ingested = store.ingest_snapshot(
            token, rows, "2026-08-11T09:50:00+00:00",
            source_fingerprints=[f"source-v-{index}-sku-{index}" for index in range(rows_count)],
            customer_fingerprints=[f"customer-v-{index}" for index in range(rows_count)],
            headers=["vCommodityId", "skuId", "extLink"],
            evidence={"saas_count": rows_count, "customer_count": 0},
        )
        snapshot_id = int(ingested["snapshot"]["id"])
        for item in store.ordered_snapshot_items(snapshot_id):
            detail = store.ensure_stage_work(token, int(item["id"]), "DETAIL", f"detail-{item['id']}")
            store.claim_stage_work(token, int(detail["id"]), max_attempts=1, max_elapsed_seconds=60)
            store.complete_stage_work(token, int(detail["id"]), {"customerPrice": "10"})
            model = store.ensure_stage_work(token, int(item["id"]), "MODEL", f"model-{item['id']}")
            store.claim_stage_work(token, int(model["id"]), max_attempts=1, max_elapsed_seconds=60)
            store.complete_stage_work(token, int(model["id"]), {"match_result": "SAME", "confidence": 1})
        store.freeze_snapshot(token, snapshot_id)
        job = store.prepare_remote_intent(
            token, prepared.logical_job_key, prepared.task_name, prepared.payload_sha256,
            mall_key=prepared.mall_key, input_path=str(prepared.template_path),
            result_path=str(prepared.result_path),
            payload_json={"requests": [request.__dict__ for request in requests], "batch_state": str(batch_state)},
        )
        for index, request in enumerate(requests):
            crawl = store.ensure_crawl_work(
                token, request.mall_key, request.sku_id, request.url, request.backend_version,
                f"crawl-{index}", source_id=f"v-{index}",
            )
            store.link_crawl_work_to_remote_job(token, int(crawl["id"]), int(job["id"]))
        store.claim_remote_submission(token, int(job["id"]))
        store.reconcile_remote_job(
            token, int(job["id"]), remote_identity="mall|task-id|task-number", state="SUBMITTED",
        )
        store.mark_remote_manual(token, int(job["id"]), "export validation failed")
        job_id = int(job["id"])
    finally:
        store.release_lease(token)
        lock.release()
    return state_path, run_dir, job_id, adapter


def test_remote_harvest_recovery_finalizes_the_day_in_the_same_command(tmp_path, monkeypatch):
    state_path, run_dir, job_id, adapter = _seed_recoverable_manual_job(tmp_path)
    monkeypatch.setattr(
        runtime_module, "CnncLegacyZhaoshangAdapter", lambda: adapter
    )

    result = run_remote_harvest_recovery_command(
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path / "runtime",
        remote_job_id=job_id,
        remote_identity="mall|task-id|task-number",
    )

    assert result["outcome"] == "REMOTE_HARVEST_RECOVERY_COMPLETED"
    assert result["closeout"]["finalized"] is True
    store = DailyAuditStore(state_path)
    assert store.get_run(BUSINESS_DATE)["state"] == "FINALIZED"
    assert (run_dir / "saas_audit_final_20260811.xlsx").is_file()


def test_recovery_closeout_reports_not_ready_while_remote_still_running(tmp_path, monkeypatch):
    state_path, run_dir, job_id, _adapter = _seed_recoverable_manual_job(tmp_path)

    def running_harvest(**_kwargs):
        return {
            "outcome": "REMOTE_EXISTING_HARVEST_RUNNING",
            "business_date": BUSINESS_DATE,
            "remote_job_id": job_id,
            "remote_identity": "mall|task-id|task-number",
            "remote_calls": 1,
            "add_calls": 0,
        }

    monkeypatch.setattr(runtime_module, "run_existing_remote_harvest", running_harvest)
    result = runtime_module.run_existing_remote_harvest_command(
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path / "runtime",
        remote_job_id=job_id,
        remote_identity="mall|task-id|task-number",
    )

    assert result["outcome"] == "REMOTE_EXISTING_HARVEST_RUNNING"
    assert result["closeout"]["finalized"] is False
    assert result["closeout"]["reason"] == "NOT_READY"
    store = DailyAuditStore(state_path)
    assert store.get_run(BUSINESS_DATE)["state"] != "FINALIZED"
    assert not (run_dir / "saas_audit_final_20260811.xlsx").exists()


def test_priority_recovery_targets_one_job_and_never_reposts(tmp_path, monkeypatch):
    state_path, _run_dir, job_id, _adapter = _seed_recoverable_manual_job(tmp_path)
    targeted_calls: list[dict[str, Any]] = []

    def priority_recovered(**_kwargs):
        return {
            "outcome": "REMOTE_PRIORITY_RECOVERY_COMPLETED",
            "business_date": BUSINESS_DATE,
            "remote_job_id": job_id,
            "remote_identity": "mall|task-id|task-number",
            "add_calls": 0,
        }

    def targeted_harvest(**kwargs):
        targeted_calls.append(kwargs)
        return {
            "outcome": "REMOTE_EXISTING_HARVEST_RUNNING",
            "business_date": kwargs["business_date"],
            "remote_job_id": kwargs["remote_job_id"],
            "remote_identity": kwargs["remote_identity"],
            "remote_calls": 1,
            "add_calls": 0,
        }

    monkeypatch.setattr(runtime_module, "run_existing_remote_priority_recovery", priority_recovered)
    monkeypatch.setattr(runtime_module, "run_existing_remote_harvest", targeted_harvest)
    result = runtime_module.run_remote_priority_recovery_command(
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path / "runtime",
        remote_job_id=job_id,
        remote_identity="mall|task-id|task-number",
    )

    assert result["outcome"] == "REMOTE_PRIORITY_RECOVERY_COMPLETED"
    # Exactly one bounded targeted harvest of exactly the recovered job; no
    # full sweep and no second /add.
    assert len(targeted_calls) == 1
    assert targeted_calls[0]["remote_job_id"] == job_id
    assert result["targeted_harvest"]["add_calls"] == 0
    assert result["closeout"]["finalized"] is False
    assert DailyAuditStore(state_path).get_run(BUSINESS_DATE)["state"] != "FINALIZED"


def test_status_stays_read_only_after_recovery_closeout(tmp_path, monkeypatch):
    state_path, _run_dir, job_id, adapter = _seed_recoverable_manual_job(tmp_path)
    monkeypatch.setattr(
        runtime_module, "CnncLegacyZhaoshangAdapter", lambda: adapter
    )
    finalized = run_remote_harvest_recovery_command(
        business_date=BUSINESS_DATE,
        runtime_root=tmp_path / "runtime",
        remote_job_id=job_id,
        remote_identity="mall|task-id|task-number",
    )
    assert finalized["closeout"]["finalized"] is True

    before = state_path.read_bytes()
    report = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=tmp_path / "runtime")

    assert report["state"] == "FINALIZED"
    assert report["final"]["finalized"] is True
    assert state_path.read_bytes() == before
