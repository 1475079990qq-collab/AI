"""语言表达层：只负责「怎么说」。

两条路，一条都不能少：
  1) 模板路：零模型、零延迟、绝对忠实。结构性回答（身份/回忆/澄清/拒绝）优先走它。
  2) 模型路：把 ContentPlan 当成**表达任务**交给模型，不是让它重新回答一遍。

关键区别在这里：给模型的不是「用户问：我是谁，你答吧」，
而是「下面这段意思必须原样说出来，你可以调语气，不许改事实」。

模板返回 None 表示「这一档模板说不像」，必须走模型——这也是为什么模型还留着。
"""
from __future__ import annotations

from .contract import ContentPlan, RenderResult

# 占位符：{assistant} {user} {value} {words}
# None = 这一档模板不适用（闲聊靠模型），不是"没写"。
TEMPLATES: dict[str, str | None] = {
    "identity_user_unknown":
        "你是正在和我交流的用户。不过你还没告诉过我你的名字，所以我暂时不知道你叫什么。",
    "identity_user_known":
        "你是{user}，一直在这儿跟我聊着。",
    "identity_self":
        "我叫{assistant}。",
    "identity_self_unknown":
        "我这边没存到自己的名字。",
    "recall_hit":
        "你之前说过：{value}。",
    "recall_miss":
        "这件事我这边没有可用的记录，不敢乱说。",
    "conflict":
        "我这边关于这件事有两条对不上的记录，得先跟你确认一下，哪条才算数？",
    "clarify_ref":
        "你说的这个，是指哪件事？我怕接歪了。",
    "ack_emotion":
        "这话听着不轻。先说说，是哪件事把你耗成这样？",
    "act":
        None,
    "tech":
        "先说说具体现象：什么时候开始的、报什么错、中间动过什么？我按这个往下排。",
    "chat_plain":
        None,
    "filler":
        None,
    "greet":
        None,
}


def _fill(tpl: str, plan: ContentPlan) -> str:
    """只做占位替换，不做任何改写。取值一律来自计划里的主张。"""
    names = {c.subject: str(c.value) for c in plan.claims
             if c.predicate == "name" and c.known}
    stated = [str(c.value) for c in plan.claims
              if c.predicate == "stated" and c.known]
    text = tpl
    text = text.replace("{assistant}", names.get("assistant", ""))
    text = text.replace("{user}", names.get("user", ""))
    text = text.replace("{value}", stated[0] if stated else "")
    text = text.replace("{words}", "、".join(plan.points[:1]))
    return text.strip()


def render_template(plan: ContentPlan) -> RenderResult | None:
    """模板表达。做不到就返回 None，由调用方决定走模型还是兜底。"""
    tpl = TEMPLATES.get(plan.template_key)
    if tpl is None:
        if plan.fallback_text:
            return RenderResult(plan.fallback_text, by="template")
        return None
    text = _fill(tpl, plan)
    if not text:
        return None
    return RenderResult(text, by="template")


def build_render_prompt(plan: ContentPlan) -> str:
    """把定稿计划改写成「表达任务」。这是给模型看的唯一输入，不给原对话。"""
    lines = [
        "你不是在重新回答问题，你是在把下面这段意思说出来。",
        "意思不能变，事实不能加，名字不能换。只允许调整语气、语序、长度。",
        "",
        "必须表达：",
    ]
    for p in plan.points:
        lines.append(f"- {p}")
    if plan.must_include:
        lines.append("必须出现：" + "、".join(plan.must_include))
    if plan.must_not:
        lines.append("绝对不许出现：" + "、".join(plan.must_not))
    lines.append(f"长度：不超过 {plan.max_chars} 字。")
    lines.append(f"语气：{plan.tone}。")
    lines.append(f"要求：{plan.next_step}")
    lines.append("只输出这句话本身，不要解释你在做什么。")
    return "\n".join(lines)


def render_with_model(plan: ContentPlan, model_fn) -> RenderResult | None:
    """模型表达。model_fn 由调用方注入（可换模型、可 mock、可超时）。

    任何异常或空输出都返回 None——表达失败不该变成"胡说一句"，
    而应该回退模板。
    """
    if model_fn is None:
        return None
    try:
        out = model_fn(build_render_prompt(plan))
    except Exception:
        return None
    text = str(out or "").strip()
    if not text:
        return None
    return RenderResult(text[: max(plan.max_chars * 2, 40)], by="model")
