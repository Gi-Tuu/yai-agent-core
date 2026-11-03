@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%..\..\.venv\Scripts\python.exe" (
  "%SCRIPT_DIR%..\..\.venv\Scripts\python.exe" "%SCRIPT_DIR%installer.py" --action uninstall %*
) else (
  python "%SCRIPT_DIR%installer.py" --action uninstall %*
)
exit /b %ERRORLEVEL%
