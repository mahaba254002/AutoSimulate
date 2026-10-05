$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDirectory
Write-Host 'Open http://127.0.0.1:8765 after the server starts. Press Ctrl+C to stop.'
$researchPython = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$venvConfig = Join-Path $projectDirectory '.venv\pyvenv.cfg'
$configuredHome = if (Test-Path -LiteralPath $venvConfig) {
    ((Get-Content -LiteralPath $venvConfig | Where-Object { $_ -match '^home = ' }) -replace '^home = ', '')
}
if (-not $configuredHome -or -not (Test-Path -LiteralPath (Join-Path $configuredHome 'python.exe'))) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (-not (Test-Path -LiteralPath $bundledPython)) {
        throw 'The virtual environment points to a missing Python installation. Recreate .venv with Python 3.11 or later.'
    }
    $researchPython = $bundledPython
    $env:PYTHONPATH = Join-Path $projectDirectory '.venv\Lib\site-packages'
}
& $researchPython -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $researchPython -m alpha_platform.cli serve
exit $LASTEXITCODE
