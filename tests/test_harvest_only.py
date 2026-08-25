"""Directed tests for the daytime quarter-hour harvest-only command."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

import app.services.incremental_audit_runtime as runtime_module
from app.services.incremental_audit_runtime import (
    RuntimeResult,
    run_runtime_command,
    runtime_paths,
)
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.workflows.incremental_price_audit import (
    TickConfig,
    TickDependencies,
    TickSummary,
    run_harvest_only,
)
import scripts.incremental_price_audit as cli


DATE = "2026-08-11"


@dataclass
class Clock:
    value: float = 0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _config(*, budget=60, reserve=5, batch_size=6):
    return TickConfig(
        business_date=DATE,
        runtime_budget_seconds=budget,
        heavy_work_reserve_seconds=reserve,
        lease_ttl_seconds=60,
        harvest_batch_size=batch_size,
    )


def _forbidden_deps(clock):
    def forbidden(*_args, **_kwargs):
        pytest.fail("harvest-only must not discover, stage, plan, or submit")

    return TickDependencies(
        discover=forbidden,
        detail=forbidden,
        model=forbidden,
        model_batch=forbidden,
        plan_crawl=forbidden,
        finalize_crawl_plan=forbidden,
        submit_crawl=forbidden,
        monotonic=clock.monotonic,
        wall_clock=lambda: datetime(2026, 8, 11, 9, 15, tzinfo=UTC),
    )


def _seed_job(store, token, index, *, state="SUBMITTED", crawls=0):
    job = store.prepare_remote_intent(
        token,
        f"job-{index}",
        f"task-{index}",
        f"payload-{index}",
        mall_key=f"mall-{index}",
    )
    store.claim_remote_submission(token, job["id"])
    job = store.reconcile_remote_job(
        token,
        job["id"],
        remote_identity=f"remote-{index}",
        state=state,
    )
    crawl_ids = []
    for crawl_index in range(crawls):
        crawl = store.ensure_crawl_work(
            token,
            f"mall-{index}",
            f"sku-{index}-{crawl_index}",
            f"https://shop.example/item-{index}-{crawl_index}",
            "legacy-v1",
            f"fingerprint-{index}-{crawl_index}",
            source_id=f"doc-{index}-{crawl_index}",
        )
        store.link_crawl_work_to_remote_job(token, crawl["id"], job["id"])
        crawl_ids.append(int(crawl["id"]))
    return job, crawl_ids


def _snapshot_row(v="doc-1", sku="sku-1"):
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
    }


def _verify_lease(store):
    return store.acquire_lease(DATE, "verify", 60)


def test_harvest_only_without_existing_run_is_a_pure_noop(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    deps = _forbidden_deps(Clock())
    deps.harvest = lambda *_args: pytest.fail("no run means no harvest call")

    summary = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert summary.outcome == "HARVEST_NO_EXISTING_RUN"


def test_harvest_only_with_no_eligible_remote_jobs_is_a_local_noop(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    job, _ = _seed_job(store, token, 0, state="COMPLETED", crawls=1)
    store.release_lease(token)
    deps = _forbidden_deps(Clock())
    deps.harvest = lambda *_args: pytest.fail(
        "no eligible remote job must not touch the remote system"
    )

    summary = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert summary.outcome == "HARVEST_NOTHING_ELIGIBLE"
    assert summary.phase_trace == ("recover_submit_unknown", "noop:nothing_eligible")
    token = _verify_lease(store)
    jobs = store.list_remote_jobs(token)
    crawls = store.list_crawl_work(token)
    store.release_lease(token)
    assert [job_["state"] for job_ in jobs] == ["COMPLETED"]
    assert jobs[0]["harvest_attempts"] == 0
    assert [crawl["state"] for crawl in crawls] == ["PENDING"]


def test_harvest_only_checks_one_running_job_once_and_waits(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    job, _ = _seed_job(store, token, 0, state="SUBMITTED", crawls=1)
    store.release_lease(token)
    clock = Clock()
    deps = _forbidden_deps(clock)
    observed: list[list[int]] = []

    def harvest(jobs, _timeout):
        observed.append([int(j["id"]) for j in jobs])
        return {
            jobs[0]["id"]: {
                "state": "RUNNING",
                "remote_identity": jobs[0]["remote_identity"],
            }
        }

    deps.harvest = harvest
    summary = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert summary.outcome == "WAITING_REMOTE"
    assert observed == [[int(job["id"])]]
    token = _verify_lease(store)
    jobs = store.list_remote_jobs(token)
    crawls = store.list_crawl_work(token)
    store.release_lease(token)
    assert jobs[0]["state"] == "RUNNING"
    assert jobs[0]["harvest_attempts"] == 1
    assert [crawl["state"] for crawl in crawls] == ["RUNNING"]


def test_harvest_only_exports_and_ingests_a_completed_remote_in_one_command(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    job, crawl_ids = _seed_job(store, token, 0, state="SUBMITTED", crawls=2)
    store.release_lease(token)
    deps = _forbidden_deps(Clock())

    def harvest(jobs, _timeout):
        return {
            jobs[0]["id"]: {
                "state": "COMPLETED",
                "remote_identity": jobs[0]["remote_identity"],
                "export_path": "results/export-0.xlsx",
                "crawl_results": {
                    crawl_ids[0]: {"price": "10"},
                    crawl_ids[1]: {"price": "12"},
                },
            }
        }

    deps.harvest = harvest
    summary = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert summary.outcome == "HARVESTED"
    assert summary.counters["crawl_harvested"] == 2
    assert summary.counters["harvest_batches"] == 1
    assert summary.counters["harvest_swept"] == 1
    token = _verify_lease(store)
    jobs = store.list_remote_jobs(token)
    crawls = {crawl["id"]: crawl for crawl in store.list_crawl_work(token)}
    store.release_lease(token)
    assert jobs[0]["state"] == "COMPLETED"
    assert [crawls[crawl_id]["state"] for crawl_id in crawl_ids] == ["SUCCESS", "SUCCESS"]
    assert crawls[crawl_ids[0]]["result_json"]


def test_harvest_only_sweeps_every_eligible_job_across_batches(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    for index in range(8):
        _seed_job(store, token, index, state="SUBMITTED")
    store.release_lease(token)
    deps = _forbidden_deps(Clock())
    observed: list[int] = []

    def harvest(jobs, _timeout):
        observed.append(len(jobs))
        return {
            job["id"]: {
                "state": "COMPLETED",
                "remote_identity": job["remote_identity"],
                "crawl_results": {},
            }
            for job in jobs
        }

    deps.harvest = harvest
    summary = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert summary.outcome == "HARVESTED"
    assert observed == [6, 2]
    assert summary.counters["harvest_swept"] == 8
    assert summary.counters["harvest_batches"] == 2
    token = _verify_lease(store)
    states = [job["state"] for job in store.list_remote_jobs(token)]
    store.release_lease(token)
    assert states == ["COMPLETED"] * 8


def test_harvest_only_checks_later_jobs_when_earlier_batches_stay_running(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    for index in range(8):
        _seed_job(store, token, index, state="SUBMITTED")
    store.release_lease(token)
    deps = _forbidden_deps(Clock())
    observed: list[list[int]] = []

    def harvest(jobs, _timeout):
        observed.append([int(job["id"]) for job in jobs])
        return {
            job["id"]: {
                "state": "COMPLETED" if int(job["id"]) > 6 else "RUNNING",
                "remote_identity": job["remote_identity"],
                "crawl_results": {},
            }
            for job in jobs
        }

    deps.harvest = harvest
    summary = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert len(observed) == 2
    assert len(observed[0]) == 6
    assert len(observed[1]) == 2
    assert summary.counters["harvest_swept"] == 8
    assert summary.outcome == "WAITING_REMOTE"
    token = _verify_lease(store)
    jobs = store.list_remote_jobs(token)
    store.release_lease(token)
    assert [job["state"] for job in jobs[:6]] == ["RUNNING"] * 6
    assert [job["state"] for job in jobs[6:]] == ["COMPLETED"] * 2


def test_harvest_only_never_creates_business_work(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    job, crawl_ids = _seed_job(store, token, 0, state="SUBMITTED", crawls=1)
    store.release_lease(token)
    deps = _forbidden_deps(Clock())
    deps.harvest = lambda jobs, _timeout: {
        jobs[0]["id"]: {
            "state": "COMPLETED",
            "remote_identity": jobs[0]["remote_identity"],
            "crawl_results": {crawl_ids[0]: {"price": "10"}},
        }
    }

    summary = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert summary.outcome == "HARVESTED"
    token = _verify_lease(store)
    jobs = store.list_remote_jobs(token)
    crawls = store.list_crawl_work(token)
    stages = store.list_stage_work(token)
    snapshots = store.list_snapshots(DATE)
    store.release_lease(token)
    assert len(jobs) == 1
    assert len(crawls) == 1
    assert stages == []
    assert snapshots == []


def test_harvest_only_completing_all_remote_work_never_freezes_or_finalizes(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    job, crawl_ids = _seed_job(store, token, 0, state="SUBMITTED", crawls=1)
    store.release_lease(token)
    deps = _forbidden_deps(Clock())
    deps.harvest = lambda jobs, _timeout: {
        jobs[0]["id"]: {
            "state": "COMPLETED",
            "remote_identity": jobs[0]["remote_identity"],
            "crawl_results": {crawl_ids[0]: {"price": "10"}},
        }
    }

    summary = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert summary.outcome == "HARVESTED"
    assert summary.frozen_snapshot_id is None
    run = store.get_run(DATE)
    assert run["state"] != "FINALIZED"
    assert run["frozen_snapshot_id"] is None
    assert not (tmp_path / "final.xlsx").exists()


def test_harvest_only_rerun_after_completion_is_a_noop(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    job, crawl_ids = _seed_job(store, token, 0, state="SUBMITTED", crawls=1)
    store.release_lease(token)
    deps = _forbidden_deps(Clock())
    deps.harvest = lambda jobs, _timeout: {
        jobs[0]["id"]: {
            "state": "COMPLETED",
            "remote_identity": jobs[0]["remote_identity"],
            "crawl_results": {crawl_ids[0]: {"price": "10"}},
        }
    }

    first = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)
    assert first.outcome == "HARVESTED"
    token = _verify_lease(store)
    crawl = store.get_crawl_work(crawl_ids[0])
    store.release_lease(token)
    result_json = crawl["result_json"]
    updated_at = crawl["updated_at"]

    deps.harvest = lambda *_args: pytest.fail("completed work must not be re-harvested")
    second = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert second.outcome == "HARVEST_NOTHING_ELIGIBLE"
    token = _verify_lease(store)
    jobs = store.list_remote_jobs(token)
    crawl_after = store.get_crawl_work(crawl_ids[0])
    store.release_lease(token)
    assert jobs[0]["state"] == "COMPLETED"
    assert jobs[0]["harvest_attempts"] == 1
    assert crawl_after["state"] == "SUCCESS"
    assert crawl_after["result_json"] == result_json
    assert crawl_after["updated_at"] == updated_at


def test_harvest_only_recovers_crashed_submissions_before_deciding_noop(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    job = store.prepare_remote_intent(
        token, "job-0", "task-0", "payload-0", mall_key="mall-0"
    )
    store.claim_remote_submission(token, job["id"])
    store.release_lease(token)
    deps = _forbidden_deps(Clock())
    observed: list[str] = []

    def harvest(jobs, _timeout):
        observed.extend(str(job["state"]) for job in jobs)
        return {
            jobs[0]["id"]: {
                "state": "SUBMITTED",
                "remote_identity": f"remote-{jobs[0]['id']}",
            }
        }

    deps.harvest = harvest
    summary = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert observed == ["SUBMIT_UNKNOWN"]
    assert summary.counters["recovered_submit_unknown"] == 1
    assert summary.outcome == "WAITING_REMOTE"
    token = _verify_lease(store)
    jobs = store.list_remote_jobs(token)
    store.release_lease(token)
    assert jobs[0]["state"] == "SUBMITTED"


def test_harvest_only_on_finalized_run_is_a_noop(tmp_path):
    store = DailyAuditStore(tmp_path / "state.sqlite")
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    snapshot = store.ingest_snapshot(
        token,
        [_snapshot_row()],
        "2026-08-11T10:00:00+00:00",
        source_fingerprints=["source"],
        customer_fingerprints=["customer"],
        headers=["vCommodityId"],
    )["snapshot"]
    job, _ = _seed_job(store, token, 0, state="COMPLETED", crawls=0)
    store.freeze_snapshot(token, int(snapshot["id"]))
    store.prepare_finalization(token, str(tmp_path / "final.xlsx"))
    store.mark_finalized(token, str(tmp_path / "final.xlsx"), "manifest")
    store.release_lease(token)
    deps = _forbidden_deps(Clock())
    deps.harvest = lambda *_args: pytest.fail("a finalized run must not re-harvest")

    summary = run_harvest_only(store, ProcessLock(tmp_path / "lock"), _config(), deps)

    assert summary.outcome == "ALREADY_FINALIZED"
    token = _verify_lease(store)
    jobs = store.list_remote_jobs(token)
    store.release_lease(token)
    assert jobs[0]["harvest_attempts"] == 0


def test_harvest_only_missing_state_is_noop_and_does_not_create_a_database(tmp_path):
    root = tmp_path / "runtime"
    paths = runtime_paths(DATE, root)
    deps = _forbidden_deps(Clock())
    deps.harvest = lambda *_args: pytest.fail("missing state must not harvest")

    result = run_runtime_command(
        "harvest-only",
        business_date=DATE,
        runtime_root=root,
        dependencies=deps,
    )

    assert result.tick.outcome == "HARVEST_NO_EXISTING_RUN"
    assert not paths.state_path.exists()


def test_harvest_only_rejects_remote_create_capability(tmp_path):
    with pytest.raises(ValueError, match="forbids the remote-create capability"):
        run_runtime_command(
            "harvest-only",
            business_date=DATE,
            runtime_root=tmp_path / "runtime",
            enable_zhaoshang=True,
            dependencies=_forbidden_deps(Clock()),
        )


def test_harvest_only_uses_existing_remote_dependencies_not_default_tick_deps(
    tmp_path, monkeypatch
):
    root = tmp_path / "runtime"
    paths = runtime_paths(DATE, root)
    paths.run_dir.mkdir(parents=True)
    DailyAuditStore(paths.state_path).ensure_run(DATE)
    marker = _forbidden_deps(Clock())
    calls: list[str] = []

    def fake_harvest_only(_store, _lock, _config, deps):
        calls.append(deps is marker)
        return TickSummary("HARVESTED", ("harvest",), {}, {"remote_pending": 0}, None)

    monkeypatch.setattr(runtime_module, "build_default_dependencies", lambda **_kwargs: pytest.fail(
        "harvest-only must not build discovery/detail/model dependencies"
    ))
    monkeypatch.setattr(runtime_module, "build_drain_dependencies", lambda **_kwargs: marker)
    monkeypatch.setattr(runtime_module, "run_harvest_only", fake_harvest_only)
    monkeypatch.setattr(
        runtime_module,
        "run_incremental_tick",
        lambda *_args, **_kwargs: pytest.fail("harvest-only must not invoke the tick engine"),
    )

    result = run_runtime_command(
        "harvest-only",
        business_date=DATE,
        runtime_root=root,
        dependencies=None,
        materialize=lambda *_args: pytest.fail("harvest-only must never materialize"),
    )

    assert result.tick.outcome == "HARVESTED"
    assert calls == [True]
    assert result.publication is None


def test_harvest_only_end_to_end_reuses_the_single_sweep_and_skips_materialization(
    tmp_path,
):
    root = tmp_path / "runtime"
    paths = runtime_paths(DATE, root)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(DATE)
    token = store.acquire_lease(DATE, "seed", 60)
    job, crawl_ids = _seed_job(store, token, 0, state="SUBMITTED", crawls=1)
    store.release_lease(token)
    deps = _forbidden_deps(Clock())
    deps.harvest = lambda jobs, _timeout: {
        jobs[0]["id"]: {
            "state": "COMPLETED",
            "remote_identity": jobs[0]["remote_identity"],
            "crawl_results": {crawl_ids[0]: {"price": "10"}},
        }
    }

    result = run_runtime_command(
        "harvest-only",
        business_date=DATE,
        runtime_root=root,
        dependencies=deps,
        materialize=lambda *_args: pytest.fail("harvest-only must never materialize"),
    )

    assert result.tick.outcome == "HARVESTED"
    assert result.publication is None
    assert result.publication_blocked is None
    assert not paths.final_output_path.exists()
    verify = DailyAuditStore(paths.state_path)
    verify_token = verify.acquire_lease(DATE, "verify", 60)
    jobs = verify.list_remote_jobs(verify_token)
    crawls = verify.list_crawl_work(verify_token)
    verify.release_lease(verify_token)
    assert jobs[0]["state"] == "COMPLETED"
    assert [crawl["state"] for crawl in crawls] == ["SUCCESS"]


def test_cli_harvest_only_has_no_remote_create_flag_and_forwards_budget(monkeypatch):
    captured = []
    summary = TickSummary("HARVESTED", (), {}, {"remote_pending": 0}, None)
    monkeypatch.setattr(
        cli,
        "run_runtime_command",
        lambda command, **kwargs: (
            captured.append((command, kwargs)) or RuntimeResult(summary)
        ),
    )
    monkeypatch.setattr(cli, "collect_runtime_status", lambda **_kwargs: {"state": "ACTIVE"})
    monkeypatch.setattr(
        cli, "write_daily_status", lambda *_args, **_kwargs: Path("daily_status.json")
    )

    assert cli.main([
        "harvest-only",
        "--business-date",
        DATE,
        "--budget-seconds",
        "3000",
        "--invocation",
        "scheduler",
    ]) == 0

    command, kwargs = captured[0]
    assert command == "harvest-only"
    assert kwargs["enable_zhaoshang"] is False
    assert kwargs["invocation"] == "scheduler"
    assert kwargs["runtime_budget_seconds"] == 3000


def test_cli_harvest_only_rejects_remote_write_flags():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([
            "harvest-only",
            "--business-date",
            DATE,
            "--enable-remote-writes",
        ])
