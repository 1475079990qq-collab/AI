"""激活与扩散：想起一件事的时候，跟它有关的东西也会被带亮。

规则很小，但必须是真实的：
- 只有本轮真的召回的记忆节点会被激活（seed），能量 = 该条记忆的检索分数；
- 从 seed 沿权重最高的突触走一跳（默认 fanout=6），得到「联想」到的节点；
- 只读：热路径不写库。写（计数、权重）交给 plasticity 在后台补上。

这就是「记忆 A → 概念 B → 经验 C」的最小可运行版本：
不是随机亮点，也不是把整张图刷一遍，而是这一轮实际取用的那几个节点亮起来。
"""
from __future__ import annotations

from brain import db

from .neuron import fact_neuron_id


def _label_map(ids: list[str]) -> dict[str, dict]:
    try:
        return db.neuron_labels(ids)
    except Exception:
        return {}


def activate(*, user_id: str, fact_ids: list[str], scores: list[float] | None = None,
             fanout: int = 6, max_seeds: int = 4) -> dict:
    """本轮激活。返回 {seeds, spread, concepts}；任何异常都返回空结果。"""
    ids = [str(x) for x in (fact_ids or []) if str(x).strip()][:max_seeds]
    if not ids:
        return {"seeds": [], "spread": [], "concepts": []}
    seeds: list[dict] = []
    for idx, fid in enumerate(ids):
        nid = fact_neuron_id(fid)
        score = 0.5
        try:
            if scores and idx < len(scores):
                score = float(scores[idx])
        except (TypeError, ValueError):
            score = 0.5
        seeds.append({"id": nid, "energy": round(min(1.0, 0.35 + score * 0.5), 3),
                      "ref": fid})
    try:
        rows = db.synapse_neighbors(user_id, [s["id"] for s in seeds],
                                    limit=fanout * max(1, len(ids)))
    except Exception:
        rows = []
    labels = _label_map([str(r.get("dst") or "") for r in rows])
    spread: list[dict] = []
    seen: set[str] = {s["id"] for s in seeds}
    for row in rows:
        dst = str(row.get("dst") or "")
        if not dst or dst in seen:
            continue
        info = labels.get(dst) or {}
        if not info.get("kind"):
            continue
        seen.add(dst)
        energy = round(min(0.6, 0.12 + float(row.get("weight") or 0.3) * 0.55), 3)
        spread.append({"id": dst, "label": str(info.get("label") or ""),
                       "kind": str(info.get("kind") or ""),
                       "relation": str(row.get("relation") or ""),
                       "energy": energy, "via": str(row.get("src") or "")})
        if len(spread) >= fanout:
            break
    concepts = [s["label"] for s in spread if s["kind"] == "concept" and s["label"]][:6]
    return {"seeds": seeds, "spread": spread, "concepts": concepts}
