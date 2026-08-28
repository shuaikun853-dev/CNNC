"""PHASE D: read-only source-completeness diagnostics for the daily snapshot.

Three contracts are pinned here:

1. Discovery evidence records the SaaS->customer join status counts
   (MATCHED / MATCHED_NO_CATEGORY / NOT_FOUND) plus the raw ``saas_rows``
   length, next to the existing ``saas_count`` / ``customer_count``.
2. The freeze block persists those counts into the daily metrics counters
   WITHOUT changing the freeze decision: ``customer_count == saas_count`` is
   explicitly NOT a business invariant (the join allows NOT_FOUND), and no
   customer metric may ever block or gate the freeze.  The F-P1-3 hard gate
   stays DEFERRED — the negative assertions below pin that absence.
3. ``collect_runtime_status`` exposes a pure-diagnostic ``source_completeness``
   section derived from the persisted metrics; it never feeds overall state.

No test here may introduce, imply, or require any freeze threshold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_runtime import runtime_paths, snapshot_discoverer
from app.services.incremental_audit_status import collect_runtime_status
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
)


BUSINESS_DATE = "2026-08-11"


@dataclass
class _Clock:
    value: float = 0.0

    def monotonic(self) -> float:
        return self.value


def _row(v: str = "doc-1", sku: str = "sku-1") -> dict:
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": " https://shop.example/item ",
        "fourCategoryId": "cat",
        "list_reference_price": "10",
        "queryStr": "title",
        "skuName": "title",
        "mall_key": "mall-a",
    }


# ---------------------------------------------------------------------------
# 1. Discovery evidence join-status counts
# ---------------------------------------------------------------------------


def test_snapshot_discoverer_evidence_counts_join_statuses():
    class Snapshot:
        observed_at_utc = datetime(2026, 8, 11, 10, 1, tzinfo=UTC)
        saas_rows = (
            {"vCommodityId": "full", "skuId": "s1", "extLink": ""},
            {"vCommodityId": "no-cat", "skuId": "s2", "extLink": ""},
            {"vCommodityId": "missing-a", "skuId": "s3", "extLink": ""},
            {"vCommodityId": "missing-b", "skuId": "s4", "extLink": ""},
        )
        customer_rows = (
            {"documentId": "full", "fourCategoryId": "c1", "referencePrice": "10"},
            # A matched customer row without a category is a distinct status.
            {"documentId": "no-cat", "fourCategoryId": "", "referencePrice": None},
        )
        saas_count = 4
        customer_count = 2
        page_evidence = ()
        source_label = "fake"

    class Adapter:
        def fetch(self, **_kwargs):
            return Snapshot()

    state = snapshot_discoverer(Adapter())(10)

    evidence = state.evidence
    assert evidence["saas_count"] == 4
    assert evidence["customer_count"] == 2
    assert evidence["saas_rows"] == 4
    assert evidence["customer_matched"] == 1
    assert evidence["customer_matched_no_category"] == 1
    assert evidence["customer_not_found"] == 2
    # The joined rows themselves keep the authoritative per-row status.
    statuses = sorted(row["customer_lookup_status"] for row in state.rows)
    assert statuses == ["MATCHED", "MATCHED_NO_CATEGORY", "NOT_FOUND", "NOT_FOUND"]


def test_snapshot_discoverer_evidence_gap_counts_stay_consistent():
    """A total miss (every SaaS row NOT_FOUND) is recorded, not rejected."""

    class Snapshot:
        observed_at_utc = datetime(2026, 8, 11, 10, 1, tzinfo=UTC)
        saas_rows = (
            {"vCommodityId": "a", "skuId": "s1", "extLink": ""},
            {"vCommodityId": "b", "skuId": "s2", "extLink": ""},
        )
        customer_rows = ()
        saas_count = 2
        customer_count = 0
        page_evidence = ()
        source_label = "fake"

    class Adapter:
        def fetch(self, **_kwargs):
            return Snapshot()

    evidence = snapshot_discoverer(Adapter())(10).evidence
    assert evidence["customer_matched"] == 0
    assert evidence["customer_matched_no_category"] == 0
    assert evidence["customer_not_found"] == 2


# ---------------------------------------------------------------------------
# 2. Freeze path: counters record the gap; the freeze gate is unchanged
# ---------------------------------------------------------------------------


def _tick_deps(rows: list[dict], clock: _Clock, *, evidence: dict) -> TickDependencies:
    def harvest(jobs, _timeout):
        return {}

    def discover(_timeout):
        return DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=rows,
            source_fingerprints=[f"source-{row['vCommodityId']}-{row['skuId']}" for row in rows],
            customer_fingerprints=[f"customer-{row['fourCategoryId']}" for row in rows],
            evidence=evidence,
        )

    def detail(row, _timeout):
        return {"customerPrice": "10"}

    def model(row, _timeout):
        return {"match_result": "SAME"}

    return TickDependencies(
        discover=discover,
        harvest=harvest,
        detail=detail,
        model=model,
        plan_crawl=None,
        submit_crawl=None,
        finalize_crawl_plan=None,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
    )


def _config(clock: _Clock) -> TickConfig:
    return TickConfig(
        business_date=BUSINESS_DATE,
        runtime_budget_seconds=120,
        heavy_work_reserve_seconds=10,
        lease_ttl_seconds=90,
        cutoff_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
        source_stability_seconds=0.0,
    )


def test_freeze_proceeds_with_join_gap_and_records_gap_counters(tmp_path):
    """Negative assertion: NO customer metric may block the freeze.

    The snapshot is SaaS-complete (row_count == saas_count) while the join
    has a 90% NOT_FOUND rate and customer_count < saas_count; the freeze
    must still happen and the gap must only be recorded in counters.
    """
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    clock = _Clock()
    rows = [_row(f"doc-{index}", f"sku-{index}") for index in range(10)]
    evidence = {
        "saas_count": 10,
        "customer_count": 1,
        "saas_rows": 10,
        "customer_matched": 1,
        "customer_matched_no_category": 0,
        "customer_not_found": 9,
    }
    deps = _tick_deps(rows, clock, evidence=evidence)

    summary = run_incremental_tick(
        store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps, finalize=True
    )

    # The freeze gate is exactly the pre-existing SaaS-completeness rule; the
    # customer join gap neither blocks nor delays it.
    assert summary.frozen_snapshot_id is not None
    assert summary.outcome != "WAITING_SOURCE_COUNT"
    # The gap is recorded, never acted on.
    assert summary.counters["saas_count"] == 10
    assert summary.counters["customer_count"] == 1
    assert summary.counters["source_count_gap"] == 1 - 10
    assert summary.counters["customer_matched"] == 1
    assert summary.counters["customer_matched_no_category"] == 0
    assert summary.counters["customer_not_found"] == 9


def test_freeze_gate_ignores_customer_metrics_entirely(tmp_path):
    """A join gap CANNOT keep a SaaS-complete snapshot unfrozen.

    Contrast case: when row_count != saas_count the freeze waits
    (WAITING_SOURCE_COUNT) for a reason that has nothing to do with the
    customer join — proving the only gate is the source-count rule.
    """
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    clock = _Clock()
    rows = [_row(f"doc-{index}", f"sku-{index}") for index in range(9)]
    evidence = {
        "saas_count": 10,  # SaaS source still moving: row_count(9) != saas_count(10)
        "customer_count": 10,
        "customer_matched": 9,
        "customer_matched_no_category": 1,
        "customer_not_found": 0,
    }
    deps = _tick_deps(rows, clock, evidence=evidence)

    summary = run_incremental_tick(
        store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps, finalize=True
    )

    assert summary.outcome == "WAITING_SOURCE_COUNT"
    assert summary.frozen_snapshot_id is None


def test_freeze_counters_default_to_zero_without_new_evidence_fields(tmp_path):
    """Legacy evidence (pre-PHASE-D) still freezes; missing counters stay 0."""
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    clock = _Clock()
    rows = [_row(f"doc-{index}", f"sku-{index}") for index in range(3)]
    evidence = {"saas_count": 3, "customer_count": 3}
    deps = _tick_deps(rows, clock, evidence=evidence)

    summary = run_incremental_tick(
        store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps, finalize=True
    )

    assert summary.frozen_snapshot_id is not None
    assert summary.counters["customer_matched"] == 0
    assert summary.counters["customer_matched_no_category"] == 0
    assert summary.counters["customer_not_found"] == 0


# ---------------------------------------------------------------------------
# 3. collect_runtime_status: read-only source_completeness section
# ---------------------------------------------------------------------------


def test_source_completeness_reports_metrics_and_percentage_rates(tmp_path):
    root = tmp_path / "runtime"
    paths = runtime_paths(BUSINESS_DATE, root)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(BUSINESS_DATE, cutoff_at="2026-08-11T10:00:00+00:00")
    token = store.acquire_lease(BUSINESS_DATE, "seed-metrics", 30)
    store.update_daily_metrics(
        token,
        {
            "saas_count": 10,
            "customer_count": 4,
            "source_count_gap": -6,
            "customer_matched": 2,
            "customer_matched_no_category": 1,
            "customer_not_found": 7,
        },
    )
    store.release_lease(token)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["source_completeness"] == {
        "saas_count": 10,
        "customer_count": 4,
        "source_count_gap": -6,
        "customer_matched": 2,
        "customer_matched_no_category": 1,
        "customer_not_found": 7,
        "customer_match_rate": 20.0,
        "customer_not_found_rate": 70.0,
    }


def test_source_completeness_defaults_to_zero_counts_and_none_rates(tmp_path):
    """A run without metrics keeps a stable all-diagnostic shape."""
    root = tmp_path / "runtime"
    paths = runtime_paths(BUSINESS_DATE, root)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(BUSINESS_DATE, cutoff_at="2026-08-11T10:00:00+00:00")

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["source_completeness"] == {
        "saas_count": 0,
        "customer_count": 0,
        "source_count_gap": 0,
        "customer_matched": 0,
        "customer_matched_no_category": 0,
        "customer_not_found": 0,
        "customer_match_rate": None,
        "customer_not_found_rate": None,
    }


def test_source_completeness_missing_state_file_keeps_stable_shape(tmp_path):
    status = collect_runtime_status(
        business_date=BUSINESS_DATE, runtime_root=tmp_path / "runtime"
    )
    assert status["state"] == "MISSING"
    assert status["source_completeness"]["customer_match_rate"] is None
    assert status["source_completeness"]["customer_not_found_rate"] is None
    assert status["source_completeness"]["saas_count"] == 0


def test_end_to_end_freeze_metrics_surface_in_status_source_completeness(tmp_path):
    """The full chain: discovery evidence -> freeze counters -> metrics -> status."""
    root = tmp_path / "runtime"
    store = DailyAuditStore(runtime_paths(BUSINESS_DATE, root).state_path)
    clock = _Clock()
    rows = [_row(f"doc-{index}", f"sku-{index}") for index in range(10)]
    evidence = {
        "saas_count": 10,
        "customer_count": 2,
        "saas_rows": 10,
        "customer_matched": 1,
        "customer_matched_no_category": 1,
        "customer_not_found": 8,
    }
    deps = _tick_deps(rows, clock, evidence=evidence)

    summary = run_incremental_tick(
        store, ProcessLock(tmp_path / "tick.lock"), _config(clock), deps, finalize=True
    )
    assert summary.frozen_snapshot_id is not None

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)
    completeness = status["source_completeness"]
    assert completeness["saas_count"] == 10
    assert completeness["customer_count"] == 2
    assert completeness["customer_matched"] == 1
    assert completeness["customer_matched_no_category"] == 1
    assert completeness["customer_not_found"] == 8
    assert completeness["customer_match_rate"] == 10.0
    assert completeness["customer_not_found_rate"] == 80.0
    # Pure diagnosis: the section never changes the overall status shape.
    assert status["state"] in {"OPEN", "FINALIZING", 'FINALIZED'}
