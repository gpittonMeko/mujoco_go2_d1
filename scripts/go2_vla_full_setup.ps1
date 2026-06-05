#Requires -Version 5.1
<#
.SYNOPSIS
  Setup completo: crea EC2 + collega NX (un solo comando dal PC).

.USAGE:
  powershell -ExecutionPolicy Bypass -File scripts/go2_vla_full_setup.ps1

  # Con chiave PEM sulla NX + controllo EC2 dalla NX:
  powershell -ExecutionPolicy Bypass -File scripts/go2_vla_full_setup.ps1 -InstallPemOnNx -InstallEc2ControlOnNx

  # Solo collega NX (EC2 già pronta):
  powershell -ExecutionPolicy Bypass -File scripts/go2_vla_full_setup.ps1 -SkipProvision

Env AWS (se non configurato):
  $env:AWS_ACCESS_KEY_ID = "..."
  $env:AWS_SECRET_ACCESS_KEY = "..."
  $env:AWS_DEFAULT_REGION = "eu-west-1"
#>
param(
    [string]$KeyPath = "$env:USERPROFILE\Documents\LLM_14.pem",
    [string]$KeyName = "LLM_14",
    [string]$Region = $(if ($env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION } else { "eu-north-1" }),
    [switch]$SkipProvision,
    [switch]$InstallPemOnNx,
    [switch]$InstallEc2ControlOnNx
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent

Push-Location $RepoRoot
try {
    if (-not $SkipProvision) {
        Write-Host "=== FASE 1: Provision EC2 ===" -ForegroundColor Cyan
        & powershell -ExecutionPolicy Bypass -File "external/openvla_worker/aws/provision-ec2.ps1" `
            -KeyPath $KeyPath -KeyName $KeyName -Region $Region -DisableSslVerify
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } else {
        Write-Host "=== Skip provision (EC2 esistente) ===" -ForegroundColor Yellow
    }

    Write-Host "=== FASE 2: Collega NX ===" -ForegroundColor Cyan
    $connectArgs = @(
        "-ExecutionPolicy", "Bypass",
        "-File", "scripts/go2_vla_connect_nx.ps1",
        "-KeyPath", $KeyPath
    )
    if ($InstallPemOnNx) { $connectArgs += "-InstallPemOnNx" }
    if ($InstallEc2ControlOnNx) { $connectArgs += "-InstallEc2ControlOnNx" }
    & powershell @connectArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
