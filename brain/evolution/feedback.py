"""反馈：把「对了 / 不对」落回到记忆权重和策略证据上。

为什么这一步比多写十条规则重要：
没有回执的陪伴系统永远不会收敛——它犯错、被纠正、然后下一轮又犯。
这里把一次点击拆成三件事，各归各类：
1. 这一轮召回的记忆 → 权重微调（记忆进化，自动，可逆）
2. 这一轮用到的教训 → 成功/失败计数（经验进化，影响下次选哪条）
3. 当前启用的策略 → 证据计数（策略进化，失败过多会退役）

所有调整都有上下界，且都只动「权重」，不动文本、不动人格。
"""
from __future__ import annotations

import logging

from brain import db
from brain.cognitive import trace as tmod

from . import strategy as strategy_mod

log = logging.getLogger(__name__)


def on_verdict(*, request_id: str, ok: bool) -> dict:
    """一次反馈的完整落账。任何一步失败都不影响其它步。"""
    summary: dict = {"request_id": str(request_id or ""), "ok": bool(ok)}
    try:
        meta = db.turn_meta(request_id)
        user_id = str(meta.get("user_id") or "")
    except Exception:
        user_id = ""
    summary["user_id"] = user_id
    if not user_id:
        return summary

    # 1) 记忆权重：被想起的这几条，这次是帮上了还是带偏了。
    try:
        facts = db.activated_facts(request_id)
        adjusted = db.adjust_fact_importances(facts, 0.03 if ok else -0.05)
        summary["facts_adjusted"] = adjusted
    except Exception:
        log.debug("调整记忆权重失败", exc_info=True)

    # 2) 经验权重：这一轮学到的教训，被验证还是被推翻。
    try:
        lesson = db.bump_lesson_outcome(user_id=user_id, request_id=request_id, ok=ok)
        if lesson:
            summary["lesson"] = {"id": int(lesson.get("id") or 0),
                                 "weight": float(lesson.get("weight") or 0.5)}
            if ok:
                strategy_mod.candidate_from_lesson(user_id=user_id, lesson=lesson)
    except Exception:
        log.debug("调整教训权重失败", exc_info=True)

    # 3) 策略证据：当前启用的那条，这次算不算数。
    try:
        active = db.active_strategy(user_id)
        if active:
            after = db.record_strategy_outcome(user_id=user_id, name=str(active["name"]), ok=ok)
            summary["strategy"] = {"name": str(active["name"]),
                                   "status": str((after or {}).get("status") or "")}
            # 失败多的策略自动退役后，再试一次把它拉回来的机会都不给，
            # 但会尝试启用同名的候选版本（rollback 由人工/下次验证触发）。
    except Exception:
        log.debug("记录策略证据失败", exc_info=True)

    # ===== V2.5 阶段4 闭环整合（T42）：以下新链路互不牵连，各自降级 =====
    # trace_id 从 turn_meta 派生；turn_logs 无该列时回退为 request_id 派生，
    # 保证 feedback / verdict 两条 trace 能串到同一条反馈上。
    try:
        trace_id = str(meta.get("trace_id") or "").strip() or ("tr_" + str(request_id))
    except Exception:
        trace_id = "tr_" + str(request_id)
    signal = "positive" if ok else "negative"

    # 2b) 学习闭环（T41 · learner.apply_feedback）：把反馈信号转成教训权重调整。
    # 不把 trace_id 交给 learner，避免它再写一条 feedback trace（本函数下面写唯一一条）。
    # learner 的返回值并入 summary，仅新增键，不改既有键。
    try:
        from brain.evolution import learner as _learner
        runtime = db.get_runtime_params()
        result = _learner.apply_feedback(
            user_id=user_id, signal=signal, request_id=str(request_id),
            runtime=runtime, trace_id=None)
        result["trace_id"] = trace_id  # 与本函数的 feedback trace 对齐
        summary["v25_feedback"] = result
    except Exception:
        log.debug("V2.5 反馈落权重失败", exc_info=True)

    # 2c) feedback trace（技术规格 §3.2）：用户反馈落库时写一条活动节点。
    try:
        tmod.write(kind="feedback", user_id=user_id, trace_id=trace_id,
                   payload={"trace_id": trace_id, "signal": signal, "source": "explicit"},
                   node_type="activity")
    except Exception:
        log.debug("V2.5 feedback trace 写入失败", exc_info=True)

    # 4) 策略评估（T39 · evaluation.evaluate_strategy）：负向每次评；
    # 正向攒够一个评估窗口（策略成功+失败轮次）再评。评估落库只走
    # evaluation → db.apply_evaluation 单事务，本函数不自行拆写。
    try:
        from brain.evolution import evaluation as _evaluation
        from brain.evolution import reflection as _reflection

        runtime = db.get_runtime_params()
        active_now = db.active_strategy(user_id)
        if active_now:
            try:
                window = int(runtime.get("eval_window_turns", 10))
            except (TypeError, ValueError):
                window = 10
            succ = int(active_now.get("success") or 0)
            fail = int(active_now.get("failure") or 0)
            turns = succ + fail
            due = (not ok) or window <= 0 or (turns > 0 and turns % window == 0)
            if due:
                # before 取上一次评估分；无历史用中性 0.5（reflection.score 默认基线）。
                prev = db.list_evaluation_results(user_id, limit=1)
                before = 0.5
                if prev:
                    try:
                        before = float(prev[0].get("after_score") or 0.5)
                    except (TypeError, ValueError):
                        before = 0.5
                after = _reflection.score(before, signal=signal)
                result = _evaluation.evaluate_strategy(
                    user_id=user_id, strategy=str(active_now["name"]),
                    before=before, after=after, runtime=runtime, trace_id=trace_id)
                summary["v25_evaluation"] = result

                # 评估返回即 verdict 信息，写一条 verdict trace（非阻塞）。
                try:
                    vtrace_id = str(result.get("trace_id") or trace_id)
                    tmod.write(kind="verdict", user_id=user_id, trace_id=vtrace_id,
                               node_type="emergent",
                               payload={"trace_id": vtrace_id,
                                        "strategy": str(result.get("strategy") or ""),
                                        "before_score": float(result.get("before") or 0.0),
                                        "after_score": float(result.get("after") or 0.0),
                                        "decision": str(result.get("decision") or "keep")})
                except Exception:
                    log.debug("V2.5 verdict trace 写入失败", exc_info=True)
    except Exception:
        log.debug("V2.5 策略评估失败", exc_info=True)

    return summary
