from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence


CREATE_ONLY = "create-only"
WAIT_EXPORT_ONLY = "wait-export-only"


def build_zhaoshang_task_name(mall_name, now=None):
    marker = (now or datetime.now()).strftime("%Y%m%d%H%M%S%f")
    return f"cnnc_current_opt_{mall_name}_{marker}"


@dataclass(frozen=True)
class MallJob:
    mall: str
    task_name: str
    input_path: Path
    result_path: Path
    code_to_url: Mapping[str, str] = field(compare=False, repr=False)


@dataclass(frozen=True)
class JobOutcome:
    job: MallJob
    mode: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    exception: str = ""

    @property
    def succeeded(self):
        return self.returncode == 0 and not self.exception


@dataclass(frozen=True)
class PhaseResult:
    outcomes: tuple[JobOutcome, ...]
    reused: tuple[MallJob, ...]
    pending: tuple[MallJob, ...] = ()

    @property
    def failures(self):
        return tuple(outcome for outcome in self.outcomes if not outcome.succeeded)


class CreatePhaseError(RuntimeError):
    def __init__(self, outcome, completed=(), reused=()):
        self.outcome = outcome
        self.completed = tuple(completed)
        self.reused = tuple(reused)
        detail = outcome.exception or outcome.stderr or f"exit code {outcome.returncode}"
        super().__init__(f"create phase failed for mall={outcome.job.mall}: {detail}")


def _run_job(job, mode, runner):
    try:
        process = runner(job, mode)
    except Exception as exc:
        return JobOutcome(
            job=job,
            mode=mode,
            returncode=-1,
            exception=f"{type(exc).__name__}: {exc}",
        )
    return JobOutcome(
        job=job,
        mode=mode,
        returncode=int(process.returncode),
        stdout=str(process.stdout or ""),
        stderr=str(process.stderr or ""),
    )


def run_create_phase(
    jobs: Sequence[MallJob],
    runner: Callable,
    on_outcome: Callable[[JobOutcome], None] | None = None,
    is_reusable: Callable[[MallJob], bool] | None = None,
    prepare_job: Callable[[MallJob], MallJob] | None = None,
):
    outcomes = []
    reused = []
    pending = []
    reusable = is_reusable or (lambda job: job.result_path.exists())
    for original_job in jobs:
        if reusable(original_job):
            reused.append(original_job)
            continue
        job = prepare_job(original_job) if prepare_job else original_job
        outcome = _run_job(job, CREATE_ONLY, runner)
        outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome)
        if not outcome.succeeded:
            raise CreatePhaseError(outcome, completed=outcomes[:-1], reused=reused)
        if reusable(job):
            reused.append(job)
        else:
            pending.append(job)
    return PhaseResult(tuple(outcomes), tuple(reused), tuple(pending))


def run_wait_export_phase(
    jobs: Sequence[MallJob],
    runner: Callable,
    max_workers: int,
    on_outcome: Callable[[JobOutcome], None] | None = None,
    is_reusable: Callable[[MallJob], bool] | None = None,
):
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    reusable = is_reusable or (lambda job: job.result_path.exists())
    reused = tuple(job for job in jobs if reusable(job))
    pending = [job for job in jobs if not reusable(job)]
    if not pending:
        return PhaseResult((), reused)

    outcomes_by_job = {}
    worker_count = min(max_workers, len(pending))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_run_job, job, WAIT_EXPORT_ONLY, runner): job
            for job in pending
        }
        for future in as_completed(futures):
            outcome = future.result()
            outcomes_by_job[outcome.job] = outcome
            if on_outcome:
                on_outcome(outcome)

    outcomes = tuple(outcomes_by_job[job] for job in pending)
    return PhaseResult(outcomes, reused)
