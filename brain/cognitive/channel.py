"""三信号通道分级（决策台账 D04）：intent + 风险词 + 情绪强度。

规则固定、阈值进 runtime；深度优先判定，异常降级 standard。
纯函数、零 IO，可放热路径（技术规格 §5）。
"""
from __future__ import annotations

DEFAULT_RISK = "钱,转账,健康,病,隐私,删除,离婚,离职,投资,合同"
DEFAULT_PHRASES = "想清楚,认真想,仔细考虑,权衡,想明白"
FAST_INTENTS = {"filler", "greeting", "meta"}
DEEP_INTENTS = {"decision"}


def _split(raw) -> list[str]:
    return [w.strip() for w in str(raw or "").split(",") if w.strip()]


def classify_channel(*, text: str, signals: dict, intent: str,
                     emotion_label: str, emotion_intensity: float,
                     runtime: dict | None = None) -> dict:
    """判定本轮走 fast / standard / deep 通道。

    返回 {"channel": "fast|standard|deep", "reasons": [str], "signals": {...}}。
    深度优先命中；任何异常一律降级 standard 并在 reasons 记 degraded。
    """
    rt = runtime or {}
    try:
        if str(rt.get("channel_enabled", "1")) == "0":
            return {"channel": "standard", "reasons": ["disabled"], "signals": {}}

        high = float(rt.get("channel_high_intensity", "0.75"))
        risk = _split(rt.get("channel_risk_words", DEFAULT_RISK))
        phrases = _split(rt.get("channel_deep_phrases", DEFAULT_PHRASES))
        body = str(text or "")
        try:
            intensity = float(emotion_intensity)
        except (TypeError, ValueError):
            intensity = 0.0

        sig = {"intent": intent, "intensity": intensity}
        reasons: list[str] = []

        # 1) 深度优先（满足任一即命中）
        if intent in DEEP_INTENTS:
            reasons.append("intent=decision")
        if any(w in body for w in risk):
            reasons.append("risk_word")
        if any(w in body for w in phrases):
            reasons.append("deep_phrase")
        if intensity >= high:
            reasons.append("high_emotion")
        if reasons:
            return {"channel": "deep", "reasons": reasons, "signals": sig}

        # 2) 快速（未命中深度，且 intent 属轻量且情绪低于阈值）
        if intent in FAST_INTENTS and intensity < high:
            return {"channel": "fast", "reasons": ["fast_intent_low_emotion"], "signals": sig}

        # 3) 标准：其余一切
        return {"channel": "standard", "reasons": ["default"], "signals": sig}
    except Exception:
        return {"channel": "standard", "reasons": ["degraded"], "signals": {}}
