"""运行时配置：纯标准库实现，不引入 pydantic-settings，减少依赖。

注意：settings 只提供**初始默认值**。所有可热更新的运行时参数
（temperature / top_p / top_k / num_ctx ...）必须从 brain.db.get_runtime_params() 读取。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("brain.config")

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """极简 .env 读取，不覆盖已有环境变量。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _load_yaml_config() -> None:
    """启动配置的唯一入口：config.yaml，优先级高于 .env 与代码默认值。

    只认「key: value」两级，section 行和注释自动跳过；纯标准库，不引 PyYAML。
    """
    path = ROOT / "config.yaml"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key.upper(), value)


_load_yaml_config()
_load_dotenv()


def _s(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _i(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _b(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _abs(value: str) -> str:
    """配置里写相对路径时，一律相对工程根目录，不依赖启动时的 cwd。"""
    p = Path(value)
    return str(p if p.is_absolute() else ROOT / p)


@dataclass(frozen=True)
class Settings:
    ROOT: Path
    DB_PATH: str
    CHROMA_PATH: str
    OLLAMA_BASE: str
    LLM_MODEL: str
    EMBED_MODEL: str
    TTS_BASE: str
    TTS_TIMEOUT: float
    TTS_HOST: str
    TTS_PORT: int
    TTS_BACKEND: str
    TTS_MODEL_DIR: str
    TTS_CODE_DIR: str
    TTS_DEVICE: str
    TTS_VOICE_DIR: str
    TTS_UPSTREAM: str
    TTS_SOVITS_URL: str
    TTS_TEXT_LANG: str
    ASR_BASE: str
    ASR_HOST: str
    ASR_PORT: int
    ASR_MODEL_DIR: str
    ASR_LANGUAGE: str
    ASR_TIMEOUT: float
    ASR_MAX_SECONDS: float
    JUDGE_BASE: str
    JUDGE_HOST: str
    JUDGE_PORT: int
    JUDGE_MODEL_PATH: str
    JUDGE_CONTEXT_TURNS: int
    BRAIN_HOST: str
    BRAIN_PORT: int
    CONSOLE_HOST: str
    CONSOLE_PORT: int
    MEMORY_TOP_K: int
    EMOTION_MIN_INTENSITY: float
    EMOTION_RECENT_LIMIT: int
    LLM_NUM_CTX: int
    LLM_TEMPERATURE: float
    LLM_TOP_P: float
    LLM_MAX_TOKENS: int
    ADMIN_TOKEN: str
    WEB_DIR: Path


# 模型服务端口：模型由各自 bat 单独启动，这里只是「连到哪」。
# 只留一份端口，避免 host/port 和 base 两处写不一致。
_JUDGE_PORT = _i("JUDGE_PORT", 8101)
_TTS_PORT = _i("TTS_PORT", 8102)
_ASR_PORT = _i("ASR_PORT", 8103)

settings = Settings(
    ROOT=ROOT,
    DB_PATH=_abs(_s("DB_PATH", "data/companion.db")),
    CHROMA_PATH=_abs(_s("CHROMA_PATH", "data/chroma")),
    OLLAMA_BASE=_s("OLLAMA_BASE", "http://127.0.0.1:11434"),
    LLM_MODEL=_s("LLM_MODEL", "qwen3:8b"),
    EMBED_MODEL=_s("EMBED_MODEL", "bge-m3"),
    TTS_BASE=_s("TTS_BASE", f"http://127.0.0.1:{_TTS_PORT}").rstrip("/"),
    TTS_TIMEOUT=_f("TTS_TIMEOUT", 60.0),
    TTS_HOST=_s("TTS_HOST", "0.0.0.0"),
    TTS_PORT=_TTS_PORT,
    TTS_BACKEND=_s("TTS_BACKEND", "gpt_sovits"),
    TTS_MODEL_DIR=_abs(_s("TTS_MODEL_DIR", "models/IndexTTS-2")),
    # index-tts 源码目录：没 pip install -e 时也能直接 import（加进 sys.path）
    TTS_CODE_DIR=_abs(_s("TTS_CODE_DIR", "third_party/index-tts")),
    TTS_DEVICE=_s("TTS_DEVICE", "cuda:0"),
    TTS_VOICE_DIR=_abs(_s("TTS_VOICE_DIR", "models/voices")),
    # proxy 后端的上游地址：必须指向「别人」，不能指回本服务，否则自己转给自己
    TTS_UPSTREAM=_s("TTS_UPSTREAM", "http://127.0.0.1:9880/tts"),
    # gpt_sovits 后端：GPT-SoVITS 整合包里 api_v2.py 的地址（只填地址，路径自动补 /tts）
    TTS_SOVITS_URL=_s("TTS_SOVITS_URL", "http://127.0.0.1:9880").rstrip("/"),
    # 合成语言：zh（中文）/ en / ja / ko / yue；纯英文句子会自动按 en 走
    TTS_TEXT_LANG=_s("TTS_TEXT_LANG", "zh"),
    ASR_BASE=_s("ASR_BASE", f"http://127.0.0.1:{_ASR_PORT}").rstrip("/"),
    ASR_HOST=_s("ASR_HOST", "0.0.0.0"),
    ASR_PORT=_ASR_PORT,
    ASR_MODEL_DIR=_abs(_s("ASR_MODEL_DIR", "models/sense-voice")),
    # auto = 自动判断 中/英/日/韩/粤；也可锁死 zh
    ASR_LANGUAGE=_s("ASR_LANGUAGE", "auto"),
    ASR_TIMEOUT=_f("ASR_TIMEOUT", 30.0),
    ASR_MAX_SECONDS=_f("ASR_MAX_SECONDS", 60.0),
    JUDGE_BASE=_s("JUDGE_BASE", f"http://127.0.0.1:{_JUDGE_PORT}").rstrip("/"),
    JUDGE_HOST=_s("JUDGE_HOST", "0.0.0.0"),
    JUDGE_PORT=_JUDGE_PORT,
    JUDGE_MODEL_PATH=_s("JUDGE_MODEL_PATH", "models/chinese-emotion-small/model.onnx"),
    JUDGE_CONTEXT_TURNS=_i("JUDGE_CONTEXT_TURNS", 2),
    BRAIN_HOST=_s("BRAIN_HOST", "0.0.0.0"),
    BRAIN_PORT=_i("BRAIN_PORT", 8000),
    CONSOLE_HOST=_s("CONSOLE_HOST", "0.0.0.0"),
    CONSOLE_PORT=_i("CONSOLE_PORT", _i("BRAIN_PORT", 8000)),
    MEMORY_TOP_K=_i("MEMORY_TOP_K", 3),
    EMOTION_MIN_INTENSITY=_f("EMOTION_MIN_INTENSITY", 0.6),
    EMOTION_RECENT_LIMIT=_i("EMOTION_RECENT_LIMIT", 5),
    LLM_NUM_CTX=_i("LLM_NUM_CTX", 4096),
    LLM_TEMPERATURE=_f("LLM_TEMPERATURE", 0.7),
    LLM_TOP_P=_f("LLM_TOP_P", 0.9),
    LLM_MAX_TOKENS=_i("LLM_MAX_TOKENS", 512),
    ADMIN_TOKEN=_s("ADMIN_TOKEN", ""),
    WEB_DIR=ROOT / "web",
)


def write_web_config() -> None:
    """启动时把实际端口写进 web/config.local.js，前端不再写死 8000/8001。"""
    payload = {
        "brain_host": settings.BRAIN_HOST,
        "brain_port": settings.BRAIN_PORT,
        "console_host": settings.CONSOLE_HOST,
        "console_port": settings.CONSOLE_PORT,
    }
    try:
        settings.WEB_DIR.mkdir(parents=True, exist_ok=True)
        (settings.WEB_DIR / "config.local.js").write_text(
            "window.COMPANION_LOCAL = " + json.dumps(payload, ensure_ascii=False) + ";\n",
            encoding="utf-8",
        )
    except Exception as exc:
        log.error("config.local.js 写入失败：%s", type(exc).__name__, exc)
        print(f"  [警告] config.local.js 未生成，前端将用兜底端口（{type(exc).__name__}: {exc}）")
