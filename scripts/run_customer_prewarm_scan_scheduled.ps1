# CNNC Issue #9 customer prewarm scan - scheduler runner (one-shot sidecar).
# Invokes scripts/customer_prewarm_scan.py once under a hard timeout via the
# shared [CnncScheduledProcessHost] job-object host (scripts/scheduled_process_host.cs).
# The scanner shares the STEP_1 runtime root (state db + global detail.lock live
# there) but keeps its own logs under runtime\customer_prewarm\scheduler_logs.
# ASCII-only per project convention; English comments only.
# Registration is NOT performed by Issue #9: register_customer_prewarm_scan_tasks.ps1
# must be applied manually only after explicit user authorization.
[CmdletBinding()]
param(
    [string] $RepoRoot = "",
    [string] $PythonExe = "python.exe",
    [string] $RuntimeRoot = "",
    [ValidateRange(10, 300)]
    [int] $BudgetSeconds = 50,
    [ValidateRange(1, 10)]
    [int] $HardTimeoutMinutes = 2,
    [ValidateRange(0, 120)]
    [int] $ProcessExitGraceSeconds = 15,
    [switch] $DryRun,
    [switch] $Preview
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
if (-not $RuntimeRoot) {
    # Same runtime root as STEP_1: the scanner reads the per-day state db and
    # serializes read-only detail calls on the global detail.lock.
    $RuntimeRoot = Join-Path $repo "runtime\incremental_price_audit"
}
$cliPath = Join-Path $repo "scripts\customer_prewarm_scan.py"
if (-not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
    throw "Customer prewarm CLI not found: $cliPath"
}
$headroomSeconds = ($HardTimeoutMinutes * 60) - $BudgetSeconds
if ($headroomSeconds -lt 60) {
    throw "HardTimeoutMinutes must leave at least 60 seconds beyond BudgetSeconds."
}

# Business date in Asia/Shanghai (UTC+8, no DST) mirrors business_date_in_shanghai().
$businessDate = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
    [DateTime]::UtcNow,
    "China Standard Time"
).ToString("yyyy-MM-dd")

$arguments = @(
    "-u",
    $cliPath,
    "--business-date", $businessDate,
    "--runtime-root", $RuntimeRoot,
    "--budget-seconds", [string]$BudgetSeconds
)
if ($DryRun) {
    $arguments += "--dry-run"
}

if ($Preview) {
    [pscustomobject]@{
        Preview = $true
        BusinessDate = $businessDate
        Command = $PythonExe
        Arguments = $arguments
        RuntimeRoot = $RuntimeRoot
        DryRun = [bool]$DryRun
        BudgetSeconds = $BudgetSeconds
        HardTimeoutMinutes = $HardTimeoutMinutes
        LogPath = (Join-Path $repo "runtime\customer_prewarm\scheduler_logs\$businessDate.log")
    } | ConvertTo-Json -Depth 4
    exit 0
}

$pythonPath = (Get-Command $PythonExe -ErrorAction Stop).Source
$processHostPath = Join-Path $PSScriptRoot "scheduled_process_host.cs"
if (-not (Test-Path -LiteralPath $processHostPath -PathType Leaf)) {
    throw "Scheduled process host not found: $processHostPath"
}
if ($null -eq ("CnncScheduledProcessHost" -as [type])) {
    Add-Type -Path $processHostPath | Out-Null
}
$logDirectory = Join-Path $repo "runtime\customer_prewarm\scheduler_logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory ("{0}.log" -f $businessDate)
$logStream = [System.IO.File]::Open(
    $logPath,
    [System.IO.FileMode]::Append,
    [System.IO.FileAccess]::Write,
    [System.IO.FileShare]::ReadWrite
)
$logWriter = [System.IO.StreamWriter]::new(
    $logStream,
    [System.Text.UTF8Encoding]::new($false)
)
$logWriter.AutoFlush = $true
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$hardLimitSeconds = $HardTimeoutMinutes * 60
$shutdownReserveSeconds = 30

function Write-ScanLog([string] $Message) {
    $line = "{0} {1}" -f (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK"), $Message
    $logWriter.WriteLine($line)
}

try {
    Write-ScanLog "START business_date=$businessDate budget_seconds=$BudgetSeconds dry_run=$([bool]$DryRun)"
    $remainingSeconds = [math]::Floor(
        $hardLimitSeconds - $stopwatch.Elapsed.TotalSeconds - $shutdownReserveSeconds
    )
    $childDeadlineSeconds = [math]::Min($remainingSeconds, $BudgetSeconds + $ProcessExitGraceSeconds)
    if ($childDeadlineSeconds -lt 1) {
        Write-ScanLog "TIMEOUT reason=no_internal_deadline_budget"
        $exitCode = 124
    }
    else {
        $timeoutMilliseconds = [int][math]::Min(
            [int]::MaxValue,
            [math]::Ceiling($childDeadlineSeconds * 1000)
        )
        $result = [CnncScheduledProcessHost]::Run(
            $pythonPath,
            [string[]]$arguments,
            $repo,
            $logWriter,
            $timeoutMilliseconds
        )
        $exitCode = [int]$result.ExitCode
        if ($result.TimedOut) {
            Write-ScanLog "TIMEOUT deadline_seconds=$childDeadlineSeconds"
        }
        elseif ($result.WriterFailed) {
            Write-ScanLog "LOG_FAILURE diagnostic=$($result.Diagnostic)"
        }
    }
    Write-ScanLog "END exit_code=$exitCode elapsed_seconds=$([math]::Round($stopwatch.Elapsed.TotalSeconds, 1))"
}
finally {
    $logWriter.Dispose()
    $logStream.Dispose()
}
exit $exitCode
