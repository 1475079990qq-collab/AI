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
    """语音引擎已经并进大脑：不再探 :8102，直接看进程内引擎状态。

    没加载权重不等于坏：引擎是首次合成时才加载，这里如实区分
    「就绪」和「还没加载」，而不是把还没加载报成宕机。
    """
    try:
        from brain import tts_server

        state = getattr(tts_server, "_state", {}) or {}
        ready = bool(state.get("ready"))
        return {
            "up": ready,
            "status": 200 if ready else 0,
            "detail": "引擎就绪（进程内）" if ready
                      else f"引擎未加载（首次合成时加载）｜{state.get('error') or ''}",
            "payload": {"backend": state.get("backend", ""), "calls": state.get("calls", 0)},
        }
    except Exception as exc:
        return {"up": False, "status": 0, "detail": f"引擎不可用：{type(exc).__name__}",
                "payload": {}}


def probe_asr(force: bool = False) -> dict[str, Any]:
    base = asr_base()
    if not base:
        return {"up": False, "status": 0, "detail": "未配置 ASR_BASE", "payload": {}}
    if force:
        _cache.pop("asr", None)
    return _cached("asr", base + "/health")


def probe_llm() -> dict[str, Any]:
    """大模型服务形态不定（Ollama / vLLM / llama.cpp），按概率依次试两个端点。

    地址只认一处：providers.llm 的 base_url（现在指向 :8082），
    不再用 config.yaml 里那个 :11434 —— 两处写不同地址就是双轨，
    会出现「状态页说没起、实际在跑」这种假报警。
    """
    base = ""
    try:
        base = str(_provider_cfg(_provider("llm")).get("base_url") or "").rstrip("/")
    except Exception:
        base = ""
    base = base or str(settings.OLLAMA_BASE or "").rstrip("/")
    if not base:
        return {"up": False, "status": 0, "detail": "未配置大模型地址", "payload": {}}
    hit = _cache.get("llm")
    if hit and _fresh(hit):
        return hit[1]
    for path in ("/v1/models", "/api/tags"):
        result = _probe(base + path, timeout=1.2)
        if result["up"]:
            result.setdefault("payload", {})["base"] = base
            _cache["llm"] = (_now(), result)
            return result
    result = _probe(base + "/v1/models", timeout=1.2)
    result.setdefault("payload", {})["base"] = base
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
    """给启动横幅用的一行摘要（按现在的进程布局：判断/合成不再占端口）。"""
    a = probe_asr()
    t = probe_tts()
    l = probe_llm()
    return (f"判断 进程内 | 识别 {_mark(a['up'])}({settings.ASR_PORT}) | "
            f"合成 {_mark(t['up'])}（进程内） | 对话 {_mark(l['up'])}{str(settings.OLLAMA_BASE)}")


def _provider(code: str) -> dict[str, Any]:
    try:
        from brain import config_store

        return config_store.get_provider(code) or {}
    except Exception:
        return {}


def _provider_cfg(prov: dict[str, Any]) -> dict[str, Any]:
    """provider 配置在库里叫 config（老代码里也出现过 config_json），两种都认。"""
    raw = (prov or {}).get("config")
    if raw is None:
        raw = (prov or {}).get("config_json")
    cfg = raw or {}
    if isinstance(cfg, str):
        try:
            import json

            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _provider_mode(prov: dict[str, Any]) -> str:
    return str(_provider_cfg(prov).get("mode") or (prov or {}).get("mode") or "").strip().lower()


def detail_lines() -> list[str]:
    """启动时逐条打印，未启动的带上「该怎么起」。

    这里必须跟真实布局一致：判断已并进进程（onnx），合成是进程内引擎，
    识别可选，对话大模型地址来自 providers.llm 而不是写死的 :11434。
    指向已归档的旧 bat 会让人以为「服务没起是我没双击」，所以不再这么写。
    """
    a = probe_asr()
    t = probe_tts()
    l = probe_llm()
    judge_mode = _provider_mode(_provider("judge")) or "onnx"
    llm_cfg = _provider_cfg(_provider("llm"))
    llm_url = str(llm_cfg.get("base_url") or settings.OLLAMA_BASE)
    llm_model = str(llm_cfg.get("model") or "")

    lines = [
        f"  {'情感判断':<5} 进程内（{judge_mode}）  不占端口，不用单独启动",
        f"  {'语音识别':<5} {_mark(a['up'])}  http://127.0.0.1:{settings.ASR_PORT}"
        + ("  （需要在控制台 providers 里把 asr 打开）" if not a["up"] else ""),
        f"  {'语音合成':<5} {'就绪' if t['up'] else '未加载'}  进程内引擎"
        + ("" if t["up"] else "  首次合成时自动加载，不用起服务"),
        f"  {'对话大模型':<5} {_mark(l['up'])}  {llm_url}" + (f"  {llm_model}" if llm_model else ""),
    ]
    if not l["up"]:
        lines.append("        └ 你 F 盘上的模型自己起起来（llama.cpp/ollama 都行），起来后大脑自动连上")
    return lines
    return lines
