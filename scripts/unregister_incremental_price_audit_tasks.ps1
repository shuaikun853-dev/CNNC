[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string] $TaskName = "CNNC-PriceAudit-Incremental",
    [switch] $Apply
)

$ErrorActionPreference = "Stop"
if (-not $Apply) {
    [pscustomobject]@{
        Applied = $false
        Message = "Preview only. Rerun with -Apply to disable incremental scheduling; durable history is retained."
        TaskNames = @($TaskName)
    } | ConvertTo-Json -Depth 3
    exit 0
}

Import-Module ScheduledTasks -ErrorAction Stop
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task -and $PSCmdlet.ShouldProcess($TaskName, "Unregister scheduled task")) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
