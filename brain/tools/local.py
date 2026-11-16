"""本地工具：记住、提醒、查自己记过什么。全部落 SQLite，不联网、不花时间。"""
from __future__ import annotations

import uuid

from brain import db


def remember(text: str, *, user_id: str) -> dict:
    """记一条事实。重复的不重复存（search_facts 命中就不写）。"""
    body = str(text or "").strip()
    if len(body) < 2:
        return {"ok": False, "summary": "没听清要记什么", "data": {}}
    for row in db.search_facts(user_id, body, top_k=3):
        if str(row.get("text") or "").strip() == body:
            return {"ok": True, "summary": f"这条已经记着了：{body}",
                    "data": {"items": [{"title": "已有", "snippet": body}]}}
    db.add_fact(
        fact_id=uuid.uuid4().hex, user_id=user_id, session_id="tool",
        text=body, metadata={"from": "tool:remember"},
    )
    return {"ok": True, "summary": f"已记下：{body}", "data": {"items": [{"snippet": body}]}}


def remind(text: str, *, user_id: str) -> dict:
    """记一件待办（进 open_loops）。到点由主动开口那套去提。"""
    body = str(text or "").strip()
    if len(body) < 2:
        return {"ok": False, "summary": "没听清要提醒什么", "data": {}}
    db.add_open_loop(user_id, body)
    return {"ok": True, "summary": f"记着了，回头提你：{body}",
            "data": {"items": [{"snippet": body}]}}


def recall(text: str, *, user_id: str) -> dict:
    """查他自己记过什么。"""
    rows = db.search_facts(user_id, text, top_k=5)
    if not rows:
        return {"ok": False, "summary": "你这里还没记过相关的事", "data": {"items": []}}
    items = [{"snippet": str(r.get("text") or "")} for r in rows]
    return {"ok": True, "summary": "；".join(i["snippet"] for i in items[:3]),
            "data": {"items": items}}
