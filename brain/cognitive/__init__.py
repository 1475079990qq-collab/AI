"""认知调度层（Brain Core V2 · 阶段一）。

八区不是装饰，而是每轮真实执行的一串决定：
    perception.sense  → 听见了什么（信号，不猜情绪标签）
    intent.classify   → 这一句要办的是哪种事
    attention.allocate→ 这一轮该动用哪几个区、说多少话
    planner.plan      → 「这轮打算怎么接」的一条可展示结论

边界：
- 纯规则、纯函数、零模型调用、零 IO；毫秒级，可放热路径。
- 只描述系统自己做出的调度决定（意图 / 参与区 / 表达预算），
  不假装读取大模型内部的隐式思维链。
- 任何异常都由调用方降级为「普通闲聊」，绝不阻断对话。
"""
from .attention import allocate
from .intent import INTENT_CN, classify
from .perception import sense
from .planner import plan

__all__ = ["sense", "classify", "allocate", "plan", "INTENT_CN"]
