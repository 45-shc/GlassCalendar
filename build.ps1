param([string]$OutputDir = 'dist')
$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create .venv and install requirements-build.txt first.' }
$oldPath = $env:Path
try {
    # Avoid accidentally bundling incompatible DLLs from other applications.
    $env:Path = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem"
    Push-Location $project
    try {
        & $python -m PyInstaller --noconfirm --clean --windowed --icon 'assets\glass-calendar-icon.ico' --add-data 'assets;assets' --distpath $OutputDir --name GlassCalendar main.py
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
} finally { $env:Path = $oldPath }
