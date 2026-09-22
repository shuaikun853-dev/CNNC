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
    LegacyPriorityOutcome,
    LegacyReconcileOutcome,
    LegacySubmitOutcome,
    PreparedLegacyJob,
    PriorityStatus,
    ReconcileStatus,
    SubmitStatus,
)
from app.services.incremental_audit_runtime import RuntimeResult
from app.services.daily_audit_store import (
    DailyAuditStore,
    FrozenSnapshotConflict,
    LeaseUnavailable,
    ProcessLock,
)
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
            evidence={"saas_count": 1, "customer_count": 1},
        )

    return TickDependencies(
        discover=discover,
        # Round-3 contract: healthy detail evidence is READY (price + FOUND).
        detail=lambda row, timeout: {
            "customerPrice": "10", "customerPriceStatus": "FOUND",
            "customerPriceSource": "DETAIL_REFERENCE_INFOS", "customerPriceWarning": "",
            "customerPriceEvidence": "参考价1=10",
        },
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_runtime_paths_preserves_explicit_junction_alias(tmp_path):
    target = tmp_path / "physical-runtime"
    target.mkdir()
    alias = tmp_path / "runtime-alias"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
        check=True,
        capture_output=True,
    )

    paths = runtime_paths("2026-08-11", alias)

    assert paths.run_dir == alias.absolute() / "2026-08-11"
    assert paths.run_dir != target.resolve() / "2026-08-11"


def test_project_resources_and_default_runtime_root_do_not_depend_on_cwd(
    tmp_path, monkeypatch
):
    cwd_data = tmp_path / "data"
    cwd_data.mkdir()
    for name in (
        "latest_saas_sameAudit_queryAll.curl",
        "latest_cnnc_query.curl",
        "detail_request.curl",
    ):
        (cwd_data / name).write_text("curl https://cwd.invalid", encoding="utf-8")
    (cwd_data / "model.env").write_text(
        "TITLE_MATCH_MODEL_API_KEY=cwd-key\n"
        "TITLE_MATCH_MODEL_BASE_URL=https://cwd.invalid/v1\n"
        "TITLE_MATCH_MODEL_NAME=cwd-model\n",
        encoding="utf-8",
    )
    project_model_env = tmp_path / "isolated-project-model.env"
    project_model_env.write_text(
        "TITLE_MATCH_MODEL_API_KEY=fixture-not-a-secret\n"
        "TITLE_MATCH_MODEL_BASE_URL=https://project-fixture.invalid/v1\n"
        "TITLE_MATCH_MODEL_NAME=project-fixture-model\n", encoding="utf-8"
    )
    read_configuration = runtime_module._parse_env_file

    def read_project_fixture(path):
        assert Path(path) == runtime_module.PROJECT_DATA_ROOT / "model.env"
        return read_configuration(project_model_env)

    monkeypatch.setattr(runtime_module, "_parse_env_file", read_project_fixture)
    for key in runtime_module._MODEL_CONFIGURATION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)

    captured = {}

    class SnapshotAdapterSpy:
        def __init__(self, saas_path, cnnc_path, *, auth_manager, cnnc_provider=None):
            captured["saas"] = Path(saas_path)
            captured["cnnc"] = Path(cnnc_path)

    monkeypatch.setattr(runtime_module, "CnncSaasSnapshotAdapter", SnapshotAdapterSpy)
    monkeypatch.setattr(
        runtime_module,
        "load_curl_command",
        lambda path: captured.setdefault("detail", Path(path)) or {},
    )

    runtime_module.build_default_dependencies()
    expected_configuration = runtime_module._read_model_configuration(
        runtime_module.PROJECT_DATA_ROOT / "model.env"
    )
    actual_configuration = runtime_module._load_model_configuration()
    paths = runtime_paths("2026-08-11")

    assert runtime_module.PROJECT_ROOT == Path(runtime_module.__file__).resolve().parents[2]
    assert runtime_module.PROJECT_DATA_ROOT == runtime_module.PROJECT_ROOT / "data"
    assert captured == {
        "saas": runtime_module.PROJECT_DATA_ROOT / "latest_saas_sameAudit_queryAll.curl",
        "cnnc": runtime_module.PROJECT_DATA_ROOT / "latest_cnnc_query.curl",
        "detail": runtime_module.PROJECT_DATA_ROOT / "detail_request.curl",
    }
    assert all(
        actual_configuration.get(key) == expected_configuration.get(key)
        for key in runtime_module._MODEL_CONFIGURATION_KEYS
    )
    assert actual_configuration["TITLE_MATCH_MODEL_NAME"] != "cwd-model"
    assert paths.run_dir == (
        runtime_module.PROJECT_ROOT
        / "runtime"
        / "incremental_price_audit"
        / "2026-08-11"
    )


def test_finalize_dependency_construction_from_non_project_cwd_is_offline(
    tmp_path, monkeypatch
):
    project_data = tmp_path / "isolated-project-data"
    project_data.mkdir()
    for name in (
        "latest_saas_sameAudit_queryAll.curl",
        "latest_cnnc_query.curl",
        "detail_request.curl",
    ):
        (project_data / name).write_text(
            "curl https://project-fixture.invalid/read-only", encoding="utf-8"
        )
    monkeypatch.setattr(runtime_module, "PROJECT_DATA_ROOT", project_data)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        runtime_module,
        "run_incremental_tick",
        lambda *_args, **_kwargs: TickSummary(
            "FINALIZE_SNAPSHOT_NOT_READY", (), {}, {}, None
        ),
    )

    result = run_runtime_command(
        "finalize",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
    )

    assert result.tick.outcome == "FINALIZE_SNAPSHOT_NOT_READY"


@pytest.mark.parametrize("command", ["tick", "finalize", "drain-only"])
def test_daily_production_writer_lock_blocks_all_writer_commands_before_lifecycle(
    tmp_path, command, monkeypatch
):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    DailyAuditStore(paths.state_path).ensure_run("2026-08-11")
    before = paths.state_path.read_bytes()
    owner = ProcessLock(paths.lock_path).acquire(owner={
        "business_date": "2026-08-11",
        "pid": 12345,
        "command": "drain-only",
        "started_at": "2026-08-11T20:30:00+08:00",
        "invocation": "manual",
    })
    monkeypatch.setattr(
        runtime_module,
        "_run_runtime_command_locked",
        lambda *_args, **_kwargs: pytest.fail(
            "lock conflict must not enter SQLite, priority, export, harvest, or finalize lifecycle"
        ),
    )
    events: list[str] = []
    try:
        result = run_runtime_command(
            command,
            business_date="2026-08-11",
            runtime_root=root,
            dependencies=_dependencies(events),
            invocation="scheduler",
        )
    finally:
        owner.release()

    assert result.tick.outcome == "ALREADY_RUNNING"
    assert result.lock_conflict is not None
    assert result.lock_conflict["requested"] == command
    assert result.lock_conflict["command"] == "drain-only"
    assert result.lock_conflict["pid"] == 12345
    assert events == []
    assert paths.state_path.read_bytes() == before
    assert not (paths.run_dir / "BATCH_STATE.md").exists()


def test_daily_writer_lock_is_scoped_by_business_date(tmp_path):
    root = tmp_path / "runtime"
    first = runtime_paths("2026-08-11", root)
    second = runtime_paths("2026-08-12", root)
    held = ProcessLock(first.lock_path).acquire()
    try:
        other = ProcessLock(second.lock_path).acquire()
        other.release()
    finally:
        held.release()


def test_runtime_releases_outer_writer_lock_when_lifecycle_raises(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)

    def fail(*_args, **_kwargs):
        raise RuntimeError("lifecycle crashed")

    monkeypatch.setattr(runtime_module, "_run_runtime_command_locked", fail)
    with pytest.raises(RuntimeError, match="lifecycle crashed"):
        run_runtime_command("tick", business_date="2026-08-11", runtime_root=root)

    assert ProcessLock.inspect(paths.lock_path)["active"] is False
    ProcessLock(paths.lock_path).acquire().release()


def test_formal_materialization_remains_inside_daily_writer_lock(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    DailyAuditStore(paths.state_path).ensure_run("2026-08-11")
    monkeypatch.setattr(
        runtime_module,
        "run_existing_remote_drain",
        lambda *_args, **_kwargs: TickSummary("DRAINED", (), {}, {}, 1),
    )
    monkeypatch.setattr(runtime_module, "_record_batch_result", lambda *_args, **_kwargs: None)
    observed = []

    def materialize(*_args):
        with pytest.raises(LeaseUnavailable):
            ProcessLock(paths.lock_path).acquire()
        observed.append("locked")
        return object()

    result = run_runtime_command(
        "drain-only",
        business_date="2026-08-11",
        runtime_root=root,
        dependencies=_dependencies([]),
        materialize=materialize,
        runtime_budget_seconds=300,
    )

    assert result.publication is not None
    assert observed == ["locked"]
    assert ProcessLock.inspect(paths.lock_path)["active"] is False


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
        source_stability_seconds=0,
    )
    assert result.tick.frozen_snapshot_id is not None
    assert result.publication is not None
    assert calls and calls[0][0] == "2026-08-11"


def test_tick_runs_daily_curl_guard_exactly_once_for_the_business_date(
    tmp_path, monkeypatch
):
    """tick（真正抓 CNNC customer 侧快照的命令）前先跑每日 cURL guard。"""
    calls = []

    def guard(*_args, **kwargs):
        calls.append(kwargs.get("business_date"))
        return "DAILY_CURL_ALREADY_FRESH"

    monkeypatch.setattr(
        runtime_module.cnnc_auth_refresh, "ensure_daily_curl_fresh", guard
    )
    events: list[str] = []

    run_runtime_command(
        "tick", business_date="2026-08-11", runtime_root=tmp_path / "runtime",
        dependencies=_dependencies(events)
    )

    assert calls == ["2026-08-11"]  # 恰好 1 次且 business_date==tick 日


def test_harvest_only_never_runs_daily_curl_guard(tmp_path, monkeypatch):
    """harvest-only 只收既有远端任务，不抓快照 → guard 零调用。"""
    calls = []

    def guard(*_args, **kwargs):
        calls.append(kwargs.get("business_date"))
        return "DAILY_CURL_ALREADY_FRESH"

    monkeypatch.setattr(
        runtime_module.cnnc_auth_refresh, "ensure_daily_curl_fresh", guard
    )
    monkeypatch.setattr(
        runtime_module,
        "run_harvest_only",
        lambda *_args, **_kwargs: TickSummary("HARVESTED", (), {}, {}, None),
    )
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    paths.run_dir.mkdir(parents=True)
    DailyAuditStore(paths.state_path).ensure_run("2026-08-11")

    result = run_runtime_command(
        "harvest-only", business_date="2026-08-11", runtime_root=root,
        dependencies=_dependencies([]), runtime_budget_seconds=300,
    )

    assert result.tick.outcome == "HARVESTED"
    assert calls == []  # harvest-only 分支零改动、零 guard 调用


def test_customer_count_mismatch_freezes_but_pending_readiness_does_not_materialize(tmp_path):
    events: list[str] = []
    deps = _dependencies(events)
    deps.discover = lambda _timeout: DiscoveredState(
        observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
        headers=["vCommodityId"],
        rows=[{"vCommodityId": "v1", "documentId": "v1", "skuId": "s1", "extLink": "", "fourCategoryId": "c"}],
        source_fingerprints=["source"],
        customer_fingerprints=["customer"],
        evidence={"saas_count": 1, "customer_count": 0},
    )
    deps.model = lambda _row, _timeout: (_ for _ in ()).throw(TimeoutError("retry later"))
    output = tmp_path / "must-not-exist.xlsx"

    result = run_runtime_command(
        "finalize",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=deps,
        output_path=output,
        source_stability_seconds=0,
    )

    assert result.tick.frozen_snapshot_id is not None
    assert result.tick.backlog["stage_pending"] == 1
    assert result.publication is None
    assert not output.exists()


def test_customer_count_mismatch_materializes_after_all_readiness_blockers_clear(tmp_path):
    events: list[str] = []
    deps = _dependencies(events)
    deps.discover = lambda _timeout: DiscoveredState(
        observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC),
        headers=["vCommodityId"],
        rows=[{"vCommodityId": "v1", "documentId": "v1", "skuId": "s1", "extLink": "", "fourCategoryId": "c"}],
        source_fingerprints=["source"],
        customer_fingerprints=["customer"],
        evidence={"saas_count": 1, "customer_count": 0},
    )
    calls: list[str] = []

    def materialize(*_args):
        calls.append("materialize")
        return object()

    result = run_runtime_command(
        "finalize",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=deps,
        materialize=materialize,
        source_stability_seconds=0,
    )

    assert result.tick.frozen_snapshot_id is not None
    assert all(value == 0 for key, value in result.tick.backlog.items() if key != "detail_terminal")
    assert result.publication is not None
    assert calls == ["materialize"]


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
    for command in (
        "tick",
        "finalize",
        "status",
        "doctor",
        "recover-model",
        "requeue-model-transient",
        "release-model-recovery",
        "recover-remote-harvest",
        "recover-partial-exact-harvest",
        "resolve-no-result",
        "recover-remote-priority",
        "harvest-existing-remote",
        "export-review",
    ):
        assert command in completed.stdout
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


def test_detail_worker_customer_absent_produces_customer_not_found(monkeypatch):
    """2026-09-11 rule change: a row whose customer_lookup_status is NOT_FOUND
    (the round's authoritative customer snapshot query succeeded and was
    complete) is "nothing to process this round" — the worker returns
    CUSTOMER_NOT_FOUND with skip evidence, and never issues a detail request."""
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

    assert result["customerPriceStatus"] == "CUSTOMER_NOT_FOUND"
    assert result["customerPrice"] is None
    assert result["customerPriceWarning"] == "customer export did not contain this documentId"
    assert result["customerPriceEvidence"] == (
        "authoritative customer snapshot round complete; documentId absent"
    )
    assert calls == []


def test_snapshot_fetch_failure_never_produces_customer_not_found_rows():
    """Fail-closed guard for the 2026-09-11 skip rule: only a *complete*
    authoritative customer snapshot round may declare a documentId absent.
    A technical fetch failure (count drift -> whole round voided -> budget
    exhaustion) propagates as a typed error, so the tick fails as a whole and
    no joined row with customer_lookup_status=NOT_FOUND can ever be produced
    by a broken fetch."""
    from app.services.cnnc_saas_snapshot_adapter import (
        SnapshotConsistencyDrift,
        SnapshotFetchExhausted,
    )

    class DriftingAdapter:
        cnnc_provider = None

        def fetch(self, **_kwargs):
            raise SnapshotFetchExhausted(
                "saas", 1, 3, 5.0,
                SnapshotConsistencyDrift("saas", "count changed during pagination"),
            )

    discover = snapshot_discoverer(DriftingAdapter())

    with pytest.raises(SnapshotFetchExhausted):
        discover(10)


def test_finalize_publishes_frozen_scope_even_when_superseded_prefix_work_is_pending(tmp_path):
    events: list[str] = []
    states = [
        DiscoveredState(
            observed_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC), headers=["vCommodityId"],
                rows=[{"vCommodityId": "old", "documentId": "old", "skuId": "old", "extLink": "", "fourCategoryId": "c"}],
                source_fingerprints=["old-source"], customer_fingerprints=["old-customer"],
                evidence={"saas_count": 1, "customer_count": 1},
        ),
        DiscoveredState(
            observed_at=datetime(2026, 8, 11, 10, 1, tzinfo=UTC), headers=["vCommodityId"],
                rows=[{"vCommodityId": "new", "documentId": "new", "skuId": "new", "extLink": "", "fourCategoryId": "c"}],
                source_fingerprints=["new-source"], customer_fingerprints=["new-customer"],
                evidence={"saas_count": 1, "customer_count": 1},
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
        "finalize", business_date="2026-08-11", runtime_root=root, dependencies=deps, materialize=materialize,
        source_stability_seconds=0,
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
        self.priority_calls = 0

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
        return LegacySubmitOutcome(
            SubmitStatus.SUBMITTED,
            task_id="remote-1",
            task_number="number-1",
            priority_evidence="updated",
        )

    def prioritize(self, _job, *, task_id, task_number, timeout_seconds):
        self.priority_calls += 1
        return LegacyPriorityOutcome(PriorityStatus.UPDATED, weight_type=-1)

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
    propose, finalize, submit, _harvest, _probe, _restart = legacy_zhaoshang_bridge(first, paths=paths, business_date="2026-08-11")
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
    _p2, _f2, _s2, harvest, _pr2, _rs2 = legacy_zhaoshang_bridge(restarted, paths=paths, business_date="2026-08-11")
    outcome = harvest([durable_job], 10)[9]
    assert restarted.prepare_calls == 1
    assert restarted.submit_calls == 0
    assert outcome["crawl_results"][101]["page_price_value"] == 11
    assert outcome["crawl_results"][102]["page_price_value"] == 22

    unknown = dict(durable_job, state="SUBMIT_UNKNOWN")
    reconciled = harvest([unknown], 10)[9]
    assert reconciled["state"] == "PRIORITY_PENDING"
    assert reconciled["remote_identity"] == "mall|remote-unknown|number-unknown"
    assert restarted.reconcile_calls == 1
    assert restarted.submit_calls == 0

    # A crash after the durable intent/link checkpoint but before POST has no
    # in-memory prepared cache. The next tick can safely reconstruct once.
    fresh_submit = _BridgeAdapter()
    _p3, _f3, submit_after_crash, _h3, _pr3, _rs3 = legacy_zhaoshang_bridge(
        fresh_submit, paths=paths, business_date="2026-08-11"
    )
    assert submit_after_crash(durable_job, [], 10)["state"] == "SUBMITTED"
    assert fresh_submit.prepare_calls == 1
    assert fresh_submit.submit_calls == 1


def test_legacy_bridge_emits_request_level_no_result_and_manual_markers(tmp_path):
    class PartialAdapter(_BridgeAdapter):
        def harvest(self, _job, *, timeout_seconds):
            self.harvest_calls += 1
            return LegacyHarvestOutcome(
                HarvestStatus.EXPORTED,
                result_by_identity={
                    ("sku-a", "https://same.invalid/a"): {
                        "page_price_value": 11,
                        "page_price_status": "OK",
                    }
                },
                no_result_identities=frozenset(
                    {("sku-b", "https://same.invalid/b")}
                ),
                manual_identities={
                    ("sku-c", "https://same.invalid/c"): "provenance ambiguity"
                },
            )

    adapter = PartialAdapter()
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    propose, finalize, _submit, harvest, _probe, _restart = legacy_zhaoshang_bridge(
        adapter, paths=paths, business_date="2026-08-11"
    )
    rows = [
        {"vCommodityId": f"v-{suffix}", "skuId": f"sku-{suffix}", "extLink": f"https://same.invalid/{suffix}"}
        for suffix in ("a", "b", "c")
    ]
    plan = propose([{"row": row} for row in rows], 10)[0]
    finalized = finalize(plan, tuple(plan.members), 10)
    durable_job = {
        "id": 19,
        "logical_key": finalized.logical_key,
        "task_name": finalized.task_name,
        "payload_hash": finalized.payload_hash,
        "payload_json": json.dumps(finalized.payload_json),
        "input_path": finalized.input_path,
        "result_path": finalized.result_path,
        "state": "SUBMITTED",
        "remote_identity": "mall|remote-1|number-1",
        "crawl_work": [
            {
                "id": index,
                "sku_id": f"sku-{suffix}",
                "exact_url": f"https://same.invalid/{suffix}",
                "source_id": f"v-{suffix}",
            }
            for index, suffix in enumerate(("a", "b", "c"), start=101)
        ],
    }

    outcome = harvest([durable_job], 10)[19]

    assert outcome["crawl_results"][101]["page_price_value"] == 11
    assert outcome["crawl_results"][102]["no_result"] is True
    assert outcome["crawl_results"][103] == {
        "manual": True,
        "error": "provenance ambiguity",
    }
    assert outcome["state"] == "EXPORTED"


def test_legacy_bridge_finalizes_only_pending_subset_from_mixed_plan(tmp_path):
    adapter = _BridgeAdapter()
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    propose, finalize, _submit, _harvest, _probe, _restart = legacy_zhaoshang_bridge(
        adapter,
        paths=paths,
        business_date="2026-08-11",
    )
    rows = [
        {"vCommodityId": "already-complete", "skuId": "sku-old", "extLink": "https://item.invalid/old"},
        {"vCommodityId": "late-delta", "skuId": "sku-new", "extLink": "https://item.invalid/new"},
    ]
    proposal = propose([{"row": row} for row in rows], 10)[0]

    finalized = finalize(proposal, (proposal.members[1],), 10)

    assert finalized.members == (proposal.members[1],)
    assert [request["sku_id"] for request in finalized.payload_json["requests"]] == ["sku-new"]
    assert adapter.prepare_calls == 1

    with pytest.raises(ValueError, match="ordered subset"):
        finalize(proposal, (proposal.members[1], proposal.members[0]), 10)


def test_legacy_bridge_keeps_confirmed_submit_priority_pending_until_verified(tmp_path):
    class PendingPriorityAdapter(_BridgeAdapter):
        def submit(self, _job, *, timeout_seconds):
            self.submit_calls += 1
            return LegacySubmitOutcome(
                SubmitStatus.SUBMITTED,
                task_id="remote-1",
                task_number="number-1",
                priority_evidence=None,
            )

    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    adapter = PendingPriorityAdapter()
    propose, finalize, submit, harvest, _probe, _restart = legacy_zhaoshang_bridge(
        adapter,
        paths=paths,
        business_date="2026-08-11",
    )
    row = {"vCommodityId": "v-a", "skuId": "sku-a", "extLink": "https://same.invalid/p"}
    plan = propose([{"row": row}], 10)[0]
    finalized = finalize(plan, tuple(plan.members), 10)

    submitted = submit({"logical_key": finalized.logical_key}, [], 10)
    durable = {
        "id": 17,
        "logical_key": finalized.logical_key,
        "task_name": finalized.task_name,
        "payload_hash": finalized.payload_hash,
        "payload_json": json.dumps(finalized.payload_json),
        "input_path": finalized.input_path,
        "result_path": finalized.result_path,
        "state": "PRIORITY_PENDING",
        "remote_identity": submitted["remote_identity"],
        "crawl_work": [],
    }
    priority = harvest([durable], 10)[17]

    assert submitted["state"] == "PRIORITY_PENDING"
    assert priority["state"] == "SUBMITTED"
    assert priority["remote_identity"] == "mall|remote-1|number-1"
    assert adapter.submit_calls == 1
    assert adapter.priority_calls == 1
    assert adapter.harvest_calls == 0


def test_legacy_bridge_retries_transient_priority_on_same_remote_identity(tmp_path):
    class RetryingPriorityAdapter(_BridgeAdapter):
        def __init__(self):
            super().__init__()
            self.priority_outcomes = [
                LegacyPriorityOutcome(
                    PriorityStatus.UNCONFIRMED,
                    diagnostic="InterfaceError",
                    retryable=True,
                ),
                LegacyPriorityOutcome(PriorityStatus.UPDATED, weight_type=-1),
            ]

        def prioritize(self, _job, *, task_id, task_number, timeout_seconds):
            self.priority_calls += 1
            assert (task_id, task_number) == ("remote-1", "number-1")
            return self.priority_outcomes.pop(0)

    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    adapter = RetryingPriorityAdapter()
    propose, finalize, _submit, harvest, _probe, _restart = legacy_zhaoshang_bridge(
        adapter,
        paths=paths,
        business_date="2026-08-11",
    )
    row = {
        "vCommodityId": "v-a",
        "skuId": "sku-a",
        "extLink": "https://same.invalid/p",
    }
    plan = propose([{"row": row}], 10)[0]
    finalized = finalize(plan, tuple(plan.members), 10)
    identity = "mall|remote-1|number-1"
    durable = {
        "id": 17,
        "logical_key": finalized.logical_key,
        "task_name": finalized.task_name,
        "payload_hash": finalized.payload_hash,
        "payload_json": json.dumps(finalized.payload_json),
        "input_path": finalized.input_path,
        "result_path": finalized.result_path,
        "state": "PRIORITY_PENDING",
        "remote_identity": identity,
        "crawl_work": [],
    }

    first = harvest([durable], 10)[17]
    second = harvest([durable], 10)[17]

    assert first["state"] == "PRIORITY_PENDING"
    assert first["remote_identity"] == identity
    assert first["retryable_error"] == "InterfaceError"
    assert "manual_error" not in first
    assert second["state"] == "SUBMITTED"
    assert second["remote_identity"] == identity
    assert adapter.priority_calls == 2
    assert adapter.submit_calls == 0


def test_legacy_bridge_sends_deterministic_priority_conflict_directly_to_manual(
    tmp_path,
):
    class ConflictingPriorityAdapter(_BridgeAdapter):
        def prioritize(self, _job, *, task_id, task_number, timeout_seconds):
            self.priority_calls += 1
            return LegacyPriorityOutcome(
                PriorityStatus.UNCONFIRMED,
                diagnostic="priority identity does not match confirmed submission",
                retryable=False,
            )

    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    adapter = ConflictingPriorityAdapter()
    propose, finalize, _submit, harvest, _probe, _restart = legacy_zhaoshang_bridge(
        adapter,
        paths=paths,
        business_date="2026-08-11",
    )
    row = {
        "vCommodityId": "v-a",
        "skuId": "sku-a",
        "extLink": "https://same.invalid/p",
    }
    plan = propose([{"row": row}], 10)[0]
    finalized = finalize(plan, tuple(plan.members), 10)
    durable = {
        "id": 17,
        "logical_key": finalized.logical_key,
        "task_name": finalized.task_name,
        "payload_hash": finalized.payload_hash,
        "payload_json": json.dumps(finalized.payload_json),
        "input_path": finalized.input_path,
        "result_path": finalized.result_path,
        "state": "PRIORITY_PENDING",
        "remote_identity": "mall|remote-1|number-1",
        "crawl_work": [],
    }

    outcome = harvest([durable], 10)[17]

    assert outcome["manual_error"] == (
        "priority identity does not match confirmed submission"
    )
    assert "retryable_error" not in outcome
    assert adapter.submit_calls == 0


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
    propose, finalize, _submit, _harvest, _probe, _restart = legacy_zhaoshang_bridge(
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
        propose, finalize, _submit, _harvest, _probe, _restart = legacy_zhaoshang_bridge(
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


def test_batch_result_locked_legacy_alias_writes_versioned_snapshot_without_failing(
    tmp_path, monkeypatch
):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    paths.run_dir.mkdir(parents=True)
    legacy = _ensure_batch_state(paths, enable_zhaoshang=False)
    legacy_before = legacy.read_text(encoding="utf-8")
    result = RuntimeResult(
        TickSummary(
            "MANUAL_REVIEW_REQUIRED",
            (),
            {"processed": 1},
            {"remote_manual": 1},
            23,
            "manual blocker",
        )
    )
    real_replace = runtime_module.os.replace

    def locked_replace(source, destination):
        if Path(destination) == legacy:
            raise PermissionError(5, "legacy BATCH_STATE is held by a reader")
        return real_replace(source, destination)

    monkeypatch.setattr(runtime_module.os, "replace", locked_replace)

    try:
        runtime_module._record_batch_result(
            paths,
            result,
            command="closeout",
            enable_zhaoshang=True,
            output_path=paths.final_output_path,
        )
    except PermissionError as exc:
        pytest.fail(f"non-authoritative handoff alias lock escaped: {exc}")

    assert legacy.read_text(encoding="utf-8") == legacy_before
    snapshots = sorted((paths.run_dir / "batch_state_snapshots").glob("BATCH_STATE_*.md"))
    assert len(snapshots) == 1
    snapshot = snapshots[0].read_text(encoding="utf-8")
    assert "status: WAITING_USER" in snapshot
    assert '"command": "closeout"' in snapshot
    assert "legacy_alias_refresh: blocked" in snapshot


def test_batch_state_temp_cleanup_lock_does_not_escape_after_alias_replace_failure(
    tmp_path, monkeypatch
):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    paths.run_dir.mkdir(parents=True)
    legacy = _ensure_batch_state(paths, enable_zhaoshang=False)
    real_replace = runtime_module.os.replace
    real_unlink = Path.unlink

    def locked_replace(source, destination):
        if Path(destination) == legacy:
            raise PermissionError(5, "legacy alias locked")
        return real_replace(source, destination)

    def locked_temp_unlink(path, *args, **kwargs):
        if path.name.startswith(".BATCH_STATE.md."):
            raise PermissionError(5, "temporary cleanup locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(runtime_module.os, "replace", locked_replace)
    monkeypatch.setattr(Path, "unlink", locked_temp_unlink)

    try:
        runtime_module._record_batch_result(
            paths,
            RuntimeResult(TickSummary("COMPLETED", (), {}, {}, 1)),
            command="tick",
            enable_zhaoshang=False,
            output_path=paths.final_output_path,
        )
    except PermissionError as exc:
        pytest.fail(f"metadata temp cleanup lock escaped: {exc}")


def test_latest_batch_state_snapshot_path_returns_newest_versioned_snapshot(tmp_path):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    paths.run_dir.mkdir(parents=True)
    _ensure_batch_state(paths, enable_zhaoshang=False)

    runtime_module._record_batch_result(
        paths,
        RuntimeResult(TickSummary("WAITING_REMOTE", (), {}, {"remote_pending": 1}, 1)),
        command="tick",
        enable_zhaoshang=False,
        output_path=paths.final_output_path,
    )
    runtime_module._record_batch_result(
        paths,
        RuntimeResult(TickSummary("MANUAL_REVIEW_REQUIRED", (), {}, {"remote_manual": 1}, 1, "manual")),
        command="closeout",
        enable_zhaoshang=False,
        output_path=paths.final_output_path,
    )

    latest = runtime_module.latest_batch_state_snapshot_path(paths)
    assert latest is not None
    assert latest.name.startswith("BATCH_STATE_")
    assert "status: WAITING_USER" in latest.read_text(encoding="utf-8")
    assert len(list((paths.run_dir / "batch_state_snapshots").glob("BATCH_STATE_*.md"))) == 2


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
        source_stability_seconds=0,
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
        source_stability_seconds=0,
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


def test_drain_only_missing_state_is_noop_and_does_not_create_a_database(tmp_path):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)

    result = run_runtime_command(
        "drain-only",
        business_date="2026-08-11",
        runtime_root=root,
        dependencies=_dependencies([]),
    )

    assert result.tick.outcome == "DRAIN_NO_EXISTING_RUN"
    # Lock probing may still create the run directory; verify the command
    # returns before durable writes.
    assert not paths.state_path.exists()


def test_drain_only_second_instance_with_same_business_date_lock_returns_skip_without_writes(
    tmp_path, monkeypatch
):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    owner = ProcessLock(paths.lock_path).acquire(owner={
        "business_date": "2026-08-11",
        "pid": 12345,
        "command": "tick",
        "started_at": "2026-08-11T20:30:00+08:00",
        "invocation": "scheduler",
    })
    monkeypatch.setattr(
        runtime_module,
        "_run_runtime_command_locked",
        lambda *_args, **_kwargs: pytest.fail(
            "active writer conflict must skip before lifecycle, especially when sqlite missing"
        ),
    )
    try:
        result = run_runtime_command(
            "drain-only",
            business_date="2026-08-11",
            runtime_root=root,
            invocation="manual",
            dependencies=_dependencies([]),
        )
    finally:
        owner.release()

    assert result.tick.outcome == "ALREADY_RUNNING"
    assert result.lock_conflict is not None
    assert result.lock_conflict["requested"] == "drain-only"
    assert result.lock_conflict["command"] == "tick"
    assert result.lock_conflict["invocation"] == "scheduler"
    assert result.lock_conflict["pid"] == 12345
    # Acquire the writer lock on the same command/date, so the directory may be
    # present from lock metadata bookkeeping; prove the lifecycle itself did not
    # write any durable state for this second invocation.
    assert not paths.state_path.exists()
    assert not (paths.run_dir / "BATCH_STATE.md").exists()


def test_drain_only_rejects_remote_create_capability(tmp_path):
    with pytest.raises(ValueError, match="forbids the remote-create capability"):
        run_runtime_command(
            "drain-only",
            business_date="2026-08-11",
            runtime_root=tmp_path / "runtime",
            enable_zhaoshang=True,
            dependencies=_dependencies([]),
        )


def test_drain_only_default_dependencies_expose_only_existing_harvester(
    tmp_path, monkeypatch
):
    harvester = lambda _jobs, _timeout: {}
    monkeypatch.setattr(runtime_module, "CnncLegacyZhaoshangAdapter", lambda: object())
    monkeypatch.setattr(
        runtime_module,
        "legacy_zhaoshang_bridge",
        lambda *_args, **_kwargs: (object(), object(), object(), harvester, None, None),
    )

    deps = runtime_module.build_drain_dependencies(
        zhaoshang_backend="legacy",
        paths=runtime_paths("2026-08-11", tmp_path / "runtime"),
        business_date="2026-08-11",
    )

    assert deps.harvest is harvester
    assert deps.detail is None
    assert deps.model is None
    assert deps.model_batch is None
    assert deps.plan_crawl is None
    assert deps.finalize_crawl_plan is None
    assert deps.submit_crawl is None
    with pytest.raises(RuntimeConfigurationError, match="cannot discover"):
        deps.discover(1)


def test_drain_only_uses_existing_only_engine_and_materializes_when_drained(
    tmp_path, monkeypatch
):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    paths.run_dir.mkdir(parents=True)
    DailyAuditStore(paths.state_path).ensure_run("2026-08-11")
    calls = []
    monkeypatch.setattr(runtime_module, "_configured_model_name", lambda: "deployed-model")
    monkeypatch.setattr(
        runtime_module,
        "run_incremental_tick",
        lambda *_args, **_kwargs: pytest.fail("drain-only must not invoke the tick engine"),
    )

    def drain(_store, _lock, _config, _deps, *, max_iterations):
        calls.append(("drain", max_iterations))
        return TickSummary("DRAINED", ("checkpoint",), {}, {"remote_pending": 0}, 7)

    def materialize(*_args):
        calls.append(("materialize", None))
        return object()

    monkeypatch.setattr(runtime_module, "run_existing_remote_drain", drain)

    result = run_runtime_command(
        "drain-only",
        business_date="2026-08-11",
        runtime_root=root,
        runtime_budget_seconds=300,
        drain_max_iterations=23,
        dependencies=_dependencies([]),
        materialize=materialize,
        monotonic=lambda: 0.0,
    )

    assert result.publication is not None
    assert calls == [("drain", 23), ("materialize", None)]


def test_drain_only_pending_work_never_attempts_formal_materialization(
    tmp_path, monkeypatch
):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    paths.run_dir.mkdir(parents=True)
    DailyAuditStore(paths.state_path).ensure_run("2026-08-11")
    monkeypatch.setattr(runtime_module, "_configured_model_name", lambda: "deployed-model")
    monkeypatch.setattr(
        runtime_module,
        "run_existing_remote_drain",
        lambda *_args, **_kwargs: TickSummary(
            "DRAIN_NO_PROGRESS", ("stop:no_progress",), {}, {"remote_pending": 1}, 7
        ),
    )

    result = run_runtime_command(
        "drain-only",
        business_date="2026-08-11",
        runtime_root=root,
        dependencies=_dependencies([]),
        materialize=lambda *_args: pytest.fail(
            "pending drain-only work must not materialize a formal workbook"
        ),
    )

    assert result.tick.outcome == "DRAIN_NO_PROGRESS"
    assert result.publication is None


def test_drain_only_already_finalized_is_fast_noop(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    paths = runtime_paths("2026-08-11", root)
    paths.run_dir.mkdir(parents=True)
    DailyAuditStore(paths.state_path).ensure_run("2026-08-11")
    monkeypatch.setattr(runtime_module, "_configured_model_name", lambda: "deployed-model")
    monkeypatch.setattr(
        runtime_module,
        "run_existing_remote_drain",
        lambda *_args, **_kwargs: TickSummary(
            "ALREADY_FINALIZED", ("noop:already_finalized",), {}, {}, 7
        ),
    )

    result = run_runtime_command(
        "drain-only",
        business_date="2026-08-11",
        runtime_root=root,
        dependencies=_dependencies([]),
        materialize=lambda *_args: pytest.fail("already finalized must be a no-op"),
    )

    assert result.tick.outcome == "ALREADY_FINALIZED"
    state = (paths.run_dir / "BATCH_STATE.md").read_text(encoding="utf-8")
    assert "status: COMPLETE" in state
    assert "formal_excel_already_materialized" in state


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
        source_stability_seconds=0,
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
        source_stability_seconds=0,
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


def test_record_batch_result_only_masks_batch_state_lock_contention(tmp_path, monkeypatch):
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    store = DailyAuditStore(paths.state_path)
    store.ensure_run("2026-08-11")

    class _UnavailableLock:
        def __enter__(self):
            raise TimeoutError("batch state busy")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        runtime_module,
        "_batch_state_update_lock",
        lambda *args, **kwargs: _UnavailableLock(),
    )
    monkeypatch.setattr(runtime_module, "run_incremental_tick", lambda *_args, **_kwargs: TickSummary(
        "COMPLETED", (), {}, {}, 7,
    ))

    result = run_runtime_command(
        "tick",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
    )

    assert result.tick.outcome == "COMPLETED"
    assert runtime_paths("2026-08-11", tmp_path / "runtime").state_path.is_file()


def test_record_batch_result_does_not_mask_non_lock_io_failures(tmp_path, monkeypatch):
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    store = DailyAuditStore(paths.state_path)
    store.ensure_run("2026-08-11")

    class _UnavailableLock:
        def __enter__(self):
            raise OSError("batch state filesystem is unavailable")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        runtime_module,
        "_batch_state_update_lock",
        lambda *args, **kwargs: _UnavailableLock(),
    )
    monkeypatch.setattr(runtime_module, "run_incremental_tick", lambda *_args, **_kwargs: TickSummary(
        "COMPLETED", (), {}, {}, 7,
    ))

    with pytest.raises(OSError, match="batch state filesystem is unavailable"):
        run_runtime_command(
            "tick",
            business_date="2026-08-11",
            runtime_root=tmp_path / "runtime",
            dependencies=_dependencies([]),
        )


def test_missing_batch_state_bridge_uses_normal_state_document(tmp_path):
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    adapter = _BridgeAdapter()
    propose, finalize, _submit, _harvest, _probe, _restart = legacy_zhaoshang_bridge(
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


def test_model_batch_worker_exports_model_environment_once(monkeypatch):
    configuration = {
        "TITLE_MATCH_MODEL_API_KEY": "secret",
        "TITLE_MATCH_MODEL_BASE_URL": "https://model.invalid/v1",
        "TITLE_MATCH_MODEL_NAME": "model-name",
    }
    # Pre-register restores as empty strings: monkeypatch.delenv on an absent
    # variable registers no teardown, and the apply-once export must not leak
    # into later tests in this process.
    for name in configuration:
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(
        "app.services.incremental_audit_runtime._load_model_configuration",
        lambda: configuration,
    )
    seen: list[dict[str, str | None]] = []

    def call(requests, **_kwargs):
        seen.append({name: os.environ.get(name) for name in configuration})
        return {
            index: TitleMatchModelResult(match_result="SAME", confidence=1, model_match="exact")
            for index in range(len(requests))
        }

    monkeypatch.setattr("app.services.incremental_audit_runtime.call_title_match_model_batch", call)
    result = _model_batch_worker([{"queryStr": "q", "skuName": "s"}], 5)

    assert result[0]["match_result"] == "SAME"
    # During the (possibly concurrent) batch call the resolved configuration
    # is visible in the environment.
    assert seen == [configuration]
    # Apply-once export: batch workers run concurrently and a
    # mutate-then-restore context would race between threads, so the resolved
    # values stay exported for this one-shot process instead of being undone.
    assert {name: os.environ.get(name) for name in configuration} == configuration


def test_model_batch_worker_never_overwrites_an_explicit_env_pin(monkeypatch):
    configuration = {
        "TITLE_MATCH_MODEL_API_KEY": "file-key",
        "TITLE_MATCH_MODEL_BASE_URL": "https://model.invalid/v1",
        "TITLE_MATCH_MODEL_NAME": "file-model",
    }
    monkeypatch.setenv("TITLE_MATCH_MODEL_NAME", "pinned-model")
    # See above: pre-register teardown restores so the export cannot leak.
    monkeypatch.setenv("TITLE_MATCH_MODEL_API_KEY", "")
    monkeypatch.setenv("TITLE_MATCH_MODEL_BASE_URL", "")
    monkeypatch.setattr(
        "app.services.incremental_audit_runtime._load_model_configuration",
        lambda: configuration,
    )
    seen: list[dict[str, str | None]] = []

    def call(requests, **_kwargs):
        seen.append(dict(os.environ))
        return {
            index: TitleMatchModelResult(match_result="SAME", confidence=1, model_match="exact")
            for index in range(len(requests))
        }

    monkeypatch.setattr("app.services.incremental_audit_runtime.call_title_match_model_batch", call)
    _model_batch_worker([{"queryStr": "q", "skuName": "s"}], 5)

    # The explicit environment pin keeps its value (same precedence as
    # _read_model_configuration); only absent keys are filled from the file.
    assert seen[0]["TITLE_MATCH_MODEL_NAME"] == "pinned-model"
    assert seen[0]["TITLE_MATCH_MODEL_API_KEY"] == "file-key"
    assert seen[0]["TITLE_MATCH_MODEL_BASE_URL"] == "https://model.invalid/v1"


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


def _capture_tick_configs(monkeypatch, tmp_path):
    captured: list[TickConfig] = []

    def run_tick(_store, _lock, config, _dependencies, *, finalize):
        captured.append(config)
        return TickSummary("COMPLETED", (), {}, {}, None)

    monkeypatch.setattr(runtime_module, "run_incremental_tick", run_tick)
    for key in runtime_module._MODEL_RUNTIME_TUNING_KEYS:
        monkeypatch.delenv(key, raising=False)
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setattr(runtime_module, "PROJECT_DATA_ROOT", data_root)
    return captured, data_root


def test_incremental_model_tuning_defaults_without_env_or_model_env(tmp_path, monkeypatch):
    captured, _data_root = _capture_tick_configs(monkeypatch, tmp_path)

    run_runtime_command(
        "tick",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
    )

    assert len(captured) == 1
    assert captured[0].model_batch_size == 10
    assert captured[0].model_max_concurrency == 6


def test_incremental_model_tuning_model_env_override_reaches_tick_config(tmp_path, monkeypatch):
    captured, data_root = _capture_tick_configs(monkeypatch, tmp_path)
    (data_root / "model.env").write_text(
        "TITLE_MATCH_MODEL_API_KEY=file-key\n"
        "TITLE_MATCH_MODEL_BASE_URL=https://model.invalid/v1\n"
        "TITLE_MATCH_MODEL_NAME=file-model\n"
        "TITLE_MATCH_MODEL_MAX_CONCURRENCY=8\n"
        "TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE=20\n",
        encoding="utf-8",
    )

    run_runtime_command(
        "tick",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
    )

    # The data/model.env values (previously silently ignored for these keys)
    # genuinely reach the production TickConfig composition.
    assert captured[0].model_max_concurrency == 8
    assert captured[0].model_batch_size == 20


def test_incremental_model_tuning_ignores_work01_batch_size_key_in_model_env(tmp_path, monkeypatch):
    """R3 P1-B: the work_01 key TITLE_MATCH_MODEL_BATCH_SIZE must never tune
    the incremental pipeline.  A historical data/model.env entry written for
    work_01 (default 30) stays inert for STEP_1 — silently activating it on
    upgrade would be a production configuration drift."""
    captured, data_root = _capture_tick_configs(monkeypatch, tmp_path)
    (data_root / "model.env").write_text(
        "TITLE_MATCH_MODEL_BATCH_SIZE=30\n",
        encoding="utf-8",
    )

    run_runtime_command(
        "tick",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
    )

    assert captured[0].model_batch_size == 10
    assert captured[0].model_max_concurrency == 6


def test_incremental_model_tuning_ignores_work01_batch_size_key_in_process_env(tmp_path, monkeypatch):
    """R3 P1-B: same isolation for the process environment — the incremental
    runtime only reads its own TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE name."""
    captured, _data_root = _capture_tick_configs(monkeypatch, tmp_path)
    monkeypatch.setenv("TITLE_MATCH_MODEL_BATCH_SIZE", "30")

    run_runtime_command(
        "tick",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
    )

    assert captured[0].model_batch_size == 10


def test_incremental_model_tuning_environment_beats_model_env(tmp_path, monkeypatch):
    captured, data_root = _capture_tick_configs(monkeypatch, tmp_path)
    (data_root / "model.env").write_text(
        "TITLE_MATCH_MODEL_MAX_CONCURRENCY=8\n"
        "TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE=20\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_CONCURRENCY", "7")
    monkeypatch.setenv("TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE", "15")

    run_runtime_command(
        "tick",
        business_date="2026-08-11",
        runtime_root=tmp_path / "runtime",
        dependencies=_dependencies([]),
    )

    assert captured[0].model_max_concurrency == 7
    assert captured[0].model_batch_size == 15


@pytest.mark.parametrize(
    "raw",
    ["0", "-3", "not-an-int", "8.5"],
    ids=("zero", "negative", "not-an-int", "float"),
)
def test_incremental_model_tuning_invalid_values_fail_closed(tmp_path, monkeypatch, raw):
    _captured, data_root = _capture_tick_configs(monkeypatch, tmp_path)
    (data_root / "model.env").write_text(
        f"TITLE_MATCH_MODEL_MAX_CONCURRENCY={raw}\n",
        encoding="utf-8",
    )

    with pytest.raises(runtime_module.RuntimeConfigurationError):
        run_runtime_command(
            "tick",
            business_date="2026-08-11",
            runtime_root=tmp_path / "runtime",
            dependencies=_dependencies([]),
        )


@pytest.mark.parametrize(
    "raw",
    ["0", "-3", "not-an-int", "8.5"],
    ids=("zero", "negative", "not-an-int", "float"),
)
def test_incremental_model_tuning_invalid_batch_size_fails_closed(tmp_path, monkeypatch, raw):
    _captured, data_root = _capture_tick_configs(monkeypatch, tmp_path)
    (data_root / "model.env").write_text(
        f"TITLE_MATCH_MODEL_INCREMENTAL_BATCH_SIZE={raw}\n",
        encoding="utf-8",
    )

    with pytest.raises(runtime_module.RuntimeConfigurationError):
        run_runtime_command(
            "tick",
            business_date="2026-08-11",
            runtime_root=tmp_path / "runtime",
            dependencies=_dependencies([]),
        )


def test_model_runtime_tuning_keys_are_not_part_of_the_required_gate(tmp_path, monkeypatch):
    """Optional tuning keys must not widen the required-configuration gate."""
    assert runtime_module._MODEL_RUNTIME_TUNING_KEYS
    assert not set(runtime_module._MODEL_RUNTIME_TUNING_KEYS) & set(runtime_module._MODEL_CONFIGURATION_KEYS)
    # A model.env carrying ONLY the tuning keys is still an incomplete
    # required configuration.
    values = runtime_module._read_model_configuration(tmp_path / "missing.env")
    assert values == {}


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


def test_tick_cli_runtime_contract_accepts_drain_iteration_limit(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    def run_locked(command, **kwargs):
        captured.update({"command": command, **kwargs})
        return RuntimeResult(TickSummary("COMPLETED", (), {}, {}, None))

    monkeypatch.setattr(runtime_module, "_run_runtime_command_locked", run_locked)
    monkeypatch.setattr(cli, "collect_runtime_status", lambda **_kwargs: {"run_id": None})
    monkeypatch.setattr(
        cli,
        "write_daily_status",
        lambda *_args, **_kwargs: tmp_path / "daily_status.json",
    )

    assert cli.main([
        "tick",
        "--business-date",
        "2026-08-11",
        "--runtime-root",
        str(tmp_path / "runtime"),
    ]) == 0

    assert captured["command"] == "tick"
    assert captured["drain_max_iterations"] == 1000
    assert captured["paths"].run_dir == (tmp_path / "runtime" / "2026-08-11").resolve()
    assert json.loads(capsys.readouterr().out)["outcome"] == "COMPLETED"


def test_cli_drain_only_has_no_remote_create_flag_and_forwards_limits(monkeypatch):
    captured = []
    summary = TickSummary("DRAIN_NO_PROGRESS", (), {}, {"remote_pending": 1}, 7)
    monkeypatch.setattr(
        cli,
        "run_runtime_command",
        lambda command, **kwargs: (
            captured.append((command, kwargs)) or RuntimeResult(summary)
        ),
    )
    monkeypatch.setattr(cli, "collect_runtime_status", lambda **_kwargs: {"state": "FINALIZING"})
    monkeypatch.setattr(cli, "write_daily_status", lambda *_args, **_kwargs: Path("daily_status.json"))

    assert cli.main([
        "drain-only",
        "--business-date",
        "2026-08-11",
        "--budget-seconds",
        "3000",
        "--max-iterations",
        "77",
    ]) == 0

    command, kwargs = captured[0]
    assert command == "drain-only"
    assert kwargs["enable_zhaoshang"] is False
    assert kwargs["drain_max_iterations"] == 77
    assert kwargs["runtime_budget_seconds"] == 3000


def test_cli_manual_lock_conflict_fails_fast_with_owner_details(monkeypatch, capsys):
    summary = TickSummary("ALREADY_RUNNING", (), {}, {}, None)
    monkeypatch.setattr(
        cli,
        "run_runtime_command",
        lambda *_args, **_kwargs: RuntimeResult(
            summary,
            lock_conflict={
                "lock_identity": "C:/runtime/2026-08-11/writer.lock",
                "pid": 12345,
                "command": "drain-only",
                "started_at": "2026-08-11T20:31:12+08:00",
                "invocation": "scheduler",
            },
        ),
    )

    assert cli.main(["drain-only", "--business-date", "2026-08-11"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("SKIPPED_ALREADY_RUNNING\n")
    assert "requested=drain-only" in output
    assert "owner_pid=12345" in output
    assert "owner_command=drain-only" in output
    assert "owner_invocation=scheduler" in output


def test_cli_lock_status_is_read_only_and_does_not_call_runtime(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "collect_production_writer_status",
        lambda **_kwargs: {
            "active": True,
            "message": "production run currently active",
            "command": "tick",
        },
    )
    monkeypatch.setattr(
        cli,
        "run_runtime_command",
        lambda *_args, **_kwargs: pytest.fail("lock-status must not run lifecycle"),
    )

    assert cli.main(["lock-status", "--business-date", "2026-08-11"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["active"] is True
    assert report["command"] == "tick"


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


def test_cli_export_review_uses_dedicated_non_tick_command(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        cli,
        "run_review_export_command",
        lambda **kwargs: calls.append(kwargs)
        or {"outcome": "REVIEW_EXPORTED", "row_count": 2, "manual_count": 1},
        raising=False,
    )
    output = tmp_path / "saas_audit_review_20260811.xlsx"

    result = cli.main(
        [
            "export-review",
            "--business-date",
            "2026-08-11",
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--output-path",
            str(output),
        ]
    )

    assert result == 0
    assert calls == [
        {
            "business_date": "2026-08-11",
            "runtime_root": tmp_path / "runtime",
            "output_path": output,
        }
    ]


@pytest.mark.parametrize(
    "command_name,recovery_symbol,completed_outcome,skipped_outcome",
    [
        (
            "run_remote_priority_recovery_command",
            "run_existing_remote_priority_recovery",
            "REMOTE_PRIORITY_RECOVERY_COMPLETED",
            "REMOTE_PRIORITY_TARGETED_HARVEST_SKIPPED",
        ),
        (
            "run_remote_priority_zero_rows_recovery_command",
            "run_existing_remote_priority_zero_rows_recovery",
            "REMOTE_PRIORITY_ZERO_ROWS_RECOVERY_COMPLETED",
            "REMOTE_PRIORITY_ZERO_ROWS_TARGETED_HARVEST_SKIPPED",
        ),
    ],
)
def test_targeted_harvest_recovery_rejection_is_classified_not_name_error(
    tmp_path, monkeypatch, command_name, recovery_symbol, completed_outcome, skipped_outcome
):
    from app.services.incremental_audit_recovery import RecoveryRejected

    monkeypatch.setattr(runtime_module, "CnncLegacyZhaoshangAdapter", lambda: object())
    monkeypatch.setattr(
        runtime_module,
        recovery_symbol,
        lambda **_kwargs: {"outcome": completed_outcome, "business_date": "2026-08-19"},
    )

    def rejecting_harvest(**_kwargs):
        raise RecoveryRejected("test rejection")

    monkeypatch.setattr(runtime_module, "run_existing_remote_harvest", rejecting_harvest)

    command = getattr(runtime_module, command_name)
    result = command(
        business_date="2026-08-19",
        runtime_root=tmp_path,
        remote_job_id=54,
        remote_identity="mall|54|RW-20260819160830",
        expected_error_text="PriorityUpdateZeroRowsError",
    )

    assert result["outcome"] == completed_outcome
    assert result["targeted_harvest"] == {
        "outcome": skipped_outcome,
        "reason": "test rejection",
    }


def test_legacy_bridge_maps_not_attempted_to_dedicated_error(tmp_path):
    from app.workflows.incremental_price_audit import SubmitNotAttemptedError

    class NotAttemptedAdapter(_BridgeAdapter):
        def submit(self, _job, *, timeout_seconds):
            self.submit_calls += 1
            return LegacySubmitOutcome(
                SubmitStatus.NOT_ATTEMPTED,
                diagnostic="auth refresh failed before spawn",
            )

    adapter = NotAttemptedAdapter()
    paths = runtime_paths("2026-08-11", tmp_path / "runtime")
    paths.run_dir.mkdir(parents=True)
    propose, finalize, submit, _harvest, _probe, _restart = legacy_zhaoshang_bridge(
        adapter, paths=paths, business_date="2026-08-11"
    )
    rows = [{"vCommodityId": "v-a", "skuId": "sku-a", "extLink": "https://same.invalid/p"}]
    plan = propose([{"row": row} for row in rows], 10)[0]
    finalized = finalize(plan, tuple(plan.members), 10)

    with pytest.raises(SubmitNotAttemptedError) as excinfo:
        submit({"logical_key": finalized.logical_key}, [], 10)

    assert "auth refresh failed" in str(excinfo.value)
    assert adapter.submit_calls == 1
