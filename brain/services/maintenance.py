"""后台维护：衰减调度（启动一次 + 每 N 轮一次）、backfill 追平。

不在热路径：全部由线程池/启动钩子触发。
"""
from __future__ import annotations

import logging
from threading import Lock

from brain import db
from brain.cognitive import trace as tmod

log = logging.getLogger("brain.maint")

_counter: dict[str, int] = {}
_lock = Lock()


def _runtime_int(runtime: dict, key: str, default: int) -> int:
    try:
        return max(1, int(runtime.get(key, default)))
    except (TypeError, ValueError):
        return default


def startup_maintenance() -> None:
    """启动时：每个用户跑一次 backfill 追平 + 一次衰减。"""
    try:
        from brain.neural import growth, plasticity
        for uid in db.list_fact_users(limit=20):
            after = 0
            for _ in range(200):  # 防御性上限，防游标卡死
                r = growth.backfill(uid, limit=200, after_id=after)
                after = r.get("last_id", 0)
                if r.get("done") or (r.get("grown") == 0 and r.get("skipped") == r.get("facts")):
                    break
            # 教训也要补：只补 facts 的话，历史教训在真实图上永远没有节点（沉淀区空）。
            try:
                growth.backfill_lessons(uid)
            except Exception as exc:
                log.debug("教训节点回填失败 %s: %s", uid, type(exc).__name__)
            try:
                result = plasticity.decay(uid)
                tmod.write(kind="decay_done", user_id=uid, trace_id=tmod.new_trace_id(),
                           payload=result, node_type="activity")
            except Exception as exc:
                log.debug("启动衰减失败 %s: %s", uid, type(exc).__name__)
    except Exception as exc:
        log.warning("启动维护失败: %s", type(exc).__name__)


def maybe_decay(user_id: str, runtime: dict) -> None:
    """每 N 轮后台衰减一次；计数按 user_id 隔离。"""
    every = _runtime_int(runtime, "decay_every_turns", 20)
    with _lock:
        n = _counter.get(user_id, 0) + 1
        _counter[user_id] = n
    if n % every != 0:
        return
    try:
        from brain.neural import plasticity
        result = plasticity.decay(user_id)
        tmod.write(kind="decay_done", user_id=user_id, trace_id=tmod.new_trace_id(),
                   payload=result, node_type="activity")
    except Exception as exc:
        log.debug("周期衰减失败: %s", type(exc).__name__)
