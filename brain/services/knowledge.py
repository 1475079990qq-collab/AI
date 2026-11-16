"""知识库适配层（S1）：用 MCP 工具直连 WeKnora 做长期记忆与特殊条目。

配置全部来自 providers.get("knowledge")["config"]，不硬编码。
模式：mcp_http / webhook；off 或任何异常一律空结果 + degraded，
绝不抛、绝不阻塞对话。没有 mock 假片段。
"""
from __future__ import annotations

import json
import re
import threading
import time
import xml.etree.ElementTree as ET

import httpx

from brain import config_store
from brain.services import providers

# 真实 WeKnora hybrid 检索实测约 7.5-8 秒，直接放进对话热路径会明显拖慢首 token。
# 长期记忆本就是稳定内容，故采用「缓存优先 + 后台异步刷新」：
#   /chat 热路径 fast=True → 命中缓存 0ms 返回；未命中立即返回空并后台刷新，不阻塞对话。
#   控制台检索测试 fast=False → 同步等完整结果（用户主动发起，可接受），顺带预热缓存。
_CACHE_LOCK = threading.Lock()  # 仅防同进程并发刷新
_CACHE_KEY = "knowledge:long_term"
_CACHE_TTL = 600.0
_LONG_TERM_QUERY = "用户核心设定 长期偏好 稳定事实 经验教训"
_REFRESHING = False


def _cache_payload() -> dict:
    """缓存落 DB，两个进程共享（控制台预热后大脑立即命中）。"""
    try:
        return config_store.cache_get(_CACHE_KEY, _CACHE_TTL) or {}
    except Exception:
        return {}


def _cache_valid() -> bool:
    return bool(_cache_payload().get("results"))


def _cache_get() -> list[dict]:
    return [dict(x) for x in (_cache_payload().get("results") or [])]


def _cache_set(results, query: str, mode: str) -> None:
    try:
        config_store.cache_set(_CACHE_KEY, {
            "results": [dict(x) for x in results], "query": query, "mode": mode,
        })
    except Exception:
        pass


def _refresh_async(query: str | None = None, done_event: threading.Event | None = None) -> None:
    """后台刷新知识库缓存。

    done_event：调用方如果是同步等待模式（不想立即返回空），就塞一个 Event，
    刷新线程结束后 set，主线程可以 with timeout 等一下再查缓存。
    """
    global _REFRESHING
    with _CACHE_LOCK:
        if _REFRESHING:
            if done_event is not None:
                done_event.set()   # 已经有人在刷了，不重复等
            return
        _REFRESHING = True

    def worker() -> None:
        global _REFRESHING
        try:
            cfg = _cfg()
            q = query or _LONG_TERM_QUERY
            rows, degraded = _search_mcp(cfg, q, None)
            if rows and not degraded:
                _cache_set(_postprocess(rows, cfg, None), q, "mcp_http")
        except Exception:
            pass
        finally:
            with _CACHE_LOCK:
                _REFRESHING = False
            if done_event is not None:
                done_event.set()

    threading.Thread(target=worker, daemon=True, name="kb-refresh").start()


def _cfg() -> dict:
    try:
        return dict(providers.get("knowledge").get("config") or {})
    except Exception:
        return dict(config_store.PROVIDER_DEFAULTS.get("knowledge", {}))


def _row() -> dict:
    """读 providers 列（enabled/mode）——开关权威源；内层 config.mode 只是影子。"""
    try:
        return dict(providers.get("knowledge") or {})
    except Exception:
        return {}


def _disabled(row: dict) -> bool:
    """列 enabled=0 或 mode=off 即停用：不读内层 config.mode、不外呼 MCP。"""
    try:
        if int(row.get("enabled", 1)) == 0:
            return True
    except (TypeError, ValueError):
        pass
    return str(row.get("mode") or "off").strip().lower() == "off"


def _str(v) -> str:
    return "" if v is None else str(v)


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ids(v) -> list[str]:
    """kb_ids / special_documents：逗号分隔字符串 → 列表。"""
    return [x.strip() for x in _str(v).split(",") if x.strip()]


def _fallback_bases() -> list[dict]:
    return [{"id": b["id"], "name": b["name"]} for b in config_store.KNOWN_BASES]


def _deep_list(obj, depth: int = 0):
    """从 content / result / structuredContent 等常见包裹里挖出 list[dict]。"""
    if depth > 8 or obj is None:
        return None
    if isinstance(obj, list):
        if not obj:
            return None
        # MCP tools/call: content = [{"type":"text","text":"<json 或纯文本>"}]
        if all(isinstance(x, dict) and "text" in x for x in obj):
            parsed = []
            for x in obj:
                t = x.get("text")
                if isinstance(t, str):
                    try:
                        parsed.append(json.loads(t))
                    except (ValueError, TypeError):
                        parsed.append(t)
                else:
                    parsed.append(t)
            if len(parsed) == 1:
                got = _deep_list(parsed[0], depth + 1)
                if got:
                    return got
            if all(isinstance(p, str) for p in parsed):
                return [{"content": p} for p in parsed]
            return _deep_list(parsed, depth + 1)
        if all(isinstance(x, dict) for x in obj):
            return obj
        return None
    if isinstance(obj, str):
        try:
            return _deep_list(json.loads(obj), depth + 1)
        except (ValueError, TypeError):
            return None
    if isinstance(obj, dict):
        for key in ("structuredContent", "result", "content", "data",
                    "results", "bases", "documents", "items", "chunks"):
            if key in obj and obj[key] is not None:
                got = _deep_list(obj[key], depth + 1)
                if got:
                    return got
        for v in obj.values():
            got = _deep_list(v, depth + 1)
            if got:
                return got
    return None


def _extract_texts(payload) -> list[str]:
    """MCP tools/call 的 result.content[].text 文本列表。"""
    try:
        content = (payload or {}).get("result", {}).get("content", [])
        return [str(c["text"]) for c in content
                if isinstance(c, dict) and isinstance(c.get("text"), str)]
    except Exception:
        return []


def _parse_chunks_xml(text: str) -> list[dict]:
    """WeKnora search_knowledge 把结果包成
    <search_results><chunk knowledge_title=.. score=..><content>..</content></chunk>...
    实测（2026-11-13 直连 192.168.10.8 MCP）返回的就是这种 XML 文本，不是 JSON。
    """
    if "<chunk" not in text:
        return []
    out: list[dict] = []
    try:
        root = ET.fromstring(text)
        for node in root.iter("chunk"):
            title = node.attrib.get("knowledge_title", "")
            score = _float(node.attrib.get("score"), 0.0)
            body = ""
            for child in node:
                if child.tag == "content" and child.text:
                    body = child.text.strip()
                    break
            if not body:
                for child in node:
                    if child.tag == "match_snippet" and child.text:
                        body = child.text.strip().strip(".").strip()
                        break
            if body:
                out.append({"title": title, "content": body[:300],
                            "score": score, "source": "mcp"})
        if out:
            return out
    except Exception:
        pass
    for m in re.finditer(r"<chunk\b([^>]*)>(.*?)</chunk>", text, re.S):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        cm = re.search(r"<content>(.*?)</content>", m.group(2), re.S)
        body = (cm.group(1) if cm else "").strip()
        if body:
            out.append({"title": attrs.get("knowledge_title", ""),
                        "content": body[:300],
                        "score": _float(attrs.get("score"), 0.0),
                        "source": "mcp"})
    return out


def _to_result(it: dict, source: str) -> dict:
    return {
        "title": _str(it.get("title") or it.get("name") or it.get("document_title") or ""),
        "content": _str(it.get("content") or it.get("text") or it.get("snippet")
                        or it.get("chunk") or ""),
        "score": _float(it.get("score") or it.get("similarity") or 0.0, 0.0),
        "source": _str(it.get("source") or source),
    }


# WeKnora 命中为空时也会回一段提示文本，之前被当成片段落进 prompt。
_NO_MATCH_HINT = "no matching chunks"


def _clean_rows(items: list) -> list[dict]:
    rows = [_to_result(it, "mcp") for it in items if isinstance(it, dict)]
    return [r for r in rows
            if r["content"].strip()
            and not r["content"].strip().lower().startswith(_NO_MATCH_HINT)]


def _rpc(cfg: dict, tool: str, arguments: dict, timeout: float | None = None):
    """对 mcp_endpoint 发 JSON-RPC 2.0 tools/call。"""
    endpoint = _str(cfg.get("mcp_endpoint"))
    if not endpoint:
        raise RuntimeError("mcp_endpoint 未配置")
    headers = {"Content-Type": "application/json"}
    token = _str(cfg.get("mcp_token"))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    resp = httpx.post(endpoint, json=body, headers=headers,
                      timeout=float(timeout) if timeout else _float(cfg.get("timeout_s"), 12.0))
    resp.raise_for_status()
    return resp.json()


def _search_mcp(cfg: dict, query: str, top_k, timeout: float | None = None):
    if not _str(cfg.get("mcp_endpoint")):
        return [], True
    k = _int(top_k, 0) or _int(cfg.get("top_k"), 3)
    arguments = {
        "query": query,
        "limit": k,
        "knowledge_base_ids": _ids(cfg.get("kb_ids")),
        "mode": _str(cfg.get("retrieval_mode") or "hybrid"),
    }
    payload = _rpc(cfg, _str(cfg.get("tool_search") or "search_knowledge"), arguments, timeout)
    # 优先解析 WeKnora 的 XML 文本结果，其次兼容 JSON / 通用包裹
    for text in _extract_texts(payload):
        parsed = _parse_chunks_xml(text)
        if parsed:
            return parsed, False
        try:
            items = _deep_list(json.loads(text))
            if items:
                rows = _clean_rows(items)
                if rows:
                    return rows, False
        except (ValueError, TypeError):
            pass
    items = _deep_list(payload)
    if not items:
        return [], True
    rows = _clean_rows(items)
    return (rows, False) if rows else ([], True)


def _search_webhook(cfg: dict, query: str, top_k):
    url = _str(cfg.get("webhook_url"))
    if not url:
        return [], True
    k = _int(top_k, 0) or _int(cfg.get("top_k"), 3)
    body = {"query": query, "top_k": k, "kb_ids": _ids(cfg.get("kb_ids"))}
    resp = httpx.post(url, json=body, timeout=_float(cfg.get("timeout_s"), 8.0))
    resp.raise_for_status()
    data = resp.json()
    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = _deep_list(data)
    results = [_to_result(it, "webhook") for it in (items or []) if isinstance(it, dict)]
    return results, False


def _postprocess(results, cfg: dict, top_k) -> list[dict]:
    min_score = _float(cfg.get("min_score"), 0.0)
    out = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        score = _float(r.get("score"), 0.0)
        if score < min_score:
            continue
        out.append({
            "title": _str(r.get("title")), "content": _str(r.get("content")),
            "score": score, "source": _str(r.get("source") or "mcp"),
        })
    k = _int(top_k, 0) or _int(cfg.get("top_k"), 3)
    if k > 0:
        out = out[:k]
    # 特殊文档固定占位：始终注入，标注 source="special"，不受 min_score / top_k 影响
    for doc_id in _ids(cfg.get("special_documents")):
        out.append({
            "title": _str(cfg.get("section_title") or "长期记忆 / 知识片段"),
            "content": f"特殊文档占位（id={doc_id}）",
            "score": 1.0,
            "source": "special",
        })
    return out


def search(query: str, top_k: int | None = None, fast: bool = False) -> dict:
    """fast=True 用于 /chat 热路径：缓存优先 + 后台刷新，绝不阻塞对话。

    fast=False 用于控制台「检索测试」：同步等完整结果（用户主动发起），并预热缓存。
    """
    started = time.perf_counter()
    if _disabled(_row()):
        return {"results": [], "mode": "off", "degraded": True,
                "cached": False, "pending": False,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 2)}
    cfg = _cfg()
    mode = _str(cfg.get("mode") or "off").lower()
    degraded = False
    results: list[dict] = []
    cached = False
    pending = False

    if fast and mode == "mcp_http":
        if _cache_valid():
            return {"results": _cache_get(), "mode": mode, "degraded": False,
                    "cached": True, "pending": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000.0, 2)}
        # 首轮没命中：同步等最多 2 秒刷新完成（WeKnora 实测 7-8 秒大概率超时，
        # 但如果刚好控制台刚预热过、或者 MCP 端点很快，就能在这轮就拿到知识）。
        # 等不到就返回空 + 后台继续跑，不阻塞 SSE 首 token。
        WAIT_S = 2.0
        ev = threading.Event()
        _refresh_async(query, done_event=ev)
        ev.wait(WAIT_S)
        if _cache_valid():
            return {"results": _cache_get(), "mode": mode, "degraded": False,
                    "cached": True, "pending": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000.0, 2)}
        return {"results": [], "mode": mode, "degraded": False,
                "cached": False, "pending": True,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 2)}

    try:
        if mode == "mcp_http":
            results, degraded = _search_mcp(cfg, query, top_k)
        elif mode == "webhook":
            results, degraded = _search_webhook(cfg, query, top_k)
        else:  # off / 未知模式
            degraded = True
    except Exception:
        results, degraded = [], True
    results = _postprocess(results, cfg, top_k)
    if mode == "mcp_http" and results and not degraded:
        _cache_set(results, query, mode)
    return {
        "results": results, "mode": mode, "degraded": bool(degraded),
        "cached": cached, "pending": pending,
        "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
    }


def _known_bases(cfg: dict) -> list[dict]:
    raw = cfg.get("known_bases")
    items = None
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            items = None
    elif isinstance(raw, list):
        items = raw
    if isinstance(items, list) and items:
        out = [{"id": _str(it.get("id")), "name": _str(it.get("name") or it.get("id"))}
               for it in items if isinstance(it, dict) and it.get("id")]
        if out:
            return out
    return _fallback_bases()


def list_bases() -> dict:
    if _disabled(_row()):
        return {"bases": [], "mode": "off", "degraded": True}
    cfg = _cfg()
    mode = _str(cfg.get("mode") or "off").lower()
    degraded = False
    bases: list[dict] = []
    try:
        if mode == "mcp_http":
            if not _str(cfg.get("mcp_endpoint")):
                degraded = True
            else:
                payload = _rpc(cfg, _str(cfg.get("tool_list_bases") or "list_knowledge_bases"), {})
                items = _deep_list(payload)
                if items:
                    bases = [
                        {"id": _str(it.get("id") or it.get("kb_id") or it.get("knowledge_base_id")),
                         "name": _str(it.get("name") or it.get("title") or it.get("id"))}
                        for it in items if isinstance(it, dict)
                    ]
                else:
                    bases, degraded = _fallback_bases(), True
        elif mode == "webhook":
            bases, degraded = _fallback_bases(), True  # webhook 无列库协议，用兜底
        else:  # off
            degraded = True
    except Exception:
        bases, degraded = (_fallback_bases() if mode == "mcp_http" else []), True
    return {"bases": bases, "mode": mode, "degraded": bool(degraded)}


# ---- 写入粒度：一天一篇纪要，不再一问一文档 ----------------------------
# 原来的行为是每轮对话 add_document 一次，知识库里堆成一堆 10~20 字的同名碎文档：
# 解析/切片/向量化每篇都要跑一遍，检索回来的全是半句话，还无法合并演进。
# 现在固定写「当天一篇纪要」，同一篇用 update_document 追加，一天最多一篇。
_DAILY_TAG = "对话纪要"


def _structured(payload) -> dict:
    """JSON-RPC 响应里的 structuredContent（WeKnora MCP 的结构化返回）。"""
    try:
        got = (payload or {}).get("result", {}).get("structuredContent")
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def _doc_title_prefix(cfg: dict) -> str:
    return _str(cfg.get("tag_prefix"))


def _daily_title(cfg: dict, day: str) -> str:
    prefix = _doc_title_prefix(cfg)
    return f"{prefix}{_DAILY_TAG} {day}"


def _find_daily_doc(cfg: dict, kb_id: str, title: str) -> str:
    """按标题精确找当天那篇纪要，返回 knowledge_id；没有就返回空串。"""
    if not kb_id or not title:
        return ""
    payload = _rpc(cfg, "list_documents", {
        "knowledge_base_id": kb_id, "keyword": title, "page": 1, "page_size": 20,
    })
    docs = _structured(payload).get("documents") or []
    if not docs:
        docs = _deep_list(payload) or []
    for d in docs:
        if isinstance(d, dict) and _str(d.get("title")).strip() == title:
            return _str(d.get("id"))
    return ""


def _read_doc_text(cfg: dict, knowledge_id: str) -> str:
    """取已有纪要的正文（按 chunk 顺序拼回 Markdown）。"""
    payload = _rpc(cfg, "read_document", {
        "knowledge_id": knowledge_id, "limit": 400, "offset": 0,
    })
    chunks = _structured(payload).get("chunks") or []
    if not chunks:
        chunks = _deep_list(payload) or []
    parts: list[str] = []
    for ch in chunks:
        if isinstance(ch, dict):
            parts.append(_str(ch.get("content") or ch.get("text")))
        else:
            parts.append(str(ch))
    return "\n".join(p for p in parts if p).strip()


def _remember_daily(cfg: dict, text: str, timeout=None) -> tuple[bool, str]:
    """写当天纪要：已有就追加一节，没有才新建一篇。"""
    kb_id = _str(cfg.get("target_kb_id"))
    if not kb_id:
        return False, "target_kb_id 未配置，跳过写入"
    day = time.strftime("%Y-%m-%d")
    hm = time.strftime("%H:%M")
    title = _daily_title(cfg, day)
    entry = f"- {hm} {text}"

    kid = _find_daily_doc(cfg, kb_id, title)
    if kid:
        try:
            old = _read_doc_text(cfg, kid)
            new = (old + "\n" + entry).strip() if old else entry
            _rpc(cfg, "update_document", {
                "knowledge_id": kid, "title": title, "content": new,
            }, timeout)
            return True, "已追加到今天的对话纪要"
        except Exception:
            # 追加失败就退化成新建，宁可多一篇也不丢内容
            pass
    _rpc(cfg, _str(cfg.get("tool_remember") or "add_document"), {
        "knowledge_base_id": kb_id, "title": title,
        "content": f"# {title}\n\n{entry}",
    }, timeout)
    return True, "已新建今天的对话纪要"


def remember(text: str, title: str = "", tags: str = "", daily: bool = True) -> dict:
    cfg = _cfg()
    mode = _str(cfg.get("mode") or "off").lower()
    # 写操作统一带上 tag_prefix 到 title 与 tags
    prefix = _str(cfg.get("tag_prefix"))
    t_title = _str(title) or (_str(text)[:20].strip() or "对话沉淀")
    if prefix and not t_title.startswith(prefix):
        t_title = prefix + t_title
    t_tags = _str(tags)
    if prefix and prefix not in t_tags:
        t_tags = (t_tags + "," if t_tags else "") + prefix

    if mode == "off" or not (_str(cfg.get("mcp_endpoint")) or _str(cfg.get("webhook_url"))):
        return {"ok": False, "mode": mode, "detail": "知识库未启用或未配置端点，跳过写入",
                "degraded": True}
    try:
        if mode == "mcp_http":
            # daily=True 且调用方没指定标题 → 走「当天一篇纪要」的追加模式；
            # 控制台手动写入会带标题，仍然单独建一篇。
            if daily and not title:
                ok, detail = _remember_daily(
                    cfg, _str(text), _float(cfg.get("timeout_s"), 8.0))
                if not ok:
                    return {"ok": False, "mode": mode, "detail": detail, "degraded": True}
                return {"ok": True, "mode": mode, "detail": detail,
                        "title": _daily_title(cfg, time.strftime("%Y-%m-%d")),
                        "tags": t_tags}
            _rpc(cfg, _str(cfg.get("tool_remember") or "add_document"), {
                "knowledge_base_id": _str(cfg.get("target_kb_id")),
                "title": t_title,
                "content": _str(text),
            })
            detail = "已写入目标知识库"
        elif mode == "webhook":
            resp = httpx.post(_str(cfg.get("webhook_url")), json={
                "action": "remember", "text": _str(text), "title": t_title,
                "tags": t_tags, "kb_ids": _ids(cfg.get("kb_ids")),
            }, timeout=_float(cfg.get("timeout_s"), 8.0))
            resp.raise_for_status()
            detail = "已投递 webhook 写入请求"
        else:
            return {"ok": False, "mode": mode, "detail": "未知模式，跳过写入", "degraded": True}
        return {"ok": True, "mode": mode, "detail": detail, "title": t_title, "tags": t_tags}
    except Exception as exc:
        return {"ok": False, "mode": mode, "detail": f"写入失败已降级：{exc}", "degraded": True}
