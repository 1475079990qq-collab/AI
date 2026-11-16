"""实时链路路由：POST /chat(SSE) / POST /tts / GET /health。

只用原生 StreamingResponse，不依赖 sse-starlette。
模块级 asyncio.Semaphore(1) 保证全局并发 = 1（GPU 独占），忙时立即回 BUSY。
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import time
from collections import OrderedDict
from typing import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from brain import db, schemas
from brain.config import settings
from brain.schemas import ChatRequest, TTSRequest, TurnEvent, new_request_id
from brain.services import asr, pipeline, tts

log = logging.getLogger("brain.chat")

router = APIRouter()

# 并发闸门：默认只让本地 GPU 独占 1 路并发；远端 LLM 服务放开。
# 为什么：Ollama 跑在本机时 GPU 是瓶颈，多并发反而互相抢占；
#         换成云端 API / 另一台机器上的 vLLM 时，本进程只是 HTTP 客户端，
#         串行化纯属浪费，直接放开就行。
_gate = asyncio.Semaphore(1)
_gate_disabled_for_remote = True   # True=远端 LLM 不抢占 GPU，直接并发；False=始终串行


def _llm_is_local() -> bool:
    """判断 LLM base_url 是否指向本机（127.0.0.1 / localhost）。

    优先级：config_store 的 llm provider config > settings.OLLAMA_BASE。
    只认明确的本地标记，其它一律当远端放开——错放比错严好。
    """
    try:
        from brain import config_store
        llm = config_store.get_provider("llm") or {}
        base = str((llm.get("config") or {}).get("base_url") or "").strip().lower()
    except Exception:
        base = ""
    if not base:
        base = str(getattr(settings, "OLLAMA_BASE", "") or "").strip().lower()
    if not base:
        return True   # 没拿到就保守地锁，不让 GPU 打架
    for marker in ("127.0.0.1", "localhost", "0.0.0.0"):
        if marker in base:
            return True
    return False


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

# 音频上限：60 秒 16k 单声道 16bit ≈ 1.9MB，留足余量；超了直接 413，
# 不把整个 body 读进内存（原来先全量 request.body() 再校验时长，可被大包顶内存）。
_ASR_MAX_BYTES = 20 * 1024 * 1024

# 客户端零记忆：服务端维护每会话最近几轮，供 judge 和 prompt 共同使用。
# 有界 LRU：会话数超上限淘汰最久未用的，避免长跑内存无界增长。
_HISTORY: "OrderedDict[str, list[dict]]" = OrderedDict()
# 4 条 = 最近 2 轮问答。原来给 4 轮，2B 小模型会照着前几轮的句式往下抄，
# 实测连问三遍电力装备时四轮回复开头一模一样 —— 上下文给得越多，复读越狠。
_HISTORY_MAX = 4
_HISTORY_MAX_SESSIONS = 512


def _hkey(req: ChatRequest) -> str:
    return f"{req.user_id}::{req.session_id}"


def _get_history(req: ChatRequest) -> list[dict]:
    key = _hkey(req)
    turns = _HISTORY.get(key)
    if turns is None:
        return []
    _HISTORY.move_to_end(key)
    return list(turns)


def _similar(a: str, b: str) -> float:
    """粗略相似度：开头 12 字相同就算高度雷同，否则用 difflib 比例。"""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    if a[:12] == b[:12]:
        return 1.0
    try:
        return difflib.SequenceMatcher(None, a, b).ratio()
    except Exception:
        return 0.0


def _append_history(req: ChatRequest, reply: str) -> None:
    key = _hkey(req)
    turns = _HISTORY.setdefault(key, [])
    turns.append({"role": "user", "content": req.text})
    if reply:
        # 复读样本不许进历史：模型看到自己上一轮的原话会照着往下抄。
        # 与上一条 assistant 高度相似时，用新的替换旧的，历史里始终只有一条。
        prev = None
        for item in reversed(turns):
            if str(item.get("role")) == "assistant":
                prev = item
                break
        if prev is not None and _similar(str(prev.get("content") or ""), reply) >= 0.5:
            try:
                turns.remove(prev)
            except ValueError:
                pass
        turns.append({"role": "assistant", "content": reply})
    del turns[:-_HISTORY_MAX]
    _HISTORY.move_to_end(key)
    while len(_HISTORY) > _HISTORY_MAX_SESSIONS:
        _HISTORY.popitem(last=False)


def _err_frame(code: str, message: str, request_id: str, retryable: bool = True) -> str:
    return TurnEvent("error", {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": request_id,
    }).to_sse()


def _db_ok() -> str:
    try:
        conn = db.connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return "ok"
    except Exception:
        return "error"


@router.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    request_id = req.request_id or new_request_id()

    async def body() -> AsyncIterator[str]:
        # 是否需要 GPU 独占闸门：本地 Ollama 需要；远端 LLM（云端 API / 远程 vLLM）放开。
        need_gate = _gate_disabled_for_remote and _llm_is_local()
        if need_gate:
            # 非阻塞取锁：Semaphore 空闲时 acquire 不挂起，检查与获取之间无 await 竞态
            if _gate.locked():
                yield _err_frame("BUSY", "服务正忙，请稍后再试", request_id)
                return
            await _gate.acquire()
        started = time.perf_counter()
        try:
            try:
                params = db.get_runtime_params()   # 请求级快照（热切换）
            except Exception:
                log.exception("read runtime params failed")
                params = {}
            history = _get_history(req)
            reply: list[str] = []
            got_done = False
            async for ev in pipeline.handle(req, params, history):
                if ev.type == "token":
                    reply.append(str(ev.data.get("delta", "")))
                elif ev.type == "done":
                    got_done = True
                if ev.type == "ping":
                    yield schemas.ping_frame()
                else:
                    yield ev.to_sse()
            if got_done:
                _append_history(req, "".join(reply))
                log.info(
                    "对话  %s 字 → 回复 %s 字 / %.0f ms  ｜%s",
                    len(req.text or ""), len("".join(reply)),
                    (time.perf_counter() - started) * 1000,
                    (req.text or "")[:24],
                )
        except Exception as exc:
            # 兜底：pipeline 之外万一漏异常，也不让 SSE 裸奔 / 500
            log.exception("chat stream failed")
            yield _err_frame("LLM_STREAM_FAILED", str(exc), request_id)
        finally:
            if need_gate:
                _gate.release()

    return StreamingResponse(body(), media_type="text/event-stream", headers=_SSE_HEADERS)


class FeedbackBody(BaseModel):
    """一条反馈：他觉得这轮行不行。

    verdict: 1 = 对了 / -1 = 不对 / 0 = 没表态。
    带 delivery_id 时同时把这轮递出去的卡一起标记 —— 卡对不对，比话好不好更可验证。
    """

    request_id: str = ""
    verdict: int = 0
    delivery_id: int = 0
    reason: str = ""
    target: str = ""


@router.post("/feedback")
def feedback(body: FeedbackBody) -> JSONResponse:
    """收反馈：只有「对了 / 不对」两档，不让人写长文。

    为什么必须有这一步：没有回执的陪伴，再温柔也不会变准 —— 它永远不知道自己错在哪。
    """
    verdict = 1 if body.verdict > 0 else (-1 if body.verdict < 0 else 0)
    kind = {1: "good", -1: "bad", 0: "neutral"}[verdict]
    try:
        db.add_feedback(
            request_id=body.request_id,
            kind=kind,
            reason=body.reason,
            target=body.target,
        )
    except Exception:
        log.exception("写反馈失败")
        return JSONResponse(status_code=503, content={"ok": False, "detail": "FEEDBACK_UNAVAILABLE"})
    if body.delivery_id and verdict != 0:
        db.set_delivery_verdict(int(body.delivery_id), verdict)
    log.info("反馈  %s  ｜%s", kind, (body.target or "")[:24])
    return JSONResponse(content={"ok": True, "kind": kind})


@router.post("/tts")
async def tts_endpoint(req: TTSRequest) -> Response:
    request_id = req.request_id or new_request_id()
    started = time.perf_counter()
    try:
        audio = await tts.synthesize(req.text, req.emotion, req.reference_audio_id)
    except Exception:
        log.exception("tts synthesize failed")
        return JSONResponse(status_code=503, content={"detail": "TTS_UNAVAILABLE"})
    if not audio:
        log.warning("tts 返回空音频  ｜%s", (req.text or "")[:24])
        return JSONResponse(status_code=503, content={"detail": "TTS_UNAVAILABLE"})
    log.info(
        "转发合成  %s 字 → %d KB / %.1f 秒 / %s  ｜%s",
        len(req.text or ""), len(audio) // 1024,
        (time.perf_counter() - started), req.emotion, (req.text or "")[:24],
    )
    emo = str(req.emotion)
    try:
        emo.encode("latin-1")      # 响应头必须 latin-1 可编码，否则会 500
    except UnicodeEncodeError:
        emo = "neutral"
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"X-Request-Id": request_id, "X-Emotion": emo},
    )


@router.post("/asr")
async def asr_endpoint(request: Request) -> JSONResponse:
    """语音输入：收一段音频，回文字。

    前端链路是「按住说话 → 松手 → 调这里 → 拿到文字塞进输入框」。
    识别服务是独立进程（:8103），没起就回 503，前端提示一句，打字照常。
    """
    content_type = (request.headers.get("content-type") or "").lower()

    if "multipart/form-data" in content_type:
        try:
            form = await request.form()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "stage": "audio", "detail": "表单解析失败", "text": ""},
            )
        upload = form.get("file") or form.get("audio")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "stage": "audio", "detail": "multipart 里要有 file 字段", "text": ""},
            )
        data = await upload.read()
        if len(data) > _ASR_MAX_BYTES:
            return JSONResponse(
                status_code=413,
                content={"ok": False, "stage": "audio",
                         "detail": f"音频超过 {_ASR_MAX_BYTES // (1024 * 1024)}MB 上限", "text": ""},
            )
        mime = str(getattr(upload, "content_type", "") or "audio/wav")
    else:
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > _ASR_MAX_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"ok": False, "stage": "audio",
                             "detail": f"音频超过 {_ASR_MAX_BYTES // (1024 * 1024)}MB 上限", "text": ""},
                )
        data = bytes(buf)
        mime = content_type.split(";")[0].strip() or "audio/wav"

    result = await asr.transcribe(data, mime)
    if result.get("ok"):
        log.info("asr ok  %.0fms  %d 字  %s",
                 float(result.get("ms") or 0), len(result.get("text") or ""),
                 result.get("emotion"))
        return JSONResponse(content=result)

    stage = str(result.get("stage") or "remote")
    status = {"service": 503, "audio": 400}.get(stage, 502)
    log.warning("asr failed (%s): %s", stage, result.get("detail"))
    return JSONResponse(status_code=status, content=result)


@router.get("/health")
def health() -> dict:
    """如实报告「现在真的在用哪个引擎」。

    模型都是独立进程：判断服务和语音服务在不在，直接问它们，不猜。
    这样排查时一眼能看出是「服务没起」还是「起了但大脑没连上」。
    """

    judge_mode, judge_up, judge_url = "rule", False, settings.JUDGE_BASE
    try:
        from brain.services.judge import engine_status

        st = engine_status()
        judge_mode = str(st.get("effective") or "rule")
        judge_up = bool((st.get("remote") or {}).get("up"))
        judge_url = str(st.get("endpoint") or "") or settings.JUDGE_BASE
    except Exception:
        pass

    tts_mode, tts_up, tts_url = "http", False, settings.TTS_BASE
    try:
        from brain.services import tts as tts_service

        info = tts_service.status()
        tts_mode = str(info.get("mode") or tts_mode)
        tts_up = bool(info.get("up"))
        tts_url = str(info.get("endpoint") or "") or settings.TTS_BASE
    except Exception:
        pass

    asr_up, asr_url, asr_ready = False, settings.ASR_BASE, False
    try:
        info = asr.status()
        asr_up = bool(info.get("up"))
        asr_ready = bool(info.get("ready"))
        asr_url = str(info.get("endpoint") or "") or settings.ASR_BASE
    except Exception:
        pass

    return {
        "status": "ok",
        "llm": "http",
        "judge": judge_mode,
        "judge_service": {"up": judge_up, "url": judge_url},
        "asr": "sherpa-onnx-sensevoice" if asr_ready else "off",
        "asr_service": {"up": asr_up, "ready": asr_ready, "url": asr_url},
        "tts": tts_mode,
        "tts_service": {"up": tts_up, "url": tts_url},
        "memory": "sqlite",
        "gpu": {"available": False},
        "db": _db_ok(),
    }
