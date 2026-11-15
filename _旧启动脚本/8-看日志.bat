@echo off

setlocal

chcp 936 >nul

title 情感陪伴 - 看日志

cd /d "%~dp0"

set "PYTHONIOENCODING=gbk:replace"

set "PYTHONUTF8=0"

set "PY=%~dp0.venv\Scripts\python.exe"



echo.

echo   ==================================================

echo     情感陪伴   看日志

echo   --------------------------------------------------

echo     每个服务的输出都在 data\logs\ 里，一天一个文件。

echo     本窗口只看，不会启动也不会停掉任何服务。

echo   --------------------------------------------------

echo     按屏幕上列出的编号挑一个看，选 0 全部混在一起，

echo     还能选「实时刷新」像监视器一样一直刷；控制台网页里也有「服务日志」。

echo   ==================================================

echo.

if exist "%PY%" goto havevenv

echo   [提示] 还没建运行环境，先双击 1-启动全部.bat 跑一次。

echo.

pause

exit /b 1



:havevenv

"%PY%" -u tools\log_view.py

echo.

echo   看完了，按任意键关掉本窗口。

pause

endlocal

