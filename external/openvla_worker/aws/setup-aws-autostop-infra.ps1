# IAM instance profile (self-stop) + EventBridge stop notturno per go2-vla-worker.
param(
    [string]$Region = "eu-north-1",
    [string]$InstanceId = "",
    [string]$InstanceNameTag = "go2-vla-worker",
    [switch]$DisableSslVerify
)

$ErrorActionPreference = "Stop"
trap {
    if ("$($_.Exception.Message)" -match "AccessDenied") {
        Write-Host "[autostop-infra] SKIP: permessi IAM insufficienti (CreateRole/Scheduler). Stop manuale o policy IAM." -ForegroundColor Yellow
        exit 0
    }
    throw $_
}
function Invoke-Aws([string[]]$AwsArgs) {
    if ($DisableSslVerify) { $env:AWS_CA_BUNDLE = "" }
    $extra = @()
    if ($DisableSslVerify) { $extra += "--no-verify-ssl" }
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $raw = & aws --region $Region @extra @AwsArgs 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    $text = ($raw | ForEach-Object {
        $s = "$_"
        if ($s -match "InsecureRequestWarning") { return }
        $s
    } | Where-Object { $_ }) -join "`n"
    if ($code -ne 0) { throw "aws failed (exit $code): $text" }
    return $text.Trim()
}

$RoleName = "go2-vla-worker-ec2-role"
$ProfileName = "go2-vla-worker-ec2-profile"
$PolicyName = "go2-vla-worker-self-stop"
$RuleName = "go2-vla-worker-nightly-stop"

$trust = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
$policy = @"
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ec2:StopInstances", "ec2:DescribeInstances"],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "ec2:ResourceTag/Name": "$InstanceNameTag"
        }
      }
    }
  ]
}
"@

Write-Host "[autostop-infra] IAM role $RoleName"
try {
    Invoke-Aws @("iam", "get-role", "--role-name", $RoleName) | Out-Null
} catch {
    Invoke-Aws @("iam", "create-role", "--role-name", $RoleName, "--assume-role-policy-document", $trust) | Out-Null
}

$policyArn = "arn:aws:iam::$( (Invoke-Aws @('sts','get-caller-identity','--query','Account','--output','text')).Trim() ):policy/$PolicyName"
try {
    Invoke-Aws @("iam", "get-policy", "--policy-arn", $policyArn) | Out-Null
} catch {
    $tmp = New-TemporaryFile
    Set-Content -Path $tmp -Value $policy -Encoding UTF8
    Invoke-Aws @("iam", "create-policy", "--policy-name", $PolicyName, "--policy-document", "file://$tmp") | Out-Null
    Remove-Item $tmp -Force
}

Invoke-Aws @("iam", "attach-role-policy", "--role-name", $RoleName, "--policy-arn", $policyArn) 2>$null | Out-Null

try {
    Invoke-Aws @("iam", "get-instance-profile", "--instance-profile-name", $ProfileName) | Out-Null
} catch {
    Invoke-Aws @("iam", "create-instance-profile", "--instance-profile-name", $ProfileName) | Out-Null
    Start-Sleep -Seconds 5
}
try {
    Invoke-Aws @("iam", "add-role-to-instance-profile", "--instance-profile-name", $ProfileName, "--role-name", $RoleName) | Out-Null
} catch {}

if ($InstanceId) {
    Write-Host "[autostop-infra] attach profile to $InstanceId"
    $assoc = (Invoke-Aws @(
        "ec2", "describe-iam-instance-profile-associations",
        "--filters", "Name=instance-id,Values=$InstanceId",
        "--query", "IamInstanceProfileAssociations[0].AssociationId",
        "--output", "text"
    )).Trim()
    if ($assoc -and $assoc -ne "None") {
        Invoke-Aws @("ec2", "replace-iam-instance-profile-association", "--association-id", $assoc, "--iam-instance-profile", "Name=$ProfileName") | Out-Null
    } else {
        Invoke-Aws @("ec2", "associate-iam-instance-profile", "--instance-id", $InstanceId, "--iam-instance-profile", "Name=$ProfileName") | Out-Null
    }
}

$account = (Invoke-Aws @("sts", "get-caller-identity", "--query", "Account", "--output", "text")).Trim()
$targetArn = "arn:aws:ec2:${Region}:${account}:instance/*"
$roleArn = "arn:aws:iam::${account}:role/$RoleName"

$ebTrust = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
$EbRoleName = "go2-vla-scheduler-stop-role"
try {
    Invoke-Aws @("iam", "get-role", "--role-name", $EbRoleName) | Out-Null
} catch {
    Invoke-Aws @("iam", "create-role", "--role-name", $EbRoleName, "--assume-role-policy-document", $ebTrust) | Out-Null
}
$ebPolicy = @"
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ec2:StopInstances",
      "Resource": "$targetArn",
      "Condition": {
        "StringEquals": { "ec2:ResourceTag/Name": "$InstanceNameTag" }
      }
    }
  ]
}
"@
$ebPolName = "go2-vla-scheduler-stop"
$ebPolArn = "arn:aws:iam::${account}:policy/$ebPolName"
try {
    Invoke-Aws @("iam", "get-policy", "--policy-arn", $ebPolArn) | Out-Null
} catch {
    $tmp2 = New-TemporaryFile
    Set-Content -Path $tmp2 -Value $ebPolicy -Encoding UTF8
    Invoke-Aws @("iam", "create-policy", "--policy-name", $ebPolName, "--policy-document", "file://$tmp2") | Out-Null
    Remove-Item $tmp2 -Force
}
Invoke-Aws @("iam", "attach-role-policy", "--role-name", $EbRoleName, "--policy-arn", $ebPolArn) 2>$null | Out-Null

# 21:00 UTC ≈ 23:00 ora legale Italia
$schedule = "cron(0 21 * * ? *)"
$target = @{
    Arn = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    RoleArn = "arn:aws:iam::${account}:role/$EbRoleName"
    Input = "{`"InstanceIds`":[`"$InstanceId`"]}"
} | ConvertTo-Json -Compress
if (-not $InstanceId) {
    Write-Host "[autostop-infra] skip EventBridge target (no instance id)"
} else {
    try {
        Invoke-Aws @("scheduler", "get-schedule", "--name", $RuleName) | Out-Null
        Invoke-Aws @("scheduler", "update-schedule", "--name", $RuleName, "--schedule-expression", $schedule, "--flexible-time-window", "Mode=OFF", "--target", $target, "--state", "ENABLED") | Out-Null
    } catch {
        Invoke-Aws @(
            "scheduler", "create-schedule",
            "--name", $RuleName,
            "--schedule-expression", $schedule,
            "--flexible-time-window", "Mode=OFF",
            "--target", $target,
            "--state", "ENABLED"
        ) | Out-Null
    }
    Write-Host "[autostop-infra] EventBridge $RuleName -> stop $InstanceId daily 21:00 UTC"
}

Write-Host "[autostop-infra] OK"
