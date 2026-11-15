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
