"""中文 8 类情绪分类模型（ONNX / CPU 推理）。

模型：Johnson8187/Chinese-Emotion-Small
  DeBERTa-v2 12 层 / hidden 768 / 词表 251k，FP32 ONNX 约 1.06 GB
  输出 8 类：平淡 / 关切 / 开心 / 愤怒 / 悲伤 / 疑问 / 惊奇 / 厌恶

三条设计约束，改动前请先读：
1. 只用 CPUExecutionProvider。显存留给 LLM 与 TTS，8GB 卡上不允许再挤。
2. 会话与分词器是**进程内单例**。InferenceSession 构造要读 1GB 权重，
   每次调用新建会让单轮延迟从 60ms 涨到 1s 以上。
3. 任何异常一律返回 None，由 judge.py 降级到关键词规则，绝不影响对话链路。

标签映射差异（重要）：
  模型没有 fear，而有 questioning。fear 由 judge.py 的关键词修正层补齐，
  questioning 默认归到 neutral（陪伴场景里反问句通常就是平淡语气）。
"""
from __future__ import annotations

import threading
from pathlib import Path

# 模型原始 id2label（顺序即模型输出 logits 下标，勿调换）
MODEL_ID2LABEL: tuple[str, ...] = (
    "neutral",    # 0 平淡語氣
    "care",       # 1 關切語調
    "happy",      # 2 開心語調
    "angry",      # 3 憤怒語調
    "sad",        # 4 悲傷語調
    "neutral",    # 5 疑問語調 -> 无对应枚举，归平淡
    "surprised",  # 6 驚奇語調
    "disgust",    # 7 厭惡語調
)

# 模型缺 fear 这一类，用高精度关键词补；命中才覆盖，不命中保持模型结论。
_FEAR_HINTS: tuple[str, ...] = ("怕", "担心", "害怕", "焦虑", "紧张", "不安", "慌", "恐惧")

_MAX_LEN = 48          # 与导出时的静态长度一致，改这里必须重新导出 ONNX
_INTRA_THREADS = 4     # 8 核实测：1→136ms 2→95ms 4→75ms 8→115ms（超线程争抢反而更慢）

_lock = threading.Lock()
_session = None        # onnxruntime.InferenceSession
_tokenizer = None      # tokenizers.Tokenizer
_input_names: tuple[str, ...] = ()
_loaded_path = ""
_load_error = ""


def _imports():
    """延迟导入：未装 onnxruntime / tokenizers 时本模块可正常 import。"""
    import onnxruntime as ort
    from tokenizers import Tokenizer
    return ort, Tokenizer


def _resolve(model_path: str | Path) -> tuple[Path, Path]:
    """返回 (onnx 路径, tokenizer.json 路径)。同目录找不到分词器就往上找一层。"""
    p = Path(model_path)
    if not p.is_absolute():
        from brain.config import ROOT
        p = ROOT / p
    tok = p.parent / "tokenizer.json"
    return p, tok


def available(model_path: str | Path) -> bool:
    """轻量可用性检查：文件在且依赖在，不加载权重。"""
    onnx_path, tok_path = _resolve(model_path)
    if not onnx_path.is_file() or not tok_path.is_file():
        return False
    try:
        _imports()
        return True
    except Exception:
        return False


def load(model_path: str | Path):
    """加载（或复用）会话。返回 (session, tokenizer)；失败抛异常由调用方兜。"""
    global _session, _tokenizer, _input_names, _loaded_path, _load_error

    onnx_path, tok_path = _resolve(model_path)
    key = str(onnx_path)
    with _lock:
        if _session is not None and _loaded_path == key:
            return _session, _tokenizer

        ort, Tokenizer = _imports()
        try:
            so = ort.SessionOptions()
            so.intra_op_num_threads = _INTRA_THREADS
            so.inter_op_num_threads = 1
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess = ort.InferenceSession(
                str(onnx_path), sess_options=so, providers=["CPUExecutionProvider"]
            )
            tok = Tokenizer.from_file(str(tok_path))
            # 静态图：截断到 48 + 补齐到 48，形状与导出时完全一致
            tok.enable_truncation(max_length=_MAX_LEN)
            tok.enable_padding(length=_MAX_LEN)

            _session, _tokenizer = sess, tok
            _input_names = tuple(i.name for i in sess.get_inputs())
            _loaded_path = key
            _load_error = ""
            return sess, tok
        except Exception as exc:  # 记下原因，供 /health 与控制台展示
            _session, _tokenizer = None, None
            _loaded_path = ""
            _load_error = f"{type(exc).__name__}: {exc}"
            raise


def load_error() -> str:
    return _load_error


def is_loaded(model_path: str | Path) -> bool:
    return _session is not None and _loaded_path == str(_resolve(model_path)[0])


def _softmax(xs: list[float]) -> list[float]:
    m = max(xs)
    es = [pow(2.718281828459045, x - m) for x in xs]
    s = sum(es)
    return [e / s for e in es]


def predict(model_path: str | Path, text: str) -> dict | None:
    """返回 {label, confidence, intensity, probs}；不可用/异常返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        sess, tok = load(model_path)
        import numpy as np

        enc = tok.encode(text)
        ids = list(enc.ids) or [0]
        mask = list(enc.attention_mask) or [1] * len(ids)
        arr_ids = np.asarray([ids], dtype=np.int64)
        arr_mask = np.asarray([mask], dtype=np.int64)

        feeds: dict[str, object] = {}
        for name in _input_names:
            low = name.lower()
            if "mask" in low:
                feeds[name] = arr_mask
            # DeBERTa-v2 的 type_vocab_size=0，若图里带了也不要喂 token_type_ids
            elif "input_ids" in low or low == "ids":
                feeds[name] = arr_ids
        if not feeds:
            raise RuntimeError(f"无法识别的模型输入：{_input_names}")

        logits = np.asarray(sess.run(None, feeds)[0]).reshape(-1).tolist()
        probs = _softmax(logits)
        idx = int(max(range(len(probs)), key=lambda i: probs[i]))
        label = MODEL_ID2LABEL[idx] if idx < len(MODEL_ID2LABEL) else "neutral"
        confidence = round(probs[idx], 4)

        # 模型缺 fear：高精度关键词命中即覆盖，置信度取模型次高值作参考。
        # 例外是安慰语——「别担心」「别怕」里也含 fear 词，但那是在安慰人，
        # 交给 judge.py 的 care 规则判，这里不能抢先标成恐惧。
        if any(h in text for h in _FEAR_HINTS) and not any(
            p in text for p in ("别担心", "别怕", "不用担心", "不要怕")
        ):
            label = "fear"
            confidence = max(confidence, 0.72)

        # 强度不能直接拿置信度：模型对「我有点累」这类日常句往往只给 0.4~0.5，
        # 直接当强度会跌破 0.6 记忆阈值，等于判断出来了却记不住。
        # 这里做成分段映射：明确情绪抬到阈值以上，neutral 压到阈值以下。
        if label == "neutral":
            intensity = round(min(0.50, 0.24 + 0.26 * confidence), 4)
        else:
            intensity = round(min(0.95, 0.60 + 0.35 * confidence), 4)

        return {
            "label": label,
            "confidence": confidence,
            "intensity": intensity,
            "raw_index": idx,
            "probs": [round(p, 4) for p in probs],
        }
    except Exception:
        return None


def info(model_path: str | Path) -> dict:
    """给控制台/健康检查用的运行信息。"""
    onnx_path, tok_path = _resolve(model_path)
    return {
        "path": str(onnx_path),
        "tokenizer": str(tok_path),
        "exists": onnx_path.is_file() and tok_path.is_file(),
        "size_mb": round(onnx_path.stat().st_size / 1048576, 1) if onnx_path.is_file() else 0,
        "loaded": _session is not None,
        "inputs": list(_input_names),
        "error": _load_error,
        "labels": list(MODEL_ID2LABEL),
    }
