param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$PublicUrl,
    [ValidateRange(1, 65535)]
    [int]$Port = 8766,
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8765
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$ctnTaskName = 'CTN-TunnelAutostart'
$ctnWatcherPath = Join-Path $PSScriptRoot 'Watch-CTN-Tunnel.ps1'
if (-not (Test-Path -LiteralPath $ctnWatcherPath -PathType Leaf)) { throw 'Watch-CTN-Tunnel.ps1 is missing.' }

$ctnPythonCandidates = @(
    $env:CTN_PYTHON,
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
$ctnPython = $ctnPythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $ctnPython) { $ctnPython = (Get-Command python -ErrorAction Stop).Source }
$ctnCheckArgs = @('-X', 'utf8', '-m', 'ctn_data.tunnel_proxy', '--public-url', $PublicUrl, '--port', $Port, '--backend-port', $BackendPort, '--check')
$ctnCanonicalUrl = (& $ctnPython @ctnCheckArgs | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Public URL or port configuration is invalid. No task has been installed.' }

$ctnCurrentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$ctnCurrentUser = $ctnCurrentIdentity.Name
$ctnCurrentUserSid = $ctnCurrentIdentity.User.Value
$ctnWindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$ctnTaskArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $ctnWatcherPath
$ctnExistingTask = Get-ScheduledTask -TaskName $ctnTaskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($ctnExistingTask) {
    $ctnOwnedActions = @($ctnExistingTask.Actions | Where-Object {
        $_.Execute -eq $ctnWindowsPowerShell -and $_.Arguments -eq $ctnTaskArguments -and $_.WorkingDirectory -eq $PSScriptRoot
    })
    $ctnOwnedPrincipal = $ctnExistingTask.Principal.UserId -in @($ctnCurrentUser, $ctnCurrentUserSid)
    if ($ctnOwnedActions.Count -ne 1 -or @($ctnExistingTask.Actions).Count -ne 1 -or -not $ctnOwnedPrincipal) {
        throw 'CTN-TunnelAutostart belongs to a different user, command or checkout. It will not be modified.'
    }
}

$ctnDataDirectory = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Path $ctnDataDirectory -Force | Out-Null
$ctnConfigPath = Join-Path $ctnDataDirectory 'tunnel-config.json'
$ctnConfigTemporary = Join-Path $ctnDataDirectory ('tunnel-config.{0}.tmp' -f $PID)
$ctnSavedConfig = [ordered]@{
    public_url = $ctnCanonicalUrl
    port = $Port
    backend_port = $BackendPort
}
try {
    $ctnSavedConfig | ConvertTo-Json | Set-Content -LiteralPath $ctnConfigTemporary -Encoding UTF8
    Move-Item -LiteralPath $ctnConfigTemporary -Destination $ctnConfigPath -Force
} finally {
    if (Test-Path -LiteralPath $ctnConfigTemporary) { Remove-Item -LiteralPath $ctnConfigTemporary -Force }
}

$ctnAction = New-ScheduledTaskAction -Execute $ctnWindowsPowerShell -Argument $ctnTaskArguments -WorkingDirectory $PSScriptRoot
$ctnTrigger = New-ScheduledTaskTrigger -AtLogOn -User $ctnCurrentUser
$ctnPrincipal = New-ScheduledTaskPrincipal -UserId $ctnCurrentUser -LogonType Interactive -RunLevel Limited
$ctnSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $ctnTaskName -TaskPath '\' -Action $ctnAction -Trigger $ctnTrigger -Principal $ctnPrincipal -Settings $ctnSettings -Description 'Restore the independent CTN public bridge after this Windows user logs in; the backend and cloudflared remain separate.' -Force | Out-Null
Start-ScheduledTask -TaskName $ctnTaskName -TaskPath '\'
Write-Output ('Saved {0} and started {1}. It will return after this Windows user logs in.' -f $ctnCanonicalUrl, $ctnTaskName)
Write-Output ('Logs: {0}' -f (Join-Path $ctnDataDirectory 'logs\tunnel-watch.log'))
