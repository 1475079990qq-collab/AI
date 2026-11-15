"""SQLite 存储层：两个进程唯一共享的介质。

控制平面只写配置 / 修正，实时链路只读配置 + 写事件。
全部为同步函数，内部每次开新连接（WAL），避免跨线程 / 跨进程连接复用。
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import settings

_DB_PATH = Path(settings.DB_PATH)

DEFAULT_PERSONA = (
    "你是一个温和、耐心、会记得对方情绪的陪伴者。\n"
    "- 用口语、短句回应，不说教，不空洞安慰。\n"
    "- 只输出回复正文，不要输出情绪标签、分析过程或 JSON。\n"
    "- 若上下文提供了「近期情绪事件」，自然地体现你记得，但不要生硬复述。"
)

# 注意：temperature / top_p / num_ctx / max_tokens / top_k 都不在这里种默认值。
# 这四项（top_k 是生成采样参数）的唯一来源是「模型服务」provider 配置；
# 两边都存会让运行时默认值永久遮蔽模型卡里改的值（历史上真踩过）。
DEFAULT_PARAMS: dict[str, str] = {
    "memory_top_k": "3",
    "emotion_min_intensity": "0.6",
    "gpu_exclusive": "1",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS emotion_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    request_id    TEXT,
    emotion_label TEXT NOT NULL,
    intensity     REAL NOT NULL DEFAULT 0.5,
    confidence    REAL NOT NULL DEFAULT 0,
    summary       TEXT NOT NULL DEFAULT '',
    reply_text    TEXT NOT NULL DEFAULT '',
    fact_ids      TEXT NOT NULL DEFAULT '[]',
    degraded      INTEGER NOT NULL DEFAULT 0,
    truncated     INTEGER NOT NULL DEFAULT 0,
    closed        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ee_user ON emotion_events(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_ee_label ON emotion_events(emotion_label);

CREATE TABLE IF NOT EXISTS persona_templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 0,
    version       INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_params (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS judge_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    request_id    TEXT,
    input_text    TEXT,
    context_text  TEXT,
    output_label  TEXT,
    confidence    REAL,
    intensity     REAL,
    degraded      INTEGER NOT NULL DEFAULT 0,
    latency_ms    REAL,
    source        TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    session_id  TEXT,
    text        TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id);

CREATE TABLE IF NOT EXISTS user_profile (
    user_id    TEXT PRIMARY KEY,
    text       TEXT NOT NULL DEFAULT '',
    turns      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        if mode and str(mode[0]).lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # 并发初始化时切 WAL 可能撞锁；失败就沿用当前模式，不致命
        pass
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """对已有库做向后兼容升级（幂等，失败不阻断启动）。

    - emotion_events.reply_text：把「情绪摘要」和「整段回复」拆成两列，
      老库补列后历史行的 reply_text 为空，读侧按空串处理。
    """
    try:
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(emotion_events)").fetchall()}
        if "reply_text" not in cols:
            conn.execute("ALTER TABLE emotion_events ADD COLUMN reply_text TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass


def init_db() -> None:
    """建表 + 种子。两个进程可能同时启动 → 撞锁时重试，最终放行不阻断启动。"""
    last_error: Exception | None = None
    for attempt in range(4):
        conn = None
        try:
            conn = connect()
            conn.executescript(_SCHEMA)
            _migrate(conn)
            count = conn.execute("SELECT COUNT(*) AS n FROM persona_templates").fetchone()["n"]
            if count == 0:
                conn.execute(
                    "INSERT INTO persona_templates(name, system_prompt, is_active, version, updated_at) "
                    "VALUES(?,?,1,1,?)",
                    ("默认陪伴者", DEFAULT_PERSONA, _now()),
                )
            for key, value in DEFAULT_PARAMS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO runtime_params(key, value, updated_at) VALUES(?,?,?)",
                    (key, value, _now()),
                )
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
        finally:
            if conn is not None:
                conn.close()
    print(f"[companion] 数据库初始化被跳过（{last_error}），服务照常启动。")


# ---------- 情感事件 ----------

def open_event(*, user_id: str, session_id: str, request_id: str, label: str,
               intensity: float, confidence: float, degraded: int = 0,
               summary: str = "") -> int:
    """开流前同步落库（评审硬要求：<1ms）。

    summary 只放用户原话的短摘要（≤120 字），整段回复由 close_event(reply_text=…) 另存。
    """
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO emotion_events(user_id,session_id,timestamp,request_id,emotion_label,"
            "intensity,confidence,summary,reply_text,fact_ids,degraded,truncated,closed) "
            "VALUES(?,?,?,?,?,?,?,?,?,'[]',?,0,0)",
            (user_id, session_id, _now(), request_id, label, float(intensity),
             float(confidence), str(summary or "")[:120], "", int(degraded)),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def close_event(event_id: int, summary: str | None = None,
                fact_ids: list[str] | None = None, reply_text: str = "") -> None:
    """关事件：reply_text 存整段回复；summary 只在显式传入时覆盖（默认保留用户短摘要）。"""
    conn = connect()
    try:
        if summary is None:
            conn.execute(
                "UPDATE emotion_events SET reply_text=?, fact_ids=?, closed=1 WHERE id=?",
                (str(reply_text or ""), json.dumps(fact_ids or [], ensure_ascii=False),
                 int(event_id)),
            )
        else:
            conn.execute(
                "UPDATE emotion_events SET summary=?, reply_text=?, fact_ids=?, closed=1 WHERE id=?",
                (str(summary)[:120], str(reply_text or ""),
                 json.dumps(fact_ids or [], ensure_ascii=False), int(event_id)),
            )
        conn.commit()
    finally:
        conn.close()


def mark_truncated(event_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE emotion_events SET truncated=1, closed=1 WHERE id=?", (int(event_id),)
        )
        conn.commit()
    finally:
        conn.close()


def recent_emotions(user_id: str, min_intensity: float = 0.6, limit: int = 5) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id,user_id,session_id,timestamp,request_id,emotion_label,intensity,"
            "confidence,summary,fact_ids FROM emotion_events "
            "WHERE user_id=? AND intensity>=? AND closed=1 ORDER BY id DESC LIMIT ?",
            (user_id, float(min_intensity), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_emotion_events(label: str | None = None, min_intensity: float | None = None,
                        limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM emotion_events WHERE 1=1"
    args: list[Any] = []
    if label:
        sql += " AND emotion_label=?"
        args.append(label)
    if min_intensity is not None:
        sql += " AND intensity>=?"
        args.append(float(min_intensity))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def update_emotion_event(event_id: int, label: str | None = None,
                         intensity: float | None = None, summary: str | None = None) -> bool:
    fields: list[str] = []
    args: list[Any] = []
    if label is not None:
        fields.append("emotion_label=?")
        args.append(label)
    if intensity is not None:
        fields.append("intensity=?")
        args.append(float(intensity))
    if summary is not None:
        fields.append("summary=?")
        args.append(summary)
    if not fields:
        return False
    args.append(int(event_id))
    conn = connect()
    try:
        cur = conn.execute(f"UPDATE emotion_events SET {','.join(fields)} WHERE id=?", args)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------- 人格模板 ----------

def get_active_persona() -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM persona_templates WHERE is_active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return dict(row)
        return {"id": 0, "name": "fallback", "system_prompt": DEFAULT_PERSONA,
                "is_active": 1, "version": 1, "updated_at": _now()}
    finally:
        conn.close()


def list_personas() -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM persona_templates ORDER BY id ASC").fetchall()]
    finally:
        conn.close()


def save_persona(persona_id: int | None, name: str, system_prompt: str) -> int:
    conn = connect()
    try:
        if persona_id:
            cur = conn.execute(
                "UPDATE persona_templates SET name=?, system_prompt=?, version=version+1, "
                "updated_at=? WHERE id=?",
                (name, system_prompt, _now(), int(persona_id)),
            )
            if cur.rowcount == 0:
                cur = conn.execute(
                    "INSERT INTO persona_templates(name,system_prompt,is_active,version,updated_at) "
                    "VALUES(?,?,0,1,?)", (name, system_prompt, _now()))
                new_id = int(cur.lastrowid)
            else:
                new_id = int(persona_id)
        else:
            cur = conn.execute(
                "INSERT INTO persona_templates(name,system_prompt,is_active,version,updated_at) "
                "VALUES(?,?,0,1,?)", (name, system_prompt, _now()))
            new_id = int(cur.lastrowid)
        conn.commit()
        return new_id
    finally:
        conn.close()


def activate_persona(persona_id: int) -> dict:
    conn = connect()
    try:
        conn.execute("UPDATE persona_templates SET is_active=0")
        cur = conn.execute(
            "UPDATE persona_templates SET is_active=1, version=version+1, updated_at=? WHERE id=?",
            (_now(), int(persona_id)),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError("persona not found")
        row = conn.execute("SELECT * FROM persona_templates WHERE id=?", (int(persona_id),)).fetchone()
        return dict(row)
    finally:
        conn.close()


# ---------- 运行时参数 ----------

def get_runtime_params() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT key,value FROM runtime_params").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def set_runtime_param(key: str, value: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO runtime_params(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), _now()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- 判断日志 ----------

def log_judge(*, request_id: str, input_text: str, context_text: str, output_label: str,
              confidence: float, intensity: float, degraded: int, latency_ms: float,
              source: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO judge_logs(created_at,request_id,input_text,context_text,output_label,"
            "confidence,intensity,degraded,latency_ms,source) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (_now(), request_id, input_text, context_text, output_label, float(confidence),
             float(intensity), int(degraded), float(latency_ms), source),
        )
        conn.commit()
    finally:
        conn.close()


def list_judge_logs(limit: int = 100) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM judge_logs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]
    finally:
        conn.close()


# ---------- 事实记忆兜底 ----------

def add_fact(*, fact_id: str, user_id: str, session_id: str, text: str,
             metadata: dict | None = None) -> str:
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO facts(id,user_id,session_id,text,metadata,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (fact_id, user_id, session_id, text,
             json.dumps(metadata or {}, ensure_ascii=False), _now()),
        )
        conn.commit()
        return fact_id
    finally:
        conn.close()


def _bigrams(text: str) -> set[str]:
    cleaned = "".join(ch for ch in text.lower() if not ch.isspace())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def search_facts(user_id: str, query: str, top_k: int = 3) -> list[dict]:
    """零依赖的中文 bigram 相似度兜底，Chroma 不可用时启用。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id,user_id,session_id,text,metadata FROM facts "
            "WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 200",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    qb = _bigrams(query)
    if not qb:
        return []
    scored: list[dict] = []
    for row in rows:
        fb = _bigrams(row["text"])
        score = len(qb & fb) / max(1, len(qb))
        if score > 0:
            scored.append({"id": row["id"], "text": row["text"], "score": round(score, 4),
                           "metadata": row["metadata"]})
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:int(top_k)]


# ---------- 用户画像 ----------
# 画像只是「派生索引」：原始记忆在知识库，这里存压缩后的一页纸，便于每轮注入。

def get_profile(user_id: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT text, turns FROM user_profile WHERE user_id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"text": "", "turns": 0}
    return {"text": str(row["text"] or ""), "turns": int(row["turns"] or 0)}


def set_profile(user_id: str, text: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO user_profile(user_id, text, turns, updated_at) VALUES(?,?,0,?) "
            "ON CONFLICT(user_id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
            (user_id, text, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def bump_profile_turns(user_id: str) -> int:
    """画像计数器 +1 并返回累计轮次，供「每 N 轮刷新一次画像」判断。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO user_profile(user_id, text, turns, updated_at) VALUES(?,'',1,?) "
            "ON CONFLICT(user_id) DO UPDATE SET turns=turns+1",
            (user_id, _now()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT turns FROM user_profile WHERE user_id=?", (user_id,)
        ).fetchone()
        return int(row["turns"]) if row else 0
    finally:
        conn.close()


def list_recent_facts(user_id: str, limit: int = 60) -> list[str]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT text FROM facts WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [str(r["text"]) for r in rows if str(r["text"]).strip()]


def stats() -> dict:
    conn = connect()
    try:
        def _n(table: str) -> int:
            return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        return {
            "emotion_events": _n("emotion_events"),
            "personas": _n("persona_templates"),
            "facts": _n("facts"),
            "judge_logs": _n("judge_logs"),
            "active_persona": get_active_persona().get("name"),
        }
    finally:
        conn.close()
