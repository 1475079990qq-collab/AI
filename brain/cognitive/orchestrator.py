"""每轮编排入口：感知->通道->评估->工作区->决策->校验->trace。

L1 层，只 import L0/L1（ brain.cognitive / brain.db / brain.schemas ），
由 pipeline(L2) 反向调用——本模块绝不 import services / routers（单向依赖）。
任何异常降级为最小决策 + degraded trace，绝不打断对话。

三档链路（技术规格 §5.3）：
- fast    ：跳过 appraisal/motivation/goal_manager/deliberation/verifier/模型填充，
            只写 1 条 kind=trace 最小记录（≤3ms）。
- standard：appraisal + goal_manager + motivation + workspace + decision + verifier，
            无 deliberation、无模型填充（≤10ms）。
- deep    ：全走，含 deliberation + 模型填充 + verifier（除模型外 ≤20ms，模型 ≤800ms）。
"""
from __future__ import annotations

import logging

from brain.cognitive import (channel, appraisal, goal_manager, motivation,
                             workspace as wsmod, decision, verifier, deliberation,
                             trace as tmod)

log = logging.getLogger("brain.orchestrator")

# 由 pipeline 注入模型填充回调（仅 deep 通道使用）；默认不调用模型。
_MINIMAL_CALL = None

# 白名单 kind/node_type（与 trace._KINDS / _NODE_TYPES 对齐）
_KIND_TRACE = "trace"
_KIND_DECISION = "decision"
_NODE_ACTIVITY = "activity"
_NODE_STRUCTURE = "structure"


def _model_fill_call(skeleton: dict, trace_id: str):
    """deep 且注入了回调时，返回给 verifier 的候选 callable。

    callable 内部仍走 decision.fill_with_model；交给 verifier 调用是为了让
    §7.1「模型填充 ≤800ms 超时回退」真正生效——verifier 会对 callable 计时并在
    超时时回退骨架、记 verified=false 的 decision trace。无回调返回 None。
    """
    fn = _MINIMAL_CALL
    if not fn:
        return None
    return lambda sk: decision.fill_with_model(sk, model_fn=fn, timeout_s=0.8)


def _write(kind: str, *, user_id: str, trace_id: str, turn_id: int | None,
           payload: dict, node_type: str) -> None:
    """统一 trace 写入：payload 强制带 trace_id，kind/node_type 用白名单。"""
    try:
        body = dict(payload or {})
        body["trace_id"] = str(trace_id)
        tmod.write(kind=kind, user_id=str(user_id), trace_id=str(trace_id),
                   turn_id=turn_id, payload=body, node_type=node_type)
    except Exception as exc:  # 观测失败绝不影响回答
        log.debug("orchestrator trace 写入失败: %s", type(exc).__name__)


def _fallback(*, user_id: str, trace_id: str, turn_id: int | None) -> dict:
    """异常降级：基础 decision（无候选）+ degraded trace，绝不抛给对话链路。"""
    # 空 workspace（无 signals/goals/recalls）→ build_skeleton 产出 options=[]、chosen_id=None
    ws = wsmod.Workspace(trace_id=str(trace_id), user_id=str(user_id), channel="standard")
    dec = decision.build_skeleton(ws, trace_id=str(trace_id))  # options=[], chosen_id=None
    _write(_KIND_TRACE, user_id=user_id, trace_id=trace_id, turn_id=turn_id,
           payload={"channel": "standard", "degraded": True,
                    "reason": "orchestrator_exception", "decision_summary": "降级回应"},
           node_type=_NODE_ACTIVITY)
    return {"channel": "standard", "decision": dec, "workspace": ws,
            "appraisal": {"v": 0.0, "a": 0.0, "goal_hit": None},
            "verified": True, "degraded": True, "events": [_KIND_TRACE]}


def run(*, user_id: str, trace_id: str, turn_id: int | None,
        text: str, signals: dict, intent: str, emotion,
        emotion_intensity: float, recalls: list[dict], runtime: dict) -> dict:
    """编排一轮：判档 -> 分级调度 -> 产出 decision/workspace/events。

    返回 dict 至少含 channel / decision / workspace / appraisal / events；
    standard/deep 另含 verified / goal_found。异常一律降级，不抛异常。
    """
    try:
        emotion_label = str(getattr(emotion, "label", "neutral") or "neutral")
        ch = channel.classify_channel(
            text=text, signals=signals, intent=intent,
            emotion_label=emotion_label, emotion_intensity=emotion_intensity,
            runtime=runtime)["channel"]

        # ---------- fast：只写最小 trace，不碰 DB（§5.3 跳过表） ----------
        if ch == "fast":
            ws = wsmod.Workspace(
                trace_id=str(trace_id), user_id=str(user_id), channel="fast",
                signals={**(signals or {}), "intent": intent,
                         "text_len": len(str(text or ""))},
                recalls=list(recalls or [])[:10])
            dec = decision.build_skeleton(ws, trace_id=str(trace_id))  # skip=True
            _write(_KIND_TRACE, user_id=user_id, trace_id=trace_id, turn_id=turn_id,
                   payload={"channel": ch, "intent": intent, "emotion": emotion_label,
                            "memory_ids": [str(r.get("id")) for r in ws.recalls if r.get("id")],
                            "decision_summary": "快速回应"},
                   node_type=_NODE_ACTIVITY)
            return {"channel": ch, "decision": dec, "workspace": ws,
                    "appraisal": {"v": 0.0, "a": 0.0, "goal_hit": None},
                    "verified": True, "events": [_KIND_TRACE]}

        # ---------- 非 fast：目标识别（明说才建 active） ----------
        found = goal_manager.detect_from_text(str(user_id), str(text or ""))
        goals = goal_manager.active(str(user_id))
        ws = wsmod.Workspace(
            trace_id=str(trace_id), user_id=str(user_id), channel=ch,
            signals={**(signals or {}), "intent": intent,
                     "text_len": len(str(text or ""))},
            recalls=list(recalls or [])[:10], goals=goals)

        # 评估 + 动机（standard/deep 都走；appraisal 内部写 appraisal/goal_hit trace）
        appr = appraisal.evaluate(user_id=str(user_id), emotion=emotion, text=text,
                                  trace_id=str(trace_id), turn_id=turn_id)
        ws.appraisal = appr
        ws.motivation = motivation.drive(appr.get("state") or {}, goals)

        # deep 才做推演出候选；standard 无 deliberation
        cand = deliberation.deliberate(ws) if ch == "deep" else []
        ws.candidates = cand

        skel = decision.build_skeleton(ws, trace_id=str(trace_id))
        events: list[str] = []
        if appr.get("goal_hit") is not None:
            events.append("goal_hit")
        events.append("appraisal")

        # deep 且开模型才填充并校验；其余直接用规则骨架。
        # 把 fill callable 交给 verifier：既保留 fill_with_model 在链路上，
        # 又由 verifier 施加 ≤800ms 超时，超时/越界一律回退骨架。
        if ch == "deep" and not skel.get("skip"):
            fill_fn = _model_fill_call(skel, str(trace_id))
            if fill_fn is not None:
                dec, verified = verifier.verify_decision(skel, fill_fn)
            else:
                dec, verified = skel, True
        else:
            dec, verified = skel, True

        # 决策 trace：payload 含 trace_id/options/chosen_id/chosen_by/verified
        _write(_KIND_DECISION, user_id=user_id, trace_id=trace_id, turn_id=turn_id,
               payload={k: dec.get(k) for k in
                        ("options", "chosen_id", "chosen_by", "verified")},
               node_type=_NODE_STRUCTURE)
        _write(_KIND_TRACE, user_id=user_id, trace_id=trace_id, turn_id=turn_id,
               payload={"channel": ch, "intent": intent, "emotion": emotion_label,
                        "memory_ids": [str(r.get("id")) for r in ws.recalls if r.get("id")],
                        "decision_summary":
                            str((dec.get("options") or [{}])[0].get("summary", ""))[:40]},
               node_type=_NODE_ACTIVITY)
        events.extend([_KIND_DECISION, _KIND_TRACE])
        if ch == "deep" and not verified:
            events.append("verdict")

        return {"channel": ch, "decision": dec, "workspace": ws, "appraisal": appr,
                "verified": bool(verified), "goal_found": found, "events": events}
    except Exception as exc:
        log.warning("orchestrator.run 异常降级: %s", type(exc).__name__)
        return _fallback(user_id=user_id, trace_id=trace_id, turn_id=turn_id)
