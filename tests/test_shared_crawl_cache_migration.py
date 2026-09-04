"""Migration-safety tests for the shared crawl cache (Issue #7).

The shared cache must be a standalone database file: STEP1's per-day
``state.sqlite3`` must remain byte-for-byte untouched in table set and row
counts, and the cache itself must rebuild idempotently across reopens.
"""

from __future__ import annotations

import sqlite3

from app.services.daily_audit_store import DailyAuditStore
from app.services.shared_crawl_cache import SharedCrawlCache


def _table_row_counts(path) -> dict[str, int]:
    connection = sqlite3.connect(str(path))
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for name in tables:
            counts[name] = int(
                connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            )
        return counts
    finally:
        connection.close()


def _count_rows(path, table: str) -> int:
    connection = sqlite3.connect(str(path))
    try:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        connection.close()


def test_step1_state_untouched(tmp_path):
    day_dir = tmp_path / "2026-09-01"
    state_path = day_dir / "state.sqlite3"
    store = DailyAuditStore(state_path)
    store.ensure_run("2026-09-01")
    token = store.acquire_lease("2026-09-01", "issue7-test", 60.0)
    store.release_lease(token)
    tables_before = store.table_names()
    counts_before = _table_row_counts(state_path)

    cache = SharedCrawlCache(tmp_path / "shared_crawl_cache.sqlite3")
    cache.record_success(
        mall="jd",
        submitted_url="https://item.jd.com/100012043978.html",
        price="109.00",
        source="issue7-migration-test",
    )

    assert store.table_names() == tables_before
    assert _table_row_counts(state_path) == counts_before
    reopened = DailyAuditStore(state_path)
    token2 = reopened.acquire_lease("2026-09-01", "issue7-test-2", 60.0)
    reopened.release_lease(token2)


def test_cache_reopen_after_table_drop_rebuilds_without_losing_rows(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cache = SharedCrawlCache(path)
    url1 = "https://www.ofs.cn/product-111111.html"
    url2 = "https://www.ofs.cn/product-222222.html"
    cache.record_success(mall="ofs", submitted_url=url1, source="unit-test")
    cache.record_success(mall="ofs", submitted_url=url2, source="unit-test")

    connection = sqlite3.connect(str(path))
    try:
        connection.execute("DROP TABLE crawl_urls")
        connection.commit()
    finally:
        connection.close()

    reopened = SharedCrawlCache(path)
    assert "crawl_urls" in reopened.table_names()
    # The alias table is empty again, so these lookups can only hit via the
    # mall-parser level-3 identity path; no new crawl_results rows are written
    # here, so the result history must be exactly what survived the drop.
    hit1 = reopened.lookup_current("ofs", url1)
    hit2 = reopened.lookup_current("ofs", url2)
    assert hit1 is not None
    assert hit1["match_level"] == "MALL_PARSER"
    assert hit2 is not None
    assert hit2["match_level"] == "MALL_PARSER"
    assert _count_rows(path, "crawl_results") == 2


def test_reopen_twice_idempotent_rows_preserved(tmp_path):
    path = tmp_path / "cache.sqlite3"
    cache = SharedCrawlCache(path)
    cache.record_success(
        mall="jd",
        submitted_url="https://item.jd.com/1.html",
        source="unit-test",
    )
    first_count = _count_rows(path, "crawl_results")
    SharedCrawlCache(path)
    SharedCrawlCache(path)
    assert _count_rows(path, "crawl_results") == first_count
    assert first_count == 1
