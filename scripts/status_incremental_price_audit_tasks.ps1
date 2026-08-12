[CmdletBinding()]
param(
    [string] $TaskPrefix = "CNNC-PriceAudit-Incremental"
)

$ErrorActionPreference = "Stop"
Import-Module ScheduledTasks -ErrorAction Stop
foreach ($taskName in @("$TaskPrefix-Tick", "$TaskPrefix-Finalize")) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        [pscustomobject]@{
            TaskName = $taskName
            Installed = $false
            State = "Missing"
            LastRunTime = $null
            LastTaskResult = $null
            NextRunTime = $null
        }
        continue
    }
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    [pscustomobject]@{
        TaskName = $taskName
        Installed = $true
        State = [string]$task.State
        LastRunTime = $info.LastRunTime
        LastTaskResult = $info.LastTaskResult
        NextRunTime = $info.NextRunTime
    }
}
