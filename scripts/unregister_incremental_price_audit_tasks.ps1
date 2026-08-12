[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $TaskPrefix = "CNNC-PriceAudit-Incremental",
    [switch] $Apply
)

$ErrorActionPreference = "Stop"
$taskNames = @("$TaskPrefix-Tick", "$TaskPrefix-Finalize")
if (-not $Apply) {
    [pscustomobject]@{
        Applied = $false
        Message = "Preview only. Rerun with -Apply to disable incremental scheduling; durable history is retained."
        TaskNames = $taskNames
    } | ConvertTo-Json -Depth 3
    exit 0
}

Import-Module ScheduledTasks -ErrorAction Stop
foreach ($taskName in $taskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task -and $PSCmdlet.ShouldProcess($taskName, "Unregister scheduled task")) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
}
