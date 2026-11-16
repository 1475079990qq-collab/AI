"""点点勾，就能改下一次怎么给。

以前反馈只是「记下来」，没人看。这里把它变成规则：
* 某一类卡连续被点「不对」→ 这一类先别出了（宁可不给，也别烦人）；
* 整体「不对」占比过半 → 出卡门槛提高（条目要够多、回复要够长才配成卡）。
不调模型、纯统计，60 秒缓存，热路径上几乎零成本。
"""
from __future__ import annotations

import time

from brain import db

_TTL = 60.0
_cache: dict = {"at": 0.0, "policy": None}


def compute(window: int = 40) -> dict:
    """从最近 window 条交付卡里算出策略。没有样本时给中性值。"""
    try:
        rows = db.recent_deliveries(window)
    except Exception:
        rows = []
    rated = [r for r in rows if int(r.get("verdict") or 0) != 0]
    good = sum(1 for r in rated if int(r.get("verdict") or 0) > 0)
    bad = len(rated) - good
    bad_ratio = (bad / len(rated)) if rated else 0.0

    by_kind: dict[str, list[int]] = {}
    for r in rated:
        kind = str(r.get("kind") or "note")
        by_kind.setdefault(kind, []).append(int(r.get("verdict") or 0))

    deny_kinds = [
        kind for kind, vs in by_kind.items()
        if sum(1 for v in vs if v < 0) >= 2 and sum(v for v in vs) < 0
    ]
    allow_kinds = [
        kind for kind, vs in by_kind.items()
        if sum(1 for v in vs if v > 0) >= 2 and sum(v for v in vs) > 0
    ]

    # 整体不对过半 → 门槛抬高：至少 3 条才配成卡，回复也要更长
    min_items = 3 if bad_ratio > 0.5 else 2
    min_reply = 32 if bad_ratio > 0.5 else 24
    return {
        "deny_kinds": deny_kinds,
        "allow_kinds": allow_kinds,
        "min_items": min_items,
        "min_reply": min_reply,
        "rated": len(rated),
        "good": good,
        "bad": bad,
        "bad_ratio": round(bad_ratio, 3),
    }


def policy() -> dict:
    now = time.time()
    if _cache["policy"] is not None and now - _cache["at"] < _TTL:
        return _cache["policy"]
    _cache["policy"] = compute()
    _cache["at"] = now
    return _cache["policy"]


def note_new_feedback() -> None:
    """收到反馈就作废缓存，下一次立刻按新数据算。"""
    _cache["at"] = 0.0
    _cache["policy"] = None
