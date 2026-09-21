@echo off
chcp 65001 >nul
setlocal
rem 一键启动 host_g 沙箱代码工具演示网页（本地，仅监听 127.0.0.1:8201）。
rem 本脚本位于 examples\host_g_sandbox\，仓库根目录是上两级。
set "ROOT=%~dp0..\..\"
set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [错误] 没有找到虚拟环境：%PY%
  echo 请先在仓库根目录创建虚拟环境并安装依赖（uv sync）。
  pause
  exit /b 1
)

cd /d "%ROOT%"
echo 正在启动 host_g 沙箱演示网页，浏览器将自动打开 http://127.0.0.1:8201 ...
echo 关闭此窗口或按 Ctrl+C 即可停止。
"%PY%" examples\host_g_sandbox\web_app.py %*
echo.
echo 服务已退出。
pause
