@echo off
chcp 65001 >nul
title YAI 数字员工
cd /d "%~dp0.."
echo 正在启动 YAI 数字员工（原生桌面端，关闭本窗口即退出）...
".venv\Scripts\python.exe" -m shell.desktop
if errorlevel 1 pause
