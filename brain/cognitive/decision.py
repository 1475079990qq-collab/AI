"""规则骨架 + 模型填充的骨架侧（D06）。输出枚举化结构，便于 verifier 校验。

设计约束（技术规格 §1.2 / §6 / 实施计划 T20）：
- L1 纯认知模块：不 import services / routers，只依赖 workspace 的 dict 结构，不做任何 IO。
- 规则产出选项与选择；模型只改 summary/cost 的措辞，越界由 T21 verifier 兜底回退。
- 结构字段写死、顺序稳定；无候选时 options=[]、chosen_id=None（§1.2 失败语义）。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("brain.decision")

# 骨架字段固定顺序（写死，供 verifier 稳定校验）；模型不许改这些字段。
_FIELDS = ("trace_id", "options", "chosen_id", "chosen_by", "verified", "skip")


def _assemble(trace_id: str, options: list[dict[str, Any]], chosen_id: str | None,
              *, skip: bool) -> dict:
    """按固定字段顺序组装骨架，保证字段顺序稳定。"""
    return {
        "trace_id": trace_id,
        "options": options,
        "chosen_id": chosen_id,
        "chosen_by": "rule",
        "verified": True,
        "skip": skip,
    }


def build_skeleton(workspace, *, trace_id: str) -> dict:
    """基于工作区生成候选与选择。模型无关，换模型不动这里。"""
    # 安全取 workspace 的 dict 字段（缺字段 / None 安全）
    goals = list(getattr(workspace, "goals", None) or [])
    signals = dict(getattr(workspace, "signals", None) or {})
    appraisal = dict(getattr(workspace, "appraisal", None) or {})
    motivation = dict(getattr(workspace, "motivation", None) or {})
    recalls = list(getattr(workspace, "recalls", None) or [])
    channel = getattr(workspace, "channel", "standard") or "standard"

    # 快速通道不产出决策骨架（只写最小 trace）
    if channel == "fast":
        return _assemble(trace_id, [], None, skip=True)

    # 规则基于 workspace 生成结构化候选（每项含 id/summary/cost/source，source=rule）
    options: list[dict[str, Any]] = []
    if goals:
        g = goals[0]
        title = str(g.get("title", ""))[:20] if isinstance(g, dict) else str(g)[:20]
        options.append({"id": "o1", "summary": f"推进目标：{title}",
                        "cost": "需要多轮跟进", "source": "rule"})
        options.append({"id": "o2", "summary": "先接住当下情绪，不急着推进",
                        "cost": "目标进展慢", "source": "rule"})
    elif signals or appraisal or motivation or recalls:
        # 有信号但无明确目标：给出单一自然回应候选
        options.append({"id": "o1", "summary": "顺着对方的话自然回应",
                        "cost": "无明确推进", "source": "rule"})
    # 完全无候选 → options=[]、chosen_id=None（§1.2 失败语义）

    if not options:
        return _assemble(trace_id, [], None, skip=False)

    # chosen_id 由规则选定（基于 priority / appraisal）
    chosen = options[0]["id"]
    try:
        v = appraisal.get("v")
        if (len(options) > 1 and str(motivation.get("priority", "") or "") == "high"
                and isinstance(v, (int, float)) and v < 0):
            # 负向且高优先：先接住情绪，不急着推进目标
            chosen = options[1]["id"]
    except Exception as exc:  # 防御：任何异常回退首个选项，绝不冒泡
        log.debug("decision 选优降级用首选项: %s", type(exc).__name__)
        chosen = options[0]["id"]
    if chosen not in {o["id"] for o in options}:
        chosen = options[0]["id"]

    return _assemble(trace_id, options, chosen, skip=False)


def fill_with_model(skeleton: dict, *, model_fn, timeout_s: float = 0.8) -> dict:
    """模型只改 summary/cost 措辞；不合法由 verifier 兜底（技术规格 §6.2）。"""
    if skeleton.get("skip"):
        return skeleton
    try:
        filled = model_fn(skeleton)
        if not isinstance(filled, dict):
            return skeleton
        out = dict(skeleton)
        opts = []
        for i, opt in enumerate(skeleton["options"]):
            fopt = (filled.get("options") or [])
            f = fopt[i] if i < len(fopt) and isinstance(fopt[i], dict) else {}
            o = dict(opt)
            if isinstance(f.get("summary"), str) and f["summary"].strip():
                o["summary"] = f["summary"].strip()[:80]
            if isinstance(f.get("cost"), str) and f["cost"].strip():
                o["cost"] = f["cost"].strip()[:60]
            opts.append(o)
        out["options"] = opts
        out["chosen_by"] = "model"
        return out
    except Exception as exc:
        log.debug("模型填充失败，用骨架: %s", type(exc).__name__)
        return skeleton
