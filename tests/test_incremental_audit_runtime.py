from __future__ import annotations

import os
from datetime import UTC, datetime
import json
import subprocess
import sys
from pathlib import Path

import pytest

import app.services.incremental_audit_runtime as runtime_module
from app.services.batch_zhaoshang_adapter import CnncBatchZhaoshangAdapter
from app.services.incremental_audit_runtime import (
    _detail_worker,
    _ensure_batch_state,
    _load_model_configuration,
    _model_batch_worker,
    RuntimeConfigurationError,
    business_date_in_shanghai,
    legacy_zhaoshang_bridge,
    read_runtime_status,
    run_runtime_command,
    runtime_paths,
    snapshot_discoverer,
)
from app.services.legacy_zhaoshang_adapter import (
    CrawlRequest,
    HarvestStatus,
    LegacyHarvestOutcome,
    LegacyReconcileOutcome,
    LegacySubmitOutcome,
    PreparedLegacyJob,
    ReconcileStatus,
    SubmitStatus,
)
from app.services.incremental_audit_runtime import RuntimeResult
from app.services.daily_audit_store import FrozenSnapshotConflict
from app.services.incremental_audit_materializer import ArtifactConflictError
from app.services.title_match_model_adapter import TitleMatchModelResult
from app.workflows.incremental_price_audit import DiscoveredState, TickDependencies, TickSummary
from app.workflows.incremental_price_audit_finalize import (
    FinalizeAlreadyRunning,
    FinalizeBlocked,
    FinalizeReadiness,
)
from scripts import incremental_price_audit as cli


def _dependencies(events: list[str]) -> TickDependencies:
    def discover(timeout):
        events.append("discover")
        return DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC), headers=["vCommodityId"],
            rows=[{"vCommodityId": "v1", "documentId": "v1", "skuId": "s1", "extLink": "", "fourCategoryId": "c"}],
            source_fingerprints=["source"], customer_fingerprints=["customer"],
        )

    return TickDependencies(
        discover=discover,
        detail=lambda row, timeout: {"customerPrice": "10"},
        model=lambda row, timeout: {"match_result": "SAME", "confidence": 1, "model_match": "exact", "spec_match": True},
        wall_clock=lambda: datetime(2026, 8, 11, 10, 2, tzinfo=UTC),
    )


def test_shanghai_rollover_and_status_have_no_write_side_effects(tmp_path):
    assert business_date_in_shanghai(datetime(2026, 8, 11, 15, 59, tzinfo=UTC)) == "2026-08-11"
    assert business_date_in_shanghai(datetime(2026, 8, 11, 16, 0, tzinfo=UTC)) == "2026-08-12"
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    assert read_runtime_status(business_date="2026-08-11", runtime_root=tmp_path / "runtime")["state"] == "MISSING"
    assert not paths.run_dir.exists()


def test_detail_lock_is_shared_across_business_dates(tmp_path):
    first = runtime_paths("2026-08-11", tmp_path / "runtime")
    second = runtime_paths("2026-08-12", tmp_path / "runtime")

    assert first.lock_path != second.lock_path
    assert first.detail_lock_path == second.detail_lock_path
    assert first.detail_lock_path.parent == (tmp_path / "runtime").resolve()


def test_tick_uses_default_daily_path_and_zhaoshang_is_not_enabled_by_default(tmp_path):
    events: list[str] = []
    result = run_runtime_command(
        "tick", business_date="2026-08-11", runtime_root=tmp_path / "runtime", dependencies=_dependencies(events)
    )
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    assert events == ["discover"]
    assert paths.state_path.is_file()
    batch_state = (paths.run_dir / "BATCH_STATE.md").read_text(encoding="utf-8")
    assert "status: ACTIVE" in batch_state
    assert "tenant_id=188605, commodity_pool_id=222" in batch_state
    assert "zhaoshang_create=disabled" in batch_state
    assert not list(paths.run_dir.glob("*.xlsx"))
    assert result.tick.outcome in {"PENDING_RETRY", "COMPLETED"}


def test_finalize_reuses_tick_core_and_then_calls_materializer(tmp_path):
    events: list[str] = []
    calls = []

    def materialize(store, lock, business_date, output):
        calls.append((business_date, output))
        return object()

    result = run_runtime_command(
        "finalize", business_date="2026-08-11", runtime_root=tmp_path / "runtime",
        dependencies=_dependencies(events), materialize=materialize,
    )
    assert result.tick.frozen_snapshot_id is not None
    assert result.publication is not None
    assert calls and calls[0][0] == "2026-08-11"


def test_cli_help_does_not_construct_runtime_or_network(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "run_runtime_command", lambda *args, **kwargs: called.append(args))
    with pytest.raises(SystemExit) as exited:
        cli.main(["--help"])
    assert exited.value.code == 0
    assert called == []
    assert not list(tmp_path.iterdir())


def test_direct_script_help_bootstraps_project_imports_without_runtime_writes(tmp_path):
    script = Path("scripts/incremental_price_audit.py")
    completed = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    assert "{tick,finalize,status,doctor}" in completed.stdout
    assert not (tmp_path / "runtime").exists()


def test_injected_default_dependencies_never_enable_a_writer_without_a_planner(tmp_path):
    events: list[str] = []
    result = run_runtime_command(
        "tick", business_date="2026-08-11", runtime_root=tmp_path / "runtime",
        dependencies=_dependencies(events), enable_zhaoshang=False,
    )
    assert result.tick.counters["crawl_submitted"] == 0


def test_snapshot_discoverer_normalizes_raw_rows_and_forwards_tick_budget():
    captured = []

    class Snapshot:
        observed_at_utc = datetime(2026, 8, 11, 10, 1, tzinfo=UTC)
        saas_rows = ({"vCommodityId": "v1", "skuId": 7.0, "commodityName": "alias", "referenceLink": " https://x "},)
        customer_rows = ({"documentId": "v1", "fourCategoryId": "c", "referencePrice": "10"},)
        saas_count = customer_count = 1
        page_evidence = ()
        source_label = "fake"

    class Adapter:
        def fetch(self, **kwargs):
            captured.append(kwargs)
            return Snapshot()

    state = snapshot_discoverer(Adapter())(7.5)
    assert captured == [{"source_label": "incremental-runtime", "max_elapsed_seconds": 7.5}]
    assert state.rows[0]["skuId"] == "7"
    assert state.rows[0]["extLink"] == "https://x"
    assert state.source_fingerprints[0] == state.source_fingerprints[0]


def test_snapshot_discovery_persists_mall_eligibility_not_url_presence():
    class Snapshot:
        observed_at_utc = datetime(2026, 8, 11, 10, 1, tzinfo=UTC)
        saas_rows = (
            {"vCommodityId": "mapped-empty", "skuId": "s1", "vendorName": "mapped", "extLink": ""},
            {"vCommodityId": "unsupported", "skuId": "s2", "vendorName": "other", "extLink": "https://unsupported.invalid/p"},
        )
        customer_rows = (
            {"documentId": "mapped-empty", "fourCategoryId": "c"},
            {"documentId": "unsupported", "fourCategoryId": "c"},
        )
        saas_count = customer_count = 2
        page_evidence = ()
        source_label = "fake"

    class Adapter:
        def fetch(self, **_kwargs):
            return Snapshot()

    def resolve(row):
        if row["vCommodityId"] == "mapped-empty":
            return CrawlRequest("mall", "mall-source", "Mall", "s1", "", "legacy-v1")
        return None

    state = snapshot_discoverer(Adapter(), crawl_request_resolver=resolve)(10)
    assert [row["__crawl_required"] for row in state.rows] == [True, False]


def test_snapshot_discovery_resolves_policy_once_and_fingerprints_backend_changes():
    class Snapshot:
        observed_at_utc = datetime(2026, 8, 11, 10, 1, tzinfo=UTC)
        saas_rows = ({"vCommodityId": "v1", "skuId": "s1", "extLink": "https://item.invalid/1"},)
        customer_rows = ({"documentId": "v1", "fourCategoryId": "c"},)
        saas_count = customer_count = 1
        page_evidence = ()
        source_label = "fake"

    class Adapter:
        def fetch(self, **_kwargs):
            return Snapshot()

    calls: list[str] = []

    def resolver(version):
        def resolve(_row):
            calls.append(version)
            return CrawlRequest("mall", "mall-source", "Mall", "s1", "https://item.invalid/1", version)

        return resolve

    first = snapshot_discoverer(Adapter(), crawl_request_resolver=resolver("legacy-v1"))(10)
    second = snapshot_discoverer(Adapter(), crawl_request_resolver=resolver("legacy-v2"))(10)

    assert calls == ["legacy-v1", "legacy-v2"]
    assert first.rows[0]["__crawl_required"] is True
    assert first.rows[0]["__crawl_mall_key"] == "mall"
    assert first.rows[0]["__crawl_mall_source_id"] == "mall-source"
    assert first.rows[0]["__crawl_backend_version"] == "legacy-v1"
    assert first.source_fingerprints != second.source_fingerprints


def test_detail_worker_preserves_legacy_lookup_failed_branch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.incremental_audit_runtime.call_detail_api",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    worker = _detail_worker({"url": "https://detail.invalid"}, request_interval_seconds=0)

    result = worker(
        {
            "vCommodityId": "missing-customer",
            "fourCategoryId": "",
            "customer_lookup_status": "NOT_FOUND",
        },
        10,
    )

    assert result["customerPriceStatus"] == "LOOKUP_FAILED"
    assert calls == []


def test_finalize_publishes_frozen_scope_even_when_superseded_prefix_work_is_pending(tmp_path):
    events: list[str] = []
    states = [
        DiscoveredState(
            observed_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC), headers=["vCommodityId"],
            rows=[{"vCommodityId": "old", "documentId": "old", "skuId": "old", "extLink": "", "fourCategoryId": "c"}],
            source_fingerprints=["old-source"], customer_fingerprints=["old-customer"],
        ),
        DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC), headers=["vCommodityId"],
            rows=[{"vCommodityId": "new", "documentId": "new", "skuId": "new", "extLink": "", "fourCategoryId": "c"}],
            source_fingerprints=["new-source"], customer_fingerprints=["new-customer"],
        ),
    ]

    def discover(_timeout):
        return states.pop(0)

    deps = _dependencies(events)
    deps.discover = discover
    root = tmp_path / "runtime"
    run_runtime_command("tick", business_date="2026-08-11", runtime_root=root, dependencies=deps)
    from app.services.daily_audit_store import DailyAuditStore, ProcessLock

    paths = runtime_paths("2026-08-11", root)
    store = DailyAuditStore(paths.state_path)
    lock = ProcessLock(paths.lock_path)
    lock.acquire()
    token = store.acquire_lease("2026-08-11", "test-prefix", ttl_seconds=60, process_lock=lock)
    try:
        crawl = store.ensure_crawl_work(token, "mall", "old", "https://old.invalid", "v1", "fp", source_id="old")
        remote = store.prepare_remote_intent(
            token, "old-pending", "old-pending", "h", payload_json={}, input_path="", result_path=""
        )
        store.link_crawl_work_to_remote_job(token, int(crawl["id"]), int(remote["id"]))
    finally:
        store.release_lease(token)
        lock.release()

    published = []

    def materialize(*_args):
        published.append(True)
        return object()

    result = run_runtime_command(
        "finalize", business_date="2026-08-11", runtime_root=root, dependencies=deps, materialize=materialize
    )
    assert result.tick.outcome in {"WAITING_REMOTE", "PENDING_RETRY"}
    assert result.tick.frozen_snapshot_id is not None
    assert result.publication is not None
    assert published == [True]


class _BridgeAdapter:
    def __init__(self):
        self.prepare_calls = 0
        self.submit_calls = 0
        self.harvest_calls = 0
        self.reconcile_calls = 0

    def request_from_saas_row(self, row, *, backend_version):
        return CrawlRequest("mall", "mall-source", "Mall", str(row["skuId"]), str(row["extLink"]), backend_version)

    def prepare_job(self, business_date, requests, template_path, result_path, task_name, batch_state_path):
        self.prepare_calls += 1
        template_path = Path(template_path)
        result_path = Path(result_path)
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text("immutable", encoding="utf-8")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        canonical = tuple(requests)
        return PreparedLegacyJob(
            business_date, "mall", "mall-source", "Mall", task_name, template_path, result_path,
            Path(batch_state_path), canonical, "logical", "payload",
        )

    def submit(self, _job, *, timeout_seconds):
        self.submit_calls += 1
        return LegacySubmitOutcome(SubmitStatus.SUBMITTED, task_id="remote-1", task_number="number-1")

    def reconcile_unknown(self, _job, *, timeout_seconds):
        self.reconcile_calls += 1
        return LegacyReconcileOutcome(ReconcileStatus.FOUND, task_id="remote-unknown", task_number="number-unknown")

    def harvest(self, _job, *, timeout_seconds):
        self.harvest_calls += 1
        return LegacyHarvestOutcome(
            HarvestStatus.EXPORTED,
            result_map={"https://same.invalid/p": {"page_price_value": 999}},
            result_by_identity={
                ("sku-a", "https://same.invalid/p"): {"page_price_value": 11, "page_price_status": "OK"},
                ("sku-b", "https://same.invalid/p"): {"page_price_value": 22, "page_price_status": "OK"},
            },
        )


def test_legacy_bridge_rebuilds_persisted_job_and_fans_out_by_sku_url_identity(tmp_path):
    first = _BridgeAdapter()
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    propose, finalize, submit, _harvest = legacy_zhaoshang_bridge(first, paths=paths, business_date="2026-08-11")
    rows = [
        {"vCommodityId": "v-a", "skuId": "sku-a", "extLink": "https://same.invalid/p"},
        {"vCommodityId": "v-b", "skuId": "sku-b", "extLink": "https://same.invalid/p"},
    ]
    plan = propose([{"row": row} for row in rows], 10)[0]
    assert [member.source_id for member in plan.members] == ["v-a", "v-b"]
    finalized = finalize(plan, tuple(plan.members), 10)
    assert finalized.task_name.startswith("cnnc_current_opt_Mall_")
    marker = finalized.task_name.rsplit("_", 1)[-1]
    assert len(marker) == 14 and marker.isdigit()
    submitted = submit({"logical_key": finalized.logical_key}, [], 10)
    assert submitted["state"] == "SUBMITTED"
    assert first.submit_calls == 1

    # This represents remote_jobs persisted by the engine before a process crash.
    durable_job = {
        "id": 9, "logical_key": finalized.logical_key, "task_name": finalized.task_name,
        "payload_hash": finalized.payload_hash, "payload_json": json.dumps(finalized.payload_json), "input_path": finalized.input_path,
        "result_path": finalized.result_path, "state": "SUBMITTED", "remote_identity": "remote-1",
        "crawl_work": [
            {"id": 101, "sku_id": "sku-a", "exact_url": "https://same.invalid/p", "source_id": "v-a"},
            {"id": 102, "sku_id": "sku-b", "exact_url": "https://same.invalid/p", "source_id": "v-b"},
        ],
    }
    restarted = _BridgeAdapter()
    _p2, _f2, _s2, harvest = legacy_zhaoshang_bridge(restarted, paths=paths, business_date="2026-08-11")
    outcome = harvest([durable_job], 10)[9]
    assert restarted.prepare_calls == 1
    assert restarted.submit_calls == 0
    assert outcome["crawl_results"][101]["page_price_value"] == 11
    assert outcome["crawl_results"][102]["page_price_value"] == 22

    unknown = dict(durable_job, state="SUBMIT_UNKNOWN")
    reconciled = harvest([unknown], 10)[9]
    assert reconciled["state"] == "SUBMITTED"
    assert reconciled["remote_identity"] == "mall|remote-unknown|number-unknown"
    assert restarted.reconcile_calls == 1
    assert restarted.submit_calls == 0

    # A crash after the durable intent/link checkpoint but before POST has no
    # in-memory prepared cache. The next tick can safely reconstruct once.
    fresh_submit = _BridgeAdapter()
    _p3, _f3, submit_after_crash, _h3 = legacy_zhaoshang_bridge(
        fresh_submit, paths=paths, business_date="2026-08-11"
    )
    assert submit_after_crash(durable_job, [], 10)["state"] == "SUBMITTED"
    assert fresh_submit.prepare_calls == 1
    assert fresh_submit.submit_calls == 1


def test_batch_bridge_persists_slice_payload_and_reconciles_unknown_without_resubmit(tmp_path):
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    post_calls = []
    progress_calls = []
    batch = CnncBatchZhaoshangAdapter(
        write_enabled=True,
        post_json=lambda *_args: (post_calls.append(1) or (_ for _ in ()).throw(TimeoutError())),
        get_json=lambda _url, params, _timeout: (
            progress_calls.append(params) or (
                200,
                {
                    "rspCode": "0000",
                    "success": True,
                    "data": {
                        "batchNumber": params["batchNumber"],
                        "mallLogo": "mall",
                        "totalCount": 1,
                        "completedCount": 0,
                        "pendingCount": 1,
                        "runningCount": 0,
                        "failedCount": 0,
                        "pendingStoreCount": 0,
                    },
                },
            )
        ),
    )
    legacy = _BridgeAdapter()
    propose, finalize, submit, harvest = runtime_module.batch_zhaoshang_bridge(
        batch, legacy, paths=paths, business_date="2026-08-11"
    )
    row = {"vCommodityId": "v-a", "skuId": "sku-a", "extLink": "https://same.invalid/p"}
    plan = propose([{"row": row}], 10)[0]
    finalized = finalize(plan, tuple(plan.members), 10)
    persisted = {
        "id": 3,
        "logical_key": finalized.logical_key,
        "task_name": finalized.task_name,
        "payload_hash": finalized.payload_hash,
        "payload_json": json.dumps(finalized.payload_json),
        "state": "SUBMIT_UNKNOWN",
        "remote_identity": "",
    }

    with pytest.raises(TimeoutError):
        submit(persisted, [], 10)
    outcome = harvest([persisted], 10)[3]

    assert outcome["state"] == "SUBMITTED"
    assert post_calls == [1]
    assert progress_calls == [{"batchNumber": finalized.task_name}]


def test_legacy_bridge_honors_frozen_crawl_policy_and_fails_closed_on_mapping_drift(tmp_path):
    class MutableMappingAdapter(_BridgeAdapter):
        def __init__(self):
            super().__init__()
            self.source_id = "mall-source"
            self.resolve_calls: list[str] = []

        def request_from_saas_row(self, row, *, backend_version):
            self.resolve_calls.append(backend_version)
            return CrawlRequest(
                "mall", self.source_id, "Mall", str(row["skuId"]), str(row["extLink"]), backend_version
            )

    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    adapter = MutableMappingAdapter()
    propose, finalize, _submit, _harvest = legacy_zhaoshang_bridge(
        adapter, paths=paths, business_date="2026-08-11"
    )
    disabled = {
        "vCommodityId": "disabled",
        "skuId": "s0",
        "extLink": "https://item.invalid/0",
        "__crawl_required": False,
        "__crawl_mall_key": "",
        "__crawl_mall_source_id": "",
        "__crawl_backend_version": "",
    }
    assert propose([{"row": disabled}], 10) == []
    assert adapter.resolve_calls == []

    frozen = {
        "vCommodityId": "v1",
        "skuId": "s1",
        "extLink": "https://item.invalid/1",
        "__crawl_required": True,
        "__crawl_mall_key": "mall",
        "__crawl_mall_source_id": "mall-source",
        "__crawl_backend_version": "legacy-v2",
    }
    plan = propose([{"row": frozen}], 10)[0]
    assert adapter.resolve_calls == ["legacy-v2"]
    assert plan.members[0].backend_version == "legacy-v2"

    adapter.source_id = "changed-source"
    with pytest.raises(RuntimeConfigurationError, match="frozen crawl policy conflicts"):
        finalize(plan, tuple(plan.members), 10)


def test_legacy_task_name_is_stable_within_day_but_rolls_over_across_days(tmp_path):
    row = {"vCommodityId": "v1", "skuId": "s1", "extLink": "https://item.invalid/1"}

    def task_name(day):
        paths = runtime_paths(day, tmp_path / "runtime")
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        propose, finalize, _submit, _harvest = legacy_zhaoshang_bridge(
            _BridgeAdapter(), paths=paths, business_date=day
        )
        plan = propose([{"row": row}], 10)[0]
        return finalize(plan, tuple(plan.members), 10).task_name

    first = task_name("2026-08-11")
    repeated = task_name("2026-08-11")
    next_day = task_name("2026-08-12")

    assert first == repeated
    assert first != next_day
    assert first.startswith("cnnc_current_opt_Mall_")
    assert len(first.rsplit("_", 1)[-1]) == 14


def test_batch_state_exclusive_create_never_overwrites_a_racing_writer(tmp_path, monkeypatch):
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    batch_state = paths.run_dir / "BATCH_STATE.md"
    batch_state.write_text("external writer won\n", encoding="utf-8")
    original_exists = Path.exists

    def racing_exists(path):
        if path == batch_state:
            return False
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", racing_exists)
    _ensure_batch_state(paths, enable_zhaoshang=False)

    assert batch_state.read_text(encoding="utf-8") == "external writer won\n"


def test_finalize_updates_batch_state_and_preserves_external_events(tmp_path):
    root = tmp_path / "runtime"
    events: list[str] = []
    run_runtime_command("tick", business_date="2026-08-11", runtime_root=root, dependencies=_dependencies(events))
    paths = runtime_paths("2026-08-11", root)
    external_event = '- `{"action":"PRIORITY_UPDATED","mall":"jd","task_name":"task","task_id":"1","task_number":"2","affected_rows":1}`'
    with (paths.run_dir / "BATCH_STATE.md").open("a", encoding="utf-8") as stream:
        stream.write(external_event + "\n")
    output = tmp_path / "formal.xlsx"

    result = run_runtime_command(
        "finalize",
        business_date="2026-08-11",
        runtime_root=root,
        dependencies=_dependencies(events),
        enable_zhaoshang=True,
        output_path=output,
        materialize=lambda *_args: object(),
    )

    assert result.publication is not None
    batch_state = (paths.run_dir / "BATCH_STATE.md").read_text(encoding="utf-8")
    assert "status: COMPLETE" in batch_state
    assert "authorization: zhaoshang_create=enabled" in batch_state
    assert f"output: {output.resolve()}" in batch_state
    assert "blocker: none" in batch_state
    assert external_event in batch_state


def test_finalize_block_updates_batch_state_with_recoverable_blocker(tmp_path):
    root = tmp_path / "runtime"

    def blocked(*_args):
        raise FinalizeBlocked("frozen crawl work is not ready")

    result = run_runtime_command(
        "finalize",
        business_date="2026-08-11",
        runtime_root=root,
        dependencies=_dependencies([]),
        materialize=blocked,
    )

    assert result.publication_blocked == "frozen crawl work is not ready"
    batch_state = (runtime_paths("2026-08-11", root).run_dir / "BATCH_STATE.md").read_text(encoding="utf-8")
    assert "status: BLOCKED" in batch_state
    assert "blocker: frozen crawl work is not ready" in batch_state
    assert '"counters":' in batch_state
    assert '"backlog":' in batch_state
    assert '"error_type": "FinalizeBlocked"' in batch_state


def test_finalize_pending_remote_is_a_recoverable_active_drain(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "_configured_model_name", lambda: "deployed-model")
    monkeypatch.setattr(
        runtime_module,
        "run_incremental_tick",
        lambda *_args, **_kwargs: TickSummary(
            "WAITING_REMOTE", (), {}, {"remote_pending": 1}, 7
        ),
    )

    def pending(*_args):
        raise FinalizeBlocked(
            "frozen snapshot still has mandatory pending or invalid work",
            FinalizeReadiness(crawl_pending=1),
        )

    result = run_runtime_command(
        "finalize",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
        materialize=pending,
    )

    assert result.tick.outcome == "WAITING_REMOTE"
    assert result.publication_blocked is None
    state = (runtime_paths("2026-08-11", tmp_path / "runtime").run_dir / "BATCH_STATE.md").read_text(
        encoding="utf-8"
    )
    assert "status: ACTIVE" in state
    assert "blocker: none" in state


@pytest.mark.parametrize(
    "conflict",
    [
        FrozenSnapshotConflict("frozen output path conflicts"),
        ArtifactConflictError("published artifact conflicts"),
    ],
)
def test_finalize_expected_publication_conflict_is_actionable_and_persisted(
    tmp_path, conflict
):
    def blocked(*_args):
        raise conflict

    result = run_runtime_command(
        "finalize",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
        materialize=blocked,
    )

    assert result.publication_blocked == str(conflict)
    assert result.publication_error_type == type(conflict).__name__
    state = (runtime_paths("2026-08-11", tmp_path / "runtime").run_dir / "BATCH_STATE.md").read_text(
        encoding="utf-8"
    )
    assert "status: BLOCKED" in state
    assert f'"error_type": "{type(conflict).__name__}"' in state


def test_finalize_materializer_overlap_is_normal_defer(tmp_path):
    def overlapping(*_args):
        raise FinalizeAlreadyRunning("another finalizer owns the lock")

    result = run_runtime_command(
        "finalize",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
        materialize=overlapping,
    )

    assert result.tick.outcome == "ALREADY_RUNNING"
    assert result.publication_blocked is None


def test_finalize_never_materializes_after_tick_budget_exhaustion(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "_configured_model_name", lambda: "deployed-model")
    monkeypatch.setattr(
        runtime_module,
        "run_incremental_tick",
        lambda *_args, **_kwargs: TickSummary("BUDGET_EXHAUSTED", (), {}, {}, 7),
    )

    result = run_runtime_command(
        "finalize",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
        materialize=lambda *_args: pytest.fail("budget-exhausted finalize must not materialize"),
        monotonic=lambda: 100.0,
    )

    assert result.tick.outcome == "BUDGET_EXHAUSTED"
    assert result.publication is None


def test_finalize_reserves_wall_budget_before_starting_materialization(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "_configured_model_name", lambda: "deployed-model")
    monkeypatch.setattr(
        runtime_module,
        "run_incremental_tick",
        lambda *_args, **_kwargs: TickSummary("COMPLETED", ("checkpoint",), {}, {}, 7),
    )
    ticks = iter((100.0, 230.0))

    result = run_runtime_command(
        "finalize",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        runtime_budget_seconds=180,
        dependencies=_dependencies([]),
        materialize=lambda *_args: pytest.fail("finalize must preserve the materialization reserve"),
        monotonic=lambda: next(ticks),
    )

    assert result.tick.outcome == "BUDGET_EXHAUSTED"
    assert result.tick.phase_trace[-1] == "publication_deferred"


def test_batch_state_lock_timeout_does_not_reverse_checkpointed_tick(tmp_path, monkeypatch):
    class UnavailableLock:
        def __enter__(self):
            raise TimeoutError("busy")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(runtime_module, "_batch_state_update_lock", lambda *_args, **_kwargs: UnavailableLock())

    result = run_runtime_command(
        "tick",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
    )

    assert result.tick.outcome in {"PENDING_RETRY", "COMPLETED"}
    assert runtime_paths("2026-08-11", tmp_path / "runtime").state_path.is_file()


def test_missing_batch_state_bridge_uses_normal_state_document(tmp_path):
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    adapter = _BridgeAdapter()
    propose, finalize, _submit, _harvest = legacy_zhaoshang_bridge(
        adapter, paths=paths, business_date="2026-08-11"
    )
    row = {"vCommodityId": "v1", "skuId": "s1", "extLink": "https://item.invalid/1"}
    plan = propose([{"row": row}], 10)[0]

    finalize(plan, tuple(plan.members), 10)

    state = (paths.run_dir / "BATCH_STATE.md").read_text(encoding="utf-8")
    assert "step: STEP_1_PRICE_AUDIT" in state
    assert "scope: tenant_id=188605, commodity_pool_id=222" in state
    assert "authorization: zhaoshang_create=enabled" in state


def test_model_configuration_load_does_not_mutate_process_environment(tmp_path, monkeypatch):
    names = (
        "TITLE_MATCH_MODEL_API_KEY",
        "TITLE_MATCH_MODEL_BASE_URL",
        "TITLE_MATCH_MODEL_NAME",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "model.env"
    path.write_text(
        "TITLE_MATCH_MODEL_API_KEY=secret\n"
        "TITLE_MATCH_MODEL_BASE_URL=https://model.invalid/v1\n"
        "TITLE_MATCH_MODEL_NAME=model-name\n",
        encoding="utf-8",
    )

    configuration = _load_model_configuration(path)

    assert configuration["TITLE_MATCH_MODEL_API_KEY"] == "secret"
    assert all(name not in os.environ for name in names)


def test_model_batch_worker_restores_temporary_model_environment(monkeypatch):
    configuration = {
        "TITLE_MATCH_MODEL_API_KEY": "secret",
        "TITLE_MATCH_MODEL_BASE_URL": "https://model.invalid/v1",
        "TITLE_MATCH_MODEL_NAME": "model-name",
    }
    for name in configuration:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "app.services.incremental_audit_runtime._load_model_configuration",
        lambda: configuration,
    )

    def call(requests, **_kwargs):
        assert {name: os.environ.get(name) for name in configuration} == configuration
        return {
            index: TitleMatchModelResult(match_result="SAME", confidence=1, model_match="exact")
            for index in range(len(requests))
        }

    monkeypatch.setattr("app.services.incremental_audit_runtime.call_title_match_model_batch", call)
    result = _model_batch_worker([{"queryStr": "q", "skuName": "s"}], 5)

    assert result[0]["match_result"] == "SAME"
    assert all(name not in os.environ for name in configuration)


def test_runtime_uses_configured_model_name_in_tick_config(tmp_path, monkeypatch):
    captured: list[str] = []

    def run_tick(_store, _lock, config, _dependencies, *, finalize):
        captured.append(config.model_name)
        return TickSummary("COMPLETED", (), {}, {}, None)

    monkeypatch.setattr(runtime_module, "_configured_model_name", lambda: "deployed-model-name")
    monkeypatch.setattr(runtime_module, "run_incremental_tick", run_tick)

    run_runtime_command(
        "tick",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
    )

    assert captured == ["deployed-model-name"]


def test_runtime_uses_content_addressed_model_contract_versions(tmp_path, monkeypatch):
    captured = []

    def run_tick(_store, _lock, config, _dependencies, *, finalize):
        captured.append((config.model_prompt_version, config.model_schema_version))
        return TickSummary("COMPLETED", (), {}, {}, None)

    monkeypatch.setattr(runtime_module, "run_incremental_tick", run_tick)

    run_runtime_command(
        "tick",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
    )

    assert captured == [(
        runtime_module.TITLE_MATCH_PROMPT_VERSION,
        runtime_module.TITLE_MATCH_SCHEMA_VERSION,
    )]


def test_cli_forwards_explicit_zhaoshang_backend(monkeypatch):
    captured = []
    summary = TickSummary("COMPLETED", (), {}, {}, None)
    monkeypatch.setattr(
        cli,
        "run_runtime_command",
        lambda *_args, **kwargs: (captured.append(kwargs) or RuntimeResult(summary)),
    )
    monkeypatch.setattr(cli, "collect_runtime_status", lambda **_kwargs: {"state": "OPEN"})
    monkeypatch.setattr(cli, "write_daily_status", lambda *_args, **_kwargs: Path("daily_status.json"))

    assert cli.main([
        "tick", "--business-date", "2026-08-11",
        "--enable-remote-writes", "--zhaoshang-backend", "batch",
    ]) == 0

    assert captured[0]["enable_zhaoshang"] is True
    assert captured[0]["zhaoshang_backend"] == "batch"


@pytest.mark.parametrize(
    ("outcome", "publication_blocked", "expected"),
    [
        ("ALREADY_RUNNING", None, 0),
        ("WAITING_REMOTE", None, 0),
        ("FAILED", None, 2),
        ("MANUAL_REVIEW_REQUIRED", None, 2),
        ("DISCOVERY_INCOMPLETE", None, 2),
        ("COMPLETED", "formal workbook is blocked", 2),
    ],
)
def test_cli_exit_code_distinguishes_overlap_from_actionable_failure(
    monkeypatch, outcome, publication_blocked, expected
):
    summary = TickSummary(outcome, (), {}, {}, None)
    monkeypatch.setattr(
        cli,
        "run_runtime_command",
        lambda *_args, **_kwargs: RuntimeResult(summary, publication_blocked=publication_blocked),
    )

    assert cli.main(["tick", "--business-date", "2026-08-11"]) == expected


def test_cli_publication_success_wins_over_superseded_backlog_outcome(monkeypatch):
    summary = TickSummary("MANUAL_REVIEW_REQUIRED", (), {}, {"stage_terminal": 1}, 9)
    monkeypatch.setattr(
        cli,
        "run_runtime_command",
        lambda *_args, **_kwargs: RuntimeResult(summary, publication=object()),
    )

    assert cli.main(["finalize", "--business-date", "2026-08-11"]) == 0
