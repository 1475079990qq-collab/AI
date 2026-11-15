@echo off
chcp 936 >nul
setlocal
title 情感陪伴 - 体检
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk:replace"
set "PYTHONUTF8=0"
set "PY=%~dp0.venv\Scripts\python.exe"

echo.
echo   ==================================================
echo     情感陪伴   体检（只看，不改任何东西）
echo   --------------------------------------------------
echo     进程有没有起、生效的是哪个模型、最近多慢、
echo     自检不合格率、记忆条数、磁盘占用，一屏看完。
echo   ==================================================
echo.

if not exist "%PY%" goto nopy
"%PY%" -u tools\doctor.py
goto done

:nopy
echo   还没建运行环境，先双击 1-启动.bat 跑一次。

:done
echo.
pause
endlocal
