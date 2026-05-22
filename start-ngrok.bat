@echo off
cd /d "%~dp0"
set "NGROK_EXE=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"
if not exist "%NGROK_EXE%" (
  echo ngrok.exe not found. Install with: winget install -e --id Ngrok.Ngrok
  exit /b 1
)
echo Checking ngrok version...
"%NGROK_EXE%" version
echo.
echo Tunnel to local bridge on port 8787...
echo Copy the https://....ngrok-free.app URL into n8n.
echo.
"%NGROK_EXE%" http 8787
