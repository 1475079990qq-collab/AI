"""离线压缩：把一天的流水压成一段人话，并挑出「还没收尾的事」。

关键取舍：这里**不调用模型**生成摘要。
2B 做摘要会跑偏、会编、还会把自己上一轮的套话再总结一遍。
规则拼装（他今天说过什么 + 当时情绪）已经足够让下一轮的它「记得」，
而且不会引入任何幻觉。

真正需要模型能力的「画像更新」（他是什么人）仍走 profile，每 N 轮一次。
"""
from __future__ import annotations

import datetime

from brain import db

_EMO_CN = {"happy": "开心", "sad": "难过", "angry": "生气", "fear": "害怕",
           "surprised": "惊讶", "disgust": "厌恶", "care": "关心", "neutral": ""}

# 只在出现这些「指向未来」的词时才记一件待办；问句不算。
_LOOP_MARKERS = ("明天", "后天", "下周", "周末", "回头", "改天", "到时候",
                 "等我", "要去", "准备去", "还没", "打算")
_LOOP_SKIP_TAIL = ("？", "?", "吗", "呢")

_MAX_LINES = 8
_FRAG_LEN = 42


def _today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def rebuild_episode(user_id: str, day: str | None = None) -> str:
    """按当天流水重建一段摘要（幂等，可反复调用）。返回落库的文本。"""
    day = day or _today()
    try:
        rows = db.day_rows(user_id, day)
    except Exception:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for r in rows:
        s = str(r.get("summary") or "").strip()
        if len(s) < 4:
            continue
        key = s[:18]
        if key in seen:
            continue
        seen.add(key)
        label = _EMO_CN.get(str(r.get("emotion_label") or ""), "")
        frag = s if len(s) <= _FRAG_LEN else s[:_FRAG_LEN] + "…"
        lines.append("- " + frag + (f"（{label}）" if label else ""))
    text = "\n".join(lines[:_MAX_LINES])
    try:
        db.upsert_episode(user_id, day, text, len(rows))
    except Exception:
        pass
    return text


def detect_open_loop(user_text: str, user_id: str) -> bool:
    """他说了一件「以后要做」的事 → 记成待办。返回是否记下。"""
    t = (user_text or "").strip()
    if len(t) < 8 or len(t) > 80:
        return False
    if t.endswith(_LOOP_SKIP_TAIL):
        return False
    if not any(m in t for m in _LOOP_MARKERS):
        return False
    try:
        db.add_open_loop(user_id, t[:60])
        return True
    except Exception:
        return False
