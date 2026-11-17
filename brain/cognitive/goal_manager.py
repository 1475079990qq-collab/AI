"""目标管理：用户明说的落 active；推断的落 paused 待确认（设计文档块②风险）。

设计约束（技术规格 §2.2 / 设计文档块②）：
- 归属：所有查询/写入强制带 user_id（E09）。
- 降级：DB 异常只记日志并返回安全值，绝不冒泡到对话链路。
- L1 纯数据模块：只依赖 brain.db，不 import services / routers。
"""
from __future__ import annotations

import logging
import re

from brain import db

log = logging.getLogger("brain.goal_manager")

STATUS = {"active", "paused", "done", "abandoned"}
SOURCE = {"user_said", "inferred"}
_DEADLINE = re.compile(r"(下周|明天|后天|这周|月底|周[一二三四五六日])")
_WANT = re.compile(r"(要交|要做|得弄|计划|打算|要写|要准备)")


def add(user_id: str, title: str, *, source: str = "inferred") -> dict | None:
    """新建目标；同 user 同 title 且仍 active/paused 时不重复建。

    来源 user_said → active；inferred → paused（待用户确认）。
    非法 title/source 或 DB 异常返回 None，只记日志不阻断。
    """
    title = str(title or "").strip()[:80]
    if not title:
        log.warning("goal_manager.add 拒绝空 title: user=%s", user_id)
        return None
    if source not in SOURCE:
        log.warning("goal_manager.add 拒绝非法 source=%r: user=%s", source, user_id)
        return None
    status = "active" if source == "user_said" else "paused"
    conn = None
    try:
        conn = db.connect()
        row = conn.execute(
            "SELECT id,status FROM goals WHERE user_id=? AND title=? "
            "AND status IN ('active','paused') ORDER BY id DESC LIMIT 1",
            (str(user_id), title)).fetchone()
        if row:
            return {"id": row["id"], "title": title, "status": row["status"],
                    "exists": True}
        cur = conn.execute(
            "INSERT INTO goals(user_id,title,status,source,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (str(user_id), title, status, source, db._now(), db._now()))
        conn.commit()
        return {"id": cur.lastrowid, "title": title, "status": status,
                "source": source}
    except Exception as exc:
        log.warning("goal_manager.add 失败: %s", type(exc).__name__)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def update(user_id: str, goal_id: int, *, status: str) -> bool:
    """改指定用户目标的状态；非法 status 或 DB 异常返回 False，只记日志。"""
    if status not in STATUS:
        log.warning("goal_manager.update 拒绝非法 status=%r: user=%s id=%s",
                    status, user_id, goal_id)
        return False
    conn = None
    try:
        conn = db.connect()
        cur = conn.execute(
            "UPDATE goals SET status=?,updated_at=? WHERE id=? AND user_id=?",
            (status, db._now(), int(goal_id), str(user_id)))
        conn.commit()
        return cur.rowcount > 0
    except Exception as exc:
        log.warning("goal_manager.update 失败: %s", type(exc).__name__)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def active(user_id: str, limit: int = 5) -> list[dict]:
    """列出该用户当前 active 目标（新到旧）；DB 异常返回空列表，只记日志。"""
    conn = None
    try:
        conn = db.connect()
        return [dict(r) for r in conn.execute(
            "SELECT id,title,status,source FROM goals WHERE user_id=? AND status='active' "
            "ORDER BY id DESC LIMIT ?", (str(user_id), int(limit))).fetchall()]
    except Exception as exc:
        log.warning("goal_manager.active 失败: %s", type(exc).__name__)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def detect_from_text(user_id: str, text: str) -> dict | None:
    """从用户原话识别明确目标；无明确表达返回 None（不误判随口一句）。"""
    t = str(text or "")
    if _WANT.search(t) and _DEADLINE.search(t):
        return add(user_id, t[:60], source="user_said")
    return None
