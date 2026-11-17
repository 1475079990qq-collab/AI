"""动机：把状态和能量翻成这一轮的驱动力（drive/priority），供编排选路。

设计约束（技术规格 §1.2 / 实施计划 T18）：
- 规则最小：纯函数，基于 state(valence/energy) 与 goals 用规则算出驱动力，不调用模型。
- L1 纯认知模块：无 IO，不 import services / routers；state/goals 由编排从
  agent_state(T15)/goal_manager(T16) 取得后传入。
- 降级：任一步失败返回中性 {drive:"", priority:0} 并记日志，绝不冒泡。
"""
from __future__ import annotations

import logging

log = logging.getLogger("brain.motivation")


def drive(state: dict, goals: list[dict]) -> dict:
    """纯函数：状态 + 目标 -> {drive, priority}。无 IO。"""
    try:
        v = float((state or {}).get("valence", 0.0) or 0.0)
        energy = float((state or {}).get("energy", 0.5) or 0.5)
        if v < -0.3:
            d = "repair"       # 情绪偏低：先修复关系
        elif goals:
            d = "advance"      # 有活跃目标：推进目标
        else:
            d = "connect"      # 否则：维持连接
        priority = "high" if (v < -0.5 or energy < 0.2) else "normal"
        return {"drive": d, "priority": priority}
    except Exception as exc:
        log.warning("motivation.drive 异常降级: %s", type(exc).__name__)
        return {"drive": "", "priority": 0}
