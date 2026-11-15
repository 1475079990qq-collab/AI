"""给每个服务接一份自己的日志文件：窗口照常打印，同时落到 data/logs/。

为什么这样做：
    这个工程的输出八成是 print。要「一句都不漏」，最省事的办法是把
    sys.stdout / sys.stderr 换成「两头写」的代理——窗口一份、文件一份，
    连三方库（uvicorn、sherpa、transformers）的报错也一起收进来。
    需要分级的业务行再单独用 log.info / log.warning。

日志在哪：
    data/logs/<服务名>-YYYYMMDD.log
    中文文件名，一天一个。窗口关掉、服务崩了都还在，事后能翻。

怎么用：
    各服务 main() 的第一行（在 _fix_console 之后）调用 start("tts") 之类，
    之后所有 print 自动进文件，不用改任何一行业务代码。
"""

from __future__ import annotations

import atexit
import io
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

# 服务代号 → 日志文件名里的中文名
SERVICES: dict[str, str] = {
    "all": "全部",
    "brain": "大脑",
    "console": "控制台",
    "judge": "情感判断",
    "tts": "语音合成",
    "asr": "语音识别",
    "gpt": "GPT语音模型",
}

# 日志留多久（天）。给人看的，别攒成几个 G。
KEEP_DAYS = 30

_state: dict[str, object] = {"file": None, "path": None, "name": ""}
_lock = threading.Lock()


class _Tee(io.TextIOBase):
    """把写操作同时转给「原来的窗口」和「日志文件」。

    只做透传，不改内容——所以窗口里什么样，文件里就什么样（不带前缀）。
    """

    def __init__(self, stream, handle) -> None:
        self._stream = stream
        self._handle = handle

    def write(self, text: str) -> int:  # type: ignore[override]
        if not text:
            return 0
        with _lock:
            try:
                self._stream.write(text)
            except Exception:
                pass  # 窗口没了（被关掉）也不能让业务跟着挂
            try:
                self._handle.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        for target in (self._stream, self._handle):
            try:
                target.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._stream.fileno()  # type: ignore[union-attr]

    def reconfigure(self, **kwargs) -> None:  # type: ignore[override]
        try:
            self._stream.reconfigure(**kwargs)  # type: ignore[union-attr]
        except Exception:
            pass

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._stream, "encoding", "utf-8") or "utf-8"

    @property
    def buffer(self):  # type: ignore[override]
        return getattr(self._stream, "buffer", None)

    def __getattr__(self, item):
        return getattr(self._stream, item)


def _root() -> Path:
    """工程根目录（有 data/ 的那一层）。"""
    try:
        from brain.config import settings

        return Path(settings.ROOT)
    except Exception:
        return Path(__file__).resolve().parents[1]


def _prune(log_dir: Path) -> None:
    """删掉超过 KEEP_DAYS 天的日志，别让目录无限长。"""
    cutoff = datetime.now().timestamp() - KEEP_DAYS * 86400
    for old in log_dir.glob("*.log"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except Exception:
            pass


def start(service: str, echo: bool = True) -> Path | None:
    """接上日志。返回日志文件路径；重复调用只生效一次。

    service：SERVICES 里的代号（brain/console/tts/asr/judge/all/gpt）
    echo：控制台要不要打一行「日志已写入 xxx」，脚本里可以关掉
    """
    name = SERVICES.get(service, service)
    if _state["file"] is not None:
        return _state["path"]  # type: ignore[return-value]

    log_dir = _root() / "data" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None

    path = log_dir / f"{name}-{datetime.now():%Y%m%d}.log"
    try:
        handle = open(path, "a", encoding="utf-8", buffering=1)  # 行缓冲，崩了也不丢
    except Exception:
        return None

    _state.update({"file": handle, "path": path, "name": name})
    _prune(log_dir)

    handle.write(
        f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  {name} 启动（pid {__import__('os').getpid()}）=====\n"
    )

    stdout, stderr = sys.stdout, sys.stderr
    sys.stdout = _Tee(stdout, handle)  # type: ignore[assignment]
    sys.stderr = _Tee(stderr, handle)  # type: ignore[assignment]

    # uvicorn 用 logging 打自己的启动/错误行；不让它抢 root，才能流到文件
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(stdout),
            logging.FileHandler(path, encoding="utf-8"),
        ],
        force=True,
    )

    atexit.register(stop)

    # httpx 每发一个请求就 INFO 一行，把「谁在什么时候合成了什么」挤没了；
    # 只留它的告警，正常请求由我们自己记一行更有信息量的。
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if echo:
        print(f"[日志] {name} → {path}")
    return path


def log() -> logging.Logger:
    """业务分级日志用这个：log().info(...)。"""
    return logging.getLogger(_state.get("name") or "companion")


def stop() -> None:
    handle = _state.get("file")
    if handle is None:
        return
    try:
        handle.write(f"===== {datetime.now():%Y-%m-%d %H:%M:%S}  {_state['name']} 退出 =====\n")
        handle.flush()
        handle.close()
    except Exception:
        pass
    _state.update({"file": None})
    sys.stdout = sys.__stdout__  # type: ignore[assignment]
    sys.stderr = sys.__stderr__  # type: ignore[assignment]


def current_path() -> Path | None:
    return _state["path"]  # type: ignore[return-value]
