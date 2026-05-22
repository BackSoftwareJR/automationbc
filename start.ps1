# Quick start bridge on Windows
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path "venv\Scripts\Activate.ps1")) {
    Write-Host "venv not found. Run: .\setup.bat" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".env")) {
    Write-Host ".env not found. Copy .env.example to .env and set BRIDGE_API_KEY." -ForegroundColor Red
    exit 1
}

& ".\venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8787
