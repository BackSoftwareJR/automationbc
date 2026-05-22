@echo off
cd /d "%~dp0"
set "NGROK_EXE=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"
if not exist "%NGROK_EXE%" (
  echo ngrok.exe not found. Run: winget install -e --id Ngrok.Ngrok
  exit /b 1
)
if "%~1"=="" (
  echo Usage: ngrok-setup.bat YOUR_NGROK_AUTHTOKEN
  echo Get token from: https://dashboard.ngrok.com/get-started/your-authtoken
  exit /b 1
)
"%NGROK_EXE%" config add-authtoken %~1
echo Authtoken saved. Now run: start-ngrok.bat
