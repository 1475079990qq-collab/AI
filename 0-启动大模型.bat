@echo off
chcp 936 >nul
setlocal
title 对话大模型 Ollama（端口 11434）

echo.
echo  ============================================
echo    对话大模型（Ollama，端口 11434）
echo  ============================================
echo.

set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
set "PY=%~dp0.venv\Scripts\python.exe"

REM 已在跑就别重复起，提示后退出
netstat -ano | findstr "LISTENING" | findstr ":11434" >nul 2>nul
if %errorlevel%==0 goto busy

if not exist "%OLLAMA_EXE%" goto nodir

echo    程序 : %OLLAMA_EXE%
echo    端口 : 11434
echo    模型 : qwen3:8b（第一次要下载约 5GB，见窗口提示）
echo.
echo    看到下面出现 "server running" / 11434 就说明模型服务好了，
echo    再双击 1-启动全部.bat，对话就接上真模型了。
echo.
echo    第一次启动如果提示 "model not found"，本窗口先别关，
echo    另开一个窗口双击 0-下载大模型.bat（只需一次）。
echo.
echo    窗口输出同时写进日志（关了窗口也能查）：
echo    日志 : data\logs\大模型-Ollama.log
echo    这个窗口关掉 = 大模型停掉，聊天会明确提示模型未启动。
echo  ============================================
echo.

set "LOGDIR=%~dp0data\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\大模型-Ollama.log"
if exist "%LOGFILE%" for %%A in ("%LOGFILE%") do if %%~zA GTR 20971520 del "%LOGFILE%"

if not exist "%PY%" goto raw
"%OLLAMA_EXE%" serve 2>&1 | "%PY%" -u "%~dp0tools\tee.py" "%LOGFILE%"
goto done

:raw
"%OLLAMA_EXE%" serve >> "%LOGFILE%" 2>&1
goto done

:busy
echo    [提示] 端口 11434 已经有大模型在跑了（可能托盘里有 Ollama 图标）。
echo.
echo    不用再起一个。想在本窗口看日志：
echo    另开窗口双击 8-看日志.bat，或在控制台网页点「服务日志」。
echo.
pause
goto done

:nodir
echo    [还没装] 没找到 %OLLAMA_EXE%
echo.
echo    这个不是故障，只是 Ollama 还没装。
echo.
echo    1. 如果 D://_dl//OllamaSetup.exe 已经下载好了，直接双击它安装
echo       （一路下一步，装完它自己会在托盘里，不用管）。
echo.
echo    2. 没下载就去官网（或看我发你的安装包）：
echo       https://ollama.com/download
echo.
echo    3. 装完后：双击本文件启动服务，
echo       再双击 0-下载大模型.bat 拉取 qwen3:8b（约 5GB，一次就够）。
echo.
pause

:done
endlocal
