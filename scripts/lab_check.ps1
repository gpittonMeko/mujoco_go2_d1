# Stesso lab_check ma lanciabile anche se sei gia' in scripts\
# Uso:  .\lab_check.ps1
$Root = Split-Path -Parent $PSScriptRoot
& "$Root\lab_check.ps1" @args
