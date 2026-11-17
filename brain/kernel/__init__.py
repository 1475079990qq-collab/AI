"""认知内核（V3）：程序思考，大模型表达。

对外只暴露三个东西：
    WorldSnapshot   — 一轮开始时的只读事实快照（由 adapters 装填）
    ContentPlan     — 定稿的「说什么」（语言层只能表达，不能改）
    run_turn        — 跑完一轮，返回 TurnOutcome（含真实事件流）

边界：本包不 import brain.services / brain.routers / 任何模型客户端。
      需要真实数据时，由 adapters 显式注入。
"""
from .adapters import snapshot_from_db
from .contract import (Action, Certainty, CheckResult, Claim, CogEvent,
                       ContentPlan, Evidence, RenderResult, TurnOutcome)
from .engine import run_turn
from .knowledge import WorldSnapshot

__all__ = [
    "run_turn", "snapshot_from_db", "WorldSnapshot",
    "ContentPlan", "Claim", "Evidence", "Certainty",
    "Action", "RenderResult", "CheckResult", "CogEvent", "TurnOutcome",
]
