"""口吻 LoRA 训练（本地单卡，P104 8G 可跑）。

默认按这台机器的现实取值：fp16（P104 是计算能力 6.1，没有 bf16）、
seq 1024、batch 1 + 梯度累积 8、只训 LoRA 旁路参数、开梯度检查点。

用法：
    python tools/train_lora.py --dry-run                 # 先自检：依赖/数据/显存估算，不训练
    python tools/train_lora.py --base-model F:\\models\\MiniCPM5-2B-HF
    python tools/train_lora.py --base-model ... --merge  # 训完把 LoRA 融进权重，导出可转换的目录

预计耗时（P104 8G、2B、300 条、3 epoch）：约 40–90 分钟/epoch，属估算，首次以实测为准。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MIN_TRAIN = 200


def _deps() -> dict[str, bool]:
    status: dict[str, bool] = {}
    for name in ("torch", "transformers", "peft", "accelerate", "datasets", "bitsandbytes"):
        try:
            __import__(name)
            status[name] = True
        except Exception:
            status[name] = False
    return status


def _gpu() -> str:
    try:
        import torch

        if not torch.cuda.is_available():
            return "CUDA 不可用（torch 可能是 CPU 版）"
        name = torch.cuda.get_device_name(0)
        mem = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
        cap = torch.cuda.get_device_capability(0)
        return f"{name} {mem:.1f}GB cc{cap[0]}.{cap[1]}"
    except Exception as exc:  # pragma: no cover
        return f"探测失败：{exc}"


def _count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-model", default="", help="HuggingFace 原始权重目录（不是 GGUF）")
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "lora"))
    ap.add_argument("--out", default=str(ROOT / "data" / "lora" / "adapter"))
    ap.add_argument("--seq-len", type=int, default=1024)
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--dropout", type=float, default=0.05)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--qlora", action="store_true", help="4bit 量化训练（bitsandbytes 可用且显存更紧时用）")
    ap.add_argument("--merge", action="store_true", help="训完合并权重，输出可转 GGUF 的目录")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="数据不足 MIN_TRAIN 也硬跑")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    train_n = _count(data_dir / "train.jsonl")
    valid_n = _count(data_dir / "valid.jsonl")

    print("== 环境自检 ==")
    deps = _deps()
    for k, v in deps.items():
        print(f"  {'OK ' if v else '缺 '} {k}")
    print(f"  GPU: {_gpu()}")
    print(f"  数据: train={train_n} valid={valid_n} @ {data_dir}")
    print(f"  配置: fp16 seq={args.seq_len} batch={args.batch} ga={args.grad_accum} "
          f"rank={args.rank} epochs={args.epochs} {'QLoRA(4bit)' if args.qlora else 'LoRA(fp16)'}")

    missing = [k for k in ("torch", "transformers", "peft", "accelerate") if not deps[k]]
    if missing:
        print("\n缺依赖，先装：")
        print("  .venv\\Scripts\\python.exe -m pip install " + " ".join(missing))
        if args.qlora:
            print("  另需 bitsandbytes（4bit 训练）；P104 上支持不稳，失败就退回不加 --qlora")
        return 1

    if train_n == 0:
        print("\n没有训练集，先跑： python tools/build_lora_dataset.py --with-persona")
        return 1
    if train_n < MIN_TRAIN and not args.force:
        print(f"\n训练集只有 {train_n} 条，低于起步线 {MIN_TRAIN}。"
              f"先多攒点「对了」再训，或用 --force 试跑。")
        return 1
    if not args.base_model:
        print("\n没给 --base-model。需要一个 HuggingFace 原始权重目录（GGUF 不能直接训）。")
        return 1
    base = Path(args.base_model)
    if not (base / "config.json").exists():
        print(f"\n{base} 下没有 config.json，不像 HF 权重目录。")
        return 1
    if args.dry_run:
        print("\n自检通过，可以开始训练（去掉 --dry-run）。")
        return 0

    # ---- 真正训练 ----
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (AutoModelForCausalLM, AutoTokenizer,
                              Trainer, TrainingArguments)

    tokenizer = AutoTokenizer.from_pretrained(str(base), trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    kwargs: dict = {"trust_remote_code": True, "torch_dtype": torch.float16}
    if args.qlora:
        from transformers import BitsAndBytesConfig

        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True, bnb_4bit_quant_type="nf4")
    model = AutoModelForCausalLM.from_pretrained(str(base), **kwargs)
    model.config.use_cache = False
    if args.qlora:
        model = prepare_model_for_kbit_training(model)
    model.gradient_checkpointing_enable()

    lora = LoraConfig(
        r=args.rank, lora_alpha=args.alpha, lora_dropout=args.dropout,
        bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    def encode(example: dict) -> dict:
        msgs = example["messages"]
        prompt_text = tokenizer.apply_chat_template(
            msgs[:-1], tokenize=False, add_generation_prompt=True)
        full_text = prompt_text + msgs[-1]["content"] + tokenizer.eos_token
        p_ids = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
        f_ids = tokenizer(full_text, truncation=True, max_length=args.seq_len,
                          add_special_tokens=False)["input_ids"]
        labels = list(f_ids)
        n_mask = min(len(p_ids), len(labels))
        for i in range(n_mask):
            labels[i] = -100            # 只学回复，不学用户的话
        return {"input_ids": f_ids, "labels": labels, "attention_mask": [1] * len(f_ids)}

    def load(path: Path) -> Dataset:
        rows = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return Dataset.from_list(rows).map(encode, remove_columns=["messages"])

    train_ds = load(data_dir / "train.jsonl")
    valid_ds = load(data_dir / "valid.jsonl") if valid_n else None

    from transformers import DataCollatorForSeq2Seq

    collator = DataCollatorForSeq2Seq(tokenizer=tokenizer, padding=True,
                                      label_pad_token_id=-100, return_tensors="pt")

    targs = TrainingArguments(
        output_dir=str(Path(args.out) / "run"),
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.grad_accum,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        logging_steps=10,
        save_strategy="epoch",
        save_total_limit=1,
        fp16=True,
        gradient_checkpointing=True,
        report_to="none",
        remove_unused_columns=False,
        dataloader_num_workers=0,
        eval_strategy="epoch" if valid_ds is not None else "no",
    )

    trainer = Trainer(model=model, args=targs, train_dataset=train_ds,
                      eval_dataset=valid_ds, data_collator=collator)
    trainer.train()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))
    print(f"\nLoRA 权重已存：{out_dir}")

    if args.merge:
        merged = Path(str(out_dir) + "-merged")
        model = model.merge_and_unload()
        model.save_pretrained(str(merged), safe_serialization=True)
        tokenizer.save_pretrained(str(merged))
        print(f"合并后权重：{merged}")
        print("下一步转 GGUF（需 llama.cpp）：")
        print(f"  python convert_hf_to_gguf.py {merged} --outfile tone-2b-f16.gguf --outtype f16")
        print("  llama-quantize tone-2b-f16.gguf tone-2b-q4km.gguf Q4_K_M")
        print("然后把 providers.llm 的 model 指向新 gguf，重启大脑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
