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
    "-NoProfile",
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
$at = [datetime]::Today.Add([TimeSpan]::Parse($StartAt))
$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek $weekdays `
    -At $at
$repetition = New-CimInstance `
    -ClassName MSFT_TaskRepetitionPattern `
    -Namespace Root/Microsoft/Windows/TaskScheduler `
    -ClientOnly `
    -Property @{
        Interval = "PT15M"
        Duration = "PT11H1M"
        StopAtDurationEnd = $false
    }
$trigger.CimInstanceProperties["Repetition"].Value = $repetition
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
    StartBoundary = $at.ToString("HH:mm")
    RepetitionInterval = "PT15M"
    RepeatFor = "PT11H1M"
    LastPlannedStart = "20:00"
    TickTimes = "09:00-17:00 hourly; 17:15; 17:30; 17:45"
    HarvestOnlyTimes = "09:15-16:45 quarter-hour slots (sweep existing Zhaoshang remote jobs only; no SaaS discovery, Detail, Model, submit, or finalize)"
    CloseoutHours = "18:00-20:00 every 15 minutes (wall budget $CloseoutBudgetSeconds s; IgnoreNew drops overlaps; 20:00 is the last automatic start)"
    FinalizeHours = "none after 20:00; operators run an explicit manual closeout instead"
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
    } | ConvertTo-Json -Depth 5
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
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Finite CNNC/SaaS incremental audit; 15-minute wakeups with runner time routing, SQLite state, and process locks are authoritative."
if ($PSCmdlet.ShouldProcess($TaskName, "Register or update scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $definition -Force | Out-Null
}

$plan
