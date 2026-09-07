# CNNC Issue #9 customer prewarm scan - Task Scheduler registration script.
# REGISTRATION IS NOT PERFORMED BY ISSUE #9. Run manually only after explicit
# user authorization. Default is preview-only (no system change); pass -Apply
# to register the single task described below.
#
# One task, one trigger: a one-shot start that repeats every 1 minute
# indefinitely (sidecar cadence; the scan's own 50s budget, the
# customer_prewarm.lock non-blocking process lock and IgnoreNew together
# guarantee no overlap and no queueing). Mirrors the production conventions of
# register_incremental_price_audit_tasks.ps1: Hidden + NonInteractive action,
# Interactive/Limited principal, AllowStartIfOnBatteries/DontStopIfGoingOnBatteries.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $TaskName = "CNNC-CustomerPrewarm-Scan",
    [string] $RepoRoot = "",
    [string] $PythonExe = "python.exe",
    [string] $RuntimeRoot = "",
    [ValidateRange(10, 300)]
    [int] $BudgetSeconds = 50,
    [ValidateRange(2, 15)]
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
$runnerPath = Join-Path $repo "scripts\run_customer_prewarm_scan_scheduled.ps1"
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
    "-HardTimeoutMinutes", $ExecutionTimeLimitMinutes
)
$actionArguments = $runnerArguments -join " "

# Repeat every 1 minute indefinitely: -Once trigger with a repetition interval
# and no repetition duration means "repeat forever" on Windows 10+.
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ([datetime]::Now.AddSeconds(30)) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
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
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "CNNC Issue #9 customer prewarm scan: one bounded read-only sidecar pass per minute; overlap prevented by IgnoreNew plus the customer_prewarm.lock process lock."

$plan = [pscustomobject]@{
    TaskName = $TaskName
    Command = $powerShellPath
    Arguments = $actionArguments
    WorkingDirectory = $repo
    PythonExe = $pythonPath
    RuntimeRoot = $RuntimeRoot
    TriggerType = "Once, repeat every 1 minute indefinitely"
    RepetitionInterval = "PT1M"
    MultipleInstances = "IgnoreNew"
    ExecutionTimeLimit = "PT${ExecutionTimeLimitMinutes}M"
    BudgetSeconds = $BudgetSeconds
    DryRun = $false
}

if (-not $Apply) {
    [pscustomobject]@{
        Applied = $false
        Message = "Preview only. Issue #9 does NOT register this task; rerun with -Apply only after explicit user authorization."
        MultipleInstances = "IgnoreNew"
        ExecutionTimeLimit = "PT${ExecutionTimeLimitMinutes}M"
        BudgetSeconds = $BudgetSeconds
        Tasks = @($plan)
    } | ConvertTo-Json -Depth 5
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, "Register or update scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $definition -Force | Out-Null
}

$plan
