"""准备语音识别模型（SenseVoice，sherpa-onnx 版）：下模型、体检。

用法：
    python tools/setup_asr_model.py --check      # 只体检，告诉你缺什么
    python tools/setup_asr_model.py --download   # 下模型到 models/sense-voice（约 156MB）

为什么选它：
    中文识别这块，SenseVoice（阿里达摩院）在开源的里最准，而且是**纯 ONNX、
    纯 CPU**，跟情感判断模型用的是同一套 onnxruntime，不占显存、不碰显卡驱动。
    sherpa-onnx 的 wheel 自带引擎，装了不会动现有依赖（实测 onnxruntime 版本不变）。

    模型 155MB（int8 量化），10 秒音频 CPU 上 0.3 秒左右出字。

和小智的关系：
    小智本地 ASR 只有三条路——FunASR、sherpa-onnx、Vosk。它那个
    `sherpa_onnx_local.py` 走的就是这个引擎，模型可以共用。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
ARCHIVE = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
URL = f"{RELEASE}/{ARCHIVE}.tar.bz2"

# 少一个都跑不起来
REQUIRED = ("model.int8.onnx", "tokens.txt")


def _settings():
    from brain.config import settings

    return settings


def _model_dir() -> Path:
    return Path(_settings().ASR_MODEL_DIR)


def check() -> bool:
    s = _settings()
    model_dir = _model_dir()

    print("=" * 60)
    print("  语音识别 体检（SenseVoice + sherpa-onnx）")
    print("=" * 60)
    print(f"  模型目录  {model_dir}")

    ok = True
    missing = [n for n in REQUIRED if not (model_dir / n).is_file()]
    if model_dir.is_dir():
        size = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
        print(f"    体积 {size / 1024 ** 2:.0f} MB")
    if missing:
        ok = False
        print(f"    [缺] {' / '.join(missing)}")
    else:
        print("    [OK] 模型文件齐全")

    try:
        import sherpa_onnx  # noqa: F401

        print("    [OK] sherpa-onnx 已安装")
    except Exception as exc:
        ok = False
        print(f"    [缺] sherpa-onnx 没装（{type(exc).__name__}）")

    print(f"  监听端口  {s.ASR_PORT}")
    print(f"  识别语言  {s.ASR_LANGUAGE}（auto = 自动判断中/英/日/韩/粤）")
    print("=" * 60)

    if ok:
        print("  体检通过，可以双击 7-启动语音识别.bat")
    else:
        print("  还没齐，按下面两步补：")
        print()
        print("  # 1) 装引擎（纯 Python 包，不动现有依赖）")
        print('  ".venv\\Scripts\\python.exe" -m pip install sherpa-onnx')
        print()
        print("  # 2) 下模型（约 156MB）")
        print('  ".venv\\Scripts\\python.exe" tools\\setup_asr_model.py --download')
    print()
    return ok


def download() -> int:
    import httpx

    target = _model_dir()
    target.mkdir(parents=True, exist_ok=True)
    archive = target.parent / f"{ARCHIVE}.tar.bz2"

    print("=" * 60)
    print("  下载 SenseVoice 模型（约 156MB）")
    print("=" * 60)
    print(f"  来源  {URL}")
    print(f"  落地  {target}")
    print()

    got = 0
    with httpx.Client(trust_env=True, timeout=60, follow_redirects=True) as client:
        with client.stream("GET", URL) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            with archive.open("wb") as fh:
                for chunk in resp.iter_bytes(65536):
                    fh.write(chunk)
                    got += len(chunk)
                    if total:
                        pct = got * 100 // total
                        if got % (1024 * 1024 * 8) < 65536:
                            print(f"    {pct:3d}%  {got / 1024 ** 2:.0f} / {total / 1024 ** 2:.0f} MB")

    print(f"  下载完成 {got / 1024 ** 2:.0f} MB，开始解压 ...")

    with tarfile.open(archive, "r:bz2") as tar:
        members = tar.getmembers()
        tar.extractall(target.parent, members=members, filter="data")

    # 解出来的是一层同名目录，把内容挪到 models/sense-voice/
    unpacked = target.parent / ARCHIVE
    if unpacked.is_dir():
        for item in unpacked.iterdir():
            dest = target / item.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            shutil.move(str(item), str(dest))
        unpacked.rmdir()

    archive.unlink(missing_ok=True)

    missing = [n for n in REQUIRED if not (target / n).is_file()]
    if missing:
        print(f"  [失败] 解压后仍缺 {' / '.join(missing)}")
        return 1

    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"  [OK] 模型就绪，{size / 1024 ** 2:.0f} MB")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="准备语音识别模型")
    parser.add_argument("--check", action="store_true", help="只体检")
    parser.add_argument("--download", action="store_true", help="下模型")
    args = parser.parse_args()

    if not args.check and not args.download:
        parser.print_help()
        return 0

    code = 0
    if args.download:
        code = download()
        if code:
            return code
    return 0 if check() else 1


if __name__ == "__main__":
    raise SystemExit(main())
