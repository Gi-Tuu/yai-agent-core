@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if exist "%SCRIPT_DIR%..\..\.venv\Scripts\python.exe" (
  "%SCRIPT_DIR%..\..\.venv\Scripts\python.exe" "%SCRIPT_DIR%installer.py" --action install %*
) else (
  python "%SCRIPT_DIR%installer.py" --action install %*
)
exit /b %ERRORLEVEL%
