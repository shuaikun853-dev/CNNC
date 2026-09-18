"""客户预爬生命周期收口(WS-A)行为测试:plan / reconcile / redispatch / harvest。

真实 CustomerPrewarmStore + SharedCrawlCache + per-day DailyAuditStore
(tmp_path),注入计数 fake adapter(镜像 tests/test_customer_prewarm_dispatch
的 _CountingAdapter 模式,另加 reconcile_unknown 计数)。harvest 原语用
monkeypatch 替身计数(原语自带 ProcessLock+lease,单测不进真实远端协议)。

关键不变量:
- canary 模式(canary_scope 非 None)三阶段全部跳过且 summary 键集合逐字节不变;
- plan-only 追加 lifecycle_planned 且全程零写(三库哈希不变);
- reconcile NOT_FOUND 计入 recovery_attempts,≥阈值判死转 redispatch;
- redispatch 每候选每日上限按 prewarm_remote_job_links 当日行数计。

全程零真实远端调用。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.services import customer_prewarm_dispatch as dispatch_service
from app.services import customer_prewarm_lifecycle as lifecycle
from app.services.customer_prewarm_lifecycle import (
    HARVEST_PER_PASS,
    RECONCILE_PER_PASS,
    REDISPATCH_DAILY_CAP,
    RECONCILE_DEAD_THRESHOLD,
    plan_lifecycle_actions,
    run_harvest_phase,
    run_reconcile_phase,
    run_redispatch_phase,
)
from app.services.customer_prewarm_dispatch import (
    BACKEND_VERSION,
    run_prewarm_dispatch_pass,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_runtime import runtime_paths
from app.services.legacy_zhaoshang_adapter import (
    CrawlRequest,
    LegacyPriorityOutcome,
    LegacyReconcileOutcome,
    LegacySubmitOutcome,
    PriorityStatus,
    ReconcileStatus,
    SubmitStatus,
)
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts import customer_prewarm_dispatch as cli
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

TODAY = "2026-09-16"
YESTERDAY = "2026-09-15"
PRODUCER = "customer-prewarm-zhaoshang"

COMIX = ZhaoshangMallMapping(
    mall_name="齐心商城",
    source_id="SRC-COMIX",
    mall_key="comix",
    domains=("qx.com", "www.qx.com"),
    vendor_keywords=("comix",),
)

SUBMITTED_SUCCESS = LegacySubmitOutcome(
    SubmitStatus.SUBMITTED,
    task_id="remote-1",
    task_number="number-1",
    priority_evidence="updated",
)
RECONCILE_FOUND = LegacyReconcileOutcome(
    ReconcileStatus.FOUND, task_id="found-task", task_number="found-number"
)
RECONCILE_NOT_FOUND = LegacyReconcileOutcome(ReconcileStatus.NOT_FOUND)


def _fake_canonical(business_date, requests):
    return json.dumps(
        {
            "business_date": business_date,
            "requests": [
                request.identity
                for request in sorted(requests, key=lambda value: value.identity)
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class _FakePreparedJob:
    """Attribute-compatible PreparedLegacyJob stand-in (dispatcher only does
    attribute access).  Keys are deterministic so a reconstruct via
    ``prepare_job`` verifies against the originally prepared evidence."""

    def __init__(self, business_date, requests, task_name, template_path, result_path, batch_state_path):
        canonical = _fake_canonical(business_date, requests)
        self.business_date = business_date
        self.mall_key = requests[0].mall_key
        self.mall_source_id = requests[0].mall_source_id
        self.mall_name = requests[0].mall_name
        self.task_name = task_name
        self.template_path = Path(template_path)
        self.result_path = Path(result_path)
        self.batch_state_path = Path(batch_state_path)
        self.requests = tuple(requests)
        self.logical_job_key = "logical-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        self.payload_sha256 = "payload-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def payload_json(self):
        return {
            "requests": [
                {
                    "mall_key": request.mall_key,
                    "mall_source_id": request.mall_source_id,
                    "mall_name": request.mall_name,
                    "sku_id": request.sku_id,
                    "url": request.url,
                    "backend_version": request.backend_version,
                }
                for request in self.requests
            ],
            "batch_state": str(self.batch_state_path),
        }


class _CountingAdapter:
    """Fake adapter with counters, mirroring _BridgeAdapter conventions."""

    def __init__(self, *, submit_outcome=None, priority_outcomes=None, reconcile_outcome=None):
        self.prepare_calls = 0
        self.submit_calls = 0
        self.prioritize_calls = 0
        self.reconcile_calls = 0
        self.submit_outcome = submit_outcome or SUBMITTED_SUCCESS
        self.priority_outcomes = list(priority_outcomes or [])
        self.reconcile_outcome = reconcile_outcome or RECONCILE_NOT_FOUND
        self.reconcile_log: list[_FakePreparedJob] = []
        self.prepared_log: list[_FakePreparedJob] = []

    def prepare_job(self, business_date, requests, template_path, result_path, task_name, batch_state_path):
        self.prepare_calls += 1
        template_path = Path(template_path)
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text("immutable-prewarm", encoding="utf-8")
        Path(result_path).parent.mkdir(parents=True, exist_ok=True)
        job = _FakePreparedJob(
            business_date, list(requests), task_name, template_path, result_path, batch_state_path
        )
        self.prepared_log.append(job)
        return job

    def submit(self, _job, *, timeout_seconds):
        self.submit_calls += 1
        return self.submit_outcome

    def prioritize(self, _job, *, task_id, task_number, timeout_seconds):
        self.prioritize_calls += 1
        if self.priority_outcomes:
            return self.priority_outcomes.pop(0)
        return LegacyPriorityOutcome(PriorityStatus.UPDATED, weight_type=-1)

    def reconcile_unknown(self, job, *, timeout_seconds):
        self.reconcile_calls += 1
        self.reconcile_log.append(job)
        return self.reconcile_outcome


# ---- fixtures and seeders -----------------------------------------------------


def make_paths(runtime_root: Path, business_date: str):
    paths = runtime_paths(business_date, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    return paths


def make_state_db(runtime_root: Path, business_date: str) -> object:
    paths = make_paths(runtime_root, business_date)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(business_date)
    return paths


def make_candidate(prewarm: CustomerPrewarmStore, url: str, *, mall="comix", lookup_url=None, document="D1"):
    upsert = prewarm.upsert_link(document, url, mall=mall, lookup_url=lookup_url or url)
    candidate_id = int(upsert["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    return candidate_id


def _state_rows(path, query, params=()):
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def _remote_job_row(state_path, job_id):
    rows = _state_rows(state_path, "SELECT * FROM remote_jobs WHERE id=?", (int(job_id),))
    return rows[0] if rows else None


@contextmanager
def leased(paths, business_date=TODAY):
    """Acquire the per-day process lock + durable lease (mirrors the seeder)."""
    store = DailyAuditStore(paths.state_path)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    try:
        token = store.acquire_lease(business_date, "test-lifecycle", 120.0)
        try:
            yield store, token
        finally:
            store.release_lease(token)
    finally:
        lock.release()


def seed_remote_work(
    runtime_root: Path,
    business_date: str,
    adapter: _CountingAdapter,
    *,
    mapping,
    url,
    job_state="SUBMITTED",
    remote_identity=None,
):
    """Seed one bound crawl row + remote job with adapter-consistent evidence."""
    paths = make_paths(runtime_root, business_date)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(business_date)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    try:
        token = store.acquire_lease(business_date, "seeder", 120.0)
        sku = "zhpq-" + hashlib.sha256(f"{mapping.mall_key}\n{url}".encode("utf-8")).hexdigest()[:16]
        crawl = store.ensure_crawl_work(
            token, mapping.mall_key, sku, url, BACKEND_VERSION, "seed-fp", source_id=mapping.source_id
        )
        batch_state = paths.run_dir / "BATCH_STATE.md"
        if not batch_state.is_file():
            batch_state.touch()
        requests = [
            CrawlRequest(mapping.mall_key, mapping.source_id, mapping.mall_name, sku, url, BACKEND_VERSION)
        ]
        seed_suffix = f"{int(hashlib.sha256(url.encode('utf-8')).hexdigest(), 16) % (10 ** 14):014d}"
        seed_dir = paths.run_dir / "zhaoshang" / mapping.mall_key / f"seed-{seed_suffix}"
        prepared = adapter.prepare_job(
            business_date,
            requests,
            seed_dir / "input.xlsx",
            seed_dir / "result.xlsx",
            f"cnnc_current_opt_{mapping.mall_name}_{seed_suffix}",
            batch_state_path=batch_state,
        )
        job_row = store.prepare_remote_intent(
            token,
            prepared.logical_job_key,
            prepared.task_name,
            prepared.payload_sha256,
            mall_key=mapping.mall_key,
            input_path=str(prepared.template_path),
            result_path=str(prepared.result_path),
            payload_json=prepared.payload_json(),
        )
        store.link_crawl_work_to_remote_job(token, int(crawl["id"]), int(job_row["id"]))
        if remote_identity:
            claim = store.claim_remote_submission(token, int(job_row["id"]))
            assert claim is not None
            store.reconcile_remote_job(
                token, int(job_row["id"]), remote_identity=remote_identity, state=job_state
            )
            store.advance_crawl_work_for_job(token, int(job_row["id"]), job_state)
        return paths, int(job_row["id"])
    finally:
        store.release_lease(token)
        lock.release()


def seed_unknown_job(runtime_root: Path, business_date: str, adapter: _CountingAdapter, *, mapping, url):
    """SUBMIT_UNKNOWN job with durable POST evidence and NO identity."""
    paths, job_id = seed_remote_work(
        runtime_root, business_date, adapter, mapping=mapping, url=url
    )
    with leased(paths, business_date) as (store, token):
        claim = store.claim_remote_submission(token, job_id)
        assert claim is not None
        store.mark_submit_unknown(token, job_id, "seed submit unknown")
    return paths, job_id


def seed_manual_job(runtime_root: Path, business_date: str, adapter: _CountingAdapter, *, mapping, url):
    """MANUAL_REQUIRED job with a confirmed remote identity (dead job)."""
    mall = mapping.mall_key
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    paths, job_id = seed_remote_work(
        runtime_root,
        business_date,
        adapter,
        mapping=mapping,
        url=url,
        job_state="SUBMITTED",
        remote_identity=f"{mall}|seed-task-{digest}|seed-number-{digest}",
    )
    with leased(paths, business_date) as (store, token):
        store.mark_remote_manual(token, job_id, "seed manual required")
    return paths, job_id


def seed_remote_candidate(prewarm: CustomerPrewarmStore, url: str, job_id: int, *, status=CACHE_STATUS_REMOTE_DISPATCHED, business_date=TODAY, document="D1", mall="comix"):
    """REMOTE_* candidate bound to a job, with the linkage row recorded."""
    candidate_id = make_candidate(prewarm, url, mall=mall, document=document)
    prewarm.record_candidate_result(
        candidate_id, cache_status=status, remote_job_id=int(job_id)
    )
    prewarm.record_remote_job_link(business_date, candidate_id, int(job_id), PRODUCER)
    return candidate_id


def candidate_by_id(prewarm: CustomerPrewarmStore, candidate_id: int) -> dict:
    return next(row for row in prewarm.candidates() if int(row["id"]) == candidate_id)


def _new_summary() -> dict:
    """Summary shape matching the live pass keys the phases touch."""
    return {
        "business_date": TODAY,
        "phase_errors": [],
        "reconciled_found": 0,
        "reconcile_not_found": 0,
        "redispatched_urls": 0,
        "redispatch_jobs": 0,
        "redispatch_blocked": 0,
        "harvest_attempted": 0,
        "harvested": 0,
        "jobs_created": 0,
        "new_remote_urls": 0,
        "existing_remote_reused": 0,
        "submit_unknown": 0,
        "submit_not_attempted": 0,
        "priority_confirmed": 0,
        "priority_unconfirmed": 0,
        "by_mall": {},
        "batch_sizes": [],
    }


def run_reconcile(paths, adapter, *, summary=None):
    summary = summary if summary is not None else _new_summary()
    with leased(paths) as (store, token):
        run_reconcile_phase(
            store=store,
            token=token,
            get_adapter=lambda: adapter,
            paths=paths,
            deadline=time.monotonic() + 30.0,
            monotonic_value=time.monotonic,
            summary=summary,
        )
    return summary


def run_redispatch(runtime_root, paths, prewarm, adapter, *, summary=None, business_date=TODAY, max_jobs_per_pass=8, mappings=(COMIX,)):
    summary = summary if summary is not None else _new_summary()
    with leased(paths, business_date) as (store, token):
        run_redispatch_phase(
            store=store,
            token=token,
            prewarm_store=prewarm,
            paths=paths,
            mappings=list(mappings),
            get_adapter=lambda: adapter,
            business_date=business_date,
            deadline=time.monotonic() + 30.0,
            monotonic_value=time.monotonic,
            max_jobs_per_pass=max_jobs_per_pass,
            summary=summary,
        )
    return summary


# ---- 1. plan:三类 job 的只读分类 ----------------------------------------------------


def test_plan_classifies_harvest_reconcile_and_redispatch(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    adapter = _CountingAdapter()
    live_paths, live_job = seed_remote_work(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/plan-live.html", job_state="SUBMITTED",
        remote_identity="comix|seed-live|seed-live-n",
    )
    unknown_paths, unknown_job = seed_unknown_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/plan-unknown.html"
    )
    manual_paths, manual_job = seed_manual_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/plan-manual.html"
    )
    manual_candidate = seed_remote_candidate(prewarm, "https://qx.com/plan-manual.html", manual_job)

    plan = plan_lifecycle_actions(paths.state_path, prewarm, TODAY, [COMIX])

    assert set(plan) == {
        "harvest_job_ids",
        "reconcile_jobs",
            "cross_day_reconcile_jobs",
            "cross_day_harvest_jobs",
        "redispatch_groups",
        "redispatch_blocked",
    }
    assert plan["cross_day_reconcile_jobs"] == []
    assert live_job in plan["harvest_job_ids"]
    assert {"job_id": unknown_job, "recovery_attempts": 0} in plan["reconcile_jobs"]
    assert plan["redispatch_groups"] == {"comix": [candidate_by_id(prewarm, manual_candidate)]}
    assert plan["redispatch_blocked"] == []
    # 纯只读:plan 后三库无任何状态变化。
    assert _remote_job_row(paths.state_path, manual_job)["state"] == "MANUAL_REQUIRED"
    assert _remote_job_row(paths.state_path, unknown_job)["state"] == "SUBMIT_UNKNOWN"


def test_plan_dead_unknown_excluded_and_cap_blocked(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    dead_paths, dead_job = seed_unknown_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/plan-dead.html"
    )
    with leased(dead_paths) as (store, token):
        for _ in range(RECONCILE_DEAD_THRESHOLD):
            store.bump_remote_job_recovery_attempt(token, dead_job)
    dead_candidate = seed_remote_candidate(prewarm, "https://qx.com/plan-dead.html", dead_job)
    # 超限候选:当日 links 行数已达上限 2。
    exhausted_paths, exhausted_job = seed_manual_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/plan-capped.html"
    )
    exhausted_candidate = seed_remote_candidate(prewarm, "https://qx.com/plan-capped.html", exhausted_job)
    for index in range(REDISPATCH_DAILY_CAP):
        prewarm.record_remote_job_link(
            TODAY, exhausted_candidate, 9000 + index, PRODUCER
        )
    prewarm.record_remote_job_link(TODAY, exhausted_candidate, exhausted_job, PRODUCER)

    plan = plan_lifecycle_actions(paths.state_path, prewarm, TODAY, [COMIX])

    assert plan["reconcile_jobs"] == []  # attempts>=阈值:不再进对账集
    grouped_ids = [int(row["id"]) for row in plan["redispatch_groups"].get("comix", [])]
    assert dead_candidate in grouped_ids
    assert exhausted_candidate not in grouped_ids
    assert plan["redispatch_blocked"] == [
        {"candidate_id": exhausted_candidate, "reason": "redispatch budget exhausted"}
    ]


# ---- 2. reconcile FOUND:身份写入 + PRIORITY_PENDING + prioritize 确认 ----------------


def test_reconcile_found_writes_identity_and_confirms_priority(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    seed_paths, job_id = seed_unknown_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/reconcile-found.html"
    )
    adapter = _CountingAdapter(reconcile_outcome=RECONCILE_FOUND)

    summary = run_reconcile(paths, adapter)

    assert summary["reconciled_found"] == 1
    assert summary["reconcile_not_found"] == 0
    assert adapter.reconcile_calls == 1
    assert adapter.prioritize_calls == 1  # 一次 prioritize 确认(PROVEN 才 SUBMITTED)
    job = _remote_job_row(paths.state_path, job_id)
    assert job["remote_identity"] == "comix|found-task|found-number"
    assert job["state"] == "SUBMITTED"  # PROVEN weight_type == -1
    assert summary["priority_confirmed"] == 1
    crawls = _state_rows(paths.state_path, "SELECT state FROM crawl_work")
    assert all(row["state"] == "SUBMITTED" for row in crawls)


# ---- 3. reconcile NOT_FOUND:recovery_attempts 递增,≥阈值判死 -------------------------


def test_reconcile_not_found_bumps_attempts_until_dead(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    seed_paths, job_id = seed_unknown_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/reconcile-nf.html"
    )
    adapter = _CountingAdapter(reconcile_outcome=RECONCILE_NOT_FOUND)

    for round_index in range(RECONCILE_DEAD_THRESHOLD):
        summary = run_reconcile(paths, adapter)
        assert summary["reconcile_not_found"] == 1

    job = _remote_job_row(paths.state_path, job_id)
    assert job["recovery_attempts"] == RECONCILE_DEAD_THRESHOLD
    assert adapter.reconcile_calls == RECONCILE_DEAD_THRESHOLD
    # 第 4 次后判死:phase note 记录,且不再进 reconcile 集。
    dead_summary = run_reconcile(paths, adapter)
    assert adapter.reconcile_calls == RECONCILE_DEAD_THRESHOLD  # 不再对账
    assert dead_summary["reconcile_not_found"] == 0
    assert any(
        "judged dead" in note for note in dead_summary["phase_errors"]
    ), dead_summary["phase_errors"]
    plan = plan_lifecycle_actions(paths.state_path, prewarm, TODAY, [COMIX])
    assert plan["reconcile_jobs"] == []


def test_today_manual_reconcile_blocks_existing_redispatch_owner(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths, job_id = seed_unknown_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/today-manual-conflict.html",
    )
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    seed_remote_candidate(
        prewarm, "https://qx.com/today-manual-conflict.html", job_id
    )
    adapter = _CountingAdapter(
        reconcile_outcome=LegacyReconcileOutcome(
            ReconcileStatus.MANUAL, diagnostic="submission evidence conflicts with exact remote identity"
        )
    )

    run_reconcile(paths, adapter)
    run_reconcile(paths, adapter)

    job = _remote_job_row(paths.state_path, job_id)
    assert job["state"] == "MANUAL_REQUIRED"
    assert job["recovery_attempts"] == 0
    assert adapter.reconcile_calls == 1
    assert plan_lifecycle_actions(paths.state_path, prewarm, TODAY, [COMIX])[
        "redispatch_groups"
    ] == {}
    assert adapter.submit_calls == 0


def test_identityless_post_attempt_manual_job_cannot_auto_redispatch(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths, job_id = seed_unknown_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/tick-manual-unknown.html",
    )
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    seed_remote_candidate(prewarm, "https://qx.com/tick-manual-unknown.html", job_id)
    with leased(paths, TODAY) as (store, token):
        store.mark_remote_manual(token, job_id, "ambiguous submit evidence")

    job = _remote_job_row(paths.state_path, job_id)
    assert job["post_attempted"] == 1
    assert not job["remote_identity"]
    assert plan_lifecycle_actions(paths.state_path, prewarm, TODAY, [COMIX])[
        "redispatch_groups"
    ] == {}


def test_inconclusive_not_found_does_not_spend_absence_attempts(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths, job_id = seed_unknown_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/today-inconclusive.html",
    )
    adapter = _CountingAdapter(
        reconcile_outcome=LegacyReconcileOutcome(
            ReconcileStatus.NOT_FOUND,
            diagnostic="exact task reconciliation is not yet conclusive",
        )
    )

    for _ in range(RECONCILE_DEAD_THRESHOLD):
        run_reconcile(paths, adapter)

    job = _remote_job_row(paths.state_path, job_id)
    assert job["state"] == "SUBMIT_UNKNOWN"
    assert job["recovery_attempts"] == 0
    assert adapter.submit_calls == 0


def test_reconcile_respects_per_pass_cap(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    for index in range(RECONCILE_PER_PASS + 2):
        seed_unknown_job(
            runtime_root, TODAY, _CountingAdapter(), mapping=COMIX,
            url=f"https://qx.com/reconcile-cap-{index}.html",
        )
    adapter = _CountingAdapter(reconcile_outcome=RECONCILE_NOT_FOUND)

    summary = run_reconcile(paths, adapter)

    assert adapter.reconcile_calls == RECONCILE_PER_PASS
    assert summary["reconcile_not_found"] == RECONCILE_PER_PASS
    assert any("reconcile cap" in note for note in summary["phase_errors"])


# ---- 4. redispatch:新 job 创建 + linkage + cap 生效 ---------------------------------


def test_redispatch_creates_new_job_and_records_linkage(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/redispatch-live.html"
    dead_paths, dead_job = seed_manual_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url=url
    )
    candidate_id = seed_remote_candidate(prewarm, url, dead_job)
    jobs_before = _state_rows(paths.state_path, "SELECT id FROM remote_jobs")
    adapter = _CountingAdapter()

    summary = run_redispatch(runtime_root, paths, prewarm, adapter)

    assert summary["redispatched_urls"] == 1
    assert summary["redispatch_jobs"] == 1
    assert summary["redispatch_blocked"] == 0
    assert adapter.submit_calls == 1 and adapter.prioritize_calls == 1
    jobs_after = _state_rows(paths.state_path, "SELECT id, state, remote_identity FROM remote_jobs ORDER BY id")
    assert len(jobs_after) == len(jobs_before) + 1  # 新 job,旧死 job 保留
    new_job = jobs_after[-1]
    assert new_job["state"] == "SUBMITTED"  # 提优确认后 PROVEN
    assert str(new_job["remote_identity"]).startswith("comix|")
    assert _remote_job_row(paths.state_path, dead_job)["state"] == "MANUAL_REQUIRED"
    # 候选重绑到新 job,linkage 记当日行(producer 沿用 customer-prewarm-zhaoshang)。
    candidate = candidate_by_id(prewarm, candidate_id)
    assert candidate["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert int(candidate["remote_job_id"]) == int(new_job["id"])
    links = _state_rows(
        runtime_root / "customer_prewarm.sqlite3",
        "SELECT business_date, remote_job_id, producer FROM prewarm_remote_job_links WHERE candidate_id=?",
        (candidate_id,),
    )
    assert {"business_date": TODAY, "remote_job_id": int(new_job["id"]), "producer": PRODUCER} in links
    # 新爬取行绑定新 job(zhpq- 前缀保持,原死 job 的爬取行不动)。
    crawls = _state_rows(
        paths.state_path,
        "SELECT sku_id, remote_job_id FROM crawl_work ORDER BY id",
    )
    assert [row["remote_job_id"] for row in crawls] == [dead_job, int(new_job["id"])]
    assert all(str(row["sku_id"]).startswith("zhpq-") for row in crawls)


def test_redispatch_binding_cannot_crash_between_link_and_candidate(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    url = "https://qx.com/atomic-rebind.html"
    _dead_paths, dead_job = seed_manual_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url=url,
    )
    candidate_id = seed_remote_candidate(prewarm, url, dead_job)

    def crash_on_separate_candidate_write(*_args, **_kwargs):
        raise RuntimeError("crash between linkage writes")

    monkeypatch.setattr(lifecycle, "_record", crash_on_separate_candidate_write)
    try:
        run_redispatch(runtime_root, paths, prewarm, _CountingAdapter())
    except RuntimeError:
        pass
    candidate = prewarm.candidate_by_id(candidate_id)
    latest = prewarm.remote_job_link_for_candidate(candidate_id)
    assert latest["remote_job_id"] == candidate["remote_job_id"]


def test_redispatch_daily_cap_blocks_and_notes_last_error(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    dead_paths, dead_job = seed_manual_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/redispatch-cap.html"
    )
    candidate_id = seed_remote_candidate(prewarm, "https://qx.com/redispatch-cap.html", dead_job)
    for index in range(REDISPATCH_DAILY_CAP):
        prewarm.record_remote_job_link(TODAY, candidate_id, 8000 + index, PRODUCER)
    prewarm.record_remote_job_link(TODAY, candidate_id, dead_job, PRODUCER)
    adapter = _CountingAdapter()

    summary = run_redispatch(runtime_root, paths, prewarm, adapter)

    assert summary["redispatch_blocked"] == 1
    assert summary["redispatched_urls"] == 0
    assert adapter.submit_calls == 0
    jobs = _state_rows(paths.state_path, "SELECT id FROM remote_jobs")
    assert len(jobs) == 1  # 无新 job
    candidate = candidate_by_id(prewarm, candidate_id)
    assert int(candidate["remote_job_id"]) == dead_job  # 保留死绑
    assert "redispatch budget exhausted" in (candidate["last_error"] or "")


def test_redispatch_cross_day_dead_job_creates_job_today(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    url = "https://qx.com/redispatch-crossday.html"
    yesterday_paths, dead_job = seed_manual_job(
        runtime_root, YESTERDAY, _CountingAdapter(), mapping=COMIX, url=url
    )
    candidate_id = make_candidate(prewarm, url, document="D-cross")
    prewarm.record_candidate_result(
        candidate_id, cache_status=CACHE_STATUS_REMOTE_DISPATCHED, remote_job_id=int(dead_job)
    )
    # linkage 行记昨日:job 归属日由 links 的 business_date 判定。
    prewarm.record_remote_job_link(YESTERDAY, candidate_id, int(dead_job), PRODUCER)
    adapter = _CountingAdapter()

    summary = run_redispatch(runtime_root, paths, prewarm, adapter)

    assert summary["redispatched_urls"] == 1
    # 新 job 建在 pass 的 business_date(今日 DB),昨日 DB 不动。
    today_jobs = _state_rows(paths.state_path, "SELECT id, state FROM remote_jobs ORDER BY id")
    assert [row["state"] for row in today_jobs] == ["SUBMITTED"]
    assert _remote_job_row(yesterday_paths.state_path, dead_job)["state"] == "MANUAL_REQUIRED"
    candidate = candidate_by_id(prewarm, candidate_id)
    assert int(candidate["remote_job_id"]) == int(today_jobs[0]["id"])
    links = _state_rows(
        runtime_root / "customer_prewarm.sqlite3",
        "SELECT business_date, remote_job_id FROM prewarm_remote_job_links WHERE candidate_id=?",
        (candidate_id,),
    )
    assert {"business_date": TODAY, "remote_job_id": int(today_jobs[0]["id"])} in links


# ---- 5. harvest:原语调用计数、budget 截断、RUNNING 在途 ------------------------------


def _eligible_harvest_jobs(runtime_root, count):
    paths = None
    for index in range(count):
        paths, job_id = seed_remote_work(
            runtime_root, TODAY, _CountingAdapter(), mapping=COMIX,
            url=f"https://qx.com/harvest-{index}.html", job_state="SUBMITTED",
            remote_identity=f"comix|harvest-{index}|harvest-n-{index}",
        )
    return paths


def test_harvest_phase_calls_primitive_per_job_up_to_cap(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    paths = _eligible_harvest_jobs(runtime_root, HARVEST_PER_PASS + 1)
    calls = []

    def fake_primitive(**kwargs):
        calls.append(int(kwargs["remote_job_id"]))
        return {"outcome": "REMOTE_EXISTING_HARVEST_COMPLETED", "remote_job_id": kwargs["remote_job_id"]}

    monkeypatch.setattr(lifecycle, "harvest_existing_customer_prewarm_job", fake_primitive)
    summary = _new_summary()

    run_harvest_phase(
        paths.state_path,
        paths.lock_path,
        TODAY,
        deadline=time.monotonic() + 60.0,
        monotonic_value=time.monotonic,
        summary=summary,
    )

    assert len(calls) == HARVEST_PER_PASS  # 每 pass 上限 3
    assert summary["harvested"] == HARVEST_PER_PASS
    assert summary["harvest_attempted"] == HARVEST_PER_PASS
    assert calls == sorted(calls)  # 稳定顺序,可重入


def test_harvest_phase_budget_truncates_and_running_is_in_flight(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    paths = _eligible_harvest_jobs(runtime_root, 2)
    calls = []

    def fake_primitive(**kwargs):
        calls.append(int(kwargs["remote_job_id"]))
        return {"outcome": "REMOTE_EXISTING_HARVEST_RUNNING", "remote_job_id": kwargs["remote_job_id"]}

    monkeypatch.setattr(lifecycle, "harvest_existing_customer_prewarm_job", fake_primitive)

    class _Clock:
        def __init__(self, start):
            self.now = start

        def __call__(self):
            self.now += 1.0
            return self.now

    clock = _Clock(time.monotonic())
    summary = _new_summary()

    run_harvest_phase(
        paths.state_path,
        paths.lock_path,
        TODAY,
        deadline=clock.now + 1.5,  # 第 2 个 job 前预算耗尽
        monotonic_value=clock,
        summary=summary,
    )

    assert len(calls) == 1  # budget 截断
    assert summary["harvested"] == 0
    assert any("running" in note.lower() for note in summary["phase_errors"])
    assert not any("Traceback" in note for note in summary["phase_errors"])


# ---- 6. canary 模式:三阶段全部跳过,summary 键集合不变 --------------------------------


def test_canary_mode_skips_lifecycle_and_keeps_key_set(tmp_path):
    runtime_root = tmp_path / "runtime"
    paths = make_state_db(runtime_root, TODAY)
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    cache = SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    _seed_paths, unknown_job = seed_unknown_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/canary-unknown.html"
    )
    _dead_paths, manual_job = seed_manual_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/canary-manual.html"
    )
    seed_remote_candidate(prewarm, "https://qx.com/canary-manual.html", manual_job)
    canary_scope = dispatch_service.CanaryScope(only_mall="comix", max_urls=1, max_jobs=1)

    plan = run_prewarm_dispatch_pass(
        prewarm, cache, business_date=TODAY, runtime_root=runtime_root,
        mappings=[COMIX], plan_only=True, canary_scope=canary_scope,
    )
    budget = dispatch_service.CanaryLiveBudget.from_plan(plan, canary_scope)
    summary = run_prewarm_dispatch_pass(
        prewarm, cache, business_date=TODAY, runtime_root=runtime_root,
        mappings=[COMIX], adapter=_CountingAdapter(), plan_only=False,
        canary_scope=canary_scope, canary_budget=budget, max_jobs_per_pass=1,
    )

    assert summary["outcome"] == "OK"
    assert "lifecycle skipped in canary mode" in summary["phase_errors"]
    # 普通模式新增键绝不出现在 canary summary。
    for key in (
        "lifecycle_planned",
        "reconciled_found",
        "reconcile_not_found",
        "redispatched_urls",
        "redispatch_jobs",
        "redispatch_blocked",
        "harvest_attempted",
        "harvested",
    ):
        assert key not in summary
    # 既有键集合逐字节不变(基线 = 改动前 canary live 键集合)。
    expected = {
        "outcome", "business_date", "plan_only", "absorbed", "cache_hit_after_recheck",
        "staples_skipped", "manual_skipped", "routing_blocked", "mapped_zhaoshang",
        "existing_remote_reused", "new_remote_urls", "jobs_created", "jobs_reused",
        "submit_unknown", "submit_not_attempted", "priority_confirmed", "priority_boosted",
        "priority_unconfirmed", "phase_errors", "by_mall", "batch_sizes",
        "canary_mode", "only_mall", "canary_max_urls", "candidates_seen_total",
        "candidates_in_scope", "skipped_by_canary_filter", "remote_interaction_urls",
        "other_remote_tasks_created", "other_priority_changed", "priority_updates_performed",
        "canary_revalidated_targets",
    }
    assert set(summary) == expected
    # 三阶段零副作用:job 状态原样,无新 job。
    assert _remote_job_row(paths.state_path, unknown_job)["state"] == "SUBMIT_UNKNOWN"
    assert _remote_job_row(paths.state_path, manual_job)["state"] == "MANUAL_REQUIRED"
    assert len(_state_rows(paths.state_path, "SELECT id FROM remote_jobs")) == 2


# ---- 7. plan_only:lifecycle_planned 摘要 + 可证明零写 -------------------------------


def test_plan_only_report_has_lifecycle_planned_and_zero_write(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    prewarm_store_path = runtime_root / "customer_prewarm.sqlite3"
    cache_path = runtime_root / "shared_crawl_cache.sqlite3"
    prewarm = CustomerPrewarmStore(prewarm_store_path)
    cache = SharedCrawlCache(cache_path)
    paths = make_state_db(runtime_root, TODAY)
    _live_paths, live_job = seed_remote_work(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX,
        url="https://qx.com/po-live.html", job_state="SUBMITTED",
        remote_identity="comix|po-live|po-live-n",
    )
    _unknown_paths, unknown_job = seed_unknown_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/po-unknown.html"
    )
    _dead_paths, manual_job = seed_manual_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/po-manual.html"
    )
    manual_candidate = seed_remote_candidate(prewarm, "https://qx.com/po-manual.html", manual_job)

    def _sha256(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def _file_set(directory):
        return {entry.name for entry in Path(directory).iterdir()}

    hashes_before = {
        "prewarm": _sha256(prewarm_store_path),
        "cache": _sha256(cache_path),
        "state": _sha256(paths.state_path),
    }
    files_before = {
        "runtime": _file_set(runtime_root),
        "day": _file_set(runtime_root / TODAY),
    }

    class _NoAdapter:
        def __init__(self, *args, **kwargs):
            raise AssertionError("plan-only must never construct the legacy adapter")

    monkeypatch.setattr(dispatch_service, "CnncLegacyZhaoshangAdapter", _NoAdapter)

    summary = run_prewarm_dispatch_pass(
        prewarm, cache, business_date=TODAY, runtime_root=runtime_root,
        mappings=[COMIX], plan_only=True,
    )

    assert summary["lifecycle_planned"] == {
        "harvest_jobs": 1,
        "reconcile_jobs": 1,
        "cross_day_reconcile_jobs": 0,
        "cross_day_harvest_jobs": 0,
        "redispatch_urls": 1,
        "redispatch_blocked": 0,
    }
    assert _sha256(prewarm_store_path) == hashes_before["prewarm"]
    assert _sha256(cache_path) == hashes_before["cache"]
    assert _sha256(paths.state_path) == hashes_before["state"]
    new_runtime_files = _file_set(runtime_root) - files_before["runtime"]
    assert all(name.endswith(("-shm", "-wal")) for name in new_runtime_files)
    new_day_files = _file_set(runtime_root / TODAY) - files_before["day"]
    assert all(name.endswith(("-shm", "-wal")) for name in new_day_files)


# ---- 8. CLI:--no-lifecycle(默认执行)与输出行 ---------------------------------------


def _write_mapping_workbook(path: Path) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["商城名称", "sourceId", "商城标识 identification"])
    sheet.append(["齐心商城", "SRC-COMIX", "comix"])
    workbook.save(path)
    return path


def test_shared_harvest_deadline_is_one_process_one_budget():
    """regression guard:harvest deadline = pass_started + budget,绝不重起新窗口。

    旧行为(pass 返回后 `time.monotonic() + budget`)在 pass 用满预算时把进程
    总墙钟推到 2×budget,超过调度 runner 的 budget+grace(255s)上限被硬杀
    (summary JSON 写不出、日志记 TIMEOUT)。
    """
    from scripts.customer_prewarm_dispatch import _shared_harvest_deadline

    assert _shared_harvest_deadline(1000.0, 50.0) == 1050.0
    assert _shared_harvest_deadline(1000.0, 50.0) <= 1000.0 + 50.0  # 绝不超出总预算
    assert _shared_harvest_deadline(1000.0, 0.0) == 1000.0
    assert _shared_harvest_deadline(1000.0, -5.0) == 1000.0  # 负预算夹紧到 pass_started


def test_cli_harvest_deadline_shares_pass_budget_not_a_fresh_window(tmp_path, monkeypatch):
    """CLI 行为级回归:harvest 收到的 deadline ≤ pass 开始时刻 + budget。

    fake pass 记录被调时刻并推进真实墙钟 0.25s——旧实现(返回后重起
    `monotonic()+budget`)的 deadline 距 pass 开始 > budget,此断言必失败;
    新实现(共享绝对预算)恰好等于 pass_started + budget。
    """
    import scripts.customer_prewarm_dispatch as cli_module

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    budget = 2.0
    recorded: dict = {}

    def fake_pass(*_args, **_kwargs):
        recorded["pass_entry"] = time.monotonic()
        time.sleep(0.25)  # pass 消耗一段真实墙钟
        return {"outcome": "OK", "phase_errors": []}

    def fake_harvest(_state_path, _lock_path, _business_date, **kwargs):
        recorded["harvest_deadline"] = float(kwargs["deadline"])
        return None

    monkeypatch.setattr(cli_module, "run_prewarm_dispatch_pass", fake_pass)
    monkeypatch.setattr(cli_module, "run_harvest_phase", fake_harvest)

    exit_code = cli_module.main(
        [
            "--business-date", TODAY,
            "--runtime-root", str(runtime_root),
            "--mapping-path", str(_write_mapping_workbook(tmp_path / "mapping.xlsx")),
            "--budget-seconds", str(budget),
        ]
    )

    assert exit_code == 0
    assert "pass_entry" in recorded and "harvest_deadline" in recorded
    # 共享同一绝对预算:harvest 窗口从未超过 pass_started + budget(防 2×budget 回归)。
    assert recorded["harvest_deadline"] - recorded["pass_entry"] <= budget + 0.05
    # 且几乎拿满剩余窗口(静息 pass 不吞预算):deadline ≈ pass_started + budget。
    assert recorded["harvest_deadline"] >= recorded["pass_entry"] + budget - 0.05


def test_cli_no_lifecycle_flag_defaults_to_enabled():
    parser = cli.build_parser()
    args = parser.parse_args([])
    assert args.no_lifecycle is False
    args_off = parser.parse_args(["--no-lifecycle"])
    assert args_off.no_lifecycle is True


def test_cli_plan_only_prints_lifecycle_planned(tmp_path, capsys):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    prewarm = CustomerPrewarmStore(runtime_root / "customer_prewarm.sqlite3")
    SharedCrawlCache(runtime_root / "shared_crawl_cache.sqlite3")
    paths = make_state_db(runtime_root, TODAY)
    _manual_paths, manual_job = seed_manual_job(
        runtime_root, TODAY, _CountingAdapter(), mapping=COMIX, url="https://qx.com/cli-manual.html"
    )
    seed_remote_candidate(prewarm, "https://qx.com/cli-manual.html", manual_job)

    exit_code = cli.main(
        [
            "--business-date", TODAY,
            "--runtime-root", str(runtime_root),
            "--mapping-path", str(_write_mapping_workbook(tmp_path / "mapping.xlsx")),
            "--plan-only",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "lifecycle_planned" in output
    assert "harvest_jobs=0" in output
    assert "reconcile_jobs=0" in output
    assert "redispatch_urls=1" in output
