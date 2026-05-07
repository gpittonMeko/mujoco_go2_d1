# Verifica rapida signaling WebRTC su host tipici LAN Unitree 192.168.123.x
# (dalla root del repo). Richiede Python e unitree-webrtc-connect.
# Riferimenti in repo: scripts/nx_go2_sta_and_dds_troubleshoot.txt
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$candidates = @(
    "192.168.123.161",  # computer di bordo Go2 (Quick Start SDK Unitree)
    "192.168.123.1",
    "192.168.123.18",
    "192.168.123.20"
)

Write-Host "=== Probe TCP 9991 / 8081 (signaling WebRTC) ===" -ForegroundColor Cyan
foreach ($ip in $candidates) {
    Write-Host "`n--- $ip ---" -ForegroundColor Yellow
    & python scripts/pc_go2_webrtc_crouch.py --probe --ip $ip
}

Write-Host "`n=== Crouch: usa l'IP dove almeno una porta era OK ===" -ForegroundColor Cyan
Write-Host "  python scripts/pc_go2_webrtc_crouch.py --ip 192.168.123.161"
Write-Host "oppure (gateway automatico Wi-Fi su Windows):"
Write-Host "  python scripts/pc_go2_webrtc_crouch.py --ip auto"
