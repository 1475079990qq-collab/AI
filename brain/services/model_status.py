"""模型服务探活：只发 HTTP，绝不加载任何权重。

为什么单独一层
--------------
模型（判断 / 语音 / 大模型）都由各自的 bat 单独启动，大脑进程只做编排。
大脑启动时要做的事只有一件：**看它们在不在**，然后如实报告。
不在就降级（判断走关键词、语音走内置占位音），而不是把 1GB 权重拖进
大脑进程里——那会让「关掉模型 bat」变成一句空话。

约定
----
* 全部 `trust_env=False`：本机常见系统代理会把 127.0.0.1 也拦掉。
* 2 秒 TTL 缓存：/health 会被客户端反复轮询，不能每次都真发请求。
* 任何异常都返回结构化的 up=False，绝不抛给调用方。
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from brain.config import settings

_TTL_UP = 2.0      # 在跑：2 秒（够新，也不至于每轮都打）
_TTL_DOWN = 30.0   # 没起：30 秒。Windows 上连接一个没监听的端口要等 2 秒
                   # （实测 connect_ex 2039ms），不缓存的话每轮对话都要白等这 2 秒。
_cache: dict[str, tuple[float, dict]] = {}


def _now() -> float:
    return time.monotonic()


def _probe(url: str, timeout: float = 1.5) -> dict[str, Any]:
    started = _now()
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            resp = client.get(url)
        payload: dict[str, Any] = {}
        try:
            body = resp.json()
            if isinstance(body, dict):
                payload = body
        except Exception:
            payload = {}
        ok = 200 <= resp.status_code < 300
        detail = str(payload.get("detail") or payload.get("error") or "")
        return {
            "up": ok,
            "status": resp.status_code,
            "latency_ms": round((_now() - started) * 1000.0, 1),
            "url": url,
            "detail": detail,
            "payload": payload,
        }
    except Exception as exc:
        return {
            "up": False,
            "status": 0,
            "latency_ms": round((_now() - started) * 1000.0, 1),
            "url": url,
            "detail": f"{type(exc).__name__}",
            "payload": {},
        }


def _fresh(hit: tuple[float, dict]) -> bool:
    ttl = _TTL_UP if (hit[1] or {}).get("up") else _TTL_DOWN
    return _now() - hit[0] < ttl


def _cached(key: str, url: str, timeout: float = 1.5) -> dict[str, Any]:
    hit = _cache.get(key)
    if hit and _fresh(hit):
        return hit[1]
    result = _probe(url, timeout)
    _cache[key] = (_now(), result)
    return result


def clear_cache() -> None:
    _cache.clear()


def judge_base() -> str:
    return str(settings.JUDGE_BASE or "").rstrip("/")


def tts_base() -> str:
    return str(settings.TTS_BASE or "").rstrip("/")


def asr_base() -> str:
    return str(settings.ASR_BASE or "").rstrip("/")


def probe_judge(force: bool = False) -> dict[str, Any]:
    base = judge_base()
    if not base:
        return {"up": False, "status": 0, "detail": "未配置 JUDGE_BASE", "payload": {}}
    if force:
        _cache.pop("judge", None)
    return _cached("judge", base + "/health")


def probe_tts(force: bool = False) -> dict[str, Any]:
    base = tts_base()
    if not base:
        return {"up": False, "status": 0, "detail": "未配置 TTS_BASE", "payload": {}}
    if force:
        _cache.pop("tts", None)
    return _cached("tts", base + "/health")


def probe_asr(force: bool = False) -> dict[str, Any]:
    base = asr_base()
    if not base:
        return {"up": False, "status": 0, "detail": "未配置 ASR_BASE", "payload": {}}
    if force:
        _cache.pop("asr", None)
    return _cached("asr", base + "/health")


def probe_llm() -> dict[str, Any]:
    """大模型服务形态不定（Ollama / vLLM / llama.cpp），按概率依次试两个端点。"""
    base = str(settings.OLLAMA_BASE or "").rstrip("/")
    if not base:
        return {"up": False, "status": 0, "detail": "未配置 OLLAMA_BASE", "payload": {}}
    hit = _cache.get("llm")
    if hit and _fresh(hit):
        return hit[1]
    for path in ("/v1/models", "/api/tags"):
        result = _probe(base + path, timeout=1.2)
        if result["up"]:
            _cache["llm"] = (_now(), result)
            return result
    result = _probe(base + "/v1/models", timeout=1.2)
    _cache["llm"] = (_now(), result)
    return result


def probe_all() -> dict[str, Any]:
    return {
        "judge": probe_judge(),
        "asr": probe_asr(),
        "tts": probe_tts(),
        "llm": probe_llm(),
    }


def _mark(up: bool) -> str:
    return "已启动" if up else "未启动"


def summary_line() -> str:
    """给启动横幅用的一行摘要。"""
    j = probe_judge()
    a = probe_asr()
    t = probe_tts()
    l = probe_llm()
    return (f"判断 {_mark(j['up'])}({settings.JUDGE_PORT}) | "
            f"识别 {_mark(a['up'])}({settings.ASR_PORT}) | "
            f"合成 {_mark(t['up'])}({settings.TTS_PORT}) | "
            f"对话 {_mark(l['up'])}({settings.OLLAMA_BASE})")


def detail_lines() -> list[str]:
    """启动时逐条打印，未启动的带上「该怎么起」。"""
    j = probe_judge()
    a = probe_asr()
    t = probe_tts()
    l = probe_llm()
    rows = [
        ("情感判断", j, f"http://127.0.0.1:{settings.JUDGE_PORT}", "4-启动情感判断.bat"),
        ("语音识别", a, f"http://127.0.0.1:{settings.ASR_PORT}", "7-启动语音识别.bat"),
        ("语音合成", t, f"http://127.0.0.1:{settings.TTS_PORT}", "5-启动语音合成.bat"),
        ("对话大模型", l, str(settings.OLLAMA_BASE), "0-启动大模型.bat"),
    ]
    lines = []
    for name, info, url, how in rows:
        if info["up"]:
            payload = info.get("payload") or {}
            note = "（模型未就绪）" if payload.get("ready") is False else ""
            lines.append(f"  {name:<5} 已启动  {url}  {info['latency_ms']:.0f}ms{note}")
            if note:
                why = str(payload.get("detail") or "").strip()
                # 只取第一句，把「怎么修」的长提示留给服务自己的窗口
                why = why.split("。")[0]
                if ":" in why[:24]:
                    why = why.split(":", 1)[1].strip()
                if why:
                    lines.append(f"        └ {why[:80]}")
        else:
            lines.append(f"  {name:<5} 未启动  {url}  → 需要时双击 {how}")
    return lines
