"""说完话之后的「真反思」：拿这一轮的真实材料问本地小模型，复盘两段人话。

和旧的规则反刍的区别：
- 旧路径（checks.py）只判机械失败（复读、旁白、太长……），给一句固定改法；
- 这里把「他原话 / 我回的话 / 自检命中 / 这轮想起的事实和情绪 / 工具结果」
  一起喂给本地模型，让它像人一样回头想：刚才为什么那么说、下次具体怎么改。

纪律（红线）：
- 只在后台线程跑，绝不进 SSE 热路径；同步链路新增开销≈0。
- 模型调用硬超时（默认 8s，可配）；不可达 / 超时 / 输出不合规 → 降级回规则级
  note，记降级原因；绝不抛错、绝不写脏数据。
- 不引向量、不引 embedding；全部本机 HTTP，无云端。
- 复盘必须是两段：为什么（指到具体触发点，不复述规则名）+ 下次（可落地的句式 /
  长度 / 是否先反问一句）。
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import httpx

from brain.services import checks

log = logging.getLogger("brain.reflect")

# ---- 预算：喂给模型的材料与落库的话都有上限，别把上下文和 prompt 撑爆 ----
_CTX_LIMITS = {
    "user_text": 200, "reply": 400, "prev_reply": 200, "tool_summary": 120,
}
_FACT_MAX = 5
_FACT_LEN = 40
_WHY_MAX = 120
_HOW_MAX = 120
_MODEL_MAX_TOKENS = 300

# 模型被要求不许复述规则名；输出里出现这些英文标签就算不合规，走降级。
_RULE_LABELS = {
    "repeat_head", "repeat_loop", "narration", "breaking_character", "too_long",
    "too_short", "wrong_self_name", "latency_over_4s", "latency_over_2s",
    "user_repeated_self", "empty_reply", "prompt_pollution", "after_error",
    "identity_not_in_prompt",
}

# 规则 → 人话的「为什么」兜底（模型不可用时用，指到具体问题，不写规则名）。
_RULE_WHY = {    "repeat_head": "回复开头和上一轮几乎一字不差，等于把同一句话又说了一遍。",
    "repeat_loop": "同一句在回复里原地重复了好几次，听着像卡住了。",
    "narration": "回复里夹了括号里的语气说明，不像随口说出来的话。",
    "breaking_character": "话里把自己说成了工具或 AI，脱离了这个角色。",
    "wrong_self_name": "嘴上用的名字和当前这个角色对不上。",
    "user_repeated_self": "他没得到答案又问了一遍，说明上一轮没答到点上。",
    "too_short": "这一轮说得太短，接不住他那句话。",
    "empty_reply": "这一轮实际上没说出什么内容。",
    "prompt_pollution": "记忆里混进了摘要类的元文本，被顺带复述了出来。",
    "identity_not_in_prompt": "当前名字没进人格，嘴上容易说错称呼。",
    "after_error": "上一轮出错之后接着答，没先确认他现在等的是什么。",
}

# 规则 → 中文提示（只给模型对照用；英文标签不进模型输入，避免它原样抄进复盘）。
_RULE_CN = {
    "repeat_head": "开头和上一轮雷同",
    "repeat_loop": "同一句在回复里原地重复",
    "narration": "回复里夹了括号语气说明",
    "breaking_character": "话里把自己说成了工具或 AI",
    "too_long": "回复篇幅太长",
    "too_short": "回复太短、接不住话",
    "wrong_self_name": "嘴上用的名字不对",
    "latency_over_4s": "开口偏慢",
    "latency_over_2s": "开口偏慢",
    "user_repeated_self": "他没得到答案又问了一遍",
    "empty_reply": "这一轮没说出内容",
    "prompt_pollution": "复述了记忆里的摘要元文本",
    "identity_not_in_prompt": "当前名字没进人格",
    "after_error": "出错之后接着答",
}

# 通过轮次抽样计数：user_id → 已轮过的 pass 轮次。进程内有界，不写库。
_pass_counter: dict[str, int] = {}
_COUNTER_MAX = 512

_SYSTEM_PROMPT = (
    "你就是这个陪伴角色本人，刚说完一轮话，现在回头复盘自己刚才那句回答。\n"
    "你不是在写检查报告，不要复述任何规则名称或英文标签，也不要只贴现象标签。\n"
    "只根据下面这一轮的真实材料，说清两件事：\n"
    "1. 为什么刚才那么说：回看自己上一句原话，说清是哪一句口吻不对、哪段太长、"
    "哪条记得的事被误用了，还是哪个点没接住他、他根本没得到答案；\n"
    "2. 下次怎么改：具体到句式、长短、要不要先反问一句，是一句能落地的话。\n"
    "输出格式严格如下，不要加标题、不要列表、不要多余解释：\n"
    "为什么：<一两句话，说人话，指到具体那句话>\n"
    "下次：<一句话，具体可执行>"
)


def _clip(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _runtime_flag(runtime: dict, key: str, default: bool) -> bool:
    try:
        raw = (runtime or {}).get(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return default


def _runtime_int(runtime: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(float((runtime or {}).get(key, default)))
        return max(lo, min(hi, v))
    except (TypeError, ValueError):
        return default


def _resolve_endpoint(runtime: dict) -> tuple[str, str, float]:
    """复盘用的本地模型地址 / 型号 / 超时。

    优先级：运行时参数 > 环境变量 > 对话大模型卡配置 > 代码兜底。
    任何读取异常都回退兜底，绝不因为配置读失败把后台复盘打挂。
    """
    runtime = runtime or {}
    timeout_s = float(_runtime_int(runtime, "reflect_timeout_s", 8, 1, 15))
    base = str(runtime.get("reflect_base_url") or "").strip().rstrip("/")
    model = str(runtime.get("reflect_model") or "").strip()
    if not base:
        base = os.environ.get("REFLECT_BASE_URL", "").strip().rstrip("/")
    if not model:
        model = os.environ.get("REFLECT_MODEL", "").strip()
    try:
        from brain.services import providers

        llm = providers.get("llm") or {}
        cfg = llm.get("config") if isinstance(llm.get("config"), dict) else llm
        cfg = cfg or {}
        if not base:
            base = str(cfg.get("base_url") or "").strip().rstrip("/")
        if not model:
            model = str(cfg.get("model") or "").strip()
    except Exception:
        pass
    if not base:
        base = "http://127.0.0.1:8082"
    if not model:
        model = "MiniCPM5-2B"
    return base, model, timeout_s


def should_reflect(runtime: dict, verdict: str, rules: list[str], user_id: str) -> bool:
    """这一轮要不要跑模型复盘。

    - 开关关 → 不跑（仍走旧的规则级记录，行为不变）；
    - fail / warn 必跑；
    - pass 按 N 轮抽样一次，避免本地小模型被复盘占着影响下一轮开口。
    """
    if not _runtime_flag(runtime, "reflect_enabled", True):
        return False
    if str(verdict) in ("fail", "warn"):
        return True
    sample_n = _runtime_int(runtime, "reflect_sample_n", 6, 1, 20)
    uid = str(user_id or "")
    _pass_counter[uid] = _pass_counter.get(uid, 0) + 1
    if len(_pass_counter) > _COUNTER_MAX:
        _pass_counter.clear()
    return _pass_counter[uid] % sample_n == 0


def _build_user_message(ctx: dict) -> str:
    facts = [str(f.get("text") or f) for f in (ctx.get("facts") or []) if str(f).strip()]
    facts = [_clip(f, _FACT_LEN) for f in facts[:_FACT_MAX]]
    episodes = [_clip(str(e.get("text") or ""), 60) for e in (ctx.get("episodes") or [])][:2]
    loops = [_clip(str(l.get("text") or ""), 40) for l in (ctx.get("open_loops") or [])][:2]

    lines = [
        f"这一轮他说：{_clip(ctx.get('user_text'), _CTX_LIMITS['user_text'])}",
        f"你回的是：{_clip(ctx.get('reply'), _CTX_LIMITS['reply'])}",
    ]
    if str(ctx.get("prev_reply") or "").strip():
        lines.append(f"（上一轮你回过：{_clip(ctx.get('prev_reply'), _CTX_LIMITS['prev_reply'])}）")
    if facts:
        lines.append("当时想起的关于他的事：" + "；".join(facts))
    if episodes:
        lines.append("前几天的事：" + "；".join(episodes))
    if loops:
        lines.append("他还没收尾的事：" + "；".join(loops))
    if str(ctx.get("tool_summary") or "").strip():
        lines.append(f"工具结果：{_clip(ctx.get('tool_summary'), _CTX_LIMITS['tool_summary'])}")
    lines.append(f"当时情绪：{ctx.get('emotion') or 'neutral'}"
                 f"（强度 {ctx.get('intensity') or 0}）")
    # 规则只翻成中文现象描述给模型对照，英文标签不进输入，避免被原样抄进复盘。
    cn_rules = [_RULE_CN.get(r, r) for r in (ctx.get("rules") or [])]
    lines.append(f"这轮自检看到的现象（用你自己的话说，别照抄这些词）："
                 f"{', '.join(cn_rules) or '无'}")
    lines.append(f"这一轮结论：{ctx.get('verdict') or 'pass'}")
    return "\n".join(lines)


_ENUM_PREFIX = re.compile(r"^\s*(?:[-•*]|\d+[.、)）]|[一二三四五六七八九十]+[、.）)]|[（(]\d+[）)])\s*")


def _clean_line(line: str) -> str:
    return _ENUM_PREFIX.sub("", str(line or "").strip()).strip()


def _parse_content(content: str) -> tuple[str, str, float]:
    """从模型输出里拆出 为什么 / 下次 / 可选信心；不合规返回空串。"""
    text = str(content or "").strip()
    if not text:
        return "", "", 0.0
    # 个别后端没压住思考链时，<think>…</think> 可能漏进正文，先整段摘掉。
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    text = re.sub(r"</?think>", "", text, flags=re.I).strip()
    if not text:
        return "", "", 0.0
    why, how, confidence = "", "", 0.0

    # 先按标签做全局正则提取（容忍两段写在同一行、带序号前缀等情况）。
    m_why = re.search(r"为什么[：:]\s*(.*?)(?=下次[：:]|$)", text, re.S)
    m_how = re.search(r"下次[：:]\s*(.*)$", text, re.S)
    if m_why:
        why = _clean_line(m_why.group(1))
    if m_how:
        how = _clean_line(m_how.group(1))

    # 兜底：标签写法变体（原因 / 改进 / how / why）逐行找。
    for raw in text.splitlines():
        line = _clean_line(raw)
        if not line:
            continue
        low = line.lower()
        if not why and (line.startswith(("原因", "why")) or low.startswith("why")):
            why = re.sub(r"^(原因|why)[：:]\s*", "", line, flags=re.I).strip()
        if not how and (line.startswith(("改进", "怎么改", "how")) or low.startswith("how")):
            how = re.sub(r"^(改进|怎么改|how)[：:]\s*", "", line, flags=re.I).strip()
        if line.startswith(("信心", "置信", "confidence")):
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", line)
            if m:
                confidence = max(0.0, min(1.0, float(m.group(1))))
    # 再兜底：没有按标签分段时，按句号切两段。
    if (not why or not how) and "。" in text:
        parts = [p.strip() for p in text.split("。") if p.strip()]
        if len(parts) >= 2:
            why = why or _clean_line(parts[0])
            how = how or _clean_line(parts[1])
    why = _clip(why, _WHY_MAX)
    how = _clip(how, _HOW_MAX)
    return why, how, confidence


def _invalid(why: str, how: str) -> str:
    """返回不合规原因；合规返回空串。"""
    if len(why) < 6:
        return "why 过短或缺失"
    if len(how) < 4:
        return "how 过短或缺失"
    blob = (why + " " + how).lower()
    for label in _RULE_LABELS:
        if label in blob:
            return f"复述了规则名 {label}"
    return ""


def _rule_fallback(rules: list[str], reply: str, user_text: str) -> tuple[str, str, str]:
    """模型不可用时的规则级兜底：仍凑出「为什么 / 下次」两段人话。"""
    kind, how = checks.note_for(rules or [])
    why = ""
    for rule in rules or []:
        if rule in _RULE_WHY:
            why = _RULE_WHY[rule]
            break
    if not why:
        if len(str(reply or "")) > checks._MAX_LEN:
            why = f"这一回回了 {len(str(reply or ''))} 字，说得太满，没留给他接话的口子。"
        else:
            why = "这一轮自检没全过，回看回复还有不顺的地方。"
    if not how:
        how = "先接住他这句话里那件具体的事，说短一点，留一句给他接。"
    return kind or "rule", _clip(why, _WHY_MAX), _clip(how, _HOW_MAX)


def run(ctx: dict, runtime: dict | None = None) -> dict[str, Any]:
    """跑一次复盘。返回结构见下方 RESULT 注释；任何失败都降级，不抛异常。

    result = {
      "kind", "verdict", "why", "how", "note", "source", "confidence",
      "degrade_reason", "ms", "used_model",
    }
    note = "为什么：…\\n下次：…"，前端 pre-wrap 原样展示，保留换行。
    """
    runtime = runtime or {}
    started = time.perf_counter()
    verdict = str(ctx.get("verdict") or "pass")
    rules = list(ctx.get("rules") or [])
    base, model, timeout_s = _resolve_endpoint(runtime)

    def _result(kind: str, why: str, how: str, source: str,
                confidence: float, degrade_reason: str) -> dict[str, Any]:
        note = f"为什么：{why}\n下次：{how}" if why and how else how or why
        return {
            "kind": kind, "verdict": verdict, "why": why, "how": how, "note": note,
            "source": source, "confidence": round(float(confidence or 0.0), 2),
            "degrade_reason": degrade_reason,
            "ms": round((time.perf_counter() - started) * 1000.0, 1),
            "used_model": model,
        }

    # 先备一份规则兜底，模型路径任何一步失败都能立刻用上。
    fb_kind, fb_why, fb_how = _rule_fallback(rules, ctx.get("reply"), ctx.get("user_text"))

    try:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(ctx)},
            ],
            "stream": False,
            "temperature": 0.3,
            "max_tokens": _MODEL_MAX_TOKENS,
            # MiniCPM5 是思考型模型：不开思考链，否则预算被内部推理吃光、正文留空。
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers: dict[str, str] = {}
        try:
            from brain.services import providers

            llm = providers.get("llm") or {}
            cfg = llm.get("config") if isinstance(llm.get("config"), dict) else llm
            if cfg and str(cfg.get("api_key") or "").strip():
                headers["Authorization"] = "Bearer " + str(cfg["api_key"])
        except Exception:
            pass
        timeout = httpx.Timeout(timeout_s, connect=1.5, read=timeout_s, write=2.0, pool=1.5)
        with httpx.Client(timeout=timeout, headers=headers, trust_env=False) as client:
            resp = client.post(base + "/v1/chat/completions", json=payload)
            resp.raise_for_status()
            obj = resp.json()
        message = (obj.get("choices") or [{}])[0].get("message") or {}
        content = str(message.get("content") or "")
        why, how, confidence = _parse_content(content)
        bad = _invalid(why, how)
        if bad:
            log.info("复盘输出不合规（%s），降级规则级 note", bad)
            return _result(fb_kind, fb_why, fb_how, "rule", 0.3, f"模型输出不合规：{bad}")
        if confidence <= 0.0:
            confidence = 0.6
        return _result(fb_kind or "reflect", why, how, "model", confidence, "")
    except httpx.TimeoutException:
        reason = f"模型复盘超时（>{timeout_s:g}s）"
        log.warning(reason)
        return _result(fb_kind, fb_why, fb_how, "rule", 0.3, reason)
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
        reason = f"复盘模型不可用：{type(exc).__name__}: {exc}"
        log.warning(reason)
        return _result(fb_kind, fb_why, fb_how, "rule", 0.3, reason)
    except Exception as exc:  # 最后兜底：绝不把异常带进链路
        reason = f"复盘异常：{type(exc).__name__}: {exc}"
        log.exception(reason)
        return _result(fb_kind, fb_why, fb_how, "rule", 0.3, reason)
