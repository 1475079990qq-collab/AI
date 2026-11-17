"""决策系统：把推理结论翻译成「这一轮到底要说什么」。

这一步是全项目的分水岭。过了这一步，答案已经定稿：
  - points    = 必须表达出来的语义要点
  - must_include / must_not = 硬约束（校验层据此判定）
  - uncertainty = 整轮确定性（取最弱一环）

语言层拿到的是这个计划，不是一个待续写的故事。它只能选词，不能选题。
"""
from __future__ import annotations

from .contract import Action, Certainty, Claim, ContentPlan, Evidence
from .perception import Percept
from .rules import Inference

# 各类回答的篇幅预算（字数）。陪伴的节奏靠这个守，不靠提示词喊。
_BUDGET = {
    "identity_self": 60,
    "identity_user": 80,
    "recall": 120,
    "acknowledge": 70,
    "clarify": 60,
    "refuse_unknown": 80,
    "answer": 200,
    "act": 60,
}


def _name_claims(claims: tuple[Claim, ...] | list[Claim]) -> dict[str, Claim]:
    """按 subject 取姓名类主张，方便规则判「知不知道」。"""
    out: dict[str, Claim] = {}
    for c in claims:
        if c.predicate == "name":
            out[c.subject] = c
    return out


def facts_for_rules(percept: Percept, claims: list[Claim], *,
                    has_fact: bool, conflict: bool = False) -> dict:
    """把感知与主张压成规则能吃的扁平事实表。

    这层刻意保持"字典 + 布尔"的简单形状：规则不读对象，只判真假，
    这样规则表可以整段替换、单条回退。
    """
    names = _name_claims(claims)
    return {
        "query_target": percept.query_target,
        "intent": percept.intent,
        "user_name_known": names.get("user").known if names.get("user") else False,
        "assistant_name_known": names.get("assistant").known if names.get("assistant") else False,
        "has_fact": bool(has_fact),
        "needs_world": bool(percept.needs_world_knowledge),
        "refs_context": bool(percept.refs_context),
        "conflict": bool(conflict),
    }


def decide(percept: Percept, inference: Inference, claims: list[Claim],
           evidence: list[Evidence], *, query: str = "") -> ContentPlan:
    """产出定稿计划。缺证据就明说缺，绝不用一句「我在思考」糊过去。"""
    then = inference.then
    act = str(then.get("speech_act") or "answer")
    key = str(then.get("template_key") or "")
    names = _name_claims(claims)

    certs = [c.certainty for c in claims] or [Certainty.UNKNOWN]
    uncertainty = Certainty.weakest(certs)

    # --- 证据冲突：不选边，先澄清 ---
    if inference.conflict:
        return ContentPlan(
            speech_act="clarify",
            points=("我这边关于这件事有两条对不上的记录",
                    "得先跟你确认哪一条是对的"),
            claims=tuple(claims),
            must_include=("对不上",),
            must_not=(),
            tone="neutral",
            max_chars=_BUDGET["clarify"],
            template_key="conflict",
            evidence=tuple(evidence),
            uncertainty=Certainty.CONFLICT,
            next_step="说明有冲突，并请他确认；不要自己挑一条当成事实。",
        )

    # --- 身份：问的是他 ---
    if act == "identity_user":
        user_name = names.get("user")
        asst_name = names.get("assistant")
        must_not: list[str] = []
        # 我自己叫什么，绝不能出现在「他是谁」的回答里——真实翻车就是这么来的
        if asst_name and asst_name.known:
            must_not.append(str(asst_name.value))

        if user_name and user_name.known:
            return ContentPlan(
                speech_act="identity_user",
                points=(f"他叫「{user_name.value}」", "他就是正在和你交流的人"),
                claims=tuple(claims),
                must_include=(str(user_name.value),),
                must_not=tuple(must_not),
                tone="neutral",
                max_chars=_BUDGET["identity_user"],
                template_key="identity_user_known",
                evidence=tuple(evidence),
                uncertainty=uncertainty,
                next_step="直接叫出他的名字，只说这一件事，不要展开。",
            )

        # 姓名未知：能确定的只有角色。这是合法答案，不是失败。
        return ContentPlan(
            speech_act="identity_user",
            points=("他是正在和我交流的用户",
                    "他还没有明确告诉过我他的名字"),
            claims=tuple(claims),
            must_include=("用户",),
            must_not=tuple(must_not),
            tone="neutral",
            max_chars=_BUDGET["identity_user"],
            template_key="identity_user_unknown",
            evidence=tuple(evidence),
            uncertainty=Certainty.UNKNOWN,
            next_step=("只说你确定的两点：他是正在和你交流的用户；"
                       "以及你确实不知道他的名字。不许用你自己的名字填进去，也不许猜。"),
        )

    # --- 身份：问的是我 ---
    if act == "identity_self":
        asst_name = names.get("assistant")
        if asst_name and asst_name.known:
            return ContentPlan(
                speech_act="identity_self",
                points=(f"我叫「{asst_name.value}」",),
                claims=tuple(claims),
                must_include=(str(asst_name.value),),
                tone="brief",
                max_chars=_BUDGET["identity_self"],
                template_key="identity_self",
                evidence=tuple(evidence),
                uncertainty=Certainty.CERTAIN,
                next_step="只报自己的名字，一句话说完。",
            )
        return ContentPlan(
            speech_act="identity_self",
            points=("我这边没有存到自己的名字",),
            claims=tuple(claims),
            must_include=("没有",),
            tone="brief",
            max_chars=_BUDGET["identity_self"],
            template_key="identity_self_unknown",
            evidence=tuple(evidence),
            uncertainty=Certainty.UNKNOWN,
        )

    # --- 回忆 ---
    if act == "recall":
        stated = [c for c in claims if c.predicate == "stated" and c.known]
        if not stated:
            return _refuse("recall_miss", claims, evidence, percept)
        top = stated[0]
        return ContentPlan(
            speech_act="recall",
            points=(f"他之前说过：{top.value}",),
            claims=tuple(claims),
            must_include=(str(top.value)[:10],),
            must_not=(),
            tone="neutral",
            max_chars=_BUDGET["recall"],
            template_key="recall_hit",
            evidence=tuple(evidence),
            uncertainty=Certainty.SUPPORTED,
            next_step="引用这一条旧事，再顺着往下接；不要补他没说过的细节。",
        )

    if act == "refuse_unknown":
        return _refuse(key or "recall_miss", claims, evidence, percept)

    # --- 办事：签发动作权限，说话交给执行后的结果 ---
    if act == "act":
        kind = str(then.get("action_kind") or "web_search")
        return ContentPlan(
            speech_act="act",
            points=(),
            claims=tuple(claims),
            actions=(Action(kind, query or percept.text,
                            reason=f"他说的是要办的事（{','.join(percept.hits) or '明说要办'}）",
                            permission="granted"),),
            tone="neutral",
            max_chars=_BUDGET["act"],
            template_key="act",
            evidence=tuple(evidence),
            uncertainty=Certainty.UNKNOWN,
            next_step="先执行动作，再根据真实结果说话；没结果就说没查到，不许编。",
        )

    if act == "acknowledge":
        words = "、".join(percept.hits[:3]) or "这个状态"
        return ContentPlan(
            speech_act="acknowledge",
            points=(f"他现在的状态：{words}", "先接住，不急着给方案"),
            claims=tuple(claims),
            must_include=(),
            must_not=("建议如下", "第一，", "首先，"),
            tone="warm",
            max_chars=_BUDGET["acknowledge"],
            template_key="ack_emotion",
            evidence=tuple(evidence),
            uncertainty=uncertainty,
            next_step="先接一句他的感受，可以问一句具体是哪件事；不要列清单，不要讲道理。",
        )

    if act == "clarify":
        return ContentPlan(
            speech_act="clarify",
            points=("他用了指代词但没交代前文", "问清是哪一件事再答"),
            claims=tuple(claims),
            must_include=(),
            tone="neutral",
            max_chars=_BUDGET["clarify"],
            template_key="clarify_ref",
            evidence=tuple(evidence),
            uncertainty=Certainty.UNKNOWN,
            next_step="用一句话问清他指的具体是哪件事。",
        )

    # --- 默认 answer ---
    return ContentPlan(
        speech_act="answer",
        points=(f"按「{key or 'chat'}」这一档自然回应",),
        claims=tuple(claims),
        tone="neutral",
        max_chars=_BUDGET["answer"],
        template_key=key or "chat_plain",
        evidence=tuple(evidence),
        uncertainty=uncertainty,
        next_step="顺着他的话回应；没有证据支持的事实不要补充。",
    )


def _refuse(key: str, claims: list[Claim], evidence: list[Evidence],
            percept: Percept) -> ContentPlan:
    """「不知道」是一个合法结论，必须能说出口；不许拿编造填空。"""
    return ContentPlan(
        speech_act="refuse_unknown",
        points=("这件事我这边没有可用的记录", "如实说没记清/没查到"),
        claims=tuple(claims),
        must_include=("没",),
        must_not=("我记得", "上次你说的是"),
        tone="neutral",
        max_chars=_BUDGET["refuse_unknown"],
        template_key=key,
        evidence=tuple(evidence),
        uncertainty=Certainty.UNKNOWN,
        next_step="照实说没有记录，并说明缺的是哪一类信息；不许编内容填空。",
    )
