@echo off
chcp 65001 >nul
title YAI 数字员工（真实模型）
cd /d "%~dp0.."
echo 正在启动 YAI 数字员工 · 真实模型（会调用你配置的模型 API，关闭本窗口即退出）...
echo 仓库专员已接沙箱，可现场造代码工具；其余专员无 live 时自动回退离线演示。
".venv\Scripts\python.exe" -m shell.desktop --live
if errorlevel 1 pause
