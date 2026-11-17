"""自我进化的三个层次（V2 · 阶段三）。

    记忆进化     —— 自动：写进来就带类型和重要度，被想起就涨，被否掉就降。
    认知策略进化 —— 半自动：同类教训攒成候选，攒够验证才启用，可回滚。
    模型能力进化 —— 离线：好回复攒成语料，够量才 LoRA，不在每轮对话里训练。

刻意不做的事：不在对话结束后改源码、不改人格文本、不让模型重写自己的系统提示。
那些做法看起来「进化」，实际上是不可回滚的漂移。
"""
from __future__ import annotations

from .feedback import on_verdict  # noqa: F401
from .strategy import active_line, candidate_from_lesson, promote_validated  # noqa: F401

__all__ = ["on_verdict", "active_line", "candidate_from_lesson", "promote_validated"]
