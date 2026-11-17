"""认知事件流：只有程序真的跑过，才记一条。

这是"脑图必须由真实执行点亮"的落地方式：
  每条事件带 system（哪个系统）、action（做了什么）、because（因为什么）、
  sources（可点亮的真实节点）、region（对应脑区）。

铁律：**没有真实来源，就不写 this 条事件。** 宁可脑图是空的，
也不要为了好看编一条"正在思考"。
"""
from __future__ import annotations

from .contract import CogEvent

# 脑区下标与 web/brain.js 的 regionOf 对齐（0 感知 / 1 情绪 / 2 记忆 / 3 关联 / 4 判断 / 5 表达 / 6 反思 / 7 成长）
REGION = {
    "perception": 0,
    "emotion": 1,
    "memory": 2,
    "association": 3,
    "judge": 4,
    "express": 5,
    "reflect": 6,
    "growth": 7,
}


class EventLog:
    """一轮的事件账本。顺序即真实执行顺序，不做并行合并。"""

    def __init__(self) -> None:
        self._items: list[CogEvent] = []
        self._seq = 0

    def add(self, system: str, action: str, *, because: str = "",
            sources: tuple[str, ...] = (), region: str = "",
            **data) -> CogEvent:
        self._seq += 1
        ev = CogEvent(
            seq=self._seq,
            system=system,
            action=action,
            because=str(because or ""),
            sources=tuple(sources or ()),
            region=region or system,
            data=dict(data),
        )
        self._items.append(ev)
        return ev

    def to_list(self) -> list[dict]:
        return [e.to_dict() for e in self._items]

    def __len__(self) -> int:
        return len(self._items)

    def explain(self) -> str:
        """人类可读的一行一步，给侧栏/日志用（不是给用户的回答）。"""
        lines = []
        for e in self._items:
            parts = [f"[{e.system}] {e.action}"]
            if e.because:
                parts.append(f"因为 {e.because}")
            if e.sources:
                parts.append("点亮 " + ",".join(e.sources))
            lines.append(" ".join(parts))
        return "\n".join(lines)
