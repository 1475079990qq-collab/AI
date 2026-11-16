"""LLM 流式生成：全部走真实模型服务（OpenAI 兼容接口）；没有模拟回复。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

from brain.config import settings

log = logging.getLogger("brain.llm")

# 配置中心适配层（S1 新建）。未落地 / 任何异常一律降级回退原 settings 行为。
try:
    from brain.services import providers
except Exception:  # pragma: no cover - 取决于 S1 是否落地
    providers = None


class LLMUnavailable(Exception):
    """Ollama 不可达 / 超时 / 流式响应异常；由 pipeline 转成 error 事件。"""


def _num(params: dict, key: str, default, cast):
    try:
        return cast(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _active_code() -> str:
    """当前用哪张模型卡：备用 4B 卡打开就接管，否则用主模型卡。

    判定只看配置，不做任何探测：运行方式不是 off 且没被停用 = 用户明确要用它。
    任何读取异常都回退主模型，绝不因为配置读失败把对话打挂。
    """
    if providers is None:
        return "llm"
    try:
        alt = providers.get("llm_alt") or {}
        try:
            enabled = int(alt.get("enabled", 0) or 0)
        except (TypeError, ValueError):
            enabled = 0
        if enabled and str(alt.get("mode") or "off").strip().lower() != "off":
            return "llm_alt"
    except Exception:
        return "llm"
    return "llm"


def _cfg() -> dict:
    """读当前模型卡配置；兼容 {config:{...}} 与顶层字段两种形状；异常返回 {}。"""
    try:
        if providers is None:
            return {}
        raw = providers.get(_active_code()) or {}
        if not isinstance(raw, dict):
            return {}
        inner = raw.get("config")
        if isinstance(inner, dict):
            return dict(inner)
        return {k: v for k, v in raw.items()
                if k not in {"code", "name", "category", "enabled", "mode",
                             "version", "updated_at", "fields"}}
    except Exception as exc:
        # 读卡异常不能静默：留痕，下游会落到 yaml 兜底地址/模型。
        log.warning("读取模型卡配置异常，将回退 yaml 兜底（%s: %s）",
                    type(exc).__name__, exc)
        return {}


def _pick(cfg: dict, params: dict, key: str, default, cast):
    """参数优先级：provider config > runtime params > settings 默认；异常逐级回退。

    控制台「模型服务」卡是模型参数的唯一入口，必须优先；runtime 只作老数据的兜底。
    （原来反过来，导致模型卡里改的 temperature / num_ctx 永远不生效。）
    """
    for src in (cfg, params):
        try:
            if isinstance(src, dict) and src.get(key) is not None:
                return cast(src[key])
        except (TypeError, ValueError):
            continue
    return default


async def _ollama_stream(messages: list[dict], params: dict) -> AsyncIterator[str]:
    cfg = _cfg()
    params = params or {}
    # api_style 读出作语义保留：本链路统一走 OpenAI 兼容端点（原契约行为）。
    _api_style = str(cfg.get("api_style") or "openai")
    card_url = str(cfg.get("base_url") or "").rstrip("/")
    base_url = card_url or str(settings.OLLAMA_BASE or "").rstrip("/")
    url = base_url + "/v1/chat/completions"
    # 主模型；__model_override 只由 stream() 的降级路径塞进来
    card_model = str(cfg.get("model") or "").strip()
    model = str(params.get("__model_override") or card_model
                or cfg.get("fallback_model") or settings.LLM_MODEL)

    # 兜底留痕：地址/型号任一来自 yaml 默认值，都打 warning，避免悄悄换模型没人知道。
    fallback_reasons: list[str] = []
    if not cfg:
        fallback_reasons.append("模型卡为空或读取异常")
    if not card_url:
        fallback_reasons.append(f"base_url 未配置，回退 {settings.OLLAMA_BASE}")
    if not card_model and not params.get("__model_override"):
        fallback_reasons.append(f"model 未配置，回退 {settings.LLM_MODEL}")
    if fallback_reasons:
        log.warning("LLM 走 yaml 兜底：%s；实际生效 url=%s model=%s",
                    "；".join(fallback_reasons), base_url, model)
    try:
        timeout_s = float(cfg.get("timeout_s") or 60.0)
    except (TypeError, ValueError):
        timeout_s = 60.0
    try:
        num_gpu = int(float(cfg.get("num_gpu") or 999))
    except (TypeError, ValueError):
        num_gpu = 999
    stream_flag = cfg.get("stream", True)
    if isinstance(stream_flag, str):
        stream_flag = stream_flag.strip().lower() not in {"0", "false", "no", "off"}
    stream_flag = bool(stream_flag)
    # force_gpu 读出作语义保留（GPU 溢出检测属服务端职责，大脑只透传参数）。
    _force_gpu = cfg.get("force_gpu", True)
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream_flag,
        "temperature": _pick(cfg, params, "temperature", settings.LLM_TEMPERATURE, float),
        "top_p": _pick(cfg, params, "top_p", settings.LLM_TOP_P, float),
        "num_ctx": _pick(cfg, params, "num_ctx", settings.LLM_NUM_CTX, int),
        "max_tokens": _pick(cfg, params, "max_tokens", settings.LLM_MAX_TOKENS, int),
    }
    # Ollama 专有 options（num_gpu / num_thread 等）——只有 api_style 是 ollama 才塞进去。
    # vLLM / OpenAI 兼容后端不认 options 字段，传了也不会报错，但为了协议干净，按 style 分支。
    api_style = str(cfg.get("api_style") or "openai").strip().lower()
    if api_style in ("ollama",):
        try:
            if cfg.get("force_gpu"):
                payload["options"] = {"num_gpu": num_gpu}
            else:
                payload["options"] = {"num_gpu": 0}   # force_gpu=False 时告诉 Ollama 用 CPU
        except Exception:
            pass
    headers: dict[str, str] = {}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + str(cfg["api_key"])
    timeout = httpx.Timeout(timeout_s, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            # httpx 流式传输（等价 stream=True），逐行读 SSE。
            async with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            delta = obj["choices"][0]["delta"].get("content")
                            if delta:
                                yield delta
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                            continue
                    elif not stream_flag:
                        # 非流式：单行完整 JSON 也解析，避免拿到空回复。
                        try:
                            obj = json.loads(line)
                            delta = obj["choices"][0]["message"].get("content")
                            if delta:
                                yield delta
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                            continue
    except (httpx.HTTPError, OSError) as exc:
        # 不可达 / 超时 / 非 2xx 一律转成 LLMUnavailable，不抛 500。
        raise LLMUnavailable(str(exc)) from exc


async def stream(messages: list[dict], params: dict) -> AsyncIterator[str]:
    """异步生成器：逐 token yield。只走真实模型服务，没有模拟分支。

    fallback_model 现在是**真的备用**：主模型连不上 / 超时 / 报错，
    且在还没吐出任何字之前，自动换备用模型再试一次。
    （以前它只是「model 没配时的默认值」，OOM 了照样断，等于没有。）
    """
    params = params or {}
    # 控制台里把「对话大模型」停用 = 明确告知没有这个能力，不再偷偷造假回复
    try:
        if providers is not None:
            p = providers.get(_active_code()) or {}
            if int(p.get("enabled", 1)) == 0:
                raise LLMUnavailable("对话大模型已在控制台停用")
    except LLMUnavailable:
        raise
    except Exception:
        pass

    cfg = _cfg()
    primary = str(cfg.get("model") or "").strip()
    backup = str(cfg.get("fallback_model") or "").strip()
    emitted = False
    try:
        async for token in _ollama_stream(messages, params):
            emitted = True
            yield token
        return
    except LLMUnavailable as exc:
        # 已经吐过字就不能重来：换模型重生成会把前半句接成两段。
        if emitted or not backup or backup == primary:
            raise
        log.warning("主模型 %s 失败（%s），改用备用模型 %s 重试一次", primary, exc, backup)
    async for token in _ollama_stream(messages, {**params, "__model_override": backup}):
        yield token
