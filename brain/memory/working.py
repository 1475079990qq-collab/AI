"""工作记忆：本轮信号的轻量快照，不落库。"""
from __future__ import annotations


def snapshot(signals: dict, text: str, limit: int = 12) -> dict:
    body = str(text or "")
    return {"length": len(body), "head": body[:limit],
            "intent_hits": list((signals or {}).get("tech_hits") or []),
            "emotion_words": list((signals or {}).get("emotion_words") or [])}
