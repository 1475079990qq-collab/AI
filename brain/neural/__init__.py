"""神经网络模拟层（V2 · 阶段二）：节点、突触、激活、可塑性、生长。

设计约束（写在这里，防止后来的人又把它做回装饰动画）：
1. 节点和突触存在 SQLite 里，前端画的是这份数据，不是自己随机生成的点；
2. 激活有真实来源（这一轮召回了哪几条记忆），可回放到 activations 表；
3. 衰减只降激活度和连接权重，不降 importance、不删记忆；
4. 概念节点是「反复出现的词」，不是词典切词拼出来的噪声。
"""
from __future__ import annotations

from . import activation, growth, neuron, plasticity, synapse

__all__ = ["activation", "growth", "neuron", "plasticity", "synapse"]
