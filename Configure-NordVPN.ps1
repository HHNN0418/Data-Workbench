$ErrorActionPreference = 'Stop'
$ctnSecrets = Join-Path $PSScriptRoot 'data\secrets'
New-Item -ItemType Directory -Path $ctnSecrets -Force | Out-Null
Write-Host 'Enter NordVPN SERVICE credentials from Nord Account > Manual setup. Not your email/password.'
foreach ($ctnField in @('nord_username','nord_password')) {
    $ctnSecure = Read-Host $ctnField -AsSecureString
    $ctnPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ctnSecure)
    try {
        $ctnValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ctnPointer)
        if ([string]::IsNullOrWhiteSpace($ctnValue)) { throw 'Value must not be empty.' }
        Push-Location -LiteralPath $PSScriptRoot
        try {
            $ctnValue | & (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -m ctn_data.secret_files set --root $PSScriptRoot --name $ctnField
            if ($LASTEXITCODE -ne 0) { throw 'Unable to save encrypted credentials.' }
        } finally { Pop-Location }
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ctnPointer)
        $ctnValue = $null
    }
}
& (Join-Path $PSScriptRoot 'Protect-CTNSecrets.ps1') -Mode prepare
Write-Host 'Saved locally. Do not paste credentials into chat.'
