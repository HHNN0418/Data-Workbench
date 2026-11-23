param([switch]$BuildOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$ctnRuntime = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies'
$ctnBasePython = $env:CTN_PYTHON
if (-not $ctnBasePython) {
    $ctnBasePython = Join-Path $ctnRuntime 'python\python.exe'
    if (-not (Test-Path -LiteralPath $ctnBasePython)) { $ctnBasePython = (Get-Command python -ErrorAction Stop).Source }
}
if (-not $BuildOnly) {
    & $ctnBasePython -m venv --without-pip .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
    & $ctnBasePython -m pip --python .venv/Scripts/python.exe install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
}
$ctnNode = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $ctnNode) { $ctnNode = Join-Path $ctnRuntime 'node\bin\node.exe' }
$ctnPnpm = Get-Command pnpm -ErrorAction SilentlyContinue
$ctnBundledPnpm = Join-Path $ctnRuntime 'node\node_modules\pnpm\bin\pnpm.cjs'
$env:PATH = (Split-Path -Parent $ctnNode) + [IO.Path]::PathSeparator + $env:PATH
Push-Location frontend
try {
    if ($ctnPnpm) { & $ctnPnpm.Source install --frozen-lockfile }
    elseif (Test-Path -LiteralPath $ctnBundledPnpm) { & $ctnNode $ctnBundledPnpm install --frozen-lockfile }
    else { throw 'pnpm is required. Install pnpm 11 with npm install -g pnpm@11.' }
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    & $ctnNode node_modules/typescript/bin/tsc --noEmit
    if ($LASTEXITCODE -ne 0) { throw 'TypeScript check failed.' }
    & $ctnNode node_modules/vite/bin/vite.js build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
Write-Host 'Ready. Run .\Start-CTN.ps1'
