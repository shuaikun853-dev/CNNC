# query_scheduled_task_json.ps1 - READ-ONLY scheduled task state probe.
# Emits one-line JSON (ConvertTo-Json -Compress) for a task name, or "null"
# when the task is not registered. Never registers/unregisters anything.
# ASCII-only on purpose: PS 5.1 reads BOM-less files as ANSI on this host.
param([Parameter(Mandatory=$true)][string]$TaskName)

$ErrorActionPreference = "Stop"
# UTF-8 stdout: PS 5.1 redirected output defaults to the OEM codepage (GBK on
# this host), which would mojibake CJK task arguments (e.g. -RepoRoot paths)
# for the Python caller that decodes with encoding=utf-8.
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        "null"
        exit 0
    }
    $info = $task | Get-ScheduledTaskInfo
    $a = $task.Actions | Select-Object -First 1
    $lastRun = $null
    if ($info.LastRunTime) { $lastRun = $info.LastRunTime.ToString("yyyy-MM-ddTHH:mm:ss") }
    $nextRun = $null
    if ($info.NextRunTime) { $nextRun = $info.NextRunTime.ToString("yyyy-MM-ddTHH:mm:ss") }
    $definitionHash = $null
    try {
        $definitionXml = Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $hashAlgorithm = [System.Security.Cryptography.SHA256]::Create()
        try {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$definitionXml)
            $definitionHash = ([System.BitConverter]::ToString($hashAlgorithm.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
        } finally { $hashAlgorithm.Dispose() }
    } catch { $definitionHash = $null }
    $obj = [pscustomobject]@{
        task_name        = [string]$task.TaskName
        definition_sha256 = $definitionHash
        state            = [string]$task.State
        last_run_time    = $lastRun
        next_run_time    = $nextRun
        last_task_result = [int]$info.LastTaskResult
        action           = [string]$a.Execute
        arguments        = [string]$a.Arguments
    }
    $obj | ConvertTo-Json -Compress
    exit 0
} catch {
    # Keep the payload JSON-safe and ASCII-safe: strip quotes, backslashes and
    # any non-printable-ASCII char from the message, then hand-build the JSON.
    $msg = [string]$_.Exception.Message
    $safe = $msg -replace '"', "'" -replace '\\', '/' -replace '[^\x20-\x7E]', '?'
    ('{"error":"' + $safe + '"}')
    exit 0
}
