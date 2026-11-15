# companion · 情感陪伴大脑（本期：大脑端 + 安卓端 + Web 端）

一句话：Windows 上跑两个 Python 进程，浏览器和安卓手机连同一个大脑。
**全部走真实服务**：程序里没有模拟/假数据路径。模型服务没起时，对应功能会明确报错（聊天提示模型未启动、语音提示服务未启动），绝不返回写死的假回复。

---

## 一、怎么跑起来

**直接双击两个 bat 就行**，不用手动装依赖。首次运行 bat 会自动在工程下建 `.venv` 并装好 4 个包（fastapi / uvicorn / httpx / pydantic），**不需要 CUDA、PyTorch、Ollama**。

> 为什么用 `.venv`：系统 PATH 里的 `python` 常被解析成 WorkBuddy 自带的 `dsh-runtimes` 运行时，直接 `python -m uvicorn` 会报 `No module named uvicorn`。工程自带环境绕开了这个问题。
> 手动补装： `".venv\Scripts\python.exe" -m pip install -r requirements.txt`
> 端口已被占用时 bat 不会重复起服务，会直接打开对应页面。

**要改启动项，只改工程根目录的 `config.yaml` 这一个文件**（端口、监听地址、模型服务地址、控制台口令都在里面）。改完重启两个窗口，启动时会把当前配置原样打印出来，改没生效一眼能看到。已实测：把 `BRAIN_PORT` 改成 8100，重启后读到的就是 8100。
`.env` / `.env.example` 只是旧版兼容，不用管；`config.yaml` 优先。

**双击 `1-启动全部.bat` 就行**：一个窗口同时跑大脑和控制台，关掉这个窗口就一起停。启动器会等两个端口真的起来（首次约 5~10 秒），再自动打开控制台页面，并打印一行 `[OK] 已就绪`——没看到这行就是没起来，窗口里会有原因。

> **改 bat 只有两条铁律**（2026-11-13 实测）：① 换行必须是 CRLF——LF-only 会把 `if (...)` 多行块切碎，报「系统找不到指定的路径」然后窗口闪退；② 文件必须存成 **ANSI(GBK)** 并在开头 `chcp 936`——用 UTF-8 存中文会让 cmd 读串行，后面的 `goto` 直接「找不到批处理标签」。窗口里的提示语现在全是中文，只用记事本另存为 ANSI 就对了。
> 改完跑一遍 `python tools/fix_bat_crlf.py --check`，它会把换行、编码、BOM，以及「GBK 第二字节撞上 `& | >`」的汉字全查出来。

| 文件 | 作用 |
|---|---|
| `1-启动全部.bat` | **日常就用这个**：一个窗口起大脑+控制台（8000 + 8001），关窗即全停 |
| `2-启动大脑.bat` | 只要聊天、不想要设置页时用（8000） |
| `3-启动控制台.bat` | 只改人格/音色/参数时用（8001），聊天得另开 1 号或 2 号 |
| `4-启动情感判断.bat` | 只起情感判断模型（8101），关掉即退回关键词规则 |
| `5-启动语音合成.bat` | 只起语音合成（8102），关掉后语音请求明确报错 |
| `6-启动GPT语音.bat` | 起 GPT-SoVITS 那个模型本体（9880）。**要在 5 号之前起**；没装会告诉你去哪下 |
| `7-启动语音识别.bat` | 起语音识别（8103），语音输入用。纯 CPU，不占显存；关掉就只能打字 |
| `8-看日志.bat` | 看每个服务写了什么（菜单挑、能实时刷）；也可以在控制台网页点「服务日志」 |
| `9-检查是否在运行.bat` | 只看起没起，不启动、不安装任何东西 |

**模型和大脑是分开的两件事**：大脑只负责编排，不加载任何权重。模型 bat 起没起，大脑启动横幅会一行行列出来；没起的模型对应的请求会明确报错或走规则兜底（判断走关键词规则），不会造假数据。

**日志怎么看**：每个 bat 起的服务，输出都同时写进 `data/logs/<服务名>-日期.log`（一天一个，留 30 天自动清；GPT 语音模型那份是 `GPT语音模型.log`，超过 20MB 自动清）。三个入口：

1. **bat 窗口本身**：窗口里滚什么，文件里就有什么（6 号现在也滚动显示了）
2. **控制台网页 → 左侧「服务日志」**：点服务名切换，勾「每 3 秒自动刷新」就像监视器
3. **双击 `8-看日志.bat`**：菜单挑一个，能只看最后几行，也能一直跟着刷

窗口关了、进程崩了，日志文件都还在——出了问题先翻它。

访问地址：

- 对话页：<http://127.0.0.1:8000/>
- 控制台：<http://127.0.0.1:8001/>
- 手机同一个 Wi-Fi：把 `127.0.0.1` 换成电脑局域网 IP，并在 Windows 防火墙放行 8000 / 8001。

## 二、怎么验收

```
python tools/smoke_test.py
```

本机实测 **34/34 PASS**（2026-11-14，接真实 MiniCPM5 模型后全量跑通）。验的都是能自动判定的东西，不靠肉眼看回复：

- `/health` 通、运行模式为真实服务
- 两轮 `/chat` 有流式 token、`meta` 首帧、`done` 末帧、情绪在 8 枚举内
- **第二轮 prompt 确实注入了第一轮情绪**（以服务端落库的 prompt 快照为准，这是上一轮评审要求的确定性验证）
- `emotion_events` 恰好两条、`closed=1`、能被检索命中
- 控制台改人格后**下一次 `/chat` 立即生效，不重启**（实测 version 2 → 4）
- `/tts` 返回可播放 WAV（RIFF 头，24044 字节）
- 控制台 4 个接口不 500，CORS 预检 200
- **配置中心**：6 类模型服务 / 75 项配置可读可改、改模型配置不重启即生效、知识库真实 MCP 可列库可检索、召回片段注入 prompt、8 情绪语音映射可改、每轮明细落库、语音试听出 WAV

## 三、三条线分别交了什么

| 线 | 交付 |
|---|---|
| **大脑端** | `judge / memory / llm / tts / prompt / pipeline` 六层 + `/chat` SSE + `/tts` + `/health`；判定开流前同步落库、流后只 update；`Semaphore(1)` 给 GPU 分时独占留位；断连补 `truncated` |
| **Web 端** | `index.html` 对话页（fetch 流 + `\n\n` 缓冲切帧 + 增量 UTF-8 解码 + `done` 才播 TTS + 错误保留半句）；`admin.html` 控制台（人格/参数/事件修正/判断日志/GPU 五块） |
| **安卓端** | Flutter 工程 13 个文件：`package:http` 的 `Client.send()` 解析 SSE、`ChangeNotifier` 状态、`just_audio` 播放、服务器地址可配、`usesCleartextTraffic` 已开；本期键盘输入先行，录音留了 TODO 插入点 |
| **控制平面** | 独立进程 :8001，11 条管理 API、参数值域白名单、`X-Admin-Token`、GPU 探测降级；与实时链路只共享 `data/companion.db`（进程级隔离，落实上一轮评审结论） |

## 三·补 配置中心（v2）：三层分离

你提的四点都落在这里。**模型是模型，控制大脑是控制大脑，前端是前端**：

| 层 | 职责 | 边界 |
|---|---|---|
| 模型 / 知识库 / TTS | 独立部署的服务 | 大脑**不启动、不加载、不管理**它们 |
| 大脑（:8000） | 编排 + 读配置 + 落库 | 只保存「连到哪、用什么参数、开不开」 |
| 控制台（:8001） | 唯一配置入口 | 改完**立即生效，不重启** |
| Web / 安卓前端 | 只管 UI 和音频 | 完全不感知配置 |

配置分两类，边界照小智划：**启动配置在 `config.yaml`**（端口 / 地址 / 模型服务地址 / 口令），启动读一次、改完重启；**运行时配置在 `data/companion.db`**（人格、情绪、音色、温度等），控制台改完保存即生效、不用重启，且每轮请求读一次（请求级快照）。控制台与大脑两个进程只共享这个库，浏览器端口由启动时生成的 `web/config.local.js` 告知，不再写死。

### 控制台从 5 个板块扩到 12 个
概览（服务状态卡 + 轮次统计）｜人格模板｜**模型服务**｜**知识库 / 长期记忆**｜**语音合成**｜参数与开关｜情感事件｜**会话记录**｜**事实记忆**｜**改动记录**｜情绪判断记录｜**显卡与系统**。

- **模型服务**：6 类服务（对话大模型 / 文本向量 / 情感判断 / 语音识别 / 语音合成 / 知识库），**75 个可配置项**由后端返回的字段元数据动态生成表单，不在前端写死；每项都能改、能启停、能点「连接测试」。LLM 卡含 base_url、api_key、主/备用模型、`num_gpu=999` 强制全量 GPU、num_ctx、温度、超时、force_gpu 等 13 项。
- **知识库**：模式（MCP HTTP / Webhook / 关闭）、MCP 端点与 Token、工具名、**库 ID 多选**、检索模式与条数、是否注入 prompt、自动沉淀时机与阈值、写入标签前缀、**特殊文档 ID**。带「拉取知识库列表」「检索测试」「手动沉淀」按钮。
- **TTS**：8 类情绪各自映射参考音频 / 音色 / 8 维情感向量 / 语速 / 音量，参考音频可增删，带「试听」直接出 WAV。
- **10 项运行时开关**：维护模式、是否记录 prompt / 原文、知识库注入、自动沉淀、SSE 心跳间隔、并发数等。

### 交互照小智那样收了两处
- **模型 = 一行一条**：6 类服务各占一行，行内只放「接入方式、是否启用、密钥」，其余参数点「更多参数」才展开——不是 5 张大表单卡片。
- **参数 = 一张扁平表**：所有可调项按 `llm.num_gpu` 这样的编码列成**一张表**（82 项、6 个分组），带搜索框，改完失焦即存；接口是 `GET/PUT /api/admin/flat-params`。

### 数据不再只有几行
新增 `turn_logs`（每轮 prompt 快照、回复、token 数、耗时、降级标记、当时的 provider 版本）、`config_audit`（谁改了什么）、`providers`、`voice_profiles`、`reference_audios`；控制台能按会话筛选、看事实记忆、看配置变更历史。

### 知识库已直连你的真实 MCP
从 `~/.workbuddy/mcp.json` 读到 WeKnora 端点，**已预填进配置并切到 `mcp_http` 模式**，不用手填。实测 `list_knowledge_bases` 返回 4 个库，`search_knowledge` 召回 3 条，第一条就是「我的核心设定.md」；返回的 XML 文本已做结构化解析。

> **性能边界（实测）**：WeKnora hybrid 检索单次约 **7.5–8 秒**，直接放进对话会拖慢首字。按「长期记忆本就是稳定内容」处理：热路径**缓存优先 + 后台异步刷新**，缓存落在 SQLite（两个进程共享），命中 0ms；控制台点「检索测试」才同步等完整结果并预热缓存。实测首 token **约 370ms**。WeKnora 没开时检索降级为空，**不影响对话**。
> 端点与 Token 存在本地 `data/companion.db`，控制台以密码框显示；**别把 8001 端口暴露到公网**。
> 控制台鉴权：`ADMIN_TOKEN` 留空时只允许本机回环访问（局域网请求一律 403 并提示去填口令）；填了值后所有接口都要带 `X-Admin-Token`。`api_key` / `mcp_token` 在接口返回里一律脱敏成空串，库里真值不动。对话页 `/chat`、`/tts`、`/asr` 仍不鉴权（否则手机端要同步改），如果要把 8000 开出去，请在反向代理层加认证。

## 四、接真模型（判断 / 识别已接，合成可选，LLM 用你自己的服务）

代码里所有外部依赖都是「有就用、没有就降级」：

| 组件 | 现在 | 接真的要做什么 |
|---|---|---|
| LLM | 未接（聊天会提示模型未启动） | 起你自己的模型服务，控制台「对话大模型」卡里填地址 / 模型名 / 密钥，运行方式选「独立服务」 |
| **情感判断** | **独立服务（:8101），CPU 推理** | 双击 `4-启动情感判断.bat`，见第五节 |
| 事实记忆 | SQLite bigram 兜底 | 装 `chromadb` 自动启用 |
| **TTS** | **独立服务（:8102）**，服务没起时语音请求明确报错 | 双击 `5-启动语音合成.bat`，见第五节 |

**接真模型前必须先做阶段 0 三件实测**：驱动放行 + 全量 GPU 验证、TTS 单句合成的显存与延迟、判断层 CPU 耗时。
显存口径更新（2026-11-14 实测）：TTS 已换成 GPT-SoVITS（只吃 1.3GB），qwen3:8b（Q4 约 5.2GB）+ 判断小模型 + GPT-SoVITS 同时驻留约 6.6GB，8GB 卡放得下；IndexTTS-2（5.5GB）才需要分时独占，`Semaphore(1)` 就是给那时留的。

## 五、模型服务：判断 / 语音（各自 bat 单独跑）

### 为什么拆成独立进程

| | 拆之前 | 拆之后（现在） |
|---|---|---|
| 谁加载模型 | 大脑进程 | 判断服务 / 语音服务各自加载 |
| 大脑内存 | 61MB → **1.4GB**（+1GB 权重） | **61MB**（实测） |
| 关掉模型 | 等于大脑重启 | 关掉对应 bat 即可，大脑自动降级 |
| 换模型 | 改代码 → 重启大脑 | 只重启那个模型 bat |

启动时大脑**只发 HTTP 探活**，横幅里直接告诉你谁在跑：

```
  模型服务（各自 bat 单独启动，大脑只连不加载）
  情感判断  已启动  http://127.0.0.1:8101  31ms
  语音合成  未启动  http://127.0.0.1:8102  → 需要时双击 5-启动语音合成.bat
  对话大模型 未启动  http://127.0.0.1:11434  → 需要时双击 0-启动大模型.bat
```

不确定谁没起，双击 `9-检查是否在运行.bat`（只检查、不启动）。

### 端口与地址

| 服务 | bat | 端口 | 改哪里 |
|---|---|---|---|
| 情感判断 | `4-启动情感判断.bat` | 8101 | `config.yaml` 的 `JUDGE_PORT` |
| 语音识别 | `7-启动语音识别.bat` | 8103 | `config.yaml` 的 `ASR_PORT` |
| 语音合成 | `5-启动语音合成.bat` | 8102 | `config.yaml` 的 `TTS_PORT` |
| 对话大模型 | `0-启动大模型.bat` | 11434 | Ollama 自带（模型名在控制台「对话大模型」卡里改） |
| GPT-SoVITS | `6-启动GPT语音.bat` | 9880 | `config.yaml` 的 `TTS_SOVITS_URL` |
| 大脑 / 控制台 | `1-启动全部.bat` | 8000 / 8001 | `BRAIN_PORT` / `CONSOLE_PORT` |

服务在另一台机器上时，填 `JUDGE_BASE` / `TTS_BASE` 为完整地址即可；不填就按端口自动拼 `127.0.0.1`。

### 判断服务（已装好，直接可用）

- 模型：`models/chinese-emotion-small/model.onnx`（1064MB）+ `tokenizer.json`
- 启动后 `http://127.0.0.1:8101/health` 能看到 `ready: true`、加载耗时 4.1s、单句 70ms
- 接口：`POST /judge {"text": "..."}` → `{label, confidence, intensity}`
- 它只吐**模型结果**；「低置信度用关键词复核」留在大脑侧（那里有对话上下文）

### 语音识别（已装好，纯 CPU，不占显存）

小智那条链是 **流式 ASR → LLM 对话 → 流式 TTS**。之前只有后两段，现在补上前一段：
`7-启动语音识别.bat`（:8103）把你说的话变成文字，再交给大脑。

| 项目 | 值 |
|---|---|
| 引擎 | sherpa-onnx + SenseVoice（阿里达摩院） |
| 模型 | `models/sense-voice/`，int8 量化 229MB |
| 设备 | **纯 CPU**，占显存 0；跟情感判断模型共用同一套 onnxruntime |
| 实测 | 5.6 秒中文音频 **299ms** 出字；粤语 267ms；英文 466ms |
| 语言 | 自动判断 中 / 英 / 日 / 韩 / 粤（`ASR_LANGUAGE: auto`） |

选它的理由：中文这块 SenseVoice 在开源的里最准，不用装 torch、不用显卡，
和判断模型完全同源。小智本地 ASR 也只有三条路（FunASR / sherpa-onnx / Vosk），
它那个 `sherpa_onnx_local.py` 用的就是这个引擎，模型可以共用。

装法（两行，跑一次就够；7 号 bat 也会自己检测并补上）：

```bat
".venv\Scripts\python.exe" -m pip install sherpa-onnx                    :: 引擎 20MB，不动现有依赖
".venv\Scripts\python.exe" tools\setup_asr_model.py --download           :: 模型 156MB
```

接口：

```
POST /asr    音频字节（audio/wav，也接受 ffmpeg 能解的格式）→ 下面这些
GET  /health 是否就绪、模型、加载耗时
```

返回不只有文字，还有两个白捡的信号：

- `emotion` —— **声学情绪**（HAPPY / SAD / ANGRY / FEARFUL / …），说的是「语气」，
  跟大脑里那套「文字情绪」正好互补：文字说「我没事」、声音在抖，这种才是该接住的时候
- `event` —— 声音事件（Speech / Laughter / Cry / Applause / BGM …），现在原样返回，后面能接

前端：对话页输入框左边多了个 🎤「按住说话」，松手就转成文字填进输入框。
采集走 Web Audio，前端自己编码成 16k 单声道 wav —— 不用 MediaRecorder 是因为
它给的是 webm/opus，后端还得装 ffmpeg 才能解。

**和「流式」的差别要说清**：现在这条是**整段识别**，松手后 0.3 秒出全文。
小智那种「边说边出字」要换 streaming Paraformer 模型（另 200MB）+ WebSocket 推流，
留到做 ESP32 那步再上；网页这里松手出字其实更好用。

### 语音服务（四条路，推荐 GPT-SoVITS 整合包）

`config.yaml` 里的 `TTS_BACKEND` 有四个后端：

| `TTS_BACKEND` | 干什么 | 要不要装依赖 |
|---|---|---|
| `gpt_sovits`（默认） | 连 GPT-SoVITS 整合包的 `api_v2.py`（:9880） | **不用**，它自带环境 |
| `indextts` | 本地 IndexTTS-2，8 维情感向量 | 要装，且挑 Python 版本 |
| `proxy` | 原样转发给别的 HTTP 服务 | 不用 |
| `openai` | OpenAI 兼容的 `/v1/audio/speech` | 不用 |

#### 路线 0（推荐）：GPT-SoVITS 整合包 —— **本机已装好，不用再下**

小智本地能用的语音模型只有四个——GPT-SoVITS、Fish Speech、PaddleSpeech、IndexTTS。
社区教程最多、最稳的是 GPT-SoVITS，它的 `api_v2.py` 正好就是小智
`gpt_sovits_v2.py` / `gpt_sovits_v3.py` 用的那一套接口。

**为什么选它**：整合包自带 Python + torch + 权重，解压双击就能跑，跟本工程
`.venv`（3.12，`pip` 装不动 index-tts）**完全不共用依赖**，上一轮那个死结就这么绕开了。

**当前机器上的实况**（2026-11-14 装好并验证过）：

| 项 | 值 |
|---|---|
| 目录 | `D:/GPT-SoVITS`（12.5GB，7z 包留在 `D:/_dl` 可删） |
| 自带环境 | Python 3.9.13 + torch 2.0.0+cu118 |
| 显卡 | 识别到 `NVIDIA P104-100`，走 CUDA |
| 6 号 bat | `SOVITS_DIR` 已指向该目录，**双击即可** |
| 实测 | 一句话 4~6 秒音频约 3~4 秒合成（首次含预热约 15 秒） |

```bat
:: 只有在换机器 / 目录被你挪走时，才需要改 6-启动GPT语音.bat 顶部这一行：
:: set "SOVITS_DIR=D:/GPT-SoVITS"
::
:: 换机器重装：下这个包（6.4GB，国内走 hf-mirror 最快），解压到纯英文目录即可
:: https://hf-mirror.com/lj1995/GPT-SoVITS-windows-package/resolve/main/GPT-SoVITS-v3lora-20250228.7z
```

目录不在时双击 6 号**不会报错**，它只会告诉你还没装、去哪儿下、下完改哪一行。

跑起来之后的链路：

```
大脑 :8000  →  语音服务 :8102（本工程的协议适配层）  →  GPT-SoVITS :9880（真正合成）
```

- 参考音频放 `models/voices/`，按情绪命名：`sad.wav` `happy.wav` `default.wav` …
  每个 wav 旁边放一个**同名 txt** 写这段录音的原话；规则和示例见该目录里的
  「放参考音频在这里.txt」
- 双击 `5-启动语音合成.bat` 起 8102
- 双击 `9-检查是否在运行.bat` 看全链路；断了会直接说是哪一段

情感怎么表达：**靠换参考音频**。`sad.wav` 就用难过语气录的，`happy.wav` 用开心语气录的，
模型切情绪就是切参考音频。比调向量参数稳，也不挑 v2/v3/v4 版本。

#### 路线 1：IndexTTS-2（权重已下好在 `models/IndexTTS-2/`，5.49GB）

先记一条硬约束：**index-tts 的 `pyproject.toml` 写死了 `requires-python >=3.10,<3.12`**，
Python 3.12 及以上会被 pip 直接拒绝；它也**没有 `requirements.txt`**（网上很多教程写
`pip install -r requirements.txt` 是错的，仓库里根本没这个文件）。

`.venv` 用 Python 3.11 建时，所有服务共用一个环境：

```bat
:: 1) 源码（没有 PyPI 包，只能从仓库拉；放这里会被自动加进 sys.path）
git clone https://github.com/index-tts/index-tts third_party\index-tts

:: 2) 依赖（读 pyproject，会顺带装 torch 2.8；用 3.11 的解释器）
".venv\Scripts\python.exe" -m pip install -e third_party\index-tts

:: 3) 权重——本机已下好，没复制 models/IndexTTS-2 才需要跑
".venv\Scripts\python.exe" tools\setup_tts_model.py --download

:: 4) 体检（逐项说还缺什么，也会报 Python 版本是否匹配）
".venv\Scripts\python.exe" tools\setup_tts_model.py --check
```

再把 `config.yaml` 的 `TTS_BACKEND` 改回 `indextts`。

#### 路线 2：`.venv` 已经是 3.12+（本机就是）

只能用它自带的 uv 建独立环境，那个环境的依赖项目 `.venv` 看不到，`tts_server` 没法直接 import。
所以这个环境下**别走 `indextts`**，改用路线 0。

**显存**（8GB 卡要注意）：

- GPT-SoVITS v2 半精度实测**只占 1.3GB 显存**（P104 上 `is_half=true` 正常跑），8B 大模型（Q4）约 5GB，
  两个同时驻留约 6.3GB，8GB 卡放得下。换 v3/v4 权重或大模型更吃显存时，再把
  `GPT_SoVITS/configs/tts_infer.yaml` 的 `device` 改成 `cpu`（慢一些，一句话仍然可用），或者分时启停。
- P104 是 2016 年的 Pascal 架构（算力 6.1），新版 PyTorch 的 CUDA 构建不一定还带 sm_61。
  起不来又报 CUDA 相关错误时改 `cpu` 就行，不必折腾显卡。
- IndexTTS-2 是 5.5GB 权重，`cuda:0` 上大约吃 6GB，8GB 卡和 8B 大模型基本没法同时驻留。

### 降级行为（模型没起会怎样）

| 情况 | 判断 | 识别 | 合成 | 对话 |
|---|---|---|---|---|
| 服务没起 | 关键词规则（8 类，含 fear） | 前端提示「识别没开」，只能打字 | 内置占位音 | 正常 |
| 服务起了但模型没装 | 关键词规则 | 同上，并说明缺什么 | 占位音 | 正常 |
| 服务正常 | 模型 + 关键词交叉复核 | 声学情绪 + 文字 | 真实合成 | 正常 |

三个模型互不影响：识别挂了照聊、合成挂了看字、判断挂了走关键词。

`/health` 里能分别看到 `judge_service` / `asr_service` / `tts_service` 的 `up`，控制台「模型服务」点**测试**也会说明当前实际生效的是服务还是规则。

### 复制到服务器要带什么

| 带上 | 不用带 |
|---|---|
| `brain/` `web/` `tools/` `config.yaml` `requirements.txt` `*.bat` | `.venv/`（服务器上重新建，Windows 环境不能跨机复制） |
| `models/chinese-emotion-small/`（判断模型 1GB） | `models/chinese-emotion-small/model.safetensors`（转换用的中间产物，1GB，可删） |
| `models/sense-voice/`（识别模型 229MB） | `models/sense-voice/test_wavs/`（示例音频，可删） |
| `data/`（想保留历史对话才带） | `__pycache__/` `data/logs/` |
| `models/IndexTTS-2/`（语音权重 5.5GB，已下好） | `third_party/index-tts/.git/` |

服务器首次运行任意 bat 会自动建 `.venv` 装基础依赖；**判断模型**的依赖（onnxruntime / numpy / tokenizers）在 `requirements.txt` 里；**语音模型**的 torch 不用单独装——`pip install -e third_party\index-tts` 会按它自己的 pyproject 装 torch 2.8（这条也要求 `.venv` 是 Python 3.11，见第五节路线 A）。

## 六、情感判断模型（已部署，CPU 跑，不占显存）

### 选的哪个、为什么

**`Johnson8187/Chinese-Emotion-Small`**，DeBERTa-v2 12 层 / hidden 768，FP32 ONNX 1064 MB。

选它的理由只有一条：**标签体系跟我们 8 枚举几乎一一对应**。

| 模型输出 | 本工程枚举 | 说明 |
|---|---|---|
| 平淡語氣 | neutral | |
| 關切語調 | care | |
| 開心語調 | happy | |
| 憤怒語調 | angry | |
| 悲傷語調 | sad | |
| 疑問語調 | neutral | 反问句在陪伴场景基本就是平淡语气 |
| 驚奇語調 | surprised | |
| 厭惡語調 | disgust | |
| —（模型没有） | **fear** | 由关键词层补：怕/担心/焦虑/紧张…命中即覆盖 |

被否掉的两个候选：
- `Johnson8187/Chinese-Emotion`（XLM-R **large**，2136 MB，24 层）：CPU 单句 400–600ms，判断在开流前同步执行，这个延迟会直接压到首字延迟上，不可接受；
- `LXDaugh/chinese-6-emotion-model`（BERT-base，390 MB，6 类）：速度最好，但没有 `care` 和 `disgust`，而 `care` 在陪伴场景是高频类，缺了等于把「谢谢你」判成 happy。

### 装一次要多久、装了什么

```bat
.venv\Scripts\python.exe tools\setup_judge_model.py
```

脚本会自动做三件事：下载权重 → 转 ONNX → 跑自检。中断了重跑即可（已存在的文件跳过）。

- **运行期依赖只有三个**：`onnxruntime` + `numpy` + `tokenizers`（分词器只读 `tokenizer.json`，不拖 transformers/torch）
- **torch / transformers 只在转换那一步用**，导完以后可以不管，生产链路不 import
- 模型文件（`models/chinese-emotion-small/`）：`model.onnx` 1064 MB + `tokenizer.json` 16 MB + 几个 config

要重新转换（换了模型、改了长度）：

```bat
.venv\Scripts\python.exe tools\export_onnx.py
```

> 转换有两个坑，脚本里已绕开，改代码前先看注释：
> 1. torch 2.15 的旧导出路径不认 `dynamic_axes` 的 dict 写法，会报 `_jit_pass_onnx_set_dynamic_input_shape` 参数不兼容；DeBERTa-v2 的解耦注意力本身还带动态控制流。所以**导出成固定长度 48 的静态图**（陪伴场景单句极少超过 48 字），ORT 上还能多吃常量折叠。
> 2. 必须 FP32。FP16 在 ORT CPU 上会插 Cast 节点把权重转回 FP32，既多一次拷贝又拿不到收益。

### 实测数据（本机 8 核 CPU，38GB 内存）

| 指标 | 数值 |
|---|---|
| 单句推理 | 中位 **71 ms**，P90 80 ms（4 线程；1 线程 136ms、8 线程反而 115ms） |
| 首次推理 | 约 4.1 s（图优化 + 加载 1GB 权重）→ **判断服务启动时同步预热**，服务端口通了就等于模型可用 |
| 15 句人工样例 | 模型 11/15；叠加关键词复核后 13/15 |
| 占用 | 判断服务进程约 1.4GB 内存；**大脑进程只有 61MB**（实测）；显存 0 |

延迟是 4 线程测出来的。`brain/services/sentiment_model.py` 里 `_INTRA_THREADS` 别随手调大，8 核上 8 线程比 4 线程更慢（超线程争抢）。

### 模型 + 关键词是怎么配合的

这个模型学的是**语气**（平淡/关切/疑问/惊奇），不是情绪内容。所以「最近工作压力很大，晚上总睡不好」这种陈述句，它会高置信度判成**平淡語氣**——单靠模型会漏掉真实情绪。

`judge.py` 的 `_low_confidence_fallback` 做了三条交叉复核：

1. **模型置信度 < 0.65** → 关键词复核。实测翻车的都是这类：「烦死了，别问了」判 care(0.58)、「我不想说话」判 disgust(0.45)。
2. **模型判 neutral，但句子里有 ≥2 个情绪词** → 以关键词为准。门槛定 2 个词是为了放过「不累」「别担心」这类一两个词的反向表达。
3. **安慰短语**（别担心/别怕/早点休息/谢谢你/感谢你）→ 无条件判 care。否则安慰会被回成恐惧，连 TTS 音色都会选错。

反过来，关键词也有些地方明显不如模型，实际的坑：
- 「天气」的「气」曾命中愤怒规则 → 规则表里**不能放单字「气」**，已改成「生气/气死/火大」这类双字词；
- 命中并列时按 **(命中词数, 最长命中词长度)** 排序，「别担心」才能压过「担心」。

### 怎么调、怎么看

- 控制台 →「模型服务」→ 情感判断 → **测试**：会直接告诉你现在是「判断服务生效」「本机模型生效」还是「走关键词规则」，附服务延迟和模型体积；
- 控制台 →「运行时参数」→ 搜 `judge`：
  - `engine`：`http`（默认，调 :8101 的判断服务）/ `auto`（服务优先，服务没起用本机模型）/ `onnx`（只本机）/ `rule`（只关键词）
  - `endpoint`：判断服务地址，留空 = `JUDGE_BASE + /judge`
  - `confidence_floor`：低置信度回退阈值，默认 0.65
  - `label_map`：模型输出 → 枚举的覆盖表，JSON
- `GET /health` 里 `judge` 报**实际生效的引擎**（`http` / `onnx` / `rule`），并附 `judge_service.up`；
- 命令行对比模型与规则：`.venv\Scripts\python.exe tools\judge_check.py`

### 已知限制（如实列）

1. **fear 不是模型学会的**，是关键词补的。所以「有点慌」能判对，「心里空落落的」这种没有 fear 词的说法，模型只会给 neutral 或 sad；
2. **反讽和隐喻判不了**。「这也能行？」模型给 neutral，人知道那是质疑；
3. 模型标签是 `LABEL_0..7` 占位符，语义来自模型卡 README 的 `label_mapping`。**换模型必须重新核对这层映射**，否则 8 个类会整体错位；
4. 模型卡标 **MIT**，但底座是 `joeddav/xlm-roberta-large-xnli`（XLM-R 系，MIT）。真要商用前建议自己再核一遍许可证链。

## 七、安卓端怎么跑（**本机无 Flutter SDK，未编译验证**）

```
cd client
flutter pub get
flutter create --platforms android .     # 若缺 gradle wrapper
flutter run
```

App 顶部「设置」里把服务器地址改成 `http://192.168.x.x:8000`。
本期只做：键盘输入 → 流式回复 → 情绪语音播放。**录音 / ASR / 唤醒没做**，`RECORD_AUDIO` 只是预留权限。

## 八、本期明确没做

Windows 客户端、ESP32、录音 / ASR、唤醒词、全双工、LLM 与 TTS 的真实接入、完整 RBAC。
（情感判断模型的真实接入已完成，见第五节。）
这些按上一轮路线排在阶段 5 之后，不在这次范围里。

## 九、数据与重置

所有状态在 `data/companion.db`（SQLite，WAL）。库里目前有冒烟测试产生的情感事件与判断日志，控制台里可以直接看到效果。
想从零开始：**停掉两个服务 → 删掉这个文件 → 重新双击两个 bat**。人格模板已恢复为「默认陪伴者」。

## 十、已知边界（如实列出）

- `/api/admin/overview` 的 health 代理地址已改为跟随 `config.yaml` 的 `BRAIN_PORT`，换端口不用改代码。
- `/health` 的 `judge` 字段已改成**实际生效的引擎**（读 `engine_status`）；`llm/tts/memory` 仍是按 `MOCK` 推断，没探测真实组件是否可用。
- 控制平面侧 SQLite 异常未包 try（契约只要求实时链路兜底）。
- SSE 心跳是「空闲 15 秒发一次 `: ping`」，持续出 token 时不插帧。
- 安卓工程未跑 `flutter analyze` / 真机调试；Gradle Wrapper 二进制无法用文本生成，需用 SDK 补齐。
- 真实 MCP 检索约 8 秒，故热路径走共享缓存（见上文）；缓存 TTL 10 分钟，过期后首轮不注入知识片段但**不阻塞**，后台自动刷新。
- `mcp_http` 模式的端点与 Token 以明文存本地 SQLite（控制台密码框显示），仅限本机 / 可信局域网使用。
- `judge.engine=http` 目前降级为规则判断；`llm.fallback_model` 仅记录、未做 OOM 自动切换；这两处是接真模型时要补的。
