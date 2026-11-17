"""可塑性：用进、废退，但不遗忘。

- 强化：这一轮被共同点亮的节点，它们之间那条突触权重涨一点（用进）。
- 衰减：长期没激活的节点，activation 按半衰期下降；久不用的突触，weight 也降，
  但有地板值，且永远不删行、不动 importance（废退 ≠ 遗忘）。

这两件事都发生在对话结束之后的线程里，不在首字前的热路径上。
"""
from __future__ import annotations

import logging

from brain import db

log = logging.getLogger("brain.plasticity")


def reinforce(*, user_id: str, request_id: str, seed_ids: list[str],
              spread_ids: list[str]) -> dict:
    """给这一轮点亮的节点记账，并强化参与其中的连线。"""
    items: list[dict] = []
    for nid in (seed_ids or [])[:6]:
        items.append({"id": nid, "energy": 0.45})
    for nid in (spread_ids or [])[:8]:
        items.append({"id": nid, "energy": 0.18})
    if not items:
        return {"activated": 0, "reinforced": 0}
    try:
        db.record_activations(user_id=user_id, request_id=request_id, items=items)
    except Exception:
        return {"activated": 0, "reinforced": 0}
    pairs: list[tuple[str, str]] = []
    for a in (seed_ids or [])[:4]:
        for b in (spread_ids or [])[:6]:
            if a != b:
                pairs.append((a, b))
    reinforced = 0
    if pairs:
        # V2.5 修正：不再只强化 about，四类关系一起强化（技术规格 §2.4）。
        # bump_synapses 只 UPDATE 已有行、不会建新行，所以先用 synapse_upsert
        # 确保四类突触都在，再用 bump_synapses 做「用进」强化（计数+权重）。
        for relation in ("about", "semantic", "cooccur", "lesson"):
            try:
                for a, b in pairs:
                    db.synapse_upsert(user_id=user_id, src=a, dst=b,
                                      relation=relation, weight=0.3, delta=0.0)
                db.bump_synapses(user_id, pairs, relation=relation, delta=0.03)
                reinforced += len(pairs)
            except Exception as exc:
                log.debug("强化 %s 失败: %s", relation, type(exc).__name__)
    return {"activated": len(items), "reinforced": reinforced}


def decay(user_id: str) -> dict:
    try:
        return db.decay(user_id)
    except Exception:
        return {"neurons": 0, "synapses": 0}
