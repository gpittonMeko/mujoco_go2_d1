# Verifica dashboard NX — funziona SEMPRE (doppio click o da qualsiasi cartella).
# Uso:  .\lab_check.ps1
#       .\lab_check.ps1 http://192.168.123.18:5050
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Url = if ($args.Count -gt 0) { $args[0] } else { "http://192.168.123.18:5052" }
Set-Location $Root
Write-Host "Repo: $Root"
Write-Host "URL:  $Url"
Write-Host ""
python "$Root\scripts\verify_dashboard_http.py" $Url
exit $LASTEXITCODE
