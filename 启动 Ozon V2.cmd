@echo off
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%.venv\Scripts\python.exe" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\install_ozon_v2.ps1"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\launch_workbench.ps1"
)
if errorlevel 1 (
  echo WORKBENCH_START_FAILED
  pause
  exit /b 1
)
exit /b 0
