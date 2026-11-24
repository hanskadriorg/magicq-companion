# Build a portable Windows folder + zip with PyInstaller (including offline ONNX model).
# Run from the repo root on Windows:
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "==> Creating venv..."
if (-not (Test-Path .venv)) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { py -3 -m venv .venv }
}
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\pip install pyinstaller

Write-Host "==> Fetching offline ONNX genre model..."
.\.venv\Scripts\python scripts\fetch_models.py --dest models

Write-Host "==> Running PyInstaller..."
Remove-Item -Recurse -Force dist\MagicQCompanion -ErrorAction SilentlyContinue
.\.venv\Scripts\pyinstaller --noconfirm --clean packaging\magicq_companion.spec

Copy-Item config.toml dist\MagicQCompanion\config.toml -Force

$zip = "dist\MagicQCompanion-windows.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\MagicQCompanion\* -DestinationPath $zip

Write-Host ""
Write-Host "Portable build ready:"
Write-Host "  dist\MagicQCompanion\MagicQCompanion.exe"
Write-Host "  $zip"
