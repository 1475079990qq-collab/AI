"""多方案推演的薄接口（接口+触发点档，真实被 orchestrator 在 deep 调用）。

2B 模型撑不起自由推演，这里基于工作区材料生成候选，不做长链推理。

设计约束（技术规格 §1.2 / §4 / §7.1 / 实施计划 T22）：
- L1 纯认知模块：不 import services / routers，只依赖 workspace 的 dict 结构，不做 IO、不调模型。
- 输入 workspace，输出 candidates[]（每项含 id/summary/cost/source，source=rule）。
- deep 通道预算 ≤10ms（§7.1），超时降级空候选；异常降级空候选，绝不冒泡。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("brain.deliberation")

_BUDGET_MS = 10.0
_MAX_CANDIDATES = 3


def deliberate(workspace) -> list[dict]:
    """从工作区的目标/记忆/信号里提炼 2-3 个候选方案。无材料返回空。

    任何异常或超出 deep 预算（§7.1 ≤10ms）都降级为空候选列表，绝不冒泡。
    """
    started = time.perf_counter()
    try:
        out: list[dict] = []

        goals = list(getattr(workspace, "goals", None) or [])
        recalls = list(getattr(workspace, "recalls", None) or [])
        signals = dict(getattr(workspace, "signals", None) or {})
        appraisal = dict(getattr(workspace, "appraisal", None) or {})

        # 目标优先：最多 2 个候选，围绕目标标题给下一步
        for i, g in enumerate(goals[:2]):
            title = str(g.get("title", "") or "")[:18] if isinstance(g, dict) else str(g)[:18]
            out.append({"id": f"g{i}", "summary": f"围绕「{title}」给下一步",
                        "cost": "需要多轮跟进", "source": "rule"})

        # 相关旧事：最多 1 个候选
        for i, _r in enumerate(recalls[:1]):
            out.append({"id": f"r{i}", "summary": "先对上一条相关旧事再回答",
                        "cost": "可能偏离当前语境", "source": "rule"})

        # 只有信号/评估、没有目标与回忆时，给 1 个自然回应候选
        if not out and (signals or appraisal):
            out.append({"id": "s0", "summary": "顺着当下的话自然回应",
                        "cost": "无明确推进", "source": "rule"})

        out = out[:_MAX_CANDIDATES]

        # 超预算降级为空候选（§7.1 deep ≤10ms）
        if (time.perf_counter() - started) * 1000.0 > _BUDGET_MS:
            log.debug("deliberation 超出 %sms 预算，降级空候选", _BUDGET_MS)
            return []
        return out
    except Exception as exc:
        log.warning("deliberation 异常降级为空候选: %s", type(exc).__name__)
        return []
