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
    try:
        from brain.services import live
        live.emit("fact_saved", fact_text=body[:14])
    except Exception:
        pass
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


# --- 本地文档读取：只读用户主动放进 data/docs/ 的文件，路径归一化后必须在根内 ---
# 注：from __future__ import annotations 已在文件顶部声明，勿在函数中途重复声明。
from pathlib import Path
import logging

log = logging.getLogger("brain.tools.local")


def read_text(rel_path: str, *, base: str | None = None) -> str:
    """rel_path 必须相对 data/docs/；越界返回空串（security-baseline 路径穿越规则）。"""
    root = Path(base) if base else (Path(__file__).resolve().parent.parent.parent / "data" / "docs")
    root = root.resolve()
    try:
        target = (root / str(rel_path or "")).resolve()
        if root != target and root not in target.parents:
            log.warning("本地文档路径越界拒绝: %s", rel_path)
            return ""
        if not target.is_file() or target.stat().st_size > 2_000_000:
            return ""
        return target.read_text(encoding="utf-8", errors="ignore")[:100_000]
    except Exception as exc:
        log.debug("本地文档读取失败: %s", type(exc).__name__)
        return ""
