"""反馈落权重（L1 · 阶段 4，T41）。

把用户的显式反馈先交给 reflection（T40）折算成「更新意图」，再落到 request_id
关联的 lesson 权重上。

纪律：
- 只依赖标准库 + brain.db + brain.cognitive.trace + brain.evolution.reflection，
  不 import services/routers，不联网、不调模型（L1 规则层）。
- 权重落库只走既有 DAO db.bump_lesson_outcome()，不自开 SQL、不碰策略状态
  （策略回滚/升级是 evaluation T39 的职责，本模块不重复）。
- 方向：负向反馈下调 lesson 权重（这条经验可能带偏），正向上调（被验证），
  中性不动（安全默认）。步长读 runtime.feedback_weight_delta（默认 0.05）。
- 异常降级不抛，只记日志；返回 dict 供 T42 feedback 接线用。
"""
from __future__ import annotations

import logging

from brain import db
from brain.cognitive import trace as tmod
from brain.evolution import reflection

log = logging.getLogger("brain.learner")

_DEFAULT_DELTA = 0.05


def _delta(runtime: dict) -> float:
    """读权重步长；非法配置记 ERROR 并回退安全默认值（不静默吞、不抛）。"""
    raw = runtime.get("feedback_weight_delta", _DEFAULT_DELTA)
    try:
        d = float(raw)
    except (TypeError, ValueError):
        log.error("feedback_weight_delta 配置非法(%r)，回退默认值 %s",
                  raw, _DEFAULT_DELTA)
        return _DEFAULT_DELTA
    if d < 0:
        log.error("feedback_weight_delta 为负(%r)，回退默认值 %s",
                  raw, _DEFAULT_DELTA)
        return _DEFAULT_DELTA
    return d


def apply_feedback(*, user_id: str, signal: str, request_id: str,
                   context: dict | None = None,
                   runtime: dict | None = None,
                   trace_id: str | None = None) -> dict:
    """把一条反馈信号落到 request_id 关联的 lesson 权重上。

    signal: positive / negative / neutral（同义词由 reflection 归一化）。
    context: 可选，交给 reflection 折算意图（verdict / rules / severity 等）；
             缺失 / 非 dict 时 reflection 内部降级到安全默认，不抛。
    返回 dict：changed / signal / delta / verdict / kind / weight 等，异常不抛。
    """
    runtime = runtime or {}
    delta = _delta(runtime)

    # 1) reflection 产出更新意图（内部已对 context 做安全降级，这里再兜一层）
    try:
        intent = reflection.reflect(signal=signal, context=context)
    except Exception:
        log.debug("reflection 调用失败，按原始 signal 降级", exc_info=True)
        intent = {"signal": "neutral", "verdict": "neutral", "weight": 0.5,
                  "kind": "general", "note": "", "how": "", "severity": 0.0}

    norm = str(intent.get("signal") or "neutral").strip().lower()
    result: dict = {
        "signal": norm,
        "delta": 0.0,
        "changed": 0,
        "verdict": intent.get("verdict"),
        "kind": intent.get("kind"),
        "weight_intent": intent.get("weight"),
        "severity": intent.get("severity"),
        "user_id": str(user_id),
        "request_id": str(request_id),
        "source": "explicit",
        "trace_id": trace_id,
    }

    # 2) 中性 / 无法判定：安全默认，不动 lesson 权重
    if norm not in ("positive", "negative"):
        return result

    # 3) 方向：负向→下调(ok=False)；正向→上调(ok=True)。唯一落库入口是既有 DAO。
    ok = (norm == "positive")
    try:
        row = db.bump_lesson_outcome(user_id=user_id, request_id=request_id,
                                     ok=ok, delta_good=delta, delta_bad=delta)
        if row is not None:
            result["changed"] = 1
            result["delta"] = delta if ok else -delta
            result["weight"] = row.get("weight")
    except Exception as exc:
        log.warning("反馈落权重失败(%s)，本次未持久化: %s",
                    type(exc).__name__, str(exc)[:200])

    # 4) 认知轨迹（非阻塞；失败不影响主结果）
    if trace_id:
        try:
            tmod.write(kind="feedback", user_id=user_id, trace_id=trace_id,
                       payload={"signal": norm, "source": "explicit",
                                "changed": result["changed"], "delta": result["delta"]},
                       node_type="activity")
        except Exception:
            log.debug("反馈 trace 写入降级", exc_info=True)

    return result
