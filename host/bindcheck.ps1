# bindcheck.ps1 -- why is lemond still on 127.0.0.1 after host=0.0.0.0?
# Read-mostly: lists processes, hunts the config file, stops ALL lemond
# instances, restarts one fresh, and re-reads the listen addresses.
# Pure ASCII (PowerShell 5.1 ANSI trap).

Write-Output "== lemond processes:"
Get-Process lemond -ErrorAction SilentlyContinue |
    ForEach-Object { "PID " + $_.Id + " started " + $_.StartTime }

Write-Output "== lemonade config files on disk:"
$roots = @($env:LOCALAPPDATA, $env:APPDATA, $env:USERPROFILE)
foreach ($r in $roots) {
    Get-ChildItem -Path $r -Recurse -Depth 3 -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "lemonade" -and $_.Extension -match "json|yaml|yml|toml" } |
        ForEach-Object { $_.FullName }
}

Write-Output "== stopping all lemond, verifying port free:"
Stop-Process -Name lemond -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
$still = Get-NetTCPConnection -LocalPort 13305 -State Listen -ErrorAction SilentlyContinue
if ($still) { Write-Output "STILL LISTENING (some other process owns 13305)" }
else { Write-Output "port 13305 is free" }

$bin = Join-Path $env:LOCALAPPDATA "wsl-npu-bridge\lemonade\lemonade-embeddable-2026.44.1-windows-x64"
& (Join-Path $bin "lemonade.exe") config 2>&1 | Select-String "host "

Write-Output "== starting one fresh lemond:"
Start-Process -FilePath (Join-Path $bin "lemond.exe") -WindowStyle Hidden
Start-Sleep -Seconds 7
Get-NetTCPConnection -LocalPort 13305 -State Listen |
    ForEach-Object { "listening on " + $_.LocalAddress }
