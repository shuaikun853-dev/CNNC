# CNNC customer prewarm dispatch - Task Scheduler registration script.
# REGISTRATION IS NOT PERFORMED BY DEFAULT. Run manually only after explicit
# user authorization. Default is preview-only (no system change); pass -Apply
# to register the single task described below.
#
# One task, two DAILY triggers:
#   - Trigger A starts 00:05 and repeats every 30 minutes for 16h55m
#     (00:05..17:00 window, last fire 16:35) - daytime cadence.
#   - Trigger B starts 17:05 and repeats every 5 minutes for 6h40m
#     (17:05..23:45 window, last fire 23:45) - post-17:00 catch-up cadence.
# Overlap is prevented by IgnoreNew plus the customer_prewarm.lock process
# lock; passes also back off against the PriceAudit tick through the STEP_1
# day lock. Mirrors the production conventions of
# register_customer_prewarm_scan_tasks.ps1 and
# register_incremental_price_audit_tasks.ps1: Hidden + NonInteractive action,
# Interactive/Limited principal, AllowStartIfOnBatteries/DontStopIfGoingOnBatteries.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $TaskName = "CNNC-CustomerPrewarm-Dispatch",
    [string] $RepoRoot = "",
    [string] $PythonExe = "python.exe",
    [string] $RuntimeRoot = "",
    [ValidateRange(30, 300)]
    [int] $BudgetSeconds = 240,
    [ValidateRange(1, 50)]
    [int] $MaxJobsPerPass = 16,
    [ValidateRange(2, 10)]
    [int] $ExecutionTimeLimitMinutes = 5,
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
$runnerPath = Join-Path $repo "scripts\run_customer_prewarm_dispatch_scheduled.ps1"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Scheduled runner not found: $runnerPath"
}
if (($ExecutionTimeLimitMinutes * 60) - ($BudgetSeconds + 60) -lt 0) {
    throw "ExecutionTimeLimitMinutes must leave at least 60 seconds beyond BudgetSeconds."
}
$pythonCommand = Get-Command $PythonExe -ErrorAction Stop
$pythonPath = $pythonCommand.Source
$powerShellPath = (Get-Command "powershell.exe" -ErrorAction Stop).Source
Import-Module ScheduledTasks -ErrorAction Stop

function Quote-CommandArgument([string] $Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$runnerArguments = @(
    # Hidden + NonInteractive keeps the scheduled top-level PowerShell off the
    # user's desktop (same rationale as the STEP_1 registration script).
    "-NoProfile",
    "-NonInteractive",
    "-WindowStyle", "Hidden",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-CommandArgument $runnerPath),
    "-RepoRoot", (Quote-CommandArgument $repo),
    "-PythonExe", (Quote-CommandArgument $pythonPath),
    "-RuntimeRoot", (Quote-CommandArgument $RuntimeRoot),
    "-BudgetSeconds", $BudgetSeconds,
    "-HardTimeoutMinutes", $ExecutionTimeLimitMinutes,
    "-MaxJobsPerPass", $MaxJobsPerPass
)
$actionArguments = $runnerArguments -join " "

# PS 5.1's New-ScheduledTaskTrigger -Daily cannot attach a repetition pattern
# directly. Verified workaround: build a -Once trigger carrying the repetition
# interval/duration, then copy its .Repetition onto the -Daily trigger.
# Anchor times use concrete today/tomorrow datetimes so StartBoundary carries
# the local timezone.
$now = [datetime]::Now
$dayStart = $now.Date.AddMinutes(5)                   # 00:05
if ($dayStart -le $now) { $dayStart = $dayStart.AddDays(1) }
$eveningStart = $now.Date.AddHours(17).AddMinutes(5)  # 17:05
if ($eveningStart -le $now) { $eveningStart = $eveningStart.AddDays(1) }

$dayInterval = New-TimeSpan -Minutes 30
$dayDuration = New-TimeSpan -Hours 16 -Minutes 55     # 00:05 -> 17:00, last fire 16:35
$eveningInterval = New-TimeSpan -Minutes 5
$eveningDuration = New-TimeSpan -Hours 6 -Minutes 40  # 17:05 -> 23:45, last fire 23:45

$dayOnceTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At $dayStart `
    -RepetitionInterval $dayInterval `
    -RepetitionDuration $dayDuration
$dayTrigger = New-ScheduledTaskTrigger -Daily -At $dayStart
$dayTrigger.Repetition = $dayOnceTrigger.Repetition

$eveningOnceTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At $eveningStart `
    -RepetitionInterval $eveningInterval `
    -RepetitionDuration $eveningDuration
$eveningTrigger = New-ScheduledTaskTrigger -Daily -At $eveningStart
$eveningTrigger.Repetition = $eveningOnceTrigger.Repetition

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionTimeLimitMinutes) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
# Same principal convention as STEP_1: current user, Interactive, Limited
# (S4U would need re-validated network/cookie/profile access first).
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$action = New-ScheduledTaskAction `
    -Execute $powerShellPath `
    -Argument $actionArguments `
    -WorkingDirectory $repo
$definition = New-ScheduledTask `
    -Action $action `
    -Trigger @($dayTrigger, $eveningTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description "CNNC customer prewarm dispatch: bounded remote job-create passes every 30 minutes by day and every 5 minutes after 17:00; overlap prevented by IgnoreNew plus the customer_prewarm.lock process lock; backs off against the PriceAudit tick via the STEP_1 day lock."

$plan = [pscustomobject]@{
    TaskName = $TaskName
    Command = $powerShellPath
    Arguments = $actionArguments
    WorkingDirectory = $repo
    PythonExe = $pythonPath
    RuntimeRoot = $RuntimeRoot
    TriggerType = "Two daily triggers: 00:05 every 30 min for 16h55m; 17:05 every 5 min for 6h40m"
    Triggers = @(
        [pscustomobject]@{
            TriggerName = "Daytime30Minutes"
            # StartBoundary comes back from CIM as a string; cast before formatting.
            StartBoundary = ([datetime]$dayTrigger.StartBoundary).ToString("yyyy-MM-ddTHH:mm:ss")
            RepetitionInterval = "PT30M"
            RepetitionDuration = "PT16H55M"
        },
        [pscustomobject]@{
            TriggerName = "Evening5Minutes"
            StartBoundary = ([datetime]$eveningTrigger.StartBoundary).ToString("yyyy-MM-ddTHH:mm:ss")
            RepetitionInterval = "PT5M"
            RepetitionDuration = "PT6H40M"
        }
    )
    MultipleInstances = "IgnoreNew"
    ExecutionTimeLimit = "PT${ExecutionTimeLimitMinutes}M"
    BudgetSeconds = $BudgetSeconds
    MaxJobsPerPass = $MaxJobsPerPass
    DryRun = $false
}

if (-not $Apply) {
    [pscustomobject]@{
        Applied = $false
        Message = "Preview only. This script does NOT register the task by default; rerun with -Apply only after explicit user authorization."
        MultipleInstances = "IgnoreNew"
        ExecutionTimeLimit = "PT${ExecutionTimeLimitMinutes}M"
        BudgetSeconds = $BudgetSeconds
        MaxJobsPerPass = $MaxJobsPerPass
        Tasks = @($plan)
    } | ConvertTo-Json -Depth 5
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, "Register or update scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $definition -Force | Out-Null
}

$plan
