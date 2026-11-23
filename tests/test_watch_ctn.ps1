# Execute the actual watcher loop body with all process/network operations mocked.
# This does not start a watcher, Docker, a backend or a scheduled task.
$ErrorActionPreference = 'Stop'
$ctnWatchPath = Join-Path $PSScriptRoot '..\Watch-CTN.ps1'
$ctnTokens = $null
$ctnErrors = $null
$ctnAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $ctnWatchPath, [ref]$ctnTokens, [ref]$ctnErrors)
if ($ctnErrors.Count) { throw 'Watcher has PowerShell syntax errors.' }
foreach ($ctnFunction in $ctnAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    . ([scriptblock]::Create($ctnFunction.Extent.Text))
}
$ctnComposeProbe = ${function:Test-CTNComposeRunning}
$ctnLoop = $ctnAst.Find({ param($n) $n -is [System.Management.Automation.Language.WhileStatementAst] }, $true)
$ctnMockProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ctnIteration = [scriptblock]::Create('$PSScriptRoot = $ctnMockProjectRoot; foreach ($ctnMockIteration in 1) ' + $ctnLoop.Body.Extent.Text)
$ctnOuterTry = $ctnAst.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.TryStatementAst] } | Select-Object -Last 1
$ctnGuard = [scriptblock]::Create(($ctnOuterTry.Body.Statements | Where-Object {
    $_ -isnot [System.Management.Automation.Language.WhileStatementAst]
} | ForEach-Object { $_.Extent.Text }) -join "`n")
$ctnCleanup = [scriptblock]::Create($ctnOuterTry.Finally.Extent.Text.Trim().TrimStart('{').TrimEnd('}'))
$ctnScratch = Join-Path ([IO.Path]::GetTempPath()) ('ctn-watch-mock-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($ctnScratch) | Out-Null

function Assert-CTNMock([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Write-CTNWatchEvent([string]$Event, [string]$Details) {
    $script:ctnEvents.Add([pscustomobject]@{Event=$Event; Details=$Details})
}
function Test-CTNComposeRunning { param([switch]$Proxy) return $script:ctnComposeReady }
function Initialize-CTNRuntimeSecrets { return $true }
function Write-CTNTaskBackendReceipt {
    param($Root)
    $script:ctnReceiptWrites++
    if ($script:ctnReceiptThrows) { throw 'DO_NOT_LOG_RECEIPT_SECRET' }
    return $true
}
function Test-Path {
    param($LiteralPath)
    return $script:ctnNoAutoCollect -and $LiteralPath.EndsWith('data\no-auto-collect')
}
function Start-Sleep { param($Seconds, $Milliseconds) $script:ctnSleeps.Add($Seconds) }
function Get-Command { param($Name) [pscustomobject]@{Source='mock-powershell.exe'} }
function New-CTNMockProcess([bool]$Exited=$false, [int]$Code=0, [bool]$Completes=$true) {
    $ctnMock = [pscustomobject]@{Id=1234;HasExited=$Exited;ExitCode=$Code;Completes=$Completes}
    $ctnMock | Add-Member ScriptMethod Refresh {}
    $ctnMock | Add-Member ScriptMethod WaitForExit {
        param($Milliseconds)
        $script:ctnCommandWaits.Add($Milliseconds)
        $this.HasExited = $this.Completes
        return $this.Completes
    }
    $ctnMock | Add-Member ScriptMethod Kill {
        $script:ctnKilledCommands.Add($this.Id)
        if ($script:ctnKillThrows) { throw 'DO_NOT_LOG_KILL_SECRET' }
        $this.HasExited = $true
    }
    $ctnMock | Add-Member ScriptMethod Dispose {
        if ($script:ctnDisposeThrows) { throw 'DO_NOT_LOG_DISPOSE_SECRET' }
    }
    return $ctnMock
}
function New-Object {
    $ctnMock = [pscustomobject]@{}
    $ctnMock | Add-Member ScriptMethod WaitOne { param($Timeout) return $script:ctnMutexAvailable }
    $ctnMock | Add-Member ScriptMethod ReleaseMutex { $script:ctnMutexReleases++ }
    $ctnMock | Add-Member ScriptMethod Dispose {}
    return $ctnMock
}
function Get-CTNWatchListenerIds {
    if ($script:ctnListeners.Count -eq 0) { throw 'Unexpected listener inspection.' }
    return $script:ctnListeners.Dequeue()
}
function Invoke-RestMethod {
    param($Uri, $TimeoutSec)
    $script:ctnHealthRequests++
    if ($script:ctnHealthResults.Count -eq 0) { throw 'Unexpected health request.' }
    if (-not $script:ctnHealthResults.Dequeue()) { throw 'Mock health timeout.' }
    return [pscustomobject]@{ok=$true}
}
function Start-Process {
    param($FilePath, $WindowStyle, [switch]$PassThru, $ErrorAction, $ArgumentList,
        $WorkingDirectory, $RedirectStandardOutput, $RedirectStandardError)
    if ($FilePath -eq $script:ctnDocker) {
        $script:ctnDockerStarts.Add([pscustomobject]@{Out=$RedirectStandardOutput;Err=$RedirectStandardError;Args=$ArgumentList;WindowStyle=$WindowStyle;HealthChecks=$script:ctnHealthRequests})
        $ctnResult = if ($script:ctnDockerResults.Count) { $script:ctnDockerResults.Dequeue() }
            else { [pscustomobject]@{ExitCode=0;Output=@();Completes=$true} }
        [IO.File]::WriteAllLines($RedirectStandardOutput, [string[]]$ctnResult.Output)
        [IO.File]::WriteAllText($RedirectStandardError, '')
        return New-CTNMockProcess -Code $ctnResult.ExitCode -Completes $ctnResult.Completes
    }
    if ($script:ctnStartThrows) { throw 'DO_NOT_LOG_SECRET' }
    $script:ctnStarts.Add([pscustomobject]@{Out=$RedirectStandardOutput;Err=$RedirectStandardError;Args=$ArgumentList})
    return New-CTNMockProcess
}
function Reset-CTNMock {
    $script:ctnEvents = [Collections.Generic.List[object]]::new()
    $script:ctnStarts = [Collections.Generic.List[object]]::new()
    $script:ctnDockerStarts = [Collections.Generic.List[object]]::new()
    $script:ctnDockerResults = [Collections.Queue]::new()
    $script:ctnCommandWaits = [Collections.Generic.List[int]]::new()
    $script:ctnKilledCommands = [Collections.Generic.List[int]]::new()
    $script:ctnSleeps = [Collections.Generic.List[object]]::new()
    $script:ctnListeners = [Collections.Queue]::new()
    $script:ctnHealthResults = [Collections.Queue]::new()
    $script:ctnHealthState = $null
    $script:ctnBlockedListener = $null
    $script:ctnBackendProcess = $null
    $script:ctnStartThrows = $false
    $script:ctnNoAutoCollect = $false
    $script:ctnReceiptWrites = 0
    $script:ctnReceiptThrows = $false
    $script:ctnReceiptFailure = $false
    $script:ctnNextDependencyCheck = [datetime]::MinValue
    $script:ctnHealthRequests = 0
    $script:ctnKillThrows = $false
    $script:ctnDisposeThrows = $false
    $script:ctnMutexAvailable = $true
    $script:ctnMutexReleases = 0
    $script:ctnOwnsWatcherMutex = $false
    $script:ctnComposeReady = $true
    $script:ctnNextPostgresAttempt = [datetime]::MinValue
    $script:ctnNextProxyAttempt = [datetime]::MinValue
    $script:ctnProxyProcess = $null
    $script:ctnDocker = 'mock-docker.exe'
    $script:ctnLogDir = $ctnScratch
}

try {
    Reset-CTNMock
    $ctnHealthResults.Enqueue($true)
    . $ctnIteration
    Assert-CTNMock ($ctnStarts.Count -eq 0) 'A healthy backend must not be launched again.'
    Assert-CTNMock ($ctnEvents[0].Details -eq 'state=ready') 'Ready health event missing.'
    Assert-CTNMock ($ctnReceiptWrites -eq 1) 'Ready backends must record their verified task origin.'

    Reset-CTNMock
    $ctnReceiptThrows = $true
    $ctnHealthResults.Enqueue($true)
    . $ctnIteration
    $ctnReceiptThrows = $false
    $ctnHealthResults.Enqueue($true)
    . $ctnIteration
    Assert-CTNMock ($ctnReceiptWrites -eq 2) 'A failed origin receipt must be retried on the next healthy iteration.'
    Assert-CTNMock ($ctnStarts.Count -eq 0) 'Receipt failures must preserve a healthy backend.'
    Assert-CTNMock (($ctnEvents | ConvertTo-Json) -notmatch 'DO_NOT_LOG_RECEIPT_SECRET') 'Receipt errors must log only safe exception types.'
    Assert-CTNMock (@($ctnEvents | Where-Object Event -eq 'backend-receipt-unavailable').Count -eq 1) 'Receipt failure must be visible without interrupting the watcher.'

    Reset-CTNMock
    $ctnHealthResults.Enqueue($false)
    $ctnListeners.Enqueue(@(4321))
    . $ctnIteration
    Assert-CTNMock ($ctnStarts.Count -eq 0) 'A busy listener must block duplicate startup.'
    Assert-CTNMock ($ctnEvents[-1].Details -eq 'listener_pids=4321') 'Listener event missing.'
    $ctnHealthResults.Enqueue($false)
    $ctnListeners.Enqueue(@(4321))
    . $ctnIteration
    Assert-CTNMock ($ctnEvents.Count -eq 2) 'Unchanged health/listener state must not spam events.'

    Reset-CTNMock
    $ctnHealthResults.Enqueue($false)
    $ctnHealthResults.Enqueue($false)
    $ctnListeners.Enqueue(@())
    $ctnListeners.Enqueue(@(5678))
    . $ctnIteration
    Assert-CTNMock ($ctnStarts.Count -eq 0) 'Listener appearing under the launch mutex must block startup.'
    Assert-CTNMock ($ctnMutexReleases -eq 1) 'Launch mutex must be released.'

    Reset-CTNMock
    $ctnBackendProcess = New-CTNMockProcess
    $ctnHealthResults.Enqueue($false)
    $ctnListeners.Enqueue(@())
    . $ctnIteration
    Assert-CTNMock ($ctnStarts.Count -eq 0) 'A live startup launcher must not be duplicated.'

    Reset-CTNMock
    $ctnHealthResults.Enqueue($false)
    $ctnHealthResults.Enqueue($false)
    $ctnHealthResults.Enqueue($true)
    $ctnListeners.Enqueue(@())
    $ctnListeners.Enqueue(@())
    . $ctnIteration
    Assert-CTNMock ($ctnStarts.Count -eq 1) 'An absent backend must be recovered once.'
    Assert-CTNMock ([IO.Path]::GetFileName($ctnStarts[0].Out) -match '^backend-\d{8}-\d{6}-\d{3}-\d+\.out\.log$') 'Backend output needs a per-start log.'
    Assert-CTNMock ($ctnStarts[0].Err -ne $ctnStarts[0].Out) 'Output and error logs must be distinct.'
    Assert-CTNMock ($ctnStarts[0].Args -notcontains '-NoAutoCollect') 'Automatic collection must remain enabled without its marker.'
    Assert-CTNMock ($ctnEvents[-1].Details -eq 'state=ready') 'Recovery must record readiness.'

    Reset-CTNMock
    $ctnNoAutoCollect = $true
    $ctnHealthResults.Enqueue($false)
    $ctnHealthResults.Enqueue($false)
    $ctnHealthResults.Enqueue($true)
    $ctnListeners.Enqueue(@())
    $ctnListeners.Enqueue(@())
    . $ctnIteration
    Assert-CTNMock ($ctnStarts.Count -eq 1 -and $ctnStarts[0].Args -contains '-NoAutoCollect') 'Recovery must preserve disabled automatic collection.'

    Reset-CTNMock
    $ctnBackendProcess = New-CTNMockProcess -Exited $true -Code 17
    $ctnHealthResults.Enqueue($true)
    . $ctnIteration
    Assert-CTNMock ($ctnEvents[0].Details -eq 'launcher_pid=1234 exit_code=17') 'Launcher exit code must be preserved.'
    Assert-CTNMock ($ctnStarts.Count -eq 0) 'An externally recovered backend must not be duplicated.'

    Reset-CTNMock
    $ctnStartThrows = $true
    $ctnHealthResults.Enqueue($false)
    $ctnHealthResults.Enqueue($false)
    $ctnListeners.Enqueue(@())
    $ctnListeners.Enqueue(@())
    . $ctnIteration
    Assert-CTNMock (($ctnEvents | ConvertTo-Json) -notmatch 'DO_NOT_LOG_SECRET') 'Exception messages must not enter watcher events.'
    Assert-CTNMock ($ctnEvents[-1].Event -eq 'backend-launch-failed') 'Launch failure event missing.'
    Assert-CTNMock ($ctnSleeps -contains 15) 'Failed launch needs a recovery backoff.'

    Reset-CTNMock
    $ctnMutexAvailable = $false
    . $ctnGuard
    . $ctnCleanup
    Assert-CTNMock ($ctnEvents.Count -eq 1 -and $ctnEvents[0].Event -eq 'watcher-already-running') 'A second watcher must exit before its loop.'
    Assert-CTNMock ($ctnMutexReleases -eq 0) 'A duplicate watcher must not release a mutex it does not own.'

    Reset-CTNMock
    $ctnComposeReady = $false
    $ctnHealthResults.Enqueue($true)
    . $ctnIteration
    $ctnHealthResults.Enqueue($true)
    . $ctnIteration
    Assert-CTNMock (@($ctnEvents | Where-Object Event -eq 'postgres-start-attempt').Count -eq 1) 'Missing containers need startup backoff.'
    $ctnDockerLogs = @(Get-ChildItem -LiteralPath $ctnScratch -Filter 'docker-*.log')
    Assert-CTNMock ($ctnDockerLogs.Count -eq 1) 'Container startup must use a unique preserved log.'
    Assert-CTNMock ($ctnCommandWaits -contains 60000) 'PostgreSQL startup must have a bounded wait.'

    Reset-CTNMock
    $ctnDockerResults.Enqueue([pscustomobject]@{ExitCode=0;Output=@('postgres|running');Completes=$true})
    Assert-CTNMock (& $ctnComposeProbe) 'Running PostgreSQL must pass the bounded compose probe.'
    Assert-CTNMock ($ctnDockerStarts[0].Args -contains 'ps') 'Compose state must be inspected without starting containers.'
    Assert-CTNMock ($ctnDockerStarts[0].WindowStyle -eq 'Hidden') 'Docker probes must not open a visible window.'
    Assert-CTNMock ($ctnCommandWaits[0] -eq 15000) 'Docker probes must have a 15 second wait limit.'
    Assert-CTNMock (@(Get-ChildItem -LiteralPath $ctnScratch -Filter '.docker-probe-*').Count -eq 0) 'Probe captures must be cleaned after success.'

    Reset-CTNMock
    $ctnDockerResults.Enqueue([pscustomobject]@{ExitCode=0;Output=@('postgres|running');Completes=$true})
    Assert-CTNMock (-not (& $ctnComposeProbe -Proxy)) 'Missing proxy services must not be treated as running.'

    Reset-CTNMock
    $ctnDockerResults.Enqueue([pscustomobject]@{ExitCode=17;Output=@('postgres|running');Completes=$true})
    Assert-CTNMock (-not (& $ctnComposeProbe)) 'A failed Docker command must not look healthy.'

    Reset-CTNMock
    $ctnDockerResults.Enqueue([pscustomobject]@{ExitCode=0;Output=@();Completes=$false})
    $ctnHealthResults.Enqueue($true)
    . $ctnIteration
    Assert-CTNMock ($ctnHealthState -eq 'ready') 'A Docker timeout must not interrupt backend health checks.'
    Assert-CTNMock ($ctnKilledCommands.Count -eq 1 -and $ctnKilledCommands[0] -eq 1234) 'A timed out probe must terminate only its own process.'
    Assert-CTNMock ($ctnStarts.Count -eq 0) 'A Docker timeout must not terminate or start a backend.'
    Assert-CTNMock (@(Get-ChildItem -LiteralPath $ctnScratch -Filter '.docker-probe-*').Count -eq 0) 'Timed out probe captures must be cleaned.'
    Assert-CTNMock ($ctnDockerStarts[0].HealthChecks -eq 1) 'Backend health must be checked before Docker.'
    $ctnHealthResults.Enqueue($true)
    . $ctnIteration
    Assert-CTNMock ($ctnHealthRequests -eq 2 -and $ctnDockerStarts.Count -eq 1) 'Docker timeouts need backoff while health checks continue.'
    Assert-CTNMock (@($ctnEvents | Where-Object Event -eq 'docker-command-timeout').Count -eq 1) 'Docker timeout must be recorded safely.'

    Reset-CTNMock
    $ctnKillThrows = $true
    $ctnDisposeThrows = $true
    $ctnDockerResults.Enqueue([pscustomobject]@{ExitCode=0;Output=@();Completes=$false})
    $ctnHealthResults.Enqueue($true)
    . $ctnIteration
    $ctnHealthResults.Enqueue($true)
    . $ctnIteration
    Assert-CTNMock ($ctnHealthRequests -eq 2 -and $ctnStarts.Count -eq 0) 'Docker cleanup failures must preserve the healthy backend and watcher loop.'
    Assert-CTNMock (($ctnEvents | ConvertTo-Json) -notmatch 'DO_NOT_LOG_(KILL|DISPOSE)_SECRET') 'Docker cleanup must log only safe exception types.'

    Reset-CTNMock
    $ctnDockerResults.Enqueue([pscustomobject]@{ExitCode=0;Output=@();Completes=$false})
    $ctnHealthResults.Enqueue($false)
    $ctnHealthResults.Enqueue($false)
    $ctnHealthResults.Enqueue($true)
    $ctnListeners.Enqueue(@())
    $ctnListeners.Enqueue(@())
    . $ctnIteration
    Assert-CTNMock ($ctnStarts.Count -eq 1 -and $ctnHealthState -eq 'ready') 'Backend recovery must succeed even when Docker probing times out.'
    Assert-CTNMock ($ctnDockerStarts[0].HealthChecks -eq 3) 'An absent backend must be started before Docker probing.'
    $ctnFatalExit = $ctnOuterTry.CatchClauses[0].Body.Find({
        param($ctnNode)
        $ctnNode -is [System.Management.Automation.Language.ExitStatementAst] -and $ctnNode.Pipeline.Extent.Text -eq '1'
    }, $true)
    Assert-CTNMock ($null -ne $ctnFatalExit) 'A fatal watcher error must return a failure status for scheduled restart.'

    'Watcher mock checks passed: 17 scenarios; no live processes or tasks changed.'
} finally {
    Get-ChildItem -LiteralPath $ctnScratch -File | ForEach-Object { [IO.File]::Delete($_.FullName) }
    [IO.Directory]::Delete($ctnScratch)
}
