"""情感判断层（独立成层，LLM 禁止输出情感标签）。

引擎优先级由配置中心 judge.engine 决定：

  http  调本地独立判断服务（:8101）。**大脑不加载任何权重**，
        服务没起来就退回关键词规则，对话链路不断；独立启动脚本已归档到 `_旧启动脚本/`。
  auto  判断服务优先 → 服务不在再用本机 ONNX → 都不行用关键词。
  onnx  只认本机 ONNX。
  rule  只用关键词（最快、零依赖）。

为什么要分出去：模型 1GB，大脑是编排层；分成两个进程后模型可以单独启停，
判断服务挂了也只是降级，不会把大脑带崩。见 brain/judge_server.py。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from brain import db
from brain.config import settings
from brain.schemas import EMOTION_LABELS, EmotionResult, new_request_id

try:
    from brain.services import providers
except Exception:  # pragma: no cover - 取决于 S1 是否落地
    providers = None

try:
    from brain.services import sentiment_model
except Exception:  # pragma: no cover
    sentiment_model = None  # type: ignore[assignment]

# 推理依赖不在这里 import：真正的模型加载与分词都在 sentiment_model 里，
# 本层只负责「配置驱动选引擎 + 失败降级 + 写判断日志」。


# 安慰类短语：模型按语气判，见到「担心/怕」就归 fear，但「别担心」是在安慰人。
# 这几句命中就无条件判 care，否则安慰会被回成恐惧，连 TTS 音色都会跟着选错。
_CARE_STRONG: tuple[str, ...] = ("别担心", "别怕", "早点休息", "保重身体", "谢谢你", "感谢你")

# 中文关键词 -> 8 个枚举。并列命中时先比命中词数，再比最长命中词（长词更具体）。
# 注意：
#   * 不要放单字「气」——「天气」「客气」「气氛」全会误判成愤怒；
#   * 「别担心」这类安慰语要靠长词优先压过「担心」，所以 care 词表要收完整短语。
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sad",       ("累", "压力", "难过", "伤心", "委屈", "低落", "疲惫", "撑不住",
                   "睡不着", "睡不好", "失眠", "没精神", "辛苦")),
    ("happy",     ("开心", "高兴", "快乐", "太好了", "舒服")),
    ("angry",     ("生气", "气死", "烦", "恼火", "愤怒", "火大")),
    ("fear",      ("怕", "担心", "害怕", "焦虑", "紧张", "不安", "慌", "恐惧")),
    ("disgust",   ("恶心", "反胃", "讨厌")),
    ("surprised", ("惊讶", "居然", "竟然", "没想到", "震惊")),
    ("care",      ("别担心", "别怕", "关心", "谢谢", "感谢", "保重", "照顾", "早点休息")),
)


def _scan(text: str) -> tuple[str, int]:
    """返回 (label, 命中数)；无命中返回 (neutral, 0)。

    排序键是 (命中数, 最长命中词长度)：
      「别担心」同时命中 care 的「别担心」和 fear 的「担心」，
      命中数都是 1，但 care 那条更长、更具体，应该赢。
    """
    text = text or ""
    best_label, best_hits, best_len = "neutral", 0, 0
    for label, words in _RULES:
        hits, longest = 0, 0
        for w in words:
            if w and w in text:
                hits += 1
                longest = max(longest, len(w))
        if hits > best_hits or (hits == best_hits and hits > 0 and longest > best_len):
            best_label, best_hits, best_len = label, hits, longest
    return best_label, best_hits


def _rule(text: str, history: list[dict]) -> EmotionResult:
    """规则兜底：给合理 confidence / intensity，命中时强度下限 0.7。

    0.7 是为了稳过 recent_emotions 默认 0.6 阈值，保证下一轮能回忆起本轮情绪。
    """
    label, hits = _scan(text)
    if hits:
        confidence = min(0.95, 0.72 + 0.06 * (hits - 1))
        intensity = min(0.95, 0.70 + 0.05 * (hits - 1))
    else:
        # 本句无情绪词：借最近一轮 user 的情绪做弱延续，强度压到阈值以下，
        # 避免把「嗯」「好的」这类回应误记成高强度事件。
        prev = ""
        for turn in reversed(history or []):
            if isinstance(turn, dict) and turn.get("role") == "user":
                prev = str(turn.get("content", ""))
                break
        p_label, p_hits = _scan(prev)
        if p_hits:
            label, confidence, intensity = p_label, 0.55, 0.50
        else:
            label, confidence, intensity = "neutral", 0.40, 0.25
    return EmotionResult(
        label=label, intensity=intensity, confidence=confidence,
        degraded=True, source="rule",
    ).normalized()


def _model_path(cfg: dict | None = None) -> Path | None:
    """模型路径：配置中心 > config.yaml。只检查文件存在，不加载权重。"""
    raw = ""
    if isinstance(cfg, dict):
        raw = str(cfg.get("model_path") or "").strip()
    if not raw:
        raw = settings.JUDGE_MODEL_PATH
    p = Path(raw)
    if not p.is_absolute():
        p = settings.ROOT / p
    return p if p.is_file() else None


def _onnx(text: str, cfg: dict | None = None) -> EmotionResult:
    """真实推理路径：中文 8 类情绪模型（ONNX / CPU）。

    - 分词与推理都在 brain.services.sentiment_model，会话进程内单例复用
    - 模型缺 fear 类，由该模块的关键词修正层补齐
    - 模型只有 8 维 logits、无强度回归头，软标签置信度即 intensity 代理
    - 任何失败都抛异常，让 _decide 降级到关键词规则
    """
    path = _model_path(cfg)
    if path is None or sentiment_model is None:
        raise RuntimeError("onnx model / runtime unavailable")

    out = sentiment_model.predict(path, text)
    if not out:
        raise RuntimeError("onnx inference failed")
    return EmotionResult(
        label=str(out["label"]),
        intensity=float(out["intensity"]),
        confidence=float(out["confidence"]),
        degraded=False,
        source="onnx",
    ).normalized()


def _judge_endpoint(cfg: dict | None = None) -> str:
    """判断服务地址：endpoint 填了就用作完整 URL，否则 JUDGE_BASE + /judge。"""
    raw = ""
    if isinstance(cfg, dict):
        raw = str(cfg.get("endpoint") or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    base = str(settings.JUDGE_BASE or "").rstrip("/")
    if raw:
        return base + "/" + raw.lstrip("/")
    return base + "/judge"


def _http(text: str, cfg: dict | None = None) -> EmotionResult:
    """调独立判断服务。失败一律抛异常，由 _decide 决定退到哪一层。"""
    url = _judge_endpoint(cfg)
    if not url:
        raise RuntimeError("judge endpoint not configured")
    try:
        timeout = float((cfg or {}).get("timeout_s") or 5.0)
    except (TypeError, ValueError):
        timeout = 5.0
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        resp = client.post(url, json={"text": text})
    resp.raise_for_status()
    data = resp.json()
    return EmotionResult(
        label=str(data.get("label") or "neutral"),
        intensity=float(data.get("intensity") or 0.5),
        confidence=float(data.get("confidence") or 0.0),
        degraded=False,
        source="onnx-http",
    ).normalized()


def _http_up(cfg: dict | None = None) -> bool:
    """判断服务是否在跑。

    必须先问这一句再发 /judge：Windows 上连一个没监听的端口要等 2 秒，
    而判断在开流前同步执行——不预检就等于每轮对话白等 2 秒。
    probe 带缓存（在跑 2s / 没起 30s），代价可以忽略。
    """
    url = _judge_endpoint(cfg)
    default_url = str(settings.JUDGE_BASE or "").rstrip("/") + "/judge"
    try:
        from brain.services import model_status

        if url == default_url:
            return bool(model_status.probe_judge().get("up"))
    except Exception:
        pass
    try:
        base = url.rsplit("/judge", 1)[0]
        with httpx.Client(timeout=1.2, trust_env=False) as client:
            return client.get(base + "/health").status_code == 200
    except Exception:
        return False


def _safe_log(*, result: EmotionResult, text: str, history: list[dict],
              latency_ms: float, request_id: str = "") -> None:
    """写判断日志；DB 未初始化 / 任何异常一律吞掉，绝不影响对话。

    request_id 必须由调用方传进来（同一轮对话的那一个），
    否则 judge_logs 与 turn_logs 永远对不上号，「这轮判错没判错」就查不回去。
    """
    try:
        db.log_judge(
            request_id=request_id or new_request_id(),
            input_text=text,
            context_text=json.dumps(history or [], ensure_ascii=False),
            output_label=result.label,
            confidence=result.confidence,
            intensity=result.intensity,
            degraded=int(result.degraded),
            latency_ms=latency_ms,
            source=result.source,
        )
    except Exception:
        pass


def _judge_cfg() -> dict:
    """读 judge provider 配置；异常 / 未落地返回 fallback（回退原行为）。"""
    fallback = {
        "_fallback": True,
        "engine": "rule",
        "model_path": settings.JUDGE_MODEL_PATH,
        "context_turns": settings.JUDGE_CONTEXT_TURNS,
        "confidence_as_intensity": True,
        "intensity_threshold": 0.6,
        "label_map": {},
    }
    try:
        if providers is None:
            return fallback
        raw = providers.get("judge") or {}
        if not isinstance(raw, dict):
            return fallback
        cfg = raw.get("config") if isinstance(raw.get("config"), dict) else {
            k: v for k, v in raw.items()
            if k not in {"code", "name", "category", "enabled", "mode",
                         "version", "updated_at", "fields"}
        }
        merged = dict(fallback)
        # 白名单必须覆盖「代码里真正会读的所有键」：漏了哪个，控制台填了也不生效，
        # 而且不报错——之前 endpoint / timeout_s / confidence_floor 就是这么消失的。
        for key in ("engine", "model_path", "context_turns", "confidence_as_intensity",
                    "intensity_threshold", "label_map", "endpoint", "timeout_s",
                    "confidence_floor", "device"):
            if key in cfg:
                merged[key] = cfg[key]
        merged["_fallback"] = False
        lm = merged.get("label_map")
        if isinstance(lm, str):
            try:
                lm = json.loads(lm or "{}")
            except ValueError:
                lm = {}
        if not isinstance(lm, dict):
            lm = {}
        merged["label_map"] = lm
        return merged
    except Exception:
        return fallback


def _decide(text: str, history: list[dict], cfg: dict) -> EmotionResult:
    engine = str(cfg.get("engine") or "http").strip().lower()
    if cfg.get("_fallback"):
        engine = "http"

    if engine in {"http", "auto"}:
        # 默认路径：只调独立判断服务。
        # auto 是历史名字，现在与 http 等价——**刻意不退回本机 ONNX**：
        # 用户要的是「关掉判断 bat 就等于关掉模型」，大脑偷偷加载 1GB 权重
        # 会让这句话变成假的，还会把首字延迟拖到 4 秒。真要本机模型用 onnx。
        if _http_up(cfg):
            try:
                res = _http(text, cfg)
                return _low_confidence_fallback(text, history, res, cfg)
            except Exception:
                pass
        return _rule(text, history)

    if engine == "onnx":
        if _model_path(cfg) is not None:
            try:
                res = _onnx(text, cfg)
                return _low_confidence_fallback(text, history, res, cfg)
            except Exception:
                pass
        return _rule(text, history)

    # rule / 未知引擎：关键词规则，零依赖
    return _rule(text, history)


def _low_confidence_fallback(text: str, history: list[dict],
                             res: EmotionResult, cfg: dict) -> EmotionResult:
    """模型与关键词的交叉复核。

    两类翻案，都只在证据足够时才动，避免把模型的细粒度判断冲掉：

    1) 低置信度：实测翻车的都是这类——「烦死了，别问了」判 care(0.58)、
       「我不想说话」判 disgust(0.45)。模型在硬猜，关键词反而更准。
    2) 模型判 neutral 但句子里有 ≥2 个明确情绪词：这个模型学的是**语气**
       （平淡/关切/疑问/惊奇），不看情绪内容，所以「最近工作压力很大，
       晚上总睡不好」会被它判成平淡语气 0.97。陪伴场景要的是情绪内容，
       这种时候以关键词为准。门槛定 2 个词是为了放过「不累」「别担心」
       这类一两个词的反向表达。
    """
    try:
        floor = float(cfg.get("confidence_floor") or 0.65)
    except (TypeError, ValueError):
        floor = 0.65

    alt = _rule(text, history)
    _, hits = _scan(text)

    if res.confidence < floor:
        if alt.label != "neutral" and alt.label != res.label:
            # 规则有明确依据且与模型不一致 → 采信规则
            return EmotionResult(
                label=alt.label,
                intensity=max(alt.intensity, res.intensity),
                confidence=max(alt.confidence, res.confidence),
                degraded=False,
                source="onnx+rule",
            ).normalized()
        if alt.label == "neutral":
            # 模型没把握、规则也没找到依据：两边都在猜。
            # 与其把猜测写进长期情绪记忆（后续回复会驴唇不对马嘴），不如记成平淡。
            return EmotionResult(
                label="neutral", intensity=0.30, confidence=res.confidence,
                degraded=False, source="onnx+rule",
            ).normalized()
        return res

    if res.label == "neutral" and hits >= 2 and alt.label != "neutral":
        return EmotionResult(
            label=alt.label,
            intensity=max(alt.intensity, 0.70),
            confidence=max(alt.confidence, res.confidence),
            degraded=False,
            source="onnx+rule",
        ).normalized()

    if res.label != "care" and any(p in text for p in _CARE_STRONG):
        # 安慰/道谢短语：不管模型多有把握，都按 care 处理
        return EmotionResult(
            label="care",
            intensity=max(res.intensity, 0.75),
            confidence=0.80,
            degraded=False,
            source="onnx+rule",
        ).normalized()

    return res


def _apply_label_map(result: EmotionResult, label_map: dict | None) -> EmotionResult:
    if not label_map:
        return result
    mapped = label_map.get(result.label, result.label)
    return EmotionResult(
        label=str(mapped), intensity=result.intensity, confidence=result.confidence,
        degraded=result.degraded, source=result.source,
    ).normalized()


# 同一句话的判定缓存：ONNX 单次约 3 秒，重复问句不该重复等（5 分钟有效、上限 256 条）
_CACHE: dict[str, tuple[float, "EmotionResult"]] = {}
_CACHE_TTL_S = 300.0
_CACHE_MAX = 256


def _cache_key(text: str, history: list[dict], cfg: dict) -> str:
    """键 = 原话 + 引擎/模型 + 最近两轮上下文；任一项变了就不复用旧判断。"""
    tail = ""
    try:
        tail = "|".join(
            f"{str(h.get('role') or '')}:{str(h.get('content') or '')[:60]}"
            for h in list(history or [])[-2:]
        )
    except Exception:
        tail = ""
    return f"{text.strip()}|{cfg.get('engine')}|{cfg.get('model')}|{tail}"


def _ctx_history(history: list[dict], cfg: dict) -> list[dict]:
    """context_turns 只用于日志上下文裁剪，不改动判定结果。"""
    try:
        ct = max(0, int(float(cfg.get("context_turns") or 0)))
    except (TypeError, ValueError):
        ct = 0
    if ct <= 0:
        return []
    return list(history or [])[-(2 * ct):]


def judge(text: str, history: list[dict], request_id: str = "") -> EmotionResult:
    """同步判断情感：配置驱动引擎，任何不可用都降级到关键词规则。

    request_id 由 pipeline 传入，保证 judge_logs 能顺着它找回 turn_logs 那一轮。
    """
    started = time.perf_counter()
    cfg = _judge_cfg()
    key = _cache_key(text, history, cfg)
    hit = _CACHE.get(key)
    if hit is not None and (time.time() - hit[0]) < _CACHE_TTL_S:
        # 同一句话几分钟内又问一遍（问候、复述、测试）：直接复用上次判断。
        # 不是为了省电，是因为 ONNX 单次约 3 秒——重复问句不该让人再等 3 秒。
        result = hit[1].model_copy(deep=True)
        _safe_log(result=result, text=text, history=_ctx_history(history, cfg),
                  latency_ms=(time.perf_counter() - started) * 1000.0,
                  request_id=request_id)
        return result
    result = _decide(text, history, cfg)
    result = _apply_label_map(result, cfg.get("label_map"))
    _CACHE[key] = (time.time(), result.model_copy(deep=True))
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)), None)
    _safe_log(result=result, text=text, history=_ctx_history(history, cfg),
              latency_ms=(time.perf_counter() - started) * 1000.0,
              request_id=request_id)
    return result


def engine_status(force: bool = False) -> dict:
    """控制台「测试」按钮用：说清楚现在实际走的是服务、本机模型还是规则。

    注意这里**不会加载任何权重**，只做文件存在性检查和一次 HTTP 探活，
    所以可以随便调，不会把 1GB 模型拖进大脑进程。
    """
    cfg = _judge_cfg()
    path = _model_path(cfg)
    engine = str(cfg.get("engine") or "http").strip().lower()

    remote = {"up": False, "latency_ms": 0.0, "url": _judge_endpoint(cfg), "detail": ""}
    try:
        from brain.services import model_status

        if _judge_endpoint(cfg).startswith(str(settings.JUDGE_BASE or "\0")):
            remote.update(model_status.probe_judge(force=force))
    except Exception:
        pass

    if engine in {"http", "auto"}:
        effective = "http" if remote.get("up") else "rule"
    elif engine == "onnx":
        effective = "onnx" if path is not None else "rule"
    else:
        effective = engine

    model_info = None
    if path is not None and sentiment_model is not None:
        try:
            model_info = sentiment_model.info(path)
        except Exception:
            model_info = None
    return {
        "engine": engine,
        "effective": effective,
        "endpoint": _judge_endpoint(cfg),
        "remote": {k: remote.get(k) for k in ("up", "latency_ms", "detail")},
        "model_path": str(path) if path else "",
        "model": model_info,
    }
