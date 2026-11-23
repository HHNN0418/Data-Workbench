$ErrorActionPreference = 'Stop'
$ctnSourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ctnRuntimePath = Join-Path $ctnSourceRoot 'scripts\CTN-TaskRuntime.ps1'
. $ctnRuntimePath
$ctnTestParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$ctnProjectRoot = Join-Path $ctnTestParent ('ctn-task-runtime-mock-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory((Join-Path $ctnProjectRoot 'data')) | Out-Null

function Assert-CTNTaskMock([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function New-CTNTaskMockProcess(
    [int]$ProcessId,
    [int]$ParentProcessId,
    [string]$Name,
    [string]$ExecutablePath,
    [string]$CommandLine,
    [int]$CreatedMinutes
) {
    return [pscustomobject]@{
        ProcessId = $ProcessId
        ParentProcessId = $ParentProcessId
        Name = $Name
        ExecutablePath = $ExecutablePath
        CommandLine = $CommandLine
        CreationDate = ([datetime]'2026-10-21T00:00:00Z').AddMinutes($CreatedMinutes)
    }
}

function Reset-CTNTaskMock {
    Get-ChildItem -LiteralPath (Join-Path $ctnProjectRoot 'data') -File | ForEach-Object {
        [IO.File]::Delete($_.FullName)
    }
    $script:ctnMockProcesses = @{}
    $script:ctnMockTaskState = 'Running'
    $script:ctnMockTaskStates = [Collections.Queue]::new()
    $script:ctnMockTaskReads = 0
    $script:ctnMockSleeps = 0
    $script:ctnMockSchedulePid = 100
    $script:ctnMockListenerPids = @(500)
    $ctnPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $ctnWatchScript = Join-Path $ctnProjectRoot 'Watch-CTN.ps1'
    $script:ctnMockProcesses[100] = New-CTNTaskMockProcess 100 4 'svchost.exe' `
        (Join-Path $env:SystemRoot 'System32\svchost.exe') 'svchost.exe -k netsvcs -p -s Schedule' 0
    $script:ctnMockProcesses[200] = New-CTNTaskMockProcess 200 100 'powershell.exe' `
        $ctnPowerShell ('powershell.exe -NoProfile -NonInteractive -File "' + $ctnWatchScript + '"') 1
    $script:ctnMockProcesses[300] = New-CTNTaskMockProcess 300 200 'powershell.exe' `
        $ctnPowerShell ('powershell.exe -NoProfile -File "' + (Join-Path $ctnProjectRoot 'Start-CTN.ps1') + '"') 2
    $script:ctnMockProcesses[400] = New-CTNTaskMockProcess 400 300 'python.exe' `
        (Join-Path $ctnProjectRoot '.venv\Scripts\python.exe') 'python.exe -m ctn_data serve' 3
    $script:ctnMockProcesses[500] = New-CTNTaskMockProcess 500 400 'python.exe' `
        (Join-Path $ctnProjectRoot '.venv\Scripts\python.exe') 'python.exe -m ctn_data serve' 4
}

function Get-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    if ($TaskName -ne 'CTN-AutoStart') { throw 'Unexpected task lookup.' }
    $script:ctnMockTaskReads++
    $ctnState = $script:ctnMockTaskState
    if ($script:ctnMockTaskStates.Count) { $ctnState = $script:ctnMockTaskStates.Dequeue() }
    if ($null -eq $ctnState) { return $null }
    return [pscustomobject]@{TaskName = 'CTN-AutoStart'; State = $ctnState}
}

function Get-CimInstance {
    param($ClassName, $Filter, $ErrorAction)
    if ($ClassName -eq 'Win32_Service') {
        return [pscustomobject]@{Name = 'Schedule'; ProcessId = $script:ctnMockSchedulePid}
    }
    if ($ClassName -ne 'Win32_Process') { throw 'Unexpected CIM class.' }
    if ($Filter -match '(?i)ProcessId\s*=\s*(\d+)') {
        return $script:ctnMockProcesses[[int]$Matches[1]]
    }
    if ($Filter -and $Filter -notmatch '(?i)Name\s*=\s*[''"]powershell\.exe[''"]') {
        throw 'Unexpected process filter.'
    }
    return @($script:ctnMockProcesses.Values | Where-Object Name -eq 'powershell.exe')
}

function Start-Sleep {
    param($Seconds, $Milliseconds)
    $script:ctnMockSleeps++
}

function Get-NetTCPConnection {
    param($LocalPort, $State, $ErrorAction)
    if ($LocalPort -ne 8765 -or $State -ne 'Listen') { throw 'Unexpected socket lookup.' }
    foreach ($ctnMockListenerPid in $script:ctnMockListenerPids) {
        [pscustomobject]@{OwningProcess = $ctnMockListenerPid}
    }
}

function Start-Process { throw 'Task ownership checks must never start processes.' }
function Stop-Process { throw 'Task ownership checks must never stop processes.' }
function Start-ScheduledTask { throw 'Task ownership checks must never start tasks.' }
function Stop-ScheduledTask { throw 'Task ownership checks must never stop tasks.' }

function New-CTNReceiptTakeoverMock {
    Reset-CTNTaskMock
    $ctnReceiptWritten = Write-CTNTaskBackendReceipt -Root $ctnProjectRoot `
        -OriginWatcher $ctnMockProcesses[200]
    Assert-CTNTaskMock ($ctnReceiptWritten -eq $true) 'The original task-owned backend needs a receipt.'
    $ctnNewWatcher = $ctnMockProcesses[200].PSObject.Copy()
    $ctnNewWatcher.ProcessId = 201
    $ctnNewWatcher.CreationDate = $ctnNewWatcher.CreationDate.AddMinutes(5)
    $ctnMockProcesses.Remove(200)
    $ctnMockProcesses[201] = $ctnNewWatcher
}

function Assert-CTNReceiptFieldRejected([scriptblock]$Mutate, [string]$Message) {
    New-CTNReceiptTakeoverMock
    $ctnReceiptPath = Join-Path $ctnProjectRoot 'data\ctn-task-backend.json'
    $ctnReceipt = Get-Content -LiteralPath $ctnReceiptPath -Raw | ConvertFrom-Json
    & $Mutate $ctnReceipt
    $ctnReceipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ctnReceiptPath -Encoding UTF8
    Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
        $Message
}

try {
Reset-CTNTaskMock
$ctnWatcher = Get-CTNTaskWatcher -Root $ctnProjectRoot
Assert-CTNTaskMock ($null -ne $ctnWatcher -and $ctnWatcher.ProcessId -eq 200) `
    'The Schedule service ancestry must identify the expected watcher.'
$ctnOwner = Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500]
Assert-CTNTaskMock ($null -ne $ctnOwner) 'The complete task-owned backend chain must be accepted.'

Reset-CTNTaskMock
$ctnMockTaskState = 'Ready'
Assert-CTNTaskMock ($null -eq (Get-CTNTaskWatcher -Root $ctnProjectRoot)) `
    'A Ready task must not establish ownership of a watcher.'
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'A Ready task must not establish ownership of a backend.'

Reset-CTNTaskMock
$ctnMockTaskState = $null
Assert-CTNTaskMock ($null -eq (Get-CTNTaskWatcher -Root $ctnProjectRoot)) `
    'An absent task must not establish ownership.'

Reset-CTNTaskMock
$ctnMockProcesses[200].ParentProcessId = 900
$ctnMockProcesses[900] = New-CTNTaskMockProcess 900 4 'Codex.exe' `
    'C:\Mock\Codex.exe' 'Codex.exe' 0
Assert-CTNTaskMock ($null -eq (Get-CTNTaskWatcher -Root $ctnProjectRoot)) `
    'A watcher launched by Codex must not count as task-owned.'
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'A backend launched through a Codex-owned watcher must not count as task-owned.'

Reset-CTNTaskMock
$ctnMockProcesses[200].CommandLine = 'powershell.exe -NoProfile -File "C:\Other\Watch-CTN.ps1"'
Assert-CTNTaskMock ($null -eq (Get-CTNTaskWatcher -Root $ctnProjectRoot)) `
    'A watcher for another project must not count as owned.'

Reset-CTNTaskMock
$ctnMockProcesses[200].ExecutablePath = 'C:\Other\powershell.exe'
Assert-CTNTaskMock ($null -eq (Get-CTNTaskWatcher -Root $ctnProjectRoot)) `
    'A different PowerShell executable must not count as owned.'

Reset-CTNTaskMock
$ctnMockProcesses[200].CommandLine = 'powershell.exe -NoProfile -Command "' + (Join-Path $ctnProjectRoot 'Watch-CTN.ps1') + '"'
Assert-CTNTaskMock ($null -eq (Get-CTNTaskWatcher -Root $ctnProjectRoot)) `
    'A command mentioning the watcher without invoking its file must not count as owned.'

Reset-CTNTaskMock
$ctnMockProcesses[100].CreationDate = $ctnMockProcesses[200].CreationDate.AddMinutes(1)
Assert-CTNTaskMock ($null -eq (Get-CTNTaskWatcher -Root $ctnProjectRoot)) `
    'A reused Schedule service PID must not establish watcher ownership.'

Reset-CTNTaskMock
$ctnMockProcesses[300].CreationDate = $ctnMockProcesses[400].CreationDate.AddMinutes(1)
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'A reused intermediate parent PID must not establish backend ownership.'

Reset-CTNTaskMock
$ctnStaleBackend = $ctnMockProcesses[500].PSObject.Copy()
$ctnStaleBackend.CreationDate = $ctnStaleBackend.CreationDate.AddMinutes(-1)
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnStaleBackend)) `
    'A stale backend process snapshot must not establish ownership.'

Reset-CTNTaskMock
$ctnMockProcesses[500].CreationDate = $null
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'A backend without a creation time must not establish ownership.'

Reset-CTNTaskMock
$ctnMockProcesses[500].ParentProcessId = 900
$ctnMockProcesses[900] = New-CTNTaskMockProcess 900 4 'Codex.exe' `
    'C:\Mock\Codex.exe' 'Codex.exe' 0
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'A separate task-owned watcher must not legitimize a backend launched by Codex.'

Reset-CTNTaskMock
$ctnMockProcesses[201] = $ctnMockProcesses[200].PSObject.Copy()
$ctnMockProcesses[201].ProcessId = 201
$ctnAmbiguous = $false
try { $null = Get-CTNTaskWatcher -Root $ctnProjectRoot } catch { $ctnAmbiguous = $true }
Assert-CTNTaskMock $ctnAmbiguous 'Multiple task-owned watchers must fail explicitly.'

Reset-CTNTaskMock
$ctnMockTaskStates.Enqueue('Ready')
$ctnMockTaskStates.Enqueue('Running')
$ctnWatcher = Wait-CTNTaskWatcher -Root $ctnProjectRoot
Assert-CTNTaskMock ($null -ne $ctnWatcher -and $ctnWatcher.ProcessId -eq 200) `
    'Watcher polling must return the verified watcher after the task starts.'
Assert-CTNTaskMock ($ctnMockTaskReads -ge 2 -and $ctnMockSleeps -ge 1) `
    'Watcher polling must wait and recheck an initially Ready task.'

Reset-CTNTaskMock
$ctnMockTaskState = 'Ready'
$ctnTimedOut = $false
try { $null = Wait-CTNTaskWatcher -Root $ctnProjectRoot -TimeoutSeconds 0 } catch { $ctnTimedOut = $true }
Assert-CTNTaskMock ($ctnTimedOut -and $ctnMockTaskReads -eq 1) `
    'An immediate timeout must inspect once, then fail without accepting an unowned watcher.'

Reset-CTNTaskMock
$ctnReceiptWritten = Write-CTNTaskBackendReceipt -Root $ctnProjectRoot -OriginWatcher $ctnMockProcesses[200]
$ctnReceiptPath = Join-Path $ctnProjectRoot 'data\ctn-task-backend.json'
Assert-CTNTaskMock ($ctnReceiptWritten -eq $true -and (Test-Path -LiteralPath $ctnReceiptPath)) `
    'A verified task-owned backend must produce a receipt inside the temporary project.'
$ctnReceipt = Get-Content -LiteralPath $ctnReceiptPath -Raw | ConvertFrom-Json
Assert-CTNTaskMock ($ctnReceipt.schema_version -eq 1 -and $ctnReceipt.backend_pid -eq 500 -and
    $ctnReceipt.launcher_pid -eq 300 -and $ctnReceipt.origin_watcher_pid -eq 200 -and
    $ctnReceipt.scheduler_pid -eq 100 -and $ctnReceipt.task_name -eq 'CTN-AutoStart' -and
    $ctnReceipt.project_root -eq $ctnProjectRoot) 'The receipt must bind the complete original launch source.'

New-CTNReceiptTakeoverMock
$ctnOwner = Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500]
Assert-CTNTaskMock ($null -ne $ctnOwner -and $ctnOwner.ProcessId -eq 201) `
    'A new task-owned watcher must adopt a live backend with a valid launch receipt.'

$ctnReceiptPath = Join-Path $ctnProjectRoot 'data\ctn-task-backend.json'
$ctnOriginalReceipt = Get-Content -LiteralPath $ctnReceiptPath -Raw
$ctnReceiptWritten = Write-CTNTaskBackendReceipt -Root $ctnProjectRoot -OriginWatcher $ctnMockProcesses[201]
Assert-CTNTaskMock ($ctnReceiptWritten -eq $true -and
    (Get-Content -LiteralPath $ctnReceiptPath -Raw) -eq $ctnOriginalReceipt) `
    'Adopting a valid receipt must preserve its original launch source.'

Assert-CTNReceiptFieldRejected { param($Receipt) $Receipt.project_root = 'C:\Other\CTN' } `
    'A receipt for another project must not establish ownership.'
Assert-CTNReceiptFieldRejected { param($Receipt) $Receipt.task_name = 'Other-Task' } `
    'A receipt for another task must not establish ownership.'
Assert-CTNReceiptFieldRejected { param($Receipt) $Receipt.backend_pid = 501 } `
    'A receipt for another backend PID must not establish ownership.'
Assert-CTNReceiptFieldRejected { param($Receipt) $Receipt.backend_created_utc = '2026-10-21T00:05:00.0000000Z' } `
    'A receipt for another backend creation time must not establish ownership.'
Assert-CTNReceiptFieldRejected { param($Receipt) $Receipt.launcher_pid = 301 } `
    'A receipt for another launcher PID must not establish ownership.'
Assert-CTNReceiptFieldRejected { param($Receipt) $Receipt.launcher_created_utc = '2026-10-21T00:03:00.0000000Z' } `
    'A receipt for another launcher creation time must not establish ownership.'

New-CTNReceiptTakeoverMock
$ctnMockProcesses[300].ExecutablePath = 'C:\Other\powershell.exe'
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'A receipt must not accept a launcher running a different executable.'

New-CTNReceiptTakeoverMock
$ctnMockProcesses[300].CommandLine = 'powershell.exe -NoProfile -File "C:\Other\Start-CTN.ps1"'
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'A receipt must not accept a launcher running a different project script.'

New-CTNReceiptTakeoverMock
$ctnMockProcesses[500].ParentProcessId = 900
$ctnMockProcesses[900] = New-CTNTaskMockProcess 900 4 'Codex.exe' 'C:\Mock\Codex.exe' 'Codex.exe' 0
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'Matching receipt fields cannot replace the actual backend-to-launcher ancestry.'

New-CTNReceiptTakeoverMock
$ctnMockProcesses[500].CreationDate = $ctnMockProcesses[500].CreationDate.AddMinutes(7)
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'A reused backend PID must not inherit a historical launch receipt.'

New-CTNReceiptTakeoverMock
$ctnMockProcesses[300].CreationDate = $ctnMockProcesses[300].CreationDate.AddMinutes(7)
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'A reused launcher PID must not inherit a historical launch receipt.'

Reset-CTNTaskMock
$ctnMockProcesses[200].ParentProcessId = 900
$ctnMockProcesses[900] = New-CTNTaskMockProcess 900 4 'Codex.exe' 'C:\Mock\Codex.exe' 'Codex.exe' 0
$ctnUnownedWriteRejected = $false
try { $null = Write-CTNTaskBackendReceipt -Root $ctnProjectRoot } catch { $ctnUnownedWriteRejected = $true }
Assert-CTNTaskMock ($ctnUnownedWriteRejected -and
    -not (Test-Path -LiteralPath (Join-Path $ctnProjectRoot 'data\ctn-task-backend.json'))) `
    'A manually launched Codex backend must not mint a task-owned launch receipt.'

Reset-CTNTaskMock
$ctnWrongOrigin = $ctnMockProcesses[200].PSObject.Copy()
$ctnWrongOrigin.ProcessId = 201
$ctnOriginRejected = $false
try { $null = Write-CTNTaskBackendReceipt -Root $ctnProjectRoot -OriginWatcher $ctnWrongOrigin } catch { $ctnOriginRejected = $true }
Assert-CTNTaskMock $ctnOriginRejected 'Writing a receipt must reject a mismatched origin watcher PID.'

Reset-CTNTaskMock
$ctnWrongOrigin = $ctnMockProcesses[200].PSObject.Copy()
$ctnWrongOrigin.CreationDate = $ctnWrongOrigin.CreationDate.AddMinutes(-1)
$ctnOriginRejected = $false
try { $null = Write-CTNTaskBackendReceipt -Root $ctnProjectRoot -OriginWatcher $ctnWrongOrigin } catch { $ctnOriginRejected = $true }
Assert-CTNTaskMock $ctnOriginRejected 'Writing a receipt must reject a stale origin watcher creation time.'

Reset-CTNTaskMock
$ctnMockListenerPids = @()
$ctnListenerRejected = $false
try { $null = Write-CTNTaskBackendReceipt -Root $ctnProjectRoot } catch { $ctnListenerRejected = $true }
Assert-CTNTaskMock $ctnListenerRejected 'Writing a receipt must reject an absent backend listener.'

Reset-CTNTaskMock
$ctnMockListenerPids = @(500, 501)
$ctnListenerRejected = $false
try { $null = Write-CTNTaskBackendReceipt -Root $ctnProjectRoot } catch { $ctnListenerRejected = $true }
Assert-CTNTaskMock $ctnListenerRejected 'Writing a receipt must reject ambiguous backend listeners.'

New-CTNReceiptTakeoverMock
$ctnMockTaskState = 'Ready'
Assert-CTNTaskMock ($null -eq (Get-CTNTaskBackendOwner -Root $ctnProjectRoot -Process $ctnMockProcesses[500])) `
    'A historical receipt must still require a current verified task-owned watcher.'

'Task runtime mock checks passed: 35 scenarios; no live processes or tasks changed.'
} finally {
    $ctnCleanupPath = [IO.Path]::GetFullPath($ctnProjectRoot)
    $ctnCleanupParent = [IO.Path]::GetFullPath((Split-Path -Parent $ctnCleanupPath))
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals(
            $ctnCleanupParent.TrimEnd('\'), $ctnTestParent.TrimEnd('\')) -or
            [IO.Path]::GetFileName($ctnCleanupPath) -notmatch '^ctn-task-runtime-mock-[a-f0-9]{32}$') {
        throw 'Refusing to clean a directory outside the task runtime test root.'
    }
    Remove-Item -LiteralPath $ctnCleanupPath -Recurse -Force
}
