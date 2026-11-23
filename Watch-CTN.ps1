# Started at Windows logon. Container volumes and local state survive restarts.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'scripts\CTN-TaskRuntime.ps1')
$ctnDocker = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
$ctnDesktop = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'
$ctnLogDir = Join-Path $PSScriptRoot 'data\logs'
New-Item -ItemType Directory -Path $ctnLogDir -Force | Out-Null
$ctnWatchLog = Join-Path $ctnLogDir 'watcher.log'
$ctnBackendProcess = $null
$ctnHealthState = $null
$ctnBlockedListener = $null
$ctnWatcherMutex = $null
$ctnOwnsWatcherMutex = $false
$ctnProxyProcess = $null
$ctnNextPostgresAttempt = [datetime]::MinValue
$ctnNextProxyAttempt = [datetime]::MinValue
$ctnReceiptFailure = $false
$ctnNextDependencyCheck = [datetime]::MinValue

function Invoke-CTNWatchDocker {
    param(
        [string[]]$DockerArguments,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 15,
        [string]$OutputPath
    )
    $ctnTemporaryOutput = -not $OutputPath
    if ($ctnTemporaryOutput) {
        $OutputPath = Join-Path $ctnLogDir ('.docker-probe-' + [guid]::NewGuid().ToString('N') + '.out.tmp')
    }
    $ctnCommandErrorPath = $OutputPath + '.err'
    $ctnCommandProcess = $null
    try {
        $ctnCommandProcess = Start-Process -FilePath $ctnDocker -WindowStyle Hidden -PassThru -ErrorAction Stop `
            -ArgumentList $DockerArguments -WorkingDirectory $PSScriptRoot `
            -RedirectStandardOutput $OutputPath -RedirectStandardError $ctnCommandErrorPath
        $null = $ctnCommandProcess.Handle
        if (-not $ctnCommandProcess.WaitForExit($TimeoutSeconds * 1000)) {
            try { $ctnCommandProcess.Kill() }
            catch { Write-CTNWatchEvent 'docker-command-cleanup-failed' ('exception_type=' + $_.Exception.GetType().Name) }
            try { $null = $ctnCommandProcess.WaitForExit(5000) }
            catch { Write-CTNWatchEvent 'docker-command-cleanup-failed' ('exception_type=' + $_.Exception.GetType().Name) }
            Write-CTNWatchEvent 'docker-command-timeout' ('timeout_seconds=' + $TimeoutSeconds)
            return [pscustomobject]@{ExitCode = -1; Output = @(); TimedOut = $true}
        }
        $ctnCommandProcess.Refresh()
        return [pscustomobject]@{
            ExitCode = $ctnCommandProcess.ExitCode
            Output = @([IO.File]::ReadAllLines($OutputPath))
            TimedOut = $false
        }
    } catch {
        Write-CTNWatchEvent 'docker-command-failed' ('exception_type=' + $_.Exception.GetType().Name)
        return [pscustomobject]@{ExitCode = -1; Output = @(); TimedOut = $false}
    } finally {
        if ($ctnCommandProcess) {
            try { $ctnCommandProcess.Dispose() }
            catch { Write-CTNWatchEvent 'docker-command-cleanup-failed' ('exception_type=' + $_.Exception.GetType().Name) }
        }
        if ($ctnTemporaryOutput) {
            foreach ($ctnTemporaryPath in @($OutputPath, $ctnCommandErrorPath)) {
                try { if ([IO.File]::Exists($ctnTemporaryPath)) { [IO.File]::Delete($ctnTemporaryPath) } }
                catch { Write-CTNWatchEvent 'docker-command-cleanup-failed' ('exception_type=' + $_.Exception.GetType().Name) }
            }
        }
    }
}

function Test-CTNComposeRunning([switch]$Proxy) {
    $ctnComposeArgs = @('compose','-p','ctn','-f','compose.yaml')
    $ctnExpected = @('postgres')
    if ($Proxy) {
        $ctnComposeArgs += @('-f','compose.proxy.yaml')
        $ctnExpected += @('gluetun-au-a','gluetun-au-b','worker-au-a','worker-au-b')
    }
    $ctnProbe = Invoke-CTNWatchDocker -DockerArguments ($ctnComposeArgs + @('ps','-a','--format','{{.Service}}|{{.State}}'))
    if ($ctnProbe.ExitCode -ne 0) { return $null }
    $ctnStates = $ctnProbe.Output
    foreach ($ctnService in $ctnExpected) {
        if ($ctnStates -notcontains ($ctnService + '|running')) { return $false }
    }
    # Container health has its own checks; compose up cannot repair a running
    # but unhealthy container. Never continually recreate healthy dependencies.
    return $true
}

function Initialize-CTNRuntimeSecrets {
    & (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -m ctn_data.secret_files prepare --root $PSScriptRoot *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-CTNWatchEvent 'runtime-secrets-unavailable' 'retry_after_seconds=60'
        return $false
    }
    return $true
}

function Update-CTNWatchDependencies([bool]$BackendReady) {
    if ((Get-Date) -lt $script:ctnNextDependencyCheck) { return }
    try {
        $ctnDockerInfo = Invoke-CTNWatchDocker -DockerArguments @('info','--format','{{.ServerVersion}}')
        if ($ctnDockerInfo.ExitCode -ne 0) {
            if (-not $ctnDockerInfo.TimedOut -and -not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
                Start-Process -FilePath $ctnDesktop -WindowStyle Hidden
            }
            return
        }
        $ctnPostgresRunning = Test-CTNComposeRunning
        if ($null -eq $ctnPostgresRunning) { return }
        if (-not $ctnPostgresRunning -and (Get-Date) -ge $script:ctnNextPostgresAttempt) {
            $script:ctnNextPostgresAttempt = (Get-Date).AddSeconds(60)
            if (-not (Initialize-CTNRuntimeSecrets)) { return }
            $ctnDockerLog = Join-Path $ctnLogDir ('docker-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + $PID + '.log')
            $ctnPostgresStart = Invoke-CTNWatchDocker -DockerArguments @('compose','-p','ctn','-f','compose.yaml','up','-d','postgres') `
                -TimeoutSeconds 60 -OutputPath $ctnDockerLog
            Write-CTNWatchEvent 'postgres-start-attempt' ('exit_code=' + $ctnPostgresStart.ExitCode + ' log=' + [IO.Path]::GetFileName($ctnDockerLog))
            if ($ctnPostgresStart.ExitCode -ne 0) { return }
        }
        if ((Test-Path -LiteralPath 'data\proxy-enabled') -and (Test-Path -LiteralPath 'data\secrets\nord_password')) {
            if ((-not $script:ctnProxyProcess -or $script:ctnProxyProcess.HasExited) -and
                (Get-Date) -ge $script:ctnNextProxyAttempt) {
                $ctnProxyRunning = Test-CTNComposeRunning -Proxy
                if ($null -eq $ctnProxyRunning -or $ctnProxyRunning) { return }
                $script:ctnNextProxyAttempt = (Get-Date).AddMinutes(5)
                if (-not (Initialize-CTNRuntimeSecrets)) { return }
                $ctnProxyStamp = (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + $PID
                $script:ctnProxyProcess = Start-Process -FilePath $ctnDocker -WindowStyle Hidden -PassThru `
                    -ArgumentList @('compose','-p','ctn','-f','compose.yaml','-f','compose.proxy.yaml','up','-d') `
                    -WorkingDirectory $PSScriptRoot -RedirectStandardOutput (Join-Path $ctnLogDir ('proxy-' + $ctnProxyStamp + '.out.log')) `
                    -RedirectStandardError (Join-Path $ctnLogDir ('proxy-' + $ctnProxyStamp + '.err.log'))
                Write-CTNWatchEvent 'proxy-start-attempt' ('launcher_pid=' + $script:ctnProxyProcess.Id + ' log_prefix=proxy-' + $ctnProxyStamp)
            }
        }
    } catch {
        Write-CTNWatchEvent 'dependency-recovery-failed' ('exception_type=' + $_.Exception.GetType().Name)
    } finally {
        $script:ctnNextDependencyCheck = (Get-Date).AddSeconds($(if ($BackendReady) { 120 } else { 30 }))
    }
}

function Write-CTNWatchEvent([string]$Event, [string]$Details) {
    # Callers supply only fixed status text, process IDs, exit codes and log names.
    # Never write command lines, exception messages or environment values here.
    Add-Content -LiteralPath $ctnWatchLog -Encoding UTF8 -ErrorAction Continue `
        -Value ((Get-Date -Format o) + ' ' + $Event + ' ' + $Details)
}

function Set-CTNWatchHealth([string]$State) {
    if ($script:ctnHealthState -ne $State) {
        Write-CTNWatchEvent 'backend-health' ('state=' + $State)
        $script:ctnHealthState = $State
    }
    if ($State -eq 'ready') {
        try {
            $null = Write-CTNTaskBackendReceipt -Root $PSScriptRoot
            $script:ctnReceiptFailure = $false
        } catch {
            if (-not $script:ctnReceiptFailure) {
                Write-CTNWatchEvent 'backend-receipt-unavailable' ('exception_type=' + $_.Exception.GetType().Name)
            }
            $script:ctnReceiptFailure = $true
        }
    }
}

function Get-CTNWatchListenerIds {
    $ctnOwners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ctnOwners.Count -gt 0) { return $ctnOwners }
    # Confirm an empty CIM result through the socket API as well. If ownership
    # inspection is unavailable, an existing listener must still block startup.
    try {
        $ctnEndpoints = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
        if (@($ctnEndpoints | Where-Object { $_.Port -eq 8765 }).Count -gt 0) { return 'unknown' }
    } catch {
        Write-CTNWatchEvent 'listener-check-failed' ('exception_type=' + $_.Exception.GetType().Name)
        return 'unknown'
    }
}

try {
    $ctnWatcherMutex = New-Object System.Threading.Mutex($false, 'Local\CTN-Watcher-8765')
    try { $ctnOwnsWatcherMutex = $ctnWatcherMutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $ctnOwnsWatcherMutex = $true }
    if (-not $ctnOwnsWatcherMutex) {
        Write-CTNWatchEvent 'watcher-already-running' ('ignored_watcher_pid=' + $PID)
        return
    }
    Write-CTNWatchEvent 'watcher-started' ('watcher_pid=' + $PID)
while ($true) {
    if ($ctnBackendProcess) {
        $ctnBackendProcess.Refresh()
        if ($ctnBackendProcess.HasExited) {
            # This is the Start-CTN PowerShell launcher, not necessarily the listener.
            Write-CTNWatchEvent 'backend-launcher-exited' `
                ('launcher_pid=' + $ctnBackendProcess.Id + ' exit_code=' + $ctnBackendProcess.ExitCode)
            $ctnBackendProcess = $null
        }
    }
    try {
        $ctnHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 5
        if ($ctnHealth.ok) {
            Set-CTNWatchHealth 'ready'
            $ctnBlockedListener = $null
            Update-CTNWatchDependencies -BackendReady $true
            Start-Sleep -Seconds 15
            continue
        }
    } catch {}
    Set-CTNWatchHealth 'unreachable'
    # Health timeouts can occur while a live backend is busy. A listener started
    # by another launcher still owns this port; never create a competing backend.
    $ctnListenerIds = @(Get-CTNWatchListenerIds)
    if ($ctnListenerIds.Count -gt 0) {
        $ctnListenerKey = ($ctnListenerIds | Sort-Object) -join ','
        if ($ctnBlockedListener -ne $ctnListenerKey) {
            Write-CTNWatchEvent 'backend-listener-present' ('listener_pids=' + $ctnListenerKey)
            $ctnBlockedListener = $ctnListenerKey
        }
        Update-CTNWatchDependencies -BackendReady $false
        Start-Sleep -Seconds 15
        continue
    }
    $ctnBlockedListener = $null
    # A live launcher may be waiting for PostgreSQL recovery. Do not duplicate it.
    if ($ctnBackendProcess -and -not $ctnBackendProcess.HasExited) {
        Update-CTNWatchDependencies -BackendReady $false
        Start-Sleep -Seconds 15
        continue
    }
    $ctnLaunchMutex = New-Object System.Threading.Mutex($false, 'Local\CTN-Launcher-8765')
    $ctnLaunchLock = $false
    try {
        try { $ctnLaunchLock = $ctnLaunchMutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $ctnLaunchLock = $true }
        if ($ctnLaunchLock) {
            $ctnAlreadyReady = $false
            try { $ctnAlreadyReady = (Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 3).ok } catch {}
            # Recheck ownership after taking the shared launcher lock.
            if (-not $ctnAlreadyReady -and @(Get-CTNWatchListenerIds).Count -eq 0) {
                $ctnStartScript = Join-Path $PSScriptRoot 'Start-CTN.ps1'
                $ctnStartStamp = (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + $PID
                $ctnOutputLog = Join-Path $ctnLogDir ('backend-' + $ctnStartStamp + '.out.log')
                $ctnErrorLog = Join-Path $ctnLogDir ('backend-' + $ctnStartStamp + '.err.log')
                try {
                    $ctnStartArguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + $ctnStartScript + '"'))
                    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'data\no-auto-collect')) {
                        $ctnStartArguments += '-NoAutoCollect'
                    }
                    $ctnBackendProcess = Start-Process -FilePath (Get-Command powershell.exe).Source -WindowStyle Hidden -PassThru -ErrorAction Stop `
                        -ArgumentList $ctnStartArguments `
                        -WorkingDirectory $PSScriptRoot -RedirectStandardOutput $ctnOutputLog `
                        -RedirectStandardError $ctnErrorLog
                    Write-CTNWatchEvent 'backend-launcher-started' `
                        ('launcher_pid=' + $ctnBackendProcess.Id + ' stdout=' + [IO.Path]::GetFileName($ctnOutputLog) + ' stderr=' + [IO.Path]::GetFileName($ctnErrorLog))
                } catch {
                    Write-CTNWatchEvent 'backend-launch-failed' ('exception_type=' + $_.Exception.GetType().Name)
                    Update-CTNWatchDependencies -BackendReady $false
                    Start-Sleep -Seconds 15
                    continue
                }
                $ctnLaunchTimer = [Diagnostics.Stopwatch]::StartNew()
                do {
                    try {
                        if ((Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2).ok) {
                            Set-CTNWatchHealth 'ready'
                            break
                        }
                    } catch {}
                    $ctnBackendProcess.Refresh()
                    if ($ctnBackendProcess.HasExited) { break }
                    Start-Sleep -Milliseconds 500
                } while ($ctnLaunchTimer.Elapsed.TotalSeconds -lt 30)
            }
        }
    } finally {
        if ($ctnLaunchLock) { $ctnLaunchMutex.ReleaseMutex() }
        $ctnLaunchMutex.Dispose()
    }
    Update-CTNWatchDependencies -BackendReady ($ctnHealthState -eq 'ready')
    Start-Sleep -Seconds 15
}
} catch {
    Write-CTNWatchEvent 'watcher-failed' ('exception_type=' + $_.Exception.GetType().Name)
    exit 1
} finally {
    if ($ctnOwnsWatcherMutex) { $ctnWatcherMutex.ReleaseMutex() }
    if ($ctnWatcherMutex) { $ctnWatcherMutex.Dispose() }
}
