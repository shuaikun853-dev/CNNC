"""customer_prewarm_store 行为测试(Issue #9 Phase 1)。

覆盖:初始化幂等、document 幂等与 category 重置、pending 过滤、detail 成功/
失败路径、link/candidate 幂等与跨商城复用、状态迁移、ledger 幂等与崩溃恢复、
stats、WAL/busy 冒烟。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app.services.customer_prewarm_store import CustomerPrewarmStore

EXPECTED_TABLES = {
    "prewarm_documents",
    "prewarm_candidates",
    "prewarm_links",
    "prewarm_sync_ledger",
}


@pytest.fixture()
def store(tmp_path):
    return CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")


class _FakeClock:
    """受控时钟:每次调用返回递增的稳定 ISO 时间戳。"""

    def __init__(self) -> None:
        self._step = 0

    def __call__(self) -> str:
        self._step += 1
        return f"2026-09-07T00:00:{self._step:02d}.000000+00:00"


@pytest.fixture()
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(CustomerPrewarmStore, "_utcnow", staticmethod(fake))
    return fake


def _doc_row(store, document_id):
    with store._reader() as connection:
        row = connection.execute(
            "SELECT * FROM prewarm_documents WHERE document_id=?", (document_id,)
        ).fetchone()
    return dict(row) if row is not None else None


# ---- 初始化 ------------------------------------------------------------------


def test_init_idempotent_table_names_stable(tmp_path):
    db = tmp_path / "prewarm.sqlite3"
    first = CustomerPrewarmStore(db)
    assert first.table_names() == EXPECTED_TABLES
    first.upsert_document("2026-09-07", "D1", "C1")
    second = CustomerPrewarmStore(db)
    assert second.table_names() == EXPECTED_TABLES
    assert [r["document_id"] for r in second.pending_documents(10)] == ["D1"]


def test_init_never_touches_sibling_files(tmp_path):
    sibling = tmp_path / "sibling.sqlite3"
    sibling.write_text("do-not-touch", encoding="utf-8")
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    store.upsert_document("2026-09-07", "D1", "C1")
    store.upsert_link("D1", "https://x.example/1")
    assert sibling.read_text(encoding="utf-8") == "do-not-touch"


# ---- documents ----------------------------------------------------------------


def test_upsert_document_idempotent_and_source_priority(store, clock):
    first = store.upsert_document("2026-09-07", "D1", "C1")
    second = store.upsert_document("2026-09-07", "D1", "C1")
    rows = store.pending_documents(10)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == first["id"] == second["id"]
    assert row["first_seen_at"] == first["first_seen_at"]
    assert row["last_seen_at"] == "2026-09-07T00:00:02.000000+00:00"  # last_seen 被刷新
    assert row["source"] == "event"
    # scanner 复见:source 保持 event,business_date 保持首见日
    rescanned = store.upsert_document("2026-09-08", "D1", "C1", source="scanner")
    assert rescanned["source"] == "event"
    assert rescanned["business_date"] == "2026-09-07"
    # scanner 首见之后 event 到达:source 升级为 event
    store.upsert_document("2026-09-07", "D2", "C1", source="scanner")
    upgraded = store.upsert_document("2026-09-07", "D2", "C1", source="event")
    assert upgraded["source"] == "event"


def test_same_category_reseen_does_not_reset_but_category_change_does(store):
    store.upsert_document("2026-09-07", "D1", "C1")
    store.record_document_detail_failure("D1", "boom")
    store.record_document_detail_failure("D1", "boom again")
    store.record_document_detail_success("D1")
    # 同 category 复见:不重置(DONE 保持)
    same = store.upsert_document("2026-09-07", "D1", "C1")
    assert same["detail_state"] == "DONE"
    assert same["detail_attempts"] == 2
    assert same["four_category_id"] == "C1"
    # category 变化:重置 PENDING + attempts 归零 + last_error 清空
    changed = store.upsert_document("2026-09-07", "D1", "C2")
    assert changed["four_category_id"] == "C2"
    assert changed["detail_state"] == "PENDING"
    assert changed["detail_attempts"] == 0
    assert changed["last_error"] is None
    assert [r["document_id"] for r in store.pending_documents(10)] == ["D1"]


def test_empty_to_nonempty_category_resets(store):
    store.upsert_document("2026-09-07", "D1")
    assert store.pending_documents(10) == []  # 无 category 永远不可 detail
    upgraded = store.upsert_document("2026-09-07", "D1", "C9")
    assert upgraded["four_category_id"] == "C9"
    assert upgraded["detail_state"] == "PENDING"
    assert [r["document_id"] for r in store.pending_documents(10)] == ["D1"]


def test_pending_documents_filters_order_and_limit(store):
    store.upsert_document("2026-09-07", "D1", "C1")
    store.upsert_document("2026-09-07", "D2", "C2")
    store.upsert_document("2026-09-07", "D3", "C3")
    store.upsert_document("2026-09-07", "D4", "C4")
    store.upsert_document("2026-09-07", "D5")  # 无 category → 永远排除
    store.record_document_detail_success("D2")  # DONE → 排除
    for _ in range(5):
        store.record_document_detail_failure("D3", "x")  # FAILED → 排除
    store.record_document_detail_failure("D4", "x")
    store.record_document_detail_failure("D4", "x")  # attempts=2
    # D5 无 category → 永远排除
    assert [r["document_id"] for r in store.pending_documents(10)] == ["D1", "D4"]
    assert [r["document_id"] for r in store.pending_documents(10, max_attempts=2)] == ["D1"]
    assert [r["document_id"] for r in store.pending_documents(1)] == ["D1"]  # limit + id 升序


def test_failure_then_success_lifecycle(store):
    store.upsert_document("2026-09-07", "D1", "C1")
    store.record_document_detail_failure("D1", "网络超时")
    row = store.pending_documents(10)[0]
    assert row["detail_attempts"] == 1
    assert row["last_error"] == "网络超时"
    assert row["last_checked_at"] is not None
    store.record_document_detail_success("D1")
    assert store.pending_documents(10) == []
    stored = _doc_row(store, "D1")
    assert stored["detail_state"] == "DONE"
    assert stored["last_error"] is None
    assert stored["last_checked_at"] is not None


def test_failure_at_max_attempts_becomes_failed(store):
    store.upsert_document("2026-09-07", "D1", "C1")
    for attempt in range(1, 6):
        store.record_document_detail_failure("D1", f"e{attempt}")
        if attempt < 5:
            assert [r["document_id"] for r in store.pending_documents(10, max_attempts=5)] == ["D1"]
    stored = _doc_row(store, "D1")
    assert stored["detail_state"] == "FAILED"
    assert stored["detail_attempts"] == 5
    # FAILED 终态:同 category 复见不复活;category 变化才重置
    assert store.upsert_document("2026-09-07", "D1", "C1")["detail_state"] == "FAILED"
    assert store.upsert_document("2026-09-07", "D1", "C2")["detail_state"] == "PENDING"


def test_success_sets_done_regardless_of_attempts(store):
    store.upsert_document("2026-09-07", "D1", "C1")
    for attempt in range(4):
        store.record_document_detail_failure("D1", f"e{attempt}")
    store.record_document_detail_success("D1")
    stored = _doc_row(store, "D1")
    assert stored["detail_state"] == "DONE"
    assert stored["detail_attempts"] == 4


# ---- links / candidates -------------------------------------------------------


def test_upsert_link_defaults_and_idempotency(store):
    first = store.upsert_link("D1", "https://x.example/1")
    candidate, link = first["candidate"], first["link"]
    assert candidate["mall"] == ""
    assert candidate["original_url"] == "https://x.example/1"
    assert candidate["lookup_url"] == "https://x.example/1"  # 缺省 = original_url
    assert candidate["cache_status"] == "PENDING_LOOKUP"
    assert candidate["routing_url"] is None
    assert link["candidate_id"] == candidate["id"]
    assert link["document_id"] == "D1"
    assert link["source"] == "detail"
    # 重复 upsert:仍是 1 link 1 candidate
    second = store.upsert_link("D1", "https://x.example/1")
    assert second["candidate"]["id"] == candidate["id"]
    assert second["link"]["id"] == link["id"]
    assert len(store.links_for_document("D1")) == 1
    assert len(store.candidates()) == 1


def test_upsert_link_stores_routing_url(store):
    result = store.upsert_link(
        "D1", "https://www.jslink.com/p/1", routing_url="https://b2b.nbdeli.com/p/1"
    )
    assert result["candidate"]["original_url"] == "https://www.jslink.com/p/1"
    assert result["candidate"]["routing_url"] == "https://b2b.nbdeli.com/p/1"


def test_three_documents_share_one_candidate(store):
    candidate_ids = []
    for document_id in ("D1", "D2", "D3"):
        result = store.upsert_link(document_id, "https://shared.example/1")
        candidate_ids.append(result["candidate"]["id"])
    assert candidate_ids[0] == candidate_ids[1] == candidate_ids[2]
    assert len(store.links_for_document("D1")) == 1
    links = store.links_for_candidate(candidate_ids[0])
    assert [link["document_id"] for link in links] == ["D1", "D2", "D3"]


def test_cross_mall_reuse_updates_mall_for_non_hit(store):
    first = store.upsert_link("D1", "https://x.example/1", mall="a")
    candidate_id = first["candidate"]["id"]
    second = store.upsert_link("D2", "https://x.example/1", mall="b")
    assert second["candidate"]["id"] == candidate_id  # 同一 candidate 被复用
    assert second["candidate"]["mall"] == "b"
    assert second["candidate"]["cache_status"] == "PENDING_LOOKUP"
    assert len(store.candidates()) == 1


def test_cross_mall_reuse_never_touches_cache_hit(store):
    first = store.upsert_link(
        "D1", "https://y.example/1", mall="a", routing_url="https://r.example/1"
    )
    candidate_id = first["candidate"]["id"]
    store.record_candidate_result(
        candidate_id, cache_status="CACHE_HIT", match_level="EXACT", resolved_identity_id=42
    )
    before = store.candidates()[0]
    second = store.upsert_link("D2", "https://y.example/1", mall="b")
    after = second["candidate"]
    assert after["id"] == candidate_id
    assert after["mall"] == "a"  # CACHE_HIT 行不被改道
    assert after["routing_url"] == "https://r.example/1"
    assert after == before
    # link 仍然挂到同一 candidate
    assert [link["document_id"] for link in store.links_for_candidate(candidate_id)] == ["D1", "D2"]
    assert second["link"]["mall"] == "b"


# ---- candidate 状态迁移 --------------------------------------------------------


def test_record_candidate_result_transitions(store):
    candidate_id = store.upsert_link("D1", "https://x.example/1")["candidate"]["id"]

    miss = store.record_candidate_result(candidate_id, cache_status="PENDING_PREWARM", error="cache miss")
    assert miss["cache_status"] == "PENDING_PREWARM"
    assert miss["last_error"] == "cache miss"

    hit = store.record_candidate_result(
        candidate_id, cache_status="CACHE_HIT", match_level="MALL_PARSER", resolved_identity_id=7
    )
    assert hit["cache_status"] == "CACHE_HIT"
    assert hit["match_level"] == "MALL_PARSER"
    assert hit["resolved_identity_id"] == 7
    assert hit["last_error"] is None  # 命中清空 last_error

    # 同状态幂等:仅刷新 last_checked_at,其他字段不动
    again = store.record_candidate_result(candidate_id, cache_status="CACHE_HIT")
    assert again["cache_status"] == "CACHE_HIT"
    assert again["match_level"] == "MALL_PARSER"
    assert again["last_checked_at"] >= hit["last_checked_at"]

    # 命中是终态:回退必须 ValueError,且状态不被部分改写
    with pytest.raises(ValueError):
        store.record_candidate_result(candidate_id, cache_status="PENDING_PREWARM")
    assert store.candidates()[0]["cache_status"] == "CACHE_HIT"


def test_record_candidate_result_rejects_bad_transitions(store):
    candidate_id = store.upsert_link("D1", "https://x.example/1")["candidate"]["id"]
    with pytest.raises(ValueError):
        store.record_candidate_result(candidate_id, cache_status="NONSENSE")
    with pytest.raises(ValueError):
        store.record_candidate_result(999, cache_status="CACHE_HIT")
    store.record_candidate_result(candidate_id, cache_status="PENDING_PREWARM")
    with pytest.raises(ValueError):
        store.record_candidate_result(candidate_id, cache_status="PENDING_LOOKUP")
    assert store.candidates()[0]["cache_status"] == "PENDING_PREWARM"


def test_list_unique_cache_misses_only_pending_prewarm(store):
    hit = store.upsert_link("D1", "https://hit.example/1")["candidate"]["id"]
    miss = store.upsert_link("D2", "https://miss.example/1")["candidate"]["id"]
    lookup = store.upsert_link("D3", "https://lookup.example/1")["candidate"]["id"]
    store.record_candidate_result(
        hit, cache_status="CACHE_HIT", match_level="EXACT", resolved_identity_id=1
    )
    store.record_candidate_result(miss, cache_status="PENDING_PREWARM", error="miss")
    misses = store.list_unique_cache_misses()
    assert [candidate["id"] for candidate in misses] == [miss]
    assert all(candidate["cache_status"] == "PENDING_PREWARM" for candidate in misses)
    assert [candidate["id"] for candidate in store.candidates(cache_status="PENDING_LOOKUP")] == [lookup]


# ---- ledger --------------------------------------------------------------------


def test_ledger_claim_complete_and_recovery(store, clock):
    source = "daily-internet"
    claim = store.ledger_claim(source, "evt-1")
    assert claim["cache_result_id"] is None
    assert claim["error"] is None
    # 幂等:重复 claim 返回原行,created_at 不变
    assert store.ledger_claim(source, "evt-1") == claim

    store.ledger_complete(source, "evt-1", cache_result_id=123)
    done = store.ledger_get(source, "evt-1")
    assert done["cache_result_id"] == 123
    assert done["error"] is None

    # 确定性失败路径
    store.ledger_claim(source, "evt-2")
    store.ledger_complete(source, "evt-2", error="ValueError: alias conflict")
    failed = store.ledger_get(source, "evt-2")
    assert failed["cache_result_id"] is None
    assert failed["error"] == "ValueError: alias conflict"

    # 未 finalize 的 claim 出现在崩溃恢复探测里(按 created_at 过滤)
    store.ledger_claim(source, "evt-3")
    pending = store.ledger_pending_claims(source, older_than="2026-09-07T00:00:09.000000+00:00")
    assert [row["source_event_key"] for row in pending] == ["evt-3"]

    # 其他 source 不可见
    assert store.ledger_pending_claims("other-source", older_than="2026-09-07T00:00:09.000000+00:00") == []

    # drop_claim:仅未 finalize 的可删;finalize 行是 append-only 历史,删不掉
    store.ledger_drop_claim(source, "evt-3")
    assert store.ledger_get(source, "evt-3") is None
    store.ledger_drop_claim(source, "evt-1")
    assert store.ledger_get(source, "evt-1")["cache_result_id"] == 123
    # 不存在的 key:drop 无副作用,complete 报 KeyError
    store.ledger_drop_claim(source, "never-existed")
    with pytest.raises(KeyError):
        store.ledger_complete(source, "never-existed", cache_result_id=1)


# ---- stats ----------------------------------------------------------------------


def test_stats_counts(store):
    store.upsert_document("2026-09-07", "D1")  # PENDING,无 category
    store.upsert_document("2026-09-07", "D2", "C1")  # PENDING
    store.upsert_document("2026-09-07", "D3", "C2")
    store.upsert_document("2026-09-07", "D4", "C3")
    store.record_document_detail_success("D3")  # DONE
    for _ in range(5):
        store.record_document_detail_failure("D4", "x")  # FAILED
    hit = store.upsert_link("D2", "https://hit.example/1", mall="m")["candidate"]["id"]
    miss = store.upsert_link("D2", "https://miss.example/1")["candidate"]["id"]
    store.upsert_link("D3", "https://lookup.example/1")
    store.upsert_link("D4", "https://hit.example/1", mall="m")  # 与 D2 共享 hit candidate
    store.record_candidate_result(
        hit, cache_status="CACHE_HIT", match_level="EXACT", resolved_identity_id=3
    )
    store.record_candidate_result(miss, cache_status="PENDING_PREWARM", error="miss")
    store.ledger_claim("daily-internet", "evt-ok")
    store.ledger_complete("daily-internet", "evt-ok", cache_result_id=9)
    store.ledger_claim("daily-internet", "evt-bad")
    store.ledger_complete("daily-internet", "evt-bad", error="boom")
    store.ledger_claim("daily-internet", "evt-pending")

    stats = store.stats()
    assert stats["documents"] == {"PENDING": 2, "DONE": 1, "FAILED": 1}
    assert stats["candidates"] == {"PENDING_LOOKUP": 1, "PENDING_PREWARM": 1, "CACHE_HIT": 1}
    assert stats["links"] == 4
    assert stats["ledger"] == {"pending": 1, "completed": 1, "errored": 1}
    assert stats["candidates_with_multiple_documents"] == 1


# ---- 校验 ------------------------------------------------------------------------


def test_validation_rejects_empty_required_fields(store):
    with pytest.raises(ValueError):
        store.upsert_document("2026-09-07", "  ")
    with pytest.raises(ValueError):
        store.upsert_document("  ", "D1")
    with pytest.raises(ValueError):
        store.upsert_link("D1", "  ")
    with pytest.raises(ValueError):
        store.upsert_link("  ", "https://x.example/1")
    with pytest.raises(ValueError):
        store.ledger_claim("daily-internet", "  ")


# ---- WAL / busy 冒烟 ---------------------------------------------------------------


def test_two_instances_short_busy_lock_smoke(tmp_path):
    db = tmp_path / "prewarm.sqlite3"
    store_a = CustomerPrewarmStore(db)
    store_a.upsert_document("2026-09-07", "D1", "C1")

    holder = sqlite3.connect(str(db), isolation_level=None, check_same_thread=False)
    holder.execute("PRAGMA busy_timeout = 5000")
    holder.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(0.3, holder.execute, args=("COMMIT",))
    timer.start()
    try:
        # store_b 的初始化/写入会等锁,busy_timeout 内持锁释放后应成功
        store_b = CustomerPrewarmStore(db)
        store_b.upsert_document("2026-09-07", "D2", "C1")
    finally:
        timer.join()
        holder.close()

    expected = ["D1", "D2"]
    assert [row["document_id"] for row in store_a.pending_documents(10)] == expected
    assert [row["document_id"] for row in store_b.pending_documents(10)] == expected
    check = sqlite3.connect(str(db))
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
