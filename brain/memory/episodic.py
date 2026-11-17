"""情景记忆：读现有 episodes 表，返回近期情景（薄封装，真做）。"""
from __future__ import annotations
from brain import db


def recent(user_id: str, days: int = 3) -> list[dict]:
    try:
        return db.get_recent_episodes(user_id, days=days) or []
    except Exception:
        return []


def digest(user_id: str, limit: int = 3) -> str:
    rows = recent(user_id)[:limit]
    return "；".join(str(r.get("text", ""))[:40] for r in rows if r.get("text"))
