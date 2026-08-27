# CNNC STEP_1 只读看板 - 登录自启注册脚本(Reliability Fix 2026-08-27;Final Quote Fix 同日)。
# 默认预览:只打印将要注册的任务,不改系统;显式传 -Apply 才注册。
# 只注册 CNNC-Dashboard-ReadOnly 一个任务,不读取、不修改现有审计计划任务。
# 注册走 ScheduledTasks 模块的注册 cmdlet,以显式控制长期可用性:
#   - ExecutionTimeLimit = 0(默认 72 小时会杀死长驻服务)
#   - 电池供电允许启动且不中断(默认双禁)
#   - MultipleInstances = IgnoreNew(登录多次不叠加实例)
#   - 当 Task Scheduler 将该任务判定为失败时,最多自动重启 3 次、每次间隔 1 分钟。
# Arguments 用 PowerShell 单引号格式串构造真实双引号(不含字面 \"),
# 工作目录由 -WorkingDirectory 显式固定,载荷不再重复切换工作目录。
[CmdletBinding()]
param(
    [string] $RepoRoot = "",
    [string] $PythonExe = "E:\Python314\python.exe",
    [int] $Port = 8765,
    [switch] $Apply
)

$ErrorActionPreference = "Stop"
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$taskName = "CNNC-Dashboard-ReadOnly"

$payload = "& '$PythonExe' -m uvicorn app.main:app --host 127.0.0.1 --port $Port"
$arguments = '-NoProfile -WindowStyle Hidden -Command "{0}"' -f $payload
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
# 本机 help 未提供注册 cmdlet 默认 Principal 的语义描述,不做未证假设:
# 显式注册为当前用户 Interactive 令牌(无密码、无 S4U),RunLevel 默认 Limited。
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

$preview = [ordered]@{
    "TaskName"                   = $taskName
    "Executable"                 = $action.Execute
    "Arguments"                  = $action.Arguments
    "WorkingDirectory"           = $action.WorkingDirectory
    "Trigger"                    = "登录时(当前用户)"
    "Principal"                  = "$($principal.UserId) / $($principal.LogonType) / $($principal.RunLevel)"
    "ExecutionTimeLimit"         = "PT0S(无限)"
    "AllowStartIfOnBatteries"    = $true
    "DontStopIfGoingOnBatteries" = $true
    "MultipleInstances"          = "IgnoreNew"
    "RestartCount"               = 3
    "RestartInterval"            = "PT1M(1 分钟)"
    "Apply"                      = [bool]$Apply
}
$preview | ConvertTo-Json
if (-not $Apply) {
    Write-Host "预览模式:未做任何更改。加 -Apply 才注册(不会影响现有审计计划任务)。"
    exit 0
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "已注册 $taskName(登录自启;无执行时限;电池可启;判定失败时自动重启 3 次/间隔 1 分钟;只读服务绑定 127.0.0.1:$Port;现有审计计划任务未受影响)。"
