"""把工程里的 .bat 检查成 Windows 双击就能跑的样子。

为什么要有这个脚本（这三个坑都真踩过，表现都是「窗口闪一下就没」）：
    1. 换行是 LF（Unix 换行）→ `if ... (` 这种多行括号块被切碎，
       报「系统找不到指定的路径」「不是内部或外部命令」。
    2. 编码不对 → bat 必须用 GBK(ANSI) 存，并在开头 chcp 936 与系统
       代码页对齐。用 UTF-8 存中文的 bat，cmd 读着读着就会串行，
       后面的 goto 直接「找不到批处理标签」。
    3. GBK 汉字的第二字节偶尔会落在 & | < > % 这些符号上，
       cmd 会把半个汉字当命令符号。这里一并扫出来。

用法：
    python tools/fix_bat_crlf.py          # 就地修（只修换行，其它问题只报不改）
    python tools/fix_bat_crlf.py --check  # 只检查，有问题时退出码 1
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")
DANGER = set(b'&|<>^%"!()\\;,')


def check_file(path: Path, check_only: bool) -> list[str]:
    raw = path.read_bytes()
    problems: list[str] = []

    for bom in BOMS:
        if raw.startswith(bom):
            problems.append(f"{path.name}: 带 BOM，cmd 会把第一行读成乱码，必须存成「无 BOM」")
            raw = raw[len(bom):]
            break

    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError as exc:
        problems.append(f"{path.name}: 不是 GBK 编码（{exc}），请用记事本另存为 ANSI")
        text = raw.decode("gbk", errors="replace")

    fixed = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if fixed.encode("gbk", errors="replace") != raw:
        problems.append(f"{path.name}: 换行不是纯 CRLF")
        if not check_only:
            path.write_bytes(fixed.encode("gbk", errors="replace"))

    lines = text.split("\n")
    chcp_at = next(
        (i for i, line in enumerate(lines) if line.strip().lower().startswith("chcp")),
        None,
    )
    first_zh = next(
        (i for i, line in enumerate(lines) if any(ord(ch) > 127 for ch in line)),
        None,
    )
    if chcp_at is None:
        problems.append(f"{path.name}: 没有 chcp 936 这一行，中文可能花屏")
    elif first_zh is not None and first_zh < chcp_at:
        problems.append(
            f"{path.name}: 第 {first_zh + 1} 行就有中文，但 chcp 在第 {chcp_at + 1} 行，"
            "中文会按错编码解析"
        )

    for no, line in enumerate(lines, 1):
        for ch in line:
            if ord(ch) < 128:
                continue
            try:
                data = ch.encode("gbk")
            except UnicodeEncodeError:
                problems.append(f"{path.name}:{no} 汉字「{ch}」GBK 里没有，输出会变成问号")
                continue
            if len(data) == 2 and data[1] in DANGER:
                problems.append(
                    f"{path.name}:{no} 汉字「{ch}」GBK 第二字节 0x{data[1]:02x}，"
                    "会被 cmd 当成命令符号，换个词"
                )
    return problems


def main() -> int:
    check_only = "--check" in sys.argv
    bats = sorted(ROOT.glob("*.bat"))
    if not bats:
        print("没找到 .bat 文件")
        return 0

    bad: list[str] = []
    for bat in bats:
        problems = check_file(bat, check_only)
        bad.extend(problems)
        print(("BAD  " if problems else "OK   ") + bat.name)

    if not bad:
        return 0

    print()
    for item in bad:
        print("  -", item)
    if check_only:
        print("\n执行 python tools/fix_bat_crlf.py 可自动修换行；编码和用词要手工改。")
        return 1
    print("\n换行已自动修好；标了「不是 GBK」「带 BOM」「汉字」的需要手工处理。")
    return 0 if not any("GBK" in p or "BOM" in p or "汉字" in p for p in bad) else 1


if __name__ == "__main__":
    raise SystemExit(main())
