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

from brain import db
from brain.config import settings
from brain.schemas import ChatRequest, EmotionResult, TurnEvent, new_request_id
from brain.services import judge, llm, memory, prompt

try:
    from brain import config_store
except Exception:  # pragma: no cover
    config_store = None
log = logging.getLogger("brain.pipeline")

_PING_INTERVAL = 15.0
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
_HEAD_LEN = 12
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
                         runtime: dict, user_id: str) -> None:
    """流结束后的后台功课：规则自检 + 每日摘要 + 待办识别。

    全部丢线程池，绝不进 SSE 热路径；任何异常只记日志。
    """
    prev_reply = _prev_reply(history)
    prev_user = ""
    for turn in reversed(history or []):
        if isinstance(turn, dict) and str(turn.get("role")) == "user":
            prev_user = str(turn.get("content") or "")
            break
    assistant_name = str((runtime or {}).get("assistant_name") or "").strip()

    def _do() -> None:
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
            log.exception("流后后台任务失败（自检/摘要）")

    try:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, _do)
        fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    except Exception:
        pass


async def handle(req: ChatRequest, params: dict, history: list[dict]) -> AsyncIterator[TurnEvent]:
    started = time.perf_counter()
    request_id = req.request_id or new_request_id()
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
        emotion: EmotionResult = (
            await asyncio.to_thread(judge.judge, req.text, history)
        ).normalized()

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
        bundle = await asyncio.to_thread(
            memory.retrieve, req.user_id, req.text, top_k
        ) or {}

        # ---- 5) 组装 prompt（请求级快照）----
        stage = "prompt"
        messages = prompt.build(persona, bundle, emotion, req.text, history)
        # 防复读：把上一轮回复的指纹写进 system，逼它换说法/正面答
        _anti_repeat(messages, history)

        # ---- 6) LLM 流式输出：逐 token + 按 sse_ping_interval 空闲插 ping ----
        # LLM 生成器跑在独立 task 里：ping 计时只 cancel Queue.get（安全），
        # 绝不会 cancel 正在产出 token 的生成器 / httpx 流。
        stage = "llm"
        agen = llm.stream(messages, effective_params)
        queue: asyncio.Queue = asyncio.Queue()
        _END = object()

        async def _produce(gen, q):
            # q 必须显式传：重启时 queue 会换新对象，靠闭包会写进新队列造成误判结束。
            try:
                async for delta in gen:
                    await q.put(("token", delta))
            except Exception as exc:      # LLM 异常转交消费端，统一发 error
                await q.put(("error", exc))
            finally:
                try:
                    q.put_nowait(("end", _END))
                except Exception:
                    pass

        producer = asyncio.ensure_future(_produce(agen, queue))
        prev_reply = _prev_reply(history)
        head_buf: list[str] = []          # 开头片段先扣着，判定完再决定放不放行
        head_checked = False
        retried = False

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

        # ---- 7) 流后只 update，关闭事件 ----
        # summary 保留开流时写入的用户短摘要；整段回复另存 reply_text，
        # 避免下一轮把好几段旧回复当成「情绪摘要」塞进 prompt。
        fact_ids = [str(f["id"]) for f in (bundle.get("facts") or []) if f.get("id")]
        if event_id:
            _safe_db(db.close_event, event_id, reply_text=summary, fact_ids=fact_ids)

        # ---- 8) done（末帧，与 error 互斥）----
        final_status = "ok"
        finished = True
        yield TurnEvent("done", {
            "done": True,
            "emotion": emotion.label,
            "intensity": emotion.intensity,
            "event_id": event_id or 0,
            "truncated": False,
        })

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
        )

    except asyncio.CancelledError:
        # 客户端断连：不吞掉取消，交给 finally 补标 truncated
        truncated = True
        final_status = "truncated"
        raise
    except Exception as exc:
        # 任何下游异常 → error 后 return；与 done 互斥。
        final_status = "error"
        if not finished:
            yield TurnEvent("error", {
                "code": _error_code(exc, stage),
                "message": str(exc),
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
