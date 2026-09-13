@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 shoonya_local_relay.py
) else (
  python shoonya_local_relay.py
)
endlocal
