"""动态生长：新记忆一落库，节点和连线当轮长出来。

旧实现里，脑图只在页面初始化时 loadFacts() 一次：后端写入新事实，
前端要刷新页面才能看到那颗点。这里的 grow_fact / grow_lesson 是写入侧的生长，
写完就返回「新增了哪颗节点、接上了哪几条线」，pipeline 会把它们塞进
fact_saved / lesson_saved 事件，前端据此增量长一颗，不用整图重算。
"""
from __future__ import annotations

from brain import db

from . import neuron, synapse


def grow_fact(*, user_id: str, fact_id: str, text: str, importance: float = 0.5,
              confidence: float = 0.5) -> dict:
    """一条新事实 → 一颗记忆节点 + 若干概念节点 + 与旧记忆的语义边。

    概念节点只在「这个词已经出现在第二条记忆里」时才长出来：
    说一次的词是这句话的一部分，反复说的词才是他脑子里的一个概念。
    """
    body = str(text or "").strip()
    nid = neuron.ensure_fact_neuron(user_id=user_id, fact_id=fact_id, text=body,
                                    importance=importance, confidence=confidence)
    terms = neuron.extract_concepts(body, limit=10)
    materialized: list[str] = []
    for term in terms:
        try:
            count = db.count_facts_like(user_id, term)
        except Exception:
            count = 0
        if count >= 2:
            neuron.ensure_concept_neuron(user_id=user_id, term=term, occurrences=count)
            materialized.append(term)
    links = synapse.link_fact(user_id=user_id, fact_id=fact_id, concepts=materialized)
    return {"neuron": nid, "concepts": materialized, "links": links}


def grow_lesson(*, user_id: str, lesson_id: int | str, note: str,
                weight: float = 0.5) -> dict:
    body = str(note or "").strip()
    nid = neuron.ensure_lesson_neuron(user_id=user_id, lesson_id=lesson_id,
                                      note=body, weight=weight)
    terms = neuron.extract_concepts(body, limit=8)
    materialized: list[str] = []
    for term in terms:
        try:
            count = db.count_facts_like(user_id, term)
        except Exception:
            count = 0
        if count >= 1:
            neuron.ensure_concept_neuron(user_id=user_id, term=term,
                                         occurrences=max(2, count))
            materialized.append(term)
    links = synapse.link_lesson(user_id=user_id, lesson_id=lesson_id, concepts=materialized)
    return {"neuron": nid, "concepts": materialized, "links": links}


def backfill_lessons(user_id: str, limit: int = 400) -> dict:
    """历史教训补节点：neurons 表里没有 kind='lesson' 的行时，真实图上沉淀区是空的。

    为什么单独补：backfill() 只走 facts，写入侧 grow_lesson 只在「新教训产生」时调用。
    V2.5 上线前攒下的教训（lessons 表里已有、neurons 里没有）永远不会出现在脑图上，
    于是「记忆 → 概念 → 经验」这条链缺最后一跳。这里按 lessons.id 幂等补齐：
    先看 neurons 有没有这颗点，有就跳过；绝不 upsert 已存在的点（那会当成强化）。
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, note, weight, kind FROM lessons WHERE user_id=? ORDER BY id ASC LIMIT ?",
            (str(user_id), int(limit)),
        ).fetchall()
        notes = [dict(r) for r in rows]
        have = conn.execute(
            "SELECT ref_id FROM neurons WHERE user_id=? AND kind='lesson'", (str(user_id),)
        ).fetchall()
        already = {str(r[0]) for r in have}
    finally:
        conn.close()
    grown = 0
    skipped = 0
    for item in notes:
        lid = str(item.get("id"))
        if lid in already:
            skipped += 1
            continue
        try:
            body = str(item.get("note") or "").strip()
            if not body:
                skipped += 1
                continue
            grow_lesson(user_id=user_id, lesson_id=lid, note=body,
                        weight=float(item.get("weight") or 0.5))
            grown += 1
        except Exception:
            continue
    return {"lessons": len(notes), "grown": grown, "skipped": skipped}


def backfill(user_id: str, limit: int = 200, after_id: int = 0) -> dict:
    """按数值游标分批补节点，直到追平最新（幂等、可重复执行）。

    V2.5 修正：旧版固定 ORDER BY created_at ASC LIMIT 300，每轮都读最早那批，
    "新建节点数"永远不减。改用自增游标推进，每次只取游标之后的一批。

    游标用 facts.rowid 而非 facts.id：id 是 TEXT（形如 "bf0"），数值比较无法
    推进；rowid 是插入序的整数键，等价于 created_at 升序，做游标才稳。
    幂等靠「先看有没有」，不能靠 upsert：upsert 遇到已存在的突触会当成强化 +0.03，
    每次重启都补一遍会把权重一路顶到 1.0，那不是生长的样子。
    """
    facts: list[dict] = []
    already: set[str] = set()
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT rowid AS _rid, id, text, kind, importance, confidence FROM facts "
            "WHERE user_id=? AND rowid>? ORDER BY rowid ASC LIMIT ?",
            (str(user_id), int(after_id), int(limit)),
        ).fetchall()
        facts = [dict(r) for r in rows]
        have = conn.execute(
            "SELECT ref_id FROM neurons WHERE user_id=? AND kind='fact'", (str(user_id),)
        ).fetchall()
        already = {str(r[0]) for r in have}
    finally:
        conn.close()
    grown = 0
    skipped = 0
    last_id = int(after_id)
    for f in facts:
        fid = str(f["id"])
        try:
            last_id = max(last_id, int(f["_rid"]))
        except (TypeError, ValueError):
            pass
        if fid in already:
            skipped += 1
            continue
        try:
            grow_fact(user_id=user_id, fact_id=fid, text=str(f.get("text") or ""),
                      importance=float(f.get("importance") or 0.5),
                      confidence=float(f.get("confidence") or 0.5))
            grown += 1
        except Exception:
            continue
    try:
        stats = db.neuron_stats(user_id)
    except Exception:
        stats = {}
    done = len(facts) < int(limit)
    return {"facts": len(facts), "grown": grown, "skipped": skipped,
            "stats": stats, "last_id": last_id, "done": done}
