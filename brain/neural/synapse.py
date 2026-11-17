"""突触：节点之间真实存在的关系。

三种关系，语义上不一样，不能混成一个「连线」：
- about      记忆 ──关于──▸ 概念      （这条记忆在说哪个词）
- cooccur    概念 ──同现──▸ 概念      （这两个词总一起出现）
- semantic   记忆 ──相似──▸ 记忆      （两条记忆共享同一个概念，所以它们是一类事）
- lesson     教训 ──关于──▸ 概念      （这条教训是踩在哪个词上换来的）

以前是「空间近邻 + 任意两字词重合」：谁离得近就画一条，两个记忆里都有「今天」
也画一条。那种连线只说明字面碰巧，不能说明它们说的是同一件事；而且一刷新就重算，
连线本身没有权重、没有计数、也没有转录。

现在每条突触都带 weight / activation_count / last_activated，用一次加一点，
长期不用慢慢变淡（见 plasticity）。
"""
from __future__ import annotations

from brain import db

from .neuron import concept_neuron_id, fact_neuron_id, lesson_neuron_id

_MAX_LINKS = 16


def link_fact(*, user_id: str, fact_id: str, concepts: list[str]) -> list[dict]:
    """一条新记忆落库后，把它的关系接上。返回新增/强化的连线（给前端即时生长）。"""
    nid = fact_neuron_id(fact_id)
    terms = [t for t in (concepts or []) if t][:8]
    if not terms:
        return []
    links: list[dict] = []

    # 1) 记忆 → 概念：权重随「这个词在多少条记忆里出现」上升（越普遍越不值得强调，
    #    所以这里用的是 0.5 打底，真正的语义强度靠 repetition 决定的 importance）。
    for term in terms:
        cid = concept_neuron_id(term)
        db.synapse_upsert(user_id=user_id, src=nid, dst=cid, relation="about", weight=0.55)
        links.append({"src": nid, "dst": cid, "relation": "about", "weight": 0.55})

    # 2) 概念之间同现：同一条记忆里同时说的两个词，彼此有关系。
    for i, a in enumerate(terms[:5]):
        for b in terms[i + 1:5]:
            if a == b:
                continue
            db.synapse_upsert(user_id=user_id, src=concept_neuron_id(a),
                              dst=concept_neuron_id(b), relation="cooccur", weight=0.3)
            links.append({"src": concept_neuron_id(a), "dst": concept_neuron_id(b),
                          "relation": "cooccur", "weight": 0.3})

    # 3) 记忆 ↔ 记忆：共享概念就是「同一类事」，这条才是旧实现里最缺的一类边。
    for term in terms[:4]:
        cid = concept_neuron_id(term)
        for row in db.synapse_sources(user_id, [cid], relation="about", limit=6):
            other = str(row.get("src") or "")
            if not other or other == nid:
                continue
            db.synapse_upsert(user_id=user_id, src=nid, dst=other,
                              relation="semantic", weight=0.45)
            links.append({"src": nid, "dst": other, "relation": "semantic", "weight": 0.45})

    return links[:_MAX_LINKS]


def link_lesson(*, user_id: str, lesson_id: int | str, concepts: list[str]) -> list[dict]:
    lid = lesson_neuron_id(lesson_id)
    links: list[dict] = []
    for term in [t for t in (concepts or []) if t][:5]:
        cid = concept_neuron_id(term)
        db.synapse_upsert(user_id=user_id, src=lid, dst=cid, relation="lesson", weight=0.4)
        links.append({"src": lid, "dst": cid, "relation": "lesson", "weight": 0.4})
    return links[:_MAX_LINKS]
