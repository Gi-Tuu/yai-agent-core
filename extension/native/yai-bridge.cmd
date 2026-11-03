@echo off
rem YAI Agent Core Native Messaging Host 启动包装器（Windows）。
rem Chrome 会以 argv[1] 传入扩展来源（chrome-extension://<id>/），本脚本把它透传给
rem Python Bridge；配置路径由环境变量 YAI_BRIDGE_CONFIG 指定（不放在浏览器消息里）。
setlocal
set "YAI_PYTHON=REPLACE_WITH_ABSOLUTE_PATH_TO_PYTHON_EXE"
set "YAI_BRIDGE_CONFIG=REPLACE_WITH_ABSOLUTE_PATH_TO_BRIDGE_CONFIG_JSON"
"%YAI_PYTHON%" -m yai_core.sidecar.native_bridge %1
exit /b %ERRORLEVEL%
