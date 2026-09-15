"""Tests for the prewarm dashboard read model (Issue #13, Review Round 2).

Fixtures build REAL databases under ``tmp_path`` only: the prewarm store via
``CustomerPrewarmStore`` (RW constructor, incl. the Round-2 daily-observation
/ remote-job-link tables), the staples sidecar tables via
``StaplesPrewarmStore`` (same sqlite file), the shared crawl cache via
``SharedCrawlCache`` (RW, incl. the Round-2 ``producer`` column) and a minimal
STEP1 ``state.sqlite3`` via raw sqlite.  Production runtime paths are never
touched; the projection itself must stay zero-write (missing stores are never
created).

Round-2 contracts under test here:

- B1: the cache fallback is mall-PROVEN (identity = mall + URL); a same URL
  cached under two malls never leaks across candidates and URL-only / SKU
  matching is forbidden.
- B2: summary / list / detail are consistently scoped to the business day's
  observed candidate set, and remote jobs resolve through the linkage's (or
  legacy candidate's) OWN business date — never the selected date by bare id.
- B3: ``classify_crawl_producer`` is the single classification authority;
  producer wins over source; KPIs and the source filter use the 5 classes.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import prewarm_dashboard_projection as projection
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_MANUAL_SKIP,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CACHE_STATUS_ROUTING_BLOCKED,
    CACHE_STATUS_STAPLES_PENDING,
    CustomerPrewarmStore,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from app.services.staples_prewarm import StaplesPrewarmStore

BUSINESS_DATE = "2026-09-08"
DAY_2 = "2026-09-09"
# 2026-09-08 00:00 Asia/Shanghai == 2026-09-07T16:00:00Z: TS_OLD is a hit from
# a PREVIOUS business day (historical), TS_TODAY is inside the business day.
TS_OLD = "2026-09-07T10:00:00+00:00"
TS_TODAY = "2026-09-08T04:00:00+00:00"

STAPLES_URL_BASE = "https://www.stbchina.cn/item.html?itemId="

# Whitelist of one list_prewarm_links row (guards against payload_json leaks).
LINK_ROW_KEYS = {
    "candidate_id",
    "document_ids",
    "document_count",
    "mall",
    "original_url",
    "cache_status",
    "cache_status_label",
    "match_level",
    "source_class",
    "producer",
    "title",
    "price",
    "crawl_id",
    "real_sku_id",
    "sku_status",
    "sku_status_label",
    "sku_requires_review",
    "last_success_at",
    "remote_job_state",
    "staples_job_state",
}

KPI_INT_KEYS = {
    "reference_link_count",
    "unique_url_count",
    "candidate_count",
    "cache_hit_candidate_count",
    "historical_cache_hit_count",
    "daily_internet_hit_count",
    "prewarm_new_success_count",
    "manual_recrawl_hit_count",
    "zhaoshang_running_candidate_count",
    "zhaoshang_running_job_count",
    "staples_running_count",
    "failure_count",
    "url_parsed_unverified_count",
    "unresolved_sku_count",
    "remote_job_count",
    "remote_url_count",
}


# ---- fixture helpers ----------------------------------------------------------


_REMOTE_JOBS_DDL = """
CREATE TABLE remote_jobs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    logical_key TEXT NOT NULL,
    task_name TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    mall_key TEXT,
    input_path TEXT,
    result_path TEXT,
    payload_json TEXT,
    state TEXT NOT NULL DEFAULT 'SUBMIT_INTENT',
    post_attempted INTEGER NOT NULL DEFAULT 0,
    remote_identity TEXT,
    export_path TEXT,
    result_json TEXT,
    error_text TEXT,
    harvest_attempts INTEGER NOT NULL DEFAULT 0,
    harvest_elapsed_seconds REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    recovery_evidence_json TEXT
)
"""


def _make_state_db(
    runtime_root,
    jobs: list[tuple[int, str]],
    *,
    business_date: str = BUSINESS_DATE,
    task_names: dict[int, str] | None = None,
) -> None:
    """Minimal STEP1 state DB with the remote_jobs columns the module reads."""
    task_names = task_names or {}
    day_dir = runtime_root / business_date
    day_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(day_dir / "state.sqlite3"))
    try:
        connection.execute(_REMOTE_JOBS_DDL)
        for job_id, state in jobs:
            task_name = task_names.get(job_id, f"cnnc_current_opt_mallx_{job_id:014d}")
            connection.execute(
                """INSERT INTO remote_jobs
                   (id, run_id, logical_key, task_name, payload_hash, mall_key,
                    state, created_at, updated_at)
                   VALUES (?, 1, ?, ?, 'hash', 'comix', ?, ?, ?)""",
                (
                    job_id,
                    f"logical-{business_date}-{job_id}",
                    task_name,
                    state,
                    TS_TODAY,
                    TS_TODAY,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _seed_cache_hit(
    prewarm: CustomerPrewarmStore,
    shared: SharedCrawlCache,
    document_id: str,
    url: str,
    mall: str,
    *,
    source: str,
    succeeded_at: str = TS_TODAY,
    sku_status: str,
    real_sku_id: str | None,
    title: str | None,
    price: str | None = "12.5",
    producer: str | None = None,
    observed: str = BUSINESS_DATE,
) -> int:
    """CACHE_HIT candidate observed on ``observed`` (upsert wiring records the
    document-day observation; candidate.business_date == observed so the
    legacy no-linkage job fallback resolves the same day's state DB)."""
    row = shared.record_success(
        mall,
        url,
        source=source,
        succeeded_at=succeeded_at,
        sku_status=sku_status,
        real_sku_id=real_sku_id,
        title=title,
        price=price,
        producer=producer,
    )
    upserted = prewarm.upsert_link(document_id, url, mall=mall, business_date=observed)
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(
        candidate_id,
        cache_status=CACHE_STATUS_CACHE_HIT,
        match_level="EXACT",
        resolved_identity_id=int(row["identity_id"]),
    )
    return candidate_id


def _seed_hit_without_identity(
    prewarm: CustomerPrewarmStore,
    document_id: str,
    url: str,
    mall: str,
    *,
    observed: str = BUSINESS_DATE,
) -> int:
    """CACHE_HIT candidate with NO resolved identity → exercises the B1
    mall-proven cache fallback (identity must come from mall + URL)."""
    upserted = prewarm.upsert_link(document_id, url, mall=mall, business_date=observed)
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_CACHE_HIT)
    return candidate_id


def _seed_status_candidate(
    prewarm: CustomerPrewarmStore,
    document_id: str,
    url: str,
    mall: str,
    *,
    cache_status: str,
    remote_job_id: int | None = None,
    observed: str = BUSINESS_DATE,
) -> int:
    upserted = prewarm.upsert_link(document_id, url, mall=mall, business_date=observed)
    candidate_id = int(upserted["candidate"]["id"])
    if cache_status != CACHE_STATUS_PENDING_PREWARM:
        prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(
        candidate_id, cache_status=cache_status, remote_job_id=remote_job_id
    )
    return candidate_id


def _seed_staples_job(db_path, candidate_id: int, state: str) -> None:
    """Ensure the sidecar row exists (PENDING), then force the wanted state."""
    StaplesPrewarmStore(db_path).ensure_job(candidate_id)
    if state != "PENDING":
        connection = sqlite3.connect(str(db_path))
        try:
            connection.execute(
                "UPDATE staples_prewarm_jobs SET state=?, updated_at=? WHERE candidate_id=?",
                (state, TS_TODAY, candidate_id),
            )
            connection.commit()
        finally:
            connection.close()


@pytest.fixture()
def env(tmp_path):
    """Rich mixed fixture: 10 candidates covering every KPI bucket exactly.

    All candidates are observed on BUSINESS_DATE (the day the tests view), so
    the Round-2 day scope keeps every one of them visible.
    """
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    db_path = runtime_root / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(db_path)
    shared = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    StaplesPrewarmStore(db_path)  # sidecar tables live in the same file

    ids: dict[str, int] = {}
    # 1: CACHE_HIT, daily-internet, succeeded BEFORE the business day (historical).
    ids["old_daily"] = _seed_cache_hit(
        prewarm, shared, "D1", "https://qx.com/item-a.html", "comix",
        source="daily-internet", succeeded_at=TS_OLD,
        sku_status="CONFIRMED", real_sku_id="SKU-A", title="Title A",
    )
    # 2: CACHE_HIT, staples-local, TODAY, sku URL_PARSED_UNVERIFIED.
    ids["staples_today"] = _seed_cache_hit(
        prewarm, shared, "D2", STAPLES_URL_BASE + "1001358701037447", "stbchina",
        source="staples-local", succeeded_at=TS_TODAY,
        sku_status="URL_PARSED_UNVERIFIED", real_sku_id="1001358701037447",
        title="Title B", price="8.8",
    )
    # 3: REMOTE_DISPATCHED bound to RUNNING job 1.
    ids["remote_running"] = _seed_status_candidate(
        prewarm, "D3", "https://qx.com/item-c.html", "comix",
        cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=1,
    )
    # 4: ROUTING_BLOCKED.
    ids["blocked"] = _seed_status_candidate(
        prewarm, "D4", "https://qx.com/item-d.html", "comix",
        cache_status=CACHE_STATUS_ROUTING_BLOCKED,
    )
    # 5: CACHE_HIT, daily-internet TODAY, sku UNRESOLVED, no title.
    ids["unresolved"] = _seed_cache_hit(
        prewarm, shared, "D5", STAPLES_URL_BASE + "1001358701037448", "stbchina",
        source="daily-internet", succeeded_at=TS_TODAY,
        sku_status="UNRESOLVED", real_sku_id=None, title=None, price="3.3",
    )
    # 6: REMOTE_DISPATCHED bound to FAILED job 2.
    ids["remote_failed"] = _seed_status_candidate(
        prewarm, "D6", "https://qx.com/item-f.html", "comix",
        cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=2,
    )
    # 7: STAPLES_PENDING with no sidecar job.
    ids["staples_pending"] = _seed_status_candidate(
        prewarm, "D7", STAPLES_URL_BASE + "1001358701037449", "stbchina",
        cache_status=CACHE_STATUS_STAPLES_PENDING,
    )
    # 8: PENDING_PREWARM but sidecar job PENDING (running via job only).
    ids["pending_with_job"] = _seed_status_candidate(
        prewarm, "D8", STAPLES_URL_BASE + "1001358701037450", "stbchina",
        cache_status=CACHE_STATUS_PENDING_PREWARM,
    )
    _seed_staples_job(db_path, ids["pending_with_job"], "PENDING")
    # 9: STAPLES_PENDING with FAILED sidecar job (failure bucket).
    ids["staples_failed"] = _seed_status_candidate(
        prewarm, "D9", STAPLES_URL_BASE + "1001358701037451", "stbchina",
        cache_status=CACHE_STATUS_STAPLES_PENDING,
    )
    _seed_staples_job(db_path, ids["staples_failed"], "FAILED")
    # 10: MANUAL_SKIP.
    ids["manual_skip"] = _seed_status_candidate(
        prewarm, "D10", "https://qx.com/item-j.html", "comix",
        cache_status=CACHE_STATUS_MANUAL_SKIP,
    )

    _make_state_db(runtime_root, jobs=[(1, "RUNNING"), (2, "FAILED")])
    return SimpleNamespace(
        runtime_root=runtime_root,
        prewarm_db=db_path,
        cache_db=runtime_root / "shared_crawl_cache.sqlite3",
        state_db=runtime_root / BUSINESS_DATE / "state.sqlite3",
        ids=ids,
    )


def _small_env(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    db_path = runtime_root / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(db_path)
    shared = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    StaplesPrewarmStore(db_path)
    return runtime_root, prewarm, shared


# ---- P1: missing stores -------------------------------------------------------


def test_p1_missing_stores_summary_degraded_zero_filled_without_file_creation(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    paths = projection.prewarm_paths(BUSINESS_DATE, runtime_root=runtime_root)
    assert paths.prewarm_db == runtime_root / "customer_prewarm.sqlite3"
    assert paths.cache_db == runtime_root / "shared_crawl_cache.sqlite3"
    assert paths.state_db == runtime_root / BUSINESS_DATE / "state.sqlite3"

    summary = projection.build_prewarm_dashboard_summary(BUSINESS_DATE, runtime_root=runtime_root)
    assert summary["business_date"] == BUSINESS_DATE
    assert summary["generated_at"]
    assert summary["available"] == {
        "prewarm_store": False,
        "shared_cache": False,
        "state_db": False,
    }
    assert summary["degraded"] is True
    assert summary["degraded_reasons"]
    assert summary["scope"] == {"business_date": BUSINESS_DATE, "candidate_count_in_scope": 0}
    kpi = summary["kpi"]
    assert set(kpi) == KPI_INT_KEYS | {"status_distribution"}
    for key in KPI_INT_KEYS:
        assert kpi[key] == 0, key
    assert kpi["status_distribution"] == []
    # Read-only discipline: the empty runtime dir must stay empty.
    assert list(runtime_root.rglob("*")) == []


def test_p1_missing_stores_links_returns_empty_page(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    result = projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)
    assert result["links"] == []
    assert result["pagination"] == {"page": 1, "page_size": 50, "total": 0, "total_pages": 0}
    assert result["filters_applied"] == {}
    assert result["generated_at"]
    assert result["degraded"] is True
    assert list(runtime_root.rglob("*")) == []


def test_p1_missing_stores_detail_returns_none(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    assert projection.get_prewarm_link_detail(BUSINESS_DATE, 1, runtime_root=runtime_root) is None
    assert list(runtime_root.rglob("*")) == []


# ---- P2: mixed KPI fixture, exact units ----------------------------------------


def test_p2_kpi_counts_mixed_fixture_exact_units(env):
    summary = projection.build_prewarm_dashboard_summary(
        BUSINESS_DATE, runtime_root=env.runtime_root
    )
    assert summary["available"] == {"prewarm_store": True, "shared_cache": True, "state_db": True}
    assert summary["degraded"] is False
    assert summary["degraded_reasons"] == []
    assert summary["scope"] == {"business_date": BUSINESS_DATE, "candidate_count_in_scope": 10}
    kpi = summary["kpi"]
    assert kpi["reference_link_count"] == 10  # prewarm_links rows in the day scope
    assert kpi["unique_url_count"] == 10  # distinct original_url in the day scope
    assert kpi["candidate_count"] == 10
    assert kpi["cache_hit_candidate_count"] == 3  # old_daily + staples_today + unresolved
    assert kpi["historical_cache_hit_count"] == 1  # succeeded before 2026-09-08 00:00 +08:00
    assert kpi["daily_internet_hit_count"] == 2  # old_daily + unresolved
    assert kpi["prewarm_new_success_count"] == 1  # staples-local current row
    assert kpi["manual_recrawl_hit_count"] == 0
    assert kpi["zhaoshang_running_candidate_count"] == 1  # job 1 RUNNING only
    assert kpi["zhaoshang_running_job_count"] == 1  # distinct running jobs
    # Union semantics: STAPLES_PENDING status OR sidecar job PENDING/RETRY_WAIT,
    # one per candidate — candidates 7 (status), 8 (job) and 9 (status, despite
    # its FAILED job the status itself is still STAPLES_PENDING).
    assert kpi["staples_running_count"] == 3
    assert kpi["failure_count"] == 4  # ROUTING_BLOCKED + MANUAL_SKIP + staples FAILED + remote FAILED
    assert kpi["url_parsed_unverified_count"] == 1
    assert kpi["unresolved_sku_count"] == 1
    assert kpi["remote_job_count"] == 2  # distinct jobs bound to REMOTE_* candidates
    assert kpi["remote_url_count"] == 1  # candidate rows bound to non-terminal jobs (job 1 only)
    # List shape: {status, label, count} sorted by status; labels come from
    # the module's _CACHE_STATUS_LABELS (unknown statuses degrade to raw).
    assert kpi["status_distribution"] == [
        {"status": "CACHE_HIT", "label": "缓存命中", "count": 3},
        {"status": "MANUAL_SKIP", "label": "人工跳过", "count": 1},
        {"status": "PENDING_PREWARM", "label": "待预爬", "count": 1},
        {"status": "REMOTE_DISPATCHED", "label": "招商局已派发", "count": 2},
        {"status": "ROUTING_BLOCKED", "label": "路由受阻", "count": 1},
        {"status": "STAPLES_PENDING", "label": "史泰博预爬中", "count": 2},
    ]


# ---- P3: duplicate document references -----------------------------------------


def test_p3_duplicate_document_refs_share_candidate_and_show_both_documents(tmp_path):
    runtime_root, prewarm, shared = _small_env(tmp_path)
    url = "https://qx.com/shared.html"
    prewarm.upsert_link("DOC-A", url, mall="comix", business_date=BUSINESS_DATE)
    prewarm.upsert_link("DOC-B", url, mall="comix", business_date=BUSINESS_DATE)

    summary = projection.build_prewarm_dashboard_summary(BUSINESS_DATE, runtime_root=runtime_root)
    assert summary["kpi"]["reference_link_count"] == 2
    assert summary["kpi"]["unique_url_count"] == 1
    assert summary["kpi"]["candidate_count"] == 1

    result = projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)
    assert result["pagination"]["total"] == 1
    row = result["links"][0]
    assert sorted(row["document_ids"]) == ["DOC-A", "DOC-B"]
    assert row["document_count"] == 2


# ---- P4: producer classification authority (B3) ----------------------------------


def test_p4_producer_classification_authority():
    classify = projection.classify_crawl_producer
    # Non-NULL producer wins and maps directly (the 5 known producers).
    assert classify("daily-internet", "daily-internet") == "daily-internet"
    assert classify("daily-internet", "customer-prewarm-zhaoshang") == "customer-prewarm-zhaoshang"
    assert classify("staples-local", "staples-local") == "staples-local-prewarm"
    assert classify("daily-internet", "manual-recrawl-staples") == "manual-recrawl"
    assert classify("staples-local", "manual-recrawl-zhaoshang") == "manual-recrawl"
    # Unknown producer → other (never falls through to source).
    assert classify("daily-internet", "mystery-producer") == "other"
    # NULL/empty producer → legacy source-based classification.
    assert classify("daily-internet", None) == "daily-internet"
    assert classify("daily-internet", "") == "daily-internet"
    assert classify("staples-local", None) == "staples-local-prewarm"
    assert classify("manual-recrawl-staples", None) == "manual-recrawl"
    assert classify("weird-mall-bot", None) == "other"
    assert classify("", None) == "other"


def test_p4_unknown_cache_source_is_classified_other_without_crash(tmp_path):
    runtime_root, prewarm, shared = _small_env(tmp_path)
    _seed_cache_hit(
        prewarm, shared, "D1", "https://qx.com/bot.html", "comix",
        source="weird-mall-bot", succeeded_at=TS_TODAY,
        sku_status="CONFIRMED", real_sku_id="SKU-1", title="Bot title",
    )
    result = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"source": "other"}, runtime_root=runtime_root
    )
    assert result["pagination"]["total"] == 1
    assert result["links"][0]["source_class"] == "other"
    assert result["links"][0]["producer"] is None
    summary = projection.build_prewarm_dashboard_summary(BUSINESS_DATE, runtime_root=runtime_root)
    kpi = summary["kpi"]
    assert kpi["cache_hit_candidate_count"] == 1
    assert kpi["daily_internet_hit_count"] == 0
    assert kpi["prewarm_new_success_count"] == 0
    assert kpi["manual_recrawl_hit_count"] == 0


def test_legacy_null_producer_classifies_by_source(tmp_path):
    """B3: producer NULL (pre-R2 rows) keeps classifying by source until the
    URL is re-crawled — documented limitation, never an exception."""
    runtime_root, prewarm, shared = _small_env(tmp_path)
    _seed_cache_hit(
        prewarm, shared, "D1", STAPLES_URL_BASE + "1001358701037447", "stbchina",
        source="staples-local", succeeded_at=TS_TODAY,
        sku_status="URL_PARSED_UNVERIFIED", real_sku_id="1001358701037447",
        title="Legacy staples", price="6.6", producer=None,
    )
    links = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"source": "staples-local-prewarm"}, runtime_root=runtime_root
    )
    assert links["pagination"]["total"] == 1
    assert links["links"][0]["source_class"] == "staples-local-prewarm"
    assert links["links"][0]["producer"] is None
    kpi = projection.build_prewarm_dashboard_summary(
        BUSINESS_DATE, runtime_root=runtime_root
    )["kpi"]
    assert kpi["prewarm_new_success_count"] == 1
    assert kpi["daily_internet_hit_count"] == 0
    assert kpi["manual_recrawl_hit_count"] == 0


def test_four_producer_classes_strictly_separated(tmp_path):
    """B3: production-shape fixture — the same ``source`` can carry different
    producers; class (not source) drives KPIs, filters and row payloads."""
    runtime_root, prewarm, shared = _small_env(tmp_path)
    specs = [
        ("daily", "https://qx.com/pd.html", "daily-internet", "daily-internet"),
        ("zprewarm", "https://qx.com/pz.html", "daily-internet", "customer-prewarm-zhaoshang"),
        ("staples", STAPLES_URL_BASE + "1001358701037447", "staples-local", "staples-local"),
        ("manual", "https://qx.com/pm.html", "daily-internet", "manual-recrawl-zhaoshang"),
    ]
    ids: dict[str, int] = {}
    for name, url, source, producer in specs:
        ids[name] = _seed_cache_hit(
            prewarm, shared, f"D-{name}", url, "comix",
            source=source, succeeded_at=TS_TODAY,
            sku_status="CONFIRMED", real_sku_id=f"SKU-{name}",
            title=f"Title {name}", price="1.1", producer=producer,
        )

    kpi = projection.build_prewarm_dashboard_summary(
        BUSINESS_DATE, runtime_root=runtime_root
    )["kpi"]
    assert kpi["cache_hit_candidate_count"] == 4
    assert kpi["daily_internet_hit_count"] == 1  # only producer=daily-internet
    assert kpi["prewarm_new_success_count"] == 2  # staples-local + zhaoshang prewarm
    assert kpi["manual_recrawl_hit_count"] == 1  # manual (zhaoshang here)

    rows = {
        row["source_class"]: row
        for row in projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)["links"]
    }
    assert set(rows) == {"daily-internet", "customer-prewarm-zhaoshang",
                         "staples-local-prewarm", "manual-recrawl"}
    # The source filter accepts exactly the 5 class values and matches the
    # classify_crawl_producer result of the current row.
    for class_value, expected_id in (
        ("daily-internet", ids["daily"]),
        ("customer-prewarm-zhaoshang", ids["zprewarm"]),
        ("staples-local-prewarm", ids["staples"]),
        ("manual-recrawl", ids["manual"]),
    ):
        filtered = projection.list_prewarm_links(
            BUSINESS_DATE, filters={"source": class_value}, runtime_root=runtime_root
        )
        assert filtered["pagination"]["total"] == 1, class_value
        assert filtered["links"][0]["candidate_id"] == expected_id, class_value
    assert projection.list_prewarm_links(
        BUSINESS_DATE, filters={"source": "staples-local"}, runtime_root=runtime_root
    )["pagination"]["total"] == 0  # raw source strings are NOT filter values
    assert projection.list_prewarm_links(
        BUSINESS_DATE, filters={"source": "other"}, runtime_root=runtime_root
    )["pagination"]["total"] == 0

    detail = projection.get_prewarm_link_detail(
        BUSINESS_DATE, ids["zprewarm"], runtime_root=runtime_root
    )
    assert detail["current"]["producer"] == "customer-prewarm-zhaoshang"
    assert detail["current"]["source_class"] == "customer-prewarm-zhaoshang"
    assert detail["current"]["source"] == "daily-internet"


# ---- B1: mall-scoped cache fallback -----------------------------------------------


def test_cross_mall_same_url_never_shared(tmp_path):
    """B1: one URL cached under two malls → each mall-identity candidate sees
    ONLY its own mall's current row; the URL string alone never matches."""
    runtime_root, prewarm, shared = _small_env(tmp_path)
    url = "https://item.example.com/1001.html"
    row_jd = shared.record_success(
        "jd", url, source="daily-internet", succeeded_at=TS_TODAY,
        sku_status="CONFIRMED", real_sku_id="JD-1", title="JD title", price="11",
    )
    row_zkh = shared.record_success(
        "zkh", url, source="daily-internet", succeeded_at=TS_TODAY,
        sku_status="CONFIRMED", real_sku_id="ZKH-1", title="ZKH title", price="22",
    )
    assert row_jd["identity_id"] != row_zkh["identity_id"]
    candidate_jd = _seed_hit_without_identity(prewarm, "DA", url, "jd")
    candidate_zkh = _seed_hit_without_identity(prewarm, "DB", url, "zkh")

    links = projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)
    assert links["pagination"]["total"] == 2
    by_candidate = {row["candidate_id"]: row for row in links["links"]}
    assert by_candidate[candidate_jd]["title"] == "JD title"
    assert by_candidate[candidate_jd]["price"] == "11"
    assert by_candidate[candidate_zkh]["title"] == "ZKH title"
    assert by_candidate[candidate_zkh]["price"] == "22"

    summary = projection.build_prewarm_dashboard_summary(
        BUSINESS_DATE, runtime_root=runtime_root
    )
    kpi = summary["kpi"]
    assert kpi["candidate_count"] == 2
    assert kpi["cache_hit_candidate_count"] == 2
    assert kpi["daily_internet_hit_count"] == 2  # both count as hits, no cross-contamination

    detail = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_jd, runtime_root=runtime_root
    )
    assert detail["current"]["mall"] == "jd"
    assert detail["current"]["identity_id"] == int(row_jd["identity_id"])
    assert detail["current"]["title"] == "JD title"
    detail_zkh = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_zkh, runtime_root=runtime_root
    )
    assert detail_zkh["current"]["mall"] == "zkh"
    assert detail_zkh["current"]["identity_id"] == int(row_zkh["identity_id"])


def test_staples_empty_mall_fallback_via_stbchina(tmp_path):
    """B1: empty candidate mall + stbchina URL + no identity → the staples
    mall is proven from the URL classification and the current row resolves."""
    runtime_root, prewarm, shared = _small_env(tmp_path)
    url = STAPLES_URL_BASE + "1001358701037447"
    shared.record_success(
        "stbchina", url, source="staples-local", succeeded_at=TS_TODAY,
        sku_status="URL_PARSED_UNVERIFIED", real_sku_id="1001358701037447",
        title="Staples title", price="8.8",
    )
    candidate_id = _seed_hit_without_identity(prewarm, "DS", url, "")

    links = projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)
    assert links["pagination"]["total"] == 1
    row = links["links"][0]
    assert row["mall"] == ""  # displayed mall stays the candidate's own
    assert row["title"] == "Staples title"
    assert row["source_class"] == "staples-local-prewarm"

    kpi = projection.build_prewarm_dashboard_summary(
        BUSINESS_DATE, runtime_root=runtime_root
    )["kpi"]
    assert kpi["cache_hit_candidate_count"] == 1
    assert kpi["prewarm_new_success_count"] == 1
    assert kpi["daily_internet_hit_count"] == 0
    assert kpi["manual_recrawl_hit_count"] == 0


def test_no_fallback_when_mall_unprovable(tmp_path):
    """B1: empty mall + URL that classifies to no mall → NO cache row may be
    attached, even when the URL string exists under some other mall."""
    runtime_root, prewarm, shared = _small_env(tmp_path)
    _make_state_db(runtime_root, jobs=[])  # all stores exist → no unrelated degraded note
    url = "https://www.example.com/mystery-item.html"
    shared.record_success(
        "comix", url, source="daily-internet", succeeded_at=TS_TODAY,
        sku_status="CONFIRMED", real_sku_id="DECOY-1", title="Decoy title", price="9",
    )
    candidate_id = _seed_hit_without_identity(prewarm, "DX", url, "")

    links = projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)
    assert links["pagination"]["total"] == 1
    row = links["links"][0]
    assert row["title"] is None
    assert row["source_class"] is None
    assert row["producer"] is None
    assert row["last_success_at"] is None

    kpi = projection.build_prewarm_dashboard_summary(
        BUSINESS_DATE, runtime_root=runtime_root
    )["kpi"]
    assert kpi["cache_hit_candidate_count"] == 1  # candidate status still counts
    assert kpi["daily_internet_hit_count"] == 0  # but no cache row was attached

    detail = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_id, runtime_root=runtime_root
    )
    assert detail["current"] is None
    assert detail["history"] == []
    assert detail["degraded"] is False  # unprovable mall is data absence, not a read failure


def test_zhaoshang_empty_mall_fallback_via_routing(monkeypatch, tmp_path):
    """B1: empty mall + zhaoshang URL → the mapped mall key is proven through
    the routing table and scopes the fallback lookup."""
    from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

    mapping = ZhaoshangMallMapping(
        mall_name="齐心商城", source_id="SRC-COMIX", mall_key="comix",
        domains=("qx.com", "www.qx.com"), vendor_keywords=("comix",),
    )
    monkeypatch.setattr(
        "scripts.zhaoshang_mall_mapping.load_zhaoshang_mall_mappings", lambda: [mapping]
    )
    runtime_root, prewarm, shared = _small_env(tmp_path)
    url = "https://qx.com/zs-only.html"
    shared.record_success(
        "comix", url, source="daily-internet", succeeded_at=TS_TODAY,
        sku_status="CONFIRMED", real_sku_id="ZS-1", title="ZS title", price="3",
    )
    candidate_id = _seed_hit_without_identity(prewarm, "DZ", url, "")

    links = projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)
    assert links["pagination"]["total"] == 1
    assert links["links"][0]["title"] == "ZS title"


# ---- B2: day scope + per-date remote job resolution --------------------------------


def test_cross_day_job_id_collision(tmp_path):
    """B2: job id 17 exists on BOTH days with unrelated jobs; the linkage row
    pins the candidate to Day 1's job — never the selected day's bare id."""
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")  # every store exists
    StaplesPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    day1_task = "cnnc_current_opt_mallx_day1_17"
    day2_task = "cnnc_current_opt_mallx_day2_17"
    _make_state_db(runtime_root, jobs=[(17, "RUNNING")],
                   business_date=BUSINESS_DATE, task_names={17: day1_task})
    _make_state_db(runtime_root, jobs=[(17, "COMPLETED")],
                   business_date=DAY_2, task_names={17: day2_task})

    url = "https://qx.com/collision.html"
    upserted = prewarm.upsert_link("DOC-X", url, mall="comix", business_date=BUSINESS_DATE)
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=17
    )
    # Linkage pins (business_date=D1, job 17); Day 2's job 17 is UNRELATED.
    prewarm.record_remote_job_link(BUSINESS_DATE, candidate_id, 17, "customer-prewarm-zhaoshang")
    # The same candidate is observed again on Day 2 (repeat reference).
    prewarm.record_daily_observation(DAY_2, candidate_id, "DOC-X", url)

    summary = projection.build_prewarm_dashboard_summary(DAY_2, runtime_root=runtime_root)
    assert summary["degraded"] is False
    assert summary["scope"] == {"business_date": DAY_2, "candidate_count_in_scope": 1}
    kpi = summary["kpi"]
    assert kpi["candidate_count"] == 1
    # Day 1's job 17 is RUNNING → running KPI; Day 2's job 17 is COMPLETED and
    # must never be consulted.
    assert kpi["zhaoshang_running_candidate_count"] == 1
    assert kpi["zhaoshang_running_job_count"] == 1
    assert kpi["remote_url_count"] == 1  # non-terminal per Day 1's job state

    links = projection.list_prewarm_links(DAY_2, runtime_root=runtime_root)
    assert links["pagination"]["total"] == 1
    assert links["links"][0]["remote_job_state"] == "RUNNING"

    detail = projection.get_prewarm_link_detail(DAY_2, candidate_id, runtime_root=runtime_root)
    assert detail["remote_job"]["task_name"] == day1_task
    assert detail["remote_job"]["state"] == "RUNNING"

    # Day 1 scope resolves the same (its own) job.
    detail_d1 = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_id, runtime_root=runtime_root
    )
    assert detail_d1["remote_job"]["task_name"] == day1_task


def test_repeat_reference_across_dates(tmp_path):
    """B2: the same document/url observed on two days appears in BOTH days'
    candidate sets (summary scope + link list)."""
    runtime_root, prewarm, _shared = _small_env(tmp_path)
    url = "https://qx.com/repeat.html"
    prewarm.upsert_link("DOC-Y", url, mall="comix", business_date=BUSINESS_DATE)
    prewarm.upsert_link("DOC-Y", url, mall="comix", business_date=DAY_2)

    for day in (BUSINESS_DATE, DAY_2):
        summary = projection.build_prewarm_dashboard_summary(day, runtime_root=runtime_root)
        assert summary["scope"] == {"business_date": day, "candidate_count_in_scope": 1}, day
        assert summary["kpi"]["candidate_count"] == 1, day
        assert summary["kpi"]["reference_link_count"] == 1, day
        links = projection.list_prewarm_links(day, runtime_root=runtime_root)
        assert links["pagination"]["total"] == 1, day
        assert links["links"][0]["document_ids"] == ["DOC-Y"], day


def test_day_scoped_document_ids_two_docs_two_days(tmp_path):
    """G1: the documents map is day-scoped.  Day 1 DOC-A and Day 2 DOC-B map
    to the SAME candidate (identity is mall+url), yet each day's view shows
    only ITS OWN observation rows — summary count, list row and detail
    alike."""
    runtime_root, prewarm, _shared = _small_env(tmp_path)
    url = "https://qx.com/day-scope-docs.html"
    upserted = prewarm.upsert_link("DOC-A", url, mall="comix", business_date=BUSINESS_DATE)
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.upsert_link("DOC-B", url, mall="comix", business_date=DAY_2)

    summary_d1 = projection.build_prewarm_dashboard_summary(
        BUSINESS_DATE, runtime_root=runtime_root
    )
    assert summary_d1["kpi"]["reference_link_count"] == 1
    links_d1 = projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)
    assert links_d1["pagination"]["total"] == 1
    assert links_d1["links"][0]["document_ids"] == ["DOC-A"]
    assert links_d1["links"][0]["document_count"] == 1

    summary_d2 = projection.build_prewarm_dashboard_summary(DAY_2, runtime_root=runtime_root)
    assert summary_d2["kpi"]["reference_link_count"] == 1
    links_d2 = projection.list_prewarm_links(DAY_2, runtime_root=runtime_root)
    assert links_d2["pagination"]["total"] == 1
    assert links_d2["links"][0]["document_ids"] == ["DOC-B"]
    assert links_d2["links"][0]["document_count"] == 1

    detail_d1 = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_id, runtime_root=runtime_root
    )
    assert detail_d1["document_ids"] == ["DOC-A"]
    detail_d2 = projection.get_prewarm_link_detail(
        DAY_2, candidate_id, runtime_root=runtime_root
    )
    assert detail_d2["document_ids"] == ["DOC-B"]


def test_day_scoped_reference_link_count_multi_doc(tmp_path):
    """G1: reference_link_count counts the VIEWED day's observation rows
    among the in-scope candidates; the candidate itself is counted once."""
    runtime_root, prewarm, _shared = _small_env(tmp_path)
    url = "https://qx.com/multi-doc.html"
    prewarm.upsert_link("DOC-A", url, mall="comix", business_date=BUSINESS_DATE)
    prewarm.upsert_link("DOC-B", url, mall="comix", business_date=BUSINESS_DATE)
    prewarm.upsert_link("DOC-C", url, mall="comix", business_date=DAY_2)

    summary_d1 = projection.build_prewarm_dashboard_summary(
        BUSINESS_DATE, runtime_root=runtime_root
    )
    assert summary_d1["scope"] == {"business_date": BUSINESS_DATE, "candidate_count_in_scope": 1}
    kpi_d1 = summary_d1["kpi"]
    assert kpi_d1["candidate_count"] == 1  # summary scope counts the candidate once
    assert kpi_d1["reference_link_count"] == 2  # DOC-A + DOC-B observed on Day 1
    assert kpi_d1["unique_url_count"] == 1
    row_d1 = projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)["links"][0]
    assert sorted(row_d1["document_ids"]) == ["DOC-A", "DOC-B"]
    assert row_d1["document_count"] == 2

    summary_d2 = projection.build_prewarm_dashboard_summary(DAY_2, runtime_root=runtime_root)
    assert summary_d2["scope"] == {"business_date": DAY_2, "candidate_count_in_scope": 1}
    kpi_d2 = summary_d2["kpi"]
    assert kpi_d2["candidate_count"] == 1
    assert kpi_d2["reference_link_count"] == 1  # only DOC-C observed on Day 2
    assert kpi_d2["unique_url_count"] == 1
    row_d2 = projection.list_prewarm_links(DAY_2, runtime_root=runtime_root)["links"][0]
    assert row_d2["document_ids"] == ["DOC-C"]
    assert row_d2["document_count"] == 1


def test_no_future_linkage_leak(tmp_path):
    """G1: a linkage dated AFTER the viewed day never projects — View D1
    resolves Day 1's job 17 even though a Day-2 linkage (job 31) exists, and
    View D2 resolves job 31."""
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")  # every store exists
    StaplesPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    day1_task = "cnnc_current_opt_mallx_d1_17"
    day2_task = "cnnc_current_opt_mallx_d2_31"
    _make_state_db(runtime_root, jobs=[(17, "RUNNING")],
                   business_date=BUSINESS_DATE, task_names={17: day1_task})
    _make_state_db(runtime_root, jobs=[(31, "RUNNING")],
                   business_date=DAY_2, task_names={31: day2_task})

    url = "https://qx.com/future-leak.html"
    upserted = prewarm.upsert_link("DOC-F", url, mall="comix", business_date=BUSINESS_DATE)
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=17
    )
    # Same candidate observed again on Day 2 (so both views are in scope).
    prewarm.record_daily_observation(DAY_2, candidate_id, "DOC-F", url)

    # Natural order: D1 linkage recorded first, D2 linkage later.
    prewarm.record_remote_job_link(BUSINESS_DATE, candidate_id, 17, "customer-prewarm-zhaoshang")
    prewarm.record_remote_job_link(DAY_2, candidate_id, 31, "customer-prewarm-zhaoshang")

    summary_d1 = projection.build_prewarm_dashboard_summary(
        BUSINESS_DATE, runtime_root=runtime_root
    )
    assert summary_d1["degraded"] is False
    assert summary_d1["scope"]["candidate_count_in_scope"] == 1
    kpi_d1 = summary_d1["kpi"]
    # job 17 (Day 1) RUNNING — Day 2's job 31 must never leak into View D1.
    assert kpi_d1["zhaoshang_running_candidate_count"] == 1
    assert kpi_d1["remote_job_count"] == 1
    detail_d1 = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_id, runtime_root=runtime_root
    )
    assert detail_d1["remote_job"]["task_name"] == day1_task
    assert detail_d1["remote_job"]["state"] == "RUNNING"

    summary_d2 = projection.build_prewarm_dashboard_summary(DAY_2, runtime_root=runtime_root)
    assert summary_d2["kpi"]["zhaoshang_running_candidate_count"] == 1
    detail_d2 = projection.get_prewarm_link_detail(
        DAY_2, candidate_id, runtime_root=runtime_root
    )
    assert detail_d2["remote_job"]["task_name"] == day2_task
    assert detail_d2["remote_job"]["state"] == "RUNNING"

    # Inversion: refresh the D2 linkage to an EARLIER recorded_at and the D1
    # linkage to a LATER one — the business_date filter (not recorded_at
    # ordering) must decide, so View D1 still picks job 17.
    early = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
    late = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
    prewarm.record_remote_job_link(
        DAY_2, candidate_id, 31, "customer-prewarm-zhaoshang", now=early
    )
    prewarm.record_remote_job_link(
        BUSINESS_DATE, candidate_id, 17, "customer-prewarm-zhaoshang", now=late
    )
    detail_d1b = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_id, runtime_root=runtime_root
    )
    assert detail_d1b["remote_job"]["task_name"] == day1_task
    assert detail_d1b["remote_job"]["state"] == "RUNNING"


def test_latest_business_date_linkage_wins_over_refreshed_recorded_at(tmp_path):
    """Among the linkage rows dated ON OR BEFORE the viewed day the MAX
    business date must win first (then recorded_at, rowid INSIDE that day):
    a late wall-clock refresh of an OLDER day's linkage never out-ranks the
    newest business day's linkage."""
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")  # every store exists
    StaplesPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    day1_task = "cnnc_current_opt_mallx_d1_17"
    day2_task = "cnnc_current_opt_mallx_d2_31"
    _make_state_db(runtime_root, jobs=[(17, "RUNNING")],
                   business_date=BUSINESS_DATE, task_names={17: day1_task})
    _make_state_db(runtime_root, jobs=[(31, "RUNNING")],
                   business_date=DAY_2, task_names={31: day2_task})

    url = "https://qx.com/late-refresh.html"
    upserted = prewarm.upsert_link("DOC-R", url, mall="comix", business_date=BUSINESS_DATE)
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=17
    )
    # Same candidate observed again on Day 2 (so both views are in scope).
    prewarm.record_daily_observation(DAY_2, candidate_id, "DOC-R", url)

    # Natural wall-clock order: the D1 linkage recorded BEFORE the D2 one.
    prewarm.record_remote_job_link(
        BUSINESS_DATE, candidate_id, 17, "customer-prewarm-zhaoshang",
        now=datetime(2026, 9, 14, 20, 0, tzinfo=UTC),
    )
    prewarm.record_remote_job_link(
        DAY_2, candidate_id, 31, "customer-prewarm-zhaoshang",
        now=datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
    )

    detail_d1 = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_id, runtime_root=runtime_root
    )
    assert detail_d1["degraded"] is False
    assert detail_d1["remote_job"]["task_name"] == day1_task
    assert detail_d1["remote_job"]["state"] == "RUNNING"

    detail_d2 = projection.get_prewarm_link_detail(
        DAY_2, candidate_id, runtime_root=runtime_root
    )
    assert detail_d2["remote_job"]["task_name"] == day2_task
    assert detail_d2["remote_job"]["state"] == "RUNNING"

    # Refresh the D1 linkage so its recorded_at becomes the LATEST of all —
    # max business_date must still decide: View D2 keeps resolving job 31.
    prewarm.record_remote_job_link(
        BUSINESS_DATE, candidate_id, 17, "customer-prewarm-zhaoshang",
        now=datetime(2026, 9, 15, 23, 0, tzinfo=UTC),
    )
    detail_d2b = projection.get_prewarm_link_detail(
        DAY_2, candidate_id, runtime_root=runtime_root
    )
    assert detail_d2b["degraded"] is False
    assert detail_d2b["remote_job"]["task_name"] == day2_task
    assert detail_d2b["remote_job"]["state"] == "RUNNING"


def test_no_linkage_falls_back_to_candidate_day(tmp_path):
    """B2: legacy pre-linkage candidate (remote_job_id only) resolves its job
    from the candidate's OWN business date DB, regardless of the viewed day."""
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")  # every store exists
    StaplesPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    day1_task = "cnnc_current_opt_mallx_legacy_17"
    _make_state_db(runtime_root, jobs=[(17, "RUNNING")],
                   business_date=BUSINESS_DATE, task_names={17: day1_task})
    _make_state_db(runtime_root, jobs=[], business_date=DAY_2)  # selected day: no job 17

    url = "https://qx.com/legacy.html"
    upserted = prewarm.upsert_link("DOC-L", url, mall="comix", business_date=BUSINESS_DATE)
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=17
    )
    prewarm.record_daily_observation(DAY_2, candidate_id, "DOC-L", url)

    summary = projection.build_prewarm_dashboard_summary(DAY_2, runtime_root=runtime_root)
    assert summary["degraded"] is False
    assert summary["kpi"]["candidate_count"] == 1
    assert summary["kpi"]["zhaoshang_running_candidate_count"] == 1

    links = projection.list_prewarm_links(DAY_2, runtime_root=runtime_root)
    assert links["links"][0]["remote_job_state"] == "RUNNING"
    detail = projection.get_prewarm_link_detail(DAY_2, candidate_id, runtime_root=runtime_root)
    assert detail["remote_job"]["task_name"] == day1_task
    assert detail["remote_job"]["state"] == "RUNNING"


def test_day_scope_consistency(tmp_path):
    """B2: a candidate observed only on Day 1 is absent from Day 2's summary,
    list AND detail (404 semantics); Day 1 keeps it everywhere."""
    runtime_root, prewarm, shared = _small_env(tmp_path)
    row = shared.record_success(
        "comix", "https://qx.com/scope.html", source="daily-internet",
        succeeded_at=TS_TODAY, sku_status="CONFIRMED", real_sku_id="S-1",
        title="Scoped title", price="5",
    )
    upserted = prewarm.upsert_link(
        "DOC-S", "https://qx.com/scope.html", mall="comix", business_date=BUSINESS_DATE
    )
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_CACHE_HIT,
        resolved_identity_id=int(row["identity_id"]),
    )

    # Day 2: out of scope everywhere.
    summary = projection.build_prewarm_dashboard_summary(DAY_2, runtime_root=runtime_root)
    assert summary["scope"] == {"business_date": DAY_2, "candidate_count_in_scope": 0}
    assert summary["kpi"]["candidate_count"] == 0
    assert summary["kpi"]["reference_link_count"] == 0
    assert summary["kpi"]["cache_hit_candidate_count"] == 0
    assert summary["kpi"]["status_distribution"] == []
    links = projection.list_prewarm_links(DAY_2, runtime_root=runtime_root)
    assert links["pagination"]["total"] == 0
    assert projection.get_prewarm_link_detail(DAY_2, candidate_id, runtime_root=runtime_root) is None

    # Day 1: in scope everywhere.
    summary_d1 = projection.build_prewarm_dashboard_summary(
        BUSINESS_DATE, runtime_root=runtime_root
    )
    assert summary_d1["scope"] == {
        "business_date": BUSINESS_DATE,
        "candidate_count_in_scope": 1,
    }
    assert summary_d1["kpi"]["candidate_count"] == 1
    assert summary_d1["kpi"]["cache_hit_candidate_count"] == 1
    links_d1 = projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)
    assert links_d1["pagination"]["total"] == 1
    detail_d1 = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_id, runtime_root=runtime_root
    )
    assert detail_d1 is not None
    assert detail_d1["candidate_id"] == candidate_id


# ---- filters (server-side: counts come from the module) -------------------------


def test_filter_mall_substring_case_insensitive_matches_candidate_or_cache_mall(tmp_path):
    runtime_root, prewarm, shared = _small_env(tmp_path)
    # Candidate mall is EMPTY; only the cache row knows mall "comix".
    row = shared.record_success(
        "comix", "https://qx.com/mallless.html", source="daily-internet", price="1",
        real_sku_id="M-1", sku_status="CONFIRMED",
    )
    upserted = prewarm.upsert_link(
        "D1", "https://qx.com/mallless.html", mall="", business_date=BUSINESS_DATE
    )
    prewarm.record_candidate_result(
        int(upserted["candidate"]["id"]),
        cache_status=CACHE_STATUS_CACHE_HIT,
        resolved_identity_id=int(row["identity_id"]),
    )
    prewarm.upsert_link(
        "D2", STAPLES_URL_BASE + "1001358701037447", mall="stbchina",
        business_date=BUSINESS_DATE,
    )

    lower = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"mall": "omi"}, runtime_root=runtime_root
    )
    upper = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"mall": "OMI"}, runtime_root=runtime_root
    )
    assert lower["pagination"]["total"] == 1  # matched via the cache-row mall
    assert lower["filters_applied"] == {"mall": "omi"}
    assert lower["links"][0]["mall"] == ""  # displayed mall stays the candidate mall
    assert upper["pagination"]["total"] == 1
    miss = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"mall": "stbchina"}, runtime_root=runtime_root
    )
    assert miss["pagination"]["total"] == 1  # the stbchina candidate, not the comix one
    assert miss["links"][0]["mall"] == "stbchina"


def test_filter_status_exact_cache_status(env):
    hits = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"status": "CACHE_HIT"}, runtime_root=env.runtime_root
    )
    assert hits["pagination"]["total"] == 3
    assert all(row["cache_status"] == "CACHE_HIT" for row in hits["links"])
    assert set(hits["links"][0]) == LINK_ROW_KEYS  # whitelist guard incl. no payload_json
    remote = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"status": "REMOTE_DISPATCHED"}, runtime_root=env.runtime_root
    )
    assert remote["pagination"]["total"] == 2


def test_filter_source_matches_current_row_source_class(env):
    daily = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"source": "daily-internet"}, runtime_root=env.runtime_root
    )
    assert daily["pagination"]["total"] == 2
    staples = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"source": "staples-local-prewarm"}, runtime_root=env.runtime_root
    )
    assert staples["pagination"]["total"] == 1
    assert staples["links"][0]["source_class"] == "staples-local-prewarm"
    # Class filter values without any matching candidate → honest empty page.
    for class_value in ("customer-prewarm-zhaoshang", "manual-recrawl"):
        result = projection.list_prewarm_links(
            BUSINESS_DATE, filters={"source": class_value}, runtime_root=env.runtime_root
        )
        assert result["pagination"]["total"] == 0, class_value


def test_filter_missing_title_flags_rows_without_current_title(env):
    missing = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"missing_title": True}, runtime_root=env.runtime_root
    )
    assert missing["pagination"]["total"] == 1  # the UNRESOLVED row recorded with title=None
    assert missing["links"][0]["candidate_id"] == env.ids["unresolved"]
    # falsy values never filter anything (truthy-only flags).
    off = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"missing_title": False}, runtime_root=env.runtime_root
    )
    assert off["pagination"]["total"] == 10


def test_filter_unverified_sku_flags_url_parsed_unverified_rows(env):
    result = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"unverified_sku": True}, runtime_root=env.runtime_root
    )
    assert result["pagination"]["total"] == 1
    assert result["links"][0]["candidate_id"] == env.ids["staples_today"]
    assert result["links"][0]["sku_requires_review"] is True


def test_filter_unresolved_sku_flags_unresolved_rows(env):
    result = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"unresolved_sku": True}, runtime_root=env.runtime_root
    )
    assert result["pagination"]["total"] == 1
    assert result["links"][0]["candidate_id"] == env.ids["unresolved"]


def test_filter_failure_combines_blocked_skip_staples_failed_and_remote_failed(env):
    result = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"failure": True}, runtime_root=env.runtime_root
    )
    got = sorted(row["candidate_id"] for row in result["links"])
    assert got == sorted(
        [
            env.ids["blocked"],
            env.ids["manual_skip"],
            env.ids["staples_failed"],
            env.ids["remote_failed"],
        ]
    )


def test_filter_cache_miss_excludes_cache_hits(env):
    result = projection.list_prewarm_links(
        BUSINESS_DATE, filters={"cache_miss": True}, runtime_root=env.runtime_root
    )
    assert result["pagination"]["total"] == 7
    assert all(row["cache_status"] != "CACHE_HIT" for row in result["links"])


def test_filter_combined_mall_and_unverified_sku(env):
    result = projection.list_prewarm_links(
        BUSINESS_DATE,
        filters={"mall": "stb", "unverified_sku": True},
        runtime_root=env.runtime_root,
    )
    assert result["pagination"]["total"] == 1
    assert result["links"][0]["candidate_id"] == env.ids["staples_today"]
    assert result["filters_applied"] == {"mall": "stb", "unverified_sku": True}


# ---- detail + history ------------------------------------------------------------


def test_detail_history_three_successes_oldest_first_with_single_current(tmp_path):
    runtime_root, prewarm, shared = _small_env(tmp_path)
    url = "https://qx.com/orig-hist.html"
    for index, (succeeded_at, title) in enumerate(
        [
            ("2026-09-06T01:00:00+00:00", "Title-1"),
            ("2026-09-07T01:00:00+00:00", "Title-2"),
            ("2026-09-08T02:00:00+00:00", "Title-3"),
        ],
        start=1,
    ):
        shared.record_success(
            "comix", url, source="daily-internet", succeeded_at=succeeded_at,
            title=title, price=str(index), real_sku_id=f"SKU-{index}",
            sku_status="CONFIRMED",
        )
    upserted = prewarm.upsert_link("D1", url, mall="comix", business_date=BUSINESS_DATE)
    candidate_id = int(upserted["candidate"]["id"])
    current_identity = int(shared.lookup_current("comix", url)["identity_id"])
    prewarm.record_candidate_result(
        candidate_id,
        cache_status=CACHE_STATUS_CACHE_HIT,
        match_level="EXACT",
        resolved_identity_id=current_identity,
    )

    detail = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_id, runtime_root=runtime_root
    )
    assert detail is not None
    history = detail["history"]
    assert [entry["succeeded_at"] for entry in history] == [
        "2026-09-06T01:00:00.000000+00:00",  # store normalizes to microseconds
        "2026-09-07T01:00:00.000000+00:00",
        "2026-09-08T02:00:00.000000+00:00",
    ]  # oldest first
    assert [entry["is_current"] for entry in history] == [False, False, True]
    newest = history[-1]
    assert newest["title"] == "Title-3"
    current = detail["current"]
    assert current is not None
    assert current["title"] == newest["title"]
    assert current["succeeded_at"] == newest["succeeded_at"]
    assert current["price"] == newest["price"]
    assert current["source_class"] == "daily-internet"
    assert current["producer"] is None
    assert current["sku_status_label"] == "已确认"
    assert detail["remote_job"] is None
    assert detail["staples_job"] is None


def test_detail_exposes_full_url_chain_variants(tmp_path):
    runtime_root, prewarm, shared = _small_env(tmp_path)
    row = shared.record_success(
        "comix",
        "https://qx.com/submitted-view.html",
        original_url="https://qx.com/original.html",
        returned_url="https://qx.com/returned.html",
        canonical_url="https://qx.com/canonical.html",
        source="daily-internet",
        succeeded_at=TS_TODAY,
        title="Chain title",
        price="9.9",
        real_sku_id="SKU-CHAIN",
        sku_status="CONFIRMED",
    )
    upserted = prewarm.upsert_link(
        "D1", "https://qx.com/original.html", mall="comix", business_date=BUSINESS_DATE
    )
    candidate_id = int(upserted["candidate"]["id"])
    prewarm.record_candidate_result(
        candidate_id,
        cache_status=CACHE_STATUS_CACHE_HIT,
        resolved_identity_id=int(row["identity_id"]),
    )

    detail = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_id, runtime_root=runtime_root
    )
    current = detail["current"]
    assert current["original_url"] == "https://qx.com/original.html"
    assert current["submitted_url"] == "https://qx.com/submitted-view.html"
    assert current["returned_url"] == "https://qx.com/returned.html"
    assert current["canonical_url"] == "https://qx.com/canonical.html"
    assert current["crawl_id"] is None  # whitelist fields only, payload never projected
    assert detail["history"][0]["is_current"] is True


def test_detail_remote_and_staples_job_blocks(tmp_path):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    db_path = runtime_root / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(db_path)
    StaplesPrewarmStore(db_path)
    candidate_id = _seed_status_candidate(
        prewarm, "D1", "https://qx.com/remote.html", "comix",
        cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=7,
    )
    _make_state_db(runtime_root, jobs=[(7, "RUNNING")])
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute(
            """INSERT INTO staples_prewarm_jobs
               (candidate_id, state, attempt_count, created_at, updated_at)
               VALUES (?, 'RETRY_WAIT', 2, ?, ?)""",
            (candidate_id, TS_TODAY, TS_TODAY),
        )
        connection.commit()
    finally:
        connection.close()

    detail = projection.get_prewarm_link_detail(
        BUSINESS_DATE, candidate_id, runtime_root=runtime_root
    )
    assert detail["remote_job"] == {
        "state": "RUNNING",
        "task_name": "cnnc_current_opt_mallx_00000000000007",
        "remote_identity": None,
        "updated_at": TS_TODAY,
    }
    assert detail["staples_job"] == {
        "state": "RETRY_WAIT",
        "attempt_count": 2,
        "updated_at": TS_TODAY,
    }


def test_detail_unknown_candidate_returns_none(env):
    assert (
        projection.get_prewarm_link_detail(
            BUSINESS_DATE, 999999, runtime_root=env.runtime_root
        )
        is None
    )


# ---- labels ----------------------------------------------------------------------


def test_sku_status_labels_and_requires_review_including_unknown():
    assert projection.sku_status_label("URL_PARSED_UNVERIFIED") == "URL解析·未验证"
    assert projection.sku_requires_review("URL_PARSED_UNVERIFIED") is True
    assert projection.sku_status_label("UNRESOLVED") == "未解析"
    assert projection.sku_requires_review("UNRESOLVED") is True
    assert projection.sku_status_label("CONFIRMED") == "已确认"
    assert projection.sku_requires_review("CONFIRMED") is False
    assert projection.sku_status_label("UPSTREAM_PROVIDED") == "上游提供"
    assert projection.sku_requires_review("UPSTREAM_PROVIDED") is False
    # Unknown values degrade honestly to the raw string and keep the review flag.
    assert projection.sku_status_label("WEIRD") == "WEIRD"
    assert projection.sku_requires_review("WEIRD") is True


# ---- pagination -------------------------------------------------------------------


def test_page_size_clamped_to_200(env):
    result = projection.list_prewarm_links(
        BUSINESS_DATE, page=1, page_size=500, runtime_root=env.runtime_root
    )
    assert result["pagination"]["page_size"] == 200
    assert len(result["links"]) == 10
    assert result["pagination"]["total"] == 10
    paged = projection.list_prewarm_links(
        BUSINESS_DATE, page=2, page_size=3, runtime_root=env.runtime_root
    )
    assert paged["pagination"] == {"page": 2, "page_size": 3, "total": 10, "total_pages": 4}
    assert len(paged["links"]) == 3
    got_ids = [row["candidate_id"] for row in paged["links"]]
    assert got_ids == sorted(got_ids)  # stable candidate_id order


# ---- degraded read (locked/unreadable store) ---------------------------------------


def test_degraded_on_store_read_error_returns_payload_with_note(monkeypatch, tmp_path):
    runtime_root, prewarm, shared = _small_env(tmp_path)
    original_connect = projection._connect_ro

    def flaky_connect(path):
        if "customer_prewarm" in str(path):
            raise sqlite3.OperationalError("database is locked")
        return original_connect(path)

    monkeypatch.setattr(projection, "_connect_ro", flaky_connect)

    summary = projection.build_prewarm_dashboard_summary(BUSINESS_DATE, runtime_root=runtime_root)
    assert summary["degraded"] is True
    assert any("prewarm_store" in reason for reason in summary["degraded_reasons"])
    assert summary["available"]["prewarm_store"] is False
    assert summary["available"]["shared_cache"] is True
    assert summary["kpi"]["reference_link_count"] == 0
    assert summary["kpi"]["candidate_count"] == 0

    links = projection.list_prewarm_links(BUSINESS_DATE, runtime_root=runtime_root)
    assert links["links"] == []
    assert links["degraded"] is True

    detail = projection.get_prewarm_link_detail(BUSINESS_DATE, 1, runtime_root=runtime_root)
    assert detail is None  # candidate cannot be proven without the prewarm store


# ---- zero-write discipline on populated stores ---------------------------------------


def test_read_paths_do_not_modify_store_files(env):
    before = {
        path.name: path.read_bytes()
        for path in (env.prewarm_db, env.cache_db, env.state_db)
    }
    projection.build_prewarm_dashboard_summary(BUSINESS_DATE, runtime_root=env.runtime_root)
    projection.list_prewarm_links(BUSINESS_DATE, page=1, runtime_root=env.runtime_root)
    projection.list_prewarm_links(BUSINESS_DATE, page=2, page_size=3, runtime_root=env.runtime_root)
    projection.get_prewarm_link_detail(
        BUSINESS_DATE, env.ids["staples_today"], runtime_root=env.runtime_root
    )
    after = {
        path.name: path.read_bytes()
        for path in (env.prewarm_db, env.cache_db, env.state_db)
    }
    assert after == before
