param(
    [switch]$SimulateWatcherCrash,
    [ValidateRange(30, 180)][int]$RecoveryTimeoutSeconds = 120,
    [guid]$MaintenanceId = [guid]::NewGuid()
)
$ErrorActionPreference = 'Stop'
$ctnRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'CTN-TaskRuntime.ps1')
$ctnPython = Join-Path $ctnRoot '.venv\Scripts\python.exe'
$ctnSecretFile = Join-Path $ctnRoot 'data\secrets\postgres_url'
$ctnMaintenanceId = $MaintenanceId.ToString()
$ctnMaintenanceStarted = $false
$ctnWatcherStopped = $false
$ctnStage = 'verify_supervision'
$ctnRecord = [ordered]@{started_at=(Get-Date -Format o);result='starting';simulated_watcher_crash=[bool]$SimulateWatcherCrash}

function Get-CTNSupervisionSnapshot {
    $ctnWatcher = Get-CTNTaskWatcher -Root $ctnRoot
    if (-not $ctnWatcher) { throw 'A scheduler-owned CTN watcher is required.' }
    $ctnOwners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction Stop |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ctnOwners.Count -ne 1) { throw 'The CTN listener is missing or ambiguous.' }
    $ctnBackend = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ctnOwners[0]) -ErrorAction Stop
    if (-not (Get-CTNTaskBackendOwner -Root $ctnRoot -Process $ctnBackend)) {
        throw 'The backend is not verified as independently supervised.'
    }
    $ctnHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 8
    if ($ctnHealth.ok -ne $true -or $ctnHealth.backend -ne 'fastapi' -or $ctnHealth.database -ne 'postgresql') {
        throw 'The CTN health check did not pass.'
    }
    return [pscustomobject]@{watcher_pid=$ctnWatcher.ProcessId;watcher_created=$ctnWatcher.CreationDate;
        watcher_parent_pid=$ctnWatcher.ParentProcessId;backend_pid=$ctnBackend.ProcessId;
        backend_created=$ctnBackend.CreationDate;health_ok=$true}
}

function Invoke-CTNRecoveryMaintenance([string]$Action) {
    $ctnArguments = @('-X','utf8','-m','ctn_data.agent_maintenance','--secret-file',$ctnSecretFile,
        $Action,'--id',$ctnMaintenanceId,'--timeout','300')
    $ctnOutput = @(& $ctnPython @ctnArguments 2>$null)
    $ctnExit = $LASTEXITCODE
    $ctnState = ($ctnOutput -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
    if ($ctnExit -ne 0 -or $ctnState.ok -ne $true -or $ctnState.id -cne $ctnMaintenanceId) {
        throw ('Recovery maintenance ' + $Action + ' was not confirmed.')
    }
    return $ctnState
}

function Assert-CTNRecoveryDrained($State) {
    if ($State.active -ne $true -or $State.phase -ne 'drained' -or $State.drained -ne $true -or
        $State.active_generations -ne 0 -or $State.active_operations -ne 0 -or
        $State.sending_actions -ne 0 -or $State.registering_accounts -ne 0 -or $State.active_research -ne 0) {
        throw 'Existing work must be drained before testing recovery.'
    }
}

try {
    $ctnBefore = Get-CTNSupervisionSnapshot
    $ctnRecord.before = $ctnBefore
    $ctnOrigin = Get-CTNTaskWatcher -Root $ctnRoot
    if (-not (Write-CTNTaskBackendReceipt -Root $ctnRoot -OriginWatcher $ctnOrigin)) {
        throw 'The independent backend receipt was not confirmed.'
    }
    if ($SimulateWatcherCrash) {
        $ctnStage = 'drain_existing_work'
        $ctnMaintenanceStarted = $true
        $null = Invoke-CTNRecoveryMaintenance 'begin'
        Assert-CTNRecoveryDrained (Invoke-CTNRecoveryMaintenance 'wait')
        $ctnBefore = Get-CTNSupervisionSnapshot
        $ctnRecord.before = $ctnBefore
        Assert-CTNRecoveryDrained (Invoke-CTNRecoveryMaintenance 'wait')
        $ctnLatest = Get-CTNTaskWatcher -Root $ctnRoot
        if (-not $ctnLatest -or $ctnLatest.ProcessId -ne $ctnBefore.watcher_pid -or
            $ctnLatest.CreationDate -ne $ctnBefore.watcher_created) { throw 'The watcher changed before the recovery test.' }
        $ctnStage = 'simulate_watcher_crash'
        $ctnRecoveryTimer = [Diagnostics.Stopwatch]::StartNew()
        $ctnWatcherStopped = $true
        Stop-Process -Id $ctnLatest.ProcessId -ErrorAction Stop
        $ctnStage = 'wait_for_automatic_recovery'
        $ctnRecovered = $false
        do {
            try {
                $ctnAfter = Get-CTNSupervisionSnapshot
                if ($ctnAfter.watcher_pid -ne $ctnBefore.watcher_pid -or
                    $ctnAfter.watcher_created -ne $ctnBefore.watcher_created) {
                    $ctnRecovered = $true
                    break
                }
            } catch {}
            Start-Sleep -Seconds 2
        } while ($ctnRecoveryTimer.Elapsed.TotalSeconds -lt $RecoveryTimeoutSeconds)
        if (-not $ctnRecovered) { throw 'Automatic watcher recovery was not verified before the timeout.' }
        $ctnRecord.recovery_seconds = [math]::Round($ctnRecoveryTimer.Elapsed.TotalSeconds, 3)
        $ctnRecord.after = $ctnAfter
        $ctnRecord.backend_preserved = $ctnAfter.backend_pid -eq $ctnBefore.backend_pid -and
            $ctnAfter.backend_created -eq $ctnBefore.backend_created
        $ctnStage = 'resume_existing_settings'
        $ctnFinish = Invoke-CTNRecoveryMaintenance 'finish'
        if ($ctnFinish.active -ne $false -or $ctnFinish.phase -ne 'completed') { throw 'Maintenance completion was not confirmed.' }
        $ctnMaintenanceStarted = $false
    }
    $ctnRecord.result = 'passed'
} catch {
    $ctnRecord.failed_stage = $ctnStage
    $ctnRecord.error_type = $_.Exception.GetType().Name
    $ctnRecord.result = 'failed'
    if ($ctnMaintenanceStarted) {
        if (-not $ctnWatcherStopped) {
            try { $null = Invoke-CTNRecoveryMaintenance 'abort'; $ctnMaintenanceStarted = $false } catch {}
        }
        $ctnRecord.maintenance_recovery_required = $ctnMaintenanceStarted
        $ctnRecord.maintenance_id = $ctnMaintenanceId
    }
} finally {
    $ctnRecord.finished_at = Get-Date -Format o
    $ctnRecord | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $ctnRoot 'reports\generated\scheduled-task-recovery-verification.json') -Encoding UTF8
}
$ctnRecord | ConvertTo-Json -Depth 6
if ($ctnRecord.result -ne 'passed') { throw ('CTN supervision verification failed during ' + $ctnStage) }
