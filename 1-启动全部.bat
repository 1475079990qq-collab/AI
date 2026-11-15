@echo off
setlocal
chcp 936 >nul
title 情感陪伴 - 启动全部（大脑 8000 + 控制台 8001）
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk:replace"
set "PYTHONUTF8=0"
set "PY=%~dp0.venv\Scripts\python.exe"

echo.
echo   ==================================================
echo     情感陪伴   一键启动全部
echo   --------------------------------------------------
echo     一次开全部服务：
echo       大脑          http://127.0.0.1:8000
echo       控制台        http://127.0.0.1:8001/admin.html
echo       情感判断      http://127.0.0.1:8101
echo       语音识别      http://127.0.0.1:8103
echo       语音合成      http://127.0.0.1:8102
echo   --------------------------------------------------
echo     关掉这个窗口 → 全部服务一起停
echo     平时只管双击这个，2 3 4 5 7 号不用管
echo     大模型(Ollama)和 GPT-SoVITS 还是单独起
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
"%PY%" -c "import uvicorn, fastapi, httpx, pydantic" >nul 2>&1
if not errorlevel 1 goto run
echo.
echo   [第 2 步] 首次运行，正在安装依赖包，约 1~3 分钟，别关窗口 ...
"%PY%" -m pip install -r requirements.txt

:run
"%PY%" -u -m brain.launch all
if errorlevel 1 goto fail
echo.
echo   [已停止] 服务已经关闭。
echo.
pause
goto end

:fail
echo.
echo   [出错] 启动失败。详细原因已写到 data\logs\startup-error.log
echo          把这个文件发给开发者就行。
echo.
pause

:end
endlocal
