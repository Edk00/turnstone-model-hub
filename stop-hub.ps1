# Stops a background Model Hub (started with run-hub.ps1 -Background).
$c = Get-NetTCPConnection -LocalPort 8099 -State Listen -ErrorAction SilentlyContinue
if ($c) {
    $c.OwningProcess | Select-Object -Unique | ForEach-Object {
        if ((Get-Process -Id $_).ProcessName -like "python*") { Stop-Process -Id $_ -Force; Write-Host "Stopped Model Hub (pid $_)." }
    }
} else { Write-Host "Model Hub is not running." }
