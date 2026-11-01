# diag.ps1 -- why can't WSL reach lemond? Read-only network diagnostics.
# Pure ASCII (PowerShell 5.1 ANSI trap).
Write-Output "== listen sockets on 13305:"
Get-NetTCPConnection -LocalPort 13305 -State Listen |
    ForEach-Object { $_.LocalAddress }

Write-Output "== host via its own vEthernet (WSL) IP 172.22.208.1:"
try {
    $r = Invoke-RestMethod -Uri "http://172.22.208.1:13305/api/v1/models" -TimeoutSec 5
    Write-Output ("OK, models: " + $r.data.Count)
} catch { Write-Output ("FAIL: " + $_.Exception.Message) }

Write-Output "== host via its LAN IP:"
$lan = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.InterfaceAlias -match "Wi-Fi|Ethernet" -and $_.IPAddress -notmatch "^169"
} | Select-Object -First 1).IPAddress
Write-Output ("LAN IP: " + $lan)
try {
    $r = Invoke-RestMethod -Uri ("http://" + $lan + ":13305/api/v1/models") -TimeoutSec 5
    Write-Output ("OK, models: " + $r.data.Count)
} catch { Write-Output ("FAIL: " + $_.Exception.Message) }

Write-Output "== firewall profiles:"
Get-NetFirewallProfile | ForEach-Object { $_.Name + " enabled=" + $_.Enabled }

Write-Output "== connection profiles:"
Get-NetConnectionProfile | ForEach-Object { $_.InterfaceAlias + " : " + $_.NetworkCategory }

Write-Output "== existing firewall rules mentioning lemond/lemonade:"
Get-NetFirewallRule -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -match "lemon" } |
    ForEach-Object { $_.DisplayName + " action=" + $_.Action + " enabled=" + $_.Enabled }
