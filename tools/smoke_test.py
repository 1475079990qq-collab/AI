"""端到端冒烟验收（v3）：只验现在真实存在的东西。

跑法：
    .venv\\Scripts\\python.exe tools\\smoke_test.py            # 默认打 http://127.0.0.1:8000
    .venv\\Scripts\\python.exe tools\\smoke_test.py --base http://127.0.0.1:18081

判定：任何 FAIL → 退出码 1；SKIP 不算失败（但会明说跳了什么、为什么）。
纪律：大模型没起就 SKIP 对话用例，绝不为了好看造假回复；测试写进去的数据最后清掉。
"""
from __future__ import annotations

import argparse
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

_results: list[tuple[str, str, str]] = []   # (name, PASS/FAIL/SKIP, detail)


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    _results.append((name, status, detail))
    print(f"{status}  {name}" + (f"   {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    _results.append((name, "SKIP", why))
    print(f"SKIP  {name}   {why}")


def info(name: str, detail: str) -> None:
    _results.append((name, "SKIP", detail))
    print(f"INFO  {name}   {detail}")


# ---------- SSE ----------

def chat(user_id: str, session_id: str, text: str,
         base: str = BRAIN, timeout: float = 120.0) -> list[dict]:
    events: list[dict] = []
    buffer = ""
    with httpx.stream(
        "POST", f"{base}/chat",
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


# ---------- 清理 ----------

def cleanup(uid: str) -> None:
    """把这次冒烟写进去的东西删干净，不污染用户真实记忆。"""
    conn = db.connect()
    try:
        for table, col in (
            ("facts", "user_id"), ("turn_logs", "user_id"),
            ("emotion_events", "user_id"), ("episodes", "user_id"),
            ("open_loops", "user_id"), ("tool_runs", "user_id"),
            ("proactive_msgs", "user_id"), ("deliveries", "user_id"),
        ):
            try:
                conn.execute(f"DELETE FROM {table} WHERE {col}=?", (uid,))
            except Exception:
                pass
        try:
            conn.execute("DELETE FROM feedback WHERE target LIKE 'smoke%'")
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BRAIN)
    ap.add_argument("--keep", action="store_true", help="保留测试数据（排查用）")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 64)
    print(f"companion 冒烟验收 v3  →  {base}")
    print("=" * 64)

    # ---------------- 1. 大脑活着 ----------------
    try:
        h = httpx.get(f"{base}/health", timeout=6).json()
    except Exception as exc:
        check("1 /health 可达", False, repr(exc))
        print("\n大脑没起：先跑 1-启动.bat，或按 _tmp_run_brain.py 起临时端口。")
        return 1

    check("1 /health 可达", h.get("status") == "ok", json.dumps(h, ensure_ascii=False)[:200])
    check("1b 记忆落在 sqlite", str(h.get("memory")) == "sqlite", f"memory={h.get('memory')}")
    check("1c 数据库可读", str(h.get("db")) == "ok", f"db={h.get('db')}")

    # ---------------- 2. 情绪判断在链路里、不在独立进程 ----------------
    judge_engine = str((h.get("judge") or {}))
    check("2 情绪判断已进链路", "rule" in judge_engine or "onnx" in judge_engine,
          f"judge={judge_engine[:60]}")

    # ---------------- 3. 控制面（同一端口） ----------------
    for path in ("/api/admin/overview", "/api/admin/params", "/api/admin/providers",
                 "/api/admin/feedback", "/api/admin/deliveries", "/api/admin/tools",
                 "/api/admin/proactive", "/api/admin/feedback-policy"):
        try:
            r = httpx.get(base + path, timeout=8)
            check(f"3 控制台 {path} 不 5xx", r.status_code < 500, f"status={r.status_code}")
        except Exception as exc:
            check(f"3 控制台 {path} 不 5xx", False, repr(exc))

    # ---------------- 4. 配置真值 ----------------
    try:
        provs = httpx.get(f"{base}/api/admin/providers", timeout=8).json()
        items = provs.get("items") if isinstance(provs, dict) else provs
        by_code = {p.get("code"): p for p in (items or [])}
        kb = by_code.get("knowledge") or {}
        check("4 知识库已降级（enabled=0）",
              int(kb.get("enabled", 1)) == 0, f"enabled={kb.get('enabled')} mode={kb.get('mode')}")
        tts = by_code.get("tts") or {}
        check("4b 语音合成走进程内", str(h.get("tts") or "") == "inproc",
              f"health tts={h.get('tts')}")
        rt = httpx.get(f"{base}/api/admin/params", timeout=8).json()
        p = (rt.get("params") if isinstance(rt, dict) else {}) or {}
        name = str(p.get("assistant_name") or "")
        check("4c 身份一致（assistant_name 非空）", bool(name), f"assistant_name={name}")
    except Exception as exc:
        check("4 配置真值可读", False, repr(exc))

    # ---------------- 5. 工具层（纯单元，不依赖模型） ----------------
    from brain import tools

    cases = [
        ("帮我记一下，我妈生日是农历三月初五", "remember"),
        ("提醒我明天带伞", "remind"),
        ("查一下广元明天天气", "web_search"),
        ("知识库里翻一下我之前存的出差标准", "kb_search"),
        ("我今天有点累", None),
        ("嗯", None),
    ]
    ok_all = True
    for text, want in cases:
        got = tools.detect(text)
        got_tool = got[0] if got else None
        if got_tool != want:
            ok_all = False
            print(f"     判定不符：{text!r} → {got_tool}（期望 {want}）")
    check("5 工具判定 6 例全对", ok_all, "记住/提醒/上网/知识库/情绪话/短句")

    uid = "smoke_" + time.strftime("%H%M%S")
    try:
        res = tools.run_sync("remember", "smoke 记一条：周三要交体检报告", user_id=uid)
        check("5b remember 真写事实", bool(res.get("ok")), str(res.get("summary"))[:60])
        rows = [r for r in db.search_facts(uid, "体检报告", top_k=3)]
        check("5c 写得进、查得回", bool(rows), f"{len(rows)} 条")
        runs = [r for r in db.recent_tool_runs(50) if r.get("user_id") == uid]
        check("5d tool_runs 落了账", bool(runs),
              f"{len(runs)} 条，tool={runs[0].get('tool') if runs else ''}")
    except Exception as exc:
        check("5b remember 真写事实", False, repr(exc))

    # ---------------- 6. 交付卡规则（纯单元，含反馈反哺） ----------------
    try:
        from brain.services import deliver, feedback_rules

        reply = ("第一天早上去剑门关，路上两小时；\n"
                 "中午在景区门口吃豆腐宴，人均四十；\n"
                 "第二天上午去昭化古城，慢慢逛到下午。")
        card = deliver.build("帮我列个去广元两天的行程", reply, "neutral", {})
        check("6 该给的时候真给卡", bool(card) and len(card.get("items") or []) >= 2,
              f"kind={card.get('kind') if card else None} items={len((card or {}).get('items') or [])}")
        heavy = deliver.build("帮我列个去广元两天的行程", reply, "sad", {})
        check("6b 难受时不出清单", heavy is None, "sad → 先陪着，不递卡")
        blocked = deliver.build("帮我列个去广元两天的行程", reply, "neutral",
                                {}, policy={"deny_kinds": ["list"], "min_items": 2, "min_reply": 24})
        check("6c 被点过「不对」的类先不出", blocked is None, "policy 生效")
        pol = feedback_rules.compute()
        check("6d 反馈策略能算出来",
              isinstance(pol, dict) and "deny_kinds" in pol and "min_items" in pol,
              f"rated={pol.get('rated')} bad_ratio={pol.get('bad_ratio')}")
    except Exception as exc:
        check("6 交付卡规则", False, repr(exc))

    # ---------------- 7. 反馈 API 走通 ----------------
    rid = "smoke-" + time.strftime("%H%M%S")
    try:
        f = httpx.post(f"{base}/feedback",
                       json={"request_id": rid, "verdict": 1, "target": "smoke-反馈"},
                       timeout=8)
        check("7 /feedback 收下", f.status_code == 200 and f.json().get("ok") is True,
              f.status_code)
        fb = httpx.get(f"{base}/api/admin/feedback", timeout=8).json()
        hit = [x for x in (fb.get("items") or []) if x.get("request_id") == rid]
        check("7b 反馈立即可读", bool(hit), f"{len(hit)} 条")
    except Exception as exc:
        check("7 /feedback 收下", False, repr(exc))

    # ---------------- 8. 主动开口：接口在、闸门在 ----------------
    try:
        from brain.services import proactive

        pr = httpx.get(f"{base}/proactive?user_id={uid}", timeout=8)
        check("8 /proactive 探针可达", pr.status_code == 200 and "items" in pr.json(),
              f"status={pr.status_code} items={len(pr.json().get('items') or [])}")
        check("8b 静默时段/每天上限有闸门",
              proactive.MAX_PER_DAY == 2 and proactive.QUIET_START == 22,
              f"{proactive.QUIET_START}:00–{proactive.QUIET_END}:00，每天 {proactive.MAX_PER_DAY} 次")
    except Exception as exc:
        check("8 /proactive 探针可达", False, repr(exc))

    # ---------------- 9. 对话链路：模型在才验 ----------------
    llm_up = False
    try:
        from brain.services import providers as pv

        cfg = pv.get("llm") or {}
        inner = cfg.get("config") if isinstance(cfg.get("config"), dict) else cfg
        llm_base = str((inner or {}).get("base_url") or "").rstrip("/")
        if llm_base:
            r0 = httpx.get(llm_base + "/v1/models", timeout=6)
            llm_up = r0.status_code < 500
    except Exception:
        llm_up = False

    if not llm_up:
        skip("9 对话链路（meta→token→card→done）", "本地大模型没起：起你的模型服务后再跑全量")
    else:
        try:
            e1 = chat(uid, "s1", "最近工作压力大，晚上总睡不好", base=base)
            m1 = first(e1, "meta")
            d1 = first(e1, "done")
            check("9 帧序从 meta 起、以 done 收",
                  bool(e1) and e1[0].get("type") == "meta" and e1[-1].get("type") == "done",
                  f"首={e1[0].get('type') if e1 else None} 末={e1[-1].get('type') if e1 else None}")
            check("9b 有正文 token", len(tokens(e1)) > 0, f"{len(tokens(e1))} 字")
            check("9c 情绪合法", d1.get("emotion") in EMOTION_LABELS,
                  f"emotion={d1.get('emotion')} intensity={d1.get('intensity')}")

            # 本轮最重要的贯通验证：judge_logs 的 request_id 必须等于 meta 的
            rid1 = m1.get("request_id")
            logs = [r for r in db.list_judge_logs(limit=30) if r.get("request_id") == rid1]
            check("9d judge_logs 与 turn 同一 request_id", bool(logs),
                  f"request_id={rid1} 命中 {len(logs)} 条")

            e2 = chat(uid, "s1", "提醒我明天带伞", base=base)
            check("9e 第二轮正常收尾", bool(e2) and e2[-1].get("type") == "done", "")
            runs2 = [r for r in db.recent_tool_runs(30) if r.get("user_id") == uid]
            check("9f 工具在真实对话里被调起", any(r.get("tool") == "remind" for r in runs2),
                  f"tool_runs={[r.get('tool') for r in runs2][:3]}")
            card_evt = first(e2, "card")
            if card_evt:
                check("9g 该给时帧里有 card", bool(card_evt.get("title")),
                      f"title={card_evt.get('title')}")
            else:
                info("9g 本轮没出卡", "规则未命中（正常，不是失败）")
        except Exception as exc:
            check("9 对话链路", False, repr(exc))

    # ---------------- 10. 语音：在就要求真 WAV，不在就要求如实 503 ----------------
    try:
        t = httpx.post(f"{base}/tts", json={"text": "今天辛苦了", "emotion": "sad"}, timeout=60)
        if t.status_code == 200:
            body = t.content
            check("10 /tts 返回可播放 WAV", body[:4] == b"RIFF" and len(body) > 1024,
                  f"bytes={len(body)}")
        else:
            detail = ""
            try:
                detail = str(t.json().get("detail") or "")[:60]
            except Exception:
                pass
            check("10 语音没就绪时如实报错（不造假音）",
                  t.status_code == 503 and bool(detail), f"{t.status_code} {detail}")
    except Exception as exc:
        check("10 /tts", False, repr(exc))

    # ---------------- 11. 收尾：清测试数据 ----------------
    if not args.keep:
        try:
            cleanup(uid)
            info("11 测试数据已清理", f"user_id={uid}")
        except Exception as exc:
            info("11 测试数据清理", repr(exc))

    passed = sum(1 for _, st, _ in _results if st == "PASS")
    failed = sum(1 for _, st, _ in _results if st == "FAIL")
    skipped = sum(1 for _, st, _ in _results if st == "SKIP")
    print("-" * 64)
    print(f"结果：{passed} PASS / {failed} FAIL / {skipped} SKIP")
    if failed:
        print("未通过：")
        for name, st, detail in _results:
            if st == "FAIL":
                print(f"  - {name}  {detail}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
