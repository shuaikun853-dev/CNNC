# CNNC customer prewarm dispatch - scheduler runner (one-shot sidecar).
# Invokes scripts/customer_prewarm_dispatch.py once under a hard timeout via the
# shared [CnncScheduledProcessHost] job-object host (scripts/scheduled_process_host.cs).
# Division of labor with the scan sidecar: run_customer_prewarm_scan_scheduled.ps1
# drives discovery/refresh (scan); this runner drives remote job creation (dispatch).
# Logs plus one per-pass summary JSON live under
# runtime\customer_prewarm\dispatch_scheduler_logs.
# Exit codes: 0 also covers expected runtime states (LOCK_BUSY,
# SKIPPED_ALREADY_RUNNING); 2 = configuration error or DISPATCH_FAILED.
# ASCII-only per project convention; English comments only.
# Registration is NOT performed here: register_customer_prewarm_dispatch_task.ps1
# must be applied manually only after explicit user authorization.
[CmdletBinding()]
param(
    [string] $RepoRoot = "",
    [string] $PythonExe = "python.exe",
    [string] $RuntimeRoot = "",
    [ValidateRange(30, 300)]
    [int] $BudgetSeconds = 240,
    [ValidateRange(1, 50)]
    [int] $MaxJobsPerPass = 16,
    [ValidateRange(2, 10)]
    [int] $HardTimeoutMinutes = 5,
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
    # Same runtime root as STEP_1: the dispatch CLI reads the per-day state db
    # and honors the same global detail.lock serialization as the scanner.
    $RuntimeRoot = Join-Path $repo "runtime\incremental_price_audit"
}
$cliPath = Join-Path $repo "scripts\customer_prewarm_dispatch.py"
if (-not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
    throw "Customer prewarm dispatch CLI not found: $cliPath"
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

$dispatchLogDirectory = Join-Path $repo "runtime\customer_prewarm\dispatch_scheduler_logs"
# Per-pass summary JSON goes next to the text log; the timestamp is captured
# from Get-Date at start so each pass writes a unique file.
$jsonOutputPath = Join-Path $dispatchLogDirectory (
    "summary_{0}T{1}.json" -f $businessDate, (Get-Date).ToString("HHmmss")
)

$arguments = @(
    "-u",
    $cliPath,
    "--business-date", $businessDate,
    "--runtime-root", $RuntimeRoot,
    "--budget-seconds", [string]$BudgetSeconds,
    "--max-jobs-per-pass", [string]$MaxJobsPerPass,
    "--json-output", $jsonOutputPath
)
if ($DryRun) {
    $arguments += "--plan-only"
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
        MaxJobsPerPass = $MaxJobsPerPass
        HardTimeoutMinutes = $HardTimeoutMinutes
        LogPath = (Join-Path $dispatchLogDirectory ("{0}.log" -f $businessDate))
        JsonOutputDir = $dispatchLogDirectory
        JsonOutputPath = $jsonOutputPath
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
New-Item -ItemType Directory -Path $dispatchLogDirectory -Force | Out-Null
$logPath = Join-Path $dispatchLogDirectory ("{0}.log" -f $businessDate)
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

function Write-DispatchLog([string] $Message) {
    $line = "{0} {1}" -f (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK"), $Message
    $logWriter.WriteLine($line)
}

try {
    Write-DispatchLog "START business_date=$businessDate budget_seconds=$BudgetSeconds max_jobs_per_pass=$MaxJobsPerPass dry_run=$([bool]$DryRun)"
    $remainingSeconds = [math]::Floor(
        $hardLimitSeconds - $stopwatch.Elapsed.TotalSeconds - $shutdownReserveSeconds
    )
    $childDeadlineSeconds = [math]::Min($remainingSeconds, $BudgetSeconds + $ProcessExitGraceSeconds)
    if ($childDeadlineSeconds -lt 1) {
        Write-DispatchLog "TIMEOUT reason=no_internal_deadline_budget"
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
            Write-DispatchLog "TIMEOUT deadline_seconds=$childDeadlineSeconds"
        }
        elseif ($result.WriterFailed) {
            Write-DispatchLog "LOG_FAILURE diagnostic=$($result.Diagnostic)"
        }
    }
    Write-DispatchLog "END exit_code=$exitCode elapsed_seconds=$([math]::Round($stopwatch.Elapsed.TotalSeconds, 1))"
}
finally {
    $logWriter.Dispose()
    $logStream.Dispose()
}
exit $exitCode
