"""注意力分配：这一轮动用哪几个区、能说多少话。

为什么要单独一层：以前的 prompt 对所有轮次用同一套规矩（「一般只说 1-2 句」），
结果技术问题也只答一句、情绪倾诉也被压缩成短句，人一听就觉得「它没在听」。
注意力不是「更努力一点」，而是「这一轮把预算给谁」：

    技术问题  → 判断区/联想区预算大，允许分步说清楚
    情绪倾诉  → 情绪区优先，先接住，不给方案
    语气词    → 只留听见/表达，记忆不翻箱倒柜

预算里 max_tokens 是硬上限（直接决定模型能说多少字，也决定首字之后等多久），
其余是给提示词的「怎么说」——两件事分开，才不会又退化成固定话术。
"""
from __future__ import annotations

# 八个区的下标（与 web/brain.js、brain-growth.js 的 REGIONS 一致）：
# 0 听见 / 1 情绪 / 2 回忆 / 3 联想 / 4 判断 / 5 表达 / 6 反思 / 7 沉淀
REGION_NAMES = ("听见", "情绪", "回忆", "联想", "判断", "表达", "反思", "沉淀")

_PROFILE = {
    "filler": {
        "regions": (0, 1, 5), "top_k": 1, "max_tokens": 120, "max_chars": 60,
        "sentences": 1, "allow_steps": False, "questions": 0,
        "hint": "一句话就够，顺着上文接一句，别当成没听懂。",
    },
    "greeting": {
        "regions": (0, 5), "top_k": 1, "max_tokens": 140, "max_chars": 70,
        "sentences": 2, "allow_steps": False, "questions": 1,
        "hint": "随口应一声，像熟人见面，不用展开。",
    },
    "meta": {
        "regions": (0, 4, 5), "top_k": 1, "max_tokens": 240, "max_chars": 160,
        "sentences": 3, "allow_steps": False, "questions": 1,
        "hint": "照实说自己是谁、能做什么，不演也不回避。",
    },
    "recall": {
        "regions": (0, 2, 3, 5, 6), "top_k": 4, "max_tokens": 300, "max_chars": 220,
        "sentences": 3, "allow_steps": False, "questions": 1,
        "hint": "先说你记得的那件具体事（时间、地点、原话里的细节），再说记不清的部分。",
    },
    "task": {
        "regions": (0, 4, 5, 7), "top_k": 2, "max_tokens": 220, "max_chars": 140,
        "sentences": 2, "allow_steps": False, "questions": 1,
        "hint": "先说办没办成、办成了什么，再说下一步。",
    },
    "technical": {
        "regions": (0, 1, 2, 3, 4, 5, 6), "top_k": 3, "max_tokens": 520, "max_chars": 420,
        "sentences": 6, "allow_steps": True, "questions": 1,
        "hint": "该说清楚就说清楚：先给最可能的那一个原因，再给下一步怎么验证；"
                "需要时分成 2-4 步，别用一句空话打发。",
    },
    "decision": {
        "regions": (0, 2, 3, 4, 5, 6), "top_k": 3, "max_tokens": 420, "max_chars": 330,
        "sentences": 5, "allow_steps": True, "questions": 1,
        "hint": "先给判断依据（看哪一两个条件），再给一个明确选项；不要列菜单让他自己挑。",
    },
    "emotional": {
        "regions": (0, 1, 2, 5), "top_k": 2, "max_tokens": 220, "max_chars": 150,
        "sentences": 3, "allow_steps": False, "questions": 0,
        "hint": "先接住他那句话里的那件具体事，再说你的反应；不要急着给建议或讲道理。",
    },
    "followup": {
        "regions": (0, 2, 3, 4, 5, 6), "top_k": 4, "max_tokens": 360, "max_chars": 280,
        "sentences": 4, "allow_steps": True, "questions": 0,
        "hint": "上一轮没答到点上：这轮直接说更具体的那一层（哪个环节、什么例子、怎么验证），"
                "不要换个说法再绕一遍。",
    },
    "chat": {
        "regions": (0, 1, 2, 4, 5), "top_k": 2, "max_tokens": 260, "max_chars": 200,
        "sentences": 3, "allow_steps": False, "questions": 1,
        "hint": "1-3 句为主，像随口说话；他愿意多说，你也可以多说两句，别为了短而短。",
    },
}


def _label_of(emotion) -> tuple[str, float]:
    try:
        return str(getattr(emotion, "label", "") or "neutral"), float(
            getattr(emotion, "intensity", 0.0) or 0.0)
    except (TypeError, ValueError):
        return "neutral", 0.0


def allocate(signals: dict, intent_info: dict, emotion=None) -> dict:
    """按意图给这一轮分配「参与区 + 表达预算」。"""
    s = signals or {}
    intent = str((intent_info or {}).get("intent") or "chat")
    base = dict(_PROFILE.get(intent, _PROFILE["chat"]))

    label, intensity = _label_of(emotion)
    regions = list(base["regions"])
    max_chars = int(base["max_chars"])
    questions = int(base["questions"])
    tone = "natural"

    # 情绪很强：先把情绪接住，再谈内容；只降篇幅，不降温度。
    if intensity >= 0.7 and label not in ("neutral", "") and intent not in (
            "technical", "decision", "recall", "task"):
        regions = sorted(set(regions + [1]))
        max_chars = max(60, max_chars - 40)
        tone = "warm"
    # 情绪很淡但话题是技术：不用留温度位，把预算给判断。
    elif intent in ("technical", "decision") and intensity < 0.3:
        tone = "plain"

    # 他明确在追问：这轮别再反问，直接给内容。
    if intent == "followup" or s.get("repeat_prev"):
        questions = 0

    focus = ""
    for key in ("tech_hits", "decision_hits", "task_hits", "recall_hits", "emotion_words"):
        if s.get(key):
            focus = str(s[key][0])
            break
    if not focus and s.get("refer"):
        focus = str(s["refer"][0])

    return {
        "regions": regions,
        "region_names": [REGION_NAMES[i] for i in regions],
        "memory_top_k": int(base["top_k"]),
        "focus": focus,
        "tone": tone,
        "budget": {
            "max_tokens": int(base["max_tokens"]),
            "max_chars": max_chars,
            "sentences": int(base["sentences"]),
            "allow_steps": bool(base["allow_steps"]),
            "questions": int(questions),
        },
        "length_hint": str(base["hint"]),
    }
