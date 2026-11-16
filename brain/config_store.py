"""配置中心存储层 —— 三层分离中的「连接配置」。

原则（用户 2026-11-13 明确）：
  模型是模型，控制大脑是控制大脑，前端是前端，这是两回事。
  模型 / 知识库 / TTS 都是**独立部署的服务**，本模块只保存
  「连到哪里、用什么参数、开不开」，绝不启动、加载或管理任何模型进程。
  实时链路每轮请求读一次（请求级快照），控制台改完立即生效，不需要重启。

所有表与 data/companion.db 同库，两个进程共享；复用 db.connect() 的 WAL 连接。
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import time
from typing import Any

from . import db
from .schemas import EMOTION_LABELS

PROVIDER_CODES: tuple[str, ...] = ("llm", "llm_alt", "embedding", "judge", "asr", "tts", "knowledge")

PROVIDER_NAMES: dict[str, str] = {
    "llm": "对话大模型",
    "llm_alt": "本地 4B 模型（备用）",
    "embedding": "文本向量",
    "judge": "情感判断模型",
    "asr": "语音识别",
    "tts": "语音合成",
    "knowledge": "知识库 / 长期记忆",
}

# 本机 WeKnora 实拉结果（2026-11-13 list_knowledge_bases），仅作控制台下拉候选；
# KB id 会随实例变动，实际以「拉取知识库列表」按钮的结果为准。
KNOWN_BASES: list[dict[str, str]] = [
    {"id": "3f63b758-026c-4467-b2db-f69f3ad249db", "name": "思维大脑"},
    {"id": "1ef89e17-53ec-4f34-947f-90af42024d4c", "name": "小说写作技巧"},
    {"id": "9c29adc9-7a63-42f9-9fb5-f3392c2fedd6", "name": "公众号写作技巧"},
    {"id": "304642b0-8555-47aa-a741-8bba330981ca", "name": "pcdn爱好者"},
]


def F(key: str, label: str, type_: str = "text", default: Any = "", desc: str = "",
      options: list[str] | None = None, secret: bool = False,
      minimum: float | None = None, maximum: float | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": key, "label": label, "type": type_, "default": default, "desc": desc,
    }
    if options is not None:
        item["options"] = options
    if secret:
        item["secret"] = True
    if minimum is not None:
        item["min"] = minimum
    if maximum is not None:
        item["max"] = maximum
    return item


# ---- 字段元数据：控制台据此动态渲染表单，不硬编码字段 ----
PROVIDER_FIELDS: dict[str, list[dict[str, Any]]] = {
    "llm": [
        F("api_style", "接口形态", "select", "openai", "「云端接口」= 任何 OpenAI 兼容服务；「本机 Ollama」= 本机跑的 Ollama", ["openai", "ollama"]),
        F("base_url", "服务地址", "text", "http://127.0.0.1:11434", "模型服务地址（本机或局域网）"),
        F("api_key", "密钥", "text", "", "本地服务可留空", secret=True),
        F("model", "主模型", "text", "qwen3:8b", "当前使用的模型名"),
        F("fallback_model", "备用模型", "text", "qwen3:4b", "OOM / 超时时降级用"),
        F("timeout_s", "超时(秒)", "number", 60, "单次生成超时", minimum=1, maximum=600),
        F("num_gpu", "显卡层数", "number", 999, "999=全部放进显卡，不允许偷偷退回 CPU", minimum=0, maximum=999),
        F("num_ctx", "上下文长度", "number", 4096, "能记住多少前文，越长越吃显存", minimum=512, maximum=32768),
        F("temperature", "温度", "number", 0.7, "生成随机性", minimum=0, maximum=2),
        F("top_p", "采样范围", "number", 0.9, "越小越保守，一般 0.9", minimum=0, maximum=1),
        F("max_tokens", "最大生成", "number", 512, "", minimum=32, maximum=4096),
        F("stream", "流式输出", "bool", True, "关闭则一次性返回"),
        F("force_gpu", "强制全部放显卡", "bool", True, "开=显存放不下就直接报错，不偷偷退回 CPU"),
    ],
    "embedding": [
        F("base_url", "服务地址", "text", "http://127.0.0.1:11434", "向量服务地址，通常和对话模型同一个"),
        F("api_key", "密钥", "text", "", "", secret=True),
        F("model", "模型", "text", "bge-m3", "中文语义检索"),
        F("dimension", "向量维度", "number", 1024, "要和向量库索引对上", minimum=64, maximum=4096),
        F("force_cpu", "只跑 CPU", "bool", True, "把显存留给对话模型和语音"),
        F("timeout_s", "超时(秒)", "number", 10, "", minimum=1, maximum=120),
        F("normalize", "归一化", "bool", True, ""),
    ],
    "judge": [
        F("engine", "判断引擎", "select", "http",
          "「独立服务」= 调 4 号 bat 起的判断服务（推荐）；「跟随独立服务」= 同上，兼容旧配置；"
          "「进程内加载」= 直接在大脑里跑，会多吃 1~2G 内存；「只用关键词」= 不加载模型",
          ["http", "auto", "onnx", "rule"]),
        F("endpoint", "判断服务地址", "text", "",
          "留空就用 config.yaml 里配的判断服务地址；也可以直接填完整网址"),
        F("model_path", "本机模型路径", "text", "models/chinese-emotion-small/model.onnx",
          "只有选「进程内加载」才用；同目录还要有分词器文件"),
        F("context_turns", "上下文轮数", "number", 2, "判断时带最近几轮", minimum=0, maximum=8),
        F("confidence_as_intensity", "置信度代替强度", "bool", True, "这个模型只给情绪类别，不给强度数值"),
        F("confidence_floor", "低置信度回退", "number", 0.65,
          "模型置信度低于此值时让关键词复核一次", minimum=0, maximum=1),
        F("intensity_threshold", "记忆阈值", "number", 0.6, "超过才进入长期情绪记忆", minimum=0, maximum=1),
        F("label_map", "标签映射（高级）", "textarea", "{}", "模型输出对应到哪 8 类情绪"),
        F("timeout_s", "超时(秒)", "number", 5, "", minimum=1, maximum=60),
    ],
    "asr": [
        F("api_style", "接口形态", "select", "custom",
          "「自定义地址」= 我们自己的识别服务（7 号 bat 起的，填服务地址就行）；"
          "「云端接口」= OpenAI 风格的 /audio/transcriptions 接口", ["openai", "custom"]),
        F("base_url", "服务地址", "text", "http://127.0.0.1:8103",
          "7-启动语音识别.bat 起的识别服务（sherpa-onnx + SenseVoice，纯 CPU）"),
        F("endpoint", "接口路径", "text", "/asr", "填完整 URL 则忽略服务地址"),
        F("api_key", "密钥", "text", "", "", secret=True),
        F("model", "模型", "text", "sensevoice-small", "多语种，229MB int8，不占显存"),
        F("language", "语言", "select", "auto", "auto = 自动判断 中/英/日/韩/粤",
          ["auto", "zh", "en", "yue"]),
        F("max_seconds", "最长录音(秒)", "number", 60, "超过直接拒，防止传错文件",
          minimum=1, maximum=300),
        F("timeout_s", "超时(秒)", "number", 30, "", minimum=1, maximum=300),
        F("with_emotion", "返回声学情绪", "bool", True, "SenseVoice 顺带输出的语气标签"),
    ],
    "tts": [
        F("api_style", "接口形态", "select", "custom",
          "「自定义地址」= 我们自己的语音服务（5 号 bat / GPT-SoVITS 适配层，填服务地址就行）；"
          "「云端接口」= OpenAI 风格的 /audio/speech 接口", ["openai", "custom"]),
        F("base_url", "服务地址", "text", "http://127.0.0.1:8102",
          "5-启动语音合成.bat 起的语音服务；也可指向已有的 GPT-SoVITS 适配层"),
        F("endpoint", "接口路径", "text", "/tts", "填完整 URL 则忽略服务地址"),
        F("api_key", "密钥", "text", "", "", secret=True),
        F("model", "模型 / 版本", "text", "IndexTTS-2", ""),
        F("timeout_s", "超时(秒)", "number", 60, "", minimum=1, maximum=300),
        F("format", "输出格式", "select", "wav", "", ["wav", "mp3", "opus", "pcm"]),
        F("sample_rate", "采样率", "number", 24000, "", minimum=8000, maximum=48000),
        F("default_voice", "默认音色", "text", "warm_female", "无情绪映射时的兜底"),
        F("speed", "语速", "number", 1.0, "", minimum=0.5, maximum=2.0),
        F("volume", "音量", "number", 1.0, "", minimum=0, maximum=2.0),
        F("use_reference_audio", "用参考音频", "bool", True, "按情绪切换预置参考音频"),
        F("use_emotion_vector", "用情感向量", "bool", True, "8 维情感向量控制"),
        F("emotion_control", "情感控制方式", "select", "reference_audio", "用文字描述语气在 8G 显存下跑不动",
          ["reference_audio", "emotion_vector", "none"]),
        F("reference_audio_dir", "参考音频目录", "text", "models/voices", ""),
        F("disable_qwen_emo", "关闭文本情感模型", "bool", True, "省约 1.2GB 显存"),
    ],
    "knowledge": [
        F("mode", "接入方式", "select", "off", "「知识库直连」= 连现成的知识库服务（推荐）；「回调接口」= 填一个你自己的网址；「关闭」= 不用知识库",
          ["mcp_http", "webhook", "off"]),
        F("mcp_endpoint", "知识库服务地址", "text", "", "MCP 服务的地址"),
        F("mcp_token", "访问密钥", "text", "", "", secret=True),
        F("tool_list_bases", "列库工具名", "text", "list_knowledge_bases", ""),
        F("tool_search", "检索工具名", "text", "search_knowledge", ""),
        F("tool_remember", "写入工具名", "text", "add_document", ""),
        F("webhook_url", "回调地址", "text", "", "会向这个地址发查询，拿回召回的片段"),
        F("kb_ids", "已选知识库", "textarea",
          "3f63b758-026c-4467-b2db-f69f3ad249db", "多个用逗号分隔；用下面「拉取知识库列表」勾选更省事"),
        F("known_bases", "知识库清单（高级）", "textarea", json.dumps(KNOWN_BASES, ensure_ascii=False),
          "由「拉取知识库列表」按钮刷新"),
        F("retrieval_mode", "检索模式", "select", "hybrid", "", ["hybrid", "semantic", "keyword"]),
        F("top_k", "召回条数", "number", 3, "", minimum=1, maximum=20),
        F("min_score", "最低分数", "number", 0.0, "", minimum=0, maximum=1),
        F("inject_prompt", "注入系统提示", "bool", True, "把召回内容当成「长期记忆」一段塞进去"),
        F("section_title", "段落标题", "text", "长期记忆 / 知识片段", "注入时这一段叫什么标题"),
        F("auto_remember", "自动沉淀", "bool", False, "把对话结论写回知识库"),
        F("remember_on", "沉淀时机", "select", "manual", "", ["manual", "every", "high_intensity"]),
        F("remember_threshold", "沉淀强度阈值", "number", 0.6, "", minimum=0, maximum=1),
        F("tag_prefix", "写入标签前缀", "text", "【情感陪伴】", ""),
        F("target_kb_id", "写入目标库", "text",
          "3f63b758-026c-4467-b2db-f69f3ad249db", "沉淀到哪个库"),
        F("special_documents", "指定文档", "textarea", "", "每次都会带上的文档编号，逗号分隔"),
        F("timeout_s", "超时(秒)", "number", 8, "", minimum=1, maximum=60),
    ],
}

PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    code: {f["key"]: f["default"] for f in fields}
    for code, fields in PROVIDER_FIELDS.items()
}

# 本地 4B 备用模型：字段与主模型完全一致，只换默认值。
# 它是「第二张模型卡」，默认运行方式=已关闭；在控制台把它打开就接管对话，
# 关掉就回到主模型，不需要改代码、不需要重启。
_LLM_ALT_OVERRIDES = {
    "model": "qwen3:4b",
    "fallback_model": "",
    "num_ctx": 8192,   # 4B 权重小，显存能撑更长的上下文，记忆注入更从容
}
PROVIDER_FIELDS["llm_alt"] = [
    {**f, "default": _LLM_ALT_OVERRIDES.get(f["key"], f["default"])}
    for f in PROVIDER_FIELDS["llm"]
]
PROVIDER_DEFAULTS["llm_alt"] = {f["key"]: f["default"] for f in PROVIDER_FIELDS["llm_alt"]}

PROVIDER_MODES: dict[str, str] = {
    "llm": "http", "llm_alt": "off", "embedding": "off", "judge": "rule",
    "asr": "http", "tts": "inproc", "knowledge": "off",
}

# 每个 provider 允许的「运行方式」：控制台下拉框只列说得通的选项。
# 语音识别不可能「知识库直连」，知识库不需要「进程内模型」——按服务各给各的。
PROVIDER_MODE_OPTIONS: dict[str, list[str]] = {
    "llm":       ["http", "off"],
    "llm_alt":   ["http", "off"],
    "embedding": ["http", "off"],
    "judge":     ["http", "onnx", "rule", "off"],
    "asr":       ["http", "off"],
    "tts":       ["inproc", "http", "off"],
    "knowledge": ["mcp_http", "webhook", "off"],
}

# 运行时参数 / 全局开关：单一数据源，admin.py 直接引用，不再各自硬编码
RUNTIME_RULES: dict[str, dict[str, Any]] = {
    # temperature / top_p / num_ctx / max_tokens 不在这里出现：
    # 它们是「模型服务」provider 的字段（llm.temperature 等），全局只存一份，
    # 避免运行时默认值把模型卡里改的值永久遮蔽（历史上真踩过）。
    "memory_top_k":          {"min": 1, "max": 20, "type": "int", "desc": "事实召回条数"},
    "emotion_min_intensity": {"min": 0, "max": 1, "type": "float", "desc": "进入长期情绪记忆的阈值"},
    "gpu_exclusive":         {"min": 0, "max": 1, "type": "int", "desc": "1=对话模型和语音轮流用显卡"},
    "maintenance_mode":      {"min": 0, "max": 1, "type": "int", "desc": "1=维护模式，只回一句提示，不走大模型"},
    "log_prompt":            {"min": 0, "max": 1, "type": "int", "desc": "1=把完整的提示词原文也存下来"},
    "log_input_text":        {"min": 0, "max": 1, "type": "int", "desc": "1=记录用户原文"},
    "knowledge_inject":      {"min": 0, "max": 1, "type": "int", "desc": "1=把知识库片段塞进提示词"},
    "auto_remember":         {"min": 0, "max": 1, "type": "int", "desc": "1=自动沉淀到知识库"},
    "sse_ping_interval":     {"min": 5, "max": 120, "type": "int", "desc": "流式推送的心跳间隔(秒)"},
    "max_concurrency":       {"min": 1, "max": 4, "type": "int", "desc": "同时进行的对话数"},
}

DEFAULT_SWITCHES: dict[str, str] = {
    "maintenance_mode": "0", "log_prompt": "1", "log_input_text": "1",
    "knowledge_inject": "1", "auto_remember": "1", "sse_ping_interval": "15",
    "max_concurrency": "1",
}

# 8 类情绪 → 语音默认映射（向量顺序：happy/angry/sad/afraid/disgusted/melancholic/surprised/calm）
VOICE_DEFAULTS: dict[str, dict[str, Any]] = {
    "happy":     {"reference_audio_id": "", "voice_id": "warm_female", "emotion_vector": [0.8, 0, 0, 0, 0, 0, 0.2, 0.2], "speed": 1.22, "volume": 1.0},
    "sad":       {"reference_audio_id": "", "voice_id": "warm_female", "emotion_vector": [0, 0, 0.7, 0, 0, 0.6, 0, 0.1], "speed": 1.0, "volume": 0.9},
    "angry":     {"reference_audio_id": "", "voice_id": "calm_female", "emotion_vector": [0, 0.7, 0, 0, 0, 0.1, 0, 0], "speed": 1.18, "volume": 1.0},
    "surprised": {"reference_audio_id": "", "voice_id": "warm_female", "emotion_vector": [0.2, 0, 0, 0.2, 0, 0, 0.8, 0], "speed": 1.25, "volume": 1.0},
    "fear":      {"reference_audio_id": "", "voice_id": "calm_female", "emotion_vector": [0, 0, 0.2, 0.7, 0, 0.2, 0.1, 0], "speed": 1.1, "volume": 0.9},
    "disgust":   {"reference_audio_id": "", "voice_id": "calm_female", "emotion_vector": [0, 0.2, 0, 0, 0.7, 0.1, 0, 0], "speed": 1.06, "volume": 1.0},
    "neutral":   {"reference_audio_id": "", "voice_id": "warm_female", "emotion_vector": [0, 0, 0, 0, 0, 0, 0, 1.0], "speed": 1.15, "volume": 1.0},
    "care":      {"reference_audio_id": "", "voice_id": "warm_female", "emotion_vector": [0.2, 0, 0, 0, 0, 0, 0, 0.8], "speed": 1.08, "volume": 0.95},
}

_SCHEMA_EXT = """
CREATE TABLE IF NOT EXISTS providers (
    code         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    mode         TEXT NOT NULL DEFAULT 'off',
    config_json  TEXT NOT NULL DEFAULT '{}',
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_profiles (
    emotion             TEXT PRIMARY KEY,
    reference_audio_id  TEXT NOT NULL DEFAULT '',
    voice_id            TEXT NOT NULL DEFAULT '',
    emotion_vector      TEXT NOT NULL DEFAULT '[]',
    speed               REAL NOT NULL DEFAULT 1.0,
    volume              REAL NOT NULL DEFAULT 1.0,
    note                TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reference_audios (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    file_path   TEXT NOT NULL DEFAULT '',
    emotion     TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_logs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT NOT NULL,
    request_id         TEXT,
    user_id            TEXT,
    session_id         TEXT,
    input_text         TEXT NOT NULL DEFAULT '',
    prompt_snapshot    TEXT NOT NULL DEFAULT '',
    emotion_label      TEXT NOT NULL DEFAULT '',
    intensity          REAL NOT NULL DEFAULT 0,
    reply_text         TEXT NOT NULL DEFAULT '',
    token_count        INTEGER NOT NULL DEFAULT 0,
    latency_ms         REAL NOT NULL DEFAULT 0,
    degraded           INTEGER NOT NULL DEFAULT 0,
    truncated          INTEGER NOT NULL DEFAULT 0,
    provider_snapshot  TEXT NOT NULL DEFAULT '{}',
    persona_version    INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_turn_user ON turn_logs(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_turn_session ON turn_logs(user_id, session_id, id DESC);
CREATE TABLE IF NOT EXISTS config_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'admin',
    target      TEXT NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kv_cache (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_ts  REAL NOT NULL
);
"""


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_store() -> None:
    """建表 + 幂等种子；两个进程启动时各调一次。

    控制平面与大脑可能同一秒启动、同时建表 → SQLite 会短暂锁表，
    这里重试几次并最终放行（表多半已由另一个进程建好），绝不因初始化失败而阻止服务启动。
    """
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            _init_store_once()
            return
        except sqlite3.OperationalError as exc:
            last_error = exc
            time.sleep(0.4 * (attempt + 1))
    print(f"[companion] 配置中心初始化被跳过（{last_error}），服务照常启动。")


def _init_store_once() -> None:
    conn = db.connect()
    try:
        conn.executescript(_SCHEMA_EXT)
        for code in PROVIDER_CODES:
            conn.execute(
                "INSERT OR IGNORE INTO providers(code,name,category,enabled,mode,config_json,version,updated_at) "
                "VALUES(?,?,?,1,?,?,1,?)",
                (code, PROVIDER_NAMES[code], code, PROVIDER_MODES[code],
                 json.dumps(PROVIDER_DEFAULTS[code], ensure_ascii=False), _now()),
            )
        for emotion in EMOTION_LABELS:
            preset = VOICE_DEFAULTS.get(emotion, {})
            conn.execute(
                "INSERT OR IGNORE INTO voice_profiles(emotion,reference_audio_id,voice_id,"
                "emotion_vector,speed,volume,note) VALUES(?,?,?,?,?,?,'')",
                (emotion, preset.get("reference_audio_id", ""), preset.get("voice_id", ""),
                 json.dumps(preset.get("emotion_vector", [])), preset.get("speed", 1.0),
                 preset.get("volume", 1.0)),
            )
        for key, value in DEFAULT_SWITCHES.items():
            conn.execute(
                "INSERT OR IGNORE INTO runtime_params(key,value,updated_at) VALUES(?,?,?)",
                (key, value, _now()),
            )
        # 老库里存的是「语音识别 ASR」「向量 / Embedding」这类半中半英的名字，
        # 统一成纯中文（只改这几个明确的老名字，不动用户自定义的）
        for _code, _old, _new in (
            ("asr", "语音识别 ASR", "语音识别"),
            ("tts", "语音合成 TTS", "语音合成"),
            ("embedding", "向量 / Embedding", "文本向量"),
        ):
            conn.execute(
                "UPDATE providers SET name=? WHERE code=? AND name=?", (_new, _code, _old)
            )
        _migrate_judge_model(conn)
        _migrate_model_services(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_model_services(conn) -> None:
    """把「模型跟大脑一起跑」时期的旧地址升级成分离式部署的地址。

    只改「还是旧默认值」的那一种，用户手工填过的一律不动：
      tts.base_url: http://127.0.0.1:9880 → config.yaml 的 TTS_BASE（默认 8102）
      judge.engine: rule → http（只有当判断服务地址能连上时才改，避免瞎切）

    为什么值得改：9880 是 GPT-SoVITS 的常用端口，我们自己的语音服务在 8102；
    留着 9880 会让「明明起了 5-启动语音合成.bat 却连不上」这种问题反复出现。
    """
    try:
        from .config import settings

        row = conn.execute("SELECT mode, config_json FROM providers WHERE code='tts'").fetchone()
        if row:
            cfg = json.loads(row[1] or "{}")
            tts_mode = str(row[0] or "").strip().lower()
            # 进程内引擎不占独立端口：不再把空地址回刷成 :8102。
            if isinstance(cfg, dict) and tts_mode != "inproc":
                cur = str(cfg.get("base_url") or "").strip().rstrip("/")
                if cur in ("", "http://127.0.0.1:9880"):
                    cfg["base_url"] = settings.TTS_BASE
                    conn.execute(
                        "UPDATE providers SET config_json=?, updated_at=? WHERE code='tts'",
                        (json.dumps(cfg, ensure_ascii=False), _now()),
                    )

        # 老库里的 mock/off 模式统一刷成真实模式（程序里已没有模拟路径）
        try:
            conn.execute("UPDATE providers SET mode='http' WHERE mode IN ('mock','off')"
                         " AND code IN ('llm','asr','tts')")
            conn.execute("UPDATE providers SET mode='off' WHERE mode='mock'"
                         " AND code IN ('embedding','knowledge')")
        except Exception:
            pass

        row = conn.execute(
            "SELECT mode, config_json FROM providers WHERE code='judge'").fetchone()
        if not row:
            return
        try:
            cfg = json.loads(row[1] or "{}")
        except (ValueError, TypeError):
            cfg = {}
        if not isinstance(cfg, dict):
            return
        engine = str(cfg.get("engine") or "").strip().lower()

        # 1) auto 是「模型跟大脑一起跑」时期的名字：那时它会在本机加载 ONNX。
        #    现在大脑不加载权重，auto 与 http 等价，统一写回 http 免得误会。
        if engine == "auto":
            cfg["engine"] = "http"
            engine = "http"
            conn.execute(
                "UPDATE providers SET config_json=?, updated_at=? WHERE code='judge'",
                (json.dumps(cfg, ensure_ascii=False), _now()),
            )

        # 2) 老库里 engine 为空（早期版本没这个字段）且判断服务在跑 → 切 http。
        #    只动「空」，rule 是用户明确选的，不覆盖。
        if not engine:
            try:
                from .services import model_status

                if model_status.probe_judge().get("up"):
                    cfg["engine"] = "http"
                    engine = "http"
                    conn.execute(
                        "UPDATE providers SET config_json=?, updated_at=? WHERE code='judge'",
                        (json.dumps(cfg, ensure_ascii=False), _now()),
                    )
            except Exception:
                pass

        # 3) 表格那列（mode）跟着实际引擎走，别出现「显示 onnx、测试说走服务」
        want = {"http": "http", "auto": "http", "onnx": "onnx", "rule": "rule"}.get(engine)
        if want and str(row[0]) != want:
            conn.execute("UPDATE providers SET mode=? WHERE code='judge'", (want,))
    except Exception:
        pass


def _migrate_judge_model(conn) -> None:
    """把「旧默认模型路径」升级成实际部署的模型。

    背景：早期库里写的是 models/sentiment_8class.onnx（一个从来没存在过的占位路径），
    engine 也是 rule。换了真模型以后这两项会一直把链路按在规则上，
    用户在控制台看到的和自己的部署对不上。

    三条护栏，避免覆盖用户的手工配置：
      1. 当前路径的文件真的存在 → 说明是用户自己放的，不动；
      2. 新模型文件不存在 → 没得换，不动；
      3. 只在 engine 仍是 rule/空 时才顺手改成 auto。
    """
    try:
        from pathlib import Path

        from .config import ROOT, settings

        row = conn.execute("SELECT config_json FROM providers WHERE code='judge'").fetchone()
        if not row:
            return
        cfg = json.loads(row[0] or "{}")
        if not isinstance(cfg, dict):
            return

        cur = str(cfg.get("model_path") or "").strip()
        target = settings.JUDGE_MODEL_PATH
        target_file = Path(target) if Path(target).is_absolute() else ROOT / target

        # 原来这里会把 mode 改成 onnx。现在判断可能走独立服务（http），
        # 静态写 onnx 会和「测试显示判断服务生效」矛盾，交给
        # _migrate_model_services 按实际 engine 统一。

        if cur == target:
            return
        if cur and (Path(cur) if Path(cur).is_absolute() else ROOT / cur).is_file():
            return
        if not target_file.is_file():
            return

        cfg["model_path"] = target
        if str(cfg.get("engine") or "").strip().lower() in ("", "rule"):
            cfg["engine"] = "auto"
        conn.execute(
            "UPDATE providers SET config_json=?, updated_at=? WHERE code='judge'",
            (json.dumps(cfg, ensure_ascii=False), _now()),
        )
    except Exception:
        # 迁移失败不影响启动：_judge_cfg 会回退到 config.yaml 的默认值
        pass


# ---------- providers ----------

def _row_to_provider(row) -> dict[str, Any]:
    try:
        config = json.loads(row["config_json"] or "{}")
    except (ValueError, TypeError):
        config = {}
    merged = dict(PROVIDER_DEFAULTS.get(row["code"], {}))
    merged.update(config)
    return {
        "code": row["code"], "name": row["name"], "category": row["category"],
        "enabled": int(row["enabled"]), "mode": row["mode"],
        "config": merged, "version": int(row["version"]), "updated_at": row["updated_at"],
        "fields": PROVIDER_FIELDS.get(row["code"], []),
        "mode_options": PROVIDER_MODE_OPTIONS.get(row["code"], []),
    }


def list_providers() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM providers ORDER BY code").fetchall()
        return [_row_to_provider(r) for r in rows]
    finally:
        conn.close()


def get_provider(code: str) -> dict[str, Any]:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM providers WHERE code=?", (code,)).fetchone()
        if row is None:
            return {"code": code, "name": code, "category": code, "enabled": 0,
                    "mode": PROVIDER_MODES.get(code, "off"),
                    "config": dict(PROVIDER_DEFAULTS.get(code, {})), "version": 0,
                    "updated_at": "", "fields": PROVIDER_FIELDS.get(code, []),
                    "mode_options": PROVIDER_MODE_OPTIONS.get(code, [])}
        return _row_to_provider(row)
    finally:
        conn.close()


def _coerce(field: dict[str, Any], value: Any) -> Any:
    kind = field.get("type")
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if kind == "number":
        num = float(value)
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and num < lo:
            raise ValueError(f"{field['key']} < {lo}")
        if hi is not None and num > hi:
            raise ValueError(f"{field['key']} > {hi}")
        return int(num) if num.is_integer() else num
    if kind == "select":
        options = field.get("options") or []
        if options and value not in options:
            raise ValueError(f"{field['key']} must be one of {options}")
        return value
    if kind == "textarea":
        return str(value)
    return str(value)


def update_provider(code: str, patch: dict[str, Any], enabled: int | None = None,
                    mode: str | None = None, actor: str = "admin") -> dict[str, Any]:
    """只接受字段白名单内的 key；越界 / 类型错抛 ValueError。"""
    if code not in PROVIDER_CODES:
        raise ValueError(f"unknown provider: {code}")
    if mode is not None:
        mode = str(mode).strip().lower()
        allowed = PROVIDER_MODE_OPTIONS.get(code, [])
        if allowed and mode not in allowed:
            raise ValueError(f"运行方式 {mode} 不适用于 {code}，可选：{' / '.join(allowed)}")
    current = get_provider(code)
    field_map = {f["key"]: f for f in PROVIDER_FIELDS[code]}
    config = dict(current["config"])
    changed: dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if key not in field_map:
            raise ValueError(f"field '{key}' not allowed for {code}")
        coerced = _coerce(field_map[key], value)
        config[key] = coerced
        changed[key] = coerced
    # 列 mode 与内层 config.mode 是同一事实：写列时同步进内层，避免两处漂移。
    if mode is not None:
        config["mode"] = mode
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE providers SET config_json=?, enabled=?, mode=?, version=version+1, updated_at=? "
            "WHERE code=?",
            (json.dumps(config, ensure_ascii=False),
             int(enabled) if enabled is not None else current["enabled"],
             mode if mode is not None else current["mode"], _now(), code),
        )
        conn.execute(
            "INSERT INTO config_audit(created_at,actor,target,action,detail) VALUES(?,?,?,?,?)",
            (_now(), actor, f"provider:{code}", "update",
             json.dumps({"changed": changed, "enabled": enabled, "mode": mode}, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    return get_provider(code)


def provider_snapshot() -> dict[str, Any]:
    """写进 turn_logs：记录这一轮实际用了哪些 provider 与模式。"""
    return {p["code"]: {"mode": p["mode"], "enabled": p["enabled"],
                        "version": p["version"]} for p in list_providers()}


# ---------- 语音配置 ----------

def list_voice_profiles() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM voice_profiles").fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["emotion_vector"] = json.loads(item["emotion_vector"] or "[]")
            except ValueError:
                item["emotion_vector"] = []
            out.append(item)
        order = {label: i for i, label in enumerate(EMOTION_LABELS)}
        out.sort(key=lambda x: order.get(x["emotion"], 99))
        return out
    finally:
        conn.close()


def update_voice_profile(emotion: str, patch: dict[str, Any], actor: str = "admin") -> dict[str, Any]:
    if emotion not in EMOTION_LABELS:
        raise ValueError(f"emotion must be one of {EMOTION_LABELS}")
    current = next((p for p in list_voice_profiles() if p["emotion"] == emotion), {})
    allowed = {"reference_audio_id", "voice_id", "emotion_vector", "speed", "volume", "note"}
    merged = {k: current.get(k) for k in allowed}
    merged.update({k: v for k, v in (patch or {}).items() if k in allowed})
    vector = merged["emotion_vector"]
    if isinstance(vector, str):
        try:
            vector = json.loads(vector)
        except ValueError:
            vector = []
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO voice_profiles(emotion,reference_audio_id,voice_id,emotion_vector,speed,volume,note) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(emotion) DO UPDATE SET "
            "reference_audio_id=excluded.reference_audio_id, voice_id=excluded.voice_id, "
            "emotion_vector=excluded.emotion_vector, speed=excluded.speed, "
            "volume=excluded.volume, note=excluded.note",
            (emotion, str(merged.get("reference_audio_id") or ""), str(merged.get("voice_id") or ""),
             json.dumps(vector), float(merged.get("speed") or 1.0),
             float(merged.get("volume") or 1.0), str(merged.get("note") or "")),
        )
        conn.execute(
            "INSERT INTO config_audit(created_at,actor,target,action,detail) VALUES(?,?,?,?,?)",
            (_now(), actor, f"voice:{emotion}", "update", json.dumps(patch, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    return next(p for p in list_voice_profiles() if p["emotion"] == emotion)


def list_reference_audios() -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reference_audios ORDER BY created_at DESC, id DESC").fetchall()]
    finally:
        conn.close()


def save_reference_audio(audio_id: str, name: str, file_path: str = "",
                         emotion: str = "", note: str = "", actor: str = "admin") -> dict:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO reference_audios(id,name,file_path,emotion,note,created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, file_path=excluded.file_path, "
            "emotion=excluded.emotion, note=excluded.note",
            (audio_id, name, file_path, emotion, note, _now()),
        )
        conn.execute(
            "INSERT INTO config_audit(created_at,actor,target,action,detail) VALUES(?,?,?,?,?)",
            (_now(), actor, f"audio:{audio_id}", "save", name),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": audio_id, "name": name, "file_path": file_path, "emotion": emotion, "note": note}


def delete_reference_audio(audio_id: str) -> bool:
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM reference_audios WHERE id=?", (audio_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------- 轮次明细 / 会话 ----------

def open_turn(*, request_id: str, user_id: str, session_id: str, input_text: str = "",
              prompt_snapshot: str = "", emotion_label: str = "", intensity: float = 0,
              persona_version: int = 0, provider_snapshot: dict | None = None) -> int:
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO turn_logs(created_at,request_id,user_id,session_id,input_text,"
            "prompt_snapshot,emotion_label,intensity,persona_version,provider_snapshot,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'running')",
            (_now(), request_id, user_id, session_id, input_text, prompt_snapshot,
             emotion_label, float(intensity), int(persona_version),
             json.dumps(provider_snapshot or {}, ensure_ascii=False)),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def close_turn(turn_id: int, *, reply_text: str = "", token_count: int = 0,
               latency_ms: float = 0, degraded: int = 0, truncated: int = 0,
               status: str = "ok", prompt_snapshot: str | None = None) -> None:
    conn = db.connect()
    try:
        if prompt_snapshot is None:
            conn.execute(
                "UPDATE turn_logs SET reply_text=?, token_count=?, latency_ms=?, degraded=?, "
                "truncated=?, status=? WHERE id=?",
                (reply_text, int(token_count), float(latency_ms), int(degraded),
                 int(truncated), status, int(turn_id)),
            )
        else:
            conn.execute(
                "UPDATE turn_logs SET reply_text=?, prompt_snapshot=?, token_count=?, latency_ms=?, "
                "degraded=?, truncated=?, status=? WHERE id=?",
                (reply_text, prompt_snapshot, int(token_count), float(latency_ms),
                 int(degraded), int(truncated), status, int(turn_id)),
            )
        conn.commit()
    finally:
        conn.close()


def list_turn_logs(user_id: str | None = None, session_id: str | None = None,
                   limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM turn_logs WHERE 1=1"
    args: list[Any] = []
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    if session_id:
        sql += " AND session_id=?"
        args.append(session_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def list_sessions(limit: int = 50) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT user_id, session_id, COUNT(*) AS turns, MIN(created_at) AS first_at, "
            "MAX(created_at) AS last_at, "
            "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_turns "
            "FROM turn_logs GROUP BY user_id, session_id ORDER BY last_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            emo = conn.execute(
                "SELECT emotion_label, intensity FROM emotion_events "
                "WHERE user_id=? AND session_id=? ORDER BY id DESC LIMIT 1",
                (r["user_id"], r["session_id"]),
            ).fetchone()
            item["last_emotion"] = emo["emotion_label"] if emo else ""
            item["last_intensity"] = emo["intensity"] if emo else 0
            out.append(item)
        return out
    finally:
        conn.close()


def turn_stats() -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS turns, COALESCE(SUM(token_count),0) AS tokens, "
            "COALESCE(AVG(latency_ms),0) AS avg_latency, "
            "COALESCE(SUM(CASE WHEN degraded=1 THEN 1 ELSE 0 END),0) AS degraded_turns "
            "FROM turn_logs"
        ).fetchone()
        return {"turns": int(row["turns"]), "tokens": int(row["tokens"]),
                "avg_latency_ms": round(float(row["avg_latency"]), 1),
                "degraded_turns": int(row["degraded_turns"])}
    finally:
        conn.close()


# ---------- 事实记忆 / 审计 ----------

def list_facts(user_id: str | None = None, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM facts WHERE 1=1"
    args: list[Any] = []
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(int(limit))
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def delete_fact(fact_id: str) -> bool:
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def log_audit(*, target: str, action: str, detail: str = "", actor: str = "admin") -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO config_audit(created_at,actor,target,action,detail) VALUES(?,?,?,?,?)",
            (_now(), actor, target, action, detail),
        )
        conn.commit()
    finally:
        conn.close()


def list_audit(limit: int = 100) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM config_audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]
    finally:
        conn.close()


def flat_params() -> list[dict]:
    """把 provider 字段 + 运行时参数拍平成一张可搜索的表（对标小智「参数管理」）。"""
    items: list[dict] = []
    for code in PROVIDER_CODES:
        prov = get_provider(code)
        cfg = prov.get("config") or {}
        for f in PROVIDER_FIELDS.get(code, []):
            items.append({
                "code": f"{code}.{f['key']}",
                "label": f"{PROVIDER_NAMES.get(code, code)} · {f['label']}",
                "group": code,
                "group_label": PROVIDER_NAMES.get(code, code),
                "value": cfg.get(f["key"], f.get("default")),
                "type": f.get("type", "text"),
                "min": f.get("min"), "max": f.get("max"),
                "options": f.get("options"), "desc": f.get("desc", ""),
                "secret": bool(f.get("secret")),
                "source": "provider",
            })
    params = db.get_runtime_params()
    for key, rule in RUNTIME_RULES.items():
        items.append({
            "code": key,
            "label": rule.get("desc") or key,
            "group": "runtime",
            "group_label": "运行时开关",
            "value": params.get(key, ""),
            "type": rule.get("type", "text"),
            "min": rule.get("min"), "max": rule.get("max"),
            "options": None, "desc": rule.get("desc", ""),
            "secret": False, "source": "runtime",
        })
    return items


def set_provider_field(code: str, key: str, value: Any) -> dict:
    """扁平参数表写回：单个 provider 字段（值域校验走字段元数据）。"""
    if code not in PROVIDER_CODES:
        raise ValueError(f"unknown provider: {code}")
    field = next((f for f in PROVIDER_FIELDS[code] if f["key"] == key), None)
    if field is None:
        raise ValueError(f"unknown field: {key}")
    coerced = _coerce(field, value)
    return update_provider(code, {key: coerced})


def cache_get(key: str, ttl: float = 600.0) -> Any | None:
    """进程共享的 KV 缓存：控制平面与实时链路只通过 DB 交换缓存。"""
    conn = db.connect()
    try:
        row = conn.execute("SELECT value_json, updated_ts FROM kv_cache WHERE key=?",
                           (key,)).fetchone()
        if row is None or (time.time() - float(row["updated_ts"])) >= ttl:
            return None
        return json.loads(row["value_json"])
    except Exception:
        return None
    finally:
        conn.close()


def cache_set(key: str, value: Any) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO kv_cache(key,value_json,updated_ts) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_ts=excluded.updated_ts",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def extended_stats() -> dict:
    base = db.stats()
    base.update(turn_stats())
    conn = db.connect()
    try:
        base["providers"] = int(conn.execute("SELECT COUNT(*) AS n FROM providers").fetchone()["n"])
        base["reference_audios"] = int(conn.execute(
            "SELECT COUNT(*) AS n FROM reference_audios").fetchone()["n"])
        base["config_changes"] = int(conn.execute(
            "SELECT COUNT(*) AS n FROM config_audit").fetchone()["n"])
    finally:
        conn.close()
    return base
