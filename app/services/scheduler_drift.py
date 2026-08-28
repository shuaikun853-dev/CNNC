"""Read-only Task Scheduler configuration drift detection.

The production schedule is a single Windows scheduled task
(``CNNC-PriceAudit-Incremental``) with four weekday-only triggers.  The
registration script is the source of truth; after it is applied, any manual
change (a deleted 20:35 safety-closeout trigger, ``IgnoreNew`` flipped to
``Parallel``, a stretched ``ExecutionTimeLimit``) previously went undetected.

This module compares what the registration script *would* register (its
``-NoApply`` preview JSON) against what the live machine actually has
(``Get-ScheduledTask`` read-only query).  It never mutates the scheduler:
``Register-``/``Set-``/``Unregister-``/``Start-``/``Stop-ScheduledTask`` and
the script's ``-Apply`` switch are forbidden here.

DaysOfWeek bitmask layout — measured on the deployment host (2026-08-28):
``Get-ScheduledTask`` exposes the mask as ``System.UInt16`` and every trigger
of the real task reported ``62`` while ``Export-ScheduledTask`` XML listed
exactly ``<Monday/><Tuesday/><Wednesday/><Thursday/><Friday/>``.  62 =
2|4|8|16|32, which pins the layout used below (Sunday=1 .. Saturday=64).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

SCHEDULER_TASK_NAME = "CNNC-PriceAudit-Incremental"

_REGISTRATION_SCRIPT = "scripts/register_incremental_price_audit_tasks.ps1"

#: Verified against the live task (see module docstring), not guessed.
_DAY_BIT_TO_NAME: dict[int, str] = {
    1: "Sunday",
    2: "Monday",
    4: "Tuesday",
    8: "Wednesday",
    16: "Thursday",
    32: "Friday",
    64: "Saturday",
}

_DAY_NAME_TO_BIT = {name: bit for bit, name in _DAY_BIT_TO_NAME.items()}

_LIVE_QUERY_TEMPLATE = r"""
$t = Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue
if (-not $t) {{ Write-Output '{{}}'; exit 0 }}
[pscustomobject]@{{
  state = [string]$t.State
  multiple_instances = [string]$t.Settings.MultipleInstances
  execution_time_limit = [string]$t.Settings.ExecutionTimeLimit
  start_when_available = [bool]$t.Settings.StartWhenAvailable
  action_execute = [string]$t.Actions[0].Execute
  action_arguments = [string]$t.Actions[0].Arguments
  working_directory = [string]$t.Actions[0].WorkingDirectory
  principal_logon_type = [string]$t.Principal.LogonType
  triggers = @($t.Triggers | ForEach-Object {{ [pscustomobject]@{{
    start_boundary = [string]$_.StartBoundary
    days_of_week = ([int]$_.DaysOfWeek)
    weeks_interval = [int]$_.WeeksInterval
    repetition_interval = [string]$_.Repetition.Interval
    repetition_duration = [string]$_.Repetition.Duration }} }})
}} | ConvertTo-Json -Depth 5 -Compress
"""


def normalize_iso_duration(value: object) -> str | None:
    """Normalize an ISO 8601 duration for both sides of the comparison.

    Uppercase and strip whitespace; empty/absent values become ``None`` so a
    missing repetition interval and an empty live ``""`` compare equal.
    """
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def hhmm_from_start_boundary(value: object) -> str | None:
    """Extract ``HH:mm`` from a trigger boundary.

    Live CIM boundaries look like ``2026-08-24T09:00:00+08:00`` (take the 5
    characters after ``T``); preview boundaries are already ``HH:mm``.
    """
    text = str(value or "").strip()
    marker = text.find("T")
    if marker >= 0 and len(text) >= marker + 6:
        candidate = text[marker + 1 : marker + 6]
        if len(candidate) == 5 and candidate[2] == ":":
            return candidate
    if len(text) == 5 and text[2] == ":":
        return text
    return None


def day_names_from_bitmask(mask: int) -> list[str]:
    """Decode a Task Scheduler DaysOfWeek bitmask into sorted day names."""
    bits = int(mask or 0)
    return sorted(name for bit, name in _DAY_BIT_TO_NAME.items() if bits & bit)


def bitmask_from_day_names(names: object) -> int:
    """Encode day names into a Task Scheduler DaysOfWeek bitmask."""
    if not names:
        return 0
    mask = 0
    for name in names:
        bit = _DAY_NAME_TO_BIT.get(str(name).strip())
        if bit is None:
            raise ValueError(f"unknown day-of-week name: {name!r}")
        mask |= bit
    return mask


def _powershell_executable() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("powershell")


def _run_powershell(command: str, *, timeout_seconds: float) -> subprocess.CompletedProcess:
    powershell = _powershell_executable()
    if powershell is None:
        raise RuntimeError("powershell.exe not found on PATH")
    # ``powershell.exe`` writes redirected stdout in the console code page
    # (GBK on the deployment host, which cannot round-trip the repo's Chinese
    # path), so force UTF-8 in the child exactly like the existing operations
    # tests do before decoding as utf-8-sig.
    return subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )


def _loads_lenient(stdout: str) -> object:
    text = stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _quote_ps_single(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def run_registration_preview(
    repo_root: Path,
    *,
    task_name: str = SCHEDULER_TASK_NAME,
    timeout_seconds: float = 60.0,
    enable_remote_writes: bool = False,
) -> dict | None:
    """Run the registration script in preview mode (never ``-Apply``) and parse its JSON.

    ``enable_remote_writes`` mirrors how the production host registered the
    task (``-EnableRemoteWrites`` changes the action arguments), so the
    expected spec matches what that machine actually carries instead of
    producing a permanent ``action_arguments_mismatch`` false positive.
    Returns ``None`` on any failure (script missing, powershell missing,
    timeout, non-JSON output, or a non-dict result).
    """
    try:
        root = Path(repo_root).resolve()
        script = root / _REGISTRATION_SCRIPT
        if not script.is_file():
            return None
        command = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            f"& {_quote_ps_single(script)} "
            f"-TaskName {_quote_ps_single(task_name)} "
            f"-RepoRoot {_quote_ps_single(root)} "
            f"-PythonExe {_quote_ps_single(sys.executable)}"
        )
        if enable_remote_writes:
            command += " -EnableRemoteWrites"
        completed = _run_powershell(command, timeout_seconds=timeout_seconds)
        if completed.returncode != 0:
            return None
        preview = _loads_lenient(completed.stdout.decode("utf-8-sig"))
    except Exception:
        return None
    return preview if isinstance(preview, dict) else None


def expected_scheduler_spec(preview: Mapping) -> dict:
    """Normalize the registration preview JSON into the expected spec shape."""
    tasks = preview.get("Tasks") or []
    task = tasks[0]
    weeks_interval = int(task.get("WeeksInterval") or 1)
    triggers = []
    for trigger in task.get("Triggers") or []:
        triggers.append(
            {
                "name": str(trigger.get("Name") or ""),
                "start": hhmm_from_start_boundary(trigger.get("StartBoundary")),
                "interval": normalize_iso_duration(trigger.get("RepetitionInterval")),
                "repeat_for": normalize_iso_duration(trigger.get("RepeatFor")),
                "days_of_week": sorted(str(day) for day in (trigger.get("DaysOfWeek") or [])),
                "weeks_interval": weeks_interval,
            }
        )
    return {
        "task_name": str(task.get("TaskName") or ""),
        "multiple_instances": str(preview.get("MultipleInstances") or ""),
        "execution_time_limit": normalize_iso_duration(preview.get("ExecutionTimeLimit")),
        "action_execute": str(task.get("Command") or ""),
        "action_arguments": str(task.get("Arguments") or ""),
        "working_directory": str(task.get("WorkingDirectory") or ""),
        "triggers": triggers,
    }


def live_scheduler_spec(
    *, task_name: str = SCHEDULER_TASK_NAME, timeout_seconds: float = 30.0
) -> dict | None:
    """Query the live scheduled task read-only (``Get-*`` only) and normalize it.

    Returns ``None`` when the task does not exist on this machine (the probe
    emits ``{}`).  Raises ``RuntimeError`` when the query itself fails, so a
    broken query is never mistaken for a missing task.
    """
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        + _LIVE_QUERY_TEMPLATE.format(task_name=str(task_name).replace("'", "''"))
    )
    completed = _run_powershell(command, timeout_seconds=timeout_seconds)
    if completed.returncode != 0:
        raise RuntimeError(
            f"live scheduler query failed with exit code {completed.returncode}: "
            + completed.stderr.decode("utf-8", errors="replace").strip()[:200]
        )
    try:
        raw = _loads_lenient(completed.stdout.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"live scheduler query returned unparseable output: {type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("live scheduler query returned unexpected JSON shape")
    if not raw:
        return None
    triggers = []
    for trigger in raw.get("triggers") or []:
        triggers.append(
            {
                "name": "",
                "start": hhmm_from_start_boundary(trigger.get("start_boundary")),
                "interval": normalize_iso_duration(trigger.get("repetition_interval")),
                "repeat_for": normalize_iso_duration(trigger.get("repetition_duration")),
                "days_of_week": day_names_from_bitmask(trigger.get("days_of_week") or 0),
                "days_of_week_bitmask": int(trigger.get("days_of_week") or 0),
                "weeks_interval": int(trigger.get("weeks_interval") or 0),
            }
        )
    return {
        "task_name": str(task_name),
        "state": str(raw.get("state") or ""),
        "multiple_instances": str(raw.get("multiple_instances") or ""),
        "execution_time_limit": normalize_iso_duration(raw.get("execution_time_limit")),
        "start_when_available": bool(raw.get("start_when_available")),
        "action_execute": str(raw.get("action_execute") or ""),
        "action_arguments": str(raw.get("action_arguments") or ""),
        "working_directory": str(raw.get("working_directory") or ""),
        "principal_logon_type": str(raw.get("principal_logon_type") or ""),
        "triggers": triggers,
    }


def _path_key(value: object) -> str:
    return str(value or "").strip().rstrip("\\/").casefold()


def _trigger_key(trigger: Mapping) -> tuple[str, str | None]:
    return (str(trigger.get("start") or ""), normalize_iso_duration(trigger.get("interval")))


def _same_window(expected_trigger: Mapping, live_trigger: Mapping) -> bool:
    return (
        sorted(expected_trigger.get("days_of_week") or []) == sorted(live_trigger.get("days_of_week") or [])
        and int(expected_trigger.get("weeks_interval") or 0) == int(live_trigger.get("weeks_interval") or 0)
        and normalize_iso_duration(expected_trigger.get("repeat_for"))
        == normalize_iso_duration(live_trigger.get("repeat_for"))
    )


def compare_scheduler_specs(expected: Mapping, live: Mapping) -> dict:
    """Pure comparison of the expected and live (already normalized) specs.

    Returns ``{"SCHEDULER_CONFIG_MATCH": bool, "drift": [str], "compared_fields": [str]}``.
    Only fields both sides reliably expose are compared; drift codes are
    emitted once each and sorted for stable output.
    """
    drift: set[str] = set()
    compared: list[str] = []

    if str(expected.get("multiple_instances") or "").strip() != str(live.get("multiple_instances") or "").strip():
        drift.add("multiple_instances_mismatch")
    compared.append("multiple_instances")

    if normalize_iso_duration(expected.get("execution_time_limit")) != normalize_iso_duration(
        live.get("execution_time_limit")
    ):
        drift.add("execution_time_limit_mismatch")
    compared.append("execution_time_limit")

    if _path_key(expected.get("action_execute")) != _path_key(live.get("action_execute")):
        drift.add("action_execute_mismatch")
    compared.append("action_execute")

    if str(expected.get("action_arguments") or "") != str(live.get("action_arguments") or ""):
        drift.add("action_arguments_mismatch")
    compared.append("action_arguments")

    if _path_key(expected.get("working_directory")) != _path_key(live.get("working_directory")):
        drift.add("working_directory_mismatch")
    compared.append("working_directory")

    if live.get("start_when_available") is True:
        drift.add("start_when_available_enabled")
    compared.append("start_when_available")

    live_by_key: dict[tuple[str, str | None], list[Mapping]] = {}
    for trigger in live.get("triggers") or []:
        live_by_key.setdefault(_trigger_key(trigger), []).append(trigger)
    for expected_trigger in expected.get("triggers") or []:
        start = str(expected_trigger.get("start") or "")
        bucket = live_by_key.get(_trigger_key(expected_trigger))
        compared.append(f"trigger@{start}")
        if bucket:
            live_trigger = bucket.pop(0)
            if not _same_window(expected_trigger, live_trigger):
                drift.add(f"trigger_window_mismatch_at_{start}")
        else:
            drift.add(f"missing_trigger_at_{start}")
    for key, bucket in live_by_key.items():
        for _ in bucket:
            drift.add(f"unexpected_trigger_at_{key[0]}")
            compared.append(f"trigger@{key[0]}")

    logon_type = str(live.get("principal_logon_type") or "").strip()
    if logon_type and logon_type != "Interactive":
        drift.add("principal_logon_type_mismatch")
    compared.append("principal_logon_type")

    sorted_drift = sorted(drift)
    return {
        "SCHEDULER_CONFIG_MATCH": not sorted_drift,
        "drift": sorted_drift,
        "compared_fields": compared,
    }


def check_scheduler_drift(
    repo_root: Path,
    *,
    task_name: str = SCHEDULER_TASK_NAME,
    enable_remote_writes: bool = False,
) -> dict:
    """Orchestrate preview-vs-live drift detection; strictly read-only.

    ``enable_remote_writes`` must reflect how the live task was registered:
    the production schedule carries ``-EnableRemoteWrites`` in its action
    arguments, so the expected preview must be built with the same flag
    (still never ``-Apply``) to keep the comparison meaningful.
    Raises ``RuntimeError`` when the check itself cannot run (powershell.exe
    unavailable, or the registration preview cannot be produced) so callers
    can distinguish "check unavailable" from "task missing" or "drift found".
    """
    if _powershell_executable() is None:
        raise RuntimeError("powershell.exe not found on PATH; scheduler drift check unavailable")
    preview = run_registration_preview(
        Path(repo_root), task_name=task_name, enable_remote_writes=enable_remote_writes
    )
    if preview is None:
        raise RuntimeError(
            f"registration preview unavailable for {task_name}; cannot derive expected scheduler spec"
        )
    expected = expected_scheduler_spec(preview)
    live = live_scheduler_spec(task_name=task_name)
    if live is None:
        return {
            "live_task_missing": True,
            "SCHEDULER_CONFIG_MATCH": False,
            "drift": ["live_task_missing"],
            "compared_fields": [],
        }
    report = compare_scheduler_specs(expected, live)
    report["live_state"] = live.get("state")
    return report
