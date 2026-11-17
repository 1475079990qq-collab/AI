"""自身状态：单用户单行，覆盖写。情绪值 [-1,1]，能量 [0,1]，非法钳制并记日志。

设计约束（技术规格 §2.2 / 设计文档块②）：
- 归属：读写强制带 user_id（E09），单用户单行。
- 降级：DB 异常返回默认值 / 已钳制的目标值，绝不冒泡到对话链路。
- L1 纯数据模块：只依赖 brain.db，不 import services / routers。
"""
from __future__ import annotations

import logging

from brain import db

log = logging.getLogger("brain.agent_state")

STANCES = {"steady", "warm", "plain", "guard"}


def _default() -> dict:
    """新用户 / 降级时的零状态（每次返回新对象，避免共享可变字典）。"""
    return {"valence": 0.0, "arousal": 0.0, "energy": 0.5,
            "stance": "steady", "updated_at": ""}


def _clamp(v, lo, hi, name):
    try:
        x = float(v)
    except (TypeError, ValueError):
        x = 0.0
        log.warning("状态字段 %s 非法: %r", name, v)
    if x < lo or x > hi:
        log.warning("状态字段 %s 越界钳制: %r -> [%s,%s]", name, v, lo, hi)
    return max(lo, min(hi, x))


def get(user_id: str) -> dict:
    """读单用户状态；无记录或 DB 异常都回零状态，不抛异常。"""
    conn = None
    try:
        conn = db.connect()
        r = conn.execute(
            "SELECT valence,arousal,energy,stance,updated_at "
            "FROM agent_state WHERE user_id=?", (str(user_id),)
        ).fetchone()
        return dict(r) if r else _default()
    except Exception as exc:
        log.warning("agent_state get 失败: %s", type(exc).__name__)
        return _default()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def upsert(user_id: str, *, valence=0.0, arousal=0.0, energy=0.5,
           stance="steady") -> dict:
    """覆盖写单用户单行；越界值钳制，非法 stance 回 steady。

    DB 异常时仍返回已钳制的目标状态，链路按「没有状态」降级，不抛异常。
    """
    v = _clamp(valence, -1.0, 1.0, "valence")
    a = _clamp(arousal, -1.0, 1.0, "arousal")
    e = _clamp(energy, 0.0, 1.0, "energy")
    s = str(stance) if str(stance) in STANCES else "steady"
    conn = None
    try:
        conn = db.connect()
        conn.execute(
            "INSERT INTO agent_state(user_id,valence,arousal,energy,stance,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "valence=excluded.valence,arousal=excluded.arousal,energy=excluded.energy,"
            "stance=excluded.stance,updated_at=excluded.updated_at",
            (str(user_id), v, a, e, s, db._now()))
        conn.commit()
    except Exception as exc:
        log.warning("agent_state upsert 失败: %s", type(exc).__name__)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return {"valence": v, "arousal": a, "energy": e, "stance": s}
