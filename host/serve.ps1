# serve.ps1 -- start (or restart) the host side of the WSL2 NPU bridge.
#
# Starts AMD's Lemonade daemon (lemond.exe), which serves an
# OpenAI-compatible API on port 13305 and drives the XDNA NPU through
# its FastFlowLM / Ryzen AI backends. WSL2 programs reach it through
# the client in client\wsl_npu.py, which auto-discovers this endpoint
# (mirrored-mode localhost, or the NAT gateway address).
#
# Pure ASCII on purpose (PowerShell 5.1 ANSI trap -- see install.ps1).
#
#   powershell -ExecutionPolicy Bypass -File serve.ps1           # start
#   powershell -ExecutionPolicy Bypass -File serve.ps1 -Stop     # stop

param([switch]$Stop)

$ErrorActionPreference = "Stop"
$LemonadeVersion = "2026.44.1"
$Root = Join-Path $env:LOCALAPPDATA "wsl-npu-bridge"
$BinDir = Join-Path $Root "lemonade\lemonade-embeddable-$LemonadeVersion-windows-x64"
$Lemond = Join-Path $BinDir "lemond.exe"
$Port = 13305

$running = Get-Process lemond -ErrorAction SilentlyContinue
if ($Stop) {
    if ($running) { $running | Stop-Process; Write-Host "lemond stopped." }
    else { Write-Host "lemond is not running." }
    return
}
if (-not (Test-Path $Lemond)) {
    throw "lemond.exe not found at $Lemond -- run host\install.ps1 first."
}
if ($running) {
    Write-Host "lemond already running (PID $($running.Id)). Use -Stop first to restart."
    return
}

# WSL reachability: lemond defaults to binding "localhost" only, which
# a NAT-mode WSL guest cannot reach at all (its 127.0.0.1 is itself, and
# the host's loopback is not routed). Binding 0.0.0.0 makes the API
# reachable from the WSL gateway address. This exposes the API on the
# machine's LAN interfaces too -- it has no auth by default, so only do
# this on networks you trust, or set a Lemonade API key. Idempotent.
$LemonadeCli = Join-Path $BinDir "lemonade.exe"
if (Test-Path $LemonadeCli) { & $LemonadeCli config set host=0.0.0.0 | Out-Null }

$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$proc = Start-Process -FilePath $Lemond -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "lemond.out.log") `
    -RedirectStandardError (Join-Path $logDir "lemond.err.log")
Write-Host "lemond started (PID $($proc.Id)). Waiting for the API..."

# Readiness gate: poll the model list until the daemon answers, so the
# caller never has to guess whether the service is actually up.
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/v1/models" -TimeoutSec 2
        Write-Host "API is up on port $Port. Models known: $($r.data.Count)"
        Write-Host "From WSL, verify with:  python3 client\wsl_npu.py doctor"
        return
    } catch { Start-Sleep -Seconds 1 }
}
throw "lemond started but the API did not answer within 30 s. See $logDir\lemond.err.log"
