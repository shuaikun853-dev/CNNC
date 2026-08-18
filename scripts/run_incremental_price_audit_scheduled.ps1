[CmdletBinding()]
param(
    [string] $RepoRoot = "",
    [string] $PythonExe = "python.exe",
    [string] $RuntimeRoot = "",
    [ValidateRange(1, 3000)]
    [int] $BudgetSeconds = 720,
    [ValidateRange(1, 3500)]
    [int] $CloseoutBudgetSeconds = 1680,
    [ValidateRange(2, 59)]
    [int] $HardTimeoutMinutes = 32,
    [ValidateRange(0, 120)]
    [int] $ProcessExitGraceSeconds = 30,
    [ValidateSet("legacy")]
    [string] $ZhaoshangBackend = "legacy",
    [switch] $EnableRemoteWrites,
    [Nullable[datetime]] $At = $null,
    [switch] $Preview
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$repo = (Resolve-Path -LiteralPath $RepoRoot).Path
if (-not $RuntimeRoot) {
    $RuntimeRoot = Join-Path $repo "runtime\incremental_price_audit"
}
$cliPath = Join-Path $repo "scripts\incremental_price_audit.py"
if (-not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
    throw "Incremental CLI not found: $cliPath"
}
if (-not $Preview -and $null -ne $At) {
    throw "-At is only allowed with -Preview. Production routing always uses the current local time."
}
$headroomSeconds = ($HardTimeoutMinutes * 60) - $BudgetSeconds
if ($headroomSeconds -lt 60) {
    throw "HardTimeoutMinutes must leave at least 60 seconds beyond BudgetSeconds."
}
$closeoutHeadroomSeconds = ($HardTimeoutMinutes * 60) - $CloseoutBudgetSeconds
if ($closeoutHeadroomSeconds -lt 90) {
    throw "HardTimeoutMinutes must leave at least 90 seconds beyond CloseoutBudgetSeconds for checkpoint, final.xlsx materialization, manifest, and exit."
}

$now = if ($null -ne $At) { [datetime]$At } else { Get-Date }
$hour = $now.Hour
$minute = $now.Minute
$phases = @()
$isWeekday = $now.DayOfWeek -notin @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)
if ($isWeekday -and $minute -eq 0 -and $hour -ge 9 -and $hour -le 17) {
    $phases = @("tick")
}
elseif ($isWeekday -and $hour -eq 17 -and $minute -in @(15, 30, 45)) {
    $phases = @("tick")
}
elseif (
    $isWeekday -and (
        ($hour -ge 18 -and $hour -le 19 -and ($minute % 15) -eq 0) -or
        ($hour -eq 20 -and $minute -eq 0)
    )
) {
    # Closeout owns the production writer until FINALIZED, its wall deadline,
    # or a manual blocker; overlapping 15-minute wakeups are dropped by the
    # task's IgnoreNew policy, and a fresh closeout continues the durable
    # SQLite state when the previous one already exited.  20:00 is the last
    # automatic closeout; 20:15 and later never auto-run, so post-20:00
    # progress is an explicit manual closeout from the operator.
    $phases = @("closeout")
}

$modeArgument = if ($EnableRemoteWrites) { "--enable-remote-writes" } else { "--shadow" }
function Get-PhaseBudgetSeconds([string] $Phase) {
    if ($Phase -eq "closeout") {
        return [int][math]::Min($CloseoutBudgetSeconds, $HardTimeoutMinutes * 60 - 90)
    }
    return [int][math]::Min($BudgetSeconds, $HardTimeoutMinutes * 60 - 60)
}
function Get-PhaseArguments([string] $Phase, [int] $PhaseBudgetSeconds) {
    $arguments = @(
        "-u",
        $cliPath,
        $Phase,
        "--runtime-root", $RuntimeRoot,
        "--budget-seconds", [string]$PhaseBudgetSeconds
    )
    if ($Phase -ne "drain-only") {
        $arguments += $modeArgument
    }
    $arguments += @("--invocation", "scheduler")
    $arguments += @("--zhaoshang-backend", $ZhaoshangBackend)
    return $arguments
}

if ($Preview) {
    $commands = @(
        foreach ($phase in $phases) {
            [pscustomobject]@{
                Phase = $phase
                Command = $PythonExe
                Arguments = (Get-PhaseArguments $phase (Get-PhaseBudgetSeconds $phase))
            }
        }
    )
    [pscustomobject]@{
        Preview = $true
        At = $now.ToString("yyyy-MM-ddTHH:mm:ss")
        Hour = $hour
        DayOfWeek = [string]$now.DayOfWeek
        IsWeekday = $isWeekday
        Phases = @($phases)
        Commands = $commands
        RuntimeRoot = $RuntimeRoot
        RemoteWrites = [bool]$EnableRemoteWrites
        BudgetSeconds = $BudgetSeconds
        CloseoutBudgetSeconds = $CloseoutBudgetSeconds
        HardTimeoutMinutes = $HardTimeoutMinutes
        LogPath = (Join-Path $RuntimeRoot ("scheduler_logs\{0}.log" -f $now.ToString("yyyy-MM-dd")))
    } | ConvertTo-Json -Depth 6
    exit 0
}

if ($phases.Count -eq 0) {
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
$logDirectory = Join-Path $RuntimeRoot "scheduler_logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory ("{0}.log" -f $now.ToString("yyyy-MM-dd"))
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
$shutdownReserveSeconds = 60

function Write-SchedulerOutput([string] $Message) {
    $logWriter.WriteLine($Message)
}

function Write-SchedulerLog([string] $Message) {
    $line = "{0} {1}" -f (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK"), $Message
    Write-SchedulerOutput $line
}

function Invoke-Phase([string] $Phase, [int] $PhaseBudgetSeconds) {
    $arguments = Get-PhaseArguments $Phase $PhaseBudgetSeconds
    $phaseMode = if ($Phase -eq "drain-only") { "drain-only" } else { $modeArgument }
    Write-SchedulerLog "START phase=$Phase budget_seconds=$PhaseBudgetSeconds mode=$phaseMode backend=$ZhaoshangBackend"
    $remainingSeconds = [math]::Floor(
        $hardLimitSeconds - $stopwatch.Elapsed.TotalSeconds - $shutdownReserveSeconds
    )
    $childDeadlineSeconds = [math]::Min(
        $remainingSeconds,
        $PhaseBudgetSeconds + $ProcessExitGraceSeconds
    )
    if ($childDeadlineSeconds -lt 1) {
        Write-SchedulerLog "TIMEOUT phase=$Phase reason=no_internal_deadline_budget"
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
            Write-SchedulerLog "TIMEOUT phase=$Phase deadline_seconds=$childDeadlineSeconds"
        }
        elseif ($result.WriterFailed) {
            try {
                Write-SchedulerLog "LOG_FAILURE phase=$Phase diagnostic=$($result.Diagnostic)"
            }
            catch {
                return 74
            }
        }
    }
    Write-SchedulerLog "END phase=$Phase exit_code=$exitCode elapsed_seconds=$([math]::Round($stopwatch.Elapsed.TotalSeconds, 1))"
    return [int]$exitCode
}

try {
    Write-SchedulerLog "RUN at=$($now.ToString('yyyy-MM-ddTHH:mm:ssK')) phases=$($phases -join ',')"
    foreach ($phase in $phases) {
        $remainingSeconds = [math]::Floor($hardLimitSeconds - $stopwatch.Elapsed.TotalSeconds - $shutdownReserveSeconds)
        if ($remainingSeconds -lt 1) {
            Write-SchedulerLog "SKIP phase=$phase reason=insufficient_wall_clock_budget remaining_seconds=$remainingSeconds"
            break
        }
        $phaseBudget = [math]::Min((Get-PhaseBudgetSeconds $phase), $remainingSeconds)
        $exitCode = Invoke-Phase $phase $phaseBudget
        if ($exitCode -ne 0) {
            Write-SchedulerLog "STOP reason=phase_failed phase=$phase exit_code=$exitCode"
            exit $exitCode
        }
    }
    Write-SchedulerLog "DONE elapsed_seconds=$([math]::Round($stopwatch.Elapsed.TotalSeconds, 1))"
}
finally {
    $logWriter.Dispose()
    $logStream.Dispose()
}
exit 0
