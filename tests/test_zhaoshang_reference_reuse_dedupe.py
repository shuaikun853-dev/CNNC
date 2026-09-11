"""Cross-source placeholder reuse dedupe tests (Issue #10, spec §11/§15).

``reuse_successful_crawl_work`` 在既有三条 (mall, sku, url, backend) 兄弟层
全部落空后,新增两个 URL-only 的 zhpq- 占位兄弟层:占位爬取在途 → 跟随同一
远端任务(单一作业),占位爬取终态成功 → 复制结果;占位的 TERMINAL_FAILED
绝不传播,真实 SKU 的五元组语义保持不变。
"""

from __future__ import annotations

import pytest

from app.services.daily_audit_store import DailyAuditStore

DATE = "2026-09-07"
MALL = "comix"
MALL_SOURCE_ID = "SRC-COMIX"  # #10 语义:占位行的 source_id = 商城的 sourceId
BACKEND = "legacy-v1"
URL = "https://www.qx.com/product-1.html"
PLACEHOLDER_SKU = "zhpq-deadbeef0011223"
REAL_SKU = "SKU-REAL-1"


@pytest.fixture()
def store(tmp_path):
    return DailyAuditStore(tmp_path / "state.sqlite3")


def _lease(store, owner="step1-a"):
    store.ensure_run(DATE)
    return store.acquire_lease(DATE, owner, ttl_seconds=30)


def _crawl(store, token, sku, *, url=URL, source_id="doc-step1"):
    return store.ensure_crawl_work(
        token,
        MALL,
        sku,
        url,
        BACKEND,
        f"fp-{sku}-{url}-{source_id}",
        source_id=source_id,
    )


def _bind_inflight_job(store, token, crawl_id, *, key="prewarm-job", identity="remote-prewarm-1"):
    """prepare → claim → reconcile → link,得到一个可被跟随的在途远端任务。"""
    job = store.prepare_remote_intent(
        token, key, f"task-{key}", f"payload-{key}", mall_key=MALL
    )
    store.claim_remote_submission(token, job["id"])
    job = store.reconcile_remote_job(token, job["id"], remote_identity=identity, state="SUBMITTED")
    return store.link_crawl_work_to_remote_job(token, crawl_id, job["id"])


def _remote_job_count(store):
    with store._reader() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM remote_jobs").fetchone()[0])


# ---- Tier P-terminal:占位终态成功 → 复制结果 -------------------------------------


def test_placeholder_terminal_success_copies_outcome(store):
    token = _lease(store)
    placeholder = _crawl(store, token, PLACEHOLDER_SKU, source_id=MALL_SOURCE_ID)
    store.complete_crawl_work(token, placeholder["id"], {"page_price_status": "OK", "price": "12.30"})
    assert store.get_crawl_work(placeholder["id"])["state"] == "SUCCESS"

    target = _crawl(store, token, REAL_SKU, source_id="doc-step1-real")
    assert target["state"] == "PENDING"

    reused = store.reuse_successful_crawl_work(token, target["id"])
    assert reused is not None
    assert reused["state"] == "SUCCESS"
    assert reused["result_json"] == store.get_crawl_work(placeholder["id"])["result_json"]
    assert reused["error_text"] is None
    assert reused["remote_job_id"] is None
    # 占位兄弟行自身不被改写
    untouched = store.get_crawl_work(placeholder["id"])
    assert untouched["sku_id"] == PLACEHOLDER_SKU
    assert untouched["state"] == "SUCCESS"


# ---- Tier P-inflight:占位在途 → 跟随同一远端任务 ----------------------------------


def test_placeholder_inflight_follows_same_remote_job(store):
    token = _lease(store)
    placeholder = _crawl(store, token, PLACEHOLDER_SKU, source_id=MALL_SOURCE_ID)
    bound = _bind_inflight_job(store, token, placeholder["id"])
    assert store.advance_crawl_work_for_job(token, bound["remote_job_id"], "RUNNING") == 1

    target = _crawl(store, token, REAL_SKU, source_id="doc-step1-real")
    reused = store.reuse_successful_crawl_work(token, target["id"])
    assert reused is not None
    assert reused["state"] == "RUNNING"
    assert reused["remote_job_id"] == bound["remote_job_id"]
    assert reused["error_text"] is None


# ---- 负例:五元组语义保持 / 失败不传播 ----------------------------------------------


def test_real_sku_sibling_with_different_sku_never_reuses(store):
    token = _lease(store)
    sibling = _crawl(store, token, "SKU-OTHER", source_id="doc-other")
    store.complete_crawl_work(token, sibling["id"], {"page_price_status": "OK"})

    target = _crawl(store, token, REAL_SKU, source_id="doc-step1-real")
    assert store.reuse_successful_crawl_work(token, target["id"]) is None
    # STEP1 随后创建自己的作业:目标行原样可绑定,未被任何兄弟改写
    job = store.prepare_remote_intent(token, "step1-job", "task-step1", "payload-step1", mall_key=MALL)
    store.claim_remote_submission(token, job["id"])
    bound = store.link_crawl_work_to_remote_job(token, target["id"], job["id"])
    assert bound["remote_job_id"] == job["id"]
    assert bound["state"] == "PENDING"
    assert _remote_job_count(store) == 1


def test_placeholder_terminal_failed_does_not_propagate(store):
    token = _lease(store)
    placeholder = _crawl(store, token, PLACEHOLDER_SKU, source_id=MALL_SOURCE_ID)
    failed = store.fail_crawl_work(token, placeholder["id"], "prewarm boom", retryable=False)
    assert failed["state"] == "TERMINAL_FAILED"

    target = _crawl(store, token, REAL_SKU, source_id="doc-step1-real")
    assert store.reuse_successful_crawl_work(token, target["id"]) is None
    after = store.get_crawl_work(target["id"])
    assert after["state"] == "PENDING"
    assert after["error_text"] is None


# ---- 优先级:真实 SKU 兄弟层先于占位兄弟层 ------------------------------------------


def test_real_sku_terminal_sibling_wins_over_placeholder_inflight(store):
    token = _lease(store)
    placeholder = _crawl(store, token, PLACEHOLDER_SKU, source_id=MALL_SOURCE_ID)
    job = _bind_inflight_job(store, token, placeholder["id"])
    # 与目标同 (mall, sku, url, backend) 的真实终态兄弟 → 既有 tier 1 命中
    real = _crawl(store, token, REAL_SKU, source_id="doc-other-real")
    store.complete_crawl_work(token, real["id"], {"page_price_status": "OK"})

    target = _crawl(store, token, REAL_SKU, source_id="doc-step1-real")
    reused = store.reuse_successful_crawl_work(token, target["id"])
    assert reused is not None
    assert reused["state"] == "SUCCESS"
    assert reused["remote_job_id"] is None
    # 占位作业不受影响
    assert store.get_remote_job(job["id"])["state"] == "SUBMITTED"


# ---- Case 10:跨源单作业 -----------------------------------------------------------


def test_case10_step1_row_follows_placeholder_job_and_job_count_stays_one(store):
    token = _lease(store)
    # #10 的占位爬取行:source_id = 商城 sourceId,sku = zhpq-<hash>,RUNNING 并绑定作业
    placeholder = store.ensure_crawl_work(
        token, MALL, "zhpq-deadbeef", URL, BACKEND, "fp-case10", source_id=MALL_SOURCE_ID
    )
    job = _bind_inflight_job(store, token, placeholder["id"], key="prewarm-job-10")
    store.advance_crawl_work_for_job(token, job["remote_job_id"], "RUNNING")

    # STEP1 形状的新行:同 mall+url,真实 SKU → 复用同一 remote_job_id
    target = store.ensure_crawl_work(
        token, MALL, REAL_SKU, URL, BACKEND, "fp-case10-step1", source_id="doc-step1"
    )
    reused = store.reuse_successful_crawl_work(token, target["id"])
    assert reused is not None
    assert reused["state"] == "RUNNING"
    assert reused["remote_job_id"] == job["remote_job_id"]
    # 跨源复用不产生第二个远端作业
    assert _remote_job_count(store) == 1
