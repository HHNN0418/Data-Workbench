param(
    [ValidateSet('prepare','migrate','restore')][string]$Mode = 'migrate',
    [string]$Backup = ''
)
$ErrorActionPreference = 'Stop'
$ctnSecretPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $ctnSecretPython)) { throw 'Run Setup-CTN.ps1 first.' }
$ctnSecretArguments = @('-m','ctn_data.secret_files',$Mode,'--root',$PSScriptRoot)
if ($Backup) { $ctnSecretArguments += @('--backup',$Backup) }
Push-Location -LiteralPath $PSScriptRoot
try {
    & $ctnSecretPython @ctnSecretArguments
    if ($LASTEXITCODE -ne 0) { throw 'Credential preparation failed; no services were started.' }
} finally { Pop-Location }
