"""请求级 prompt 快照构建。

system 内容依次放入：
  1) persona["system_prompt"]
  2) 事实片段（memory.retrieve 返回的 facts）
  2.5) 长期记忆 / 知识片段（bundle["knowledge"]，标题取 knowledge.section_title）
  3) "近期情绪事件:" 段落（每条含 label 与 intensity，供 LLM 记忆、
     也供冒烟测试断言「上一轮情绪是否进了 prompt」；格式不得改动）
  4) 对话风格约定（口语、短句、不复读）
  5) "只输出回复正文，禁止输出情感标签/分析/JSON"
最后按时间顺序追加历史对话（user / assistant），再追加本轮 {"role":"user"}。
历史对话让「啥」「哦」这类短句也有上下文可依，避免每轮像第一次见面。
"""
from __future__ import annotations

from brain.schemas import EmotionResult

try:
    from brain.services import providers
except Exception:  # pragma: no cover - 取决于 S1 是否落地
    providers = None

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


def _section_title() -> str:
    """从 knowledge 配置读段落标题；异常 / 空值回退默认标题。"""
    try:
        if providers is not None:
            raw = providers.get("knowledge") or {}
            cfg = raw.get("config") if isinstance(raw.get("config"), dict) else {
                k: v for k, v in raw.items()
                if k not in {"code", "name", "category", "enabled", "mode",
                             "version", "updated_at", "fields"}
            }
            title = str(cfg.get("section_title") or "").strip()
            if title:
                return title
    except Exception:
        pass
    return _DEFAULT_SECTION_TITLE


def build(persona: dict, bundle: dict, emotion: EmotionResult, text: str,
          history: list[dict] | None = None) -> list[dict]:
    blocks: list[str] = []

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

    # 2.5) 长期记忆 / 知识片段：位于事实之后、「近期情绪事件:」之前。
    #      该段落不含「近期情绪事件」字样，不影响 mock 回显断言。
    knowledge = (bundle or {}).get("knowledge") or []
    if knowledge:
        kn_lines = [_section_title() + ":"]
        for item in knowledge:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            source = str(item.get("source") or "").strip()
            # 碎句过滤：一段一两句话的「记忆」没有可复用信息，塞进来只会让模型
            # 抓住那几个词反复复述（用户实测的复读就是这么来的）。
            if len(content) < 20:
                continue
            parts = [p for p in (title, content) if p]
            if not parts:
                continue
            line = "- " + ("：".join(parts) if len(parts) > 1 else parts[0])
            if source:
                line += f"（来源:{source}）"
            kn_lines.append(line)
        if len(kn_lines) > 1:
            blocks.append("\n".join(kn_lines))

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
