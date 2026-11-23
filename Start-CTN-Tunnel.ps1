param(
    [string]$PublicUrl = '',
    [ValidateRange(1, 65535)]
    [int]$Port = 8766,
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8765
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not $PublicUrl) {
    $ctnConfigPath = Join-Path $PSScriptRoot 'data\tunnel-config.json'
    if (-not (Test-Path -LiteralPath $ctnConfigPath)) {
        throw 'Specify -PublicUrl https://ctn.example.com or run .\Install-CTN-TunnelAutostart.ps1 first.'
    }
    $ctnSavedConfig = Get-Content -LiteralPath $ctnConfigPath -Raw | ConvertFrom-Json
    $PublicUrl = [string]$ctnSavedConfig.public_url
    if (-not $PSBoundParameters.ContainsKey('Port')) { $Port = [int]$ctnSavedConfig.port }
    if (-not $PSBoundParameters.ContainsKey('BackendPort')) { $BackendPort = [int]$ctnSavedConfig.backend_port }
}

$ctnPythonCandidates = @(
    $env:CTN_PYTHON,
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
$ctnPython = $ctnPythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $ctnPython) { $ctnPython = (Get-Command python -ErrorAction Stop).Source }

$ctnProxyArgs = @(
    '-X', 'utf8', '-m', 'ctn_data.tunnel_proxy',
    '--public-url', $PublicUrl,
    '--port', $Port,
    '--backend-port', $BackendPort
)
& $ctnPython @ctnProxyArgs
exit $LASTEXITCODE
