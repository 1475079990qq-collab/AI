"""语音识别：把用户说的话转成文字。

识别服务可选、独立于大脑进程（:8103），这里只做转发和降级判断，
大脑进程不 import sherpa_onnx、不加载权重。独立启动脚本已归档到 `_旧启动脚本/`。

服务没起时返回 ok=False，路由给前端一句「语音识别没开」，
打字聊天完全不受影响——和小智那种「ASR 挂了整条链路就断」不一样。
"""
from __future__ import annotations

import httpx

from brain.config import settings
from brain.services import model_status

_DOWN_HINT = "语音识别服务没启动（可选服务；独立启动脚本已归档到 `_旧启动脚本/`）"


def status(force: bool = False) -> dict:
    """给 /health 和控制台用：识别服务在不在、模型可不可用。"""
    remote = model_status.probe_asr(force=force)
    payload = remote.get("payload") or {}
    return {
        "endpoint": model_status.asr_base() + "/asr",
        "up": bool(remote.get("up")),
        "ready": bool(payload.get("ready", remote.get("up"))),
        "detail": str(payload.get("detail") or remote.get("detail") or ""),
        "model": str(payload.get("model") or ""),
        "language": str(payload.get("language") or settings.ASR_LANGUAGE),
    }


async def transcribe(data: bytes, content_type: str = "audio/wav") -> dict:
    """把音频送过去识别。

    返回统一结构，调用方只看 ok：
        {"ok": True, "text": "…", "emotion": "sad", "lang": "zh", ...}
        {"ok": False, "detail": "为什么不行", "text": ""}
    """
    if not data:
        return {"ok": False, "stage": "audio", "detail": "没有收到音频", "text": ""}

    remote = model_status.probe_asr()
    if not remote.get("up"):
        # 探活结果是缓存的（没起会缓存 30 秒）。用户主动按住说话，说明他期待
        # 能用——很可能是识别服务刚起来、缓存还是旧结论。这时候值得再真探一次：
        # 多花 2 秒，好过让人对着麦克风说话却一直被告知「服务没启动」。
        remote = model_status.probe_asr(force=True)
    if not remote.get("up"):
        return {"ok": False, "stage": "service", "detail": _DOWN_HINT, "text": ""}
    payload = remote.get("payload") or {}
    if payload.get("ready") is False:
        why = str(payload.get("detail") or "识别模型未就绪").split("。")[0]
        return {"ok": False, "stage": "service", "detail": why, "text": ""}

    url = model_status.asr_base() + "/asr"
    try:
        async with httpx.AsyncClient(timeout=settings.ASR_TIMEOUT, trust_env=False) as client:
            resp = await client.post(
                url, content=data, headers={"content-type": content_type or "audio/wav"}
            )
    except Exception as exc:
        return {
            "ok": False, "stage": "service",
            "detail": f"连不上识别服务（{type(exc).__name__}）", "text": "",
        }

    if resp.status_code >= 400:
        try:
            detail = str(resp.json().get("detail") or "")
        except Exception:
            detail = resp.text[:160]
        stage = "audio" if resp.status_code == 400 else "remote"
        return {"ok": False, "stage": stage, "detail": detail or f"识别失败 {resp.status_code}", "text": ""}

    try:
        body = resp.json()
    except Exception:
        return {"ok": False, "stage": "remote", "detail": "识别服务返回了非 JSON", "text": ""}

    return {
        "ok": True,
        "stage": "done",
        "text": str(body.get("text") or ""),
        "lang": str(body.get("lang") or ""),
        "emotion": str(body.get("emotion") or "neutral"),
        "event": str(body.get("event") or ""),
        "duration_ms": int(body.get("duration_ms") or 0),
        "ms": float(body.get("ms") or 0),
        "detail": "",
    }
