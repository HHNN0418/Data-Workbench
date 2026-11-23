# Double-click the desktop shortcut to start CTN and open its dashboard.
param([switch]$NoOpen)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$ctnUrl = 'http://127.0.0.1:8765/'
$ctnDatabase = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'data\ctn.sqlite3'))
$ctnPostgres = Test-Path -LiteralPath (Join-Path $PSScriptRoot 'data\postgres-active')
$ctnLogDirectory = Join-Path $PSScriptRoot 'reports\generated\startup'
$ctnMutex = $null
$ctnOwnsMutex = $false
. (Join-Path $PSScriptRoot 'scripts\CTN-TaskRuntime.ps1')

function Get-CTNListenerId {
    $ctnOwners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ctnOwners.Count -eq 0) { return $null }
    if ($ctnOwners.Count -ne 1) { throw 'Port 8765 has ambiguous process ownership. No service was started.' }
    return [int]$ctnOwners[0]
}

function Test-CTNProjectProcess([int]$ProcessId) {
    # Windows venv Python can spawn the bundled interpreter. Check its parent
    # chain as well; never display command lines, which may contain secrets.
    $ctnExpectedPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $ctnExpectedScript = Join-Path $PSScriptRoot 'Start-CTN.ps1'
    $ctnCurrentId = $ProcessId
    $ctnChildCreated = $null
    $ctnModuleSeen = $false
    for ($ctnDepth = 0; $ctnDepth -lt 4 -and $ctnCurrentId -gt 0; $ctnDepth++) {
        $ctnProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ctnCurrentId) -ErrorAction Stop
        if (-not $ctnProcess) { return $false }
        if ($ctnChildCreated -and $ctnProcess.CreationDate -gt $ctnChildCreated) { return $false }
        if ($ctnProcess.CommandLine -match '-m\s+ctn_data(?:\s|$)') { $ctnModuleSeen = $true }
        if ($ctnModuleSeen -and (
            [StringComparer]::OrdinalIgnoreCase.Equals($ctnProcess.ExecutablePath, $ctnExpectedPython) -or
            $ctnProcess.CommandLine -match [regex]::Escape($ctnExpectedScript))) { return $true }
        $ctnChildCreated = $ctnProcess.CreationDate
        $ctnCurrentId = [int]$ctnProcess.ParentProcessId
    }
    return $false
}

function Test-CTNReady {
    try {
        $ctnHealth = Invoke-RestMethod -UseBasicParsing -Uri ($ctnUrl + 'api/health') -TimeoutSec 2
    } catch {
        return $false
    }
    if ($ctnHealth.ok -ne $true -or $ctnHealth.backend -ne 'fastapi') {
        throw 'Port 8765 is occupied by another application. Close it and try again.'
    }
    $ctnBootstrap = Invoke-RestMethod -UseBasicParsing -Uri ($ctnUrl + 'api/bootstrap') -TimeoutSec 2
    if ($ctnPostgres) {
        if ($ctnHealth.database -ne 'postgresql' -or $ctnBootstrap.db_path -ne 'PostgreSQL / ctn') {
            throw 'Port 8765 is serving a different CTN database. No service was started.'
        }
        $ctnListenerId = Get-CTNListenerId
        if (-not $ctnListenerId -or -not (Test-CTNProjectProcess $ctnListenerId)) {
            throw 'The running PostgreSQL dashboard cannot be verified as this project. No service was started.'
        }
    } elseif (-not $ctnBootstrap.db_path -or $ctnHealth.database -eq 'postgresql' -or
              -not [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath($ctnBootstrap.db_path), $ctnDatabase)) {
        throw 'Port 8765 is serving a different CTN database. No service was started.'
    }
    return $true
}

try {
    $ctnMutex = New-Object System.Threading.Mutex($false, 'Local\CTN-Launcher-8765')
    try {
        $ctnOwnsMutex = $ctnMutex.WaitOne(60000)
    } catch [System.Threading.AbandonedMutexException] {
        $ctnOwnsMutex = $true
    }
    if (-not $ctnOwnsMutex) { throw 'Another CTN launch is still in progress. Please try again shortly.' }

    if (-not $ctnPostgres -and -not (Test-Path -LiteralPath $ctnDatabase -PathType Leaf)) {
        throw ('Existing CTN database not found: ' + $ctnDatabase)
    }
    $ctnListenerId = Get-CTNListenerId
    if ($ctnListenerId -and -not (Test-CTNProjectProcess $ctnListenerId)) {
        throw 'Port 8765 is occupied by another application. No service was started.'
    }
    & (Join-Path $PSScriptRoot 'Install-Autostart.ps1') -Ensure
    $ctnMutex.ReleaseMutex()
    $ctnOwnsMutex = $false
    $ctnWatcher = Wait-CTNTaskWatcher -Root $PSScriptRoot
    $ctnTimer = [Diagnostics.Stopwatch]::StartNew()
    $ctnReady = $false
    do {
        if (Test-CTNReady) {
            $ctnListener = Get-CimInstance Win32_Process -Filter ('ProcessId=' + (Get-CTNListenerId)) -ErrorAction Stop
            if (-not (Get-CTNTaskBackendOwner -Root $PSScriptRoot -Process $ctnListener)) {
                throw 'CTN is running outside Task Scheduler. Run scripts\Reload-CTN.ps1 to transfer the existing service safely.'
            }
            $ctnReady = $true
            break
        }
        Start-Sleep -Milliseconds 500
    } while ($ctnTimer.Elapsed.TotalSeconds -lt 180)
    if (-not $ctnReady) { throw 'CTN is still recovering. See data\logs\watcher.log and backend startup logs.' }

    if (-not $NoOpen) { Start-Process -FilePath $ctnUrl }
    Write-Output ('CTN ready: ' + $ctnUrl + ' (Windows Task Scheduler)')
} catch {
    $ctnFailure = $_.Exception.Message
    New-Item -ItemType Directory -Path $ctnLogDirectory -Force | Out-Null
    Add-Content -LiteralPath (Join-Path $ctnLogDirectory 'launcher.log') -Encoding UTF8 `
        -Value ((Get-Date -Format 's') + ' ' + $ctnFailure)
    if ($NoOpen) { [Console]::Error.WriteLine($ctnFailure) }
    else {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($ctnFailure, 'CTN startup', 'OK', 'Error') | Out-Null
    }
    exit 1
} finally {
    if ($ctnOwnsMutex) { $ctnMutex.ReleaseMutex() }
    if ($ctnMutex) { $ctnMutex.Dispose() }
}
