"""策略评估：窗口内 after<before 即回滚，否则保持（块⑥ / 阶段 4，T39）。

分数来源（见 evolution/reflection.py，T40）：显式反馈 + 是否重复追问折算而成；
本模块只接收算好的 before/after，不在代码里写死任何分数或阈值。
窗口大小读 runtime 的 eval_window_turns（默认 10，可在控制台热改）。

E04 硬约束：写评估结果 + 改策略状态必须同事务。落库只走 db.apply_evaluation()，
它在一个 conn 上 INSERT evaluation_results + 执行等价策略 SQL 后只 commit 一次，
异常整批回滚。本模块不得自行拆成 add_evaluation_result + rollback_strategy /
record_strategy_outcome 两次顺序写。
"""
from __future__ import annotations

import logging

from brain import db

log = logging.getLogger("brain.evaluation")

_DEFAULT_WINDOW = 10


def decide(before: float, after: float) -> str:
    """可判定规则：分数下降即回滚，否则保持。不引入魔法数字阈值。"""
    return "rollback" if float(after) < float(before) else "keep"


def _window(runtime: dict) -> int:
    """读评估窗口；解析失败记 ERROR 并回退安全默认值（不静默吞、不抛）。"""
    try:
        return int(runtime.get("eval_window_turns", _DEFAULT_WINDOW))
    except (TypeError, ValueError):
        log.error("eval_window_turns 配置非法(%r)，回退默认值 %s",
                  runtime.get("eval_window_turns"), _DEFAULT_WINDOW)
        return _DEFAULT_WINDOW


def evaluate_strategy(*, user_id: str, strategy: str, before: float, after: float,
                      runtime: dict | None = None, trace_id: str | None = None) -> dict:
    """评估一条策略：算 decision，落库+改状态只交给 db.apply_evaluation()。

    异常降级不抛，只记日志；返回结果 dict 供调用方写 trace verdict 事件用。
    """
    before = float(before)
    after = float(after)
    window = _window(runtime or {})
    decision = decide(before, after)

    result = {"strategy": str(strategy), "decision": decision,
              "before": before, "after": after, "window": window,
              "user_id": str(user_id), "trace_id": trace_id,
              "persisted": False}
    try:
        # 唯一落库入口：单事务写结果 + 改策略状态（E04）。禁止拆两次写。
        db.apply_evaluation(user_id=user_id, strategy=strategy, window=str(window),
                            before_score=before, after_score=after, decision=decision)
        result["persisted"] = True
    except Exception as exc:
        log.warning("评估落库失败(%s)，本次结果未持久化: %s",
                    type(exc).__name__, str(exc)[:200])
    return result
