"""下载并部署情感判断模型（一步到位）。

做三件事：
  1. 从 hf-mirror 拉模型文件到 models/chinese-emotion-small/
  2. 缺 model.onnx 时调用 tools/export_onnx.py 转换
  3. 跑一遍自检，确认模型真的能出结论

用法：
    .venv\\Scripts\\python.exe tools\\setup_judge_model.py
    .venv\\Scripts\\python.exe tools\\setup_judge_model.py --skip-export

说明：
  * 下载走系统代理（HTTP_PROXY / HTTPS_PROXY），公司网/校园网里通常已经配好。
  * 导出需要 torch + transformers，只在这一步用；导完以后生产环境
    只需要 onnxruntime + numpy + tokenizers，不用留 torch。
  * 已存在的文件默认跳过，中断了重跑即可。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "Johnson8187/Chinese-Emotion-Small"
MIRROR = "https://hf-mirror.com"
FILES = ("config.json", "tokenizer.json", "tokenizer_config.json",
         "special_tokens_map.json", "added_tokens.json", "model.safetensors")


def _client():
    import httpx
    return httpx.Client(timeout=60, trust_env=True, follow_redirects=True)


def download(dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    c = _client()
    for name in FILES:
        target = dst / name
        url = f"{MIRROR}/{REPO}/resolve/main/{name}"
        try:
            head = c.head(url)
            if head.status_code != 200:
                print(f"  [x] {name} HTTP {head.status_code}")
                return 1
            total = int(head.headers.get("content-length") or 0)
            if target.is_file() and total and target.stat().st_size == total:
                print(f"  [=] {name} 已存在（{total/1048576:.1f} MB），跳过")
                continue
            print(f"  [↓] {name}  {total/1048576:.1f} MB")
            got = 0
            with c.stream("GET", url) as r:
                r.raise_for_status()
                with open(target, "wb") as fp:
                    for chunk in r.iter_bytes(1 << 20):
                        fp.write(chunk)
                        got += len(chunk)
                        if total and got % (32 << 20) < (1 << 20):
                            print(f"      {got/1048576:6.0f}/{total/1048576:.0f} MB", flush=True)
            if total and target.stat().st_size != total:
                print(f"  [x] {name} 大小不符（{target.stat().st_size}/{total}），重跑本脚本续传")
                return 1
        except Exception as exc:
            print(f"  [x] {name} 失败：{type(exc).__name__}: {exc}")
            print("      如果是网络问题，先确认代理可用：echo $HTTPS_PROXY")
            return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dst", default=str(ROOT / "models" / "chinese-emotion-small"))
    ap.add_argument("--skip-export", action="store_true")
    args = ap.parse_args()
    dst = Path(args.dst)

    print(f"[1/3] 下载 {REPO}")
    rc = download(dst)
    if rc:
        return rc

    onnx_file = dst / "model.onnx"
    if onnx_file.is_file():
        print(f"[2/3] ONNX 已存在（{onnx_file.stat().st_size/1048576:.1f} MB），跳过转换")
    elif args.skip_export:
        print("[2/3] 按要求跳过转换")
        return 0
    else:
        print("[2/3] 转换 safetensors → ONNX（需要 torch + transformers）")
        rc = subprocess.call([sys.executable, str(ROOT / "tools" / "export_onnx.py"),
                              "--src", str(dst)])
        if rc:
            print("     转换失败。先装：pip install -i https://mirrors.aliyun.com/pypi/simple/ torch transformers")
            return rc

    print("[3/3] 自检")
    return subprocess.call([sys.executable, str(ROOT / "tools" / "judge_check.py")])


if __name__ == "__main__":
    sys.exit(main())
