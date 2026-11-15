@echo off
setlocal
chcp 936 >nul
title 情感陪伴 - 检查服务是否在运行
cd /d "%~dp0"
set "PYTHONIOENCODING=gbk:replace"
set "PYTHONUTF8=0"
set "PY=%~dp0.venv\Scripts\python.exe"

echo.
echo   ==================================================
echo     情感陪伴   检查服务是否在运行
echo   --------------------------------------------------
echo     只检查、不启动、不安装，看完按任意键关掉
echo   ==================================================
echo.

if not exist "%PY%" goto nopy
"%PY%" -u -m brain.launch status
goto done

:nopy
echo   [失败] 还没创建本地运行环境，先双击 1-启动全部.bat 跑一次。
goto done

:done
echo.
echo   提示：上面写着未启动的，双击对应的号就行——
echo         判断 8101 双击 4 号，合成 8102 双击 5 号，
echo         识别 8103 双击 7 号，GPT 模型 9880 双击 6 号，
echo         大模型 11434 双击 0-启动大模型.bat。
echo         合成要先等 6 号起来，其它几个顺序随便。
echo.
pause
endlocal
