# -*- coding: utf-8 -*-
"""体检脚本：一屏看清「现在到底在跑什么、用的哪个模型、哪里在打架」。

为什么需要它：用户反复说「系统乱、感觉缺东西」，但说不清乱在哪。
乱是一种体感，只有变成一张清单才可处理。

用法（在 companion 目录下，用工程自带环境跑）：
    .venv\\Scripts\\python.exe tools\\doctor.py
    .venv\\Scripts\\python.exe tools\\doctor.py --json     # 给看板/脚本用

不改任何配置、不写数据库（除了什么都不写），纯读。
"""
from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "companion.db"
CONFIG_PATH = ROOT / "config.yaml"

OK, WARN, BAD = "[OK]  ", "[WARN]", "[FAIL]"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    except sqlite3.Error:
        return None


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _http_json(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _yaml_flat(path: Path) -> dict[str, str]:
    """与 brain/config.py 同款解析：只认 key: value，忽略 section 行。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().upper(), value.strip().strip('"').strip("'")
        if key and value:
            out[key] = value
    return out


def _provider(conn: sqlite3.Connection, code: str) -> dict:
    try:
        row = conn.execute("SELECT * FROM providers WHERE code=?", (code,)).fetchone()
        if not row:
            return {}
        data = dict(row)
        cfg = {}
        # 表里真实列名是 config_json（历史代码里有 config 的旧叫法，两个都认）
        for key in ("config_json", "config"):
            raw = data.get(key)
            if isinstance(raw, str) and raw.strip():
                try:
                    cfg = json.loads(raw)
                    break
                except json.JSONDecodeError:
                    continue
            if isinstance(raw, dict):
                cfg = raw
                break
        return {"code": code, "enabled": data.get("enabled"), "mode": data.get("mode"),
                "config": cfg}
    except sqlite3.Error:
        return {}


def _latency(conn: sqlite3.Connection, limit: int = 20) -> dict:
    """最近 N 轮耗时：优先 turn_logs，没有就退 turn_checks。"""
    for table in ("turn_logs", "turn_checks"):
        cols = _cols(conn, table)
        if "latency_ms" not in cols:
            continue
        try:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE latency_ms IS NOT NULL "
                "ORDER BY id DESC LIMIT ?", (int(limit),),
            ).fetchall()
        except sqlite3.Error:
            continue
        vals = [float(r["latency_ms"] or 0) for r in rows if float(r["latency_ms"] or 0) >= 0]
        if vals:
            vals_sorted = sorted(vals)
            out = {
                "source": table,
                "n": len(vals),
                "avg": round(sum(vals) / len(vals), 0),
                "p50": round(vals_sorted[len(vals_sorted) // 2], 0),
                "slowest": round(max(vals), 0),
            }
            # 降级轮次：走的是关键词规则而不是真模型，这类轮次「答得像」但「不准」
            if "degraded" in cols:
                try:
                    out["degraded"] = int(conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE degraded=1 "
                        "AND id IN (SELECT id FROM " + table + " ORDER BY id DESC LIMIT ?)",
                        (int(limit),),
                    ).fetchone()["n"])
                except sqlite3.Error:
                    pass
            return out
    return {}


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _gib(n: int) -> str:
    return f"{n / (1024 ** 3):.2f} GB"


def collect() -> dict:
    y = _yaml_flat(CONFIG_PATH)
    brain_port = int(y.get("BRAIN_PORT", "8000") or 8000)
    host = "127.0.0.1"

    report: dict = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sections": []}

    # ---- 1) 进程 ----
    try:
        conn = _conn()
    except sqlite3.Error as exc:
        report["sections"].append({"title": "数据库", "lines": [
            (BAD, f"打不开 {DB_PATH}: {exc}")]})
        return report

    llm = _provider(conn, "llm")
    llm_base = str((llm.get("config") or {}).get("base_url") or "")
    llm_port = 0
    if llm_base:
        tail = llm_base.rsplit(":", 1)
        if len(tail) == 2 and tail[1].strip("/").isdigit():
            llm_port = int(tail[1].strip("/"))
    if not llm_port:
        tail = str(y.get("OLLAMA_BASE", "")).rsplit(":", 1)
        llm_port = int(tail[1].strip("/")) if len(tail) == 2 and tail[1].isdigit() else 11434

    tts_port = int(y.get("TTS_PORT", "8102") or 8102)
    asr_port = int(y.get("ASR_PORT", "8103") or 8103)
    judge_port = int(y.get("JUDGE_PORT", "8101") or 8101)

    judge = _provider(conn, "judge")
    judge_mode = str(judge.get("mode") or "").lower()
    procs = [
        ("大脑/控制台", brain_port, True),
        ("对话大模型", llm_port, True),
        ("语音识别", asr_port, False),
    ]
    # 语音合成已并进大脑（进程内），不再占 :8102；独立跑只用于调试
    procs.append(("语音合成(进程内引擎)", -1, False))
    # 判断已改成进程内 onnx：这时不该再有 :8101 的独立进程，也不该去探它
    if judge_mode in ("onnx", "rule", "off"):
        procs.append((f"情感判断(进程内 {judge_mode})", -1, False))
    else:
        procs.append(("情感判断(独立进程)", judge_port, False))

    lines = []
    for name, port, required in procs:
        if port < 0:
            lines.append((OK, f"{name:<28} 进程内运行，不占端口"))
            continue
        up = _port_open(host, port)
        tag = OK if up else (BAD if required else WARN)
        lines.append((tag, f"{name:<28} :{port}  {'在听' if up else '没起'}"))
    health = _http_json(f"http://{host}:{brain_port}/health")
    if health:
        lines.insert(0, (OK, f"大脑健康检查 status={health.get('status')} db={health.get('db')}"))
    else:
        lines.insert(0, (WARN, "大脑 /health 无响应（没启动或端口不对）"))
    report["sections"].append({"title": "进程与端口", "lines": lines})

    # ---- 2) 生效配置 vs 配置文件（双轨检测）----
    lines = []
    if llm:
        cfg = llm.get("config") or {}
        lines.append((OK, f"生效大模型  {cfg.get('base_url','')}  型号={cfg.get('model','')}  "
                          f"mode={llm.get('mode')} enabled={llm.get('enabled')}"))
        yaml_base = str(y.get("OLLAMA_BASE", ""))
        yaml_model = str(y.get("LLM_MODEL", ""))
        # 只有当数据库里没有这张卡时，config.yaml 的兜底才真的生效；
        # 否则那两行只是摆设，不该报警（报警会让真问题被噪音淹掉）。
        if not cfg.get("base_url"):
            lines.append((WARN, f"数据库缺大模型卡，这次实际用的是 config.yaml 兜底：{yaml_base}"))
        elif yaml_base and yaml_base.rstrip("/") != str(cfg.get("base_url")).rstrip("/"):
            lines.append((OK, f"config.yaml 里那行 {yaml_base} 是兜底值，当前未生效（以数据库为准）"))
    alt = _provider(conn, "llm_alt")
    if alt:
        lines.append((OK if str(alt.get("mode")) == "off" else WARN,
                      f"备用模型卡 mode={alt.get('mode')} enabled={alt.get('enabled')}"
                      + ("（关着，正常）" if str(alt.get("mode")) == "off" else "（开着=接管主模型）")))
    kb = _provider(conn, "knowledge")
    if kb:
        kb_off = str(kb.get("mode")) == "off" or int(kb.get("enabled") or 0) == 0
        lines.append((OK if kb_off else BAD,
                      f"知识库 mode={kb.get('mode')} enabled={kb.get('enabled')}"
                      + ("（已关闭，不再每轮注入）" if kb_off else "（仍在每轮注入=污染源）")))
    params = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM runtime_params")}
    lines.append((OK, f"身份称呼  助手={params.get('assistant_name') or '未设置'}  "
                      f"用户={params.get('user_name') or '未设置'}"))
    report["sections"].append({"title": "生效配置", "lines": lines})

    # ---- 3) 最近耗时 ----
    lat = _latency(conn)
    lines = []
    if lat:
        tag = OK if lat["p50"] <= 1500 else (WARN if lat["p50"] <= 2500 else BAD)
        lines.append((tag, f"最近 {lat['n']} 轮：中位 {int(lat['p50'])}ms  平均 {int(lat['avg'])}ms  "
                           f"最慢 {int(lat['slowest'])}ms（来源 {lat['source']}）"))
        if lat.get("degraded"):
            r = lat["degraded"] / max(1, lat["n"])
            lines.append((WARN if r < 0.35 else BAD,
                          f"其中 {lat['degraded']} 轮是降级回答（{r * 100:.0f}%）："
                          "这类轮次用的是规则/兜底，不是真模型"))
    else:
        lines.append((WARN, "没有耗时记录（还没聊过，或 turn_logs 为空）"))
    report["sections"].append({"title": "速度", "lines": lines})

    # ---- 4) 自检概览 ----
    try:
        from brain.db import checks_summary  # 复用同一份统计口径
        summary = checks_summary(7)
    except Exception:
        summary = {}
    lines = []
    if summary and summary.get("total"):
        tag = OK if summary["fail_rate"] <= 0.1 else (WARN if summary["fail_rate"] <= 0.25 else BAD)
        lines.append((tag, f"近 7 天 {summary['total']} 轮，不合格 {summary['fails']} 轮"
                           f"（{summary['fail_rate'] * 100:.1f}%）"))
        for name, n in list(summary["rules"].items())[:6]:
            lines.append((WARN, f"  {name}: {n} 次"))
    else:
        lines.append((WARN, "还没有自检记录（对话一轮后才有）"))
    report["sections"].append({"title": "质量自检", "lines": lines})

    # ---- 5) 记忆 ----
    lines = []
    for table, label in (("facts", "事实"), ("episodes", "每日摘要"),
                         ("open_loops", "未完成的事"), ("turn_checks", "自检记录")):
        n = _count(conn, table)
        if n is None:
            lines.append((WARN, f"{label:<8} 表还没建（重启一次大脑后自动创建）"))
        else:
            lines.append((OK, f"{label:<8} {n} 条"))
    try:
        bad = conn.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE text LIKE '%# Summary%' OR text LIKE '%来源:mcp%'"
        ).fetchone()["n"]
        lines.append((OK if bad == 0 else BAD, f"事实里的纪要元文本污染：{bad} 条"))
    except sqlite3.Error:
        pass
    report["sections"].append({"title": "记忆", "lines": lines})

    # ---- 6) 磁盘 ----
    lines = []
    items = [("models/IndexTTS-2（已切到 gpt_sovits，可删）", ROOT / "models" / "IndexTTS-2"),
             ("models/", ROOT / "models"),
             ("client/build/", ROOT / "client" / "build"),
             (".venv/", ROOT / ".venv")]
    for label, path in items:
        if path.exists():
            lines.append((OK, f"{label:<40} {_gib(_dir_size(path))}"))
    apk = ROOT / "companion-debug.apk"
    if apk.exists():
        lines.append((OK, f"{'companion-debug.apk':<40} {_gib(apk.stat().st_size)}"))
    report["sections"].append({"title": "磁盘", "lines": lines})

    conn.close()
    return report


def render(report: dict) -> str:
    out = [f"companion 体检  {report['time']}", "=" * 64]
    for sec in report.get("sections", []):
        out.append("")
        out.append(f"--- {sec['title']} " + "-" * max(0, 60 - len(sec["title"])))
        for tag, text in sec["lines"]:
            out.append(f"{tag} {text}")
    out.append("")
    out.append("=" * 64)
    out.append("提示：配置一律以数据库为准；这里只读不写。")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    report = collect()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
