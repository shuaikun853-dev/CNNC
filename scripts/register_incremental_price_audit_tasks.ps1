[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $TaskPrefix = "CNNC-PriceAudit-Incremental",
    [string] $RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string] $PythonExe = "python.exe",
    [string] $RuntimeRoot = "",
    [int] $BudgetSeconds = 720,
    [string] $TickAt = "09:00",
    [string] $FinalizeAt = "18:03",
    [switch] $EnableRemoteWrites,
    [switch] $Apply
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $repo "runtime\incremental_price_audit"
}
$scriptPath = Join-Path $repo "scripts\incremental_price_audit.py"
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Incremental CLI not found: $scriptPath"
}
if ($BudgetSeconds -lt 60 -or $BudgetSeconds -gt 780) {
    throw "BudgetSeconds must be between 60 and 780 so the 15-minute schedule retains shutdown headroom."
}

function Quote-CommandArgument([string] $Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$writeFlag = if ($EnableRemoteWrites) { " --enable-remote-writes" } else { " --shadow" }
$common = "--runtime-root $(Quote-CommandArgument $RuntimeRoot) --budget-seconds $BudgetSeconds$writeFlag"
$plans = @(
    [pscustomobject]@{
        TaskName = "$TaskPrefix-Tick"
        Command = $PythonExe
        Arguments = "-u $(Quote-CommandArgument $scriptPath) tick $common"
        StartAt = $TickAt
        RepeatFor = "PT8H45M"
    },
    [pscustomobject]@{
        TaskName = "$TaskPrefix-Finalize"
        Command = $PythonExe
        Arguments = "-u $(Quote-CommandArgument $scriptPath) finalize $common"
        StartAt = $FinalizeAt
        RepeatFor = "PT5H45M"
    }
)

if (-not $Apply) {
    [pscustomobject]@{
        Applied = $false
        Message = "Preview only. Rerun with -Apply after the shadow-day and manual-finalize gates pass."
        MultipleInstances = "IgnoreNew"
        ExecutionTimeLimit = "PT14M"
        RemoteWrites = [bool]$EnableRemoteWrites
        Tasks = $plans
    } | ConvertTo-Json -Depth 5
    exit 0
}

Import-Module ScheduledTasks -ErrorAction Stop
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 14) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited

foreach ($plan in $plans) {
    $at = [datetime]::Today.Add([TimeSpan]::Parse($plan.StartAt))
    $trigger = New-ScheduledTaskTrigger -Daily -At $at
    $trigger.Repetition.Interval = "PT15M"
    $trigger.Repetition.Duration = $plan.RepeatFor
    $action = New-ScheduledTaskAction `
        -Execute $plan.Command `
        -Argument $plan.Arguments `
        -WorkingDirectory $repo
    $definition = New-ScheduledTask `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Finite CNNC/SaaS incremental audit; SQLite and process locks are authoritative."
    if ($PSCmdlet.ShouldProcess($plan.TaskName, "Register or update scheduled task")) {
        Register-ScheduledTask -TaskName $plan.TaskName -InputObject $definition -Force | Out-Null
    }
}

$plans | Select-Object TaskName, StartAt, RepeatFor, Arguments
