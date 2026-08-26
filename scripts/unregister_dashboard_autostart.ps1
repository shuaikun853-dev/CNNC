# CNNC STEP_1 只读看板 - 登录自启卸载脚本。
# 默认预览:只打印将要执行的两条命令,不改系统;显式传 -Apply 才卸载。
# 只操作 CNNC-Dashboard-ReadOnly 一个任务,不读取、不修改现有审计计划任务。
[CmdletBinding()]
param(
    [switch] $Apply
)

$ErrorActionPreference = "Stop"
$taskName = "CNNC-Dashboard-ReadOnly"
$endCommand = "schtasks /End /TN $taskName"
$deleteCommand = "schtasks /Delete /TN $taskName /F"
$preview = [pscustomobject]@{
    TaskName      = $taskName
    Note          = "只读看板服务任务(服务绑定 127.0.0.1:8765);现有审计计划任务不受影响"
    EndCommand    = $endCommand
    DeleteCommand = $deleteCommand
    Apply         = [bool]$Apply
}
$preview | ConvertTo-Json -Depth 3
if (-not $Apply) {
    Write-Host "预览模式:未做任何更改。加 -Apply 才卸载(不会影响现有审计计划任务)。"
    exit 0
}
# 先尝试停止正在运行的实例;任务未在运行时 /End 返回非零属预期,不阻塞删除。
schtasks /End /TN $taskName | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "schtasks /End 未成功(任务可能未在运行),继续删除。"
}
schtasks /Delete /TN $taskName /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "schtasks delete failed: $LASTEXITCODE"
}
Write-Host "已卸载 $taskName(只读服务原绑定 127.0.0.1:8765;现有审计计划任务未受影响)。"
