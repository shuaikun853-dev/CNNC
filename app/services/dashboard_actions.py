"""Bounded operator-action runtime for the local dashboard.

POST 只做三件事:校验 → durable 意图落账(dashboard_actions.jsonl) → spawn
既有 CLI 子进程(closeout/re-export/rebuild)。HTTP 立即返回 ACTION_RUNNING;
结果由 CLI 自身写回 JSONL(run_*_command)或 runner 线程在子进程退出后补记
(closeout)。绝不实现第二套业务状态机;一切锁/幂等语义都在 CLI 指向的既有
函数里。refresh-progress 不经子进程,由调用方直接用 progress service。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from app.services.dashboard_manual_ops import (
    REBUILD_SUCCESS_OUTCOMES,
    REEXPORT_SUCCESS_OUTCOMES,
    read_operator_actions,
    record_operator_action,
)
from app.services.incremental_audit_runtime import DEFAULT_RUNTIME_ROOT

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AUDIT_SCRIPT = Path("scripts") / "incremental_price_audit.py"

ACTION_TYPES = frozenset({"closeout", "force-closeout", "re-export", "rebuild"})

# Per-type supervision budget (wall seconds) for the joiner thread; the CLI
# budgets themselves are smaller (closeout 720s), these add restart headroom.
_MAX_WAIT_SECONDS = {
    "closeout": 900.0,
    "force-closeout": 900.0,
    "re-export": 420.0,
    "rebuild": 300.0,
}
_TERMINATE_GRACE_SECONDS = 10.0

# closeout result codes that map onto a FAILED handle.
_CLOSEOUT_FAILED_CODES = frozenset({"BLOCKED", "FAILED", "TIMEOUT"})
_CLOSEOUT_BLOCKED_OUTCOMES = frozenset(
    {"FAILED", "MANUAL_REVIEW_REQUIRED", "DISCOVERY_INCOMPLETE", "INCOMPLETE"}
)
_FINALIZATION_ACTION_TYPES = frozenset({"closeout", "force-closeout"})


class DashboardActionError(RuntimeError):
    """Operator-action failure carrying a stable dashboard error code."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        existing_action_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.existing_action_id = existing_action_id


@dataclass
class ActionHandle:
    action_id: str
    action_type: str
    target: str
    business_date: str
    started_at: str
    status: str = "RUNNING"
    outcome: dict | None = None
    pid: int | None = None


def _default_spawn(argv: list[str], *, cwd: Path, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
    )


def _parse_last_json_line(text: str) -> dict | None:
    """Best-effort: the last JSON-object line of a CLI stdout dump."""
    for line in reversed((text or "").splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            data = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _closeout_result_code(parsed: dict | None, exit_code: int | None, stdout: str) -> str:
    raw = str((parsed or {}).get("outcome") or "")
    publication = (parsed or {}).get("publication")
    blocked = (parsed or {}).get("publication_blocked")
    if publication is not None:
        return "FINALIZED"
    if raw == "FORCE_FINALIZED" and bool((parsed or {}).get("finalized")):
        return "FINALIZED"
    if raw == "ALREADY_FINALIZED":
        return "ALREADY_FINALIZED"
    if raw == "ALREADY_RUNNING" or "SKIPPED_ALREADY_RUNNING" in (stdout or ""):
        return "ALREADY_RUNNING"
    if blocked or (exit_code == 2 and raw in _CLOSEOUT_BLOCKED_OUTCOMES):
        return "BLOCKED"
    if raw == "CLOSEOUT_DEADLINE":
        return "DEADLINE"
    if raw.startswith("FORCE_FINALIZE_"):
        return "BLOCKED"
    return "FAILED"


def _durable_status_from_record(record: dict) -> str:
    result_code = record.get("result_code")
    if result_code:
        return "FAILED" if str(result_code) in _CLOSEOUT_FAILED_CODES else "SUCCEEDED"
    outcome = record.get("outcome")
    if isinstance(outcome, str) and outcome:
        action_type = str(record.get("action_type") or "")
        if action_type == "manual_reexport":
            return "SUCCEEDED" if outcome in REEXPORT_SUCCESS_OUTCOMES else "FAILED"
        if action_type == "rebuild":
            return "SUCCEEDED" if outcome in REBUILD_SUCCESS_OUTCOMES else "FAILED"
    return "RUNNING"


class DashboardActionRunner:
    """Spawns the existing audit CLI for dashboard operator clicks.

    One (action_type, target) pair may run at a time; every start is durably
    recorded before the subprocess exists, and the joiner thread closes the
    loop (closeout gets a runner-written ``finished`` record; re-export and
    rebuild already write their own result records inside the CLI).
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        runtime_root=DEFAULT_RUNTIME_ROOT,
        python_executable: str = sys.executable,
        spawn: Callable[..., subprocess.Popen] | None = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._runtime_root = Path(runtime_root)
        self._python_executable = str(python_executable)
        self._spawn: Callable[..., subprocess.Popen] = spawn or _default_spawn
        self._handles: dict[str, ActionHandle] = {}
        self._inflight: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        action_type: str,
        business_date: str,
        remote_job_id: int | None = None,
        remote_identity: str | None = None,
        timeout_seconds: float | None = None,
        action_id: str | None = None,
        max_incomplete: int | None = None,
        expected_missing_page_price_rows: int | None = None,
    ) -> ActionHandle:
        if action_type not in ACTION_TYPES:
            raise DashboardActionError(
                f"unsupported dashboard action type: {action_type!r}",
                code="INVALID_ACTION",
            )
        resolved_action_id = str(action_id) if action_id else uuid.uuid4().hex
        target = f"{business_date}:{remote_job_id if remote_job_id is not None else '-'}"
        action_group = "closeout" if action_type in _FINALIZATION_ACTION_TYPES else action_type
        key = (action_group, target)
        handle = ActionHandle(
            action_id=resolved_action_id,
            action_type=action_type,
            target=target,
            business_date=business_date,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        # Atomic double-click guard: check and reserve the (type, target) slot
        # under one lock so concurrent starts can never both spawn.
        with self._lock:
            existing_id = self._inflight.get(key)
            if existing_id is not None:
                existing = self._handles.get(existing_id)
                if existing is not None and existing.status == "RUNNING":
                    raise DashboardActionError(
                        f"action already in flight for {target}",
                        code="ACTION_IN_FLIGHT",
                        existing_action_id=existing_id,
                    )
            self._inflight[key] = resolved_action_id
            self._handles[resolved_action_id] = handle
        argv = self._build_argv(
            action_type=action_type,
            business_date=business_date,
            remote_job_id=remote_job_id,
            remote_identity=remote_identity,
            timeout_seconds=timeout_seconds,
            action_id=resolved_action_id,
            max_incomplete=max_incomplete,
            expected_missing_page_price_rows=expected_missing_page_price_rows,
        )
        run_dir = self._runtime_root / business_date
        # Durable intent precedes the subprocess: a crash between the two
        # leaves a started record, never a ghost process without a record.
        try:
            record_operator_action(
                run_dir,
                action_type=action_type,
                action_id=resolved_action_id,
                payload={
                    "event": "started",
                    "target": target,
                    "pid": None,
                    "argv_tail": argv[-6:],
                },
            )
        except Exception as exc:
            handle.status = "FAILED"
            handle.outcome = {"error": f"intent record failed: {exc}"}
            with self._lock:
                if self._inflight.get(key) == resolved_action_id:
                    self._inflight.pop(key, None)
            raise DashboardActionError(
                f"failed to record dashboard action intent: {exc}",
                code="RECORD_FAILED",
            ) from exc
        try:
            process = self._spawn(argv, cwd=self._repo_root, env=dict(os.environ))
        except Exception as exc:
            handle.status = "FAILED"
            handle.outcome = {"error": f"spawn failed: {exc}"}
            with self._lock:
                if self._inflight.get(key) == resolved_action_id:
                    self._inflight.pop(key, None)
            self._record_finished(
                run_dir,
                action_type,
                resolved_action_id,
                target,
                result_code="FAILED",
                outcome_summary={"error": f"spawn failed: {exc}"},
                exit_code=None,
            )
            raise DashboardActionError(
                f"failed to spawn dashboard action subprocess: {exc}",
                code="SPAWN_FAILED",
            ) from exc
        handle.pid = getattr(process, "pid", None)
        thread = threading.Thread(
            target=self._supervise,
            args=(handle, process, run_dir),
            name=f"dashboard-action-{resolved_action_id[:8]}",
            daemon=True,
        )
        thread.start()
        return handle

    def get(self, action_id: str) -> ActionHandle | None:
        with self._lock:
            return self._handles.get(str(action_id))

    def list_recent(self, limit: int = 20) -> list[ActionHandle]:
        with self._lock:
            handles = list(self._handles.values())
        running = [h for h in handles if h.status == "RUNNING"]
        finished = sorted(
            (h for h in handles if h.status != "RUNNING"),
            key=lambda h: h.started_at,
            reverse=True,
        )
        return (running + finished)[: max(0, int(limit))]

    def durable_status(self, action_id: str, run_dir) -> dict | None:
        """Latest durable JSONL record for one action id in a run dir."""
        try:
            records = read_operator_actions(run_dir, limit=200)
        except Exception:
            return None
        matches = [
            record for record in records
            if str(record.get("action_id") or "") == str(action_id)
        ]
        if not matches:
            return None
        finished = [record for record in matches if record.get("event") == "finished"]
        record = finished[-1] if finished else matches[-1]
        outcome = record.get("outcome")
        return {
            "action_id": str(record.get("action_id") or action_id),
            "action_type": str(record.get("action_type") or ""),
            "status": _durable_status_from_record(record),
            "outcome": dict(outcome) if isinstance(outcome, dict) else (
                {"outcome": outcome} if outcome is not None else None
            ),
            "started_at": record.get("recorded_at"),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_argv(
        self,
        *,
        action_type: str,
        business_date: str,
        remote_job_id: int | None,
        remote_identity: str | None,
        timeout_seconds: float | None,
        action_id: str,
        max_incomplete: int | None,
        expected_missing_page_price_rows: int | None,
    ) -> list[str]:
        script = str(self._repo_root / _AUDIT_SCRIPT)
        common = [
            self._python_executable,
            script,
        ]
        if action_type == "closeout":
            return [
                *common,
                "closeout",
                "--business-date",
                business_date,
                "--runtime-root",
                str(self._runtime_root),
                "--invocation",
                "manual",
            ]
        if action_type == "force-closeout":
            return [
                *common,
                "force-finalize",
                "--business-date",
                business_date,
                "--runtime-root",
                str(self._runtime_root),
                "--max-incomplete",
                str(int(max_incomplete if max_incomplete is not None else 20)),
                "--accept-unresolved-crawl",
                "--expected-missing-page-price-rows",
                str(int(expected_missing_page_price_rows or 0)),
            ]
        command = "reexport-existing-remote" if action_type == "re-export" else "rebuild-remote-task"
        default_timeout = 180.0 if action_type == "re-export" else 120.0
        return [
            *common,
            command,
            "--business-date",
            business_date,
            "--runtime-root",
            str(self._runtime_root),
            "--remote-job-id",
            str(int(remote_job_id or 0)),
            "--remote-identity",
            str(remote_identity or ""),
            "--timeout-seconds",
            str(float(timeout_seconds if timeout_seconds is not None else default_timeout)),
            "--action-id",
            action_id,
        ]

    @staticmethod
    def _record_finished(
        run_dir,
        action_type: str,
        action_id: str,
        target: str,
        *,
        result_code: str,
        outcome_summary: dict | None,
        exit_code: int | None,
        timed_out: bool = False,
    ) -> None:
        payload: dict = {
            "event": "finished",
            "target": target,
            "result_code": result_code,
            "outcome": outcome_summary,
            "exit_code": exit_code,
        }
        if timed_out:
            payload["timeout"] = True
        try:
            record_operator_action(
                run_dir,
                action_type=action_type,
                action_id=action_id,
                payload=payload,
            )
        except Exception:
            # The durable result record is best-effort; the handle (and the
            # CLI's own durable writes) already carry the authoritative state.
            pass

    def _supervise(self, handle: ActionHandle, process, run_dir) -> None:
        max_wait = _MAX_WAIT_SECONDS.get(handle.action_type, 300.0)
        try:
            timed_out = False
            try:
                stdout, _stderr = process.communicate(timeout=max_wait)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()
                try:
                    stdout, _stderr = process.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, _stderr = process.communicate()
            exit_code = process.returncode
            parsed = _parse_last_json_line(stdout or "")
            if handle.action_type in _FINALIZATION_ACTION_TYPES:
                if timed_out:
                    result_code = "TIMEOUT"
                else:
                    result_code = _closeout_result_code(parsed, exit_code, stdout or "")
                raw_outcome = str((parsed or {}).get("outcome") or "") or None
                summary = {
                    "outcome": raw_outcome,
                    "publication": (parsed or {}).get("publication") or (parsed or {}).get("output_path"),
                    "publication_blocked": (parsed or {}).get("publication_blocked"),
                    "missing_page_price_rows": (parsed or {}).get("missing_page_price_rows"),
                }
                handle.outcome = {
                    "result_code": result_code,
                    "exit_code": exit_code,
                    **summary,
                }
                handle.status = (
                    "FAILED" if result_code in _CLOSEOUT_FAILED_CODES else "SUCCEEDED"
                )
                self._record_finished(
                    run_dir,
                    handle.action_type,
                    handle.action_id,
                    handle.target,
                    result_code=result_code,
                    outcome_summary=summary,
                    exit_code=exit_code,
                    timed_out=timed_out,
                )
            else:
                handle.outcome = dict(parsed) if parsed is not None else {
                    "exit_code": exit_code,
                    **({"timeout": True} if timed_out else {}),
                }
                if timed_out:
                    handle.outcome["error"] = "TIMEOUT"
                handle.status = "SUCCEEDED" if exit_code == 0 and not timed_out else "FAILED"
        except Exception as exc:  # pragma: no cover - defensive supervisor guard
            handle.outcome = {"error": str(exc)}
            handle.status = "FAILED"
            self._record_finished(
                run_dir,
                handle.action_type,
                handle.action_id,
                handle.target,
                result_code="FAILED",
                outcome_summary={"error": str(exc)},
                exit_code=None,
            )
        finally:
            with self._lock:
                action_group = (
                    "closeout"
                    if handle.action_type in _FINALIZATION_ACTION_TYPES
                    else handle.action_type
                )
                current = self._inflight.get((action_group, handle.target))
                if current == handle.action_id:
                    self._inflight.pop((action_group, handle.target), None)


_RUNNER: DashboardActionRunner | None = None
_RUNNER_LOCK = threading.Lock()


def get_action_runner() -> DashboardActionRunner:
    """Process-lifetime singleton for dashboard injection; tests build their own."""
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = DashboardActionRunner(
                repo_root=_PROJECT_ROOT,
                runtime_root=DEFAULT_RUNTIME_ROOT,
            )
        return _RUNNER
