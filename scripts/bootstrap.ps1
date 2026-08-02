# LocalDocForge development/profile bootstrap (Windows PowerShell)
# Creates a profile-specific venv, installs hash-locked dependencies, and runs diagnostics.
param(
    [ValidateSet("lite", "standard", "full", "dev")]
    [string]$Profile = "dev"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$python = $null
foreach ($candidate in @("3.14", "3.13", "3.12")) {
    & py "-$candidate" -c "print()" 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
}
if (-not $python) { throw "CPython 3.12, 3.13, or 3.14 is required." }

# PowerShell does not turn a failed native command into a terminating error by
# default. Enable that behavior after the best-effort interpreter probes so a
# failed venv, install, test, lint, type-check, or doctor command cannot fall
# through to the success banner.
$PSNativeCommandUseErrorActionPreference = $true

$venv = if ($Profile -eq "dev") { ".venv" } else { ".venv-$Profile" }
Write-Host "Using Python $python with the $Profile profile in $venv"
if (-not (Test-Path -LiteralPath $venv)) { & py "-$python" -m venv $venv }
$venvPython = Join-Path $venv "Scripts\python.exe"
$venvLdf = Join-Path $venv "Scripts\ldf.exe"
$lock = Join-Path "requirements\locks" "$Profile.txt"
& $venvPython -m pip install --require-hashes -r $lock
& $venvPython -m pip install -e ".[${Profile}]" --no-deps
if ($Profile -eq "dev") {
    & $venvPython -m pytest tests -q
    & $venvPython -m ruff check src tests scripts
    & $venvPython -m mypy
} else {
    & $venvPython scripts\profile_smoke.py --profile $Profile
}
& $venvLdf --json doctor
Write-Host "`nBootstrap complete. Try: $venvLdf --help"
