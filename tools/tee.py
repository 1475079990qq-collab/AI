"""把别的程序的输出「一分为二」：一边照常滚进 bat 窗口，一边写进日志文件。

为什么要有它：GPT-SoVITS 是第三方整合包，改不了它的代码。原来 6 号 bat 用
`>> 文件 2>&1` 重定向——文件里有了，窗口里却一个字都不滚，看起来像卡住。
接上这个 tee，窗口和文件就都有内容了（和 brain/log_setup.py 的 _Tee 一个思路，
只是那个管不了别人的进程）。

用法（在 bat 里）：
    某程序 ... 2>&1 | "%PY%" -u tools\\tee.py "data\\logs\\GPT语音模型.log"

编码：
    子进程吐出来的是控制台编码（GBK）的字节；窗口接着用 GBK 打出来（打不出的
    字替换成 ?，绝不崩）；文件用 UTF-8 存——和 log_setup 的日志同一套，8 号
    和控制台的「服务日志」面板都能直接读。
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print("用法: tee.py <日志文件路径>   （从管道读，写窗口 + 写文件）")
        sys.exit(2)

    path = Path(args[0])
    path.parent.mkdir(parents=True, exist_ok=True)

    # 文件用 UTF-8、行缓冲；窗口流打不出的字符替换，不让一行乱码弄死整个管道
    handle = open(path, "a", encoding="utf-8", buffering=1, errors="replace")
    out = sys.stdout

    while True:
        chunk = sys.stdin.buffer.read1(8192) if hasattr(sys.stdin, "buffer") else b""
        if not chunk:
            break
        # 子进程吐的是控制台编码（GBK）字节：窗口原样透传（保真、不重编码），
        # 文件要 Unicode 就得解一次——解错了也是替换，不崩
        buf = getattr(out, "buffer", None)
        if buf is not None:
            try:
                buf.write(chunk)
                buf.flush()
            except Exception:
                pass
        else:
            try:
                out.write(chunk.decode("gbk", errors="replace"))
                out.flush()
            except Exception:
                pass
        try:
            handle.write(chunk.decode("gbk", errors="replace"))
        except Exception:
            pass

    handle.close()


if __name__ == "__main__":
    main()
