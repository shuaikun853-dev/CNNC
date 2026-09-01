"""Cached degraded finalization core: ten-gate evaluator plus guarded runner.

The module under test must decide purely from durable SQLite whether the
day's last-known-good authoritative snapshot can be frozen and materialized
with zero network I/O (2026-08-31 incident: business all green, three
identical snapshots stable for 10797s, authoritative auth unavailable).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.daily_audit_store import DailyAuditStore, LeaseUnavailable, ProcessLock
from app.services.incremental_audit_cached_finalize import (
    CACHED_FINALIZATION_MODE,
    STRICT_FINALIZATION_MODE,
    CachedFinalizeBlocked,
    evaluate_cached_finalization_readiness,
    run_cached_finalization,
)
from app.services.incremental_audit_materializer import LegacyWorkbookMaterializer

BUSINESS_DATE = "2026-08-31"
CUTOFF = datetime(2026, 8, 31, 10, 0, 0, tzinfo=UTC)
AFTER_CUTOFF = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
INCIDENT_OBSERVED_ATS = ("04:00:08", "05:00:05", "07:00:05")
INCIDENT_STABILITY_SECONDS = 10797.0


def _row(index: int, *, url: str | None = None) -> dict[str, object]:
    return {
        "vCommodityId": f"v-{index}",
        "documentId": f"v-{index}",
        "skuId": f"sku-{index}",
        "extLink": url if url is not None else f"https://shop.example/item/{index}",
        "queryStr": "客户标题",
        "sassVCommodityName": "客户标题",
        "skuName": "互联网标题",
        "vendorName": "商城",
        "shopName": "店铺",
        "price": "10",
        "fourCategoryId": "cat-1",
        "fourCategoryName": "分类",
        "list_reference_price": "10",
        "customer_lookup_status": "MATCHED",
        "__crawl_required": True,
    }


def _complete_detail(store: DailyAuditStore, token, work_id: int) -> None:
    store.complete_stage_work(token, work_id, {
        "customerPrice": "10",
        "customerPriceStatus": "FOUND",
        "customerPriceSource": "DETAIL_REFERENCE_INFOS",
        "customerPriceWarning": "",
        "customerPriceEvidence": "customer=10",
    })


def _complete_model(store: DailyAuditStore, token, work_id: int) -> None:
    store.complete_stage_work(token, work_id, {
        "match_result": "SAME", "confidence": 0.95, "model_match": "exact", "reason": "一致",
    })


def _seed_full_good_day(
    store_path: Path,
    business_date: str,
    *,
    observed_times: tuple[str, ...] = INCIDENT_OBSERVED_ATS,
    rows: list[dict[str, object]] | None = None,
    observed_rows: tuple[list[dict[str, object]], ...] | None = None,
    model_pending_indexes: set[int] = frozenset(),
    model_terminal_indexes: set[int] = frozenset(),
    crawl_pending_indexes: set[int] = frozenset(),
    extra_remote_state: str | None = None,
) -> tuple[DailyAuditStore, list[dict[str, object]]]:
    """Seed one fully green day: identical snapshots, stages SUCCESS,
    crawl SUCCESS bound to COMPLETED remote jobs (4 jobs for 9 rows)."""
    rows = list(rows if rows is not None else [_row(i) for i in range(1, 10)])
    if observed_rows is not None:
        assert len(observed_rows) == len(observed_times)
        per_observation = [list(items) for items in observed_rows]
    else:
        per_observation = [list(rows) for _ in observed_times]
    store = DailyAuditStore(store_path)
    store.ensure_run(business_date, cutoff_at=f"{business_date}T10:00:00+00:00")
    token = store.acquire_lease(business_date, "seed-cached", 600)
    headers = [name for name in rows[0] if not name.startswith("__")]
    evidence = {"observed_count": len(rows), "saas_count": len(rows), "customer_count": len(rows)}
    snapshots = []
    for moment, observation_rows in zip(observed_times, per_observation, strict=True):
        ingested = store.ingest_snapshot(
            token,
            observation_rows,
            f"{business_date}T{moment}+00:00",
            headers=headers,
            evidence=dict(evidence),
        )
        snapshots.append(ingested["snapshot"])

    items = store.ordered_snapshot_items(int(snapshots[-1]["id"]))
    for index, item in enumerate(items):
        detail = store.ensure_stage_work(token, item["id"], "DETAIL", f"detail-{item['id']}")
        store.claim_stage_work(token, detail["id"], max_attempts=3, max_elapsed_seconds=300)
        _complete_detail(store, token, int(detail["id"]))
        model = store.ensure_stage_work(token, item["id"], "MODEL", f"model-{item['id']}")
        if index in model_pending_indexes:
            continue  # exists, stays PENDING
        store.claim_stage_work(token, model["id"], max_attempts=1, max_elapsed_seconds=300)
        if index in model_terminal_indexes:
            store.fail_stage_work(
                token, int(model["id"]),
                elapsed_seconds=1.0, error="model exploded", retryable=False,
                max_attempts=1, max_elapsed_seconds=300,
            )
            continue
        _complete_model(store, token, int(model["id"]))

    jobs = []
    for job_index in range(4):
        job = store.prepare_remote_intent(
            token,
            f"mall:key-{job_index}",
            f"daily-{business_date.replace('-', '')}-mall-{job_index}",
            f"payload-{job_index}",
            result_path=str(store_path.parent / f"mall-result-{job_index}.xlsx"),
        )
        store.claim_remote_submission(token, int(job["id"]))
        store.reconcile_remote_job(
            token,
            int(job["id"]),
            remote_identity=f"mall|task-{job_index}|num-{job_index}",
            state="COMPLETED",
            result={"page": "ok"},
        )
        jobs.append(int(job["id"]))

    for index, item in enumerate(items):
        row = rows[index]
        crawl = store.ensure_crawl_work(
            token, "mall", str(row["skuId"]), str(row["extLink"]), "legacy-v1",
            f"crawl-fp-{item['id']}", source_id=str(row["vCommodityId"]),
        )
        store.link_crawl_work_to_remote_job(token, int(crawl["id"]), jobs[index * 4 // len(items)])
        if index in crawl_pending_indexes:
            continue  # stays PENDING
        store.complete_crawl_work(token, int(crawl["id"]), {
            "page_price_value": "9.5", "page_price_status": "OK", "page_price_source": "招商局",
        })

    if extra_remote_state is not None:
        job = store.prepare_remote_intent(
            token, "mall:key-extra", f"daily-{business_date.replace('-', '')}-mall-extra", "payload-extra",
            result_path=str(store_path.parent / "mall-result-extra.xlsx"),
        )
        store.claim_remote_submission(token, int(job["id"]))
        if extra_remote_state == "SUBMIT_UNKNOWN":
            store.mark_submit_unknown(token, int(job["id"]), "post outcome unknown")
        elif extra_remote_state == "MANUAL_REQUIRED":
            store.mark_remote_manual(token, int(job["id"]), "needs human export")
        else:
            raise ValueError(f"unsupported extra_remote_state {extra_remote_state!r}")

    store.release_lease(token)
    return store, snapshots


def _writer(path, headers, rows, enriched, audit_response) -> None:
    from scripts.audit_saas_excel_file import _format_cell_value

    workbook = Workbook()
    summary = workbook.active
    summary.title = "summary"
    summary.append(["total"])
    summary.append([len(rows)])
    sheet = workbook.create_sheet("record_results")
    sheet.append([*headers, "客户价", "最终决策"])
    for row, info, result in zip(rows, enriched, audit_response.record_results, strict=True):
        sheet.append([
            *(_format_cell_value(row.get(header, "")) for header in headers),
            str(info.get("customerPrice") or ""), result.primary_action.value,
        ])
    workbook.save(path)
    workbook.close()


def _evaluate_blocked(store: DailyAuditStore, **overrides):
    token = store.acquire_lease(BUSINESS_DATE, "evaluate-probe", 600)
    try:
        with pytest.raises(CachedFinalizeBlocked) as excinfo:
            evaluate_cached_finalization_readiness(
                store,
                token,
                cutoff_at=overrides.pop("cutoff_at", CUTOFF),
                now=overrides.pop("now", AFTER_CUTOFF),
                source_stability_threshold_seconds=overrides.pop("threshold", 600.0),
            )
    finally:
        store.release_lease(token)
    assert not overrides
    return excinfo.value


def _run_cached(store: DailyAuditStore, lock_path: Path, output: Path, **overrides):
    return run_cached_finalization(
        store,
        ProcessLock(lock_path),
        BUSINESS_DATE,
        output,
        cutoff_at=overrides.pop("cutoff_at", CUTOFF),
        now=overrides.pop("now", AFTER_CUTOFF),
        source_stability_threshold_seconds=overrides.pop("threshold", 600.0),
        degraded_reason_code=overrides.pop("reason", "SOURCE_AUTH_UNAVAILABLE"),
        degraded_source_side=overrides.pop("side", "customer"),
        materializer=overrides.pop("materializer", None),
        **overrides,
    )


def test_constants_use_fixed_mode_names():
    assert CACHED_FINALIZATION_MODE == "CACHED_DEGRADED"
    assert STRICT_FINALIZATION_MODE == "STRICT"


def test_a_single_observation_is_blocked(tmp_path):
    store, _snapshots = _seed_full_good_day(
        tmp_path / "state.sqlite", BUSINESS_DATE, observed_times=("04:00:08",),
    )
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "SINGLE_OBSERVATION"


def test_b_tail_content_differs_is_blocked(tmp_path):
    first = [_row(i) for i in range(1, 10)]
    second = [_row(i) for i in range(1, 9)] + [_row(9, url="https://shop.example/item/9-b")]
    store, _snapshots = _seed_full_good_day(
        tmp_path / "state.sqlite", BUSINESS_DATE,
        observed_times=("04:00:08", "07:00:05"),
        observed_rows=(first, second),
    )
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "TAIL_CONTENT_DIFFERS"


def test_c_stability_below_threshold_is_blocked(tmp_path):
    store, _snapshots = _seed_full_good_day(
        tmp_path / "state.sqlite", BUSINESS_DATE,
        observed_times=("04:00:00", "04:05:00"),
    )
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "STABILITY_BELOW_THRESHOLD"
    assert blocked.detail["computed"] == 300.0


def test_d_model_pending_is_blocked(tmp_path):
    store, _snapshots = _seed_full_good_day(
        tmp_path / "state.sqlite", BUSINESS_DATE, model_pending_indexes={3},
    )
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "STAGE_PENDING"
    assert blocked.detail["model_pending"] >= 1


def test_e_model_terminal_failed_is_blocked(tmp_path):
    store, _snapshots = _seed_full_good_day(
        tmp_path / "state.sqlite", BUSINESS_DATE, model_terminal_indexes={4},
    )
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "STAGE_PENDING"
    assert blocked.detail["model_terminal"] >= 1


def test_f_crawl_pending_is_blocked(tmp_path):
    store, _snapshots = _seed_full_good_day(
        tmp_path / "state.sqlite", BUSINESS_DATE, crawl_pending_indexes={2},
    )
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "CRAWL_PENDING"


def test_g_remote_submit_unknown_is_blocked(tmp_path):
    store, _snapshots = _seed_full_good_day(
        tmp_path / "state.sqlite", BUSINESS_DATE, extra_remote_state="SUBMIT_UNKNOWN",
    )
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "REMOTE_PENDING_OR_MANUAL"


def test_h_remote_manual_required_is_blocked(tmp_path):
    store, _snapshots = _seed_full_good_day(
        tmp_path / "state.sqlite", BUSINESS_DATE, extra_remote_state="MANUAL_REQUIRED",
    )
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "REMOTE_PENDING_OR_MANUAL"


def test_i_deleted_snapshot_member_is_incomplete(tmp_path):
    store_path = tmp_path / "state.sqlite"
    store, snapshots = _seed_full_good_day(store_path, BUSINESS_DATE)
    connection = sqlite3.connect(store_path)
    try:
        with connection:
            connection.execute(
                "DELETE FROM snapshot_members WHERE snapshot_id=? AND row_ordinal=0",
                (int(snapshots[-1]["id"]),),
            )
    finally:
        connection.close()
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "MEMBERS_INCOMPLETE"


def test_j_before_cutoff_is_blocked(tmp_path):
    store, _snapshots = _seed_full_good_day(tmp_path / "state.sqlite", BUSINESS_DATE)
    blocked = _evaluate_blocked(store, now=datetime(2026, 8, 31, 9, 0, 0, tzinfo=UTC))
    assert blocked.reason_code == "BEFORE_CUTOFF"


def test_no_snapshots_is_blocked(tmp_path):
    store_path = tmp_path / "state.sqlite"
    store = DailyAuditStore(store_path)
    store.ensure_run(BUSINESS_DATE, cutoff_at="2026-08-31T10:00:00+00:00")
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "NO_SNAPSHOTS"


def test_saas_count_mismatch_is_blocked(tmp_path):
    store_path = tmp_path / "state.sqlite"
    store, snapshots = _seed_full_good_day(store_path, BUSINESS_DATE)
    connection = sqlite3.connect(store_path)
    try:
        with connection:
            connection.execute(
                "UPDATE snapshots SET evidence_json=? WHERE id=?",
                (json.dumps({"observed_count": 9, "saas_count": 8}), int(snapshots[-1]["id"])),
            )
    finally:
        connection.close()
    blocked = _evaluate_blocked(store)
    assert blocked.reason_code == "SAAS_COUNT_MISMATCH"


def test_happy_path_freezes_last_snapshot_materializes_and_records_provenance(tmp_path):
    store_path = tmp_path / "state.sqlite"
    store, snapshots = _seed_full_good_day(store_path, BUSINESS_DATE)
    output = tmp_path / "formal.xlsx"
    writes = []

    def counting_writer(path, headers, rows, enriched, audit_response):
        writes.append(Path(path))
        _writer(path, headers, rows, enriched, audit_response)

    outcome = _run_cached(
        store, tmp_path / "closeout.lock", output,
        materializer=LegacyWorkbookMaterializer(writer=counting_writer),
    )

    assert outcome.outcome == "CACHED_FINALIZED"
    assert outcome.publication is not None
    assert outcome.publication.materialization is not None
    assert outcome.publication.materialization.row_count == 9
    assert outcome.cached_snapshot_id == int(snapshots[-1]["id"])
    assert outcome.last_source_observed_at == f"{BUSINESS_DATE}T07:00:05+00:00"
    assert outcome.source_stability_seconds == pytest.approx(INCIDENT_STABILITY_SECONDS)
    assert output.exists()
    assert len(writes) == 1

    run = store.get_run(BUSINESS_DATE)
    assert run["state"] == "FINALIZED"
    assert int(run["frozen_snapshot_id"]) == int(snapshots[-1]["id"])
    metrics = json.loads(run["metrics_json"])
    assert metrics["finalized_rows"] == 9
    assert metrics["finalization_mode"] == "CACHED_DEGRADED"
    assert metrics["cached_snapshot_id"] == int(snapshots[-1]["id"])
    assert metrics["cached_last_source_observed_at"] == f"{BUSINESS_DATE}T07:00:05+00:00"
    assert metrics["cached_source_stability_seconds"] == pytest.approx(INCIDENT_STABILITY_SECONDS)
    assert metrics["degraded_reason_code"] == "SOURCE_AUTH_UNAVAILABLE"
    assert metrics["degraded_source_side"] == "customer"
    assert metrics["frozen_snapshot_id"] == int(snapshots[-1]["id"])
    assert "cached_finalized_at" in metrics


def test_second_run_after_cached_finalize_is_idempotent(tmp_path):
    store_path = tmp_path / "state.sqlite"
    store, snapshots = _seed_full_good_day(store_path, BUSINESS_DATE)
    output = tmp_path / "formal.xlsx"
    writes = []

    def counting_writer(path, headers, rows, enriched, audit_response):
        writes.append(Path(path))
        _writer(path, headers, rows, enriched, audit_response)

    materializer = LegacyWorkbookMaterializer(writer=counting_writer)
    first = _run_cached(store, tmp_path / "closeout.lock", output, materializer=materializer)
    assert first.outcome == "CACHED_FINALIZED"

    second = _run_cached(store, tmp_path / "closeout.lock", output, materializer=materializer)
    assert second.outcome == "ALREADY_FINALIZED"
    assert second.publication is None
    assert second.cached_snapshot_id == int(snapshots[-1]["id"])
    assert second.last_source_observed_at is None
    assert second.source_stability_seconds == 0.0
    assert len(writes) == 1


def test_already_frozen_same_snapshot_id_still_materializes(tmp_path):
    store_path = tmp_path / "state.sqlite"
    store, snapshots = _seed_full_good_day(store_path, BUSINESS_DATE)
    token = store.acquire_lease(BUSINESS_DATE, "pre-freeze", 600)
    try:
        store.freeze_snapshot(token, int(snapshots[-1]["id"]))
    finally:
        store.release_lease(token)
    output = tmp_path / "formal.xlsx"

    outcome = _run_cached(
        store, tmp_path / "closeout.lock", output,
        materializer=LegacyWorkbookMaterializer(writer=_writer),
    )

    assert outcome.outcome == "CACHED_FINALIZED"
    assert output.exists()


def test_lock_held_blocks_without_touching_run(tmp_path):
    store, _snapshots = _seed_full_good_day(tmp_path / "state.sqlite", BUSINESS_DATE)
    lock_path = tmp_path / "closeout.lock"
    holder = ProcessLock(lock_path)
    holder.acquire()
    try:
        with pytest.raises(CachedFinalizeBlocked) as excinfo:
            _run_cached(store, lock_path, tmp_path / "formal.xlsx")
        assert excinfo.value.reason_code == "LOCK_HELD"
    finally:
        holder.release()
    run = store.get_run(BUSINESS_DATE)
    assert run["frozen_snapshot_id"] is None
    assert run["state"] != "FINALIZED"


def test_20260831_regression_cached_closeout_core(tmp_path):
    store_path = tmp_path / "state.sqlite"
    store, snapshots = _seed_full_good_day(store_path, BUSINESS_DATE)
    output = tmp_path / "2026-08-31" / "final.xlsx"

    outcome = _run_cached(
        store, tmp_path / "closeout.lock", output,
        now=datetime(2026, 8, 31, 12, 35, 0, tzinfo=UTC),
        reason="SOURCE_AUTH_UNAVAILABLE", side="customer",
        materializer=LegacyWorkbookMaterializer(writer=_writer),
    )

    assert outcome.outcome == "CACHED_FINALIZED"
    assert output.exists()
    run = store.get_run(BUSINESS_DATE)
    assert int(run["frozen_snapshot_id"]) == int(snapshots[-1]["id"]) == 3
    assert run["state"] == "FINALIZED"
    metrics = json.loads(run["metrics_json"])
    assert metrics["finalization_mode"] == "CACHED_DEGRADED"
    assert metrics["degraded_reason_code"] == "SOURCE_AUTH_UNAVAILABLE"
    assert metrics["degraded_source_side"] == "customer"
    assert metrics["cached_last_source_observed_at"] == "2026-08-31T07:00:05+00:00"
    assert metrics["cached_source_stability_seconds"] == INCIDENT_STABILITY_SECONDS
    assert metrics["cached_snapshot_id"] == 3
    assert metrics["frozen_snapshot_id"] == 3


def test_20260831_regression_gate_fires_before_freeze_leaves_day_unfrozen(tmp_path):
    store_path = tmp_path / "state.sqlite"
    store, _snapshots = _seed_full_good_day(store_path, BUSINESS_DATE)
    output = tmp_path / "2026-08-31" / "final.xlsx"

    blocked = _evaluate_blocked(store, threshold=40000.0)
    assert blocked.reason_code == "STABILITY_BELOW_THRESHOLD"

    with pytest.raises(CachedFinalizeBlocked) as excinfo:
        _run_cached(store, tmp_path / "closeout.lock", output, threshold=40000.0)
    assert excinfo.value.reason_code == "STABILITY_BELOW_THRESHOLD"

    run = store.get_run(BUSINESS_DATE)
    assert run["frozen_snapshot_id"] is None
    assert run["state"] != "FINALIZED"
    assert not output.exists()
