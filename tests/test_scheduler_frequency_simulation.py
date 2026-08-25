"""Scheduler-frequency contracts: trigger timeline + IgnoreNew overlap model.

The late-day high-frequency windows (17:00-17:50 full ticks, the 17:55
harvest-only drain, and 18:00-20:00 closeout start opportunities, all every 5
minutes) must raise idle-response speed without ever producing a second
production writer.  These tests derive the real weekday trigger timeline from
the registration script's preview JSON and simulate Task Scheduler's
``MultipleInstances=IgnoreNew`` behaviour for representative run durations.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEEKDAY = datetime(2026, 8, 10)  # Monday


def _run_register_preview() -> dict:
    register = ROOT / "scripts/register_incremental_price_audit_tasks.ps1"
    encoded = str(register).replace("'", "''")
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"& '{encoded}' -EnableRemoteWrites"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return json.loads(completed.stdout.decode("utf-8-sig"))


def _parse_iso_duration(text: str) -> timedelta:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text)
    assert match is not None, f"unsupported repetition duration: {text}"
    hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _parse_clock(text: str) -> timedelta:
    hours, minutes = (int(part) for part in text.split(":"))
    return timedelta(hours=hours, minutes=minutes)


def _weekday_trigger_times(register_plan: dict) -> list[datetime]:
    task = register_plan["Tasks"][0]
    times: list[datetime] = []
    for trigger in task["Triggers"]:
        start = WEEKDAY + _parse_clock(trigger["StartBoundary"])
        if trigger["RepetitionInterval"] is None:
            # A one-shot daily trigger fires exactly once at its boundary.
            times.append(start)
            continue
        interval = _parse_iso_duration(trigger["RepetitionInterval"])
        span = _parse_iso_duration(trigger["RepeatFor"])
        elapsed = timedelta(0)
        while elapsed < span:
            times.append(start + elapsed)
            elapsed += interval
    return sorted(times)


def _simulate_ignore_new(
    trigger_times: list[datetime], duration_for: Callable[[datetime], timedelta]
) -> tuple[list[tuple[datetime, datetime]], list[datetime]]:
    """Model the task-level IgnoreNew policy over one weekday.

    A trigger firing while the previous instance still runs is dropped, not
    queued; the first trigger after the instance exits starts normally.
    """
    started: list[tuple[datetime, datetime]] = []
    skipped: list[datetime] = []
    running_until: datetime | None = None
    for moment in trigger_times:
        if running_until is not None and moment < running_until:
            skipped.append(moment)
            continue
        end = moment + duration_for(moment)
        started.append((moment, end))
        running_until = end
    return started, skipped


def _durations(
    overrides: dict[str, int], default: Callable[[datetime], timedelta]
) -> Callable[[datetime], timedelta]:
    def duration_for(moment: datetime) -> timedelta:
        override = overrides.get(moment.strftime("%H:%M"))
        if override is not None:
            return timedelta(minutes=override)
        return default(moment)

    return duration_for


def _small_default(moment: datetime) -> timedelta:
    # Daytime baseline and un-overridden slots finish well inside one grid
    # step so only the explicitly overridden runs can overlap a trigger.
    return timedelta(seconds=30)


def _as_hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


PEAK_FULL_TICKS = [f"17:{minute:02d}" for minute in range(0, 55, 5)]  # 17:00-17:50
CLOSEOUT_SLOTS = [f"{hour:02d}:{minute:02d}" for hour in (18, 19) for minute in range(0, 60, 5)] + [
    "20:00"
]


def test_trigger_plan_covers_baseline_peak_and_closeout_windows():
    plan = _run_register_preview()
    times = _weekday_trigger_times(plan)
    slots = [_as_hhmm(moment) for moment in times]

    # Daytime baseline is unchanged: 09:00-16:45 on the 15-minute grid.
    assert slots[:32] == [
        f"{hour:02d}:{minute:02d}" for hour in range(9, 17) for minute in (0, 15, 30, 45)
    ]
    # Peak window: every 5 minutes from 17:00 through the 17:55 drain slot.
    assert slots[32:44] == [f"17:{minute:02d}" for minute in range(0, 60, 5)]
    # Closeout window: every 5 minutes from 18:00 through the last 5-minute
    # start at 20:00, then the one-shot 20:35 safety closeout.
    assert slots[44:69] == CLOSEOUT_SLOTS
    assert slots[69:] == ["20:35"]
    assert len(slots) == 70
    assert slots[0] == "09:00" and slots[-1] == "20:35"
    # No trigger fires before 09:00 or after the 20:35 safety closeout.
    assert all("09:00" <= slot <= "20:35" for slot in slots)
    # All triggers remain weekday-only weekly patterns.
    task = plan["Tasks"][0]
    for trigger in task["Triggers"]:
        assert trigger["DaysOfWeek"] == [
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        ]


def test_two_minute_peak_ticks_use_every_five_minute_slot():
    times = _weekday_trigger_times(_run_register_preview())
    started, skipped = _simulate_ignore_new(
        times, _durations({slot: 2 for slot in PEAK_FULL_TICKS}, _small_default)
    )

    peak_starts = [_as_hhmm(moment) for moment, _ in started if _as_hhmm(moment) in PEAK_FULL_TICKS]
    assert peak_starts == PEAK_FULL_TICKS
    # Every batch that lands on a peak boundary is picked up by that very
    # slot; nothing is skipped and no backlog can form.
    assert skipped == []


def test_six_and_nine_minute_peak_ticks_skip_overlaps_without_pileup():
    times = _weekday_trigger_times(_run_register_preview())
    for minutes in (6, 9):
        started, skipped = _simulate_ignore_new(
            times, _durations({slot: minutes for slot in PEAK_FULL_TICKS}, _small_default)
        )
        peak_started = [
            _as_hhmm(moment) for moment, _ in started if _as_hhmm(moment) in PEAK_FULL_TICKS
        ]
        # A run longer than the 5-minute grid naturally skips the slots it
        # overlaps; the next free grid point always starts, so skipped
        # triggers are dropped (never queued) and nothing accumulates.  Both
        # 6- and 9-minute ticks land on a stable every-10-minute cadence.
        assert peak_started == [f"17:{minute:02d}" for minute in range(0, 55, 10)]
        if minutes == 6:
            assert "17:05" in [_as_hhmm(moment) for moment in skipped]
        # Every skip is attributable to a still-running instance: no trigger
        # is skipped while the system is idle.
        running = [end for _, end in started]
        for moment in skipped:
            assert any(start < moment < end for start, end in started)
        # Once a run finishes, the next start is at most one grid step away;
        # the one deliberate exception is the gap from the last 5-minute
        # start (20:00) to the one-shot 20:35 safety closeout.
        for (start, end), (next_start, _next_end) in zip(started, started[1:]):
            if _as_hhmm(next_start) == "20:35" and _as_hhmm(start) != "20:35":
                continue
            assert next_start - end <= timedelta(minutes=15)
        assert all(end - start >= timedelta(minutes=minutes) for start, end in started if _as_hhmm(start) in PEAK_FULL_TICKS)


def test_nine_minute_1730_tick_drops_1735_and_1740_starts():
    # Section 9 Case 1: the 17:30 full tick runs 9 minutes; the 17:35 trigger
    # cannot create a second production writer, and after the 17:39 exit the
    # 17:40 trigger starts a new tick normally.
    times = _weekday_trigger_times(_run_register_preview())
    started, skipped = _simulate_ignore_new(times, _durations({"17:30": 9}, _small_default))

    slots_started = [_as_hhmm(moment) for moment, _ in started]
    assert "17:30" in slots_started and "17:40" in slots_started
    assert "17:35" in [_as_hhmm(moment) for moment in skipped]
    seventeen_thirty = next(end for moment, end in started if _as_hhmm(moment) == "17:30")
    assert seventeen_thirty.strftime("%H:%M") == "17:39"


def test_1750_tick_blocks_1755_drain_but_not_the_1800_closeout():
    # Section 9 Case 2: a 17:50 full tick still running at 17:55 drops that
    # harvest-only trigger via IgnoreNew; once it exits, the 18:00 closeout
    # starts on time.
    times = _weekday_trigger_times(_run_register_preview())
    started, skipped = _simulate_ignore_new(times, _durations({"17:50": 9}, _small_default))

    slots_started = [_as_hhmm(moment) for moment, _ in started]
    assert "17:55" in [_as_hhmm(moment) for moment in skipped]
    assert "18:00" in slots_started
    seventeen_fifty = next(end for moment, end in started if _as_hhmm(moment) == "17:50")
    assert seventeen_fifty.strftime("%H:%M") == "17:59"


def test_twelve_minute_closeout_drops_two_triggers_and_1815_restarts():
    # Section 9 Case 3: an 18:00 closeout running 12 minutes drops the 18:05
    # and 18:10 triggers; the 18:15 slot starts the next closeout.
    times = _weekday_trigger_times(_run_register_preview())
    started, skipped = _simulate_ignore_new(times, _durations({"18:00": 12}, _small_default))

    skipped_slots = [_as_hhmm(moment) for moment in skipped]
    assert skipped_slots == ["18:05", "18:10"]
    slots_started = [_as_hhmm(moment) for moment, _ in started]
    assert "18:15" in slots_started


def test_twenty_minute_closeout_restarts_within_one_grid_step():
    # Section 10: a 20-minute closeout started at 18:00 drops 18:05/18:10/
    # 18:15; the moment it exits, the next 5-minute opportunity (18:20)
    # starts a fresh closeout.  High frequency raises idle response speed,
    # not concurrency.
    times = _weekday_trigger_times(_run_register_preview())
    started, skipped = _simulate_ignore_new(times, _durations({"18:00": 20}, _small_default))

    skipped_slots = [_as_hhmm(moment) for moment in skipped]
    assert skipped_slots == ["18:05", "18:10", "18:15"]
    slots_started = [_as_hhmm(moment) for moment, _ in started]
    assert "18:20" in slots_started


def test_safety_closeout_starts_after_long_1955_run_dropped_2000():
    # Safety Case A: the 19:55 closeout runs its full 28-minute wall budget,
    # so the 20:00 trigger is dropped by IgnoreNew and the run ends at 20:23.
    # The 5-minute window has no further start, yet the day still returns
    # WAITING_REMOTE — the one-shot 20:35 safety closeout must start.
    times = _weekday_trigger_times(_run_register_preview())
    started, skipped = _simulate_ignore_new(times, _durations({"19:55": 28}, _small_default))

    skipped_slots = [_as_hhmm(moment) for moment in skipped]
    assert "20:00" in skipped_slots
    nineteen_fifty_five = next(
        end for moment, end in started if _as_hhmm(moment) == "19:55"
    )
    assert nineteen_fifty_five.strftime("%H:%M") == "20:23"
    slots_started = [_as_hhmm(moment) for moment, _ in started]
    assert "20:35" in slots_started
    # Between 20:23 and 20:35 nothing else can start, and after 20:35 there
    # is no further automatic start.
    assert all(slot <= "20:00" or slot == "20:35" for slot in slots_started)


def test_safety_closeout_starts_even_after_full_limit_2000_run():
    # Safety Case B: a 20:00 closeout may occupy at most the PT32M
    # ExecutionTimeLimit, so it is gone by 20:32 at the latest; the 20:35
    # safety trigger always finds the task idle and starts.
    times = _weekday_trigger_times(_run_register_preview())
    started, _skipped = _simulate_ignore_new(times, _durations({"20:00": 32}, _small_default))

    twenty_o_clock = next(end for moment, end in started if _as_hhmm(moment) == "20:00")
    assert twenty_o_clock.strftime("%H:%M") == "20:32"
    slots_started = [_as_hhmm(moment) for moment, _ in started]
    assert "20:35" in slots_started


def test_never_more_than_one_production_writer_in_any_scenario():
    # Section 12: across fast and slow scenarios, running intervals never
    # overlap.  Task-level IgnoreNew therefore keeps concurrent
    # production writers at <= 1, so the higher frequency cannot by itself
    # create lock conflicts, submit_unknown growth, remote_manual growth or
    # stale leases — those business invariants are owned by the unchanged
    # engine layers and their existing suites.
    times = _weekday_trigger_times(_run_register_preview())
    scenarios = (
        {slot: 2 for slot in PEAK_FULL_TICKS},   # fast ticks
        {slot: 6 for slot in PEAK_FULL_TICKS},   # medium ticks
        {slot: 9 for slot in PEAK_FULL_TICKS},   # slow ticks
        {"17:50": 9},                             # peak drain collision
        {"18:00": 12},                            # closeout overlap
        {"18:00": 20},                            # long closeout
        {"18:00": 28},                            # closeout at full budget
        {"19:55": 28},                            # safety Case A
        {"20:00": 32},                            # safety Case B
    )
    for overrides in scenarios:
        started, _skipped = _simulate_ignore_new(
            times, _durations(overrides, _small_default)
        )
        for (_start, end), (next_start, _next_end) in zip(started, started[1:]):
            assert next_start >= end


def test_five_minute_batches_wait_at_most_one_slot_under_fast_ticks():
    # Section 12 throughput: with new SaaS batches landing every 5 minutes
    # during the peak and full ticks finishing in 2 minutes, each batch is
    # discovered by the slot it arrived on — at most ~5 minutes of wait for
    # discovery plus the tick itself, versus up to 15 minutes of extra wait
    # under the old quarter-hour peak grid.
    times = _weekday_trigger_times(_run_register_preview())
    started, _skipped = _simulate_ignore_new(
        times, _durations({slot: 2 for slot in PEAK_FULL_TICKS}, _small_default)
    )
    tick_starts = [
        moment for moment, _ in started if _as_hhmm(moment) in PEAK_FULL_TICKS
    ]
    for index in range(len(PEAK_FULL_TICKS)):
        arrival = WEEKDAY + _parse_clock(PEAK_FULL_TICKS[index])
        pickup = next(moment for moment in tick_starts if moment >= arrival)
        assert pickup - arrival <= timedelta(minutes=5)
