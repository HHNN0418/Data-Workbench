param([int]$Port = 8765, [string]$Database = 'data/ctn.sqlite3', [switch]$NoAutoCollect)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$ctnPythonCandidates = @(
    $env:CTN_PYTHON,
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
$ctnPython = $ctnPythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $ctnPython) { $ctnPython = (Get-Command python -ErrorAction Stop).Source }
& $ctnPython -c "import fastapi, uvicorn, tzdata"
if ($LASTEXITCODE -ne 0) { throw 'Dependencies missing. Run .\Setup-CTN.ps1 first.' }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend\dist\index.html'))) { throw 'Frontend not built. Run .\Setup-CTN.ps1 first.' }
$ctnPgMarker = Join-Path $PSScriptRoot 'data\postgres-active'
if ((Test-Path -LiteralPath $ctnPgMarker) -and -not $PSBoundParameters.ContainsKey('Database')) {
    $ctnServeArgs = @('-X', 'utf8', '-m', 'ctn_data', '--db-secret-file',
        (Join-Path $PSScriptRoot 'data\secrets\postgres_url'), 'serve', '--port', $Port)
} else {
    $ctnServeArgs = @('-X', 'utf8', '-m', 'ctn_data', '--db', $Database, 'serve', '--port', $Port)
}
if (-not $NoAutoCollect) { $ctnServeArgs += '--auto-collect' }
& $ctnPython @ctnServeArgs
exit $LASTEXITCODE
