[CmdletBinding()]
param(
    [string] $TaskName = "CNNC-PriceAudit-Incremental"
)

$ErrorActionPreference = "Stop"
Import-Module ScheduledTasks -ErrorAction Stop

function ConvertFrom-DayOfWeekMask([object] $Value) {
    if ($null -eq $Value) {
        return @()
    }
    $mask = [int]$Value
    $days = [ordered]@{
        Monday = 2
        Tuesday = 4
        Wednesday = 8
        Thursday = 16
        Friday = 32
        Saturday = 64
        Sunday = 1
    }
    return @($days.GetEnumerator() | Where-Object { ($mask -band $_.Value) -ne 0 } | ForEach-Object Key)
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    [pscustomobject]@{
        TaskName = $TaskName
        Installed = $false
        State = "Missing"
        LastRunTime = $null
        LastTaskResult = $null
        NextRunTime = $null
        MultipleInstances = $null
        ExecutionTimeLimit = $null
        TriggerType = $null
        DaysOfWeek = @()
        WeeksInterval = $null
        StartBoundary = $null
        RepetitionInterval = $null
        RepetitionDuration = $null
        TriggerCount = 0
        Triggers = @()
        StartWhenAvailable = $null
        ConflictingLegacyTasks = @(
            "$TaskName-Tick", "$TaskName-Finalize" |
                Where-Object { $null -ne (Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue) }
        )
    }
    exit 0
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName
$triggerSummaries = @(
    foreach ($trigger in $task.Triggers) {
        $localStart = [datetime]::Parse([string]$trigger.StartBoundary).ToLocalTime()
        [pscustomobject]@{
            TriggerType = [string]$trigger.CimClass.CimClassName
            DaysOfWeek = @(ConvertFrom-DayOfWeekMask $trigger.DaysOfWeek)
            WeeksInterval = [int]$trigger.WeeksInterval
            StartBoundary = $localStart.ToString("yyyy-MM-ddTHH:mm:ssK")
            StartTime = $localStart.ToString("HH:mm")
            RepetitionInterval = [string]$trigger.Repetition.Interval
            RepetitionDuration = [string]$trigger.Repetition.Duration
        }
    }
)
$first = $triggerSummaries[0]
[pscustomobject]@{
    TaskName = $TaskName
    Installed = $true
    State = [string]$task.State
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    NextRunTime = $info.NextRunTime
    MultipleInstances = [string]$task.Settings.MultipleInstances
    ExecutionTimeLimit = [string]$task.Settings.ExecutionTimeLimit
    TriggerType = "Weekly"
    TriggerCount = $triggerSummaries.Count
    DaysOfWeek = $first.DaysOfWeek
    WeeksInterval = $first.WeeksInterval
    StartBoundary = $first.StartBoundary
    StartTime = $first.StartTime
    RepetitionInterval = $first.RepetitionInterval
    RepetitionDuration = $first.RepetitionDuration
    Triggers = $triggerSummaries
    StartWhenAvailable = [bool]$task.Settings.StartWhenAvailable
    Action = [string]$task.Actions[0].Execute
    Arguments = [string]$task.Actions[0].Arguments
    WorkingDirectory = [string]$task.Actions[0].WorkingDirectory
    ConflictingLegacyTasks = @(
        "$TaskName-Tick", "$TaskName-Finalize" |
            Where-Object { $null -ne (Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue) }
    )
}
