"""认知内核的对外契约（V3）。

一句话的定稿权在这里：`ContentPlan` 是「说什么」的唯一载体，
语言层只准按它说，不准改它的主张（claims）、必含项（must_include）、
禁含项（must_not）和确定性等级（certainty）。

设计约束：
- 纯标准库，零第三方依赖。认知内核不许 import services / routers / 模型客户端。
- 数据结构不可变优先：计划生成后只读，避免下游偷偷改主意。
- 每个字段都要能回答「它从哪来」，来源一律落在 Evidence 上。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Certainty(str, Enum):
    """确定性等级。只有这五档，没有「大概知道」这种模糊档。

    这是"不确定性推理"的落地：程序不能只存知道/不知道，
    必须区分「硬事实」「多证一致」「合理推测」「证据冲突」「无法判断」。
    """

    CERTAIN = "certain"      # 用户明确说过，或系统硬事实（如注入的当天日期）
    SUPPORTED = "supported"  # 多条独立证据一致
    INFERRED = "inferred"    # 由规则/类比推出，没有被直接证实
    CONFLICT = "conflict"    # 证据之间互相矛盾，必须先澄清
    UNKNOWN = "unknown"      # 当前无法判断（没证据，不是"没有"）

    @property
    def rank(self) -> int:
        return {
            "certain": 4, "supported": 3, "inferred": 2,
            "conflict": 1, "unknown": 0,
        }[self.value]

    @classmethod
    def weakest(cls, items: list["Certainty"]) -> "Certainty":
        """整轮确定性取最弱的一环：一个环节是推测，整句就不许说成确定。"""
        if not items:
            return cls.UNKNOWN
        return min(items, key=lambda c: c.rank)


# 证据来源种类。trust 是"这条证据有多可信"，不是"我们有多相信结论"。
EVIDENCE_KINDS = (
    "identity",   # 身份卡（系统维护的硬事实）
    "profile",    # 长期画像
    "fact",       # 事实库里的陈述
    "episode",    # 每日摘要 / 情景记忆
    "rule",       # 规则推导
    "tool",       # 工具真实返回
    "user_said",  # 本轮用户原话
    "world",      # 外部知识
)

# 回答类型：程序先定"这是哪一类话"，语言层再想"怎么说"。
SPEECH_ACTS = (
    "identity_self",       # 问我是谁 → 报自己的名字
    "identity_user",       # 问我是谁（问的是他） → 报角色，名字未知则明说
    "recall",              # 要旧事 → 报记得的，不确定就说不确定
    "answer",              # 有答案 → 给结论
    "acknowledge",         # 情绪/状态 → 先接住
    "clarify",             # 指代不清/证据冲突 → 先问一句
    "refuse_unknown",      # 真没有 → 说不知道，不编
    "act",                 # 要动手 → 交给执行层，不先说话
)

# 需要执行的动作种类（行动权限由认知层签发，语言层无权发起）。
ACTION_KINDS = ("web_search", "kb_search", "remember", "remind", "compute", "none")


@dataclass(frozen=True)
class Evidence:
    """一条证据。`because` 就是靠它拼出来的，脑图点亮也必须指到这里。"""

    kind: str
    ref: str
    text: str
    trust: float = 0.5

    def because(self) -> str:
        return f"{self.kind}:{self.ref}" + (f"（{self.text}）" if self.text else "")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref,
                "text": self.text, "trust": round(self.trust, 3)}


@dataclass(frozen=True)
class Claim:
    """一条主张：谁 / 什么属性 / 什么值 / 有多大把握。

    value 为 None 表示「明确未知」。这不是缺字段，是一个合法结论：
    "我不知道他叫什么"本身就是认知结果，不能被渲染层补成某个名字。
    """

    subject: str          # user | assistant | world
    predicate: str        # name | role | emotion | ...
    value: str | None
    certainty: Certainty
    evidence: tuple[Evidence, ...] = ()

    @property
    def known(self) -> bool:
        return self.value is not None and self.certainty != Certainty.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject, "predicate": self.predicate,
            "value": self.value, "certainty": self.certainty.value,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass(frozen=True)
class Action:
    """一个待执行动作。permission 由认知层签发：denied 就是不许做。"""

    kind: str
    query: str
    reason: str
    permission: str = "granted"   # granted | denied

    @property
    def allowed(self) -> bool:
        return self.permission == "granted" and self.kind != "none"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "query": self.query,
                "reason": self.reason, "permission": self.permission}


@dataclass(frozen=True)
class ContentPlan:
    """「说什么」的定稿。语言层拿到它，只能表达，不能重新决定。

    points 是语义要点（不是最终措辞）；template_key 是表达层的兜底模板；
    next_step 是给表达模型的指令：说的是这段内容，不是让它自由发挥。
    """

    speech_act: str
    points: tuple[str, ...] = ()
    claims: tuple[Claim, ...] = ()
    must_include: tuple[str, ...] = ()
    must_not: tuple[str, ...] = ()
    tone: str = "neutral"          # neutral | warm | brief
    max_chars: int = 90
    template_key: str = ""
    actions: tuple[Action, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    uncertainty: Certainty = Certainty.UNKNOWN
    next_step: str = "如实说出下面这些要点，不要新增事实。"
    fallback_text: str = ""        # 连模板都不想用时，直接用这句

    @property
    def needs_action(self) -> bool:
        return any(a.allowed for a in self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "speech_act": self.speech_act,
            "points": list(self.points),
            "claims": [c.to_dict() for c in self.claims],
            "must_include": list(self.must_include),
            "must_not": list(self.must_not),
            "tone": self.tone,
            "max_chars": self.max_chars,
            "template_key": self.template_key,
            "actions": [a.to_dict() for a in self.actions],
            "evidence": [e.to_dict() for e in self.evidence],
            "uncertainty": self.uncertainty.value,
            "next_step": self.next_step,
        }


@dataclass(frozen=True)
class RenderResult:
    """语言层的产出。by 记录是谁说的——模板还是模型，必须有账。"""

    text: str
    by: str = "template"      # template | model
    tried: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "by": self.by, "tried": self.tried}


@dataclass(frozen=True)
class CheckResult:
    """表达忠实度校验结果。ok=False 时一律回退模板，不许放行。"""

    ok: bool
    failures: tuple[str, ...] = ()
    fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "failures": list(self.failures),
                "fallback": self.fallback}


@dataclass
class CogEvent:
    """真实认知事件：只有对应程序真的跑过才产生。

    字段刻意与前端脑区对齐：
      system  → 哪个系统干的
      action  → 干了什么
      because → 因为哪条证据/哪条规则（点开能看见）
      sources → 可点亮的真实节点 id（没有就不亮）
      region  → 对应脑区（perception/emotion/memory/judge/express/reflect/growth）
    """

    seq: int
    system: str
    action: str
    because: str = ""
    sources: tuple[str, ...] = ()
    region: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "system": self.system, "action": self.action,
            "because": self.because, "sources": list(self.sources),
            "region": self.region, "data": self.data,
        }


@dataclass
class TurnOutcome:
    """一轮的完整结果：计划、表达、校验、事件。缺一项都不算跑完。"""

    plan: ContentPlan
    render: RenderResult | None = None
    check: CheckResult | None = None
    events: list[CogEvent] = field(default_factory=list)
    final_text: str = ""
    # 需要先执行动作才能说话：此时 final_text 为空，由执行层办完再回灌
    awaiting_action: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "render": self.render.to_dict() if self.render else None,
            "check": self.check.to_dict() if self.check else None,
            "final_text": self.final_text,
            "events": [e.to_dict() for e in self.events],
        }
