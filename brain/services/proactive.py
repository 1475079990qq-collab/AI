"""主动开口：没人说话的时候，它先开一次口。

为什么要有：陪伴不是等召唤。它记着他没说完的事，隔一阵问一句，
比「你问我答」更像一个人。

三条不打扰的硬规矩（宁可不说，也别烦到人）：
1. 22:00–08:00 一律闭嘴；
2. 一天最多两次；
3. 最近三小时内聊过，就不开口——他刚说完话，不需要被追问。
"""
from __future__ import annotations

import datetime

from brain import db

QUIET_START = 22
QUIET_END = 8
MAX_PER_DAY = 2
IDLE_HOURS = 3
OWNER = "u1"          # App 与 Web 共用的那个人


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def due(user_id: str = OWNER) -> dict | None:
    """返回该说的一句（还没入库），或 None。

    只在「确实有事可提」时才生成：有没收尾的事，或者有日摘要可回顾。
    没有记忆的新用户不该被硬撩。
    """
    now = _now()
    hour = now.hour
    if hour >= QUIET_START or hour < QUIET_END:
        return None
    try:
        if db.proactive_today_count(user_id) >= MAX_PER_DAY:
            return None
    except Exception:
        return None

    try:
        last = db.last_turn_at(user_id)
    except Exception:
        last = ""
    if last:
        try:
            last_dt = datetime.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
            if (now - last_dt).total_seconds() < IDLE_HOURS * 3600:
                return None
        except ValueError:
            pass
    elif not _has_any_memory(user_id):
        return None

    loops = []
    try:
        loops = db.list_open_loops(user_id, limit=3)
    except Exception:
        loops = []
    for loop in loops:
        text = str(loop.get("text") or "").strip()
        if len(text) >= 6:
            return {"kind": "loop", "text": _polish_loop(text)}

    try:
        episodes = db.get_recent_episodes(user_id, days=2)
    except Exception:
        episodes = []
    if episodes:
        return {"kind": "care", "text": "这两天过得怎么样？有事就说说，没事我也在。"}
    return None


def _polish_loop(text: str) -> str:
    """别把原话原样复述回去（像在念记录）；换个问法，短一点。"""
    t = text.strip().rstrip("。！？?")
    if len(t) > 22:
        t = t[:22] + "…"
    return f"你之前提过「{t}」，后来怎么样了？"


def _has_any_memory(user_id: str) -> bool:
    try:
        return bool(db.get_recent_episodes(user_id, days=7))
    except Exception:
        return False


def ticket(user_id: str = OWNER) -> dict | None:
    """到点就生成一条并入库（落账，避免同一件事反复说）。"""
    item = due(user_id)
    if not item:
        return None
    try:
        db.add_proactive(user_id, item["text"], item["kind"])
    except Exception:
        return None
    item["user_id"] = user_id
    return item
