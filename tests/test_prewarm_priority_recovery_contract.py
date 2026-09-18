"""Focused safety contract for cross-day priority recovery.

The runtime, state DB, and remote adapter are all test-local fixtures.
"""

from __future__ import annotations

import sqlite3

from app.services.customer_prewarm_cross_day_reconcile import (
    run_cross_day_reconcile_phase,
)
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.legacy_zhaoshang_adapter import (
    LegacyPriorityOutcome,
    PriorityStatus,
)
from tests.test_customer_prewarm_lifecycle import (
    COMIX,
    RECONCILE_FOUND,
    TODAY,
    YESTERDAY,
    _CountingAdapter,
    _new_summary,
    _remote_job_row,
    seed_remote_candidate,
    seed_unknown_job,
)


def test_found_unconfirmed_then_next_pass_proven_without_repeat_reconcile(tmp_path):
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    paths, job_id = seed_unknown_job(
        runtime_root,
        YESTERDAY,
        _CountingAdapter(),
        mapping=COMIX,
        url="https://qx.com/standalone-priority-contract.html",
    )
    seed_remote_candidate(
        prewarm,
        "https://qx.com/standalone-priority-contract.html",
        job_id,
        business_date=YESTERDAY,
    )
    adapter = _CountingAdapter(
        reconcile_outcome=RECONCILE_FOUND,
        priority_outcomes=[
            LegacyPriorityOutcome(PriorityStatus.UNCONFIRMED, weight_type=None),
            LegacyPriorityOutcome(PriorityStatus.ALREADY_HIGHEST, weight_type=-1),
        ],
    )

    def pass_once():
        summary = _new_summary()
        run_cross_day_reconcile_phase(
            prewarm_store=prewarm,
            current_business_date=TODAY,
            state_runtime_root=runtime_root,
            get_adapter=lambda: adapter,
            deadline=10_000.0,
            monotonic_value=lambda: 1.0,
            summary=summary,
        )
        return summary

    first = pass_once()
    assert first["cross_day_reconcile_planned"] == 1
    assert _remote_job_row(paths.state_path, job_id)["state"] == "PRIORITY_PENDING"
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 1

    second = pass_once()
    assert second["cross_day_reconcile_planned"] == 1
    assert _remote_job_row(paths.state_path, job_id)["state"] == "SUBMITTED"
    with sqlite3.connect(paths.state_path) as connection:
        states = {
            row[0]
            for row in connection.execute(
                "SELECT state FROM crawl_work WHERE remote_job_id=?", (job_id,)
            )
        }
    assert states == {"SUBMITTED"}
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 2
    assert adapter.submit_calls == 0

    third = pass_once()
    assert third["cross_day_reconcile_planned"] == 0
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 2
    assert adapter.submit_calls == 0
