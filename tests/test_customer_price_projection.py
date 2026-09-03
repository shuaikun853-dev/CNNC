"""Row-level customer-price dashboard projection (plan §8 + §9.E).

Seeds a real durable state DB through ``DailyAuditStore`` (no schema
shortcuts), then asserts ``project_customer_price`` counts, ordering,
filters, attempts rendering, sanitization and limit truncation.
"""
from __future__ import annotations

from pathlib import Path

from app.services.customer_price_semantics import encode_business_error
from app.services.daily_audit_store import DailyAuditStore
from app.services.dashboard_projection import (
    empty_customer_price_projection,
    project_customer_price,
)

BUSINESS_DATE = "2026-08-11"


def _row(v: str, *, query: str = "", price: object = None) -> dict:
    row = {"vCommodityId": v, "documentId": v, "skuId": f"sku-{v}", "extLink": ""}
    if query:
        row["queryStr"] = query
    if price is not None:
        row["price"] = price
    return row


def _claim_and_complete(store: DailyAuditStore, lease, work_id: int, result: dict) -> None:
    assert store.claim_stage_work(lease, work_id, max_attempts=3, max_elapsed_seconds=3600)
    store.complete_stage_work(lease, work_id, result)


def _claim_and_fail(
    store: DailyAuditStore, lease, work_id: int, error: str, *, rounds: int
) -> None:
    for _ in range(rounds):
        claimed = store.claim_stage_work(lease, work_id, max_attempts=3, max_elapsed_seconds=3600)
        if claimed is None:
            raise AssertionError("expected the row to still be retryable")
        store.fail_stage_work(
            lease, work_id, elapsed_seconds=0.0, error=error, retryable=True,
            max_attempts=3, max_elapsed_seconds=3600)


def _seed(tmp_path: Path) -> Path:
    """Eight snapshot members covering every business state bucket."""
    db = tmp_path / "state.sqlite3"
    store = DailyAuditStore(db)
    store.ensure_run(BUSINESS_DATE)
    lease = store.acquire_lease(BUSINESS_DATE, "seed-cp-projection", 600)
    try:
        members = store.ingest_snapshot(
            lease,
            [
                _row("v-ready", query="商品甲", price="12.5"),
                _row("v-missing"),
                _row("v-failed"),
                _row("v-retrying", query="商品丁"),
                _row("v-absent"),
                _row("v-legacy"),
                _row("v-invalid"),
                _row("v-nostage", query="商品辛"),
            ],
            source_fingerprints=[f"src-{i}" for i in range(8)],
            customer_fingerprints=[f"cus-{i}" for i in range(8)],
        )["members"]
        work = [
            store.ensure_stage_work(lease, int(m["audit_item_id"]), "DETAIL", f"fp-{i}")
            for i, m in enumerate(members[:7])   # 成员 8 刻意无 DETAIL stage 行
        ]
        # 1 READY: FOUND with a usable price.
        _claim_and_complete(store, lease, int(work[0]["id"]), {
            "customerPrice": "10", "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "", "customerPriceEvidence": "参考价1=10"})
        # 2 MISSING: NOT_FOUND durable business failure, budget exhausted.
        _claim_and_fail(store, lease, int(work[1]["id"]),
                        encode_business_error("NOT_FOUND", "详情接口未返回有效参考价"), rounds=3)
        # 3 FAILED: DETAIL_REQUEST_FAILED durable business failure, exhausted.
        _claim_and_fail(store, lease, int(work[2]["id"]),
                        encode_business_error("DETAIL_REQUEST_FAILED", "请求失败"), rounds=3)
        # 4 RETRYING: one retryable business failure, budget remains.
        _claim_and_fail(store, lease, int(work[3]["id"]),
                        encode_business_error("NOT_FOUND", "详情接口未返回有效参考价"), rounds=1)
        # 5 MISSING_PROVEN: deterministic absence with complete evidence.
        _claim_and_complete(store, lease, int(work[4]["id"]), {
            "customerPrice": None, "customerPriceStatus": "LOOKUP_FAILED",
            "customerPriceSource": "",
            "customerPriceWarning": "customer export did not contain this documentId",
            "customerPriceEvidence": ""})
        # 6 FAILED (legacy invalid success): retryable status checkpointed as SUCCESS.
        _claim_and_complete(store, lease, int(work[5]["id"]), {
            "customerPrice": None, "customerPriceStatus": "NOT_FOUND",
            "customerPriceSource": "",
            "customerPriceWarning": "详情接口未返回有效参考价",
            "customerPriceEvidence": ""})
        # 7 INVALID: SUCCESS but zero price.
        _claim_and_complete(store, lease, int(work[6]["id"]), {
            "customerPrice": "0", "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS",
            "customerPriceWarning": "", "customerPriceEvidence": ""})
        # 8: no DETAIL stage work at all (LEFT JOIN miss) → PENDING.
    finally:
        store.release_lease(lease)
    return db


def _by_id(rows: list[dict]) -> dict:
    return {r["document_id"]: r for r in rows}


def test_counts_buckets_sum_to_total_required(tmp_path):
    data = project_customer_price(_seed(tmp_path), BUSINESS_DATE)
    assert data["business_date"] == BUSINESS_DATE
    assert data["filter"] == "all"
    assert data["total_required"] == 8
    assert data["counts"] == {"ready": 1, "pending": 1, "retrying": 1, "missing": 1,
                              "failed": 2, "invalid": 1, "absent_proven": 1}
    assert sum(data["counts"].values()) == data["total_required"]
    assert data["max_attempts"] == 3
    assert data["truncated"] == 0


def test_row_fields_and_unfinished_first_ordering(tmp_path):
    data = project_customer_price(_seed(tmp_path), BUSINESS_DATE)
    order = [r["document_id"] for r in data["rows"]]
    # 获取中 → 待重试 → 缺失 → 失败(按快照顺序) → 无效 → 证据缺席 → 已获取。
    assert order == ["v-nostage", "v-retrying", "v-missing", "v-failed", "v-legacy",
                     "v-invalid", "v-absent", "v-ready"]

    ready = _by_id(data["rows"])["v-ready"]
    assert ready["state"] == "READY"
    assert ready["customer_price"] == "10"
    assert ready["customer_price_status"] == "FOUND"
    assert ready["customer_price_source"] == "DETAIL_REFERENCE_INFOS"
    assert ready["detail_state"] == "SUCCESS"
    assert ready["attempts"] == 1 and ready["max_attempts"] == 3
    assert ready["retryable"] is False
    assert ready["saas_price"] == "12.5"
    assert ready["title"] == "商品甲"

    missing = _by_id(data["rows"])["v-missing"]
    assert missing["state"] == "MISSING"
    assert missing["customer_price_status"] == "NOT_FOUND"   # decoded from error_text
    assert missing["detail_state"] == "TERMINAL_FAILED"
    assert missing["attempts"] == 3 and missing["max_attempts"] == 3
    assert missing["retryable"] is False
    assert missing["customer_price"] is None

    retrying = _by_id(data["rows"])["v-retrying"]
    assert retrying["state"] == "RETRYING"
    assert retrying["attempts"] == 1 and retrying["retryable"] is True
    assert retrying["error_text"].startswith("customerPriceStatus=NOT_FOUND")

    no_stage = _by_id(data["rows"])["v-nostage"]
    assert no_stage["state"] == "PENDING"
    assert no_stage["detail_state"] == ""
    assert no_stage["attempts"] == 0
    assert no_stage["sku_id"] == "sku-v-nostage"
    assert no_stage["saas_price"] is None


def test_filters_unacquired_retrying_terminal(tmp_path):
    db = _seed(tmp_path)
    unacquired = project_customer_price(db, BUSINESS_DATE, filter="unacquired")
    assert sorted(r["document_id"] for r in unacquired["rows"]) == [
        "v-failed", "v-invalid", "v-legacy", "v-missing", "v-nostage", "v-retrying"]
    retrying = project_customer_price(db, BUSINESS_DATE, filter="retrying")
    assert [r["document_id"] for r in retrying["rows"]] == ["v-retrying"]
    terminal = project_customer_price(db, BUSINESS_DATE, filter="terminal")
    assert sorted(r["document_id"] for r in terminal["rows"]) == [
        "v-failed", "v-invalid", "v-legacy", "v-missing"]
    # counts 与 total_required 始终是全量口径,不随 filter 收窄。
    assert terminal["counts"]["ready"] == 1 and terminal["total_required"] == 8


def test_invalid_filter_defends_to_all(tmp_path):
    data = project_customer_price(_seed(tmp_path), BUSINESS_DATE, filter=";drop table")
    assert data["filter"] == "all"
    assert len(data["rows"]) == 8


def test_error_text_is_sanitized(tmp_path):
    """敏感头/凭据材料绝不能穿透到 UI:Authorization/Bearer/长 hex 一律 REDACT。"""
    db = _seed(tmp_path)
    store = DailyAuditStore(db)
    lease = store.acquire_lease(BUSINESS_DATE, "inject-sensitive-error", 600)
    try:
        rows = store.list_stage_work_for_business_date(BUSINESS_DATE, stage="DETAIL")
        work_id = next(int(w["id"]) for w in rows if w["state"] == "RETRYABLE_FAILED")
        claimed = store.claim_stage_work(lease, work_id, max_attempts=3, max_elapsed_seconds=3600)
        assert claimed is not None
        store.fail_stage_work(
            lease, work_id, elapsed_seconds=0.0, retryable=True,
            error="request failed: Authorization: Bearer abc123def456ghi789, "
                  "Cookie: SESSDATA=deadbeefdeadbeefdeadbeefdeadbeef",
            max_attempts=3, max_elapsed_seconds=3600)
    finally:
        store.release_lease(lease)
    data = project_customer_price(db, BUSINESS_DATE, filter="retrying")
    assert len(data["rows"]) == 1
    error_text = data["rows"][0]["error_text"]
    assert "abc123def456ghi789" not in error_text
    assert "deadbeef" not in error_text
    assert "[REDACTED]" in error_text


def test_limit_truncates_and_reports_remainder(tmp_path):
    data = project_customer_price(_seed(tmp_path), BUSINESS_DATE, limit=3)
    assert len(data["rows"]) == 3
    assert data["truncated"] == 5
    # 截断后保留的仍必须是"未完成优先"的最前面几行。
    assert [r["document_id"] for r in data["rows"]] == [
        "v-nostage", "v-retrying", "v-missing"]


def test_frozen_snapshot_rows_win_over_latest(tmp_path):
    db = _seed(tmp_path)
    store = DailyAuditStore(db)
    lease = store.acquire_lease(BUSINESS_DATE, "freeze-first-snapshot", 600)
    try:
        assert project_customer_price(db, BUSINESS_DATE)["total_required"] == 8
        store.ingest_snapshot(
            lease, [_row("v-extra")],
            source_fingerprints=["src-x"], customer_fingerprints=["cus-x"])
        # 未冻结:投影读最新快照(仅含 v-extra 这 1 个成员)。
        latest = project_customer_price(db, BUSINESS_DATE)
        assert latest["total_required"] == 1
        assert [r["document_id"] for r in latest["rows"]] == ["v-extra"]
        snapshots = store.list_snapshots(BUSINESS_DATE)
        store.freeze_snapshot(lease, int(snapshots[0]["id"]))
    finally:
        store.release_lease(lease)
    # 冻结后:投影读冻结快照(8 行成员),不是最新快照。
    data = project_customer_price(db, BUSINESS_DATE)
    assert data["total_required"] == 8
    assert all(r["document_id"] != "v-extra" for r in data["rows"])


def test_missing_run_or_snapshot_returns_empty_structure(tmp_path):
    db = _seed(tmp_path)
    other_day = project_customer_price(db, "2026-08-12")
    assert other_day == empty_customer_price_projection("2026-08-12")
    assert other_day["rows"] == [] and other_day["total_required"] == 0
    assert other_day["counts"] == {"ready": 0, "pending": 0, "retrying": 0,
                                   "missing": 0, "failed": 0, "invalid": 0,
                                   "absent_proven": 0}
    empty = empty_customer_price_projection("2026-08-12", "terminal")
    assert empty["filter"] == "terminal" and empty["max_attempts"] == 3
