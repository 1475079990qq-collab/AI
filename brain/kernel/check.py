"""表达忠实度校验：模型说的这句，到底是不是程序让它说的那句。

只校验**可机械验证**的部分，不做主观打分：
  1) 必含项是否都在（人名、关键结论）；
  2) 禁含项是否都没出现（尤其是「拿自己的名字顶用户」这种串号）；
  3) 有没有冒出证据之外的名字（编造）；
  4) 长度是否超预算；
  5) 有没有"作为AI/根据我的分析"这类元话。

验证不了的（语气像不像人、有没有共情）不在这层装懂——
那交给模板优先策略和后续的反馈回路，而不是假装能打分。
"""
from __future__ import annotations

import re

from .contract import CheckResult, ContentPlan

_META = (
    "作为ai", "作为人工智能", "作为语言模型", "我是语言模型", "我是ai助手",
    "根据我的分析", "根据以上分析", "综上所述", "总结如下",
)

_NAME_CALL = re.compile(r"(?:叫|名叫|名字是|名字叫)\s*[「\"'“”‘’]?([\u4e00-\u9fa5A-Za-z0-9]{1,8})")

# 「不知道你叫什么」里的「什么」不是名字。疑问代词/人称代词一律不算实体。
_NOT_A_NAME = {
    "什么", "啥", "谁", "哪", "怎么", "多少", "怎样", "如何",
    "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "这样", "那样", "这个", "那个",
}

_MAX_SLACK = 1.15  # 字数允许 15% 浮动，再超就是没按预算说


def _allowed_entities(plan: ContentPlan) -> set[str]:
    """允许在回答里出现的实体：计划里已确认、且没被禁用的值 + 必含项。

    注意：claim.value 里如果本身含「叫 X」，X 也要算允许——
    否则引用旧事「他叫阿伟」会被误判成编造。
    """
    known: set[str] = set()
    for c in plan.claims:
        if c.known and c.value:
            val = str(c.value)
            known.add(val)
            for m in _NAME_CALL.finditer(val):
                known.add(m.group(1))
    banned = set(plan.must_not)
    return (known - banned) | set(plan.must_include)


def check(plan: ContentPlan, text: str) -> CheckResult:
    """校验一次表达。ok=False 时必须回退，不许放行。"""
    body = str(text or "").strip()
    failures: list[str] = []

    if not body:
        return CheckResult(False, ("empty",))

    # 1) 必含项
    for need in plan.must_include:
        if need and need not in body:
            failures.append(f"missing:{need}")

    # 2) 禁含项（串号、编造、元话）
    for bad in plan.must_not:
        if bad and bad in body:
            failures.append(f"forbidden:{bad}")

    # 3) 编造名字：出现了「叫 X」，但 X 不在允许集合里
    allowed = _allowed_entities(plan)
    for m in _NAME_CALL.finditer(body):
        name = m.group(1)
        if name in _NOT_A_NAME:
            continue
        if name and name not in allowed and name not in body[: m.start()]:
            failures.append(f"invented_name:{name}")

    # 4) 字数预算
    limit = int(plan.max_chars * _MAX_SLACK)
    if plan.max_chars and len(body) > limit:
        failures.append(f"too_long:{len(body)}>{limit}")

    # 5) 元话（客服腔/说明书腔）
    low = body.lower()
    for phrase in _META:
        if phrase in low:
            failures.append(f"meta:{phrase}")

    # 6) 确定性保真：整轮为 UNKNOWN 时，不许出现"我记得/我确定"这类断言
    if plan.uncertainty.value in ("unknown", "conflict"):
        for word in ("我确定", "肯定就是", "我记得很清楚"):
            if word in body:
                failures.append(f"overclaim:{word}")

    return CheckResult(bool(not failures), tuple(failures), fallback=False)
