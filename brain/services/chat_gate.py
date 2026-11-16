"""对话进行中标记：给后台反思一道「别抢正在说的话」的小闸。

原则：宁可少做一轮反思，也不拖正在进行的对话。
- /chat 进入本地对话路径时 `enter_chat()`，离开时 `leave_chat()`；线程安全。
- 后台反思在真正发模型调用前 `wait_idle(timeout)`：有人在聊就最多等 timeout 秒，
  等不到就跳过本轮反思；**对话侧绝不反向等待反思**，这里只有反思单向探测。
- 计数只反映本进程内的 /chat；反思线程不持本标记，也不阻塞新对话进来。
"""
from __future__ import annotations

import threading

_lock = threading.Condition()
_active = 0


def enter_chat() -> None:
    global _active
    with _lock:
        _active += 1


def leave_chat() -> None:
    global _active
    with _lock:
        _active = max(0, _active - 1)
        _lock.notify_all()


def chat_active() -> bool:
    with _lock:
        return _active > 0


def wait_idle(timeout: float) -> bool:
    """等到对话空闲返回 True；超时仍忙返回 False。timeout<=0 时只做一次非阻塞探测。"""
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 3.0
    with _lock:
        if _active == 0:
            return True
        if timeout <= 0:
            return False
        return bool(_lock.wait_for(lambda: _active == 0, timeout=timeout))
