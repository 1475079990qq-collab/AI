@echo off
chcp 936 >nul
setlocal
title 下载对话大模型 qwen3:8b（约 5GB，只需一次）

echo.
echo  ============================================
echo    下载大模型 qwen3:8b（约 5GB，只需一次）
echo  ============================================
echo.

set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"

if not exist "%OLLAMA_EXE%" goto nodir

REM 模型服务必须先开着，pull 是连着服务下载的
netstat -ano | findstr "LISTENING" | findstr ":11434" >nul 2>nul
if not %errorlevel%==0 goto noserve

"%OLLAMA_EXE%" list 2>nul | findstr "qwen3:8b" >nul
if %errorlevel%==0 goto have

echo    开始下载，进度条会显示百分比。
echo    下载期间可以干别的，但别关这个窗口。
echo    下完以后：模型自动就位，重开 1-启动全部.bat 即可真聊。
echo.
"%OLLAMA_EXE%" pull qwen3:8b
if %errorlevel%==0 goto ok
echo.
echo    [没下成] 下载中断了（多半是网络）。重新双击本文件会接着断点续传。
echo.
pause
goto done

:ok
echo.
echo    [完成] qwen3:8b 已就位。以后不用再来这个窗口。
echo    双击 0-启动大模型.bat 开服务，再开 1-启动全部.bat 就能真聊了。
echo.
pause
goto done

:have
echo    [已装过] qwen3:8b 已经下载好了，不用再下。
echo    直接双击 0-启动大模型.bat 开服务就行。
echo.
pause
goto done

:noserve
echo    [先开服务] 11434 端口没有大模型服务在跑。
echo.
echo    请先双击 0-启动大模型.bat（那个窗口要一直开着），
echo    然后再回到本文件双击。
echo.
pause
goto done

:nodir
echo    [还没装] 没找到 %OLLAMA_EXE%
echo.
echo    Ollama 还没安装：先装 D://_dl//OllamaSetup.exe（或去 https://ollama.com/download ），
echo    装完再回来双击本文件。
echo.
pause

endlocal
