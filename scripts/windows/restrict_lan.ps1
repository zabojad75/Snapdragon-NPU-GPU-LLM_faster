# restrict_lan.ps1 — keep the model servers private to this PC + WSL
#
# llama-server (:8081) and geniex (:18181) listen on 0.0.0.0 without any API
# key, because WSL (NAT mode) reaches them through the vEthernet gateway IP.
# When Windows first asked "allow access?", the answer created inbound ALLOW
# rules for ANY remote address — so on café / hotel Wi-Fi anyone on the same
# network can use your models.
#
# This script narrows every inbound allow rule for those two programs to:
#   127.0.0.1 (this PC)  +  172.16.0.0/12 (the WSL NAT range)
#
# Usage (elevated PowerShell):
#   powershell -ExecutionPolicy Bypass -File restrict_lan.ps1            # apply
#   powershell -ExecutionPolicy Bypass -File restrict_lan.ps1 -WhatIf    # show only
#   powershell -ExecutionPolicy Bypass -File restrict_lan.ps1 -AllowLan  # undo (Any)
param([switch]$WhatIf, [switch]$AllowLan)

$programs = @('llama-server.exe', 'geniex.exe')
$remote   = if ($AllowLan) { @('Any') } else { @('127.0.0.1', '172.16.0.0/12') }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $WhatIf -and -not $isAdmin) {
    Write-Host "Needs an elevated PowerShell (Run as administrator), or use -WhatIf." -ForegroundColor Yellow
    exit 1
}

$rules = Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
    Where-Object {
        $app = ($_ | Get-NetFirewallApplicationFilter).Program
        $app -and ($programs -contains [IO.Path]::GetFileName($app).ToLower())
    }
if (-not $rules) { Write-Host "No inbound allow rules found for $($programs -join ', ')."; exit 0 }

foreach ($r in $rules) {
    $app  = ($r | Get-NetFirewallApplicationFilter).Program
    $was  = ($r | Get-NetFirewallAddressFilter).RemoteAddress -join ','
    $line = "{0} [{1}] {2}: remote {3} -> {4}" -f $r.DisplayName, $r.Profile, $app, $was, ($remote -join ',')
    if ($WhatIf) { Write-Host "would set  $line"; continue }
    Set-NetFirewallRule -Name $r.Name -RemoteAddress $remote
    Write-Host "set  $line" -ForegroundColor Green
}
