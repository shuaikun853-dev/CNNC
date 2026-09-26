# Called only after the child exited. Never infers business counters after a kill.
function Ensure-CnncDispatchSummary {
    param(
        [string] $Path, [string] $InvocationId, [string] $BusinessDate,
        [int] $ExitCode, [double] $DurationSeconds
    )
    $absolute = [System.IO.Path]::GetFullPath($Path)
    if ($absolute -notmatch '^E:\\' -or $InvocationId -notmatch '^[A-Za-z0-9_-]+$') {
        throw 'Dispatch summary requires an E-drive path and a safe invocation id.'
    }
    $encoding = [System.Text.UTF8Encoding]::new($false, $true)
    $stream = $null
    for ($attempt = 0; $attempt -lt 10 -and $null -eq $stream; $attempt++) {
        try {
            $stream = [System.IO.File]::Open($absolute, [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        }
        catch [System.IO.IOException] {
            if ($attempt -eq 9) { throw }
            Start-Sleep -Milliseconds 100
        }
    }
    try {
        if ($stream.Length -gt [int]::MaxValue) { throw 'Dispatch summary is too large to inspect safely.' }
        $bytes = [byte[]]::new([int]$stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($read -le 0) { throw 'Incomplete dispatch summary read.' }
            $offset += $read
        }
        $prefixLength = $bytes.Length
        while ($prefixLength -gt 0 -and $bytes[$prefixLength - 1] -ne 10) { $prefixLength-- }
        $prefix = $encoding.GetString($bytes, 0, $prefixLength)
        $tailLength = $bytes.Length - $prefixLength
        $tail = ''
        $partial = $false
        if ($tailLength -gt 0) {
            try {
                $tail = $encoding.GetString($bytes, $prefixLength, $tailLength)
                $null = ConvertFrom-Json -InputObject $tail -ErrorAction Stop
            }
            catch { $partial = $true }
        }
        $found = $false
        foreach ($line in (($prefix + $(if ($partial) { '' } else { $tail })) -split "`n")) {
            if (-not $line.Trim()) { continue }
            try { $record = ConvertFrom-Json -InputObject $line -ErrorAction Stop }
            catch { continue }
            if ($record.invocation_id -eq $InvocationId) { $found = $true }
        }
        if ($partial) {
            # Persist the exact interrupted byte fragment before truncating it.
            $fragment = [System.IO.File]::Open(
                "$absolute.partial_$InvocationId.txt", [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
            try {
                $fragment.Write($bytes, $prefixLength, $tailLength)
                $fragment.Flush($true)
            }
            finally { $fragment.Dispose() }
            $stream.SetLength($prefixLength)
        }
        $stream.Position = $stream.Length
        if ($tailLength -gt 0 -and -not $partial) {
            $stream.WriteByte(10)
        }
        if (-not $found) {
            $outcome = if ($ExitCode -eq 124) { 'DISPATCH_TIMEOUT' } else { 'DISPATCH_SUMMARY_MISSING' }
            $record = [ordered]@{
                timestamp = [DateTimeOffset]::Now.ToString('o')
                invocation_id = $InvocationId
                business_date = $BusinessDate
                outcome = $outcome
                counters = @{}
                errors = @('CHILD_SUMMARY_MISSING')
                duration = $DurationSeconds
                exit_code = $ExitCode
                scheduler_fallback = $true
            }
            $line = (ConvertTo-Json -InputObject $record -Compress -Depth 8) + "`n"
            $encoded = $encoding.GetBytes($line)
            $stream.Write($encoded, 0, $encoded.Length)
        }
        $stream.Flush($true)
    }
    finally { $stream.Dispose() }
}
