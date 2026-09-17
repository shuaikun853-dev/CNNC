from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3
from decimal import Decimal
from zipfile import ZipFile

import pytest
from openpyxl import Workbook

from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_local_fallback import (
    AFTER_1800,
    DAYTIME_60M,
    PEAK_3M,
    SUPPORTED_MALLS,
    eligibility_reason,
    run_incremental_local_fallback,
    read_crawler_output,
)
from app.workflows.incremental_price_audit import (
    TickBudget, TickConfig, TickDependencies, _backlog,
    _final_remote_check_for_local_fallback, _harvest,
)


def at(hour, minute=0, second=0):
    return datetime(2026, 9, 17, hour, minute, second, tzinfo=UTC)


@pytest.mark.parametrize(
    "now,started,last_check,expected",
    [
        (at(8, 59, 59), at(8), None, None),       # 16:59:59 CST, 59m59s
        (at(9), at(8), None, None),               # 17:00 CST is peak, no prior peak check
        (at(8, 59, 59), at(7, 59, 59), None, DAYTIME_60M),
        (at(9), at(7), None, None),
        (at(9, 2, 59), at(7), at(9), None),
        (at(9, 3), at(7), at(9), PEAK_3M),
        (at(9, 57), at(7), at(9, 54), PEAK_3M),
        (at(9, 59, 59), at(7), at(9, 56, 59), PEAK_3M),
        (at(10), at(9, 59), None, AFTER_1800),
    ],
)
def test_time_policy_boundaries(now, started, last_check, expected):
    assert eligibility_reason(now, started, last_check) == expected


def setup_work(tmp_path, *, mall="nbdeli", count=1, state="SUBMITTED", started=at(7)):
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    store.ensure_run("2026-09-17")
    token = store.acquire_lease("2026-09-17", "test", ttl_seconds=600)
    job = store.prepare_remote_intent(token, "job", "task", "hash", mall_key=mall)
    rows = []
    for index in range(count):
        work = store.ensure_crawl_work(
            token, mall, f"sku-{index}", f"https://{mall}.test/item/{index}",
            "legacy-v1", f"fp-{index}", source_id=f"doc-{index}",
        )
        store.link_crawl_work_to_remote_job(token, work["id"], job["id"])
        rows.append(work)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE remote_jobs SET state=?, created_at=? WHERE id=?", (state, started.isoformat(), job["id"]))
        conn.execute("UPDATE crawl_work SET state=? WHERE remote_job_id=?", (state, job["id"]))
    return store, token, rows, job


def success_result(price="12.30"):
    return {
        "page_price_value": Decimal(price),
        "page_price_status": "OK" if Decimal(price) else "PRICE_IS_ZERO",
        "page_price_source": "本地兜底:nbdeli",
    }


def checked_jobs(candidates):
    return {int(candidate["remote_job_id"]) for candidate in candidates}


def test_schema_and_atomic_claim_allow_only_one_owner(tmp_path):
    store, token, rows, _job = setup_work(tmp_path)
    observed = store.observe_local_fallback_remote_checks(
        token, at(8, 30).isoformat(), SUPPORTED_MALLS, checked_remote_job_ids=set()
    )
    assert observed[0]["last_remote_check_at"] is None
    first = store.claim_local_fallback(token, rows[0]["id"], DAYTIME_60M, "tick-a", at(8, 30).isoformat())
    second = store.claim_local_fallback(token, rows[0]["id"], DAYTIME_60M, "tick-b", at(8, 30).isoformat())
    assert first is not None and second is None


def test_peak_first_check_then_second_check_runs_once(tmp_path):
    store, token, rows, _job = setup_work(tmp_path)
    calls = []
    first = run_incremental_local_fallback(
        store, token, now=at(9), tick_id="17:00",
        final_remote_check=lambda candidates: calls.append("remote") or checked_jobs(candidates),
        execute=lambda claims: calls.append("local") or {},
    )
    second = run_incremental_local_fallback(
        store, token, now=at(9, 3), tick_id="17:03",
        final_remote_check=lambda candidates: calls.append("remote") or checked_jobs(candidates),
        execute=lambda claims: {claims[0]["id"]: {"result": success_result()}},
    )
    assert first["pending"] == 1 and "local" not in calls
    assert second["success"] == 1 and calls.count("remote") == 2
    assert store.get_crawl_work(rows[0]["id"])["state"] == "SUCCESS"


def test_peak_timestamp_requires_completed_check_for_exact_job(tmp_path):
    store, token, _rows, job = setup_work(tmp_path)
    run_incremental_local_fallback(
        store, token, now=at(9), tick_id="failed-check",
        final_remote_check=lambda _candidates: set(), execute=lambda _claims: {},
    )
    ledger = store.list_local_fallback_work(token)[0]
    assert ledger["last_remote_check_at"] is None

    run_incremental_local_fallback(
        store, token, now=at(9, 3), tick_id="actual-check",
        final_remote_check=lambda _candidates: {int(job["id"])}, execute=lambda _claims: {},
    )
    ledger = store.list_local_fallback_work(token)[0]
    assert ledger["last_remote_check_at"] == at(9, 3).isoformat()


def test_failed_final_remote_check_neither_stamps_nor_falls_back(tmp_path):
    store, token, rows, _job = setup_work(tmp_path)
    local_calls = []

    def failed_check(_candidates):
        raise TimeoutError("remote query timed out")

    metrics = run_incremental_local_fallback(
        store, token, now=at(8, 30), tick_id="failed-final-check",
        final_remote_check=failed_check,
        execute=lambda claims: local_calls.append(claims) or {},
    )
    assert metrics["pending"] == 1
    assert local_calls == []
    assert store.list_local_fallback_work(token)[0]["last_remote_check_at"] is None
    assert store.get_crawl_work(rows[0]["id"])["state"] == "SUBMITTED"


def test_final_remote_result_wins_before_local_claim(tmp_path):
    store, token, rows, _job = setup_work(tmp_path)
    local_calls = []
    def remote(_candidates):
        result = success_result("9.99")
        result["page_price_source"] = "招商局"
        store.complete_crawl_work(token, rows[0]["id"], result)
        return checked_jobs(_candidates)
    metrics = run_incremental_local_fallback(
        store, token, now=at(8, 30), tick_id="day", final_remote_check=remote,
        execute=lambda claims: local_calls.append(claims) or {},
    )
    assert metrics["success"] == 0 and metrics["remote_success"] == 1 and local_calls == []
    assert json.loads(store.get_crawl_work(rows[0]["id"])["result_json"])["page_price_source"] == "招商局"


def test_partial_job_only_unresolved_urls_are_executed(tmp_path):
    store, token, rows, _job = setup_work(tmp_path, count=20)
    for row in rows[:15]:
        store.complete_crawl_work(token, row["id"], success_result())
    received = []
    def execute(claims):
        received.extend(claims)
        return {claim["id"]: {"result": success_result("4.56")} for claim in claims}
    metrics = run_incremental_local_fallback(
        store, token, now=at(8, 30), tick_id="partial", final_remote_check=checked_jobs,
        execute=execute,
    )
    assert metrics["success"] == 5
    assert len(received) == 5
    assert len({claim["exact_url"] for claim in received}) == 5


def test_duplicate_exact_url_is_claimed_once_and_fans_out(tmp_path):
    store, token, rows, job = setup_work(tmp_path)
    duplicate = store.ensure_crawl_work(
        token, "nbdeli", "sku-0", rows[0]["exact_url"], "legacy-v1", "other-fp", source_id="doc-other"
    )
    store.link_crawl_work_to_remote_job(token, duplicate["id"], job["id"])
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE crawl_work SET state='SUBMITTED' WHERE id=?", (duplicate["id"],))
    received = []
    metrics = run_incremental_local_fallback(
        store, token, now=at(8, 30), tick_id="dedupe", final_remote_check=checked_jobs,
        execute=lambda claims: received.extend(claims) or {claims[0]["id"]: {"result": success_result()}},
    )
    assert len(received) == 1
    assert store.get_crawl_work(rows[0]["id"])["state"] == "SUCCESS"
    assert store.get_crawl_work(duplicate["id"])["state"] == "SUCCESS"
    assert metrics["pending"] == 0 and metrics["success"] == 1 and metrics["total"] == 1
    assert metrics["links"] == [{
        "mall_key": "nbdeli", "exact_url": rows[0]["exact_url"], "state": "SUCCESS",
        "source": "本地兜底:nbdeli", "remote_job_id": job["id"],
    }]


def test_late_remote_cannot_overwrite_local_success(tmp_path):
    store, token, rows, _job = setup_work(tmp_path)
    run_incremental_local_fallback(
        store, token, now=at(8, 30), tick_id="local", final_remote_check=checked_jobs,
        execute=lambda claims: {claims[0]["id"]: {"result": success_result("4.56")}},
    )
    with pytest.raises(Exception, match="immutable"):
        store.complete_crawl_work(token, rows[0]["id"], success_result("99.99"))


def test_opening_harvest_ignores_late_remote_after_local_success(tmp_path):
    store, token, rows, job = setup_work(tmp_path)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE remote_jobs SET remote_identity='remote-job', post_attempted=1 WHERE id=?",
            (job["id"],),
        )
    run_incremental_local_fallback(
        store, token, now=at(8, 30), tick_id="local", final_remote_check=checked_jobs,
        execute=lambda claims: {claims[0]["id"]: {"result": success_result("4.56")}},
    )
    remote = {"page_price_value": "99.99", "page_price_status": "OK", "page_price_source": "招商局"}
    deps = TickDependencies(
        discover=lambda _timeout: None,
        harvest=lambda _jobs, _timeout: {
            int(job["id"]): {
                "state": "COMPLETED", "remote_identity": "remote-job",
                "crawl_results": {int(rows[0]["id"]): remote},
            }
        },
        monotonic=lambda: 0,
    )
    counters = {"crawl_harvested": 0}
    checked = _harvest(
        store, token, deps, TickBudget(0, 60, 5, lambda: 0), counters,
        TickConfig(business_date="2026-09-17"),
    )
    assert checked == {int(job["id"])}
    assert counters["late_remote_ignored"] == 1
    assert json.loads(store.get_crawl_work(rows[0]["id"])["result_json"])["page_price_value"] == "4.56"


def test_remote_check_proof_requires_per_job_evidence(tmp_path):
    store, token, rows, job = setup_work(tmp_path)
    deps = TickDependencies(
        discover=lambda _timeout: None,
        harvest=lambda _jobs, _timeout: {},
        monotonic=lambda: 0,
    )
    checked: set[int] = set()
    swept = _harvest(
        store, token, deps, TickBudget(0, 60, 5, lambda: 0),
        {"crawl_harvested": 0}, TickConfig(business_date="2026-09-17"),
        checked_job_ids=checked,
    )
    assert swept == {int(job["id"])} and checked == set()
    assert _final_remote_check_for_local_fallback(
        store, token, deps, TickBudget(0, 60, 5, lambda: 0),
        [store.get_crawl_work(rows[0]["id"])],
    ) == set()


def test_stale_submit_unknown_does_not_block_after_local_success(tmp_path):
    store, token, rows, job = setup_work(tmp_path, state="SUBMIT_UNKNOWN")
    run_incremental_local_fallback(
        store, token, now=at(8, 30), tick_id="local", final_remote_check=checked_jobs,
        execute=lambda claims: {claims[0]["id"]: {"result": success_result("4.56")}},
    )
    assert store.get_remote_job(job["id"])["state"] == "SUBMIT_UNKNOWN"
    backlog = _backlog(store, token, [])
    assert backlog["remote_unknown"] == 0
    assert backlog["crawl_pending"] == 0


def test_mixed_remote_and_local_success_does_not_keep_job_pending(tmp_path):
    store, token, rows, job = setup_work(tmp_path, count=2, state="SUBMIT_UNKNOWN")
    store.complete_crawl_work(token, rows[0]["id"], {
        "page_price_value": "1.23", "page_price_status": "OK",
        "page_price_source": "招商局",
    })
    store.complete_crawl_work(token, rows[1]["id"], success_result("4.56"))

    assert store.get_remote_job(job["id"])["state"] == "SUBMIT_UNKNOWN"
    assert _backlog(store, token, [])["remote_unknown"] == 0


@pytest.mark.parametrize("bad_price", ["not-a-price", "NaN", "Infinity"])
def test_malformed_sibling_success_keeps_stale_remote_job_pending(tmp_path, bad_price):
    store, token, rows, job = setup_work(tmp_path, count=2, state="SUBMIT_UNKNOWN")
    store.complete_crawl_work(token, rows[0]["id"], {
        "page_price_value": bad_price, "page_price_status": "OK",
        "page_price_source": "招商局",
    })
    store.complete_crawl_work(token, rows[1]["id"], success_result("4.56"))

    assert store.get_remote_job(job["id"])["state"] == "SUBMIT_UNKNOWN"
    assert _backlog(store, token, [])["remote_unknown"] == 1


def test_retryable_failure_waits_for_future_tick_and_caps_at_two(tmp_path):
    store, token, rows, _job = setup_work(tmp_path)
    calls = []
    def fail(claims):
        calls.append(claims[0]["claim_tick_id"])
        return {claims[0]["id"]: {"error": "NETWORK_ERROR", "retryable": True}}
    one = run_incremental_local_fallback(store, token, now=at(8, 30), tick_id="a", final_remote_check=checked_jobs, execute=fail)
    same = run_incremental_local_fallback(store, token, now=at(8, 31), tick_id="a", final_remote_check=checked_jobs, execute=fail)
    two = run_incremental_local_fallback(store, token, now=at(8, 32), tick_id="b", final_remote_check=checked_jobs, execute=fail)
    three = run_incremental_local_fallback(store, token, now=at(8, 33), tick_id="c", final_remote_check=checked_jobs, execute=fail)
    assert [one["failed"], same["failed"], two["failed"], three["failed"]] == [1, 1, 1, 1]
    assert calls == ["a", "b"]
    ledger = store.list_local_fallback_work(token)[0]
    assert ledger["attempt_count"] == 2 and ledger["state"] == "TERMINAL_FAILED"


def test_unsupported_mall_and_completed_are_never_candidates(tmp_path):
    store, token, rows, _job = setup_work(tmp_path, mall="other")
    assert run_incremental_local_fallback(
        store, token, now=at(10), tick_id="x", final_remote_check=checked_jobs,
        execute=lambda c: pytest.fail("must not execute"),
    )["total"] == 0


def test_historical_replay_candidate_counts_are_unique(tmp_path):
    store, token, rows, _job = setup_work(tmp_path, count=171, started=at(6))
    received = []
    run_incremental_local_fallback(
        store, token, now=at(8), tick_id="2016-09-15", final_remote_check=checked_jobs,
        execute=lambda claims: received.extend(claims) or {
            claim["id"]: {"error": "RISK_STOP", "retryable": False} for claim in claims
        },
    )
    assert len(received) == 171
    assert len({claim["exact_url"] for claim in received}) == 171


def test_20260916_replay_manual_qstbg_does_not_wait_forever(tmp_path):
    store, token, _rows, _job = setup_work(
        tmp_path, mall="qstbg", count=50, state="MANUAL_REQUIRED", started=at(7)
    )
    received = []
    run_incremental_local_fallback(
        store, token, now=at(8, 30), tick_id="2026-09-16", final_remote_check=checked_jobs,
        execute=lambda claims: received.extend(claims) or {
            claim["id"]: {"error": "RISK_STOP", "retryable": False} for claim in claims
        },
    )
    assert len(received) == 50
    assert {claim["eligible_reason"] for claim in received} == {DAYTIME_60M}


def write_output(path, rows):
    book = Workbook()
    sheet = book.active
    sheet.append(["商城商品编码", "商品标题", "商品价格", "商品链接", "采集状态", "失败原因"])
    for row in rows:
        sheet.append(row)
    book.save(path)


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-1"])
def test_crawler_output_rejects_non_finite_and_negative_prices(tmp_path, price):
    path = tmp_path / "out.xlsx"
    url = "https://b2b.nbdeli.com/pcweb/detail?sku=1"
    write_output(path, [["1", "item", price, url, "SUCCESS", ""]])
    with pytest.raises(ValueError, match="价格"):
        read_crawler_output(path, "nbdeli", [url])


def test_crawler_output_maps_zero_and_rejects_duplicate_foreign_or_missing(tmp_path):
    url = "https://b2b.nbdeli.com/pcweb/detail?sku=1"
    path = tmp_path / "zero.xlsx"
    write_output(path, [["1", "item", 0, url, "SUCCESS", ""]])
    assert read_crawler_output(path, "nbdeli", [url])[url]["result"]["page_price_status"] == "PRICE_IS_ZERO"

    duplicate = tmp_path / "duplicate.xlsx"
    write_output(duplicate, [["1", "item", 1, url, "SUCCESS", ""], ["1", "item", 1, url, "SUCCESS", ""]])
    with pytest.raises(ValueError, match="重复"):
        read_crawler_output(duplicate, "nbdeli", [url])

    foreign = tmp_path / "foreign.xlsx"
    write_output(foreign, [["1", "item", 1, "https://evil.example/1", "SUCCESS", ""]])
    with pytest.raises(ValueError, match="站点|锁定"):
        read_crawler_output(foreign, "nbdeli", [url])

    missing = tmp_path / "missing.xlsx"
    write_output(missing, [])
    with pytest.raises(ValueError, match="锁定"):
        read_crawler_output(missing, "nbdeli", [url])


def test_crawler_output_rejects_malformed_xlsx(tmp_path):
    path = tmp_path / "malformed.xlsx"
    path.write_bytes(b"not a zip workbook")
    with pytest.raises(ValueError, match="不是有效 XLSX"):
        read_crawler_output(path, "nbdeli", ["https://b2b.nbdeli.com/pcweb/detail?sku=1"])


def test_crawler_output_rejects_structurally_invalid_xlsx(tmp_path):
    path = tmp_path / "missing-content-types.xlsx"
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    with pytest.raises(ValueError, match="不是有效 XLSX"):
        read_crawler_output(path, "nbdeli", ["https://b2b.nbdeli.com/pcweb/detail?sku=1"])
