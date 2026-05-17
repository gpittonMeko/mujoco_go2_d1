# Clone + avvio worker grasp sul PC Windows con RTX (PowerShell).
# Prerequisiti: Git, Python 3.11+ in PATH (o launcher `py`).
#
# Uso (incolla tutto in PowerShell, oppure):
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup_rtx_grasp_worker.ps1

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/gpittonMeko/mujoco_go2_d1.git"
$Branch  = "dashboard/mission-control-cleanup"
$Parent  = Join-Path $env:USERPROFILE "source"
$RepoDir = Join-Path $Parent "mujoco_go2_d1"

Write-Host ">>> Cartella repo: $RepoDir" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $Parent | Out-Null

if (-not (Test-Path (Join-Path $RepoDir ".git"))) {
    Write-Host ">>> git clone (repo privato: serve login GitHub / token se richiesto)" -ForegroundColor Yellow
    git clone $RepoUrl $RepoDir
} else {
    Write-Host ">>> Repo già presente: git pull" -ForegroundColor Yellow
    Push-Location $RepoDir
    git fetch origin
    git checkout $Branch
    git pull origin $Branch
    Pop-Location
}

Push-Location $RepoDir
git checkout $Branch
Pop-Location

$Bootstrap = Join-Path $RepoDir "external\openvla_worker\bootstrap_worker_host.ps1"
if (-not (Test-Path $Bootstrap)) {
    Write-Error "Manca $Bootstrap — branch o repo errato?"
    exit 1
}

Write-Host ">>> Avvio bootstrap worker (venv, pip+tqdm, Flask)…" -ForegroundColor Green
powershell -NoProfile -ExecutionPolicy Bypass -File $Bootstrap
