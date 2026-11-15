"""情感判断自检：同一批句子，同时看「规则」和「模型」的结论与耗时。

用途有两个：
1. 装完模型后确认它真的在干活，而不是静默降级成规则；
2. 调 label_map / 换模型时对比效果。

用法：
    .venv\\Scripts\\python.exe tools\\judge_check.py
    .venv\\Scripts\\python.exe tools\\judge_check.py --engine onnx --repeat 20
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLES: list[tuple[str, str]] = [
    ("今天真的累死了，一点力气都没有", "sad"),
    ("好吧，那就这样吧", "neutral"),
    ("谢谢你一直陪着我", "care"),
    ("你居然记得我生日！", "surprised"),
    ("烦死了，别问了", "angry"),
    ("我怕明天面试过不了", "fear"),
    ("这事儿听着就恶心", "disgust"),
    ("今天天气不错，心情挺好", "happy"),
    ("在吗", "neutral"),
    ("我有点撑不住了", "sad"),
    ("你也早点休息，别熬太晚", "care"),
    ("这也能行？", "surprised"),
    ("我不想说话", "sad"),
    ("随便吧", "neutral"),
    ("有点紧张，睡不着", "fear"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="", help="留空=按配置；rule / onnx")
    ap.add_argument("--repeat", type=int, default=3, help="每条句子跑几次取平均")
    args = ap.parse_args()

    from brain.config import settings
    from brain.services import judge as J
    from brain.services import sentiment_model

    st = J.engine_status()
    print("=" * 78)
    print(f"引擎配置 {st['engine']}  →  实际生效 {st['effective']}")
    m = st.get("model") or {}
    print(f"模型路径 {st['model_path'] or '（未配置）'}")
    if m:
        print(f"模型 {m.get('size_mb')}MB  已加载={m.get('loaded')}  输入={m.get('inputs')}")
        if m.get("error"):
            print(f"加载错误 {m['error']}")
    print("=" * 78)

    if not m.get("exists"):
        print("[x] 模型文件不齐（需要 model.onnx + tokenizer.json），下面只能跑规则")
        return 1

    # 预热：第一次推理包含图优化与内存分配，不计入统计
    t0 = time.perf_counter()
    first = sentiment_model.predict(settings.JUDGE_MODEL_PATH, "预热")
    warm = (time.perf_counter() - t0) * 1000
    print(f"首次推理（含图优化）{warm:.0f} ms\n")

    if first is None:
        print(f"[x] 推理返回空：{sentiment_model.load_error()}")
        return 1

    head = f"{'句子':<22}{'模型':<10}{'p':<7}{'纯规则':<9}{'耗时ms':<9}备注"
    print(head)
    print("-" * 78)

    hit = 0
    times: list[float] = []
    for text, expect in SAMPLES:
        t = time.perf_counter()
        out = None
        for _ in range(max(1, args.repeat)):
            out = sentiment_model.predict(settings.JUDGE_MODEL_PATH, text)
        cost = (time.perf_counter() - t) * 1000 / max(1, args.repeat)
        times.append(cost)
        # 纯规则要用 _rule，直接调 judge() 会走 auto 引擎、拿到的还是模型结果
        rule = J._rule(text, [])
        label = out["label"] if out else "ERR"
        p = out["confidence"] if out else 0
        mark = "✓" if label == expect else ("· 期望 " + expect)
        if label == expect:
            hit += 1
        print(f"{text[:20]:<22}{label:<10}{p:<7.2f}{rule.label:<9}{cost:<9.1f}{mark}")

    times.sort()
    print("-" * 78)
    print(f"命中 {hit}/{len(SAMPLES)}   延迟 中位 {times[len(times)//2]:.1f} ms  "
          f"P90 {times[int(len(times)*0.9)]:.1f} ms  max {times[-1]:.1f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
