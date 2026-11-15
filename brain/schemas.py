"""共享数据结构与 SSE 事件定义。"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

EMOTION_LABELS: tuple[str, ...] = (
    "happy", "sad", "angry", "surprised", "fear", "disgust", "neutral", "care",
)

EVENT_TYPES: tuple[str, ...] = ("meta", "token", "clarify", "ping", "done", "error")


class ChatRequest(BaseModel):
    user_id: str = Field(default="default", max_length=64)
    session_id: str = Field(default="default", max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    request_id: str | None = None


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    emotion: str = "neutral"
    reference_audio_id: str | None = None
    request_id: str | None = None


class EmotionResult(BaseModel):
    label: str
    intensity: float = 0.5
    confidence: float = 0.0
    degraded: bool = False
    source: str = "model"

    def normalized(self) -> "EmotionResult":
        label = self.label if self.label in EMOTION_LABELS else "neutral"
        return EmotionResult(
            label=label,
            intensity=max(0.0, min(1.0, float(self.intensity))),
            confidence=max(0.0, min(1.0, float(self.confidence))),
            degraded=bool(self.degraded),
            source=self.source,
        )


@dataclass
class TurnEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        payload: dict[str, Any] = {"type": self.type, **self.data}
        return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def ping_frame() -> str:
    """SSE 注释帧心跳，客户端按规范跳过。"""
    return ": ping\n\n"
