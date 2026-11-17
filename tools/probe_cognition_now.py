"""现状探针（只读）：把「一句话进来」现在被怎么判，原样倒出来。

用途：改 V3 内核之前，先拿真实库核对三件事——
  1) 意图判成了什么（词表覆盖到哪）；
  2) 工具闸门有没有抢话（本该答身份的去联网）；
  3) 记忆里到底能召回什么形状（陈述还是问句）。

只读：不写 trace、不改表、不动运行参数。输出 JSON 到 _audit_final/probe_now.json。
用法（在 companion 目录下）：
    .venv\\Scripts\\python.exe tools\\probe_cognition_now.py
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SAMPLES = [
    "我是谁？",
    "你叫什么？",
    "你还记得我上次说的显卡吗？",
    "我今天很累",
    "帮我查下广元天气",
    "这台机器蓝屏了",
]

OUT = os.path.join(ROOT, "_audit_final", "probe_now.json")


def main() -> None:
    from brain.cognitive import sense, classify
    from brain.services import memory
    from brain.tools import detect

    rows = []

    # 身份卡（不带 user_id 的全局身份；prompt 里每轮必带的那份）
    try:
        ident = memory.retrieve("u1", "身份", 3).get("identity") or {}
    except Exception as exc:  # 探针不因为取不到身份就中断
        ident = {"_error": type(exc).__name__}

    for text in SAMPLES:
        sig = sense(text)
        it = classify(sig)
        try:
            bundle = memory.retrieve("u1", text, 3)
        except Exception as exc:
            bundle = {"_error": type(exc).__name__}
        facts = bundle.get("facts") or []
        rows.append({
            "text": text,
            "intent": it.get("intent"),
            "intent_cn": it.get("intent_cn"),
            "reason": it.get("reason"),
            "hits": it.get("hits"),
            "meta_hits": sig.get("meta_hits"),
            "recall_hits": sig.get("recall_hits"),
            "tech_hits": sig.get("tech_hits"),
            "tool": detect(text),
            "facts_n": len(facts),
            "facts": [str(f.get("text") or "")[:30] for f in facts[:6]],
            "identity": bundle.get("identity"),
        })

    payload = {
        "identity_card": ident,
        "profile_head": str((memory.retrieve("u1", "我是谁", 3).get("profile") or ""))[:400],
        "rows": rows,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    # 控制台只打结构，不打正文，避免编码/隐私问题
    for r in rows:
        print(f"{r['text']} -> intent={r['intent']} tool={r['tool']} facts={r['facts_n']}")
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
