@echo off
chcp 936 >nul
setlocal
title 情感陪伴 - 启动
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk:replace"
set "PYTHONUTF8=0"
set "PY=%~dp0.venv\Scripts\python.exe"

echo.
echo   ==================================================
echo     情感陪伴   启动
echo   --------------------------------------------------
echo     一个进程同时提供：
echo       聊天与大模型转发   /chat
echo       语音识别与合成     /asr  /tts
echo       后台控制台         /admin.html      ^(同一端口^)
echo       情绪判断           进程内运行，不再单独起服务
echo   --------------------------------------------------
echo     不归这里管、需要你另开的：
echo       对话大模型   F 盘你自己启动的那个
echo       GPT-SoVITS   D:\GPT-SoVITS 里的 api_v2.py
echo   --------------------------------------------------
echo     关掉这个窗口 = 全部停止
echo   ==================================================
echo.

if exist "%PY%" goto havevenv
echo   [1/2] 第一次运行，正在创建本地运行环境，约一分钟 ...
python -m venv .venv
if exist "%PY%" goto havevenv
echo.
echo   [失败] 这台机器上没有找到 Python。
echo          装 Python 3.10 以上，安装时勾上 Add Python to PATH，
echo          装完重新双击本文件。
pause
exit /b 1

:havevenv
"%PY%" -c "import uvicorn, fastapi, httpx, pydantic" >nul 2>&1
if not errorlevel 1 goto run
echo   [2/2] 第一次运行，正在安装依赖包，约 1~3 分钟，别关窗口 ...
"%PY%" -m pip install -r requirements.txt

:run
"%PY%" -u -m brain.launch all
echo.
echo   服务已经停止。
pause
exit /b 0
