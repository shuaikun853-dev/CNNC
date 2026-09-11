"""Refresh v2(#10+#11 合并树)cross-contract cases H-K:生产客户缺席语义线与
#9 prewarm intake / #10 dispatcher / #11 Staples 集成线的交叉合同。

合并树同时保留两条权威语义线,本文件验证二者互不越界:
- Case H:权威客户快照(document 缺席,查询成功/分页完整/count 一致)与
  prewarm intake 同 tick 共存——分类仍 CUSTOMER_ABSENT_SKIPPED(非阻塞),
  intake seam 被真实调用,二者互不改变对方语义;
- Case I:prewarm_intake 抛异常——缺席分类与 finalize readiness 判定不受
  影响,异常只计入 prewarm_intake_failures 诊断计数;
- Case J:客户快照技术失败(count 漂移→整轮作废)永远不能变成缺席跳过——
  STEP1 整体 fail-closed,既有失败行保持原失败态/blocking;
- Case K:prewarm 集成在场时,finalize 投影仍 1:1 对齐——absent 行
  NO_ACTION/needs_human_approval=False/execution_allowed=False/executed=False,
  普通行保留原 verdict,record_results 与冻结行逐行对位。

全部 hermetic:tmp SQLite / fake adapter / fake intake,零真实远端、零真实爬取。
构造法照抄生产测试:test_customer_prewarm_step1_isolation(tick seam)、
test_customer_absent_closeout_projection(投影 seed)、
test_incremental_audit_runtime(快照失败注入)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import app.services.incremental_audit_runtime as runtime_module
from app.schemas.workflow import WorkflowAction
from app.services.cnnc_saas_snapshot_adapter import (
    SnapshotConsistencyDrift,
    SnapshotFetchExhausted,
)
from app.services.customer_prewarm_discovery import intake_documents
from app.services.customer_prewarm_store import (
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_REMOTE_DISPATCHED,
    CustomerPrewarmStore,
)
from app.services.customer_price_semantics import (
    DetailRequestError,
    customer_price_business_state,
)
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
)
from app.workflows.incremental_price_audit_finalize import (
    build_frozen_audit_artifact,
)
from scripts.audit_saas_excel_file import RESULT_APPENDED_COLUMNS

# ---- 共享基建:STEP1 tick fake 时钟 / 快照 adapter -------------------------------

BUSINESS_DATE = "2026-08-11"


@dataclass
class _Clock:
    value: float = 0

    def monotonic(self) -> float:
        return self.value


def _tick_config(clock: _Clock) -> TickConfig:
    return TickConfig(
        business_date=BUSINESS_DATE,
        runtime_budget_seconds=60,
        heavy_work_reserve_seconds=10,
        lease_ttl_seconds=90,
    )


class _SnapshotAdapter:
    """权威客户快照 fake:查询成功、分页完整(page_evidence 空)、count 一致。

    客户导出只含 doc-customer-only;SaaS 行 v-absent 的 documentId 确定不在
    快照中——这正是生产 join_saas_with_customer 只能在此前提下产出
    customer_lookup_status=NOT_FOUND 的真实构造法。
    """

    observed_at_utc = datetime(2026, 8, 11, 10, 1, tzinfo=UTC)
    saas_rows = (
        {"vCommodityId": "v-absent", "skuId": "sku-absent", "extLink": ""},
    )
    customer_rows = (
        {
            "documentId": "doc-customer-only",
            "fourCategoryId": "cat-9",
            "fourCategoryName": "九类",
            "commodityName": "commodity-c",
            "referenceLink": "https://ref.example/c",
        },
    )
    saas_count = 1
    customer_count = 1
    page_evidence = ()
    source_label = "fake"

    def fetch(self, **_kwargs):
        return self


# ---- Case H:客户缺席与 prewarm intake 同 tick 共存 -------------------------------


def test_case_h_customer_absence_coexists_with_prewarm_intake(tmp_path):
    """权威快照查询成功且 documentId 缺席的行,与生产 prewarm intake worker
    在同一个 STEP1 tick 中共存:缺席行照常分类 CUSTOMER_ABSENT_SKIPPED
    (blocking=False/wait_class=NONE)且不阻塞收口;intake seam 被真实调用并
    按客户文档覆盖 sidecar;两条语义线互不改变对方(缺席证据与 prewarm
    文档/候选逐字段完好)。"""
    clock = _Clock()
    prewarm_db = tmp_path / "prewarm-root" / "customer_prewarm.sqlite3"
    received: list[tuple[str, list[dict[str, Any]]]] = []
    real_intake = runtime_module._prewarm_intake_worker(prewarm_db)

    def recording_intake(business_date: str, rows) -> None:
        received.append((business_date, [dict(row) for row in rows]))
        real_intake(business_date, rows)

    store = DailyAuditStore(tmp_path / "case-h.sqlite3")
    summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "case-h.lock"),
        _tick_config(clock),
        TickDependencies(
            discover=runtime_module.snapshot_discoverer(_SnapshotAdapter()),
            # 生产 detail worker 的真实缺席短路:customer_lookup_status=NOT_FOUND
            # 直接返回 CUSTOMER_NOT_FOUND,绝不发详情请求。
            detail=runtime_module._detail_worker(
                {"url": "https://detail.invalid"}, request_interval_seconds=0
            ),
            model=lambda _row, _timeout: {"match_result": "SAME"},
            prewarm_intake=recording_intake,
            monotonic=clock.monotonic,
            wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
        ),
    )

    # 缺席行不阻塞收口:tick 正常完成,intake 成功计数
    assert summary.outcome == "COMPLETED"
    assert summary.counters["prewarm_intake_ok"] == 1
    assert not summary.counters.get("prewarm_intake_failures")
    assert summary.backlog["customer_absent_skipped"] == 1
    assert summary.backlog["customer_price_missing"] == 0
    assert summary.backlog["customer_price_failed"] == 0

    # 持久证据分类:仍是权威缺席态,非阻塞、无需等待
    details = store.list_stage_work_for_business_date(BUSINESS_DATE, stage="DETAIL")
    assert len(details) == 1
    persisted = json.loads(details[0]["result_json"])
    assert persisted["customerPriceStatus"] == "CUSTOMER_NOT_FOUND"
    assert persisted["customerPriceEvidence"] == (
        "authoritative customer snapshot round complete; documentId absent"
    )
    state = customer_price_business_state(
        details[0]["state"], persisted, details[0]["error_text"]
    )
    assert state.key == "CUSTOMER_ABSENT_SKIPPED"
    assert state.blocking is False
    assert state.wait_class == "NONE"
    assert state.status == "CUSTOMER_NOT_FOUND"

    # intake seam 被真实调用:只覆盖客户侧文档;缺席的 SaaS documentId 绝不混入
    assert len(received) == 1
    hooked_date, hooked_rows = received[0]
    assert hooked_date == BUSINESS_DATE
    assert hooked_rows == [
        {
            "documentId": "doc-customer-only",
            "fourCategoryId": "cat-9",
            "fourCategoryName": "九类",
            "commodityName": "commodity-c",
            "referenceLink": "https://ref.example/c",
        }
    ]
    prewarm = CustomerPrewarmStore(prewarm_db)
    assert [
        row["document_id"] for row in prewarm.pending_documents(10)
    ] == ["doc-customer-only"]

    # 二者互不改变对方语义:缺席证据原样,sidecar 文档原样(PENDING 待预热)
    with prewarm._reader() as connection:
        stored_doc = dict(
            connection.execute(
                "SELECT * FROM prewarm_documents WHERE document_id='doc-customer-only'"
            ).fetchone()
        )
    assert stored_doc["four_category_id"] == "cat-9"
    assert stored_doc["detail_state"] == "PENDING"
    assert details[0]["state"] == "SUCCESS"


# ---- Case I:prewarm intake 失败不破坏缺席收口 -------------------------------------


def test_case_i_prewarm_intake_failure_does_not_break_absence_closeout(tmp_path):
    """intake hook 抛 RuntimeError:缺席行的分类与 finalize readiness 判定
    与正常 intake 的 baseline 完全一致,异常只计 prewarm_intake_failures,
    绝不改变 tick 行为或把缺席行变成阻塞。"""
    clock = _Clock()

    def make_deps(prewarm_intake) -> TickDependencies:
        return TickDependencies(
            discover=runtime_module.snapshot_discoverer(_SnapshotAdapter()),
            detail=runtime_module._detail_worker(
                {"url": "https://detail.invalid"}, request_interval_seconds=0
            ),
            model=lambda _row, _timeout: {"match_result": "SAME"},
            prewarm_intake=prewarm_intake,
            monotonic=clock.monotonic,
            wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
        )

    def run_tick(name: str, deps: TickDependencies):
        store = DailyAuditStore(tmp_path / f"{name}.sqlite3")
        summary = run_incremental_tick(
            store,
            ProcessLock(tmp_path / f"{name}.lock"),
            _tick_config(clock),
            deps,
        )
        return store, summary

    baseline_store, baseline = run_tick(
        "case-i-baseline",
        make_deps(runtime_module._prewarm_intake_worker(tmp_path / "baseline-prewarm.sqlite3")),
    )

    def exploding_intake(_business_date, _rows):
        raise RuntimeError("prewarm intake boom")

    failing_store, failing = run_tick("case-i-failing", make_deps(exploding_intake))

    # intake 异常只计诊断计数
    assert failing.counters["prewarm_intake_failures"] == 1
    assert "prewarm_intake_ok" not in failing.counters
    assert baseline.counters["prewarm_intake_ok"] == 1

    # tick 行为与 baseline 逐字节等价(剥离 prewarm_* 诊断计数)
    assert failing.outcome == baseline.outcome == "COMPLETED"
    assert failing.phase_trace == baseline.phase_trace

    def _strip_prewarm(counters):
        return {k: v for k, v in counters.items() if not k.startswith("prewarm_")}

    assert _strip_prewarm(failing.counters) == _strip_prewarm(baseline.counters)

    # 缺席分类不受影响:仍是非阻塞 CUSTOMER_ABSENT_SKIPPED
    details = failing_store.list_stage_work_for_business_date(BUSINESS_DATE, stage="DETAIL")
    assert len(details) == 1
    state = customer_price_business_state(
        details[0]["state"], json.loads(details[0]["result_json"]), details[0]["error_text"]
    )
    assert state.key == "CUSTOMER_ABSENT_SKIPPED"
    assert state.blocking is False
    assert state.wait_class == "NONE"

    # finalize readiness 判定不受影响:冻结后 ready=True,缺席只进观测计数
    snapshot = failing_store.list_snapshots(BUSINESS_DATE)[-1]
    token = failing_store.acquire_lease(BUSINESS_DATE, "case-i-freeze", 60)
    failing_store.freeze_snapshot(token, int(snapshot["id"]))
    built = build_frozen_audit_artifact(
        failing_store, token, output_identity="daily-final-case-i"
    )
    assert built.readiness.ready is True
    assert built.readiness.customer_absent_skipped == 1
    assert built.artifact is not None
    result = built.artifact.audit_response.record_results[0]
    assert result.price_vs_customer_check == "CUSTOMER_ABSENT_SKIPPED"
    assert result.primary_action == WorkflowAction.NO_ACTION
    assert result.needs_human_approval is False
    failing_store.release_lease(token)


# ---- Case J:客户快照技术失败永远不能变成缺席跳过 -----------------------------------


def _manual_discovery(v: str, sku: str) -> DiscoveredState:
    return DiscoveredState(
        observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
        headers=["vCommodityId"],
        rows=[
            {
                "vCommodityId": v,
                "documentId": v,
                "skuId": sku,
                "extLink": "",
                "fourCategoryId": "cat-1",
            }
        ],
        source_fingerprints=[f"source-{v}-{sku}"],
        customer_fingerprints=["customer-cat-1-10"],
        evidence={"saas_count": 1, "customer_count": 1},
    )


def _all_detail_classifications(store: DailyAuditStore) -> list[str]:
    rows = store.list_stage_work_for_business_date(BUSINESS_DATE, stage="DETAIL")
    return [
        customer_price_business_state(
            row["state"],
            json.loads(row["result_json"]) if row["result_json"] else None,
            row["error_text"],
        ).key
        for row in rows
    ]


def test_case_j_technical_customer_failure_never_becomes_absent_skip(tmp_path):
    """客户快照技术失败(count 漂移→整轮作废→SnapshotFetchExhausted,fail-
    closed)时:STEP1 整体 FAILED,任何行都不可能被分类为
    CUSTOMER_ABSENT_SKIPPED;既有技术失败行(RETRYABLE_FAILED)保持原失败态
    /blocking。即使 prewarm intake 全程可运行也一样。"""
    clock = _Clock()
    received: list[str] = []

    def working_intake(business_date, _rows) -> None:
        received.append(business_date)

    store = DailyAuditStore(tmp_path / "case-j.sqlite3")
    deps = TickDependencies(
        discover=lambda _timeout: _manual_discovery("v-tech", "s1"),
        # 技术失败注入:详情请求 HTTP 503(可重试传输失败)→ 持久 RETRYABLE_FAILED
        detail=lambda _row, _timeout: (_ for _ in ()).throw(
            DetailRequestError(http_status=503, message="gateway unavailable", retryable=True)
        ),
        model=lambda _row, _timeout: {"match_result": "SAME"},
        prewarm_intake=working_intake,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
    )
    first = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "case-j.lock"),
        _tick_config(clock),
        deps,
    )
    # intake 可运行;技术失败行处于可重试失败态(等待重试,非缺席)
    assert first.counters["prewarm_intake_ok"] == 1
    details = store.list_stage_work_for_business_date(BUSINESS_DATE, stage="DETAIL")
    assert [row["state"] for row in details] == ["RETRYABLE_FAILED"]
    state = customer_price_business_state(details[0]["state"], None, details[0]["error_text"])
    assert state.key == "RETRYING"
    assert state.blocking is True
    assert state.wait_class == "WAIT_RETRY"

    # 第二个 tick:权威客户快照整轮技术失败(count 漂移,生产同款注入法)
    class _DriftingAdapter:
        def fetch(self, **_kwargs):
            raise SnapshotFetchExhausted(
                "saas", 1, 3, 5.0,
                SnapshotConsistencyDrift("saas", "count changed during pagination"),
            )

    drifting_deps = TickDependencies(
        discover=runtime_module.snapshot_discoverer(_DriftingAdapter()),
        detail=lambda _row, _timeout: {"customerPrice": "10"},
        model=lambda _row, _timeout: {"match_result": "SAME"},
        prewarm_intake=working_intake,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 11, 10, 3, tzinfo=UTC),
    )
    second = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "case-j.lock"),
        _tick_config(clock),
        drifting_deps,
    )

    # STEP1 整体 fail-closed:快照技术失败让整个 tick FAILED
    assert second.outcome == "FAILED"
    assert "SnapshotFetchExhausted" in str(second.error)
    assert "discover" in second.phase_trace
    # intake 没被牵连:第二个 tick 既没有成功也没有把失败记到 intake 头上
    assert "prewarm_intake_failures" not in second.counters
    assert "prewarm_intake_ok" not in second.counters

    # CUSTOMER_ABSENT_SKIPPED 计数=0:技术失败永远不能变成缺席跳过
    assert _all_detail_classifications(store) == ["RETRYING"]
    assert second.backlog["customer_absent_skipped"] == 0

    # 该行保持原失败态/blocking:RETRYABLE_FAILED 未被改写
    after = store.list_stage_work_for_business_date(BUSINESS_DATE, stage="DETAIL")
    assert [row["state"] for row in after] == ["RETRYABLE_FAILED"]
    assert len(received) == 1  # 第二个 tick 未到 intake(失败在发现阶段)


# ---- Case K:prewarm 集成在场时 finalize 投影仍 1:1 对齐 ---------------------------

DATE = "2026-08-24"


def _row(v: str, sku: str, **extra) -> dict[str, Any]:
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": "",
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
        **extra,
    }


def _proven_absent_result(status: str, *, evidence: str = "") -> dict[str, Any]:
    return {
        "customerPrice": None,
        "customerPriceStatus": status,
        "customerPriceSource": "",
        "customerPriceWarning": "authoritative customer export has no such document",
        "customerPriceEvidence": evidence,
    }


def _seed_frozen_pair(tmp_path: Path):
    """冻结 1 条缺席行 + 1 条普通行(照抄生产投影测试构造法):
    Detail/Model 完成后把缺席行重做为 CUSTOMER_NOT_FOUND 证据。"""
    rows = [_row("v-absent-0", "sku-absent-0"), _row("v-ok", "sku-ok")]
    run_dir = tmp_path / "runtime" / DATE
    run_dir.mkdir(parents=True, exist_ok=True)
    store = DailyAuditStore(run_dir / "state.sqlite3")
    store.ensure_run(DATE, cutoff_at=f"{DATE}T10:00:00+00:00")
    token = store.acquire_lease(DATE, "seed", 60)
    source_headers = [
        name for name in rows[0]
        if not name.startswith("__") and name not in RESULT_APPENDED_COLUMNS
    ]
    snapshot = store.ingest_snapshot(
        token,
        rows,
        f"{DATE}T10:01:00+00:00",
        headers=source_headers,
        evidence={"observed_count": len(rows)},
    )["snapshot"]
    store.freeze_snapshot(token, snapshot["id"])
    for item in {item["id"]: item for item in store.ordered_snapshot_items(snapshot["id"])}.values():
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
    # 把 v-absent-0 的 DETAIL 重做为权威缺席证据
    absent_item = next(
        item for item in store.ordered_snapshot_items(snapshot["id"])
        if str(item["row"]["vCommodityId"]) == "v-absent-0"
    )
    prior = next(
        row for row in store.list_stage_work(token, stage="DETAIL")
        if row["audit_item_id"] == absent_item["id"]
    )
    reworked = store.ensure_stage_work(
        token, absent_item["id"], "DETAIL", str(prior["fingerprint"]) + "-rework"
    )
    claimed = store.claim_stage_work(
        token, reworked["id"], max_attempts=1, max_elapsed_seconds=300
    )
    assert claimed is not None
    store.complete_stage_work(token, reworked["id"], _proven_absent_result(
        "CUSTOMER_NOT_FOUND",
        evidence="authoritative customer snapshot round complete; documentId absent",
    ))
    store.release_lease(token)
    return store, snapshot


def test_case_k_final_projection_alignment_with_prewarm_present(tmp_path):
    """prewarm 集成(sidecar 文档 + #10 已派发候选)在场时,冻结日的 finalize
    投影不变:absent 行 NO_ACTION/不需要人工/不可执行/未执行,普通行保留原
    verdict;record_results 与冻结行 1:1 逐行对位,顺序未被打乱;prewarm 侧
    状态也未被 finalize 触碰。"""
    store, snapshot = _seed_frozen_pair(tmp_path)

    # prewarm 集成在场:#9 sidecar 文档 + #10 已派发候选(带 remote_job_id)
    prewarm = CustomerPrewarmStore(tmp_path / "customer_prewarm.sqlite3")
    intake_documents(
        prewarm,
        DATE,
        [
            {"documentId": "doc-sidecar-a", "fourCategoryId": "cat-a"},
            {"documentId": "doc-sidecar-b", "fourCategoryId": ""},
        ],
        source="event",
    )
    upserted = prewarm.upsert_link(
        "doc-sidecar-a", "https://qx.com/sidecar-a.html", mall="comix"
    )
    candidate_id = int(upserted["candidate"]["id"])
    # PENDING_LOOKUP 只能先到 PENDING_PREWARM(照抄 Case A-G 的 _seed_candidate 同法)
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(
        candidate_id,
        cache_status=CACHE_STATUS_REMOTE_DISPATCHED,
        remote_job_id=31,
    )

    token = store.acquire_lease(DATE, "case-k", 60)
    built = build_frozen_audit_artifact(
        store, token, output_identity=f"daily-final-{DATE.replace('-', '')}"
    )

    assert built.readiness.ready is True
    assert built.readiness.customer_absent_skipped == 1
    assert built.artifact is not None
    response = built.artifact.audit_response
    results = response.record_results
    assert len(results) == 2

    # 1:1 逐行对位:record_results 顺序与冻结行顺序完全一致(投影未乱序/漏行)
    frozen_vs = [
        str(item["row"]["vCommodityId"])
        for item in store.ordered_snapshot_items(snapshot["id"])
    ]
    assert [result.vCommodityId for result in results] == frozen_vs
    assert frozen_vs == ["v-absent-0", "v-ok"]

    # absent 行:最终 NO_ACTION 投影,全程无人工、无执行
    absent = results[0]
    assert absent.price_vs_customer_check == "CUSTOMER_ABSENT_SKIPPED"
    assert absent.primary_action == WorkflowAction.NO_ACTION
    assert absent.suggested_actions == [WorkflowAction.NO_ACTION]
    assert absent.needs_human_approval is False
    assert absent.execution_allowed is False
    assert absent.executed is False
    assert absent.reason == "客户侧权威快照当前无此单据，本轮无需处理"
    assert "customerPriceStatus=CUSTOMER_NOT_FOUND" in absent.evidence

    # 普通行保留原 verdict:仍需人工审批,绝不被缺席投影波及
    normal = results[1]
    assert normal.price_vs_customer_check != "CUSTOMER_ABSENT_SKIPPED"
    assert normal.needs_human_approval is True
    assert normal.primary_action == WorkflowAction.MANUAL_CHECK

    summary = response.summary
    assert summary["customer_absent_skipped_count"] == 1
    assert summary["customer_price_missing_count"] == 0

    # finalize 未触碰 prewarm 侧:候选仍 #10 已派发态,文档仍两条
    row = prewarm.candidate_by_id(candidate_id)
    assert row["cache_status"] == CACHE_STATUS_REMOTE_DISPATCHED
    assert row["remote_job_id"] == 31
    with prewarm._reader() as connection:
        docs = [
            dict(r) for r in connection.execute(
                "SELECT document_id FROM prewarm_documents ORDER BY document_id"
            ).fetchall()
        ]
    assert [d["document_id"] for d in docs] == ["doc-sidecar-a", "doc-sidecar-b"]
    store.release_lease(token)
