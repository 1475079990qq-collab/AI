"""模型能力进化（离线，不参与每轮对话）。

原则：不每轮训练、不在线改权重。只在「他自己点过『对了』」的回复攒够量之后，
导出成对话格式的语料，再拿去做 LoRA。

数据和训练分开：
- 语料来源是 tone_samples（反馈里 verdict=+1 才会写进去，见 routers/chat.py）
- 这里只负责「够不够、能不能导」，不负责真的开训练进程

阈值 200 条不是拍的：低于这个量，LoRA 学到的更可能是重复句式而不是语感。
"""
from __future__ import annotations

import datetime
import json
import logging
import os

from brain import db

log = logging.getLogger(__name__)

MIN_SAMPLES = 200


def readiness() -> dict:
    """离能训一次还差多少。"""
    conn = db.connect()
    try:
        row = conn.execute("SELECT COUNT(*) FROM tone_samples").fetchone()
        n = int(row[0] or 0)
    except Exception:
        n = 0
    finally:
        conn.close()
    return {"samples": n, "min_samples": MIN_SAMPLES,
            "ready": n >= MIN_SAMPLES,
            "gap": max(0, MIN_SAMPLES - n)}


def export_dataset(out_dir: str | None = None, limit: int = 2000) -> dict:
    """把语料导成 JSONL（OpenAI messages 格式），返回路径与条数。"""
    base = out_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "data", "exports")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "lora-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl")
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT input_text, reply_text FROM tone_samples "
            "ORDER BY id DESC LIMIT ?", (int(limit),),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    written = 0
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            user_text = str(r["input_text"] or "").strip()
            reply = str(r["reply_text"] or "").strip()
            if len(reply) < 8 or not user_text:
                continue
            fh.write(json.dumps({
                "messages": [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": reply},
                ]
            }, ensure_ascii=False) + "\n")
            written += 1
    return {"path": path, "written": written, "skipped": len(rows) - written,
            "ready": written >= MIN_SAMPLES}
