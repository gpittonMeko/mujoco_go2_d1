# Avvio worker grasp su Windows — **nessun monolite**; piano da ``scripts/box_grasp_planner`` se repo completo.
# Esegui da qualunque cwd — path assoluto consigliato:
#   powershell -NoProfile -ExecutionPolicy Bypass -File "C:\...\mujoco_go2_d1\external\openvla_worker\bootstrap_worker_host.ps1"
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$RepoRoot = (Resolve-Path (Join-Path $Root "..\..")).Path
$Planner = Join-Path $RepoRoot "scripts\box_grasp_planner.py"
if (-not (Test-Path $Planner)) {
    Write-Error "Serve il clone **completo** del repo (manca: $Planner). Copia tutto ``mujoco_go2_d1`` sul PC RTX, non solo ``external/openvla_worker``."
    exit 1
}

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host ">>> Creo venv .venv"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        Write-Error "Installa Python 3 (https://www.python.org/) con 'Add to PATH', poi riprova."
        exit 1
    }
}

Write-Host ">>> pip: aggiorno pip / wheel e tqdm (barra sui pacchetti dopo)"
& $venvPy -m pip install -q -U pip tqdm wheel

Write-Host ">>> Installazione requirements (tqdm per riga in setup_windows_worker.py)"
& $venvPy (Join-Path $Root "setup_windows_worker.py")

if (-not $env:WORKER_BIND_HOST) { $env:WORKER_BIND_HOST = "0.0.0.0" }
if (-not $env:WORKER_PORT) { $env:WORKER_PORT = "8765" }
if (-not $env:GO2_GRASP_WORKER_BACKEND) { $env:GO2_GRASP_WORKER_BACKEND = "planner" }
if (-not $env:WORKER_CAMERA_JPG_URL) {
    $env:WORKER_CAMERA_JPG_URL = "http://192.168.123.18:5052/api/robot/camera/0.jpg"
}

Write-Host ""
Write-Host "Modalità worker: GO2_GRASP_WORKER_BACKEND=$($env:GO2_GRASP_WORKER_BACKEND)" -ForegroundColor Cyan
Write-Host "JPEG sorgente (GET per plan): WORKER_CAMERA_JPG_URL=$($env:WORKER_CAMERA_JPG_URL)" -ForegroundColor Cyan
Write-Host "Repo (per box_grasp_planner): $RepoRoot" -ForegroundColor DarkGray
Write-Host "Indirizzi IPv4 (cerca 192.168.123.x per la NX):" -ForegroundColor Cyan
try {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } |
        ForEach-Object { Write-Host ("  {0}  ({1})" -f $_.IPAddress, $_.InterfaceAlias) }
} catch {
    ipconfig | Write-Host
}

Write-Host ""
Write-Host "Sulla macchina di deploy imposta (esempio):" -ForegroundColor Yellow
$lab = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like '192.168.123.*' } | Select-Object -ExpandProperty IPAddress -First 1)
if ($lab) {
    Write-Host ('  $env:GO2_DEPLOY_ANYGRASP_WORKER_URL="http://{0}:{1}"' -f $lab, $env:WORKER_PORT)
} else {
    Write-Host ('  $env:GO2_DEPLOY_ANYGRASP_WORKER_URL="http://<TUO_IP_LAN>:' + $env:WORKER_PORT + '"')
}
Write-Host "  python scripts/deploy_dashboard_to_nx.py"
Write-Host ""
Write-Host "Test locale: curl http://127.0.0.1:$($env:WORKER_PORT)/health" -ForegroundColor DarkGray
Write-Host ">>> Avvio Flask su $($env:WORKER_BIND_HOST):$($env:WORKER_PORT)  (Ctrl+C per uscire)" -ForegroundColor Green
& $venvPy (Join-Path $Root "app.py")
