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

-- 每日摘要（情景记忆）：一晚一行，热路径每轮都带，替代「检索回来才想起来」。
CREATE TABLE IF NOT EXISTS episodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    day        TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    turns      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, day)
);

-- 未完成的事：他提过但还没收尾的话题，可以主动开口用一次。
CREATE TABLE IF NOT EXISTS open_loops (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    text       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 规则自检结果：判官是规则，不是模型。
CREATE TABLE IF NOT EXISTS turn_checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_id TEXT,
    turn_id    INTEGER,
    rules      TEXT NOT NULL DEFAULT '[]',
    verdict    TEXT NOT NULL DEFAULT 'pass',
    latency_ms REAL,
    note       TEXT NOT NULL DEFAULT ''
);

-- 反刍：说完话之后，自己回头复盘学到的这一条。
-- 和人一样，错的不是「下次注意」这种空话，而是一条能改的规矩。
CREATE TABLE IF NOT EXISTS lessons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    request_id TEXT,
    kind       TEXT NOT NULL,
    note       TEXT NOT NULL,
    verdict    TEXT NOT NULL DEFAULT ''
);

-- 反馈信号：显式（对了/不对）与隐式（打断/重说/续聊）统一入库。
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_id TEXT,
    kind       TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    target     TEXT NOT NULL DEFAULT ''
);

-- 递出去的那张卡：给了什么 + 他认不认。
-- 「懂我」不是感觉，是这两列：给过什么、给完之后他点没点那个勾。
CREATE TABLE IF NOT EXISTS deliveries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    request_id TEXT,
    turn_id    INTEGER,
    user_id    TEXT,
    kind       TEXT,
    title      TEXT,
    body       TEXT,                 -- 卡片内容 JSON（items 等）
    verdict    INTEGER DEFAULT 0     -- 0 没评 / 1 对了 / -1 不对
);
CREATE INDEX IF NOT EXISTS idx_deliveries_req ON deliveries(request_id);

-- 它真去办了哪些事（查了什么、记了什么）。没有这张表，「工具层」就只是传说。
CREATE TABLE IF NOT EXISTS tool_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_id TEXT,
    user_id    TEXT,
    tool       TEXT NOT NULL,
    args       TEXT NOT NULL DEFAULT '',
    ok         INTEGER NOT NULL DEFAULT 0,
    summary    TEXT NOT NULL DEFAULT '',
    ms         REAL NOT NULL DEFAULT 0
);

-- 它自己先开的口。主动开口必须有账：什么时候说的、说了什么、有没有被送到。
CREATE TABLE IF NOT EXISTS proactive_msgs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    text         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'care',
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_proactive_user ON proactive_msgs(user_id, delivered_at);

-- 口吻正样本：他点过「对了」的轮次，原样留一份。
-- 不训练、只积累——攒够 300–500 条，LoRA 才有料。
-- 存的是清洗后的正文（不含括号旁白），因为训练时不该学那些。
CREATE TABLE IF NOT EXISTS tone_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    request_id    TEXT NOT NULL UNIQUE,
    user_id       TEXT,
    input_text    TEXT NOT NULL DEFAULT '',
    reply_text    TEXT NOT NULL DEFAULT '',
    emotion_label TEXT NOT NULL DEFAULT '',
    intensity     REAL NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT ''
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
    """人不会只用一种线索回忆：词像、最近说过、带着情绪，三路一起上来。

    - 词重合：本轮 query 与事实共享的两字词比例（原来的唯一路径）
    - 最近说过：越新的事越容易被想起（权重低，只做次级线索）
    - 带情绪：metadata 里记过情绪的，容易被同类话题勾出来
    score 仍以词重合为主，后两项只加分；`why` 记录这一条是被哪条线索捞回来的。
    """
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
    total = max(1, len(rows))
    scored: list[dict] = []
    for idx, row in enumerate(rows):
        fb = _bigrams(row["text"])
        sim = len(qb & fb) / max(1, len(qb))
        recent = 1.0 - (idx / total)               # 1.0 = 最新那条
        emo = 0.18 if '"emotion"' in (row["metadata"] or "") else 0.0
        if sim <= 0 and recent < 0.55:
            continue
        why: list[str] = []
        if sim > 0:
            why.append("词重合")
        if recent >= 0.55:
            why.append("最近说过")
        if emo:
            why.append("带情绪")
        scored.append({
            "id": row["id"], "text": row["text"],
            "score": round(sim + recent * 0.22 + emo, 4),
            "sim": round(sim, 4), "why": why,
            "metadata": row["metadata"],
        })
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
            "episodes": _n("episodes"),
            "open_loops": _n("open_loops"),
            "turn_checks": _n("turn_checks"),
            "feedback": _n("feedback"),
            "active_persona": get_active_persona().get("name"),
        }
    finally:
        conn.close()


# ---------- 记忆层：每日摘要 ----------

def upsert_episode(user_id: str, day: str, text: str, turns: int = 0) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO episodes(user_id,day,text,turns,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id,day) DO UPDATE SET text=excluded.text, "
            "turns=excluded.turns, updated_at=excluded.updated_at",
            (user_id, day, str(text or ""), int(turns), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_episodes(user_id: str, days: int = 3) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT day,text,turns FROM episodes WHERE user_id=? "
            "ORDER BY day DESC LIMIT ?", (user_id, max(1, int(days))),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def day_rows(user_id: str, day: str) -> list[dict]:
    """当天发生过的事（用户原话摘要 + 情绪）。用于拼每日摘要，不做任何模型生成。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT timestamp,emotion_label,intensity,summary,reply_text FROM emotion_events "
            "WHERE user_id=? AND timestamp>=? AND timestamp<? ORDER BY id ASC",
            (user_id, day + " 00:00:00", day + " 23:59:59"),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- 记忆层：未完成的事 ----------

def add_open_loop(user_id: str, text: str, keep: int = 10) -> None:
    """记一件他提过、还没收尾的事；同文去重，只保留最近 keep 条 open。"""
    cleaned = str(text or "").strip()
    if len(cleaned) < 6:
        return
    conn = connect()
    try:
        dup = conn.execute(
            "SELECT id FROM open_loops WHERE user_id=? AND status='open' AND text=?",
            (user_id, cleaned),
        ).fetchone()
        if dup:
            return
        conn.execute(
            "INSERT INTO open_loops(user_id,text,status,created_at,updated_at) "
            "VALUES(?,?,'open',?,?)", (user_id, cleaned, _now(), _now()),
        )
        rows = conn.execute(
            "SELECT id FROM open_loops WHERE user_id=? AND status='open' ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        for r in rows[int(keep):]:
            conn.execute("UPDATE open_loops SET status='dropped', updated_at=? WHERE id=?",
                         (_now(), int(r["id"])))
        conn.commit()
    finally:
        conn.close()


def list_open_loops(user_id: str, limit: int = 3) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id,text,created_at FROM open_loops WHERE user_id=? AND status='open' "
            "ORDER BY id DESC LIMIT ?", (user_id, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def resolve_open_loop(loop_id: int, status: str = "done") -> bool:
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE open_loops SET status=?, updated_at=? WHERE id=?",
            (str(status), _now(), int(loop_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------- 质量自检 ----------

def log_check(*, request_id: str, turn_id: int | None, rules: list[str],
              verdict: str, latency_ms: float, note: str = "") -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO turn_checks(created_at,request_id,turn_id,rules,verdict,latency_ms,note) "
            "VALUES(?,?,?,?,?,?,?)",
            (_now(), request_id, turn_id,
             json.dumps(rules or [], ensure_ascii=False), str(verdict),
             float(latency_ms), str(note or "")),
        )
        conn.commit()
    finally:
        conn.close()


def recent_checks(limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM turn_checks ORDER BY id DESC LIMIT ?", (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_lesson(*, user_id: str, request_id: str, kind: str, note: str,
               verdict: str = "", keep: int = 200) -> int:
    """反刍留下的这一条经验；只留最近 keep 条，避免无限堆积。"""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO lessons(created_at,user_id,request_id,kind,note,verdict) "
            "VALUES(?,?,?,?,?,?)",
            (_now(), str(user_id), str(request_id), str(kind), str(note), str(verdict)),
        )
        conn.execute(
            "DELETE FROM lessons WHERE id <= (SELECT id FROM lessons ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (max(1, int(keep)),),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_lessons(user_id: str, limit: int = 3) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM lessons WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (str(user_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def recent_lessons(limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM lessons ORDER BY id DESC LIMIT ?",
                            (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def checks_summary(days: int = 7) -> dict:
    """最近 N 天的自检概览：不合格率 + 各规则命中次数。"""
    since = (datetime.datetime.now() - datetime.timedelta(days=int(days))
             ).strftime("%Y-%m-%d %H:%M:%S")
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT verdict,rules,latency_ms FROM turn_checks WHERE created_at>=?",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    total = len(rows)
    fails = sum(1 for r in rows if str(r["verdict"]) != "pass")
    hits: dict[str, int] = {}
    for r in rows:
        try:
            for name in json.loads(r["rules"] or "[]"):
                hits[str(name)] = hits.get(str(name), 0) + 1
        except Exception:
            continue
    lat = [float(r["latency_ms"] or 0) for r in rows]
    lat.sort()
    p50 = lat[len(lat) // 2] if lat else 0.0
    return {
        "days": int(days),
        "total": total,
        "fails": fails,
        "fail_rate": round(fails / total, 4) if total else 0.0,
        "rules": dict(sorted(hits.items(), key=lambda kv: kv[1], reverse=True)),
        "latency_p50_ms": round(p50, 1),
    }


# ---------- 反馈 ----------

def add_feedback(*, request_id: str, kind: str, reason: str = "",
                 target: str = "") -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO feedback(created_at,request_id,kind,reason,target) VALUES(?,?,?,?,?)",
            (_now(), str(request_id or ""), str(kind), str(reason or ""), str(target or "")),
        )
        conn.commit()
    finally:
        conn.close()


_TONE_SAMPLE_DDL = """
CREATE TABLE IF NOT EXISTS tone_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    request_id    TEXT NOT NULL UNIQUE,
    user_id       TEXT,
    input_text    TEXT NOT NULL DEFAULT '',
    reply_text    TEXT NOT NULL DEFAULT '',
    emotion_label TEXT NOT NULL DEFAULT '',
    intensity     REAL NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT ''
)
"""


def capture_tone_sample(request_id: str, note: str = "") -> bool:
    """把这轮「他说对了」的输入+回复存进口吻语料。返回是否真的存进去。

    只在 verdict=1 时调用；同一 request_id 只存一次（前端重复点勾不会灌水）。
    取自 turn_logs，而不是请求体——客户端说什么都可以，日志里那版才是真说出去的。
    """
    req = str(request_id or "").strip()
    if not req:
        return False
    conn = connect()
    try:
        conn.execute(_TONE_SAMPLE_DDL)
        row = conn.execute(
            "SELECT user_id,input_text,reply_text,emotion_label,intensity "
            "FROM turn_logs WHERE request_id=? ORDER BY id DESC LIMIT 1",
            (req,),
        ).fetchone()
        if not row:
            return False
        reply = str(row["reply_text"] or "").strip()
        user_text = str(row["input_text"] or "").strip()
        # 空回复、太短的（「嗯」「好呀」）当不成训练样本，只会教坏语感
        if len(reply) < 8 or not user_text:
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO tone_samples"
            "(created_at,request_id,user_id,input_text,reply_text,emotion_label,intensity,note) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (_now(), req, row["user_id"], user_text, reply,
             str(row["emotion_label"] or ""), float(row["intensity"] or 0.0), str(note or "")),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def count_tone_samples() -> int:
    conn = connect()
    try:
        conn.execute(_TONE_SAMPLE_DDL)
        row = conn.execute("SELECT COUNT(*) FROM tone_samples").fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        conn.close()


def list_tone_samples(limit: int = 500) -> list[dict]:
    conn = connect()
    try:
        conn.execute(_TONE_SAMPLE_DDL)
        rows = conn.execute(
            "SELECT created_at,request_id,user_id,input_text,reply_text,emotion_label,intensity "
            "FROM tone_samples ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def recent_feedback(limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- 递出去的卡 ----------

def add_delivery(*, request_id: str, turn_id: int | None, user_id: str,
                 kind: str, title: str, body: str) -> int | None:
    """记一条「给了什么」。返回 id，失败返回 None（绝不因为记不上就中断回答）。"""
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO deliveries(created_at,request_id,turn_id,user_id,kind,title,body) "
            "VALUES(?,?,?,?,?,?,?)",
            (_now(), str(request_id or ""), turn_id, str(user_id or ""),
             str(kind or ""), str(title or ""), str(body or "")),
        )
        conn.commit()
        return int(cur.lastrowid or 0) or None
    except Exception:
        return None
    finally:
        conn.close()


def set_delivery_verdict(delivery_id: int, verdict: int) -> bool:
    conn = connect()
    try:
        conn.execute(
            "UPDATE deliveries SET verdict=?, updated_at=? WHERE id=?",
            (int(verdict), _now(), int(delivery_id)),
        )
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def recent_deliveries(limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM deliveries ORDER BY id DESC LIMIT ?", (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- 工具层 ----------

def add_tool_run(*, request_id: str, user_id: str, tool: str, args: str,
                 ok: bool, summary: str, ms: float) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO tool_runs(created_at,request_id,user_id,tool,args,ok,summary,ms) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (_now(), str(request_id or ""), str(user_id or ""), str(tool or ""),
             str(args or "")[:500], 1 if ok else 0, str(summary or "")[:500],
             float(ms or 0)),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def recent_tool_runs(limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tool_runs ORDER BY id DESC LIMIT ?", (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- 主动开口 ----------

def add_proactive(user_id: str, text: str, kind: str = "care") -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO proactive_msgs(created_at,user_id,text,kind) VALUES(?,?,?,?)",
            (_now(), str(user_id or ""), str(text or ""), str(kind or "care")),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def pending_proactive(user_id: str, limit: int = 3) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM proactive_msgs WHERE user_id=? AND delivered_at IS NULL "
            "ORDER BY id ASC LIMIT ?", (str(user_id or ""), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_proactive_delivered(msg_id: int) -> None:
    conn = connect()
    try:
        conn.execute("UPDATE proactive_msgs SET delivered_at=? WHERE id=?",
                     (_now(), int(msg_id)))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def recent_proactive(limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM proactive_msgs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]
    finally:
        conn.close()


def proactive_today_count(user_id: str) -> int:
    """今天已经说出去几次（不管送没送到，只要生成过就算，避免反复生成）。"""
    day = _now()[:10]
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM proactive_msgs WHERE user_id=? AND created_at >= ?",
            (str(user_id or ""), day + " 00:00:00"),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def last_turn_at(user_id: str) -> str:
    """最后一次真实对话的时间；没有返回空串。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT MAX(created_at) FROM turn_logs WHERE user_id=?", (str(user_id or ""),),
        ).fetchone()
        return str(row[0] or "") if row else ""
    finally:
        conn.close()
