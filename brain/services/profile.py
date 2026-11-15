"""用户画像：把零散记忆压成一页「他是什么人」，每轮注入 prompt。

分工要说清楚：
- 知识库存原始记忆（他说过的话、发生过的事），可追溯、可人工修正；
- 这里存派生索引（画像），便宜、稳定，适合每轮都塞进 system。
画像只是压缩，不新增事实；原文没了就重建，随时可丢。
"""
from __future__ import annotations

import asyncio
import re

from brain import db
from brain.services import llm

# 每 N 轮重建一次画像：太频繁烧 token，太稀疏跟不上近况。
REFRESH_EVERY = 8

_EXTRACT_PROMPT = (
    "你在维护一份关于「对方」的长期画像，供陪伴者参考。\n"
    "输入是他自己说过的原话，里面混着提问和随口一说，你要分辨哪些是事实。\n"
    "【铁律】\n"
    "- 提问不算事实。「最近投电力装备怎么样？」只说明他问过这个话题，"
    "绝对不能写成「雷区：忌提电力装备」，也不能写成「偏好：喜欢电力装备」。\n"
    "- 「雷区」只能是他明确表示反感、明确要求你别问别做的事；没说过就整条跳过。\n"
    "- 「偏好」只能是他明确说过喜欢的东西；问过一次的话题不算偏好。\n"
    "- 「称呼」只写他明确的自称或被称呼，没说过就跳过，不许猜。\n"
    "- 不确定就留空，绝不推测、绝不编造；宁可少写，不可写错。\n"
    "按这几类输出，每条一行，没有的类别就跳过：\n"
    "- 称呼：他怎么被称呼、怎么自称\n"
    "- 生活：工作、作息、家庭、身体等长期情况\n"
    "- 偏好：他明确说过喜欢的东西或说话方式\n"
    "- 雷区：他明确不想被提起、或让他反感的事\n"
    "- 近况：最近反复出现的情绪或事情\n"
    "每条不超过 30 字，最多 12 条。只输出条目本身，不要解释。"
)


def get(user_id: str) -> str:
    try:
        return db.get_profile(user_id).get("text", "")
    except Exception:
        return ""


# ---- 画像清洗 ------------------------------------------------------------
# 小模型（MiniCPM5-2B）做自由提炼时会把「他问过电力装备」写成「雷区：忌提电力装备」，
# 还会编出「称呼：情绪 care / 情绪 happy」这种把上下文标记词当事实的条目。
# 一旦这种错条目进了画像，每轮都注入 system，模型就开始围着错误前提打转（复读+拒答）。
# 所以落库前一律过三道闸：
#   1) 只认五类前缀，去掉其他杂行；
#   2) 条目必须在原话里能找到至少 3 个连续字（可追溯），否则视为编造；
#   3) 「雷区」必须由原话里的明确否定词支撑，问过 ≠ 忌讳。
_ALLOWED_CATS = ("称呼", "生活", "偏好", "雷区", "近况")
_MAX_ITEMS = {"称呼": 1, "生活": 4, "偏好": 4, "雷区": 3, "近况": 3}
_NEGATION = ("别", "不要", "不想", "反感", "讨厌", "不喜欢", "受不了", "忌讳")
_EMO_LEAK = ("care", "happy", "sad", "angry", "fear", "neutral", "surprise",
             "disgust", "情绪", "label", "intensity", "confidence")


def _clean(val: str) -> str:
    return re.sub(r"[\s，。；、：:,.!?！？“”\"'（）()~～…\-—]", "", val)


def _traceable(val: str, memories: list[str]) -> bool:
    """条目里的字必须能在原话里找到，挡掉模型编出来的内容。"""
    clean = _clean(val)
    if len(clean) < 2:
        return False
    corpus = "".join(memories)
    if clean in corpus:
        return True
    for i in range(len(clean) - 2):
        if clean[i:i + 3] in corpus:
            return True
    return False


def _sanitize(raw: str, memories: list[str]) -> list[tuple[str, str]]:
    items: dict[str, list[str]] = {}
    for line in (raw or "").splitlines():
        m = re.match(r"\s*[-*•·]?\s*(称呼|生活|偏好|雷区|近况)\s*[:：]\s*(.+)", line)
        if not m:
            continue
        cat, val = m.group(1), m.group(2).strip().strip("。；; ")
        if not val or len(val) > 40:
            continue
        if any(tok.lower() in val.lower() for tok in _EMO_LEAK):
            continue
        if not _traceable(val, memories):
            continue
        if cat == "雷区" and not any(w in val for w in _NEGATION):
            continue
        bucket = items.setdefault(cat, [])
        if val in bucket or len(bucket) >= _MAX_ITEMS[cat]:
            continue
        bucket.append(val)
    return [(c, v) for c in _ALLOWED_CATS for v in items.get(c, [])]


# 兜底：模型输出通不过校验时，直接从原话里按句式抽，慢一点但绝不会编。
_RULE_PATTERNS = (
    ("称呼", r"(?:叫我|我叫|喊我|别人都叫我)([^，。！？,.!?]{1,8})"),
    ("偏好", r"(?:我喜欢|我最喜欢|我爱|我爱好|我常)([^，。！？,.!?]{1,20})"),
    ("雷区", r"(?:别跟我|别和我|不要跟我|不要和我|不想听|反感|讨厌|不喜欢|受不了)"
             r"([^，。！？,.!?]{1,20})"),
)


def _rule_extract(memories: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for cat, pat in _RULE_PATTERNS:
        for text in memories:
            for m in re.finditer(pat, text):
                val = m.group(1).strip()
                if 1 <= len(val) <= 20 and val not in [v for _, v in out]:
                    out.append((cat, val))
                    break
    return out[:10]


def maybe_refresh(user_id: str) -> None:
    """在后台线程里调用：计数到阈值才真正重建，异常一律吞掉。"""
    try:
        turns = db.bump_profile_turns(user_id)
    except Exception:
        return
    if turns % REFRESH_EVERY != 0:
        return
    refresh(user_id)


def refresh(user_id: str) -> None:
    try:
        memories = db.list_recent_facts(user_id, limit=60)
    except Exception:
        return
    if not memories:
        return
    lines = ["以下是对方说过的原话（含提问，提问不代表偏好或雷区）："]
    lines += [f"- {t}" for t in memories[:60]]
    try:
        for ev in db.recent_emotions(user_id, min_intensity=0.5, limit=8):
            label = str(ev.get("emotion_label") or ev.get("label") or "").strip()
            summary = str(ev.get("summary") or "").strip()
            if label:
                lines.append(f"- 情绪 {label}：{summary}" if summary else f"- 情绪 {label}")
    except Exception:
        pass

    messages = [
        {"role": "system", "content": _EXTRACT_PROMPT},
        {"role": "user", "content": "\n".join(lines)[:6000]},
    ]
    try:
        raw = asyncio.run(_collect(messages)).strip()
    except Exception:
        raw = ""
    # 先信模型（但要过闸），通不过就从原话里规则抽；都抽不到就保留旧画像，
    # 绝不把「没把握的东西」写进每轮都注入的 system。
    items = _sanitize(raw, memories) or _rule_extract(memories)
    if not items:
        return
    try:
        db.set_profile(user_id, "\n".join(f"{c}：{v}" for c, v in items))
    except Exception:
        pass


async def _collect(messages: list[dict]) -> str:
    out: list[str] = []
    async for chunk in llm.stream(messages, {"max_tokens": 400, "temperature": 0.3}):
        out.append(str(chunk))
    return "".join(out)
