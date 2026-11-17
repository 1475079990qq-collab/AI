"""巩固：把用户原话里的事实沉成带来源的信念；新证据替代旧信念（同一事务语义）。

E04 硬约束（技术规格 §2.4）：
  1) 注册来源 + 写 belief 必须同一事务，否则留孤儿 belief；
  2) 新 belief 插入 + 旧 belief superseded_by 更新必须同一事务。
因此这里自己拿一个 conn，多次 execute 后「只 commit 一次」；任一步异常整批 rollback。
不 import services/routers（L1）；所有读/写带 user_id（E09）；异常降级不抛，记日志。
"""
from __future__ import annotations

import datetime
import logging

from brain import db

log = logging.getLogger("brain.consolidation")

# 来源 kind 白名单（与 db._SRC_KINDS 一致；显式声明以免依赖私有变量）
SOURCE_KINDS = frozenset({"user_said", "local_doc", "remote_kb", "web", "unknown"})

# 只巩固「事实性陈述」，避免把闲聊也沉成信念。
_WANT = ("不吃", "喜欢", "忌口", "我叫", "在", "做")

_TRUST = {
    "user_said": 0.9,
    "local_doc": 0.7,
    "remote_kb": 0.6,
    "web": 0.5,
    "unknown": 0.3,
}


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def consolidate(user_id: str, text: str, *, source_kind: str = "user_said") -> dict:
    """把一句话巩固成「带来源的信念」；同主题旧 active 信念被新证据取代（保留旧行）。

    返回 ``{"stored": bool, "belief_id": int|None, "source_kind": str}``。
    非法来源 kind / 空文本 / 无事实关键词 → 直接拒绝（stored=False，不写库）。
    注册来源、写新信念、supersede 旧信念在同一 conn 上完成后「一次 commit」；
    任一步异常整批 rollback，绝不留下孤儿 source / belief。
    """
    uid = str(user_id or "")
    body = str(text or "").strip()
    kind = str(source_kind or "")
    result: dict = {"stored": False, "belief_id": None, "source_kind": kind}

    if (not body or not uid or kind not in SOURCE_KINDS
            or not any(w in body for w in _WANT)):
        return result

    conn = None
    try:
        conn = db.connect()

        # 1) 注册来源（E09：带 user_id）
        cur = conn.execute(
            "INSERT INTO knowledge_sources(user_id,uri,kind,trust,fetched_at) VALUES(?,?,?,?,?)",
            (uid, f"{kind}://local", kind, _TRUST.get(kind, 0.3), _now()))
        sid = int(cur.lastrowid)

        # 2) 写新信念（E09：带 user_id；新建即 active，superseded_by 默认 NULL）
        predicate = body[:12]
        cur = conn.execute(
            "INSERT INTO beliefs(user_id,subject,predicate,object,confidence,source_id,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (uid, "我", predicate, body[-20:], 0.7, sid, _now()))
        bid = int(cur.lastrowid)

        # 3) 同主题旧 active 信念：新证据替代旧（写 superseded_by，保留旧行）
        old_rows = conn.execute(
            "SELECT id FROM beliefs WHERE user_id=? AND predicate=? "
            "AND id<>? AND superseded_by IS NULL",
            (uid, predicate, bid)).fetchall()
        for old in old_rows:
            conn.execute(
                "UPDATE beliefs SET superseded_by=? WHERE id=? AND user_id=?",
                (bid, int(old["id"]), uid))

        # 4) 唯一一次 commit：上面所有写在同一事务内原子落库
        conn.commit()
        result.update({"stored": True, "belief_id": bid, "source_kind": kind})
    except Exception as exc:
        # 任一步失败 → 整批回滚，绝不留下孤儿 source / belief
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log.debug("巩固失败: %s", type(exc).__name__)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return result
