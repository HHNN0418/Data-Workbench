$ErrorActionPreference = 'Stop'
$ctnMockRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ctnInstaller = Join-Path $ctnMockRoot 'Install-Autostart.ps1'
$ctnMockIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$ctnMockPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$ctnMockWatcher = Join-Path $ctnMockRoot 'Watch-CTN.ps1'
$ctnMockArguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $ctnMockWatcher
$ctnMockLegacyArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $ctnMockWatcher

function Assert-CTNAutostart([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Get-ScheduledTask {
    [CmdletBinding()]
    param($TaskName, $TaskPath)
    Assert-CTNAutostart ($TaskName -eq 'CTN-AutoStart' -and $TaskPath -eq '\') 'Only the root CTN task may be inspected.'
    return $global:ctnMockExisting
}
function New-ScheduledTaskAction {
    param($Execute, $Argument, $WorkingDirectory)
    return [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory}
}
function New-ScheduledTaskTrigger {
    param([switch]$AtLogOn, [switch]$Once, $User, $At, [timespan]$RepetitionInterval, $RepetitionDuration)
    return [pscustomobject]@{Logon=[bool]$AtLogOn;Once=[bool]$Once;User=$User;At=$At;Interval=$RepetitionInterval;Duration=$RepetitionDuration}
}
function New-ScheduledTaskPrincipal {
    param($UserId, $LogonType, $RunLevel)
    return [pscustomobject]@{UserId=$UserId;LogonType=$LogonType;RunLevel=$RunLevel}
}
function New-ScheduledTaskSettingsSet {
    param([switch]$StartWhenAvailable, [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries,
        $MultipleInstances, [timespan]$ExecutionTimeLimit, [int]$RestartCount, [timespan]$RestartInterval)
    return [pscustomobject]@{StartWhenAvailable=[bool]$StartWhenAvailable;AllowBattery=[bool]$AllowStartIfOnBatteries;
        KeepBattery=[bool]$DontStopIfGoingOnBatteries;MultipleInstances=$MultipleInstances;Limit=$ExecutionTimeLimit;
        RestartCount=$RestartCount;RestartInterval=$RestartInterval}
}
function Register-ScheduledTask {
    param($TaskName, $TaskPath, $Action, $Trigger, $Principal, $Settings, $Description, [switch]$Force)
    $global:ctnMockRegistrations.Add([pscustomobject]@{Name=$TaskName;Path=$TaskPath;Action=$Action;Triggers=@($Trigger);
        Principal=$Principal;Settings=$Settings;Description=$Description;Force=[bool]$Force})
}
function Start-ScheduledTask {
    param($TaskName, $TaskPath)
    $global:ctnMockStarts.Add([pscustomobject]@{Name=$TaskName;Path=$TaskPath})
}
function Reset-CTNAutostartMock {
    $global:ctnMockExisting = $null
    $global:ctnMockRegistrations = [Collections.Generic.List[object]]::new()
    $global:ctnMockStarts = [Collections.Generic.List[object]]::new()
}
function New-CTNExistingTask([string]$Arguments=$ctnMockLegacyArguments, [string]$State='Ready') {
    return [pscustomobject]@{State=$State;Actions=@([pscustomobject]@{Execute=$ctnMockPowerShell;
        Arguments=$Arguments;WorkingDirectory=$ctnMockRoot});Principal=[pscustomobject]@{
        UserId=$ctnMockIdentity.Name;LogonType=3;RunLevel=0}}
}
function New-CTNConfiguredTask([string]$State='Running') {
    $ctnConfiguredTask = New-CTNExistingTask -Arguments $ctnMockArguments -State $State
    $ctnConfiguredTask | Add-Member NoteProperty Triggers @(
        [pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskLogonTrigger'};
            UserId=$ctnMockIdentity.Name;Enabled=$true},
        [pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskTimeTrigger'};
            Enabled=$true;EndBoundary='';Repetition=[pscustomobject]@{Interval='PT1M';Duration=''}}
    )
    $ctnConfiguredTask | Add-Member NoteProperty Settings ([pscustomobject]@{
        MultipleInstances=2;ExecutionTimeLimit='PT0S';RestartCount=999;RestartInterval='PT1M';
        StartWhenAvailable=$true;DisallowStartIfOnBatteries=$false;StopIfGoingOnBatteries=$false;Enabled=$true})
    return $ctnConfiguredTask
}
function Assert-CTNAutostartRejected {
    $ctnRejected = $false
    try { & $ctnInstaller | Out-Null } catch { $ctnRejected = $true }
    Assert-CTNAutostart $ctnRejected 'An unrelated task must be rejected.'
    Assert-CTNAutostart ($ctnMockRegistrations.Count -eq 0 -and $ctnMockStarts.Count -eq 0) 'Rejected ownership must not mutate any task.'
}

Reset-CTNAutostartMock
& $ctnInstaller | Out-Null
Assert-CTNAutostart ($ctnMockRegistrations.Count -eq 1 -and $ctnMockStarts.Count -eq 1) 'A new task must be registered and started.'
$ctnRegistered = $ctnMockRegistrations[0]
Assert-CTNAutostart ($ctnRegistered.Name -eq 'CTN-AutoStart' -and $ctnRegistered.Path -eq '\') 'The canonical task identity must be preserved.'
Assert-CTNAutostart ($ctnRegistered.Action.Execute -eq $ctnMockPowerShell -and $ctnRegistered.Action.Arguments -eq $ctnMockArguments) 'The task must use system Windows PowerShell in hidden noninteractive mode.'
Assert-CTNAutostart ($ctnRegistered.Action.WorkingDirectory -eq $ctnMockRoot) 'The task must run from this checkout.'
Assert-CTNAutostart ($ctnRegistered.Principal.UserId -eq $ctnMockIdentity.Name -and $ctnRegistered.Principal.LogonType -eq 'Interactive' -and $ctnRegistered.Principal.RunLevel -eq 'Limited') 'Existing credentials require the current interactive user without elevation.'
Assert-CTNAutostart ($ctnRegistered.Triggers.Count -eq 2 -and $ctnRegistered.Triggers[0].Logon -and $ctnRegistered.Triggers[1].Once) 'Logon and recurring recovery triggers are required.'
Assert-CTNAutostart ($ctnRegistered.Triggers[1].Interval.TotalMinutes -eq 1 -and $null -eq $ctnRegistered.Triggers[1].Duration) 'Recovery must repeat every minute without an expiry.'
Assert-CTNAutostart ($ctnRegistered.Settings.MultipleInstances -eq 'IgnoreNew' -and $ctnRegistered.Settings.Limit -eq [timespan]::Zero) 'Duplicate watchers and execution time limits must be disabled.'
Assert-CTNAutostart ($ctnRegistered.Settings.RestartCount -eq 999 -and $ctnRegistered.Settings.RestartInterval.TotalMinutes -eq 1) 'Watcher failure must request one-minute retries.'
Assert-CTNAutostart ($ctnRegistered.Settings.AllowBattery -and $ctnRegistered.Settings.KeepBattery -and $ctnRegistered.Settings.StartWhenAvailable) 'Desktop and battery availability must not prevent recovery.'

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask
& $ctnInstaller | Out-Null
Assert-CTNAutostart ($ctnMockRegistrations.Count -eq 1 -and $ctnMockStarts.Count -eq 1) 'An owned legacy task must be upgraded and started.'

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask -Arguments $ctnMockArguments -State Running
& $ctnInstaller | Out-Null
Assert-CTNAutostart ($ctnMockRegistrations.Count -eq 1 -and $ctnMockStarts.Count -eq 0) 'Updating a running task must not restart it.'

Reset-CTNAutostartMock
& $ctnInstaller -NoStart | Out-Null
Assert-CTNAutostart ($ctnMockRegistrations.Count -eq 1 -and $ctnMockStarts.Count -eq 0) 'NoStart must install without immediately starting the task.'

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask -Arguments ($ctnMockLegacyArguments + ' -UnrelatedCommand')
Assert-CTNAutostartRejected

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask
$ctnMockExisting.Actions += $ctnMockExisting.Actions[0]
Assert-CTNAutostartRejected

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask
$ctnMockExisting.Principal.UserId = 'S-1-5-18'
Assert-CTNAutostartRejected

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask
$ctnMockExisting.Actions[0].WorkingDirectory = Join-Path $ctnMockRoot 'unrelated'
Assert-CTNAutostartRejected

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask
$ctnMockExisting.Actions[0].Execute = 'unrelated-powershell.exe'
Assert-CTNAutostartRejected

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask
$ctnMockExisting.Principal.UserId = $ctnMockIdentity.User.Value
& $ctnInstaller | Out-Null
Assert-CTNAutostart ($ctnMockRegistrations.Count -eq 1) 'The current user SID must be accepted.'

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask
$ctnMockExisting.Principal.RunLevel = 1
Assert-CTNAutostartRejected

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask
$ctnMockExisting.Principal.LogonType = 5
Assert-CTNAutostartRejected

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNConfiguredTask
& $ctnInstaller -Ensure | Out-Null
Assert-CTNAutostart ($ctnMockRegistrations.Count -eq 0 -and $ctnMockStarts.Count -eq 0) 'Ensure must preserve an already configured running task.'

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNConfiguredTask -State Ready
& $ctnInstaller -Ensure | Out-Null
Assert-CTNAutostart ($ctnMockRegistrations.Count -eq 0 -and $ctnMockStarts.Count -eq 1) 'Ensure must start a configured idle task without re-registering it.'

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNConfiguredTask
$ctnMockExisting.Triggers[1].Repetition.Duration = 'P1D'
& $ctnInstaller -Ensure | Out-Null
Assert-CTNAutostart ($ctnMockRegistrations.Count -eq 1 -and $ctnMockStarts.Count -eq 0) 'Ensure must repair an expiring recovery trigger without stopping a running task.'

Reset-CTNAutostartMock
$global:ctnMockExisting = New-CTNExistingTask
& $ctnInstaller -Ensure | Out-Null
Assert-CTNAutostart ($ctnMockRegistrations.Count -eq 1 -and $ctnMockStarts.Count -eq 1) 'Ensure must upgrade and start a legacy task.'

'Autostart mock checks passed: 16 scenarios; no live tasks or processes changed.'
