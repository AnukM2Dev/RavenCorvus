# Windows version of build-addon.sh. Run from the repo root:  .\scripts\build-addon.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root 'dist\ravencorvus'
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force -Path $out | Out-Null
Copy-Item -Recurse -Force (Join-Path $root 'addon\ravencorvus\*') $out
Copy-Item (Join-Path $root 'pyproject.toml'), (Join-Path $root 'README.md') $out
Copy-Item -Recurse (Join-Path $root 'src') (Join-Path $out 'src')
Get-ChildItem -Recurse -Directory -Filter '__pycache__' $out | Remove-Item -Recurse -Force
# Linux line endings, or run.sh will not start in the container
Get-ChildItem -Recurse -File $out -Include *.sh,*.yaml,*.py,Dockerfile | ForEach-Object {
  $t = [IO.File]::ReadAllText($_.FullName) -replace "`r`n", "`n"
  [IO.File]::WriteAllText($_.FullName, $t, (New-Object Text.UTF8Encoding $false))
}
Write-Host "Built $out"
