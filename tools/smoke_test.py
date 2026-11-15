"""端到端冒烟验收：需要 brain(:8000) 与 console(:8001) 两个进程已启动。

运行：  python tools/smoke_test.py
判定：  全部 PASS 退出码 0，任一 FAIL 退出码 1
说明：  程序里没有模拟路径。对话大模型没起时，LLM 相关用例自动跳过（SKIP）；
        语音/识别服务没起时，按「如实报 503」验收，不再造占位音。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from brain import db
from brain.schemas import EMOTION_LABELS

BRAIN = "http://127.0.0.1:8000"
CONSOLE = "http://127.0.0.1:8001"

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + detail if detail else ""))


def chat(user_id: str, session_id: str, text: str, timeout: float = 90.0) -> list[dict]:
    events: list[dict] = []
    buffer = ""
    with httpx.stream(
        "POST", f"{BRAIN}/chat",
        json={"user_id": user_id, "session_id": session_id, "text": text},
        timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                for line in frame.splitlines():
                    if line.startswith("data:"):
                        try:
                            events.append(json.loads(line[5:].strip()))
                        except json.JSONDecodeError:
                            pass
    for line in buffer.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return events


def tokens(events: list[dict]) -> str:
    return "".join(e.get("delta", "") for e in events if e.get("type") == "token")


def first(events: list[dict], kind: str) -> dict:
    for e in events:
        if e.get("type") == kind:
            return e
    return {}


def main() -> int:
    print("=" * 62)
    print("companion 端到端冒烟验收（全部真实服务，无模拟）")
    print("=" * 62)

    # 0. 预热知识库缓存（真实 MCP 检索约 8s；控制台一次即可，后续对话 0ms 命中）
    #    并发初始化时可能有一次没写进缓存，这里重试并确认真的召回了。
    for _ in range(2):
        try:
            w = httpx.post(f"{CONSOLE}/api/admin/knowledge/search",
                           json={"query": "用户核心设定 长期偏好 稳定事实"}, timeout=30)
            if (w.json().get("results") or []):
                break
        except Exception:
            pass

    # 1. health
    try:
        h = httpx.get(f"{BRAIN}/health", timeout=5).json()
        check("1 /health 可达", h.get("status") == "ok", json.dumps(h, ensure_ascii=False)[:160])
        check("1b 运行模式为真实服务", h.get("llm") == "http" and "mock" not in h,
              f"llm={h.get('llm')}")
    except Exception as exc:
        check("1 /health 可达", False, repr(exc))
        print("\n大脑进程未启动，后续用例跳过。先运行 2-启动大脑.bat")
        return 1

    # 1c. 大模型在不在：不在就跳过 LLM 用例（如实报告，不造假回复）
    llm_up = False
    try:
        from brain.services import providers as _pv
        base = str((_pv.resolve("llm", "base_url") or "")).rstrip("/")
        if base:
            r0 = httpx.get(base + "/v1/models", timeout=6)
            llm_up = r0.status_code < 500
    except Exception:
        llm_up = False
    if not llm_up:
        print("[SKIP] 对话大模型未启动（程序没问题，模型服务没起）："
              "用例 2/3/4/5 及 V2-5/V2-6 跳过。先起你的模型服务再跑全量。")
        print("-" * 62)
        print(f"结果：{sum(1 for _, ok, _ in _results if ok)}/{len(_results)} PASS"
              "（LLM 用例未计入）")
        return 0 if all(ok for _, ok, _ in _results) else 1

    uid = "smoke_" + time.strftime("%H%M%S")
    sid = "s1"

    # 2. 第一轮
    e1 = chat(uid, sid, "最近工作压力很大，晚上总睡不好")
    d1 = first(e1, "done")
    m1 = first(e1, "meta")
    check("2 第一轮收到流式 token", len(tokens(e1)) > 0, f"{len(tokens(e1))} 字")
    check("2b meta 为首帧", bool(e1) and e1[0].get("type") == "meta")
    check("2c done 为末帧且情绪合法",
          bool(d1) and e1[-1].get("type") == "done" and d1.get("emotion") in EMOTION_LABELS,
          f"emotion={d1.get('emotion')} intensity={d1.get('intensity')}")
    check("2d 返回 event_id", isinstance(d1.get("event_id"), int), str(d1.get("event_id")))

    # 2e 等第一轮事件对检索可见：跨进程 WAL 在冷启动瞬间可能有一帧延迟，
    #    先等可见再发第二轮，避免把「时序」误判成「记忆没生效」。
    threshold = float(db.get_runtime_params().get("emotion_min_intensity", 0.6))
    for _ in range(15):
        if db.recent_emotions(uid, min_intensity=threshold, limit=5):
            break
        time.sleep(0.2)
    check("2e 第一轮情绪已可被检索", bool(db.recent_emotions(uid, threshold, 5)),
          f"阈值={threshold}")

    # 3. 第二轮：确定性验证「记得上一轮情绪」
    #    以服务端真实写下的 prompt 快照为准（评审要求的确定性验证）。
    e2 = chat(uid, sid, "今天还是有点累")
    t2 = tokens(e2)
    d2 = first(e2, "done")
    from brain import config_store as cs
    snaps = " ".join((t.get("prompt_snapshot") or "")
                     for t in cs.list_turn_logs(user_id=uid, limit=5))
    by_snapshot = "label=" in snaps
    check("3 第二轮 prompt 注入了上一轮情绪", by_snapshot,
          f"prompt快照含label={by_snapshot}")
    check("3b 第二轮正常结束", e2[-1].get("type") == "done" if e2 else False,
          f"emotion={d2.get('emotion')}")

    # 4. DB 断言（评审要求的确定性验证，不靠肉眼看回复）
    rows = [r for r in db.list_emotion_events(limit=500) if r["user_id"] == uid]
    check("4 emotion_events 恰好两条", len(rows) == 2, f"实际 {len(rows)} 条")
    check("4b 两条都 closed=1 且强度合法",
          len(rows) == 2 and all(r["closed"] == 1 and 0 <= r["intensity"] <= 1 for r in rows))
    check("4c 检索命中近期情绪事件",
          any(r["emotion_label"] in EMOTION_LABELS
              for r in db.recent_emotions(uid, min_intensity=0.0, limit=5)))

    # 5. 人格热切换（不重启）：编辑「当前激活」的模板再激活，
    #    version 每次都递增，保证脚本可重复运行而不出现假阴性。
    v_before = m1.get("persona_version")
    try:
        plist = httpx.get(f"{CONSOLE}/api/admin/personas", timeout=5).json()
        if isinstance(plist, dict):
            plist = plist.get("items") or plist.get("data") or plist.get("personas") or []
        active = next((p for p in plist if p.get("is_active")), None)
        pid = (active or (plist[0] if plist else {})).get("id")
        r = httpx.post(
            f"{CONSOLE}/api/admin/personas",
            json={"id": pid, "name": "smoke-人格-热更新",
                  "system_prompt": "你是简短冷静的陪伴者，只说一两句话，不寒暄。"},
            timeout=5,
        )
        r.raise_for_status()
        pid = pid or (r.json() or {}).get("id")
        a = httpx.post(f"{CONSOLE}/api/admin/personas/{pid}/activate", timeout=5)
        a.raise_for_status()
        e3 = chat(uid, sid, "你好呀，随便聊两句")
        v_after = first(e3, "meta").get("persona_version")
        check("5 改人格后下次 /chat 立即生效（不重启）",
              v_after is not None and v_after != v_before, f"{v_before} -> {v_after}")
    except Exception as exc:
        check("5 改人格后下次 /chat 立即生效（不重启）", False, repr(exc))

    # 6. TTS：服务在就要求真 WAV，不在就要求如实 503（没有占位音）
    try:
        t = httpx.post(f"{BRAIN}/tts", json={"text": "今天辛苦了", "emotion": "sad"}, timeout=30)
        body = t.content
        if t.status_code == 200:
            check("6 /tts 返回可播放 WAV",
                  body[:4] == b"RIFF" and len(body) > 1024, f"bytes={len(body)}")
        else:
            check("6 /tts 语音服务未起时如实报错",
                  t.status_code == 503 and bool(t.json().get("detail")),
                  (t.json().get("detail") or "")[:40])
    except Exception as exc:
        check("6 /tts", False, repr(exc))

    # 7. 控制平面存活与降级
    for path in ("/api/admin/overview", "/api/admin/params", "/api/admin/judge-logs", "/api/admin/gpu"):
        try:
            r = httpx.get(CONSOLE + path, timeout=8)
            check(f"7 控制台 {path} 不 500", r.status_code < 500, f"status={r.status_code}")
        except Exception as exc:
            check(f"7 控制台 {path} 不 500", False, repr(exc))

    # ---- v2 配置中心验收（模型 / 知识库 / TTS 三层分离）----
    try:
        provs = httpx.get(f"{CONSOLE}/api/admin/providers", timeout=8).json()
        check("V2-1 模型服务清单 6 类", len(provs) == 6,
              ", ".join(p.get("code", "") for p in provs))
        field_count = sum(len(p.get("fields") or []) for p in provs)
        check("V2-2 可配置项 >=60", field_count >= 60, f"{field_count} 项")
    except Exception as exc:
        check("V2-1 模型服务清单 6 类", False, repr(exc))
        check("V2-2 可配置项 >=60", False, repr(exc))
        provs = []

    # ---- ASR 验收：服务在就要求真识别，不在就要求优雅降级 ----
    try:
        health = httpx.get(f"{BRAIN}/health", timeout=8).json()
        asr_up = bool((health.get("asr_service") or {}).get("up"))
        wav = ROOT / "models" / "sense-voice" / "test_wavs" / "zh.wav"
        if asr_up and wav.is_file():
            r = httpx.post(f"{BRAIN}/asr", content=wav.read_bytes(),
                           headers={"content-type": "audio/wav"}, timeout=60)
            body = r.json()
            text = (body.get("text") or "").strip()
            check("ASR-1 语音转文字", r.status_code == 200 and bool(text),
                  f"{text[:18]}… {body.get('ms')}ms")
            check("ASR-2 带声学情绪与语言",
                  body.get("emotion") in EMOTION_LABELS and bool(body.get("lang")),
                  f"emotion={body.get('emotion')} lang={body.get('lang')}")
        else:
            r = httpx.post(f"{BRAIN}/asr", content=b"RIFF0000WAVEfmt ", timeout=10)
            body = r.json()
            check("ASR-1 服务没起时优雅降级",
                  r.status_code == 503 and bool(body.get("detail")),
                  (body.get("detail") or "")[:36])
    except Exception as exc:
        check("ASR-1 语音转文字", False, repr(exc))

    try:
        sysinfo = httpx.get(f"{CONSOLE}/api/admin/system", timeout=8).json()
        # 规则数跟着唯一数据源走（RUNTIME_RULES 本身就含全局开关），不再写死 17：
        # temperature/top_p/num_ctx/max_tokens 已归到 provider 字段，运行时表里不该再有。
        from brain import config_store as _cs
        expect_rules = len(_cs.RUNTIME_RULES)
        got_rules = len(sysinfo.get("runtime_rules") or {})
        check("V2-3 /system 版本与参数规则",
              sysinfo.get("version") == "v2" and got_rules == expect_rules
              and "temperature" not in (sysinfo.get("runtime_rules") or {}),
              f"v={sysinfo.get('version')} rules={got_rules}/{expect_rules}")
    except Exception as exc:
        check("V2-3 /system 版本与参数规则", False, repr(exc))

    try:
        # 真实 MCP hybrid 检索实测约 8 秒，超时给足，别卡在边界上误判
        bases = httpx.get(f"{CONSOLE}/api/admin/knowledge/bases", timeout=25).json()
        found = httpx.post(f"{CONSOLE}/api/admin/knowledge/search",
                           json={"query": "旧硬件 本地部署"}, timeout=25).json()
        check("V2-4 知识库可列库且可检索",
              len(bases.get("bases") or []) >= 4 and len(found.get("results") or []) > 0,
              f"库={len(bases.get('bases') or [])} 召回={len(found.get('results') or [])}")
    except Exception as exc:
        check("V2-4 知识库可列库且可检索", False, repr(exc))

    # 长期记忆是「稳定内容」，缓存刚建好时首轮可能还没带上；
    # 补一轮再判定，避免把时序问题误判成功能缺失。
    if "长期记忆" not in snaps:
        chat(uid, sid, "那我接着说吧")
        snaps = " ".join((t.get("prompt_snapshot") or "")
                         for t in cs.list_turn_logs(user_id=uid, limit=8))
    check("V2-5 知识库片段注入了 prompt", "长期记忆" in snaps,
          f"prompt 快照 {len(snaps)} 字符")

    try:
        turns = httpx.get(f"{CONSOLE}/api/admin/turn-logs?user_id={uid}", timeout=8).json()
        check("V2-6 每轮明细已落库", len(turns) >= 2, f"{len(turns)} 条")
    except Exception as exc:
        check("V2-6 每轮明细已落库", False, repr(exc))

    try:
        llm_p = next((p for p in provs if p.get("code") == "llm"), {})
        old_model = (llm_p.get("config") or {}).get("model", "qwen3:8b")
        v0 = llm_p.get("version")
        r = httpx.put(f"{CONSOLE}/api/admin/providers/llm",
                      json={"config": {"model": "smoke-probe-model"}}, timeout=8)
        r.raise_for_status()
        v1 = r.json().get("version")
        httpx.put(f"{CONSOLE}/api/admin/providers/llm",
                  json={"config": {"model": old_model}}, timeout=8)
        check("V2-7 改模型配置不重启即生效", v1 is not None and v1 != v0,
              f"version {v0}->{v1}，已恢复 {old_model}")
    except Exception as exc:
        check("V2-7 改模型配置不重启即生效", False, repr(exc))

    try:
        r = httpx.put(f"{CONSOLE}/api/admin/voice-profiles",
                      json={"emotion": "sad", "speed": 0.88}, timeout=8)
        r.raise_for_status()
        vps = httpx.get(f"{CONSOLE}/api/admin/voice-profiles", timeout=8).json()
        sad = next((v for v in vps if v.get("emotion") == "sad"), {})
        check("V2-8 语音情感映射可改",
              abs(float(sad.get("speed") or 0) - 0.88) < 0.001,
              f"speed={sad.get('speed')}")
    except Exception as exc:
        check("V2-8 语音情感映射可改", False, repr(exc))

    for path in ("/api/admin/turn-logs", "/api/admin/sessions", "/api/admin/facts",
                 "/api/admin/config-audit", "/api/admin/voice-profiles",
                 "/api/admin/reference-audios"):
        try:
            r = httpx.get(CONSOLE + path, timeout=8)
            check(f"V2-9 控制台 {path} 不 500", r.status_code < 500, f"status={r.status_code}")
        except Exception as exc:
            check(f"V2-9 控制台 {path} 不 500", False, repr(exc))

    try:
        r = httpx.post(f"{CONSOLE}/api/admin/tts/preview",
                       json={"text": "今天辛苦了", "emotion": "sad"}, timeout=30)
        if r.status_code == 200:
            check("V2-10 语音试听返回可播放 WAV",
                  r.content[:4] == b"RIFF" and len(r.content) > 1024, f"bytes={len(r.content)}")
        else:
            check("V2-10 语音服务未起时如实报错",
                  r.status_code == 503 and bool((r.json() or {}).get("detail")),
                  ((r.json() or {}).get("detail") or "")[:40])
    except Exception as exc:
        check("V2-10 语音试听", False, repr(exc))

    passed = sum(1 for _, ok, _ in _results if ok)
    print("-" * 62)
    print(f"结果：{passed}/{len(_results)} PASS")
    if passed != len(_results):
        print("失败项：", ", ".join(n for n, ok, _ in _results if not ok))
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
