"""Round-3 final-state consistency contracts for the status projection.

The status layer must agree with the tick/finalize view of "does a customer
price block the final sheet": SUCCESS-stage business blockers (INVALID /
MISSING_PROVEN / legacy FAILED) count into ``computed_pending`` exactly once,
TERMINAL-stage blockers stay single-counted through ``detail.terminal``, and
the detail statistics follow the frozen-authoritative snapshot.
"""

from __future__ import annotations

from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_runtime import runtime_paths
from app.services.incremental_audit_status import collect_runtime_status

BUSINESS_DATE = "2026-08-11"

FOUND_PAYLOAD = {
    "customerPrice": "10",
    "customerPriceStatus": "FOUND",
    "customerPriceSource": "DETAIL_REFERENCE_INFOS",
    "customerPriceWarning": "",
    "customerPriceEvidence": "参考价1=10",
}

ABSENT_SKIP_PAYLOAD = {
    "customerPrice": None,
    "customerPriceStatus": "LOOKUP_FAILED",
    "customerPriceSource": "",
    "customerPriceWarning": "customer export did not contain this documentId",
    "customerPriceEvidence": "authoritative customer snapshot round complete; documentId absent",
}

CONTRADICTORY_PRICE_PAYLOAD = {
    "customerPrice": "100",
    "customerPriceStatus": "NOT_FOUND",
    "customerPriceSource": "",
    "customerPriceWarning": "详情接口未返回有效参考价",
    "customerPriceEvidence": "",
}

MISSING_TERMINAL_ERROR = "customerPriceStatus=NOT_FOUND；详情接口未返回有效参考价"


def _row(v="doc-1", sku="sku-1"):
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": "",
        "fourCategoryId": "cat",
        "list_reference_price": "10",
        "queryStr": "title",
        "skuName": "title",
        "mall_key": "mall-a",
    }


def _store(root) -> DailyAuditStore:
    return DailyAuditStore(runtime_paths(BUSINESS_DATE, root).state_path)


def _seed_snapshot(store: DailyAuditStore, rows, observed_at: str) -> dict:
    token = store.acquire_lease(BUSINESS_DATE, "seed-snapshot", 60)
    try:
        return store.ingest_snapshot(
            token, rows, observed_at,
            source_fingerprints=[f"source-{row['vCommodityId']}" for row in rows],
            customer_fingerprints=[f"customer-{row['fourCategoryId']}" for row in rows],
            headers=["vCommodityId"],
            evidence={"saas_count": len(rows), "customer_count": len(rows)},
        )["snapshot"]
    finally:
        store.release_lease(token)


def _item_id(store: DailyAuditStore, snapshot_id: int, v: str) -> int:
    for item in store.ordered_snapshot_items(snapshot_id):
        if item["row"]["vCommodityId"] == v:
            return int(item["id"])
    raise AssertionError(f"no snapshot item for {v}")


def _complete_detail(store: DailyAuditStore, item_id: int, payload: dict) -> None:
    token = store.acquire_lease(BUSINESS_DATE, "seed-detail", 60)
    try:
        work = store.ensure_stage_work(token, item_id, "DETAIL", f"manual-{item_id}")
        claimed = store.claim_stage_work(token, work["id"], max_attempts=3, max_elapsed_seconds=300)
        assert claimed is not None
        store.complete_stage_work(token, work["id"], dict(payload))
    finally:
        store.release_lease(token)


def _terminal_detail(store: DailyAuditStore, item_id: int, error: str) -> None:
    token = store.acquire_lease(BUSINESS_DATE, "seed-detail", 60)
    try:
        work = store.ensure_stage_work(token, item_id, "DETAIL", f"manual-{item_id}")
        claimed = store.claim_stage_work(token, work["id"], max_attempts=1, max_elapsed_seconds=300)
        assert claimed is not None
        store.fail_stage_work(
            token, work["id"], elapsed_seconds=0.1, error=error, retryable=False,
            max_attempts=1, max_elapsed_seconds=300,
        )
    finally:
        store.release_lease(token)


def _complete_model(store: DailyAuditStore, item_id: int) -> None:
    token = store.acquire_lease(BUSINESS_DATE, "seed-model", 60)
    try:
        work = store.ensure_stage_work(token, item_id, "MODEL", f"manual-model-{item_id}")
        claimed = store.claim_stage_work(token, work["id"], max_attempts=3, max_elapsed_seconds=300)
        assert claimed is not None
        store.complete_stage_work(token, work["id"], {"match_result": "SAME"})
    finally:
        store.release_lease(token)


def test_success_customer_absent_skip_counts_as_nonblocking_skip(tmp_path):
    """2026-09-11 rule change: a SUCCESS LOOKUP_FAILED row (legacy spelling of
    the deterministic documentId absence) projects into customer_absent_skipped
    and never into the blocker views."""
    root = tmp_path / "runtime"
    store = _store(root)
    store.ensure_run(BUSINESS_DATE)
    snapshot = _seed_snapshot(store, [_row()], "2026-08-11T09:00:00+00:00")
    item = _item_id(store, snapshot["id"], "doc-1")
    _complete_detail(store, item, ABSENT_SKIP_PAYLOAD)
    _complete_model(store, item)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["detail"]["terminal"] == 0
    assert status["detail"]["customer_absent_skipped"] == 1
    assert status["detail"]["customer_price_absent_proven"] == 0
    assert status["detail"]["customer_price_success_blockers"] == 0
    assert status["final"]["blocker_summary"]["customer_absent_skipped"] == 1
    assert status["final"]["blocker_summary"]["customer_price_absent_proven"] == 0
    assert status["final"]["computed_pending"] == 0


def test_success_proven_absent_counts_into_computed_pending(tmp_path):
    """MISSING_PROVEN (no detail identity / price not provided) still blocks:
    it stays in absent_proven, success_blockers and computed_pending."""
    root = tmp_path / "runtime"
    store = _store(root)
    store.ensure_run(BUSINESS_DATE)
    snapshot = _seed_snapshot(store, [_row()], "2026-08-11T09:00:00+00:00")
    item = _item_id(store, snapshot["id"], "doc-1")
    _complete_detail(store, item, {**ABSENT_SKIP_PAYLOAD, "customerPriceStatus": "DETAIL_REQUEST_SKIPPED"})
    _complete_model(store, item)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["detail"]["terminal"] == 0
    assert status["detail"]["customer_price_absent_proven"] == 1
    assert status["detail"]["customer_absent_skipped"] == 0
    assert status["detail"]["customer_price_success_blockers"] == 1
    assert status["final"]["blocker_summary"]["customer_price_absent_proven"] == 1
    assert status["final"]["computed_pending"] == 1


def test_success_contradictory_price_counts_into_computed_pending(tmp_path):
    root = tmp_path / "runtime"
    store = _store(root)
    store.ensure_run(BUSINESS_DATE)
    snapshot = _seed_snapshot(store, [_row()], "2026-08-11T09:00:00+00:00")
    item = _item_id(store, snapshot["id"], "doc-1")
    _complete_detail(store, item, CONTRADICTORY_PRICE_PAYLOAD)
    _complete_model(store, item)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["detail"]["customer_price_invalid"] == 1
    assert status["detail"]["customer_price_success_blockers"] == 1
    assert status["final"]["blocker_summary"]["customer_price_invalid"] == 1
    assert status["final"]["computed_pending"] == 1


def test_terminal_missing_row_is_single_counted_not_double(tmp_path):
    """One TERMINAL(NOT_FOUND exhausted) row: detail.terminal and
    customer_price_missing both see it, but computed_pending counts it once."""
    root = tmp_path / "runtime"
    store = _store(root)
    store.ensure_run(BUSINESS_DATE)
    snapshot = _seed_snapshot(store, [_row()], "2026-08-11T09:00:00+00:00")
    item = _item_id(store, snapshot["id"], "doc-1")
    _terminal_detail(store, item, MISSING_TERMINAL_ERROR)
    _complete_model(store, item)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["detail"]["terminal"] == 1
    assert status["detail"]["customer_price_missing"] == 1
    assert status["detail"]["customer_price_success_blockers"] == 0
    assert status["final"]["computed_pending"] == 1


def test_terminal_plus_success_invalid_blockers_sum_without_overlap(tmp_path):
    root = tmp_path / "runtime"
    store = _store(root)
    store.ensure_run(BUSINESS_DATE)
    snapshot = _seed_snapshot(store, [_row("doc-1", "sku-1"), _row("doc-2", "sku-2")], "2026-08-11T09:00:00+00:00")
    terminal_item = _item_id(store, snapshot["id"], "doc-1")
    invalid_item = _item_id(store, snapshot["id"], "doc-2")
    _terminal_detail(store, terminal_item, MISSING_TERMINAL_ERROR)
    _complete_detail(store, invalid_item, CONTRADICTORY_PRICE_PAYLOAD)
    _complete_model(store, terminal_item)
    _complete_model(store, invalid_item)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["detail"]["terminal"] == 1
    assert status["detail"]["customer_price_missing"] == 1
    assert status["detail"]["customer_price_invalid"] == 1
    assert status["detail"]["customer_price_success_blockers"] == 1
    assert status["final"]["computed_pending"] == 2


def test_empty_latest_snapshot_is_not_polluted_by_dropped_history(tmp_path):
    """Snapshot A held one terminal detail row; the latest snapshot B has zero
    rows and is authoritative (not frozen): history must not leak into any
    customer-price bucket or computed_pending."""
    root = tmp_path / "runtime"
    store = _store(root)
    store.ensure_run(BUSINESS_DATE)
    snapshot_a = _seed_snapshot(store, [_row("doc-a", "sku-a")], "2026-08-11T09:00:00+00:00")
    _terminal_detail(store, _item_id(store, snapshot_a["id"], "doc-a"), MISSING_TERMINAL_ERROR)
    _seed_snapshot(store, [], "2026-08-11T09:30:00+00:00")

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["counts"]["unique_items"] == 0
    assert status["detail"]["customer_price_required"] == 0
    for key in (
        "customer_price_missing",
        "customer_price_failed",
        "customer_price_invalid",
        "customer_price_absent_proven",
        "customer_absent_skipped",
        "customer_price_success_blockers",
    ):
        assert status["detail"][key] == 0
    assert status["detail"]["terminal"] == 0
    assert status["final"]["computed_pending"] == 0


def test_detail_statistics_follow_frozen_snapshot_not_latest(tmp_path):
    """After freeze(A), the detail/customer-price statistics describe the
    frozen authoritative snapshot A even when a later empty snapshot B is the
    latest observation — finalize materializes A, so A's blockers count."""
    root = tmp_path / "runtime"
    store = _store(root)
    store.ensure_run(BUSINESS_DATE)
    snapshot_a = _seed_snapshot(store, [_row("doc-a", "sku-a")], "2026-08-11T09:00:00+00:00")
    frozen_item = _item_id(store, snapshot_a["id"], "doc-a")
    _terminal_detail(store, frozen_item, MISSING_TERMINAL_ERROR)
    _complete_model(store, frozen_item)
    _seed_snapshot(store, [], "2026-08-11T09:30:00+00:00")
    token = store.acquire_lease(BUSINESS_DATE, "freeze-a", 60)
    try:
        store.freeze_snapshot(token, int(snapshot_a["id"]))
    finally:
        store.release_lease(token)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["frozen_snapshot_id"] == int(snapshot_a["id"])
    assert status["counts"]["unique_items"] == 1
    assert status["detail"]["terminal"] == 1
    assert status["detail"]["customer_price_missing"] == 1
    # Frozen A still blocks exactly once; the empty latest snapshot must not
    # dilute the frozen-authoritative view to zero.
    assert status["final"]["computed_pending"] == 1


def test_final_status_counts_missing_page_price_by_frozen_business_row(tmp_path):
    root = tmp_path / "runtime"
    store = _store(root)
    store.ensure_run(BUSINESS_DATE)
    rows = []
    for index in range(4):
        row = _row(f"doc-{index}", f"sku-{index}")
        row["extLink"] = f"https://shop.example/{index}"
        row["__crawl_required"] = True
        rows.append(row)
    snapshot = _seed_snapshot(store, rows, "2026-08-11T09:00:00+00:00")
    token = store.acquire_lease(BUSINESS_DATE, "seed-crawl", 60)
    store.freeze_snapshot(token, int(snapshot["id"]))
    crawls = []
    for row in rows:
        crawls.append(store.ensure_crawl_work(
            token,
            "mall-a",
            row["skuId"],
            row["extLink"],
            "legacy-v1",
            f"crawl-{row['skuId']}",
            source_id=row["vCommodityId"],
        ))
    store.complete_crawl_work(token, crawls[0]["id"], {
        "page_price_value": "9.5", "page_price_status": "OK", "page_price_source": "招商局",
    })
    store.complete_crawl_work(token, crawls[1]["id"], {
        "page_price_value": "0", "page_price_status": "PRICE_IS_ZERO", "page_price_source": "招商局",
    })
    store.release_lease(token)

    status = collect_runtime_status(business_date=BUSINESS_DATE, runtime_root=root)

    assert status["final"]["page_price_required_rows"] == 4
    assert status["final"]["missing_page_price_rows"] == 2
    assert status["final"]["force_closeout_limit"] == 20
