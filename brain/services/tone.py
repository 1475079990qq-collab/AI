"""语色清洗：把「不像人」的痕迹按固定规则去掉。

为什么要在服务端做，而不是只写进 prompt：
小模型（2B）对「禁止输出括号旁白」这类指令的遵守率很低——实测 16 轮里
有 5 轮正文里带着「（接住情绪）」「（语气轻松，开放不设限）」。
规则能写，但它会漏；漏出来的东西必须在出口处截住。

只删**明确是舞台说明**的括号，正常括号（如「（大概三点）」）不动。
"""
from __future__ import annotations

import re

# 括号里出现这些词，说明它是在描述自己的语气/动作，而不是在说话
_META_WORDS: tuple[str, ...] = (
    "语气", "情绪", "接住", "开放", "轻量", "试探", "停顿", "陪伴",
    "旁白", "动作", "表情", "自我", "平静", "放松", "温柔", "带点",
    "保持", "轻松", "认真", "沉默", "克制", "体贴", "小心",
)

_OPEN = {"（": "）", "(": ")"}
_MAX_INNER = 24          # 括号里超过这么多字，多半是正常内容，不当旁白删

_BLANK_LINES = re.compile(r"\n{2,}")
_SPACE_BEFORE_NL = re.compile(r"[ \t]+\n")


def _is_meta(inner: str) -> bool:
    return any(w in inner for w in _META_WORDS)


def clean(text: str) -> str:
    """整段清洗：给非流式场景（日志、摘要、回放）用。"""
    out: list[str] = []
    buf: list[str] = []
    closing = ""
    for ch in str(text or ""):
        if closing:
            buf.append(ch)
            if ch == closing:
                inner = "".join(buf)[1:-1]
                if _is_meta(inner):
                    buf = []          # 整段旁白丢掉
                else:
                    out.append("".join(buf))   # 正常括号原样放回
                    buf = []
                closing = ""
            elif len(buf) > _MAX_INNER or ch == "\n":
                out.append("".join(buf))
                buf = []
                closing = ""
            continue
        if ch in _OPEN:
            closing = _OPEN[ch]
            buf = [ch]
            continue
        out.append(ch)
    if buf:
        out.append("".join(buf))
    return _tidy("".join(out))


_OPEN_INV = {"）": "（", ")": "("}


def _tidy(text: str) -> str:
    text = _SPACE_BEFORE_NL.sub("\n", text)
    text = _BLANK_LINES.sub("\n", text)
    return text.strip()


class StreamFilter:
    """流式版：边收边删，不打断出字。

    只在遇到「括号未闭合」时短暂扣字（一般 1-3 个 token），
    闭合后立刻判定：是旁白就吞掉，是正常括号就原样放回。
    另外顺手压掉「开头空行」和「连续空行」——旁白删掉后留下的空档不该被看见。
    """

    def __init__(self) -> None:
        self._closing = ""
        self._buf: list[str] = []
        self._started = False       # 还没吐出第一个可见字符
        self._last_nl = False       # 上一个吐出去的字符是不是换行

    def _emit(self, out: list[str], piece: str) -> None:
        for ch in piece:
            if not self._started:
                if ch.isspace():
                    continue        # 开头的空行 / 空格一律不吐
                self._started = True
            if ch == "\n":
                if self._last_nl:
                    continue        # 连续空行压成一行
                self._last_nl = True
            else:
                self._last_nl = False
            out.append(ch)

    def feed(self, delta: str) -> str:
        out: list[str] = []
        for ch in str(delta or ""):
            if self._closing:
                self._buf.append(ch)
                if ch == self._closing:
                    inner = "".join(self._buf)[1:-1]
                    if not _is_meta(inner):
                        self._emit(out, "".join(self._buf))
                    self._buf = []
                    self._closing = ""
                elif len(self._buf) > _MAX_INNER or ch == "\n":
                    self._emit(out, "".join(self._buf))
                    self._buf = []
                    self._closing = ""
                continue
            if ch in _OPEN:
                self._closing = _OPEN[ch]
                self._buf = [ch]
                continue
            self._emit(out, ch)
        return "".join(out)

    def flush(self) -> str:
        rest: list[str] = []
        if self._buf:
            self._emit(rest, "".join(self._buf))
        self._buf = []
        self._closing = ""
        return "".join(rest)
