"""思考任务规划：把「听懂了什么」变成「这轮打算怎么接」。

产出两样东西，用途不同：
1. plan_text —— 一句人话，写进「判断」区的副文案。它是系统真实做出的调度决定，
   不是假装读了大模型内部的隐式思维链（后端确实没有这个东西，也不硬造）。
2. directives —— 给提示词的三四条「这轮怎么说」，按意图和手里的材料定制，
   取代原来那套对所有轮次一视同仁的固定话术。

纪律：directives 最多 4 条，每条都指到这一轮的具体东西（哪件事、哪个环节、
要不要反问）；宁可少给，也不堆规则——堆规则正是「回复死板」的来源。
"""
from __future__ import annotations

from .intent import INTENT_CN


def _clip(text: str, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _first_fact(bundle: dict) -> str:
    try:
        facts = (bundle or {}).get("facts") or []
        for f in facts:
            txt = str((f or {}).get("text") or "").strip()
            if txt:
                return txt
    except Exception:
        pass
    return ""


def plan(signals: dict, intent_info: dict, attention: dict,
         bundle: dict | None = None, emotion=None) -> dict:
    """合成这一轮的认知计划；任何字段缺失都退化成「普通闲聊」。"""
    s = signals or {}
    info = intent_info or {}
    att = attention or {}
    intent = str(info.get("intent") or "chat")
    budget = dict(att.get("budget") or {})
    fact = _first_fact(bundle or {})

    directives: list[str] = []
    plan_text = "先按直觉回"

    if intent == "filler":
        plan_text = "不接新话题，顺着他上一句往下"
        directives = [
            "他这句几乎没有内容，不要当成新问题。",
            "顺着上一句自然接一句，可以反问、可以打趣，别问「你想聊什么」。",
        ]
    elif intent == "greeting":
        plan_text = "应一声，别展开"
        directives = [
            "他在打招呼或收尾，随口应一声就好，不用铺开话题。",
        ]
    elif intent == "meta":
        plan_text = "照实说我是谁，不演"
        directives = [
            "他在问我是谁 / 什么模型 / 能做什么：照实说，不装人，也不回避。",
            "说完就停，不要顺势讲一串介绍。",
        ]
    elif intent == "recall":
        if fact:
            plan_text = f"先把「{_clip(fact, 12)}」对出来"
            directives = [
                f"记忆里有这件：「{_clip(fact, 60)}」。先用它回答，别只说「我记不清」。",
                "如果细节记不全，就说清哪部分确定、哪部分不确定，不要编。",
            ]
        else:
            plan_text = "记忆里没对上，照实说"
            directives = [
                "这次没想起对应的事：照实说记不清，再让他补一个关键词。",
                "不要为了显得记得而编一件旧事。",
            ]
    elif intent == "task":
        plan_text = "先看这件事办成没有"
        directives = [
            "他要的是「办了没有」，不是感想：先说结果，再说下一步。",
        ]
    elif intent == "technical":
        plan_text = "先分原因，再给下一步验证"
        directives = [
            "他要的是解法，不是安慰：先给最可能的那一个原因，再说怎么验证。",
            "允许分成 2-4 步说清楚，每步都指到具体的东西（哪个参数、哪个日志、哪个进程）。",
            "不确定的地方就说「这个得看实际情况」，不要编参数值。",
        ]
        if fact:
            directives.insert(0, f"他之前提过相关的：「{_clip(fact, 50)}」——能对上就先接这句。")
    elif intent == "decision":
        plan_text = "先给判断依据，再落一个选项"
        directives = [
            "先说他该看哪一两个条件，再给一个明确的选择。",
            "不要列三个选项让他自己挑，那是把决定推回给他。",
        ]
    elif intent == "emotional":
        plan_text = "先接住情绪，不急着给建议"
        directives = [
            "先接住他这句话里那件具体的事，再说你的反应。",
            "不要在这轮给方案、讲道理或劝他想开点。",
        ]
    elif intent == "followup":
        plan_text = "上一轮没答到点上，这轮说具体"
        directives = [
            "同一件事他已经在追问了：这轮必须比上一轮更具体（环节、例子、怎么试）。",
            "不要换一种说法把上一轮的话再说一遍，也不要反问他想聊什么。",
        ]
    else:
        plan_text = "接着他的话说，不用铺开"
        directives = [
            "顺着他的话题说，1-3 句为主；他愿意多说，你也可以多说两句。",
            "他这句里有具体的事（哪个人、哪个东西），先接住那件事再表达态度。",
        ]

    # 篇幅 / 反问：只加一条，且用他的话，不用数字编号。
    sentences = int(budget.get("sentences") or 3)
    questions = int(budget.get("questions") or 0)
    if questions <= 0:
        directives.append(f"这一轮别反问，最多 {sentences} 句说完。")
    elif sentences >= 4:
        directives.append(f"这一轮可以说到 {sentences} 句左右；要问就只问一个。")

    if str(s.get("phase") or "") == "" and s.get("repeat_prev"):
        directives.append("他刚才已经问过同一件事：直接回答，别再让他重复。")

    return {
        "intent": intent,
        "intent_cn": INTENT_CN.get(intent, intent),
        "confidence": float(info.get("confidence") or 0.7),
        "reason": str(info.get("reason") or ""),
        "regions": list(att.get("regions") or []),
        "focus": str(att.get("focus") or ""),
        "tone": str(att.get("tone") or "natural"),
        "budget": budget,
        "length_hint": str(att.get("length_hint") or ""),
        "memory_top_k": int(att.get("memory_top_k") or 2),
        "plan_text": _clip(plan_text, 26),
        "directives": [d for d in directives if d][:5],
    }
