"""把「说」变成「给」：从这一轮回答里抽出一张能直接用的卡。

为什么要有它：以前的出口只有一个——一句话。听完还得自己从中摘、自己记。
用户要的「快，给到手上来」，就差这一下：把回答里的干货压成一张纸递过去。

三条硬规矩（宁可不出卡，也不出噪音卡）：
1. **不编内容**：卡里的每一句都必须是这轮回答里原样摘出来的，不新增事实。
2. **不抢情绪**：他难受的时候不给清单（那是「说」该干的事），只在真有事要做时给。
3. **不动热路径**：纯规则、不调模型，毫秒级，出错就当作没有卡。
"""
from __future__ import annotations

import re

# 出现这些词，说明他要的是「东西」，不是「一句安慰」
_ASK_WORDS = (
    "帮我", "给我", "替我", "能不能", "可不可以", "可以帮我", "怎么", "如何",
    "推荐", "列", "整理", "写一", "写个", "做个", "计划", "步骤", "流程",
    "清单", "哪些", "几样", "几点", "哪家", "多少钱", "怎么办", "要注意",
    "安排", "对比", "算一下", "查一下", "教",
)

# 情绪重的时候不给卡：这时候递清单像在敷衍
_HEAVY_EMOTIONS = ("sad", "anger", "angry", "fear", "disgust")

_SPLIT = re.compile(r"[。！？；\n]+")
_LEAD_JUNK = re.compile(
    r"^(?:(?:小智|好吧|那|嗯|其实|就是说|这样|帮我|给我|替我|能不能|可不可以|麻烦)"
    r"[，,：:、\s]*)+"
)
_LEAD_VERB = re.compile(r"^(?:列个|列一下|列|整理一下|整理|写个|写一下|做个|做一个|推荐一下|推荐)[，,：:、\s]*")
# 注意不要把「第一天/第二步」当成编号剥掉——那是内容本身
_ITEM_JUNK = re.compile(r"^\s*(?:[-•·]|\(?\d{1,2}[.、)）]|[{（(]?\d{1,2}[)）])\s*")


def _clean_title(user_text: str) -> str:
    text = str(user_text or "").strip()
    text = _LEAD_JUNK.sub("", text)
    text = _LEAD_VERB.sub("", text)
    text = re.sub(r"[？?。！!，,\s]+$", "", text)
    text = text.strip("「」\"“”' ")
    if len(text) > 18:
        text = text[:18] + "…"
    return text or "这一轮的事"


def _items_from(reply: str) -> list[str]:
    out: list[str] = []
    for raw in _SPLIT.split(reply or ""):
        piece = _ITEM_JUNK.sub("", raw).strip(" ，,、　")
        if len(piece) < 5:          # 太短的碎片（"好的"、"嗯"）不要
            continue
        if len(piece) > 46:
            piece = piece[:46] + "…"
        if piece in out:
            continue
        out.append(piece)
        if len(out) >= 6:
            break
    return out


def _kind_of(user_text: str, items: list[str]) -> str:
    joined = (user_text or "") + "".join(items)
    if any(k in joined for k in ("步骤", "流程", "先", "然后", "第一步", "怎么", "如何")):
        return "steps"
    if any(k in joined for k in ("清单", "哪些", "推荐", "对比", "列")):
        return "list"
    return "note"


def build(user_text: str, reply: str, emotion: str = "",
          bundle: dict | None = None, policy: dict | None = None) -> dict | None:
    """返回卡片 dict 或 None。

    policy 来自反馈统计（feedback_rules）：点过「不对」的那类先别出，
    整体不对过半就把门槛抬高。没有 policy 时用默认值。
    """
    text = str(user_text or "").strip()
    body = str(reply or "").strip()
    pol = policy or {}
    try:
        min_reply = int(pol.get("min_reply") or 24)
        min_items = int(pol.get("min_items") or 2)
    except (TypeError, ValueError):
        min_reply, min_items = 24, 2
    deny_kinds = set(pol.get("deny_kinds") or [])

    if len(body) < min_reply:
        return None                       # 一句话的回复不配成卡
    if str(emotion or "") in _HEAVY_EMOTIONS:
        return None                       # 难受的时候先陪着，别递清单
    if not any(w in text for w in _ASK_WORDS):
        return None                       # 不是「要东西」，只是闲聊

    items = _items_from(body)
    if len(items) < max(2, min_items):
        return None                       # 摘不出够用的条目，就不是能用的东西

    kind = _kind_of(text, items)
    if kind in deny_kinds:
        # 这一类被连着说「不对」：这轮先不出，宁可不给也别烦人
        return None

    return {
        "kind": kind,
        "title": _clean_title(text),
        "items": items,
        "note": "从这一轮回答里摘的，方便你直接照着做",
    }
