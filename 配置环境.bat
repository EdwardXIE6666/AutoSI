@echo off
cd /d "%~dp0"
rem Request admin if needed
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator permission...
  powershell -Command "Start-Process '%~f0' -Verb RunAs"
  exit /b
)
if exist "%~dp0_internal\setup_env.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\setup_env.ps1"
) else if exist "%~dp0_internal\���û���.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_internal\���û���.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0���û���.ps1"
)
echo.
pause
