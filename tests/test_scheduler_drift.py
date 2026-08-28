"""Scheduler config drift detector tests.

Test levels (see docs/superpowers conventions):
- pure-function cases: hermetic ``compare_scheduler_specs`` comparisons built
  from an embedded preview-equivalent fixture; no subprocess, no scheduler.
- L2 cases: run the real registration script preview through powershell.exe
  (read-only; the script exits before any Register-ScheduledTask because
  ``-Apply`` is never passed).
- live-machine case: read-only ``Get-ScheduledTask`` comparison, skipped
  unless ``CNNC_SCHEDULER_LIVE_DRIFT=1`` is set in the environment.
"""

from __future__ import annotations

import copy
import os
import shutil
from pathlib import Path

import pytest

from app.services.scheduler_drift import (
    SCHEDULER_TASK_NAME,
    bitmask_from_day_names,
    compare_scheduler_specs,
    day_names_from_bitmask,
    expected_scheduler_spec,
    live_scheduler_spec,
    run_registration_preview,
)

ROOT = Path(__file__).resolve().parents[1]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
WEEKDAY_MASK = bitmask_from_day_names(WEEKDAYS)
POWERSHELL_AVAILABLE = shutil.which("powershell.exe") is not None or shutil.which("powershell") is not None

# Equivalent to the registration script's preview JSON (scripts/
# register_incremental_price_audit_tasks.ps1 without -Apply); see the L2 test
# below which asserts the real script still agrees with this fixture.
PREVIEW_FIXTURE = {
    "Applied": False,
    "Message": "Preview only. Rerun with -Apply only after reviewing this exact production schedule.",
    "MultipleInstances": "IgnoreNew",
    "ExecutionTimeLimit": "PT32M",
    "BudgetSeconds": 720,
    "CloseoutBudgetSeconds": 1680,
    "RemoteWrites": False,
    "ConflictingTaskNames": [f"{SCHEDULER_TASK_NAME}-Tick", f"{SCHEDULER_TASK_NAME}-Finalize"],
    "Tasks": [
        {
            "TaskName": SCHEDULER_TASK_NAME,
            "Command": "C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
            "Arguments": (
                "-NoProfile -ExecutionPolicy Bypass "
                "-File \"E:\\repo\\scripts\\run_incremental_price_audit_scheduled.ps1\" "
                "-RepoRoot \"E:\\repo\" -PythonExe \"E:\\Python314\\python.exe\" "
                "-RuntimeRoot \"E:\\repo\\runtime\\incremental_price_audit\" "
                "-BudgetSeconds 720 -CloseoutBudgetSeconds 1680 "
                "-HardTimeoutMinutes 32 -ZhaoshangBackend legacy"
            ),
            "WorkingDirectory": "E:\\repo",
            "PythonExe": "E:\\Python314\\python.exe",
            "RuntimeRoot": "E:\\repo\\runtime\\incremental_price_audit",
            "TriggerType": "Weekly",
            "DaysOfWeek": WEEKDAYS,
            "WeeksInterval": 1,
            "StartBoundary": "09:00",
            "LastPlannedStart": "20:35",
            "Triggers": [
                {
                    "Name": "Baseline",
                    "DaysOfWeek": WEEKDAYS,
                    "StartBoundary": "09:00",
                    "RepetitionInterval": "PT15M",
                    "RepeatFor": "PT7H46M",
                    "LastPlannedStart": "16:45",
                },
                {
                    "Name": "Peak",
                    "DaysOfWeek": WEEKDAYS,
                    "StartBoundary": "17:00",
                    "RepetitionInterval": "PT5M",
                    "RepeatFor": "PT56M",
                    "LastPlannedStart": "17:55",
                },
                {
                    "Name": "Closeout",
                    "DaysOfWeek": WEEKDAYS,
                    "StartBoundary": "18:00",
                    "RepetitionInterval": "PT5M",
                    "RepeatFor": "PT2H1M",
                    "LastPlannedStart": "20:00",
                },
                {
                    "Name": "SafetyCloseout",
                    "DaysOfWeek": WEEKDAYS,
                    "StartBoundary": "20:35",
                    "RepetitionInterval": None,
                    "RepeatFor": None,
                    "LastPlannedStart": "20:35",
                },
            ],
        }
    ],
}


def _expected_fixture() -> dict:
    return expected_scheduler_spec(PREVIEW_FIXTURE)


def _remote_writes_preview_fixture() -> dict:
    """PREVIEW_FIXTURE as the registration script emits it with -EnableRemoteWrites.

    The production host registers the task with ``-EnableRemoteWrites`` (never
    ``-Apply``): the switch appends ``-EnableRemoteWrites`` to the action
    arguments and flips the preview's ``RemoteWrites`` marker.
    """
    flagged = copy.deepcopy(PREVIEW_FIXTURE)
    flagged["RemoteWrites"] = True
    flagged["Tasks"][0]["Arguments"] += " -EnableRemoteWrites"
    return flagged


def _live_match_fixture(expected: dict) -> dict:
    """A live spec that matches ``expected`` (one bitmask-decoded trigger per expected trigger)."""
    return {
        "task_name": expected["task_name"],
        "state": "Ready",
        "multiple_instances": expected["multiple_instances"],
        "execution_time_limit": expected["execution_time_limit"],
        "start_when_available": False,
        "action_execute": expected["action_execute"],
        "action_arguments": expected["action_arguments"],
        "working_directory": expected["working_directory"],
        "principal_logon_type": "Interactive",
        "triggers": [
            {
                "name": "",
                "start": trigger["start"],
                "interval": trigger["interval"],
                "repeat_for": trigger["repeat_for"],
                "days_of_week": day_names_from_bitmask(WEEKDAY_MASK),
                "days_of_week_bitmask": WEEKDAY_MASK,
                "weeks_interval": trigger["weeks_interval"],
            }
            for trigger in expected["triggers"]
        ],
    }


def test_bitmask_round_trip_matches_measured_monday_to_friday_value():
    # Measured on the real task: [int]$_.DaysOfWeek == 62 with XML confirming
    # Monday..Friday, so the bit layout must decode 62 to exactly those days.
    assert WEEKDAY_MASK == 62
    assert day_names_from_bitmask(62) == sorted(WEEKDAYS)


def test_compare_reports_match_for_identical_specs():
    expected = _expected_fixture()
    report = compare_scheduler_specs(expected, _live_match_fixture(expected))
    assert report["SCHEDULER_CONFIG_MATCH"] is True
    assert report["drift"] == []
    assert "trigger@20:35" in report["compared_fields"]


def test_compare_flags_missing_safety_closeout_trigger():
    expected = _expected_fixture()
    live = _live_match_fixture(expected)
    live["triggers"] = [t for t in live["triggers"] if t["start"] != "20:35"]
    report = compare_scheduler_specs(expected, live)
    assert report["SCHEDULER_CONFIG_MATCH"] is False
    assert "missing_trigger_at_20:35" in report["drift"]


def test_compare_flags_parallel_multiple_instances():
    expected = _expected_fixture()
    live = _live_match_fixture(expected)
    live["multiple_instances"] = "Parallel"
    report = compare_scheduler_specs(expected, live)
    assert "multiple_instances_mismatch" in report["drift"]
    assert report["SCHEDULER_CONFIG_MATCH"] is False


def test_compare_flags_execution_time_limit_stretch():
    expected = _expected_fixture()
    live = _live_match_fixture(expected)
    live["execution_time_limit"] = "PT1H"
    report = compare_scheduler_specs(expected, live)
    assert "execution_time_limit_mismatch" in report["drift"]


def test_compare_flags_action_execute_change():
    expected = _expected_fixture()
    live = _live_match_fixture(expected)
    live["action_execute"] = "C:\\Windows\\System32\\cmd.exe"
    report = compare_scheduler_specs(expected, live)
    assert "action_execute_mismatch" in report["drift"]


def test_compare_flags_unexpected_extra_trigger():
    expected = _expected_fixture()
    live = _live_match_fixture(expected)
    live["triggers"].append(
        {
            "name": "",
            "start": "21:00",
            "interval": "PT5M",
            "repeat_for": "PT10M",
            "days_of_week": day_names_from_bitmask(WEEKDAY_MASK),
            "days_of_week_bitmask": WEEKDAY_MASK,
            "weeks_interval": 1,
        }
    )
    report = compare_scheduler_specs(expected, live)
    assert "unexpected_trigger_at_21:00" in report["drift"]


def test_compare_flags_days_of_week_lost_friday():
    expected = _expected_fixture()
    live = _live_match_fixture(expected)
    friday_removed_mask = WEEKDAY_MASK & ~bitmask_from_day_names(["Friday"])
    for trigger in live["triggers"]:
        trigger["days_of_week_bitmask"] = friday_removed_mask
        trigger["days_of_week"] = day_names_from_bitmask(friday_removed_mask)
    report = compare_scheduler_specs(expected, live)
    assert report["SCHEDULER_CONFIG_MATCH"] is False
    assert "trigger_window_mismatch_at_09:00" in report["drift"]
    assert "missing_trigger_at_09:00" not in report["drift"]


def test_compare_flags_start_when_available_enabled():
    expected = _expected_fixture()
    live = _live_match_fixture(expected)
    live["start_when_available"] = True
    report = compare_scheduler_specs(expected, live)
    assert "start_when_available_enabled" in report["drift"]
    assert report["SCHEDULER_CONFIG_MATCH"] is False


def test_compare_ignores_empty_principal_logon_type_and_case_in_execute_path():
    expected = _expected_fixture()
    live = _live_match_fixture(expected)
    live["action_execute"] = "c:\\windows\\system32\\windowspowershell\\v1.0\\powershell.exe"
    live["principal_logon_type"] = ""
    report = compare_scheduler_specs(expected, live)
    assert report["drift"] == []
    assert report["SCHEDULER_CONFIG_MATCH"] is True


# ---------------------------------------------------------------------------
# enable_remote_writes: the production schedule is registered with
# -EnableRemoteWrites, so the expected preview must be built with the same
# switch or the comparison reports a permanent action_arguments_mismatch
# false positive.  The preview stays read-only: -Apply is never passed.
# ---------------------------------------------------------------------------


def test_expected_spec_with_enable_remote_writes_carries_the_flag():
    expected = expected_scheduler_spec(_remote_writes_preview_fixture())
    assert "-EnableRemoteWrites" in expected["action_arguments"]
    # The default (shadow) expected spec never carries the flag.
    assert "-EnableRemoteWrites" not in _expected_fixture()["action_arguments"]


def test_compare_reports_no_arguments_mismatch_for_flagged_expected_and_flagged_live():
    expected = expected_scheduler_spec(_remote_writes_preview_fixture())
    live = _live_match_fixture(expected)
    report = compare_scheduler_specs(expected, live)
    assert report["SCHEDULER_CONFIG_MATCH"] is True
    assert "action_arguments_mismatch" not in report["drift"]


def test_compare_keeps_strict_arguments_comparison_across_the_flag_boundary():
    # An expected spec WITHOUT the flag compared against a live task
    # registered WITH the flag must still mismatch (strict semantics: the
    # drift detector never papering over real argument differences).
    expected = _expected_fixture()
    live = _live_match_fixture(expected_scheduler_spec(_remote_writes_preview_fixture()))
    report = compare_scheduler_specs(expected, live)
    assert "action_arguments_mismatch" in report["drift"]
    assert report["SCHEDULER_CONFIG_MATCH"] is False


@pytest.mark.skipif(not POWERSHELL_AVAILABLE, reason="powershell.exe not available")
def test_real_registration_preview_reports_production_plan():
    preview = run_registration_preview(ROOT)
    assert preview is not None
    assert preview["Applied"] is False
    expected = expected_scheduler_spec(preview)
    assert expected["task_name"] == SCHEDULER_TASK_NAME
    assert expected["multiple_instances"] == "IgnoreNew"
    assert expected["execution_time_limit"] == "PT32M"
    assert "run_incremental_price_audit_scheduled.ps1" in expected["action_arguments"]
    assert [trigger["start"] for trigger in expected["triggers"]] == ["09:00", "17:00", "18:00", "20:35"]
    assert [trigger["interval"] for trigger in expected["triggers"]] == ["PT15M", "PT5M", "PT5M", None]
    for trigger in expected["triggers"]:
        assert trigger["days_of_week"] == sorted(WEEKDAYS)
        assert trigger["weeks_interval"] == 1


@pytest.mark.skipif(not POWERSHELL_AVAILABLE, reason="powershell.exe not available")
def test_real_registration_preview_enable_remote_writes_matches_production_registration():
    """The flagged preview mirrors how the production host registered the task.

    Read-only: the script runs in preview mode (``-Apply`` is never passed by
    ``run_registration_preview``); ``-EnableRemoteWrites`` only changes the
    previewed action arguments and the ``RemoteWrites`` marker.
    """
    preview = run_registration_preview(ROOT, enable_remote_writes=True)
    assert preview is not None
    assert preview["Applied"] is False
    assert preview["RemoteWrites"] is True
    expected = expected_scheduler_spec(preview)
    assert expected["action_arguments"].endswith("-EnableRemoteWrites")
    assert expected["action_arguments"].count("-EnableRemoteWrites") == 1
    # Everything else about the production plan is unchanged by the flag.
    shadow_preview = run_registration_preview(ROOT)
    assert shadow_preview is not None
    assert shadow_preview["RemoteWrites"] is False
    shadow_expected = expected_scheduler_spec(shadow_preview)
    assert shadow_expected["action_arguments"] + " -EnableRemoteWrites" == expected["action_arguments"]


@pytest.mark.skipif(
    os.environ.get("CNNC_SCHEDULER_LIVE_DRIFT") != "1" or not POWERSHELL_AVAILABLE,
    reason="set CNNC_SCHEDULER_LIVE_DRIFT=1 (with powershell.exe) for the read-only live comparison",
)
def test_live_machine_comparison_schema():
    live = live_scheduler_spec()
    if live is None:
        pytest.skip("live scheduled task not present on this machine")
    preview = run_registration_preview(ROOT)
    assert preview is not None
    report = compare_scheduler_specs(expected_scheduler_spec(preview), live)
    # Schema only: the live machine may legitimately differ (e.g. it runs the
    # production repo checkout rather than this worktree).
    assert set(report) >= {"SCHEDULER_CONFIG_MATCH", "drift", "compared_fields"}
    assert isinstance(report["SCHEDULER_CONFIG_MATCH"], bool)
    assert isinstance(report["drift"], list)
    assert report["drift"] == sorted(report["drift"])
    assert isinstance(report["compared_fields"], list) and report["compared_fields"]
