# Starts Model Hub on http://127.0.0.1:8099 and opens it in your browser.
#   .\run-hub.ps1            # start in this window (Ctrl+C to stop)
#   .\run-hub.ps1 -Background # start hidden; stop with .\stop-hub.ps1
param([switch]$Background, [switch]$NoBrowser)
Set-Location $PSScriptRoot
$url = "http://127.0.0.1:8099"

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Write-Error "Python 3.11+ is required (https://www.python.org/downloads/)."; exit 1 }

if (Get-NetTCPConnection -LocalPort 8099 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Model Hub is already running at $url"
} elseif ($Background) {
    New-Item -ItemType Directory -Force logs | Out-Null
    Start-Process $python -ArgumentList "-m", "hub" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
        -RedirectStandardOutput logs\hub.out.log -RedirectStandardError logs\hub.log
    Start-Sleep 2
    Write-Host "Model Hub started at $url (logs in model-hub\logs)"
} else {
    if (-not $NoBrowser) { Start-Job { Start-Sleep 2; Start-Process $using:url } | Out-Null }
    & $python -m hub
    exit
}
if (-not $NoBrowser) { Start-Process $url }
