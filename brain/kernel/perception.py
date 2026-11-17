"""感知与理解系统：先把「谁在问谁」定下来，再谈别的。

这个模块存在的唯一理由：**身份与指代不能交给生成式模型猜**。
「我是谁」和「你是谁」在字面上只差一个字，指代反了，后面全错。

纪律：
- 纯规则、纯函数、零 IO、零模型调用，单句 <1ms。
- 「我」永远指当前说话的用户，「你」永远指系统自己。这条不做概率判断。
- 判不出来的就标 unclear，交给下游澄清，绝不默认成「闲聊」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 问「他本人是谁」——注意：这些必须先于「你是谁」被匹配，
# 否则「你知道我是谁吗」会被「你是谁」的子串抢走。
_SELF_ASK = (
    "我是谁", "我叫什么", "我叫啥", "我的名字", "我是哪位", "我是什么人",
    "你知道我是谁", "记得我是谁", "认得我是谁", "我对于你", "我对你来说是谁",
    "我是你的谁", "你还记得我吗", "你记得我吗", "记得我吗",
)

# 问「系统自己是谁」。
_ASSISTANT_ASK = (
    "你是谁", "你叫什么", "你叫啥", "你的名字", "你是谁", "你是什么模型",
    "你是ai", "你是机器人", "你是真人", "你是什么", "你会什么", "你能干什么",
)

# 往回要记忆（但不是问身份）。
_RECALL = (
    "还记得", "记得吗", "记不记得", "之前说", "上次说", "我说过", "以前说",
    "上次那个", "之前那个", "再说一遍", "再说一次", "刚才说",
)

# 要动手办的事（注意：这里只负责「听出来要办事」，不做权限判定）。
_TASK = (
    "提醒我", "提醒一下", "帮我记", "记一下", "帮我查", "查一下", "查查",
    "帮我找", "帮我写", "搜一下", "搜一搜", "定个", "设置一个", "记下来",
)

# 外部世界的问题（要查才知道，不是记忆里有的）。
_WORLD = (
    "天气", "多少钱", "什么价", "新闻", "汇率", "股价", "比分", "航班",
    "哪家", "怎么走", "多远", "几点开门",
)

# 技术/设备：要的是解法，不是资料。
_TECH = (
    "模型", "显存", "显卡", "驱动", "量化", "推理", "部署", "端口", "接口",
    "报错", "蓝屏", "卡顿", "死机", "重装", "系统", "配置", "参数", "代码",
    "脚本", "数据库", "网络", "路由", "交换机", "nas", "docker", "python",
    "内存", "cpu", "gpu", "硬盘", "固件", "刷机", "编译", "训练", "微调",
    "lora", "pcdn", "代理", "日志", "反编译", "sqlite", "api", "token",
    "提示词", "embedding", "向量",
)

_EMOTION = (
    "难受", "难过", "累", "烦", "崩溃", "焦虑", "压力", "睡不着", "失眠",
    "郁闷", "委屈", "生气", "气死", "害怕", "担心", "孤独", "没意思", "想哭",
    "撑不住", "开心", "高兴", "舒服", "踏实", "兴奋", "感动",
)

_REFER = ("这个", "那个", "它", "这事", "那事", "这样", "那样", "上面")

_PUNCT = "，。！？,.!? \n\t“”\"'（）()…、；:：-—~～"
_FILLER = {"嗯", "哦", "啊", "哈", "好", "好的", "行", "嗯嗯", "哦哦", "在"}


@dataclass(frozen=True)
class Percept:
    """一句话听完之后的结构化理解。任何字段缺失都有合法默认，调用方不必判空。"""

    text: str
    plain: str
    chars: int
    speaker: str = "user"                 # 说话人：永远是他，不是模型
    query_target: str = "none"            # user | assistant | world | none
    intent: str = "chat"                  # identity | recall | task | technical | emotional | greet | chat
    is_question: bool = False
    is_filler: bool = False
    pronoun: dict[str, str] = field(default_factory=dict)
    hits: tuple[str, ...] = ()
    needs_world_knowledge: bool = False   # 这句话的知识是不是「外面才有」
    refs_context: bool = False            # 用了"这个/那个"，没有上文就接不住

    def to_dict(self) -> dict:
        return {
            "text": self.text, "speaker": self.speaker,
            "query_target": self.query_target, "intent": self.intent,
            "is_question": self.is_question, "hits": list(self.hits),
            "needs_world_knowledge": self.needs_world_knowledge,
            "refs_context": self.refs_context,
        }


def _plain(text: str) -> str:
    return "".join(ch for ch in str(text or "") if ch not in _PUNCT).strip()


def _hits(plain: str, terms: tuple[str, ...], limit: int = 4) -> list[str]:
    low = plain.lower()
    return [t for t in terms if t.lower() in low][:limit]


def understand(text: str, *, has_history: bool = False) -> Percept:
    """解析一句话。顺序即优先级：身份 → 回忆 → 办事 → 情绪 → 技术 → 闲聊。

    优先级这么定的理由：身份类问题一旦被当成熟聊，就会去联网查「我是谁」，
    这是历史上最难看的一种翻车；所以身份永远第一个判。
    """
    raw = str(text or "")
    body = raw.strip()
    plain = _plain(body)
    chars = len(plain)

    is_filler = plain in _FILLER or chars <= 1
    is_question = ("?" in body or "？" in body
                   or any(w in plain for w in ("谁", "什么", "怎么", "为什么", "吗", "多少", "哪")))

    # --- 身份：先分清问的是他，还是我 ---
    self_hits = _hits(plain, _SELF_ASK)
    asst_hits = _hits(plain, _ASSISTANT_ASK)

    target = "none"
    intent = "chat"
    hits: list[str] = []
    if self_hits:
        target, intent, hits = "user", "identity", self_hits
    elif asst_hits:
        target, intent, hits = "assistant", "identity", asst_hits
    else:
        recall_hits = _hits(plain, _RECALL)
        task_hits = _hits(plain, _TASK)
        world_hits = _hits(plain, _WORLD)
        tech_hits = _hits(plain, _TECH)
        emo_hits = _hits(plain, _EMOTION)
        if recall_hits:
            # 回忆不是问身份：target 保持 none，避免身份规则被顺手命中
            target, intent, hits = "none", "recall", recall_hits
        elif task_hits:
            target, intent, hits = "world", "task", task_hits
        elif world_hits:
            target, intent, hits = "world", "task", world_hits
        elif tech_hits:
            target, intent, hits = "world", "technical", tech_hits
        elif emo_hits and chars >= 4:
            target, intent, hits = "user", "emotional", emo_hits
        elif is_filler:
            intent = "filler"
        elif any(t in plain for t in ("你好", "在吗", "早", "晚安", "拜拜", "再见")):
            intent = "greet"
        else:
            intent = "chat"

    needs_world = bool(_hits(plain, _WORLD)) or (intent == "task" and target == "world")
    refs_context = any(r in plain for r in _REFER) and not has_history

    return Percept(
        text=body,
        plain=plain,
        chars=chars,
        speaker="user",
        query_target=target,
        intent=intent,
        is_question=is_question,
        is_filler=is_filler,
        pronoun={"我": "user", "你": "assistant"},
        hits=tuple(hits),
        needs_world_knowledge=needs_world,
        refs_context=refs_context,
    )
