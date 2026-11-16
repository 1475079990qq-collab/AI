"""请求级 prompt 快照构建。

system 内容依次放入：
  0) 身份块（名字 / 称呼）——每轮直连置顶，绝不走检索
  1) persona["system_prompt"]
  2) 事实片段（memory.retrieve 返回的 facts）
  2.2) 长期画像：他是什么人
  2.4) 前几天的事（episodes，压缩过的每日摘要）
  2.5) 他还没收尾的事（open_loops，最多提一件）
  3) "近期情绪事件:" 段落（每条含 label 与 intensity，供 LLM 记忆、
     也供冒烟测试断言「上一轮情绪是否进了 prompt」；格式不得改动）
  4) 对话风格约定（口语、短句、不复读）
  5) "只输出回复正文，禁止输出情感标签/分析/JSON"
最后按时间顺序追加历史对话（user / assistant），再追加本轮 {"role":"user"}。

知识库不再出现在这里：它曾经每轮被检索并整段灌入（纪要元文本一起进来），
既是「听不懂」的源头，也是 2 秒阻塞的源头。现在只在明确提问时由工具去查。
"""
from __future__ import annotations

from brain.schemas import EmotionResult

_OUTPUT_RULE = (
    "只输出你这个角色要说的话本身。\n"
    "禁止输出任何括号里的旁白、情绪标注、语气说明或动作描写（例如「（语气轻松）」「（接住情绪）」）；"
    "禁止解释自己为什么这样回答；禁止输出情感标签、分析或 JSON。"
)
_DEFAULT_SECTION_TITLE = "长期记忆 / 知识片段"

# 对话风格：用户反馈「回复很怪、不像人」，这里把口语化要求写死在 system 里，
# 不依赖人格模板写得多细。历史对话条数有限（chat.py 只留最近几轮）。
_STYLE_RULE = (
    "聊天方式：像真人朋友随口说话，不用书面语、不用客服腔、不要排比和总结。\n"
    "- 一般只说 1-2 句，短句为主，别长篇大论。\n"
    "- 不要重复上一次说过的话，也不要把问题原样反问回去。\n"
    "- 对方只说「啥」「哦」「嗯」这类很短的词时，就顺着上文自然接一句，"
    "可以反问、可以打趣，别当成没听懂。\n"
    "- 需要追问时最多问一个问题。"
)

# 用户实测反馈：「每次都用『作为陪伴者，我更擅长聊生活里的小确幸』把话题岔开，
# 问了三遍都不正面回答」。人格里的「不是顾问」被小模型过度泛化成「不聊任何正经话题」，
# 这里必须把边界收窄，并把「对方追问 = 上一轮没答到点上」写死。
_NO_DODGE_RULE = (
    "【必须正面回应，不许岔开话题】\n"
    "- 对方问技术、设备、行业、投资、行情、产品这类问题，就用你确实知道的回答一两句；"
    "确实不知道就照实说不清楚，但不要回避问题本身。\n"
    "- 禁止用「我更擅长聊生活里的小确幸」「我们把话题变成轻松对话」「你想聊哪个方向」"
    "这类话把人挡回去，也不要在对方已经说清楚话题时再反问他想聊什么。\n"
    "- 人格里的「不给专业建议」只指看病、用药、法律、心理危机这四类，"
    "不是拒绝日常讨论的借口。\n"
    "- 对方问「怎么办」「怎么放」「选哪个」这类具体问题时，至少给一个具体的选项或判断依据"
    "（比如先看用多久、先留应急的钱、先看波动范围），不要只回「慢慢来」「别急」「耐心点」这种空话。\n"
    "- 如果对方连续追问同一件事，说明你上一轮答得太空，这一轮要说出更具体的内容"
    "（具体环节、趋势、例子、注意点），而不是换个说法再绕一遍。\n"
    "- 对方表达不满（说你没听他的、在抱怨你）时，先直接回应他的不满，"
    "不要继续讲上一个话题。"
)


# 语色：用户反复反馈「假假的、像客服」。这里写的每一条都对应一次真实翻车，
# 不是抽象要求——对小模型来说，「别说这句」比「要自然一点」有用得多。
_TONE_RULE = (
    "【说话味儿：像人，不像客服，也不像 AI 自我说明】\n"
    "以下每一条都是真出过的问题，出现任何一条都算错：\n"
    "- 不写括号旁白。不写「（语气轻松）」「（接住情绪）」「（保持开放，不设限）」"
    "这类自我说明，只写要说的话。\n"
    "- 不用客服腔。「抱歉刚才没明确回应你的需求」「下次需要帮忙的话，我会先确认」"
    "「我确实无法直接回应指令」——朋友不会这样汇报工作。\n"
    "- 不列选项追问。「长期规划？具体产品？还是最近市场波动？」这种三连问像在填表；"
    "一次最多问一个问题，而且要口语，不要菜单。\n"
    "- 不单独说空话。「可以慢慢来」「别急」「耐心点」单摆着就是敷衍；要给就给一件具体的事。\n"
    "- 不复述他说过的话，不总结，不解释自己为什么这么回答。\n"
    "怎么说才对：先接住他这句话里的那件具体事（哪笔钱、哪个名字、哪只产品），"
    "再说你的真实反应；句子可以短、可以不完整（「嗯……那确实够呛」）；"
    "可以有态度（他太急就说「你这急得有点早」），不必永远中立。"
)


def build(persona: dict, bundle: dict, emotion: EmotionResult, text: str,
          history: list[dict] | None = None) -> list[dict]:
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

    # 1) 人格模板
    blocks.append(str((persona or {}).get("system_prompt", "")).strip())

    # 2) 事实片段
    facts = (bundle or {}).get("facts") or []
    fact_lines = ["以下是你可能记得的关于对方的事实："]
    for f in facts:
        frag = str(f.get("text", "")).strip()
        # 碎句 / 问句不当事实注入：这些是用户的原话，模型会把它们当既定前提反复引用。
        if len(frag) < 12 or frag.endswith(("？", "?", "。", "吗", "呢")):
            continue
        fact_lines.append(f"- {frag}")
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

    # 2.6) 反刍：上一轮说完之后，自己复盘出的一条「下次怎么改」。
    #      条数 / 字数都有预算：最多 3 条、每条 180 字、整段 480 字，别把 prompt 撑爆。
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

    # 4) 对话风格约定
    blocks.append(_STYLE_RULE)

    # 4.5) 不许岔开话题（人格里的"不做顾问"常被小模型过度泛化成拒答）
    blocks.append(_NO_DODGE_RULE)

    # 4.6) 语色：把「假」的具体样子一条条列出来，放在靠近输出端的位置
    blocks.append(_TONE_RULE)

    # 5) 输出约束
    blocks.append(_OUTPUT_RULE)

    # 历史对话：只放用户说过的话，绝不放自己上一轮的回复。
    # 2B 模型看到自己上一轮的原话会整段照抄（实测连续四轮开头一字不差），
    # 去掉这份可抄的样本才是治本；用户的话收进 system，保持 system+user 的干净结构。
    prior: list[str] = []
    for turn in (history or []):
        if not isinstance(turn, dict):
            continue
        if str(turn.get("role") or "").strip() == "user":
            content = str(turn.get("content") or "").strip()
            if content:
                prior.append(content)
    system = "\n\n".join(b for b in blocks if b)
    if prior:
        system += "\n\n他前面还说过这些（按时间先后，别拿来复述）：\n" + \
                  "\n".join(f"- {p}" for p in prior[-4:])
    return [{"role": "system", "content": system},
            {"role": "user", "content": text}]
