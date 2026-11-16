"""训练中心：环境体检 + 可选动作（数据准备 / 训练 / 合并 / 装依赖）。

原则：**不替用户做决定**。这里只提供事实（缺什么、有多大、放哪）
和一键动作，选哪个由用户在 /train.html 上自己点。

训练是后台进程，不占大脑的主线程；同一时刻只允许一个任务。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from brain.config import ROOT, settings
from brain import db

PY = str(Path(sys.executable))
LOG_DIR = ROOT / "data" / "train"
LOG_FILE = LOG_DIR / "job.log"

_LOCK = threading.Lock()
_JOB: dict = {"name": "", "status": "idle", "started_at": 0.0, "ended_at": 0.0,
              "code": None, "cmd": ""}
_PROC: subprocess.Popen | None = None

# /status 每次都 import torch + 扫模型目录，重且重复：30 秒内复用同一份快照。
_STATUS_CACHE: tuple[float, dict] | None = None
_STATUS_TTL = 30.0


# ---------------- 环境体检 ----------------

def _dep(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _torch_state() -> dict:
    try:
        import torch

        cuda = bool(torch.cuda.is_available())
        info = {"installed": True, "version": str(torch.__version__), "cuda": cuda}
        if cuda:
            info["gpu"] = torch.cuda.get_device_name(0)
            info["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / 1024 ** 3, 1)
        return info
    except Exception as exc:
        return {"installed": False, "error": str(exc)[:120], "cuda": False}


def _nvidia() -> list[str]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8,
        )
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except Exception:
        return 0


_MODEL_DIRS = [Path("F:/1/models"), Path("F:/models"), ROOT / "models"]


def _is_trainable_hf(p: Path) -> bool:
    """只认「能训的因果语言模型」目录：判官/embedding 那种小模型不算底座。"""
    cfg = p / "config.json"
    if not cfg.exists():
        return False
    name = str(p).lower()
    if any(k in name for k in ("emotion", "judge", "embedding", "reranker")):
        return False
    try:
        data = json.loads(cfg.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return False
    archs = " ".join(data.get("architectures") or [])
    if "ForCausalLM" in archs:
        return True
    return str(data.get("model_type") or "").lower() in {"qwen2", "qwen3", "minicpm", "llama"}


def list_models() -> dict:
    """摆出可选底座：GGUF（只能跑、不能训）与 HF 目录（能训）。"""
    gguf, hf = [], []
    for d in _MODEL_DIRS:
        if not d.exists():
            continue
        try:
            for p in d.glob("*.gguf"):
                gguf.append({"path": str(p), "size_gb": round(p.stat().st_size / 1024 ** 3, 2)})
            for p in d.iterdir():
                if p.is_dir() and _is_trainable_hf(p):
                    hf.append({"path": str(p), "size_gb": round(
                        sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 ** 3, 2)})
        except Exception:
            continue
    return {
        "gguf": sorted(gguf, key=lambda x: x["path"]),
        "hf": sorted(hf, key=lambda x: x["path"]),
        "dirs": [str(d) for d in _MODEL_DIRS if d.exists()],
    }


def _llamacpp() -> list[str]:
    hits: list[str] = []
    for cand in ("E:/情感开发/llama.cpp", "F:/llama.cpp", "D:/llama.cpp",
                 "E:/llama.cpp", str(ROOT / "llama.cpp")):
        p = Path(cand)
        if p.exists():
            hits.append(str(p))
    return hits


def install_options() -> list[dict]:
    """可选项 + 真实体积 + 到底解决什么问题。命令只是给人看的，点按钮才执行。"""
    return [
        {"id": "cuda_torch", "title": "CUDA 版 torch（cu121）", "size": "约 2.5GB",
         "solves": "让 P104 真正参与训练；现在装的是 CPU 版，训练等于没有显卡",
         "cmd": f'"{PY}" -m pip install --upgrade torch --index-url https://download.pytorch.org/whl/cu121'},
        {"id": "hf_base", "title": "MiniCPM5-2B 的 HF 原版权重", "size": "约 5GB",
         "solves": "训练起点；GGUF 无法训练",
         "cmd": "（需指定仓库名；我没有替你选仓库，请先确认来源再点）"},
        {"id": "llamacpp", "title": "llama.cpp 工具链", "size": "约 300MB",
         "solves": "把训好的权重转成 GGUF 并量化，模型服务才认得",
         "cmd": "（需要 git clone + cmake 编译；我没有自动装，避免污染你的环境）"},
        {"id": "bnb", "title": "bitsandbytes（4bit 可选）", "size": "约 60MB",
         "solves": "只在显存更紧时用 QLoRA；P104 上支持不稳，非必需",
         "cmd": f'"{PY}" -m pip install bitsandbytes'},
    ]


def status() -> dict:
    global _STATUS_CACHE
    now = time.monotonic()
    if _STATUS_CACHE is not None and now - _STATUS_CACHE[0] < _STATUS_TTL:
        return dict(_STATUS_CACHE[1])

    deps = {name: _dep(name) for name in
            ("torch", "transformers", "peft", "accelerate", "datasets", "bitsandbytes")}
    data_dir = ROOT / "data" / "lora"
    samples = db.count_tone_samples()
    result = {
        "python": PY,
        "torch": _torch_state(),
        "deps": deps,
        "gpus": _nvidia(),
        "samples": samples,
        "dataset": {
            "dir": str(data_dir),
            "train": _count_lines(data_dir / "train.jsonl"),
            "valid": _count_lines(data_dir / "valid.jsonl"),
            "ready": _count_lines(data_dir / "train.jsonl") >= 200,
        },
        "models": list_models(),
        "llamacpp": _llamacpp(),
        "options": install_options(),
        "job": job(),
    }
    _STATUS_CACHE = (now, result)
    return dict(result)


# ---------------- 后台任务 ----------------

def job() -> dict:
    with _LOCK:
        info = dict(_JOB)
    if info["status"] == "running" and info["started_at"]:
        info["elapsed_s"] = int(time.time() - info["started_at"])
    info["log_tail"] = tail(80)
    return info


def tail(lines: int = 80) -> str:
    try:
        if not LOG_FILE.exists():
            return ""
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception:
        return ""


def _spawn(name: str, args: list[str]) -> dict:
    global _PROC
    with _LOCK:
        if _PROC is not None and _PROC.poll() is None:
            return {"ok": False, "error": "已有一个任务在跑", "job": dict(_JOB)}
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text("", encoding="utf-8")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        try:
            fh = LOG_FILE.open("a", encoding="utf-8")
            _PROC = subprocess.Popen(
                [PY, "-u"] + args, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return {"ok": False, "error": f"启动失败：{exc}"}
        _JOB.update({"name": name, "status": "running", "started_at": time.time(),
                     "ended_at": 0.0, "code": None, "cmd": " ".join(args)})
    threading.Thread(target=_watch, args=(_PROC,), daemon=True).start()
    return {"ok": True, "job": job()}


def _watch(proc: subprocess.Popen) -> None:
    code = proc.wait()
    with _LOCK:
        _JOB.update({"status": "done" if code == 0 else "failed", "ended_at": time.time(), "code": code})


def start_dataset(with_persona: bool = True) -> dict:
    args = ["tools/build_lora_dataset.py"]
    if with_persona:
        args.append("--with-persona")
    return _spawn("准备训练集", args)


def start_train(base_model: str, epochs: float = 3.0, rank: int = 16,
                qlora: bool = False, merge: bool = True) -> dict:
    base = Path(str(base_model))
    if not (base / "config.json").exists():
        return {"ok": False, "error": "这个目录没有 config.json，不像 HF 权重（GGUF 不能训）"}
    train_n = _count_lines(ROOT / "data" / "lora" / "train.jsonl")
    if train_n == 0:
        return {"ok": False, "error": "训练集是空的，先点「准备训练集」"}
    args = ["tools/train_lora.py", "--base-model", str(base),
            "--epochs", str(epochs), "--rank", str(rank)]
    if qlora:
        args.append("--qlora")
    if merge:
        args.append("--merge")
    return _spawn("训练口吻 LoRA", args)


def stop() -> dict:
    global _PROC
    with _LOCK:
        if _PROC is None or _PROC.poll() is not None:
            return {"ok": False, "error": "没有在跑的任务"}
        _PROC.terminate()
        _JOB.update({"status": "stopped", "ended_at": time.time()})
    return {"ok": True}


def install(target: str) -> dict:
    """只做用户点过的那一件事，不顺手装别的。"""
    if target == "cuda_torch":
        return _spawn("安装 CUDA torch", ["-m", "pip", "install", "--upgrade", "torch",
                                         "--index-url", "https://download.pytorch.org/whl/cu121"])
    if target == "bnb":
        return _spawn("安装 bitsandbytes", ["-m", "pip", "install", "bitsandbytes"])
    if target in ("hf_base", "llamacpp"):
        return {"ok": False, "error": f"{target} 需要外部来源/编译，我不替你选；请按文档手动准备后再回来点训练"}
    return {"ok": False, "error": "未知选项"}
