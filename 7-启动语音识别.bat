@echo off
setlocal
chcp 936 >nul
title 情感陪伴 - 启动语音识别（8103）
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk:replace"
set "PYTHONUTF8=0"
set "PY=%~dp0.venv\Scripts\python.exe"

echo.
echo   ==================================================
echo     情感陪伴   启动语音识别
echo   --------------------------------------------------
echo       服务      http://127.0.0.1:8103
echo       模型      models\sense-voice（SenseVoice，纯 CPU）
echo       占显存    0，不跟大模型抢显卡
echo   --------------------------------------------------
echo     它干什么：把你说的话变成文字，再交给大脑回答
echo     关掉本窗口 → 语音输入停用，打字聊天照常
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
if not errorlevel 1 goto checkasr
echo.
echo   [第 2 步] 首次运行，正在安装基础依赖，约 1~2 分钟，别关窗口 ...
"%PY%" -m pip install -r requirements.txt

:checkasr
"%PY%" -c "import sherpa_onnx" >nul 2>&1
if not errorlevel 1 goto checkmodel
echo.
echo   [第 3 步] 正在安装识别引擎 sherpa-onnx（约 20MB）...
"%PY%" -m pip install sherpa-onnx

:checkmodel
if exist "models\sense-voice\model.int8.onnx" goto run
echo.
echo   [第 4 步] 还没有识别模型，正在下载（约 156MB），等一两分钟 ...
"%PY%" tools\setup_asr_model.py --download

:run
"%PY%" -u -m brain.asr_server
echo.
echo   [已停止] 语音识别已关闭，现在只能键盘打字了。
echo.
pause
endlocal
