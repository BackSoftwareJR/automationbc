@echo off
cd /d "%~dp0"
echo Installing Python 3.12 via winget...
echo (Close and reopen the terminal after install, then run setup.bat)
echo.
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo.
  echo winget install failed. Install manually:
  echo https://www.python.org/downloads/
  exit /b 1
)
echo.
echo Done. Open a NEW PowerShell window, then run:  setup.bat
pause
