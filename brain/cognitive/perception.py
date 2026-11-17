"""感知区：听见这句话，先把它拆成几个能用的信号。

不是情绪判断——情绪由 judge 的模型/规则单独给（label + intensity）。
这里只做「结构上的听」：是不是问句、问的是哪一类、有没有指代上文、
是不是把上一轮的话又问了一遍、句子里有没有技术词 / 决定词 / 任务词。

全部是本地规则，不调模型：这一段必须能在 1ms 内跑完，因为它每轮都跑。
"""
from __future__ import annotations

import re
from typing import Any

# 技术 / 设备 / 软件：这些词出现，基本就不是闲聊，而是「要一个解法」。
_TECH_TERMS = (
    "模型", "显存", "显卡", "驱动", "量化", "推理", "部署", "服务", "端口", "接口",
    "报错", "错误", "崩溃", "蓝屏", "卡顿", "死机", "重装", "系统", "配置", "参数",
    "代码", "脚本", "程序", "数据库", "网络", "路由", "交换机", "nas", "docker",
    "python", "java", "模型文件", "内存", "cpu", "gpu", "硬盘", "固件", "刷机",
    "安装", "编译", "训练", "微调", "lora", "pcdn", "代理", "日志", "反编译",
    "sqlite", "api", "token", "上下文", "提示词", "prompt", "embedding", "向量",
)

# 要一个判断：不是「是什么」，而是「我该怎么办」。
_DECISION_TERMS = (
    "怎么办", "怎么选", "选哪个", "哪个好", "哪个更", "值不值", "值不值得",
    "该不该", "要不要", "建议", "推荐", "划算", "合适吗", "能不能买", "怎么弄",
    "有没有必要", "区别大吗", "值得吗",
)

# 要我去办一件事。
_TASK_TERMS = (
    "提醒我", "提醒一下", "帮我记", "记一下", "帮我查", "查一下", "查查",
    "帮我找", "帮我写", "定个", "设置一个", "记下来", "别忘了",
)

# 往回要记忆：他在问「你还记得吗」，不是问新知识。
_RECALL_TERMS = (
    "还记得", "记得吗", "记不记得", "之前说", "上次说", "我说过", "以前说",
    "上次那个", "之前那个", "我跟你讲", "我们说好", "昨天说", "前面说",
    # 「我刚才跟你说的」也是回头查账，不是新问题
    "我刚才说", "刚才说", "我刚才跟你", "刚跟你",
    # 「我上次跟你说的那台机器」「再说一遍」：要的是把旧账对出来，
    # 不能因为句子里带「显卡/机器」就被判成技术排查。
    "上次跟你", "上次那", "上回", "刚才跟你", "刚说",
    "跟你说过", "跟你说", "我说的话", "再说一遍", "再说一次",
    "前面提到", "之前提到",
)

# 他是谁 / 我是什么：身份类问题。
_META_TERMS = (
    "你是谁", "你叫什么", "什么模型", "你是ai", "你是ai吗", "你是机器人",
    "你会什么", "你能干什么", "你怎么工作", "你有意识", "你是真人",
)

# 打招呼 / 收尾，不需要展开。
_GREETING_TERMS = (
    "你好", "在吗", "在不在", "早上好", "晚上好", "晚安", "早", "嗨", "hi",
    "hello", "拜拜", "再见", "睡了",
)

# 情绪词：只用来判断「这句要不要先接情绪」，不产生 label。
_EMOTION_WORDS = (
    "难受", "难过", "累", "烦", "崩溃", "焦虑", "压力", "睡不着", "失眠", "郁闷",
    "委屈", "生气", "气死", "害怕", "担心", "孤独", "没意思", "想哭", "撑不住",
    "开心", "高兴", "舒服", "踏实", "爽", "兴奋", "感动",
)

# 「这个 / 那个 / 它 / 这事」——没有上文就接不住的话。
_REFER = ("这个", "那个", "它", "这事", "那事", "这样", "那样", "刚才", "上面")

_QUESTION_WORDS = ("怎么", "为什么", "为啥", "如何", "多少", "几个", "哪", "什么",
                   "是不是", "能不能", "会不会", "可不可以", "有没有")

_PUNCT = "，。！？,.!? \n\t“”\"'（）()…、；:：-—~～"

_FILLER_WORDS = {"嗯", "哦", "啊", "哈", "好", "好的", "行", "嗯嗯", "哦哦",
                 "好的好的", "?", "？", "。", "…", "在"}


def _plain(text: str) -> str:
    return "".join(ch for ch in str(text or "") if ch not in _PUNCT).strip()


def _hits(text: str, terms: tuple[str, ...], limit: int = 4) -> list[str]:
    low = str(text or "").lower()
    out = [t for t in terms if t.lower() in low]
    return out[:limit]


def _bigrams(text: str) -> set[str]:
    cleaned = "".join(ch for ch in str(text or "").lower() if not ch.isspace())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def _overlap(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / max(1, len(ga | gb))


def sense(text: str, prev_user: str = "", prev_reply: str = "") -> dict[str, Any]:
    """把一句话拆成信号。任何输入都返回同一个结构，调用方不必判空。"""
    raw = str(text or "")
    body = raw.strip()
    plain = _plain(body)
    chars = len(plain)

    # 语气词的标准要收紧：只有「整句就是一个语气词」才算，
    # 「在吗」「好啊」这种两个字的招呼是有内容的，交给 greeting 判。
    is_filler = plain in _FILLER_WORDS or chars <= 1
    has_question_mark = any(m in body for m in ("?", "？"))
    question_words = [w for w in _QUESTION_WORDS if w in plain]
    is_question = bool(has_question_mark or question_words) and not is_filler

    tech = _hits(plain, _TECH_TERMS)
    decision = _hits(plain, _DECISION_TERMS)
    task = _hits(plain, _TASK_TERMS)
    recall = _hits(plain, _RECALL_TERMS)
    meta = _hits(plain, _META_TERMS)
    greeting = _hits(plain, _GREETING_TERMS)
    emo_words = _hits(plain, _EMOTION_WORDS)
    refer = [w for w in _REFER if w in plain]

    # 他把上一轮问过的话又问了一遍：说明上一轮没答到点上。
    repeat_prev = bool(prev_user) and _overlap(plain, prev_user) >= 0.6 and chars >= 4
    # 他在指着上文说：接不住就要问一句「你说的是哪件事」。
    refs_context = bool(refer) and bool(prev_user or prev_reply)

    return {
        "text": body,
        "plain": plain,
        "chars": chars,
        "is_filler": is_filler,
        "is_short": chars <= 6,
        "is_question": is_question,
        "question_words": question_words,
        "tech_hits": tech,
        "decision_hits": decision,
        "task_hits": task,
        "recall_hits": recall,
        "meta_hits": meta,
        "greeting_hits": greeting,
        "emotion_words": emo_words,
        "refer": refer,
        "refs_context": refs_context,
        "repeat_prev": repeat_prev,
        "has_number": bool(re.search(r"\d", plain)),
        "has_latin": bool(re.search(r"[a-zA-Z]{2,}", body)),
        "asks_price": bool(re.search(r"(多少钱|价格|贵不贵|划算)", plain)),
        "segments": len([s for s in re.split(r"[，。！？,;；\n]", body) if s.strip()]),
    }
