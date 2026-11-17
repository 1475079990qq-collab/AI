"""在测试实例上跑一串真实对话，把「认知调度」下发了什么、回复长什么样打出来。

    .venv\\Scripts\\python.exe tools\\try_turn.py --base http://127.0.0.1:8010
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def turn(base: str, user_id: str, session_id: str, text: str) -> dict:
    import httpx

    out = {"text": text, "reply": "", "events": [], "intent": None, "plan": None,
           "max_tokens": None, "regions": None, "budget": None, "request_id": None,
           "memory": None}
    buffer = ""
    with httpx.stream("POST", f"{base}/chat",
                      json={"user_id": user_id, "session_id": session_id, "text": text},
                      timeout=180.0) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                data = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
                if not data:
                    continue
                try:
                    ev = json.loads("\n".join(data))
                except Exception:
                    continue
                kind = ev.get("type")
                if kind:
                    out["events"].append(kind)
                if kind == "intent":
                    out["intent"] = ev.get("intent_cn") or ev.get("intent")
                    out["plan"] = ev.get("plan")
                    out["regions"] = ev.get("regions")
                    out["budget"] = ev.get("budget")
                elif kind == "llm_start":
                    out["max_tokens"] = ev.get("max_tokens")
                    out["request_id"] = ev.get("request_id")
                elif kind == "memory":
                    out["memory"] = {"facts": ev.get("facts"), "concepts": ev.get("concepts"),
                                     "neurons": len(ev.get("neurons") or [])}
                elif kind == "meta":
                    out["request_id"] = out["request_id"] or ev.get("request_id")
                elif kind in ("token", "tokens"):
                    out["reply"] += ev.get("delta") or ""
                elif kind == "done":
                    if ev.get("text"):
                        out["reply"] = ev["text"]
    return out


CASES = [
    "我叫阿伟，在成都做运维，最近在折腾本地大模型",
    "我的模型跑起来特别慢，7B 的，显存 8G，是不是没救",
    "那量化会好一点吗",
    "嗯",
    "今天有点累，不太想折腾了",
    "我刚才跟你说的那台机器，显存多大",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8010")
    ap.add_argument("--user", default="u-e2e-v2")
    ap.add_argument("--session", default="s-e2e-v2")
    args = ap.parse_args()

    for i, text in enumerate(CASES, 1):
        t0 = time.time()
        r = turn(args.base, args.user, args.session, text)
        print(f"\n[{i}] 用户：{text}")
        print(f"    意图：{r['intent']}  计划：{r['plan']}")
        print(f"    参与脑区：{r['regions']}  预算：{r['budget']}")
        print(f"    max_tokens={r['max_tokens']}  回忆：{r['memory']}  "
              f"耗时 {time.time() - t0:.1f}s")
        print(f"    事件流：{' → '.join(r['events'])}")
        print(f"    回复（{len(r['reply'])} 字）：{r['reply']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

