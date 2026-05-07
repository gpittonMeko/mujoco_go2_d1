#Requires -RunAsAdministrator
<#
  Lascia solo HP OfficeJet Pro 9120e, svuota lo spooler, imposta default e ottimizza dialoghi stampa.
  Windows può ripristinare stampanti WSD se restano sulla LAN: spegni la ENVY o scollegal dalla Wi‑Fi.

  Esecuzione: tasto destro PowerShell -> Esegui come amministratore, poi:
  cd 'c:\Users\user\MujocoCaneD1\mujoco_go2_d1\scripts'
  Set-ExecutionPolicy -Scope Process Bypass -Force
  .\fix_windows_printing_admin.ps1
#>

$ErrorActionPreference = 'Continue'

function Remove-PrinterSafe([string]$Name) {
    if (-not (Get-Printer -Name $Name -ErrorAction SilentlyContinue)) { return }
    Remove-Printer -Name $Name -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 400
    $escaped = $Name -replace '"', '\"'
    Start-Process -FilePath 'rundll32.exe' -ArgumentList @('printui.dll,PrintUIEntry', '/dn', '/n', "`"$escaped`"") -Wait -WindowStyle Hidden
}

Write-Host '=== Arresto Spooler e svuotamento coda ===' -ForegroundColor Cyan
Stop-Service Spooler -Force -ErrorAction Stop
Start-Sleep -Seconds 2

$spool = Join-Path $env:SystemRoot 'System32\spool\PRINTERS'
if (Test-Path $spool) {
    Get-ChildItem $spool -Force -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
}

Write-Host '=== Rimozione tutte le stampanti tranne HP OfficeJet Pro 9120e ===' -ForegroundColor Cyan
$keepers = @(
    'HP OfficeJet Pro 9120e Series [39A50A]'
)
$all = @(Get-Printer -ErrorAction SilentlyContinue)
foreach ($p in $all) {
    if ($keepers -contains $p.Name) { continue }
    Write-Host "Rimuovo: $($p.Name)"
    Remove-PrinterSafe -Name $p.Name
}

Write-Host '=== Avvio Spooler ===' -ForegroundColor Cyan
Start-Service Spooler -ErrorAction Stop
Start-Sleep -Seconds 2

Write-Host '=== Rimozione porte WSD non usate (ENVY / fantasma) ===' -ForegroundColor Cyan
$used = @(Get-Printer | Select-Object -ExpandProperty PortName)
Get-PrinterPort -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -like 'WSD-*' -and ($used -notcontains $_.Name)
} | ForEach-Object {
    try {
        Remove-PrinterPort -Name $_.Name -ErrorAction Stop
        Write-Host "Porta rimossa: $($_.Name)"
    } catch {
        Write-Host "Porta non rimossa (ancora in uso?): $($_.Name)"
    }
}

Write-Host '=== Stampante predefinita = OfficeJet 9120e ===' -ForegroundColor Cyan
$hp = Get-CimInstance Win32_Printer -ErrorAction SilentlyContinue | Where-Object { $_.Name -like '*OfficeJet Pro 9120e*' } | Select-Object -First 1
if ($hp) {
    $null = $hp.InvokeMethod('SetDefaultPrinter', @())
    Write-Host "Default impostata su: $($hp.Name)"
} else {
    Write-Host 'ATTENZIONE: stampante OfficeJet 9120e non trovata dopo la pulizia.'
}

Write-Host '=== Registry utente: niente cambio automatico default ===' -ForegroundColor Cyan
$winKey = 'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Windows'
if (Test-Path $winKey) {
    New-ItemProperty -Path $winKey -Name LegacyDefaultPrinterMode -PropertyType DWord -Value 1 -Force | Out-Null
}

Write-Host '=== Registry utente: dialogo stampa legacy (meno scan WSD infinito) ===' -ForegroundColor Cyan
$upd = 'HKCU:\Software\Microsoft\Print\UnifiedPrintDialog'
if (-not (Test-Path $upd)) { New-Item -Path $upd -Force | Out-Null }
New-ItemProperty -Path $upd -Name PreferLegacyPrintDialog -PropertyType DWord -Value 1 -Force | Out-Null

Write-Host '=== Chrome (machine): disabilita anteprima integrata ===' -ForegroundColor Cyan
$chromePolicy = 'HKLM:\SOFTWARE\Policies\Google\Chrome'
if (-not (Test-Path $chromePolicy)) { New-Item -Path $chromePolicy -Force | Out-Null }
New-ItemProperty -Path $chromePolicy -Name DisablePrintPreview -PropertyType DWord -Value 1 -Force | Out-Null

Write-Host ''
Write-Host 'Elenco finale:' -ForegroundColor Green
Get-Printer | Format-Table Name, PrinterStatus, DriverName, PortName -AutoSize

Write-Host ''
Write-Host 'Fatto. Chiudi Chrome dalla tray e riaprilo. Riavvia Excel se era aperto.' -ForegroundColor Green
Write-Host 'Se la ENVY torna da sola: spegnila o toglila dalla Wi‑Fi (Windows la ridiscovered).' -ForegroundColor Yellow
