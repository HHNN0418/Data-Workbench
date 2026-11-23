function Get-CTNTaskWatcher([string]$Root) {
    $ctnTask = Get-ScheduledTask -TaskName 'CTN-AutoStart' -TaskPath '\' -ErrorAction Stop
    if ([string]$ctnTask.State -ne 'Running') { return $null }
    $ctnScheduler = Get-CimInstance Win32_Service -Filter "Name='Schedule'" -ErrorAction Stop
    if (-not $ctnScheduler -or $ctnScheduler.ProcessId -le 0) { return $null }
    $ctnPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $ctnWatcherPath = Join-Path $Root 'Watch-CTN.ps1'
    $ctnMatches = @()
    foreach ($ctnCandidate in @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction Stop)) {
        if (-not [StringComparer]::OrdinalIgnoreCase.Equals($ctnCandidate.ExecutablePath, $ctnPowerShell) -or
            $ctnCandidate.CommandLine -notmatch ('(?:^|\s)-File\s+"' + [regex]::Escape($ctnWatcherPath) + '"(?:\s|$)')) {
            continue
        }
        $ctnCurrent = $ctnCandidate
        for ($ctnDepth = 0; $ctnDepth -lt 5 -and $ctnCurrent; $ctnDepth++) {
            if ($ctnCurrent.ProcessId -eq $ctnScheduler.ProcessId) {
                $ctnMatches += $ctnCandidate
                break
            }
            $ctnChildCreated = $ctnCurrent.CreationDate
            $ctnCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ctnCurrent.ParentProcessId) -ErrorAction Stop
            if ($ctnCurrent -and (-not $ctnChildCreated -or -not $ctnCurrent.CreationDate -or
                    $ctnCurrent.CreationDate -gt $ctnChildCreated)) { break }
        }
    }
    if ($ctnMatches.Count -gt 1) { throw 'Multiple scheduler-owned CTN watchers were found.' }
    if ($ctnMatches.Count -eq 1) { return $ctnMatches[0] }
    return $null
}

function Wait-CTNTaskWatcher([string]$Root, [int]$TimeoutSeconds = 30) {
    $ctnTimer = [Diagnostics.Stopwatch]::StartNew()
    do {
        $ctnWatcher = Get-CTNTaskWatcher -Root $Root
        if ($ctnWatcher) { return $ctnWatcher }
        Start-Sleep -Milliseconds 500
    } while ($ctnTimer.Elapsed.TotalSeconds -lt $TimeoutSeconds)
    throw 'CTN-AutoStart did not start a watcher owned by Windows Task Scheduler.'
}

function ConvertTo-CTNTaskProcessStamp($Value) {
    if (-not $Value) { throw 'Process creation time is unavailable.' }
    return ([datetime]$Value).ToUniversalTime().ToString('o')
}

function Test-CTNTaskAncestor($Process, $Ancestor) {
    if (-not $Process -or -not $Ancestor -or -not $Process.CreationDate -or -not $Ancestor.CreationDate) {
        return $false
    }
    $ctnCurrent = $Process
    for ($ctnDepth = 0; $ctnDepth -lt 8 -and $ctnCurrent; $ctnDepth++) {
        if ($ctnCurrent.ProcessId -eq $Ancestor.ProcessId -and
            $ctnCurrent.CreationDate -eq $Ancestor.CreationDate) { return $true }
        $ctnChildCreated = $ctnCurrent.CreationDate
        $ctnCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ctnCurrent.ParentProcessId) -ErrorAction Stop
        if ($ctnCurrent -and (-not $ctnChildCreated -or -not $ctnCurrent.CreationDate -or
                $ctnCurrent.CreationDate -gt $ctnChildCreated)) { return $false }
    }
    return $false
}

function Get-CTNTaskBackendChain([string]$Root, $Process) {
    if (-not $Process -or -not $Process.CreationDate) { return $null }
    $ctnCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $Process.ProcessId) -ErrorAction Stop
    if (-not $ctnCurrent -or $ctnCurrent.CreationDate -ne $Process.CreationDate) { return $null }
    if ($ctnCurrent.Name -ne 'python.exe' -or
        $ctnCurrent.CommandLine -notmatch '-m\s+ctn_data\s+(?:.*\s)?serve(?:\s|$)') { return $null }
    $ctnBackend = $ctnCurrent
    $ctnPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $ctnStartPath = Join-Path $Root 'Start-CTN.ps1'
    for ($ctnDepth = 0; $ctnDepth -lt 8 -and $ctnCurrent; $ctnDepth++) {
        if ([StringComparer]::OrdinalIgnoreCase.Equals($ctnCurrent.ExecutablePath, $ctnPowerShell) -and
            $ctnCurrent.CommandLine -match ('(?:^|\s)-File\s+"' + [regex]::Escape($ctnStartPath) + '"(?:\s|$)')) {
            return [pscustomobject]@{Backend = $ctnBackend; Launcher = $ctnCurrent}
        }
        $ctnChildCreated = $ctnCurrent.CreationDate
        $ctnCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ctnCurrent.ParentProcessId) -ErrorAction Stop
        if ($ctnCurrent -and (-not $ctnChildCreated -or -not $ctnCurrent.CreationDate -or
                $ctnCurrent.CreationDate -gt $ctnChildCreated)) { return $null }
    }
    return $null
}

function Get-CTNTaskBackendReceipt([string]$Root, $Chain) {
    if (-not $Chain) { return $null }
    $ctnReceiptPath = Join-Path $Root 'data\ctn-task-backend.json'
    try {
        if (-not [IO.File]::Exists($ctnReceiptPath)) { return $null }
        $ctnReceipt = [IO.File]::ReadAllText($ctnReceiptPath) | ConvertFrom-Json -ErrorAction Stop
        $ctnReceiptRoot = [IO.Path]::GetFullPath([string]$ctnReceipt.project_root).TrimEnd('\')
        $ctnExpectedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
        if ($ctnReceipt.schema_version -ne 1 -or $ctnReceipt.task_name -cne 'CTN-AutoStart' -or
            -not [StringComparer]::OrdinalIgnoreCase.Equals($ctnReceiptRoot, $ctnExpectedRoot) -or
            $ctnReceipt.backend_pid -ne $Chain.Backend.ProcessId -or
            $ctnReceipt.launcher_pid -ne $Chain.Launcher.ProcessId -or
            (ConvertTo-CTNTaskProcessStamp $ctnReceipt.backend_created_utc) -cne
                (ConvertTo-CTNTaskProcessStamp $Chain.Backend.CreationDate) -or
            (ConvertTo-CTNTaskProcessStamp $ctnReceipt.launcher_created_utc) -cne
                (ConvertTo-CTNTaskProcessStamp $Chain.Launcher.CreationDate) -or
            $ctnReceipt.origin_watcher_pid -le 0 -or $ctnReceipt.scheduler_pid -le 0 -or
            (ConvertTo-CTNTaskProcessStamp $ctnReceipt.origin_watcher_created_utc) -cgt
                (ConvertTo-CTNTaskProcessStamp $Chain.Launcher.CreationDate) -or
            $Chain.Launcher.ParentProcessId -ne $ctnReceipt.origin_watcher_pid) { return $null }
        return $ctnReceipt
    } catch { return $null }
}

function Get-CTNTaskBackendOwner([string]$Root, $Process) {
    $ctnWatcher = Get-CTNTaskWatcher -Root $Root
    if (-not $ctnWatcher) { return $null }
    $ctnChain = Get-CTNTaskBackendChain -Root $Root -Process $Process
    if (-not $ctnChain) { return $null }
    if (Test-CTNTaskAncestor -Process $ctnChain.Launcher -Ancestor $ctnWatcher) { return $ctnWatcher }
    if (Get-CTNTaskBackendReceipt -Root $Root -Chain $ctnChain) { return $ctnWatcher }
    return $null
}

function Write-CTNTaskBackendReceipt([string]$Root, $OriginWatcher) {
    $ctnWatcher = Get-CTNTaskWatcher -Root $Root
    if (-not $ctnWatcher -or ($OriginWatcher -and
        ($OriginWatcher.ProcessId -ne $ctnWatcher.ProcessId -or
            $OriginWatcher.CreationDate -ne $ctnWatcher.CreationDate))) {
        throw 'The current scheduler-owned watcher could not be verified.'
    }
    $ctnOwners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction Stop |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ctnOwners.Count -ne 1) { throw 'The CTN backend listener is ambiguous.' }
    $ctnBackend = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ctnOwners[0]) -ErrorAction Stop
    $ctnChain = Get-CTNTaskBackendChain -Root $Root -Process $ctnBackend
    if (-not $ctnChain) { throw 'The project backend launcher could not be verified.' }
    $ctnReceipt = Get-CTNTaskBackendReceipt -Root $Root -Chain $ctnChain
    if ($ctnReceipt) { return $true }
    if (-not (Test-CTNTaskAncestor -Process $ctnChain.Launcher -Ancestor $ctnWatcher)) {
        throw 'The backend was not started by the current scheduler-owned watcher.'
    }
    $ctnScheduler = Get-CimInstance Win32_Service -Filter "Name='Schedule'" -ErrorAction Stop
    if (-not $ctnScheduler -or $ctnScheduler.ProcessId -le 0) { throw 'The Schedule service is unavailable.' }
    $ctnRecord = [ordered]@{
        schema_version = 1
        project_root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
        task_name = 'CTN-AutoStart'
        backend_pid = $ctnChain.Backend.ProcessId
        backend_created_utc = ConvertTo-CTNTaskProcessStamp $ctnChain.Backend.CreationDate
        launcher_pid = $ctnChain.Launcher.ProcessId
        launcher_created_utc = ConvertTo-CTNTaskProcessStamp $ctnChain.Launcher.CreationDate
        origin_watcher_pid = $ctnWatcher.ProcessId
        origin_watcher_created_utc = ConvertTo-CTNTaskProcessStamp $ctnWatcher.CreationDate
        scheduler_pid = $ctnScheduler.ProcessId
        recorded_at_utc = [datetime]::UtcNow.ToString('o')
    }
    $ctnReceiptPath = Join-Path $Root 'data\ctn-task-backend.json'
    $ctnTemporaryPath = $ctnReceiptPath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($ctnReceiptPath)) | Out-Null
        [IO.File]::WriteAllText($ctnTemporaryPath, ($ctnRecord | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($ctnReceiptPath)) { [IO.File]::Replace($ctnTemporaryPath, $ctnReceiptPath, $null) }
        else { [IO.File]::Move($ctnTemporaryPath, $ctnReceiptPath) }
    } finally {
        if ([IO.File]::Exists($ctnTemporaryPath)) { [IO.File]::Delete($ctnTemporaryPath) }
    }
    return $true
}
