#Requires -Version 5.1
<#
.SYNOPSIS
  Crea EC2 g5.xlarge per VLA, bootstrap Docker worker, salva pairing per la NX.

.USAGE (PowerShell, dalla root repo):
  powershell -ExecutionPolicy Bypass -File external/openvla_worker/aws/provision-ec2.ps1

  # Chiave locale (default Documents\LLM_14.pem):
  powershell -ExecutionPolicy Bypass -File external/openvla_worker/aws/provision-ec2.ps1 `
    -KeyPath "$env:USERPROFILE\Documents\LLM_14.pem" -KeyName LLM_14 -Region eu-west-1

Env opzionali:
  $env:AWS_DEFAULT_REGION = "eu-west-1"
  Se SSL fallisce: $env:AWS_CA_BUNDLE = ""  (solo lab) oppure aggiorna certificati AWS CLI

Output:
  data/go2_vla_ec2_state.json
  go2-vla-pairing.env (in root repo)
#>
param(
    [string]$KeyPath = "$env:USERPROFILE\Documents\LLM_14.pem",
    [string]$KeyName = "LLM_14",
    [string]$Region = $(if ($env:AWS_DEFAULT_REGION) { $env:AWS_DEFAULT_REGION } else { "eu-north-1" }),
    [string]$InstanceType = "g5.xlarge",
    [int]$VolumeGb = 100,
    [string]$InstanceName = "go2-vla-worker",
    [switch]$ReuseExisting,
    [string]$ExistingInstanceId = "",
    [switch]$DisableSslVerify
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$StatePath = Join-Path $RepoRoot "data/go2_vla_ec2_state.json"
$PairingLocal = Join-Path $RepoRoot "go2-vla-pairing.env"
$WorkerLocal = Join-Path $RepoRoot "external/openvla_worker"

function Invoke-Aws {
    param([string[]]$AwsArgs)
    if ($DisableSslVerify) { $env:AWS_CA_BUNDLE = "" }
    else {
        try {
            $certifi = python -m certifi 2>$null
            if ($certifi) { $env:AWS_CA_BUNDLE = $certifi.Trim() }
        } catch {}
    }
    $awsExtra = @()
    if ($DisableSslVerify) { $awsExtra += "--no-verify-ssl" }
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $raw = & aws --region $Region @awsExtra @AwsArgs 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    $lines = @($raw | ForEach-Object {
        $s = "$_"
        if ($s -match "InsecureRequestWarning") { return }
        $s
    } | Where-Object { $_ })
    $text = ($lines -join "`n").Trim()
    if ($code -ne 0) { throw "aws failed (exit $code): $text" }
    return $text
}

function Wait-Ssh {
    param([string]$Ip, [string]$Key, [int]$MaxSec = 600)
    $deadline = (Get-Date).AddSeconds($MaxSec)
    while ((Get-Date) -lt $deadline) {
        $r = & ssh -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=8 "ubuntu@$Ip" "echo ok" 2>$null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Seconds 8
    }
    return $false
}

function Wait-WorkerHealth {
    param([string]$Ip, [int]$MaxSec = 900)
    $deadline = (Get-Date).AddSeconds($MaxSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-RestMethod -Uri "http://${Ip}:8765/health" -TimeoutSec 8
            if ($r.ok) { return $true }
        } catch {}
        Start-Sleep -Seconds 10
    }
    return $false
}

Write-Host "[provision] repo=$RepoRoot region=$Region type=$InstanceType"

if (-not (Test-Path $KeyPath)) {
    throw "Chiave PEM non trovata: $KeyPath"
}

# Verifica AWS identity
try {
    $id = Invoke-Aws @("sts", "get-caller-identity", "--output", "json") | ConvertFrom-Json
    Write-Host "[provision] AWS account $($id.Account) user $($id.Arn)"
} catch {
    Write-Host "ERRORE AWS CLI (credenziali o SSL). Prova:" -ForegroundColor Red
    Write-Host '  $env:AWS_ACCESS_KEY_ID="..."; $env:AWS_SECRET_ACCESS_KEY="..."'
    Write-Host '  Se SSL: $env:AWS_CA_BUNDLE=""  (solo debug lab)'
    throw
}

$InstanceId = $ExistingInstanceId
$PublicIp = $null

if ($ReuseExisting -and $InstanceId) {
    Write-Host "[provision] reuse instance $InstanceId"
} elseif (Test-Path $StatePath) {
    $prev = Get-Content $StatePath -Raw | ConvertFrom-Json
    if ($prev.instance_id) {
        $InstanceId = $prev.instance_id
        Write-Host "[provision] trovato state esistente instance $InstanceId"
        $st = Invoke-Aws @("ec2", "describe-instances", "--instance-ids", $InstanceId, "--query", "Reservations[0].Instances[0].State.Name", "--output", "text")
        if ($st.Trim() -eq "stopped") {
            Write-Host "[provision] avvio istanza fermata..."
            Invoke-Aws @("ec2", "start-instances", "--instance-ids", $InstanceId) | Out-Null
            Invoke-Aws @("ec2", "wait", "instance-running", "--instance-ids", $InstanceId) | Out-Null
        }
    }
}

function Invoke-AutostopInfra {
    param([string]$Iid = "")
    $autostopScript = Join-Path $PSScriptRoot "setup-aws-autostop-infra.ps1"
    if (-not (Test-Path $autostopScript)) { return $false }
    $args = @("-ExecutionPolicy", "Bypass", "-File", $autostopScript, "-Region", $Region)
    if ($Iid) { $args += @("-InstanceId", $Iid) }
    if ($DisableSslVerify) { $args += "-DisableSslVerify" }
    try {
        & powershell @args
        if ($LASTEXITCODE -ne 0) { throw "exit $LASTEXITCODE" }
        return $true
    } catch {
        Write-Host "[provision] AVVISO auto-stop infra (IAM/EventBridge): $_" -ForegroundColor Yellow
        Write-Host "[provision] Usa: python scripts/aws_vla_ec2_control.py stop  oppure stop notturno manuale." -ForegroundColor Yellow
        return $false
    }
}

function Test-Go2IamInstanceProfile {
    try {
        Invoke-Aws @("iam", "get-instance-profile", "--instance-profile-name", "go2-vla-worker-ec2-profile") | Out-Null
        return $true
    } catch {
        return $false
    }
}

Write-Host "[provision] IAM + EventBridge auto-stop (infra, best-effort)..."
Invoke-AutostopInfra | Out-Null
$script:Go2HasIamInstanceProfile = Test-Go2IamInstanceProfile
if ($script:Go2HasIamInstanceProfile) {
    Write-Host "[provision] IAM instance profile OK (idle self-stop on EC2 possibile)"
} else {
    Write-Host '[provision] Nessun IAM profile - idle stop via PC: python scripts/aws_vla_ec2_control.py stop'
}

if (-not $InstanceId) {
    Write-Host "[provision] AMI Deep Learning GPU Ubuntu 22.04..."
    $Ami = Invoke-Aws @(
        "ec2", "describe-images",
        "--owners", "amazon",
        "--filters", "Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*", "Name=state,Values=available",
        '--query', 'sort_by(Images, &CreationDate)[-1].ImageId',
        "--output", "text"
    )
    $Ami = $Ami.Trim()
    Write-Host "[provision] AMI=$Ami"

    $SgName = "go2-vla-worker-sg"
    $VpcId = (Invoke-Aws @("ec2", "describe-vpcs", "--filters", "Name=isDefault,Values=true", "--query", "Vpcs[0].VpcId", "--output", "text")).Trim()
    $SgId = $null
    try {
        $SgId = (Invoke-Aws @("ec2", "describe-security-groups", "--filters", "Name=group-name,Values=$SgName", "--query", "SecurityGroups[0].GroupId", "--output", "text")).Trim()
    } catch {}
    if (-not $SgId -or $SgId -eq "None") {
        $SgId = (Invoke-Aws @("ec2", "create-security-group", "--group-name", $SgName, "--description", "Go2 VLA worker", "--vpc-id", $VpcId, "--query", "GroupId", "--output", "text")).Trim()
        Invoke-Aws @("ec2", "authorize-security-group-ingress", "--group-id", $SgId, "--protocol", "tcp", "--port", "22", "--cidr", "0.0.0.0/0") | Out-Null
        Invoke-Aws @("ec2", "authorize-security-group-ingress", "--group-id", $SgId, "--protocol", "tcp", "--port", "8765", "--cidr", "0.0.0.0/0") | Out-Null
        Write-Host "[provision] creato SG $SgId (22, 8765)"
    } else {
        Write-Host "[provision] SG esistente $SgId"
    }

    Write-Host "[provision] launch $InstanceType..."
    $bdmFile = Join-Path $env:TEMP "go2-vla-bdm.json"
    $bdmJson = '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":' + $VolumeGb + ',"VolumeType":"gp3","DeleteOnTermination":true}}]'
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($bdmFile, $bdmJson, $utf8NoBom)
    $bdmUri = "file://" + ($bdmFile -replace "\\", "/")
    $tagSpec = 'ResourceType=instance,Tags=[{Key=Name,Value=' + $InstanceName + '}]'
    $runArgs = @(
        "ec2", "run-instances",
        "--image-id", $Ami,
        "--instance-type", $InstanceType,
        "--key-name", $KeyName,
        "--security-group-ids", $SgId,
        "--block-device-mappings", $bdmUri,
        "--tag-specifications", $tagSpec,
        '--query', 'Instances[0].InstanceId',
        "--output", "text"
    )
    if ($script:Go2HasIamInstanceProfile) {
        $runArgs = @(
            "ec2", "run-instances",
            "--image-id", $Ami,
            "--instance-type", $InstanceType,
            "--key-name", $KeyName,
            "--iam-instance-profile", "Name=go2-vla-worker-ec2-profile",
            "--security-group-ids", $SgId,
            "--block-device-mappings", $bdmUri,
            "--tag-specifications", $tagSpec,
            '--query', 'Instances[0].InstanceId',
            "--output", "text"
        )
    }
    $RunJson = Invoke-Aws $runArgs
    $InstanceId = $RunJson.Trim()
    Write-Host "[provision] instance_id=$InstanceId"
    Invoke-Aws @("ec2", "wait", "instance-running", "--instance-ids", $InstanceId) | Out-Null
}

$PublicIp = (Invoke-Aws @("ec2", "describe-instances", "--instance-ids", $InstanceId, "--query", "Reservations[0].Instances[0].PublicIpAddress", "--output", "text")).Trim()
Write-Host "[provision] public_ip=$PublicIp"

Write-Host "[provision] attendo SSH..."
if (-not (Wait-Ssh -Ip $PublicIp -Key $KeyPath)) {
    throw "SSH non raggiungibile su $PublicIp"
}

Write-Host "[provision] copia worker su EC2..."
$RemoteDir = "/home/ubuntu/mujoco_go2_d1/external/openvla_worker"
& ssh -i $KeyPath -o StrictHostKeyChecking=no "ubuntu@$PublicIp" "mkdir -p /home/ubuntu/mujoco_go2_d1/external/openvla_worker/aws"
if ($LASTEXITCODE -ne 0) { throw "ssh mkdir failed" }

# tar stream (Windows tar)
Push-Location $WorkerLocal
tar -cf - . | ssh -i $KeyPath -o StrictHostKeyChecking=no "ubuntu@$PublicIp" "cd /home/ubuntu/mujoco_go2_d1/external/openvla_worker && tar -xf -"
Pop-Location
if ($LASTEXITCODE -ne 0) { throw "tar scp failed" }

Write-Host "[provision] bootstrap Docker + worker (stub, 2-5 min)..."
& ssh -i $KeyPath -o StrictHostKeyChecking=no "ubuntu@$PublicIp" "cd /home/ubuntu/mujoco_go2_d1/external/openvla_worker && chmod +x aws/*.sh && GO2_INSTALL_DIR=/home/ubuntu/mujoco_go2_d1 GO2_EC2_REGION=$Region GO2_EC2_IDLE_STOP_MIN=20 GO2_WORKER_STUB=1 bash aws/bootstrap-ec2.sh"
if ($LASTEXITCODE -ne 0) { throw "bootstrap failed" }

Write-Host "[provision] scarico pairing..."
& scp -i $KeyPath -o StrictHostKeyChecking=no "ubuntu@${PublicIp}:~/go2-vla-pairing.env" $PairingLocal
if ($LASTEXITCODE -ne 0) { throw "scp pairing failed" }

Wait-WorkerHealth -Ip $PublicIp | Out-Null

Invoke-AutostopInfra -Iid $InstanceId | Out-Null

$token = (Get-Content $PairingLocal | Where-Object { $_ -match '^GO2_WORKER_TOKEN=' }) -replace '^GO2_WORKER_TOKEN=', ''
$state = @{
    instance_id = $InstanceId
    region = $Region
    auto_stop_idle_min = 20
    nightly_stop_utc = "21:00"
    instance_type = $InstanceType
    public_ip = $PublicIp
    worker_url = "http://${PublicIp}:8765"
    key_name = $KeyName
    key_path_local = $KeyPath
    pairing_file = "go2-vla-pairing.env"
    worker_token_hint = if ($token.Length -gt 8) { $token.Substring(0, 8) + "..." } else { "***" }
    created_at = (Get-Date).ToString("o")
}
New-Item -ItemType Directory -Force -Path (Split-Path $StatePath) | Out-Null
$state | ConvertTo-Json | Set-Content -Path $StatePath -Encoding UTF8

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host " EC2 VLA PRONTA"
Write-Host " Instance: $InstanceId"
Write-Host " URL:      http://${PublicIp}:8765"
Write-Host " Pairing:  $PairingLocal"
Write-Host " State:    $StatePath"
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Prossimo passo (PC, NX accesa in LAN):"
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts/go2_vla_connect_nx.ps1"
Write-Host ""
