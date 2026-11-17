"""认知轨迹：每轮怎么想，落 SQLite，重启不丢。

设计约束（技术规格 §3）：
- write() 非阻塞：丢给单线程池，失败只记日志，绝不冒泡到对话链路。
- 归属：每条带 user_id；query 强制带 user_id（E09）。
- payload 写入前脱敏（技术规格 §8.3 白名单）。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from brain import db

log = logging.getLogger("brain.trace")

_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace-write")

_KINDS = {"trace", "decision", "appraisal", "memory", "goal_hit",
          "feedback", "verdict", "decay_done", "config_warn"}
_NODE_TYPES = {"structure", "memory", "activity", "emergent"}

_PHONE = re.compile(r"1[3-9]\d{9}")
_LONGNUM = re.compile(r"\d{12,19}")


def new_trace_id() -> str:
    """一轮一个 trace_id；与 request_id 并存。"""
    return "tr_" + uuid.uuid4().hex[:12]


def mask_text(text: str) -> str:
    """日志/trace 脱敏白名单外的内容（手机号、长号码）。"""
    s = str(text or "")
    s = _PHONE.sub("1**********", s)
    s = _LONGNUM.sub(lambda m: "*" * (len(m.group()) - 4) + m.group()[-4:], s)
    return s


def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    """深拷贝一层，只对字符串值脱敏；结构字段原样保留。"""
    out: dict[str, Any] = {}
    for k, v in (payload or {}).items():
        if isinstance(v, str):
            out[k] = mask_text(v)
        elif isinstance(v, list):
            out[k] = [mask_text(x) if isinstance(x, str) else x for x in v]
        elif isinstance(v, dict):
            out[k] = {kk: (mask_text(vv) if isinstance(vv, str) else vv)
                      for kk, vv in v.items()}
        else:
            out[k] = v
    return out


def _do_write(kind, user_id, trace_id, payload, node_type, turn_id) -> None:
    if kind not in _KINDS or node_type not in _NODE_TYPES:
        log.warning("trace kind/node_type 非法: %s/%s", kind, node_type)
        return
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO cognitive_trace(trace_id,user_id,turn_id,kind,node_type,payload,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(trace_id), str(user_id), turn_id, str(kind), str(node_type),
             json.dumps(_sanitize(payload), ensure_ascii=False), db._now()),
        )
        conn.commit()
    except Exception as exc:
        # 观测失败不影响回答：只记日志
        log.warning("trace 写失败: %s", type(exc).__name__)
    finally:
        conn.close()


def write(*, kind: str, user_id: str, trace_id: str, payload: dict[str, Any] | None = None,
          node_type: str = "activity", turn_id: int | None = None) -> None:
    """非阻塞写入（线程池）。热路径调用它不会等 DB。"""
    try:
        _EXEC.submit(_do_write, kind, str(user_id), str(trace_id),
                     payload or {}, str(node_type), turn_id)
    except Exception as exc:
        log.warning("trace submit 失败: %s", type(exc).__name__)


def query(*, user_id: str, trace_id: str | None = None, kind: str | None = None,
          node_type: str | None = None, limit: int = 40, before_id: int = 0) -> list[dict]:
    """分页查询（id DESC，before_id 游标）。归属条件必带。"""
    sql = "SELECT * FROM cognitive_trace WHERE user_id=?"
    args: list[Any] = [str(user_id)]
    if trace_id:
        sql += " AND trace_id=?"; args.append(str(trace_id))
    if kind:
        sql += " AND kind=?"; args.append(str(kind))
    if node_type:
        sql += " AND node_type=?"; args.append(str(node_type))
    if before_id:
        sql += " AND id<?"; args.append(int(before_id))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 40), 200)))
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
