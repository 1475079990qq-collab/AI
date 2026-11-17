"""记忆价值判断：这句话值不值得记、以什么身份记、有多重要。

这是「记忆不像人的记忆」的直接修法。以前 memory.py 把用户说过的内容基本照存，
所以「今天吃了什么」和「他忌口不吃香菜」在召回里一样重。现在写入前先问三个问题：

    这说的是关于他的什么？   身份 / 约束 / 偏好 / 关系 / 目标 / 经历 / 技能 / 问题 / 回声
    该用多大权重记？         0.35 起，身份与禁忌到 0.9 以上
    值不值得记？             提问、测试、纯回声 → 不记（keep=False）

权重只决定「多容易被想起来」，不决定要不要留：低权重的经历仍然写进库，
只是不会在无关话题里冒出来。
"""
from __future__ import annotations

import re

# 顺序即优先级：身份 > 禁忌 > 偏好 > 关系 > 目标 > 任务 > 技能 > 经历。
_RULES: tuple[tuple[str, float, float, tuple[str, ...]], ...] = (
    ("identity", 0.95, 0.9,
     ("我叫", "我的名字", "我是做", "我的职业", "我今年", "我住在", "我在做", "我干这行")),
    ("constraint", 0.9, 0.85,
     ("忌口", "不吃", "过敏", "不能吃", "医生", "吃药", "手术", "血压", "血糖", "腰",
      "颈椎", "心脏", "早搏", "戒了", "伤了", "扭了", "疼")),
    ("preference", 0.85, 0.8,
     ("喜欢", "爱吃", "爱喝", "讨厌", "受不了", "习惯", "偏好", "不喜欢", "最爱")),
    ("relation", 0.8, 0.85,
     ("老婆", "媳妇", "孩子", "儿子", "女儿", "爸", "妈", "家里", "朋友", "同事",
      "合伙人", "客户")),
    ("goal", 0.78, 0.7,
     ("打算", "计划", "准备", "目标", "以后", "明年", "想考", "想买", "准备做", "要上")),
    ("task", 0.62, 0.7,
     ("要交", "得去", "待会", "明天要", "下周要", "约了", "答应", "截止")),
    ("tech", 0.55, 0.65,
     ("配置", "设置", "部署", "安装", "驱动", "端口", "参数", "脚本", "端口映射",
      "模型文件", "硬盘", "机器", "电脑", "路由器")),
    ("episode", 0.35, 0.6,
     ("今天", "昨天", "刚才", "早上", "中午", "晚上", "吃了", "去了", "睡了", "聊了")),
)

_ECHO_PAT = re.compile(r"(你在吗|在不在|你记得|你还记得|你是谁|什么模型|测试一下|听得见|能听到)")
# 回头查旧账 / 要我复述：这是在考我的记忆，不是他新说了一件关于自己的事。
# 这类句子往往带「机器/显卡」等技术词，不能因此被当成一条技术事实存下来。
_LOOKBACK_PAT = re.compile(
    r"(再说一遍|再说一次|你记得|还记得|记不记得|我刚才说|刚跟你|上次跟你|上回跟你|跟你说过)")
_QUESTION_TAIL = ("吗", "呢", "?", "？")

_NEUTRAL = {"kind": "neutral", "importance": 0.42, "confidence": 0.55,
            "reason": "没有明确属性，按一般记忆记"}


def score(text: str) -> dict:
    """返回 {kind, importance, confidence, reason, keep}。纯规则，不调模型。"""
    raw = str(text or "").strip()
    plain = re.sub(r"[\s，。！？,.!?、；;：:（）()\"'“”…]", "", raw)
    if not plain:
        return {"kind": "echo", "importance": 0.1, "confidence": 0.5,
                "reason": "空内容", "keep": False}

    # 在问「你还记得吗 / 你是谁 / 听得见吗」：这是测试与回声，不是关于他的事。
    if _ECHO_PAT.search(plain) and len(plain) <= 24:
        return {"kind": "echo", "importance": 0.12, "confidence": 0.5,
                "reason": "这句是在试探我，不是在说他自己的事", "keep": False}

    # 「我上次跟你说的那台机器，再说一遍」：这是回头查旧账，不是在报新事实。
    # 命中它就先判成回声，避免「机器/显卡」这类词把它当技术事实存进长期记忆。
    if _LOOKBACK_PAT.search(plain) and not any(
            w in plain for w in ("我今天", "我昨天", "我刚买", "我换了", "我装了")):
        return {"kind": "echo", "importance": 0.12, "confidence": 0.55,
                "reason": "这句是要我复述旧账，不是在报新的个人事实", "keep": False}

    # 纯提问、且没有第一人称陈述：要的是答案，不是一条关于他的事实。
    is_question = plain.endswith(_QUESTION_TAIL) or any(
        w in plain for w in ("怎么", "为什么", "如何", "哪", "多少"))
    if is_question and not any(w in plain for w in ("我", "我们", "咱")):
        return {"kind": "question", "importance": 0.2, "confidence": 0.55,
                "reason": "这是向我提问，不是他的个人事实", "keep": False}

    for kind, imp, conf, words in _RULES:
        hit = next((w for w in words if w in plain), "")
        if hit:
            # 太短的命中（比如单独一个「腰」）降权：证据不足。
            adjust = -0.08 if len(plain) <= 4 else 0.0
            return {"kind": kind, "importance": round(max(0.2, imp + adjust), 3),
                    "confidence": conf, "reason": f"命中「{hit}」", "keep": True}

    # 第一人称 + 陈述句：记，但按一般经历对待。
    if any(w in plain for w in ("我", "咱")) and len(plain) >= 6:
        return dict(_NEUTRAL, keep=True, reason="第一人称陈述，按一般记忆记")
    return dict(_NEUTRAL, keep=len(plain) >= 10, reason="信息量不足，先只留摘要")
