"""策略：从「一条教训」长成「一条可验证、可回滚的策略」。

层级关系：
    教训（lesson）     一次复盘的一句话：这轮哪里没做好
    策略（strategy）   同一条教训反复出现后抽出来的行为规则：以后遇到同类就这么做

策略不是人格，不带情绪色彩，也不写死在提示词里：
- 候选（candidate）：只是一条观察，证据不够，不进行为
- 启用（active）：证据 >= 2 且没被反馈打脸，进提示词，每次只用一条
- 退役（retired）：失败多过成功。原文保留在库里，随时能回滚重启用

这样「自我进化」就是可审计的：它不会偷偷改掉自己是谁，只会调整「这类事怎么办」。
"""
from __future__ import annotations

import logging

from brain import db

log = logging.getLogger(__name__)

# 证据门槛：同类教训至少出现两次才算「规律」，一次只是意外。
_MIN_EVIDENCE = 2


def candidate_from_lesson(*, user_id: str, lesson: dict) -> dict | None:
    """把一条被验证过的教训登记成策略候选（同名累积证据，不重复建）。"""
    if not user_id or not lesson:
        return None
    kind = str(lesson.get("kind") or "").strip() or "general"
    how = str(lesson.get("how") or "").strip() or str(lesson.get("note") or "").strip()
    if not how:
        return None
    name = f"lesson_{kind}"
    text = how[:160]
    try:
        row = db.upsert_strategy(user_id=user_id, name=name, text=text)
    except Exception:
        log.debug("登记策略候选失败", exc_info=True)
        return None
    # 证据够了、又没有明显失败记录，才自动启用（阈值写在这里，不散在各处）。
    if int(row.get("evidence") or 0) >= _MIN_EVIDENCE:
        try:
            return db.activate_strategy(user_id=user_id, name=name,
                                        min_evidence=_MIN_EVIDENCE) or row
        except Exception:
            log.debug("启用策略失败", exc_info=True)
    return row


def promote_validated(user_id: str) -> dict | None:
    """给还没启用的候选一次「凭证据上位」的机会；失败多于成功的不动。"""
    try:
        rows = db.list_strategies(user_id, limit=20)
    except Exception:
        return None
    for row in rows:
        if str(row.get("status") or "") != "candidate":
            continue
        if int(row.get("evidence") or 0) >= _MIN_EVIDENCE and \
                int(row.get("success") or 0) >= int(row.get("failure") or 0):
            try:
                return db.activate_strategy(user_id=user_id, name=str(row["name"]),
                                            min_evidence=_MIN_EVIDENCE)
            except Exception:
                continue
    return None


def active_line(user_id: str) -> str:
    """进提示词的唯一一条策略（没有就返回空串）。

    只放一条：策略堆起来就是又一套固定话术，和这次要解决的根本问题是同一个。
    """
    try:
        row = db.active_strategy(user_id)
    except Exception:
        return ""
    if not row:
        return ""
    text = str(row.get("text") or "").strip()
    if not text:
        return ""
    return text[:110]


def rollback(user_id: str, name: str) -> dict | None:
    """人工/自动回滚入口：留着的旧版本重新顶上来。"""
    try:
        return db.rollback_strategy(user_id=user_id, name=name)
    except Exception:
        log.debug("回滚策略失败", exc_info=True)
        return None
