"""口吻语料 → LoRA 训练集。

从 tone_samples 表取「他点过对了」的样本，清洗、去重、切分，落成 chat 格式 JSONL。
不训练，只准备料——这样训练脚本只管训，数据口径只有一处。

用法：
    python tools/build_lora_dataset.py                 # 生成 data/lora/train.jsonl + valid.jsonl
    python tools/build_lora_dataset.py --with-persona  # 带上人格 system（推荐，语境一致）
    python tools/build_lora_dataset.py --dry-run       # 只看会产出多少条，不写文件
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain import db  # noqa: E402

# 只留"像人话"的样本：太短、带明显客服腔、或含旁白的直接剔掉，
# 训练集里混进一句「抱歉刚才没明确回应你的需求」，学出来的就是客服。
_MIN_REPLY = 8
_MAX_REPLY = 220
_BAD_MARKERS = ("抱歉刚才", "下次需要帮忙的话", "我确实无法", "作为AI", "作为 AI",
                "我理解你的感受", "希望能帮到你", "还有什么可以帮")
_META_PAREN = re.compile(r"[（(][^）)]*(语气|情绪|接住|开放|轻量|试探)[^）)]*[）)]")


def _usable(row: dict) -> bool:
    reply = str(row.get("reply_text") or "").strip()
    user = str(row.get("input_text") or "").strip()
    if len(user) < 2 or not (_MIN_REPLY <= len(reply) <= _MAX_REPLY):
        return False
    if _META_PAREN.search(reply):
        return False
    return not any(m in reply for m in _BAD_MARKERS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "lora"))
    ap.add_argument("--with-persona", action="store_true", help="带当前人格做 system")
    ap.add_argument("--persona-chars", type=int, default=400, help="system 截断长度")
    ap.add_argument("--valid-ratio", type=float, default=0.05)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    total = db.count_tone_samples()
    rows = db.list_tone_samples(limit=5000)
    good = [r for r in rows if _usable(r)]
    dropped = total - len(good)

    system_text = ""
    if args.with_persona:
        persona = db.get_active_persona() or {}
        system_text = str(persona.get("system_prompt") or "").strip()[: args.persona_chars]

    random.Random(20261116).shuffle(good)
    n_valid = max(1, int(len(good) * args.valid_ratio)) if len(good) >= 20 else 0
    valid, train = good[:n_valid], good[n_valid:]

    print(f"语料总数 {total}，可用 {len(good)}，剔除 {dropped}")
    print(f"训练 {len(train)} / 验证 {len(valid)}，system {'带' if system_text else '不带'}")

    if args.dry_run:
        if len(train) < 200:
            print(f"提示：训练集 {len(train)} 条，低于 200 条起步线；可以先用 --force 试跑，但学不出稳定口吻。")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, part in (("train.jsonl", train), ("valid.jsonl", valid)):
        path = out_dir / name
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for r in part:
                messages = []
                if system_text:
                    messages.append({"role": "system", "content": system_text})
                messages.append({"role": "user", "content": str(r["input_text"]).strip()})
                messages.append({"role": "assistant", "content": str(r["reply_text"]).strip()})
                fh.write(json.dumps({"messages": messages}, ensure_ascii=False) + "\n")
        print(f"写入 {path}  {len(part)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
