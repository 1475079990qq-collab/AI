"""程序性记忆：把已验证策略/高分教训作为「怎么做」的条目（接口+触发点档）。"""
from __future__ import annotations
from brain import db


def current(user_id: str) -> list[dict]:
    """返回启用中的策略（真实被 orchestrator 消费）。"""
    try:
        s = db.active_strategy(user_id)
        return [s] if s else []
    except Exception:
        return []


def lessons(user_id: str, limit: int = 3) -> list[dict]:
    try:
        return db.lessons_for_prompt(user_id, limit=limit) or []
    except Exception:
        return []
