# LocalDocForge development bootstrap (Windows PowerShell)
# Creates .venv, installs pinned dependencies + the package, runs diagnostics.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$python = $null
foreach ($candidate in @("3.14", "3.13", "3.12")) {
    & py "-$candidate" -c "print()" 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}
if (-not $python) { throw "Python 3.12+ is required (py launcher found none)." }

Write-Host "Using Python $python"
if (-not (Test-Path ".venv")) { & py "-$python" -m venv .venv }
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -r requirements-lock.txt
& .venv\Scripts\python.exe -m pip install -e . --no-deps
& .venv\Scripts\python.exe -m pytest tests -q
& .venv\Scripts\ldf.exe doctor
Write-Host "`nBootstrap complete. Try: .venv\Scripts\ldf.exe --help"
