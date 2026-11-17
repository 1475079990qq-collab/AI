"""SQLite 存储层：两个进程唯一共享的介质。

控制平面只写配置 / 修正，实时链路只读配置 + 写事件。
全部为同步函数，内部每次开新连接（WAL），避免跨线程 / 跨进程连接复用。
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import threading
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
    # V2.5
    "channel_enabled": "1",
    "channel_high_intensity": "0.75",
    "channel_risk_words": "钱,转账,健康,病,隐私,删除,离婚,离职,投资,合同",
    "channel_deep_phrases": "想清楚,认真想,仔细考虑,权衡,想明白",
    "trace_recent_limit": "40",
    "decay_every_turns": "20",
    "eval_window_turns": "10",
    "feedback_weight_delta": "0.05",
    "v25_orchestrator_enabled": "1",
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

-- 神经元：脑图上的一颗点就是一个真实节点（事实 / 概念 / 教训 / 策略）。
-- 属性照 V2 契约：importance、confidence、activation、activation_count、last_activated。
CREATE TABLE IF NOT EXISTS neurons (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    kind             TEXT NOT NULL,
    ref_id           TEXT NOT NULL DEFAULT '',
    label            TEXT NOT NULL DEFAULT '',
    text             TEXT NOT NULL DEFAULT '',
    importance       REAL NOT NULL DEFAULT 0.5,
    confidence       REAL NOT NULL DEFAULT 0.5,
    activation       REAL NOT NULL DEFAULT 0,
    activation_count INTEGER NOT NULL DEFAULT 0,
    last_activated   TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_neurons_user ON neurons(user_id, kind);

-- 突触：有方向的真实关系。relation ∈ about / cooccur / semantic / lesson / strategy。
CREATE TABLE IF NOT EXISTS synapses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT NOT NULL,
    src              TEXT NOT NULL,
    dst              TEXT NOT NULL,
    relation         TEXT NOT NULL,
    weight           REAL NOT NULL DEFAULT 0.3,
    activation_count INTEGER NOT NULL DEFAULT 0,
    last_activated   TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(user_id, src, dst, relation)
);
CREATE INDEX IF NOT EXISTS idx_syn_src ON synapses(user_id, src);
CREATE INDEX IF NOT EXISTS idx_syn_dst ON synapses(user_id, dst);

-- 激活记录：哪一轮点亮了哪些神经元。可回放，不是动画。
CREATE TABLE IF NOT EXISTS activations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_id TEXT,
    user_id    TEXT,
    neuron_id  TEXT NOT NULL,
    energy     REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_act_req ON activations(request_id);

-- 策略：候选 → 启用 → 回滚。策略不是人格，是一组带证据的行为参数。
CREATE TABLE IF NOT EXISTS strategies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    evidence   INTEGER NOT NULL DEFAULT 0,
    success    INTEGER NOT NULL DEFAULT 0,
    failure    INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'candidate',
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, name, version)
);

-- V2.5：agent 情绪状态（每用户一行，valence/arousal/energy/stance）
CREATE TABLE IF NOT EXISTS agent_state (
    user_id     TEXT PRIMARY KEY,
    valence     REAL NOT NULL DEFAULT 0.0,
    arousal     REAL NOT NULL DEFAULT 0.0,
    energy      REAL NOT NULL DEFAULT 0.5,
    stance      TEXT NOT NULL DEFAULT 'steady',
    updated_at  TEXT NOT NULL
);
-- V2.5：目标（inferred / explicit，候选 → 活跃 → 完成/暂停）
CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'paused',
    source      TEXT NOT NULL DEFAULT 'inferred',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goals_user_status ON goals(user_id, status);
-- V2.5：知识来源（带信任度，供信念溯源）
CREATE TABLE IF NOT EXISTS knowledge_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    uri         TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'unknown',
    trust       REAL NOT NULL DEFAULT 0.5,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ks_user_kind ON knowledge_sources(user_id, kind);
-- V2.5：信念（可被新证据取代，superseded_by 指向更新的一条）
CREATE TABLE IF NOT EXISTS beliefs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    subject       TEXT NOT NULL,
    predicate     TEXT NOT NULL DEFAULT '',
    object        TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0.5,
    source_id     INTEGER REFERENCES knowledge_sources(id),
    superseded_by INTEGER REFERENCES beliefs(id),
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beliefs_user_subject ON beliefs(user_id, subject);
CREATE INDEX IF NOT EXISTS idx_beliefs_source ON beliefs(source_id);
-- V2.5：认知轨迹（每轮怎么想，trace_id 聚合，重启不丢）
CREATE TABLE IF NOT EXISTS cognitive_trace (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    turn_id     INTEGER,
    kind        TEXT NOT NULL,
    node_type   TEXT NOT NULL DEFAULT 'activity',
    payload     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ct_trace ON cognitive_trace(trace_id);
CREATE INDEX IF NOT EXISTS idx_ct_user_id ON cognitive_trace(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_ct_kind ON cognitive_trace(kind);
CREATE INDEX IF NOT EXISTS idx_ct_node_type ON cognitive_trace(node_type);
-- V2.5：策略评估结果（策略在某窗口内的前后分数与去留决策）
CREATE TABLE IF NOT EXISTS evaluation_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    window        TEXT NOT NULL DEFAULT '',
    before_score  REAL NOT NULL DEFAULT 0.0,
    after_score   REAL NOT NULL DEFAULT 0.0,
    decision      TEXT NOT NULL DEFAULT 'keep',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_er_user_strategy ON evaluation_results(user_id, strategy);
"""


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# WAL 切换必须在「建业务连接之前」串行完成：若把切换放进每个 connect()，
# 多线程/进程各自第一次连新库会同时切 WAL，撞锁的同时连接会拿到旧 pager 状态，
# 后续 INSERT 报 "attempt to write a readonly database"（trace 写入就栽在这）。
_WAL_INIT_LOCK = threading.Lock()
# 每个库路径保留一个常驻引导连接，维持 WAL 索引文件稳定。
_WAL_KEEPERS: dict[str, sqlite3.Connection] = {}


def _ensure_wal(path: str) -> None:
    """每库路径只初始化一次 WAL；撞锁重试，仍失败就沿用当前模式，不致命。"""
    if path in _WAL_KEEPERS:
        return
    with _WAL_INIT_LOCK:
        if path in _WAL_KEEPERS:
            return
        boot = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
        for attempt in range(3):
            try:
                mode = boot.execute("PRAGMA journal_mode").fetchone()
                if mode and str(mode[0]).lower() != "wal":
                    boot.execute("PRAGMA journal_mode=WAL").fetchone()
                # 切到 WAL 后必须做一次真实写入来初始化 -wal/-shm 索引文件，
                # 否则后续并发新连接会拿到只读态，INSERT 报
                # "attempt to write a readonly database"。写回原值即无害 no-op。
                uv = boot.execute("PRAGMA user_version").fetchone()[0]
                boot.execute(f"PRAGMA user_version={uv}")
                break
            except sqlite3.OperationalError:
                # PRAGMA 撞锁失败后可能停在未完成事务态，清掉再短退避重试。
                try:
                    boot.rollback()
                except sqlite3.OperationalError:
                    pass
                if attempt < 2:
                    time.sleep(0.01 * (attempt + 1))
        # 不关闭 boot：常驻维持 WAL 文件，避免最后一个连接关闭删掉 WAL 索引。
        _WAL_KEEPERS[path] = boot


def connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    path = str(_DB_PATH)
    # WAL 在独立引导连接上一次性切好并初始化，业务连接只做普通打开，
    # 避免并发首次连库时各自切 WAL 撞锁、或拿到只读 pager 态。
    _ensure_wal(path)
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 兜底：返回的连接不在未提交事务中。
    try:
        conn.rollback()
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """老库补一列；列已存在或表不存在都静默跳过，绝不阻断启动。"""
    try:
        cols = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError:
        pass


def _migrate(conn: sqlite3.Connection) -> None:
    """对已有库做向后兼容升级（幂等，失败不阻断启动）。

    - emotion_events.reply_text：把「情绪摘要」和「整段回复」拆成两列，
      老库补列后历史行的 reply_text 为空，读侧按空串处理。
    - lessons 真反思字段：source/model 或 rule、why/how 两段、confidence、降级原因。
      老列 id/created_at/user_id/request_id/kind/note/verdict 原样保留。
    """
    try:
        cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(emotion_events)").fetchall()}
        if "reply_text" not in cols:
            conn.execute("ALTER TABLE emotion_events ADD COLUMN reply_text TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    _add_column(conn, "lessons", "source", "TEXT NOT NULL DEFAULT 'rule'")
    _add_column(conn, "lessons", "why", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "lessons", "how", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "lessons", "confidence", "REAL NOT NULL DEFAULT 0")
    _add_column(conn, "lessons", "degrade_reason", "TEXT NOT NULL DEFAULT ''")
    # V2：教训不再是平权的「最近三条」，而是带权重和成败计数的经验。
    _add_column(conn, "lessons", "weight", "REAL NOT NULL DEFAULT 0.5")
    _add_column(conn, "lessons", "uses", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "lessons", "success", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "lessons", "failure", "INTEGER NOT NULL DEFAULT 0")
    # V2：事实不再全是同权重的原话，写进来时就有类型和重要程度。
    _add_column(conn, "facts", "kind", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "facts", "importance", "REAL NOT NULL DEFAULT 0.5")
    _add_column(conn, "facts", "confidence", "REAL NOT NULL DEFAULT 0.5")
    _add_column(conn, "facts", "activation", "REAL NOT NULL DEFAULT 0")
    _add_column(conn, "facts", "activation_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "facts", "last_activated", "TEXT")


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
             metadata: dict | None = None, kind: str = "",
             importance: float = 0.5, confidence: float = 0.5) -> str:
    """写一条事实。kind/importance/confidence 由写入侧判定，回读时参与排序。

    为什么带这三个属性：以前的 71 条事实权重全一样，「今天吃了什么」和
    「他忌口不吃香菜」在召回里并列，人的记忆不是这样排的。
    """
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO facts(id,user_id,session_id,text,metadata,created_at,"
            "kind,importance,confidence,activation,activation_count) "
            "VALUES(?,?,?,?,?,?,?,?,?,0,0)",
            (fact_id, user_id, session_id, text,
             json.dumps(metadata or {}, ensure_ascii=False), _now(),
             str(kind or ""), float(importance), float(confidence)),
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
    """人不会只用一种线索回忆：词像、最近说过、带着情绪、这件事有多重要，四路一起上。

    - 词重合：本轮 query 与事实共享的两字词比例（原来的唯一路径）
    - 最近说过：越新的事越容易被想起（权重低，只做次级线索）
    - 带情绪：metadata 里记过情绪的，容易被同类话题勾出来
    - 重要程度：写入侧判过 importance 的事（身份 / 偏好 / 忌口 / 长期目标）
      会稳定压过「今天吃了什么」这种流水；越常被想起的（activation）再加一点。
    score 以词重合为主，其余只加分；`why` 记录这一条是被哪条线索捞回来的。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id,user_id,session_id,text,metadata,kind,importance,confidence,"
            "activation,activation_count FROM facts "
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
        try:
            imp = max(0.0, min(1.0, float(row["importance"] or 0.5)))
        except (TypeError, ValueError):
            imp = 0.5
        try:
            act = max(0.0, min(1.0, float(row["activation"] or 0.0)))
        except (TypeError, ValueError):
            act = 0.0
        if sim <= 0 and recent < 0.55 and not (imp >= 0.85 and recent >= 0.15):
            continue
        why: list[str] = []
        if sim > 0:
            why.append("词重合")
        if recent >= 0.55:
            why.append("最近说过")
        if emo:
            why.append("带情绪")
        if imp >= 0.75:
            why.append("重要记忆")
        if act >= 0.5:
            why.append("常被想起")
        scored.append({
            "id": row["id"], "text": row["text"], "kind": str(row["kind"] or ""),
            "importance": round(imp, 3), "confidence": round(float(row["confidence"] or 0.5), 3),
            "score": round(sim + recent * 0.22 + emo + imp * 0.30 + act * 0.15, 4),
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


def _note_similar(a: str, b: str) -> float:
    """两条教训的两字重合度；用于相近教训合并，避免同一类问题堆几十行。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / max(1, len(ga | gb))


def add_lesson(*, user_id: str, request_id: str, kind: str, note: str,
               verdict: str = "", keep: int = 200, source: str = "rule",
               why: str = "", how: str = "", confidence: float = 0.0,
               degrade_reason: str = "", weight: float = 0.5) -> tuple[int, bool]:
    """记一条复盘经验；同用户同类型（或文本高度相近）的旧教训直接合并更新。

    返回 (lesson_id, merged)：merged=True 表示没有新增行，而是更新了旧教训。
    只留最近 keep 条，避免无限堆积。既有列 id/created_at/user_id/request_id/
    kind/note/verdict 一个不少，新增列只做补充。

    weight 是「这条经验现在有多值得听」：同类问题再犯一次就加固一点，
    反馈说「不对」时由 evolution 侧下调。prompt 侧按 weight 取前三条。
    """
    note = str(note or "").strip()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, kind, note, weight FROM lessons WHERE user_id=? "
            "ORDER BY id DESC LIMIT 30", (str(user_id),),
        ).fetchall()
        merged_id: int | None = None
        merged_weight = float(weight or 0.5)
        for r in rows:
            if str(r["kind"]) == str(kind):
                merged_id = int(r["id"])
                merged_weight = min(1.0, max(float(r["weight"] or 0.5), merged_weight) + 0.05)
                break
        if merged_id is None:
            for r in rows:
                if _note_similar(str(r["note"] or ""), note) >= 0.8:
                    merged_id = int(r["id"])
                    merged_weight = min(1.0, max(float(r["weight"] or 0.5), merged_weight) + 0.05)
                    break
        if merged_id is not None:
            conn.execute(
                "UPDATE lessons SET request_id=?, kind=?, note=?, verdict=?, source=?, "
                "why=?, how=?, confidence=?, degrade_reason=?, weight=?, uses=uses+1, "
                "created_at=? WHERE id=?",
                (str(request_id), str(kind), note, str(verdict), str(source or "rule"),
                 str(why or ""), str(how or ""), float(confidence or 0.0),
                 str(degrade_reason or ""), float(merged_weight), _now(), merged_id),
            )
            conn.commit()
            return merged_id, True
        cur = conn.execute(
            "INSERT INTO lessons(created_at,user_id,request_id,kind,note,verdict,"
            "source,why,how,confidence,degrade_reason,weight,uses) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (_now(), str(user_id), str(request_id), str(kind), note, str(verdict),
             str(source or "rule"), str(why or ""), str(how or ""),
             float(confidence or 0.0), str(degrade_reason or ""), float(weight or 0.5)),
        )
        conn.execute(
            "DELETE FROM lessons WHERE id <= (SELECT id FROM lessons ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (max(1, int(keep)),),
        )
        conn.commit()
        return int(cur.lastrowid or 0), False
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


def lessons_for_prompt(user_id: str, limit: int = 3) -> list[dict]:
    """进提示词的那几条经验：按权重，不按时间。

    以前是「最近三条」——最新的一条哪怕只出现过一次，也会压过反复验证过的老经验。
    现在 weight 高（反复加固 + 反馈认可）的先上，同权重再按新。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM lessons WHERE user_id=? ORDER BY weight DESC, id DESC LIMIT ?",
            (str(user_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def bump_lesson_outcome(*, user_id: str, request_id: str, ok: bool,
                        delta_good: float = 0.06, delta_bad: float = 0.12) -> dict | None:
    """用户对某一轮的表态，落回到那一轮学到的经验上。

    说「对了」→ 这条经验被验证（weight 微涨）；说「不对」→ 这条经验可能带偏了
    （weight 明显下调）。反复不行的那条会被降到底，自然退出提示词前排。
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM lessons WHERE user_id=? AND request_id=? "
            "ORDER BY id DESC LIMIT 1", (str(user_id), str(request_id)),
        ).fetchone()
        if row is None:
            return None
        w = float(row["weight"] or 0.5)
        w = min(1.0, w + delta_good) if ok else max(0.05, w - delta_bad)
        succ = int(row["success"] or 0) + (1 if ok else 0)
        fail = int(row["failure"] or 0) + (0 if ok else 1)
        conn.execute(
            "UPDATE lessons SET weight=?, success=?, failure=? WHERE id=?",
            (round(w, 3), succ, fail, int(row["id"])),
        )
        conn.commit()
        return dict(row, weight=round(w, 3), success=succ, failure=fail)
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


def turn_meta(request_id: str) -> dict:
    """按 request_id 取这一轮的真实记录（反馈落权重时要用 user_id 和召回证据）。"""
    req = str(request_id or "").strip()
    if not req:
        return {}
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM turn_logs WHERE request_id=? ORDER BY id DESC LIMIT 1", (req,),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def adjust_fact_importances(fact_ids: list[str], delta: float) -> int:
    """反馈落到记忆权重上：说对了，这几条记忆更可信；说不对，先降一点。"""
    ids = [str(x) for x in (fact_ids or []) if str(x).strip()]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    conn = connect()
    try:
        cur = conn.execute(
            f"UPDATE facts SET importance=MAX(0.05, MIN(1.0, importance+?)) "
            f"WHERE id IN ({marks})", tuple([float(delta)] + ids),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def activated_facts(request_id: str) -> list[str]:
    """这一轮点亮了哪些事实节点（从 activations 反查，用于反馈回溯）。"""
    req = str(request_id or "").strip()
    if not req:
        return []
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT neuron_id FROM activations WHERE request_id=?", (req,),
        ).fetchall()
        out = []
        for r in rows:
            nid = str(r["neuron_id"] or "")
            if nid.startswith("n_fact_"):
                out.append(nid[len("n_fact_"):])
        return out
    finally:
        conn.close()


# ---------- 神经元 / 突触 / 激活（V2 神经网络） ----------
# 这一层的规矩：脑图上的每一次亮起，都要能查到是哪一颗、为什么亮。
# 只有 facts 表的时候，一条事实就是一行文本；有了 neurons，它才是一个「节点」。

def neuron_upsert(*, neuron_id: str, user_id: str, kind: str, ref_id: str = "",
                  label: str = "", text: str = "", importance: float = 0.5,
                  confidence: float = 0.5) -> str:
    """建一颗神经元；已存在时只涨不跌（importance/confidence 取大者）。

    为什么取大者：同一条事实可能被再次提到，再提到应该让它更重要，
    而不是把之前的判断抹平；activation 与计数一个都不重置。
    """
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO neurons(id,user_id,kind,ref_id,label,text,importance,confidence,"
            "activation,activation_count,created_at) VALUES(?,?,?,?,?,?,?,?,0,0,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "label=excluded.label, text=excluded.text, ref_id=excluded.ref_id, "
            "importance=MAX(neurons.importance, excluded.importance), "
            "confidence=MAX(neurons.confidence, excluded.confidence)",
            (str(neuron_id), str(user_id), str(kind), str(ref_id or ""),
             str(label or "")[:60], str(text or "")[:400],
             float(importance), float(confidence), _now()),
        )
        conn.commit()
        return str(neuron_id)
    finally:
        conn.close()


def neuron_get(neuron_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM neurons WHERE id=?", (str(neuron_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def neuron_labels(neuron_ids: list[str]) -> dict[str, dict]:
    """一次取回一批节点的 label/kind（激活扩散时别为每颗开一次连接）。"""
    ids = [str(x) for x in (neuron_ids or []) if str(x).strip()]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT id,label,kind,importance,activation FROM neurons "
            f"WHERE id IN ({marks})", tuple(ids),
        ).fetchall()
        return {str(r["id"]): dict(r) for r in rows}
    finally:
        conn.close()


def list_neurons(user_id: str | None = None, kind: str | None = None,
                 limit: int = 5000) -> list[dict]:
    sql = "SELECT * FROM neurons WHERE 1=1"
    args: list[Any] = []
    if user_id:
        sql += " AND user_id=?"
        args.append(str(user_id))
    if kind:
        sql += " AND kind=?"
        args.append(str(kind))
    sql += " ORDER BY importance DESC, id DESC LIMIT ?"
    args.append(int(limit))
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def neuron_stats(user_id: str | None = None) -> dict:
    sql = "SELECT kind, COUNT(*) AS n FROM neurons"
    args: list[Any] = []
    if user_id:
        sql += " WHERE user_id=?"
        args.append(str(user_id))
    sql += " GROUP BY kind"
    conn = connect()
    try:
        rows = conn.execute(sql, args).fetchall()
        syn = conn.execute("SELECT COUNT(*) FROM synapses" +
                           (" WHERE user_id=?" if user_id else ""),
                           ([str(user_id)] if user_id else [])).fetchone()
        return {"by_kind": {str(r["kind"]): int(r["n"]) for r in rows},
                "neurons": sum(int(r["n"]) for r in rows),
                "synapses": int(syn[0] or 0)}
    finally:
        conn.close()


def synapse_upsert(*, user_id: str, src: str, dst: str, relation: str,
                   weight: float = 0.3, delta: float = 0.03) -> None:
    """建一条突触；重复建就按 delta 强化（权重只涨不跌，封顶 1.0）。

    V2.5 修正：默认 delta 从 0 改为 0.03 —— 之前重复建边不强化，"用进"失效。
    """
    if not src or not dst or src == dst:
        return
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO synapses(user_id,src,dst,relation,weight,activation_count,created_at) "
            "VALUES(?,?,?,?,?,0,?) "
            "ON CONFLICT(user_id,src,dst,relation) DO UPDATE SET "
            "weight=MIN(1.0, MAX(synapses.weight, excluded.weight) + ?)",
            (str(user_id), str(src), str(dst), str(relation),
             max(0.0, min(1.0, float(weight))), _now(), max(0.0, float(delta))),
        )
        conn.commit()
    finally:
        conn.close()


def list_synapses(user_id: str | None = None, limit: int = 4000,
                  min_weight: float = 0.0) -> list[dict]:
    """整张图的突触（前端画真实关系用；按权重从高到低取，超量只截尾）。"""
    sql = "SELECT src,dst,relation,weight,activation_count FROM synapses WHERE 1=1"
    args: list[Any] = []
    if user_id:
        sql += " AND user_id=?"
        args.append(str(user_id))
    if min_weight > 0:
        sql += " AND weight>=?"
        args.append(float(min_weight))
    sql += " ORDER BY weight DESC, activation_count DESC LIMIT ?"
    args.append(int(limit))
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def synapse_neighbors(user_id: str, neuron_ids: list[str],
                      limit: int = 12) -> list[dict]:
    """从一组节点出发，取权重最高的相邻突触（激活扩散只走这条）。"""
    ids = [str(x) for x in (neuron_ids or []) if str(x).strip()]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT src,dst,relation,weight FROM synapses "
            f"WHERE user_id=? AND src IN ({marks}) ORDER BY weight DESC LIMIT ?",
            tuple([str(user_id)] + ids + [int(limit)]),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def synapse_sources(user_id: str, dst_ids: list[str], relation: str | None = None,
                    limit: int = 20) -> list[dict]:
    """反向查询：谁连到我（概念节点找回「哪些记忆是关于它的」）。"""
    ids = [str(x) for x in (dst_ids or []) if str(x).strip()]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    sql = f"SELECT src,dst,relation,weight FROM synapses WHERE user_id=? AND dst IN ({marks})"
    args: list[Any] = [str(user_id)] + ids
    if relation:
        sql += " AND relation=?"
        args.append(str(relation))
    sql += " ORDER BY weight DESC LIMIT ?"
    args.append(int(limit))
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
    finally:
        conn.close()


def find_facts_like(user_id: str, term: str, limit: int = 8) -> list[dict]:
    """按字面包含查事实（概念成形的依据：同一个词出现过的所有记忆）。"""
    if not str(term or "").strip():
        return []
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id,text,importance FROM facts WHERE user_id=? AND text LIKE ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (str(user_id), "%" + str(term) + "%", int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_facts_like(user_id: str, term: str) -> int:
    if not str(term or "").strip():
        return 0
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE user_id=? AND text LIKE ?",
            (str(user_id), "%" + str(term) + "%"),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def list_fact_users(limit: int = 20) -> list[str]:
    """有过长期记忆的用户（启动时给历史事实补神经节点用）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT user_id, COUNT(*) AS n FROM facts WHERE user_id<>'' "
            "GROUP BY user_id ORDER BY n DESC LIMIT ?", (int(limit),),
        ).fetchall()
        return [str(r[0]) for r in rows if r[0]]
    finally:
        conn.close()


def bump_synapses(user_id: str, pairs: list[tuple[str, str]], relation: str,
                  delta: float = 0.03) -> None:
    """共同激活的一对节点：连接变强一点。这就是「用进」。"""
    pairs = [(str(a), str(b)) for a, b in (pairs or []) if str(a) and str(b) and a != b]
    if not pairs:
        return
    conn = connect()
    try:
        conn.executemany(
            "UPDATE synapses SET weight=MIN(1.0, weight+?), "
            "activation_count=activation_count+1, last_activated=? "
            "WHERE user_id=? AND src=? AND dst=? AND relation=?",
            [(float(delta), _now(), str(user_id), a, b, str(relation))
             for a, b in pairs],
        )
        conn.commit()
    finally:
        conn.close()


def record_activations(*, user_id: str, request_id: str, items: list[dict]) -> None:
    """把这一轮点亮的节点记账（items: [{id, energy}]），顺带涨 activation。"""
    items = [(str(it.get("id")), float(it.get("energy") or 0.0))
             for it in (items or []) if str(it.get("id") or "").strip()]
    if not items:
        return
    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO activations(created_at,request_id,user_id,neuron_id,energy) "
            "VALUES(?,?,?,?,?)",
            [(_now(), str(request_id), str(user_id), nid, energy)
             for nid, energy in items],
        )
        conn.executemany(
            "UPDATE neurons SET activation=MIN(1.0, activation+?), "
            "activation_count=activation_count+1, last_activated=? WHERE id=?",
            [(min(0.5, max(0.0, energy)), _now(), nid) for nid, energy in items],
        )
        conn.commit()
    finally:
        conn.close()


def touch_facts(fact_ids: list[str]) -> None:
    """本轮被想起的事实：涨一点激活度（读侧只读这个值，不写热路径）。"""
    ids = [str(x) for x in (fact_ids or []) if str(x).strip()]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    conn = connect()
    try:
        conn.execute(
            f"UPDATE facts SET activation=MIN(1.0, activation+0.12), "
            f"activation_count=activation_count+1, last_activated=? WHERE id IN ({marks})",
            tuple([_now()] + ids),
        )
        conn.commit()
    finally:
        conn.close()


def decay(user_id: str, neuron_half_life_days: float = 7.0,
          synapse_half_life_days: float = 30.0, floor: float = 0.05) -> dict:
    """衰减：长期没被激活的节点与连接慢慢变淡，但绝不被删除。

    只降「激活度 / 连接权重」，不降 importance —— 重要事实仍然重要，
    只是这一阵子不容易被想起来。这就是衰减与遗忘的区别。
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, activation, last_activated FROM neurons "
            "WHERE user_id=? AND activation > 0.01", (str(user_id),),
        ).fetchall()
        now = datetime.datetime.now()
        upd = []
        for r in rows:
            try:
                last = datetime.datetime.strptime(str(r["last_activated"]), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            days = max(0.0, (now - last).total_seconds() / 86400.0)
            val = float(r["activation"] or 0.0) * (0.5 ** (days / max(0.1, neuron_half_life_days)))
            upd.append((round(val, 4), str(r["id"])))
        if upd:
            conn.executemany("UPDATE neurons SET activation=? WHERE id=?", upd)
        srows = conn.execute(
            "SELECT id, weight, last_activated FROM synapses WHERE user_id=? AND weight > ?",
            (str(user_id), float(floor)),
        ).fetchall()
        supd = []
        for r in srows:
            try:
                last = datetime.datetime.strptime(str(r["last_activated"] or ""), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                last = now - datetime.timedelta(days=synapse_half_life_days * 4)
            days = max(0.0, (now - last).total_seconds() / 86400.0)
            val = float(r["weight"] or 0.0) * (0.5 ** (days / max(0.1, synapse_half_life_days)))
            supd.append((round(max(float(floor), val), 4), int(r["id"])))
        if supd:
            conn.executemany("UPDATE synapses SET weight=? WHERE id=?", supd)
        conn.commit()
        return {"neurons": len(upd), "synapses": len(supd)}
    finally:
        conn.close()


# ---------- 策略（V2 自我进化：候选 → 启用 → 回滚） ----------

def upsert_strategy(*, user_id: str, name: str, text: str) -> dict:
    """同名策略没有就建候选，有就累积证据；证据够了才谈启用。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM strategies WHERE user_id=? AND name=? AND status!='retired' "
            "ORDER BY version DESC LIMIT 1", (str(user_id), str(name)),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO strategies(user_id,name,text,evidence,status,version,"
                "created_at,updated_at) VALUES(?,?,?,1,'candidate',1,?,?)",
                (str(user_id), str(name), str(text), _now(), _now()),
            )
            conn.commit()
            return {"id": int(cur.lastrowid or 0), "name": str(name), "text": str(text),
                    "evidence": 1, "status": "candidate", "version": 1}
        conn.execute(
            "UPDATE strategies SET evidence=evidence+1, text=?, updated_at=? WHERE id=?",
            (str(text), _now(), int(row["id"])),
        )
        conn.commit()
        return dict(row, evidence=int(row["evidence"]) + 1, text=str(text))
    finally:
        conn.close()


def record_strategy_outcome(*, user_id: str, name: str, ok: bool) -> dict | None:
    """策略有没有用：成功一次 +1，失败一次 +1；失败多过成功就退役。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM strategies WHERE user_id=? AND name=? AND status!='retired' "
            "ORDER BY version DESC LIMIT 1", (str(user_id), str(name)),
        ).fetchone()
        if row is None:
            return None
        succ = int(row["success"] or 0) + (1 if ok else 0)
        fail = int(row["failure"] or 0) + (0 if ok else 1)
        status = str(row["status"] or "candidate")
        if status == "active" and fail >= 3 and fail > succ:
            status = "retired"
        conn.execute(
            "UPDATE strategies SET success=?, failure=?, status=?, updated_at=? WHERE id=?",
            (succ, fail, status, _now(), int(row["id"])),
        )
        conn.commit()
        return dict(row, success=succ, failure=fail, status=status)
    finally:
        conn.close()


def activate_strategy(*, user_id: str, name: str, version: int | None = None,
                      min_evidence: int = 2) -> dict | None:
    """启用一条候选策略：同用户只留一条 active，旧的回滚为 retired（可再启用）。"""
    conn = connect()
    try:
        sql = ("SELECT * FROM strategies WHERE user_id=? AND name=? "
               + ("AND version=? " if version else "AND status!='retired' ")
               + "ORDER BY version DESC LIMIT 1")
        args = [str(user_id), str(name)] + ([int(version)] if version else [])
        row = conn.execute(sql, tuple(args)).fetchone()
        if row is None or int(row["evidence"] or 0) < int(min_evidence):
            return None
        conn.execute("UPDATE strategies SET status='retired', updated_at=? "
                     "WHERE user_id=? AND status='active'", (_now(), str(user_id)))
        conn.execute("UPDATE strategies SET status='active', updated_at=? WHERE id=?",
                     (_now(), int(row["id"])))
        conn.commit()
        return dict(row, status="active")
    finally:
        conn.close()


def rollback_strategy(*, user_id: str, name: str) -> dict | None:
    """回滚：把当前 active 的退役，再把上一版重新启用（没有上一版就退回无策略）。"""
    conn = connect()
    try:
        cur = conn.execute(
            "SELECT * FROM strategies WHERE user_id=? AND name=? AND status='active' "
            "ORDER BY version DESC LIMIT 1", (str(user_id), str(name)),
        ).fetchone()
        if cur is None:
            return None
        conn.execute("UPDATE strategies SET status='retired', updated_at=? WHERE id=?",
                     (_now(), int(cur["id"])))
        prev = conn.execute(
            "SELECT * FROM strategies WHERE user_id=? AND name=? AND status='retired' "
            "AND version < ? ORDER BY version DESC LIMIT 1",
            (str(user_id), str(name), int(cur["version"])),
        ).fetchone()
        if prev is not None:
            conn.execute("UPDATE strategies SET status='active', updated_at=? WHERE id=?",
                         (_now(), int(prev["id"])))
        conn.commit()
        return {"rolled_back": int(cur["id"]),
                "active": (dict(prev, status="active") if prev is not None else None)}
    finally:
        conn.close()


def list_strategies(user_id: str, limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM strategies WHERE user_id=? ORDER BY status='active' DESC, "
            "version DESC LIMIT ?", (str(user_id), int(limit))).fetchall()]
    finally:
        conn.close()


def active_strategy(user_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM strategies WHERE user_id=? AND status='active' "
            "ORDER BY version DESC LIMIT 1", (str(user_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------- 知识来源与信念（块④）----------
# 每个写 DAO 都是「单次 execute + 单次 commit」（E04）；
# §2.4 标注的「注册来源+写 belief」「新 belief+supersede」多写同事务，
# 由上层模块（T28 的 consolidation / verifier）在同一 conn 上组合完成。
_SRC_KINDS = {"user_said", "local_doc", "remote_kb", "web", "unknown"}


def add_knowledge_source(*, user_id: str, uri: str, kind: str,
                         trust: float = 0.5) -> int | None:
    if kind not in _SRC_KINDS:
        return None
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO knowledge_sources(user_id,uri,kind,trust,fetched_at) VALUES(?,?,?,?,?)",
            (str(user_id), str(uri or ""), str(kind),
             max(0.0, min(1.0, float(trust))), _now()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_knowledge_sources(user_id: str, kind: str | None = None,
                           limit: int = 50) -> list[dict]:
    """查某用户的知识来源（E09：必带 user_id）。"""
    sql = "SELECT * FROM knowledge_sources WHERE user_id=?"
    args: list[Any] = [str(user_id)]
    if kind:
        sql += " AND kind=?"
        args.append(str(kind))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def add_belief(*, user_id: str, subject: str, predicate: str, object: str,
               confidence: float = 0.5, source_id: int | None = None) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO beliefs(user_id,subject,predicate,object,confidence,source_id,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(user_id), str(subject), str(predicate), str(object),
             max(0.0, min(1.0, float(confidence))), source_id, _now()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def supersede_belief(*, user_id: str, old_id: int, new_id: int) -> bool:
    """新证据不删旧信念，写 superseded_by 指向替代者（同一事务只更新）。"""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE beliefs SET superseded_by=? WHERE id=? AND user_id=?",
            (int(new_id), int(old_id), str(user_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_beliefs(user_id: str, subject: str | None = None, limit: int = 50) -> list[dict]:
    """查某用户的信念（E09：必带 user_id）。"""
    sql = "SELECT * FROM beliefs WHERE user_id=?"
    args: list[Any] = [str(user_id)]
    if subject:
        sql += " AND subject=?"
        args.append(str(subject))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ---------- 评估结果（块⑥ / 阶段 4，T38）----------
_EVAL_DECISIONS = {"keep", "rollback"}


def add_evaluation_result(*, user_id: str, strategy: str, window: str,
                          before_score: float, after_score: float,
                          decision: str) -> int:
    """写一条策略评估结果（E09：必带 user_id）。非法 decision 归一化为 keep。"""
    if decision not in _EVAL_DECISIONS:
        decision = "keep"
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO evaluation_results(user_id,strategy,window,before_score,after_score,decision,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(user_id), str(strategy), str(window), float(before_score),
             float(after_score), decision, _now()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_evaluation_results(user_id: str, limit: int = 20) -> list[dict]:
    """查某用户的评估结果（E09：必带 user_id）。"""
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM evaluation_results WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (str(user_id), int(limit))).fetchall()]
    finally:
        conn.close()


def apply_evaluation(*, user_id: str, strategy: str, window: str,
                     before_score: float, after_score: float,
                     decision: str) -> dict:
    """单事务：写评估结果 + 按 decision 改策略状态，只 commit 一次（E04 硬约束）。

    - decision='rollback'：等价于 rollback_strategy（当前 active 退役 + 上一版复活）。
    - 其余（keep）：等价于 record_strategy_outcome(ok=True)（success +1，必要时退役）。

    两步在同一 conn 上 execute，任一异常 → conn.rollback() 整批回滚，
    不留孤儿 evaluation 行、策略状态不变。T39 必须只调本函数，不得自行拆两次写。
    """
    if decision not in _EVAL_DECISIONS:
        decision = "keep"
    conn = connect()
    try:
        # 第 1 步：写评估结果（与 add_evaluation_result 同构）
        conn.execute(
            "INSERT INTO evaluation_results(user_id,strategy,window,before_score,after_score,decision,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(user_id), str(strategy), str(window), float(before_score),
             float(after_score), decision, _now()))
        now = _now()

        # 第 2 步：按 decision 改策略状态（等价 rollback_strategy / record_strategy_outcome）
        if decision == "rollback":
            cur = conn.execute(
                "SELECT * FROM strategies WHERE user_id=? AND name=? AND status='active' "
                "ORDER BY version DESC LIMIT 1", (str(user_id), str(strategy)),
            ).fetchone()
            if cur is not None:
                conn.execute(
                    "UPDATE strategies SET status='retired', updated_at=? WHERE id=?",
                    (now, int(cur["id"])))
                prev = conn.execute(
                    "SELECT * FROM strategies WHERE user_id=? AND name=? AND status='retired' "
                    "AND version < ? ORDER BY version DESC LIMIT 1",
                    (str(user_id), str(strategy), int(cur["version"])),
                ).fetchone()
                if prev is not None:
                    conn.execute(
                        "UPDATE strategies SET status='active', updated_at=? WHERE id=?",
                        (now, int(prev["id"])))
        else:
            row = conn.execute(
                "SELECT * FROM strategies WHERE user_id=? AND name=? AND status!='retired' "
                "ORDER BY version DESC LIMIT 1", (str(user_id), str(strategy)),
            ).fetchone()
            if row is not None:
                succ = int(row["success"] or 0) + 1
                fail = int(row["failure"] or 0)
                status = str(row["status"] or "candidate")
                if status == "active" and fail >= 3 and fail > succ:
                    status = "retired"
                conn.execute(
                    "UPDATE strategies SET success=?, failure=?, status=?, updated_at=? WHERE id=?",
                    (succ, fail, status, now, int(row["id"])))

        # 只 commit 一次
        conn.commit()
        return {"strategy": str(strategy), "decision": decision,
                "before": float(before_score), "after": float(after_score)}
    except Exception:
        conn.rollback()  # 整批回滚：无孤儿结果行、策略状态不变
        raise
    finally:
        conn.close()
