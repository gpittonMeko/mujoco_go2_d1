# Launcher: richiede elevazione e esegue il fix stampa (spooler + solo 9120e + Chrome policy).
$script = Join-Path $PSScriptRoot 'fix_windows_printing_admin.ps1'
if (-not (Test-Path $script)) {
    Write-Error "Script non trovato: $script"
    exit 1
}
$p = Start-Process -FilePath powershell.exe -Verb RunAs -ArgumentList @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', $script
) -Wait -PassThru
exit $p.ExitCode
