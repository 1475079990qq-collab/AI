"""工具层：让它能真去办，而不是只会说。

为什么单独一层：模型再会说，也只是「嘴」。要「给到手上来」，手里得有东西——
查到的、记住的、提醒的。工具层就是那双手。

纪律：
* 工具自己不做判断、不编内容，只返回真实结果或真实的失败原因；
* 任何工具失败都不许伪装成成功（宁可说「没查到」，也不许编一条）；
* 判定用规则（快、可解释），不用模型再判一次（那会多一轮 3 秒）。
"""
from __future__ import annotations

import asyncio
import time

from brain import db
from brain.tools import kb, local, web

# 名字 → 实现
_REGISTRY = {
    "remember": local.remember,
    "remind": local.remind,
    "recall": local.recall,
    "kb_search": kb.search,
    "web_search": web.search,
}

# 判定用的触发词。顺序即优先级：先认「让他记住」，再认「提醒」，再认「查自己的」，最后才是上网查。
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("remember", ("记一下", "记住", "帮我记住", "别忘了", "记着")),
    ("remind", ("提醒我", "提醒一下", "到点提醒", "叫我", "记得提醒")),
    ("kb_search", ("知识库", "我存的", "我记的笔记", "我之前存的", "资料里", "文档里")),
    ("web_search", ("查一下", "查一查", "搜一下", "搜一搜", "搜索", "网上", "新闻",
                    "天气", "多少钱", "哪家", "怎么走", "什么价", "最新", "是谁",
                    "怎么样", "是什么", "什么时候", "有哪些")),
)

# 去掉触发词后的「要办什么」
_PREFIX_STRIP = (
    "帮我", "给我", "替我", "麻烦", "你能不能", "能不能", "可以", "请你",
    "记一下", "记住", "帮我记住", "别忘了", "记着",
    "提醒我", "提醒一下", "到点提醒", "叫我", "记得提醒",
    "查一下", "查一查", "搜一下", "搜一搜", "搜索", "网上查", "帮我查",
)


def detect(text: str) -> tuple[str, str] | None:
    """判定这一句要不要动工具。返回 (tool, 参数原文) 或 None。

    只认明确的「办事」语气；「我今天好累」这种纯情绪不给工具，
    否则每句话都去查一遍，既慢又像在敷衍人。
    """
    body = str(text or "").strip()
    if len(body) < 3:
        return None
    for tool, words in _RULES:
        if any(w in body for w in words):
            arg = body
            for p in _PREFIX_STRIP:
                if arg.startswith(p):
                    arg = arg[len(p):]
            arg = arg.strip("，,。！？?：: 　")
            if len(arg) < 2:
                arg = body
            return tool, arg
    return None


async def run(tool: str, arg: str, *, user_id: str, request_id: str = "") -> dict:
    """执行一个工具，并把这一笔记账（tool_runs）。

    返回 {ok, summary, data}：summary 直接进提示词，data 供卡片使用。
    """
    fn = _REGISTRY.get(tool)
    if fn is None:
        return {"ok": False, "summary": f"没有叫 {tool} 的工具", "data": {}}
    started = time.perf_counter()
    try:
        # 全部同步 IO（SQLite / HTTP）：丢线程池，绝不阻塞 SSE 事件循环
        result = await asyncio.to_thread(fn, arg, user_id=user_id)
    except Exception as exc:
        result = {"ok": False, "summary": f"工具执行失败：{type(exc).__name__}", "data": {}}
    ms = (time.perf_counter() - started) * 1000
    try:
        db.add_tool_run(
            request_id=request_id, user_id=user_id, tool=tool,
            args=arg, ok=bool(result.get("ok")),
            summary=str(result.get("summary") or ""), ms=ms,
        )
    except Exception:
        pass
    result["tool"] = tool
    result["ms"] = round(ms, 1)
    return result


def run_sync(tool: str, arg: str, *, user_id: str, request_id: str = "") -> dict:
    """同步入口：给脚本 / 冒烟测试用（链路里仍然走 await run）。"""
    return asyncio.run(run(tool, arg, user_id=user_id, request_id=request_id))


def prompt_block(tool: str, arg: str, result: dict) -> str:
    """把工具结果压成一段给模型看的「刚查到的」，明确要求它只能用这些。"""
    label = {
        "web_search": "刚在网上查到的",
        "kb_search": "刚从你的知识库里查到的",
        "remember": "刚替你记下的",
        "remind": "刚替你记下的提醒",
        "recall": "你之前记下的",
    }.get(tool, "刚办完的事")
    detail = result.get("summary") or ""
    if not result.get("ok"):
        return (
            f"【{label}】这次没办成：{detail}。\n"
            f"**不要编内容**：就照实说没查到/没记上，并说明原因；不许假装知道。"
        )
    items = result.get("data", {}).get("items") or []
    lines = []
    for it in items[:5]:
        if isinstance(it, dict):
            t = str(it.get("title") or "").strip()
            s = str(it.get("snippet") or "").strip()
            u = str(it.get("url") or "").strip()
            lines.append(f"- {t}｜{s}" + (f"｜{u}" if u else ""))
    extra = "\n".join(lines)
    return (
        f"【{label}】（围绕「{arg}」）\n{detail}\n{extra}\n"
        f"回答要求：只用上面这些内容说话，不要补充来源里没有的事实；"
        f"如果上面的内容不够回答，就直接说「这个我没查到」并给出你确定的部分。"
    )
