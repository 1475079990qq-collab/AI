"""认知总引擎：一轮话按真实顺序跑完，每一步都留痕。

顺序（与设计文档一一对应，不跳步、不并行糊在一起）：
  感知 → 身份/记忆 → 事实联想 → 规则推理 → 决策 → 表达 → 校验 → 输出

两条重要纪律：
  1) 表达**模板优先**：结构性问题（身份/回忆/澄清/拒绝）用模板就能说准，
     根本不需要大模型。模型只在「模板说不像」的时候上（闲聊、技术）。
  2) 动作在表达之前签发：要办事就先去办，办完再说话；不许先编一句再去查。
"""
from __future__ import annotations

from typing import Callable

from .check import check
from .contract import Certainty, ContentPlan, RenderResult, TurnOutcome
from .decide import decide, facts_for_rules
from .knowledge import WorldSnapshot, clean_facts, find_claims, identity_claims
from .perception import understand
from .render import render_template, render_with_model
from .rules import infer
from .trace import EventLog

_DEGRADED_TEXT = "我这边有点乱，先按你刚才说的接着聊。"


def run_turn(text: str, snap: WorldSnapshot,
             model_fn: Callable[[str], str] | None = None) -> TurnOutcome:
    """跑一轮。任何异常都降级成一句实话，绝不把异常抛给对话链路。"""
    log = EventLog()
    try:
        # ---- 1) 感知：谁在问谁 ----
        percept = understand(text, has_history=snap.has_history)
        log.add("perception", "parse",
                because=(f"说话人=user；代词 我→user、你→assistant；"
                         f"命中={','.join(percept.hits) or '无'}"),
                sources=(),
                region="perception",
                intent=percept.intent, query_target=percept.query_target)

        # ---- 2) 身份：硬事实先摆出来 ----
        claims, ev = identity_claims(snap)
        name_bits = [f"{c.subject}:{c.value or '未知'}" for c in claims if c.predicate == "name"]
        log.add("memory", "identity_lookup",
                because="；".join(name_bits),
                sources=("identity:assistant_name", "identity:user_name"),
                region="memory",
                known_user_name=any(c.predicate == "name" and c.subject == "user" and c.known
                                    for c in claims))

        # ---- 3) 事实联想：洗掉问句，只留陈述 ----
        dropped = len(snap.facts) - len(clean_facts(snap.facts))
        f_claims, f_ev = find_claims(snap, text)
        claims = claims + f_claims
        ev = ev + f_ev
        log.add("memory", "fact_recall",
                because=(f"召回 {len(f_claims)} 条陈述，挡下 {dropped} 条问句/碎句"
                         if snap.facts else "事实库为空"),
                sources=tuple(str(e.ref) for e in f_ev if e.kind == "fact"),
                region="memory")

        # ---- 4) 规则推理：条件满足则结论成立 ----
        facts = facts_for_rules(percept, claims, has_fact=bool(f_claims))
        inf = infer(facts)
        log.add("judge", "rule_infer",
                because=(f"命中规则 {inf.rule.id}（{inf.rule.note}）" if inf.rule
                         else "没有规则命中，落到默认回应"),
                sources=(f"rule:{inf.rule.id}",) if inf.rule else (),
                region="judge",
                considered=list(inf.considered), conflict=list(inf.conflict))

        # ---- 5) 决策：把答案定下来 ----
        plan = decide(percept, inf, claims, ev, query=str(text or ""))
        log.add("judge", "decide",
                because=f"speech_act={plan.speech_act}，确定性={plan.uncertainty.value}",
                sources=tuple(e.ref for e in plan.evidence[:4]),
                region="judge",
                must_include=list(plan.must_include),
                must_not=list(plan.must_not))

        # ---- 5.5) 要办事：先签发动作，不先说 ----
        if plan.needs_action:
            for a in plan.actions:
                if a.allowed:
                    log.add("judge", "issue_action",
                            because=a.reason, region="judge",
                            action_kind=a.kind, query=a.query)
            return TurnOutcome(plan=plan, events=log.to_list(), awaiting_action=True)

        # ---- 6) 表达：模板优先，模板说不像才上模型 ----
        out = _render(plan, model_fn=model_fn, log=log)

        # ---- 7) 校验 + 回退 ----
        result = check(plan, out.text)
        log.add("express", "verify",
                because=("通过" if result.ok else "不通过：" + ",".join(result.failures)),
                region="express", by=out.by, chars=len(out.text))

        if not result.ok and out.by == "model":
            # 模型说了不该说的：给它一次机会，仍不过就换模板/兜底
            retry = render_with_model(plan, model_fn)
            if retry is not None:
                again = check(plan, retry.text)
                if again.ok:
                    out, result = retry, again
                    log.add("express", "verify_retry",
                            because="重生成一次后通过", region="express")
        if not result.ok:
            tpl = render_template(plan)
            if tpl is not None and check(plan, tpl.text).ok:
                out, result = tpl, check(plan, tpl.text)
                log.add("express", "fallback_template",
                        because="表达不合规，回退模板", region="express",
                        failures=list(result.failures))
            else:
                out, result = out, result

        final = out.text if result.ok else (plan.fallback_text or _DEGRADED_TEXT)
        log.add("express", "emit", because=f"最终由 {out.by} 产出，校验{'通过' if result.ok else '未通过（用兜底句）'}",
                region="express", chars=len(final))

        return TurnOutcome(plan=plan, render=out, check=result,
                           events=log.to_list(), final_text=final)
    except Exception as exc:  # 认知内核崩了也不能让用户干等
        log.add("judge", "degraded", because=f"{type(exc).__name__}: {exc}",
                region="judge")
        plan = ContentPlan(speech_act="answer", points=(), uncertainty=Certainty.UNKNOWN,
                           fallback_text=_DEGRADED_TEXT)
        return TurnOutcome(plan=plan, events=log.to_list(), final_text=_DEGRADED_TEXT)


def _render(plan: ContentPlan, *, model_fn, log: EventLog):
    """表达策略：能模板就模板；模板 None 才走模型；模型也不行就兜底。"""
    tpl = render_template(plan)
    if tpl is not None:
        log.add("express", "render_template",
                because=f"模板 {plan.template_key} 可覆盖这一档",
                region="express", chars=len(tpl.text))
        return tpl

    if model_fn is not None:
        got = render_with_model(plan, model_fn)
        if got is not None:
            log.add("express", "render_model",
                    because=f"模板 {plan.template_key} 不适用，交给语言模型",
                    region="express", chars=len(got.text))
            return got

    # 既没模板也没模型：说一句实话，不装
    log.add("express", "render_fallback",
            because="模板与模型都不可用", region="express")
    return RenderResult(plan.fallback_text or _DEGRADED_TEXT, by="template")
