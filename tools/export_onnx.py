"""把 HuggingFace safetensors 情绪模型转成 FP32 ONNX。

一次性工具，**生产环境不需要 torch / transformers**。
转完以后 brain 只需要 onnxruntime + numpy + tokenizers 就能跑。

用法：
    .venv\\Scripts\\python.exe tools\\export_onnx.py
    .venv\\Scripts\\python.exe tools\\export_onnx.py --src models/chinese-emotion-small

输入目录约定：
    <src>/model.safetensors     权重
    <src>/config.json           结构 + id2label
    <src>/tokenizer.json        分词器（由 brain 直接读取，不参与导出）

输出：
    <src>/model.onnx            FP32，动态 batch/seq 轴
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="models/chinese-emotion-small")
    ap.add_argument("--opset", type=int, default=14, help="ORT 1.31 支持到 21，14 最稳")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_absolute():
        src = ROOT / src
    if not (src / "model.safetensors").is_file():
        print(f"[x] 找不到权重：{src / 'model.safetensors'}")
        return 1

    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:
        print(f"[x] 缺转换依赖：{exc}")
        print("    装一次即可：pip install torch transformers")
        return 1

    print(f"[1/4] 读取模型 {src}")
    tok = AutoTokenizer.from_pretrained(str(src))
    model = AutoModelForSequenceClassification.from_pretrained(str(src))
    model.eval()

    n_param = sum(p.numel() for p in model.parameters())
    print(f"      结构 {model.config.architectures} / 参数量 {n_param/1e6:.1f}M")
    print(f"      标签 {[model.config.id2label.get(i) for i in range(model.config.num_labels)]}")

    print("[2/4] 构造示例输入")
    # 固定长度 48 的静态图：
    #   torch 2.15 的旧导出路径对 dynamic_axes 的底层接口已变，传 dict 会报
    #   _jit_pass_onnx_set_dynamic_input_shape 参数不兼容；而 DeBERTa-v2 的
    #   解耦注意力本身带动态控制流，动态轴更容易导出成哑巴或失败。
    #   陪伴场景单句极少超过 48 字，静态图在 ORT 上还能吃到更多常量折叠。
    MAX_LEN = 48
    enc = tok("今天有点累，想聊聊", return_tensors="pt",
              max_length=MAX_LEN, padding="max_length", truncation=True)
    keep = [k for k in enc.keys() if k in ("input_ids", "attention_mask")]
    inputs = tuple(enc[k] for k in keep)
    names = tuple(keep)
    print(f"      输入 {names} 形状 {[tuple(t.shape) for t in inputs]}  (固定长度)")

    class Wrapper(torch.nn.Module):
        """只吐 logits，导出图更干净，也省掉 ORT 里没用的输出分支。"""

        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, input_ids, attention_mask):
            out = self.m(input_ids=input_ids, attention_mask=attention_mask)
            return out.logits

    out_path = src / "model.onnx"
    print(f"[3/4] 导出 {out_path.name}（opset {args.opset}，静态 1x{MAX_LEN}）")
    with torch.no_grad():
        torch.onnx.export(
            Wrapper(model),
            inputs,
            str(out_path),
            input_names=list(names),
            output_names=["logits"],
            opset_version=args.opset,
            do_constant_folding=True,
            dynamo=False,
        )

    size_mb = out_path.stat().st_size / 1048576
    print(f"[4/4] 完成：{out_path}  {size_mb:.1f} MB")

    # 立即用 ONNX 复算一次，和 torch 结果对齐，避免导出成哑巴模型
    try:
        import numpy as np
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        sess = ort.InferenceSession(str(out_path), sess_options=so,
                                    providers=["CPUExecutionProvider"])
        feeds = {n: v.numpy() for n, v in zip(names, inputs)}
        logits = sess.run(None, feeds)[0].reshape(-1)
        probs = np.exp(logits - logits.max())
        probs = probs / probs.sum()
        idx = int(probs.argmax())
        label = model.config.id2label.get(idx, str(idx))
        print(f"      自检：样例 -> {label} (p={probs[idx]:.3f})  输入名 {[i.name for i in sess.get_inputs()]}")
    except Exception as exc:
        print(f"      [warn] 自检没跑通：{type(exc).__name__}: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
