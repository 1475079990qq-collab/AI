"""记忆与知识系统：把「关于他和关于我」整理成带证据的主张。

这里的核心不是存，是**判断某条信息能不能当事实用**。
真实库里最脏的两类数据：
  1) 问句被当成事实（「我问的是我是谁？」）——它是当时的提问，不是关于他的陈述；
  2) 改名的动作被记成他的称呼（「称呼：改名叫小志或小智」）——那是在给我改名。

所以本模块只做三件事：
  取（snapshot 由适配层注入）→ 洗（滤掉问句/噪声）→ 定级（给每条标注确定性）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .contract import Certainty, Claim, Evidence

# 只认这类明确句式，才把自由文本里的名字当「他叫什么」。
# 「称呼：改名叫小智」故意不匹配：那是给我改名，不是他的名字。
_NAME_PATTERNS = (
    re.compile(r"我(?:叫|名叫|是)\s*[\"'“”‘’]?(\w{1,12})"),
    re.compile(r"(?:他|对方|用户)(?:叫|名叫)\s*[\"'“”‘’]?(\w{1,12})"),
)

# 明显不是名字的尾缀（避免「我是做运维的」抽成名字「做运维的」）。
_NOT_NAME = ("的", "吗", "呢", "了", "人", "员", "工", "师", "吧", "啊")


@dataclass(frozen=True)
class WorldSnapshot:
    """一轮开始时关于「他和这个世界」的只读快照。

    由适配层（adapters.py）从真实库取，内核只消费不取数：
    换库、换模型、换存储都不改内核，只换适配层。
    """

    assistant_name: str = ""
    user_name: str = ""
    profile_text: str = ""
    facts: tuple[dict[str, Any], ...] = ()
    has_history: bool = False
    today_label: str = ""
    source: str = "injected"


def _is_question_like(text: str) -> bool:
    """问句不参与事实判定。这是「问句仓库」的止血点。"""
    s = str(text or "").strip()
    if s.endswith(("？", "?", "吗", "呢")):
        return True
    return any(k in s for k in ("我问的是", "我在问", "问我", "你还记得", "知道我是谁"))


def _extract_name(profile_text: str) -> tuple[str, str]:
    """从画像自由文本里抽「他的名字」。返回 (名字, 命中的原句)。

    抽不到就返回空：宁可不知道，也不许把别的东西当名字。
    """
    for line in str(profile_text or "").splitlines():
        for pat in _NAME_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            cand = m.group(1).strip()
            if not cand or any(cand.endswith(x) for x in _NOT_NAME):
                continue
            if len(cand) > 8:
                continue
            return cand, line.strip()
    return "", ""


def clean_facts(facts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """洗事实：去掉问句与过短碎句，保留陈述。顺序保持原样（已是相关度排序）。"""
    out: list[dict[str, Any]] = []
    for f in facts or []:
        text = str((f or {}).get("text") or "").strip()
        if len(text) < 6:
            continue
        if _is_question_like(text):
            continue
        item = dict(f)
        item["text"] = text
        out.append(item)
    return out


def identity_claims(snap: WorldSnapshot) -> tuple[list[Claim], list[Evidence]]:
    """产出身份类主张：我是谁、他叫什么、他是什么角色。

    关键纪律：`user_name` 为空时，结论是 UNKNOWN 且 value=None，
    而不是「不知道」这种模糊说法，更不许拿 assistant_name 顶上。
    """
    claims: list[Claim] = []
    ev: list[Evidence] = []

    # --- 我自己叫什么 ---
    asst = str(snap.assistant_name or "").strip()
    if asst:
        e = Evidence("identity", "assistant_name", asst, trust=0.95)
        ev.append(e)
        claims.append(Claim("assistant", "name", asst, Certainty.CERTAIN, (e,)))
    else:
        claims.append(Claim("assistant", "name", None, Certainty.UNKNOWN, ()))

    # --- 他叫什么：三档优先，逐级降信 ---
    user = str(snap.user_name or "").strip()
    e_user = None
    if user:
        e_user = Evidence("identity", "user_name", user, trust=0.95)
        claims.append(Claim("user", "name", user, Certainty.CERTAIN, (e_user,)))
    else:
        prof_name, prof_line = _extract_name(snap.profile_text)
        if prof_name:
            e_user = Evidence("profile", "profile:name", prof_line, trust=0.7)
            claims.append(Claim("user", "name", prof_name, Certainty.INFERRED, (e_user,)))
        else:
            # 明确未知：value=None，证据指向「查过哪些地方都没有」
            claims.append(Claim("user", "name", None, Certainty.UNKNOWN, (
                Evidence("identity", "user_name", "", trust=0.95),
                Evidence("profile", "profile:scan", "画像里没有『我叫 X / 他叫 X』这类明确句式", trust=0.7),
            )))
    if e_user:
        ev.append(e_user)

    # --- 他是什么角色：这是唯一能在姓名未知时确定回答的东西 ---
    e_role = Evidence("rule", "speaker_of_turn", "本条消息的发送者就是当前对话用户", trust=0.95)
    ev.append(e_role)
    claims.append(Claim("user", "role", "当前对话用户", Certainty.CERTAIN, (e_role,)))

    return claims, ev


def find_claims(snap: WorldSnapshot, query: str, *, limit: int = 3) -> tuple[list[Claim], list[Evidence]]:
    """按字面重合从洗过的事实里找可直接引用的陈述。

    刻意用双字重合而不是向量：先让形状对，再谈语义召回。
    """
    facts = clean_facts(snap.facts)[:limit]
    gram_q = _bigrams(query)
    claims: list[Claim] = []
    ev: list[Evidence] = []
    for f in facts:
        text = f["text"]
        overlap = len(gram_q & _bigrams(text)) / max(1, len(gram_q | _bigrams(text)))
        if overlap <= 0:
            continue
        e = Evidence("fact", str(f.get("id") or "fact"), text, trust=0.75)
        ev.append(e)
        claims.append(Claim("user", "stated", text, Certainty.SUPPORTED, (e,)))
    return claims, ev


def _bigrams(text: str) -> set[str]:
    s = "".join(ch for ch in str(text or "").lower() if not ch.isspace())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


@dataclass
class RecallResult:
    """检索结果：能用的陈述 + 被挡下的问句数（挡了多少也要有账）。"""

    claims: list[Claim] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    dropped_questions: int = 0
    total: int = 0
