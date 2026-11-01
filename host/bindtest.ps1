# bindtest.ps1 -- decisive bind experiment.
# Starts lemond with the documented runtime override --host 0.0.0.0
# (src/cpp/server/cli_parser.cpp), captures its own log lines, and
# prints the real listen addresses. Pure ASCII.
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:LOCALAPPDATA "wsl-npu-bridge"
$bin = Join-Path $Root "lemonade\lemonade-embeddable-2026.44.1-windows-x64"
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Stop-Process -Name lemond -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$out = Join-Path $logDir "bindtest.out.log"
$err = Join-Path $logDir "bindtest.err.log"
$p = Start-Process -FilePath (Join-Path $bin "lemond.exe") `
    -ArgumentList "--host","0.0.0.0" -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $out -RedirectStandardError $err
Write-Output ("started PID " + $p.Id)
Start-Sleep -Seconds 8

Write-Output "== listen addresses on 13305:"
Get-NetTCPConnection -LocalPort 13305 -State Listen |
    ForEach-Object { $_.LocalAddress + " pid=" + $_.OwningProcess }

Write-Output "== lemond log lines:"
if (Test-Path $out) { Get-Content $out -TotalCount 30 }
if (Test-Path $err) { Get-Content $err -TotalCount 10 }
