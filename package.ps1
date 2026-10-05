# Zips the Model Hub source (code, docs, licence, default config) for sharing.
# Excludes secrets (config\turnstone.token), logs and caches.
$out = Join-Path $PSScriptRoot "dist"
New-Item -ItemType Directory -Force $out | Out-Null
$version = (Select-String -Path (Join-Path $PSScriptRoot "hub\__init__.py") -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$zip = Join-Path $out "turnstone-model-hub-$version.zip"
$stage = Join-Path ([System.IO.Path]::GetTempPath()) "model-hub-$version"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory $stage | Out-Null
foreach ($item in "hub", "web", "docs", "tests", "config", "README.md", "LICENSE", "NOTICE", ".gitignore", "run-hub.ps1", "stop-hub.ps1", "package.ps1") {
    Copy-Item (Join-Path $PSScriptRoot $item) $stage -Recurse
}
Get-ChildItem $stage -Recurse -Include "__pycache__", "*.pyc", "turnstone.token", "*.tmp" -Force | Remove-Item -Recurse -Force
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip
Remove-Item $stage -Recurse -Force
Write-Host "Created $zip"
