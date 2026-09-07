"""Characterization of the harvest claim-closure protocol at the store level.

These tests pin the CURRENT durable behavior of ``DailyAuditStore`` when a
claimed remote harvest job is closed.  They drive, method for method, the
exact sequences that production drives today:

* ``app/services/recovery/harvest.py`` — ``_existing_remote_harvest_core``
  (RUNNING / MANUAL / EXPORTED outcome branches) and
  ``run_local_remote_harvest_recovery``.
* ``app/services/recovery/wait_export.py`` —
  ``run_remote_wait_export_recovery``.

The point is the store-call sequence and its full durable end state, so the
tests use the store directly (no adapters, no workflows).
"""

from __future__ import annotations

import json

import pytest

from app.services.daily_audit_store import DailyAuditStore, RemoteIntentConflict


BUSINESS_DATE = "2026-09-07"
IDENTITY = "lxwl|1246803|RW-20260907120000"
NO_RESULT_REASON = "provenance-valid remote request has no result row"
MANUAL_CRAWL_ERROR = "provenance-valid row lacks durable request-group mapping"
MANUAL_JOB_ERROR = "harvest has unresolved provenance mappings"
RUNNING_DIAGNOSTIC = "legacy wait-export-only is still running"
MANUAL_DIAGNOSTIC = "legacy wait-export-only requires manual review"


def _seed_job(tmp_path, *, crawl_count=3, identity=IDENTITY, result_path=None):
    # result_path defaults to str(tmp_path / "result.xlsx") (file need not exist)
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    token = store.acquire_lease(BUSINESS_DATE, "seed", 300)
    job = store.prepare_remote_intent(
        token, "lxwl|test-job", "cnnc_current_opt_test_20260907120000", "hash-1",
        mall_key="lxwl", input_path=str(tmp_path / "input.xlsx"),
        result_path=result_path or str(tmp_path / "result.xlsx"),
        payload_json={"requests": [], "batch_state": str(tmp_path / "BATCH_STATE.md")},
    )
    job_id = int(job["id"])
    store.claim_remote_submission(token, job_id)
    store.reconcile_remote_job(token, job_id, remote_identity=identity, state="SUBMITTED")
    crawl_ids = []
    for index in range(crawl_count):
        crawl = store.ensure_crawl_work(
            token, "lxwl", f"sku-{index}", f"https://shop.example/{index}",
            "legacy-v1", f"fp-{index}", source_id=f"v-{index}",
        )
        store.link_crawl_work_to_remote_job(token, int(crawl["id"]), job_id)
        crawl_ids.append(int(crawl["id"]))
    store.release_lease(token)
    return store, job_id, crawl_ids


def close_exported(store, token, job_id, matched, no_result_ids, manual, export_path, result, *, elapsed, max_attempts, max_elapsed):
    """EXPORTED branch (harvest.py 880-920 / wait_export.py 275-315)."""
    for crawl_id, crawl_result in matched.items():
        store.complete_crawl_work(token, crawl_id, crawl_result)
    for crawl_id in no_result_ids:
        store.complete_partial_exact_no_result(
            token,
            crawl_id,
            remote_job_id=job_id,
            remote_identity=IDENTITY,
            result_path=export_path,
            reason=NO_RESULT_REASON,
        )
    remote_state = "EXPORTED" if manual else "COMPLETED"
    store.reconcile_remote_job(
        token,
        job_id,
        remote_identity=IDENTITY,
        state=remote_state,
        export_path=export_path,
        result=result,
    )
    if manual:
        store.mark_remote_manual(token, job_id, MANUAL_JOB_ERROR)
        for crawl_id, error in manual:
            store.mark_partial_exact_crawl_manual(
                token,
                crawl_id,
                remote_job_id=job_id,
                remote_identity=IDENTITY,
                error=error,
            )
    store.record_harvest(
        token,
        job_id,
        elapsed_seconds=elapsed,
        max_attempts=max_attempts,
        max_elapsed_seconds=max_elapsed,
    )


def close_running(store, token, job_id, *, diagnostic, elapsed, max_attempts, max_elapsed):
    """RUNNING branch (harvest.py 781-795 / wait_export.py 173-187)."""
    store.reconcile_remote_job(token, job_id, remote_identity=IDENTITY, state="RUNNING")
    store.advance_crawl_work_for_job(token, job_id, "RUNNING")
    store.record_harvest(
        token,
        job_id,
        elapsed_seconds=elapsed,
        max_attempts=max_attempts,
        max_elapsed_seconds=max_elapsed,
        error=diagnostic,
    )


def close_manual(store, token, job_id, *, diagnostic, elapsed, max_attempts, max_elapsed):
    """MANUAL branch (harvest.py 805-815 / wait_export.py 198-208)."""
    store.mark_remote_manual(token, job_id, diagnostic)
    store.record_harvest(
        token,
        job_id,
        elapsed_seconds=elapsed,
        max_attempts=max_attempts,
        max_elapsed_seconds=max_elapsed,
        error=diagnostic,
    )


def _full_match_payload(crawl_ids):
    prices = (12.3, 4.5, 6.0)
    matched = {
        crawl_id: {"page_price_status": "OK", "value": price}
        for crawl_id, price in zip(crawl_ids, prices)
    }
    result_map = {
        f"https://shop.example/{index}": {"page_price_status": "OK", "value": price}
        for index, price in enumerate(prices)
    }
    return matched, result_map


def test_exported_full_match_closes_completed(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    export_path = str(tmp_path / "result.xlsx")
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        claim = store.claim_harvest(token, job_id, max_attempts=5, max_elapsed_seconds=100)
        assert claim is not None
        assert int(claim["harvest_attempts"]) == 1

        matched, result_map = _full_match_payload(crawl_ids)
        close_exported(
            store, token, job_id, matched, [], [], export_path, result_map,
            elapsed=2.0, max_attempts=5, max_elapsed=100,
        )

        job = store.get_remote_job(job_id)
        assert job["state"] == "COMPLETED"
        assert job["export_path"] == export_path
        assert json.loads(job["result_json"]) == result_map
        assert job["error_text"] is None
        assert int(job["harvest_attempts"]) == 1
        assert float(job["harvest_elapsed_seconds"]) == 2.0

        crawls = store.crawl_work_for_job(job_id)
        assert [int(crawl["id"]) for crawl in crawls] == crawl_ids
        for crawl in crawls:
            assert crawl["state"] == "SUCCESS"
            assert json.loads(crawl["result_json"]) == matched[int(crawl["id"])]
            assert crawl["error_text"] is None
    finally:
        store.release_lease(token)


def test_exported_with_manual_crawl_marks_job_exported_and_manual_crawls(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    export_path = str(tmp_path / "result.xlsx")
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        assert store.claim_harvest(token, job_id, max_attempts=5, max_elapsed_seconds=100) is not None

        matched = {crawl_ids[0]: {"page_price_status": "OK", "value": 12.3}}
        result_map = {"https://shop.example/0": {"page_price_status": "OK", "value": 12.3}}
        close_exported(
            store, token, job_id,
            matched,
            [crawl_ids[1]],
            [(crawl_ids[2], MANUAL_CRAWL_ERROR)],
            export_path,
            result_map,
            elapsed=2.0, max_attempts=5, max_elapsed=100,
        )

        # Current end state: the reconcile step first records the EXPORTED
        # evidence (export_path/result_json), then mark_remote_manual flips the
        # job to MANUAL_REQUIRED, and the closing record_harvest runs without an
        # error, which always rewrites error_text to NULL.
        job = store.get_remote_job(job_id)
        assert job["state"] == "MANUAL_REQUIRED"
        assert job["error_text"] is None
        assert job["export_path"] == export_path
        assert json.loads(job["result_json"]) == result_map
        assert int(job["harvest_attempts"]) == 1

        matched_crawl = store.get_crawl_work(crawl_ids[0])
        assert matched_crawl["state"] == "SUCCESS"
        assert json.loads(matched_crawl["result_json"]) == {"page_price_status": "OK", "value": 12.3}
        assert matched_crawl["error_text"] is None

        no_result_crawl = store.get_crawl_work(crawl_ids[1])
        assert no_result_crawl["state"] == "NO_RESULT"
        evidence = json.loads(no_result_crawl["result_json"])
        assert evidence["evidence_type"] == "EXPORTED_NO_MATCHING_ROW"
        assert evidence["no_result_reason"] == NO_RESULT_REASON
        assert evidence["remote_identity"] == IDENTITY
        assert evidence["remote_state"] == "COMPLETED"
        assert evidence["result_path"] == export_path
        assert evidence["page_price_status"] == "NO_RESULT"
        assert evidence["page_price_source"] == "招商局（无采集结果）"
        assert evidence["page_price_value"] is None
        assert no_result_crawl["error_text"] == NO_RESULT_REASON

        manual_crawl = store.get_crawl_work(crawl_ids[2])
        assert manual_crawl["state"] == "MANUAL_REQUIRED"
        assert manual_crawl["result_json"] is None
        assert manual_crawl["error_text"] == MANUAL_CRAWL_ERROR
    finally:
        store.release_lease(token)


def test_exported_terminal_evidence_wins_over_retry_cap(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    export_path = str(tmp_path / "result.xlsx")
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        # The last permitted harvest claim burns attempts to 1 == max_attempts.
        claim = store.claim_harvest(token, job_id, max_attempts=1, max_elapsed_seconds=100)
        assert claim is not None
        assert int(claim["harvest_attempts"]) == 1

        matched, result_map = _full_match_payload(crawl_ids)
        close_exported(
            store, token, job_id, matched, [], [], export_path, result_map,
            elapsed=0.5, max_attempts=1, max_elapsed=100,
        )

        # Terminal evidence wins over the retry cap: record_harvest never flips
        # a COMPLETED/EXPORTED job to MANUAL_REQUIRED.
        job = store.get_remote_job(job_id)
        assert job["state"] == "COMPLETED"
        assert job["error_text"] is None
        assert int(job["harvest_attempts"]) == 1
        assert all(crawl["state"] == "SUCCESS" for crawl in store.crawl_work_for_job(job_id))
    finally:
        store.release_lease(token)


def test_running_outcome_advances_crawls_and_records_diagnostic(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        assert store.claim_harvest(token, job_id, max_attempts=5, max_elapsed_seconds=100) is not None
        close_running(
            store, token, job_id,
            diagnostic=RUNNING_DIAGNOSTIC, elapsed=3.0, max_attempts=5, max_elapsed=100,
        )
        job = store.get_remote_job(job_id)
        assert job["state"] == "RUNNING"
        assert job["error_text"] == RUNNING_DIAGNOSTIC
        assert int(job["harvest_attempts"]) == 1
        assert float(job["harvest_elapsed_seconds"]) == 3.0
        assert [crawl["state"] for crawl in store.crawl_work_for_job(job_id)] == [
            "RUNNING",
            "RUNNING",
            "RUNNING",
        ]
    finally:
        store.release_lease(token)


def test_running_outcome_advance_never_touches_terminal_crawls(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        id0_result = {"page_price_status": "OK", "value": 12.3}
        store.complete_crawl_work(token, crawl_ids[0], id0_result)
        assert store.claim_harvest(token, job_id, max_attempts=5, max_elapsed_seconds=100) is not None
        close_running(
            store, token, job_id,
            diagnostic=RUNNING_DIAGNOSTIC, elapsed=1.5, max_attempts=5, max_elapsed=100,
        )
        crawls = {int(crawl["id"]): crawl for crawl in store.crawl_work_for_job(job_id)}
        assert crawls[crawl_ids[0]]["state"] == "SUCCESS"
        assert json.loads(crawls[crawl_ids[0]]["result_json"]) == id0_result
        assert crawls[crawl_ids[0]]["error_text"] is None
        assert crawls[crawl_ids[1]]["state"] == "RUNNING"
        assert crawls[crawl_ids[2]]["state"] == "RUNNING"
    finally:
        store.release_lease(token)


def test_running_outcome_capped_flips_to_manual_required(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        assert store.claim_harvest(token, job_id, max_attempts=1, max_elapsed_seconds=100) is not None
        close_running(
            store, token, job_id,
            diagnostic=RUNNING_DIAGNOSTIC, elapsed=2.0, max_attempts=1, max_elapsed=100,
        )
        job = store.get_remote_job(job_id)
        assert job["state"] == "MANUAL_REQUIRED"
        assert job["error_text"] == RUNNING_DIAGNOSTIC
        assert int(job["harvest_attempts"]) == 1
    finally:
        store.release_lease(token)


def test_manual_outcome_marks_manual_and_closes_claim(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        assert store.claim_harvest(token, job_id, max_attempts=5, max_elapsed_seconds=100) is not None
        close_manual(
            store, token, job_id,
            diagnostic=MANUAL_DIAGNOSTIC, elapsed=1.0, max_attempts=5, max_elapsed=100,
        )
        job = store.get_remote_job(job_id)
        assert job["state"] == "MANUAL_REQUIRED"
        assert job["error_text"] == MANUAL_DIAGNOSTIC
        assert int(job["harvest_attempts"]) == 1
        # The MANUAL branch never touches crawl rows.
        assert [crawl["state"] for crawl in store.crawl_work_for_job(job_id)] == [
            "PENDING",
            "PENDING",
            "PENDING",
        ]
    finally:
        store.release_lease(token)


def test_wrong_identity_rejected_after_crawl_writes_leaves_partial_state(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        assert store.claim_harvest(token, job_id, max_attempts=5, max_elapsed_seconds=100) is not None
        # The matched crawl write lands first...
        store.complete_crawl_work(token, crawl_ids[0], {"page_price_status": "OK", "value": 12.3})
        # ...then the identity guard rejects the conflicting reconcile.  The
        # protocol is intentionally NOT atomic: the crawl write stays.
        with pytest.raises(RemoteIntentConflict, match="remote identity is immutable once recorded"):
            store.reconcile_remote_job(
                token, job_id, remote_identity="lxwl|999|OTHER", state="RUNNING",
            )
        job = store.get_remote_job(job_id)
        assert job["state"] == "SUBMITTED"
        assert job["remote_identity"] == IDENTITY
        assert int(job["harvest_attempts"]) == 1
        assert store.get_crawl_work(crawl_ids[0])["state"] == "SUCCESS"
        assert store.get_crawl_work(crawl_ids[1])["state"] == "PENDING"
    finally:
        store.release_lease(token)


def test_successful_crawl_evidence_is_immutable(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    export_path = str(tmp_path / "result.xlsx")
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        assert store.claim_harvest(token, job_id, max_attempts=5, max_elapsed_seconds=100) is not None
        matched, result_map = _full_match_payload(crawl_ids)
        close_exported(
            store, token, job_id, matched, [], [], export_path, result_map,
            elapsed=2.0, max_attempts=5, max_elapsed=100,
        )
        original = store.get_crawl_work(crawl_ids[0])
        with pytest.raises(RemoteIntentConflict, match="successful crawl evidence is immutable"):
            store.complete_crawl_work(token, crawl_ids[0], {"page_price_status": "OK", "value": 999.0})
        # Replaying the exact same evidence is an idempotent no-op.
        replay = store.complete_crawl_work(token, crawl_ids[0], matched[crawl_ids[0]])
        assert replay["state"] == "SUCCESS"
        after = store.get_crawl_work(crawl_ids[0])
        assert after["state"] == "SUCCESS"
        assert after["result_json"] == original["result_json"]
        assert after["error_text"] is None
    finally:
        store.release_lease(token)


def test_no_result_on_success_crawl_rejected(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    export_path = str(tmp_path / "result.xlsx")
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        assert store.claim_harvest(token, job_id, max_attempts=5, max_elapsed_seconds=100) is not None
        matched, result_map = _full_match_payload(crawl_ids)
        close_exported(
            store, token, job_id, matched, [], [], export_path, result_map,
            elapsed=2.0, max_attempts=5, max_elapsed=100,
        )
        with pytest.raises(RemoteIntentConflict, match="terminal crawl evidence is immutable"):
            store.complete_partial_exact_no_result(
                token,
                crawl_ids[0],
                remote_job_id=job_id,
                remote_identity=IDENTITY,
                result_path=export_path,
                reason=NO_RESULT_REASON,
            )
        crawl = store.get_crawl_work(crawl_ids[0])
        assert crawl["state"] == "SUCCESS"
        assert json.loads(crawl["result_json"]) == matched[crawl_ids[0]]
    finally:
        store.release_lease(token)


def test_terminal_failed_crawl_cannot_become_successful(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        store.fail_crawl_work(token, crawl_ids[0], "boom", retryable=False)
        assert store.get_crawl_work(crawl_ids[0])["state"] == "TERMINAL_FAILED"
        with pytest.raises(RemoteIntentConflict, match="terminal crawl cannot become successful"):
            store.complete_crawl_work(token, crawl_ids[0], {"page_price_status": "OK", "value": 12.3})
        crawl = store.get_crawl_work(crawl_ids[0])
        assert crawl["state"] == "TERMINAL_FAILED"
        assert crawl["error_text"] == "boom"
        assert crawl["result_json"] is None
    finally:
        store.release_lease(token)


def test_superseded_job_never_harvestable_and_reconcile_illegal(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        with store._transaction(immediate=True) as connection:
            connection.execute("UPDATE remote_jobs SET state='SUPERSEDED' WHERE id=?", (job_id,))
        # SUPERSEDED is outside the harvestable set: no claim, no state change.
        assert store.claim_harvest(token, job_id, max_attempts=5, max_elapsed_seconds=100) is None
        job = store.get_remote_job(job_id)
        assert job["state"] == "SUPERSEDED"
        with pytest.raises(RemoteIntentConflict, match=r"illegal remote state transition SUPERSEDED -> RUNNING"):
            store.reconcile_remote_job(token, job_id, remote_identity=IDENTITY, state="RUNNING")
        assert [crawl["state"] for crawl in store.crawl_work_for_job(job_id)] == [
            "PENDING",
            "PENDING",
            "PENDING",
        ]
    finally:
        store.release_lease(token)


def test_manual_marks_are_idempotent_for_resume(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    try:
        assert store.claim_harvest(token, job_id, max_attempts=5, max_elapsed_seconds=100) is not None
        store.mark_remote_manual(token, job_id, MANUAL_JOB_ERROR)
        first = store.mark_partial_exact_crawl_manual(
            token, crawl_ids[2], remote_job_id=job_id, remote_identity=IDENTITY, error=MANUAL_CRAWL_ERROR,
        )
        second = store.mark_partial_exact_crawl_manual(
            token, crawl_ids[2], remote_job_id=job_id, remote_identity=IDENTITY, error=MANUAL_CRAWL_ERROR,
        )
        assert first["state"] == "MANUAL_REQUIRED"
        assert second["state"] == "MANUAL_REQUIRED"
        crawl = store.get_crawl_work(crawl_ids[2])
        assert crawl["state"] == "MANUAL_REQUIRED"
        assert crawl["result_json"] is None
        assert crawl["error_text"] == MANUAL_CRAWL_ERROR
        job = store.get_remote_job(job_id)
        assert job["state"] == "MANUAL_REQUIRED"
        assert job["error_text"] == MANUAL_JOB_ERROR
    finally:
        store.release_lease(token)


def test_wrong_run_lease_rejected(tmp_path) -> None:
    store, job_id, crawl_ids = _seed_job(tmp_path)
    token = store.acquire_lease(BUSINESS_DATE, "test", 300)
    other = None
    try:
        other = store.acquire_lease("2026-09-08", "other", 300)
        with pytest.raises(ValueError, match="remote job must belong to the leased daily run"):
            store.reconcile_remote_job(other, job_id, remote_identity=IDENTITY, state="RUNNING")
        with pytest.raises(ValueError, match="crawl work must belong to the leased daily run"):
            store.complete_crawl_work(other, crawl_ids[0], {"page_price_status": "OK", "value": 1.0})
        job = store.get_remote_job(job_id)
        assert job["state"] == "SUBMITTED"
        assert job["remote_identity"] == IDENTITY
        assert store.get_crawl_work(crawl_ids[0])["state"] == "PENDING"
    finally:
        if other is not None:
            store.release_lease(other)
        store.release_lease(token)
