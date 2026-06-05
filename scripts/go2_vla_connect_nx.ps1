#Requires -Version 5.1
<#
.SYNOPSIS
  Collega Jetson NX al worker VLA AWS (pairing + chiave PEM opzionale + metadata EC2).

.USAGE (dopo provision-ec2.ps1):
  powershell -ExecutionPolicy Bypass -File scripts/go2_vla_connect_nx.ps1

  # Chiave PEM sulla NX (per SSH debug verso EC2):
  powershell -ExecutionPolicy Bypass -File scripts/go2_vla_connect_nx.ps1 -InstallPemOnNx
#>
param(
    [string]$PairingFile = "",
    [string]$KeyPath = "$env:USERPROFILE\Documents\LLM_14.pem",
    [string]$StatePath = "",
    [switch]$InstallPemOnNx,
    [switch]$InstallEc2ControlOnNx,
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent
if (-not $PairingFile) { $PairingFile = Join-Path $RepoRoot "go2-vla-pairing.env" }
if (-not $StatePath) { $StatePath = Join-Path $RepoRoot "data/go2_vla_ec2_state.json" }

if (-not (Test-Path $PairingFile)) {
    throw "Manca $PairingFile - lancia prima: external/openvla_worker/aws/provision-ec2.ps1"
}

$pairArgs = @(
    "scripts/pair_nx_aws_vla.py",
    "--pairing-file", $PairingFile
)
if (-not $SkipVerify) { $pairArgs += "--verify" }
if ($InstallPemOnNx -or $InstallEc2ControlOnNx) { $pairArgs += "--install-pem"; $pairArgs += $KeyPath }
if ($InstallEc2ControlOnNx) { $pairArgs += "--install-ec2-control"; $pairArgs += "--state-file"; $pairArgs += $StatePath }

Write-Host "[connect] pair NX -> AWS..."
Push-Location $RepoRoot
python @pairArgs
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { exit $code }

Write-Host ""
Write-Host "NX collegata. Dashboard: http://192.168.123.18:5052 -> tab Presa -> Piano VLA" -ForegroundColor Green
Write-Host ""
Write-Host "Controllo EC2 da PC:"
Write-Host "  python scripts/aws_vla_ec2_control.py status"
Write-Host "  python scripts/aws_vla_ec2_control.py stop"
Write-Host "  python scripts/aws_vla_ec2_control.py start --wait-health"
if ($InstallEc2ControlOnNx) {
    Write-Host ""
    Write-Host "Controllo EC2 dalla NX (serve AWS_ACCESS_KEY_ID/SECRET in nx_secrets_dashboard.sh):"
    Write-Host "  ssh unitree@192.168.123.18 'cd go2_visual_dashboard && python3 scripts/aws_vla_ec2_control.py status'"
}
