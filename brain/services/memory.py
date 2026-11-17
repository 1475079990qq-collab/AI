"""事实记忆层：只走 SQLite（中文 bigram 相似度），不引入向量库。

为什么不留向量这条路：本程序没有任何语义召回的需求场景，引一个向量库/向量模型
只会多一套索引、多一份要和外部对齐的依赖。数据量到「字面召回明显失手」时再谈，
那时也应该复用外部那套，而不是在这里再建一份。

V2 的改动（针对「能记，但不够聪明」）：
1. 写入前先判断价值（cognitive.salience）：身份/禁忌/偏好/关系/目标 权重高，
   今天的流水低，提问和试探不记。以前是「说过就存」，71 条事实权重全一样。
2. 召回时四路线索合并（词重合 / 最近 / 带情绪 / 重要程度），并向神经网络层
   发起一次真实激活：被想起的节点亮，跟它们连着的一跳（概念/旧记忆）一起带亮。
3. 新事实落库后当轮长节点、接语义边，返回给前端增量生长（不再等页面刷新）。

边界不变：
- 这里只管「关于这个人的记忆」：事实、每日摘要、未完成的事、画像、身份称呼。
- **知识库不在这里出现**，它只在明确提问时作为工具去查。
"""
from __future__ import annotations

import logging

from brain import db
from brain.cognitive import salience
from brain.config import settings
from brain.schemas import new_request_id

log = logging.getLogger(__name__)


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _runtime(top_k: int) -> tuple[float, int]:
    """阈值 / 条数一律来自 db.get_runtime_params()（值为 str，需转换）。"""
    try:
        params = db.get_runtime_params()
    except Exception:
        params = {}
    min_intensity = _as_float(
        params.get("emotion_min_intensity"), settings.EMOTION_MIN_INTENSITY)
    # 运行时 memory_top_k 优先；调用方 top_k 仅作为无配置时的回退。
    eff_top_k = _as_int(params.get("memory_top_k"), top_k)
    return min_intensity, max(1, eff_top_k)


def _min_importance() -> float:
    """低于这个重要度的内容仍然会存，但不进长期记忆的检索池。"""
    try:
        return _as_float(db.get_runtime_params().get("memory_min_importance"), 0.35)
    except Exception:
        return 0.35


def _facts_only(user_id: str, query: str, top_k: int) -> tuple[list[dict], str]:
    """四路线索合并召回（全在 SQLite 本地完成，见 db.search_facts）。"""
    try:
        rows = db.search_facts(user_id, query, top_k)
        facts = [{"id": str(r["id"]), "text": str(r["text"]),
                  "kind": str(r.get("kind") or ""),
                  "importance": float(r.get("importance") or 0.5),
                  "score": float(r.get("score") or 0.0),
                  "why": list(r.get("why") or [])} for r in rows]
        # 低重要度的流水不进上下文：它们留在库里，只是不当成「关于他的事实」用。
        floor = _min_importance()
        facts = [f for f in facts
                 if f["importance"] >= floor or "词重合" in (f.get("why") or [])]
        return facts, "sqlite"
    except Exception:
        log.debug("事实召回失败", exc_info=True)
        return [], "sqlite"


def _identity() -> dict:
    """身份与称呼：每轮直连 prompt 顶部，永不走向检索。

    为什么单独拎出来：名字这类东西一旦跟着「事实」一起进检索池，
    就会出现「记忆里写了新名字、嘴上一句没改」——历史上真发生过。
    身份是确定的，不该有召回率。
    """
    try:
        params = db.get_runtime_params() or {}
    except Exception:
        params = {}
    return {
        "assistant": str(params.get("assistant_name") or "").strip(),
        "user": str(params.get("user_name") or "").strip(),
    }


def _associate(user_id: str, facts: list[dict]) -> dict:
    """让神经网络层真的参与这一轮：这些记忆一响，连着的东西也响。"""
    if not facts:
        return {"seeds": [], "spread": [], "concepts": []}
    try:
        from brain.neural import activation
        return activation.activate(
            user_id=user_id,
            fact_ids=[f["id"] for f in facts],
            scores=[f["score"] for f in facts],
        )
    except Exception:
        log.debug("激活扩散失败", exc_info=True)
        return {"seeds": [], "spread": [], "concepts": []}


def retrieve(user_id: str, query: str, top_k: int = 3) -> dict:
    """返回热路径要用的全部记忆：facts / emotions / profile / episodes / open_loops。

    任何异常都不抛；拿不到的段落留空，prompt 侧自然就不出现。
    """
    try:
        min_intensity, eff_top_k = _runtime(top_k)
    except Exception:
        min_intensity, eff_top_k = settings.EMOTION_MIN_INTENSITY, max(1, int(top_k))

    try:
        emotions = db.recent_emotions(user_id, min_intensity=min_intensity,
                                      limit=eff_top_k)
    except Exception:
        emotions = []
    try:
        facts, source = _facts_only(user_id, query, eff_top_k)
    except Exception:
        facts, source = [], "sqlite"
    # 语义记忆：跨会话的长期语义命中（来源可标注，前端区分"你说过/查到的"）。
    sem = []
    try:
        from brain.memory import semantic
        sem = semantic.recall(user_id, query, top_k=max(1, int(top_k) // 2 or 1))
        # 给前端标"这是你上次说的/查到的"
        for item in sem:
            item["source_label"] = ("这是你上次说的" if item.get("source_kind") == "user_said"
                                    else ("这是查到的" if item.get("source_kind") in ("remote_kb", "web", "local_doc")
                                          else "我不确定"))
    except Exception:
        pass
    # 画像：压缩后的「他是什么人」，每轮都带；取不到就当空。
    try:
        from brain.services.profile import get as _get_profile
        profile_text = _get_profile(user_id)
    except Exception:
        profile_text = ""
    # 每日摘要：最近几天他都在忙什么（压缩过的，不是原文流水）。
    try:
        episodes = db.get_recent_episodes(user_id, days=3)
    except Exception:
        episodes = []
    # 未完成的事：他提过、还没收尾，可以主动问一次。
    try:
        open_loops = db.list_open_loops(user_id, limit=3)
    except Exception:
        open_loops = []
    # 经验：按权重取的几条「下次怎么改」，不再是最新三条。
    try:
        lessons = db.lessons_for_prompt(user_id, limit=3)
    except Exception:
        try:
            lessons = db.list_lessons(user_id, limit=3)
        except Exception:
            lessons = []
    # 当前启用的策略：一次只带一条。
    try:
        from brain.evolution import strategy as _strategy
        strategy_line = _strategy.active_line(user_id)
    except Exception:
        strategy_line = ""

    association = _associate(user_id, facts)
    return {
        "facts": facts,
        "semantic": sem,
        "emotions": emotions,
        "source": source,
        "knowledge": [],          # 已迁出热路径，保留键位兼容旧调用
        "profile": profile_text,
        "episodes": episodes,
        "open_loops": open_loops,
        "lessons": lessons,
        "identity": _identity(),
        "association": association,
        "neurons": association.get("seeds") or [],
        "strategy": strategy_line,
    }


def store_fact(user_id: str, session_id: str, text: str,
               fact_ids: list[str] | None = None,
               emotion_label: str = "") -> dict:
    """把本轮内容按价值筛过之后再落库，并当轮长出节点。

    返回 {"stored": bool, "fact_id", "kind", "importance", "reason",
          "neuron", "concepts", "links"}，供 pipeline 发事件 / 前端增量生长。
    任何异常都不外抛：记不住也不能把这一轮对话搞崩。
    """
    body = str(text or "").strip()
    result: dict = {"stored": False, "fact_id": "", "kind": "", "importance": 0.0,
                    "reason": "", "neuron": "", "concepts": [], "links": []}
    if not body:
        return result

    judge = salience.score(body)
    result.update({"kind": str(judge.get("kind") or ""),
                   "importance": float(judge.get("importance") or 0.0),
                   "reason": str(judge.get("reason") or "")})
    if not judge.get("keep"):
        return result

    # 去重：同一件事反复说不应该攒成一堆同权重的事实。
    try:
        near = db.search_facts(user_id, body, 1)
        if near and float(near[0].get("sim") or 0) >= 0.85:
            result["reason"] = "和已有记忆几乎相同，跳过"
            return result
    except Exception:
        pass

    ids = [str(x) for x in (fact_ids or []) if str(x).strip()]
    primary = ids[0] if ids else new_request_id()
    metadata = {
        "user_id": user_id,
        "session_id": session_id,
        "related_fact_ids": ids,
    }
    if emotion_label:
        metadata["emotion_label"] = str(emotion_label)
    try:
        db.add_fact(fact_id=primary, user_id=user_id, session_id=session_id,
                    text=body, metadata=metadata,
                    kind=str(judge.get("kind") or ""),
                    importance=float(judge.get("importance") or 0.5),
                    confidence=float(judge.get("confidence") or 0.5))
    except Exception:
        log.debug("写事实失败", exc_info=True)
        return result

    result.update({"stored": True, "fact_id": primary})

    # 当轮生长：节点、概念、语义边。失败只影响脑图，不影响记忆本身。
    try:
        from brain.neural import growth
        grown = growth.grow_fact(
            user_id=user_id, fact_id=primary, text=body,
            importance=float(judge.get("importance") or 0.5),
            confidence=float(judge.get("confidence") or 0.5))
        result.update({"neuron": str(grown.get("neuron") or ""),
                       "concepts": list(grown.get("concepts") or []),
                       "links": list(grown.get("links") or [])})
    except Exception:
        log.debug("生长节点失败", exc_info=True)

    try:
        from brain.services import live
        live.emit("fact_saved", fact_id=primary, fact_text=body[:60],
                  kind=str(judge.get("kind") or ""),
                  importance=round(float(judge.get("importance") or 0.5), 2),
                  concepts=list(result.get("concepts") or [])[:4])
    except Exception:
        pass
    return result
