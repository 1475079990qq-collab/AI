"""实时链路编排（pipeline）。

严格顺序：judge(to_thread) → open_event(开流前同步落库) → meta
→ memory.retrieve → prompt.build → llm.stream(逐 token / 空闲按 sse_ping_interval 插 ping)
→ close_event → done。

请求级快照：persona / runtime params 每轮都从 DB 重读，热切换立即生效。
每轮 config_store.open_turn / close_turn 记轮次明细；全局开关按 runtime 生效。
断连 / 异常统一在 finally 里清理 LLM 流并补标 truncated。
所有配置层读取失败都降级回退原行为，绝不打断对话。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

from brain import db, tools
from brain.config import settings
from brain.schemas import ChatRequest, EmotionResult, TurnEvent, new_request_id
from brain.services import feedback_rules, judge, live, llm, memory, prompt, tone

try:
    from brain import config_store
except Exception:  # pragma: no cover
    config_store = None
log = logging.getLogger("brain.pipeline")

_PING_INTERVAL = 15.0
# 反刍延迟：人不会刚说完就立刻复盘，隔几秒再回头想才像自己的念头。
_RUMINATE_DELAY = 6.0
# 同一用户的复盘单飞：上一轮复盘还没跑完，这一轮不叠第二个，避免本地小模型被占住。
_reflect_busy: set[str] = set()
_NOTHING_HEARD = "嗯？我没听清，你再说一次好不好。"
_MAINTENANCE = "系统正在做一点小小的维护，我很快就回来，你先照顾好自己，等会儿再陪你慢慢聊，好吗？"

_ERROR_BY_STAGE = {
    "judge": "MODEL_UNAVAILABLE",
    "memory": "MEMORY_DEGRADED",
    "prompt": "LLM_STREAM_FAILED",
    "llm": "LLM_STREAM_FAILED",
}


def _safe_db(fn, *args, **kwargs):
    """DB 写失败只记日志、不发 error 事件；失败时返回 None。"""
    try:
        return fn(*args, **kwargs)
    except Exception:
        log.exception("db op failed: %s", getattr(fn, "__name__", repr(fn)))
        return None


def _llm_base() -> str:
    """报错时给用户看真实地址，而不是一句英文 All connection attempts failed。"""
    try:
        from brain import config_store

        cfg = (config_store.get_provider("llm") or {}).get("config") or {}
        if isinstance(cfg, dict) and cfg.get("base_url"):
            return str(cfg["base_url"])
    except Exception:
        pass
    return str(settings.OLLAMA_BASE)


def _error_message(exc: BaseException, stage: str) -> str:
    text = str(exc)
    low = text.lower()
    if isinstance(exc, TimeoutError) or "timeout" in low or "timedout" in low:
        return f"大模型没在时限内回话（{_llm_base()}），再发一次试试，或者把超时调大一点。"
    if stage in ("llm", "prompt") and any(
            k in low for k in ("connect", "refused", "unreachable", "all connection attempts")):
        return (f"大模型连不上（{_llm_base()}）。先把模型服务起起来（你 F 盘那个），"
                f"起来后直接重发就行。")
    return text


def _error_code(exc: BaseException, stage: str) -> str:
    name = type(exc).__name__.lower()
    if isinstance(exc, TimeoutError) or "timeout" in name or "timedout" in name:
        return "LLM_TIMEOUT"
    return _ERROR_BY_STAGE.get(stage, "LLM_STREAM_FAILED")


def _runtime_flag(runtime: dict, key: str, default: bool) -> bool:
    """读 0/1 开关；缺键 / 异常回退默认值，不影响对话。"""
    try:
        raw = runtime.get(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return default


# 纯语气词 / 无信息量的短句，不进长期记忆，否则知识库会被「嗯」「哦」填满。
_FILLERS = {"嗯", "哦", "啊", "哈", "好", "好的", "行", "在吗", "你好",
            "嗯嗯", "哦哦", "好的好的", "?", "？", "。", "…"}

# 知识库里每条记忆的标题用中文情绪名，直接看文档列表比看 label 直观。
_EMO_CN = {"happy": "开心", "sad": "难过", "angry": "生气", "fear": "害怕",
           "surprised": "惊讶", "disgust": "厌恶", "care": "关心", "neutral": "日常"}


def _worth_remembering(user_text: str) -> bool:
    t = (user_text or "").strip()
    return len(t) >= 4 and t not in _FILLERS


def _anti_repeat(messages: list[dict], history: list[dict] | None) -> None:
    """提醒模型别在同一条上打转。

    注意不能把上一轮的原话贴进 system —— 贴了等于给它一份可以直接照抄的样本，
    2B 模型会一字不差地抄回来（实测过）。这里只说「你已经回应过」，不引用原文。
    """
    try:
        prev = _prev_reply(history)
        if not prev or len(prev) < 8:
            return
        if not messages or str(messages[0].get("role")) != "system":
            return
        messages[0]["content"] += (
            "\n\n【避免复读】你上一轮已经回应过他类似的话了。这一轮不要再用相同或相近的句式、"
            "结论和开头，换成另一种说法，直接说他这次问的具体内容（具体环节、例子、注意点）。"
        )
    except Exception:
        return


# 复读判定：去掉标点后比开头 N 个字。2B 模型复读的特征就是开头一字不差。
# 取 12 字：8 字太容易误伤「我觉得你说得对」这类正常开头，而重生成一次代价是延迟翻倍。
_HEAD_LEN = 8
_PUNCT = "，。！？,.!? \n\t“”\"'（）()…、；:：-—"


def _plain(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch not in _PUNCT)


def _prev_reply(history: list[dict] | None) -> str:
    """历史里最后一条非空 assistant 回复。"""
    for turn in reversed(history or []):
        if not isinstance(turn, dict):
            continue
        if str(turn.get("role")) == "assistant":
            content = str(turn.get("content") or "").strip()
            if content:
                return content
    return ""


def _same_head(a: str, b: str, n: int = _HEAD_LEN) -> bool:
    pa, pb = _plain(a), _plain(b)
    return len(pa) >= n and pa[:n] == pb[:n]


def _shared_terms(facts: list[dict], limit: int = 3) -> list[str]:
    """从本轮召回事实的共享两字词里取联想词；取不到返回空数组。"""
    try:
        gram_sets: list[set[str]] = []
        for f in facts or []:
            t = str((f or {}).get("text") or "")
            gram_sets.append({t[i:i + 2] for i in range(len(t) - 1)})
        if len(gram_sets) < 2:
            return []
        common = set.intersection(*gram_sets)
        return sorted(common)[:limit]
    except Exception:
        return []


def _schedule_remember(user_text: str, emotion_label: str,
                       user_id: str, session_id: str) -> None:
    """流结束后后台沉淀：把「用户说的话」写成长期记忆，绝不阻塞 SSE。

    只存用户这一侧 —— 过去存的是 AI 回复，知识库被自己的套话填满，
    检索回来的全是「我在呢，愿意多说一点吗」，这也是复读机的根源之一。
    顺手累加画像计数，到阈值就在后台重建一次画像。
    """
    if not user_text:
        return

    def _do() -> None:
        try:
            memory.store_fact(user_id, session_id, user_text, [])
        except Exception:
            pass
        try:
            from brain.services import profile
            profile.maybe_refresh(user_id)
        except Exception:
            pass

    try:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, _do)
        fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    except Exception:
        pass


def _schedule_after_turn(*, request_id: str, turn_id: int | None, user_text: str,
                         reply: str, history: list[dict] | None,
                         latency_ms: float, system_text: str,
                         runtime: dict, user_id: str,
                         bundle: dict | None = None, tool_summary: str = "",
                         emotion_label: str = "neutral",
                         emotion_intensity: float = 0.0) -> None:
    """流结束后的后台功课：规则自检 + 每日摘要 + 待办识别 + 自我复盘。

    全部丢线程池，绝不进 SSE 热路径；任何异常只记日志。复盘模型调用在
    6 秒延迟之后的后台线程里，硬超时由 reflect 模块兜底，不占这一轮的开口时间。
    """
    prev_reply = _prev_reply(history)
    prev_user = ""
    for turn in reversed(history or []):
        if isinstance(turn, dict) and str(turn.get("role")) == "user":
            prev_user = str(turn.get("content") or "")
            break
    assistant_name = str((runtime or {}).get("assistant_name") or "").strip()

    def _do() -> None:
        _t_bg = time.perf_counter()
        live.emit("bg_start", job="自检与摘要")
        ok = True
        try:
            from brain.services import checks, digest

            rules, verdict = checks.check_reply(
                reply=reply, prev_reply=prev_reply, user_text=user_text,
                prev_user=prev_user, latency_ms=latency_ms,
                assistant_name=assistant_name, system_prompt=system_text,
            )
            db.log_check(request_id=request_id, turn_id=turn_id, rules=rules,
                         verdict=verdict, latency_ms=latency_ms)
            if user_text:
                digest.detect_open_loop(user_text, user_id)
            digest.rebuild_episode(user_id)
        except Exception:
            ok = False
            log.exception("流后后台任务失败（自检/摘要）")
        finally:
            # 真实的后台耗时：说完话之后它还在整理，这一段也看得见
            live.emit("bg_done", job="自检与摘要", ok=ok,
                      ms=round((time.perf_counter() - _t_bg) * 1000.0, 1))

        # ---- 反刍：等他走出几步再回头想（不打断、也不占这一轮的任何时间）----
        reflect_payload: dict = {}
        stored = False
        try:
            time.sleep(_RUMINATE_DELAY)
            _t_ru = time.perf_counter()
            live.emit("ruminate_start", request_id=request_id, delay_s=_RUMINATE_DELAY)
            from brain.services import checks as _checks
            from brain.services import reflect as reflect_svc

            # 反刍看的是「说得像不像人」，不看快慢，所以 latency 传 0
            rules2, verdict2 = _checks.check_reply(
                reply=reply, prev_reply=prev_reply, user_text=user_text,
                prev_user=prev_user, latency_ms=0.0,
                assistant_name=assistant_name, system_prompt=system_text,
            )
            loop_unit = _checks.find_repeat_loop(reply)
            if loop_unit:
                rules2 = list(rules2) + ["repeat_loop"]
                verdict2 = "fail"

            ctx = {
                "user_text": user_text, "reply": reply,
                "prev_reply": prev_reply, "prev_user": prev_user,
                "rules": rules2, "verdict": verdict2,
                "emotion": emotion_label, "intensity": emotion_intensity,
                "facts": (bundle or {}).get("facts") or [],
                "episodes": (bundle or {}).get("episodes") or [],
                "open_loops": (bundle or {}).get("open_loops") or [],
                "tool_summary": tool_summary,
            }

            # 单飞：同一用户上一轮复盘没结束，这一轮直接跳过模型复盘。
            busy = user_id in _reflect_busy
            want_reflect = (not busy) and reflect_svc.should_reflect(
                runtime, verdict2, rules2, user_id)
            if want_reflect:
                _reflect_busy.add(user_id)
            try:
                if want_reflect:
                    reflect_payload = reflect_svc.run(ctx, runtime)
                else:
                    # 开关关 / 抽样没中 / 单飞重叠：保留旧的规则级记录路径。
                    kind, note = _checks.note_for(rules2)
                    fb_why, fb_how = "", note
                    if busy:
                        reason = "上一轮复盘未结束，本轮单飞跳过"
                    else:
                        reason = "未命中复盘触发条件（开关/抽样）"
                    reflect_payload = {
                        "kind": kind, "verdict": verdict2, "why": fb_why, "how": fb_how,
                        "note": note, "source": "rule", "confidence": 0.3,
                        "degrade_reason": reason, "ms": 0.0, "used_model": "",
                    }
            finally:
                if want_reflect:
                    _reflect_busy.discard(user_id)

            kind = str(reflect_payload.get("kind") or "")
            note = str(reflect_payload.get("note") or "")
            why = str(reflect_payload.get("why") or "")
            how = str(reflect_payload.get("how") or "")
            # fail/warn 必落库；pass 抽样复盘只有模型给出了具体改法才记一条，
            # 规则兜底的空泛结论不刷条数。
            should_store = bool(kind and note) and (
                verdict2 in ("fail", "warn")
                or (verdict2 == "pass" and reflect_payload.get("source") == "model" and how)
            )
            if should_store:
                db.add_lesson(
                    user_id=user_id, request_id=request_id, kind=kind, note=note,
                    verdict=verdict2, source=str(reflect_payload.get("source") or "rule"),
                    why=why, how=how,
                    confidence=float(reflect_payload.get("confidence") or 0.0),
                    degrade_reason=str(reflect_payload.get("degrade_reason") or ""),
                )
                stored = True
            live.emit(
                "ruminate_done", request_id=request_id, verdict=verdict2, rule=kind,
                stored=stored,
                ms=round((time.perf_counter() - _t_ru) * 1000.0, 1),
                why=why, how=how, source=reflect_payload.get("source"),
                confidence=reflect_payload.get("confidence"),
                degrade_reason=reflect_payload.get("degrade_reason"),
                reflect_ms=reflect_payload.get("ms"),
                used_model=reflect_payload.get("used_model"),
            )
        except Exception:
            log.exception("反刍失败（只影响这一条自我复盘）")

    try:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, _do)
        fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    except Exception:
        pass


async def handle(req: ChatRequest, params: dict, history: list[dict]) -> AsyncIterator[TurnEvent]:
    started = time.perf_counter()
    request_id = req.request_id or new_request_id()
    live.emit("turn_start", request_id=request_id, text_len=len(req.text or ""),
              text=str(req.text or "").strip()[:12])
    event_id: int | None = None
    turn_id: int | None = None
    stage = "judge"
    finished = False            # 是否已发出终帧（done / error / clarify）
    producer: asyncio.Task | None = None
    agen = None
    reply: list[str] = []
    messages = None
    degraded = False
    truncated = False
    final_status = "ok"
    runtime: dict = {}
    effective_params: dict = {}
    ping_interval = _PING_INTERVAL
    log_prompt = True
    log_input_text = True
    auto_remember = True
    provider_snapshot: dict = {}

    try:
        # ---- 请求级快照：每轮重读，控制平面热切换立即生效 ----
        try:
            persona = db.get_active_persona()
        except Exception:
            log.exception("read active persona failed, using default")
            persona = {"system_prompt": db.DEFAULT_PERSONA, "version": 1}
        try:
            runtime = db.get_runtime_params()
        except Exception:
            log.exception("read runtime params failed")
            runtime = {}
        effective_params = {**(params or {}), **runtime}

        # ---- 全局开关 / 心跳间隔（全部异常兜底回退原行为）----
        maintenance = _runtime_flag(runtime, "maintenance_mode", False)
        log_prompt = _runtime_flag(runtime, "log_prompt", True)
        log_input_text = _runtime_flag(runtime, "log_input_text", True)
        auto_remember = _runtime_flag(runtime, "auto_remember", True)
        try:
            ping_interval = max(5.0, min(120.0,
                                         float(runtime.get("sse_ping_interval", _PING_INTERVAL))))
        except (TypeError, ValueError):
            ping_interval = _PING_INTERVAL
        try:
            if config_store is not None:
                provider_snapshot = config_store.provider_snapshot() or {}
        except Exception:
            provider_snapshot = {}
        persona_version = int(persona.get("version", 1) or 1)

        # ---- 1) 情感判断（同步函数丢线程池，避免阻塞事件循环）----
        stage = "judge"
        _t_judge = time.perf_counter()
        emotion: EmotionResult = (
            await asyncio.to_thread(judge.judge, req.text, history, request_id)
        ).normalized()
        live.emit("judge",
                  emotion=emotion.label,
                  intensity=float(emotion.intensity or 0),
                  ms=round((time.perf_counter() - _t_judge) * 1000.0, 1),
                  degraded=bool(emotion.degraded))

        degraded = bool(emotion.degraded)

        # ---- 2) 开流前同步落库，拿 event_id ----
        event_id = _safe_db(
            db.open_event,
            user_id=req.user_id,
            session_id=req.session_id,
            request_id=request_id,
            label=emotion.label,
            intensity=emotion.intensity,
            confidence=emotion.confidence,
            degraded=int(degraded),
            summary=req.text,
        )

        # ---- 3) meta（首帧）----
        # emotion 必须随首帧下发：客户端是边收字边按句合成语音的，
        # 如果只在 done 帧带情绪，此前切出去的每句话都只能用 neutral 合成。
        yield TurnEvent("meta", {
            "request_id": request_id,
            "persona_version": persona_version,
            "degraded": degraded,
            "emotion": emotion.label,
            "intensity": emotion.intensity,
        })

        # ---- 3.5) 轮次明细 open_turn（失败只记日志，不打断 SSE）----
        if config_store is not None:
            try:
                turn_id = _safe_db(
                    config_store.open_turn,
                    request_id=request_id,
                    user_id=req.user_id,
                    session_id=req.session_id,
                    input_text=(req.text if log_input_text else ""),
                    emotion_label=emotion.label,
                    intensity=emotion.intensity,
                    persona_version=persona_version,
                    provider_snapshot=provider_snapshot,
                )
            except Exception:
                turn_id = None

        # ---- 3.6) 维护模式：温柔提示（clarify 风格），不进 LLM ----
        if maintenance:
            if event_id:
                _safe_db(db.close_event, event_id, summary=_MAINTENANCE, fact_ids=[])
            reply = [_MAINTENANCE]
            final_status = "maintenance"
            finished = True
            yield TurnEvent("clarify", {
                "question": _MAINTENANCE,
                "done": True,
                "emotion": emotion.label,
                "intensity": emotion.intensity,
                "event_id": event_id or 0,
            })
            return

        # ---- 完全没收到内容：只在空字符串时兜底 ----
        # 短句（「啥」「哦」「叫什么」）一律交给 LLM 结合上下文回答，
        # 不再用固定文案拦截——那会让每句话都得到同一句回复，像复读机。
        if not req.text.strip():
            if event_id:
                _safe_db(db.close_event, event_id, summary=_NOTHING_HEARD, fact_ids=[])
            reply = [_NOTHING_HEARD]
            final_status = "clarify"
            finished = True
            yield TurnEvent("clarify", {
                "question": _NOTHING_HEARD,
                "done": True,
                "emotion": emotion.label,
                "intensity": emotion.intensity,
                "event_id": event_id or 0,
            })
            return

        # ---- 4) 记忆检索 ----
        stage = "memory"
        try:
            top_k = int(effective_params.get("memory_top_k", settings.MEMORY_TOP_K))
        except (TypeError, ValueError):
            top_k = settings.MEMORY_TOP_K
        # 必须放线程池：memory.retrieve 内部是同步 SQLite 读，
        # 直接在事件循环里跑会卡住整条 SSE（历史上知识库 ev.wait(2.0) 就是这么卡的）。
        _t_mem = time.perf_counter()
        bundle = await asyncio.to_thread(
            memory.retrieve, req.user_id, req.text, top_k
        ) or {}
        live.emit("memory",
                  facts=len(bundle.get("facts") or []),
                  emotions=len(bundle.get("emotions") or []),
                  episodes=len(bundle.get("episodes") or []),
                  open_loops=len(bundle.get("open_loops") or []),
                  source=str(bundle.get("source") or ""),
                  # 每一条事实是被哪条线索捞回来的：词重合 / 最近说过 / 带情绪
                  routes={tag: sum(1 for f in (bundle.get("facts") or [])
                                   if tag in (f.get("why") or []))
                          for tag in ("词重合", "最近说过", "带情绪")},
                  # 这一轮到底「想起了哪几条」：脑图据此点亮对应的神经元，并带出联想
                  ids=[str(f.get("id")) for f in (bundle.get("facts") or []) if f.get("id")][:6],
                  fact_text=str((bundle.get("facts") or [{}])[0].get("text") or "").strip()[:14],
                  linked=_shared_terms(bundle.get("facts") or []),
                  ms=round((time.perf_counter() - _t_mem) * 1000.0, 1))

        # ---- 5) 组装 prompt（请求级快照）----
        stage = "prompt"
        messages = prompt.build(persona, bundle, emotion, req.text, history)
        live.emit("prompt",
                  messages=len(messages),
                  chars=sum(len(str(m.get("content") or "")) for m in messages))
        # 防复读：把上一轮回复的指纹写进 system，逼它换说法/正面答
        _anti_repeat(messages, history)

        # ---- 5.5) 工具：要办的事，真去办（查 / 记 / 提醒）----
        # 这一段是「嘴」和「手」的分界：以前它只能说说，现在能真查一条、真记一条。
        # 工具结果以 system 消息插在主提示词之后，并明令「只用这些，不许编」。
        tool_hit: tuple[str, str] | None = None
        tool_result: dict | None = None
        stage = "tool"
        try:
            _t_tool = time.perf_counter()
            tool_hit = tools.detect(req.text)
            if tool_hit:
                tool_result = await tools.run(
                    tool_hit[0], tool_hit[1],
                    user_id=req.user_id, request_id=request_id,
                )
                messages.insert(1, {
                    "role": "system",
                    "content": tools.prompt_block(tool_hit[0], tool_hit[1], tool_result),
                })
            live.emit("tool",
                      name=(tool_hit[0] if tool_hit else ""),
                      hit=bool(tool_hit),
                      ok=bool(tool_result),
                      ms=round((time.perf_counter() - _t_tool) * 1000.0, 1))
        except Exception:
            log.exception("工具调用失败（这一轮按普通回答走）")
            tool_result = None
            live.emit("tool", name="", hit=False, ok=False, ms=0.0)

        # ---- 5.7) 「先想后说」：开关打开时，先在肚子里过一句（可关，默认关）----
        # 为什么做成开关：这台机器出字约 8 字/秒，多一次短推理就要多等 3~4 秒。
        # 默认关是保陪伴的节奏；想要更像人，就在 runtime_params 里把 think_draft 设成 1。
        if str((runtime or {}).get("think_draft") or "0").lower() in ("1", "true", "on"):
            stage = "draft"
            try:
                _t_draft = time.perf_counter()
                draft_params = dict(effective_params or {})
                draft_params["max_tokens"] = 48
                draft_parts: list[str] = []
                async for piece in llm.stream(messages, draft_params):
                    draft_parts.append(str(piece))
                    if len("".join(draft_parts)) >= 64:
                        break
                draft = "".join(draft_parts).strip()
                live.emit("think_draft", chars=len(draft),
                          ms=round((time.perf_counter() - _t_draft) * 1000.0, 1))
                if draft:
                    messages[0]["content"] += (
                        "\n\n【你刚才在心里先过了一句（只有你自己听得见，别念出来、别复述）："
                        + draft + "】"
                    )
            except Exception:
                log.exception("先想后说失败（这一轮直接说）")

        # ---- 6) LLM 流式输出：逐 token + 按 sse_ping_interval 空闲插 ping ----
        # LLM 生成器跑在独立 task 里：ping 计时只 cancel Queue.get（安全），
        # 绝不会 cancel 正在产出 token 的生成器 / httpx 流。
        stage = "llm"
        agen = llm.stream(messages, effective_params)
        # 这一轮没有独立的「意图/计划」结构，intent 留空，前端走兜底文案，不硬造。
        live.emit("llm_start", messages=len(messages), intent="")
        queue: asyncio.Queue = asyncio.Queue()
        _END = object()

        # 语色门：模型爱在正文里写「（语气轻松）」这种舞台说明，
        # 出口处按规则吞掉（正常括号不动）。旁白不可能比人说话更真。
        tone_guard = tone.StreamFilter()

        async def _produce(gen, q):
            # q 必须显式传：重启时 queue 会换新对象，靠闭包会写进新队列造成误判结束。
            try:
                async for delta in gen:
                    clean_delta = tone_guard.feed(delta)
                    if clean_delta:
                        await q.put(("token", clean_delta))
                tail = tone_guard.flush()
                if tail:
                    await q.put(("token", tail))
            except Exception as exc:      # LLM 异常转交消费端，统一发 error
                await q.put(("error", exc))
            finally:
                try:
                    q.put_nowait(("end", _END))
                except Exception:
                    pass

        producer = asyncio.ensure_future(_produce(agen, queue))
        prev_reply = _prev_reply(history)
        # 没有上一轮可比时不必扣字：扣字只是为了防「开头照抄上一轮」，
        # 首轮白扣 8 个字等于凭空多等一百多毫秒。
        head_buf: list[str] = []
        head_checked = not bool(prev_reply)
        retried = False
        # 脑图打点：首个 token 的时刻、累计字数（每 24 字广播一次，别刷屏）
        _t_llm = time.perf_counter()
        _first_token = False
        _chars = 0
        _next_beat = 24
        _recent = ""

        async def _restart() -> None:
            """开头雷同时中止本次生成，删掉可抄的旧回复再重来一次（只重试一次）。"""
            nonlocal agen, producer, queue, head_buf, head_checked, retried
            if producer is not None and not producer.done():
                producer.cancel()
                try:
                    await producer
                except BaseException:
                    pass
            try:
                await agen.aclose()
            except BaseException:
                pass
            retried = True
            head_buf.clear()
            head_checked = False
            # 把历史里那条旧回复从 messages 里摘掉：留着它，模型必定照抄。
            for i in range(len(messages) - 1, 0, -1):
                if str(messages[i].get("role")) == "assistant":
                    del messages[i]
                    break
            if messages and str(messages[0].get("role")) == "system":
                messages[0]["content"] += (
                    "\n\n【这次必须换开头】重新说一遍，开头几个字和句式都要和刚才完全不同，"
                    "直接说他这次问的具体内容。"
                )
            agen = llm.stream(messages, effective_params)
            queue = asyncio.Queue()
            producer = asyncio.ensure_future(_produce(agen, queue))

        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=ping_interval)
            except asyncio.TimeoutError:
                yield TurnEvent("ping")
                continue
            if kind == "token":
                _chars += len(str(payload))
                _recent = (_recent + str(payload))[-10:]
                if not _first_token:
                    _first_token = True
                    live.emit("first_token",
                              ms=round((time.perf_counter() - _t_llm) * 1000.0, 1))
                if _chars >= _next_beat:
                    _next_beat = _chars + 24
                    live.emit("tokens", chars=_chars, text=_recent)
                if not head_checked:
                    head_buf.append(str(payload))
                    probe = "".join(head_buf)
                    if len(_plain(probe)) >= _HEAD_LEN:
                        head_checked = True
                        if (not retried) and prev_reply and _same_head(probe, prev_reply):
                            log.info("开头与上一轮雷同，重生成一次 ｜%s", _plain(probe)[:12])
                            await _restart()
                            continue
                        for piece in head_buf:
                            reply.append(piece)
                            yield TurnEvent("token", {"delta": piece})
                        head_buf.clear()
                    continue
                reply.append(str(payload))
                yield TurnEvent("token", {"delta": payload})
            elif kind == "error":
                raise payload
            else:
                if head_buf:          # 回复比判定阈值还短：原样放行
                    for piece in head_buf:
                        reply.append(piece)
                        yield TurnEvent("token", {"delta": piece})
                    head_buf.clear()
                break
        summary = "".join(reply)

        # ---- 6.9) 说完了，回头听一遍：规则自检，毫秒级，只报不改嘴 ----
        # 为什么现在做而不是只在后台：这是「说话时自己在听」的那一下，
        # 判完之后这一轮的反面例子会写进下一轮的自我复盘，而不是永远沉在日志里。
        try:
            from brain.services import checks as _checks

            rules_now, verdict_now = _checks.check_reply(
                reply=summary, prev_reply=prev_reply, user_text=req.text,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                assistant_name=str((runtime or {}).get("assistant_name") or "").strip(),
                system_prompt=((messages[0].get("content") if messages else "") or ""),
            )
            loop_unit = _checks.find_repeat_loop(summary)
            if loop_unit:
                rules_now = list(rules_now) + ["repeat_loop"]
                verdict_now = "fail"
            live.emit("self_check", rules=rules_now, verdict=verdict_now,
                      chars=len(summary), loop=loop_unit)
            self_rules = rules_now
            self_verdict = verdict_now
        except Exception:
            self_rules, self_verdict = [], "pass"
            log.exception("出口自检失败（不影响这一轮回答）")

        # ---- 7) 流后只 update，关闭事件 ----
        # summary 保留开流时写入的用户短摘要；整段回复另存 reply_text，
        # 避免下一轮把好几段旧回复当成「情绪摘要」塞进 prompt。
        fact_ids = [str(f["id"]) for f in (bundle.get("facts") or []) if f.get("id")]
        if event_id:
            _safe_db(db.close_event, event_id, reply_text=summary, fact_ids=fact_ids)

        # ---- 7.5) 给：把这一轮能用的东西压成一张卡，先递出去再说 done ----
        # 「说」是表达层的第一个出口，「给」是第二个。只有第一个出口，
        # 永远只是「聊得不错」；有了第二个，才有「给到手上来」。
        # 纯规则、不调模型，毫秒级；出任何错都当作没有卡，绝不影响回答。
        card = None
        delivery_id = None
        try:
            from brain.services import deliver as deliver_svc

            card = deliver_svc.build(
                req.text, summary, emotion.label, bundle,
                policy=feedback_rules.policy(),
            )
            if card:
                delivery_id = db.add_delivery(
                    request_id=request_id,
                    turn_id=turn_id,
                    user_id=req.user_id,
                    kind=str(card.get("kind") or ""),
                    title=str(card.get("title") or ""),
                    body=json.dumps(card, ensure_ascii=False),
                )
        except Exception:
            log.exception("生成交付卡失败（这一轮按普通回答走）")
            card = None

        # ---- 8) done（末帧，与 error 互斥）----
        final_status = "ok"
        finished = True
        if card:
            yield TurnEvent("card", {
                "request_id": request_id,
                "delivery_id": delivery_id or 0,
                "kind": card.get("kind") or "note",
                "title": card.get("title") or "",
                "items": card.get("items") or [],
                "note": card.get("note") or "",
            })
        yield TurnEvent("done", {
            "done": True,
            "emotion": emotion.label,
            "intensity": emotion.intensity,
            "event_id": event_id or 0,
            "truncated": False,
        })
        live.emit("turn_done",
                  request_id=request_id,
                  ms=round((time.perf_counter() - started) * 1000.0, 1),
                  chars=len(summary),
                  emotion=emotion.label,
                  intensity=float(emotion.intensity or 0),
                  status="ok",
                  verdict=self_verdict,
                  rules=self_rules,
                  card=str(card.get("kind") or "") if card else "")

        # ---- 8.5) 流结束后自动沉淀（后台线程，不阻塞 SSE）----
        # 存用户原话而非 AI 回复；门槛只过滤语气词，不看情绪强度 ——
        # 平淡的日常对话才是画像的主体，之前按 intensity 卡死导致几乎不落记忆。
        if auto_remember and _worth_remembering(req.text):
            _schedule_remember(req.text, emotion.label, req.user_id, req.session_id)
        # 自检 / 摘要 / 待办：后台线程，不影响这一轮的 done 帧
        _schedule_after_turn(
            request_id=request_id,
            turn_id=turn_id,
            user_text=req.text,
            reply="".join(reply),
            history=history,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            system_text=((messages[0].get("content") if messages else "") or ""),
            runtime=runtime,
            user_id=req.user_id,
            bundle=bundle,
            tool_summary=(str(tool_result.get("summary") or "") if tool_result else ""),
            emotion_label=emotion.label,
            emotion_intensity=float(emotion.intensity or 0.0),
        )

    except asyncio.CancelledError:
        # 客户端断连：不吞掉取消，交给 finally 补标 truncated
        truncated = True
        final_status = "truncated"
        raise
    except Exception as exc:
        # 任何下游异常 → error 后 return；与 done 互斥。
        final_status = "error"
        live.emit("turn_error",
                  code=_error_code(exc, stage),
                  stage=stage,
                  ms=round((time.perf_counter() - started) * 1000.0, 1))
        if not finished:
            yield TurnEvent("error", {
                "code": _error_code(exc, stage),
                "message": _error_message(exc, stage),
                "retryable": True,
                "request_id": request_id,
            })
            finished = True
        return
    finally:
        # 关闭 LLM 流 / 回收生产者（断连、异常、正常收尾都走这里）
        if producer is not None:
            if not producer.done():
                producer.cancel()
            try:
                await producer
            except BaseException:
                pass
        if agen is not None:
            try:
                await agen.aclose()
            except BaseException:
                pass
        # 断连（CancelledError / aclose 的 GeneratorExit）等未发终帧的提前结束：
        # finally 里补标 truncated；DB 写失败只记日志、不发 error 事件。
        if not finished and event_id:
            truncated = True
            final_status = "truncated"
            _safe_db(db.mark_truncated, event_id)
        # ---- 统一 close_turn（结束 / 异常 / 断连都走这里，异常只记日志）----
        if turn_id and config_store is not None:
            try:
                close_kwargs = dict(
                    reply_text="".join(reply),
                    token_count=len(reply),
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    degraded=int(degraded),
                    truncated=int(truncated),
                    status=final_status,
                )
                # log_prompt=0 不存 prompt 快照；log_input_text=0 已在 open_turn 不存原文。
                if log_prompt and messages is not None:
                    try:
                        close_kwargs["prompt_snapshot"] = json.dumps(messages, ensure_ascii=False)
                    except Exception:
                        pass
                _safe_db(config_store.close_turn, turn_id, **close_kwargs)
            except Exception:
                pass
