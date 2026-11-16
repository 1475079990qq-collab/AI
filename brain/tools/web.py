"""上网查：只拿公开网页结果，不编、不猜。

为什么不用某个「搜索 API」：那要先申请 key、绑账号，而这条链路是本地的。
这里走公开的无 key 端点（必应 RSS / DuckDuckGo），谁都能用；
全部失败时如实回「没查到」，绝不拿模型记忆冒充搜索结果。
"""
from __future__ import annotations

import html
import os
import re
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import httpx

_TIMEOUT = float(os.environ.get("SEARCH_TIMEOUT", "8") or 8)
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_TAG_RE = re.compile(r"<[^>]+>")


def _text(raw: str, limit: int = 160) -> str:
    s = html.unescape(_TAG_RE.sub(" ", str(raw or "")))
    s = " ".join(s.split())
    return s[:limit] + ("…" if len(s) > limit else "")


def _bing_rss(query: str, host: str) -> list[dict]:
    url = f"https://{host}/search?q={quote_plus(query)}&format=rss&count=8"
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": _UA}, trust_env=False) as client:
        resp = client.get(url)
    if resp.status_code >= 400 or not resp.text.strip():
        return []
    root = ET.fromstring(resp.text)
    items: list[dict] = []
    for node in root.iter():
        if node.tag.split("}")[-1] != "item":
            continue
        title = link = desc = ""
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag == "title":
                title = child.text or ""
            elif tag == "link":
                link = child.text or ""
            elif tag == "description":
                desc = child.text or ""
        if title:
            items.append({"title": _text(title, 80), "snippet": _text(desc),
                          "url": link.strip()})
        if len(items) >= 6:
            break
    return items


def _ddg(query: str) -> list[dict]:
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": _UA}, trust_env=False) as client:
        resp = client.post("https://html.duckduckgo.com/html/",
                           data={"q": query}, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if resp.status_code >= 400:
        return []
    items: list[dict] = []
    for m in re.finditer(r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.S):
        link, title = m.group(1), _text(m.group(2), 80)
        if title:
            items.append({"title": title, "snippet": "", "url": link})
        if len(items) >= 6:
            break
    return items


def search(query: str, *, user_id: str = "") -> dict:
    """同步函数（在线程池里跑，别卡事件循环）。"""
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "summary": "没说要查什么", "data": {"items": []}}
    tried: list[str] = []
    for name, fn in (
        ("cn.bing", lambda: _bing_rss(q, "cn.bing.com")),
        ("bing", lambda: _bing_rss(q, "www.bing.com")),
        ("duckduckgo", lambda: _ddg(q)),
    ):
        try:
            items = fn()
        except Exception as exc:
            tried.append(f"{name}:{type(exc).__name__}")
            continue
        if items:
            return {
                "ok": True,
                "summary": f"查到 {len(items)} 条（{name}）",
                "data": {"items": items, "engine": name},
            }
        tried.append(f"{name}:空")
    return {
        "ok": False,
        "summary": "没查到（" + "、".join(tried) + "）；可能是网络不通或搜索源被挡",
        "data": {"items": []},
    }
