param([switch]$NoStart, [switch]$Ensure)

$ErrorActionPreference = 'Stop'
$ctnTaskName = 'CTN-AutoStart'
$ctnIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$ctnUser = $ctnIdentity.Name
$ctnUserSid = $ctnIdentity.User.Value
$ctnScript = Join-Path $PSScriptRoot 'Watch-CTN.ps1'
$ctnWindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$ctnArguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $ctnScript
$ctnLegacyArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $ctnScript
if (-not (Test-Path -LiteralPath $ctnScript -PathType Leaf) -or
    -not (Test-Path -LiteralPath $ctnWindowsPowerShell -PathType Leaf)) {
    throw 'The CTN watcher or Windows PowerShell is missing. No task was installed.'
}
$ctnExisting = Get-ScheduledTask -TaskName $ctnTaskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($ctnExisting) {
    $ctnActions = @($ctnExisting.Actions)
    $ctnPrincipalMatches = $ctnExisting.Principal.UserId -in @($ctnUser, $ctnUserSid)
    if (-not $ctnPrincipalMatches) {
        try {
            $ctnAccount = New-Object Security.Principal.NTAccount($ctnExisting.Principal.UserId)
            $ctnPrincipalMatches = $ctnAccount.Translate([Security.Principal.SecurityIdentifier]).Value -eq $ctnUserSid
        } catch { $ctnPrincipalMatches = $false }
    }
    if ($ctnActions.Count -ne 1 -or
        $ctnActions[0].Execute -ne $ctnWindowsPowerShell -or
        $ctnActions[0].Arguments -notin @($ctnArguments, $ctnLegacyArguments) -or
        $ctnActions[0].WorkingDirectory -ne $PSScriptRoot -or
        -not $ctnPrincipalMatches -or
        [int]$ctnExisting.Principal.LogonType -ne 3 -or
        [int]$ctnExisting.Principal.RunLevel -ne 0) {
        throw 'CTN-AutoStart belongs to a different user, command or checkout. No task was changed.'
    }
}
$ctnWasRunning = $ctnExisting -and [string]$ctnExisting.State -eq 'Running'
$ctnConfigured = $false
if ($Ensure -and $ctnExisting -and $ctnExisting.Actions[0].Arguments -eq $ctnArguments) {
    $ctnLogonTriggers = @($ctnExisting.Triggers | Where-Object {
        $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' -and $_.Enabled -ne $false -and
        $_.UserId -in @($ctnUser, $ctnUserSid, $ctnExisting.Principal.UserId)
    })
    $ctnRecoveryTriggers = @($ctnExisting.Triggers | Where-Object {
        $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' -and $_.Enabled -ne $false -and
        $_.Repetition.Interval -eq 'PT1M' -and -not $_.Repetition.Duration -and -not $_.EndBoundary
    })
    $ctnConfigured = @($ctnExisting.Triggers).Count -eq 2 -and $ctnLogonTriggers.Count -eq 1 -and
        $ctnRecoveryTriggers.Count -eq 1 -and [int]$ctnExisting.Settings.MultipleInstances -eq 2 -and
        $ctnExisting.Settings.ExecutionTimeLimit -eq 'PT0S' -and
        $ctnExisting.Settings.RestartCount -eq 999 -and $ctnExisting.Settings.RestartInterval -eq 'PT1M' -and
        $ctnExisting.Settings.StartWhenAvailable -eq $true -and
        $ctnExisting.Settings.DisallowStartIfOnBatteries -eq $false -and
        $ctnExisting.Settings.StopIfGoingOnBatteries -eq $false -and
        $ctnExisting.Settings.Enabled -ne $false
}
if ($ctnConfigured) {
    if (-not $NoStart -and -not $ctnWasRunning) {
        Start-ScheduledTask -TaskName $ctnTaskName -TaskPath '\'
    }
    Write-Output 'CTN-AutoStart is already configured for independent supervision.'
    return
}
$ctnAction = New-ScheduledTaskAction -Execute $ctnWindowsPowerShell -Argument $ctnArguments -WorkingDirectory $PSScriptRoot
$ctnTriggers = @(
    New-ScheduledTaskTrigger -AtLogOn -User $ctnUser
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
)
$ctnPrincipal = New-ScheduledTaskPrincipal -UserId $ctnUser -LogonType Interactive -RunLevel Limited
$ctnSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $ctnTaskName -TaskPath '\' -Action $ctnAction -Trigger $ctnTriggers -Principal $ctnPrincipal `
    -Settings $ctnSettings -Description 'Independently supervise CTN after Windows logon, recover missing services and retry an exited watcher every minute.' -Force | Out-Null
if (-not $NoStart -and -not $ctnWasRunning) {
    Start-ScheduledTask -TaskName $ctnTaskName -TaskPath '\'
}
Write-Output ('CTN-AutoStart registered with logon startup and one-minute recovery.' +
    $(if ($NoStart) { ' Immediate startup was skipped.' } elseif ($ctnWasRunning) { ' The running task was preserved.' } else { ' The task was started.' }))
