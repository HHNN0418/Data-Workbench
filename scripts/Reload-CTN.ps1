param(
    [ValidateRange(0, 3600)][int]$WaitTimeoutSeconds = 300,
    [ValidateRange(5, 300)][int]$HealthTimeoutSeconds = 120,
    [string]$Purpose = 'Load Agent reliability fixes after draining existing work',
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,63}$')][string]$ReportPrefix = 'agent-reliability',
    [switch]$RecoverUnresponsiveBackend
)
$ErrorActionPreference = 'Stop'
$ctnRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ctnPython = Join-Path $ctnRoot '.venv\Scripts\python.exe'
$ctnSecretFile = Join-Path $ctnRoot 'data\secrets\postgres_url'
$ctnReports = Join-Path $ctnRoot 'reports\generated'
$ctnReportPath = Join-Path $ctnReports ($ReportPrefix + '-deployment.json')
. (Join-Path $PSScriptRoot 'CTN-TaskRuntime.ps1')
$ctnMaintenanceId = [guid]::NewGuid().ToString()
$ctnMutex = $null
$ctnLocked = $false
$ctnReloadMutex = $null
$ctnReloadLocked = $false
$ctnMaintenanceAttempted = $false
$ctnStopAttempted = $false
$ctnFailure = $null
$ctnStage = 'validate'
$ctnRecord = [ordered]@{
    started_at = (Get-Date -Format o)
    purpose = $Purpose
    maintenance_id = $ctnMaintenanceId
    result = 'starting'
}

function Get-CTNVerifiedListener {
    $ctnOwners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction Stop |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ctnOwners.Count -ne 1) { throw 'Ambiguous listener ownership' }
    $ctnListener = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ctnOwners[0]) -ErrorAction Stop
    if (-not $ctnListener -or -not $ctnListener.CreationDate -or
        $ctnListener.CommandLine -notmatch '-m\s+ctn_data\s+.*\bserve(?:\s|$)' -or
        $ctnListener.CommandLine -notmatch [regex]::Escape($ctnSecretFile)) {
        throw 'Listener is not a verifiable CTN backend'
    }
    $ctnCurrent = $ctnListener
    $ctnVerified = $false
    for ($ctnDepth = 0; $ctnDepth -lt 4 -and $ctnCurrent; $ctnDepth++) {
        if ([StringComparer]::OrdinalIgnoreCase.Equals($ctnCurrent.ExecutablePath, $ctnPython) -or
            $ctnCurrent.CommandLine -match [regex]::Escape((Join-Path $ctnRoot 'Start-CTN.ps1'))) {
            $ctnVerified = $true
            break
        }
        $ctnCreated = $ctnCurrent.CreationDate
        $ctnCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $ctnCurrent.ParentProcessId) `
            -ErrorAction SilentlyContinue
        if ($ctnCurrent -and (-not $ctnCurrent.CreationDate -or $ctnCurrent.CreationDate -gt $ctnCreated)) {
            throw 'Stale process ancestry'
        }
    }
    if (-not $ctnVerified) { throw 'Cannot verify project ownership' }
    return $ctnListener
}

function Assert-CTNSameListener($Expected) {
    $ctnLatest = Get-CTNVerifiedListener
    if ($ctnLatest.ProcessId -ne $Expected.ProcessId -or $ctnLatest.CreationDate -ne $Expected.CreationDate) {
        throw 'Listener changed before restart'
    }
}

function Invoke-CTNMaintenance([ValidateSet('begin', 'wait', 'finish', 'abort')][string]$Action) {
    $ctnCliArguments = @('-X', 'utf8', '-m', 'ctn_data.agent_maintenance',
        '--secret-file', $ctnSecretFile, $Action, '--id', $ctnMaintenanceId,
        '--timeout', $WaitTimeoutSeconds)
    try {
        # The module emits only safe JSON. Unexpected storage tracebacks must
        # never reach console output or the deployment record.
        $ctnCliOutput = @(& $ctnPython @ctnCliArguments 2>$null)
        $ctnCliExit = $LASTEXITCODE
        $ctnState = ($ctnCliOutput -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
    } catch { throw ('Maintenance ' + $Action + ' did not return a valid result') }
    if ($ctnCliExit -ne 0 -or $ctnState.ok -ne $true -or $ctnState.id -cne $ctnMaintenanceId) {
        throw ('Maintenance ' + $Action + ' was not confirmed')
    }
    return $ctnState
}

function Assert-CTNDrained($DrainState) {
    if ($null -eq $DrainState -or $DrainState.id -cne $ctnMaintenanceId -or
        $DrainState.active -ne $true -or $DrainState.phase -ne 'drained' -or
        $DrainState.drained -ne $true -or $DrainState.active_generations -ne 0 -or
        $DrainState.active_operations -ne 0 -or $DrainState.sending_actions -ne 0 -or
        $DrainState.registering_accounts -ne 0 -or $DrainState.active_research -ne 0) {
        throw 'Maintenance has not confirmed that all existing work is drained and fenced'
    }
}

function Stop-CTNDrainedListener($Expected, $DrainState) {
    # This is the script's only Stop-Process call. Both checks must succeed in
    # this function; no timeout or missing CLI field can bypass the fence.
    Assert-CTNDrained $DrainState
    Assert-CTNSameListener $Expected
    # Recheck the durable gates and exclusive research owner immediately before
    # stopping. A previously returned drain result is only a past observation.
    $ctnFreshDrain = Invoke-CTNMaintenance 'wait'
    Assert-CTNDrained $ctnFreshDrain
    Assert-CTNSameListener $Expected
    $script:ctnStopAttempted = $true
    Stop-Process -Id $Expected.ProcessId -ErrorAction Stop
}

try {
    if (-not (Test-Path -LiteralPath $ctnPython -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $ctnRoot 'Start-CTN.ps1') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $ctnRoot 'data\postgres-active') -PathType Leaf) -or
        -not (Test-Path -LiteralPath $ctnSecretFile -PathType Leaf)) {
        throw 'Expected project runtime or PostgreSQL marker is unavailable'
    }
    Set-Location -LiteralPath $ctnRoot
    New-Item -ItemType Directory -Path $ctnReports -Force | Out-Null
    $ctnReloadMutex = New-Object System.Threading.Mutex($false, 'Local\CTN-Reload-8765')
    try { $ctnReloadLocked = $ctnReloadMutex.WaitOne(10000) }
    catch [System.Threading.AbandonedMutexException] { $ctnReloadLocked = $true }
    if (-not $ctnReloadLocked) { throw 'Another CTN reload is active' }
    $ctnMutex = New-Object System.Threading.Mutex($false, 'Local\CTN-Launcher-8765')
    try { $ctnLocked = $ctnMutex.WaitOne(10000) }
    catch [System.Threading.AbandonedMutexException] { $ctnLocked = $true }
    if (-not $ctnLocked) { throw 'Another launcher is active' }

    $ctnStage = 'verify_existing_backend'
    $ctnListener = Get-CTNVerifiedListener
    $ctnHealth = $null
    try {
        $ctnHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 5
    } catch {
        # Recovery can use the exact project command, protected DB argument,
        # creation-time checked ancestry and scheduled watcher ownership when
        # HTTP is unresponsive. It never bypasses the durable drain/fence.
        if (-not $RecoverUnresponsiveBackend -or
            $_.Exception -isnot [System.Threading.Tasks.TaskCanceledException]) { throw }
        $ctnExistingOwner = Get-CTNTaskBackendOwner -Root $ctnRoot -Process $ctnListener
        if (-not $ctnExistingOwner) { throw 'Unresponsive backend has no verified task watcher owner' }
        $ctnRecord.existing_backend_health = 'unresponsive_verified_task_owned_process'
    }
    if (($null -eq $ctnHealth -and -not $ctnRecord.existing_backend_health) -or
        ($null -ne $ctnHealth -and ($ctnHealth.ok -ne $true -or $ctnHealth.backend -ne 'fastapi' -or $ctnHealth.database -ne 'postgresql'))) {
        throw 'Unexpected backend identity'
    }
    Assert-CTNSameListener $ctnListener
    $ctnAutoCollect = $ctnListener.CommandLine -match '(?:\s|^)--auto-collect(?:\s|$)'
    $ctnRecord.previous_backend_pid = $ctnListener.ProcessId
    $ctnRecord.auto_collect_preserved = $ctnAutoCollect
    $ctnStage = 'verify_scheduled_watcher'
    & (Join-Path $ctnRoot 'Install-Autostart.ps1') -Ensure
    $ctnWatcher = Wait-CTNTaskWatcher -Root $ctnRoot
    $ctnRecord.task_name = 'CTN-AutoStart'
    $ctnRecord.watcher_pid = $ctnWatcher.ProcessId
    $ctnRecord.backend_log_directory = 'data\logs'
    $ctnNoCollectPath = Join-Path $ctnRoot 'data\no-auto-collect'
    if ($ctnAutoCollect) {
        if (Test-Path -LiteralPath $ctnNoCollectPath -PathType Leaf) { Remove-Item -LiteralPath $ctnNoCollectPath -Force }
    } else {
        [IO.File]::WriteAllText($ctnNoCollectPath, '')
    }
    $ctnRecord.code_sha256 = @{}
    foreach ($ctnFile in @('ctn_data\agent_control.py', 'ctn_data\agent_actions.py',
        'ctn_data\agent_routes.py', 'ctn_data\agent_decision.py', 'ctn_data\agent_generation.py', 'ctn_data\agent_maintenance.py',
        'ctn_data\agent_audit.py', 'ctn_data\db.py', 'ctn_data\server.py',
        'ctn_data\content_research_schedule.py',
        'ctn_data\agent_interaction_policy.py', 'ctn_data\agent_budget.py',
        'ctn_data\agent_notifications.py', 'ctn_data\agent_observation.py',
        'ctn_data\agent_storage_retry.py', 'ctn_data\ai_providers.py',
        'ctn_data\agent_memory.py', 'ctn_data\agent_memory_archive.py', 'ctn_data\agent_reading.py',
        'ctn_data\ai_routes.py', 'frontend\dist\index.html')) {
        $ctnRecord.code_sha256[$ctnFile] = (Get-FileHash -LiteralPath (Join-Path $ctnRoot $ctnFile) -Algorithm SHA256).Hash
    }

    $ctnStage = 'begin_maintenance'
    $ctnMaintenanceAttempted = $true
    $ctnBegin = Invoke-CTNMaintenance 'begin'
    if ($ctnBegin.active -ne $true) { throw 'Maintenance did not acquire an active hold' }
    $ctnRecord.maintenance_started_at = $ctnBegin.started_at
    $ctnStage = 'drain_existing_work'
    $ctnDrain = Invoke-CTNMaintenance 'wait'
    Assert-CTNDrained $ctnDrain
    $ctnRecord.drained_at = Get-Date -Format o
    $ctnRecord.maintenance_phase = $ctnDrain.phase

    $ctnStage = 'stop_drained_backend'
    Stop-CTNDrainedListener -Expected $ctnListener -DrainState $ctnDrain
    $ctnStopTimer = [Diagnostics.Stopwatch]::StartNew()
    while (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
        if ($ctnStopTimer.Elapsed.TotalSeconds -gt 10) { throw 'Old listener did not exit' }
        Start-Sleep -Milliseconds 200
    }
    $ctnRecord.previous_backend_stopped_at = Get-Date -Format o
    $ctnMutex.ReleaseMutex()
    $ctnLocked = $false
    $ctnStage = 'verify_replacement_backend'
    $ctnTimer = [Diagnostics.Stopwatch]::StartNew()
    $ctnReady = $false
    do {
        try {
            $ctnHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
            if ($ctnHealth.ok -eq $true -and $ctnHealth.backend -eq 'fastapi' -and
                $ctnHealth.database -eq 'postgresql' -and $ctnHealth.agent_reliability_policy -eq 1 -and
                $ctnHealth.research_recovery_policy -eq 1 -and
                $ctnHealth.agent_heartbeat_policy -eq 1 -and
                $ctnHealth.ai_connection_setup_policy -eq 1 -and
                $ctnHealth.agent_memory_policy -eq 2) {
                $ctnReplacement = Get-CTNVerifiedListener
                $ctnOwner = Get-CTNTaskBackendOwner -Root $ctnRoot -Process $ctnReplacement
                if ($ctnOwner) {
                    $ctnRecord.watcher_pid = $ctnOwner.ProcessId
                    $ctnRecord.watcher_parent_pid = $ctnOwner.ParentProcessId
                    $ctnReady = $true
                    break
                }
            }
        } catch {}
        Start-Sleep -Milliseconds 300
    } while ($ctnTimer.Elapsed.TotalSeconds -lt $HealthTimeoutSeconds)
    if (-not $ctnReady) { throw 'Updated backend readiness timed out' }
    $ctnRecord.ready_at = Get-Date -Format o
    $ctnRecord.backend_pid = $ctnReplacement.ProcessId
    $ctnRecord.health = [ordered]@{ok = $true; backend = $ctnHealth.backend;
        database = $ctnHealth.database; agent_reliability_policy = $ctnHealth.agent_reliability_policy;
        research_recovery_policy = $ctnHealth.research_recovery_policy;
        agent_heartbeat_policy = $ctnHealth.agent_heartbeat_policy;
        ai_connection_setup_policy = $ctnHealth.ai_connection_setup_policy;
        agent_memory_policy = $ctnHealth.agent_memory_policy}
    $ctnStage = 'finish_maintenance'
    $ctnFinish = Invoke-CTNMaintenance 'finish'
    if ($ctnFinish.active -ne $false -or $ctnFinish.phase -ne 'completed') {
        throw 'Maintenance completion was not confirmed'
    }
    $ctnRecord.maintenance_phase = $ctnFinish.phase
    $ctnRecord.result = 'ready'
} catch {
    $ctnRecord.failed_stage = $ctnStage
    $ctnRecord.error_type = $_.Exception.GetType().Name
    if ($ctnMaintenanceAttempted -and -not $ctnStopAttempted) {
        try {
            $ctnAbort = Invoke-CTNMaintenance 'abort'
            if ($ctnAbort.active -ne $false) { throw 'Maintenance abort was not confirmed' }
            $ctnRecord.maintenance_phase = $ctnAbort.phase
            $ctnRecord.result = 'aborted_existing_backend_kept'
        } catch {
            $ctnRecord.result = 'maintenance_recovery_required_existing_backend_kept'
        }
    } elseif ($ctnStopAttempted) {
        # The old process may already be gone. Never resume agent writes after
        # an unconfirmed launch/readiness/finish result; keep the durable hold.
        $ctnRecord.result = 'maintenance_recovery_required'
    } else {
        $ctnRecord.result = 'failed_before_maintenance'
    }
    $ctnFailure = 'CTN reload failed during ' + $ctnStage + '; maintenance_id=' +
        $ctnMaintenanceId + '; result=' + $ctnRecord.result + '; inspect ' + $ctnReportPath
} finally {
    if ($ctnMutex) {
        if ($ctnLocked) { $ctnMutex.ReleaseMutex() }
        $ctnMutex.Dispose()
    }
    if ($ctnReloadMutex) {
        if ($ctnReloadLocked) { $ctnReloadMutex.ReleaseMutex() }
        $ctnReloadMutex.Dispose()
    }
    $ctnRecord.finished_at = Get-Date -Format o
    $ctnRecord.stop_attempted = $ctnStopAttempted
    if (Test-Path -LiteralPath $ctnReports -PathType Container) {
        $ctnRecord | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ctnReportPath -Encoding UTF8
    }
}
$ctnRecord | ConvertTo-Json -Depth 6
if ($ctnFailure) { throw $ctnFailure }
