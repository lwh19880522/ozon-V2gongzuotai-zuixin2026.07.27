@echo off
setlocal
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\install_ozon_v2.ps1"
if errorlevel 1 (
  echo INSTALL_FAILED
  echo See runtime\workbench logs for details.
  pause
  exit /b 1
)
echo INSTALL_OK
exit /b 0
