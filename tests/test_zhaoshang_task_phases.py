import importlib
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime

import pytest


def load_phases():
    try:
        return importlib.import_module("scripts.zhaoshang_task_phases")
    except ModuleNotFoundError:
        pytest.fail("scripts.zhaoshang_task_phases has not been implemented")


def make_job(module, tmp_path, mall):
    return module.MallJob(
        mall=mall,
        task_name=f"task-{mall}",
        input_path=tmp_path / f"input-{mall}.xlsx",
        result_path=tmp_path / f"result-{mall}.xlsx",
        code_to_url={mall: f"https://{mall}.example/item"},
    )


def completed(job, mode, returncode=0):
    return subprocess.CompletedProcess(
        args=[job.mall, mode],
        returncode=returncode,
        stdout=f"{job.mall}:{mode}",
        stderr="",
    )


def test_create_phase_is_sequential_and_skips_existing_exports(tmp_path):
    module = load_phases()
    jobs = [make_job(module, tmp_path, mall) for mall in ("done", "a", "b")]
    jobs[0].result_path.write_text("existing", encoding="utf-8")
    calls = []

    def runner(job, mode):
        calls.append((job.mall, mode))
        return completed(job, mode)

    result = module.run_create_phase(jobs, runner)

    assert calls == [("a", "create-only"), ("b", "create-only")]
    assert [job.mall for job in result.reused] == ["done"]
    assert [job.mall for job in result.pending] == ["a", "b"]
    assert all(outcome.succeeded for outcome in result.outcomes)


def test_create_failure_stops_later_non_idempotent_calls(tmp_path):
    module = load_phases()
    jobs = [make_job(module, tmp_path, mall) for mall in ("bad", "never")]
    calls = []

    def runner(job, mode):
        calls.append(job.mall)
        return completed(job, mode, returncode=7)

    with pytest.raises(module.CreatePhaseError) as error:
        module.run_create_phase(jobs, runner)

    assert calls == ["bad"]
    assert error.value.outcome.job.mall == "bad"


def test_wait_export_phase_is_bounded_and_actually_concurrent(tmp_path):
    module = load_phases()
    jobs = [make_job(module, tmp_path, str(index)) for index in range(4)]
    lock = threading.Lock()
    active = 0
    peak = 0

    def runner(job, mode):
        nonlocal active, peak
        assert mode == "wait-export-only"
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        return completed(job, mode)

    result = module.run_wait_export_phase(jobs, runner, max_workers=2)

    assert peak == 2
    assert len(result.outcomes) == 4
    assert all(outcome.succeeded for outcome in result.outcomes)


def test_wait_failure_does_not_cancel_other_existing_tasks(tmp_path):
    module = load_phases()
    jobs = [make_job(module, tmp_path, mall) for mall in ("a", "bad", "c")]
    calls = []

    def runner(job, mode):
        calls.append(job.mall)
        return completed(job, mode, returncode=5 if job.mall == "bad" else 0)

    result = module.run_wait_export_phase(jobs, runner, max_workers=3)

    assert set(calls) == {"a", "bad", "c"}
    assert [outcome.job.mall for outcome in result.failures] == ["bad"]


def test_wait_phase_skips_result_created_during_create_phase(tmp_path):
    module = load_phases()
    job = make_job(module, tmp_path, "complete")
    job.result_path.write_text("exported", encoding="utf-8")

    result = module.run_wait_export_phase(
        [job],
        lambda *_args: pytest.fail("existing export must not be submitted"),
        max_workers=4,
    )

    assert result.outcomes == ()
    assert result.reused == (job,)


def test_invalid_existing_result_is_not_reused(tmp_path):
    module = load_phases()
    job = make_job(module, tmp_path, "invalid")
    job.result_path.write_text("truncated", encoding="utf-8")
    calls = []

    result = module.run_create_phase(
        [job],
        lambda current, mode: calls.append((current.mall, mode))
        or completed(current, mode),
        is_reusable=lambda _job: False,
    )

    assert calls == [("invalid", "create-only")]
    assert result.reused == ()
    assert result.pending == (job,)


def test_task_name_marker_is_generated_at_job_creation_time():
    module = load_phases()

    name = module.build_zhaoshang_task_name(
        "广博商城",
        now=datetime(2026, 8, 11, 18, 11, 49, 361499),
    )

    assert name == "cnnc_current_opt_广博商城_20260811181149361499"


def test_create_phase_prepares_fresh_identity_immediately_before_runner(tmp_path):
    module = load_phases()
    original = make_job(module, tmp_path, "guangbo")
    seen = []

    result = module.run_create_phase(
        [original],
        lambda job, mode: seen.append((job.task_name, mode)) or completed(job, mode),
        prepare_job=lambda job: replace(
            job,
            task_name="cnnc_current_opt_广博商城_20260811181149361499",
        ),
    )

    assert seen == [
        ("cnnc_current_opt_广博商城_20260811181149361499", "create-only")
    ]
    assert result.pending[0].task_name.endswith("20260811181149361499")
