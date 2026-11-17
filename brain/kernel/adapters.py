"""适配层：唯一允许碰数据库/外部服务的地方。

内核本身是纯函数（只吃 WorldSnapshot），这一层负责把真实世界的东西
装进快照。换库、换模型、换存储，只改这里，内核不动——
这就是「大模型可以替换，不影响认知核心」的具体做法。

已知的真实数据问题（这里顺手纠正，但不动库）：
  profile 里那句「称呼：改名叫小志或小智」是**给我改名**的记录，
  被历史写入当成了**他的称呼**。本层不把它当 user_name（knowledge 层也不采信）。
"""
from __future__ import annotations

import datetime as _dt

from .knowledge import WorldSnapshot


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def snapshot_from_db(user_id: str = "u1", *, query: str = "",
                     top_k: int = 5) -> WorldSnapshot:
    """从真实库取一轮快照（只读）。任何一段取不到就留空，不阻断。"""
    from brain import db  # 延迟导入：内核本身不依赖 db

    params = _safe(lambda: db.get_runtime_params() or {}, {})
    assistant = str(params.get("assistant_name") or "").strip()
    user_name = str(params.get("user_name") or "").strip()

    # 画像：读 services.profile（它自己只读 db，无副作用）
    profile_text = ""
    try:
        from brain.services.profile import get as _get_profile
        profile_text = str(_get_profile(user_id) or "")
    except Exception:
        profile_text = ""

    rows: list[dict] = []
    try:
        raw = db.search_facts(user_id, query or "身份", max(1, int(top_k)))
        rows = [{"id": str(r["id"]), "text": str(r["text"]),
                 "kind": str(r.get("kind") or ""),
                 "score": float(r.get("score") or 0.0)} for r in raw]
    except Exception:
        rows = []

    has_history = False
    try:
        conn = db.connect()
        try:
            cur = conn.execute("SELECT 1 FROM turn_logs WHERE user_id=? LIMIT 1",
                               (str(user_id),))
            has_history = cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        has_history = False

    today = _safe(lambda: _dt.datetime.now().strftime("%Y年%m月%d日"), "")

    return WorldSnapshot(
        assistant_name=assistant,
        user_name=user_name,
        profile_text=profile_text,
        facts=tuple(rows),
        has_history=has_history,
        today_label=today,
        source="sqlite",
    )
