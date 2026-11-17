"""语义记忆：概念节点 + 关系边 + 信念，bigram 召回（D05 不引向量库）。

source_kind 必须落在 T34 source_label 映射认识的集合，不自造值；
事实走 metadata.source_kind，信念走 source_id → knowledge_sources.kind。
"""
from __future__ import annotations

import json

from brain import db

_KNOWN_KINDS = frozenset({"user_said", "web", "remote_kb", "local_doc", "unknown"})


def _safe_kind(value) -> str:
    """规整来源 kind：缺省 / 非法一律 unknown，保证调用方可追溯。"""
    kind = str(value or "")
    return kind if kind in _KNOWN_KINDS else "unknown"


def _belief_kind(user_id: str, source_id) -> str:
    """按 belief.source_id 查 knowledge_sources.kind（E09：带 user_id）。

    查不到 / 查询异常 → unknown，绝不返回 "belief" 这类自造值。
    """
    if source_id is None:
        return "unknown"
    try:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT kind FROM knowledge_sources WHERE id=? AND user_id=?",
                (int(source_id), str(user_id)),
            ).fetchone()
            return _safe_kind(row["kind"]) if row is not None else "unknown"
        finally:
            conn.close()
    except Exception:
        return "unknown"


def recall(user_id: str, query: str, top_k: int = 3) -> list[dict]:
    """复用现有 facts bigram 检索 + 信念命中，来源可追溯。"""
    out: list[dict] = []
    try:
        for f in db.search_facts(user_id, query, top_k=top_k) or []:
            try:
                meta = json.loads(f.get("metadata") or "{}")
            except Exception:
                meta = {}
            src_kind = _safe_kind(meta.get("source_kind") or "user_said")
            out.append({"id": f.get("id"), "text": f.get("text"),
                        "score": f.get("score") or f.get("sim"), "source_kind": src_kind})
    except Exception:
        pass
    try:
        q = str(query or "")
        for b in db.list_beliefs(user_id, limit=20):
            if b.get("subject") and b["subject"] in q:
                out.append({"id": f"belief:{b['id']}", "text":
                            f"{b['subject']}{b.get('predicate','')}{b.get('object','')}",
                            "score": float(b.get("confidence") or 0.5),
                            "source_kind": _belief_kind(user_id, b.get("source_id"))})
    except Exception:
        pass
    return out[:top_k]
