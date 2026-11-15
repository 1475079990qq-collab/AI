@echo off
chcp 936 >nul
setlocal
title GPT-SoVITS 语音模型（端口 9880）

REM ============================================================
REM  改这一行：你解压 GPT-SoVITS 整合包的目录
REM  要求：纯英文路径，别用中文、别带空格（D:/GPT-SoVITS 这样最好）
REM ============================================================
set "SOVITS_DIR=D:/GPT-SoVITS"
set "PY=%~dp0.venv\Scripts\python.exe"

echo.
echo  ============================================
echo    GPT-SoVITS 语音模型服务
echo  ============================================
echo.

if not exist "%SOVITS_DIR%\api_v2.py" goto nodir

echo    目录 : %SOVITS_DIR%
echo    端口 : 9880
echo.
echo    看到下面打印出 http://127.0.0.1:9880 就说明模型好了，
echo    这时再双击 5-启动语音合成.bat，聊天就有真人声音了。
echo.
echo    窗口里会滚动显示模型输出，同时写进日志文件（关了窗口也能查）
echo    日志 : data\logs\GPT语音模型.log
echo    想看：另开窗口双击 8-看日志.bat，或在控制台网页点「服务日志」
echo    这个窗口关掉 = 语音模型停掉，语音请求会明确报错。
echo  ============================================
echo.

set "LOGDIR=%~dp0data\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\GPT语音模型.log"
if exist "%LOGFILE%" for %%A in ("%LOGFILE%") do if %%~zA GTR 20971520 del "%LOGFILE%"

cd /d "%SOVITS_DIR%"
if exist "runtime\python.exe" goto usert
goto useplain

:usert
if not exist "%PY%" goto usert_raw
"runtime\python.exe" -u api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\configs\tts_infer.yaml 2>&1 | "%PY%" -u "%~dp0tools\tee.py" "%LOGFILE%"
goto done

:usert_raw
"runtime\python.exe" -u api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\configs\tts_infer.yaml >> "%LOGFILE%" 2>&1
goto done

:useplain
if not exist "%PY%" goto useplain_raw
python -u api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\configs\tts_infer.yaml 2>&1 | "%PY%" -u "%~dp0tools\tee.py" "%LOGFILE%"
goto done

:useplain_raw
python -u api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\configs\tts_infer.yaml >> "%LOGFILE%" 2>&1
goto done

:nodir
echo    [还没装] 没找到 %SOVITS_DIR%\api_v2.py
echo.
echo    这个不是故障，只是 GPT-SoVITS 整合包还没解压。
echo.
echo    1. 下载整合包（约 3GB，一次就够）：
echo       https://huggingface.co/lj1995/GPT-SoVITS-windows-package/resolve/main/GPT-SoVITS-v3lora-20250228.7z
echo       国内下不动就去官网 README 里的语雀网盘链接。
echo.
echo    2. 解压到纯英文目录，比如 D:/GPT-SoVITS
echo.
echo    3. 右键本文件 - 编辑，把上面 SOVITS_DIR 改成你的目录，保存。
echo.
echo    4. 再双击本文件。
echo.
pause

:done
endlocal
