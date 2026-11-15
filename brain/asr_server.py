"""语音识别服务（默认 :8103）。

小智那条链是 **流式 ASR → LLM 对话 → 流式 TTS**，我们之前只做了后两段，
这里是补上的前一段：把用户说的话变成文字。

和判断 / 语音合成一样：**模型自己一个进程，由 7-启动语音识别.bat 单独启动**，
大脑不 import sherpa_onnx、不占显存。关掉 bat 就退回「只能打字」。

接口（大脑侧按这个契约调用）
----------------------------
  GET  /health  → 是否就绪、模型目录、语言、加载耗时
  POST /asr     → 音频字节（wav，也接受 ffmpeg 能解的其它格式） → JSON
                  {text, lang, emotion, event, duration_ms, ms}

为什么用 SenseVoice 而不是 Whisper：
    中文场景它最准，而且**纯 ONNX、纯 CPU**，跟情感判断模型共用一套
    onnxruntime（实测装 sherpa-onnx 不会动现有版本），不抢那 8GB 显存。
    156MB 的 int8 模型，5.6 秒音频 414ms 出字。

顺带白捡的两个信号：
    它同时输出**声学情绪**（HAPPY/SAD/ANGRY/…）和**声音事件**
    （Speech/Laughter/Cry/Applause/…）。声学情绪是「说话人的语气」，
    跟大脑里那套「文字情绪」正好互补——文字说没事、声音在抖，这种才是
    情感陪伴真正该接住的时候。现在先原样返回，判不判断由大脑决定。
"""
from __future__ import annotations

import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from brain import log_setup
from brain.config import settings

_SAMPLE_RATE = 16000
_THREADS = max(1, min(4, (os.cpu_count() or 4)))

# SenseVoice 的标签 → 本工程的 8 个情绪枚举
_EMO_MAP = {
    "NEUTRAL": "neutral",
    "HAPPY": "happy",
    "SAD": "sad",
    "ANGRY": "angry",
    "FEARFUL": "fear",
    "DISGUSTED": "disgust",
    "SURPRISED": "surprised",
    "EMO_UNKNOWN": "neutral",
}

_state: dict = {
    "ready": False, "error": "", "loaded_ms": 0, "calls": 0,
    "last_ms": 0, "model": "",
}
_recognizer = None
_lock = threading.Lock()


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


# ---------------------------------------------------------------- 音频解码

class AudioError(ValueError):
    """音频本身有问题（格式、长度），属于 400，不是 500。"""


def _decode_wav(data: bytes) -> tuple[np.ndarray, float]:
    """wav 字节 → (16k 单声道 float32, 时长秒)。

    只认 PCM。浏览器那边我们用 Web Audio 采集后自己拼 wav，所以走到这里的
    基本都是 16-bit；真给了别的位深就明说，不猜。
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            frames = w.readframes(w.getnframes())
    except Exception as exc:
        raise AudioError(f"不是合法的 wav（{type(exc).__name__}）") from exc

    if width != 2:
        raise AudioError(f"只支持 16-bit PCM 的 wav，收到的是 {width * 8}-bit")
    if not frames:
        raise AudioError("wav 里没有音频数据")

    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)

    duration = len(audio) / float(rate or _SAMPLE_RATE)
    if rate != _SAMPLE_RATE:
        # 线性插值重采样。语音识别对这个精度不敏感，够用且零依赖
        want = int(len(audio) * _SAMPLE_RATE / rate)
        if want <= 0:
            raise AudioError("音频太短")
        grid = np.arange(want) * (rate / float(_SAMPLE_RATE))
        audio = np.interp(grid, np.arange(len(audio)), audio).astype(np.float32)
    return audio, duration


def _ffmpeg_convert(data: bytes) -> bytes:
    """非 wav（webm/m4a/mp3…）用 ffmpeg 转成 16k 单声道 wav。没有就报错。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AudioError(
            "收到的是非 wav 格式，这台机器上又没有 ffmpeg。两种办法："
            "① 装 ffmpeg 并加进 PATH；② 前端录音时直接上传 wav"
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.bin"
        dst = Path(tmp) / "out.wav"
        src.write_bytes(data)
        proc = subprocess.run(
            [exe, "-y", "-loglevel", "error", "-i", str(src),
             "-ar", str(_SAMPLE_RATE), "-ac", "1", "-sample_fmt", "s16",
             str(dst)],
            capture_output=True,
        )
        if proc.returncode != 0 or not dst.is_file():
            detail = proc.stderr.decode("utf-8", "replace")[:160]
            raise AudioError(f"ffmpeg 转码失败：{detail}")
        return dst.read_bytes()


def _normalize(raw: bytes) -> tuple[np.ndarray, float]:
    try:
        return _decode_wav(raw)
    except AudioError as exc:
        if "不是合法的 wav" not in str(exc):
            raise
        return _decode_wav(_ffmpeg_convert(raw))


def _strip(tag: str) -> str:
    return (tag or "").strip().strip("<|>").strip()


# ---------------------------------------------------------------- 模型

def _load() -> None:
    global _recognizer
    started = time.perf_counter()
    model_dir = Path(settings.ASR_MODEL_DIR)
    model = model_dir / "model.int8.onnx"
    tokens = model_dir / "tokens.txt"
    if not model.is_file():
        model = model_dir / "model.onnx"
    if not model.is_file() or not tokens.is_file():
        raise RuntimeError(
            f"模型文件不全：{model_dir} 里要有 model.int8.onnx 和 tokens.txt"
            f"（跑 tools\\setup_asr_model.py --download）"
        )
    try:
        import sherpa_onnx
    except Exception as exc:
        raise RuntimeError(
            f"没装 sherpa-onnx（{type(exc).__name__}）。"
            f'先执行 ".venv\\Scripts\\python.exe" -m pip install sherpa-onnx'
        ) from exc

    _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model),
        tokens=str(tokens),
        num_threads=_THREADS,
        language=str(settings.ASR_LANGUAGE or "auto"),
        use_itn=True,
    )
    _state["model"] = model.name
    _state["loaded_ms"] = round((time.perf_counter() - started) * 1000.0, 1)


def _load_backend() -> None:
    try:
        _load()
        _state["ready"] = True
        _state["error"] = ""
    except Exception as exc:
        _state["ready"] = False
        _state["error"] = f"{type(exc).__name__}: {exc}"
        _state["loaded_ms"] = round(_state.get("loaded_ms") or 0, 1)


def _banner() -> None:
    print("=" * 58)
    print("  语音识别服务  asr server")
    print("=" * 58)
    print(f"  监听地址  : http://{settings.ASR_HOST}:{settings.ASR_PORT}")
    print(f"  引擎      : sherpa-onnx + SenseVoice（CPU，不占显存）")
    print(f"  模型目录  : {settings.ASR_MODEL_DIR}")
    print(f"  语言      : {settings.ASR_LANGUAGE}    线程 : {_THREADS}")
    print(f"  加载耗时  : {_state['loaded_ms']} ms   状态 : {'就绪' if _state['ready'] else '未就绪'}")
    if _state["error"]:
        print(f"  [提示] {_state['error']}")
        print("         服务已启动，/asr 会返回 503；前端会提示「语音识别没开」，打字不受影响。")
    print("=" * 58)
    print("  这个窗口保持打开就是识别服务在跑；关掉即停。")
    print()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _load_backend()
    _banner()
    yield


app = FastAPI(title="companion asr", lifespan=_lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "asr",
        "ready": bool(_state["ready"]),
        "engine": "sherpa-onnx-sensevoice",
        "model": _state["model"],
        "model_dir": settings.ASR_MODEL_DIR,
        "language": settings.ASR_LANGUAGE,
        "threads": _THREADS,
        "detail": _state["error"],
        "loaded_ms": _state["loaded_ms"],
        "calls": _state["calls"],
        "last_ms": _state["last_ms"],
    }


@app.post("/asr")
async def asr(request: Request):
    if not _state["ready"] or _recognizer is None:
        return JSONResponse(
            status_code=503,
            content={"detail": f"asr not ready: {_state['error']}"},
        )

    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file") or form.get("audio")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse(status_code=400, content={"detail": "multipart 里要有 file 字段"})
        raw = await upload.read()
    else:
        raw = await request.body()

    if not raw:
        return JSONResponse(status_code=400, content={"detail": "没有收到音频数据"})

    try:
        audio, duration = _normalize(raw)
    except AudioError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    if duration > float(settings.ASR_MAX_SECONDS):
        return JSONResponse(
            status_code=400,
            content={"detail": f"音频 {duration:.1f} 秒，超过上限 {settings.ASR_MAX_SECONDS:.0f} 秒"},
        )
    if duration < 0.2:
        return JSONResponse(status_code=400, content={"detail": "音频太短，还没说话就结束了"})

    started = time.perf_counter()
    try:
        # sherpa 的 recognizer 不是线程安全的，串行解码
        with _lock:
            stream = _recognizer.create_stream()
            stream.accept_waveform(_SAMPLE_RATE, audio)
            _recognizer.decode_stream(stream)
            result = stream.result
    except Exception as exc:
        log_setup.log().warning("识别失败  %.1f 秒音频：%s", duration, exc)
        return JSONResponse(status_code=503, content={"detail": f"decode failed: {exc}"})

    ms = (time.perf_counter() - started) * 1000.0
    _state["calls"] += 1
    _state["last_ms"] = round(ms, 1)

    text = (result.text or "").strip()
    emo_tag = _strip(getattr(result, "emotion", ""))
    log_setup.log().info(
        "识别  %.1f 秒音频 → %.0f ms / %s / 声学情绪 %s  ｜%s",
        duration, ms, _strip(getattr(result, "lang", "")) or "?", emo_tag or "-",
        (text[:30] + "…") if len(text) > 30 else (text or "(空)"),
    )
    return {
        "text": text,
        "lang": _strip(getattr(result, "lang", "")) or "unknown",
        "emotion": _EMO_MAP.get(emo_tag.upper(), "neutral"),
        "emotion_tag": emo_tag,
        "event": _strip(getattr(result, "event", "")) or "Speech",
        "duration_ms": round(duration * 1000.0),
        "ms": round(ms, 1),
        "empty": not bool(text),
    }


def main() -> None:
    _fix_console()
    log_setup.start("asr")
    if _port_busy(settings.ASR_PORT):
        print()
        print(f"[提示] 端口 {settings.ASR_PORT} 已经有服务在跑了，不用重复启动。")
        print(f"       浏览器打开 http://127.0.0.1:{settings.ASR_PORT}/health 可以确认。")
        print()
        return
    uvicorn.run(app, host=settings.ASR_HOST, port=settings.ASR_PORT,
                log_level="warning", log_config=None)


if __name__ == "__main__":
    main()
