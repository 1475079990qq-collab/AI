@echo off
setlocal
chcp 936 >nul
title 情感陪伴 - 启动情感判断模型（8101）
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk:replace"
set "PYTHONUTF8=0"
set "PY=%~dp0.venv\Scripts\python.exe"

echo.
echo   ==================================================
echo     情感陪伴   启动情感判断模型
echo   --------------------------------------------------
echo       服务      http://127.0.0.1:8101
echo       模型      models\chinese-emotion-small
echo   --------------------------------------------------
echo     它干什么：判断这句话是什么情绪，结果交给大脑
echo     大脑不背这个模型、只调用它，所以它能单独开单独关
echo     关掉本窗口 → 判断服务停止，大脑自动退回关键词规则，
echo                  聊天照常，只是情绪判得粗一些
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
"%PY%" -c "import uvicorn, fastapi, httpx, onnxruntime, numpy, tokenizers" >nul 2>&1
if not errorlevel 1 goto checkmodel
echo.
echo   [第 2 步] 首次运行，正在安装依赖包（含判断模型推理库），约 2~5 分钟，别关窗口 ...
"%PY%" -m pip install -r requirements.txt

:checkmodel
if exist "models\chinese-emotion-small\model.onnx" goto run
echo.
echo   [注意] 没找到模型文件 models\chinese-emotion-small\model.onnx
echo          服务照样会起来，但状态会显示未就绪，大脑退回关键词规则。
echo          补装方法见 README 第五节（下载 1064MB，只跑 CPU，不占显存）。
echo.

:run
echo   正在加载判断模型，第一次约 3~8 秒 ...
"%PY%" -u -m brain.judge_server
echo.
echo   [已停止] 判断服务已关闭，大脑现在是关键词规则在工作。
echo            需要时重新双击本文件即可。
echo.
pause
endlocal
