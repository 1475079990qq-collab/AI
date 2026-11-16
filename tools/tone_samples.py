"""口吻语料：看看攒了多少、导出成训练格式。

用法：
    python tools/tone_samples.py --count
    python tools/tone_samples.py --export data/tone_samples.jsonl

导出格式是常见的 chat 指令样本（一行一条 JSON）：
    {"messages":[{"role":"system","content":"<人格摘要>"},
                 {"role":"user","content":"<他说的话>"},
                 {"role":"assistant","content":"<它说对了的那句>"}],
     "meta":{"emotion":"...","intensity":0.6,"request_id":"..."}}

--with-persona 才带 system（默认带当前激活人格的开头两段），
方便直接把「同一人格下的说话方式」喂给 LoRA。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain import db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", action="store_true", help="只报条数")
    ap.add_argument("--export", default="", help="导出 JSONL 的路径")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--with-persona", action="store_true", help="带 system 人格摘要")
    args = ap.parse_args()

    total = db.count_tone_samples()
    print(f"口吻语料：{total} 条")

    if args.count or not args.export:
        if total < 300:
            print(f"还差 {300 - total} 条到 300 条的目标线")
        return 0

    persona = db.get_active_persona() if args.with_persona else None
    system_text = ""
    if persona:
        system_text = str(persona.get("system_prompt") or "").strip()[:400]

    rows = db.list_tone_samples(limit=args.limit)
    out = Path(args.export)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            messages = []
            if system_text:
                messages.append({"role": "system", "content": system_text})
            messages.append({"role": "user", "content": r["input_text"]})
            messages.append({"role": "assistant", "content": r["reply_text"]})
            fh.write(json.dumps({
                "messages": messages,
                "meta": {
                    "emotion": r["emotion_label"],
                    "intensity": r["intensity"],
                    "request_id": r["request_id"],
                    "created_at": r["created_at"],
                },
            }, ensure_ascii=False) + "\n")
            n += 1
    print(f"已导出 {n} 条 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
