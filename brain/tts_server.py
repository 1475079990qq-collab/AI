"""语音合成模型独立服务（默认 :8102）。

和判断服务一样：**模型自己一个进程，由 5-启动语音合成.bat 单独启动**，
大脑只负责把文字发过来拿 WAV，不 import torch、不占显存。

四种后端，改 config.yaml 的 TTS_BACKEND 切换：

  gpt_sovits  GPT-SoVITS 整合包的 api_v2.py（:9880）。**推荐**：它自带 Python
              环境和 torch，不用碰本工程的 .venv，装不动依赖时走这条
  indextts    IndexTTS-2 本地部署（参考音频克隆 + 8 维情感向量），依赖要自己装
  proxy       转发给已经在跑的服务，原样透传 JSON
  openai      OpenAI 兼容的 /v1/audio/speech

接口（大脑侧 tts.py 按这个契约调用）
------------------------------------
  GET  /health  → 后端、设备、模型目录、是否就绪、参考音频数量
  GET  /voices  → 可用参考音频列表
  POST /tts     → JSON {text, emotion, reference_audio_id, ...} → audio/wav

模型没装好时服务照常启动，/health 会说明缺什么，/tts 返回 503 带原因。
大脑收到 503 会如实提示语音服务不可用。
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from brain import log_setup
from brain.config import settings

# IndexTTS-2 的 8 维情感向量顺序（官方文档）：
#   [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]
# 我们自己的 8 个枚举到它的映射。care（关切）没有对应维度，用「平静为主 +
# 一点开心」表达，听感是温和而不是平淡。
#
# GPT-SoVITS 不认 emotion_vector，情绪只能靠「参考音频 + 语速」表达。
# 只靠语速听起来还是平，所以再补一层音高：先把语速反向补偿掉，再整体重采样，
# 效果是「只改音高、不改快慢」——开心更亮更轻，难过更低沉。
# (整体音高, 句内升降, 颤动幅度)
#   pitch   整体升降调：开心更高更亮，难过更低更沉
#   contour 句内走势：正数=越说越高（开心/惊讶），负数=越说越低（难过/生气）
#   vibrato 音高颤动：害怕会发抖，开心带一点轻快
# 只靠 pitch 是「整句话被调了音」，听感还是平的；加上走势和颤动才像人。
_EMO_PROSODY: dict[str, tuple[float, float, float]] = {
    "happy":     (1.14, 0.12, 0.010),
    "sad":       (0.88, -0.14, 0.0),
    "angry":     (1.06, -0.05, 0.012),
    "surprised": (1.18, 0.16, 0.0),
    "fear":      (1.08, -0.06, 0.030),
    "disgust":   (1.02, -0.08, 0.0),
    # care 原来整体压低、尾音一路往下，听起来是「有气无力」而不是温柔；
    # 改成略低 + 轻微起伏，保留温度但不阴沉。
    "care":      (0.97, -0.02, 0.0),
    # neutral 原来完全不加修饰，日常对话里最常出现，听起来就是一平到底的机器音；
    # 给它很小的一点起伏，避免平淡又不会显得夸张。
    "neutral":   (1.02, 0.04, 0.0),
}

# 音节级重音：GPT-SoVITS 只靠同一段参考音频，整句只有「整体调了个音」，
# 听起来还是平。这里按能量峰值切出音节，给相邻音节不同音高偏移，
# 让句子内部有起伏（而不是整句统一升降）。数值是相对音高比例。
_EMO_ACCENT: dict[str, list[float]] = {
    "happy":     [0.16, -0.06, 0.13, -0.03],
    "sad":       [0.02, -0.07, -0.13, -0.19],
    "angry":     [0.20, 0.04, -0.09, -0.15],
    "surprised": [0.28, 0.14, 0.06, 0.0],
    "fear":      [0.04, 0.10, -0.06, 0.07],
    "disgust":   [0.08, -0.10, 0.03, -0.12],
    "care":      [0.05, -0.02, 0.01, -0.06],
    "neutral":   [0.06, -0.02, 0.03, -0.05],
}

# 外部（老客户端 / 别的模型）可能送 joy、anger、surprise；不收敛就会查不到
# 情绪参数，退回完全平调。大脑侧已收敛一次，这里再兜一层。
_EMO_ALIAS: dict[str, str] = {
    "joy": "happy", "happiness": "happy", "excited": "happy", "excitement": "happy",
    "anger": "angry", "mad": "angry", "annoyed": "angry",
    "surprise": "surprised", "shocked": "surprised",
    "sadness": "sad", "down": "sad", "gloomy": "sad",
    "afraid": "fear", "fearful": "fear", "scared": "fear",
    "trust": "care", "caring": "care", "gentle": "care", "warm": "care",
    "disgusted": "disgust", "calm": "neutral", "plain": "neutral",
}


def _canon_emotion(emotion: str) -> str:
    key = str(emotion or "").strip().lower()
    return _EMO_ALIAS.get(key, key or "neutral")


# GPT-SoVITS 的 api_v2 会把文本往 GBK 控制台打日志，遇到 emoji 直接抛
# `'gbk' codec can't encode`；只剩标点/表情的句子它又会报「请输入有效文本」。
# 这两种都会让 /tts 返回 503，大脑侧就提示「语音服务合成失败」。
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # 表情、图形、旗帜
    "\U00002600-\U000027BF"   # 杂项符号、装饰符号
    "\U0001F1E6-\U0001F1FF"   # 区域旗帜
    "\u2190-\u21FF\u2300-\u23FF\u25A0-\u25FF\u2B00-\u2BFF"
    "\uFE0F\u200D\u20E3"
    "]"
)
# Markdown 标记、代码、链接、HTML 标签：读出来没意义，还会干扰断句
_MD_RE = re.compile(
    r"```[\s\S]*?```|`[^`]*`|!?\[[^\]]*\]\([^)]*\)|https?://\S+|<[^>\n]{1,60}>|[*_#>|~^]"
)
_CTRL_RE = re.compile(r"[\u0000-\u001f\u007f\u200b-\u200f\ufeff]")


def _clean_text(text: str) -> str:
    """去掉表情、Markdown、链接、控制字符，只留能朗读的内容。"""
    s = _EMOJI_RE.sub("", str(text or ""))
    s = _MD_RE.sub("", s)
    s = _CTRL_RE.sub(" ", s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def _speakable(text: str) -> bool:
    """至少有一个汉字/字母/数字才算能读，纯标点纯符号直接拒绝。"""
    return re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text) is not None


def _accent_curve(mono, sr: int, n_out: int, pattern: list[float]):
    """按能量峰值切音节，返回逐采样点的音高偏移（±0.3 以内）。

    取 10ms 能量包络 → 找局部峰值（最小间隔 120ms）→ 峰值处套用 emotion 的
    重音模式 → 峰间线性插值 + 平滑。音节太少或静音则返回 None，退回原逻辑。
    """
    import numpy as np

    hop = max(1, int(sr * 0.01))
    win = max(hop, int(sr * 0.03))
    if len(mono) < win * 8 or not pattern:
        return None
    nf = 1 + (len(mono) - win) // hop
    env = np.array([np.sqrt(np.mean(mono[i * hop:i * hop + win] ** 2))
                    for i in range(nf)])
    if env.max() <= 1e-5:
        return None
    env = np.convolve(env, np.ones(5) / 5.0, mode="same")
    thr = env.max() * 0.18
    min_gap = max(2, int(0.12 / 0.01))
    peaks: list[int] = []
    for i in range(1, nf - 1):
        if env[i] >= env[i - 1] and env[i] >= env[i + 1] and env[i] > thr:
            if peaks and i - peaks[-1] < min_gap:
                if env[i] > env[peaks[-1]]:
                    peaks[-1] = i
            else:
                peaks.append(i)
    if len(peaks) < 3:
        return None
    vals = np.array([pattern[j % len(pattern)] for j in range(len(peaks))])
    off = np.zeros(nf)
    off[peaks] = vals
    xs = np.arange(nf)
    # 峰间线性过渡，再做一次平滑，避免音高台阶
    off = np.interp(xs, peaks, off[peaks])
    off = np.convolve(off, np.ones(7) / 7.0, mode="same")
    # 去掉均值：归一化会把「平均速率」拉回 1，留着均值只会让整句被统一调低/调高，
    # 和 _EMO_PROSODY 的整体音高打架。这里只保留音节之间的相对起伏。
    off = off - float(off.mean())
    t = np.linspace(0.0, float(nf - 1), n_out)
    return np.interp(t, xs, off)


def _apply_prosody(wav: bytes, pitch: float, gain: float,
                   contour: float = 0.0, vibrato: float = 0.0,
                   accent: list[float] | None = None) -> bytes:
    """音高/走势/颤动/音节重音/音量后处理；只支持 16bit PCM wav，其它原样返回。"""
    if (abs(pitch - 1.0) < 0.01 and abs(gain - 1.0) < 0.01
            and abs(contour) < 0.01 and vibrato < 0.005 and not accent):
        return wav
    import io
    import wave

    import numpy as np

    with wave.open(io.BytesIO(wav), "rb") as src:
        params = src.getparams()
        raw = src.readframes(params.nframes)
    if params.sampwidth != 2 or not raw:
        return wav

    data = np.frombuffer(raw, dtype=np.int16)
    if params.nchannels > 1:
        data = data.reshape(-1, params.nchannels)
    n = data.shape[0]

    if abs(contour) < 0.01 and vibrato < 0.005 and not accent:
        # 纯整体升降调：等间隔重采样，最省事也最稳
        new_n = max(1, int(round(n / pitch)))
        idx = np.linspace(0.0, float(n - 1), new_n)
    else:
        # 采样速率随时间变化：输出长度按整体音高定（保证快慢只由 profile 决定），
        # 走势和颤动只改变「每个输出点读源音频的位置」，不影响总时长。
        new_n = max(1, int(round(n / max(float(pitch), 0.05))))
        u = np.linspace(-0.5, 0.5, new_n)
        rate = np.full(new_n, float(pitch), dtype=np.float64)
        if abs(contour) >= 0.01:
            rate *= 1.0 + contour * u
        if vibrato >= 0.005:
            t = np.arange(new_n) / float(params.framerate or 24000)
            rate *= 1.0 + vibrato * np.sin(2.0 * np.pi * 5.0 * t)
        if accent:
            mono = (data.astype(np.float64).mean(axis=1) if data.ndim > 1
                    else data.astype(np.float64))
            acc = _accent_curve(mono, int(params.framerate or 24000), new_n, accent)
            if acc is not None:
                rate *= 1.0 + acc
        pos = np.cumsum(rate)
        pos *= float(n - 1) / pos[-1]
        idx = pos

    if data.ndim == 1:
        out = np.interp(idx, np.arange(n), data.astype(np.float64))
    else:
        cols = [np.interp(idx, np.arange(n), data[:, c].astype(np.float64))
                for c in range(params.nchannels)]
        out = np.stack(cols, axis=1).reshape(-1)
    out = np.clip(out * gain, -32768, 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setparams(params._replace(nframes=new_n))
        dst.writeframes(out.tobytes())
    return buf.getvalue()


def _trim_silence(wav: bytes, thresh_ratio: float = 0.015,
                  pad_ms: float = 70.0, fade_ms: float = 8.0) -> bytes:
    """切掉合成音频首尾的静音，并在切口做几毫秒淡入淡出。

    GPT-SoVITS 每段都会带 0.2~1 秒头尾静音。手机端是一句一段地播，
    这些静音累加起来就是「一句一顿、听着老是断」。同时首尾直接切会爆音，
    所以两侧各留一点余量再做线性淡入淡出。
    """
    import io
    import wave

    import numpy as np

    try:
        with wave.open(io.BytesIO(wav), "rb") as src:
            params = src.getparams()
            raw = src.readframes(params.nframes)
    except Exception:
        return wav
    if params.sampwidth != 2 or not raw:
        return wav

    data = np.frombuffer(raw, dtype=np.int16)
    ch = max(1, int(params.nchannels))
    frames = data.reshape(-1, ch)
    if frames.shape[0] < 8:
        return wav

    sr = int(params.framerate or 24000)
    mono = np.abs(frames.astype(np.float32)).max(axis=1)
    peak = float(mono.max())
    if peak <= 0:
        return wav

    # 10ms 窗取平均能量：单个尖峰（点击声、气口）不会被当成语音起点
    win = max(1, int(sr * 0.01))
    n_win = frames.shape[0] // win
    if n_win < 3:
        return wav
    env = mono[:n_win * win].reshape(n_win, win).mean(axis=1)
    active = np.where(env > peak * thresh_ratio)[0]
    if active.size == 0:
        return wav

    pad = int(sr * pad_ms / 1000.0)
    start = max(0, int(active[0]) * win - pad)
    end = min(frames.shape[0], (int(active[-1]) + 1) * win + pad)
    if end - start < int(sr * 0.05) or (start <= 0 and end >= frames.shape[0]):
        return wav

    cut = frames[start:end].astype(np.float32)
    n_fade = min(cut.shape[0] // 2, max(1, int(sr * fade_ms / 1000.0)))
    if n_fade > 1:
        ramp = np.linspace(0.0, 1.0, n_fade, dtype=np.float32)[:, None]
        cut[:n_fade] *= ramp
        cut[-n_fade:] *= ramp[::-1]

    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setparams(params._replace(nframes=cut.shape[0]))
        dst.writeframes(np.clip(cut, -32768, 32767).astype(np.int16).tobytes())
    return buf.getvalue()


_EMO_VECTOR: dict[str, list[float]] = {
    "happy":     [0.90, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.10],
    "angry":     [0.00, 0.90, 0.00, 0.00, 0.00, 0.00, 0.00, 0.10],
    "sad":       [0.00, 0.00, 0.85, 0.00, 0.00, 0.10, 0.00, 0.05],
    "fear":      [0.00, 0.00, 0.00, 0.90, 0.00, 0.00, 0.00, 0.10],
    "disgust":   [0.00, 0.00, 0.00, 0.00, 0.90, 0.00, 0.00, 0.10],
    "surprised": [0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.90, 0.10],
    "care":      [0.30, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.70],
    "neutral":   [0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 1.00],
}

_AUDIO_EXT = (".wav", ".mp3", ".flac", ".m4a", ".ogg")
_state: dict = {"ready": False, "error": "", "backend": "", "loaded_ms": 0, "calls": 0}


def _fix_console() -> None:
    """bat 里用 PYTHONIOENCODING 定了编码(GBK)，这里就别再改成 UTF-8 了。"""
    if os.environ.get("PYTHONIOENCODING"):
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass


def _port_busy(port: int) -> bool:
    sock = socket.socket()
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        sock.close()


class TTSRequest(BaseModel):
    text: str = ""
    emotion: str = "neutral"
    reference_audio_id: str = ""
    format: str = "wav"
    sample_rate: int = 24000
    speed: float = 1.0
    volume: float = 1.0
    voice_id: str = ""
    emotion_vector: list[float] = Field(default_factory=list)
    emotion_control: str = "reference_audio"
    use_reference_audio: bool = True


# ---------------------------------------------------------------- 参考音频

def _voice_files() -> list[Path]:
    root = Path(settings.TTS_VOICE_DIR)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.suffix.lower() in _AUDIO_EXT)


def _resolve_ref(ref_id: str, emotion: str) -> Path | None:
    """参考音频：先按 id 找，再按情绪找，最后用 default。

    目录约定 models/voices/ 下放 sad.wav / happy.wav ...，
    控制台里的 voice profile 记的就是这个名字（不带扩展名）。
    """
    root = Path(settings.TTS_VOICE_DIR)
    for key in (ref_id, _canon_emotion(emotion), "default"):
        key = (key or "").strip()
        if not key:
            continue
        direct = Path(key)
        if direct.is_file():
            return direct
        if not direct.is_absolute():
            for ext in _AUDIO_EXT:
                candidate = root / f"{key}{ext}"
                if candidate.is_file():
                    return candidate
    files = _voice_files()
    return files[0] if files else None


_PROMPT_CACHE: dict[str, str] = {}


def _prompt_text_for(ref: Path) -> str:
    """参考音频「说的是什么字」，GPT-SoVITS 拿它对齐音色。

    取值顺序：
      1. 同名 .txt      sad.wav → sad.txt
      2. prompts.json   {"sad.wav": "这句话的原文"}
      3. prompt.txt     全局兜底，所有参考音频共用一句
      4. 空             能合成，但音色会飘

    建议还是写上，就是那段录音里说的原话，十来二十个字最好。
    """
    key = str(ref)
    if key in _PROMPT_CACHE:
        return _PROMPT_CACHE[key]

    text = ""
    sidecar = ref.with_suffix(".txt")
    if sidecar.is_file():
        text = sidecar.read_text(encoding="utf-8", errors="replace")

    if not text:
        mapping = Path(settings.TTS_VOICE_DIR) / "prompts.json"
        if mapping.is_file():
            try:
                data = json.loads(mapping.read_text(encoding="utf-8"))
                text = str(data.get(ref.name) or data.get(ref.stem) or "")
            except Exception:
                text = ""

    if not text:
        fallback = Path(settings.TTS_VOICE_DIR) / "prompt.txt"
        if fallback.is_file():
            text = fallback.read_text(encoding="utf-8", errors="replace")

    text = " ".join(text.split())
    _PROMPT_CACHE[key] = text
    return text


def _guess_lang(text: str, default: str) -> str:
    """纯英文句子交给 en，其余按配置。混排的中英句交给 zh 更稳。"""
    default = (default or "zh").strip().lower()
    if default != "zh":
        return default
    body = text or ""
    cjk = sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in body if ch.isascii() and ch.isalpha())
    return "en" if cjk == 0 and letters > 0 else default


# ---------------------------------------------------------------- 后端

class _Backend:
    name = "none"

    def load(self) -> None:
        raise NotImplementedError

    def synth(self, req: TTSRequest) -> bytes:
        raise NotImplementedError


class GPTSovitsBackend(_Backend):
    """GPT-SoVITS 的 api_v2.py（默认 :9880）。

    它是独立整合包，自带 Python 3.10 + torch，跟本工程**不共用任何依赖**，
    所以这里只走 HTTP：一句话 = 一次 POST /tts，拿回 wav 字节。
    本工程 .venv 是 3.12、装不动 IndexTTS 依赖时，这条是绕开环境问题的路。

    情感靠「换参考音频」表达：models/voices/ 下放 sad.wav / happy.wav ...
    每段 3~10 秒、就用那种语气说。切情绪就是换参考音频——比调向量参数稳，
    也不挑模型版本（v2/v3/v4 的接口都是这个）。
    """

    name = "gpt_sovits"

    def __init__(self) -> None:
        self._base = ""

    @property
    def endpoint(self) -> str:
        return f"{self._base}/tts" if self._base else ""

    def load(self) -> None:
        base = str(getattr(settings, "TTS_SOVITS_URL", "") or "http://127.0.0.1:9880").rstrip("/")
        if not base.lower().startswith("http"):
            raise RuntimeError(f"TTS_SOVITS_URL 要填 http 开头的地址，现在是：{base}")
        if base.endswith("/tts"):
            base = base[:-4].rstrip("/")
        self._base = base

        up, detail = self._probe()
        if not up:
            raise RuntimeError(
                f"连不上 GPT-SoVITS（{base}，{detail}）。先在它的整合包里启动："
                f"双击启动服务，或命令 python api_v2.py -a 127.0.0.1 -p 9880"
            )

    def _probe(self, timeout: float = 3.0) -> tuple[bool, str]:
        """api_v2.py 没有 /health，就打 /tts 不给参数。

        任何 HTTP 响应（哪怕是 400「ref_audio_path is required」）都说明服务活着。
        它是模型加载完之后才 uvicorn.run 的，所以「能连上」≈「模型已就绪」。
        """
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                resp = client.get(self._base + "/tts")
            return True, f"HTTP {resp.status_code}"
        except Exception as exc:
            return False, type(exc).__name__

    def synth(self, req: TTSRequest) -> bytes:
        ref = _resolve_ref(req.reference_audio_id or req.voice_id, req.emotion)
        if ref is None:
            raise RuntimeError(
                f"没有可用的参考音频：往 {settings.TTS_VOICE_DIR} 里放一段人声，"
                f"命名 sad.wav / happy.wav / default.wav；"
                f"再在 wav 旁边放同名 txt 写这段话的原文字（声线会更稳）"
            )

        lang = str(getattr(settings, "TTS_TEXT_LANG", "zh") or "zh")
        # 音高在这个后端只能靠重采样做：语速先反向补偿，重采样后净效果=音高变、
        # 时长回到 profile 设置的值。所以这里送 1/pitch 给模型。
        emo = _canon_emotion(req.emotion)
        pitch, contour, vibrato = _EMO_PROSODY.get(emo, (1.0, 0.0, 0.0))
        gain = float(req.volume or 1.0)
        payload = {
            "text": req.text,
            "text_lang": _guess_lang(req.text, lang),
            "ref_audio_path": str(ref.resolve()),
            "prompt_text": _prompt_text_for(ref),
            "prompt_lang": lang,
            "text_split_method": "cut5",
            # 默认 0.3s 的句内停顿会把一句话拖得很慢（cut5 会在逗号处切片），
            # 压到 0.12s 后连贯很多，语速不再拖沓。
            "fragment_interval": 0.12,
            "media_type": "wav",
            "speed_factor": float(req.speed or 1.0) / pitch,
            "streaming_mode": False,
        }
        with httpx.Client(timeout=settings.TTS_TIMEOUT, trust_env=False) as client:
            resp = client.post(self._base + "/tts", json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"GPT-SoVITS 返回 {resp.status_code}：{resp.text[:200]}")
        if not resp.content:
            raise RuntimeError("GPT-SoVITS 返回了空音频")
        return _apply_prosody(resp.content, pitch, gain, contour, vibrato,
                              _EMO_ACCENT.get(emo))


class IndexTTSBackend(_Backend):
    """IndexTTS-2 本地推理。权重目录默认 models/IndexTTS-2。"""

    name = "indextts"

    def __init__(self) -> None:
        self._tts = None

    def load(self) -> None:
        model_dir = Path(settings.TTS_MODEL_DIR)
        if not model_dir.is_dir():
            raise RuntimeError(f"模型目录不存在：{model_dir}（跑 tools\\setup_tts_model.py 下载）")

        cfg = model_dir / "config.yaml"
        try:
            from indextts.infer_v2 import IndexTTS2  # type: ignore
        except Exception as exc:
            # index-tts 没有 PyPI 包，只能从源码装。允许用户把仓库直接放到
            # third_party/index-tts 而不用 pip install -e（服务器上更省事）。
            code_dir = Path(settings.TTS_CODE_DIR)
            if (code_dir / "indextts").is_dir():
                if str(code_dir) not in sys.path:
                    sys.path.insert(0, str(code_dir))
                try:
                    from indextts.infer_v2 import IndexTTS2  # type: ignore
                except Exception as exc2:
                    raise RuntimeError(
                        f"找到源码目录但导入失败：{exc2}。"
                        f"依赖没装齐。注意 index-tts 要求 Python 3.10~3.11，"
                        f"当前是 {sys.version.split()[0]}；"
                        f"在 {code_dir} 里用 uv sync，或用 3.11 的 python 执行"
                        f" pip install -e {code_dir}"
                    ) from exc2
            else:
                raise RuntimeError(
                    f"没装 indextts（{type(exc).__name__}）。它没有 PyPI 包："
                    f"① git clone https://github.com/index-tts/index-tts 到 {code_dir}；"
                    f"② 依赖要装（要求 Python 3.10~3.11，当前 {sys.version.split()[0]}），"
                    f"见 README 第五节"
                ) from exc

        device = settings.TTS_DEVICE
        use_fp16 = device.startswith("cuda")
        self._tts = IndexTTS2(
            cfg_path=str(cfg),
            model_dir=str(model_dir),
            use_fp16=use_fp16,
            device=device,
        )

    def synth(self, req: TTSRequest) -> bytes:
        import tempfile

        ref = _resolve_ref(req.reference_audio_id or req.voice_id, req.emotion)
        if ref is None:
            raise RuntimeError(
                f"没有可用的参考音频：把 wav 放进 {settings.TTS_VOICE_DIR}，"
                f"命名为 sad.wav / happy.wav / default.wav"
            )

        vector = req.emotion_vector or []
        if not vector and str(req.emotion_control) != "none":
            vector = _EMO_VECTOR.get(_canon_emotion(req.emotion), _EMO_VECTOR["neutral"])

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.wav"
            kwargs = {
                "spk_audio_prompt": str(ref),
                "text": req.text,
                "output_path": str(out),
                "verbose": False,
            }
            if vector:
                kwargs["emo_vector"] = list(vector)
                kwargs["emo_alpha"] = 0.8
            self._call_infer(kwargs)
            if not out.is_file():
                raise RuntimeError("推理结束但没有生成音频文件")
            data = out.read_bytes()

        if str(req.format).lower() == "wav":
            return data
        # 只做格式声明上的兼容：非 wav 需要 ffmpeg，不做静默转码，直接给 wav
        return data

    def _call_infer(self, kwargs: dict) -> None:
        """不同版本 IndexTTS2 的 infer 参数名有出入，按签名过滤后再调。"""
        import inspect

        assert self._tts is not None
        try:
            accepted = set(inspect.signature(self._tts.infer).parameters)
        except (TypeError, ValueError):
            accepted = set(kwargs)
        call = {k: v for k, v in kwargs.items() if k in accepted}
        # emo_alpha 只在对得上 emo_vector 时有效
        if "emo_vector" not in call:
            call.pop("emo_alpha", None)
        self._tts.infer(**call)


class ProxyBackend(_Backend):
    """原样转发给已有服务（GPT-SoVITS 之类），我们只做协议适配。"""

    name = "proxy"

    def load(self) -> None:
        url = str(settings.TTS_UPSTREAM or "").strip()
        if not url:
            raise RuntimeError("proxy 模式需要配置 TTS_UPSTREAM（指向已有语音服务的 /tts）")
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                resp = client.get(url.rstrip("/").rsplit("/", 1)[0] or url)
            if resp.status_code >= 500:
                raise RuntimeError(f"上游返回 {resp.status_code}")
        except Exception as exc:
            raise RuntimeError(f"上游不可达：{url}（{type(exc).__name__}）") from exc

    def synth(self, req: TTSRequest) -> bytes:
        target = str(settings.TTS_UPSTREAM or "").strip()
        with httpx.Client(timeout=settings.TTS_TIMEOUT, trust_env=False) as client:
            resp = client.post(target, json=req.model_dump())
            resp.raise_for_status()
            return resp.content


class OpenAIBackend(_Backend):
    """OpenAI 兼容 /v1/audio/speech。"""

    name = "openai"

    def load(self) -> None:
        if not settings.TTS_BASE:
            raise RuntimeError("openai 模式需要配置 TTS_BASE")

    def synth(self, req: TTSRequest) -> bytes:
        url = settings.TTS_BASE.rstrip("/") + "/v1/audio/speech"
        payload = {
            "model": "tts-1",
            "input": req.text,
            "voice": req.voice_id or "alloy",
            "response_format": "wav",
        }
        with httpx.Client(timeout=settings.TTS_TIMEOUT, trust_env=False) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            return resp.content


_BACKENDS = {
    "indextts": IndexTTSBackend,
    "gpt_sovits": GPTSovitsBackend,
    "gpt-sovits": GPTSovitsBackend,
    "sovits": GPTSovitsBackend,
    "proxy": ProxyBackend,
    "openai": OpenAIBackend,
}

_backend: _Backend | None = None


def _make_backend() -> _Backend:
    name = str(settings.TTS_BACKEND or "indextts").strip().lower()
    factory = _BACKENDS.get(name)
    return factory() if factory else _Backend()


def _load_backend() -> None:
    global _backend
    started = time.perf_counter()
    _backend = _make_backend()
    _state["backend"] = _backend.name
    try:
        if _backend.name == "none":
            raise RuntimeError(
                f"未知 TTS_BACKEND：{settings.TTS_BACKEND}"
                f"（可选 gpt_sovits / indextts / proxy / openai）"
            )
        _backend.load()
        _state["ready"] = True
        _state["error"] = ""
    except Exception as exc:
        _state["ready"] = False
        _state["error"] = f"{type(exc).__name__}: {exc}"
    _state["loaded_ms"] = round((time.perf_counter() - started) * 1000.0, 1)


def _banner() -> None:
    print("=" * 58)
    print("  语音合成服务  tts server")
    print("=" * 58)
    print(f"  监听地址  : http://{settings.TTS_HOST}:{settings.TTS_PORT}")
    print(f"  后端      : {settings.TTS_BACKEND}   设备 : {settings.TTS_DEVICE}")
    if _state["backend"] in ("gpt_sovits", "gpt-sovits", "sovits"):
        print(f"  上游      : {getattr(_backend, 'endpoint', '') or settings.TTS_SOVITS_URL}")
        print(f"  模型目录  : 由 GPT-SoVITS 自己管（本服务不加载权重）")
    else:
        print(f"  模型目录  : {settings.TTS_MODEL_DIR}")
    print(f"  参考音频  : {settings.TTS_VOICE_DIR}（{len(_voice_files())} 个）")
    print(f"  加载耗时  : {_state['loaded_ms']} ms   状态 : {'就绪' if _state['ready'] else '未就绪'}")
    if _state["error"]:
        print(f"  [提示] {_state['error']}")
        print("         服务已启动，/tts 会返回 503；大脑会退回内置占位音，对话不受影响。")
    print("=" * 58)
    print("  这个窗口保持打开就是语音服务在跑；关掉即停。")
    print()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _load_backend()
    _banner()
    yield


app = FastAPI(title="companion tts", lifespan=_lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "tts",
        "ready": bool(_state["ready"]),
        "backend": _state["backend"],
        "device": settings.TTS_DEVICE,
        "model_dir": settings.TTS_MODEL_DIR,
        "upstream": getattr(_backend, "endpoint", "") if _backend else "",
        "detail": _state["error"],
        "loaded_ms": _state["loaded_ms"],
        "voices": [p.stem for p in _voice_files()],
        "calls": _state["calls"],
    }


@app.get("/voices")
def voices() -> dict:
    return {"voices": [p.stem for p in _voice_files()], "dir": settings.TTS_VOICE_DIR}


@app.post("/tts")
def tts(req: TTSRequest):
    if not _state["ready"] or _backend is None:
        return JSONResponse(
            status_code=503,
            content={"detail": f"tts backend not ready: {_state['error']}"},
        )
    raw = (req.text or "").strip()
    if not raw:
        return JSONResponse(status_code=400, content={"detail": "text is empty"})
    # 表情 / Markdown / 纯符号会让 GPT-SoVITS 直接报错，先清成能朗读的文字
    text = _clean_text(raw)
    if not _speakable(text):
        log_setup.log().info("跳过  没有可朗读的内容：%s", raw[:30])
        return JSONResponse(
            status_code=400,
            content={"detail": "文本里没有可朗读的内容（只有表情或符号）"},
        )
    req = req.model_copy(update={"text": text})
    started = time.perf_counter()
    try:
        audio = _backend.synth(req)
    except Exception as exc:
        log_setup.log().warning(
            "合成失败  %s 字 / %s / %s：%s", len(text), req.emotion, req.reference_audio_id, exc
        )
        return JSONResponse(status_code=503, content={"detail": f"synth failed: {exc}"})
    if not audio:
        log_setup.log().warning("合成失败  %s 字：后端返回空音频", len(text))
        return JSONResponse(status_code=503, content={"detail": "synth returned empty"})
    # 去掉首尾静音：手机端逐句播放时，每句的头尾静音会累积成明显的「断」
    audio = _trim_silence(audio)
    _state["calls"] += 1
    ms = (time.perf_counter() - started) * 1000
    log_setup.log().info(
        "合成  %s 字 → %d KB / %.1f 秒 / %s / 参考音 %s  ｜%s",
        len(text), len(audio) // 1024, ms / 1000.0, req.emotion,
        req.reference_audio_id or "(按情绪)", text[:24],
    )
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "X-TTS-Backend": _state["backend"],
            "X-TTS-Ms": f"{ms:.0f}",
        },
    )


def main() -> None:
    _fix_console()
    log_setup.start("tts")
    if _port_busy(settings.TTS_PORT):
        print()
        print(f"[提示] 端口 {settings.TTS_PORT} 已经有服务在跑了，不用重复启动。")
        print(f"       浏览器打开 http://127.0.0.1:{settings.TTS_PORT}/health 可以确认。")
        print()
        return
    try:
        # log_config=None：uvicorn 默认会重置 logging，日志就流不进文件了
        uvicorn.run(app, host=settings.TTS_HOST, port=settings.TTS_PORT,
                    log_level="warning", log_config=None)
    except Exception:
        import traceback

        log = Path(settings.ROOT) / "data" / "logs" / "tts-server.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
            print(f"[ERROR] 语音服务启动失败，详细错误：{log}")
        except Exception:
            traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
