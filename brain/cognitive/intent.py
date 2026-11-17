"""意图识别：这一句要办的是哪种事。

十种意图，全部由规则判定，每条都带「为什么这么判」的命中词，
方便在脑图/日志里对上「它以为我在问什么」——判错了能看见，不是黑箱。

    filler      纯语气词（嗯 / 哦 / 在吗）→ 顺着上文接一句
    greeting    打招呼 / 收尾
    meta        问「你是谁 / 什么模型」→ 说清楚自己是谁，不演
    recall      往回要记忆（还记得 / 上次说）
    task        要我去办（提醒 / 记下 / 查一下）
    technical   技术、设备、软件、模型 → 要解法
    decision    要一个判断（怎么办 / 选哪个 / 值不值）
    emotional   情绪倾诉 → 先接情绪
    followup    追问 / 指代上文 → 接着上一轮说具体
    chat        闲聊

判定顺序是有讲究的：先认「办事」和「要记忆」，再认「技术」和「决定」，
最后才落到情绪与闲聊。技术问题里带情绪词（「这破显卡烦死了」）仍然是技术，
不能被情绪吞掉——这正是历史上的翻车点之一。
"""
from __future__ import annotations

INTENT_CN = {
    "filler": "顺口接一句",
    "greeting": "打招呼",
    "meta": "问我是谁",
    "recall": "往回要记忆",
    "task": "要我办一件事",
    "technical": "技术问题",
    "decision": "要一个判断",
    "emotional": "情绪倾诉",
    "followup": "追问上一轮",
    "chat": "闲聊",
}


def classify(signals: dict) -> dict:
    """输入 perception.sense 的信号，输出 {intent, confidence, reason, hits}。"""
    s = signals or {}

    def out(intent: str, reason: str, hits: list[str] | None = None,
            confidence: float = 0.7) -> dict:
        return {"intent": intent, "confidence": round(float(confidence), 2),
                "reason": reason, "hits": list(hits or [])[:4]}

    if s.get("is_filler"):
        return out("filler", "只有语气词，没有实际内容", [], 0.85)

    meta = s.get("meta_hits") or []
    if meta:
        return out("meta", "问的是我的身份/能力", meta, 0.85)

    recall = s.get("recall_hits") or []
    if recall:
        return out("recall", "他在问我记不记得之前的事", recall, 0.8)

    task = s.get("task_hits") or []
    if task:
        return out("task", "他要我真去办一件事", task, 0.82)

    # 「同一件事又问一遍」要排在技术/决定之前：
    # 这时他要的不是再解释一遍原理，而是「上一轮你答得太空」。
    if s.get("repeat_prev"):
        return out("followup", "同一件事又问了一遍，说明上一轮没答到点上", [], 0.75)

    tech = s.get("tech_hits") or []
    if tech:
        # 技术问题优先级高于情绪：带情绪的技术问题仍然要解法。
        return out("technical", "句子里有设备/软件/模型这类具体对象", tech, 0.8)

    decision = s.get("decision_hits") or []
    if decision:
        return out("decision", "他在要一个判断或选择", decision, 0.8)

    emo = s.get("emotion_words") or []
    if emo and s.get("chars", 0) >= 4:
        return out("emotional", "说的是自己的状态和感受", emo, 0.75)

    greeting = s.get("greeting_hits") or []
    if greeting and s.get("chars", 0) <= 8:
        return out("greeting", "只是打个招呼/收个尾", greeting, 0.8)

    if s.get("refs_context"):
        return out("followup", "用了『这个/那个/它』，得先对上是哪件事",
                   s.get("refer") or [], 0.65)

    if s.get("is_question"):
        return out("chat", "是个问题，但没有明确的技术/决定指向",
                   s.get("question_words") or [], 0.65)

    return out("chat", "普通闲聊", [], 0.7)
