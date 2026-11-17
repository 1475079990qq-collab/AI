"""演绎推理：条件满足则结论成立，用规则引擎而不是模型猜。

这是七系统里「推理」最硬的一层：给同样的输入，永远给出同样的结论，
而且每一步都能说清「因为命中哪条规则」。

实现是**前向链 + 优先级冲突消解**：
  1) 把所有规则跑一遍，收集命中的；
  2) 按 (priority, 条件数) 降序排，取第一条为结论；
  3) 若同优先级下出现互斥结论（如既判「知道姓名」又判「不知道」），
     不停在错的上面，直接标记 conflict，交下游澄清。

刻意不做的：不做自由文本蕴含、不让模型改写规则。规则写错就改规则，可回退。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    """一条规则：when 全满足 → then 成立。

    priority 越大越优先；条件越多（越具体）越优先。这是最朴素的 specificity 原则。
    """

    id: str
    when: tuple[tuple[str, object], ...]
    then: dict
    priority: int = 10
    note: str = ""

    @property
    def specificity(self) -> int:
        return len(self.when)

    def matches(self, facts: dict) -> bool:
        return all(facts.get(k) == v for k, v in self.when)


@dataclass(frozen=True)
class Inference:
    """推理结果：定论规则 + 冲突规则 + 被踩下去的其他命中（可追溯）。"""

    rule: Rule | None
    conflict: tuple[str, ...] = ()
    considered: tuple[str, ...] = ()

    @property
    def then(self) -> dict:
        return dict(self.rule.then) if self.rule else {}

    def to_dict(self) -> dict:
        return {
            "rule": self.rule.id if self.rule else None,
            "then": self.then,
            "conflict": list(self.conflict),
            "considered": list(self.considered),
        }


# ---------------------------------------------------------------------------
# 规则表：身份 / 回忆 / 情绪 / 办事 / 指代
# 顺序只是可读性，实际以 priority 与 specificity 决定。
# ---------------------------------------------------------------------------
RULES: tuple[Rule, ...] = (
    # --- 身份：问的是他（必须同时是身份意图，否则「记得我吗」会被身份规则抢走）---
    Rule("id_user_known", (("query_target", "user"), ("intent", "identity"),
                           ("user_name_known", True)),
         {"speech_act": "identity_user", "template_key": "identity_user_known"},
         priority=30, note="问的是他，且姓名有据"),
    Rule("id_user_unknown", (("query_target", "user"), ("intent", "identity"),
                             ("user_name_known", False)),
         {"speech_act": "identity_user", "template_key": "identity_user_unknown"},
         priority=30, note="问的是他，但姓名无据——只说角色，不编名字"),

    # --- 身份：问的是我 ---
    Rule("id_self_known", (("query_target", "assistant"), ("intent", "identity"),
                           ("assistant_name_known", True)),
         {"speech_act": "identity_self", "template_key": "identity_self"},
         priority=30, note="问的是系统自己"),
    Rule("id_self_unknown", (("query_target", "assistant"), ("intent", "identity"),
                             ("assistant_name_known", False)),
         {"speech_act": "identity_self", "template_key": "identity_self_unknown"},
         priority=30),

    # --- 回忆 ---
    Rule("recall_hit", (("intent", "recall"), ("has_fact", True)),
         {"speech_act": "recall", "template_key": "recall_hit"},
         priority=20, note="要旧事，且有可引用的陈述"),
    Rule("recall_miss", (("intent", "recall"), ("has_fact", False)),
         {"speech_act": "refuse_unknown", "template_key": "recall_miss"},
         priority=20, note="要旧事，但库里只有问句/碎片——如实说没记清"),

    # --- 办事：外面的事才允许动手 ---
    Rule("act_world", (("intent", "task"), ("needs_world", True)),
         {"speech_act": "act", "action_kind": "web_search"},
         priority=25, note="明确要办且属外部信息"),
    Rule("act_denied_identity", (("intent", "identity"), ("needs_world", True)),
         {"speech_act": "identity_user", "action_kind": "none"},
         priority=99,
         note="身份类永不动手：一句话里带『天气』也先答身份"),

    # --- 情绪 ---
    Rule("ack_emotion", (("intent", "emotional"),),
         {"speech_act": "acknowledge", "template_key": "ack_emotion"},
         priority=15),

    # --- 指代不清 ---
    Rule("clarify_ref", (("refs_context", True),),
         {"speech_act": "clarify", "template_key": "clarify_ref"},
         priority=18, note="他说了『这个/那个』但没有上文"),

    # --- 闲聊 / 语气 / 技术 ---
    Rule("chat_plain", (("intent", "chat"),),
         {"speech_act": "answer", "template_key": "chat_plain"}, priority=5),
    Rule("filler_plain", (("intent", "filler"),),
         {"speech_act": "answer", "template_key": "filler"}, priority=6),
    Rule("greet_plain", (("intent", "greet"),),
         {"speech_act": "answer", "template_key": "greet"}, priority=6),
    Rule("tech_ask", (("intent", "technical"),),
         {"speech_act": "answer", "template_key": "tech"}, priority=8),
)


def infer(facts: dict, rules: tuple[Rule, ...] = RULES) -> Inference:
    """前向链推理：跑全部规则，按优先级取定论，互斥则标冲突。

    刻意不用「第一条命中就返回」：那样会把冲突吞掉，
    而「证据冲突」本身就是一个必须让用户看见的结论。
    """
    hits = [r for r in rules if r.matches(facts)]
    if not hits:
        return Inference(None)

    hits.sort(key=lambda r: (r.priority, r.specificity), reverse=True)
    top = hits[0]

    # 同优先级但结论互斥 → 冲突（不让任何一条悄悄赢）
    same = [r for r in hits if r.priority == top.priority]
    acts = {r.then.get("speech_act") for r in same}
    if len(acts) > 1:
        return Inference(top, tuple(sorted(r.id for r in same)),
                         tuple(r.id for r in hits))

    return Inference(top, (), tuple(r.id for r in hits))
