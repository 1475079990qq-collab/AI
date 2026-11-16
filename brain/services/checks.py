"""规则自检（判官是规则，不是模型）。

为什么不交给模型自评：2B 模型的自我打分对语义失败几乎无感（没答到点、编了地址
都判不出来），而且天然偏向自己的输出。而实际投诉集中处基本都是机械失败——
自称错、复读、带括号旁白、太长、延迟超阈、记忆灌垃圾——这些用规则判是 100% 确定的，
而且是零成本、可解释、能直接当告警用的。

一条输出经过这里只做三件事：算出命中规则、给出 pass/warn/fail、落库。
不做纠正、不发事件、不影响对话。
"""
from __future__ import annotations

import re

# 括号旁白：整句以括号开头（「（语气轻松）…」），或句中出现 2-20 字的括号说明
_NARRATION_RE = re.compile(r"^[（(]")
_INLINE_NARRATION_RE = re.compile(r"[（(][^）)]{2,20}[）)]")

# 破人格话术：出现这些等于把自己当工具，比答错更伤。
_BREAKING = ("作为AI", "作为 AI", "作为一个AI", "作为一个人工智能",
             "我是人工智能", "语言模型", "我无法")

# prompt 污染特征：知识库纪要的元文本被整段灌进长期记忆。
_POLLUTION = ("# Summary", "来源:mcp", "（来源:", "【摘要】", "# 【情感陪伴】")

# 历史残留的称呼：出现且与当前名字不同 → 自称错。
_LEGACY_NAMES = ("小暖", "小智", "小助手")

_LATENCY_WARN_MS = 2200.0
_LATENCY_FAIL_MS = 4000.0
_MAX_LEN = 450          # 陪伴场景超过这个长度基本是「讲道理」了
_MIN_LEN = 3

_PUNCT = "".join("，。！？,.!? \n\t“”\"'（）()…、；:：-—")


def _plain(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch not in _PUNCT)


def _has_name(text: str) -> bool:
    return any(ch in text for ch in "你是你叫名叫")


def check_reply(*, reply: str, prev_reply: str = "", user_text: str = "",
                prev_user: str = "", latency_ms: float = 0.0,
                assistant_name: str = "", system_prompt: str = "",
                grew_from_error: bool = False) -> tuple[list[str], str]:
    """返回 (命中的规则名列表, verdict)。任何异常都被自己吞掉，不阻断对话。"""
    rules: list[str] = []
    try:
        if latency_ms >= _LATENCY_FAIL_MS:
            rules.append("latency_over_4s")
        elif latency_ms >= _LATENCY_WARN_MS:
            rules.append("latency_over_2s")

        body = (reply or "").strip()
        if not body:
            rules.append("empty_reply")
        elif len(body) < _MIN_LEN:
            rules.append("too_short")
        if len(body) > _MAX_LEN:
            rules.append("too_long")

        # 身份：名字必须出现在 system 最前面，否则等于人格里没有这个名字
        name = (assistant_name or "").strip()
        if name and name not in (system_prompt or ""):
            rules.append("identity_not_in_prompt")
        for legacy in _LEGACY_NAMES:
            if legacy in body and legacy != name:
                rules.append("wrong_self_name")
                break

        if _NARRATION_RE.search(body) or _INLINE_NARRATION_RE.search(body):
            rules.append("narration")
        for frag in _BREAKING:
            if frag in body:
                rules.append("breaking_character")
                break

        a, b = _plain(body), _plain(prev_reply)
        if len(a) >= 12 and len(b) >= 12 and a[:12] == b[:12]:
            rules.append("repeat_head")

        sp = system_prompt or ""
        for frag in _POLLUTION:
            if frag in sp:
                rules.append("prompt_pollution")
                break

        # 同一件事又说一遍 → 上一轮没答到点上（这是最可靠的负样本信号之一）
        u, pu = (user_text or "").strip(), (prev_user or "").strip()
        if len(u) >= 6 and u == pu:
            rules.append("user_repeated_self")

        if grew_from_error:
            rules.append("after_error")
    except Exception:
        pass

    if not rules:
        return [], "pass"
    if set(rules) <= {"latency_over_2s"}:
        return rules, "warn"
    return rules, "fail"


# 每条规则对应的「下次怎么改」：反刍不写空话，只写能改的一件具体事。
RULE_NOTES = {
    "repeat_head": "开头又和上一轮一样了：这轮直接说他这句话里的具体内容，别用同一个起手式。",
    "repeat_loop": "说到一半卡在同一句上：一句话只说一次，说完就往下走。",
    "narration": "又写了括号里的语气说明：把括号整段删掉，直接说那句话。",
    "breaking_character": "别自称 AI 或工具：只用名字直接说话。",
    "too_long": "一次给太多：压到三句以内，留一句给他接。",
    "too_short": "太短了：至少说一句能接下去的具体内容。",
    "wrong_self_name": "名字说错了：只用当前这个名字。",
    "latency_over_4s": "开口太慢：第一句先出最短的那半句，别等整段想完。",
    "latency_over_2s": "开口偏慢：第一句砍短。",
    "user_repeated_self": "他没得到答案又问了一遍：换个角度直接答，别再绕。",
    "empty_reply": "这一轮没说出话：至少先接一句。",
    "prompt_pollution": "记忆里混进了摘要元文本：别复述这些，只挑一句他的原话。",
    "after_error": "上一轮出错后接着答：先确认他现在等的是什么，再答。",
    "identity_not_in_prompt": "人格里缺名字：把名字放回最前面。",
}


def note_for(rules: list[str]) -> tuple[str, str]:
    """挑第一条能落成行动的规则，返回 (规则名, 具体改法)。

    延迟类规则排在后面：一句「开口太慢」说一百遍也改不了什么，
    优先记那些一改就能听见变化的（复读、旁白、太长、错名字）。
    """
    slow = [r for r in (rules or []) if str(r).startswith("latency")]
    fast = [r for r in (rules or []) if not str(r).startswith("latency")]
    for rule in list(fast) + list(slow):
        note = RULE_NOTES.get(str(rule))
        if note:
            return str(rule), note
    return "", ""


def find_repeat_loop(text: str, unit_min: int = 6, unit_max: int = 14,
                     times: int = 3) -> str:
    """找「同一句原地重复」：连续重复 3 次以上的 6~14 字片段，返回该片段。"""
    plain = _plain(text or "")
    n = len(plain)
    for size in range(unit_min, min(unit_max, n // times) + 1):
        for start in range(0, n - size * times + 1):
            unit = plain[start:start + size]
            if unit and unit * times in plain[start:start + size * times]:
                return unit
    return ""
