# Process-local only. Call after Preview exits, before Add-Type or any child.
param([string] $RepoRoot, [string] $RuntimeRoot)

foreach ($destination in @($RepoRoot, $RuntimeRoot)) {
    $absoluteDestination = [System.IO.Path]::GetFullPath($destination)
    if (-not [System.IO.Path]::IsPathRooted($destination) -or $absoluteDestination -notmatch '^E:\\') {
        throw "Scheduler writable paths must be absolute E-drive paths."
    }
}
$env:TEMP = 'E:\CodexData\temp\cnnc-scheduler'
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = 'E:\CodexData\cache\python-pyc\cnnc\scheduler'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PIP_CACHE_DIR = 'E:\CodexData\package-cache\pip'
$env:npm_config_cache = 'E:\CodexData\package-cache\npm'
$env:CNNC_AUTH_DATA_DIR = Join-Path ([System.IO.Path]::GetFullPath($RepoRoot)) 'data'
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
