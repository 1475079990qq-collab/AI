# companion 共享契约 v1 · 2026-11-13

本次交付范围：**大脑端 + 安卓端 + Web 端**。Windows 客户端、ESP32 本次不做。
所有并行开发者**必须先读本文件，以及 `brain/config.py`、`brain/schemas.py`、`brain/db.py`**。

## 0. 三条硬规矩

1. **默认 `BRAIN_MOCK=1`**：不装 Ollama / ONNX / Chroma / WIndexTTS，两条链路也必须能端到端跑通。任何外部依赖不可用一律降级，**绝不抛 500 打断对话**。
2. **情感判断独立成层**，LLM 只负责生成有温度的回复，**禁止 LLM 输出情感标签**。
3. **客户端零推理零记忆**：Web 与 Android 只做 UI、SSE 解析、音频播放。
4. 严格按上一轮评审结论：判定结果**开流前同步落库**，流后只 update；控制平面与实时链路**进程级隔离**。

## 1. 文件归属（写死，严禁越界）

| 归属 | 只准写这些文件 |
|---|---|
| 主智能体（已完成） | `CONTRACT.md`、`requirements.txt`、`.env.example`、`brain/{__init__,config,schemas,db}.py`、`brain/{services,routers}/__init__.py`、`start_*.bat`、`tools/smoke_test.py`、`README.md`、`data/` |
| A 大脑服务 | `brain/services/{judge,memory,llm,tts,prompt,pipeline}.py`、`brain/routers/chat.py`、`brain/app_brain.py` |
| B 控制平面 | `brain/routers/admin.py`、`brain/app_console.py` |
| C Web 前端 | `web/{index.html,admin.html,config.js,app.js,admin.js,style.css}` |
| D 安卓端 | `client/**`（Flutter 工程全部） |

**禁止修改 `CONTRACT.md`、`config.py`、`schemas.py`、`db.py` 和别人的文件。** 缺能力就在返回里说明，不要自己动手改。

## 2. 运行方式（工作目录必须是 `companion/` 根）

```
pip install -r requirements.txt
python -m uvicorn brain.app_brain:app   --host 0.0.0.0 --port 8000   # 实时链路
python -m uvicorn brain.app_console:app --host 0.0.0.0 --port 8001   # 控制平面
```

两个进程**只共享 `data/companion.db`**：控制平面只写配置/修正，实时链路只读配置 + 写事件。这是评审要求的进程级隔离，不是同应用挂两个路由。
两个 app 都必须 `CORSMiddleware(allow_origins=["*"])`，都必须挂载 `web/` 静态目录。
两个 app 启动时都调 `brain.db.init_db()`。

## 3. 共享模块（已实现，直接 import，不要重写）

### `brain.config`
`from brain.config import settings`，字段（全为只读初值）：
`ROOT, DB_PATH, CHROMA_PATH, OLLAMA_BASE, LLM_MODEL, EMBED_MODEL, TTS_BASE, TTS_TIMEOUT, JUDGE_MODEL_PATH, JUDGE_CONTEXT_TURNS, MOCK, BRAIN_HOST, BRAIN_PORT, CONSOLE_HOST, CONSOLE_PORT, MEMORY_TOP_K, EMOTION_MIN_INTENSITY, EMOTION_RECENT_LIMIT, LLM_NUM_CTX, LLM_TEMPERATURE, LLM_TOP_P, LLM_MAX_TOKENS, ADMIN_TOKEN`
**运行时参数（温度/top_p/top_k/num_gpu 等）一律从 `db.get_runtime_params()` 读，不要用 settings 的固定值。** settings 只是初始默认。

### `brain.schemas`
`EMOTION_LABELS`（8 个英文枚举 tuple）、`ChatRequest`、`TTSRequest`、`EmotionResult`、`TurnEvent`（含 `.to_sse()`）、`new_request_id()`。

### `brain.db`（全部同步函数，内部每次新连接 + WAL）
```
init_db() -> None
connect() -> sqlite3.Connection
open_event(*, user_id, session_id, request_id, label, intensity, confidence, degraded=0, summary="") -> int
close_event(event_id, summary="", fact_ids: list[str]|None=None) -> None
mark_truncated(event_id) -> None
recent_emotions(user_id, min_intensity=0.6, limit=5) -> list[dict]
list_emotion_events(label=None, min_intensity=None, limit=100) -> list[dict]
update_emotion_event(event_id, label=None, intensity=None, summary=None) -> bool
get_active_persona() -> dict                 # {id,name,system_prompt,is_active,version,updated_at}
list_personas() -> list[dict]
save_persona(persona_id: int|None, name: str, system_prompt: str) -> int
activate_persona(persona_id: int) -> dict
get_runtime_params() -> dict                # 值为 str，调用方自行转换
set_runtime_param(key: str, value: str) -> None
log_judge(*, request_id, input_text, context_text, output_label, confidence, intensity, degraded, latency_ms, source) -> None
list_judge_logs(limit=100) -> list[dict]
add_fact(*, fact_id: str, user_id: str, session_id: str, text: str, metadata: dict|None=None) -> str
search_facts(user_id, query, top_k=3) -> list[dict]   # [{id,text,score,metadata}]
stats() -> dict
DEFAULT_PERSONA: str
```

## 4. HTTP 契约（钉死，Web/Android 都按这个写）

### `POST /chat` -> `text/event-stream`
请求：`{"user_id":"u1","session_id":"s1","text":"最近工作压力很大","request_id":"可选"}`
响应**只用 `data:` 行**，每个 JSON 必带 `type`；帧间空行；心跳用 `: ping\n\n` 注释帧（客户端跳过）。禁止 gzip。

```
data: {"type":"meta","request_id":"...","persona_version":1,"degraded":false}

data: {"type":"token","delta":"听起来"}

data: {"type":"done","done":true,"emotion":"sad","intensity":0.7,"event_id":12,"truncated":false}

data: {"type":"error","code":"LLM_STREAM_FAILED","message":"...","retryable":true,"request_id":"..."}
```
- `type` 枚举：`meta` / `token` / `clarify` / `ping` / `done` / `error`。
- `meta` 必为首帧，`done` 或 `error` 必为末帧且互斥。
- 低信息量（文本去空白 < 4 字符）走 `clarify`，直接给温柔追问，**不进 LLM**：`{"type":"clarify","question":"...","done":true,"emotion":...}`。
- 头：`Cache-Control: no-cache`、`X-Accel-Buffering: no`、`Connection: keep-alive`。
- 客户端断连必须 `finally` 关掉 Ollama 流并 `mark_truncated()`。
- `error` 码：`INVALID_INPUT` / `BUSY` / `MODEL_UNAVAILABLE` / `LLM_STREAM_FAILED` / `LLM_TIMEOUT` / `JUDGE_DEGRADED` / `MEMORY_DEGRADED`。`DB_WRITE_FAILED` 只记日志、不发流。

### `POST /tts`
请求 `{"text":"...","emotion":"sad","reference_audio_id":"r1","request_id":"..."}`
成功 `200 audio/wav`（原始字节）+ `X-Request-Id` / `X-Emotion`；不可用 `503 {"detail":"TTS_UNAVAILABLE"}`。
MOCK 模式必须返回**真实可播放的 WAV 字节**（正弦波即可，时长随文本长度，不同 emotion 用不同基频），让 Web/Android 能真的听到声音。

### `GET /health`
`{"status":"ok","mock":true,"llm":"mock|ollama","judge":"rule|onnx|mock","tts":"mock|http","memory":"sqlite|chroma","gpu":{...},"db":"ok"}`

### 控制平面（:8001，前缀 `/api/admin`）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/overview` | `db.stats()` + `/health` 代理结果 + 显存信息 |
| GET/POST | `/api/admin/personas` | 列表 / 新增或编辑 `{id?,name,system_prompt}` |
| POST | `/api/admin/personas/{id}/activate` | 启用（保存即生效，不重启） |
| GET/PUT | `/api/admin/params` | 参数列表 / `{key,value}` 更新，**值域白名单校验** |
| GET | `/api/admin/emotions` | `?label=&min_intensity=&limit=` |
| PATCH | `/api/admin/emotions/{id}` | `{emotion_label?,intensity?,summary?}` 手动修正 |
| GET | `/api/admin/judge-logs` | `?limit=` |
| GET | `/api/admin/gpu` | `nvidia-smi --query-gpu=...` + `ollama ps` 解析，命令不存在时降级返回 `{"available":false}` |

`PUT /api/admin/params` 白名单：`temperature`(0-2)、`top_p`(0-1)、`top_k`(1-100)、`num_ctx`(512-8192)、`max_tokens`(32-2048)、`memory_top_k`(1-10)、`emotion_min_intensity`(0-1)、`gpu_exclusive`(0/1)。越界返回 422。
所有写操作要求头 `X-Admin-Token: <ADMIN_TOKEN>`（MOCK 默认 token 为空时放行，方便本地调试）。

## 5. services 模块签名（A 必须按此实现，全部可在 MOCK 下工作）

```python
# judge.py
def judge(text: str, history: list[dict]) -> EmotionResult
#   history 为最近 1-2 轮 [{"role":"user"/"assistant","content":"..."}]
#   真实路径：ONNX FP32 CPU；模型不存在 → 规则兜底(label 关键词 + neutral)，degraded=True
#   intensity：模型只给 logits 时用 softmax 置信度做代理（上一轮评审结论）

# memory.py
def retrieve(user_id: str, query: str, top_k: int = 3) -> dict
#   -> {"facts":[{"id","text","score"}], "emotions":[db.recent_emotions 结果], "source":"sqlite|chroma"}
def store_fact(user_id: str, session_id: str, text: str, fact_ids: list[str]) -> None
#   chromadb 装了就用，没装走 db.add_fact / db.search_facts；任何异常返回空 facts，不抛

# llm.py
async def stream(messages: list[dict], params: dict) -> AsyncIterator[str]
#   真实：httpx.AsyncClient 调 OLLAMA_BASE/v1/chat/completions，stream=True，解析 delta.content
#   MOCK：按 messages 内容生成一段中文回复，逐字/逐词 yield，带 asyncio.sleep(0.03) 模拟流式
#   不可用时抛 LLMUnavailable（自定义异常），由 pipeline 转 error 事件

# tts.py
async def synthesize(text: str, emotion: str, reference_audio_id: str|None) -> bytes
#   真实：POST TTS_BASE 拿 wav；MOCK：生成正弦波 WAV（wave+struct+math 标准库，零依赖）

# prompt.py
def build(persona: dict, bundle: dict, emotion: EmotionResult, text: str) -> list[dict]
#   请求级快照：system 含 persona + 事实片段 + 近期情感事件摘要 + "禁止输出情感标签"

# pipeline.py
async def handle(req: ChatRequest, params: dict, history: list[dict]) -> AsyncIterator[TurnEvent]
#   顺序：judge(同步, to_thread) → db.open_event(开流前同步落库) → yield meta
#         → memory.retrieve → prompt.build → llm.stream 逐 token yield
#         → db.close_event(summary, fact_ids) → yield done
#   任一环节降级都要在 meta.degraded / done 里体现；异常 yield error 后 return
#   finally 里处理断连：mark_truncated + 关闭流
```

`brain/routers/chat.py`：实现 `/chat`、`/tts`、`/health`，用原生 `StreamingResponse`（**不依赖 sse-starlette**），`TurnEvent.to_sse()` 序列化。维护一个进程内 `asyncio.Semaphore(1)` 做 GPU 独占（评审要求的全局并发=1）。
`brain/app_brain.py`：FastAPI(title="companion brain")、CORS、挂 chat router、挂 `web/` 静态目录到 `/`、启动 `init_db()`。

## 6. 降级矩阵（必须逐条实现）

| 挂掉的组件 | 行为 |
|---|---|
| Ollama 不可达 | MOCK 回复；非 MOCK 发 `LLM_STREAM_FAILED`，不 500 |
| ONNX 模型缺失 | 规则判断，`degraded=True`，仍落库 |
| Chroma 未安装 | 走 SQLite 事实表 |
| WIndexTTS 不可达 | MOCK 生成 WAV；非 MOCK 返回 503 |
| SQLite 写失败 | 只记日志，`done` 照发 |
| nvidia-smi / ollama 不存在 | `/api/admin/gpu` 返回 `available:false` |

## 7. Web 前端约定（C）

- `web/config.js`：按 `location.hostname` 推导 `BRAIN_URL = http://<host>:8000`、`CONSOLE_URL = http://<host>:8001`，允许 URL 参数 `?brain=...&console=...` 覆盖（手机局域网要用）。
- `index.html` 对话页：`fetch` + `response.body.getReader()` 读流，**按 `\n\n` 缓冲切帧**（不能按 chunk 直接解码，中文会被切碎），逐 token 追加；`done` 才取 emotion 并异步 `POST /tts` 播放 `<audio>`；`error` 保留半句 + 重试按钮。
- `admin.html` 控制台：人格模板编辑/启用、运行时参数、情感事件列表与修正、判断日志、GPU 状态；写操作带 `X-Admin-Token`。
- 纯原生 HTML/CSS/JS，**不引入任何 CDN 依赖**（本地离线可用）；浅色主题，简洁。

## 8. 安卓端约定（D）

Flutter 工程，包名 `com.companion.app`，`minSdk 21`。
- `lib/config.dart`：服务器地址可配置（默认 `http://10.0.2.2:8000`，界面上可改局域网 IP），用 `shared_preferences` 存。
- `lib/sse_client.dart`：**用 `package:http` 的 `Client.send()`（不是 dio）**，`StreamedResponse` → `.transform(utf8.decoder)` → `LineSplitter`，按 `data:` 前缀解析；显式 `Accept-Encoding: identity`；跳过 `:` 注释帧。
- `lib/chat_provider.dart`：`ChangeNotifier`，`messages / isStreaming / currentEmotion / serverUrl`，`send(String text)`；只有 `done` 结束流，`error` 保留半句不清空。
- `lib/audio_player.dart`：`just_audio` 播放 `/tts` 返回的字节。
- `lib/main.dart`：聊天 UI + 服务器地址设置；**本期键盘输入先行，录音只留接口不接**。
- `AndroidManifest.xml`：`INTERNET`、`RECORD_AUDIO`（预留）、`application android:usesCleartextTraffic="true"`。
- 附 `client/README.md`：`flutter pub get` / `flutter run` 步骤、局域网访问说明、本期不做录音。

## 9. 验收（`tools/smoke_test.py`，主智能体实现并跑）

1. `/health` 返回 ok 且 `mock=true`。
2. 第一轮「最近工作压力很大」收到流式 token 与 `done`，`emotion` 在 8 枚举内。
3. 第二轮「今天还是有点累」的 **prompt 里含第一轮情绪**（用 mock LLM 回显或 judge-logs/DB 断言，不靠肉眼看回复）。
4. `emotion_events` 恰好两条、`closed=1`、`intensity` 合理。
5. 控制台改人格模板并启用 → 下一次 `/chat` 的 `meta.persona_version` 立即变化，**不重启**。
6. `/tts` 返回可播放 WAV（RIFF 头 + 长度 > 1KB）。

---

## 10. 增量 v2 · 配置中心（2026-11-13 第二轮）

### 10.0 三层分离原则（用户明确要求）
- **模型 / 知识库 / TTS 是独立部署的服务**，大脑不启动、不加载、不管理它们，只保存「连到哪里、用什么参数、开不开」。
- **大脑 = 编排 + 读配置 + 落库**；**控制台 = 唯一配置入口**；**前端（Web / Android）完全不感知配置**。
- 全部配置存 `data/companion.db`，**每轮请求读一次**（请求级快照），改完立即生效，不重启、不清缓存。
- 用户原话强调「模型是模型，控制大脑是控制大脑，前端是前端，这是两回事」，任何把模型参数写死在前端 / 代码里的做法都算违反。

### 10.1 新增共享模块（主智能体已完成，**禁止修改**）
`brain/config_store.py`：5 类 provider（llm / embedding / judge / tts / knowledge）的字段元数据 `PROVIDER_FIELDS`（共 66 项，前端据此动态渲染表单）、`PROVIDER_DEFAULTS`、`RUNTIME_RULES`（16 项运行时参数/全局开关）、`KNOWN_BASES`（4 个知识库，2026-11-13 实拉）、`VOICE_DEFAULTS`（8 情绪），以及下述 DAO：

```
init_store()                       # 建表 + 幂等种子，两个 app 的 lifespan 都已调用
list_providers() / get_provider(code) -> dict  # {code,name,category,enabled,mode,config,version,updated_at,fields}
update_provider(code, patch, enabled=None, mode=None, actor="admin") -> dict
provider_snapshot() -> dict
list_voice_profiles() / update_voice_profile(emotion, patch)
list_reference_audios() / save_reference_audio(...) / delete_reference_audio(id)
open_turn(...) -> int / close_turn(turn_id, ...) / list_turn_logs(...) / list_sessions() / turn_stats()
list_facts(user_id,limit) / delete_fact(id)
log_audit(...) / list_audit(limit) / extended_stats()
```
新增 5 张表：`providers`、`voice_profiles`、`reference_audios`、`turn_logs`、`config_audit`。

### 10.2 文件归属（本轮）
| 角色 | 只准写 |
|---|---|
| 主智能体 | `config_store.py`、`CONTRACT.md`、两个 `app_*` 的 lifespan、`smoke_test.py`、`README.md` |
| S1 | **新建** `brain/services/providers.py`、`brain/services/knowledge.py` |
| S2 | **改** `brain/services/{llm,tts,judge,memory,prompt,pipeline}.py` |
| B | **改** `brain/routers/admin.py` |
| C | **改** `web/{admin.html,admin.js,style.css}` |

严禁改 `config_store.py`、`db.py`、`schemas.py`、`config.py` 和别人的文件。原有 SSE 契约（第 4 节）与 18 项冒烟**一律不能回归**。

### 10.3 S1 新模块接口（钉死）
```python
# providers.py
def get(code: str) -> dict                      # 薄封装 config_store.get_provider
def resolve(code: str, key: str, default=None) # 取单个配置值
def is_mock(code: str) -> bool                 # enabled=0 或 mode in {mock,off} → True
def snapshot() -> dict                         # config_store.provider_snapshot()
def test(code: str) -> dict                    # {"ok","latency_ms","mode","detail"}，真实探测；异常一律降级不抛
#   llm/embedding/tts: GET 或最小 POST 探测；judge: 用一句样例跑一次判断；knowledge: 调 list_bases()
#   mode=mock 时返回 ok=True + detail="mock 模式，未发起真实连接"

# knowledge.py —— 知识库适配层（用户用 MCP 工具直连 WeKnora 做长期记忆与特殊条目）
def search(query: str, top_k: int | None = None) -> dict
#   -> {"results":[{"title","content","score","source"}], "mode":..., "degraded":bool, "latency_ms":...}
def remember(text: str, title: str = "", tags: str = "") -> dict   # {"ok",...}
def list_bases() -> dict                                            # {"bases":[{"id","name"}], "mode", "degraded"}
```
三种模式（配置 `knowledge.mode`）：
- `mock`：返回内置示例片段（可演示注入链路，**默认**）；
- `mcp_http`：对 `mcp_endpoint` 发 JSON-RPC `tools/call`，工具名从配置读（`tool_list_bases` / `tool_search` / `tool_remember`），参数形状对齐现有 MCP 工具（`list_knowledge_bases` 无参；`search_knowledge` 传 `query` + `limit` + `knowledge_base_ids`；`add_document` 传 `knowledge_base_id` / `title` / `content`）；
- `webhook`：POST `webhook_url`，body `{query, top_k, kb_ids}` → `{results:[...]}`。
`off` / 任何异常 → 返回空 results + `degraded=True`，**绝不抛、绝不阻塞对话**。`kb_ids` 从配置按逗号解析；`min_score` 过滤；`special_documents` 里的 id 走 `search` 的固定注入占位。

### 10.4 S2 改造点（**保持原函数签名不变**）
- `llm.stream`：从 `providers.get("llm")` 读 `api_style/base_url/api_key/model/fallback_model/timeout_s/num_gpu/num_ctx/temperature/top_p/max_tokens/stream/force_gpu`；`providers.is_mock("llm")` 为真时保持现有 mock（含 `[记忆:xxx]` 回显，**不能丢，smoke_test 依赖**）。`api_key` 作 Bearer。
- `tts.synthesize`：从 `providers.get("tts")` 读 `base_url/api_key/format/sample_rate/speed/volume/default_voice/emotion_control/use_reference_audio/timeout_s`；从 `config_store.list_voice_profiles()` 取该 `emotion` 的 `reference_audio_id/voice_id/emotion_vector/speed/volume` 一起作为请求参数；mock 模式保持现有可播放正弦波 WAV。
- `judge.judge`：从 `providers.get("judge")` 读 `engine/context_turns/confidence_as_intensity/intensity_threshold/label_map`；`engine=rule` 或 mock 保持现有规则分类。
- `memory.retrieve`：返回 dict 增加 `knowledge` 字段（= `knowledge.search(query)["results"]`，仅当 `knowledge_inject=1` 且 knowledge 未关闭）；原 `facts/emotions/source` 不变，异常返回空列表。
- `prompt.build`：**签名不变**，从 `bundle["knowledge"]` 非空时追加一个段落，标题取 knowledge 配置的 `section_title`，位置在事实片段之后、`近期情绪事件:` 之前。不得改动 `近期情绪事件:` 段落的内容格式（mock 回显断言依赖它）。
- `pipeline.handle`：每轮读 `db.get_runtime_params()`；`maintenance_mode=1` → 直接温柔提示不进 LLM；`force_mock=1` → 强制 mock；开流时 `config_store.open_turn(...)`，结束/异常时 `close_turn(...)`（记录 reply/token_count/latency_ms/degraded/provider_snapshot）；`log_prompt=0` 不存 prompt 快照、`log_input_text=0` 不存原文；`auto_remember=1` 且强度达阈值时**流结束后**调 `knowledge.remember`，不得阻塞 SSE。`sse_ping_interval` 从参数读。

### 10.5 B 新增控制平面 API（前缀 `/api/admin`，写操作全部带 `X-Admin-Token`）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/providers` | 5 个 provider 全量（含 fields 元数据，供前端动态渲染） |
| GET | `/providers/{code}` | 单个 |
| PUT | `/providers/{code}` | body `{config?, enabled?, mode?}` → `config_store.update_provider`；非法字段 422 |
| POST | `/providers/{code}/test` | 调 `providers.test(code)`，永不 500 |
| GET | `/knowledge/bases` | 调 `knowledge.list_bases()`，附 `KNOWN_BASES` 兜底 |
| POST | `/knowledge/search` | body `{query, top_k?}` → `knowledge.search` |
| POST | `/knowledge/remember` | body `{text,title?,tags?}` → `knowledge.remember` |
| GET/PUT | `/voice-profiles` | 8 情绪映射；PUT body `{emotion, ...patch}` |
| GET/POST | `/reference-audios` | 列表 / 新增 `{id,name,file_path?,emotion?,note?}` |
| DELETE | `/reference-audios/{id}` | 删除 |
| GET | `/turn-logs` | `?user_id=&session_id=&limit=` |
| GET | `/sessions` | 会话聚合 |
| GET/DELETE | `/facts` | 事实记忆列表 / `?id=` 删除 |
| GET | `/config-audit` | 配置变更历史 |
| GET | `/system` | `extended_stats()` + 各 provider 模式 + `RUNTIME_RULES` + 版本 |
| POST | `/tts/preview` | body `{text, emotion}` → 直接调 `tts.synthesize` 返回 wav（试听） |
| GET/PUT | `/params` | **改用 `config_store.RUNTIME_RULES` 作为唯一白名单**（16 项，不再硬编码 8 项） |
`overview` 改用 `extended_stats()`。所有新接口异常降级，绝不 500。

### 10.6 C 控制台前端板块（从 5 个扩到 12 个，用侧边导航或分区折叠，别堆成一长条）
1 概览（provider 状态卡 / 轮次统计 / 组件当前模式）｜2 人格模板｜**3 模型服务**（5 张 provider 卡：动态表单 + 保存 + 启停 + 连接测试）｜**4 知识库 / 长期记忆**（模式、库列表可勾选、检索测试、手动沉淀、特殊文档）｜**5 TTS / 语音**（8 行情绪映射 + 参考音频管理 + 试听按钮）｜6 运行时参数 / 全局开关（16 项）｜7 情感事件｜**8 会话与每轮明细**｜**9 事实记忆**｜**10 配置变更审计**｜11 判断日志｜12 GPU / 系统。
约束：纯原生、零 CDN、浅色主题；表单由 `/providers` 返回的 `fields` **动态生成**，不许把 66 个字段写死在 HTML 里；保留原有 5 个板块的功能与容错逻辑。

### 10.7 本轮验收（主智能体执行）
1. 原 18 项冒烟**不回归**；
2. PUT 改 LLM provider 的 `model` → 下一次 `/chat` 的 `turn_logs.provider_snapshot.version` 递增，且 `turn_logs` 新增一行；
3. 知识库 mock 检索非空，`/chat` 的 prompt 快照含「长期记忆 / 知识片段」段落；
4. PUT 改 voice profile 的 `speed` → 列表回读生效；
5. 所有新增接口返回非 500，`/api/admin/system` 能看到 5 个 provider 与 16 项参数。

