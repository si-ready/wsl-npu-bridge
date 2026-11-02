# install-autostart.ps1 -- make the bridge production-shaped:
# register user-level Scheduled Tasks (NO admin needed) that
#   * start the bridge at every logon        (serve.ps1)
#   * health-check it every 5 minutes       (watchdog.ps1)
# and can remove them again just as cleanly.
#
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Remove
#
# The tasks point at the COPIES of the scripts under
# %LOCALAPPDATA%\wsl-npu-bridge\host\ -- install.ps1 places them
# there, so moving or deleting your git clone never breaks autostart.
# Pure ASCII.

param([switch]$Remove)

$ErrorActionPreference = "Stop"
$Root = Join-Path $env:LOCALAPPDATA "wsl-npu-bridge"
$HostDir = Join-Path $Root "host"
$StartName = "WSL NPU Bridge (start at logon)"
$WatchName = "WSL NPU Bridge (watchdog)"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $StartName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $WatchName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "autostart + watchdog tasks removed."
    return
}

foreach ($script in @("serve.ps1", "watchdog.ps1", "relay.ps1")) {
    if (-not (Test-Path (Join-Path $HostDir $script))) {
        throw "$script not found in $HostDir -- run host\install.ps1 first (it copies the host scripts there)."
    }
}

$ps = "powershell.exe"
$common = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden"

$startAction = New-ScheduledTaskAction -Execute $ps `
    -Argument ($common + " -File `"" + (Join-Path $HostDir "serve.ps1") + "`"")
$startTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName $StartName -Action $startAction `
    -Trigger $startTrigger -Force | Out-Null
Write-Host "registered: $StartName"

$watchAction = New-ScheduledTaskAction -Execute $ps `
    -Argument ($common + " -File `"" + (Join-Path $HostDir "watchdog.ps1") + "`"")
$watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $WatchName -Action $watchAction `
    -Trigger $watchTrigger -Force | Out-Null
Write-Host "registered: $WatchName (every 5 minutes)"

Write-Host ""
Write-Host "The bridge now starts at logon and self-heals. To undo:"
Write-Host "  powershell -ExecutionPolicy Bypass -File install-autostart.ps1 -Remove"
