@echo off
setlocal
cd /d "%~dp0"

set "RELAY_VERSION=2.0.0"
set "SHOONYA_RELAY_PORT=8766"
set "RELAY_URL=https://raw.githubusercontent.com/sandipangadi/DefineEdge-2.0/main/definedge-momentum-collector-v6-production/shoonya_local_relay.py?v=%RELAY_VERSION%"
set "RELAY_FILE=%~dp0shoonya_local_relay_v2.py"
set "RELAY_TEMP=%~dp0shoonya_local_relay_v2.download"

if not exist ".env.shoonya" if not exist "env.shoonya" (
  echo ERROR: Put .env.shoonya or env.shoonya in this same folder.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "try {$h=Invoke-RestMethod -UseBasicParsing 'http://127.0.0.1:8766/health' -TimeoutSec 2; if ($h.relay_version -eq '2.0.0') {exit 0}; exit 2} catch {exit 1}"
if "%errorlevel%"=="0" (
  echo Shoonya relay V2 is already running.
  start "" "http://127.0.0.1:8766/"
  exit /b 0
)
if "%errorlevel%"=="2" (
  echo ERROR: Port 8766 is occupied by a different program.
  echo Close that program, then run this launcher again.
  pause
  exit /b 1
)

echo Downloading verified Shoonya relay V%RELAY_VERSION%...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try {Invoke-WebRequest -UseBasicParsing '%RELAY_URL%' -OutFile '%RELAY_TEMP%'; exit 0} catch {Write-Host $_.Exception.Message; exit 1}"
if errorlevel 1 (
  echo ERROR: Download failed. Check internet access and run again.
  pause
  exit /b 1
)

findstr /C:"RELAY_VERSION" "%RELAY_TEMP%" | findstr /C:"2.0.0" >nul
if errorlevel 1 (
  del "%RELAY_TEMP%" >nul 2>&1
  echo ERROR: Downloaded file is not the expected V%RELAY_VERSION% relay.
  pause
  exit /b 1
)

move /Y "%RELAY_TEMP%" "%RELAY_FILE%" >nul
echo Starting local read-only relay at http://127.0.0.1:8766/

where py >nul 2>&1
if not errorlevel 1 (
  py -3 "%RELAY_FILE%"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo ERROR: Python 3 is not installed or is not on PATH.
    pause
    exit /b 1
  )
  python "%RELAY_FILE%"
)

if errorlevel 1 (
  echo.
  echo Relay stopped with an error. Keep this window open and share only the error text, never credentials or TOTP.
  pause
)
endlocal
