# Windows setup for n8n-Cursor bridge
# If "execution of scripts is disabled", run instead:  setup.bat
# Or once:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Test-PythonExe {
    param([string]$ExePath)
    if (-not (Test-Path $ExePath)) { return $false }
    if ($ExePath -like "*\WindowsApps\*") { return $false }
    if ($ExePath -like "*\LibreOffice\*") { return $false }

    $version = & $ExePath --version 2>&1 | Out-String
    return ($version -match "Python 3\.\d+")
}

function Test-PythonCommand {
    param([string]$Cmd, [string[]]$Args = @())
    $command = Get-Command $Cmd -ErrorAction SilentlyContinue
    if (-not $command) { return $false }
    return (Test-PythonExe $command.Source)
}

function Find-PythonFromDisk {
    $patterns = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe",
        "${env:ProgramFiles}\Python3*\python.exe",
        "${env:ProgramFiles(x86)}\Python3*\python.exe"
    )
    foreach ($pattern in $patterns) {
        $matches = Get-Item $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($exe in $matches) {
            if (Test-PythonExe $exe.FullName) {
                return @{ Cmd = $exe.FullName; Args = @() }
            }
        }
    }
    return $null
}

function Find-Python {
    $fromDisk = Find-PythonFromDisk
    if ($fromDisk) { return $fromDisk }

    if (Test-PythonCommand "py" @("-3")) {
        return @{ Cmd = "py"; Args = @("-3") }
    }
    if (Test-PythonCommand "python") {
        return @{ Cmd = "python"; Args = @() }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host "Python 3 not found (only the Microsoft Store stub is in PATH)." -ForegroundColor Red
    Write-Host ""
    Write-Host "Quick fix - run in this folder:" -ForegroundColor Yellow
    Write-Host "  .\install-python.bat"
    Write-Host "Then close this terminal, open a new one, and run:"
    Write-Host "  .\setup.bat"
    Write-Host ""
    Write-Host "Manual install: https://www.python.org/downloads/"
    Write-Host "  - Check 'Add python.exe to PATH'"
    Write-Host "  - Disable alias: Settings > Apps > App execution aliases"
    Write-Host "    (turn OFF python.exe and python3.exe)"
    exit 1
}

Write-Host "Using Python: $($python.Cmd) $($python.Args -join ' ')"
& $python.Cmd @($python.Args + @("-m", "venv", "venv"))
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$pythonExe = Join-Path $PSScriptRoot "venv\Scripts\python.exe"

& $pythonExe -m pip install --upgrade pip
& $pythonExe -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - set BRIDGE_API_KEY before starting."
}

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Start the bridge:"
Write-Host "  .\venv\Scripts\Activate.ps1"
Write-Host "  uvicorn main:app --host 0.0.0.0 --port 8787"
