$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$ctnDataDirectory = Join-Path $PSScriptRoot 'data'
$ctnConfigPath = Join-Path $ctnDataDirectory 'tunnel-config.json'
$ctnLogDirectory = Join-Path $ctnDataDirectory 'logs'
New-Item -ItemType Directory -Path $ctnLogDirectory -Force | Out-Null
$ctnWatchLog = Join-Path $ctnLogDirectory 'tunnel-watch.log'

function Write-CTNTunnelLog([string]$Message) {
    $ctnLogLine = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $ctnWatchLog -Value $ctnLogLine -Encoding UTF8
}

function Stop-CTNOwnBridge($Child) {
    if ($null -eq $Child) { return }
    try {
        if (-not $Child.HasExited) {
            # Windows venv Python may run the interpreter as a child of its
            # redirector. Keep the owned Process handle alive while stopping
            # that exact process tree; no listener or process-name lookup.
            $ctnTaskKillPath = Join-Path $env:SystemRoot 'System32\taskkill.exe'
            $ctnStopArguments = @('/PID', [string]$Child.Id, '/T', '/F')
            $ctnTreeStop = Start-Process -FilePath $ctnTaskKillPath -ArgumentList $ctnStopArguments -WindowStyle Hidden -PassThru -Wait
            try {
                if ($ctnTreeStop.ExitCode -ne 0 -and -not $Child.HasExited) {
                    Write-CTNTunnelLog ('Could not stop owned bridge tree PID {0}; taskkill exited {1}.' -f $Child.Id, $ctnTreeStop.ExitCode)
                }
            } finally { $ctnTreeStop.Dispose() }
            if (-not $Child.WaitForExit(5000)) {
                Write-CTNTunnelLog ('Owned bridge PID {0} is still exiting.' -f $Child.Id)
            }
        }
    } catch {
        Write-CTNTunnelLog ('Could not stop owned bridge: ' + $_.Exception.Message)
    } finally {
        $Child.Dispose()
    }
}

function Test-CTNBackend([int]$BackendPort) {
    try {
        $ctnHealth = Invoke-RestMethod -Uri ('http://127.0.0.1:{0}/api/health' -f $BackendPort) -TimeoutSec 5
        return ($ctnHealth.ok -eq $true -and $ctnHealth.backend -eq 'fastapi')
    } catch { return $false }
}

function Test-CTNPortOccupied([int]$ListenPort) {
    $ctnEndpoints = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return (@($ctnEndpoints | Where-Object { $_.Port -eq $ListenPort }).Count -gt 0)
}

# A user/session-local mutex prevents duplicate watchers for this checkout.
$ctnHashProvider = [System.Security.Cryptography.SHA256]::Create()
try {
    $ctnIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $ctnMutexInput = $ctnIdentity + '|' + $PSScriptRoot.ToLowerInvariant()
    $ctnMutexDigest = $ctnHashProvider.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($ctnMutexInput))
    $ctnMutexSuffix = [System.BitConverter]::ToString($ctnMutexDigest).Replace('-', '')
} finally { $ctnHashProvider.Dispose() }
$ctnMutex = New-Object System.Threading.Mutex($false, ('Local\CTNTunnelBridge_' + $ctnMutexSuffix))
$ctnOwnsMutex = $false
$ctnBridge = $null
try {
    try { $ctnOwnsMutex = $ctnMutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $ctnOwnsMutex = $true }
    if (-not $ctnOwnsMutex) {
        Write-CTNTunnelLog 'Another watcher owns the mutex. Exiting without changing any process.'
        exit 0
    }
    Write-CTNTunnelLog 'Watcher started. Only its own bridge will be managed; the CTN backend and cloudflared are independent.'
    while ($true) {
        try {
            $ctnConfigSnapshot = Get-Content -LiteralPath $ctnConfigPath -Raw
            $ctnConfig = $ctnConfigSnapshot | ConvertFrom-Json
            $ctnPublicUrl = [string]$ctnConfig.public_url
            $ctnPort = [int]$ctnConfig.port
            $ctnBackendPort = [int]$ctnConfig.backend_port
            $ctnPythonCandidates = @(
                $env:CTN_PYTHON,
                (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
                (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
            )
            $ctnPython = $ctnPythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
            if (-not $ctnPython) { $ctnPython = (Get-Command python -ErrorAction Stop).Source }

            $ctnCheckArgs = @('-X', 'utf8', '-m', 'ctn_data.tunnel_proxy', '--public-url', $ctnPublicUrl, '--port', $ctnPort, '--backend-port', $ctnBackendPort, '--check')
            $ctnCanonicalUrl = (& $ctnPython @ctnCheckArgs | Out-String).Trim()
            if ($LASTEXITCODE -ne 0) { throw 'Saved tunnel configuration is invalid.' }
            $ctnPublicUrl = $ctnCanonicalUrl

            if (-not (Test-CTNBackend $ctnBackendPort)) {
                Write-CTNTunnelLog ('Backend on port {0} is unavailable; retrying in 15 seconds.' -f $ctnBackendPort)
                Start-Sleep -Seconds 15
                continue
            }
            if (Test-CTNPortOccupied $ctnPort) {
                Write-CTNTunnelLog ('Port {0} is occupied. Leaving its process untouched; retrying in 15 seconds.' -f $ctnPort)
                Start-Sleep -Seconds 15
                continue
            }

            # The validated canonical origin and numeric ports contain no quotes.
            $ctnBridgeArguments = '-X utf8 -u -m ctn_data.tunnel_proxy --public-url "{0}" --port {1} --backend-port {2}' -f $ctnPublicUrl, $ctnPort, $ctnBackendPort
            $ctnBridge = Start-Process -FilePath $ctnPython -ArgumentList $ctnBridgeArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $ctnLogDirectory 'tunnel-bridge.log') -RedirectStandardError (Join-Path $ctnLogDirectory 'tunnel-bridge-error.log')
            Write-CTNTunnelLog ('Started owned bridge PID {0} for {1}.' -f $ctnBridge.Id, $ctnPublicUrl)
            while (-not $ctnBridge.HasExited) {
                Start-Sleep -Seconds 5
                if ((Get-Content -LiteralPath $ctnConfigPath -Raw) -cne $ctnConfigSnapshot) {
                    Write-CTNTunnelLog 'Saved configuration changed. Restarting only the owned bridge.'
                    break
                }
            }
            Write-CTNTunnelLog 'Bridge stopped or needs reconfiguration; retrying in 15 seconds.'
        } catch {
            Write-CTNTunnelLog ('Retrying in 15 seconds: ' + $_.Exception.Message)
        } finally {
            Stop-CTNOwnBridge $ctnBridge
            $ctnBridge = $null
        }
        Start-Sleep -Seconds 15
    }
} finally {
    Stop-CTNOwnBridge $ctnBridge
    if ($ctnOwnsMutex) { $ctnMutex.ReleaseMutex() }
    $ctnMutex.Dispose()
}
