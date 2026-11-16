"""查知识库：知识库从「每轮都塞进提示词」降级成「问才查」。

这是这次降级的真正目的：不是把知识库扔掉，而是把它变成第二大脑里
那块「翻得动、平时不占地方」的书架。没开的时候如实说没开，不假装检索过。
"""
from __future__ import annotations


def search(query: str, *, user_id: str = "") -> dict:
    body = str(query or "").strip()
    if not body:
        return {"ok": False, "summary": "没说要查什么", "data": {"items": []}}
    try:
        from brain.services import knowledge
    except Exception as exc:
        return {"ok": False, "summary": f"知识库模块不可用：{type(exc).__name__}",
                "data": {"items": []}}
    try:
        payload = knowledge.search(body, top_k=4, fast=True)
    except Exception as exc:
        return {"ok": False, "summary": f"知识库检索失败：{type(exc).__name__}",
                "data": {"items": []}}
    rows = list((payload or {}).get("results") or [])
    if not rows:
        return {"ok": False,
                "summary": "知识库里没翻到相关的内容（也可能是知识库没开）",
                "data": {"items": []}}
    items = []
    for r in rows[:4]:
        items.append({
            "title": str(r.get("title") or r.get("source") or "知识库"),
            "snippet": str(r.get("text") or r.get("content") or "")[:160],
            "url": str(r.get("url") or ""),
        })
    return {"ok": True, "summary": f"在知识库里翻到 {len(items)} 条", "data": {"items": items}}
