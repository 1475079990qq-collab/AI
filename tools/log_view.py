"""看日志：把 data/logs/ 下每个服务的日志翻出来，能只挑一个看，也能跟到底。

为什么单独做这个：
    六个 bat 各起一个服务，窗口一关输出就没了。日志已经由 brain/log_setup.py
    落盘，这里只负责「挑一份、看最近几行、想看就一直往下刷」。

用法：
    python tools/log_view.py                # 菜单选
    python tools/log_view.py --list         # 只列清单
    python tools/log_view.py --tail 语音合成  # 看某一个（名字或编号都行）
    python tools/log_view.py --tail 0 --follow   # 0 = 所有日志混在一起，持续刷新
    python tools/log_view.py --tail 语音合成 --lines 200

名字匹配是「包含」：写「合成」也能指到「语音合成」。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "data" / "logs"

# 服务名 → 排序权重（让清单顺序跟启动顺序一致，不是按字母）
ORDER = ["大脑", "控制台", "情感判断", "语音识别", "语音合成", "GPT语音模型", "全部"]


def _fix_stdout() -> None:
    """bat 窗口是 GBK：遇到日志里打不出来的字符就替换，别为一行字崩掉整个查看器。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _name_of(path: Path) -> str:
    """语音合成-20261114.log → 语音合成"""
    stem = path.stem
    return re.sub(r"-\d{8}$", "", stem)


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def collect() -> list[dict]:
    """每个服务只留最新的一天，避免同一天多份看花眼。"""
    if not LOG_DIR.is_dir():
        return []
    newest: dict[str, Path] = {}
    for f in LOG_DIR.glob("*.log"):
        name = _name_of(f)
        if name not in ORDER:
            continue          # 只认这六个服务，别的（历史下载日志之类）不掺进来
        old = newest.get(name)
        if old is None or f.stat().st_mtime > old.stat().st_mtime:
            newest[name] = f

    def key(item: tuple[str, Path]) -> tuple[int, float]:
        name, path = item
        rank = ORDER.index(name) if name in ORDER else len(ORDER)
        return (rank, -path.stat().st_mtime)

    rows = []
    for name, path in sorted(newest.items(), key=key):
        st = path.stat()
        rows.append({
            "name": name,
            "path": path,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime),
        })
    return rows


def print_list(rows: list[dict]) -> None:
    if not rows:
        print(f"  还没有日志。日志在服务启动后自动生成，目录：{LOG_DIR}")
        return
    print(f"  日志目录  {LOG_DIR}")
    print()
    print(f"  {'编号':<4}{'服务':<12}{'大小':>9}   {'最后写入':<10} 文件")
    print("  " + "-" * 68)
    for i, row in enumerate(rows, 1):
        print(f"  {i:<6}{row['name']:<12}{_human(row['size']):>9}   "
              f"{row['mtime']:%m-%d %H:%M}  {row['path'].name}")
    print()
    print("  0     全部（混在一起，按时间先后排）")


def _clean(line: str) -> str:
    """收拾两类「看着像乱码」的行：

    1. 进度条（tqdm）用 \\r 反复刷同一行 → 只留最后那一帧；
    2. 超长行（模型把整段文本打一行）→ 掐中间，留头尾。
    """
    if "\r" in line:
        line = line.split("\r")[-1]
    line = line.rstrip()
    if len(line) > 400:
        line = line[:260] + f" ……（省略 {len(line) - 340} 字）…… " + line[-80:]
    return line


def read_lines(path: Path, count: int) -> list[str]:
    """只取最后 count 行：日志可能几十 MB，不整个读进来。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-count:]
    except Exception as exc:
        return [f"  [读不了] {path.name}：{exc}\n"]


def _rows_for_all(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["name"] != "全部"]


def show_tail(rows: list[dict], target: str, count: int) -> None:
    """target：编号或名字；0 / 全部 → 所有日志合在一起按时间排。"""
    target = (target or "").strip()
    if target in {"0", "全部", "all"}:
        merged: list[tuple[str, str]] = []
        per = max(count // max(len(rows), 1), 15)
        for row in _rows_for_all(rows):
            for line in read_lines(row["path"], per):
                merged.append((line, row["name"]))
        print(f"  ── 最近 {len(merged)} 行（各服务混排，左侧是服务名）──")
        print()
        for line, name in merged[-count:]:
            print(f"[{name}] {_clean(line)}")
        return

    picked = None
    if target.isdigit():
        idx = int(target)
        if 1 <= idx <= len(rows):
            picked = rows[idx - 1]
    else:
        for row in rows:
            if target in row["name"]:
                picked = row
                break
    if picked is None:
        print(f"  没找到日志「{target}」。用 --list 看看有哪些。")
        return

    print(f"  ── {picked['name']}  最近 {count} 行  {picked['path']} ──")
    print()
    for line in read_lines(picked["path"], count):
        print(_clean(line))


def follow(rows: list[dict], target: str, count: int) -> None:
    """一直刷：像 Linux 的 tail -f，按 Ctrl+C 退出。"""
    target = (target or "").strip()
    if target in {"0", "全部", "all"}:
        paths = [(r["name"], r["path"]) for r in _rows_for_all(rows)]
    else:
        picked = None
        if target.isdigit():
            idx = int(target)
            if 1 <= idx <= len(rows):
                picked = rows[idx - 1]
        else:
            for row in rows:
                if target in row["name"]:
                    picked = row
                    break
        if picked is None:
            print(f"  没找到日志「{target}」。")
            return
        paths = [(picked["name"], picked["path"])]

    show_tail(rows, target, count)
    print()
    print("  ── 下面开始实时刷新，新内容会接着往下滚；按 Ctrl+C 退出 ──")
    print()

    offsets = {p: p.stat().st_size for _, p in paths if p.exists()}
    try:
        while True:
            time.sleep(1.0)
            for name, path in paths:
                if not path.exists():
                    continue
                size = path.stat().st_size
                if size < offsets.get(path, 0):
                    offsets[path] = 0      # 文件被截断/换了，从头读
                if size == offsets.get(path, 0):
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(offsets[path])
                        chunk = f.read()
                    offsets[path] = size
                except Exception:
                    continue
                prefix = f"[{name}] " if len(paths) > 1 else ""
                # 按 \n 拆（不按 \r）：进度条那一坨留给 _clean 收成一帧
                for line in chunk.split("\n"):
                    if line.strip():
                        print(prefix + _clean(line))
    except KeyboardInterrupt:
        print("\n  退出查看。")


def interactive(rows: list[dict]) -> None:
    print_list(rows)
    if not rows:
        return
    print()
    try:
        choice = input("  看哪一个（回车 = 1，q = 退出）：").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if choice.lower() == "q":
        return
    if not choice:
        choice = "1"
    try:
        count = int(input("  看最近多少行（回车 = 60）：").strip() or "60")
    except (EOFError, KeyboardInterrupt, ValueError):
        count = 60
    print()
    show_tail(rows, choice, count)
    print()
    try:
        if input("  要不要接着实时刷新（y = 要，其它 = 不要）：").strip().lower() == "y":
            follow(rows, choice, 20)
    except (EOFError, KeyboardInterrupt):
        pass


def main() -> None:
    _fix_stdout()
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--list", action="store_true", help="只列清单")
    parser.add_argument("--tail", default="", help="看哪一个：编号或名字（0 = 全部）")
    parser.add_argument("--lines", type=int, default=60, help="看最近多少行，默认 60")
    parser.add_argument("--follow", action="store_true", help="持续刷新，Ctrl+C 退出")
    args = parser.parse_args()

    rows = collect()
    if args.list:
        print_list(rows)
        return
    if not rows:
        print_list(rows)
        return
    if args.tail:
        if args.follow:
            follow(rows, args.tail, args.lines)
        else:
            show_tail(rows, args.tail, args.lines)
        return
    interactive(rows)


if __name__ == "__main__":
    main()
