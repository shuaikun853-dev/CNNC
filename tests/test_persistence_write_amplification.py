"""Offline persistence regressions; all databases are disposable fixtures."""

import sqlite3
import hashlib
import pytest

from app.services.customer_prewarm_discovery import _read_audit_item_rows, intake_documents, run_prewarm_scan
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.daily_audit_store import DailyAuditStore, FrozenSnapshotConflict
from app.services.dashboard_projection import project_customer_price
from app.services.incremental_audit_runtime import runtime_paths
from app.services.incremental_audit_status import collect_runtime_status
from app.services.snapshot_membership import membership_source_id, snapshot_row_expression
from app.services.shared_crawl_cache import SharedCrawlCache


def test_scanner_catch_up_uses_last_observed_document_version_without_losing_old_documents(tmp_path):
    business_date = "2026-09-26"
    state = DailyAuditStore(tmp_path / "state.sqlite3")
    state.ensure_run(business_date)
    lease = state.acquire_lease(business_date, "test", ttl_seconds=120)
    old = {"documentId": "doc", "vCommodityId": "doc", "skuId": "sku", "fourCategoryId": "old"}
    retained = {"documentId": "retained", "vCommodityId": "retained", "skuId": "sku", "fourCategoryId": "keep"}
    current = {**old, "fourCategoryId": "current"}
    state.ingest_snapshot(lease, [old, retained], observed_at="2026-09-26T01:00:00+00:00")
    latest = state.ingest_snapshot(lease, [current], observed_at="2026-09-26T02:00:00+00:00")
    state.freeze_snapshot(lease, latest["snapshot"]["id"])

    prewarm = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    rows = _read_audit_item_rows(state.path, business_date)
    assert {row["documentId"]: row["fourCategoryId"] for row in rows} == {
        "doc": "current", "retained": "keep"
    }
    assert len(rows) == 2
    intake_documents(prewarm, business_date, rows, source="scanner")
    prewarm.record_document_detail_success("doc")
    intake_documents(prewarm, business_date, _read_audit_item_rows(state.path, business_date), source="scanner")
    assert [row["document_id"] for row in prewarm.pending_documents(10)] == ["retained"]


def test_scanner_catch_up_reads_shared_membership_and_legacy_rows(tmp_path):
    business_date = "2026-09-26"
    state = DailyAuditStore(tmp_path / "state.sqlite3", reuse_snapshot_membership=True)
    state.ensure_run(business_date)
    lease = state.acquire_lease(business_date, "test", ttl_seconds=120)
    row = {"documentId": "doc", "vCommodityId": "doc", "skuId": "sku", "fourCategoryId": "category"}
    state.ingest_snapshot(lease, [row], observed_at="2026-09-26T01:00:00+00:00")
    reused = state.ingest_snapshot(lease, [row], observed_at="2026-09-26T02:00:00+00:00")
    assert reused["snapshot"]["membership_source_id"] is not None
    assert _read_audit_item_rows(state.path, business_date) == [row]

    with sqlite3.connect(state.path) as connection:
        connection.execute("ALTER TABLE snapshots DROP COLUMN membership_source_id")
        connection.execute("ALTER TABLE snapshot_members DROP COLUMN row_json")
    assert _read_audit_item_rows(state.path, business_date) == [row]


def test_recovery_does_not_override_snapshot_covered_document(tmp_path):
    business_date = "2026-09-26"
    state = DailyAuditStore(tmp_path / "state.sqlite3")
    state.ensure_run(business_date)
    lease = state.acquire_lease(business_date, "test", ttl_seconds=120)
    row = {"documentId": "doc", "vCommodityId": "doc", "skuId": "sku", "fourCategoryId": "current"}
    blank = {"documentId": "blank", "vCommodityId": "blank", "skuId": "sku", "fourCategoryId": ""}
    snapshot = state.ingest_snapshot(lease, [row, blank], observed_at="2026-09-26T01:00:00+00:00")
    state.freeze_snapshot(lease, snapshot["snapshot"]["id"])
    prewarm = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    cache = SharedCrawlCache(tmp_path / "cache.sqlite3")

    def scan():
        return run_prewarm_scan(
            prewarm_store=prewarm,
            shared_cache=cache,
            business_date=business_date,
            state_db_path=state.path,
            mappings=[],
            detail_fetch=lambda *_: {},
            max_documents=0,
            customer_recovery_fetch=lambda: [
                {"documentId": "doc", "fourCategoryId": "stale"},
                {"documentId": "recovery-only", "fourCategoryId": "recovered"},
                {"documentId": "blank", "fourCategoryId": "filled"},
            ],
            customer_recovery_interval_seconds=0,
        )

    first = scan()
    assert first["phase_errors"] == []
    assert prewarm.daily_document_evidence(business_date, "doc")["four_category_id"] == "current"
    assert prewarm.daily_document_evidence(business_date, "blank")["four_category_id"] == "filled"
    assert {item["document_id"] for item in prewarm.pending_documents(10)} == {"doc", "blank", "recovery-only"}
    prewarm.record_document_detail_success("doc")
    second = scan()
    assert second["phase_errors"] == []
    assert {item["document_id"] for item in prewarm.pending_documents(10)} == {"blank", "recovery-only"}

def test_identical_intake_does_not_rewrite_documents_or_evidence(tmp_path, monkeypatch):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    rows = [{"documentId": "doc-1", "fourCategoryId": "cat", "commodityName": "title"}]
    monkeypatch.setattr(store, "_utcnow", lambda: "2026-09-26T01:00:00+00:00")
    intake_documents(store, "2026-09-26", rows)
    with sqlite3.connect(store.path) as connection:
        before = connection.execute("SELECT * FROM prewarm_documents").fetchall()
        evidence = connection.execute("SELECT * FROM prewarm_document_evidence").fetchall()
    monkeypatch.setattr(store, "_utcnow", lambda: "2026-09-26T01:01:00+00:00")
    intake_documents(store, "2026-09-26", rows)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT * FROM prewarm_documents").fetchall() == before
        assert connection.execute("SELECT * FROM prewarm_document_evidence").fetchall() == evidence


def test_scanner_recovery_and_scanner_provenance_do_not_ping_pong(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    first = store.upsert_document("2026-09-26", "doc", "cat", source="scanner-recovery")
    second = store.upsert_document("2026-09-26", "doc", "cat", source="scanner")
    assert second == first
    assert store.upsert_document("2026-09-26", "doc", "cat", source="event")["source"] == "event"


def test_recovery_repeated_same_outcome_compacts_but_retains_transitions(tmp_path, monkeypatch):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    for minute in range(20):
        monkeypatch.setattr(store, "_utcnow", lambda m=minute: f"2026-09-26T01:{m:02}:00+00:00")
        store.record_recovery_run("2026-09-26", 5)
    assert store.last_recovery_run_at() == "2026-09-26T01:19:00+00:00"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM prewarm_recovery_runs").fetchone()[0] == 1
    store.record_recovery_run("2026-09-26", -1)
    store.record_recovery_run("2026-09-26", 5)
    with sqlite3.connect(store.path) as connection:
        assert [row[0] for row in connection.execute("SELECT documents_seen FROM prewarm_recovery_runs ORDER BY id")] == [5, -1, 5]


def test_unchanged_thousand_row_observation_after_cutoff_reuses_members(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite3", reuse_snapshot_membership=True)
    store.ensure_run("2026-09-26", cutoff_at="2026-09-26T10:00:00+00:00")
    lease = store.acquire_lease("2026-09-26", "test", ttl_seconds=120)
    rows = [{"vCommodityId": str(i), "skuId": "sku", "extLink": f"https://example.com/{i}"} for i in range(1000)]
    first = store.ingest_snapshot(lease, rows, observed_at="2026-09-26T09:59:00+00:00")
    second = store.ingest_snapshot(lease, rows, observed_at="2026-09-26T10:01:00+00:00")
    assert second["snapshot"]["id"] != first["snapshot"]["id"]
    assert store.first_snapshot_at_or_after("2026-09-26", "2026-09-26T10:00:00+00:00")["id"] == second["snapshot"]["id"]
    assert second["status_counts"] == {"UNCHANGED": 1000}
    assert len(store.ordered_snapshot_items(second["snapshot"]["id"])) == 1000
    store.freeze_snapshot(lease, second["snapshot"]["id"])
    assert store.get_run("2026-09-26")["frozen_snapshot_id"] == second["snapshot"]["id"]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM snapshot_members").fetchone()[0] == 1000


@pytest.mark.parametrize("change", ["title", "url", "header"])
def test_changed_snapshot_content_is_not_deduplicated_by_ordered_hash(tmp_path, change):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    store.ensure_run("2026-09-26")
    lease = store.acquire_lease("2026-09-26", "test", ttl_seconds=120)
    row = {"vCommodityId": "doc", "skuId": "sku", "title": "old", "extLink": "https://example.com/old"}
    first = store.ingest_snapshot(lease, [row], observed_at="2026-09-26T01:00:00+00:00", source_fingerprints=["stable"], headers=["old"])
    new_row = {**row, **({"title": "new"} if change == "title" else {"extLink": "https://example.com/new"} if change == "url" else {})}
    second = store.ingest_snapshot(lease, [new_row], observed_at="2026-09-26T01:01:00+00:00", source_fingerprints=["stable"], headers=["new"] if change == "header" else ["old"])
    assert first["snapshot"]["ordered_hash"] == second["snapshot"]["ordered_hash"]
    assert second["snapshot"]["membership_source_id"] is None
    assert store.ordered_snapshot_items(second["snapshot"]["id"])[0]["row"] == new_row
    assert store.ordered_snapshot_items(first["snapshot"]["id"])[0]["row"] == row


def test_repeated_intake_has_zero_wal_growth_and_keeps_next_day_observation(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    rows = [{"documentId": "doc", "fourCategoryId": "cat", "commodityName": "old"}]
    intake_documents(store, "2026-09-26", rows)
    candidate = store.upsert_link("doc", "https://example.com/item", mall="mall", business_date="2026-09-26")["candidate"]
    # Hold the WAL open; a no-op UPDATE would append a frame even with the
    # same final database size, so file size alone is not enough evidence.
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for _ in range(20):
            intake_documents(store, "2026-09-26", rows, source="scanner-recovery")
        assert store.path.with_name(store.path.name + "-wal").stat().st_size == 0
    intake_documents(store, "2026-09-27", rows)
    assert len(store.observations_for_candidate("2026-09-27", candidate["id"])) == 1
    intake_documents(store, "2026-09-27", [{**rows[0], "commodityName": "new"}])
    assert store.daily_document_evidence("2026-09-27", "doc")["customer_title"] == "new"
    intake_documents(store, "2026-09-27", [{**rows[0], "fourCategoryId": "new-cat"}])
    assert store.pending_documents(10)[0]["four_category_id"] == "new-cat"


def test_old_recovery_history_is_preserved_and_readonly_remains_usable(tmp_path):
    path = tmp_path / "prewarm.sqlite3"
    store = CustomerPrewarmStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO prewarm_recovery_runs(business_date,ran_at,documents_seen) VALUES ('2026-09-26','2026-09-26T00:00:00+00:00',5)")
        historical = connection.execute("SELECT * FROM prewarm_recovery_runs").fetchone()
    for _ in range(3):
        store.record_recovery_run("2026-09-26", 5)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM prewarm_recovery_runs WHERE id=1").fetchone() == historical
        assert connection.execute("SELECT attempt_count FROM prewarm_recovery_runs WHERE id=2").fetchone()[0] == 3
    readonly = CustomerPrewarmStore.open_read_only(path)
    assert readonly.last_recovery_run_at() == store.last_recovery_run_at()


def test_snapshot_readonly_consumers_use_observation_scope_and_legacy_db(tmp_path):
    paths = runtime_paths("2026-09-26", tmp_path)
    store = DailyAuditStore(paths.state_path, reuse_snapshot_membership=True)
    store.ensure_run("2026-09-26")
    lease = store.acquire_lease("2026-09-26", "test", ttl_seconds=120)
    row = {"vCommodityId": "doc", "skuId": "sku", "extLink": ""}
    first = store.ingest_snapshot(lease, [row], observed_at="2026-09-26T01:00:00+00:00")
    second = store.ingest_snapshot(lease, [row], observed_at="2026-09-26T02:00:00+00:00")
    store.freeze_snapshot(lease, second["snapshot"]["id"])
    before = hashlib.sha256(paths.state_path.read_bytes()).hexdigest()
    status = collect_runtime_status(business_date="2026-09-26", runtime_root=tmp_path)
    assert status["frozen_snapshot_id"] == second["snapshot"]["id"]
    assert status["counts"]["unique_items"] == 1
    assert status["counts"]["new"] == 0
    assert status["detail"]["pending"] == 1
    assert len(project_customer_price(paths.state_path, "2026-09-26")["rows"]) == 1
    assert hashlib.sha256(paths.state_path.read_bytes()).hexdigest() == before
    # A separate hand-built legacy database is opened read-only, not migrated.
    legacy = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE snapshots(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO snapshots VALUES(7)")
        connection.execute("CREATE TABLE snapshot_members(snapshot_id INTEGER)")
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()
    with sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        assert membership_source_id(connection, 7) == 7
        assert snapshot_row_expression(connection) == "i.row_json"
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before


def test_membership_reuse_requires_explicit_rollout_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("CNNC_REUSE_SNAPSHOT_MEMBERSHIP", raising=False)
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    store.ensure_run("2026-09-26")
    lease = store.acquire_lease("2026-09-26", "test", ttl_seconds=120)
    rows = [{"vCommodityId": "doc", "skuId": "sku"}]
    store.ingest_snapshot(lease, rows, observed_at="2026-09-26T01:00:00+00:00")
    second = store.ingest_snapshot(lease, rows, observed_at="2026-09-26T02:00:00+00:00")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM snapshot_members WHERE snapshot_id=?", (second["snapshot"]["id"],)).fetchone()[0] == 1
    monkeypatch.setenv("CNNC_REUSE_SNAPSHOT_MEMBERSHIP", "1")
    opted_in = DailyAuditStore(store.path)
    third = opted_in.ingest_snapshot(lease, rows, observed_at="2026-09-26T03:00:00+00:00")
    assert third["snapshot"]["membership_source_id"] == second["snapshot"]["id"]
    assert len(opted_in.ordered_snapshot_items(third["snapshot"]["id"])) == 1


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("change", [None, "title", "url", "header"])
def test_legacy_snapshot_replay_validates_full_content(tmp_path, frozen, change):
    store = DailyAuditStore(tmp_path / "legacy-state.sqlite3")
    store.ensure_run("2026-09-26")
    lease = store.acquire_lease("2026-09-26", "test", ttl_seconds=120)
    row = {"vCommodityId": "doc", "skuId": "sku", "title": "old", "extLink": "https://example.com/old"}
    first = store.ingest_snapshot(lease, [row], observed_at="2026-09-26T01:00:00+00:00", source_fingerprints=["stable"], headers=["old"])
    if frozen:
        store.freeze_snapshot(lease, first["snapshot"]["id"])
    # Additive migration gives every historical observation a NULL hash.
    with sqlite3.connect(store.path) as connection:
        connection.execute("ALTER TABLE snapshots DROP COLUMN content_hash")
    store = DailyAuditStore(store.path)
    new_row = {**row, **({"title": "new"} if change == "title" else {"extLink": "https://example.com/new"} if change == "url" else {})}
    headers = ["new"] if change == "header" else ["old"]
    if frozen and change:
        with pytest.raises(FrozenSnapshotConflict):
            store.ingest_snapshot(lease, [new_row], source_fingerprints=["stable"], headers=headers)
    else:
        replay = store.ingest_snapshot(lease, [new_row], source_fingerprints=["stable"], headers=headers)
        assert (replay["snapshot"]["id"] == first["snapshot"]["id"]) is (change is None)
        assert replay["reused"] is (change is None)
        assert store.ordered_snapshot_items(replay["snapshot"]["id"])[0]["row"] == new_row


def test_legacy_explicit_observation_rejects_conflicting_content(tmp_path):
    store = DailyAuditStore(tmp_path / "legacy-state.sqlite3")
    store.ensure_run("2026-09-26")
    lease = store.acquire_lease("2026-09-26", "test", ttl_seconds=120)
    row = {"vCommodityId": "doc", "skuId": "sku", "title": "old"}
    observed = "2026-09-26T01:00:00+00:00"
    first = store.ingest_snapshot(lease, [row], observed_at=observed, source_fingerprints=["stable"])
    with sqlite3.connect(store.path) as connection:
        connection.execute("ALTER TABLE snapshots DROP COLUMN content_hash")
    store = DailyAuditStore(store.path)
    assert store.ingest_snapshot(lease, [row], observed_at=observed, source_fingerprints=["stable"])["snapshot"]["id"] == first["snapshot"]["id"]
    with pytest.raises(ValueError, match="conflicting content"):
        store.ingest_snapshot(lease, [{**row, "title": "new"}], observed_at=observed, source_fingerprints=["stable"])
