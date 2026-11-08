@echo off
rem CloudNETP launcher for Windows: double-click to start.
rem Pass -Dev for development mode (auto reload).
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo CloudNETP failed to start. Read the messages above.
  pause
)
