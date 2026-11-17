"""每轮临时工作区：把摊开的材料装进一个对象，不落库。

设计约束（技术规格 §1.2 / §4 / §7.1 / 实施计划 T19）：
- 纯内存临时对象：只存在于本轮，生命周期由编排（orchestrator）持有，绝不落库。
- L1 纯认知模块：只依赖标准库，不 import services / routers，不做任何 IO。
- 缺字段不报错：所有字段都有默认值；assemble 对 None 安全，异常降级为空 workspace。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("brain.workspace")


@dataclass
class Workspace:
    trace_id: str
    user_id: str
    signals: dict[str, Any] = field(default_factory=dict)
    appraisal: dict[str, Any] = field(default_factory=dict)
    motivation: dict[str, Any] = field(default_factory=dict)
    recalls: list[dict] = field(default_factory=list)
    goals: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    chosen: dict[str, Any] | None = None
    channel: str = "standard"

    def as_payload(self) -> dict:
        """给 trace 的最小快照（不含原文，符合脱敏白名单）。"""
        return {
            "channel": self.channel,
            "intent": str(self.signals.get("intent") or ""),
            "recall_count": len(self.recalls),
            "goal_count": len(self.goals),
            "candidate_ids": [c.get("id") for c in self.candidates if c.get("id")],
            "chosen_id": (self.chosen or {}).get("chosen_id"),
        }


def assemble(*, trace_id: str = "", user_id: str = "", signals=None,
             appraisal=None, motivation=None, recalls=None, goals=None,
             candidates=None, chosen=None, channel: str = "standard") -> Workspace:
    """把本轮各模块产出聚合成一个临时 Workspace；纯内存、不落库。

    任何字段缺失/为 None 都取默认值；任一步异常降级为空 workspace，绝不冒泡。
    """
    try:
        return Workspace(
            trace_id=str(trace_id or ""),
            user_id=str(user_id or ""),
            signals=dict(signals) if signals else {},
            appraisal=dict(appraisal) if appraisal else {},
            motivation=dict(motivation) if motivation else {},
            recalls=list(recalls) if recalls else [],
            goals=list(goals) if goals else [],
            candidates=list(candidates) if candidates else [],
            chosen=dict(chosen) if chosen else None,
            channel=str(channel or "standard"),
        )
    except Exception as exc:
        log.warning("workspace.assemble 异常降级为空 workspace: %s", type(exc).__name__)
        return Workspace(trace_id=str(trace_id or ""), user_id=str(user_id or ""))
