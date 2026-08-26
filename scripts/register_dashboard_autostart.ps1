# CNNC STEP_1 只读看板 - 登录自启注册脚本。
# 默认预览:只打印将要注册的任务,不改系统;显式传 -Apply 才注册。
# 只注册 CNNC-Dashboard-ReadOnly 一个任务,不读取、不修改现有审计计划任务。
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
$action = "powershell.exe -NoProfile -WindowStyle Hidden -Command `"Set-Location '$RepoRoot'; & '$PythonExe' -m uvicorn app.main:app --host 127.0.0.1 --port $Port`""
$preview = [pscustomobject]@{
    TaskName = $taskName
    Action   = $action
    Trigger  = "登录时(当前用户)"
    Apply    = [bool]$Apply
}
$preview | ConvertTo-Json -Depth 3
if (-not $Apply) {
    Write-Host "预览模式:未做任何更改。加 -Apply 才注册(不会影响现有审计计划任务)。"
    exit 0
}
# PS 5.1 向原生命令传参时不转义内嵌双引号,直接 /TR $action 会在内层引号处被拆断;
# 先转义为 \",schtasks 实际收到的 /TR 值才与 $action(预览 JSON 所示动作)完全一致。
$escapedAction = $action.Replace('"', '\"')
schtasks /Create /TN $taskName /SC ONLOGON /TR $escapedAction /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "schtasks create failed: $LASTEXITCODE"
}
Write-Host "已注册 $taskName(登录自启,只读服务绑定 127.0.0.1:$Port;现有审计计划任务未受影响)。"
