# relay.ps1 -- the no-admin, no-firewall-rule way into the bridge.
#
# Why this exists: Windows Defender Firewall silently drops inbound
# connections from the WSL VM to a user process unless a rule allows
# them, and adding a rule needs an admin prompt. But OUTBOUND from
# Windows is allowed by default -- so the host dials INTO WSL over SSH
# (WSL's sshd is already there) and remote-forwards the services back
# through that tunnel:
#
#   WSL program -> 127.0.0.1:13305 (inside WSL) -> sshd -> [tunnel]
#     -> host ssh.exe -> 127.0.0.1:13305 on the host -> lemond
#   same shape for the FLM companion service on 53999.
#
# Bonus: both host services keep loopback-only binds, so the
# unauthenticated APIs are never exposed on the LAN at all.
#
# One-time setup: a dedicated keypair lives in
# %LOCALAPPDATA%\wsl-npu-bridge\. This script writes the exact
# authorized_keys line to authorized-line.txt; the WSL-side helper
# (wsl\authorize-relay.sh in this repo) converges authorized_keys to
# that line. IMPORTANT OpenSSH fact this design respects: sshd uses
# the FIRST authorized_keys line matching a key, so all permissions
# for the bridge key live on ONE line (both permitlisten options).
#
#   powershell -ExecutionPolicy Bypass -File relay.ps1          # start
#   powershell -ExecutionPolicy Bypass -File relay.ps1 -Stop    # stop
#
# Pure ASCII (PowerShell 5.1 ANSI trap).

param([switch]$Stop, [string]$Distro = "Ubuntu", [string]$WslUser = "proart")

$ErrorActionPreference = "Stop"
$Root = Join-Path $env:LOCALAPPDATA "wsl-npu-bridge"
$Key = Join-Path $Root "bridge_key"
$LineFile = Join-Path $Root "authorized-line.txt"
$Ports = @(13305, 53999)

if ($Stop) {
    Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" |
        Where-Object { $_.CommandLine -match "bridge_key" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId }
    Write-Host "relays stopped (the WSL-side listeners close with the tunnels)."
    return
}

# --- dedicated keypair + the authorized_keys line ---------------------
if (-not (Test-Path $Key)) {
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    # PS 5.1 drops truly-empty native args; '""' is the standard
    # workaround -- ssh-keygen's C runtime parses it as empty.
    & ssh-keygen.exe -t ed25519 -N '""' -f $Key -C "wsl-npu-bridge" | Out-Null
    Write-Host "Created dedicated keypair: $Key"
}
$pub = (Get-Content "$Key.pub").Trim()
$permits = ($Ports | ForEach-Object { 'permitlisten="127.0.0.1:' + $_ + '"' }) -join ","
$line = "restrict,port-forwarding," + $permits + " " + $pub
Set-Content -Path $LineFile -Value $line -Encoding Ascii

# --- has the WSL side converged authorized_keys to this line? ---------
$have = & wsl.exe -d $Distro -u $WslUser grep -c "wsl-npu-bridge" ~/.ssh/authorized_keys 2>$null
if ($have -notmatch "^\d+$" -or [int]$have -lt 1) {
    Write-Host ""
    Write-Host "ONE-TIME STEP still needed. In your WSL distro, from this repo:"
    Write-Host "  bash wsl/authorize-relay.sh"
    Write-Host "(appends the restricted line from authorized-line.txt;"
    Write-Host " safe to re-run -- it replaces any older bridge lines.)"
    Write-Host ""
    throw "bridge key is not authorized in WSL yet (see above)."
}

# --- discover the WSL guest IP (changes per boot in NAT mode) ---------
$wslIp = (& wsl.exe -d $Distro hostname -I).Trim().Split(" ")[0]
if (-not $wslIp) { throw "Could not get the WSL guest IP via wsl.exe." }
Write-Host "WSL guest: $wslIp"

# --- start one tunnel per port -----------------------------------------
foreach ($port in $Ports) {
    $sshArgs = @(
        "-N",
        "-i", $Key,
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "StrictHostKeyChecking=accept-new",
        "-R", "127.0.0.1:${port}:127.0.0.1:${port}",
        "$WslUser@$wslIp"
    )
    $p = Start-Process -FilePath "ssh.exe" -ArgumentList $sshArgs -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 2
    if ($p.HasExited) {
        Write-Host "relay for port $port exited (code $($p.ExitCode)) -- is something already listening in WSL?"
    } else {
        Write-Host "relay up: WSL 127.0.0.1:$port -> host 127.0.0.1:$port (ssh PID $($p.Id))"
    }
}
Write-Host "From WSL, verify with:  python3 client/wsl_npu.py doctor"
