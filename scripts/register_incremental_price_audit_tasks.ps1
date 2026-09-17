[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $TaskName = "CNNC-PriceAudit-Incremental",
    [string] $RepoRoot = "",
    [string] $PythonExe = "python.exe",
    [string] $RuntimeRoot = "",
    [ValidateRange(60, 3000)]
    [int] $BudgetSeconds = 720,
    [ValidateRange(60, 3500)]
    [int] $CloseoutBudgetSeconds = 1680,
    [ValidateRange(2, 59)]
    [int] $ExecutionTimeLimitMinutes = 32,
    [string] $StartAt = "09:00",
    [switch] $EnableRemoteWrites,
    [switch] $Apply
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $repo "runtime\incremental_price_audit"
}
$runnerPath = Join-Path $repo "scripts\run_incremental_price_audit_scheduled.ps1"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Scheduled runner not found: $runnerPath"
}
$pythonCommand = Get-Command $PythonExe -ErrorAction Stop
$pythonPath = $pythonCommand.Source
$powerShellPath = (Get-Command "powershell.exe" -ErrorAction Stop).Source
Import-Module ScheduledTasks -ErrorAction Stop
$headroomSeconds = ($ExecutionTimeLimitMinutes * 60) - $BudgetSeconds
if ($headroomSeconds -lt 60) {
    throw "ExecutionTimeLimitMinutes must leave at least 60 seconds beyond BudgetSeconds."
}
$closeoutHeadroomSeconds = ($ExecutionTimeLimitMinutes * 60) - $CloseoutBudgetSeconds
if ($closeoutHeadroomSeconds -lt 90) {
    throw "ExecutionTimeLimitMinutes must leave at least 90 seconds beyond CloseoutBudgetSeconds for checkpoint, final.xlsx materialization, manifest, and exit."
}

function Quote-CommandArgument([string] $Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$runnerArguments = @(
    # Hidden + NonInteractive keeps the scheduled top-level PowerShell off the
    # user's desktop: closing a visible console window took down the whole
    # kill-on-close job object (and with it the Python tick) on 2026-08-28.
    # The principal stays Interactive/Limited: S4U would need re-validated
    # network/cookie/profile access first.
    "-NoProfile",
    "-NonInteractive",
    "-WindowStyle", "Hidden",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-CommandArgument $runnerPath),
    "-RepoRoot", (Quote-CommandArgument $repo),
    "-PythonExe", (Quote-CommandArgument $pythonPath),
    "-RuntimeRoot", (Quote-CommandArgument $RuntimeRoot),
    "-BudgetSeconds", $BudgetSeconds,
    "-CloseoutBudgetSeconds", $CloseoutBudgetSeconds,
    "-HardTimeoutMinutes", $ExecutionTimeLimitMinutes,
    "-ZhaoshangBackend", "legacy"
)
if ($EnableRemoteWrites) {
    $runnerArguments += "-EnableRemoteWrites"
}
$actionArguments = $runnerArguments -join " "
$legacyTaskNames = @("$TaskName-Tick", "$TaskName-Finalize")
$weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

function ConvertTo-Iso8601Duration([TimeSpan] $Span) {
    $totalMinutes = [int][math]::Ceiling($Span.TotalMinutes)
    $hours = [int][math]::Floor($totalMinutes / 60)
    $minutes = $totalMinutes - ($hours * 60)
    if ($hours -eq 0) {
        return "PT" + $minutes + "M"
    }
    if ($minutes -eq 0) {
        return "PT" + $hours + "H"
    }
    return "PT" + $hours + "H" + $minutes + "M"
}

function New-WeeklyRepetitionTrigger([datetime] $StartAt, [string] $Interval, [string] $Duration) {
    $weekly = New-ScheduledTaskTrigger `
        -Weekly `
        -WeeksInterval 1 `
        -DaysOfWeek $weekdays `
        -At $StartAt
    $repetition = New-CimInstance `
        -ClassName MSFT_TaskRepetitionPattern `
        -Namespace Root/Microsoft/Windows/TaskScheduler `
        -ClientOnly `
        -Property @{
            Interval = $Interval
            Duration = $Duration
            StopAtDurationEnd = $false
        }
    $weekly.CimInstanceProperties["Repetition"].Value = $repetition
    return $weekly
}

# One task, four weekday-only triggers.  Trigger A keeps the daytime 15-minute
# baseline and ends before the peak; Trigger B wakes every 3 minutes through
# 17:00-17:57 and every wake is a full tick with fallback eligibility;
# Trigger C offers a closeout start every 5 minutes from
# 18:00 through 20:00.  Trigger D is a one-shot 20:35 safety closeout: a 19:55
# run can hold the writer past the dropped 20:00 trigger, and the PT32M
# ExecutionTimeLimit guarantees any 20:00 run is gone by 20:32, so 20:35 is
# the last automatic start of the day.  All triggers share one action, one
# working directory, one process lock, and IgnoreNew, so overlaps are dropped
# rather than queued or parallelized.
$triggerSpecs = @(
    [pscustomobject]@{ Name = "Baseline"; Interval = "PT15M"; At = $StartAt; LastStart = "16:45" }
    [pscustomobject]@{ Name = "Peak"; Interval = "PT3M"; At = "17:00"; LastStart = "17:57" }
    [pscustomobject]@{ Name = "Closeout"; Interval = "PT5M"; At = "18:00"; LastStart = "20:00" }
    [pscustomobject]@{ Name = "SafetyCloseout"; Interval = $null; At = "20:35"; LastStart = "20:35" }
)
$triggerPlans = @()
foreach ($spec in $triggerSpecs) {
    # Loop-local names deliberately avoid `$StartAt`: it collides
    # case-insensitively with the typed [string] script parameter above,
    # which would silently coerce these values back to strings.
    $windowStart = [datetime]::Today.Add([TimeSpan]::Parse($spec.At))
    $windowLastStart = [datetime]::Today.Add([TimeSpan]::Parse($spec.LastStart))
    if ($windowLastStart -lt $windowStart) {
        throw "Trigger $($spec.Name): start $($spec.At) must be at or before its last planned start $($spec.LastStart)."
    }
    if ($null -eq $spec.Interval) {
        # One-shot daily trigger: fires exactly once at its boundary.
        $triggerPlans += [pscustomobject]@{
            Name = $spec.Name
            DaysOfWeek = $weekdays
            StartBoundary = $windowStart.ToString("HH:mm")
            RepetitionInterval = $null
            RepeatFor = $null
            LastPlannedStart = $spec.LastStart
        }
        continue
    }
    $window = $windowLastStart - $windowStart
    $repeatFor = ConvertTo-Iso8601Duration $window.Add([TimeSpan]::FromMinutes(1))
    $triggerPlans += [pscustomobject]@{
        Name = $spec.Name
        DaysOfWeek = $weekdays
        StartBoundary = $windowStart.ToString("HH:mm")
        RepetitionInterval = $spec.Interval
        RepeatFor = $repeatFor
        LastPlannedStart = $spec.LastStart
    }
}
$cimTriggers = @(
    foreach ($triggerPlan in $triggerPlans) {
        $triggerStart = [datetime]::Today.Add([TimeSpan]::Parse($triggerPlan.StartBoundary))
        if ($null -eq $triggerPlan.RepetitionInterval) {
            New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $weekdays -At $triggerStart
        }
        else {
            New-WeeklyRepetitionTrigger $triggerStart $triggerPlan.RepetitionInterval $triggerPlan.RepeatFor
        }
    }
)
$plan = [pscustomobject]@{
    TaskName = $TaskName
    Command = $powerShellPath
    Arguments = $actionArguments
    WorkingDirectory = $repo
    PythonExe = $pythonPath
    RuntimeRoot = $RuntimeRoot
    TriggerType = "Weekly"
    DaysOfWeek = $weekdays
    WeeksInterval = 1
    StartBoundary = $triggerPlans[0].StartBoundary
    LastPlannedStart = $triggerPlans[-1].LastPlannedStart
    Triggers = $triggerPlans
    TickTimes = "$($triggerPlans[0].StartBoundary)-16:00 hourly; 17:00-17:57 every 3 minutes"
    HarvestOnlyTimes = "09:15-16:45 quarter-hour slots (sweep existing Zhaoshang remote jobs only; no SaaS discovery, Detail, Model, submit, or finalize)"
    CloseoutHours = "18:00-20:00 every 5 minutes plus a one-shot 20:35 safety closeout (wall budget $CloseoutBudgetSeconds s; IgnoreNew drops overlaps; 20:35 is the last automatic start)"
    FinalizeHours = "none after 20:35; operators run an explicit manual closeout instead"
}

if (-not $Apply) {
    [pscustomobject]@{
        Applied = $false
        Message = "Preview only. Rerun with -Apply only after reviewing this exact production schedule."
        MultipleInstances = "IgnoreNew"
        ExecutionTimeLimit = "PT${ExecutionTimeLimitMinutes}M"
        BudgetSeconds = $BudgetSeconds
        CloseoutBudgetSeconds = $CloseoutBudgetSeconds
        RemoteWrites = [bool]$EnableRemoteWrites
        ConflictingTaskNames = $legacyTaskNames
        Tasks = @($plan)
    } | ConvertTo-Json -Depth 6
    exit 0
}

$legacyTasks = @(
    $legacyTaskNames |
        ForEach-Object { Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue } |
        Where-Object { $null -ne $_ }
)
if ($legacyTasks.Count -gt 0) {
    $names = ($legacyTasks | ForEach-Object TaskName) -join ", "
    throw "Legacy scheduler tasks still exist ($names). Unregister them explicitly before applying the single-task schedule."
}

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionTimeLimitMinutes) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$action = New-ScheduledTaskAction `
    -Execute $plan.Command `
    -Argument $plan.Arguments `
    -WorkingDirectory $repo
$definition = New-ScheduledTask `
    -Action $action `
    -Trigger $cimTriggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Finite CNNC/SaaS incremental audit; 15-minute daytime and 5-minute late-day wakeups with runner time routing, SQLite state, and process locks are authoritative."
if ($PSCmdlet.ShouldProcess($TaskName, "Register or update scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $definition -Force | Out-Null
}

$plan
