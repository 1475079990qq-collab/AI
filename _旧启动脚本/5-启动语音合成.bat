@echo off
setlocal
chcp 936 >nul
title 情感陪伴 - 启动语音合成（8102）
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk:replace"
set "PYTHONUTF8=0"
set "PY=%~dp0.venv\Scripts\python.exe"

echo.
echo   ==================================================
echo     情感陪伴   启动语音合成
echo   --------------------------------------------------
echo       服务      http://127.0.0.1:8102
echo       后端      config.yaml 的 TTS_BACKEND（默认 gpt_sovits）
echo       前置      默认后端要先把 6-启动GPT语音.bat 起起来
echo   --------------------------------------------------
echo     它干什么：把回复的文字念出来
echo     关掉本窗口 → 语音停止，聊天会明确提示语音服务未启动
echo   ==================================================
echo.
if exist "%PY%" goto havevenv
echo.
echo   [第 1 步] 第一次运行，正在创建本地运行环境，请等一分钟 ...
python -m venv .venv
if exist "%PY%" goto havevenv
echo.
echo   [失败] 没能创建 .venv，说明这台机器上没找到 Python。
echo          请先安装 Python 3.10 或更高版本，
echo          安装时记得勾上 Add Python to PATH，装完重新双击本文件。
echo.
pause
exit /b 1

:havevenv
"%PY%" -c "import uvicorn, fastapi, httpx" >nul 2>&1
if not errorlevel 1 goto run
echo.
echo   [第 2 步] 首次运行，正在安装依赖包，约 1~2 分钟，别关窗口 ...
"%PY%" -m pip install -r requirements.txt

:run
"%PY%" -u -m brain.tts_server
echo.
echo   [已停止] 语音服务已关闭，语音请求会明确报错。
echo.
pause
endlocal
