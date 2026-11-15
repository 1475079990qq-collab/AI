# -*- coding: utf-8 -*-
"""一次性脚本：生成 8-看日志.bat（GBK + CRLF），并给 6 号 bat 加上日志重定向。

生成完自己就可以删。留着也无害，改 bat 文案时重跑一次即可。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOG_BAT = r"""@echo off
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
echo     选 1~6 看某一个服务，选 0 看全部混在一起。
echo     看完还能选「实时刷新」，像开着一个监视器。
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
"""


def write_bat(name: str, text: str) -> None:
    path = ROOT / name
    with open(path, "w", encoding="gbk", newline="\r\n") as f:
        f.write(text.lstrip("\n"))
    print("写入", path.name, path.stat().st_size, "字节")


def patch_gpt_bat() -> None:
    """6 号：把 api_v2.py 的输出同时落到 data/logs/GPT语音模型.log。"""
    path = ROOT / "6-启动GPT语音.bat"
    text = path.read_text(encoding="gbk")
    if "GPT语音模型.log" in text:
        print("6 号已有日志重定向，跳过")
        return

    # 1) 在 echo 段落里补上「日志写到哪」
    anchor = 'echo    这个窗口关掉 = 语音模型停掉，对话自动退回占位音，不会断。'
    assert anchor in text, "没找到 6 号的提示行"
    text = text.replace(
        anchor,
        'echo    本窗口不滚字：模型的输出直接写进日志文件\r\n'
        'echo    日志 : %LOGFILE%\r\n'
        'echo    想看：另开窗口双击 8-看日志.bat，选「GPT语音模型」\r\n'
        + anchor,
    )

    # 2) 在 cd 之前定义日志文件（超过 20MB 就清掉重来，免得越滚越大）
    old_cd = 'cd /d "%SOVITS_DIR%"'
    new_cd = (
        'set "LOGDIR=%~dp0data\\logs"\r\n'
        'if not exist "%LOGDIR%" mkdir "%LOGDIR%"\r\n'
        'set "LOGFILE=%LOGDIR%\\GPT语音模型.log"\r\n'
        'if exist "%LOGFILE%" for %%A in ("%LOGFILE%") do if %%~zA GTR 20971520 del "%LOGFILE%"\r\n'
        "\r\n"
        + old_cd
    )
    assert old_cd in text, "没找到 cd 行"
    text = text.replace(old_cd, new_cd, 1)

    # 3) 两个启动分支都加重定向
    text = text.replace(
        '"runtime\\python.exe" api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\\configs\\tts_infer.yaml',
        '"runtime\\python.exe" api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\\configs\\tts_infer.yaml >> "%LOGFILE%" 2>&1',
    )
    text = text.replace(
        'python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\\configs\\tts_infer.yaml',
        'python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\\configs\\tts_infer.yaml >> "%LOGFILE%" 2>&1',
    )

    with open(path, "w", encoding="gbk", newline="") as f:
        f.write(text)
    print("改好", path.name)


if __name__ == "__main__":
    write_bat("8-看日志.bat", LOG_BAT)
    patch_gpt_bat()
