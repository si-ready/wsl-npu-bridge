# watchdog.ps1 -- one health pass over the bridge; repairs what is
# provably dead and logs what it did. Designed to be run every few
# minutes by the scheduled task that install-autostart.ps1 registers
# (single-pass, no resident loop -- the Task Scheduler is the loop).
#
# Repair ladder (conservative: act only on clear failures):
#   lemond dead host-local      -> full serve.ps1 (rebuilds all)
#   flm dead host-local         -> restart flm serve only
#   a relay listener missing    -> relay.ps1 (restarts both relays;
#      in WSL                      sshd-side state is per-connection)
#
# Log: %LOCALAPPDATA%\wsl-npu-bridge\logs\watchdog.log (rotated at
# ~1 MB to watchdog.log.old). Pure ASCII.

$ErrorActionPreference = "Continue"
$Root = Join-Path $env:LOCALAPPDATA "wsl-npu-bridge"
$LogDir = Join-Path $Root "logs"
$Log = Join-Path $LogDir "watchdog.log"
$Bin = Join-Path $Root "lemonade\lemonade-embeddable-2026.44.1-windows-x64"
$FlmPort = 53999
$Distro = "Ubuntu"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 1MB) {
    Move-Item $Log "$Log.old" -Force
}
function Note($msg) {
    $line = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "  " + $msg
    Add-Content -Path $Log -Value $line
}

function Test-Local($url) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 4
        return $r.StatusCode -eq 200
    } catch { return $false }
}

# 1. lemond
if (-not (Test-Local "http://127.0.0.1:13305/api/v1/models")) {
    Note "lemond not answering host-local; running serve.ps1"
    & (Join-Path $Root "host\serve.ps1")
    Note "serve.ps1 finished"
    return
}

# 2. flm companion (only if its backend is installed)
$flm = Join-Path $env:USERPROFILE ".cache\lemonade\bin\flm\npu\flm.exe"
if ((Test-Path $flm) -and -not (Test-Local "http://127.0.0.1:$FlmPort/v1/models")) {
    Note "flm not answering host-local; restarting flm serve"
    Stop-Process -Name flm -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-Process -FilePath $flm `
        -ArgumentList "serve","llama3.2:1b","--host","127.0.0.1","--port",$FlmPort `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "flm.out.log") `
        -RedirectStandardError (Join-Path $LogDir "flm.err.log")
}

# 3. relay listeners inside WSL -- but never BOOT WSL just to check:
# if the distro is not already running, its owner closed it on
# purpose and the relays are none of our business until it returns.
$running = (& wsl.exe --list --running 2>$null) -join "`n"
if ($running -notmatch $Distro) { return }
$ss = (& wsl.exe -d $Distro ss -tln 2>$null) -join "`n"
$missing = @()
foreach ($port in @(13305, $FlmPort)) {
    if ($ss -notmatch (":" + $port)) { $missing += $port }
}
if ($missing.Count -gt 0) {
    Note ("relay listener(s) missing in WSL: " + ($missing -join ", ") +
          "; running relay.ps1")
    & (Join-Path $Root "host\relay.ps1")
}
