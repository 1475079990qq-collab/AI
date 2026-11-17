"""请求级 prompt 快照构建。

system 内容依次放入：
  0) 身份块（名字 / 称呼）——每轮直连置顶，绝不走检索
  0.5) 当天时间——硬事实
  1) persona["system_prompt"]
  1.5) 本轮认知调度：他要什么、这轮怎么接、能说多少（cognitive.planner 产出）
  2) 事实片段（带类型与重要度，低价值流水已被 memory.retrieve 滤掉）
  2.2) 长期画像 / 2.4) 前几天的事 / 2.5) 未收尾的事
  2.6) 自我复盘（按权重取的三条经验）/ 2.7) 已验证的策略（最多一条）
  2.8) 联想词（这一轮激活扩散带出来的概念，可选）
  3) "近期情绪事件:" 段落（格式不得改动，冒烟测试据此断言）
  3.5) V2.5 自身状态 + 活跃目标（bundle 带 state/goals 时才出现，一句人话）
  4) 说话方式：按场景给长度，不再一刀切「只说 1-2 句」
  5) 必须正面回应 / 不写旁白
最后按真实对话结构追加历史：user / assistant 原样轮次，再追加本轮 user。

V2 的两处关键修改：
- 历史里找回 assistant 的回复。旧实现把「自己说过的话」全部丢掉，只留用户发言，
  模型看不见自己上一轮说了什么，连续对话在第二轮就断了（还会把用户的话当既定前提）。
  防抄靠两道：system 里明确「记录不是模板」，以及 pipeline 的开头重合检测。
- 说话方式不再是一套固定禁令。长度/是否分步/要不要反问，由这一轮的意图决定
  （cognitive.attention 给出预算），人格稳定、表达随场景变。
"""
from __future__ import annotations

from brain.schemas import EmotionResult
from brain.services import clock

_OUTPUT_RULE = (
    "只输出你这个角色要说的话本身。\n"
    "禁止输出任何括号里的旁白、情绪标注、语气说明或动作描写（例如「（语气轻松）」「（接住情绪）」）；"
    "禁止解释自己为什么这样回答；禁止输出情感标签、分析或 JSON。"
)
_DEFAULT_SECTION_TITLE = "长期记忆 / 知识片段"

# 说话方式：保留「哪些句式真出过问题」，但长度交给场景，不再写死 1-2 句。
_STYLE_RULE = (
    "【说话方式】像真人朋友随口说话，不是写作文，也不是客服。\n"
    "- 长短跟着事走：闲聊几句就够；技术、设备、具体问题该说清楚就说清楚，"
    "可以分步骤、可以举例；不要为了显得简洁把话说空。\n"
    "- 对方只说「啥」「哦」「嗯」这类很短的词时，顺着上文自然接一句，"
    "可以反问、可以打趣，别当成没听懂。\n"
    "- 不复述他说过的话，不总结，不排比，不要每次都「首先……其次……最后」。\n"
    "- 他这句话里有具体的东西（哪台机器、哪个名字、哪笔钱、哪个词），"
    "先接住那件事再说你的反应；别上来就讲道理。\n"
    "- 你可以有态度，可以不完整（「嗯……那确实够呛」），不必永远中立、永远温柔。"
)

# 用户实测反馈：「每次都用『作为陪伴者，我更擅长聊生活里的小确幸』把话题岔开，
# 问了三遍都不正面回答」。这段是收窄边界，不是加禁令：只把「拒答」这一种错堵死。
_NO_DODGE_RULE = (
    "【必须正面回应，不许岔开】\n"
    "- 对方问技术、设备、行业、投资、行情、产品这类问题，就用你确实知道的回答；"
    "确实不知道就照实说不清楚，但不要回避问题本身。\n"
    "- 不要用「我更擅长聊生活里的小确幸」「我们把话题变成轻松对话」「你想聊哪个方向」"
    "把人挡回去，也不要在对方已经说清话题时再问他「想聊什么」。\n"
    "- 人格里的「不给专业建议」只指看病、用药、法律、心理危机这四类，"
    "不是拒绝日常讨论的借口。\n"
    "- 对方问「怎么办」「怎么弄」「选哪个」时，至少给一个具体的判断依据或选项。\n"
    "- 对方连续追问同一件事，说明上一轮答得太空：这轮说更具体的内容，"
    "而不是换个说法再绕一遍。\n"
    "- 对方在抱怨你、说你没听他的，先直接回应这件事，不要接着讲上一个话题。"
)

# 语色：每条都对应一次真实翻车；「别说这句」对小模型比「自然一点」有用。
_TONE_RULE = (
    "【说话味儿：像人，不像 AI 自我说明】\n"
    "- 不写括号旁白，不做自我说明，只写要说的话。\n"
    "- 不用客服腔（「抱歉刚才没明确回应你的需求」「我确实无法直接回应指令」）。\n"
    "- 不列菜单式追问（「长期规划？具体产品？还是最近波动？」），一次最多问一个问题。\n"
    "- 不单独说空话（「可以慢慢来」「别急」「耐心点」单摆着就是敷衍），要给就给一件具体的事。\n"
    "- 不解释自己为什么这么回答。"
)

_HISTORY_TURNS = 6          # 最近 6 轮（12 条消息）足够接上下文，又不至于挤掉记忆
_HISTORY_CHARS = 220        # 单条截断，防止一条长回复把上下文吃光


def _history_messages(history: list[dict] | None) -> list[dict]:
    """把历史还原成真正的对话轮次（user / assistant 都留）。

    只保留 user/assistant 两种角色，空内容丢掉，超长截断；取最近 N 轮。
    这就是「连续对话能力」的基本盘：模型得看得见自己上一轮说了什么。
    """
    msgs: list[dict] = []
    for turn in (history or []):
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip()
        if role not in ("user", "assistant"):
            continue
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        if len(content) > _HISTORY_CHARS:
            content = content[:_HISTORY_CHARS] + "…"
        msgs.append({"role": role, "content": content})
    if not msgs:
        return []
    return msgs[-_HISTORY_TURNS * 2:]


def _plan_block(plan: dict | None) -> str:
    """把这一轮的调度决定写成一小段。没有 plan 时整段不出现（不影响旧调用）。"""
    if not plan:
        return ""
    intent_cn = str(plan.get("intent_cn") or "").strip()
    how = str(plan.get("plan_text") or "").strip()
    length_hint = str(plan.get("length_hint") or "").strip()
    directives = [str(d).strip() for d in (plan.get("directives") or []) if str(d).strip()]
    lines = ["【这一轮怎么接｜这是系统对本轮的判断，不是人格】"]
    if intent_cn:
        lines.append(f"- 他要的是：{intent_cn}")
    if how:
        lines.append(f"- 这一轮这么办：{how}")
    if length_hint:
        lines.append(f"- 篇幅：{length_hint}")
    for d in directives[:3]:
        lines.append(f"- {d}")
    lines.append("- 以上和人格冲突时，人格不变，只按这里的「怎么办」调整；"
                 "不要把这句判断念出来，也不要提到「系统」。")
    return "\n".join(lines)


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _self_state_block(state, goals) -> str:
    """V2.5：把「自身状态 + 活跃目标」渲染成一两句人话。

    数据由 bundle 提供（T25 负责从编排结果取数塞进 bundle），这里只渲染。
    bundle 缺 state / goals、或两者都空时返回 ""（整段不出现，不影响旧链路）。
    """
    # 状态：只在真正设置过（updated_at 非空）时渲染；默认零状态视为「无状态」。
    state_text = ""
    if isinstance(state, dict) and str(state.get("updated_at") or "").strip():
        label = str(state.get("label") or state.get("mood") or "").strip()
        if not label:
            valence = _to_float(state.get("valence"), 0.0)
            energy = _to_float(state.get("energy"), 0.5)
            if valence <= -0.35:
                label = "有点低落"
            elif valence >= 0.35:
                label = "心情不错"
            elif energy <= 0.25:
                label = "有点累"
            elif energy >= 0.8:
                label = "挺有精神"
            else:
                label = "平静"
        state_text = "你现在的状态：" + label[:16]

    # 目标：只带还记挂的；abandoned / done 不进提示词（设计文档块②）。
    titles: list[str] = []
    for goal in (goals or []):
        if isinstance(goal, dict):
            if str(goal.get("status") or "") in ("abandoned", "done"):
                continue
            title = str(goal.get("title") or "").strip()
        else:
            title = str(goal).strip()
        title = title[:24]
        if title:
            titles.append(title)
    goals_text = ""
    if titles:
        goals_text = "记着的事：" + "、".join(titles[:3])

    parts = [part for part in (state_text, goals_text) if part]
    if not parts:
        return ""
    return "；".join(parts) + "。"


def build(persona: dict, bundle: dict, emotion: EmotionResult, text: str,
          history: list[dict] | None = None,
          plan: dict | None = None) -> list[dict]:
    blocks: list[str] = []

    # 0) 身份：置顶、每轮直连。名字只允许有一个来源，下文任何称呼以这里为准。
    ident = (bundle or {}).get("identity") or {}
    assistant = str(ident.get("assistant") or "").strip()
    user_name = str(ident.get("user") or "").strip()
    if assistant or user_name:
        id_lines = ["【身份｜最高优先级，与任何段落冲突时以本段为准】"]
        if assistant:
            id_lines.append(f"- 你叫「{assistant}」。别人怎么叫你、你怎么自称，都只用这个名字。")
            # 记忆里可能还留着改名前的旧名字（那是对方当时的原话）。
            # 小模型看到「你以前不叫 X」会顺手自报 X，这里先把这条堵死。
            id_lines.append(
                f"- 记忆或上下文里如果出现别的名字、旧名字（哪怕写的是「你以前叫 X」），"
                f"你现在只叫「{assistant}」，不要自报旧名字，也不要解释改名经过。"
            )
        if user_name:
            id_lines.append(f"- 对方叫「{user_name}」，可以直接叫他名字。")
        blocks.append("\n".join(id_lines))

    # 0.5) 当天时间：硬事实，由后端按 Asia/Shanghai 注入，模型不许自己估。
    blocks.append(
        "【今天是哪天｜硬事实，与任何旧对话、记忆里的日期冲突时以本段为准】\n"
        f"- 现在是 {clock.today_label()}。\n"
        "- 被问到日期、星期、几号时只能按上面说；记不准就说「我记不太清具体日子」，"
        "不要编年份、月份、日期或星期，也不要沿用记忆或旧对话里出现过的旧日期。"
    )

    # 1) 人格模板
    blocks.append(str((persona or {}).get("system_prompt", "")).strip())

    # 1.5) 本轮认知调度（意图 / 怎么接 / 篇幅）
    blocks.append(_plan_block(plan))

    # 2) 事实片段（带类型；低价值流水已在召回侧滤掉，这里只按长度和句式再挡一层）
    facts = (bundle or {}).get("facts") or []
    fact_lines = ["以下是你可能记得的关于对方的事实："]
    for f in facts:
        frag = str(f.get("text", "")).strip()
        # 碎句 / 问句不当事实注入：这些是用户的原话，模型会把它们当既定前提反复引用。
        if len(frag) < 12 or frag.endswith(("？", "?", "。", "吗", "呢")):
            continue
        kind = str(f.get("kind") or "").strip()
        tag = {"identity": "（身份）", "constraint": "（禁忌/身体状况）",
               "preference": "（偏好）", "relation": "（家人朋友）",
               "goal": "（长期打算）"}.get(kind, "")
        fact_lines.append(f"- {frag}{tag}")
    if len(fact_lines) > 1:
        blocks.append("\n".join(fact_lines))

    # 2.2) 长期画像：他是什么人（称呼 / 偏好 / 雷区）。压缩过的一页纸，
    #      每轮都带，比按话题检索更稳定，避免「每次都像第一次见面」。
    profile_text = str((bundle or {}).get("profile") or "").strip()
    if profile_text:
        blocks.append(
            "关于对方，你一直记得这些：\n" + profile_text +
            "\n（说明：「雷区」只是别主动提起，不是禁止讨论——他自己提起来就正常聊。）"
        )

    # 2.4) 前几天的事：压缩过的每日摘要，一直在身上，不用检索。
    episodes = (bundle or {}).get("episodes") or []
    if episodes:
        ep_lines = ["你记得前几天的事："]
        for ep in episodes:
            day = str(ep.get("day") or "").strip()
            body = str(ep.get("text") or "").strip()
            if not body:
                continue
            ep_lines.append(f"{day}：\n{body}")
        if len(ep_lines) > 1:
            blocks.append("\n".join(ep_lines) +
                          "\n（这是你自己的记忆，别逐条复述，挑合适的接一句就行。）")

    # 2.5) 他还没收尾的事：这一轮最多自然提一件，多提就成了催促。
    loops = (bundle or {}).get("open_loops") or []
    if loops:
        loop_lines = ["他提过、还没收尾的事（这一轮最多自然地提一件，别提第二件）："]
        for item in loops:
            txt = str(item.get("text") or "").strip()
            if txt:
                loop_lines.append(f"- {txt}")
        if len(loop_lines) > 1:
            blocks.append("\n".join(loop_lines))

    # 2.6) 经验：按权重取的三条「下次怎么改」。条数 / 字数都有预算。
    lessons = (bundle or {}).get("lessons") or []
    lesson_lines = ["【自我复盘｜你自己总结过的，这一轮别重犯】"]
    _LESSON_MAX = 3
    _LESSON_CHARS = 180
    _LESSON_BUDGET = 480
    _lesson_used = 0
    for item in lessons[:_LESSON_MAX]:
        note = str((item or {}).get("note") or "").strip()
        if not note:
            continue
        note = "\n".join(line.strip() for line in note.splitlines() if line.strip())
        if len(note) > _LESSON_CHARS:
            note = note[:_LESSON_CHARS] + "…"
        if _lesson_used + len(note) > _LESSON_BUDGET:
            break
        _lesson_used += len(note)
        lesson_lines.append(f"- {note}")
    if len(lesson_lines) > 1:
        blocks.append("\n".join(lesson_lines))

    # 2.7) 已验证的策略：一次只带一条，带版本来源，方便回滚后对得上。
    strategy_line = str((bundle or {}).get("strategy") or "").strip()
    if strategy_line:
        blocks.append(
            "【已经验证过的做法｜同类问题按这个来】\n- " + strategy_line[:110] +
            "\n（这是从反复出现的教训里抽出来的，不是本轮新加的规则。）"
        )

    # 2.8) 联想词：这一轮由记忆激活扩散带出来的概念，只做提示，不许硬扯。
    assoc = (bundle or {}).get("association") or {}
    concepts = [str(c).strip() for c in (assoc.get("concepts") or []) if str(c).strip()]
    if concepts:
        blocks.append(
            "这一轮被带亮的关联词：" + "、".join(concepts[:6]) +
            "\n（只有当它们和对方这句话确实有关时才用，别为了显得记得而硬扯。）"
        )

    # 3) 近期情绪事件：label 值本身入文（冒烟测试可据此断言），并带 intensity
    emotions = (bundle or {}).get("emotions") or []
    emo_lines = ["近期情绪事件:"]
    for ev in emotions:
        label = str(ev.get("emotion_label") or ev.get("label") or "neutral")
        try:
            intensity = float(ev.get("intensity", 0.0))
        except (TypeError, ValueError):
            intensity = 0.0
        summary = str(ev.get("summary") or "").strip()
        if len(summary) > 40:
            summary = summary[:40] + "…"
        line = f"- label={label}, intensity={intensity:.2f}"
        if summary:
            line += f", summary={summary}"
        emo_lines.append(line)
    # 当前轮只给强度（无 label），避免诱导 LLM 输出情感标签
    try:
        cur = float(emotion.intensity) if emotion is not None else 0.0
    except (TypeError, ValueError):
        cur = 0.0
    emo_lines.append(f"（本轮情绪强度参考：{cur:.2f}；请勿输出任何标签）")
    blocks.append("\n".join(emo_lines))

    # 4) 说话方式（长度已交给 1.5 的篇幅预算，这里只管「像不像人」）
    blocks.append(_STYLE_RULE)

    # 4.5) 不许岔开话题（人格里的"不做顾问"常被小模型过度泛化成拒答）
    blocks.append(_NO_DODGE_RULE)

    # 4.6) 语色：把「假」的具体样子一条条列出来，放在靠近输出端的位置
    blocks.append(_TONE_RULE)

    # 5) 输出约束
    blocks.append(_OUTPUT_RULE)

    system = "\n\n".join(b for b in blocks if b)

    # V2.5：自身状态 + 活跃目标（从 bundle 读；缺键或为空时整段不出现，旧链路不受影响）
    self_state = _self_state_block(
        (bundle or {}).get("state"), (bundle or {}).get("goals"))
    if self_state:
        system = system + "\n\n" + self_state

    # 历史：真正的对话轮次（user + assistant 都在）。它是记忆，不是模板。
    history_msgs = _history_messages(history)
    if history_msgs:
        system += ("\n\n下面是你和他刚才的对话记录（按时间先后）。这是记忆，不是模板："
                   "不要照抄你自己说过的话，不要重复上一轮的开头，也不要把它当成还要回答的问题。")
    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(history_msgs)
    messages.append({"role": "user", "content": str(text or "")})
    return messages
