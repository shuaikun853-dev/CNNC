"""Case 7: STEP1 tick isolation gate for the Issue #9 prewarm sidecar.

契约:``TickDependencies.prewarm_intake`` 只是一个 best-effort 入队 seam——
hook 抛异常(包括其内部 prewarm store / sqlite 全炸)绝不能改变 tick 的
outcome/phase_trace/既有 counters/STEP1 快照数据;默认(None)逐字节等价于
现状;生产装配(build_default_dependencies, paths 非 None)时 hook 落到
``<runtime_root>/customer_prewarm.sqlite3``;drain(不传 paths)不接 prewarm。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import app.services.incremental_audit_runtime as runtime_module
from app.services.customer_prewarm_discovery import (
    intake_documents,
    prewarm_rows_from_customer_rows,
)
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.services.incremental_audit_runtime import (
    build_default_dependencies,
    runtime_paths,
)
from app.workflows.incremental_price_audit import (
    DiscoveredState,
    TickConfig,
    TickDependencies,
    run_incremental_tick,
)


@dataclass
class Clock:
    value: float = 0

    def monotonic(self) -> float:
        return self.value


def _row(v="doc-1", sku="sku-1", **extra):
    return {
        "vCommodityId": v,
        "documentId": v,
        "skuId": sku,
        "extLink": " https://shop.example/item ",
        "fourCategoryId": "cat",
        "list_reference_price": "10",
        "queryStr": "title",
        "skuName": "title",
        "mall_key": "mall-a",
        **extra,
    }


def _config(clock, *, date="2026-08-11", budget=60, reserve=10):
    return TickConfig(
        business_date=date,
        runtime_budget_seconds=budget,
        heavy_work_reserve_seconds=reserve,
        lease_ttl_seconds=90,
    )


def _healthy_detail(_row, _timeout):
    # Round-3 contract: healthy detail evidence is READY (price + FOUND).
    return {
        "customerPrice": "10",
        "customerPriceStatus": "FOUND",
        "customerPriceSource": "DETAIL_REFERENCE_INFOS",
        "customerPriceWarning": "",
        "customerPriceEvidence": "reference price 1 = 10",
    }


def _deps(rows, clock, *, prewarm_intake=None):
    def discover(timeout):
        return DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
            headers=["vCommodityId"],
            rows=rows,
            source_fingerprints=[
                f"source-{row_value['vCommodityId']}-{row_value['skuId']}" for row_value in rows
            ],
            customer_fingerprints=[
                f"customer-{row_value['fourCategoryId']}-{row_value['list_reference_price']}"
                for row_value in rows
            ],
            evidence={"saas_count": len(rows), "customer_count": len(rows)},
            # 显式声明 customer-only prewarm 通道(生产 discoverer 语义):
            # prewarm_rows=None 表示"无通道",intake 必须整跳跳过。
            prewarm_rows=prewarm_rows_from_customer_rows(rows),
        )

    def model(_row, _timeout):
        return {"match_result": "SAME"}

    return TickDependencies(
        discover=discover,
        detail=_healthy_detail,
        model=model,
        prewarm_intake=prewarm_intake,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
    )


def _strip_prewarm(counters):
    return {key: value for key, value in counters.items() if not key.startswith("prewarm_")}


def _strip_volatile_items(items):
    # 两次独立 tick 的 audit_items.created_at 是各自 store 写入时刻的墙钟
    # 时间戳,不可能逐字节相等;等价性比较必须剥离时间性字段。
    return [
        {key: value for key, value in item.items() if key != "created_at"}
        for item in items
    ]


def _run_tick(tmp_path, name, rows, clock, *, prewarm_intake=None):
    store = DailyAuditStore(tmp_path / f"{name}.sqlite3")
    summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / f"{name}.lock"),
        _config(clock),
        _deps(rows, clock, prewarm_intake=prewarm_intake),
    )
    return store, summary


# ---- Case 7: hook 抛异常时 tick 结果逐字节等价 ---------------------------------------


def test_case7_tick_outcome_identical_when_intake_raises(tmp_path):
    rows = [_row("doc-1"), _row("doc-2")]

    def exploding_intake(business_date, discovered_rows):
        raise RuntimeError("boom")

    clock = Clock()
    baseline_store, baseline = _run_tick(tmp_path, "baseline", rows, clock)
    failing_store, failing = _run_tick(
        tmp_path, "failing", rows, clock, prewarm_intake=exploding_intake
    )

    assert failing.outcome == baseline.outcome
    assert failing.phase_trace == baseline.phase_trace
    assert _strip_prewarm(failing.counters) == _strip_prewarm(baseline.counters)
    assert failing.counters["prewarm_intake_failures"] == 1
    assert "prewarm_intake_ok" not in failing.counters
    # STEP1 数据不受影响:两次运行的权威快照明细完全一致(剥离墙钟时间戳)
    baseline_snapshot = baseline_store.list_snapshots(_config(clock).business_date)[-1]
    failing_snapshot = failing_store.list_snapshots(_config(clock).business_date)[-1]
    assert _strip_volatile_items(
        baseline_store.ordered_snapshot_items(int(baseline_snapshot["id"]))
    ) == _strip_volatile_items(
        failing_store.ordered_snapshot_items(int(failing_snapshot["id"]))
    )


def test_case7_store_explosion_isolated(tmp_path, monkeypatch):
    rows = [_row("doc-1"), _row("doc-2")]
    prewarm_store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")

    def broken_upsert_document(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such prewarm store")

    monkeypatch.setattr(prewarm_store, "upsert_document", broken_upsert_document)

    def intake(business_date, discovered_rows):
        intake_documents(prewarm_store, business_date, discovered_rows, source="event")

    clock = Clock()
    baseline_store, baseline = _run_tick(tmp_path, "baseline", rows, clock)
    failing_store, failing = _run_tick(tmp_path, "failing", rows, clock, prewarm_intake=intake)

    assert failing.outcome == baseline.outcome
    assert failing.phase_trace == baseline.phase_trace
    assert _strip_prewarm(failing.counters) == _strip_prewarm(baseline.counters)
    assert failing.counters["prewarm_intake_failures"] == 1
    baseline_snapshot = baseline_store.list_snapshots(_config(clock).business_date)[-1]
    failing_snapshot = failing_store.list_snapshots(_config(clock).business_date)[-1]
    assert _strip_volatile_items(
        baseline_store.ordered_snapshot_items(int(baseline_snapshot["id"]))
    ) == _strip_volatile_items(
        failing_store.ordered_snapshot_items(int(failing_snapshot["id"]))
    )


def test_case7_hook_receives_discovered_rows(tmp_path):
    rows = [_row("doc-1"), _row("doc-2")]
    received: list[tuple[str, list[dict]]] = []

    def recording_intake(business_date, discovered_rows):
        received.append((business_date, list(discovered_rows)))

    config = _config(Clock())
    store = DailyAuditStore(tmp_path / "state.sqlite3")
    summary = run_incremental_tick(
        store,
        ProcessLock(tmp_path / "tick.lock"),
        config,
        _deps(rows, Clock(), prewarm_intake=recording_intake),
    )
    assert summary.counters["prewarm_intake_ok"] == 1
    assert len(received) == 1
    business_date, hooked_rows = received[0]
    assert business_date == config.business_date
    assert [row["documentId"] for row in hooked_rows] == ["doc-1", "doc-2"]
    assert [row["fourCategoryId"] for row in hooked_rows] == ["cat", "cat"]


# ---- 生产装配:默认依赖接线 -----------------------------------------------------------


def test_default_dependencies_wire_prewarm_intake(tmp_path, monkeypatch):
    class SnapshotAdapterSpy:
        def __init__(self, saas_path, cnnc_path, *, auth_manager, cnnc_provider=None):
            pass

    monkeypatch.setattr(runtime_module, "CnncSaasSnapshotAdapter", SnapshotAdapterSpy)
    monkeypatch.setattr(runtime_module, "load_curl_command", lambda path: {})
    monkeypatch.setattr(runtime_module, "CnncLegacyZhaoshangAdapter", lambda: object())
    monkeypatch.setattr(
        runtime_module,
        "legacy_zhaoshang_bridge",
        lambda *_args, **_kwargs: (object(), object(), object(), lambda _j, _t: {}, None, None),
    )

    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    deps = build_default_dependencies(paths=paths, business_date="2026-08-11")
    assert deps.prewarm_intake is not None
    deps.prewarm_intake(
        "2026-08-11",
        [{"documentId": "DOC-W", "fourCategoryId": "C-1"}],
    )
    store_path = paths.run_dir.parent / "customer_prewarm.sqlite3"
    assert store_path == root / "customer_prewarm.sqlite3"
    assert store_path.is_file()
    pending = CustomerPrewarmStore(store_path).pending_documents(10)
    assert [row["document_id"] for row in pending] == ["DOC-W"]

    # drain-only(不传 paths)保持现状:不接 prewarm sidecar。
    default_deps = build_default_dependencies()
    assert default_deps.prewarm_intake is None


# ---- F1: prewarm store 懒构造(坏库绝不能炸 STEP1 依赖组装) --------------------------


def test_default_dependencies_construct_prewarm_store_lazily(tmp_path, monkeypatch):
    class ExplodingPrewarmStore:
        def __init__(self, *_args, **_kwargs):
            raise sqlite3.DatabaseError("corrupt")

    class SnapshotAdapterSpy:
        def __init__(self, saas_path, cnnc_path, *, auth_manager, cnnc_provider=None):
            pass

    monkeypatch.setattr(runtime_module, "CustomerPrewarmStore", ExplodingPrewarmStore)
    monkeypatch.setattr(runtime_module, "CnncSaasSnapshotAdapter", SnapshotAdapterSpy)
    monkeypatch.setattr(runtime_module, "load_curl_command", lambda path: {})
    monkeypatch.setattr(runtime_module, "CnncLegacyZhaoshangAdapter", lambda: object())
    monkeypatch.setattr(
        runtime_module,
        "legacy_zhaoshang_bridge",
        lambda *_args, **_kwargs: (object(), object(), object(), lambda _j, _t: {}, None, None),
    )

    root = tmp_path / "runtime"
    deps = build_default_dependencies(
        paths=runtime_paths("2026-08-11", root), business_date="2026-08-11"
    )
    # 损坏的 prewarm 库只能让 intake 调用计数失败,绝不能让 STEP1 依赖组装失败
    assert deps.prewarm_intake is not None
    # 懒构造:build 阶段不触碰 prewarm 库文件
    assert not (root / "customer_prewarm.sqlite3").exists()


def test_lazy_store_construction_failure_isolated_in_full_tick(tmp_path, monkeypatch):
    rows = [_row("doc-1"), _row("doc-2")]
    store_path = tmp_path / "prewarm-root" / "customer_prewarm.sqlite3"

    class ExplodingPrewarmStore:
        def __init__(self, *_args, **_kwargs):
            raise sqlite3.DatabaseError("corrupt")

    monkeypatch.setattr(runtime_module, "CustomerPrewarmStore", ExplodingPrewarmStore)
    intake = runtime_module._prewarm_intake_worker(store_path)

    clock = Clock()
    baseline_store, baseline = _run_tick(tmp_path, "baseline", rows, clock)
    failing_store, failing = _run_tick(tmp_path, "failing", rows, clock, prewarm_intake=intake)

    assert failing.outcome == baseline.outcome
    assert failing.phase_trace == baseline.phase_trace
    assert _strip_prewarm(failing.counters) == _strip_prewarm(baseline.counters)
    assert failing.counters["prewarm_intake_failures"] == 1
    assert "prewarm_intake_ok" not in failing.counters
    baseline_snapshot = baseline_store.list_snapshots(_config(clock).business_date)[-1]
    failing_snapshot = failing_store.list_snapshots(_config(clock).business_date)[-1]
    assert _strip_volatile_items(
        baseline_store.ordered_snapshot_items(int(baseline_snapshot["id"]))
    ) == _strip_volatile_items(
        failing_store.ordered_snapshot_items(int(failing_snapshot["id"]))
    )


def test_lazy_store_construction_retried_next_tick(tmp_path, monkeypatch):
    store_path = tmp_path / "prewarm-root" / "customer_prewarm.sqlite3"

    class ExplodingPrewarmStore:
        def __init__(self, *_args, **_kwargs):
            raise sqlite3.DatabaseError("corrupt")

    monkeypatch.setattr(runtime_module, "CustomerPrewarmStore", ExplodingPrewarmStore)
    intake = runtime_module._prewarm_intake_worker(store_path)
    rows = [_row("doc-1")]
    config = _config(Clock())
    first = run_incremental_tick(
        DailyAuditStore(tmp_path / "first.sqlite3"),
        ProcessLock(tmp_path / "first.lock"),
        config,
        _deps(rows, Clock(), prewarm_intake=intake),
    )
    assert first.counters["prewarm_intake_failures"] == 1
    assert not store_path.exists()

    # 构造失败只在下一次 intake 调用时重试(monkeypatch 撤除后走真库)
    monkeypatch.undo()
    second = run_incremental_tick(
        DailyAuditStore(tmp_path / "second.sqlite3"),
        ProcessLock(tmp_path / "second.lock"),
        config,
        _deps(rows, Clock(), prewarm_intake=intake),
    )
    assert second.counters["prewarm_intake_ok"] == 1
    assert store_path.is_file()
    pending = CustomerPrewarmStore(store_path).pending_documents(10)
    assert [row["document_id"] for row in pending] == ["doc-1"]


# ---- F2: prewarm 覆盖全部客户文档(含 customer-only;STEP1 权威行不动) ----------------


class _Snapshot:
    observed_at_utc = datetime(2026, 8, 11, 10, 1, tzinfo=UTC)
    saas_rows = (
        {"vCommodityId": "DOC-A", "skuId": "s1", "extLink": "https://shop.example/a"},
    )
    customer_rows = (
        {
            "documentId": "DOC-A",
            "fourCategoryId": "cat-a",
            "fourCategoryName": "A类",
            "commodityName": "commodity-a",
            "referenceLink": "https://ref.example/a",
        },
        {
            "documentId": "DOC-B",
            "fourCategoryId": "cat-b",
            "fourCategoryName": "B类",
            "commodityName": "commodity-b",
            "referenceLink": "https://ref.example/b",
        },
        {"documentId": "DOC-C", "fourCategoryId": ""},
    )
    saas_count = 1
    customer_count = 3
    page_evidence = ()
    source_label = "fake"


class _SnapshotAdapter:
    def fetch(self, **_kwargs):
        return _Snapshot()


def test_snapshot_discoverer_prewarm_rows_cover_all_customer_documents():
    state = runtime_module.snapshot_discoverer(_SnapshotAdapter())(10)

    # STEP1 权威快照不变:仍然只有 joined 的 saas 行,DOC-B/DOC-C 绝不出现
    assert [row.get("documentId") for row in state.rows] == ["DOC-A"]
    # sidecar 通道覆盖全部客户文档(按 documentId 排序,含 customer-only 与空 category)
    assert [(row["documentId"], row["fourCategoryId"]) for row in state.prewarm_rows] == [
        ("DOC-A", "cat-a"),
        ("DOC-B", "cat-b"),
        ("DOC-C", ""),
    ]
    doc_b = state.prewarm_rows[1]
    assert doc_b["fourCategoryName"] == "B类"
    assert doc_b["commodityName"] == "commodity-b"
    assert doc_b["referenceLink"] == "https://ref.example/b"


def test_tick_hook_receives_prewarm_rows_and_customer_only_reaches_store(tmp_path):
    received: list[tuple[str, list[dict]]] = []
    store_path = tmp_path / "prewarm-root" / "customer_prewarm.sqlite3"
    prewarm = CustomerPrewarmStore(store_path)

    def recording_intake(business_date, hooked_rows):
        received.append((business_date, list(hooked_rows)))
        intake_documents(prewarm, business_date, hooked_rows, source="event")

    config = _config(Clock())
    summary = run_incremental_tick(
        DailyAuditStore(tmp_path / "state.sqlite3"),
        ProcessLock(tmp_path / "tick.lock"),
        config,
        TickDependencies(
            discover=runtime_module.snapshot_discoverer(_SnapshotAdapter()),
            detail=_healthy_detail,
            model=lambda _row, _timeout: {"match_result": "SAME"},
            prewarm_intake=recording_intake,
            monotonic=Clock().monotonic,
            wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
        ),
    )
    assert summary.counters["prewarm_intake_ok"] == 1
    business_date, hooked = received[0]
    assert business_date == config.business_date
    assert [row["documentId"] for row in hooked] == ["DOC-A", "DOC-B", "DOC-C"]
    # 真实 store:customer-only 的 DOC-B 也被覆盖
    with prewarm._reader() as connection:
        stored = connection.execute(
            "SELECT * FROM prewarm_documents ORDER BY id"
        ).fetchall()
    by_id = {row["document_id"]: dict(row) for row in stored}
    assert set(by_id) == {"DOC-A", "DOC-B", "DOC-C"}
    assert by_id["DOC-B"]["four_category_id"] == "cat-b"


def test_empty_category_customer_document_is_parked_not_dropped(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    assert intake_documents(
        store, "2026-08-11", [{"documentId": "DOC-C", "fourCategoryId": ""}]
    ) == 1
    with store._reader() as connection:
        row = connection.execute(
            "SELECT * FROM prewarm_documents WHERE document_id='DOC-C'"
        ).fetchone()
    assert row is not None
    stored = dict(row)
    assert stored["detail_state"] == "PENDING"
    assert stored["four_category_id"] == ""
    # 空 category 文档停在 detail 扫描之外(WAITING_CATEGORY 语义),但绝不丢弃
    assert store.pending_documents(10) == []
