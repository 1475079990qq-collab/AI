"""准备语音模型（IndexTTS-2）：下权重、建参考音频目录、检查源码与依赖。

用法：
    python tools/setup_tts_model.py --check      # 只体检，告诉你缺什么
    python tools/setup_tts_model.py --download   # 下权重到 models/IndexTTS-2（约 5.5GB）

为什么不做成「一键装完」：
    index-tts 没有 PyPI 包，只能从 GitHub 源码装，而且它依赖 torch / CUDA，
    在服务器上装比在开发机上装靠谱得多。这个脚本负责**能自动化的部分**
    （权重、目录、体检），剩下的源码克隆和 pip 它会打印确切命令。

权重走 hf-mirror.com：官方 huggingface.co 在国内多半连不上。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO = "IndexTeam/IndexTTS-2"
MIRROR = "https://hf-mirror.com"

# 缺任何一个都跑不起来（qwen 目录单独判）
REQUIRED = ("config.yaml", "gpt.pth", "s2mel.pth", "bpe.model",
            "feat1.pt", "feat2.pt")


def _settings():
    from brain.config import settings

    return settings


def check() -> bool:
    s = _settings()
    model_dir = Path(s.TTS_MODEL_DIR)
    code_dir = Path(s.TTS_CODE_DIR)
    voices = Path(s.TTS_VOICE_DIR)

    print("=" * 60)
    print("  IndexTTS-2 体检")
    print("=" * 60)
    print(f"  模型目录  {model_dir}")
    ok = True
    if not model_dir.is_dir():
        print("    [缺] 目录不存在")
        ok = False
    else:
        missing = [n for n in REQUIRED if not (model_dir / n).is_file()]
        qwen = model_dir / "qwen0.6bemo4-merge"
        if not qwen.is_dir():
            missing.append("qwen0.6bemo4-merge/")
        size = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
        print(f"    体积 {size / 1024**3:.2f} GB")
        if missing:
            print(f"    [缺] {', '.join(missing)}")
            ok = False
        else:
            print("    [OK] 权重齐全")

    print(f"  源码目录  {code_dir}")
    if (code_dir / "indextts").is_dir():
        print("    [OK] 找到 indextts 源码")
    else:
        try:
            import indextts  # noqa: F401

            print("    [OK] 已通过 pip 安装")
        except Exception:
            print("    [缺] 没有源码也没有安装")
            ok = False

    print(f"  参考音频  {voices}")
    wavs = list(voices.glob("*.wav")) if voices.is_dir() else []
    if wavs:
        print(f"    [OK] {len(wavs)} 个：{', '.join(p.stem for p in wavs[:8])}")
    else:
        print("    [缺] 至少放一个 wav（建议 default.wav + sad/happy/angry/care）")

    print(f"  设备      {s.TTS_DEVICE}")
    try:
        import torch

        cuda = torch.cuda.is_available()
        print(f"    torch {torch.__version__}  CUDA {'可用' if cuda else '不可用'}")
        if cuda:
            print(f"    显卡 {torch.cuda.get_device_name(0)}")
        elif str(s.TTS_DEVICE).startswith("cuda"):
            print("    [注意] 配的是 cuda 但 CUDA 不可用，改 config.yaml 的 TTS_DEVICE: cpu")
    except Exception:
        print("    [缺] 没装 torch（IndexTTS-2 必需，服务器上用 CUDA 版）")

    print("=" * 60)
    py = sys.version_info
    fits = py.major == 3 and 10 <= py.minor <= 11
    print(f"  本环境 Python {py.major}.{py.minor}.{py.micro}"
          f"   IndexTTS-2 要求 3.10~3.11 {'[OK]' if fits else '[不匹配]'}")
    if not fits:
        print("    [注意] index-tts 的 pyproject 写死 requires-python >=3.10,<3.12，")
        print("           用现在这个解释器 pip install 会被直接拒绝，得换个 3.11 的环境。")
    print("=" * 60)
    if ok:
        print("  体检通过，可以双击 5-启动语音合成.bat")
    else:
        print("  还没齐，按上面 [缺] 的项补；下面的命令可以照抄。")
        print()
        print("  # 1) 拉源码（放工程里就行，tts_server 会自动加进 sys.path）")
        print(f"  git clone https://github.com/index-tts/index-tts {code_dir}")
        print()
        print("  # 2) 装依赖（注意：Python 必须是 3.10 或 3.11）")
        print(f"  .venv\\Scripts\\python.exe -m pip install -e {code_dir}")
        print("  装不动就用它自带的 uv：")
        print(f"  cd {code_dir} && uv sync --all-extras "
              "--default-index https://mirrors.aliyun.com/pypi/simple")
        print()
        print("  # 3) 下权重（本机已下好 5.49GB，可跳过）")
        print("  python tools/setup_tts_model.py --download")
        print()
        print("  # 4) 放参考音频（3~10 秒干净人声，没有它合成不了）")
        print(f"  把 wav 放进 {voices}，命名 default.wav / sad.wav / happy.wav ...")
    print()
    return ok


def download(local_dir: Path) -> int:
    local_dir.mkdir(parents=True, exist_ok=True)
    try:
        from huggingface_hub import snapshot_download
    except Exception:
        print("没装 huggingface_hub，先装一下（用镜像源）：")
        print("  pip install -i https://mirrors.aliyun.com/pypi/simple/ huggingface_hub")
        return 1

    # 走镜像；同时把 HF_ENDPOINT 设上，snapshot_download 内部请求才会走镜像
    os.environ.setdefault("HF_ENDPOINT", MIRROR)
    print(f"从 {MIRROR} 下载 {REPO} 到 {local_dir}（约 5.5GB，慢慢等）")
    try:
        snapshot_download(
            repo_id=REPO,
            local_dir=str(local_dir),
            endpoint=MIRROR,
            # 白名单按仓库实际文件来：safetensors 是 qwen0.6bemo4-merge 的主权重
            # （1137MB），jinja 是对话模板，漏了这两个目录就不完整、模型起不来。
            allow_patterns=["*.json", "*.yaml", "*.model", "*.pt", "*.pth", "*.txt",
                            "*.safetensors", "*.jinja", "*.jinja2"],
            max_workers=4,
        )
    except Exception as exc:
        print(f"[ERROR] 下载失败：{type(exc).__name__}: {exc}")
        print("        可以设代理再试：set HTTPS_PROXY=http://127.0.0.1:端口")
        return 1
    print("下载完成")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="IndexTTS-2 部署助手")
    parser.add_argument("--check", action="store_true", help="只体检")
    parser.add_argument("--download", action="store_true", help="下载权重")
    args = parser.parse_args()

    s = _settings()
    if args.download:
        code = download(Path(s.TTS_MODEL_DIR))
        if code:
            return code
        # 权重下齐就算这件事成功；源码/参考音频是装机时补的，不该让退出码变成失败
        if not check():
            print()
            print("  权重已下齐。上面标 [缺] 的是源码和参考音频，装机时再补。")
        return 0
    return 0 if check() else 1


if __name__ == "__main__":
    raise SystemExit(main())
