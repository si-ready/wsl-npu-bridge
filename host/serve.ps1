# serve.ps1 -- start (or stop) the host side of the WSL2 NPU bridge.
#
# The proven bring-up sequence (learned the hard way; see
# docs/TROUBLESHOOTING.md for the failure that taught each step):
#
#   1. Start ONE lemond, loopback-only bind, with its logs captured.
#      Never start a second one against a wedged port, and never trust
#      a start you have not verified.
#   2. Verify lemond answers on host loopback BEFORE anything else.
#   3. Start flm serve for the FastFlowLM model (a companion service;
#      lemond's own FLM discovery is broken in current builds -- see
#      TROUBLESHOOTING). FLM is pinned to port 53999: its default
#      52625 failed to bind on the reference machine (WSAEACCES).
#   4. Start the SSH reverse relays (relay.ps1), which carry both
#      ports into WSL over an outbound connection -- no firewall
#      rules, no admin, and both services keep loopback-only binds.
#
#   powershell -ExecutionPolicy Bypass -File serve.ps1
#   powershell -ExecutionPolicy Bypass -File serve.ps1 -Stop
#
# Pure ASCII on purpose (PowerShell 5.1 ANSI trap -- see install.ps1).

param([switch]$Stop, [string]$FlmModel = "llama3.2:1b")

$ErrorActionPreference = "Stop"
$Root = Join-Path $env:LOCALAPPDATA "wsl-npu-bridge"
$Bin = Join-Path $Root "lemonade\lemonade-embeddable-2026.44.1-windows-x64"
$LogDir = Join-Path $Root "logs"
$FlmPort = 53999

if ($Stop) {
    foreach ($n in @("lemond", "lemonade", "flm", "ryzenai-server", "whisper-server")) {
        Stop-Process -Name $n -Force -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
        Where-Object { $_.CommandLine -match "bridge_key" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host "bridge stopped."
    return
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Start from a clean slate: a half-dead lemond holding the port is the
# single most confusing state this stack has, so it is made impossible
# here rather than debugged later. Old relays go too -- a stale relay
# holds the WSL-side listener and the new one would fail to bind it.
foreach ($n in @("lemond", "flm")) {
    Stop-Process -Name $n -Force -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
    Where-Object { $_.CommandLine -match "bridge_key" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2

# 1-2. lemond, verified host-local.
$lemond = Join-Path $Bin "lemond.exe"
if (-not (Test-Path $lemond)) { throw "lemond not found -- run host\install.ps1 first." }
Start-Process -FilePath $lemond -ArgumentList "--host","localhost" `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir "lemond.out.log") `
    -RedirectStandardError (Join-Path $LogDir "lemond.err.log")
$ok = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 4
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:13305/api/v1/models" `
            -UseBasicParsing -TimeoutSec 4
        Write-Host ("lemond answering host-local (HTTP " + $r.StatusCode + ")")
        $ok = $true
        break
    } catch { }
}
if (-not $ok) {
    throw "lemond did not answer within 60 s -- see logs\lemond.out.log"
}

# 3. FLM companion service.
$flm = Join-Path $env:USERPROFILE ".cache\lemonade\bin\flm\npu\flm.exe"
if (Test-Path $flm) {
    Start-Process -FilePath $flm `
        -ArgumentList "serve",$FlmModel,"--host","127.0.0.1","--port",$FlmPort `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "flm.out.log") `
        -RedirectStandardError (Join-Path $LogDir "flm.err.log")
    Write-Host "flm serve started ($FlmModel on port $FlmPort)"
} else {
    Write-Host "flm backend not installed -- skipping the FLM companion service."
}

# 4. Relays into WSL (both ports).
& (Join-Path $Root "host\relay.ps1")

Write-Host ""
Write-Host "Bridge is up. From WSL:"
Write-Host "  python3 client\wsl_npu.py doctor"
