@echo off
setlocal
cd /d "%~dp0"
set "RELAY_URL=https://raw.githubusercontent.com/sandipangadi/DefineEdge-2.0/main/definedge-momentum-collector-v6-production/shoonya_local_relay.py"
set "RELAY_FILE=%~dp0shoonya_local_relay_latest.py"

echo Downloading the latest Shoonya relay...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri '%RELAY_URL%' -OutFile '%RELAY_FILE%'"
if errorlevel 1 (
  echo Could not download the latest relay.
  pause
  exit /b 1
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%RELAY_FILE%"
) else (
  python "%RELAY_FILE%"
)
endlocal
