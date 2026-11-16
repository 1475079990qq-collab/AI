"""TTS：语音引擎已经并进大脑进程，这里只负责「把这句话变成 wav」。

两种模式：
  inproc（默认）  直接调 brain.tts_server 里的同一套引擎（不再占 :8102、不再多一次 HTTP）
  http           只有把 base_url 指到「别的机器」时才用——比如语音跑在另一台设备上
"""
from __future__ import annotations

import asyncio

import httpx

from brain.config import settings

# 配置中心（config_store 已存在；providers 为 S1 新建，未落地 / 异常一律降级）。
try:
    from brain import config_store
except Exception:  # pragma: no cover
    config_store = None
try:
    from brain.services import providers
except Exception:  # pragma: no cover
    providers = None

def _cfg() -> dict:
    """读 tts provider 配置；兼容嵌套 / 顶层两种形状；异常返回 {}。"""
    try:
        if providers is None:
            return {}
        raw = providers.get("tts") or {}
        if not isinstance(raw, dict):
            return {}
        inner = raw.get("config")
        if isinstance(inner, dict):
            return dict(inner)
        return {k: v for k, v in raw.items()
                if k not in {"code", "name", "category", "enabled", "mode",
                             "version", "updated_at", "fields"}}
    except Exception:
        return {}


# 情绪别名：外部模型 / 老版本客户端可能送 joy、anger、surprise 这类英文标签。
# 不收敛就会查不到 voice profile，直接退回 speed=1.0 的「完全平调」——
# 之前听感平淡，一半原因在这里。
_ALIAS: dict[str, str] = {
    "joy": "happy", "happiness": "happy", "excited": "happy",
    "anger": "angry", "mad": "angry",
    "surprise": "surprised",
    "trust": "care", "caring": "care", "gentle": "care",
    "sadness": "sad", "afraid": "fear", "fearful": "fear",
    "calm": "neutral",
}


def _canon(emotion: str) -> str:
    key = str(emotion or "").strip().lower()
    return _ALIAS.get(key, key or "neutral")


def _voice_profile(emotion: str) -> dict:
    """从 config_store.list_voice_profiles() 找该情绪映射；异常返回 {}。"""
    try:
        if config_store is None:
            return {}
        for p in config_store.list_voice_profiles() or []:
            if isinstance(p, dict) and p.get("emotion") == emotion:
                return p
    except Exception:
        pass
    return {}


def _endpoint(cfg: dict) -> str:
    """拼接合成接口地址。

    配置里 base_url 只写「服务地址」（如 http://127.0.0.1:8102），
    真正的路径由 endpoint 补，默认 /tts。用户要是填了完整 URL 就原样用。
    """
    raw = str(cfg.get("endpoint") or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    base = str(cfg.get("base_url") or settings.TTS_BASE).rstrip("/")
    path = raw or "/tts"
    if not path.startswith("/"):
        path = "/" + path
    if base.endswith(path):
        return base
    return base + path


def status(force: bool = False) -> dict:
    """给 /health 用：语音引擎现在跑在本进程里，直接查引擎状态。"""
    cfg = _cfg()
    mode = str(cfg.get("mode") or "inproc").strip().lower()
    backend = str(settings.TTS_BACKEND or "")
    if mode == "http":
        try:
            from brain.services import model_status

            remote = model_status.probe_tts(force=force)
        except Exception:
            remote = {"up": False, "detail": "probe failed"}
        return {"mode": "http", "endpoint": _endpoint(cfg),
                "up": bool(remote.get("up")), "detail": str(remote.get("detail") or ""),
                "backend": backend}
    try:
        from brain import tts_server

        state = getattr(tts_server, "_state", {}) or {}
        up = bool(state.get("ready"))
        # detail 取引擎最近一次失败原文；就绪时留空，不再写"引擎已就绪"这类寒暄。
        detail = "" if up else str(state.get("error") or "引擎未加载（首次合成时加载）")
        return {"mode": "inproc", "endpoint": "inproc://tts_server",
                "up": up, "detail": detail,
                "backend": backend or str(state.get("backend") or "")}
    except Exception as exc:
        return {"mode": "inproc", "endpoint": "inproc://tts_server",
                "up": False, "detail": f"引擎不可用：{type(exc).__name__}",
                "backend": backend}


async def synthesize(text: str, emotion: str,
                     reference_audio_id: str | None) -> bytes:
    """返回 WAV 字节。只走真实语音服务；异常由 chat router 转 503。"""
    cfg = _cfg()
    # 控制台里把「语音合成」停用 = 明确没有这个能力，直接报错而不是滴滴声
    try:
        if providers is not None:
            p = providers.get("tts") or {}
            if int(p.get("enabled", 1)) == 0:
                raise RuntimeError("语音合成已在控制台停用")
    except RuntimeError:
        raise
    except Exception:
        pass
    url = _endpoint(cfg)

    emotion = _canon(emotion)
    profile = _voice_profile(emotion)
    try:
        timeout = float(cfg.get("timeout_s") or settings.TTS_TIMEOUT)
    except (TypeError, ValueError):
        timeout = settings.TTS_TIMEOUT
    use_ref = cfg.get("use_reference_audio", True)
    if isinstance(use_ref, str):
        use_ref = use_ref.strip().lower() not in {"0", "false", "no", "off"}
    use_ref = bool(use_ref)
    ref = ""
    if use_ref:
        ref = str(profile.get("reference_audio_id") or reference_audio_id or "")
    payload = {
        "text": text,
        "emotion": emotion,
        "reference_audio_id": ref,
        "format": cfg.get("format", "wav"),
        "sample_rate": cfg.get("sample_rate", 24000),
        "speed": profile.get("speed", cfg.get("speed", 1.0)),
        "volume": profile.get("volume", cfg.get("volume", 1.0)),
        "voice_id": profile.get("voice_id") or cfg.get("default_voice", ""),
        "emotion_vector": profile.get("emotion_vector", []),
        "emotion_control": cfg.get("emotion_control", "reference_audio"),
        "use_reference_audio": use_ref,
    }
    headers: dict[str, str] = {}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + str(cfg["api_key"])

    # 默认进程内：同一套引擎、同一个进程，少一次 HTTP、少一个端口
    if str(cfg.get("mode") or "inproc").strip().lower() != "http":
        try:
            from brain import tts_server

            return await asyncio.to_thread(tts_server.synth_inproc, payload)
        except Exception as exc:
            raise RuntimeError(f"语音引擎合成失败：{exc}") from exc

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers,
                                     trust_env=False) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                # 把语音服务那边的具体原因带出来，否则只看到一句 503 没法排查
                raise RuntimeError(f"{resp.status_code} {resp.text[:200]}")
            if resp.content:
                return resp.content
        raise RuntimeError("tts 服务返回空音频")
    except Exception as exc:
        # 失败就如实报错（chat router 会转成 503 + 原因），不再退回假声音。
        raise RuntimeError(f"语音服务合成失败：{exc}") from exc
