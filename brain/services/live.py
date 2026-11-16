"""实时脑图事件总线：把「这一轮大脑实际走过哪几站」广播给控制台。

为什么要它：控制台上的动画如果自己随机抖，那是装饰；只有每一条脉冲都对应
一次真实发生的事（情感判断跑完、记忆取回几条、模型吐出第几个字、语音合成
多少 KB），它才有观察价值。这里就是那条「真事」的来源。

设计约束：
- 绝不阻塞链路：emit() 只做一次 deque 追加 + 往订阅队列 put_nowait，失败全吞。
- 没有 WebSocket：控制台用 SSE，一条 GET 就能看，断线由浏览器自己重连。
- 环形缓冲保留最近 N 条：页面晚打开也能先补上「刚才发生了什么」。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from typing import Any, AsyncIterator

_MAX_EVENTS = 300
_MAX_SUBS = 16
# 晚加入的页面先补最后 40 条，够看出「刚才那一轮」的节奏。
_REPLAY = 40

_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
_subs: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()


def bind_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """应用启动时注入事件循环；worker 线程 emit 时靠它跨线程投递。"""
    global _loop
    _loop = loop


def _deliver(ev: dict[str, Any]) -> None:
    for q in list(_subs):
        try:
            q.put_nowait(ev)
        except Exception:
            # 队列满：这条丢掉，绝不在链路上阻塞或等待。
            pass


def emit(kind: str, **data: Any) -> None:
    """打一个真实事件。任何异常都不抛——观测不能反过来搞坏业务。"""
    ev: dict[str, Any] = {"t": round(time.time(), 3), "kind": str(kind)}
    ev.update(data)
    try:
        with _lock:
            _events.append(ev)
            loop = _loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(_deliver, ev)
        elif not _subs:
            return
        else:
            _deliver(ev)
    except Exception:
        pass


def recent(limit: int = _REPLAY) -> list[dict[str, Any]]:
    with _lock:
        items = list(_events)
    n = max(1, min(int(limit or _REPLAY), _MAX_EVENTS))
    return items[-n:]


async def subscribe() -> AsyncIterator[dict[str, Any]]:
    """给 SSE 用：先补一段历史，再持续推新事件。"""
    if len(_subs) >= _MAX_SUBS:
        # 太多页面同时盯着也没意义，先踢掉最早的订阅者。
        try:
            _subs.pop()
        except Exception:
            pass
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subs.add(q)
    try:
        for ev in recent():
            try:
                q.put_nowait(ev)
            except Exception:
                break
        while True:
            ev = await q.get()
            yield ev
    finally:
        _subs.discard(q)


def sse_frame(ev: dict[str, Any]) -> str:
    return "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
