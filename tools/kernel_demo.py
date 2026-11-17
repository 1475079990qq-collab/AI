"""内核演示：用真实库跑几句，看它到底在想什么、凭什么这么说。

用法（在 companion 目录下）：
    .venv\\Scripts\\python.exe tools\\kernel_demo.py

刻意不给模型：这几句走模板就能说准。演示的重点是——
    答案在调用大模型之前就已经定了。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from brain.kernel import run_turn, snapshot_from_db  # noqa: E402

CASES = [
    "我是谁？",
    "你叫什么？",
    "你还记得我上次说的显卡吗？",
    "我今天很累",
    "帮我查下广元天气",
    "这台机器蓝屏了",
]

OUT = os.path.join(ROOT, "_audit_final", "kernel_demo.json")


def main() -> None:
    rows = []
    for text in CASES:
        snap = snapshot_from_db("u1", query=text, top_k=5)
        out = run_turn(text, snap, model_fn=None)   # 不接模型
        rows.append({
            "input": text,
            "speech_act": out.plan.speech_act,
            "uncertainty": out.plan.uncertainty.value,
            "by": out.render.by if out.render else "none(需要先动手)",
            "text": out.final_text,
            "awaiting_action": out.awaiting_action,
            "must_not": list(out.plan.must_not),
            "events": out.events,
        })
        print(f"\n=== {text}")
        print(f"    类型={out.plan.speech_act}  确定性={out.plan.uncertainty.value}"
              f"  由谁产出={rows[-1]['by']}")
        print(f"    回答：{out.final_text or '（先执行动作，暂不说话）'}")
        for e in out.events:
            print("      " + str(e["seq"]).rjust(2) + f". [{e['system']}] {e['action']}"
                  + (f" | 因为 {e['because']}" if e["because"] else ""))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    print(f"\nwritten: {OUT}")


if __name__ == "__main__":
    main()
