"""神经元：把一行文本变成一个有属性的节点。

与旧实现的区别（这是 V2 最实质的一条）：
- 以前「神经元」是前端画出来的圆点：事实一多，就按哈希挤进固定几个点里，
  节点没有类型、没有重要度、没有置信度，也没有激活历史。
- 现在每颗节点都有 id / kind / label / importance / confidence /
  activation / activation_count / last_activated，写在 SQLite 里，可查询、可回放。
  前端画的是这些节点，而不是另造一份。

概念节点（concept）不是词典，而是「在这个人身上反复出现的词」：
同一个两字/三字词出现在两条以上不同的记忆里，才长成一颗概念节点。
出现一次的词只是这条记忆的一部分，不会凭空多出一个节点。
"""
from __future__ import annotations

import hashlib
import re

from brain import db

# 功能字：任何含这些字的候选词都不当概念（「我的」「就是」「这个」这类）。
# 只收真正高频的虚字；「对/到/下/前」这类会出现在正常名词里（对话、下载、后台），
# 故意不收——宁可多两条候选取代把好词杀掉。
_STOP_CHARS = set(
    "的了吗呢吧啊呀哦嗯是我你他她它这那就都也还很太不没要会能被给和与或而但因"
    "为所以什么怎自觉应该着得地又再才更最每各某另其此谁哪几咋啥么"
)

# 整词停用：光看字还不够，这些组合在中文里几乎都是语法结构。
_STOP_GRAMS = {
    "我是", "我在", "我也", "我觉", "我想", "我说", "我不", "我没", "我要", "我会",
    "你是", "你们", "你说", "你就", "你在", "我们", "他们", "她们", "它们",
    "就是", "不是", "没有", "一个", "这个", "那个", "这些", "那些", "这样", "那样",
    "什么", "怎么", "可以", "可能", "应该", "现在", "今天", "明天", "昨天", "时候",
    "觉得", "知道", "因为", "所以", "但是", "如果", "还有", "然后", "其实", "真的",
    "一样", "一些", "有点", "一直", "已经", "比较", "自己", "觉得", "东西", "意思",
    "感觉", "一下", "一点", "不会", "不要", "不能", "不用", "还是", "或者", "而且",
    "不过", "只是", "这种", "那种", "之后", "之前", "以后", "以前", "现在", "当时",
    "问题", "情况", "方面", "地方", "时间", "时候", "样子", "事情", "办法", "方式",
    "为了", "关于", "对于", "的话", "起来", "出来", "过来", "下来", "上去", "进去",
    # 「在说」「说这」这类是句子骨架，不是概念（实测里它们被长成过概念节点）
    "在说", "说这", "说那", "都说", "想说", "要说", "看了", "做了", "用了",
    "去了", "来了", "有个", "也有", "都没", "都要", "能在", "会在",
}


def _strip(text: str) -> str:
    """只留中文：标点、空白、数字、英文都去掉，避免 "gpu" 被切成一堆废词。"""
    return re.sub(r"[^\u4e00-\u9fa5]", "", str(text or ""))


def extract_concepts(text: str, limit: int = 10) -> list[str]:
    """从一句话里抽候选概念词（二字为主）。只是候选，成不成节点由出现次数决定。"""
    clean = _strip(text)
    if len(clean) < 2:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for i in range(len(clean) - 1):
        gram = clean[i:i + 2]
        if gram in seen or gram in _STOP_GRAMS:
            continue
        if any(ch in _STOP_CHARS for ch in gram):
            continue
        # 两字都相同（「谢谢」「慢慢」）不是概念
        if gram[0] == gram[1]:
            continue
        seen.add(gram)
        out.append(gram)
        if len(out) >= limit:
            break
    return out


def fact_neuron_id(fact_id: str) -> str:
    return "n_fact_" + str(fact_id)


def lesson_neuron_id(lesson_id: int | str) -> str:
    return "n_lesson_" + str(lesson_id)


def concept_neuron_id(term: str) -> str:
    digest = hashlib.sha1(str(term).encode("utf-8")).hexdigest()[:12]
    return "n_concept_" + digest


def ensure_fact_neuron(*, user_id: str, fact_id: str, text: str,
                       importance: float = 0.5, confidence: float = 0.5) -> str:
    nid = fact_neuron_id(fact_id)
    db.neuron_upsert(
        neuron_id=nid, user_id=user_id, kind="fact", ref_id=str(fact_id),
        label=str(text or "").strip()[:14], text=str(text or "").strip(),
        importance=float(importance), confidence=float(confidence),
    )
    return nid


def ensure_lesson_neuron(*, user_id: str, lesson_id: int | str, note: str,
                         weight: float = 0.5) -> str:
    nid = lesson_neuron_id(lesson_id)
    body = str(note or "").strip()
    db.neuron_upsert(
        neuron_id=nid, user_id=user_id, kind="lesson", ref_id=str(lesson_id),
        label=body.splitlines()[-1][:14] if body else "一条教训", text=body,
        importance=max(0.4, min(1.0, float(weight))), confidence=0.6,
    )
    return nid


def ensure_concept_neuron(*, user_id: str, term: str, occurrences: int) -> str:
    nid = concept_neuron_id(term)
    importance = min(0.95, 0.35 + 0.08 * max(2, int(occurrences)))
    db.neuron_upsert(
        neuron_id=nid, user_id=user_id, kind="concept", ref_id=str(term),
        label=str(term), text=str(term),
        importance=round(importance, 3), confidence=round(min(0.95, 0.4 + 0.06 * occurrences), 3),
    )
    return nid


def concept_terms_of(user_id: str, neuron_id: str) -> list[str]:
    """某个记忆节点连出去的概念词（联想区展示用）。"""
    try:
        rows = db.synapse_neighbors(user_id, [neuron_id], limit=8)
    except Exception:
        return []
    return [str(r.get("dst") or "") for r in rows if str(r.get("relation") or "") == "about"]
