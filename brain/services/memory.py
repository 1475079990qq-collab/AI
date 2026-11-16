"""事实记忆层：只走 SQLite（中文 bigram 相似度），不引入向量库。

为什么不留向量这条路：本程序没有任何语义召回的需求场景，引一个向量库/向量模型
只会多一套索引、多一份要和外部对齐的依赖。数据量到「字面召回明显失手」时再谈，
那时也应该复用外部那套，而不是在这里再建一份。

边界（重构后）：
- 这里只管「关于这个人的记忆」：事实、每日摘要、未完成的事、画像、身份称呼。
- **知识库不在这里出现**。它曾经每轮被检索一次，未命中时同步 wait(2.0) 卡死事件循环，
  还把对话纪要的元文本整段灌进 prompt。现在知识库降级为「明确提问时才查的工具」，
  与记忆彻底分开：记忆是一直带在身上的，知识是去书房翻的。
"""
from __future__ import annotations

from brain import db
from brain.config import settings
from brain.schemas import new_request_id


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


def _facts_only(user_id: str, query: str, top_k: int) -> tuple[list[dict], str]:
    """三路线索合并召回：词重合 / 最近说过 / 带情绪（全在 SQLite 本地完成）。"""
    try:
        rows = db.search_facts(user_id, query, top_k)
        facts = [{"id": str(r["id"]), "text": str(r["text"]),
                  "score": float(r.get("score") or 0.0),
                  "why": list(r.get("why") or [])} for r in rows]
        return facts, "sqlite"
    except Exception:
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
    # 反刍：上一次说完话后自己复盘出来的那条「下次怎么改」，带进下一轮的提示词。
    try:
        lessons = db.list_lessons(user_id, limit=3)
    except Exception:
        lessons = []
    return {
        "facts": facts,
        "emotions": emotions,
        "source": source,
        "knowledge": [],          # 已迁出热路径，保留键位兼容旧调用
        "profile": profile_text,
        "episodes": episodes,
        "open_loops": open_loops,
        "lessons": lessons,
        "identity": _identity(),
    }


def store_fact(user_id: str, session_id: str, text: str,
               fact_ids: list[str]) -> None:
    """把本轮事实文本落库；fact_ids 为调用方给定的标识 / 关联 id 列表。

    取首个 id 作主键，完整列表写入 metadata；为空时内部生成一个。
    只落 SQLite：这里不再维护第二份向量副本。
    """
    ids = [str(x) for x in (fact_ids or []) if str(x).strip()]
    primary = ids[0] if ids else new_request_id()
    metadata = {
        "user_id": user_id,
        "session_id": session_id,
        "related_fact_ids": ids,
    }
    try:
        db.add_fact(fact_id=primary, user_id=user_id, session_id=session_id,
                    text=text, metadata=metadata)
        try:
            from brain.services import live
            live.emit("fact_saved", fact_text=str(text or "").strip()[:14])
        except Exception:
            pass
    except Exception:
        pass
