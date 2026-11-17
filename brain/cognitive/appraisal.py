"""每轮评估：输出 (v, a, goal_hit)，v/a∈[-1,1]，goal_hit 为本轮推进的 goal id 或 None。

设计约束（技术规格 §1.2 / §3.2 / 实施计划 T17）：
- 规则最小：基于本轮文本、情绪 label/intensity、agent_state、active goals 用规则算出评估值，
  不调用模型。
- L1 纯认知模块：只依赖 agent_state(T15)/goal_manager(T16)/trace，不 import services / routers。
- 降级：任一步失败返回中性评估 {v:0,a:0,goal_hit:None,source:"degraded"} 并记日志，绝不冒泡。
- trace 契约：appraisal 事件 payload 必含 trace_id,v,a,goal_hit,source；
  goal_hit 事件 payload 必含 trace_id,goal_id,delta,status。
"""
from __future__ import annotations

import logging

from brain.cognitive import agent_state, goal_manager, trace as tmod
from brain.schemas import EmotionResult

log = logging.getLogger("brain.appraisal")

_POS = {"好", "开心", "顺利", "成了", "谢谢", "棒", "喜欢"}
_NEG = {"糟", "难", "累", "烦", "失败", "难过", "担心", "焦虑"}


def evaluate(*, user_id: str, emotion: EmotionResult, text: str,
             trace_id: str, turn_id: int | None = None) -> dict:
    """规则最小化评估本轮；任何异常都降级为中性值，不抛给对话链路。"""
    try:
        label = str(getattr(emotion, "label", "neutral") or "neutral")
        intensity = float(getattr(emotion, "intensity", 0.0) or 0.0)
        v = 0.6 if label in ("happy", "care") else (-0.6 if label in ("sad", "fear", "angry") else 0.0)
        a = max(0.0, min(1.0, intensity))  # 情绪强度作为唤醒度，钳制到 [0,1]⊂[-1,1]
        body = str(text or "")
        hit = sum(1 for w in _POS if w in body) - sum(1 for w in _NEG if w in body)
        v = max(-1.0, min(1.0, v + 0.15 * hit))

        # 自身状态：沿用上轮能量，按本轮 v 更新心境；越界由 agent_state 内部钳制
        state = agent_state.get(user_id)
        new_state = agent_state.upsert(
            user_id, valence=v, arousal=a,
            energy=max(0.0, min(1.0, float(state.get("energy", 0.5)))),
            stance="warm" if v > 0.3 else ("guard" if v < -0.3 else "steady"))

        # goal_hit：本轮文本命中目标（复用 detect_from_text）或已有活跃目标，且评估为正
        goals = goal_manager.active(user_id)
        detected = None
        try:
            detected = goal_manager.detect_from_text(user_id, body)
        except Exception as exc:
            log.warning("appraisal detect_from_text 失败: %s", type(exc).__name__)
        goal_hit = None
        if v > 0.2:
            if detected and detected.get("id") is not None:
                goal_hit = detected["id"]
            elif goals:
                goal_hit = goals[0]["id"]

        if goal_hit is not None:
            tmod.write(kind="goal_hit", user_id=user_id, trace_id=trace_id, turn_id=turn_id,
                       payload={"trace_id": trace_id, "goal_id": goal_hit,
                                "delta": round(v, 2), "status": "active"},
                       node_type="activity")
        tmod.write(kind="appraisal", user_id=user_id, trace_id=trace_id, turn_id=turn_id,
                   payload={"trace_id": trace_id, "v": round(v, 3), "a": round(a, 3),
                            "goal_hit": goal_hit, "source": "rule"},
                   node_type="activity")
        return {"v": round(v, 3), "a": round(a, 3), "goal_hit": goal_hit,
                "source": "rule", "state": new_state}
    except Exception as exc:
        log.warning("appraisal.evaluate 异常降级: %s", type(exc).__name__)
        return {"v": 0.0, "a": 0.0, "goal_hit": None, "source": "degraded"}
