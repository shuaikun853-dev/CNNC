from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.services.daily_audit_store import DailyAuditStore, FrozenSnapshotConflict, ProcessLock
from app.services.incremental_audit_materializer import LegacyWorkbookMaterializer
from app.workflows.incremental_price_audit_finalize import (
    FinalizeBlocked,
    build_frozen_audit_artifact,
    materialize_frozen_daily_run,
)


def _row(v="v-1", sku="sku-1", url="https://shop.example/item", **extra):
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": url,
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
        "__crawl_required": bool(url),
        **extra,
    }


def _seed(tmp_path: Path, rows=None, crawl_result=None):
    rows = list(rows or [_row(), _row()])
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run("2026-08-11", cutoff_at="2026-08-11T10:00:00+00:00")
    token = store.acquire_lease("2026-08-11", "seed", 60)
    snapshot = store.ingest_snapshot(
        token,
        rows,
        "2026-08-11T10:01:00+00:00",
        headers=[name for name in rows[0] if not name.startswith("__")],
        evidence={"observed_count": len(rows)},
    )["snapshot"]
    store.freeze_snapshot(token, snapshot["id"])
    items = store.ordered_snapshot_items(snapshot["id"])
    for item in {item["id"]: item for item in items}.values():
        detail = store.ensure_stage_work(token, item["id"], "DETAIL", f"detail-{item['id']}")
        store.claim_stage_work(token, detail["id"], max_attempts=3, max_elapsed_seconds=300)
        store.complete_stage_work(token, detail["id"], {
            "customerPrice": "10",
            "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "",
            "customerPriceEvidence": "customer=10",
        })
        model = store.ensure_stage_work(token, item["id"], "MODEL", f"model-{item['id']}")
        store.claim_stage_work(token, model["id"], max_attempts=1, max_elapsed_seconds=300)
        store.complete_stage_work(token, model["id"], {
            "match_result": "SAME", "confidence": 0.95, "model_match": "exact", "reason": "一致",
        })
    if rows[0].get("__crawl_required"):
        crawl = store.ensure_crawl_work(
            token, "mall", rows[0]["skuId"], rows[0]["extLink"], "legacy-v1", "crawl-fp",
            source_id=rows[0]["vCommodityId"],
        )
        store.complete_crawl_work(token, crawl["id"], crawl_result or {
            "page_price_value": "9.5", "page_price_status": "OK", "page_price_source": "招商局",
        })
    store.release_lease(token)
    return store, snapshot


def _writer(path, headers, rows, enriched, audit_response):
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


def test_build_artifact_replays_duplicate_frozen_order_and_precomputed_results(tmp_path):
    store, snapshot = _seed(tmp_path)
    token = store.acquire_lease("2026-08-11", "build", 60)

    built = build_frozen_audit_artifact(store, token, output_identity="daily-final-20260811")

    assert built.readiness.ready is True
    assert [row["vCommodityId"] for row in built.artifact.frozen_rows] == ["v-1", "v-1"]
    assert len(built.artifact.audit_response.record_results) == 2
    assert all(row["__page_price_status"] == "OK" for row in built.artifact.frozen_rows)
    assert built.artifact.frozen_snapshot_id == str(snapshot["id"])
    store.release_lease(token)


def test_model_pending_and_unplanned_required_crawl_block_official_output(tmp_path):
    store, snapshot = _seed(tmp_path, rows=[_row("v-1", "s-1"), _row("v-2", "s-2", "https://other/item")])
    token = store.acquire_lease("2026-08-11", "break", 60)
    second = store.ordered_snapshot_items(snapshot["id"])[1]
    model = next(row for row in store.list_stage_work(token, stage="MODEL") if row["audit_item_id"] == second["id"])
    # A changed fingerprint resets the second model to durable PENDING.
    store.ensure_stage_work(token, second["id"], "MODEL", model["fingerprint"] + "-changed")

    built = build_frozen_audit_artifact(store, token, output_identity="daily-final-20260811")

    assert built.readiness.ready is False
    assert built.readiness.model_pending == 1
    assert built.readiness.crawl_unplanned == 1
    store.release_lease(token)


def test_detail_terminal_is_materializable_but_missing_crawl_price_is_blocking(tmp_path):
    store, snapshot = _seed(tmp_path, crawl_result={
        "page_price_value": None, "page_price_status": "PRICE_MISSING", "page_price_source": "招商局-价格不可见",
    })
    token = store.acquire_lease("2026-08-11", "terminal", 60)
    item = store.ordered_snapshot_items(snapshot["id"])[0]
    detail = next(row for row in store.list_stage_work(token, stage="DETAIL") if row["audit_item_id"] == item["id"])
    store.ensure_stage_work(token, item["id"], "DETAIL", detail["fingerprint"] + "-changed")
    claimed = store.claim_stage_work(token, detail["id"], max_attempts=1, max_elapsed_seconds=1)
    assert claimed is not None
    store.fail_stage_work(
        token, detail["id"], elapsed_seconds=2, error="TimeoutError", retryable=True,
        max_attempts=1, max_elapsed_seconds=1,
    )
    built = build_frozen_audit_artifact(store, token, output_identity="daily-final-20260811")

    assert built.readiness.detail_terminal >= 1
    assert built.readiness.crawl_invalid_price >= 1
    assert built.readiness.ready is False
    store.release_lease(token)


def test_materialize_marks_run_finalized_and_same_finalize_reuses(tmp_path):
    store, _snapshot = _seed(tmp_path, rows=[_row(url="")])
    output = tmp_path / "formal.xlsx"
    materializer = LegacyWorkbookMaterializer(writer=_writer)

    first = materialize_frozen_daily_run(
        store,
        ProcessLock(tmp_path / "finalize.lock"),
        "2026-08-11",
        output,
        materializer=materializer,
    )
    second = materialize_frozen_daily_run(
        store,
        ProcessLock(tmp_path / "finalize.lock"),
        "2026-08-11",
        output,
        materializer=materializer,
    )

    assert first.materialization is not None and first.materialization.reused is False
    assert second.materialization is not None and second.materialization.reused is True
    run = store.get_run("2026-08-11")
    assert run["state"] == "FINALIZED"
    assert run["final_output_path"] == str(output.resolve())
    assert json.loads(run["metrics_json"])["finalized_rows"] == 1


def test_finalized_run_rejects_different_output_before_materializer_writes(tmp_path):
    store, _snapshot = _seed(tmp_path, rows=[_row(url="")])
    first_output = tmp_path / "formal.xlsx"
    conflicting_output = tmp_path / "other.xlsx"
    materializer = LegacyWorkbookMaterializer(writer=_writer)

    materialize_frozen_daily_run(
        store, ProcessLock(tmp_path / "finalize.lock"), "2026-08-11", first_output,
        materializer=materializer,
    )

    with pytest.raises(FrozenSnapshotConflict):
        materialize_frozen_daily_run(
            store, ProcessLock(tmp_path / "finalize.lock"), "2026-08-11", conflicting_output,
            materializer=materializer,
        )

    assert not conflicting_output.exists()
    assert not conflicting_output.with_name(conflicting_output.name + ".manifest.json").exists()


def test_crash_after_publish_before_store_mark_recovers_idempotently(tmp_path, monkeypatch):
    store, _snapshot = _seed(tmp_path, rows=[_row(url="")])
    output = tmp_path / "formal.xlsx"
    materializer = LegacyWorkbookMaterializer(writer=_writer)
    original = store.mark_finalized
    calls = 0

    def crash_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SystemExit("crash before DB mark")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "mark_finalized", crash_once)
    with pytest.raises(SystemExit):
        materialize_frozen_daily_run(
            store, ProcessLock(tmp_path / "finalize.lock"), "2026-08-11", output,
            materializer=materializer,
        )
    published = output.read_bytes()
    interrupted = store.get_run("2026-08-11")
    assert interrupted["state"] == "FINALIZING"
    assert interrupted["final_output_path"] == str(output.resolve())

    recovered = materialize_frozen_daily_run(
        store, ProcessLock(tmp_path / "finalize.lock"), "2026-08-11", output,
        materializer=materializer,
    )

    assert recovered.materialization is not None and recovered.materialization.reused is True
    assert output.read_bytes() == published
    assert store.get_run("2026-08-11")["state"] == "FINALIZED"


def test_blocked_readiness_never_writes_formal_workbook(tmp_path):
    store, snapshot = _seed(tmp_path, rows=[_row(url="")])
    token = store.acquire_lease("2026-08-11", "reset", 60)
    item = store.ordered_snapshot_items(snapshot["id"])[0]
    model = store.list_stage_work(token, stage="MODEL")[0]
    store.ensure_stage_work(token, item["id"], "MODEL", model["fingerprint"] + "-new")
    store.release_lease(token)
    output = tmp_path / "formal.xlsx"

    with pytest.raises(FinalizeBlocked):
        materialize_frozen_daily_run(
            store, ProcessLock(tmp_path / "finalize.lock"), "2026-08-11", output,
            materializer=LegacyWorkbookMaterializer(writer=_writer),
        )

    assert not output.exists()
