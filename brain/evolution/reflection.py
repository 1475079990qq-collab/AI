"""反思打分（L1 · 规则最小，不调模型）。

把「反馈信号 + 本轮上下文」折算成一条 lesson 更新意图，供 learner / evaluation 用。

与 brain/services/reflect.py 的区别：
- services/reflect 是 L2，后台线程里问本地小模型，产出两段人话；
- 本模块是 L1 的确定性兜底：只读反馈与上下文，按规则算权重，不联网、不调模型，
  不 import services/routers，只依赖标准库。模型不可用或上层来不及跑模型时顶上。

产出 {kind, note, how, weight, verdict}：
- weight 是「教训权重」：负向反馈 / 明显出错 → 调高（这件事要记住）；
  正向 → 调低或中性（已经做得好，不必强化）。
- 方向与 evaluation 用的 score（策略分）相反：策略分负向要降（让差策略退役），
  教训权重负向要升（让错的教训被记住）。两者各有用途，不互相覆盖。
"""
from __future__ import annotations

import logging

log = logging.getLogger("brain.evolution.reflection")

# 默认教训权重
_BASE_WEIGHT = 0.5
# 反馈信号对教训权重的影响
_SIGNAL_DELTA = {"positive": -0.1, "negative": 0.2, "neutral": 0.0}
# 自检判失败 / 明显出错的额外加权
_FAIL_DELTA = 0.2
_ERROR_DELTA = 0.15

_FAIL_WORDS = {"fail", "failure", "failed", "error", "reject", "rejected",
               "bad", "wrong", "false", "0"}
_PASS_WORDS = {"pass", "passed", "ok", "success", "good", "true", "1"}
_SEVERITY_WORDS = {"high": 1.0, "medium": 0.6, "mid": 0.6, "low": 0.3}

# rule kind → 一句可落地的「下次怎么改」（规则最小，不调模型）
_HOW_BY_KIND = {
    "repeat_head": "开头别和上一轮几乎一字不差，换个切入角度。",
    "repeat_loop": "同一句话只说一次，别在回复里反复绕。",
    "narration": "去掉括号里的语气说明，用随口说的话表达。",
    "breaking_character": "把自称拉回角色本身，别说成工具或 AI。",
    "wrong_self_name": "说话前确认当下用的名字，别叫错。",
    "user_repeated_self": "他重复问说明没答到点上，先正面回答再补一句确认。",
    "too_short": "这一轮多给一点内容，别一句话就收住。",
    "too_long": "把回复收短，只留能对上他这句的部分。",
    "empty_reply": "这一轮要给出实际内容，别空着。",
    "after_error": "出错后先确认他现在等什么，再接着答。",
    "date_mismatch": "没把握的日期星期别说成事实。",
}
_DEFAULT_HOW = "下次遇到同类情况，先确认对方真正要的，再给出具体、可落地的一句。"


def _norm_signal(signal) -> str:
    """把各种写法的反馈归到 positive / negative / neutral。"""
    s = str(signal or "").strip().lower()
    if s in {"positive", "pos", "good", "like", "up", "赞", "好评", "满意"}:
        return "positive"
    if s in {"negative", "neg", "bad", "dislike", "down", "踩", "差评", "不满意"}:
        return "negative"
    return "neutral"


def _verdict_problem(verdict):
    """None=没给 verdict；True=判失败；False=判通过。"""
    if verdict is None:
        return None
    if isinstance(verdict, bool):
        return not verdict
    s = str(verdict).strip().lower()
    if s in _FAIL_WORDS:
        return True
    if s in _PASS_WORDS:
        return False
    return None


def _severity(context: dict) -> float:
    """从上下文取 0..1 的错误严重度；取不到按「有没有明显出错」给 0 / 1。"""
    raw = context.get("severity")
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            key = str(raw).strip().lower()
            if key in _SEVERITY_WORDS:
                return _SEVERITY_WORDS[key]
    for key in ("obvious_error", "has_error", "error", "mistake"):
        if context.get(key):
            return 1.0
    return 0.0


def score(previous: float, *, signal: str, repeated: bool = False) -> float:
    """显式反馈驱动「策略分」；隐式（重复追问）只记录不计分（块⑥风险）。"""
    try:
        s = float(previous)
    except (TypeError, ValueError):
        s = 0.5
    norm = _norm_signal(signal)
    if norm == "positive":
        s += 0.1
    elif norm == "negative":
        s -= 0.1
    # repeated 为隐式信号：不改变分数，仅体现在调用方记录里
    return max(0.0, min(1.0, s))


def reflect(*, signal: str = "neutral", context: dict | None = None) -> dict:
    """对本轮做反思打分，产出一条 lesson 更新意图。

    signal: positive / negative / neutral（也接受常见同义词）。
    context: 可选，可含 verdict、rules、rule_kind/kind、severity、error/obvious_error、
             note、how、repeated 等。任何异常都降级到安全默认，不抛。
    """
    sig = _norm_signal(signal)
    ctx = context if isinstance(context, dict) else {}
    result: dict = {
        "kind": "general",
        "note": "",
        "how": "",
        "weight": _BASE_WEIGHT,
        "verdict": "neutral",
        "signal": sig,
        "repeated": False,
        "severity": 0.0,
    }
    try:
        # 规则种类（rule_kind / kind / rules[0]）
        kind = ctx.get("rule_kind") or ctx.get("kind") or ctx.get("rules")
        if isinstance(kind, (list, tuple)):
            kind = kind[0] if kind else ""
        kind = str(kind or "").strip() or "general"
        result["kind"] = kind

        # 隐式信号：重复追问只记录，不改分（与 score 一致）
        try:
            result["repeated"] = bool(ctx.get("repeated"))
        except Exception:
            result["repeated"] = False

        # 错误严重度
        sev = _severity(ctx)
        result["severity"] = sev

        # 自检结论
        problem = _verdict_problem(ctx.get("verdict"))

        # 教训权重：反馈 + 错误严重度（负向 + 明显出错 → 高）
        w = _BASE_WEIGHT + _SIGNAL_DELTA.get(sig, 0.0)
        if problem is True:
            w += _FAIL_DELTA
        if sev > 0:
            w += _ERROR_DELTA * sev
        result["weight"] = round(max(0.0, min(1.0, w)), 4)

        # 反思结论：负向 / 判失败 / 明显出错 → 反映问题
        if sig == "negative" or problem is True or sev >= 0.6:
            result["verdict"] = "needs_improvement"
        elif sig == "positive" and problem is not True and sev == 0:
            result["verdict"] = "ok"
        else:
            result["verdict"] = "neutral"

        # note / how：优先用上下文给的，否则按规则最小生成
        note = str(ctx.get("note") or "").strip()
        how = str(ctx.get("how") or "").strip()
        if not how:
            how = _HOW_BY_KIND.get(kind, _DEFAULT_HOW)
        if not note:
            if result["verdict"] == "needs_improvement":
                note = f"本轮反馈{sig}、结论{result['verdict']}，需处理{kind}类问题。"
            elif result["verdict"] == "ok":
                note = "本轮反馈良好，无需强化教训。"
            else:
                note = "本轮无明确反馈，按中性处理。"
        result["note"] = note[:160]
        result["how"] = how[:160]
    except Exception:
        log.debug("反思打分降级为安全默认", exc_info=True)
        # result 已是安全默认，直接返回
    return result
